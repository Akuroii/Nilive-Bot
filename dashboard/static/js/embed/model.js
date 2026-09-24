/* ═══════════════════════════════════════════════════════════════
   Message Builder v2 — the normalized document model.

   Phase 1, step 1. Pure data: no DOM, no editor elements, no Blobs,
   no storage, no network. Everything here is JSON-serializable and
   deterministic, so the whole model can be tested in Node with no
   shims and the preview can consume a *payload* rather than the
   editor's internal shape.

   WHY THIS FILE EXISTS
   --------------------
   v1 (`embed-composer.js`) keeps its embed as the editor's flat shape
   — `{title, color:'#7c5cbf', author:'Nero', authorIcon:'…'}` — and
   converts it to Discord's wire shape in `cleanEmbedForPayload`. That
   works, but every consumer then has to know the flat shape, the
   nested API shape AND the wire shape, and the conversion is the only
   place where the three are reconciled.

   v2 makes the *normalized* shape the single source of truth and
   derives everything from it:

       flat editor shape ─┐
       nested API shape  ─┼→  MessageDocument  →  toDiscordPayload()
       saved draft       ─┘        (normalized)      (wire)

   The wire output is not allowed to drift: `scripts/test_message_model.js`
   asserts byte-for-byte equality against v1's proven
   `cleanEmbedsForPayload` over a corpus that covers the complete
   current field set, including the legacy flat-key rows that older
   `embed_templates` still hold.

   DELIBERATE DIFFERENCES FROM v1 (all recorded in the harness)
   -----------------------------------------------------------
   1. An unparsable colour (`'red'`) becomes "unset" here and the key
      is omitted; v1 assigns `NaN`, which JSON-serializes to `null`.
      No producer in this repo emits such a value.
   2. `null`/`undefined` in, `null` out: a media slot is either
      `null` or an asset object — never `''`.
   3. Ids exist. v1 keys fields by array index, which is exactly why
      its preview cannot preserve node identity. Ids are the phase-1
      foundation for the differential preview and are never sent.

   NOT IN THIS FILE (later phases, seams are marked): uploads and the
   asset store (phase 2), components/rows (phase 3), actions (phase 4),
   saved messages and publishing (phase 5).

   Consumed by: embed/store.js, embed/preview.js (phase 1 steps 3+),
   the v2 page module. NOT consumed by v1 — v1 keeps its own proven
   implementation untouched.
   Tested by: scripts/test_message_model.js.
   ═══════════════════════════════════════════════════════════════ */
window.NERO = window.NERO || {};
window.NERO.embed = window.NERO.embed || {};

