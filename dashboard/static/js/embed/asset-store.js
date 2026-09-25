/* ═══════════════════════════════════════════════════════════════
   Message Builder v2 — the asset BYTE store + object-URL lifecycle
   (phase 2, step 7b).

   assets.js (7a) decides what a file IS: identity, format, name,
   limits, records, and which ids a document references. It never
   touches bytes beyond the ones it is handed, and it never stores
   anything. This module is the other half of the asset layer:

       pick a file → identify() → assetId
       putBytes()  → IndexedDB 'assets' store          (bytes live here)
       urlFor()    → blob: URL for the preview <img>   (bytes borrowed)
       release*()  → the URL goes away again

   WHY BYTES ARE NOT IN THE DOCUMENT
   ---------------------------------
   A MessageDocument is normalized, hashed, serialized into the draft
   and compared for dirty-ness, so it may only contain JSON. Bytes in
   it would break all four: drafts.assertSerializable() refuses a Blob
   outright, and a Uint8Array would silently become {"0":137,...} on
   the way to storage. So the document keeps METADATA (assetId,
   filename, mime, bytes) and the bytes live in their own object store,
   written once per asset — never on a keystroke. Nothing in this file
   can reach a document: it takes ids and bytes and returns ids, bytes
   and blob: URLs.

   STORAGE: THE APP'S OWN ADAPTER (one persistence path)
   ----------------------------------------------------
   bytesStorage() does not open IndexedDB itself. It builds the existing
   adapter (drafts.js → idbStorage) against the SAME v2 database and the
   SAME store list, with 'assets' as its primary store. That is the
   whole reuse: one opener, one retry/timeout/degrade policy, one place
   that knows the database name. The store's own copy of the name
   ('assets') is checked against drafts.V2_STORES by the tests, so the
   two cannot drift.

   WHEN STORAGE IS NOT THERE
   -------------------------
   IndexedDB missing, refused, timing out or failing a write is a real
   state (drafts.js latches it as `degraded`). The answer here is: keep
   the bytes in THIS INSTANCE's memory, serve them for the rest of the
   session, and say so — every result carries `persisted: false` plus
   the adapter's own reason, and mode() reports `memory` /
   `persistent: false`. The bytes are then gone after a reload, and
   getBytes() says `missing`. Nothing is ever reported as stored when
   it is not, and nothing throws: a store that cannot answer must still
   answer.

   OBJECT-URL LIFECYCLE (one URL per asset, revoked exactly once)
   -------------------------------------------------------------
     • urlFor(id) mints ONE blob: URL for a given asset + content type.
       Repeating the lookup returns the same URL and mints nothing —
       the preview patch runs on every keystroke and must not leak a
       URL per render (the v1 page's known leak).
     • A different content type means a different blob, so the old URL
       is revoked and replaced (this is the only re-mint path; bytes
       cannot change under an id — see the id check in putBytes()).
     • release(id) drops the URL and keeps the bytes; remove(id) drops
       both; releaseAll() is the teardown; destroy() is teardown + a
       closed connection, after which the store refuses every call
       rather than serving from a dead instance.
     • cachedUrl(id) is the synchronous render-path read: the preview
       resolves its sources synchronously, so 7e warms the cache with
       urlFor() and lets the render use cachedUrl() — it never mints.
     • Missing bytes never produce a URL. There is no "broken image"
       URL, no placeholder data: URL, no empty string standing in for
       one: urlFor() reports why it has nothing (missing / unavailable
       / corrupt / urls-unavailable).

   IDENTITY IS CHECKED, NOT TRUSTED
   --------------------------------
   Bytes are content-addressed: putBytes() recomputes the digest and
   refuses the write when the supplied assetId is not the id of those
   bytes. That is what makes "one id → one byte sequence" true for the
   lifetime of the store, and what lets the URL cache above be safe.

   PURITY BOUNDARY
   ---------------
   No network, no DOM query, no clock, no randomness, no module-level
   mutable state: every byte a store owns lives in ITS instance (or in
   the storage adapter it was handed). create() is the only entry
   point, and two stores never see each other's memory. The one browser
   capability that cannot live without globals — URL.createObjectURL +
   Blob — is the injectable `urls` factory, whose default is built from
   window at call time and reports `urls-unavailable` when the browser
   has neither.
   ═══════════════════════════════════════════════════════════════ */

