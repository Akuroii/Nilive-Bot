#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════
   Message Builder v2 — Phase 1, step 4: the draft persistence boundary.

   What this harness has to prove, in order of importance:

     1. ISOLATION — v2 never touches v1's database, and a v1 draft is
        read-only. Proven from an operation log: every IndexedDB call the
        module makes is recorded per database and per store, so "v2 never
        writes the v1 namespace" is a measured fact, not a claim.
     2. ROUND TRIP — a stored draft comes back as a document that produces
        the SAME canonical payload as the one that went in (compared byte
        for byte against the real, untouched v1 composer as the oracle).
     3. NO SILENT LOSS — anything unreadable is reported and preserved,
        never overwritten; a newer schema is refused rather than
        downgraded; unsupported values never enter storage.
     4. THE SAVE MODEL — nothing is written per keystroke: changes coalesce
        into one write, lifecycle events flush, failures degrade without
        breaking the editor, and the exposed state machine is real.

   NO INDEXEDDB IN THIS ENVIRONMENT. The harness ships a faithful fake:
   databases with versions, object stores, transactions that complete
   asynchronously, requests, VersionError on a downgrade, plus knobs for
   the failure modes a real browser has (unavailable, open hangs, write
   fails). The adapter under test is the real one, unmodified.

   Run:  node scripts/test_drafts.js
   ═══════════════════════════════════════════════════════════════ */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

let pass = 0, fail = 0; const failures = [];
function assert(cond, name, extra) {
    if (cond) { pass++; console.log('  PASS', name); }
    else { fail++; failures.push(name + (extra ? ' — ' + extra : '')); console.log('  FAIL', name, extra === undefined ? '' : extra); }
}
function section(t) { console.log('\n== ' + t + ' =='); }
/** Read a stored record; a missing one is a named failure, not a TypeError. */
function storedRecord(fake, db, store, key, label) {
    const rec = fake.raw(db, store, key);
    assert(rec !== undefined, 'a record exists in storage: ' + label, 'nothing at ' + db + '/' + store + '/' + key);
    return rec;
}
const JS_DIR = path.join(__dirname, '..', 'dashboard', 'static', 'js');
const micro = () => Promise.resolve();
const settle = async (n) => { for (let i = 0; i < (n || 6); i++) await micro(); };

// ═══════════════════════════════════════════════════════════════
// Deterministic clock + scheduler (the modules never read the wall clock)
// ═══════════════════════════════════════════════════════════════
function makeClock(start) {
    let t = start == null ? 1790284740000 : start;
    const timers = [];
    return {
        now: () => t,
        advance: (ms) => { t += ms; return t; },
        setTimeout: (fn, ms) => { const id = { fn, at: t + ms, cancelled: false }; timers.push(id); return id; },
        clearTimeout: (id) => { if (id) id.cancelled = true; },
        runDue: () => {
            let ran = 0;
            timers.filter(x => !x.cancelled && x.at <= t).forEach(x => { x.cancelled = true; x.fn(); ran++; });
            return ran;
        },
        pending: () => timers.filter(x => !x.cancelled).length,
        scheduler: null,
    };
}
function clockPair(start) {
    const c = makeClock(start);
    c.scheduler = { setTimeout: c.setTimeout, clearTimeout: c.clearTimeout };
    return c;
}

// ═══════════════════════════════════════════════════════════════
// A faithful fake IndexedDB (async, versioned, with failure knobs)
// ═══════════════════════════════════════════════════════════════
function fakeIndexedDB() {
    const dbs = new Map();          // name -> { version, stores: Map<store, Map<key, value>> }
    const log = [];                 // every operation, in order
    const controls = { unavailable: false, failOpen: false, hangOpen: false, failTx: false, hangTx: false };
    const keyOf = (k) => (typeof k === 'object' && k !== null ? 'json:' + JSON.stringify(k) : String(k));

    function transaction(dbName, storeName, mode) {
        const db = dbs.get(dbName);
        log.push({ op: 'transaction', db: dbName, store: storeName, mode: mode });
        const tx = { oncomplete: null, onerror: null, onabort: null, error: null };
        let done = false;
        const complete = () => { if (done) return; done = true; if (tx.oncomplete) tx.oncomplete(); };
        const abort = (why) => {
            if (done) return;
            done = true;
            tx.error = new Error(why);
            if (tx.onerror) tx.onerror(); else if (tx.onabort) tx.onabort();
        };
        if (controls.hangTx) return tx;                       // a transaction that never settles
        if (!db || !db.stores.has(storeName)) { micro().then(() => abort('NotFoundError')); return tx; }
        const data = db.stores.get(storeName);
        tx.objectStore = () => ({
            get: (k) => {
                const req = { result: undefined, onsuccess: null, onerror: null };
                log.push({ op: 'get', db: dbName, store: storeName, key: keyOf(k) });
                micro().then(() => {
                    req.result = data.has(keyOf(k)) ? data.get(keyOf(k)) : undefined;
                    if (typeof req.onsuccess === 'function') req.onsuccess();
                    micro().then(complete);
                });
                return req;
            },
            getAll: () => {
                const req = { result: undefined, onsuccess: null, onerror: null };
                log.push({ op: 'getAll', db: dbName, store: storeName });
                micro().then(() => {
                    req.result = Array.from(data.values());
                    if (typeof req.onsuccess === 'function') req.onsuccess();
                    micro().then(complete);
                });
                return req;
            },
            put: (value, k) => {
                const req = { onsuccess: null, onerror: null };
                log.push({ op: 'put', db: dbName, store: storeName, key: keyOf(k) });
                micro().then(() => {
                    if (controls.failTx) return abort('write failed');
                    data.set(keyOf(k), value);
                    micro().then(complete);
                });
                return req;
            },
            delete: (k) => {
                const req = { onsuccess: null, onerror: null };
                log.push({ op: 'delete', db: dbName, store: storeName, key: keyOf(k) });
                micro().then(() => { data.delete(keyOf(k)); micro().then(complete); });
                return req;
            },
        });
        return tx;
    }

    function open(name, version) {
        const req = { onupgradeneeded: null, onsuccess: null, onerror: null, onblocked: null, result: null, error: null };
        log.push({ op: 'open', db: name, version: version });
        micro().then(() => {
            if (controls.unavailable) {
                req.error = new Error('IndexedDB unavailable');
                if (req.onerror) req.onerror();
                return;
            }
            if (controls.hangOpen) return;                     // never settles: the timeout is the answer
            if (controls.failOpen) {
                req.error = new Error('open failed');
                if (req.onerror) req.onerror();
                return;
            }
            const existing = dbs.get(name);
            if (existing && version < existing.version) {
                const err = new Error('The requested version is lower than the existing version');
                err.name = 'VersionError';
                req.error = err;
                if (req.onerror) req.onerror();
                return;
            }
            if (!existing) {
                const stores = new Map();
                const db = {
                    version: version,
                    stores: stores,
                    objectStoreNames: { contains: (n) => stores.has(n) },
                    createObjectStore: (n) => { stores.set(n, new Map()); return n; },
                    transaction: (s, m) => transaction(name, s, m),
                    close: () => { log.push({ op: 'close', db: name }); },
                };
                log.push({ op: 'createDatabase', db: name, version: version });
                req.result = db;
                if (req.onupgradeneeded) req.onupgradeneeded();
                dbs.set(name, db);
            } else {
                req.result = existing;
            }
            if (req.onsuccess) req.onsuccess();
        });
        return req;
    }

    return {
        indexedDB: { open: open },
        dbs: dbs,
        log: log,
        controls: controls,
        seed: (dbName, version, storeName, key, value) => {
            let db = dbs.get(dbName);
            if (!db) {
                const stores = new Map();
                db = {
                    version: version,
                    stores: stores,
                    objectStoreNames: { contains: (n) => stores.has(n) },
                    createObjectStore: (n) => { stores.set(n, new Map()); return n; },
                    transaction: (s, m) => transaction(dbName, s, m),
                    close: () => { },
                };
                dbs.set(dbName, db);
            }
            if (!db.stores.has(storeName)) db.stores.set(storeName, new Map());
            db.stores.get(storeName).set(keyOf(key), value);
            return value;
        },
        raw: (dbName, storeName, key) => {
            const db = dbs.get(dbName);
            if (!db || !db.stores.has(storeName)) return undefined;
            return db.stores.get(storeName).get(keyOf(key));
        },
        ops: (filter) => log.filter((e) => !filter || filter(e)),
        writesTo: (dbName) => log.filter((e) => e.db === dbName && (e.op === 'put' || e.op === 'delete')),
        reset: () => { log.length = 0; },
    };
}

// ═══════════════════════════════════════════════════════════════
// Load the real modules
// ═══════════════════════════════════════════════════════════════
// NERO_DRAFTS_SRC lets the mutation battery point this harness at a
// deliberately broken copy of the module and confirm the checks fail.
const DRAFTS_PATH = process.env.NERO_DRAFTS_SRC || path.join(JS_DIR, 'embed', 'drafts.js');
const DRAFTS_SRC = fs.readFileSync(DRAFTS_PATH, 'utf8');
const sandbox = { window: {}, console };
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(path.join(JS_DIR, 'embed', 'model.js'), 'utf8'), sandbox);
vm.runInContext(fs.readFileSync(path.join(JS_DIR, 'embed', 'store.js'), 'utf8'), sandbox);
const NS_BEFORE = Object.keys(sandbox.window.NERO.embed).sort();
vm.runInContext(DRAFTS_SRC, sandbox);
const model = sandbox.window.NERO.embed.model;
const storeMod = sandbox.window.NERO.embed.store;
const drafts = sandbox.window.NERO.embed.drafts;

// the frozen v1 composer, as the payload oracle
const v1sandbox = { window: {}, console, Event: function () { } };
vm.createContext(v1sandbox);
vm.runInContext(fs.readFileSync(path.join(JS_DIR, 'embed-composer.js'), 'utf8'), v1sandbox);
const V1 = v1sandbox.window.EmbedComposer;

const TEST_STORAGE = 'fake-idb';
let idSeq = 0;
const nextIds = () => model.createIdFactory('d' + (++idSeq));

const G1 = '111111111111111111';
const G2 = '222222222222222222';