(function (NERO) {
    'use strict';

    const SCHEMA_VERSION = 2;
    const DEFAULT_COLOR = 0x7c5cbf;      // the dashboard accent, v1's default
    const ZERO_WIDTH = '\u200b';         // v1's placeholder for an empty field part

    // ── Ids ───────────────────────────────────────────────────────
    // Stable keys are the whole point of this model (the preview patches
    // by id, and v1's index-keyed preview is why typing re-creates
    // images). They are short, opaque and never sent to Discord.
    //
    // The generator is seeded on purpose: tests need deterministic
    // output, and two documents normalized in the same millisecond must
    // still not collide.
    function createIdFactory(seed) {
        let counter = 0;
        const salt = seed === undefined || seed === null ? '' : String(seed);
        return function nextId(prefix) {
            counter += 1;
            const n = counter.toString(36);
            const s = salt ? (salt.length % 997).toString(36) : '';
            return String(prefix || 'id') + '_' + s + n;
        };
    }

    const defaultIds = createIdFactory();

    // ── Values: colour and media ──────────────────────────────────
    /**
     * '#7c5cbf' | '7c5cbf' | 0x7c5cbf | 8129727 → 8129727 | null.
     * `#000000` yields 0 (a real colour Discord accepts), which is why
     * "unset" is `null` here and not `0`.
     */
    function colorToInt(value) {
        if (value === null || value === undefined || value === '') return null;
        if (typeof value === 'number') {
            return Number.isFinite(value) ? Math.trunc(value) : null;
        }
        const raw = String(value).trim().replace(/^#/, '');
        if (!/^[0-9a-fA-F]{1,6}$/.test(raw)) return null;
        return parseInt(raw, 16);
    }

    function colorToHex(value) {
        const int = colorToInt(value);
        if (int === null) return '';
        return '#' + int.toString(16).padStart(6, '0');
    }

    /**
     * A media slot is either `null` or an asset. Phase 1 only creates
     * `{kind:'url'}`; phase 2 adds `{kind:'upload', assetId, filename,
     * mime, bytes}` — the shape is already modelled here so the payload
     * transform has exactly one branch to grow, and storing a Discord
     * CDN url as a durable reference is never necessary.
     */
    function mediaFromValue(value) {
        if (!value) return null;
        if (typeof value === 'object') {
            if (value.kind === 'upload' && value.filename) {
                return {
                    kind: 'upload',
                    assetId: value.assetId || null,
                    filename: value.filename,
                    mime: value.mime || null,
                    bytes: typeof value.bytes === 'number' ? value.bytes : null,
                };
            }
            if (value.url) return { kind: 'url', url: String(value.url) };
            return null;
        }
        return { kind: 'url', url: String(value) };
    }

    /** Asset → what goes in the wire `{url}` / `{icon_url}` field. */
    function mediaToWireUrl(asset) {
        if (!asset) return '';
        if (asset.kind === 'upload') return 'attachment://' + asset.filename;
        return asset.url || '';
    }

    function mediaUrl(asset) {
        if (!asset) return '';
        return asset.kind === 'upload' ? '' : (asset.url || '');
    }

    // ── Blank nodes ───────────────────────────────────────────────
    function blankEmbed(opts) {
        opts = opts || {};
        const ids = opts.ids || defaultIds;
        return {
            id: opts.id || ids('emb'),
            title: '',
            url: '',
            description: '',
            color: DEFAULT_COLOR,
            author: { name: '', url: '', icon: null },
            footer: { text: '', icon: null },
            thumbnail: null,
            image: null,
            timestamp: '',
            fields: [],
        };
    }

    function blankField(opts) {
        opts = opts || {};
        const ids = opts.ids || defaultIds;
        return { id: opts.id || ids('fld'), name: '', value: '', inline: false };
    }

    function blankMessageDocument(opts) {
        opts = opts || {};
        const ids = opts.ids || defaultIds;
        return {
            schemaVersion: SCHEMA_VERSION,
            id: opts.id || ids('doc'),
            guildId: opts.guildId || null,
            layout: 'legacy',            // phase 3 seam: 'v2' = Components V2 blocks
            content: opts.content || '',
            embeds: opts.embeds && opts.embeds.length ? opts.embeds : [blankEmbed({ ids: ids })],
            rows: [],                    // phase 3/4 seam: ComponentRow[]
            assets: {},                  // phase 2 seam: assetId → upload metadata
        };
    }

    /** v1's `embedHasContent` — note it ignores url/timestamp/colour. */
    function embedHasContent(e) {
        return !!(e.title || e.description ||
                  (e.author && e.author.name) || (e.footer && e.footer.text) ||
                  e.image || e.thumbnail ||
                  (e.fields && e.fields.length));
    }

    function documentHasContent(doc) {
        return !!(doc && (doc.content || (doc.embeds || []).some(embedHasContent)));
    }

    // ── Normalization ─────────────────────────────────────────────
    function normalizeField(f, ids) {
        f = f || {};
        return {
            id: f.id || ids('fld'),
            name: f.name == null ? '' : String(f.name),
            value: f.value == null ? '' : String(f.value),
            inline: !!f.inline,
        };
    }

    /** Editor flat shape (`{title, color:'#7c5cbf', authorIcon:'…'}`) → normalized. */
    function fromEditorEmbed(e, opts) {
        opts = opts || {};
        const ids = opts.ids || defaultIds;
        e = e || {};
        return {
            id: e.id || opts.id || ids('emb'),
            title: e.title == null ? '' : String(e.title),
            url: e.url == null ? '' : String(e.url),
            description: e.description == null ? '' : String(e.description),
            color: e.color === undefined ? DEFAULT_COLOR : colorToInt(e.color),
            author: {
                name: e.author == null ? '' : String(e.author),
                url: e.authorUrl == null ? '' : String(e.authorUrl),
                icon: mediaFromValue(e.authorIcon),
            },
            footer: {
                text: e.footer == null ? '' : String(e.footer),
                icon: mediaFromValue(e.footerIcon),
            },
            thumbnail: mediaFromValue(e.thumbnail),
            image: mediaFromValue(e.image),
            timestamp: e.timestamp == null ? '' : String(e.timestamp),
            fields: (e.fields || []).map(f => normalizeField(f, ids)),
        };
    }

    function fromEditorDocument(state, opts) {
        opts = opts || {};
        const ids = opts.ids || defaultIds;
        state = state || {};
        return {
            schemaVersion: SCHEMA_VERSION,
            id: opts.id || ids('doc'),
            guildId: opts.guildId || null,
            layout: 'legacy',
            content: state.content == null ? '' : String(state.content),
            embeds: (state.embeds || []).map(e => fromEditorEmbed(e, { ids: ids })),
            rows: [],
            assets: {},
        };
    }

    /**
     * The API/DB shape → normalized. Accepts BOTH shapes this repo has
     * written over time, nested first (v1's `embedFromApi` rules, kept
     * verbatim so a legacy row cannot lose its icons):
     *   nested: {author:{name,url,icon_url}, footer:{text,icon_url}, image:{url}}
     *   flat:   {author:'name', author_icon:'…', footer:'…', footer_icon:'…'}
     */
    function fromApiEmbed(e, opts) {
        opts = opts || {};
        const ids = opts.ids || defaultIds;
        e = e || {};
        const authorName = (e.author && e.author.name) || e.author || '';
        const authorIcon = (e.author && e.author.icon_url) || e.author_icon || '';
        const authorUrl = (e.author && e.author.url) || e.author_url || '';
        const footerText = (e.footer && e.footer.text) || e.footer || '';
        const footerIcon = (e.footer && e.footer.icon_url) || e.footer_icon || '';
        const imageUrl = (e.image && e.image.url) || (typeof e.image === 'string' ? e.image : '');
        const thumbUrl = (e.thumbnail && e.thumbnail.url) || (typeof e.thumbnail === 'string' ? e.thumbnail : '');
        return {
            id: e.id || opts.id || ids('emb'),
            title: e.title == null ? '' : String(e.title),
            url: e.url == null ? '' : String(e.url),
            description: e.description == null ? '' : String(e.description),
            color: e.color === undefined || e.color === null || e.color === ''
                ? DEFAULT_COLOR : colorToInt(e.color),
            author: {
                name: authorName ? String(authorName) : '',
                url: authorUrl ? String(authorUrl) : '',
                icon: mediaFromValue(authorIcon),
            },
            footer: {
                text: footerText ? String(footerText) : '',
                icon: mediaFromValue(footerIcon),
            },
            thumbnail: mediaFromValue(thumbUrl),
            image: mediaFromValue(imageUrl),
            timestamp: e.timestamp == null ? '' : String(e.timestamp),
            fields: (e.fields || []).map(f => normalizeField(f, ids)),
        };
    }

    function fromApiDocument(content, embeds, opts) {
        opts = opts || {};
        const ids = opts.ids || defaultIds;
        const list = (embeds || []).map(e => fromApiEmbed(e, { ids: ids }));
        return {
            schemaVersion: SCHEMA_VERSION,
            id: opts.id || ids('doc'),
            guildId: opts.guildId || null,
            layout: 'legacy',
            content: content == null ? '' : String(content),
            embeds: list.length ? list : [blankEmbed({ ids: ids })],
            rows: [],
            assets: {},
        };
    }

    /**
     * Idempotent repair: whatever came out of storage gets ids, the
     * right array shapes and the current schemaVersion, without
     * touching values. `normalizeDocument(normalizeDocument(d))` must
     * equal `normalizeDocument(d)` — the harness asserts that.
     */
    function normalizeDocument(doc, opts) {
        opts = opts || {};
        const ids = opts.ids || defaultIds;
        doc = doc || {};
        const embeds = (doc.embeds || []).map(e => ({
            id: e.id || ids('emb'),
            title: e.title == null ? '' : String(e.title),
            url: e.url == null ? '' : String(e.url),
            description: e.description == null ? '' : String(e.description),
            color: e.color === undefined ? DEFAULT_COLOR : colorToInt(e.color),
            author: {
                name: (e.author && e.author.name) == null ? '' : String(e.author.name),
                url: (e.author && e.author.url) == null ? '' : String(e.author.url),
                icon: mediaFromValue(e.author && e.author.icon),
            },
            footer: {
                text: (e.footer && e.footer.text) == null ? '' : String(e.footer.text),
                icon: mediaFromValue(e.footer && e.footer.icon),
            },
            thumbnail: mediaFromValue(e.thumbnail),
            image: mediaFromValue(e.image),
            timestamp: e.timestamp == null ? '' : String(e.timestamp),
            fields: (e.fields || []).map(f => normalizeField(f, ids)),
        }));
        return {
            schemaVersion: SCHEMA_VERSION,
            id: doc.id || ids('doc'),
            guildId: doc.guildId || null,
            layout: doc.layout === 'v2' ? 'v2' : 'legacy',
            content: doc.content == null ? '' : String(doc.content),
            embeds: embeds.length ? embeds : [blankEmbed({ ids: ids })],
            rows: Array.isArray(doc.rows) ? doc.rows.slice() : [],
            assets: (doc.assets && typeof doc.assets === 'object') ? Object.assign({}, doc.assets) : {},
        };
    }

    /** Normalized → editor flat shape (round-trip aid; not used by v1). */
    function toEditorEmbed(e) {
        return {
            title: e.title || '',
            description: e.description || '',
            color: colorToHex(e.color),
            author: (e.author && e.author.name) || '',
            authorIcon: mediaUrl(e.author && e.author.icon),
            authorUrl: (e.author && e.author.url) || '',
            footer: (e.footer && e.footer.text) || '',
            footerIcon: mediaUrl(e.footer && e.footer.icon),
            url: e.url || '',
            timestamp: e.timestamp || '',
            thumbnail: mediaUrl(e.thumbnail),
            image: mediaUrl(e.image),
            fields: (e.fields || []).map(f => ({ name: f.name, value: f.value, inline: !!f.inline })),
        };
    }

    // ── The one canonical payload transform ───────────────────────
    /**
     * Byte-for-byte the shape v1's `cleanEmbedForPayload` produces —
     * same keys, same order, same omissions. Key ORDER matters: the
     * harness compares `JSON.stringify` output, and so does a human
     * reading "Copy JSON".
     */
    function toWireEmbed(e, withKeys) {
        const out = {};
        // Phase 1 step 3: `withKeys` adds the model's own ids to the payload
        // so the differential preview can key DOM nodes by them. It is the
        // ONLY difference between the keyed and the plain payload — the
        // harness asserts stripping `key` reproduces the wire bytes exactly,
        // so the preview still renders precisely what would be sent.
        if (withKeys) out.key = e.id === undefined || e.id === null ? null : String(e.id);
        if (e.title) out.title = e.title;
        if (e.description) out.description = e.description;
        if (e.color !== null && e.color !== undefined) out.color = e.color;
        const author = {};
        if (e.author && e.author.name) author.name = e.author.name;
        if (e.author && e.author.icon) author.icon_url = mediaToWireUrl(e.author.icon);
        if (e.author && e.author.url) author.url = e.author.url;
        if (Object.keys(author).length) out.author = author;
        const footer = {};
        if (e.footer && e.footer.text) footer.text = e.footer.text;
        if (e.footer && e.footer.icon) footer.icon_url = mediaToWireUrl(e.footer.icon);
        if (Object.keys(footer).length) out.footer = footer;
        if (e.url) out.url = e.url;
        if (e.timestamp) out.timestamp = e.timestamp;
        if (e.image) out.image = { url: mediaToWireUrl(e.image) };
        if (e.thumbnail) out.thumbnail = { url: mediaToWireUrl(e.thumbnail) };
        if (e.fields && e.fields.length) {
            // Note the outer test: v1 assigns `fields` whenever the INPUT
            // array is non-empty, even if filtering leaves it empty, so an
            // embed whose fields are all blank still sends `"fields": []`.
            out.fields = e.fields.filter(f => f.name || f.value).map(f => {
                const wire = {
                    name: f.name || ZERO_WIDTH,
                    value: f.value || ZERO_WIDTH,
                    inline: !!f.inline,
                };
                if (withKeys) wire.key = f.id === undefined || f.id === null ? null : String(f.id);
                return withKeys ? Object.assign({ key: wire.key }, wire) : wire;
            });
        }
        return out;
    }

    function toWireEmbeds(embeds, withKeys) {
        return (embeds || []).filter(embedHasContent).map(e => toWireEmbed(e, withKeys));
    }

    /**
     * The canonical Discord payload. `content` is omitted when empty,
     * exactly as v1's `content: state.content || undefined` behaved.
     * Phase 3 adds `components` here, phase 2 adds the attachment parts
     * — one function, one branch, one place to test.
     */
    function toDiscordPayload(doc, opts) {
        opts = opts || {};
        const embeds = toWireEmbeds((doc && doc.embeds) || [], !!opts.withKeys);
        const content = doc && doc.content;
        // Key ORDER matters (it is what a human reads in "Copy JSON" and
        // what the byte-equality harness compares): content first, then
        // embeds — the same order v1's send path builds.
        const wire = content ? { content: content, embeds: embeds } : { embeds: embeds };
        if (opts.rows && opts.rows.length) wire.components = opts.rows;
        return wire;
    }

    // ── Determinism helpers ───────────────────────────────────────
    /** JSON with object keys sorted — the basis of a stable hash. */
    function stableStringify(value) {
        if (value === null || typeof value !== 'object') return JSON.stringify(value);
        if (Array.isArray(value)) return '[' + value.map(stableStringify).join(',') + ']';
        const keys = Object.keys(value).sort();
        return '{' + keys.map(k => JSON.stringify(k) + ':' + stableStringify(value[k])).join(',') + '}';
    }

    /** FNV-1a over the stable form: cheap, dependency-free, stable. */
    function hashDocument(doc) {
        const text = stableStringify(doc);
        let h = 0x811c9dc5;
        for (let i = 0; i < text.length; i++) {
            h ^= text.charCodeAt(i);
            h = Math.imul(h, 0x01000193) >>> 0;
        }
        return h.toString(16).padStart(8, '0') + ':' + text.length.toString(36);
    }

    function cloneDocument(doc) {
        return JSON.parse(JSON.stringify(doc));
    }

    function equalDocument(a, b) {
        return hashDocument(a) === hashDocument(b);
    }

    // ── Immutable patches (by id, never by index) ─────────────────
    // Every editor mutation goes through one of these. They return a new
    // document; untouched embeds and fields keep their object identity,
    // which is what lets the store tell subscribers exactly what changed
    // and the preview skip everything else.
    function mapEmbed(doc, embedId, fn) {
        let touched = false;
        let changed = false;
        const embeds = doc.embeds.map(e => {
            if (e.id !== embedId) return e;
            touched = true;
            const next = fn(e);
            if (next !== e) changed = true;
            return next;
        });
        // A no-op MUST return the original document, not a fresh copy of it:
        // the store decides whether to notify subscribers and push history by
        // object identity, so "same value" has to mean "same object".
        if (!touched || !changed) return doc;
        return Object.assign({}, doc, { embeds: embeds });
    }

    function setContent(doc, text) {
        const next = text == null ? '' : String(text);
        if (doc.content === next) return doc;
        return Object.assign({}, doc, { content: next });
    }

    // Scalar keys only: the nested groups (author/footer/media/fields) have
    // their own setters because they need their own normalization, and
    // letting a generic setter write `fields` would bypass the id invariant.
    const SCALAR_KEYS = ['title', 'description', 'url', 'timestamp'];

    function setEmbedFields(doc, embedId, patch) {
        return mapEmbed(doc, embedId, e => {
            const next = Object.assign({}, e);
            let changed = false;
            SCALAR_KEYS.forEach(k => {
                if (patch[k] === undefined) return;
                const value = patch[k] == null ? '' : String(patch[k]);
                if (e[k] !== value) { next[k] = value; changed = true; }
            });
            return changed ? next : e;
        });
    }

    function setEmbedText(doc, embedId, key, value) {
        const next = value == null ? '' : String(value);
        return mapEmbed(doc, embedId, e => (e[key] === next ? e : Object.assign({}, e, { [key]: next })));
    }

    function setColor(doc, embedId, value) {
        const next = colorToInt(value);
        return mapEmbed(doc, embedId, e => (e.color === next ? e : Object.assign({}, e, { color: next })));
    }

    function setAuthor(doc, embedId, patch) {
        return mapEmbed(doc, embedId, e => {
            const author = Object.assign({}, e.author, patch);
            if (patch && patch.icon !== undefined) author.icon = mediaFromValue(patch.icon);
            const same = author.name === e.author.name && author.url === e.author.url &&
                         stableStringify(author.icon) === stableStringify(e.author.icon);
            return same ? e : Object.assign({}, e, { author: author });
        });
    }

    function setFooter(doc, embedId, patch) {
        return mapEmbed(doc, embedId, e => {
            const footer = Object.assign({}, e.footer, patch);
            if (patch && patch.icon !== undefined) footer.icon = mediaFromValue(patch.icon);
            const same = footer.text === e.footer.text &&
                         stableStringify(footer.icon) === stableStringify(e.footer.icon);
            return same ? e : Object.assign({}, e, { footer: footer });
        });
    }

    function setMedia(doc, embedId, slot, value) {
        if (slot !== 'image' && slot !== 'thumbnail') return doc;
        const next = mediaFromValue(value);
        return mapEmbed(doc, embedId, e =>
            (stableStringify(e[slot]) === stableStringify(next) ? e : Object.assign({}, e, { [slot]: next })));
    }

    // ── Structure (the keyed lists the preview reconciles) ────────
    function addEmbed(doc, opts) {
        opts = opts || {};
        const ids = opts.ids || defaultIds;
        const embeds = doc.embeds.slice();
        embeds.splice(opts.at == null ? embeds.length : opts.at, 0, blankEmbed({ ids: ids }));
        return Object.assign({}, doc, { embeds: embeds });
    }

    function removeEmbed(doc, embedId, opts) {
        opts = opts || {};
        const ids = opts.ids || defaultIds;
        if (doc.embeds.length <= 1) return doc;          // never zero embeds; the page always has one
        const embeds = doc.embeds.filter(e => e.id !== embedId);
        if (embeds.length === doc.embeds.length) return doc;
        return Object.assign({}, doc, { embeds: embeds });
    }

    function moveEmbed(doc, embedId, delta) {
        const from = doc.embeds.findIndex(e => e.id === embedId);
        if (from === -1) return doc;
        const to = Math.min(doc.embeds.length - 1, Math.max(0, from + delta));
        if (to === from) return doc;
        const embeds = doc.embeds.slice();
        const [moved] = embeds.splice(from, 1);
        embeds.splice(to, 0, moved);
        return Object.assign({}, doc, { embeds: embeds });
    }

    function duplicateEmbed(doc, embedId, opts) {
        opts = opts || {};
        const ids = opts.ids || defaultIds;
        const at = doc.embeds.findIndex(e => e.id === embedId);
        if (at === -1) return doc;
        const copy = cloneDocument(doc.embeds[at]);
        copy.id = ids('emb');
        copy.fields = copy.fields.map(f => Object.assign({}, f, { id: ids('fld') }));
        const embeds = doc.embeds.slice();
        embeds.splice(at + 1, 0, copy);
        return Object.assign({}, doc, { embeds: embeds });
    }

    function addField(doc, embedId, opts) {
        opts = opts || {};
        const ids = opts.ids || defaultIds;
        return mapEmbed(doc, embedId, e => {
            const fields = e.fields.slice();
            fields.splice(opts.at == null ? fields.length : opts.at, 0, blankField({ ids: ids }));
            return Object.assign({}, e, { fields: fields });
        });
    }

    function removeField(doc, embedId, fieldId) {
        return mapEmbed(doc, embedId, e => {
            const fields = e.fields.filter(f => f.id !== fieldId);
            return fields.length === e.fields.length ? e : Object.assign({}, e, { fields: fields });
        });
    }

    function moveField(doc, embedId, fieldId, delta) {
        return mapEmbed(doc, embedId, e => {
            const from = e.fields.findIndex(f => f.id === fieldId);
            if (from === -1) return e;
            const to = Math.min(e.fields.length - 1, Math.max(0, from + delta));
            if (to === from) return e;
            const fields = e.fields.slice();
            const [moved] = fields.splice(from, 1);
            fields.splice(to, 0, moved);
            return Object.assign({}, e, { fields: fields });
        });
    }

    function setField(doc, embedId, fieldId, patch) {
        return mapEmbed(doc, embedId, e => {
            let changed = false;
            const fields = e.fields.map(f => {
                if (f.id !== fieldId) return f;
                const next = Object.assign({}, f);
                if (patch.name !== undefined && patch.name !== f.name) { next.name = String(patch.name); changed = true; }
                if (patch.value !== undefined && patch.value !== f.value) { next.value = String(patch.value); changed = true; }
                if (patch.inline !== undefined && !!patch.inline !== f.inline) { next.inline = !!patch.inline; changed = true; }
                return changed ? next : f;
            });
            return changed ? Object.assign({}, e, { fields: fields }) : e;
        });
    }

    /** Structural sharing check used by the store's equality shortcuts. */
    function changedEmbedIds(before, after) {
        const ids = [];
        const beforeById = {};
        (before.embeds || []).forEach(e => { beforeById[e.id] = e; });
        (after.embeds || []).forEach(e => {
            if (beforeById[e.id] !== e) ids.push(e.id);
        });
        return ids;
    }

    NERO.embed = NERO.embed || {};
    NERO.embed.model = {
        SCHEMA_VERSION: SCHEMA_VERSION,
        DEFAULT_COLOR: DEFAULT_COLOR,
        ZERO_WIDTH: ZERO_WIDTH,
        createIdFactory: createIdFactory,
        colorToInt: colorToInt,
        colorToHex: colorToHex,
        mediaFromValue: mediaFromValue,
        mediaUrl: mediaUrl,
        mediaToWireUrl: mediaToWireUrl,
        blankEmbed: blankEmbed,
        blankField: blankField,
        blankMessageDocument: blankMessageDocument,
        embedHasContent: embedHasContent,
        documentHasContent: documentHasContent,
        fromEditorEmbed: fromEditorEmbed,
        fromEditorDocument: fromEditorDocument,
        fromApiEmbed: fromApiEmbed,
        fromApiDocument: fromApiDocument,
        normalizeDocument: normalizeDocument,
        toEditorEmbed: toEditorEmbed,
        toWireEmbed: toWireEmbed,
        toWireEmbeds: toWireEmbeds,
        toDiscordPayload: toDiscordPayload,
        stableStringify: stableStringify,
        hashDocument: hashDocument,
        cloneDocument: cloneDocument,
        equalDocument: equalDocument,
        mapEmbed: mapEmbed,
        setContent: setContent,
        setEmbedFields: setEmbedFields,
        setEmbedText: setEmbedText,
        setColor: setColor,
        setAuthor: setAuthor,
        setFooter: setFooter,
        setMedia: setMedia,
        addEmbed: addEmbed,
        removeEmbed: removeEmbed,
        moveEmbed: moveEmbed,
        duplicateEmbed: duplicateEmbed,
        addField: addField,
        removeField: removeField,
        moveField: moveField,
        setField: setField,
        changedEmbedIds: changedEmbedIds,
    };
})(window.NERO);
