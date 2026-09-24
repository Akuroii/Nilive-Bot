/* ═══════════════════════════════════════════════════════════════
   Message Builder v2 — the draft persistence boundary (phase 1, step 4).

   This module owns ONE thing: moving a normalized MessageDocument
   between the editor and storage, and nothing else.

       MessageDocument → serialize → storage
       storage → deserialize/validate → MessageDocument

   The canonical payload transform stays in model.js. This file does not
   build payloads, does not know Discord's rules, and does not decide what
   the document means — it only checks that what it stores is a document
   it can read back. If a second payload path ever appears here, the
   boundary has been broken.

   NAMESPACES AND KEYS (isolated from v1 by construction)
   -----------------------------------------------------
     v1: database  nero_embedbuilder     (one store: 'draft', key 'composer')
     v2: database  nero_message_builder  (stores: 'drafts', 'assets', 'meta')
         draft key v2:<guildId>:<documentId>

   v2 never opens v1's database for writing: the only object that ever
   touches it is the read-only adapter built by importFromV1(), and that
   adapter refuses put/remove outright. There is no code path in this file
   that writes a v1 key.

   THE SAVE MODEL
   --------------
   Editing must never wait on storage. Changes mark the session dirty and
   (re)start one idle timer; a burst of keystrokes collapses into a single
   write. Lifecycle events (pagehide / tab hidden) flush immediately.
   Every result is reported, never thrown: the editor keeps working with
   storage broken, and the save state is exposed for the UI to show.

   Record shape (fixed key order, deterministic bytes):
     { schemaVersion, namespace, key, guildId, documentId,
       revision, createdAt, updatedAt, documentHash, document }

   `schemaVersion` is the ENVELOPE version (this file's format);
   `document.schemaVersion` is the MODEL version (model.js). They move
   independently, so each is version-checked separately. A record that is
   newer than this build is PRESERVED, never silently downgraded: loading
   it raises a guard that refuses writes until the caller resolves it.

   NOT IN THIS FILE (deliberately)
   -------------------------------
   Attachments and asset bytes (phase 2 — the 'assets' store is created as
   an empty seam), the saved library / revisions browser (phase 2+), any
   UI, any route, any validation of Discord's limits, any component or
   action state (phase 3–4).

   Consumed by: the v2 page shell (step 5), the test harness.
   Tested by: scripts/test_drafts.js.
   ═══════════════════════════════════════════════════════════════ */
window.NERO = window.NERO || {};
window.NERO.embed = window.NERO.embed || {};

