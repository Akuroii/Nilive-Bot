// ═══════════════════════════════════════════════════════════════
// NERO DASHBOARD — embed/validate.js
// Message Builder v2 — phase 1, step 6a: the validation engine.
//
// WHAT THIS FILE IS
//   A PURE function of (document, limits). It takes the store's normalized
//   MessageDocument and the limits table the SERVER rendered into the page
//   (utils/discord_limits.limits_payload(), the one authority) and returns the
//   list of issues:
//
//       { code, path, nodeId, severity, message }
//
//   No DOM, no timers, no storage, no network and no module state: the same
//   document and the same limits always produce the same list, and a second
//   call cannot see anything the first one left behind. The page owns WHEN this
//   runs and WHERE the result goes — never this file.
//
//   It also MEASURES what the views display (step 6b): counts() reports each
//   node's used/max numbers and caps() reports whether a node may still grow.
//   Both are projections of the same measurement pass the rules run on, so a
//   counter and its rule can never disagree, and no view re-implements a limit.
//   Measuring is still not drawing: this file creates no element, ever.
//
// WHERE IT SITS (the whole step-6 flow, one implementation each)
//
//       served limits → validate(document, limits) → store.ui.issues
//                                                    → #mb2-strip (6a)
//                    → counts()/caps()              → counters + add caps (6b)
//                                                    → rail badges (6b, from the
//                                                      same store.ui.issues)
//
//   `ui.issues` is the ONE issue list and it already existed in
//   embed/store.js (`ui/setIssues`), so this step adds no state, no second
//   copy, no persistence and no UI/state redesign.
//
// NO LIMIT IS HARD-CODED HERE
//   Every number comes from the served table. ensureLimits() names the keys the
//   rules below actually consume, and validate() refuses to run without them:
//   a missing or unusable table yields ONE explicit issue, never a silent
//   "no limit". (The harness mutates the table — a non-default table must
//   change the outcome — and asserts validate.js contains no limit literal.)
//
// WORDING AUTHORITY (approved decision 7)
//   Where the server already has the rule (utils/embed_schema.py) its message
//   is reproduced WORD FOR WORD, so client and server explain the same problem
//   the same way. Rules the editor can see and the send-time gate cannot are
//   marked "client-only" and listed in the harness header. No second message
//   table is introduced.
//
// DELIBERATE DIVERGENCES (documented, not accidents)
//   * `attachment://` is a WARNING here and an ERROR server-side: phase 1 has
//     no uploads at all, so such a reference is unfinished work rather than a
//     mistake. The send-time gate keeps its own severity.
//   * An empty field NAME is a warning here (embed/model.js sends the
//     zero-width placeholder Discord accepts) and an error server-side.
//   * An empty document is CLEAN here — "nothing yet" is the state a new page
//     starts in, and the strip stays out of the way. The server's
//     "Nothing to send" error belongs to send time, which phase 1 does not have.
//   * Timestamps: the forms this builder produces (and plain dates) are
//     accepted; exotic ISO spellings Python's fromisoformat also accepts are
//     not, so the client can only ever be STRICTER than the server.
//
// Consumed by: embed/message-builder-page.js (which dispatches the result)
// Tested by:   scripts/test_message_builder_validate.js (rules, limits,
//              determinism, severity, paths) and
//              scripts/test_message_builder_page.js (end-to-end: document →
//              issues → strip, coalescing, change-guarded writes).
// ═══════════════════════════════════════════════════════════════
window.NERO = window.NERO || {};
window.NERO.embed = window.NERO.embed || {};

