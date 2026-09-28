#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════
   scripts/test_message_builder_asset_store.js
   Message Builder v2 — phase 2, step 7b: the asset BYTE store and
   the object-URL lifecycle.

   WHAT THIS SUITE IS ABOUT
   embed/asset-store.js is where asset bytes actually live. assets.js
   (7a) decides what a file IS; this module is the only thing that
   writes bytes down, hands them back, and lends them to the preview
   as blob: URLs. The two ways it can hurt a user are "I said I saved
   it and I did not" and "the preview shows the wrong picture", so
   this harness holds it to:

     A. the module, and the ONE storage boundary it reuses (the
        existing drafts.js adapter, pointed at the assets store — not
        a second opener), plus its distance from the DOM and the
        network;
     B. put → get: byte-exact, and provably in the 'assets' store of
        the v2 database rather than in the document store;
     C. reload: a new connection and a new instance find the bytes;
        a different browser profile does not, and says so;
     D. identity: bytes whose id does not match them are refused, so
        "one id → one byte sequence" holds for the store's lifetime;
     E. dedupe: the same file picked four times is one record, one
        write, one URL (Case E of the plan);
     F. missing / corrupt / truncated entries are reported, never
        thrown and never served;
     G. unavailable and degraded storage: memory for this session,
        `persisted: false`, the adapter's own reason, and no crash;
     H. the object-URL lifecycle: stable while unchanged, re-minted
        when the type changes, revoked on release/remove/teardown, and
        never handed out for bytes the store does not have;
     I. survey(): the id → availability report 7c/7e reconcile with;
     J. isolation and determinism: two instances, two realms, no
        module state, no document mutation, and the frozen document
        boundary still refusing typed arrays and Blobs;
     K. cost: a printed measurement with a loose guard.

   Run:  node scripts/test_message_builder_asset_store.js
         NERO_ASSET_STORE_SRC=/path/to/copy.js   (the mutation battery)
   ═══════════════════════════════════════════════════════════════ */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { createFakeIdb } = require('./support/dom_stub.js');

