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
//   No DOM, no timers, no storage, no network, no counters and no module
//   state: the same document and the same limits always produce the same list,
//   and a second call cannot see anything the first one left behind. The page
//   owns WHEN this runs and WHERE the result goes — never this file.
//
// WHERE IT SITS (the whole step-6 flow, one implementation each)
//
//       served limits → validate(document, limits) → store.ui.issues
//                                                    → #mb2-strip (6a)
//                                                    → counters/badges (6b)
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
            label: label + ' author icon',
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
            label: label + ' footer icon',
        }, footerIcon);

        // ── Media ──
        checkMedia(issues, {
            code: 'embed.image', path: prefix + '.image.url', nodeId: nodeId,
            label: label + ' image',
        }, model.mediaToWireUrl(e.image));
        checkMedia(issues, {
            code: 'embed.thumbnail', path: prefix + '.thumbnail.url', nodeId: nodeId,
            label: label + ' thumbnail',
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

    // ── The entry point ──────────────────────────────────────────
    /**
     * validate(document, limits) → [issue, …] in a deterministic order:
     * the message first, then embed by embed, each embed's issues in the order
     * the server reports them. An empty array means "nothing to say".
     */
    function validate(document_, limits) {
        if (!model) throw new Error('embed/validate.js needs embed/model.js loaded first');

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
        ensureLimits: ensureLimits,
        limitsIssue: limitsIssue,
        signature: signature,
        isHttpUrl: isHttpUrl,
        isIsoTimestamp: isIsoTimestamp,
    };
})(window.NERO);