// ═══════════════════════════════════════════════════════════════
(async function main() {

    section('the module is a pure persistence boundary (source scan)');
    {
        assert(!/\.toDiscordPayload\s*\(/.test(DRAFTS_SRC),
            'drafts.js never builds a Discord payload (that transform stays in model.js)');
        assert(!/cleanEmbed|renderPreview|innerHTML|renderDiscordMarkup/.test(DRAFTS_SRC),
            'and contains no rendering code at all');
        assert(/model\.stableStringify/.test(DRAFTS_SRC) && /model\.hashDocument/.test(DRAFTS_SRC)
            && /model\.normalizeDocument/.test(DRAFTS_SRC),
            'it delegates serialization, hashing and repair to the model');
        const nsAfter = Object.keys(sandbox.window.NERO.embed).sort();
        assert(nsAfter.filter(k => NS_BEFORE.indexOf(k) === -1).join(',') === 'drafts',
            'loading it adds exactly one namespace entry',
            'before=[' + NS_BEFORE.join(',') + '] after=[' + nsAfter.join(',') + ']');
        assert(drafts.V1_NAMESPACE === 'nero_embedbuilder' && drafts.V2_NAMESPACE === 'nero_message_builder',
            'the two namespaces are what the architecture approved',
            drafts.V1_NAMESPACE + ' / ' + drafts.V2_NAMESPACE);
        assert(drafts.V2_STORES.join(',') === 'drafts,assets,meta',
            'v2 owns three stores (assets is a seam, not a feature yet)', drafts.V2_STORES.join(','));
    }

    // ═══════════════════════════════════════════════════════════
    section('1. create / save / load round trip');
    // ═══════════════════════════════════════════════════════════
    {
        const clock = clockPair();
        const fake = fakeIndexedDB();
        const storage = drafts.idbStorage({ indexedDB: fake.indexedDB, scheduler: clock.scheduler });
        const doc = model.fromEditorDocument({
            content: 'Hello **world** <@200>',
            embeds: [{
                title: 'Rules', description: 'Be nice `now`', color: '#7c5cbf',
                author: 'Nero', authorIcon: 'https://cdn.example/a.png',
                footer: 'Nero', timestamp: '2026-09-24T08:00:00.000Z',
                image: 'https://cdn.example/i.png',
                fields: [{ name: 'Rule 1', value: 'Be kind', inline: true }, { name: 'Rule 2', value: 'Have fun' }],
            }, { description: 'second' }],
        }, { ids: nextIds(), guildId: G1 });

        const session = drafts.create({ guildId: G1, documentId: doc.id, now: clock.now, storage, scheduler: clock.scheduler });
        session.use(doc);
        assert(session.isDirty() === true, 'a document with content starts dirty (unsaved)');
        const result = await session.saveNow();
        assert(result.ok === true && result.revision === 1, 'saveNow() writes revision 1', JSON.stringify(result));
        assert(fake.raw(drafts.V2_NAMESPACE, 'drafts', result.key) !== undefined,
            'and the record is in the v2 drafts store under its key');
        assert(session.state().state === 'saved' && session.isDirty() === false,
            'the session reports saved/clean afterwards', JSON.stringify(session.state().state));

        // a fresh session (a reload) reads it back
        const reloaded = drafts.create({ guildId: G1, documentId: doc.id, now: clock.now, storage, scheduler: clock.scheduler });
        const loaded = await reloaded.load();
        assert(loaded.ok === true && loaded.status === 'ok', 'load() accepts the record', loaded.status);
        assert(JSON.stringify(loaded.document) === JSON.stringify(doc),
            'the document round-trips exactly (deep equal)');
        assert(reloaded.isDirty() === false, 'and a freshly loaded draft is clean');
        assert(!!loaded.meta, 'load() reports metadata');
        if (loaded.meta) {
            assert(loaded.meta.revision === 1 && loaded.meta.updatedAt === clock.now(),
                'with its revision and update metadata', JSON.stringify(loaded.meta));
        }

        // the payload is unchanged by the round trip
        const before = JSON.stringify(model.toDiscordPayload(doc));
        const after = JSON.stringify(model.toDiscordPayload(loaded.document));
        assert(before === after, 'the canonical payload is byte-identical after a round trip');
        assert(!!loaded.meta && loaded.meta.documentHash === model.hashDocument(doc),
            'documentHash matches model.hashDocument', loaded.meta ? String(loaded.meta.documentHash) : 'no meta');

        // and it equals what the real v1 composer would have produced for the same editor state
        const v1Payload = JSON.stringify({
            content: 'Hello **world** <@200>' || undefined,
            embeds: V1.cleanEmbedsForPayload(doc.embeds.map(model.toEditorEmbed)),
        });
        assert(v1Payload === before,
            'and still equals the frozen v1 composer payload for the same editor state');

        // load → save → load is a fixed point
        reloaded.use(loaded.document, { saved: true });
        const again = await reloaded.saveNow({ force: true });
        assert(again.ok === true && again.revision === 2,
            'an explicit re-save continues the revision sequence', JSON.stringify(again));
        const third = drafts.create({ guildId: G1, documentId: doc.id, now: clock.now, storage, scheduler: clock.scheduler });
        const thirdLoad = await third.load();
        assert(JSON.stringify(thirdLoad.document) === JSON.stringify(doc),
            'and the document is still identical after save → load → save → load');

        // no IndexedDB at all is a degraded environment, not a crash
        const noIdb = drafts.idbStorage({ indexedDB: null, scheduler: clock.scheduler });
        assert(noIdb.isAvailable() === false && noIdb.reason() === 'no-indexeddb',
            'without IndexedDB the adapter reports itself unavailable', noIdb.reason());
        const bare = drafts.create({ guildId: G1, documentId: 'x', now: clock.now, storage: noIdb, scheduler: clock.scheduler });
        bare.use(doc);
        const bareSave = await bare.saveNow();
        assert(bareSave.ok === false && bareSave.reason === 'no-indexeddb',
            'and a save resolves as a reported failure', JSON.stringify(bareSave));
        assert(bare.isDirty() === true && bare.state().degraded === 'no-indexeddb',
            'leaving the document dirty and the state degraded — nothing is lost, nothing throws');
    }

    // ═══════════════════════════════════════════════════════════
    section('2 & 3. multiple documents, multiple guilds');
    // ═══════════════════════════════════════════════════════════
    {
        const clock = clockPair();
        const fake = fakeIndexedDB();
        const storage = drafts.idbStorage({ indexedDB: fake.indexedDB, scheduler: clock.scheduler });
        const makeDoc = (title) => model.fromEditorDocument({
            content: title, embeds: [{ title: title }],
        }, { ids: nextIds(), guildId: G1 });

        // Draft identity comes from the persistence layer, not the model's id
        // factory: the factory salts by seed LENGTH and restarts per page load,
        // so two sessions can mint the same document id — and two drafts that
        // share an id share a storage key, which is silent data loss.
        const sa = drafts.create({ guildId: G1, now: clock.now, storage, scheduler: clock.scheduler });
        const sb = drafts.create({ guildId: G1, now: clock.now, storage, scheduler: clock.scheduler });
        const startedA = sa.start(null, { documentId: drafts.newDocumentId({ time: 1000, sequence: 1 }) });
        const startedB = sb.start(null, { documentId: drafts.newDocumentId({ time: 2000, sequence: 1 }) });
        const a = model.fromEditorDocument({ content: 'document A', embeds: [{ title: 'A' }] }, { ids: nextIds(), guildId: G1, id: startedA.documentId });
        const b = model.fromEditorDocument({ content: 'document B', embeds: [{ title: 'B' }] }, { ids: nextIds(), guildId: G1, id: startedB.documentId });
        assert(a.id !== b.id, 'two drafts get different identities', a.id + ' vs ' + b.id);
        sa.use(a); sb.use(b);
        assert(sa.key() !== sb.key(), 'and different draft keys', sa.key() + ' vs ' + sb.key());
        await sa.saveNow(); await sb.saveNow();

        const ra = drafts.create({ guildId: G1, documentId: a.id, now: clock.now, storage, scheduler: clock.scheduler });
        const rb = drafts.create({ guildId: G1, documentId: b.id, now: clock.now, storage, scheduler: clock.scheduler });
        const la = await ra.load(); const lb = await rb.load();
        assert(la.document.content === 'document A' && lb.document.content === 'document B',
            'each document loads back its own content (no cross-talk)');
        assert(la.document.id === a.id && lb.document.id === b.id, 'with its own document id');

        // the model's factory is page-scoped: same-length seeds collide, and a
        // fresh page restarts the counter. Prove the failure mode is real, then
        // prove the persistence layer is not exposed to it.
        const modelA = model.createIdFactory('one');
        const modelB = model.createIdFactory('two');
        assert(modelA('doc') === modelB('doc'),
            'the model id factory collides across same-length seeds (the reason for newDocumentId)',
            modelA('doc') + ' vs ' + modelB('doc'));
        const twoPages = model.createIdFactory();
        const secondPage = model.createIdFactory();
        assert(twoPages('doc') === secondPage('doc'),
            'and a fresh page restarts the counter, so ids repeat across sessions');

        const ids = [];
        for (let i = 0; i < 50; i++) ids.push(drafts.newDocumentId({ time: 1790284740000, sequence: i + 1 }));
        assert(new Set(ids).size === 50, 'newDocumentId never repeats within a session');
        const acrossSessions = [drafts.newDocumentId({ time: 1000, sequence: 1 }),
            drafts.newDocumentId({ time: 1001, sequence: 1 }),
            drafts.newDocumentId({ time: 1000, sequence: 1, entropy: 'z9' }),
            drafts.newDocumentId({ time: 1000, sequence: 1, entropy: 'z8' })];
        assert(new Set(acrossSessions).size === 4,
            'nor across sessions that start together (clock, sequence and entropy all separate)', acrossSessions.join(','));
        assert(acrossSessions[2] === 'mb_rs_1_z9', 'the entropy suffix is sanitized and appended', acrossSessions[2]);
        assert(drafts.newDocumentId({ time: 2000, sequence: 1 }) !== drafts.newDocumentId({ time: 1000, sequence: 1 }),
            'a later session gets a different identity even at the same sequence');

        // a second new draft must never overwrite the first one
        const sFirst = drafts.create({ guildId: G1, now: clock.now, storage, scheduler: clock.scheduler });
        const firstDoc = model.fromEditorDocument({ content: 'first draft', embeds: [{ title: '1' }] },
            { ids: nextIds(), guildId: G1, id: drafts.newDocumentId({ time: clock.now(), sequence: 1 }) });
        sFirst.use(firstDoc);
        assert((await sFirst.saveNow()).ok === true, 'the first new draft saves');
        const sSecond = drafts.create({ guildId: G1, now: clock.now, storage, scheduler: clock.scheduler });
        const secondDoc = model.fromEditorDocument({ content: 'second draft', embeds: [{ title: '2' }] },
            { ids: nextIds(), guildId: G1, id: drafts.newDocumentId({ time: clock.now(), sequence: 2 }) });
        sSecond.use(secondDoc);
        assert((await sSecond.saveNow()).ok === true, 'a second new draft saves');
        const both = await sSecond.listDrafts();
        const keys = both.drafts.map(d => d.key);
        assert(keys.indexOf(sFirst.key()) !== -1 && keys.indexOf(sSecond.key()) !== -1,
            'and BOTH drafts exist afterwards — starting a draft cannot overwrite one', JSON.stringify(keys));
        const reread = drafts.create({ guildId: G1, documentId: firstDoc.id, now: clock.now, storage, scheduler: clock.scheduler });
        assert((await reread.load()).document.content === 'first draft',
            'the first draft still holds its own content');
        assert(drafts.parseKey(sFirst.key()).documentId === firstDoc.id,
            'and its key still points at its own document id');

        // the same document id under a second guild is a different draft
        const sc = drafts.create({ guildId: G2, documentId: a.id, now: clock.now, storage, scheduler: clock.scheduler });
        await sc.load();
        sc.use(a, { saved: false });
        await sc.saveNow({ force: true });
        assert(sc.key() !== sa.key(), 'the same document id in another guild keys differently', sc.key());
        const listG1 = await drafts.create({ guildId: G1, now: clock.now, storage, scheduler: clock.scheduler }).listDrafts();
        const listG2 = await drafts.create({ guildId: G2, now: clock.now, storage, scheduler: clock.scheduler }).listDrafts();
        assert(listG1.drafts.length === 4 && listG2.drafts.length === 1,
            'listing is guild-scoped', JSON.stringify(listG1.drafts.map(d => d.key)));
        assert(listG1.drafts.every(d => !d.document),
            'listings carry metadata only — never whole documents');
        const all = await drafts.create({ guildId: G1, now: clock.now, storage, scheduler: clock.scheduler }).listDrafts({ all: true });
        assert(all.drafts.length === 5, 'and { all: true } sees every draft', String(all.drafts.length));
        assert(all.drafts.every((d, i, arr) => i === 0 || (arr[i - 1].updatedAt || 0) >= (d.updatedAt || 0)),
            'the list is ordered newest first');

        // meta: "reopen the last draft I edited in this guild"
        await sa.meta.rememberDocument(G1, a.id);
        const remembered = await sa.meta.lastDocumentId(G1);
        assert(remembered === a.id, 'the meta store remembers the last document id per guild', String(remembered));
        assert((await sa.meta.lastDocumentId(G2)) === null, 'and it is guild-scoped too');
        assert(fake.ops().some(e => e.op === 'put' && e.store === 'meta'),
            'the meta write went to the meta store');
        assert(fake.ops().every(e => e.store !== 'assets'),
            'the assets store was created as a seam and is never written in phase 1');
        assert(fake.dbs.get(drafts.V2_NAMESPACE).stores.has('assets'),
            'the assets store does exist in the schema (the seam is real)');
    }

    // ═══════════════════════════════════════════════════════════
    section('4. key isolation');
    // ═══════════════════════════════════════════════════════════
    {
        assert(drafts.draftKey(G1, 'doc_1') === 'v2:' + G1 + ':doc_1',
            'the key format is v2:<guildId>:<documentId>', drafts.draftKey(G1, 'doc_1'));
        assert(drafts.draftKey(null, 'doc_1') === 'v2:global:doc_1',
            'a guild-less draft uses a stable "global" segment');
        assert(drafts.draftKey('', 'doc_1') === 'v2:global:doc_1', 'an empty guild id behaves the same');
        let threw = null;
        try { drafts.draftKey(G1, ''); } catch (e) { threw = e.message; }
        assert(threw !== null, 'a missing document id is refused rather than keyed as undefined', threw || '');

        const parsed = drafts.parseKey('v2:' + G1 + ':doc_1');
        assert(parsed && parsed.guildId === G1 && parsed.documentId === 'doc_1',
            'a key parses back into its parts', JSON.stringify(parsed));
        assert(drafts.parseKey('composer') === null, 'v1\'s key is not a v2 key');
        assert(drafts.parseKey('v2:only-two') === null && drafts.parseKey('v3:' + G1 + ':doc') === null,
            'a malformed or foreign key is rejected');
        assert(drafts.parseKey('v2:a:b:c') === null, 'and so is an over-long key');
        assert(fakeIndexedDB().seed !== undefined && drafts.draftKey(G1, 'x') !== drafts.draftKey(G2, 'x'),
            'guild and document together determine identity');
    }

    // ═══════════════════════════════════════════════════════════
    section('5 & 6. schema version and document version handling');
    // ═══════════════════════════════════════════════════════════
    {
        const clock = clockPair();
        const fake = fakeIndexedDB();
        const storage = drafts.idbStorage({ indexedDB: fake.indexedDB, scheduler: clock.scheduler });
        const doc = model.fromEditorDocument({ content: 'v', embeds: [{ title: 'T' }] }, { ids: nextIds() });
        const good = drafts.buildRecord({ guildId: G1, documentId: doc.id, document: doc, now: clock.now(), previous: null });
        assert(good.schemaVersion === drafts.RECORD_VERSION && good.namespace === drafts.V2_NAMESPACE,
            'a record carries the envelope version and the namespace',
            good.schemaVersion + ' / ' + good.namespace);
        assert(Object.keys(good).join(',') === 'schemaVersion,namespace,key,guildId,documentId,revision,createdAt,updatedAt,documentHash,document',
            'with a fixed key order (deterministic bytes)', Object.keys(good).join(','));
        assert(good.document.schemaVersion === model.SCHEMA_VERSION,
            'and the document version separately from the envelope version',
            String(good.document.schemaVersion));

        const cases = [
            ['envelope version 999 (from a newer build)', Object.assign({}, good, { schemaVersion: 999 }), 'future'],
            ['envelope version 0', Object.assign({}, good, { schemaVersion: 0 }), 'corrupt'],
            ['envelope version "1" (a string)', Object.assign({}, good, { schemaVersion: '1' }), 'corrupt'],
            ['envelope version missing', Object.assign({}, good, { schemaVersion: undefined }), 'corrupt'],
            ['document schemaVersion 99', Object.assign({}, good, { document: Object.assign({}, doc, { schemaVersion: 99 }) }), 'future'],
            ['document schemaVersion 0', Object.assign({}, good, { document: Object.assign({}, doc, { schemaVersion: 0 }) }), 'corrupt'],
            ['document schemaVersion missing', Object.assign({}, good, { document: Object.assign({}, doc, { schemaVersion: undefined }) }), 'repaired'],
            ['document schemaVersion current', good, 'ok'],
        ];
        cases.forEach(([name, record, expected]) => {
            const verdict = drafts.validateRecord(record, { key: good.key });
            assert(verdict.status === expected,
                'validateRecord: ' + name + ' → ' + expected,
                'got ' + verdict.status + ' (' + verdict.problems.join('; ') + ')');
        });
        const repaired = drafts.validateRecord(cases[6][1], { key: good.key });
        assert(repaired.ok === true && repaired.repairs.indexOf('schemaVersion') !== -1,
            'a missing document version is repaired (not rejected) and reported', JSON.stringify(repaired.repairs));
        assert(repaired.document.schemaVersion === model.SCHEMA_VERSION,
            'and the repaired document carries the current version');

        // the same through a real load
        const futureRecord = Object.assign({}, good, { schemaVersion: 999 });
        fake.seed(drafts.V2_NAMESPACE, 1, 'drafts', good.key, futureRecord);
        const s = drafts.create({ guildId: G1, documentId: doc.id, now: clock.now, storage, scheduler: clock.scheduler });
        const res = await s.load();
        assert(res.ok === false && res.status === 'future' && res.preserved === true,
            'load() reports a future record as future/preserved', res.status);
    }

    // ═══════════════════════════════════════════════════════════
    section('7. malformed / corrupted stored data is preserved, never overwritten');
    // ═══════════════════════════════════════════════════════════
    {
        const clock = clockPair();
        const doc = model.fromEditorDocument({ content: 'c', embeds: [{ title: 'T' }] }, { ids: nextIds() });
        const cases = [
            ['a string', 'not a record'],
            ['a number', 42],
            ['an array', [1, 2, 3]],
            ['null-ish object', { document: null }],
            ['a record with no document', { schemaVersion: 1, namespace: drafts.V2_NAMESPACE, document: null }],
            ['a record whose key disagrees with its storage key', Object.assign(
                drafts.buildRecord({ guildId: G1, documentId: doc.id, document: doc, now: 1, previous: null }), { key: 'v2:other:other' })],
            ['a v1-shaped payload (foreign namespace)', { version: 1, content: 'hi', embeds: [], namespace: 'nero_embedbuilder' }],
            ['a record with a corrupt documentHash', Object.assign(
                drafts.buildRecord({ guildId: G1, documentId: doc.id, document: doc, now: 1, previous: null }), { documentHash: 'deadbeef:0' })],
        ];
        for (const [name, value] of cases) {
            const fake = fakeIndexedDB();
            const storage = drafts.idbStorage({ indexedDB: fake.indexedDB, scheduler: clock.scheduler });
            const key = drafts.draftKey(G1, doc.id);
            fake.seed(drafts.V2_NAMESPACE, 1, 'drafts', key, value);
            const s = drafts.create({ guildId: G1, documentId: doc.id, now: clock.now, storage, scheduler: clock.scheduler });
            s.use(doc);
            fake.reset();
            const res = await s.load();
            const expectsOk = name.indexOf('documentHash') !== -1;
            if (expectsOk) {
                assert(res.ok === true && res.hashMatches === false,
                    'corrupt data: ' + name + ' → loads but reports the checksum mismatch', res.status);
            } else {
                assert(res.ok === false && res.preserved === true && Array.isArray(res.problems) && res.problems.length > 0,
                    'corrupt data: ' + name + ' → refused with a reason, preserved', res.status + ' ' + JSON.stringify(res.problems));
                assert(s.guard() && s.guard().reason === res.status,
                    'and writes are guarded (' + name + ')', JSON.stringify(s.guard()));
            }
            storedRecord(fake, drafts.V2_NAMESPACE, 'drafts', key, 'after a failed load: ' + name);
            assert(fake.writesTo(drafts.V2_NAMESPACE).length === 0,
                'and the load wrote nothing (' + name + ')');
        }

        // a guarded session refuses to overwrite, and an explicit decision lifts it
        const fake = fakeIndexedDB();
        const storage = drafts.idbStorage({ indexedDB: fake.indexedDB, scheduler: clock.scheduler });
        const key = drafts.draftKey(G1, doc.id);
        const broken = { schemaVersion: 1, namespace: drafts.V2_NAMESPACE, key: key, document: 'broken' };
        fake.seed(drafts.V2_NAMESPACE, 1, 'drafts', key, broken);
        const s = drafts.create({ guildId: G1, documentId: doc.id, now: clock.now, storage, scheduler: clock.scheduler });
        const res = await s.load();
        assert(res.status === 'corrupt', 'a corrupt record blocks the session', res.status);
        s.use(doc);
        const refused = await s.saveNow();
        assert(refused.ok === false && refused.blocked === true && refused.preserved === broken,
            'a save is refused while the guard is up, and hands back the preserved bytes',
            JSON.stringify(refused.reason));
        assert(s.state().state === 'blocked', 'and the state machine says so', s.state().state);
        assert(fake.raw(drafts.V2_NAMESPACE, 'drafts', key) === broken,
            'the corrupt record was not overwritten');
        assert(s.resolveGuard('keep') === false, 'resolveGuard("keep") keeps refusing');
        assert(s.resolveGuard('replace') === true, 'resolveGuard("replace") is the explicit override');
        const nowSaved = await s.saveNow();
        assert(nowSaved.ok === true && nowSaved.revision === 1,
            'after which the session can save (revision restarts from the unreadable record)',
            JSON.stringify(nowSaved));
    }

    // ═══════════════════════════════════════════════════════════
    section('8. missing fields and default recovery');
    // ═══════════════════════════════════════════════════════════
    {
        const clock = clockPair();
        const fake = fakeIndexedDB();
        const storage = drafts.idbStorage({ indexedDB: fake.indexedDB, scheduler: clock.scheduler });
        const partial = { content: 'hello', schemaVersion: model.SCHEMA_VERSION };
        const key = drafts.draftKey(G1, 'doc_partial');
        fake.seed(drafts.V2_NAMESPACE, 1, 'drafts', key, {
            schemaVersion: 1, namespace: drafts.V2_NAMESPACE, key: key, guildId: G1, documentId: 'doc_partial',
            revision: 3, createdAt: 100, updatedAt: 200, documentHash: null, document: partial,
        });
        const s = drafts.create({ guildId: G1, documentId: 'doc_partial', now: clock.now, storage, scheduler: clock.scheduler });
        const res = await s.load();
        assert(res.ok === true && res.status === 'repaired', 'a partial document is repaired, not rejected', res.status);
        assert(res.repairs.indexOf('embeds') !== -1 && res.repairs.indexOf('shape') !== -1,
            'and the repairs are named', JSON.stringify(res.repairs));
        assert(!!res.document, 'a repaired document is returned');
        if (res.document) {
            assert(res.document.content === 'hello', 'the data that WAS there is kept');
            assert(typeof res.document.id === 'string' && res.document.id.length > 0, 'an id is minted for the document');
            assert(Array.isArray(res.document.embeds) && res.document.embeds.length === 1
                && typeof res.document.embeds[0].id === 'string',
                'a blank embed with an id is created, so the editor has something to show');
            assert(res.document.schemaVersion === model.SCHEMA_VERSION, 'the document version is current');
            assert(model.documentHasContent(res.document) === true, 'the recovered document is still non-empty');
            const equivalent = model.fromEditorDocument({ content: 'hello', embeds: [] }, { ids: nextIds() });
            assert(JSON.stringify(model.toDiscordPayload(res.document).content)
                === JSON.stringify(model.toDiscordPayload(equivalent).content),
                'and produces the same payload as the freshly built equivalent');
        }
        if (res.meta) {
            assert(res.meta.revision === 3 && res.meta.createdAt === 100,
                'the record metadata survives the repair', JSON.stringify(res.meta));
        } else {
            assert(false, 'the repair still reports the record metadata');
        }
        // legacy/unknown keys cannot survive normalization
        const legacyKey = drafts.draftKey(G1, 'doc_legacy');
        fake.seed(drafts.V2_NAMESPACE, 1, 'drafts', legacyKey, {
            schemaVersion: 1, namespace: drafts.V2_NAMESPACE, key: legacyKey, guildId: G1, documentId: 'doc_legacy',
            revision: 1, createdAt: 1, updatedAt: 1, documentHash: null,
            document: Object.assign({}, model.blankMessageDocument({ ids: nextIds() }), { attachments: ['a'] }),
        });
        const sl = drafts.create({ guildId: G1, documentId: 'doc_legacy', now: clock.now, storage, scheduler: clock.scheduler });
        const rl = await sl.load();
        assert(rl.ok === true && rl.document.attachments === undefined,
            'an unknown legacy key is dropped by normalization (and the load says so)',
            JSON.stringify(rl.repairs));
    }

    // ═══════════════════════════════════════════════════════════
    section('9. deterministic serialization and hashing');
    // ═══════════════════════════════════════════════════════════
    {
        const clock = clockPair();
        const doc = model.fromEditorDocument({
            content: 'stable', embeds: [{ title: 'A', fields: [{ name: 'n', value: 'v' }] }],
        }, { ids: model.createIdFactory('fixed'), guildId: G1 });
        const r1 = drafts.buildRecord({ guildId: G1, documentId: doc.id, document: doc, now: 5000, previous: null });
        const r2 = drafts.buildRecord({ guildId: G1, documentId: doc.id, document: doc, now: 5000, previous: null });
        assert(drafts.serializeRecord(r1) === drafts.serializeRecord(r2),
            'the same inputs serialize to identical bytes');
        assert(JSON.stringify(r1) === JSON.stringify(r2), 'and to identical JSON');

        // key insertion order must not matter
        const shuffled = { embeds: doc.embeds, content: doc.content, assets: doc.assets, rows: doc.rows, layout: doc.layout, guildId: doc.guildId, id: doc.id, schemaVersion: doc.schemaVersion };
        assert(model.hashDocument(shuffled) === model.hashDocument(doc),
            'a document with reordered keys hashes identically (stableStringify)');
        assert(drafts.toStorable(shuffled).content === doc.content, 'and is storable');

        // round-trip stability
        const stored = drafts.toStorable(doc);
        assert(JSON.stringify(stored) === JSON.stringify(doc), 'toStorable is a faithful JSON copy');
        assert(model.hashDocument(stored) === model.hashDocument(doc), 'the hash survives the copy');
        const record = drafts.buildRecord({ guildId: G1, documentId: doc.id, dataset: 1, document: stored, now: 5000, previous: null });
        assert(JSON.parse(drafts.serializeRecord(record)) !== null, 'the serialized record is valid JSON');
        assert(drafts.serializeRecord(record).indexOf('undefined') === -1, 'with no undefined anywhere in it');

        // revision + timestamps are the only things that move
        const r3 = drafts.buildRecord({ guildId: G1, documentId: doc.id, document: doc, now: 6000, previous: r1 });
        assert(r3.revision === 2 && r3.createdAt === r1.createdAt && r3.updatedAt === 6000,
            'a later save bumps the revision and update time but keeps createdAt',
            JSON.stringify({ rev: r3.revision, created: r3.createdAt, updated: r3.updatedAt }));
        assert(r3.documentHash === r1.documentHash, 'and the document hash is unchanged when only metadata moves');
    }

    // ═══════════════════════════════════════════════════════════
    section('10. revision and update metadata across a session');
    // ═══════════════════════════════════════════════════════════
    {
        const clock = clockPair();
        const fake = fakeIndexedDB();
        const storage = drafts.idbStorage({ indexedDB: fake.indexedDB, scheduler: clock.scheduler, timeoutMs: 100 });
        const doc = model.fromEditorDocument({ content: 'a', embeds: [{ title: 'T' }] }, { ids: nextIds() });
        const s = drafts.create({ guildId: G1, documentId: doc.id, now: clock.now, storage, scheduler: clock.scheduler, idleMs: 500 });
        s.use(doc);
        const one = await s.saveNow();
        clock.advance(1000);
        s.changed(model.setContent(doc, 'ab'));
        const two = await s.saveNow();
        clock.advance(1000);
        s.changed(model.setContent(doc, 'abc'));
        const three = await s.saveNow();
        assert(one.revision === 1 && two.revision === 2 && three.revision === 3,
            'revision increments once per successful write', [one.revision, two.revision, three.revision].join(','));
        const record = storedRecord(fake, drafts.V2_NAMESPACE, 'drafts', s.key(), 'after three saves');
        if (record) {
            assert(record.revision === 3 && record.createdAt === one.updatedAt,
                'the stored record keeps the original createdAt',
                JSON.stringify({ rev: record.revision, created: record.createdAt }));
            assert(record.updatedAt === clock.now(), 'and the newest updatedAt', String(record.updatedAt));
            assert(record.documentHash === model.hashDocument(model.setContent(doc, 'abc')),
                'documentHash describes the stored document');
            assert(record.document && record.document.content === 'abc', 'and the stored document is the newest one');
        }
        assert(s.state().revision === 3 && s.state().updatedAt === clock.now(),
            'the session state exposes the same metadata', JSON.stringify(s.state().revision));
    }

    // ═══════════════════════════════════════════════════════════
    section('11. dirty → scheduled save → clean (with the Step-1 store)');
    // ═══════════════════════════════════════════════════════════
    {
        const clock = clockPair();
        const fake = fakeIndexedDB();
        const storage = drafts.idbStorage({ indexedDB: fake.indexedDB, scheduler: clock.scheduler });
        const doc = model.fromEditorDocument({ content: 'start', embeds: [{ title: 'T' }] }, { ids: nextIds() });
        const store = storeMod.createStore({
            document: doc, scheduler: clock.scheduler, now: clock.now,
            reducers: storeMod.createReducers(),
        });
        const states = [];
        const s = drafts.create({
            guildId: G1, documentId: doc.id, now: clock.now, storage, scheduler: clock.scheduler,
            idleMs: 500, onState: (st) => states.push(st.state),
        });
        s.use(doc);
        s.attach(store);
        await s.saveNow();
        assert(store.isDirty() === false, 'after the initial save the STORE reports clean too (markSaved was called)');
        assert(fake.writesTo(drafts.V2_NAMESPACE).length === 1, 'exactly one write so far');

        store.dispatch({ type: 'content/set', text: 'start!' });
        assert(s.isDirty() === true && store.isDirty() === true, 'an edit makes both dirty');
        assert(s.state().state === 'dirty', 'and the session state says dirty', s.state().state);
        assert(s.pendingSave() === true, 'with a save pending');
        assert(fake.writesTo(drafts.V2_NAMESPACE).length === 1, 'but NOTHING has been written yet (no per-keystroke writes)');

        clock.advance(499);
        assert(fake.writesTo(drafts.V2_NAMESPACE).length === 1, 'still nothing just before the idle deadline');
        clock.advance(2);
        clock.runDue();
        await settle();
        assert(fake.writesTo(drafts.V2_NAMESPACE).length === 2, 'and exactly one write once idle', String(fake.writesTo(drafts.V2_NAMESPACE).length));
        assert(s.isDirty() === false && store.isDirty() === false, 'both are clean again');
        assert(s.state().state === 'saved', 'and the state is saved', s.state().state);
        assert(store.getDocument().content === 'start!', 'the document in the store is the edited one');
        assert(storedRecord(fake, drafts.V2_NAMESPACE, 'drafts', s.key(), 'after the idle save').document.content === 'start!',
            'and that is what storage holds');
        assert(states.indexOf('dirty') !== -1 && states.indexOf('saved') !== -1,
            'the state observer saw the transitions', states.join('>'));
    }

    // ═══════════════════════════════════════════════════════════
    section('12. rapid updates are coalesced');
    // ═══════════════════════════════════════════════════════════
    {
        const clock = clockPair();
        const fake = fakeIndexedDB();
        const storage = drafts.idbStorage({ indexedDB: fake.indexedDB, scheduler: clock.scheduler });
        let doc = model.fromEditorDocument({ content: '', embeds: [{ title: '' }] }, { ids: nextIds() });
        const s = drafts.create({ guildId: G1, documentId: doc.id, now: clock.now, storage, scheduler: clock.scheduler, idleMs: 500 });
        s.use(doc);
        await s.saveNow();
        fake.reset();
        const before = fake.writesTo(drafts.V2_NAMESPACE).length;

        for (let i = 1; i <= 20; i++) {
            doc = model.setContent(doc, 'x'.repeat(i));
            s.changed(doc);
            clock.advance(50);                 // faster than the idle window
            clock.runDue();
            assert(clock.pending() <= 1, 'at most one idle timer exists (iteration ' + i + ')');
        }
        assert(fake.writesTo(drafts.V2_NAMESPACE).length === before,
            'twenty keystrokes scheduled nothing at all', String(fake.writesTo(drafts.V2_NAMESPACE).length - before));
        clock.advance(500);
        clock.runDue();
        await settle();
        assert(fake.writesTo(drafts.V2_NAMESPACE).length === before + 1,
            'the whole burst collapsed into ONE write', String(fake.writesTo(drafts.V2_NAMESPACE).length - before));
        const burstRecord = storedRecord(fake, drafts.V2_NAMESPACE, 'drafts', s.key(), 'after the burst');
        assert(!!burstRecord && !!burstRecord.document
            && burstRecord.document.content === 'x'.repeat(20),
            'and that write holds the LAST state, not an intermediate one');

        // the pending timer is cancelled by an explicit flush, not left to double-fire
        s.changed(model.setContent(doc, 'final'));
        assert(s.pendingSave() === true, 'a new change schedules again');
        const flushed = await s.saveNow();
        assert(flushed.ok === true && s.pendingSave() === false,
            'an explicit flush cancels the pending timer', JSON.stringify(flushed));
        clock.advance(1000); clock.runDue(); await settle();
        assert(fake.writesTo(drafts.V2_NAMESPACE).length === before + 2,
            'so the cancelled timer cannot write a second time',
            String(fake.writesTo(drafts.V2_NAMESPACE).length - before));
    }

    // ═══════════════════════════════════════════════════════════
    section('13 & 14. explicit flush, pagehide / visibility / teardown');
    // ═══════════════════════════════════════════════════════════
    {
        const clock = clockPair();
        const fake = fakeIndexedDB();
        const storage = drafts.idbStorage({ indexedDB: fake.indexedDB, scheduler: clock.scheduler });
        const doc = model.fromEditorDocument({ content: 'page', embeds: [{ title: 'T' }] }, { ids: nextIds() });

        // a fake window: only the two events a page really gets
        const listeners = [];
        const target = {
            visibilityState: 'visible',
            addEventListener: (type, fn) => listeners.push({ type, fn }),
            removeEventListener: (type, fn) => {
                const i = listeners.findIndex(l => l.type === type && l.fn === fn);
                if (i !== -1) listeners.splice(i, 1);
            },
            dispatch: (type) => listeners.filter(l => l.type === type).forEach(l => l.fn()),
        };

        const s = drafts.create({ guildId: G1, documentId: doc.id, now: clock.now, storage, scheduler: clock.scheduler, idleMs: 500 });
        s.use(doc);
        await s.saveNow();
        const base = fake.writesTo(drafts.V2_NAMESPACE).length;
        const unbind = s.bindLifecycle(target);
        assert(listeners.length === 3, 'the lifecycle binding registers pagehide + visibilitychange + freeze',
            String(listeners.length));

        s.changed(model.setContent(doc, 'page 1'));
        assert(s.pendingSave() === true && fake.writesTo(drafts.V2_NAMESPACE).length === base,
            'an edit is still only pending');
        target.dispatch('pagehide');
        await settle();
        assert(fake.writesTo(drafts.V2_NAMESPACE).length === base + 1,
            'pagehide flushes immediately, without waiting for the idle timer',
            String(fake.writesTo(drafts.V2_NAMESPACE).length - base));
        assert(s.pendingSave() === false, 'and cancels the pending timer');
        assert(storedRecord(fake, drafts.V2_NAMESPACE, 'drafts', s.key(), 'after pagehide').document.content === 'page 1',
            'the flushed write is the newest state');

        s.changed(model.setContent(doc, 'page 2'));
        target.dispatch('visibilitychange');
        await settle();
        assert(fake.writesTo(drafts.V2_NAMESPACE).length === base + 1,
            'a visibilitychange while VISIBLE does not write');
        target.visibilityState = 'hidden';
        target.dispatch('visibilitychange');
        await settle();
        assert(fake.writesTo(drafts.V2_NAMESPACE).length === base + 2,
            'and hiding the tab does', String(fake.writesTo(drafts.V2_NAMESPACE).length - base));

        s.changed(model.setContent(doc, 'page 3'));
        target.dispatch('freeze');
        await settle();
        assert(fake.writesTo(drafts.V2_NAMESPACE).length === base + 3, 'freeze flushes too');

        // a flush with nothing to save writes nothing
        target.dispatch('pagehide');
        await settle();
        assert(fake.writesTo(drafts.V2_NAMESPACE).length === base + 3, 'a redundant flush is a no-op');

        unbind();
        assert(listeners.length === 0, 'unbind removes every listener');
        s.changed(model.setContent(doc, 'page 4'));
        target.dispatch('pagehide');
        await settle();
        assert(fake.writesTo(drafts.V2_NAMESPACE).length === base + 3,
            'and after unbinding the page event no longer writes anything');

        // destroy() flushes what is pending, detaches everything, then stays inert
        const states = [];
        const s2 = drafts.create({
            guildId: G1, documentId: doc.id, now: clock.now, storage, scheduler: clock.scheduler, idleMs: 500,
            onState: (st) => states.push(st.state),
        });
        s2.use(doc);
        await s2.saveNow();
        s2.bindLifecycle(target);
        s2.changed(model.setContent(doc, 'before destroy'));
        const destroyed = await s2.destroy();
        assert(destroyed.ok === true && s2.isDirty() === false,
            'destroy() flushes the pending change before it goes', JSON.stringify(destroyed));
        assert(storedRecord(fake, drafts.V2_NAMESPACE, 'drafts', s2.key(), 'after destroy').document.content === 'before destroy',
            'and the flushed state is the one that was pending');
        assert(listeners.length === 0, 'destroy() unbinds the lifecycle listeners');
        const afterDestroy = await s2.saveNow();
        assert(afterDestroy.ok === false && afterDestroy.reason === 'destroyed',
            'a save after destroy is refused, not silently dropped', JSON.stringify(afterDestroy));
        const secondDestroy = await s2.destroy();
        assert(secondDestroy.ok === true && secondDestroy.reason === 'already-destroyed',
            'and destroy() is idempotent', JSON.stringify(secondDestroy));
    }

    // ═══════════════════════════════════════════════════════════
    section('15. storage failure and degraded mode');
    // ═══════════════════════════════════════════════════════════
    {
        const clock = clockPair();
        const doc = model.fromEditorDocument({ content: 'doomed', embeds: [{ title: 'T' }] }, { ids: nextIds() });

        // (a) the database refuses to open at all
        const fakeA = fakeIndexedDB();
        fakeA.controls.unavailable = true;
        const storageA = drafts.idbStorage({ indexedDB: fakeA.indexedDB, scheduler: clock.scheduler, timeoutMs: 100 });
        const sA = drafts.create({ guildId: G1, documentId: doc.id, now: clock.now, storage: storageA, scheduler: clock.scheduler });
        sA.use(doc);
        const loadA = await sA.load();
        assert(loadA.ok === false && loadA.status === 'unavailable',
            'an unopenable database makes load report unavailable', loadA.status);
        const saveA = await sA.saveNow();
        assert(saveA.ok === false && saveA.reason === 'open-error',
            'and save report the real reason', saveA.reason);
        assert(sA.isDirty() === true, 'the document stays dirty (nothing was lost)');
        assert(sA.state().state === 'error' && sA.state().degraded !== null,
            'the state machine shows error + degraded', JSON.stringify(sA.state()));
        assert(fakeA.writesTo(drafts.V2_NAMESPACE).length === 0, 'and no write was attempted successfully');

        // (b) writes start failing after a good first save
        const fakeB = fakeIndexedDB();
        const storageB = drafts.idbStorage({ indexedDB: fakeB.indexedDB, scheduler: clock.scheduler, timeoutMs: 100 });
        const sB = drafts.create({ guildId: G1, documentId: doc.id, now: clock.now, storage: storageB, scheduler: clock.scheduler });
        sB.use(doc);
        assert((await sB.saveNow()).ok === true, 'the first save succeeds');
        fakeB.controls.failTx = true;
        sB.changed(model.setContent(doc, 'doomed 2'));
        const failed = await sB.saveNow();
        assert(failed.ok === false && failed.reason === 'write-error',
            'a failing write is reported, not thrown', JSON.stringify(failed));
        assert(sB.isDirty() === true && sB.state().state === 'error',
            'and the session is dirty + in error state', sB.state().state);
        assert(fakeB.raw(drafts.V2_NAMESPACE, 'drafts', sB.key()).document.content === 'doomed',
            'storage still holds the last good draft (no partial write)');
        const stateErr = sB.state();
        assert(stateErr.lastError && stateErr.lastError.reason === 'write-error',
            'the last error is exposed for the UI', JSON.stringify(stateErr.lastError));

        // (c) the database opens but never answers
        const fakeC = fakeIndexedDB();
        fakeC.controls.hangOpen = true;
        const storageC = drafts.idbStorage({ indexedDB: fakeC.indexedDB, scheduler: clock.scheduler, timeoutMs: 1500 });
        const sC = drafts.create({ guildId: G1, documentId: doc.id, now: clock.now, storage: storageC, scheduler: clock.scheduler });
        sC.use(doc);
        clock.advance(0);
        const loadPending = sC.load();
        clock.advance(1500);
        clock.runDue();
        const loadC = await loadPending;
        assert(loadC.ok === false && loadC.status === 'unavailable' && storageC.reason() === 'open-timeout',
            'a hanging database times out into degraded mode instead of spinning', storageC.reason());
        assert(storageC.stats().timeouts > 0, 'the timeout is counted', JSON.stringify(storageC.stats()));

        // (d) A TRANSIENT write failure degrades the adapter, and a later
        //     attempt can recover IN-SESSION: this is the policy 5d-3 Step A
        //     made true (the failure used to latch for the rest of the page
        //     life, so "retry on next idle" could never succeed). The work is
        //     still in memory and still dirty while the disk is broken, so
        //     nothing is lost either way.
        const fakeD = fakeIndexedDB();
        const storageD = drafts.idbStorage({ indexedDB: fakeD.indexedDB, scheduler: clock.scheduler });
        const sD = drafts.create({ guildId: G1, documentId: doc.id, now: clock.now, storage: storageD, scheduler: clock.scheduler });
        sD.use(doc);
        await sD.saveNow();
        fakeD.controls.failTx = true;
        sD.changed(model.setContent(doc, 'recoverable'));
        assert((await sD.saveNow()).ok === false, 'a write fails while the database is broken');
        assert(storageD.isAvailable() === false && storageD.reason() === 'write-error',
            'the adapter marks itself degraded', storageD.reason());
        assert(fakeD.raw(drafts.V2_NAMESPACE, 'drafts', sD.key()).document.content === 'doomed',
            'and the last good draft is what storage still holds (no partial write)');

        fakeD.controls.failTx = false;                       // the disk is fine again
        const opensBeforeRetry = storageD.stats().opens;
        const retried = await sD.saveNow();
        assert(retried.ok === true && retried.revision === 2,
            'the very next save retries and SUCCEEDS (recovery is not a page reload)',
            JSON.stringify(retried));
        assert(storageD.stats().recoveries === 1,
            'exactly one recovery was performed', String(storageD.stats().recoveries));
        assert(storageD.stats().opens === opensBeforeRetry + 1,
            'and it re-opened the database exactly once',
            storageD.stats().opens + ' vs ' + opensBeforeRetry);
        assert(storageD.isAvailable() === true && sD.state().degraded === null,
            'the degraded latch is gone once the write lands', JSON.stringify(sD.state().degraded));
        assert(fakeD.raw(drafts.V2_NAMESPACE, 'drafts', sD.key()).document.content === 'recoverable',
            'with the newest content');
        assert(sD.isDirty() === false && sD.state().state === 'saved', 'and the session is clean again');

        // A fresh adapter on the same database still reads the recovered draft
        // (the in-session path above must not have left anything odd behind).
        const storageD2 = drafts.idbStorage({ indexedDB: fakeD.indexedDB, scheduler: clock.scheduler });
        const sD2 = drafts.create({ guildId: G1, documentId: doc.id, now: clock.now, storage: storageD2, scheduler: clock.scheduler });
        const reread = await sD2.load();
        assert(reread.ok === true && reread.document.content === 'recoverable',
            'the next session reads the recovered draft from disk',
            reread.document && reread.document.content);
        assert(reread.meta.revision === 2, 'including its revision', JSON.stringify(reread.meta && reread.meta.revision));
    }

    // ═══════════════════════════════════════════════════════════
    section('16 & 17. v2 never writes the v1 namespace; the v1 draft is untouched');
    // ═══════════════════════════════════════════════════════════
    {
        const clock = clockPair();
        const fake = fakeIndexedDB();
        const v1Draft = {
            content: 'v1 content **bold**',
            embeds: [{
                title: 'v1 title', description: 'v1 desc', color: '#7c5cbf',
                author: 'Nero', authorIcon: 'https://cdn.example/v1.png',
                fields: [{ name: 'n', value: 'v', inline: true }],
                image: 'https://cdn.example/v1i.png', footer: 'v1 footer', timestamp: '2026-09-24T08:00:00.000Z',
            }],
            attachments: [{ id: 'att_1', name: 'notes.png', type: 'image/png', size: 1234, blob: '<the real draft holds a Blob here>', source: 'local' }],
            ts: 1700000000000,
        };
        fake.seed(drafts.V1_NAMESPACE, 1, drafts.V1_STORE, drafts.V1_KEY, v1Draft);
        const v1Before = drafts.serializeRecord(fake.raw(drafts.V1_NAMESPACE, drafts.V1_STORE, drafts.V1_KEY));

        const storage = drafts.idbStorage({ indexedDB: fake.indexedDB, scheduler: clock.scheduler });
        const doc = model.fromEditorDocument({ content: 'v2 side', embeds: [{ title: 'V2' }] }, { ids: nextIds(), guildId: G1 });
        const s = drafts.create({ guildId: G1, documentId: doc.id, now: clock.now, storage, scheduler: clock.scheduler });
        s.use(doc);
        await s.saveNow();
        s.changed(model.setContent(doc, 'v2 side edited'));
        await s.saveNow();
        await s.listDrafts();
        await s.meta.rememberDocument(G1, doc.id);
        await s.importFromV1(drafts.v1ImportStorage({ indexedDB: fake.indexedDB, scheduler: clock.scheduler }));

        assert(fake.writesTo(drafts.V1_NAMESPACE).length === 0,
            'across a full v2 session and a v1 import, ZERO writes/deletes hit the v1 database',
            JSON.stringify(fake.writesTo(drafts.V1_NAMESPACE)));
        assert(fake.ops().filter(e => e.db === drafts.V1_NAMESPACE && e.op === 'get').length > 0,
            'the v1 draft WAS read (the import really happened)');
        assert(drafts.serializeRecord(fake.raw(drafts.V1_NAMESPACE, drafts.V1_STORE, drafts.V1_KEY)) === v1Before,
            'and the v1 draft is byte-identical afterwards');
        const v1Adapter = drafts.v1ImportStorage({ indexedDB: fake.indexedDB, scheduler: clock.scheduler });
        assert(v1Adapter.readOnly === true && v1Adapter.name === drafts.V1_NAMESPACE,
            'the v1 adapter is read-only and points at v1');
        const refusedPut = await v1Adapter.put('anything', {});
        const refusedDelete = await v1Adapter.remove('anything');
        assert(refusedPut.ok === false && refusedPut.reason === 'read-only'
            && refusedDelete.ok === false && refusedDelete.reason === 'read-only',
            'and it refuses writes outright, before reaching the database',
            JSON.stringify([refusedPut, refusedDelete]));
        assert(fake.ops().every(e => !(e.db === drafts.V1_NAMESPACE && (e.op === 'put' || e.op === 'delete'))),
            'nothing in the operation log can write to v1');
        assert(fake.dbs.get(drafts.V1_NAMESPACE).stores.has('draft')
            && !fake.dbs.get(drafts.V1_NAMESPACE).stores.has('drafts'),
            'v2 never added its stores to the v1 database');
    }

    // ═══════════════════════════════════════════════════════════
    section('18. explicit v1 → v2 import');
    // ═══════════════════════════════════════════════════════════
    {
        const clock = clockPair();
        const fake = fakeIndexedDB();
        const editorEmbed = {
            title: 'Imported', description: 'from v1', color: '#5865f2',
            author: 'Nero', authorIcon: 'https://cdn.example/a.png', authorUrl: 'https://nilive.example/a',
            footer: 'F', footerIcon: 'https://cdn.example/f.png',
            image: 'https://cdn.example/i.png', thumbnail: 'https://cdn.example/t.png',
            url: 'https://nilive.example/e', timestamp: '2026-09-24T08:00:00.000Z',
            fields: [{ name: 'n1', value: 'v1', inline: true }, { name: '', value: '' }],
        };
        fake.seed(drafts.V1_NAMESPACE, 1, drafts.V1_STORE, drafts.V1_KEY, {
            content: 'imported content', embeds: [editorEmbed], attachments: [], ts: 1700000000000,
        });
        const storage = drafts.idbStorage({ indexedDB: fake.indexedDB, scheduler: clock.scheduler });
        const s = drafts.create({ guildId: G1, documentId: 'imported_doc', now: clock.now, storage, scheduler: clock.scheduler });
        const imported = await s.importFromV1(drafts.v1ImportStorage({ indexedDB: fake.indexedDB, scheduler: clock.scheduler }));
        assert(imported.ok === true && imported.status === 'imported',
            'the v1 draft imports explicitly', imported.status);
        assert(imported.document.content === 'imported content', 'with its content');
        assert(imported.document.embeds.length === 1 && imported.document.embeds[0].title === 'Imported',
            'and its embed');
        assert(imported.source.updatedAt === 1700000000000 && imported.source.embedCount === 1,
            'the import reports where it came from', JSON.stringify(imported.source));
        assert(imported.warnings.length === 0, 'and nothing was withheld (there were no attachments)');

        // the imported document produces exactly what v1 would have sent
        const v1Payload = JSON.stringify({ content: 'imported content', embeds: V1.cleanEmbedsForPayload([editorEmbed]) });
        const v2Payload = JSON.stringify(model.toDiscordPayload(imported.document));
        assert(v1Payload === v2Payload,
            'the imported document\'s canonical payload equals v1\'s payload for the same draft',
            '\n      v1: ' + v1Payload + '\n      v2: ' + v2Payload);

        // importing writes NOTHING until the caller saves
        const writesBeforeSave = fake.writesTo(drafts.V2_NAMESPACE).length;
        assert(writesBeforeSave === 0, 'importing does not write a v2 draft by itself');
        s.use(imported.document);
        const saved = await s.saveNow();
        assert(saved.ok === true, 'the caller decides when to save it as a v2 draft', JSON.stringify(saved));
        assert(fake.writesTo(drafts.V2_NAMESPACE).length === 1
            && fake.writesTo(drafts.V1_NAMESPACE).length === 0,
            'and that write goes to the v2 namespace only');

        // an empty v1 database is a normal answer
        const emptyFake = fakeIndexedDB();
        const s2 = drafts.create({ guildId: G1, documentId: 'x', now: clock.now, storage, scheduler: clock.scheduler });
        const emptyImport = await s2.importFromV1(drafts.v1ImportStorage({ indexedDB: emptyFake.indexedDB, scheduler: clock.scheduler }));
        assert(emptyImport.ok === true && emptyImport.status === 'empty',
            'a missing v1 draft is reported as empty, not as an error', emptyImport.status);

        // and a broken v1 database is a reported failure, not a crash
        const brokenFake = fakeIndexedDB();
        brokenFake.controls.failOpen = true;
        const brokenImport = await s2.importFromV1(drafts.v1ImportStorage({ indexedDB: brokenFake.indexedDB, scheduler: clock.scheduler, timeoutMs: 50 }));
        assert(brokenImport.ok === false && brokenImport.status === 'unavailable',
            'an unreadable v1 database is reported as unavailable', brokenImport.status);
    }

    // ═══════════════════════════════════════════════════════════
    section('19. attachments and blobs are refused, not stored (phase 2 seam)');
    // ═══════════════════════════════════════════════════════════
    {
        const clock = clockPair();
        const fake = fakeIndexedDB();
        const storage = drafts.idbStorage({ indexedDB: fake.indexedDB, scheduler: clock.scheduler });
        const s = drafts.create({ guildId: G1, documentId: 'att_doc', now: clock.now, storage, scheduler: clock.scheduler });

        // a v1 draft with attachments imports the text and REPORTS the files
        fake.seed(drafts.V1_NAMESPACE, 1, drafts.V1_STORE, drafts.V1_KEY, {
            content: 'with files', embeds: [{ title: 'T' }], ts: 1,
            attachments: [
                { id: 'a1', name: 'one.png', type: 'image/png', size: 10, blob: null, source: 'local' },
                { id: 'a2', name: 'two.pdf', type: 'application/pdf', size: 20, blob: null, source: 'local' },
            ],
        });
        const imported = await s.importFromV1(drafts.v1ImportStorage({ indexedDB: fake.indexedDB, scheduler: clock.scheduler }));
        assert(imported.ok === true && imported.warnings.length === 1,
            'importing a draft with attachments succeeds and warns', JSON.stringify(imported.warnings));
        const warning = (imported.warnings || [])[0];
        if (warning) {
            assert(warning.kind === 'attachments-not-imported' && warning.count === 2
                && warning.names.join(',') === 'one.png,two.pdf',
                'the warning names exactly what did not come across', JSON.stringify(warning));
        } else {
            assert(false, 'the import warning names what did not come across');
        }
        assert(imported.document.assets && Object.keys(imported.document.assets).length === 0,
            'the imported document carries an EMPTY asset registry (the phase-2 seam)');

        // a document carrying a Blob cannot be stored at all
        const poisoned = model.fromEditorDocument({ content: 'x', embeds: [{ title: 'T' }] }, { ids: nextIds() });
        poisoned.assets = { a1: { blob: new Blob(['x'], { type: 'image/png' }), filename: 'one.png' } };
        s.use(poisoned);
        const before = fake.writesTo(drafts.V2_NAMESPACE).length;
        const refused = await s.saveNow();
        assert(refused.ok === false && refused.reason === 'not-serializable',
            'a Blob inside the document makes the save refuse', JSON.stringify(refused));
        assert(String(refused.path || '').indexOf('assets') !== -1,
            'and the refusal names the path', String(refused.path));
        assert(fake.writesTo(drafts.V2_NAMESPACE).length === before,
            'nothing at all was written for that document');
        assert(s.state().state === 'error' && s.state().lastError.reason === 'not-serializable',
            'and the error state is exposed', JSON.stringify(s.state().lastError));

        // a runtime object attached to a document cannot reach storage either —
        // normalization rebuilds the document shape, so an unknown key is dropped
        // before the gate ever sees it, and the gate still refuses it if asked
        const withFn = model.fromEditorDocument({ content: 'y', embeds: [{ title: 'T' }] }, { ids: nextIds() });
        withFn.onSave = function () { };
        s.use(withFn);
        const savedWithoutFn = await s.saveNow();
        assert(savedWithoutFn.ok === true, 'a stray function is dropped by normalization, so the save is clean',
            JSON.stringify(savedWithoutFn));
        assert(storedRecord(fake, drafts.V2_NAMESPACE, 'drafts', s.key(), 'after the stray-function save').document.onSave === undefined,
            'and it is nowhere in the stored bytes');
        let directRefusal = null;
        try { drafts.assertSerializable(withFn); } catch (e) { directRefusal = e; }
        assert(directRefusal !== null && directRefusal.message.indexOf('function') !== -1,
            'the gate itself refuses that shape when asked directly', directRefusal && directRefusal.message);

        // and a clean document saves fine afterwards (the failure was not sticky)
        const clean = model.fromEditorDocument({ content: 'z', embeds: [{ title: 'T' }] }, { ids: nextIds() });
        s.use(clean);
        const okSave = await s.saveNow();
        assert(okSave.ok === true, 'a clean document saves afterwards', JSON.stringify(okSave));
    }

    // ═══════════════════════════════════════════════════════════
    section('20. a newer schema is never silently downgraded');
    // ═══════════════════════════════════════════════════════════
    {
        const clock = clockPair();
        const fake = fakeIndexedDB();
        const storage = drafts.idbStorage({ indexedDB: fake.indexedDB, scheduler: clock.scheduler });
        const doc = model.fromEditorDocument({ content: 'future', embeds: [{ title: 'T' }] }, { ids: nextIds() });
        const future = Object.assign(
            drafts.buildRecord({ guildId: G1, documentId: doc.id, document: doc, now: 1000, previous: null }),
            { schemaVersion: drafts.RECORD_VERSION + 1, futureOnlyField: { blocks: [{ type: 'container' }] } }
        );
        const key = drafts.draftKey(G1, doc.id);
        fake.seed(drafts.V2_NAMESPACE, 1, 'drafts', key, future);
        const beforeBytes = drafts.serializeRecord(future);

        const s = drafts.create({ guildId: G1, documentId: doc.id, now: clock.now, storage, scheduler: clock.scheduler });
        const res = await s.load();
        assert(res.ok === false && res.status === 'future',
            'a record from a newer build is recognised as future', res.status);
        assert(res.preserved === true && drafts.serializeRecord(res.record) === beforeBytes,
            'and handed back byte-for-byte, including fields this build does not understand');

        s.use(doc);
        const refused = await s.saveNow();
        assert(refused.ok === false && refused.reason === 'future' && refused.blocked === true,
            'writes are refused while it is preserved', JSON.stringify(refused));
        assert(drafts.serializeRecord(fake.raw(drafts.V2_NAMESPACE, 'drafts', key)) === beforeBytes,
            'so the newer record is STILL byte-identical in storage');
        s.changed(model.setContent(doc, 'edited anyway'));
        clock.advance(100000); clock.runDue(); await settle();
        assert(drafts.serializeRecord(fake.raw(drafts.V2_NAMESPACE, 'drafts', key)) === beforeBytes,
            'even the idle writer cannot overwrite it');

        // the explicit, user-driven override
        assert(s.resolveGuard('replace') === true, 'the caller can explicitly take the draft over');
        const after = await s.saveNow();
        assert(after.ok === true && after.revision === 2,
            'and then the save continues the revision sequence from the newer record',
            JSON.stringify(after));
        assert(storedRecord(fake, drafts.V2_NAMESPACE, 'drafts', key, 'after the takeover').schemaVersion === drafts.RECORD_VERSION,
            'the stored record now carries this build\'s version (the user was asked first)');

        // a document-level newer version is treated the same way
        const docFuture = Object.assign({}, doc, { schemaVersion: model.SCHEMA_VERSION + 1 });
        const rec = drafts.buildRecord({ guildId: G1, documentId: doc.id, document: doc, now: 1, previous: null });
        rec.document = docFuture;
        const verdict = drafts.validateRecord(rec, { key: key });
        assert(verdict.status === 'future' && verdict.ok === false,
            'a newer DOCUMENT version is refused too', verdict.status);
    }

    // ═══════════════════════════════════════════════════════════
    section('20b. the future-record guard holds across the whole lifecycle');
    // ═══════════════════════════════════════════════════════════
    {
        const clock = clockPair();
        const fake = fakeIndexedDB();
        const storage = drafts.idbStorage({ indexedDB: fake.indexedDB, scheduler: clock.scheduler });
        const doc = model.fromEditorDocument({ content: 'mine', embeds: [{ title: 'T' }] },
            { ids: nextIds(), guildId: G1, id: drafts.newDocumentId({ time: 9000, sequence: 1 }) });
        const key = drafts.draftKey(G1, doc.id);
        const future = Object.assign(
            drafts.buildRecord({ guildId: G1, documentId: doc.id, document: doc, now: 1000, previous: null }),
            { schemaVersion: drafts.RECORD_VERSION + 1 });
        fake.seed(drafts.V2_NAMESPACE, 1, 'drafts', key, future);
        const bytes = drafts.serializeRecord(future);
        const untouched = (why) => assert(
            drafts.serializeRecord(fake.raw(drafts.V2_NAMESPACE, 'drafts', key)) === bytes,
            'the future record is byte-identical after ' + why);
        const silent = (why) => assert(fake.writesTo(drafts.V2_NAMESPACE).length === 0,
            'and nothing was written for ' + why, JSON.stringify(fake.writesTo(drafts.V2_NAMESPACE)));

        // A guard that any later code path can lift is not a guard: `force`
        // means "write even if clean", never "ignore the preservation guard".
        const s = drafts.create({ guildId: G1, documentId: doc.id, now: clock.now, storage, scheduler: clock.scheduler, idleMs: 300 });
        await s.load();
        s.use(model.setContent(doc, 'my edit'));
        const forced = await s.saveNow({ force: true });
        assert(forced.ok === false && forced.reason === 'future' && forced.blocked === true,
            'saveNow({force:true}) cannot bypass the future guard', JSON.stringify(forced));
        untouched('saveNow({force:true})'); silent('saveNow({force:true})');

        // the idle writer
        s.changed(model.setContent(doc, 'idle edit'));
        assert(s.pendingSave() === true, 'a change still schedules normally (the editor stays responsive)');
        clock.advance(1000); clock.runDue(); await settle();
        untouched('the idle flush'); silent('the idle flush');

        // the page lifecycle
        const listeners = [];
        const target = {
            visibilityState: 'visible',
            addEventListener: (t, fn) => listeners.push({ t: t, fn: fn }),
            removeEventListener: (t, fn) => { const i = listeners.findIndex((l) => l.t === t && l.fn === fn); if (i !== -1) listeners.splice(i, 1); },
            dispatch: (t) => listeners.filter((l) => l.t === t).forEach((l) => l.fn()),
        };
        s.bindLifecycle(target);
        target.dispatch('pagehide');
        target.dispatch('freeze');
        target.visibilityState = 'hidden';
        target.dispatch('visibilitychange');
        await settle();
        untouched('pagehide / freeze / hidden'); silent('pagehide / freeze / hidden');

        // destroy()'s flush
        const destroyed = await s.destroy();
        assert(destroyed.ok === false && destroyed.reason === 'future' && s.isDirty() === true,
            'destroy()\'s flush is refused too, and the edit stays dirty', JSON.stringify(destroyed.reason));
        untouched('destroy()'); silent('destroy()');

        // store-driven automatic saving
        const store = storeMod.createStore({ document: doc, scheduler: clock.scheduler, now: clock.now, reducers: storeMod.createReducers() });
        const s2 = drafts.create({ guildId: G1, documentId: doc.id, now: clock.now, storage, scheduler: clock.scheduler, idleMs: 300 });
        await s2.load();
        s2.attach(store);
        store.dispatch({ type: 'content/set', text: 'automatic edit' });
        clock.advance(1000); clock.runDue(); await settle();
        untouched('a store-driven automatic save'); silent('a store-driven automatic save');

        // and a second session sees the same protection
        const s3 = drafts.create({ guildId: G1, documentId: doc.id, now: clock.now, storage, scheduler: clock.scheduler });
        const reloaded = await s3.load();
        assert(reloaded.status === 'future' && s3.guard().reason === 'future',
            'a new session is guarded in exactly the same way', reloaded.status);
        assert(s3.resolveGuard('keep') === false && s.resolveGuard('keep') === false,
            'resolveGuard("keep") never resolves — guarded or not');
        untouched('resolveGuard("keep")');
        assert(fake.writesTo(drafts.V2_NAMESPACE).length === 0,
            'ZERO writes across the entire lifecycle (load, save, force, idle, pagehide, freeze, hidden, destroy, store, reopen)',
            JSON.stringify(fake.writesTo(drafts.V2_NAMESPACE)));
        assert(drafts.serializeRecord(fake.raw(drafts.V2_NAMESPACE, 'drafts', key)) === bytes,
            'the preserved bytes are still identical right before the approved takeover');

        // the ONE approved path
        assert(s3.resolveGuard('replace') === true, 'resolveGuard("replace") is the approved takeover');
        s3.use(model.setContent(doc, 'taken over'));      // the caller adopts the document it means to keep
        const taken = await s3.saveNow();
        assert(taken.ok === true && taken.revision === future.revision + 1,
            'and it continues the preserved revision sequence', JSON.stringify(taken));
        assert(fake.writesTo(drafts.V2_NAMESPACE).length === 1,
            'exactly one write happened in the whole lifecycle — the approved one',
            String(fake.writesTo(drafts.V2_NAMESPACE).length));
        assert(fake.raw(drafts.V2_NAMESPACE, 'drafts', key).schemaVersion === drafts.RECORD_VERSION,
            'and the record now carries this build\'s version');
    }

    // ═══════════════════════════════════════════════════════════
    section('21. nothing but plain data can enter a draft');
    // ═══════════════════════════════════════════════════════════
    {
        const rejects = [
            ['a Blob', { blob: new Blob(['x']) }, 'Blob/File', 'blob'],
            ['a DOM node', { node: { nodeType: 1, tagName: 'DIV' } }, 'DOM node', 'node'],
            ['a function', { fn: function () { } }, 'function', 'fn'],
            ['an undefined value', { missing: undefined }, 'undefined value', 'missing'],
            ['NaN', { n: NaN }, 'non-finite number', 'n'],
            ['Infinity', { n: Infinity }, 'non-finite number', 'n'],
            ['a Date', { when: new Date(0) }, 'Date (use an ISO string)', 'when'],
            ['a RegExp', { re: /x/ }, 'RegExp', 're'],
            ['a Map', { m: new Map() }, 'Map', 'm'],
            ['a Set', { s: new Set() }, 'Set', 's'],
            ['a class instance', { node: new (class Thing { constructor() { this.a = 1; } })() }, 'class instance', 'node'],
            ['a BigInt', { big: BigInt(1) }, 'bigint', 'big'],
        ];
        let structuralFallbacks = 0;
        rejects.forEach(([name, value, expected, key]) => {
            let err = null;
            try { drafts.assertSerializable(value); } catch (e) { err = e; }
            const words = err ? err.message.split(' at ')[0] : '';
            assert(err !== null && err.name === 'SerializationError'
                && (words === expected || words.indexOf('class instance (') === 0),
                'assertSerializable refuses ' + name + ' and names a reason',
                err ? err.message : 'no error');
            assert(err !== null && typeof err.path === 'string' && err.path.indexOf('.' + key) === err.path.length - (key.length + 1),
                'and reports where it found it (' + name + ')', err ? err.path : '');
            if (words !== expected) {
                structuralFallbacks++;
                const actual = value[key];
                const typeName = actual && actual.constructor ? actual.constructor.name : typeof actual;
                assert(words === 'class instance (' + typeName + ')',
                    'the structural fallback names the actual type (' + name + ')', words);
            }
        });

        // cycles are caught rather than hanging
        const cyclic = { a: 1 };
        cyclic.self = cyclic;
        let cycleErr = null;
        try { drafts.assertSerializable(cyclic); } catch (e) { cycleErr = e; }
        assert(cycleErr !== null && cycleErr.message.indexOf('cycle') !== -1,
            'a cycle is refused instead of overflowing the stack', cycleErr && cycleErr.message);

        // The realm-specific reasons (Blob/File, Date, Map, Set, RegExp) only
        // fire when the module and the data live in the SAME realm — which is
        // true in a browser and is what this section reproduces by handing the
        // module the host's own globals.
        const sameRealm = {
            window: { NERO: { embed: { model: model } } }, console: console,
            Blob: Blob, File: typeof File === 'undefined' ? undefined : File,
            Date: Date, RegExp: RegExp, Map: Map, Set: Set, WeakMap: WeakMap,
        };
        vm.createContext(sameRealm);
        vm.runInContext(DRAFTS_SRC, sameRealm);
        const draftsSameRealm = sameRealm.window.NERO.embed.drafts;
        assert(structuralFallbacks > 0,
            'this environment needed the structural fallback for ' + structuralFallbacks + ' case(s) '
            + '(realm-specific checks cannot see across realms — the fallback is why that is safe)');
        [
            ['a Blob', { blob: new Blob(['x']) }, 'Blob/File at <root>.blob'],
            // a File IS a Blob, so it reports the combined reason — which is
            // why that reason is worded 'Blob/File' rather than 'Blob'
            ['a File', { file: new File(['x'], 'x.png', { type: 'image/png' }) }, 'Blob/File at <root>.file'],
            ['a Date', { when: new Date(0) }, 'Date (use an ISO string) at <root>.when'],
            ['a RegExp', { re: /x/ }, 'RegExp at <root>.re'],
            ['a Map', { m: new Map() }, 'Map at <root>.m'],
            ['a Set', { s: new Set() }, 'Set at <root>.s'],
        ].forEach(([name, value, expected]) => {
            let err = null;
            try { draftsSameRealm.assertSerializable(value); } catch (e) { err = e; }
            assert(err !== null && err.message === expected,
                'in one realm, ' + name + ' is refused with exactly the right reason',
                err ? err.message : 'no error');
        });
        let okDoc = null;
        try { okDoc = draftsSameRealm.assertSerializable(model.blankMessageDocument({ ids: nextIds() })); } catch (e) { okDoc = e.message; }
        assert(okDoc === true, 'and a real document still passes in that realm', String(okDoc));

        // a real document passes, and survives toStorable byte-identically
        const doc = model.fromEditorDocument({
            content: 'plain', embeds: [{ title: 'T', fields: [{ name: 'n', value: 'v' }] }],
        }, { ids: nextIds(), guildId: G1 });
        assert(drafts.assertSerializable(doc) === true, 'a real document passes the gate');
        assert(JSON.stringify(drafts.toStorable(doc)) === JSON.stringify(doc),
            'and toStorable returns the same bytes');
        assert(drafts.assertSerializable(model.blankMessageDocument({ ids: nextIds() })) === true,
            'even a blank document passes');
        // and the documents produced by the model never contain such values
        const verdicts = [];
        ['content/set', 'embed/add', 'field/add'].forEach(() => verdicts.push(drafts.assertSerializable(doc)));
        assert(verdicts.every(v => v === true), 'repeated checks agree');
    }

    // ═══════════════════════════════════════════════════════════
    section('round trip through the Step-1 model (payload identity)');
    // ═══════════════════════════════════════════════════════════
    {
        const clock = clockPair();
        const fake = fakeIndexedDB();
        const storage = drafts.idbStorage({ indexedDB: fake.indexedDB, scheduler: clock.scheduler });
        const corpus = [
            ['content only', { content: 'hello **world**' }],
            ['emoji only', { content: '🎲🎲' }],
            ['title only', { embeds: [{ title: 'T' }] }],
            ['everything', {
                embeds: [{
                    title: 'A', description: 'd `code`', color: '#123456',
                    author: 'Nero', authorIcon: 'https://cdn.example/a.png', authorUrl: 'https://nilive.example/a',
                    footer: 'F', footerIcon: 'https://cdn.example/f.png', timestamp: '2026-09-24T08:00:00.000Z',
                    image: 'https://cdn.example/i.png', thumbnail: 'https://cdn.example/t.png',
                    url: 'https://nilive.example/e',
                    fields: [{ name: 'n1', value: 'v1', inline: true }, { name: '', value: '' }],
                }],
            }],
            ['attachment refs', { embeds: [{ image: 'attachment://a.png', title: 'A' }] }],
            ['unsafe urls', { embeds: [{ title: 'T', url: 'javascript:alert(1)', image: 'javascript:alert(2)' }] }],
            ['blank document', {}],
            ['ten embeds', { embeds: Array.from({ length: 10 }, (_, i) => ({ title: 'E' + i, fields: [{ name: 'f', value: 'v' }] })) }],
        ];
        let payloadMismatches = [];
        let notFixedPoint = [];
        let phantomDirty = [];
        let blankWroteSomething = null;
        let corpusSeq = 0;
        const corpusDoc = (spec) => model.fromEditorDocument(spec, {
            ids: nextIds(), guildId: G1,
            id: drafts.newDocumentId({ time: 1790284740000, sequence: ++corpusSeq }),
        });
        for (const [name, spec] of corpus) {
            const doc = corpusDoc(spec);
            const s = drafts.create({ guildId: G1, documentId: doc.id, now: clock.now, storage, scheduler: clock.scheduler });
            // `use()` returns the normalized document the session adopts —
            // that (not a second normalize call, which would mint fresh ids
            // for an empty embeds array) is what gets stored.
            const canonical = s.use(doc);
            assert(model.hashDocument(model.normalizeDocument(canonical)) === model.hashDocument(canonical),
                'normalization is idempotent once ids exist (' + name + ')');
            const saved = await s.saveNow();
            if (!model.documentHasContent(doc)) {
                // An untouched blank document is not dirty, so it is never
                // written: opening the editor must not create empty drafts.
                blankWroteSomething = { name: name, result: saved };
                const fresh = drafts.create({ guildId: G1, documentId: doc.id, now: clock.now, storage, scheduler: clock.scheduler });
                const loaded = await fresh.load();
                if (loaded.status !== 'empty') notFixedPoint.push(name + ' (blank draft wrote something)');
                continue;
            }
            if (!saved.ok) notFixedPoint.push(name + ' (save failed: ' + saved.reason + ')');
            const fresh = drafts.create({ guildId: G1, documentId: doc.id, now: clock.now, storage, scheduler: clock.scheduler });
            const loaded = await fresh.load();
            if (JSON.stringify(model.toDiscordPayload(loaded.document)) !== JSON.stringify(model.toDiscordPayload(doc))) {
                payloadMismatches.push(name);
            }
            if (model.hashDocument(loaded.document) !== model.hashDocument(canonical)) {
                notFixedPoint.push(name);
            }
            // What comes back must BE what is on disk: if the stored bytes
            // needed repairing on the way out, save → load was not a fixed
            // point and the editor would silently rewrite the document.
            const onDisk = fake.raw(drafts.V2_NAMESPACE, 'drafts', s.key());
            if (!onDisk || !onDisk.document) {
                notFixedPoint.push(name + ' (nothing reached storage)');
            } else {
                if (JSON.stringify(loaded.document) !== JSON.stringify(onDisk.document)) {
                    notFixedPoint.push(name + ' (loaded ≠ stored bytes)');
                }
                if (onDisk.documentHash !== model.hashDocument(onDisk.document)) {
                    notFixedPoint.push(name + ' (stored hash does not describe the stored bytes)');
                }
                if (JSON.stringify(model.normalizeDocument(onDisk.document)) !== JSON.stringify(onDisk.document)) {
                    notFixedPoint.push(name + ' (the stored bytes are not already canonical)');
                }
            }
            // after a reload the draft must not look dirty: if it did, the
            // store would immediately write back a document nobody edited
            if (fresh.isDirty() !== false) phantomDirty.push(name);
        }
        assert(payloadMismatches.length === 0,
            `all ${corpus.length} documents keep their canonical payload across a storage round trip`,
            payloadMismatches.join(', '));
        assert(notFixedPoint.length === 0,
            'and the stored bytes ARE the canonical/normalized document (save → load is a fixed point)',
            notFixedPoint.join(', '));
        assert(phantomDirty.length === 0,
            'a reloaded draft is never dirty (no write-back of an unedited document)',
            phantomDirty.join(', '));
        assert(blankWroteSomething && blankWroteSomething.result.skipped === true
            && blankWroteSomething.result.reason === 'clean',
            'an untouched blank document is never written at all',
            blankWroteSomething ? JSON.stringify(blankWroteSomething.result) : 'case missing');

        // the corpus without an untouched blank document is what the payload
        // identity claim covers, so state the count rather than implying all
        const nonBlank = corpus.filter(([, spec]) => model.documentHasContent(corpusDoc(spec)));
        assert(nonBlank.length === corpus.length - 1,
            `payload identity was proven for ${nonBlank.length} of ${corpus.length} fixtures (the blank one is never stored)`);

        // an EXPLICIT save of a blank document does persist it, and still round-trips
        const blankDoc = corpusDoc({});
        const sb = drafts.create({ guildId: G1, documentId: blankDoc.id, now: clock.now, storage, scheduler: clock.scheduler });
        sb.use(blankDoc);
        const forced = await sb.saveNow({ force: true });
        assert(forced.ok === true, 'a blank document can be saved explicitly (force)', JSON.stringify(forced));
        const sb2 = drafts.create({ guildId: G1, documentId: blankDoc.id, now: clock.now, storage, scheduler: clock.scheduler });
        const blankLoaded = await sb2.load();
        assert(blankLoaded.ok === true && blankLoaded.status === 'ok'
            && JSON.stringify(model.toDiscordPayload(blankLoaded.document)) === JSON.stringify(model.toDiscordPayload(blankDoc)),
            'and it comes back with the same (empty) payload', blankLoaded.status);

        // the store's own dirty tracking survives a save/load cycle
        const doc = model.fromEditorDocument({ content: 'store cycle', embeds: [{ title: 'T' }] }, { ids: nextIds(), guildId: G1 });
        const store = storeMod.createStore({ document: doc, scheduler: clock.scheduler, now: clock.now, reducers: storeMod.createReducers() });
        const s = drafts.create({ guildId: G1, documentId: doc.id, now: clock.now, storage, scheduler: clock.scheduler, idleMs: 200 });
        s.use(doc);
        s.attach(store);
        await s.saveNow();
        store.dispatch({ type: 'content/set', text: 'store cycle edited' });
        clock.advance(200); clock.runDue(); await settle();
        assert(store.isDirty() === false && s.isDirty() === false,
            'a store edit lands in storage and both agree it is saved');
        const reload = drafts.create({ guildId: G1, documentId: doc.id, now: clock.now, storage, scheduler: clock.scheduler });
        const reloaded = await reload.load();
        assert(reloaded.document.content === 'store cycle edited',
            'and the stored bytes are the store those edits produced');
        assert(typeof reload.savedHash() === 'string'
            && reload.savedHash() === model.hashDocument(reloaded.document),
            'the loaded session recognizes the document as already saved', String(reload.savedHash()));

        // The store hands the session a document it did not normalize itself
        // (a content-only draft has an empty embeds array). What lands on disk
        // must still be canonical, or a reload silently rewrites the draft.
        const clock2 = clockPair();
        const fake2 = fakeIndexedDB();
        const storage2 = drafts.idbStorage({ indexedDB: fake2.indexedDB, scheduler: clock2.scheduler });
        const bare = model.fromEditorDocument({ content: '' }, { ids: nextIds(), guildId: G1, id: drafts.newDocumentId({ time: 7000, sequence: 1 }) });
        assert(bare.embeds.length === 0, 'the store document really starts with an empty embeds array');
        const store2 = storeMod.createStore({
            document: bare, scheduler: clock2.scheduler, now: clock2.now, reducers: storeMod.createReducers(),
        });
        const s3 = drafts.create({ guildId: G1, documentId: bare.id, now: clock2.now, storage: storage2, scheduler: clock2.scheduler, idleMs: 100 });
        s3.use(bare);
        s3.attach(store2);
        store2.dispatch({ type: 'content/set', text: 'content only draft' });
        clock2.advance(100); clock2.runDue(); await settle();
        const storedBare = fake2.raw(drafts.V2_NAMESPACE, 'drafts', s3.key());
        assert(storedBare && storedBare.document.content === 'content only draft',
            'the store-driven save wrote the draft');
        assert(JSON.stringify(model.normalizeDocument(storedBare.document)) === JSON.stringify(storedBare.document),
            'and the stored bytes are canonical (a reload cannot change them)');
        const reloadBare = drafts.create({ guildId: G1, documentId: bare.id, now: clock2.now, storage: storage2, scheduler: clock2.scheduler });
        const reloadedBare = await reloadBare.load();
        assert(JSON.stringify(reloadedBare.document) === JSON.stringify(storedBare.document),
            'so save → load is a fixed point for a store-driven edit');
        assert(reloadedBare.status === 'ok',
            'with no repair needed on the way out', reloadedBare.status);

        // detach stops the writes
        const detach = s.attach(store);
        store.dispatch({ type: 'content/set', text: 'while attached' });
        assert(s.pendingSave() === true, 'an attached store schedules a save when it changes');
        clock.advance(1000); clock.runDue(); await settle();
        assert(storedRecord(fake, drafts.V2_NAMESPACE, 'drafts', s.key(), 'while attached').document.content === 'while attached',
            'and that save reaches storage');

        detach();
        const afterDetach = fake.writesTo(drafts.V2_NAMESPACE).length;
        store.dispatch({ type: 'content/set', text: 'detached edit' });
        assert(s.pendingSave() === false, 'a DETACHED store cannot schedule a save (the subscription is gone)');
        clock.advance(5000); clock.runDue(); await settle();
        assert(fake.writesTo(drafts.V2_NAMESPACE).length === afterDetach,
            'and no write happens for it', String(fake.writesTo(drafts.V2_NAMESPACE).length - afterDetach));
    }

    // ═══════════════════════════════════════════════════════════
    section('22. savedDocument(): the last PERSISTED document, and only for this identity');
    // ═══════════════════════════════════════════════════════════
    // The one question a hash cannot answer is "what was the message before the
    // change I want to discard?". savedDocument() answers it — and it must be
    // impossible for the answer to belong to a DIFFERENT draft, or to follow an
    // in-memory edit. These checks are the contract 5d-2 builds the discard
    // action on.
    {
        const clock = clockPair();
        const fake = fakeIndexedDB();
        const storage = drafts.idbStorage({ indexedDB: fake.indexedDB, scheduler: clock.scheduler });
        const docA = model.fromEditorDocument({ content: 'saved A', embeds: [{ title: 'A' }] }, { ids: nextIds(), guildId: G1 });
        const s = drafts.create({ guildId: G1, documentId: docA.id, now: clock.now, storage, scheduler: clock.scheduler });

        assert(s.savedDocument() === null,
            'a session that has never loaded or saved has nothing persisted to return');

        s.use(docA);
        assert(s.savedDocument() === null,
            'use() resets the saved identity: taking ownership of a document is not persisting it');

        const first = await s.saveNow();
        assert(first.ok === true, 'the first save succeeds', JSON.stringify(first));
        const saved = s.savedDocument();
        assert(!!saved, 'and now a persisted document exists');
        assert(saved.content === 'saved A' &&
            model.stableStringify(model.toDiscordPayload(saved)) ===
            model.stableStringify(model.toDiscordPayload(docA)),
            'it is the document that was written, byte for byte',
            JSON.stringify(saved && saved.content));

        // A clone, twice over: on the way in and on the way out.
        assert(s.savedDocument() !== s.document(),
            'INVARIANT: the baseline is not the live document — no aliasing, ever');
        assert(s.savedDocument() !== s.savedDocument() && s.savedDocument() !== saved,
            'INVARIANT: every read is a fresh clone, never a shared reference');
        assert(s.document() === s.document(),
            'while the live document is the same object every time (the Store owns that one)');
        saved.content = 'mutated by the caller';
        saved.embeds[0].title = 'mutated too';
        assert(s.savedDocument().content === 'saved A' &&
            s.savedDocument().embeds[0].title === 'A',
            'mutating the returned document cannot change what the session holds',
            JSON.stringify(s.savedDocument().content));
        const live = s.document();
        live.content = 'edited in place by a future bug';
        assert(s.savedDocument().content === 'saved A',
            'and an in-place write to the live document cannot follow it either',
            JSON.stringify(s.savedDocument().content));
        live.content = 'saved A';              // put the live document back

        // An edit through the normal path leaves the baseline alone.
        s.changed(model.setContent(s.document(), 'edited B'));
        assert(s.savedDocument().content === 'saved A',
            'an unsaved edit does not move the baseline');
        assert(fake.writesTo(drafts.V2_NAMESPACE).length === 1,
            'and nothing was written for it yet', String(fake.writesTo(drafts.V2_NAMESPACE).length));

        // In flight: not the baseline until persistence CONFIRMS it.
        const inflight = s.saveNow();
        assert(s.savedDocument().content === 'saved A',
            'while the write of B is in flight the baseline is still A');
        const second = await inflight;
        assert(second.ok === true, 'the write of B succeeds', JSON.stringify(second));
        assert(s.savedDocument().content === 'edited B',
            'and only then does the baseline become B', s.savedDocument().content);

        // The snapshot rule: when a write completes while the user has ALREADY
        // typed on, the baseline is the document that was WRITTEN — never the
        // one that happens to be in memory at completion time.
        s.changed(model.setContent(s.document(), 'typed during the write'));
        const racing = s.saveNow();                     // writes 'typed during the write'
        s.changed(model.setContent(s.document(), 'typed after it'));   // in memory, unsaved
        await racing;
        assert(s.savedDocument().content === 'typed during the write',
            'a write that lands while the user typed on leaves the WRITTEN document as the baseline',
            s.savedDocument().content);
        assert(s.document().content === 'typed after it',
            'with the live document untouched by it', s.document().content);

        // A FAILED write never becomes the baseline.
        fake.controls.failTx = true;
        s.changed(model.setContent(s.document(), 'C that cannot be stored'));
        const failed = await s.saveNow();
        assert(failed.ok === false && s.state().lastError,
            'the next write fails', JSON.stringify(failed));
        assert(s.savedDocument().content === 'typed during the write',
            'and a failed save does not replace the baseline', s.savedDocument().content);

        // A degraded storage does not either.
        fake.controls.failTx = false;
        fake.controls.unavailable = true;
        s.changed(model.setContent(s.document(), 'D while storage is gone'));
        const degraded = await s.saveNow();
        assert(degraded.ok === false, 'with storage unavailable the save cannot succeed',
            JSON.stringify(degraded));
        assert(s.savedDocument().content === 'typed during the write',
            'and the baseline is untouched by it', s.savedDocument().content);
        fake.controls.unavailable = false;

        // IDENTITY: the baseline belongs to ONE draft and dies with it. A fresh
        // storage, because the adapter's degraded flag is deliberately latched
        // once a write fails (one flag, no spinning) — and that is not what
        // this block is about.
        const clock3 = clockPair();
        const fake3 = fakeIndexedDB();
        const storage3 = drafts.idbStorage({ indexedDB: fake3.indexedDB, scheduler: clock3.scheduler });
        const s3 = drafts.create({ guildId: G1, now: clock3.now, storage: storage3, scheduler: clock3.scheduler });
        s3.use(model.fromEditorDocument({ content: 'first identity' }, { ids: nextIds(), guildId: G1 }));
        await s3.saveNow();
        assert(s3.savedDocument() && s3.savedDocument().content === 'first identity',
            'setup: the first identity has a baseline', JSON.stringify(s3.savedDocument() && s3.savedDocument().content));

        const docE = model.fromEditorDocument({ content: 'E new draft' }, { ids: nextIds(), guildId: G1 });
        s3.start(docE);
        assert(s3.savedDocument() === null,
            'after start() a new identity has nothing persisted — the old baseline is gone');

        const third = await s3.saveNow();
        assert(third.ok === true && s3.savedDocument().content === 'E new draft',
            'saving the new identity establishes its own baseline', JSON.stringify(third));

        const docF = model.fromEditorDocument({ content: 'F adopted' }, { ids: nextIds(), guildId: G1 });
        s3.use(docF);
        assert(s3.savedDocument() === null,
            'use() drops the previous identity\'s baseline');

        const fourth = await s3.saveNow();
        assert(fourth.ok === true && s3.savedDocument().content === 'F adopted',
            'and the adopted document gets its own', JSON.stringify(fourth));

        const store = storeMod.createStore({
            document: model.blankMessageDocument({ ids: nextIds() }),
            scheduler: clock3.scheduler, now: clock3.now,
            reducers: storeMod.createReducers(),
        });
        s3.attach(store);
        assert(s3.savedDocument() === null,
            'attach() to a clean store re-establishes what "saved" means (nothing, yet)');

        // A load for one identity followed by a start for another: the loaded
        // record must not be restorable through the new identity.
        const clock2 = clockPair();
        const fake2 = fakeIndexedDB();
        const storage2 = drafts.idbStorage({ indexedDB: fake2.indexedDB, scheduler: clock2.scheduler });
        const stored = model.fromEditorDocument({ content: 'stored record' }, { ids: nextIds(), guildId: G1 });
        const writer = drafts.create({ guildId: G1, documentId: stored.id, now: clock2.now, storage: storage2, scheduler: clock2.scheduler });
        writer.use(stored);
        await writer.saveNow();

        const reader = drafts.create({ guildId: G1, now: clock2.now, storage: storage2, scheduler: clock2.scheduler });
        const loaded = await reader.load({ documentId: stored.id, guildId: G1 });
        assert(loaded.ok === true && reader.savedDocument() && reader.savedDocument().content === 'stored record',
            'a successful load establishes the baseline from the record', JSON.stringify(loaded.status));
        reader.start(model.fromEditorDocument({ content: 'another draft' }, { ids: nextIds(), guildId: G1 }));
        assert(reader.savedDocument() === null,
            'and starting a different draft identity cannot reach the loaded document');
        assert(reader.document().content === 'another draft',
            'the session is editing the new document', reader.document().content);
    }

    // ═══════════════════════════════════════════════════════════
    section('23. recover(): the latch, the re-probe, and what it must never touch');
    // ═══════════════════════════════════════════════════════════
    // The contract behind "retry on next idle" — and behind the Save button in
    // 5d-3 Step B. A TRANSIENT write failure may be retried; an ENVIRONMENT
    // failure is final and must not be re-probed (a missing IndexedDB or a
    // hanging open would otherwise cost a full open attempt per save for an
    // answer that cannot change). Recovery is not a second persistence path.
    {
        const clock = clockPair();
        const scheduler = clock.scheduler;

        /**
         * A double for the failure modes the base fake cannot be *told* to
         * produce. It delegates to the real fake and, the moment the adapter
         * takes the database handle, makes the next readwrite transaction fail
         * in the requested way — exactly what a browser does when it does.
         */
        const forcedIdb = (mode) => {
            const fake = fakeIndexedDB();
            const armed = { on: false };
            const baseOpen = fake.indexedDB.open;
            fake.indexedDB.open = (name, version) => {
                const req = baseOpen(name, version);
                let success = null;
                let upgrade = null;
                Object.defineProperty(req, 'onsuccess', {
                    configurable: true,
                    get() {
                        if (!success) return undefined;
                        return (ev) => {
                            const db = req.result;
                            const realTransaction = db.transaction;
                            db.transaction = (storeName, txMode) => {
                                if (!armed.on || txMode !== 'readwrite') {
                                    return realTransaction.call(db, storeName, txMode);
                                }
                                if (mode === 'transaction-failed') throw new Error('forced: transaction() threw');
                                if (mode === 'request-failed') {
                                    return { objectStore() { throw new Error('forced: objectStore() threw'); } };
                                }
                                if (mode === 'write-aborted') {
                                    const tx = { oncomplete: null, onerror: null, onabort: null };
                                    tx.objectStore = () => ({ put: () => ({}) });
                                    // A microtask, not a timer: the adapter assigns its
                                    // handlers synchronously right after transaction()
                                    // returns, so this runs once they are in place.
                                    Promise.resolve().then(() => { if (tx.onabort) tx.onabort(); });
                                    return tx;
                                }
                                if (mode === 'write-timeout') {
                                    // A transaction whose request never settles: the
                                    // adapter's own timeout is the only answer.
                                    return { oncomplete: null, onerror: null, onabort: null,
                                        objectStore: () => ({ put: () => ({}) }) };
                                }
                                return realTransaction.call(db, storeName, txMode);
                            };
                            success(ev);
                        };
                    },
                    set(fn) { success = fn; },
                });
                Object.defineProperty(req, 'onupgradeneeded', {
                    configurable: true,
                    get() {
                        if (!upgrade) return undefined;
                        return (ev) => {
                            if (mode === 'upgrade-failed') {
                                req.result.objectStoreNames = {
                                    contains() { throw new Error('forced: upgrade threw'); },
                                };
                            }
                            upgrade(ev);
                        };
                    },
                    set(fn) { upgrade = fn; },
                });
                return req;
            };
            return { fake: fake, break: () => { armed.on = true; }, fix: () => { armed.on = false; } };
        };

        // One armer per reason: it returns a session on a fresh adapter and a
        // function that breaks the next write.
        const armers = {
            'write-error': () => { const f = fakeIndexedDB(); return { fake: f, break: () => { f.controls.failTx = true; }, fix: () => { f.controls.failTx = false; } }; },
            'write-timeout': () => forcedIdb('write-timeout'),
            'transaction-failed': () => forcedIdb('transaction-failed'),
            'request-failed': () => forcedIdb('request-failed'),
            'write-aborted': () => forcedIdb('write-aborted'),
        };

        // ── (a) every TRANSIENT reason is recoverable, and the retry lands ──
        for (const reason of Object.keys(armers)) {
            const armer = armers[reason]();
            const storage = drafts.idbStorage({
                indexedDB: armer.fake.indexedDB, scheduler: scheduler,
                timeoutMs: 50,           // the timeout cases must not wait
            });
            const doc = model.fromEditorDocument({ content: 'first' }, { ids: nextIds(), guildId: G1 });
            const s = drafts.create({ guildId: G1, documentId: doc.id, now: clock.now, storage, scheduler: scheduler });
            s.use(doc);
            assert((await s.saveNow()).ok === true, reason + ': setup save succeeds');
            armer.break();
            s.changed(model.setContent(s.document(), 'second'));
            const pending = s.saveNow();
            await settle(6);                 // let the request reach the storage
            clock.advance(200);              // and let the adapter's own timeout fire
            clock.runDue();
            const failed = await pending;
            assert(failed.ok === false, reason + ': the write fails', JSON.stringify(failed));
            assert(storage.isAvailable() === false && storage.reason() === reason,
                reason + ': the adapter latched on exactly that reason', String(storage.reason()));
            assert(s.isDirty() === true && s.state().state === 'error',
                reason + ': the session is dirty and in error', s.state().state);

            armer.fix();
            const recoveriesBefore = storage.stats().recoveries;
            const opensBefore = storage.stats().opens;
            const retried = await s.saveNow();
            assert(retried.ok === true, reason + ': the next save recovers and succeeds',
                JSON.stringify(retried));
            assert(storage.stats().recoveries === recoveriesBefore + 1,
                reason + ': exactly one recovery was performed',
                String(storage.stats().recoveries - recoveriesBefore));
            assert(storage.stats().opens === opensBefore + 1,
                reason + ': with exactly one re-open', String(storage.stats().opens - opensBefore));
            assert(storage.isAvailable() === true && s.state().degraded === null,
                reason + ': the latch is cleared', String(s.state().degraded));
            assert(s.state().lastError === null && s.state().state === 'saved',
                reason + ': and the session reads saved', s.state().state);
            assert(armer.fake.raw(drafts.V2_NAMESPACE, 'drafts', s.key()).document.content === 'second',
                reason + ': the retried document is what storage holds');
            assert(retried.revision === 2, reason + ': the revision advanced normally',
                String(retried.revision));
        }

        // ── (b) every ENVIRONMENT reason refuses recovery, with no re-probe ──
        const finals = [
            ['no-indexeddb', null],
            ['open-error', () => { const f = fakeIndexedDB(); f.controls.unavailable = true; return f; }],
            ['open-timeout', () => { const f = fakeIndexedDB(); f.controls.hangOpen = true; return f; }],
            ['upgrade-failed', () => forcedIdb('upgrade-failed')],
        ];
        for (const [reason, make] of finals) {
            const made = make ? make() : null;
            const fake = made && made.fake ? made.fake : made;   // forcedIdb() returns a rig
            const storage = drafts.idbStorage({
                indexedDB: fake ? fake.indexedDB : null,
                scheduler: scheduler, timeoutMs: 50,
            });
            const doc = model.fromEditorDocument({ content: 'doomed' }, { ids: nextIds(), guildId: G1 });
            const s = drafts.create({ guildId: G1, documentId: doc.id, now: clock.now, storage, scheduler: scheduler });
            s.use(doc);
            const attemptPending = s.saveNow();
            await settle(6);
            clock.advance(200);              // a hanging open answers on the timeout
            clock.runDue();
            const attempted = await attemptPending;
            assert(attempted.ok === false, reason + ': the save cannot succeed', JSON.stringify(attempted));
            assert(storage.reason() === reason,
                reason + ': the adapter is latched on exactly that reason', String(storage.reason()));
            const opensBefore = storage.stats().opens;
            assert(storage.recover() === false, reason + ': recovery is refused');
            assert(storage.reason() === reason && storage.isAvailable() === false,
                reason + ': and it stays latched', String(storage.reason()));
            assert(storage.stats().opens === opensBefore,
                reason + ': with no re-open attempt (no request wasted on a final answer)',
                String(storage.stats().opens - opensBefore));
            // …and a further save attempt changes nothing.
            const againPending = s.saveNow();
            await settle(6);
            clock.advance(200);
            clock.runDue();
            const again = await againPending;
            assert(again.ok === false && storage.stats().opens === opensBefore,
                reason + ': nor does a later save try to open the database again',
                String(storage.stats().opens - opensBefore));
            assert(s.isDirty() === true && s.document().content === 'doomed',
                reason + ': the document is still in memory, still dirty');
        }

        // ── (c) THE FULL STORY with a store attached ──
        // What a successful retry updates (revision, baseline, the store\'s
        // confirmation) and what recovery itself must never touch.
        const fake = fakeIndexedDB();
        const storage = drafts.idbStorage({ indexedDB: fake.indexedDB, scheduler: scheduler });
        // Normalized the way the page canonicalizes a loaded document: the store
        // then holds a document that is a fixed point of normalization, so the
        // hash a write confirms is the hash of what the store holds.
        const doc = model.normalizeDocument(
            model.fromEditorDocument({ content: 'saved A' }, { ids: nextIds(), guildId: G1 }));
        const store = storeMod.createStore({
            document: doc, scheduler: scheduler, now: clock.now,
            reducers: storeMod.createReducers(),
        });
        const s = drafts.create({ guildId: G1, documentId: doc.id, now: clock.now, storage, scheduler: scheduler });
        s.use(doc);
        s.attach(store);
        await s.saveNow();
        const hashA = store.savedDocumentHash();
        assert(store.isDirty() === false && s.savedDocument().content === 'saved A',
            'setup: A is persisted; the store is clean and the baseline is A');

        store.dispatch({ type: 'content/set', text: 'B' });
        const identityBefore = s.documentId();
        const revisionBefore = s.state().revision;
        fake.controls.failTx = true;
        const failedB = await s.saveNow();
        assert(failedB.ok === false, 'the write of B fails', JSON.stringify(failedB));
        assert(store.isDirty() === true, 'the store is dirty while B is unpersisted');
        assert(s.savedDocument().content === 'saved A',
            'and the baseline still describes what is on disk (A), not the failed B');

        fake.controls.failTx = false;
        const retryB = await s.saveNow();
        assert(retryB.ok === true && retryB.revision === revisionBefore + 1,
            'the retry succeeds and advances the revision normally', JSON.stringify(retryB));
        assert(s.documentId() === identityBefore,
            'recovery and the retry never change the document identity', String(s.documentId()));
        assert(store.getDocument().content === 'B',
            'the store\'s document was never replaced by the adapter or the session',
            store.getDocument().content);
        assert(store.isDirty() === false && store.savedDocumentHash() !== hashA,
            'and the store became clean only because the write CONFIRMED B by hash');
        assert(store.savedDocumentHash() === model.hashDocument(store.getDocument()),
            'the confirmed hash is the hash of what the store is holding');
        assert(s.savedDocument().content === 'B',
            'with the session baseline updated to the newly persisted document',
            s.savedDocument().content);

        // Recovery alone: a storage-side operation that touches nothing else.
        fake.controls.failTx = true;
        store.dispatch({ type: 'content/set', text: 'C' });
        await s.saveNow();
        const pointerKey = drafts.metaKey(G1, 'last');
        const pointerBefore = JSON.stringify(fake.raw(drafts.V2_NAMESPACE, 'meta', pointerKey) || null);
        const recordBefore = JSON.stringify(fake.raw(drafts.V2_NAMESPACE, 'drafts', s.key()));
        assert(storage.recover() === true, 'setup: the transient failure is recoverable');
        const documentAfter = store.getDocument();
        assert(documentAfter.content === 'C',
            'recovery does not mutate the document', documentAfter.content);
        assert(store.getDocument() === documentAfter,
            'nor swap the document object the store holds');
        assert(s.documentId() === identityBefore, 'nor change the document id');
        assert(store.isDirty() === true, 'nor mark the store clean');
        assert(s.savedDocument().content === 'B',
            'nor claim the unsaved C is persisted', s.savedDocument().content);
        assert(JSON.stringify(fake.raw(drafts.V2_NAMESPACE, 'drafts', s.key())) === recordBefore,
            'nor write anything while recovering');
        assert(JSON.stringify(fake.raw(drafts.V2_NAMESPACE, 'meta', pointerKey) || null) === pointerBefore,
            'nor touch the last-draft pointer');
        assert(storage.recover() === false,
            'a second recovery is a no-op (the latch is already clear) and never throws');
        assert(JSON.stringify(fake.raw(drafts.V2_NAMESPACE, 'meta', pointerKey) || null) === pointerBefore,
            'and the pointer is still untouched after it');

        // ── (d) a recovery that does not help stays honest ──
        fake.controls.failTx = true;
        const stillBroken = await s.saveNow();
        assert(stillBroken.ok === false && stillBroken.reason === 'write-error',
            'while the disk is still broken the retry FAILS again, honestly', JSON.stringify(stillBroken));
        assert(store.getDocument().content === 'C' && store.isDirty() === true,
            'the edit is still in memory and still dirty');
        assert(s.state().degraded === 'write-error' && s.state().lastError,
            'the adapter is latched on the real reason again', String(s.state().degraded));
        assert(s.savedDocument().content === 'B',
            'and nothing pretended C or even B was re-persisted', s.savedDocument().content);
        fake.controls.failTx = false;

        // ── (e) an adapter without recover() is unaffected ──
        // The call site is guarded, so a caller-supplied storage keeps working:
        // no new requirement is imposed on the storage contract.
        const plainWrites = [];
        const plainStorage = {
            isAvailable: () => true,
            reason: () => null,
            get: () => Promise.resolve(null),
            put: (key, value) => { plainWrites.push(key); return Promise.resolve({ ok: true }); },
        };
        const plainDoc = model.fromEditorDocument({ content: 'plain' }, { ids: nextIds(), guildId: G1 });
        const plain = drafts.create({ guildId: G1, documentId: plainDoc.id, now: clock.now, storage: plainStorage, scheduler: scheduler });
        plain.use(plainDoc);
        assert((await plain.saveNow()).ok === true,
            'a storage that never heard of recover() still saves normally');
        assert(plainWrites.length === 1, 'and was written exactly once', String(plainWrites.length));
    }

    // ═══════════════════════════════════════════════════════════
    section('24. retryable(): the same question as recover(), asked without acting');
    // ═══════════════════════════════════════════════════════════
    // A UI has to decide whether to OFFER a retry, and finding out by calling
    // recover() would clear the latch as a side effect of a question. This is
    // the pure half: same answer, nothing changes — and the answer must match
    // recover()'s decision exactly, or a button would offer a retry that
    // cannot work (or hide one that can).
    {
        const clock = clockPair();

        // A transient write failure: retryable, and PROVABLY unanswered.
        const fake = fakeIndexedDB();
        const storage = drafts.idbStorage({ indexedDB: fake.indexedDB, scheduler: clock.scheduler });
        const doc = model.fromEditorDocument({ content: 'text' }, { ids: nextIds(), guildId: G1 });
        const s = drafts.create({ guildId: G1, documentId: doc.id, now: clock.now, storage, scheduler: clock.scheduler });
        s.use(doc);
        assert(s.retryable() === false, 'nothing has failed yet, so nothing is retryable');
        assert(storage.retryable() === false && storage.reason() === null,
            'and the adapter has nothing to report');

        await s.saveNow();
        assert(s.retryable() === false, 'a successful save leaves nothing to retry');

        fake.controls.failTx = true;
        s.changed(model.setContent(s.document(), 'changed'));
        const failed = await s.saveNow();
        assert(failed.ok === false, 'rig: the write failed',
            JSON.stringify(failed));
        const reasonBefore = storage.reason();
        const recoveriesBefore = storage.stats().recoveries;
        const opensBefore = storage.stats().opens;
        assert(storage.retryable() === true, 'the adapter says the latch is transient',
            String(reasonBefore));
        assert(s.retryable() === true, 'and so does the session');

        // The point of the exercise: asking is not doing.
        for (let i = 0; i < 5; i++) {
            s.retryable();
            storage.retryable();
        }
        assert(storage.reason() === reasonBefore && storage.isAvailable() === false,
            'asking five times did NOT clear the latch',
            String(storage.reason()));
        assert(storage.stats().recoveries === recoveriesBefore,
            'no recovery was performed by asking', String(storage.stats().recoveries));
        assert(storage.stats().opens === opensBefore,
            'and no database was opened by asking', String(storage.stats().opens));

        // The two answers agree — which is what makes the button honest.
        assert(storage.retryable() === true && storage.recover() === true &&
            storage.reason() === null && s.retryable() === false,
            'the predicate and the action agree: what retryable() promises, recover() accepts');

        fake.controls.failTx = false;
        const retried = await s.saveNow();
        assert(retried.ok === true && s.retryable() === false,
            'and after the retry lands there is nothing left to retry', JSON.stringify(retried));

        // An environment failure: not retryable, and asking must not re-probe.
        const noIdb = drafts.idbStorage({ indexedDB: null, scheduler: clock.scheduler });
        const s2 = drafts.create({ guildId: G1, documentId: 'doc-none', now: clock.now, storage: noIdb, scheduler: clock.scheduler });
        s2.use(doc);
        await s2.saveNow();
        const opensForNone = noIdb.stats().opens;
        assert(noIdb.reason() === 'no-indexeddb', 'rig: there is no IndexedDB',
            String(noIdb.reason()));
        assert(noIdb.retryable() === false && s2.retryable() === false,
            'an environment failure is NOT retryable');
        assert(noIdb.recover() === false, 'and recover() refuses it, as it always did');
        assert(noIdb.stats().opens === opensForNone,
            'with no open attempt from either the question or the refusal',
            String(noIdb.stats().opens - opensForNone));
        assert(s2.retryable() === false, 'and it stays a no');

        // A storage that cannot answer is not assumed retryable: an offer to
        // retry must never be a guess.
        const plain = {
            isAvailable: () => false,
            reason: () => 'write-error',
            get: () => Promise.resolve(null),
            put: () => Promise.resolve({ ok: false, reason: 'write-error' }),
        };
        const s3 = drafts.create({ guildId: G1, documentId: 'doc-plain', now: clock.now, storage: plain, scheduler: clock.scheduler });
        s3.use(doc);
        await s3.saveNow();
        assert(s3.retryable() === false,
            'a storage with no retryable() is not treated as retryable (no promises it cannot keep)');

        // A non-write failure is not a retry question at all. (The storage here
        // WORKS, so the failure has to come from the document itself.)
        const fake4 = fakeIndexedDB();
        const storage4 = drafts.idbStorage({ indexedDB: fake4.indexedDB, scheduler: clock.scheduler });
        const s4 = drafts.create({ guildId: G1, documentId: 'doc-bad', now: clock.now, storage: storage4, scheduler: clock.scheduler });
        // How the existing suite produces one: a value the model keeps and
        // JSON cannot carry. (Normalization coerces plain fields, so the
        // poison goes into the asset registry, which is preserved as-is.)
        const poisoned = model.fromEditorDocument({ content: 'x', embeds: [{ title: 'T' }] }, { ids: nextIds() });
        poisoned.assets = { a1: { blob: new Blob(['x'], { type: 'image/png' }), filename: 'one.png' } };
        s4.use(poisoned);
        const bad = await s4.saveNow();
        assert(bad.ok === false && bad.reason === 'not-serializable',
            'rig: the document cannot be serialized', JSON.stringify(bad));
        assert(s4.retryable() === false && storage4.retryable() === false,
            'a document that cannot be serialized offers no retry either');
        assert(storage4.stats().puts === 0 && storage4.isAvailable() === true,
            'and it never reached storage (so nothing latched, and nothing is blamed on the disk)',
            JSON.stringify(storage4.stats()));
    }

    section('summary');
    console.log(`\ndrafts: ${pass} passed, ${fail} failed`);
    if (fail) { console.log('Failures:'); failures.forEach(f => console.log(' -', f)); process.exit(1); }
    console.log('ALL DRAFT-PERSISTENCE TESTS PASSED');
})().catch((err) => {
    console.error('\nharness crashed:', err && err.stack || err);
    process.exit(1);
});