let pass = 0, fail = 0; const failures = [];
function assert(cond, name, extra) {
    if (cond) { pass++; console.log('  PASS', name); }
    else { fail++; failures.push(name + (extra ? ' — ' + extra : '')); console.log('  FAIL', name, extra || ''); }
}
function section(t) { console.log('\n== ' + t + ' =='); }
function eq(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
function sorted(list) { return list.slice().sort(); }

const ROOT = path.join(__dirname, '..');
const SRC_PATH = {
    assets: process.env.NERO_ASSETS_SRC || path.join(ROOT, 'dashboard', 'static', 'js', 'embed', 'assets.js'),
    model: process.env.NERO_MODEL_SRC || path.join(ROOT, 'dashboard', 'static', 'js', 'embed', 'model.js'),
    drafts: process.env.NERO_DRAFTS_SRC || path.join(ROOT, 'dashboard', 'static', 'js', 'embed', 'drafts.js'),
    store: process.env.NERO_ASSET_STORE_SRC || path.join(ROOT, 'dashboard', 'static', 'js', 'embed', 'asset-store.js'),
};
const SRC = {};
Object.keys(SRC_PATH).forEach(k => { SRC[k] = fs.readFileSync(SRC_PATH[k], 'utf8'); });

/** Comments removed, so a source scan cannot be fooled by prose. */
function codeOnly(src) {
    return String(src)
        .replace(/\/\*[\s\S]*?\*\//g, ' ')
        .replace(/(^|[^:])\/\/[^\n]*/g, '$1 ');
}
const CODE = codeOnly(SRC.store);

/**
 * A realm with NOTHING in it but `window` and `console`: no DOM, no
 * IndexedDB, no Blob/URL, no fetch, no timers, no document. Anything the
 * module needs from the browser must arrive through its arguments, which
 * is exactly the property being tested.
 */
function loadRealm(extra) {
    const win = { NERO: { embed: {} } };
    win.window = win;
    const sandbox = Object.assign({ window: win, console: console }, extra || {});
    vm.createContext(sandbox);
    ['assets', 'model', 'drafts', 'store'].forEach(k => {
        vm.runInContext(SRC[k], sandbox, { filename: k + '.js' });
    });
    return win;
}
const win = loadRealm();
const NERO = win.NERO;
const AS = NERO.embed.assetStore;
const core = NERO.embed.assets;
const drafts = NERO.embed.drafts;
const model = NERO.embed.model;

// The adapter's scheduler must come from THIS realm: the sandbox has no
// timers, and a browser would use its own. (Real timers, short timeouts —
// the fake database settles on a macrotask.)
const scheduler = {
    setTimeout: (fn, ms) => setTimeout(fn, ms),
    clearTimeout: (id) => clearTimeout(id),
};

/** A v2 database to put bytes in, plus the adapter that reaches it. */
function db(spec) {
    const fake = createFakeIdb(spec || {});
    return {
        fake: fake,
        storage: (opts) => AS.bytesStorage(Object.assign(
            { indexedDB: fake, scheduler: scheduler, timeoutMs: 60 }, opts || {})),
    };
}

/** An object-URL factory that records everything it is asked to do. */
let urlFactorySeq = 0;
function fakeUrls() {
    // Label each factory: two of them must be able to mint DIFFERENT urls,
    // or a comparison across factories would compare two identical strings.
    const label = ++urlFactorySeq;
    const seen = { created: [], details: [], revoked: [], throwOnCreate: false, throwOnRevoke: false, empty: false };
    return {
        seen: seen,
        factory: {
            kind: 'fake',
            create: (bytes, mime) => {
                if (seen.throwOnCreate) throw new Error('createObjectURL exploded');
                if (seen.empty) return '';
                const url = 'blob:fake' + label + '/' + (seen.created.length + 1);
                seen.created.push(url);
                seen.details.push({ length: bytes.length, mime: mime, type: Object.prototype.toString.call(bytes), bytes: bytes });
                return url;
            },
            revoke: (url) => {
                if (seen.throwOnRevoke) throw new Error('revokeObjectURL exploded');
                seen.revoked.push(url);
            },
        },
    };
}

function assetIdOf(bytes) { return core.assetIdFromSha(core.sha256Hex(bytes)); }
function bytesOf(values) { return new Uint8Array(values); }
function asciiBytes(text) {
    const out = new Uint8Array(text.length);
    for (let i = 0; i < text.length; i++) out[i] = text.charCodeAt(i) & 0xff;
    return out;
}
/** A structurally real PNG (signature + IHDR + IEND). */
function pngBytes() {
    return bytesOf([
        0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a,
        0x00, 0x00, 0x00, 0x0d, 0x49, 0x48, 0x44, 0x52,
        0x00, 0x00, 0x00, 0x02, 0x00, 0x00, 0x00, 0x01,
        0x08, 0x06, 0x00, 0x00, 0x00, 0x72, 0xfa, 0x7a, 0x29,
        0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4e, 0x44, 0xae, 0x42, 0x60, 0x82,
    ]);
}
function sameBytes(a, b) {
    if (!a || !b || a.length !== b.length) return false;
    for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
    return true;
}
function keysOf(store) { return sorted(Object.keys(store.fake.snapshot(drafts.V2_NAMESPACE).assets || {})); }

(async function main() {

// ── A. the module, its boundary, its distance from everything ────
section('A. the module, the boundary it reuses, and its distance from the DOM');
assert(!!AS && typeof AS.create === 'function', 'asset-store.js publishes window.NERO.embed.assetStore.create');
assert(Object.isFrozen(AS), 'the API object is frozen (a caller cannot replace a rule)');
assert(typeof AS.bytesStorage === 'function' && typeof AS.browserUrls === 'function',
    'it exposes the storage factory and the browser URL factory');
assert(AS.STORE_NAME === 'assets' && drafts.V2_STORES.indexOf(AS.STORE_NAME) !== -1,
    'the store name is "assets" and comes from the v2 store list (one source for the name)',
    AS.STORE_NAME);
assert(typeof AS.ENTRY_VERSION === 'number' && AS.ENTRY_VERSION >= 1,
    'the stored byte entry carries a version, so a future shape can be told apart');

const rig = db();
const built = AS.bytesStorage({ indexedDB: rig.fake, scheduler: scheduler, timeoutMs: 60 });
assert(!!built && built.name === drafts.V2_NAMESPACE,
    'bytesStorage() builds the app\'s existing adapter against the v2 database',
    built && built.name);
assert(eq(built.stores, drafts.V2_STORES), 'and hands it the v2 store list, so the upgrade path is the same one',
    JSON.stringify(built && built.stores));
assert(typeof built.put === 'function' && typeof built.get === 'function' && typeof built.remove === 'function',
    'and the adapter\'s own read/write/remove are what the byte store uses');

// No drafts.js → no adapter: an honest "no storage here", never an opener of
// its own.
const solo = loadRealm();
delete solo.NERO.embed.drafts;
assert(solo.NERO.embed.assetStore.bytesStorage() === null,
    'with drafts.js absent bytesStorage() returns null rather than opening a database itself');
assert(solo.NERO.embed.assetStore.browserUrls() === null,
    'and browserUrls() returns null in a sandbox with no URL/Blob (it never invents one)');

const NETWORK = [
    ['fetch', /\bfetch\s*\(/], ['XMLHttpRequest', /\bXMLHttpRequest\b/],
    ['WebSocket', /\bWebSocket\b/], ['EventSource', /\bEventSource\b/],
    ['sendBeacon', /\bsendBeacon\b/], ['importScripts', /\bimportScripts\b/],
];
const reached = NETWORK.filter(([, re]) => re.test(CODE)).map(([label]) => label);
assert(reached.length === 0, 'the source reaches for no network API at all', reached.join(', '));

const DOM = [
    ['a DOM query', /(^|[^.\w'"])document\s*[.\[]/m], ['element creation', /\bcreateElement\b/],
    ['events', /\baddEventListener\b/], ['timers', /\bsetTimeout\b|\bsetInterval\b/],
    ['web storage', /\b(localStorage|sessionStorage)\b/],
    ['direct IndexedDB', /(^|[^.\w'"])(indexedDB|IDBKeyRange)\s*[.(]/m],
    ['the clock', /\bDate\s*\.\s*now\b|\bnew\s+Date\b/], ['randomness', /\bMath\s*\.\s*random\b/],
];
const domReached = DOM.filter(([, re]) => re.test(CODE)).map(([label]) => label);
assert(domReached.length === 0,
    'no DOM, no timer, no clock, no randomness — and no direct IndexedDB (that is the adapter\'s job)',
    domReached.join(', '));
assert(!/^ {4}(let|var)\s/m.test(CODE), 'no module-level `let`/`var`: there is no module state to drift');
assert(/^ {4}const /m.test(CODE), 'rig: the module DOES declare its tables at module level (so the check above means something)');
assert(/indexedDB:\s*opts\.indexedDB/.test(CODE),
    'rig: the IndexedDB connection IS forwarded to the adapter (so the check above means something)');
assert(!/\bwindow\.(?!NERO|URL|webkitURL|Blob)/.test(CODE),
    'the only globals it reads are window.NERO and (inside the URL factory) window.URL/Blob');

// The browser URL factory, when the browser does have URL + Blob.
const fakeBlobs = [];
class FakeBlob {
    constructor(parts, options) {
        this.parts = parts; this.type = options && options.type; this.size = parts[0].byteLength;
        fakeBlobs.push(this);
    }
}
const urlsSeen = { created: [], revoked: [] };
const fakeWindow = {
    Blob: FakeBlob,
    URL: {
        createObjectURL: (blob) => { urlsSeen.created.push(blob); return 'blob:browser/' + urlsSeen.created.length; },
        revokeObjectURL: (url) => urlsSeen.revoked.push(url),
    },
};
const browserFactory = AS.browserUrls(fakeWindow);
assert(!!browserFactory && browserFactory.kind === 'browser',
    'browserUrls() builds a real factory when the window has URL + Blob');
const browserUrl = browserFactory.create(asciiBytes('abc'), 'image/gif');
assert(browserUrl === 'blob:browser/1' && urlsSeen.created.length === 1,
    'its create() hands the bytes to createObjectURL through a Blob');
assert(fakeBlobs.length === 1 && fakeBlobs[0].type === 'image/gif' && fakeBlobs[0].parts[0].length === 3,
    'and the Blob carries the declared content type and the bytes', JSON.stringify(fakeBlobs.map(b => b.type)));
browserFactory.revoke(browserUrl);
assert(eq(urlsSeen.revoked, ['blob:browser/1']), 'its revoke() calls revokeObjectURL');
assert(AS.browserUrls({}) === null && AS.browserUrls({ Blob: FakeBlob }) === null,
    'a window with only half the capability is treated as no capability');

// ── B. put → get, and where the bytes land ───────────────────────
section('B. putBytes → getBytes, and the bytes really live in the assets store');
const b = db();
const bUrls = fakeUrls();
const store = AS.create({ storage: b.storage(), urls: bUrls.factory });
const png = pngBytes();
const pngId = assetIdOf(png);

let put = await store.putBytes(pngId, png, { mime: 'image/PNG' });
assert(put.ok === true && put.assetId === pngId, 'putBytes() accepts bytes under their content-addressed id');
assert(put.persisted === true && put.source === 'indexeddb', 'and reports that they reached storage', JSON.stringify(put));
assert(put.duplicate === false && put.byteLength === png.length && put.mime === 'image/png',
    'with the length and a normalised content type', JSON.stringify(put));
assert(b.fake.storeNames(drafts.V2_NAMESPACE).indexOf('assets') !== -1,
    'the v2 database has the assets store', JSON.stringify(b.fake.storeNames(drafts.V2_NAMESPACE)));
assert(eq(keysOf(b), [pngId]), 'and exactly one entry, keyed by the asset id', JSON.stringify(keysOf(b)));
const stored = b.fake.snapshot(drafts.V2_NAMESPACE);
assert(eq(Object.keys(stored.drafts || {}), []) && eq(Object.keys(stored.meta || {}), []),
    'nothing was written to the draft store or the meta store (bytes never go near the document)');
const rawEntry = b.fake.snapshot(drafts.V2_NAMESPACE).assets[pngId];
assert(rawEntry.v === AS.ENTRY_VERSION && rawEntry.assetId === pngId && rawEntry.mime === 'image/png',
    'the stored entry carries its version, its id and its type');
assert(typeof rawEntry.byteLength === 'number' && rawEntry.byteLength === png.length,
    'and the byte length it was stored with');
const rawStored = await b.storage().get(pngId);
assert(Object.prototype.toString.call(rawStored.bytes) === '[object ArrayBuffer]',
    'the bytes are an ArrayBuffer — never a Blob, never a string, never JSON',
    Object.prototype.toString.call(rawStored.bytes));
assert(rawStored.bytes.byteLength === png.length, 'with exactly the asset\'s length');

const got = await store.getBytes(pngId);
assert(got.ok === true && got.source === 'indexeddb' && got.byteLength === png.length,
    'getBytes() returns the same bytes from storage', JSON.stringify({ ok: got.ok, source: got.source }));
assert(sameBytes(got.bytes, png), 'byte for byte');
assert(got.sha256 === core.sha256Hex(png) && got.mime === 'image/png', 'with the digest and type it was stored under');
assert(got.verified === null, 'and no verification claim when none was asked for');

// The copy is deliberate: a caller cannot reach back into the store.
const scratch = await store.getBytes(pngId);
scratch.bytes[0] = 0x00;
scratch.bytes[1] = 0x00;
const afterScratch = await store.getBytes(pngId);
assert(sameBytes(afterScratch.bytes, png),
    'mutating what getBytes() returned does not change what is stored (no aliasing)');
// ...and the same in the other direction: rewriting the buffer the caller
// handed over must not rewrite the asset (a real IndexedDB clones, but this
// store must not depend on the storage engine for its own integrity).
const handRig = db();
const handedOver = asciiBytes('caller buffer probe bytes');
const handedPristine = handedOver.slice();
const handedId = assetIdOf(handedOver);
const handStore = AS.create({ storage: handRig.storage(), urls: null });
await handStore.putBytes(handedId, handedOver, { mime: 'image/png' });
handedOver[0] = 0x00;
handedOver[1] = 0x00;
const afterHand = await handStore.getBytes(handedId, { verify: true });
assert(afterHand.ok === true && sameBytes(afterHand.bytes, handedPristine),
    'and the buffer the caller handed over is copied, so a later write into it cannot change the asset',
    JSON.stringify({ ok: afterHand.ok, reason: afterHand.reason }));
const verified = await store.getBytes(pngId, { verify: true });
assert(verified.ok === true && verified.verified === true,
    'getBytes(verify:true) proves the bytes still hash to the id they are stored under');

// Every reasonable way of handing over bytes.
const forms = [
    ['a Uint8Array', png],
    ['an ArrayBuffer', png.slice().buffer],
    ['a plain Array of octets', Array.from(png)],
    ['a view with an offset', (() => {
        const padded = new Uint8Array(png.length + 8);
        padded.set(png, 4);
        return new Uint8Array(padded.buffer, 4, png.length);
    })()],
];
for (const [label, value] of forms) {
    const viaForm = AS.create({ storage: b.storage(), urls: null });
    const r = await viaForm.putBytes(pngId, value);
    assert(r.ok === true && (await viaForm.getBytes(pngId)).byteLength === png.length,
        'bytes as ' + label + ' are accepted and stored intact');
}
const offsetPut = AS.create({ storage: b.storage(), urls: null });
const padded = new Uint8Array(png.length + 8);
padded.set(png, 4);
await offsetPut.putBytes(assetIdOf(new Uint8Array(padded.buffer, 4, png.length)),
    new Uint8Array(padded.buffer, 4, png.length));
assert(sameBytes((await offsetPut.getBytes(assetIdOf(png))).bytes, png),
    'a view is read as its OWN window (byteOffset respected, no pool leak)');

// A second, different file.
const gif = bytesOf([0x47, 0x49, 0x46, 0x38, 0x39, 0x61, 0x01, 0x00, 0x01, 0x00]);
const gifId = assetIdOf(gif);
assert(gifId !== pngId, 'two different files have two different ids');
await store.putBytes(gifId, gif, { mime: 'image/gif' });
assert(eq(keysOf(b), sorted([pngId, gifId])), 'and both are stored, side by side', JSON.stringify(keysOf(b)));

const badMime = await store.putBytes(assetIdOf(asciiBytes('mime-probe')), asciiBytes('mime-probe'), { mime: 'not a mime' });
assert(badMime.ok === true && badMime.mime === '',
    'a nonsense content type is stored as "unknown" rather than passed through', badMime.mime);

// ── C. reload ────────────────────────────────────────────────────
section('C. reload: the bytes are still there for the same database');
const cUrls = fakeUrls();
const storeA = AS.create({ storage: b.storage(), urls: bUrls.factory });
const urlBefore = await storeA.urlFor(pngId);
assert(urlBefore.ok === true && storeA.outstanding().indexOf(pngId) !== -1,
    'a live URL is held before the "reload"');
storeA.destroy();
assert(eq(bUrls.seen.revoked, [urlBefore.url]), 'tearing the old session down revoked it');

const storeB = AS.create({ storage: b.storage(), urls: cUrls.factory });
const afterReload = await storeB.getBytes(pngId);
assert(afterReload.ok === true && afterReload.source === 'indexeddb' && sameBytes(afterReload.bytes, png),
    'a new connection and a new instance find the same bytes');
const surveyAfterReload = await storeB.survey([pngId, gifId]);
assert(eq(surveyAfterReload.map(r => r.availability), ['bytes-local', 'bytes-local']),
    'and both assets report as present', JSON.stringify(surveyAfterReload.map(r => r.reason)));
const urlAfter = await storeB.urlFor(pngId);
assert(urlAfter.ok === true && urlAfter.url !== urlBefore.url && cUrls.seen.created.length === 1,
    'with a freshly minted URL (the revoked one is never handed out again)', urlAfter.url);

const otherProfile = db();
const freshStore = AS.create({ storage: otherProfile.storage(), urls: fakeUrls().factory });
const gone = await freshStore.getBytes(pngId);
assert(gone.ok === false && gone.reason === 'missing',
    'a different browser profile has no such asset, and says exactly that', gone.reason);
assert(eq((await freshStore.survey([pngId]))[0].availability, 'bytes-missing'),
    'survey() reports it as missing bytes rather than pretending');

// ── D. identity is checked, not trusted ──────────────────────────
section('D. bytes whose id does not match them are refused');
const d = db();
const dUrls = fakeUrls();
const dStore = AS.create({ storage: d.storage(), urls: dUrls.factory });
const realBytes = asciiBytes('the real asset');
const realId = assetIdOf(realBytes);
const wrongId = assetIdOf(asciiBytes('something else entirely'));
const mismatch = await dStore.putBytes(wrongId, realBytes);
assert(mismatch.ok === false && mismatch.reason === 'id-mismatch',
    'a put whose id is not those bytes\' id is refused', mismatch.reason);
assert(mismatch.expected === realId && mismatch.actual === wrongId,
    'and the refusal names both the id the bytes have and the id it was given');
assert(eq(keysOf(d), []), 'nothing was written under the bad id');
assert((await dStore.getBytes(wrongId)).ok === false, 'and the bad id holds no bytes afterwards');
assert((await dStore.urlFor(wrongId)).ok === false, 'so no URL can be minted for it');

// A stored entry that is NOT these bytes (same key, same length, another
// digest) must be replaced — "the length matches" is not identity.
const impostorRig = db();
const sameLength = asciiBytes('the right bytes!');
const impostorId = assetIdOf(sameLength);
const impostorBytes = asciiBytes('the wrong bytes!');
assert(sameLength.length === impostorBytes.length, 'rig: the impostor and the real asset are the same length',
    sameLength.length + ' vs ' + impostorBytes.length);
const impostor = {
    v: AS.ENTRY_VERSION, assetId: impostorId, sha256: core.sha256Hex(impostorBytes),
    byteLength: impostorBytes.length, mime: 'image/png', bytes: impostorBytes.slice().buffer,
};
await impostorRig.storage().put(impostorId, impostor);
await new Promise(r => setTimeout(r, 20));
const impostorStore = AS.create({ storage: impostorRig.storage(), urls: null });
const impostorPut = await impostorStore.putBytes(impostorId, sameLength, { mime: 'image/png' });
const repaired = await impostorStore.getBytes(impostorId, { verify: true });
assert(impostorPut.duplicate === false,
    'an entry holding other bytes of the same length is NOT this asset', JSON.stringify(impostorPut.duplicate));
assert(repaired.ok === true && sameBytes(repaired.bytes, sameLength),
    'so it is replaced by the real bytes rather than kept as a "duplicate"');
assert(eq(keysOf(impostorRig), [impostorId]), 'one key, the right asset');

const noBytes = await dStore.putBytes(realId, new Uint8Array(0));
assert(noBytes.ok === false && noBytes.reason === 'no-bytes', 'an empty file is refused by name', noBytes.reason);
assert((await dStore.putBytes(realId, null)).reason === 'no-bytes', 'so is a missing value');
assert((await dStore.putBytes(realId, 'not bytes')).reason === 'no-bytes',
    'and a string is never coerced into bytes');
assert((await dStore.putBytes('', realBytes)).reason === 'id-missing', 'a put with no id is refused');
assert((await dStore.putBytes(null, realBytes)).reason === 'id-missing', 'so is a non-string id');
assert((await dStore.getBytes('')).reason === 'id-missing', 'a get with no id is refused too');
assert((await dStore.putBytes(realId, realBytes)).ok === true, 'and the honest put still works afterwards');
assert((await dStore.getBytes(realId)).ok === true, 'with its bytes retrievable');
assert((await dStore.putBytes(realId, asciiBytes('different content'))).reason === 'id-mismatch',
    'replacing an asset\'s bytes under its old id is refused, so a stale URL can never point at new bytes');

// ── E. one file, four slots: one record, one write, one URL ──────
section('E. dedupe: the same file picked four times is one asset');
const e = db();
const eUrls = fakeUrls();
const eStore = AS.create({ storage: e.storage(), urls: eUrls.factory });
const fourId = assetIdOf(png);
const first = await eStore.putBytes(fourId, png, { mime: 'image/png' });
const second = await eStore.putBytes(fourId, png, { mime: 'image/png' });
assert(first.duplicate === false && second.duplicate === true,
    'the second arrival of the same bytes is recognised as a duplicate');
assert(second.persisted === true && second.source === 'indexeddb', 'and points at the stored copy, not a new one');
assert(eStore.stats().writes === 1, 'exactly ONE write reached storage for four picks',
    String(eStore.stats().writes));
assert(eq(keysOf(e), [fourId]), 'and one record exists, not four');
const beforeFour = eUrls.seen.created.length;
const fourUrls = [];
for (let slot = 0; slot < 4; slot++) fourUrls.push(await eStore.urlFor(fourId));
assert(eq(sorted(fourUrls.map(r => r.url)), [fourUrls[0].url, fourUrls[0].url, fourUrls[0].url, fourUrls[0].url].sort()),
    'four slots resolving the same asset share ONE URL');
assert(eUrls.seen.created.length - beforeFour === 1, 'and only one URL was ever minted',
    String(eUrls.seen.created.length - beforeFour));
assert(fourUrls[3].cached === true && fourUrls[3].minted === false, 'a repeat lookup mints nothing');
assert(eStore.outstanding().length === 1, 'one live URL, not four');
assert((await eStore.survey([fourId, fourId, fourId])).length === 1,
    'survey() answers once per asset, not once per reference');
const dupMime = await eStore.putBytes(fourId, png, { mime: 'image/webp' });
assert(eUrls.seen.details[0].mime === 'image/png' && eUrls.seen.details[0].length === png.length,
    'the minted URL was typed with the stored content type and given the whole asset',
    JSON.stringify(eUrls.seen.details[0]));
assert(eUrls.seen.details[0].type === '[object Uint8Array]',
    'and the factory receives its own Uint8Array copy (not the stored buffer)',
    eUrls.seen.details[0].type);
assert(dupMime.duplicate === true && dupMime.mime === 'image/png' && dupMime.mimeChanged === true,
    'a duplicate put reports the type the STORE holds, and says the request asked for another',
    JSON.stringify({ mime: dupMime.mime, changed: dupMime.mimeChanged }));
assert(eq(eUrls.seen.revoked, []), 'and a duplicate put revokes nothing');
assert((await eStore.getBytes(fourId)).source === 'indexeddb',
    'a duplicate probe reads the stored copy back (it is evidence, not an assumption)');

// ── F. missing, corrupt, unreadable ──────────────────────────────
section('F. missing and corrupt entries are reported, never thrown, never served');
const f = db();
const fUrls = fakeUrls();
const fStore = AS.create({ storage: f.storage(), urls: fUrls.factory });
const absent = await fStore.getBytes('a_0000000000000000');
assert(absent.ok === false && absent.reason === 'missing', 'an unknown asset is missing', absent.reason);
const mintsBefore = fUrls.seen.created.length;
assert((await fStore.urlFor('a_0000000000000000')).reason === 'missing', 'and has no URL to mint');
assert(fUrls.seen.created.length === mintsBefore, 'so nothing was created for it (no broken-image URL)');
assert(eq((await fStore.survey(['a_0000000000000000']))[0].availability, 'bytes-missing'),
    'survey() says bytes-missing for it');

// Everything a corrupted store could hand back.
const goodId = assetIdOf(png);
const goodGifId = assetIdOf(gif);
const garbage = {};
garbage[goodId] = 'not even an object';
garbage[goodGifId] = { v: AS.ENTRY_VERSION, assetId: goodGifId, sha256: core.sha256Hex(gif), byteLength: 5, bytes: bytesOf([1, 2, 3]).buffer };
const truncId = assetIdOf(asciiBytes('truncated entry'));
garbage[truncId] = { v: 999, assetId: truncId, sha256: core.sha256Hex(asciiBytes('truncated entry')), byteLength: 15, bytes: asciiBytes('truncated entry').buffer };
const foreignId = assetIdOf(asciiBytes('foreign key'));
garbage[foreignId] = { v: AS.ENTRY_VERSION, assetId: 'a_deadbeefdeadbeef', sha256: core.sha256Hex(asciiBytes('foreign key')), byteLength: 11, bytes: asciiBytes('foreign key').buffer };
const badShaId = assetIdOf(asciiBytes('bad digest'));
garbage[badShaId] = { v: AS.ENTRY_VERSION, assetId: badShaId, sha256: 'nope', byteLength: 10, bytes: asciiBytes('bad digest').buffer };
const stringBytesId = assetIdOf(asciiBytes('string bytes'));
garbage[stringBytesId] = { v: AS.ENTRY_VERSION, assetId: stringBytesId, sha256: core.sha256Hex(asciiBytes('string bytes')), byteLength: 12, bytes: 'string bytes' };
const c = db();
const corruptStore = AS.create({ storage: c.storage(), urls: fakeUrls().factory });
// Seed the database directly: this is what a half-written or foreign record looks like.
const seedAdapter = c.storage();
Object.keys(garbage).forEach(key => { seedAdapter.put(key, garbage[key]); });
await new Promise(r => setTimeout(r, 30));

const corruptIds = [goodId, goodGifId, truncId, foreignId, badShaId, stringBytesId];
for (const id of corruptIds) {
    const out = await corruptStore.getBytes(id);
    assert(out.ok === false && out.reason === 'corrupt',
        'a corrupted entry is reported as corrupt, not served (' + JSON.stringify(garbage[id]).slice(0, 28) + ')',
        out.reason);
}
assert((await corruptStore.urlFor(goodId)).reason === 'corrupt',
    'and no URL is minted from an entry the store cannot trust');
assert(corruptStore.stats().corrupt >= corruptIds.length,
    'every corrupt read is counted, so silent corruption cannot pass unnoticed',
    String(corruptStore.stats().corrupt));
const corruptSurvey = await corruptStore.survey(corruptIds);
assert(eq(corruptSurvey.map(r => r.availability).filter(a => a === 'bytes-local'), []),
    'and none of them is reported as available to the document');

// A content-level corruption (right length, wrong bytes) is caught when asked.
const verifyId = assetIdOf(asciiBytes('verify me please'));
const vDb = db();
await AS.create({ storage: vDb.storage(), urls: null }).putBytes(verifyId, asciiBytes('verify me please'));
const tampered = {
    v: AS.ENTRY_VERSION, assetId: verifyId, sha256: core.sha256Hex(asciiBytes('verify me please')),
    byteLength: 16, mime: 'image/png', bytes: asciiBytes('verify me NOW!!!').buffer,
};
await vDb.storage().put(verifyId, tampered);
await new Promise(r => setTimeout(r, 20));
const verifyStore = AS.create({ storage: vDb.storage(), urls: fakeUrls().factory });
const unchecked = await verifyStore.getBytes(verifyId);
assert(unchecked.ok === true, 'an unverified read returns what is stored (the cheap path)');
const checked = await verifyStore.getBytes(verifyId, { verify: true });
assert(checked.ok === false && checked.reason === 'corrupt' && checked.actual !== checked.expected,
    'and a verified read refuses bytes that no longer hash to their id',
    JSON.stringify({ reason: checked.reason, expected: checked.expected, actual: checked.actual }));

// ── G. unavailable and degraded storage ──────────────────────────
section('G. unavailable and degraded storage: honest, and never a crash');
const noStorageUrls = fakeUrls();
const memoryStore = AS.create({ storage: null, urls: noStorageUrls.factory });
const memoryMode = memoryStore.mode();
assert(memoryMode.mode === 'memory' && memoryMode.persistent === false && memoryMode.reason === 'no-storage',
    'with no adapter at all the store reports memory-only, with a reason', JSON.stringify(memoryMode));
const memPng = asciiBytes('memory only asset');
const memId = assetIdOf(memPng);
const memPut = await memoryStore.putBytes(memId, memPng, { mime: 'image/png' });
assert(memPut.ok === true && memPut.persisted === false && memPut.source === 'memory',
    'bytes are still accepted for this session', JSON.stringify({ persisted: memPut.persisted, reason: memPut.reason }));
assert(memPut.reason === 'no-storage', 'and the result says they are NOT stored', memPut.reason);
const memGet = await memoryStore.getBytes(memId);
assert(memGet.ok === true && memGet.source === 'memory' && sameBytes(memGet.bytes, memPng),
    'and they are readable for the rest of the session');
const memUrl = await memoryStore.urlFor(memId);
assert(memUrl.ok === true && noStorageUrls.seen.created.length === 1,
    'so the preview can still show an asset it cannot persist');
assert((await memoryStore.survey([memId]))[0].availability === 'bytes-local',
    'survey() reports it as local to this session, not as gone');
assert((await memoryStore.remove(memId)).cleared === true, 'removing it clears the memory copy');
assert((await memoryStore.getBytes(memId)).ok === false, 'and then it is gone');
// Teardown must not leave the session's bytes behind: the store says how many
// it still holds, and the answer has to be none.
const memoryTeardown = AS.create({ storage: null, urls: fakeUrls().factory });
const memoryOnlyId = assetIdOf(asciiBytes('unpersisted asset that must not outlive the session'));
await memoryTeardown.putBytes(memoryOnlyId, asciiBytes('unpersisted asset that must not outlive the session'));
const tornDown = memoryTeardown.destroy();
assert(tornDown.dropped.indexOf(memoryOnlyId) !== -1,
    'destroy() reports the session-only asset it dropped', JSON.stringify(tornDown.dropped));
assert(tornDown.retained === 0,
    'and holds no byte entries afterwards (an unpersisted asset does not outlive its session)',
    String(tornDown.retained));
assert(memoryTeardown.destroy().retained === 0, 'a second teardown holds none either');

// The REAL adapter, with IndexedDB unavailable to the browser.
const noIdbRig = db();
const noIdbStorage = noIdbRig.storage({ indexedDB: null });
assert(noIdbStorage.isAvailable() === false && noIdbStorage.reason() === 'no-indexeddb',
    'the real adapter degrades with its own reason when the browser has no IndexedDB');
const noIdbStore = AS.create({ storage: noIdbStorage, urls: fakeUrls().factory });
const noIdbPut = await noIdbStore.putBytes(realId, realBytes);
assert(noIdbPut.ok === true && noIdbPut.persisted === false && noIdbPut.reason === 'no-indexeddb',
    'a put through it keeps the bytes in memory and reports the adapter\'s reason', noIdbPut.reason);
assert((await noIdbStore.getBytes(realId)).source === 'memory', 'and the session can still use them');
assert(noIdbStore.mode().available === false && noIdbStore.mode().persistent === false,
    'mode() reports not-persistent while that is true');
const freshOnSame = AS.create({ storage: noIdbRig.storage({ indexedDB: null }), urls: null });
assert((await freshOnSame.getBytes(realId)).ok === false,
    'and a new session (a reload) does not find them — they were never stored');

// The real adapter, blocked open (a browser that never answers).
const blockedRig = db({ blockOpen: true });
const blockedStore = AS.create({ storage: blockedRig.storage({ timeoutMs: 20 }), urls: fakeUrls().factory });
const blockedPut = await blockedStore.putBytes(realId, realBytes);
assert(blockedPut.ok === true && blockedPut.persisted === false,
    'a blocked open times out and the put degrades instead of hanging', JSON.stringify(blockedPut.reason));
assert(blockedPut.reason === 'open-timeout', 'with the timeout named', blockedPut.reason);
assert((await blockedStore.getBytes(realId)).ok === true, 'and the session keeps working from memory');
assert(blockedStore.mode().reason === 'open-timeout' && blockedStore.mode().mode === 'memory',
    'mode() reports the timeout rather than claiming persistence');

// An adapter that fails in every way a promise can fail.
function hostileStorage() {
    const calls = { get: 0, put: 0, remove: 0 };
    return {
        calls: calls,
        isAvailable: () => true,
        reason: () => null,
        get: () => { calls.get++; return Promise.reject(new Error('get exploded')); },
        put: () => { calls.put++; return Promise.reject(new Error('put exploded')); },
        remove: () => { calls.remove++; throw new Error('remove exploded'); },
    };
}
const hostile = hostileStorage();
const hostileStore = AS.create({ storage: hostile, urls: fakeUrls().factory });
const hostilePut = await hostileStore.putBytes(realId, realBytes);
assert(hostilePut.ok === true && hostilePut.persisted === false && hostilePut.source === 'memory',
    'a rejected write becomes a memory copy, not a crash', hostilePut.reason);
assert((await hostileStore.getBytes(realId)).ok === true,
    'a rejected read still finds the session copy');
const hostileRemove = await hostileStore.remove(realId);
assert(hostileRemove.ok === true && hostileRemove.persisted === false && hostileRemove.reason === 'delete-threw',
    'a throwing delete is reported, not propagated', hostileRemove.reason);
assert(hostileStore.stats().failures >= 3,
    'and every failure is counted for the session to report', String(hostileStore.stats().failures));

// A transient read failure that takes the adapter down with it: the answer
// must be "could not look", never "there is nothing there".
const transient = (() => {
    let up = true;
    return {
        isAvailable: () => up,
        reason: () => (up ? null : 'write-timeout'),
        get: () => { up = false; return Promise.resolve(null); },
        put: () => Promise.resolve({ ok: false, reason: 'write-timeout' }),
        remove: () => Promise.resolve({ ok: false, reason: 'write-timeout' }),
    };
})();
const transientStore = AS.create({ storage: transient, urls: fakeUrls().factory });
const transientRead = await transientStore.getBytes(realId);
assert(transientRead.ok === false && transientRead.reason === 'write-timeout',
    'a read that fails and degrades the adapter reports the failure, not a missing asset',
    transientRead.reason);
assert((await transientStore.survey([realId]))[0].reason === 'write-timeout',
    'and survey() keeps the distinction the caller needs', (await transientStore.survey([realId]))[0].reason);
assert((await transientStore.urlFor(realId)).ok === false,
    'so no URL is minted from a store that could not look');

// A read-only adapter: storage exists, writes are refused by design.
const readOnlyStore = AS.create({
    storage: { isAvailable: () => true, reason: () => null, get: () => Promise.resolve(null),
        put: () => Promise.resolve({ ok: false, reason: 'read-only' }), remove: () => Promise.resolve({ ok: false, reason: 'read-only' }) },
    urls: null,
});
const readOnlyPut = await readOnlyStore.putBytes(realId, realBytes);
assert(readOnlyPut.ok === true && readOnlyPut.persisted === false && readOnlyPut.reason === 'read-only',
    'a read-only adapter yields a session-only asset with an honest reason', readOnlyPut.reason);

// An object that is not an adapter at all.
const junkStore = AS.create({ storage: {}, urls: null });
assert(junkStore.mode().mode === 'memory' && (await junkStore.putBytes(realId, realBytes)).ok === true,
    'a junk storage object is treated as no storage, not as a broken store');

// Recovery: the adapter comes back, and the bytes move to storage.
const recovering = (() => {
    const memory = {};
    let up = false;
    return {
        open: () => { up = true; },
        isAvailable: () => up,
        reason: () => (up ? null : 'write-timeout'),
        get: (key) => Promise.resolve(up && memory[key] ? memory[key] : null),
        put: (key, value) => { if (!up) return Promise.resolve({ ok: false, reason: 'write-timeout' }); memory[key] = value; return Promise.resolve({ ok: true }); },
        remove: (key) => { delete memory[key]; return Promise.resolve({ ok: true }); },
    };
})();
const recoveringStore = AS.create({ storage: recovering, urls: fakeUrls().factory });
const downPut = await recoveringStore.putBytes(realId, realBytes);
assert(downPut.persisted === false && downPut.reason === 'write-timeout',
    'while the adapter is down the bytes are session-only', downPut.reason);
recovering.open();
const upPut = await recoveringStore.putBytes(realId, realBytes);
assert(upPut.ok === true && upPut.persisted === true && upPut.source === 'indexeddb',
    'once it recovers, the next put lands in storage (availability is asked, never assumed)',
    JSON.stringify({ persisted: upPut.persisted, duplicate: upPut.duplicate }));
assert(upPut.duplicate === true && upPut.promoted === true,
    'the session copy is promoted rather than reported as "already saved"',
    JSON.stringify({ duplicate: upPut.duplicate, promoted: upPut.promoted }));
const freshLook = AS.create({ storage: recovering, urls: null });
assert((await freshLook.getBytes(realId)).ok === true,
    'and the bytes are then visible to a different instance (the memory copy was released)');

// ── H. the object-URL lifecycle ──────────────────────────────────
section('H. object URLs: stable while unchanged, revoked on time, never fabricated');
const h = db();
const hUrls = fakeUrls();
const hStore = AS.create({ storage: h.storage(), urls: hUrls.factory });
const hPng = pngBytes();
const hId = assetIdOf(hPng);
await hStore.putBytes(hId, hPng, { mime: 'image/png' });

const firstUrl = await hStore.urlFor(hId);
assert(firstUrl.ok === true && firstUrl.minted === true && firstUrl.cached === false,
    'the first lookup mints a URL', JSON.stringify(firstUrl));
assert(firstUrl.mime === 'image/png', 'typed with the asset\'s content type');
const repeat = [];
for (let i = 0; i < 20; i++) repeat.push(await hStore.urlFor(hId));
assert(eq(repeat.map(r => r.url).filter(u => u !== firstUrl.url), []),
    'twenty more lookups return the SAME URL');
assert(hStore.stats().mints === 1, 'and minted exactly one URL in total', String(hStore.stats().mints));
assert(repeat[0].cached === true && hStore.stats().urlHits === 20,
    'each of them is served from the cache');
// The factory was handed a copy: a factory that scribbles on what it was given
// must not be able to change the asset.
hUrls.seen.details[0].bytes[0] = 0x00;
hUrls.seen.details[0].bytes[1] = 0x00;
assert(sameBytes((await hStore.getBytes(hId)).bytes, hPng),
    'the bytes handed to the URL factory are its own copy (a scribbling factory cannot corrupt the asset)');
assert(hStore.cachedUrl(hId) === firstUrl.url,
    'cachedUrl() is the synchronous render-path read, and it returns it');
assert(eq(hStore.outstanding(), [hId]), 'the URL is outstanding until something releases it');
assert(hStore.cachedUrl('a_0000000000000000') === null, 'and cachedUrl() answers null for an unresolved asset');

// A different content type is a different blob.
const retyped = await hStore.urlFor(hId, { mime: 'image/jpeg' });
assert(retyped.minted === true && retyped.url !== firstUrl.url,
    'asking for a different content type mints a new URL', retyped.url);
assert(eq(hUrls.seen.revoked, [firstUrl.url]),
    'and revokes the old one (it described a different blob)');
assert(hUrls.seen.created.length === 2, 'two mints, two created URLs');
const stableAgain = await hStore.urlFor(hId, { mime: 'image/jpeg' });
assert(stableAgain.url === retyped.url && hStore.stats().mints === 2,
    'and the new type is then stable too');

// Release keeps the bytes.
const released = hStore.release(hId);
assert(released.revoked === true && hStore.cachedUrl(hId) === null && eq(hStore.outstanding(), []),
    'release() drops the URL and leaves nothing outstanding');
assert(eq(hUrls.seen.revoked, [firstUrl.url, retyped.url]), 'revoking both URLs it had minted');
assert((await hStore.getBytes(hId)).ok === true, 'and the bytes are still there');
const remint = await hStore.urlFor(hId);
assert(remint.ok === true && remint.minted === true && remint.url !== retyped.url,
    'a later lookup mints a NEW URL rather than resurrecting a revoked one', remint.url);
assert(hStore.release('a_0000000000000000').revoked === false, 'releasing an asset with no URL is a no-op, not an error');

// remove: URL and bytes.
const removed = await hStore.remove(hId);
assert(removed.ok === true && removed.revoked === true && removed.persisted === true && removed.cleared === true,
    'remove() deletes the bytes and revokes the URL', JSON.stringify(removed));
assert(eq(hStore.outstanding(), []) && hUrls.seen.revoked.indexOf(remint.url) !== -1,
    'nothing is left outstanding and the last URL was revoked');
assert(eq(keysOf(h), []), 'the stored record is gone');

// releaseAll + destroy: the teardown path.
const multi = db();
const multiUrls = fakeUrls();
const multiStore = AS.create({ storage: multi.storage(), urls: multiUrls.factory });
const ids = [];
for (let i = 0; i < 4; i++) {
    const bytes = asciiBytes('teardown asset ' + i);
    const id = assetIdOf(bytes);
    ids.push(id);
    await multiStore.putBytes(id, bytes, { mime: 'image/png' });
    await multiStore.urlFor(id);
}
assert(multiUrls.seen.created.length === 4 && multiStore.outstanding().length === 4,
    'four assets, four live URLs');
const all = multiStore.releaseAll();
assert(all.revoked === 4 && eq(all.assetIds, sorted(ids)), 'releaseAll() revokes every outstanding URL');
assert(eq(multiUrls.seen.revoked.slice().sort(), multiUrls.seen.created.slice().sort()),
    'and every URL it ever minted has now been revoked',
    'created ' + JSON.stringify(multiUrls.seen.created) + ' revoked ' + JSON.stringify(multiUrls.seen.revoked));
assert(eq(multiStore.outstanding(), []), 'nothing is outstanding afterwards');
assert((await multiStore.getBytes(ids[0])).ok === true, 'the bytes survive a release (only the URL went)');

const lateUrl = await multiStore.urlFor(ids[1]);
const destroyed = multiStore.destroy();
assert(destroyed.ok === true && destroyed.revoked === 1 && eq(destroyed.assetIds, [ids[1]]),
    'destroy() revokes what was outstanding at that moment', JSON.stringify(destroyed));
assert(eq(multiUrls.seen.revoked[multiUrls.seen.revoked.length - 1], lateUrl.url),
    'including the last one');
assert(multi.fake.log.some(l => l.op === 'close'),
    'and it closes the adapter\'s connection', JSON.stringify(multi.fake.log.map(l => l.op).slice(-4)));
assert(multiStore.mode().dead === true && multiStore.mode().reason === 'destroyed',
    'a destroyed store says so');
const deadCalls = {
    put: await multiStore.putBytes(ids[0], asciiBytes('after teardown')),
    get: await multiStore.getBytes(ids[0]),
    url: await multiStore.urlFor(ids[0]),
    survey: await multiStore.survey([ids[0]]),
};
assert(deadCalls.put.reason === 'destroyed' && deadCalls.get.reason === 'destroyed' && deadCalls.url.reason === 'destroyed',
    'and refuses to work at all rather than half-working',
    JSON.stringify([deadCalls.put.reason, deadCalls.get.reason, deadCalls.url.reason]));
assert(deadCalls.survey[0].availability === 'bytes-missing' && deadCalls.survey[0].reason === 'destroyed',
    'its survey reports nothing as available');
assert(multiStore.releaseAll().revoked === 0 && multiStore.outstanding().length === 0,
    'teardown is idempotent: a second destroy/release finds nothing to do');
assert(multiStore.destroy().revoked === 0, 'and destroy() can be called twice without throwing');
assert(multiStore.destroy().retained === 0,
    'and a store whose bytes are all in storage holds nothing in memory after teardown');

// No URL factory at all: the storage core still works (no DOM dependency),
// and the URL answer is an honest refusal.
const bare = db();
const bareStore = AS.create({ storage: bare.storage(), urls: null });
const bareId = assetIdOf(png);
assert((await bareStore.putBytes(bareId, png, { mime: 'image/png' })).persisted === true,
    'a store with no URL factory still stores bytes');
const bareUrl = await bareStore.urlFor(bareId);
assert(bareUrl.ok === false && bareUrl.reason === 'urls-unavailable' && bareStore.cachedUrl(bareId) === null,
    'and reports urls-unavailable for the URL instead of inventing one', bareUrl.reason);
assert(eq(bareStore.outstanding(), []), 'so nothing is outstanding');
assert((await bareStore.getBytes(bareId)).ok === true, 'and the bytes are readable');

// A URL factory that misbehaves.
const badUrls = fakeUrls();
badUrls.seen.throwOnCreate = true;
const badStore = AS.create({ storage: bare.storage(), urls: badUrls.factory });
const threw = await badStore.urlFor(bareId);
assert(threw.ok === false && threw.reason === 'url-create-threw' && badStore.cachedUrl(bareId) === null,
    'a factory that throws yields a refusal, not a crash', threw.reason);
badUrls.seen.throwOnCreate = false;
badUrls.seen.empty = true;
const emptyUrl = await badStore.urlFor(bareId);
assert(emptyUrl.ok === false && emptyUrl.reason === 'url-empty' && badStore.cachedUrl(bareId) === null,
    'a factory that returns nothing is treated as no URL', emptyUrl.reason);
badUrls.seen.empty = false;
await badStore.urlFor(bareId);
badUrls.seen.throwOnRevoke = true;
const stuck = badStore.release(bareId);
assert(stuck.revoked === true && badStore.cachedUrl(bareId) === null,
    'a revoke that throws still clears the cache (a URL we cannot revoke is never handed out again)');
assert(badStore.stats().failures >= 2, 'and the failures are counted', String(badStore.stats().failures));

// Replacement: a slot whose bytes change gets a new asset, and the old URL dies with the old one.
const replaced = db();
const replacedUrls = fakeUrls();
const replacedStore = AS.create({ storage: replaced.storage(), urls: replacedUrls.factory });
const oldBytes = asciiBytes('first version of the picture');
const newBytes = asciiBytes('second version of the picture!');
const oldId = assetIdOf(oldBytes);
const newId = assetIdOf(newBytes);
await replacedStore.putBytes(oldId, oldBytes, { mime: 'image/png' });
const oldUrl = (await replacedStore.urlFor(oldId)).url;
await replacedStore.putBytes(newId, newBytes, { mime: 'image/png' });
const newUrl = (await replacedStore.urlFor(newId)).url;
assert(oldId !== newId, 'replacing the bytes produces a different asset id');
assert(newUrl !== oldUrl, 'and a different URL', JSON.stringify({ oldUrl: oldUrl, newUrl: newUrl }));
assert(eq(replacedUrls.seen.revoked, []), 'the old URL is still live while its asset is still referenced');
assert((await replacedStore.remove(oldId)).revoked === true, 'removing the replaced asset revokes its URL');
assert(eq(replacedUrls.seen.revoked, [oldUrl]), 'exactly once, and only the old one');
assert(replacedStore.cachedUrl(newId) === newUrl, 'while the new asset keeps its URL');

// ── I. survey() ──────────────────────────────────────────────────
section('I. survey(): the id → availability report');
const i1 = db();
const iUrls = fakeUrls();
const iStore = AS.create({ storage: i1.storage(), urls: iUrls.factory });
const present = asciiBytes('present asset');
const memoryOnly = asciiBytes('memory asset');
const presentId = assetIdOf(present);
const memoryId = assetIdOf(memoryOnly);
await iStore.putBytes(presentId, present, { mime: 'image/png' });
const memStore2 = AS.create({ storage: null, urls: null });
await memStore2.putBytes(memoryId, memoryOnly, { mime: 'image/gif' });
const rows = await iStore.survey([presentId, memoryId, '', null, 'a_ffffffffffffffff', presentId]);
assert(rows.length === 3, 'survey() answers once per distinct id, ignoring junk', String(rows.length));
assert(rows[0].assetId === presentId && rows[0].availability === 'bytes-local' && rows[0].source === 'indexeddb',
    'a stored asset is bytes-local from indexeddb', JSON.stringify(rows[0]));
assert(rows[0].byteLength === present.length && rows[0].sha256 === core.sha256Hex(present) && rows[0].mime === 'image/png',
    'with the facts the document needs to stay honest about it');
assert(rows[1].availability === 'bytes-missing' && rows[1].reason === 'missing',
    'a different store\'s asset is missing here (stores do not share memory)', JSON.stringify(rows[1]));
assert(rows[2].availability === 'bytes-missing' && rows[2].reason === 'missing',
    'an unknown id is missing', JSON.stringify(rows[2]));
assert(eq(await iStore.survey([]), []) && eq(await iStore.survey(null), []),
    'an empty or absent list is an empty report');
assert(eq(sorted((await iStore.survey([presentId, memoryId])).map(r => r.availability)), ['bytes-local', 'bytes-missing']),
    'the availability vocabulary stays exactly bytes-local / bytes-missing');
assert(eq(await iStore.survey([memoryId]), [await iStore.survey([memoryId])][0]),
    'survey() is deterministic across calls');
const memRow = (await memStore2.survey([memoryId]))[0];
assert(memRow.availability === 'bytes-local' && memRow.source === 'memory',
    'a session-only asset is local (and says it is only in memory)', JSON.stringify(memRow));
const degradedRow = (await AS.create({ storage: { isAvailable: () => false, reason: () => 'write-timeout', get: () => Promise.resolve(null) }, urls: null })
    .survey([presentId]))[0];
assert(degradedRow.availability === 'bytes-missing' && degradedRow.reason === 'write-timeout',
    'an unreachable store reports missing bytes WITH the reason it could not look', JSON.stringify(degradedRow));

// ── J. isolation, determinism, and the document boundary ─────────
section('J. isolation, determinism, and no way into the document');
const j1 = db(), j2 = db();
const j1Urls = fakeUrls(), j2Urls = fakeUrls();
const j1Store = AS.create({ storage: j1.storage(), urls: j1Urls.factory });
const j2Store = AS.create({ storage: j2.storage(), urls: j2Urls.factory });
const jBytes = asciiBytes('isolation probe');
const jId = assetIdOf(jBytes);
await j1Store.putBytes(jId, jBytes, { mime: 'image/png' });
assert((await j2Store.getBytes(jId)).ok === false, 'two stores on two databases do not see each other\'s bytes');
assert(eq(j2Urls.seen.created, []), 'and one store\'s URLs are not the other\'s');

// Same database, two instances: still separate URL lifecycles.
const shared = db();
const sharedUrlsA = fakeUrls(), sharedUrlsB = fakeUrls();
const storeA2 = AS.create({ storage: shared.storage(), urls: sharedUrlsA.factory });
const storeB2 = AS.create({ storage: shared.storage(), urls: sharedUrlsB.factory });
const sBytes = asciiBytes('shared storage asset');
const sId = assetIdOf(sBytes);
await storeA2.putBytes(sId, sBytes, { mime: 'image/png' });
const aUrl = await storeA2.urlFor(sId);
const bUrl = await storeB2.urlFor(sId);
assert((await storeB2.getBytes(sId)).ok === true, 'a second instance on the same database finds the same bytes');
assert(aUrl.url !== bUrl.url && sharedUrlsA.seen.created.length === 1 && sharedUrlsB.seen.created.length === 1,
    'and mints its own URL (URL caches are per instance)');
storeA2.destroy();
assert(eq(sharedUrlsA.seen.revoked, [aUrl.url]) && eq(sharedUrlsB.seen.revoked, []),
    'destroying one instance revokes only its own URLs');
assert(storeB2.cachedUrl(sId) === bUrl.url && (await storeB2.getBytes(sId)).ok === true,
    'and leaves the other instance fully working');

// Two independent realms: identical inputs, identical answers.
function realmRun() {
    const rWin = loadRealm();
    const rStore = rWin.NERO.embed.assetStore;
    const rCore = rWin.NERO.embed.assets;
    const rRig = createFakeIdb({});
    const rUrls = [];
    const rSeen = { created: [], revoked: [] };
    const rInstance = rStore.create({
        storage: rStore.bytesStorage({ indexedDB: rRig, scheduler: scheduler, timeoutMs: 60 }),
        urls: {
            create: (bytes, mime) => { rSeen.created.push(mime + ':' + bytes.length); return 'blob:realm/' + rSeen.created.length; },
            revoke: (url) => rSeen.revoked.push(url),
        },
    });
    const rBytes = asciiBytes('realm probe bytes');
    const rId = rCore.assetIdFromSha(rCore.sha256Hex(rBytes));
    return rInstance.putBytes(rId, rBytes, { mime: 'image/png' })
        .then(() => rInstance.getBytes(rId))
        .then((got) => rInstance.urlFor(rId).then((url) => rInstance.survey([rId]).then((rows) => JSON.stringify({
            id: rId, byteLength: got.byteLength, sha256: got.sha256, source: got.source,
            url: url.url, minted: url.minted, rows: rows, seen: rSeen, storeNames: rRig.storeNames(rWin.NERO.embed.drafts.V2_NAMESPACE),
        }))));
}
const runOne = await realmRun();
const runTwo = await realmRun();
assert(runOne === runTwo, 'two independent realms produce byte-identical results (no shared state)',
    runOne === runTwo ? '' : runOne + ' vs ' + runTwo);
assert(runOne.indexOf('"storeNames":["assets","drafts","meta"]') !== -1,
    'and both create the same v2 database shape', runOne);

// The store's outputs are plain, and it never mutates what it is given.
const frozenDoc = Object.freeze({ assets: Object.freeze({}) });
const given = [presentId, memoryId];
const before = JSON.stringify(given);
await iStore.survey(given);
assert(JSON.stringify(given) === before, 'survey() does not mutate the list it is given');
const opts = Object.freeze({ mime: 'image/png' });
assert((await iStore.putBytes(presentId, present, opts)).ok === true,
    'putBytes() does not mutate its options (a frozen object is accepted)');
assert(JSON.stringify(present) === JSON.stringify(present), 'and the caller\'s bytes are the caller\'s');
const outA = iStore.outstanding();
outA.push('nonsense');
assert(eq(iStore.outstanding(), []), 'outstanding() returns a fresh list, not the store\'s own');
assert(JSON.stringify(frozenDoc) === '{"assets":{}}', 'an untouched document-shaped object stays untouched');

// The frozen document boundary still refuses anything byte-shaped.
let serialError = null;
try { drafts.assertSerializable({ assets: { a_1: { bytes: present } } }); } catch (e) { serialError = e; }
assert(!!serialError, 'drafts.assertSerializable() refuses a typed array inside a document',
    serialError && serialError.message);
let blobError = null;
const blobRealm = loadRealm({ Blob: class Blob {} });
try { blobRealm.NERO.embed.drafts.assertSerializable({ asset: new (blobRealm.window.Blob)() }); } catch (e) { blobError = e; }
assert(!!blobError && /Blob/.test(String(blobError.message)),
    'and refuses a Blob, so bytes cannot enter document state through the draft boundary',
    blobError && blobError.message);
assert(Object.keys(model.blankMessageDocument()).indexOf('assets') !== -1,
    'the document the model normalizes already carries an assets map (metadata only)',
    JSON.stringify(Object.keys(model.blankMessageDocument())));
assert(Object.keys(JSON.parse(JSON.stringify(model.blankMessageDocument()))).indexOf('assets') !== -1,
    'and the document is JSON-serialisable, so the byte store is the only place bytes can live');

// ── K. cost ──────────────────────────────────────────────────────
section('K. cost');
const big = new Uint8Array(1024 * 1024);
for (let i = 0; i < big.length; i += 4096) big[i] = i & 0xff;
const bigId = assetIdOf(big);
const cost = db();
const costStore = AS.create({ storage: cost.storage(), urls: fakeUrls().factory });
await costStore.putBytes(bigId, big, { mime: 'image/png' });
const putStarted = process.hrtime.bigint();
await costStore.putBytes(bigId, big, { mime: 'image/png' });   // the duplicate probe + hash
const readStarted = process.hrtime.bigint();
const bigGot = await costStore.getBytes(bigId);
const readMs = Number(process.hrtime.bigint() - readStarted) / 1e6;
const putMs = Number(readStarted - putStarted) / 1e6;
console.log('    1 MiB duplicate put (hash + probe): ' + putMs.toFixed(2) + ' ms; 1 MiB read: ' + readMs.toFixed(2) + ' ms');
assert(bigGot.byteLength === big.length && putMs < 250 && readMs < 250,
    'a 1 MiB asset is stored and read back well inside the add-an-image budget',
    putMs.toFixed(2) + ' / ' + readMs.toFixed(2) + ' ms');

await costStore.urlFor(bigId);
const urlStarted = process.hrtime.bigint();
for (let i = 0; i < 5000; i++) costStore.cachedUrl(bigId);
const urlUs = Number(process.hrtime.bigint() - urlStarted) / 1000 / 5000;
console.log('    cachedUrl() per call: ' + urlUs.toFixed(3) + ' µs');
assert(urlUs < 10, 'the render-path lookup is a map read, not work', urlUs.toFixed(3) + ' µs');

const manyIds = [];
for (let i = 0; i < 50; i++) manyIds.push(assetIdOf(asciiBytes('survey asset ' + i)));
const surveyStarted = process.hrtime.bigint();
await costStore.survey(manyIds);
const surveyMs = Number(process.hrtime.bigint() - surveyStarted) / 1e6;
console.log('    survey() over 50 missing ids: ' + surveyMs.toFixed(2) + ' ms');
assert(surveyMs < 250, 'surveying a whole document\'s asset ids stays cheap', surveyMs.toFixed(2) + ' ms');

// ═══════════════════════════════════════════════════════════════
console.log('\nmessage-builder asset store: ' + pass + ' passed, ' + fail + ' failed');
if (fail) {
    console.log('Failures:');
    failures.forEach(f => console.log(' -', f));
    process.exit(1);
}
console.log('ALL MESSAGE-BUILDER ASSET-STORE CHECKS PASSED');
process.exit(0);

})().catch(e => {
    console.error('\nHARNESS ERROR (not an assertion failure):', e && e.stack || e);
    process.exit(2);
});