(function (NERO) {
    'use strict';

    const core = NERO.embed.assets;
    if (!core) throw new Error('embed/asset-store.js needs embed/assets.js loaded first');

    /** The object store holding bytes, inside the v2 database. */
    const STORE_NAME = 'assets';
    /** Bumped only if the stored byte entry's shape ever changes. */
    const ENTRY_VERSION = 1;
    /** What a Blob is typed as when the caller does not know better. */
    const NEUTRAL_MIME = 'application/octet-stream';
    const HEX_RE = /^[0-9a-f]{64}$/;

    // ── Byte plumbing (structural, so a second realm works) ───────
    /**
     * Bytes, from anything that reasonably holds bytes. Deliberately
     * structural (`ArrayBuffer.isView`, the ArrayBuffer tag, an Array
     * of octets) and not `instanceof`: a Uint8Array built in another
     * realm — a test harness, a worker — fails this realm's
     * instanceof, which is how a byte store starts "not recognising"
     * perfectly good bytes. Blob/File are NOT read here: reading them
     * is asynchronous, and silently returning null for one would look
     * exactly like an empty file.
     */
    function toBytes(value) {
        if (!value) return null;
        if (typeof value === 'string') return null;
        if (ArrayBuffer.isView(value)) {
            return new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
        }
        if (Object.prototype.toString.call(value) === '[object ArrayBuffer]') {
            return new Uint8Array(value);
        }
        if (Array.isArray(value)) {
            const out = new Uint8Array(value.length);
            for (let i = 0; i < value.length; i++) out[i] = value[i] & 0xff;
            return out;
        }
        return null;
    }

    /** A private copy, as an ArrayBuffer. Stored bytes are never aliased. */
    function copyBuffer(view) {
        const copy = new Uint8Array(view.length);
        copy.set(view);
        return copy.buffer;
    }

    function normaliseMime(value) {
        if (typeof value !== 'string') return '';
        const mime = value.trim().toLowerCase();
        return /^[a-z0-9][a-z0-9!#$&^_.+-]*\/[a-z0-9][a-z0-9!#$&^_.+-]*$/.test(mime) ? mime : '';
    }

    /**
     * The ONE way this module reaches storage: the app's existing
     * adapter, pointed at the assets store. Returns null when drafts.js
     * is not loaded — an honest "no storage here", never a second
     * opener.
     */
    function bytesStorage(options) {
        const drafts = NERO.embed.drafts;
        if (!drafts || typeof drafts.idbStorage !== 'function') return null;
        const opts = options || {};
        // The database, the store list and the store this adapter reads
        // and writes are the v2 contract, not caller preferences: an
        // adapter aimed at 'drafts' would put bytes in the document
        // store. Connection knobs (indexedDB, scheduler, timeoutMs,
        // version) pass through untouched.
        return drafts.idbStorage({
            name: drafts.V2_NAMESPACE,
            stores: drafts.V2_STORES,
            primaryStore: STORE_NAME,
            indexedDB: opts.indexedDB,
            scheduler: opts.scheduler,
            timeoutMs: opts.timeoutMs,
            version: opts.version,
        });
    }

    /**
     * The default Object-URL factory. Built from the window at call
     * time and null when the browser has no URL/Blob — in which case
     * every urlFor() reports `urls-unavailable` instead of pretending.
     */
    function browserUrls(scope) {
        const win = scope || (typeof window !== 'undefined' ? window : null);
        const urls = win && (win.URL || win.webkitURL);
        const BlobCtor = win && win.Blob;
        if (!urls || !BlobCtor) return null;
        if (typeof urls.createObjectURL !== 'function' || typeof urls.revokeObjectURL !== 'function') return null;
        return {
            kind: 'browser',
            create: (bytes, mime) => urls.createObjectURL(new BlobCtor([bytes], { type: mime || NEUTRAL_MIME })),
            revoke: (url) => urls.revokeObjectURL(url),
        };
    }

    /** The stored entry shape, or null when the value cannot be trusted. */
    function readEntry(raw, assetId) {
        if (!raw || typeof raw !== 'object') return null;
        if (raw.v !== ENTRY_VERSION) return null;
        if (raw.assetId !== assetId) return null;                 // a key pointing at someone else's record
        if (typeof raw.sha256 !== 'string' || !HEX_RE.test(raw.sha256)) return null;
        const view = toBytes(raw.bytes);
        if (!view) return null;
        if (raw.byteLength !== view.length) return null;          // truncated or padded: not the bytes we stored
        return {
            v: ENTRY_VERSION,
            assetId: assetId,
            sha256: raw.sha256,
            byteLength: view.length,
            mime: normaliseMime(raw.mime),
            bytes: view,
        };
    }

    /**
     * A byte store. Everything mutable lives in this closure: two
     * instances share nothing but the modules they were built from.
     */
    function create(options) {
        const settings = options || {};
        if (typeof settings.hash !== 'function' && typeof core.sha256Hex !== 'function') {
            throw new Error('embed/asset-store.js needs a sha256 implementation (assets.sha256Hex)');
        }
        const hash = settings.hash || core.sha256Hex;
        const storage = settings.storage === undefined ? bytesStorage(settings.adapter) : settings.storage;
        const urls = settings.urls === undefined ? browserUrls(settings.scope) : settings.urls;

        // Bytes that no persistent storage holds (degraded mode only).
        const memory = {};
        // assetId → {url, mime}: the live object URLs, and the type each was minted for.
        const urlCache = {};
        const stats = {
            puts: 0, writes: 0, deletes: 0, duplicates: 0, refused: 0,
            reads: 0, misses: 0, corrupt: 0, mints: 0, revokes: 0,
            urlHits: 0, urlMisses: 0, failures: 0,
        };
        let dead = false;

        function refuse(assetId, reason, extra) {
            stats.refused++;
            return Object.assign({ ok: false, assetId: assetId || null, reason: reason }, extra || {});
        }

        function available() {
            if (!storage || typeof storage.get !== 'function') return false;
            return typeof storage.isAvailable === 'function' ? !!storage.isAvailable() : true;
        }

        function storageReason() {
            const why = storage && typeof storage.reason === 'function' ? storage.reason() : null;
            return why || 'storage-unavailable';
        }

        function releaseAsset(assetId) {
            const cached = urlCache[assetId];
            if (!cached) return { ok: true, assetId: assetId, revoked: false };
            revokeUrl(cached.url);
            delete urlCache[assetId];
            return { ok: true, assetId: assetId, revoked: true };
        }

        function releaseAllUrls() {
            const ids = Object.keys(urlCache).sort();
            ids.forEach((id) => {
                revokeUrl(urlCache[id].url);
                delete urlCache[id];
            });
            return { ok: true, revoked: ids.length, assetIds: ids };
        }

        function revokeUrl(url) {
            if (urls && typeof urls.revoke === 'function') {
                try { urls.revoke(url); } catch (e) { stats.failures++; }
            }
            stats.revokes++;
        }

        /** The bytes for an id, from this session's memory or from storage. */
        function read(assetId) {
            if (memory[assetId]) {
                return Promise.resolve({ ok: true, entry: memory[assetId], source: 'memory' });
            }
            if (!storage || typeof storage.get !== 'function') {
                return Promise.resolve({ ok: false, reason: 'missing' });
            }
            if (!available()) {
                return Promise.resolve({ ok: false, reason: storageReason() });
            }
            stats.reads++;
            let result;
            try {
                result = storage.get(assetId);
            } catch (e) {
                stats.failures++;
                return Promise.resolve({ ok: false, reason: 'read-threw' });
            }
            return Promise.resolve(result).then(
                (raw) => {
                    if (raw === null || raw === undefined) {
                        // Null means "nothing under this key" — unless the
                        // read itself knocked the adapter over, in which case
                        // the honest answer is that we could not tell.
                        if (!available()) return { ok: false, reason: storageReason() };
                        stats.misses++;
                        return { ok: false, reason: 'missing' };
                    }
                    const entry = readEntry(raw, assetId);
                    if (!entry) {
                        stats.corrupt++;
                        return { ok: false, reason: 'corrupt' };
                    }
                    return { ok: true, entry: entry, source: 'indexeddb' };
                },
                () => {
                    stats.failures++;
                    return { ok: false, reason: 'read-failed' };
                }
            );
        }

        /** Write one entry through the adapter. Never throws; never lies. */
        function write(assetId, entry) {
            if (!storage || typeof storage.put !== 'function') {
                return Promise.resolve({ persisted: false, reason: 'no-storage' });
            }
            if (!available()) {
                return Promise.resolve({ persisted: false, reason: storageReason() });
            }
            let result;
            try {
                result = storage.put(assetId, entry);
            } catch (e) {
                stats.failures++;
                return Promise.resolve({ persisted: false, reason: 'write-threw' });
            }
            return Promise.resolve(result).then(
                (r) => {
                    if (r && r.ok) {
                        stats.writes++;
                        return { persisted: true, reason: null };
                    }
                    return { persisted: false, reason: (r && r.reason) || 'write-failed' };
                },
                () => {
                    stats.failures++;
                    return { persisted: false, reason: 'write-failed' };
                }
            );
        }

        return {
            storeName: STORE_NAME,

            /** What this instance is, honestly. */
            mode: () => {
                const has = available();
                return {
                    mode: has ? 'indexeddb' : 'memory',
                    persistent: has,
                    available: has,
                    reason: dead ? 'destroyed' : (has ? null : (storage ? storageReason() : 'no-storage')),
                    storeName: STORE_NAME,
                    urls: !!urls,
                    dead: dead,
                };
            },

            /**
             * Store one asset's bytes under its content-addressed id.
             * The id is verified against the bytes: if they disagree the
             * write is refused, because a store that keeps "the id" and
             * "the bytes" as separate beliefs cannot keep them equal.
             */
            putBytes: (assetId, bytes, opts) => {
                if (dead) return Promise.resolve(refuse(assetId, 'destroyed'));
                if (typeof assetId !== 'string' || !assetId) return Promise.resolve(refuse(assetId, 'id-missing'));
                const view = toBytes(bytes);
                if (!view || !view.length) return Promise.resolve(refuse(assetId, 'no-bytes'));
                const sha256 = hash(view);
                const expected = core.assetIdFromSha(sha256);
                if (!expected || expected !== assetId) {
                    return Promise.resolve(refuse(assetId, 'id-mismatch', { expected: expected, actual: assetId }));
                }
                const entry = {
                    v: ENTRY_VERSION,
                    assetId: assetId,
                    sha256: sha256,
                    byteLength: view.length,
                    mime: normaliseMime(opts && opts.mime),
                    bytes: copyBuffer(view),
                };
                stats.puts++;
                // Already stored, byte-identical? Then this is the same asset
                // arriving again (four slots, one file) and there is nothing
                // to write. read() is the probe: memory first, storage second.
                return read(assetId).then((found) => {
                    const duplicate = !!(found.ok && found.entry.sha256 === sha256 &&
                        found.entry.byteLength === entry.byteLength);
                    if (duplicate) {
                        stats.duplicates++;
                        // The bytes were already here, so nothing was written —
                        // including the content type. The answer describes what
                        // the store holds, and says whether the request asked
                        // for a type other than the one already recorded.
                        const described = {
                            ok: true, assetId: assetId, duplicate: true, persisted: found.source === 'indexeddb',
                            reason: found.source === 'indexeddb' ? null : 'memory-only',
                            byteLength: entry.byteLength, sha256: sha256, mime: found.entry.mime,
                            mimeChanged: found.entry.mime !== entry.mime,
                            source: found.source,
                        };
                        // ...unless the only copy is the session one and storage
                        // has come back: then this put is the chance to promote
                        // it, and reporting "already here" while the bytes are
                        // still only in memory would strand an asset that the
                        // user believes is saved.
                        if (found.source !== 'memory') return described;
                        return write(assetId, entry).then((w) => {
                            if (w.persisted) delete memory[assetId];
                            return Object.assign(described, {
                                promoted: w.persisted, persisted: w.persisted,
                                reason: w.reason, source: w.persisted ? 'indexeddb' : 'memory',
                            });
                        });
                    }
                    return write(assetId, entry).then((w) => {
                        if (w.persisted) {
                            // Storage has it: this instance does not need a
                            // second copy for the rest of the session.
                            delete memory[assetId];
                        } else {
                            memory[assetId] = entry;
                        }
                        return {
                            ok: true, assetId: assetId, duplicate: false, persisted: w.persisted,
                            reason: w.reason, byteLength: entry.byteLength, sha256: sha256,
                            mime: entry.mime, source: w.persisted ? 'indexeddb' : 'memory',
                        };
                    });
                });
            },

            /**
             * The bytes for an id, as a fresh Uint8Array. The copy is
             * deliberate: a caller cannot corrupt what is stored by
             * writing into what it was handed.
             */
            getBytes: (assetId, opts) => {
                if (dead) return Promise.resolve(refuse(assetId, 'destroyed'));
                if (typeof assetId !== 'string' || !assetId) return Promise.resolve(refuse(assetId, 'id-missing'));
                return read(assetId).then((found) => {
                    if (!found.ok) return refuse(assetId, found.reason);
                    const view = toBytes(found.entry.bytes);
                    if (!view) {
                        stats.corrupt++;
                        return refuse(assetId, 'corrupt');
                    }
                    const copy = new Uint8Array(view.length);
                    copy.set(view);
                    if (opts && opts.verify) {
                        const actual = hash(copy);
                        if (actual !== found.entry.sha256) {
                            stats.corrupt++;
                            return refuse(assetId, 'corrupt', { expected: found.entry.sha256, actual: actual });
                        }
                    }
                    return {
                        ok: true, assetId: assetId, bytes: copy, byteLength: copy.length,
                        sha256: found.entry.sha256, mime: found.entry.mime, source: found.source,
                        verified: opts && opts.verify ? true : null,
                    };
                });
            },

            /**
             * One entry per id: is it here, where, and how big. This is
             * the reconciliation 7c/7e need after a reload (the document
             * names ids; this says which of them still have bytes).
             * Deterministic: input order, duplicates dropped.
             */
            survey: (assetIds) => {
                const ids = [];
                (Array.isArray(assetIds) ? assetIds : []).forEach((id) => {
                    if (typeof id === 'string' && id && ids.indexOf(id) === -1) ids.push(id);
                });
                if (dead) {
                    return Promise.resolve(ids.map(id => ({
                        assetId: id, present: false, availability: 'bytes-missing',
                        source: null, byteLength: null, sha256: null, mime: null, reason: 'destroyed',
                    })));
                }
                return Promise.all(ids.map(id => read(id).then(
                    (found) => ({
                        assetId: id,
                        present: found.ok,
                        availability: found.ok ? 'bytes-local' : 'bytes-missing',
                        source: found.ok ? found.source : null,
                        byteLength: found.ok ? found.entry.byteLength : null,
                        sha256: found.ok ? found.entry.sha256 : null,
                        mime: found.ok ? found.entry.mime : null,
                        reason: found.ok ? null : found.reason,
                    })
                )));
            },

            /** Drop one asset: the URL first, then the bytes. */
            remove: (assetId) => {
                if (dead) return Promise.resolve(refuse(assetId, 'destroyed'));
                if (typeof assetId !== 'string' || !assetId) return Promise.resolve(refuse(assetId, 'id-missing'));
                const revoked = releaseAsset(assetId);
                const hadMemory = !!memory[assetId];
                delete memory[assetId];
                if (!storage || typeof storage.remove !== 'function') {
                    return Promise.resolve({
                        ok: true, assetId: assetId, cleared: hadMemory,
                        revoked: revoked.revoked, persisted: false, reason: 'no-storage',
                    });
                }
                if (!available()) {
                    return Promise.resolve({
                        ok: true, assetId: assetId, cleared: hadMemory,
                        revoked: revoked.revoked, persisted: false, reason: storageReason(),
                    });
                }
                let result;
                try {
                    result = storage.remove(assetId);
                } catch (e) {
                    stats.failures++;
                    return Promise.resolve({
                        ok: true, assetId: assetId, cleared: hadMemory,
                        revoked: revoked.revoked, persisted: false, reason: 'delete-threw',
                    });
                }
                return Promise.resolve(result).then((r) => {
                    const persisted = !!(r && r.ok);
                    if (persisted) stats.deletes++;
                    return {
                        ok: true, assetId: assetId, cleared: hadMemory || persisted,
                        revoked: revoked.revoked, persisted: persisted,
                        reason: persisted ? null : ((r && r.reason) || 'delete-failed'),
                    };
                }, () => {
                    stats.failures++;
                    return {
                        ok: true, assetId: assetId, cleared: hadMemory,
                        revoked: revoked.revoked, persisted: false, reason: 'delete-failed',
                    };
                });
            },

            /**
             * A blob: URL for an asset — or an explicit reason there is
             * none. Stable for unchanged bytes: the second lookup of the
             * same asset mints nothing.
             */
            urlFor: (assetId, opts) => {
                if (dead) return Promise.resolve(refuse(assetId, 'destroyed'));
                if (typeof assetId !== 'string' || !assetId) return Promise.resolve(refuse(assetId, 'id-missing'));
                return read(assetId).then((found) => {
                    if (!found.ok) {
                        stats.urlMisses++;
                        return refuse(assetId, found.reason);
                    }
                    const mime = normaliseMime(opts && opts.mime) || found.entry.mime || NEUTRAL_MIME;
                    const cached = urlCache[assetId];
                    if (cached && cached.mime === mime) {
                        stats.urlHits++;
                        return { ok: true, assetId: assetId, url: cached.url, minted: false, cached: true, mime: mime };
                    }
                    if (cached) releaseAsset(assetId);       // a different blob type: the old URL was another resource
                    const view = toBytes(found.entry.bytes);
                    if (!view) {
                        stats.corrupt++;
                        stats.urlMisses++;
                        return refuse(assetId, 'corrupt');
                    }
                    if (!urls || typeof urls.create !== 'function') {
                        stats.urlMisses++;
                        return refuse(assetId, 'urls-unavailable', { mime: mime });
                    }
                    const forUrl = new Uint8Array(view.length);
                    forUrl.set(view);
                    let url;
                    try {
                        url = urls.create(forUrl, mime);
                    } catch (e) {
                        stats.failures++;
                        stats.urlMisses++;
                        return refuse(assetId, 'url-create-threw', { mime: mime });
                    }
                    if (!url) {
                        stats.urlMisses++;
                        return refuse(assetId, 'url-empty', { mime: mime });
                    }
                    urlCache[assetId] = { url: url, mime: mime };
                    stats.mints++;
                    return { ok: true, assetId: assetId, url: url, minted: true, cached: false, mime: mime };
                });
            },

            /**
             * The render-path lookup: a live URL, or null. Synchronous
             * on purpose — the preview resolver is synchronous, so the
             * URL is resolved ahead of the render and read here. It
             * never mints and never touches storage.
             */
            cachedUrl: (assetId) => (urlCache[assetId] ? urlCache[assetId].url : null),

            /** Live URLs, sorted — the teardown evidence. */
            outstanding: () => Object.keys(urlCache).sort(),

            /** Drop the URL, keep the bytes. */
            release: (assetId) => releaseAsset(assetId),

            releaseAll: () => releaseAllUrls(),

            stats: () => Object.assign({}, stats),

            /**
             * End of session: every outstanding URL goes, the memory
             * fallback goes, the connection closes, and the instance
             * refuses further work instead of half-working.
             */
            destroy: () => {
                const released = releaseAllUrls();
                const dropped = Object.keys(memory).sort();
                dropped.forEach((id) => { delete memory[id]; });
                if (storage && typeof storage.close === 'function') {
                    try { storage.close(); } catch (e) { stats.failures++; }
                }
                dead = true;
                // `retained` is how many byte entries this instance still holds.
                // A teardown that leaves one behind has leaked a whole image, and
                // saying the number out loud is what makes that checkable instead
                // of hoped for.
                return {
                    ok: true, revoked: released.revoked, assetIds: released.assetIds,
                    dropped: dropped, retained: Object.keys(memory).length,
                };
            },
        };
    }

    NERO.embed.assetStore = Object.freeze({
        create: create,
        bytesStorage: bytesStorage,
        browserUrls: browserUrls,
        STORE_NAME: STORE_NAME,
        ENTRY_VERSION: ENTRY_VERSION,
        NEUTRAL_MIME: NEUTRAL_MIME,
    });
})(window.NERO);