(function (NERO) {
    'use strict';

    const model = NERO.embed.model;
    if (!model) throw new Error('embed/drafts.js needs embed/model.js loaded first');

    const V1_NAMESPACE = 'nero_embedbuilder';
    const V1_STORE = 'draft';
    const V1_KEY = 'composer';
    const V2_NAMESPACE = 'nero_message_builder';
    const V2_STORES = ['drafts', 'assets', 'meta'];
    const KEY_PREFIX = 'v2';
    const RECORD_VERSION = 1;
    const DEFAULT_IDLE_MS = 1500;
    const DEFAULT_TIMEOUT_MS = 1500;

    // ── Keys ──────────────────────────────────────────────────────
    function draftKey(guildId, documentId) {
        const guild = guildId === null || guildId === undefined || guildId === '' ? 'global' : String(guildId);
        if (documentId === null || documentId === undefined || documentId === '') {
            throw new Error('draftKey needs a documentId');
        }
        return KEY_PREFIX + ':' + guild + ':' + String(documentId);
    }

    function parseKey(key) {
        const parts = String(key || '').split(':');
        if (parts.length !== 3 || parts[0] !== KEY_PREFIX || !parts[1] || !parts[2]) return null;
        return { prefix: parts[0], guildId: parts[1], documentId: parts[2] };
    }

    /**
     * A draft's identity.
     *
     * This deliberately does NOT reuse the model's id factory. That factory
     * salts ids by the LENGTH of its seed (see createIdFactory in model.js),
     * so two sessions that seed it differently but at the same length mint
     * identical ids — and ids from a fresh page load restart at 1. Two new
     * drafts would then key to the same `v2:<guild>:<documentId>` and the
     * second would silently overwrite the first. Persistence cannot inherit
     * that, so the draft id carries its own uniqueness: the injected clock
     * plus a monotonic sequence, plus optional entropy from the caller
     * (crypto.randomUUID in a browser) for two tabs opened together.
     */
    let documentSeq = 0;
    function newDocumentId(opts) {
        opts = opts || {};
        const t = typeof opts.now === 'function' ? opts.now() : (opts.time == null ? 0 : opts.time);
        const seq = opts.sequence == null ? (documentSeq += 1) : opts.sequence;
        const entropy = typeof opts.entropy === 'function' ? String(opts.entropy() || '') : String(opts.entropy || '');
        const clean = entropy.replace(/[^A-Za-z0-9-]/g, '').slice(0, 16);
        const parts = ['mb', Math.floor(Number(t) || 0).toString(36), seq.toString(36)];
        if (clean) parts.push(clean);
        return parts.join('_');
    }

    function metaKey(guildId, name) {
        const guild = guildId === null || guildId === undefined || guildId === '' ? 'global' : String(guildId);
        return KEY_PREFIX + ':' + guild + ':' + String(name);
    }

    // ── Serialization gate ────────────────────────────────────────
    // Structured clone would throw on some of these anyway (DataCloneError
    // on a DOM node); everything else — a Blob, a Map, an undefined value,
    // NaN — would be stored as something DIFFERENT from what was handed in,
    // which is silent data loss. So the gate rejects all of it by path, and
    // a save that fails the gate writes nothing at all.
    function SerializationError(message, path) {
        const err = new Error(message + ' at ' + (path || '<root>'));
        err.name = 'SerializationError';
        err.path = path;
        return err;
    }

    /**
     * A plain object in ANY realm. Comparing against this realm's
     * Object.prototype would reject perfectly good data that came from
     * another realm (a different iframe, or this module's test harness), so
     * the test is structural: a prototype whose own prototype is null.
     * A class instance's prototype sits on Object.prototype, so it still fails.
     */
    function isPlainObject(v) {
        if (v === null || typeof v !== 'object') return false;
        const proto = Object.getPrototypeOf(v);
        if (proto === null) return true;
        if (Object.getPrototypeOf(proto) !== null) return false;
        return Object.prototype.hasOwnProperty.call(proto, 'constructor')
            && typeof proto.constructor === 'function'
            && proto.constructor.name === 'Object';
    }

    function assertSerializable(value, path) {
        const seen = [];
        (function walk(v, p) {
            const where = p || '<root>';
            if (v === null) return;
            const t = typeof v;
            if (t === 'string' || t === 'boolean') return;
            if (t === 'number') {
                if (!isFinite(v)) throw SerializationError('non-finite number', where);
                return;
            }
            if (t === 'undefined') throw SerializationError('undefined value', where);
            if (t === 'function') throw SerializationError('function', where);
            if (t === 'symbol' || t === 'bigint') throw SerializationError(t, where);
            if (typeof Blob !== 'undefined' && v instanceof Blob) throw SerializationError('Blob/File', where);
            if (typeof File !== 'undefined' && v instanceof File) throw SerializationError('File', where);
            if (typeof Node !== 'undefined' && v instanceof Node) throw SerializationError('DOM node', where);
            if (typeof v.nodeType === 'number' && typeof v.tagName === 'string') {
                throw SerializationError('DOM node', where);
            }
            if (v instanceof Date) throw SerializationError('Date (use an ISO string)', where);
            if (v instanceof RegExp) throw SerializationError('RegExp', where);
            if (typeof Map !== 'undefined' && v instanceof Map) throw SerializationError('Map', where);
            if (typeof Set !== 'undefined' && v instanceof Set) throw SerializationError('Set', where);
            if (typeof WeakMap !== 'undefined' && v instanceof WeakMap) throw SerializationError('WeakMap', where);
            if (seen.indexOf(v) !== -1) throw SerializationError('cycle', where);
            seen.push(v);
            if (Array.isArray(v) || (typeof ArrayBuffer !== 'undefined' && ArrayBuffer.isView(v))) {
                if (!Array.isArray(v)) throw SerializationError('typed array', where);
                v.forEach((item, i) => walk(item, where + '[' + i + ']'));
            } else {
                if (!isPlainObject(v)) {
                    throw SerializationError('class instance ('
                        + ((v.constructor && v.constructor.name) || 'unknown') + ')', where);
                }
                Object.keys(v).forEach(k => walk(v[k], where + '.' + k));
            }
            seen.pop();
        })(value, '');
        return true;
    }

    /** The exact bytes this module would store (stable, key-order independent). */
    function serializeRecord(record) {
        return model.stableStringify(record);
    }

    /** A JSON-clean copy — after the gate, this cannot lose anything. */
    function toStorable(document) {
        assertSerializable(document);
        return JSON.parse(JSON.stringify(document));
    }

    // ── Record construction and validation ────────────────────────
    function buildRecord(fields) {
        // Gate the ORIGINAL first: if the document carries something that
        // cannot be stored (a Blob in the asset registry, a DOM node from a
        // half-wired editor), the caller must be told, not quietly handed a
        // document that has been sanitized into something else.
        assertSerializable(fields.document);
        // Then store the NORMALIZED document, so save → load is a fixed point:
        // normalizeDocument is additive (it fills in a blank embed for an empty
        // embeds array, ids, the current schema version), and normalizing on
        // the way in means the hash and the bytes a reload produces are the
        // same ones that were written — no phantom "dirty" state after a load.
        const doc = toStorable(model.normalizeDocument(fields.document));
        const prev = fields.previous || null;
        return {
            schemaVersion: RECORD_VERSION,
            namespace: V2_NAMESPACE,
            key: draftKey(fields.guildId, fields.documentId),
            guildId: fields.guildId === undefined ? null : fields.guildId,
            documentId: fields.documentId,
            revision: (prev && Number.isInteger(prev.revision) ? prev.revision : 0) + 1,
            createdAt: prev && prev.createdAt ? prev.createdAt : fields.now,
            updatedAt: fields.now,
            documentHash: model.hashDocument(doc),
            document: doc,
        };
    }

    /**
     * Decide what a stored value means. Never throws, never repairs in
     * place, never writes. Returns a verdict the caller can act on:
     *
     *   ok        usable as-is
     *   repaired  usable after normalizeDocument() filled in what was missing
     *   corrupt   unusable; the stored bytes are reported so they survive
     *   future    written by a newer build; preserved, writes refused
     *   foreign   not a v2 record (another namespace, e.g. a v1 payload)
     *   unsupported  older than this build with no migration path
     */
    function validateRecord(record, expected) {
        expected = expected || {};
        const problems = [];
        const verdict = (status, extra) => Object.assign({
            status: status,
            ok: status === 'ok' || status === 'repaired',
            problems: problems,
        }, extra || {});

        if (!record || typeof record !== 'object' || Array.isArray(record)) {
            problems.push('record is not an object');
            return verdict('corrupt', { record: record });
        }

        if (record.namespace !== undefined && record.namespace !== V2_NAMESPACE) {
            problems.push('namespace is ' + JSON.stringify(record.namespace) + ', not ' + V2_NAMESPACE);
            return verdict('foreign', { record: record });
        }

        const version = record.schemaVersion;
        if (!Number.isInteger(version) || version < 1) {
            problems.push('schemaVersion is not a positive integer (' + JSON.stringify(version) + ')');
            return verdict('corrupt', { record: record });
        }
        if (version > RECORD_VERSION) {
            problems.push('record version ' + version + ' is newer than this build (' + RECORD_VERSION + ')');
            return verdict('future', { record: record });
        }
        if (version < RECORD_VERSION) {
            problems.push('record version ' + version + ' has no migration to ' + RECORD_VERSION);
            return verdict('unsupported', { record: record });
        }

        if (expected.key && record.key !== undefined && record.key !== expected.key) {
            problems.push('record key ' + JSON.stringify(record.key) + ' does not match ' + expected.key);
            return verdict('corrupt', { record: record });
        }

        const doc = record.document;
        if (!doc || typeof doc !== 'object' || Array.isArray(doc)) {
            problems.push('document is missing or not an object');
            return verdict('corrupt', { record: record });
        }

        const docVersion = doc.schemaVersion;
        if (docVersion !== undefined && (!Number.isInteger(docVersion) || docVersion < 1)) {
            problems.push('document.schemaVersion is invalid (' + JSON.stringify(docVersion) + ')');
            return verdict('corrupt', { record: record });
        }
        if (Number.isInteger(docVersion) && docVersion > model.SCHEMA_VERSION) {
            problems.push('document schemaVersion ' + docVersion + ' is newer than this model (' + model.SCHEMA_VERSION + ')');
            return verdict('future', { record: record });
        }
        if (Number.isInteger(docVersion) && docVersion < model.SCHEMA_VERSION) {
            problems.push('document schemaVersion ' + docVersion + ' has no migration to ' + model.SCHEMA_VERSION);
            return verdict('unsupported', { record: record });
        }

        // Normalize (additive repair) and report precisely what was missing.
        const normalized = model.normalizeDocument(doc);
        const repairs = [];
        const beforeKeys = Object.keys(doc).sort().join(',');
        const afterKeys = Object.keys(normalized).sort().join(',');
        if (beforeKeys !== afterKeys) repairs.push('shape');
        if (docVersion === undefined) repairs.push('schemaVersion');
        if (!Array.isArray(doc.embeds) || !doc.embeds.length) repairs.push('embeds');
        if (typeof doc.content !== 'string') repairs.push('content');
        const missingIds = [];
        (normalized.embeds || []).forEach((e, i) => {
            if (!e.id) missingIds.push('embed[' + i + '].id');
            (e.fields || []).forEach((f, j) => { if (!f.id) missingIds.push('embed[' + i + '].field[' + j + '].id'); });
        });
        if (missingIds.length) repairs.push('ids');
        if (!normalized.id) missingIds.push('document.id');
        if (model.stableStringify(normalized) !== model.stableStringify(doc)) {
            if (!repairs.length) repairs.push('normalized');
        }

        const actualHash = model.hashDocument(normalized);
        const hashMatches = !record.documentHash || record.documentHash === actualHash;
        if (!hashMatches) problems.push('documentHash does not match the stored document');

        return verdict(repairs.length ? 'repaired' : 'ok', {
            record: record,
            document: normalized,
            repairs: repairs,
            hashMatches: hashMatches,
            meta: {
                revision: Number.isInteger(record.revision) ? record.revision : 0,
                createdAt: record.createdAt || null,
                updatedAt: record.updatedAt || null,
                documentHash: actualHash,
                guildId: record.guildId === undefined ? null : record.guildId,
                documentId: record.documentId === undefined ? null : record.documentId,
            },
        });
    }

    // ── Storage adapters ──────────────────────────────────────────
    // An adapter is any object implementing this small surface:
    //   open() -> Promise<bool>          isAvailable() -> bool
    //   reason() -> string|null          get(key) -> Promise<record|null>
    //   put(key, value) -> Promise<{ok, reason?}>
    //   remove(key)      -> Promise<{ok, reason?}>
    //   getAll() -> Promise<record[]>    stats() -> object
    //   getMeta(key) / putMeta(key, value)                  (optional)
    //   readOnly -> bool
    // Every method resolves; none throws at the caller. IndexedDB is the
    // real implementation; the read-only v1 view is the same code pointed at
    // the other database with `readOnly: true`.

    /**
     * IndexedDB adapter. Deliberately mirrors the failure handling the v1
     * page already proved in production: one short timeout instead of
     * spinning, a single degraded flag, no console spam, and every
     * operation resolving rather than throwing.
     *
     * `readOnly: true` refuses every write before it reaches the database —
     * that is what makes the v1 import boundary structural rather than a
     * promise. `stores` are created on upgrade; no existing database is
     * ever migrated or deleted.
     */
    function idbStorage(options) {
        options = options || {};
        const name = options.name || V2_NAMESPACE;
        const version = options.version || 1;
        const stores = options.stores || V2_STORES;
        const primary = options.primaryStore || stores[0];
        const timeoutMs = options.timeoutMs == null ? DEFAULT_TIMEOUT_MS : options.timeoutMs;
        const scheduler = options.scheduler || {
            setTimeout: (fn, ms) => setTimeout(fn, ms),
            clearTimeout: (id) => clearTimeout(id),
        };
        const idb = options.indexedDB || (typeof indexedDB !== 'undefined' ? indexedDB : null);
        const readOnly = !!options.readOnly;

        const stats = { opens: 0, gets: 0, puts: 0, deletes: 0, writes: 0, failures: 0, timeouts: 0 };
        let db = null;
        let degraded = idb ? null : 'no-indexeddb';
        let openPromise = null;

        function degrade(why) {
            if (degraded === null) stats.failures++;
            degraded = degraded || why;
            return degraded;
        }

        // ── open ──────────────────────────────────────────────────
        // One timeout, one degraded flag, no spinning. A VersionError (a
        // newer build already owns this database) degrades rather than
        // forcing the database open — refusing is the safe answer.
        function doOpen() {
            if (db) return Promise.resolve(true);
            if (degraded) return Promise.resolve(false);
            if (openPromise) return openPromise;
            stats.opens++;
            openPromise = new Promise((resolve) => {
                let settled = false;
                const timer = scheduler.setTimeout(() => {
                    if (settled) return;
                    settled = true;
                    stats.timeouts++;
                    degrade('open-timeout');
                    openPromise = null;
                    resolve(false);
                }, timeoutMs);
                let req;
                try {
                    req = idb.open(name, version);
                } catch (e) {
                    settled = true;
                    scheduler.clearTimeout(timer);
                    degrade('open-threw');
                    openPromise = null;
                    return resolve(false);
                }
                req.onupgradeneeded = () => {
                    try {
                        stores.forEach((storeName) => {
                            if (!req.result.objectStoreNames.contains(storeName)) {
                                req.result.createObjectStore(storeName);
                            }
                        });
                    } catch (e) {
                        degrade('upgrade-failed');
                    }
                };
                req.onsuccess = () => {
                    if (settled) return;
                    settled = true;
                    scheduler.clearTimeout(timer);
                    db = req.result;
                    openPromise = null;
                    resolve(true);
                };
                req.onerror = () => {
                    if (settled) return;
                    settled = true;
                    scheduler.clearTimeout(timer);
                    // A VersionError here means a newer build already owns
                    // this database: refuse rather than force it open.
                    degrade('open-error');
                    openPromise = null;
                    resolve(false);
                };
                req.onblocked = () => { /* blocked by another tab: the timeout answers */ };
            });
            return openPromise;
        }

        /** Run one request inside a transaction and report its outcome. */
        function run(storeName, mode, work) {
            return doOpen().then((ok) => {
                if (!ok) return { ok: false, reason: degraded || 'unavailable' };
                return new Promise((resolve) => {
                    let settled = false;
                    const timer = scheduler.setTimeout(() => {
                        if (settled) return;
                        settled = true;
                        stats.timeouts++;
                        degrade('write-timeout');
                        resolve({ ok: false, reason: 'write-timeout' });
                    }, timeoutMs);
                    const finish = (result) => {
                        if (settled) return;
                        settled = true;
                        scheduler.clearTimeout(timer);
                        resolve(result);
                    };
                    let tx;
                    try {
                        tx = db.transaction(storeName, mode);
                    } catch (e) {
                        degrade('transaction-failed');
                        return finish({ ok: false, reason: 'transaction-failed' });
                    }
                    let result;
                    tx.oncomplete = () => finish({ ok: true, value: result });
                    tx.onerror = () => { degrade('write-error'); finish({ ok: false, reason: 'write-error' }); };
                    tx.onabort = () => { degrade('write-aborted'); finish({ ok: false, reason: 'write-aborted' }); };
                    try {
                        work(tx.objectStore(storeName), (v) => { result = v; });
                    } catch (e) {
                        degrade('request-failed');
                        finish({ ok: false, reason: 'request-failed' });
                    }
                });
            });
        }

        return {
            name: name,
            version: version,
            mode: 'indexeddb',
            readOnly: readOnly,
            stores: stores.slice(),
            open: doOpen,
            isAvailable: () => !degraded,
            reason: () => degraded,
            get: (key) => {
                if (!idb || degraded) return Promise.resolve(null);
                stats.gets++;
                return run(primary, 'readonly', (store, set) => {
                    const req = store.get(key);
                    req.onsuccess = () => set(req.result === undefined ? null : req.result);
                    req.onerror = () => set(null);
                }).then((r) => (r.ok ? r.value : null));
            },
            put: (key, value) => {
                if (readOnly) return Promise.resolve({ ok: false, reason: 'read-only' });
                if (!idb) return Promise.resolve({ ok: false, reason: degraded || 'unavailable' });
                stats.puts++;
                if (degraded) return Promise.resolve({ ok: false, reason: degraded });
                return run(primary, 'readwrite', (store) => { store.put(value, key); })
                    .then((r) => { if (r.ok) stats.writes++; return r; });
            },
            remove: (key) => {
                if (readOnly) return Promise.resolve({ ok: false, reason: 'read-only' });
                if (!idb || degraded) return Promise.resolve({ ok: false, reason: degraded || 'unavailable' });
                stats.deletes++;
                return run(primary, 'readwrite', (store) => { store.delete(key); });
            },
            getAll: () => {
                if (!idb || degraded) return Promise.resolve([]);
                return run(primary, 'readonly', (store, set) => {
                    if (typeof store.getAll !== 'function') return set([]);
                    const req = store.getAll();
                    req.onsuccess = () => set(req.result || []);
                    req.onerror = () => set([]);
                }).then((r) => (r.ok && Array.isArray(r.value) ? r.value : []));
            },
            getMeta: (key) => {
                if (!idb || degraded) return Promise.resolve(null);
                const metaStore = stores.indexOf('meta') !== -1 ? 'meta' : primary;
                return run(metaStore, 'readonly', (store, set) => {
                    const req = store.get(key);
                    req.onsuccess = () => set(req.result === undefined ? null : req.result);
                    req.onerror = () => set(null);
                }).then((r) => (r.ok ? r.value : null));
            },
            putMeta: (key, value) => {
                if (readOnly) return Promise.resolve({ ok: false, reason: 'read-only' });
                if (!idb || degraded) return Promise.resolve({ ok: false, reason: degraded || 'unavailable' });
                const metaStore = stores.indexOf('meta') !== -1 ? 'meta' : primary;
                return run(metaStore, 'readwrite', (store) => { store.put(value, key); });
            },
            stats: () => Object.assign({}, stats),
            close: () => {
                if (db && typeof db.close === 'function') db.close();
                db = null;
            },
        };
    }

    /** Read-only IndexedDB adapter pointed at v1's database (import only). */
    function v1ImportStorage(options) {
        options = options || {};
        return idbStorage(Object.assign({}, options, {
            name: V1_NAMESPACE,
            version: options.version || 1,
            stores: [V1_STORE],
            primaryStore: V1_STORE,
            readOnly: true,
        }));
    }

    // ── The session ───────────────────────────────────────────────
    /**
     * create({
     *   guildId, documentId,           // identity of the draft being edited
     *   now,                           // injected clock (ms) — required
     *   storage,                       // adapter; default: IndexedDB
     *   scheduler, idleMs,             // coalescing timer
     *   ids,                           // id factory for imported documents
     *   onState,                       // fn({state, ...}) — the UI's future hook
     * })
     */
    function create(options) {
        options = options || {};
        if (typeof options.now !== 'function') {
            throw new TypeError('drafts.create needs opts.now — time is injected, never read');
        }
        const now = options.now;
        const idleMs = options.idleMs == null ? DEFAULT_IDLE_MS : options.idleMs;
        const scheduler = options.scheduler || {
            setTimeout: (fn, ms) => setTimeout(fn, ms),
            clearTimeout: (id) => clearTimeout(id),
        };
        const storage = options.storage || idbStorage({ scheduler: scheduler });
        const ids = options.ids || model.createIdFactory();

        let guildId = options.guildId === undefined ? null : options.guildId;
        let documentId = options.documentId || null;
        const makeId = options.newDocumentId || (() => newDocumentId({ now: now, entropy: options.entropy }));
        let revision = 0;
        let createdAt = null;
        let lastSavedHash = null;       // hash of what storage holds
        // The DOCUMENT that hash describes — the last successfully persisted
        // version of the CURRENT draft identity, held as a private clone.
        //
        // It exists for exactly one question, the one the hash cannot answer:
        // "what was the message before the edit the user wants to discard?".
        // It is set only where persistence is KNOWN to have succeeded (a
        // confirmed write, a successful load) and cleared wherever the saved
        // identity is deliberately reset (start/use/attach) — so it can never
        // describe a document that belongs to a different draft.
        //
        // It is never a second copy of the canonical document: the store still
        // owns what is being edited, and reads it from here at most once, at
        // the moment a user asks to go back.
        let lastSavedDocument = null;
        let lastError = null;
        let lastUpdatedAt = null;
        let currentDocument = options.document || null;
        let blocked = null;             // { reason, key, record } — writes refused while set
        let destroyed = false;
        let timer = null;
        let inFlight = null;
        let pendingAfterFlight = false;
        let saveCount = 0;
        let unsubscribe = null;
        let boundStore = null;
        let lifecycleTarget = null;
        const lifecycleHandlers = [];
        const observers = [];

        function key() {
            return documentId ? draftKey(guildId, documentId) : null;
        }

        /**
         * Start a NEW draft with an identity of its own. Creating a draft
         * never touches an existing one: this only mints an id and takes the
         * document, and nothing is written until the caller saves.
         */
        function start(document, opts) {
            opts = opts || {};
            documentId = opts.documentId || makeId();
            if (opts.guildId !== undefined) guildId = opts.guildId;
            currentDocument = document ? model.normalizeDocument(document) : model.blankMessageDocument({ ids: ids });
            lastSavedHash = null;
            lastSavedDocument = null;      // a NEW identity has nothing persisted yet
            revision = 0;
            createdAt = null;
            lastUpdatedAt = null;
            blocked = null;
            lastError = null;
            cancelScheduled();
            notify();
            return { documentId: documentId, key: draftKey(guildId, documentId), document: currentDocument };
        }

        function notify() {
            const snapshot = state();
            observers.slice().forEach((fn) => {
                try { fn(snapshot); } catch (e) { /* an observer must never break a save */ }
            });
            return snapshot;
        }

        function state() {
            const dirty = isDirty();
            const label = !dirty
                ? (lastSavedHash === null ? 'clean' : 'saved')
                : (lastError ? 'error' : (inFlight ? 'saving' : 'dirty'));
            return {
                state: blocked ? 'blocked' : label,
                dirty: dirty,
                saving: !!inFlight,
                blocked: blocked ? blocked.reason : null,
                degraded: storage.isAvailable ? (storage.isAvailable() ? null : storage.reason()) : null,
                lastError: lastError ? { reason: lastError.reason, message: lastError.message || null } : null,
                revision: revision,
                documentId: documentId,
                guildId: guildId,
                key: key(),
                updatedAt: lastUpdatedAt,
                writes: saveCount,
            };
        }

        function isDirty() {
            if (!currentDocument) return false;
            // Nothing has ever been written: a brand-new draft counts as
            // dirty only once it has content (an untouched blank document
            // must not create a record just by being open).
            const hash = model.hashDocument(currentDocument);
            if (lastSavedHash === null) return model.documentHasContent(currentDocument);
            return hash !== lastSavedHash;
        }

        if (typeof options.onState === 'function') observers.push(options.onState);

        function onState(fn) {
            observers.push(fn);
            return function off() {
                const i = observers.indexOf(fn);
                if (i !== -1) observers.splice(i, 1);
            };
        }

        // ── Scheduling ────────────────────────────────────────────
        function cancelScheduled() {
            if (timer !== null) {
                scheduler.clearTimeout(timer);
                timer = null;
                return true;
            }
            return false;
        }

        function schedule() {
            if (destroyed) return false;
            cancelScheduled();                  // a burst collapses into ONE write
            timer = scheduler.setTimeout(() => {
                timer = null;
                saveNow();
            }, idleMs);
            return true;
        }

        // ── Writes ────────────────────────────────────────────────
        function saveNow(opts) {
            opts = opts || {};
            if (destroyed) return Promise.resolve({ ok: false, reason: 'destroyed' });
            cancelScheduled();
            // The preservation guard is ABSOLUTE. `force` means "write even if
            // clean", nothing more — it must never lift a guard, because the
            // only thing allowed to replace a preserved record is the explicit,
            // user-driven resolveGuard('replace').
            if (blocked) {
                return Promise.resolve({ ok: false, reason: blocked.reason, blocked: true, preserved: blocked.record });
            }
            if (!currentDocument) return Promise.resolve({ ok: false, reason: 'no-document' });
            if (!documentId) return Promise.resolve({ ok: false, reason: 'no-document-id' });
            if (!isDirty() && !opts.force) {
                // Nothing to write — but cancelScheduled() above may just have
                // cleared a pending write, which is state subscribers show
                // ("the newest edit is not written yet"). Without this notify a
                // skipped save leaves the status bar claiming unsaved work for
                // content that storage already holds.
                notify();
                return Promise.resolve({ ok: true, skipped: true, reason: 'clean', key: key() });
            }
            if (inFlight) {
                // One write at a time; the newest state is saved after it.
                pendingAfterFlight = true;
                return inFlight;
            }

            let record;
            try {
                record = buildRecord({
                    guildId: guildId,
                    documentId: documentId,
                    document: currentDocument,
                    now: now(),
                    previous: { revision: revision, createdAt: createdAt },
                });
            } catch (err) {
                lastError = { reason: 'not-serializable', message: err.message || String(err) };
                notify();
                return Promise.resolve({ ok: false, reason: 'not-serializable', error: err, path: err.path || null });
            }

            const targetKey = record.key;
            inFlight = storage.put(targetKey, record).then((result) => {
                inFlight = null;
                if (result && result.ok) {
                    revision = record.revision;
                    createdAt = record.createdAt;
                    lastUpdatedAt = record.updatedAt;
                    lastSavedHash = record.documentHash;
                    // The write is CONFIRMED: this record's document is now the
                    // last persisted version of this draft. Cloned on the way in
                    // — the caller's document object may be edited in place by a
                    // later bug, and a clone cannot follow it there.
                    lastSavedDocument = model.cloneDocument(record.document);
                    lastError = null;
                    saveCount++;
                    // Confirm the write to the store by HASH, never by handing it
                    // a document. This call is asynchronous: `record` may be
                    // document A while `currentDocument` is already B, because
                    // the user typed during the write. markSavedHash() records
                    // "what storage holds is A" and leaves the store's document
                    // (B), its history and its undo stack exactly as they are —
                    // so the store stays dirty until B itself is written.
                    // Handing the document over instead would either rewind the
                    // store to A or (markSaved(currentDocument)) claim B is on
                    // disk when it is not.
                    if (boundStore && boundStore.markSavedHash) {
                        boundStore.markSavedHash(record.documentHash);
                    }
                    if (pendingAfterFlight) { pendingAfterFlight = false; schedule(); }
                    notify();
                    return { ok: true, key: targetKey, revision: revision, updatedAt: lastUpdatedAt };
                }
                lastError = { reason: (result && result.reason) || 'write-failed' };
                if (pendingAfterFlight) { pendingAfterFlight = false; schedule(); }
                notify();
                return { ok: false, reason: lastError.reason, key: targetKey };
            }, (err) => {
                inFlight = null;
                lastError = { reason: 'write-failed', message: err && err.message };
                notify();
                return { ok: false, reason: 'write-failed' };
            });
            notify();
            return inFlight;
        }

        // ── Reading ───────────────────────────────────────────────
        /**
         * Never throws. `status` is one of:
         *   empty | ok | repaired | corrupt | future | foreign | unsupported | unavailable
         * Anything that is not usable is PRESERVED in the result (and in
         * storage) and raises the write guard so nothing overwrites it.
         */
        function load(target) {
            const want = target || {};
            if (want.guildId !== undefined) guildId = want.guildId;
            if (want.documentId !== undefined) documentId = want.documentId;
            if (!documentId) return Promise.resolve({ ok: false, status: 'no-document-id' });
            const lookupKey = draftKey(guildId, documentId);
            if (storage.isAvailable && !storage.isAvailable()) {
                return Promise.resolve({
                    ok: false, status: 'unavailable', key: lookupKey,
                    reason: storage.reason ? storage.reason() : 'unavailable',
                });
            }
            return storage.get(lookupKey).then((raw) => {
                if (storage.isAvailable && !storage.isAvailable()) {
                    // The read itself failed (open error, timeout): that is a
                    // degraded environment, NOT an empty draft. Reporting
                    // "empty" here would invite a save that overwrites data.
                    return {
                        ok: false, status: 'unavailable', key: lookupKey,
                        reason: storage.reason ? storage.reason() : 'unavailable',
                    };
                }
                if (raw === null || raw === undefined) {
                    return { ok: true, status: 'empty', key: lookupKey };
                }
                const verdict = validateRecord(raw, { key: lookupKey });
                if (!verdict.ok) {
                    blocked = { reason: verdict.status, key: lookupKey, record: raw };
                    const out = {
                        ok: false,
                        status: verdict.status,
                        key: lookupKey,
                        problems: verdict.problems,
                        preserved: true,
                        record: raw,
                    };
                    notify();
                    return out;
                }
                // Defensive on purpose: this function must never throw, so a
                // verdict that somehow lacks its metadata degrades into the
                // most conservative reading rather than a crash inside load.
                blocked = null;
                const meta = verdict.meta || {};
                currentDocument = verdict.document || null;
                revision = Number.isInteger(meta.revision) ? meta.revision : 0;
                createdAt = meta.createdAt || null;
                lastUpdatedAt = meta.updatedAt || null;
                lastSavedHash = meta.documentHash || null;
                // The record was read successfully, so this document IS what
                // storage holds for this identity — the baseline a discard
                // returns to. (meta.documentHash may be absent on a repaired
                // record; status 'ok'/'repaired' is the verdict that matters.)
                lastSavedDocument = verdict.document ? model.cloneDocument(verdict.document) : null;
                lastError = null;
                notify();
                return {
                    ok: true,
                    status: verdict.status,          // 'ok' | 'repaired'
                    key: lookupKey,
                    document: verdict.document,
                    repairs: verdict.repairs || [],
                    problems: verdict.problems,
                    hashMatches: verdict.hashMatches,
                    record: raw,
                    meta: verdict.meta || null,
                };
            }, () => ({ ok: false, status: 'unavailable', key: lookupKey }));
        }

        /**
         * Take ownership of the document this session edits. Called by the
         * page (or by load()); marks the state clean against storage.
         */
        function use(document, opts) {
            opts = opts || {};
            currentDocument = model.normalizeDocument(document);
            // Adopt the document's own identity when the session has none:
            // the key must describe the document actually being edited.
            if (opts.documentId) documentId = opts.documentId;
            else if (!documentId && currentDocument.id) documentId = currentDocument.id;
            if (opts.guildId !== undefined) guildId = opts.guildId;
            if (opts.saved !== false) {
                lastSavedHash = null;
                lastSavedDocument = null;  // the saved identity is being reset
            }
            notify();
            return currentDocument;
        }

        /** The document changed: mark dirty and (re)schedule one write. */
        function changed(document) {
            if (document) currentDocument = document;
            if (destroyed) return false;
            notify();
            if (!isDirty()) {
                // While a write is in flight the clean verdict is PROVISIONAL:
                // lastSavedHash still describes the state from before that
                // write, so an edit landing in this window can look like
                // "nothing worth storing" and never be queued. The visible
                // case is clearing the message back to nothing while the first
                // save is still going: it read as clean (no content, nothing
                // ever saved) and the clearing was silently dropped, so a
                // reload brought the old text back. Queue a re-check instead —
                // the write's own completion decides, with the fresh hash.
                if (inFlight) pendingAfterFlight = true;
                cancelScheduled();
                return false;
            }
            return schedule();
        }

        /** Two-way wiring with the Step-1 store: edits schedule, saves confirm. */
        function attach(store) {
            // Attaching twice must not leak the first subscription: the
            // previous one is removed before a new one is taken.
            if (unsubscribe) { unsubscribe(); unsubscribe = null; }
            boundStore = store;
            const doc = store.getDocument ? store.getDocument() : null;
            if (doc && !currentDocument) currentDocument = doc;
            if (doc && store.isDirty && !store.isDirty()) {
                lastSavedHash = null;
                lastSavedDocument = null;  // attaching re-establishes what "saved" means
            }
            unsubscribe = store.subscribe(
                (s) => s.document,
                (next) => { changed(next); }
            );
            return function detach() {
                if (unsubscribe) unsubscribe();
                unsubscribe = null;
                boundStore = null;
            };
        }

        /** Flush on the boundaries a page actually gets. */
        function bindLifecycle(target) {
            lifecycleTarget = target;
            const flush = (why) => {
                if (destroyed) return;
                if (!isDirty() && !inFlight) return;
                saveNow({ reason: why });
            };
            const onPageHide = () => flush('pagehide');
            const onVisibility = () => {
                if (target.visibilityState === 'hidden') flush('hidden');
            };
            const onFreeze = () => flush('freeze');
            target.addEventListener('pagehide', onPageHide);
            target.addEventListener('visibilitychange', onVisibility);
            target.addEventListener('freeze', onFreeze);
            lifecycleHandlers.push(
                () => target.removeEventListener('pagehide', onPageHide),
                () => target.removeEventListener('visibilitychange', onVisibility),
                () => target.removeEventListener('freeze', onFreeze)
            );
            return function unbind() {
                lifecycleHandlers.splice(0).forEach((off) => off());
                lifecycleTarget = null;
            };
        }

        /** Explicit override for a preserved record (the UI asks the user first). */
        function resolveGuard(action) {
            // 'keep' means "leave the preserved record alone": it never
            // resolves, whether or not a record is currently guarded.
            if (action !== 'replace') return false;
            if (!blocked) return true;
            // Continue the preserved record's sequence rather than restarting
            // it: the user is replacing the CONTENT, and the revision history
            // should still move forward.
            const preserved = blocked.record;
            if (preserved && typeof preserved === 'object') {
                if (Number.isInteger(preserved.revision)) revision = preserved.revision;
                if (preserved.createdAt) createdAt = preserved.createdAt;
            }
            blocked = null;
            notify();
            return true;
        }

        // ── v1 import (read-only, explicit, lossless in what it reports) ──
        /**
         * Read v1's draft and turn it into a v2 document. v1's database is
         * opened READ-ONLY: nothing here can modify or delete it, and the
         * result reports every part that was not imported rather than
         * dropping it silently.
         */
        function importFromV1(adapter) {
            const source = adapter || v1ImportStorage({ scheduler: scheduler });
            if (source.isAvailable && !source.isAvailable()) {
                return Promise.resolve({
                    ok: false, status: 'unavailable', reason: source.reason ? source.reason() : 'unavailable',
                    namespace: V1_NAMESPACE,
                });
            }
            return source.get(V1_KEY).then((raw) => {
                if (source.isAvailable && !source.isAvailable()) {
                    return {
                        ok: false, status: 'unavailable', namespace: V1_NAMESPACE,
                        reason: source.reason ? source.reason() : 'unavailable',
                    };
                }
                if (!raw || typeof raw !== 'object') {
                    return { ok: true, status: 'empty', namespace: V1_NAMESPACE, key: V1_KEY };
                }
                const document = model.fromEditorDocument({
                    content: typeof raw.content === 'string' ? raw.content : '',
                    embeds: Array.isArray(raw.embeds) ? raw.embeds : [],
                }, { ids: ids, guildId: guildId });
                const attachments = Array.isArray(raw.attachments) ? raw.attachments : [];
                const warnings = [];
                if (attachments.length) {
                    // Attachments are phase 2 (the asset store). They are
                    // NOT silently discarded: they are counted and named so
                    // the UI can tell the user what did not come across.
                    warnings.push({
                        kind: 'attachments-not-imported',
                        count: attachments.length,
                        names: attachments.map((a) => (a && a.name) || 'unnamed').slice(0, 20),
                        note: 'attachment files are not part of the v2 draft yet (phase 2 assets)',
                    });
                }
                return {
                    ok: true,
                    status: 'imported',
                    namespace: V1_NAMESPACE,
                    key: V1_KEY,
                    document: document,
                    source: {
                        revision: null,
                        updatedAt: (raw && raw.ts) || null,
                        hadContent: !!raw.content,
                        embedCount: Array.isArray(raw.embeds) ? raw.embeds.length : 0,
                    },
                    warnings: warnings,
                };
            }, () => ({ ok: false, status: 'unavailable', namespace: V1_NAMESPACE }));
        }

        // ── Listing / meta (the future library and "reopen last" seam) ────
        /**
         * Metadata for the drafts this build can see, newest first.
         * listDrafts() defaults to THIS session's guild; pass
         * { guildId: 'x' } for another one, or { all: true } for every guild.
         */
        function listDrafts(filter) {
            filter = filter || {};
            if (storage.isAvailable && !storage.isAvailable()) {
                return Promise.resolve({ ok: false, status: 'unavailable', drafts: [] });
            }
            const norm = (g) => (g === null || g === undefined || g === '' ? 'global' : String(g));
            const scoped = filter.all === true ? null : norm('guildId' in filter ? filter.guildId : guildId);
            return storage.getAll().then((records) => {
                const drafts = (records || [])
                    .filter((r) => r && typeof r === 'object' && r.namespace === V2_NAMESPACE)
                    .filter((r) => scoped === null || norm(r.guildId) === scoped)
                    .map((r) => ({
                        key: r.key,
                        guildId: r.guildId === undefined ? null : r.guildId,
                        documentId: r.documentId,
                        revision: Number.isInteger(r.revision) ? r.revision : 0,
                        createdAt: r.createdAt || null,
                        updatedAt: r.updatedAt || null,
                        documentHash: r.documentHash || null,
                        schemaVersion: r.schemaVersion,
                    }))
                    .sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
                return { ok: true, status: 'ok', drafts: drafts };
            }, () => ({ ok: false, status: 'unavailable', drafts: [] }));
        }

        const meta = {
            key: (name) => metaKey(guildId, name),
            get: (name) => {
                if (storage.isAvailable && !storage.isAvailable()) return Promise.resolve(null);
                if (typeof storage.getMeta === 'function') return storage.getMeta(metaKey(guildId, name));
                return Promise.resolve(null);
            },
            set: (name, value) => {
                if (typeof storage.putMeta !== 'function') return Promise.resolve({ ok: false, reason: 'no-meta-store' });
                return storage.putMeta(metaKey(guildId, name), value);
            },
            lastDocumentId: (guildFor) => {
                const g = guildFor === undefined ? guildId : guildFor;
                if (storage.isAvailable && !storage.isAvailable()) return Promise.resolve(null);
                if (typeof storage.getMeta !== 'function') return Promise.resolve(null);
                return storage.getMeta(metaKey(g, 'last')).then((v) => (v && v.documentId) || null);
            },
            rememberDocument: (guildFor, id) => {
                const g = guildFor === undefined ? guildId : guildFor;
                if (typeof storage.putMeta !== 'function') return Promise.resolve({ ok: false, reason: 'no-meta-store' });
                return storage.putMeta(metaKey(g, 'last'), { documentId: id, updatedAt: now() });
            },
        };

        // ── Teardown ──────────────────────────────────────────────
        function destroy(teardownOpts) {
            teardownOpts = teardownOpts || {};
            if (destroyed) return Promise.resolve({ ok: true, reason: 'already-destroyed' });
            cancelScheduled();
            const finish = (result) => {
                destroyed = true;
                if (unsubscribe) { unsubscribe(); unsubscribe = null; }
                lifecycleHandlers.splice(0).forEach((off) => off());
                lifecycleTarget = null;
                observers.length = 0;
                notify();
                return result;
            };
            if (teardownOpts.save !== false && isDirty()) {
                return saveNow({ force: false }).then(finish, finish);
            }
            return Promise.resolve(finish({ ok: true, skipped: true }));
        }

        return {
            // identity
            key: key,
            documentId: () => documentId,
            guildId: () => guildId,
            use: use,
            start: start,
            newDocumentId: makeId,
            // lifecycle
            attach: attach,
            bindLifecycle: bindLifecycle,
            changed: changed,
            schedule: schedule,
            saveNow: saveNow,
            load: load,
            listDrafts: listDrafts,
            importFromV1: importFromV1,
            meta: meta,
            // state (for the future UI; nothing here renders)
            state: state,
            isDirty: isDirty,
            onState: onState,
            document: () => currentDocument,
            resolveGuard: resolveGuard,
            guard: () => (blocked ? { reason: blocked.reason, key: blocked.key } : null),
            savedHash: () => lastSavedHash,
            /**
             * The last successfully persisted document for the CURRENT draft
             * identity, or null when nothing has been established yet. Always a
             * fresh clone: a caller may keep it, compare it or hand it to the
             * store, and cannot reach session state through it.
             */
            savedDocument: () => (lastSavedDocument ? model.cloneDocument(lastSavedDocument) : null),
            destroy: destroy,
            // test/debug seams
            storage: () => storage,
            pendingSave: () => timer !== null,
        };
    }

    NERO.embed.drafts = {
        create: create,
        idbStorage: idbStorage,
        v1ImportStorage: v1ImportStorage,
        draftKey: draftKey,
        parseKey: parseKey,
        newDocumentId: newDocumentId,
        metaKey: metaKey,
        validateRecord: validateRecord,
        buildRecord: buildRecord,
        assertSerializable: assertSerializable,
        serializeRecord: serializeRecord,
        toStorable: toStorable,
        V1_NAMESPACE: V1_NAMESPACE,
        V1_STORE: V1_STORE,
        V1_KEY: V1_KEY,
        V2_NAMESPACE: V2_NAMESPACE,
        V2_STORES: V2_STORES,
        KEY_PREFIX: KEY_PREFIX,
        RECORD_VERSION: RECORD_VERSION,
        DEFAULT_IDLE_MS: DEFAULT_IDLE_MS,
    };
})(window.NERO);