(function (NERO) {
    'use strict';

    // Loaded before this file (data-page-script order in
    // manage/message_builder.html). Used for the two shared vocabulary
    // questions — "does this embed carry anything?" and "what URL would this
    // media slot send?" — so those answers are never reimplemented here.
    const model = NERO.embed.model;

    // 7c: the asset rules read the record/reference/fact vocabulary from
    // embed/assets.js — the canonical record shape lives there and nowhere
    // else. Loaded before this file (data-page-script order in
    // manage/message_builder.html), and only ever read, never re-implemented.
    const assets = NERO.embed.assets;

    /**
     * The rail's / inspector's node id for the message root. The validator
     * reports a nodeId for every issue so a later step can jump to it, and the
     * message-level rules belong to that node. The harness asserts this equals
     * rail.CONTENT_NODE and inspector.CONTENT_NODE — one vocabulary, no drift.
     */
    const CONTENT_NODE = 'content';

    const ERROR = 'error';
    const WARNING = 'warning';

    /**
     * The limit keys the rules below consume, in payload order. This list is
     * the contract with utils/discord_limits.limits_payload(): if the server
     * stops serving one of these, validation must FAIL EXPLICITLY instead of
     * treating the missing number as "no limit".
     */
    const REQUIRED_LIMITS = [
        ['message', 'content_max'],
        ['message', 'embeds_max'],
        ['message', 'embed_total_chars_max'],
        // 7c: the two keys the asset count/size rules measure with. They are
        // REQUIRED for the same reason as the rest: a missing number must be
        // an explicit failure, never silence and never a client-side guess.
        ['attachments', 'count_max'],
        ['attachments', 'total_bytes_max'],
        ['embed', 'title_max'],
        ['embed', 'description_max'],
        ['embed', 'fields_max'],
        ['embed', 'field_name_max'],
        ['embed', 'field_value_max'],
        ['embed', 'footer_text_max'],
        ['embed', 'author_name_max'],
    ];

    // ── Small pure helpers ───────────────────────────────────────
    function isObject(value) {
        return !!value && typeof value === 'object' && !Array.isArray(value);
    }

    /** Times a text field, exactly as utils/embed_schema.py's _text_len does. */
    function textLen(value) {
        return typeof value === 'string' ? value.length : 0;
    }

    /**
     * The client's mirror of the server's _is_http_url(): a scheme of http or
     * https AND a non-empty host.
     */
    function isHttpUrl(value) {
        if (typeof value !== 'string') return false;
        const raw = value.trim();
        if (!raw) return false;
        const match = /^([a-zA-Z][a-zA-Z0-9+.-]*):\/\/([^\s/?#]+)/.exec(raw);
        return !!match && /^https?$/i.test(match[1]) && match[2].length > 0;
    }

    function isAttachmentRef(value) {
        return typeof value === 'string' && value.indexOf('attachment://') === 0;
    }

    /** A media value that carries an uploaded file (rather than a URL). */
    function isUploadValue(value) {
        return !!value && typeof value === 'object' && value.kind === 'upload';
    }

    function daysInMonth(year, month) {
        if (month === 2) {
            const leap = (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
            return leap ? 29 : 28;
        }
        return [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1];
    }

    /**
     * The client's mirror of the server's _valid_timestamp(). Deliberately
     * explicit rather than "whatever Date.parse accepts": engines accept
     * 2026-02-31 (V8 rolls it over) and other spellings Python rejects, so the
     * shape and the ranges are checked here and Date.parse is only the final
     * sanity check.
     */
    function isIsoTimestamp(value) {
        if (typeof value !== 'string') return false;
        const raw = value.trim();
        if (!raw) return false;
        const match = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,]\d{1,9})?)?\s*(?:Z|z|[+-]\d{2}:?\d{2})?)?$/.exec(raw);
        if (!match) return false;
        const year = Number(match[1]);
        const month = Number(match[2]);
        const day = Number(match[3]);
        if (month < 1 || month > 12) return false;
        if (day < 1 || day > daysInMonth(year, month)) return false;
        if (match[4] !== undefined) {
            if (Number(match[4]) > 23 || Number(match[5]) > 59) return false;
            if (match[6] !== undefined && Number(match[6]) > 59) return false;
        }
        const normalized = raw.replace(/^(\d{4})-(\d{2})-(\d{2}) /, '$1-$2-$3T').replace(/[zZ]$/, '+00:00');
        return isFinite(Date.parse(normalized));
    }

    // ── The limits table ─────────────────────────────────────────
    /**
     * Is this limits table usable? Returns { ok, missing: ['message.content_max', …] }.
     * Zero is a legitimate limit; undefined, null, NaN and non-numbers are not.
     */
    function ensureLimits(limits) {
        const missing = [];
        for (let i = 0; i < REQUIRED_LIMITS.length; i++) {
            const block = REQUIRED_LIMITS[i][0];
            const key = REQUIRED_LIMITS[i][1];
            const section = isObject(limits) ? limits[block] : null;
            const value = isObject(section) ? section[key] : undefined;
            if (typeof value !== 'number' || !isFinite(value) || value < 0) {
                missing.push(block + '.' + key);
            }
        }
        return { ok: missing.length === 0, missing: missing };
    }

    /**
     * The ONE issue that stands in for every rule when the table itself never
     * arrived. It is an error (not a silent pass): the user is told why nothing
     * has been checked, instead of the page pretending the message is fine.
     */
    function limitsIssue() {
        return {
            code: 'limits.missing',
            path: '',
            nodeId: CONTENT_NODE,
            severity: ERROR,
            message: "Discord's limits did not reach this page, so nothing here has been checked against them. Reload to try again.",
        };
    }

    // ── Issue accumulation ───────────────────────────────────────
    function push(issues, code, path, nodeId, severity, message) {
        issues.push({
            code: code,
            path: path,
            nodeId: nodeId,
            severity: severity,
            message: message,
        });
    }

    /**
     * One URL-ish slot, exactly as utils/embed_schema.py's _check_media_url
     * does it: an `attachment://` reference (a warning here, see the header) or
     * an http(s) URL. `wire` is what would actually be sent.
     */
    function checkMedia(issues, slot, wire) {
        if (!wire) return;
        // 7c: an `{kind:'upload'}` slot is described by the asset rules (it
        // needs a record, and the record needs bytes) — this rule is about
        // URL values, including the legacy `attachment://name` string, and
        // firing it on a linked upload would say "no file is being uploaded"
        // about a file that IS. One problem, one message.
        if (slot.upload) return;
        if (isAttachmentRef(wire)) {
            const name = wire.slice('attachment://'.length);
            const message = name
                ? slot.label + ' points at the attachment "' + name + '", but no file with that name is being uploaded — reattach the file before sending.'
                : slot.label + ' references an attachment with no file name.';
            push(issues, slot.code + '.attachment-missing', slot.path, slot.nodeId, WARNING, message);
            return;
        }
        if (!isHttpUrl(wire)) {
            push(issues, slot.code + '.url-invalid', slot.path, slot.nodeId, ERROR,
                slot.label + ' must start with http:// or https://.');
        }
    }

    // ── The rules, per embed ─────────────────────────────────────
    /**
     * Mirrors validate_embed(): the same rule order, the same paths
     * (`embeds.<i>.title`, …) and the same wording. Client-only rules are
     * marked and come last, so the server-mirrored order is preserved.
     */
    function validateEmbed(issues, embed, index, context) {
        const limits = context.limits;
        const prefix = 'embeds.' + index;
        const label = 'Embed ' + (index + 1);
        const nodeId = embed && embed.id !== undefined && embed.id !== null ? String(embed.id) : null;
        const e = isObject(embed) ? embed : {};
        const author = isObject(e.author) ? e.author : {};
        const footer = isObject(e.footer) ? e.footer : {};

        if (textLen(e.title) > limits.embed.title_max) {
            push(issues, 'embed.title.too-long', prefix + '.title', nodeId, ERROR,
                label + ' title is ' + textLen(e.title) + ' characters; Discord\'s limit is ' + limits.embed.title_max + '.');
        }

        if (textLen(e.description) > limits.embed.description_max) {
            push(issues, 'embed.description.too-long', prefix + '.description', nodeId, ERROR,
                label + ' description is ' + textLen(e.description) + ' characters; Discord\'s limit is ' + limits.embed.description_max + '.');
        }

        const url = typeof e.url === 'string' ? e.url : '';
        if (url && !isHttpUrl(url) && !isAttachmentRef(url)) {
            push(issues, 'embed.url.invalid', prefix + '.url', nodeId, ERROR,
                label + ' URL must start with http:// or https://.');
        }

        const timestamp = typeof e.timestamp === 'string' ? e.timestamp : '';
        if (timestamp && !isIsoTimestamp(timestamp)) {
            push(issues, 'embed.timestamp.invalid', prefix + '.timestamp', nodeId, ERROR,
                label + ' timestamp must be an ISO 8601 date/time (e.g. 2026-09-23T18:00:00+00:00).');
        }

        // ── Author ──
        const authorName = typeof author.name === 'string' ? author.name : '';
        if (textLen(authorName) > limits.embed.author_name_max) {
            push(issues, 'embed.author.name.too-long', prefix + '.author.name', nodeId, ERROR,
                label + ' author name is ' + textLen(authorName) + ' characters; Discord\'s limit is ' + limits.embed.author_name_max + '.');
        }
        const authorUrl = typeof author.url === 'string' ? author.url : '';
        const authorIcon = model.mediaToWireUrl(author.icon);
        if ((authorUrl || authorIcon) && !textLen(authorName)) {
            push(issues, 'embed.author.name.required', prefix + '.author.name', nodeId, ERROR,
                label + ' author name is required when an author icon or link is set.');
        }
        if (authorUrl && !isHttpUrl(authorUrl)) {
            push(issues, 'embed.author.url.invalid', prefix + '.author.url', nodeId, ERROR,
                label + ' author link must start with http:// or https://.');
        }
        checkMedia(issues, {
            code: 'embed.author.icon', path: prefix + '.author.icon_url', nodeId: nodeId,
            label: label + ' author icon', upload: isUploadValue(author.icon),
        }, authorIcon);

        // ── Footer ──
        const footerText = typeof footer.text === 'string' ? footer.text : '';
        if (textLen(footerText) > limits.embed.footer_text_max) {
            push(issues, 'embed.footer.text.too-long', prefix + '.footer.text', nodeId, ERROR,
                label + ' footer text is ' + textLen(footerText) + ' characters; Discord\'s limit is ' + limits.embed.footer_text_max + '.');
        }
        const footerIcon = model.mediaToWireUrl(footer.icon);
        if (footerIcon && !textLen(footerText)) {
            push(issues, 'embed.footer.text.required', prefix + '.footer.text', nodeId, ERROR,
                label + ' footer text is required when a footer icon is set.');
        }
        checkMedia(issues, {
            code: 'embed.footer.icon', path: prefix + '.footer.icon_url', nodeId: nodeId,
            label: label + ' footer icon', upload: isUploadValue(footer.icon),
        }, footerIcon);

        // ── Media ──
        checkMedia(issues, {
            code: 'embed.image', path: prefix + '.image.url', nodeId: nodeId,
            label: label + ' image', upload: isUploadValue(e.image),
        }, model.mediaToWireUrl(e.image));
        checkMedia(issues, {
            code: 'embed.thumbnail', path: prefix + '.thumbnail.url', nodeId: nodeId,
            label: label + ' thumbnail', upload: isUploadValue(e.thumbnail),
        }, model.mediaToWireUrl(e.thumbnail));

        // ── Fields ──
        const fields = Array.isArray(e.fields) ? e.fields : [];
        if (fields.length > limits.embed.fields_max) {
            push(issues, 'embed.fields.too-many', prefix + '.fields', nodeId, ERROR,
                label + ' has ' + fields.length + ' fields; Discord\'s limit is ' + limits.embed.fields_max + '.');
        }
        for (let i = 0; i < fields.length; i++) {
            const field = isObject(fields[i]) ? fields[i] : {};
            const fpath = prefix + '.fields.' + i;
            const flabel = label + ' field ' + (i + 1);
            const fnode = field.id !== undefined && field.id !== null ? String(field.id) : nodeId;
            const name = typeof field.name === 'string' ? field.name : '';
            const value = typeof field.value === 'string' ? field.value : '';
            if (!textLen(name)) {
                // client severity (see the header): the wire carries the
                // zero-width placeholder, so Discord accepts it — the server
                // gate is stricter at send time.
                push(issues, 'embed.field.name.missing', fpath + '.name', fnode, WARNING,
                    flabel + ' needs a name.');
            } else if (textLen(name) > limits.embed.field_name_max) {
                push(issues, 'embed.field.name.too-long', fpath + '.name', fnode, ERROR,
                    flabel + ' name is ' + textLen(name) + ' characters; Discord\'s limit is ' + limits.embed.field_name_max + '.');
            }
            if (textLen(value) > limits.embed.field_value_max) {
                push(issues, 'embed.field.value.too-long', fpath + '.value', fnode, ERROR,
                    flabel + ' value is ' + textLen(value) + ' characters; Discord\'s limit is ' + limits.embed.field_value_max + '.');
            } else if (textLen(name) > 0 && !textLen(value)) {
                // client-only. Gated on a NAMED field on purpose: a field the
                // user has not filled in at all is reported once ("needs a
                // name"), not twice — an empty value is what Discord shows as
                // a blank line, which is worth saying only about a field the
                // user clearly meant to write.
                push(issues, 'embed.field.value.missing', fpath + '.value', fnode, WARNING,
                    flabel + ' has no value — Discord will show it empty.');
            }
        }

        // ── The per-embed character budget (the server checks it last too) ──
        const total = embedCharCount(e, fields);
        if (total > limits.message.embed_total_chars_max) {
            push(issues, 'embed.total-chars', prefix, nodeId, ERROR,
                label + ' holds ' + total + ' characters in total; Discord\'s per-embed limit across all of its text is ' +
                limits.message.embed_total_chars_max + '.');
        }

        // ── client-only: an embed that would silently not be sent ──
        // Two conditions, both about not crying wolf:
        //   * the message must have something else to send (on a page nobody
        //     has typed into, an empty embed is the starting state), and
        //   * there must be MORE THAN ONE embed, because the page itself
        //     starts with one empty embed — reporting that one the moment the
        //     user types a message would make the strip permanent noise
        //     instead of a signal.
        // With two or more embeds an empty one is a row the user created and
        // left behind, and toWireEmbeds() really does drop it from the wire.
        if (context.embedCount > 1 && context.messageHasContent && !model.embedHasContent(e)) {
            push(issues, 'embed.unused', prefix, nodeId, WARNING,
                label + ' is empty and will not be sent.');
        }
    }

    /** The server's embed_char_count(): the same six text sources. */
    function embedCharCount(embed, fields) {
        let total = textLen(embed.title) + textLen(embed.description);
        if (isObject(embed.footer)) total += textLen(embed.footer.text);
        if (isObject(embed.author)) total += textLen(embed.author.name);
        for (let i = 0; i < fields.length; i++) {
            total += textLen(fields[i].name) + textLen(fields[i].value);
        }
        return total;
    }

    // ── The facts the views display (step 6b) ────────────────────
    /**
     * ONE measurement pass over (document, limits). Every number 6b shows — the
     * character counters, the embed/field caps — comes from here, and it is
     * measured with the SAME helpers the rules above use (textLen,
     * embedCharCount), so "the counter says over" and "the validator says
     * too long" can never disagree: they are the same comparison.
     *
     * Pure like everything else in this file: no DOM, no state, and an unusable
     * table yields `ok: false` with no facts at all. The views fail CLOSED on
     * that (counters blank, add controls disabled) rather than showing a page
     * that "has no limits".
     *
     * Shape:
     *   { ok, message: { content, embeds },
     *     embeds: [ { id, index, label,
     *                 controls: [ {key, used, max}, … ],   // fixed order
     *                 fields:   { used, max, canAdd },
     *                 fieldCounts: [ { id, index, label, controls: [ … ] } ] } ] }
     */
    function measure(document_, limits) {
        const check = ensureLimits(limits);
        const doc = isObject(document_) ? document_ : {};
        const embeds = Array.isArray(doc.embeds) ? doc.embeds : [];
        if (!check.ok) return { ok: false, message: null, embeds: [] };

        const out = {
            ok: true,
            message: {
                content: { key: 'content', used: textLen(doc.content), max: limits.message.content_max },
                embeds: { key: 'embeds', used: embeds.length, max: limits.message.embeds_max },
            },
            embeds: [],
        };

        for (let i = 0; i < embeds.length; i++) {
            const e = isObject(embeds[i]) ? embeds[i] : {};
            const fields = Array.isArray(e.fields) ? e.fields : [];
            const author = isObject(e.author) ? e.author : {};
            const footer = isObject(e.footer) ? e.footer : {};
            const fieldCounts = [];

            for (let j = 0; j < fields.length; j++) {
                const f = isObject(fields[j]) ? fields[j] : {};
                fieldCounts.push({
                    id: f.id === undefined || f.id === null ? null : String(f.id),
                    index: j,
                    label: 'Field ' + (j + 1),
                    controls: [
                        { key: 'field.name', used: textLen(f.name), max: limits.embed.field_name_max },
                        { key: 'field.value', used: textLen(f.value), max: limits.embed.field_value_max },
                    ],
                });
            }

            out.embeds.push({
                id: e.id === undefined || e.id === null ? null : String(e.id),
                index: i,
                label: 'Embed ' + (i + 1),
                // Fixed order — the inspector paints counters by key, the tests
                // assert the order, and nothing here depends on object key order.
                controls: [
                    { key: 'title', used: textLen(e.title), max: limits.embed.title_max },
                    { key: 'description', used: textLen(e.description), max: limits.embed.description_max },
                    { key: 'author.name', used: textLen(author.name), max: limits.embed.author_name_max },
                    { key: 'footer.text', used: textLen(footer.text), max: limits.embed.footer_text_max },
                    { key: 'total', used: embedCharCount(e, fields), max: limits.message.embed_total_chars_max },
                    { key: 'fields', used: fields.length, max: limits.embed.fields_max },
                ],
                fields: {
                    used: fields.length,
                    max: limits.embed.fields_max,
                    canAdd: fields.length < limits.embed.fields_max,
                },
                fieldCounts: fieldCounts,
            });
        }
        return out;
    }

    /** One measurement entry, with the comparison the views paint. */
    function readout(entry) {
        return { key: entry.key, used: entry.used, max: entry.max, over: entry.used > entry.max };
    }

    /**
     * counts(document, limits) → { ok, nodes: { <nodeId>: [ {key, used, max, over} ] }, message }
     * The per-node counters, addressed by the SAME node ids the issues carry, so
     * a view can paint the node it is showing without deriving anything.
     */
    function counts(document_, limits) {
        const m = measure(document_, limits);
        const nodes = {};
        if (!m.ok) return { ok: false, nodes: nodes, message: null };

        nodes[CONTENT_NODE] = [readout(m.message.content)];
        m.embeds.forEach(function (embed) {
            if (embed.id !== null) nodes[embed.id] = embed.controls.map(readout);
            embed.fieldCounts.forEach(function (field) {
                if (field.id !== null) nodes[field.id] = field.controls.map(readout);
            });
        });
        return { ok: true, nodes: nodes, message: { embeds: readout(m.message.embeds) } };
    }

    /**
     * caps(document, limits) → { ok, embeds: {used, max, canAdd},
     *                            fields: { <embedId>: {used, max, canAdd} } }
     *
     * `canAdd` is `used < max` — adding is refused exactly AT the cap, while the
     * rules above only complain ABOVE it. Both are true at once by design: a
     * full embed is not an error, it is a control that has nothing left to do.
     * An unusable table can only ever answer "cannot add" (fail closed).
     */
    function caps(document_, limits) {
        const m = measure(document_, limits);
        const fields = {};
        if (!m.ok) {
            return { ok: false, embeds: { used: 0, max: 0, canAdd: false }, fields: fields };
        }
        m.embeds.forEach(function (embed) {
            if (embed.id !== null) {
                fields[embed.id] = { used: embed.fields.used, max: embed.fields.max, canAdd: embed.fields.canAdd };
            }
        });
        return {
            ok: true,
            embeds: {
                used: m.message.embeds.used,
                max: m.message.embeds.max,
                canAdd: m.message.embeds.used < m.message.embeds.max,
            },
            fields: fields,
        };
    }

    // ── Assets (phase 2, step 7c) ────────────────────────────────
    /**
     * THE ASSET RULES. They run LAST — after the message and after every embed
     * — so the first issue a reader sees is still the first problem in reading
     * order, and they read exactly two inputs:
     *
     *   • the DOCUMENT's own metadata, through embed/assets.js: references,
     *     records, ids, byte counts. No second record model lives here. This
     *     file owns wording, severity, order and the limits conversation —
     *     never the vocabulary for what a record or a reference IS.
     *   • the FACTS the page probed from the byte store. Facts are
     *     OBSERVATIONS, not document content.
     *
     * WHAT THIS CANNOT DO, BY CONSTRUCTION: open storage, read bytes, hash a
     * file, mint a URL, mutate the document, or mutate the facts. A missing
     * asset, an unreachable store and an asset nobody has looked at are three
     * different states: a missing file warns, an unreachable store warns
     * differently, and an unobserved asset stays SILENT (assuming either way
     * is how an editor tells someone their image is broken when it is not).
     *
     * Client-only rules are marked below: the server's embed_schema.py has no
     * asset concepts, so nothing here claims to mirror a server message.
     */

    /** `<label>` for a media slot, from the reference's own slot name. */
    function slotLabel(ref) {
        if (!ref) return 'A file slot';
        if (ref.slot === 'image') return 'An image';
        if (ref.slot === 'thumbnail') return 'A thumbnail';
        if (ref.slot === 'author.icon') return 'The author icon';
        if (ref.slot === 'footer.icon') return 'The footer icon';
        return 'A file slot';
    }

    /** Where an asset-level issue points: the first slot that uses it. */
    function assetPath(refs, assetId) {
        for (let i = 0; i < refs.length; i++) {
            if (refs[i].assetId === assetId) return refs[i];
        }
        return null;
    }

    /** 'A file that Discord can show in an embed: .jpg, .jpeg, …' (one table). */
    function allowedExtensions() {
        return assets.ALLOWED_EXTENSIONS.map(function (ext) { return '.' + ext; }).join(', ');
    }

    /**
     * One referenced asset's metadata: is it described well enough to send?
     * Everything here is document-only (no facts), so it is checkable the
     * moment a document loads, before any probe has run.
     */
    function checkAssetRecord(issues, view, assetId, limits) {
        const ref = assetPath(view.refs, assetId);
        const path = ref ? ref.path : 'assets.' + assetId;
        const nodeId = ref && ref.embedId ? ref.embedId : CONTENT_NODE;
        const record = view.records[assetId];

        if (!record) {
            // A slot points at an asset the document does not describe. The
            // reference alone can never become an upload: there is no file
            // name, no type and no size for it anywhere.
            push(issues, 'assets.record-missing', path, nodeId, ERROR,
                slotLabel(ref) + ' points at a file this message does not carry. Add the file again.');
            return;
        }

        const ext = assets.filenameExtension(record.filename);
        const known = ext && Object.prototype.hasOwnProperty.call(assets.MIME_BY_EXTENSION, ext);
        if (!known) {
            push(issues, 'assets.extension-not-allowed', path, nodeId, ERROR,
                'Embed images must be one of: ' + allowedExtensions() + ' — "' + record.filename + '" is not.');
        } else if (record.mime && assets.MIME_BY_EXTENSION[ext] !== record.mime) {
            push(issues, 'assets.format-mismatch', path, nodeId, WARNING,
                'The file name says .' + ext + ' but the file is recorded as ' + assets.mimeLabel(record.mime) + '.');
        } else if (!record.mime) {
            push(issues, 'assets.mime-unknown', path, nodeId, WARNING,
                'There is no content type recorded for "' + record.filename + '", so Discord may not show it.');
        }
    }

    /**
     * What the byte store observed, and whether it agrees with the record.
     * `facts` is the product of assets.assetFacts() (or null): states keyed by
     * id, rows for the id that were probed. Nothing is hashed here — a plain
     * probe cannot prove bytes ARE the bytes, so corruption is only ever
     * reported when the store said so, and the record/row comparison below
     * catches the disagreements that need no digest at all.
     */
    function checkAssetFacts(issues, view, assetId, facts) {
        const record = view.records[assetId];
        const ref = assetPath(view.refs, assetId);
        const path = ref ? ref.path : 'assets.' + assetId;
        const nodeId = ref && ref.embedId ? ref.embedId : CONTENT_NODE;
        const name = record && record.filename ? '"' + record.filename + '"' : 'A file in this message';
        const state = (facts && facts.states) ? facts.states[assetId] : null;

        if (state === assets.FACT_STATES.CORRUPT) {
            push(issues, 'assets.bytes-corrupt', path, nodeId, ERROR,
                'The stored copy of ' + name + ' is damaged, so it cannot be attached. Add the file again.');
            return;                       // a damaged entry has nothing to compare against
        }
        if (state === assets.FACT_STATES.MISSING) {
            push(issues, 'assets.bytes-missing', path, nodeId, WARNING,
                'The bytes of ' + name + ' are no longer stored in this browser, so it cannot be attached. Add the file again.');
            return;
        }
        if (state === assets.FACT_STATES.UNAVAILABLE) {
            push(issues, 'assets.bytes-unavailable', path, nodeId, WARNING,
                'The stored copy of ' + name + ' could not be checked — this browser\'s storage is not available right now.');
            return;
        }
        if (state !== assets.FACT_STATES.LOCAL) return;    // unknown: nothing was observed, so nothing is claimed

        const row = (facts.rows) ? facts.rows[assetId] : null;
        if (!row || !record) return;
        if (record.sha256 && row.sha256 && String(row.sha256) !== String(record.sha256)) {
            push(issues, 'assets.bytes-mismatch', path, nodeId, ERROR,
                'The stored copy of ' + name + ' is not the file this message describes. Add the file again.');
            return;
        }
        if (typeof record.bytes === 'number' && typeof row.byteLength === 'number' &&
            row.byteLength !== record.bytes) {
            push(issues, 'assets.bytes-mismatch', path, nodeId, ERROR,
                'The stored copy of ' + name + ' is not the file this message describes. Add the file again.');
            return;
        }
        // Only an image type the store actually recognised is worth comparing:
        // a stored blob with no content type is not evidence of a change.
        if (record.mime && row.mime && row.mime.indexOf('image/') === 0 && row.mime !== record.mime) {
            push(issues, 'assets.mime-mismatch', path, nodeId, WARNING,
                name + ' is recorded as ' + assets.mimeLabel(record.mime) +
                ' but the stored copy is ' + assets.mimeLabel(row.mime) + '.');
        }
    }

    /**
     * The whole asset conversation, in one deterministic pass.
     *
     * Order inside the block (documented because the strip shows the FIRST
     * issue): records that cannot be read, slots that point at nothing, the
     * message-level count/size pair, filename agreement, then each referenced
     * asset by id, then the records nothing uses.
     */
    function checkAssets(issues, doc, limits, facts) {
        const view = assets.assetView(doc);
        const messageNode = CONTENT_NODE;

        // ── 1. Records the document carries but cannot read ──
        view.unreadable.forEach(function (entry) {
            const extra = entry.reason === 'non-json-value'
                ? ' It holds a value that cannot be saved in a draft.'
                : '';
            push(issues, 'assets.record-unreadable', 'assets.' + entry.assetId, messageNode, ERROR,
                'A file this message carries (' + entry.assetId + ') could not be read.' + extra +
                ' Remove it and add the file again.');
        });

        // ── 2. Slots that point at no file at all ──
        view.unlinked.forEach(function (ref) {
            push(issues, 'assets.unlinked', ref.path, ref.embedId || messageNode, ERROR,
                slotLabel(ref) + ' is set to an uploaded file, but no file is attached to it. Choose the file again.');
        });

        // ── 3. The message-level measurement (count, then size) ──
        const sizes = assets.assetBytes(view);
        const measured = assets.checkLimits(sizes.count, sizes.total, limits);
        if (measured.usable) {
            if (measured.count.over) {
                push(issues, 'assets.too-many', 'embeds', messageNode, ERROR,
                    'A message can carry at most ' + measured.count.max + ' files; this one has ' +
                    measured.count.used + '. Remove ' + (measured.count.used - measured.count.max) + '.');
            }
            if (sizes.unknown.length) {
                // A record with no byte count: the total cannot be computed.
                push(issues, 'assets.size-unknown', 'embeds', messageNode, WARNING,
                    'At least one file in this message has no size recorded, so the total was not checked against Discord\'s limit.');
            } else if (!sizes.missing.length && measured.bytes.over) {
                // (A reference with no record at all is already an error above;
                // adding "the total is unknown" to it would be the same problem
                // said twice, in a way that reads like a second one.)
                push(issues, 'assets.total-size', 'embeds', messageNode, ERROR,
                    'The files in this message add up to ' + assets.describeSize(sizes.total) +
                    '; Discord accepts at most ' + assets.describeSize(measured.bytes.max) + '.');
            }
        }

        // ── 4. Filenames: Discord needs one file per name, and a slot must
        //     agree with the record about what its file is called. ──
        const byName = {};
        view.ids.forEach(function (assetId) {
            const record = view.records[assetId];
            if (!record) return;
            if (!byName[record.filename]) byName[record.filename] = [];
            byName[record.filename].push(assetId);
        });
        Object.keys(byName).sort().forEach(function (name) {
            if (byName[name].length < 2) return;
            push(issues, 'assets.filename-clash', 'embeds', messageNode, ERROR,
                'Two files in this message are both named "' + name +
                '". Discord needs a different name for each attached file.');
        });
        view.refs.forEach(function (ref) {
            const record = view.records[ref.assetId];
            if (!record || !ref.filename || ref.filename === record.filename) return;
            push(issues, 'assets.filename-changed', ref.path, ref.embedId || messageNode, WARNING,
                'A file slot is named "' + ref.filename + '" but the file it points at is "' +
                record.filename + '".');
        });

        // ── 5. Each referenced asset, by id (sorted, so the list is stable) ──
        // An id the record map holds but cannot read is already reported above:
        // it must not also be described as "no record for this slot", which
        // would read as a second, unrelated problem.
        const unreadableIds = {};
        view.unreadable.forEach(function (entry) { unreadableIds[entry.assetId] = true; });
        view.ids.forEach(function (assetId) {
            if (unreadableIds[assetId]) return;
            checkAssetRecord(issues, view, assetId, limits);
            checkAssetFacts(issues, view, assetId, facts || null);
            checkAssetSize(issues, view, assetId, limits);
        });

        // ── 6. Records nothing points at ──
        view.orphans.forEach(function (assetId) {
            push(issues, 'assets.unused', 'assets.' + assetId, messageNode, WARNING,
                'A file was added to this message but no image, thumbnail or icon uses it, so it will not be uploaded.');
        });
    }

    /**
     * One file against the served per-file advisory. The number is ADVISORY
     * unless the server says otherwise (`file_advisory_is_hard`) — only
     * Discord knows a guild's real ceiling, so a guess here would refuse a
     * file Discord accepts. The words come from assets.describeSize(), the
     * decision from assets.checkFileSize(): this file owns neither.
     */
    function checkAssetSize(issues, view, assetId, limits) {
        const record = view.records[assetId];
        if (!record || typeof record.bytes !== 'number') return;
        const verdict = assets.checkFileSize(record.bytes, limits);
        if (!verdict.usable || !verdict.oversized) return;
        const ref = assetPath(view.refs, assetId);
        push(issues, 'assets.file-too-large',
            ref ? ref.path : 'assets.' + assetId,
            ref && ref.embedId ? ref.embedId : CONTENT_NODE,
            verdict.blocked ? ERROR : WARNING,
            'The file "' + record.filename + '" is ' + assets.describeSize(verdict.size) +
            '; Discord\'s limit for one file is ' + assets.describeSize(verdict.advisoryMax) + '.');
    }

    // ── The entry point ──────────────────────────────────────────
    /**
     * validate(document, limits, facts) → [issue, …] in a deterministic order:
     * the message first, then embed by embed, each embed's issues in the order
     * the server reports them. An empty array means "nothing to say".
     */
    function validate(document_, limits, facts) {
        if (!model) throw new Error('embed/validate.js needs embed/model.js loaded first');
        if (!assets) throw new Error('embed/validate.js needs embed/assets.js loaded first');

        const check = ensureLimits(limits);
        if (!check.ok) return [limitsIssue()];

        const doc = isObject(document_) ? document_ : {};
        const embeds = Array.isArray(doc.embeds) ? doc.embeds : [];
        const issues = [];

        // ── The message ──
        const content = typeof doc.content === 'string' ? doc.content : '';
        if (content.length > limits.message.content_max) {
            push(issues, 'content.too-long', 'content', CONTENT_NODE, ERROR,
                'Message content is ' + content.length + ' characters; Discord\'s limit is ' + limits.message.content_max + '.');
        } else if (content.length && content.replace(/[\s\u200b]+/g, '').length === 0) {
            // client-only: Discord accepts it and shows nothing.
            push(issues, 'content.whitespace-only', 'content', CONTENT_NODE, WARNING,
                'Message content is only spaces — Discord will show an empty message.');
        }

        if (embeds.length > limits.message.embeds_max) {
            // Mirrors validate_message(): the message-level error is the one
            // that matters, and reporting every field of every embed on top of
            // it would bury it.
            push(issues, 'embeds.too-many', 'embeds', CONTENT_NODE, ERROR,
                'A message can carry at most ' + limits.message.embeds_max + ' embeds; this one has ' + embeds.length + '.');
            return issues;
        }

        const context = {
            limits: limits,
            messageHasContent: model.documentHasContent(doc),
            embedCount: embeds.length,
        };
        for (let i = 0; i < embeds.length; i++) validateEmbed(issues, embeds[i], i, context);

        // 7c: the assets last, so every existing rule keeps its place in the
        // list. `facts` is the page's probe result (or nothing at all): the
        // call above never received one and still works, which is what keeps
        // this an extension of the entry point rather than a new one.
        checkAssets(issues, doc, limits, facts || null);
        return issues;
    }

    /**
     * The change signature of an issue list: the page dispatches only when this
     * differs, so "the strip is written only when its content changes" starts
     * with "the store is told only when the list changes". Order-sensitive on
     * purpose — the list is deterministic, and the first issue is what the
     * strip shows.
     */
    function signature(issues) {
        const list = Array.isArray(issues) ? issues : [];
        return list.map(function (issue) {
            return [issue.severity, issue.code, issue.path, issue.nodeId, issue.message].join('|');
        }).join('\n');
    }

    NERO.embed.validate = {
        ERROR: ERROR,
        WARNING: WARNING,
        CONTENT_NODE: CONTENT_NODE,
        REQUIRED_LIMITS: REQUIRED_LIMITS,
        validate: validate,
        counts: counts,
        caps: caps,
        ensureLimits: ensureLimits,
        limitsIssue: limitsIssue,
        signature: signature,
        isHttpUrl: isHttpUrl,
        isIsoTimestamp: isIsoTimestamp,
    };
})(window.NERO);
