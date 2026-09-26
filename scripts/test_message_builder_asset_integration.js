#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════
   scripts/test_message_builder_asset_integration.js — step 7c.

   WHAT STEP 7c IS
   The asset METADATA layer and the page's coordination of it:
   `document.assets` (canonical record map), the references that point
   into it, the two store actions that edit it, the validator rules that
   describe it, and the ONE page behaviour that ties them to reality —
   asking the accepted byte store (step 7b) what it knows about EXACTLY
   the ids the document references, once per change of that set.

   WHAT THIS HARNESS PROVES
     A. The contract and the boundaries: the template loads the asset
        layer before its consumers; the page opens no storage, mints no
        URL, deletes no byte and owns exactly ONE issue-list writer; the
        byte store is untouched by 7c.
     B. The document contract: one canonical record shape, JSON-only
        admission, no aliasing, no history entry for a no-op, and a
        payload that still does not know what an asset is.
     C. References: a slot points at an id (never at bytes), one file can
        serve several slots, an unlinked slot is its own problem, and a
        URL slot stays a URL slot.
     D. The store: the two asset actions are ordinary document edits —
        undo/redo reach them, "no change" is not an edit, and
        historyDocuments() hands out private clones of what undo can
        reach (including the redo tail).
     E. Validation, metadata only: record-missing/unreadable, unlinked,
        unused, count, total size, unknown size, per-file advisory,
        filename clashes/changes, extension/mime/format — every number
        from the served table, every rule deterministic, existing issues
        unmoved.
     F. Validation with FACTS: available / missing / unavailable / corrupt /
        unknown are five different answers, the record-vs-row comparisons
        need no digest, and with no facts at all the availability rules
        stay SILENT.
     G. The page: one probe per change of the referenced id set, zero
        probes per keystroke, no byte written or deleted, and the strip
        painted from the store's ONE issue list.
     H. Reload/restore over the same fake database, plus the tamper cases
        (corrupt entry, wrong digest, wrong length, missing bytes).
     I. Degraded storage: "could not check" is never "not there", the
        session copy still counts, and teardown is clean.
     J. Retention: pure, deterministic, fail-closed, session-safe — and
        applied by nobody (no step may delete bytes here).
     K. Performance: keystrokes cost no probe and no storage read.
     L. Two realms agree (determinism), and a destroyed page stays dead.

   HOW IT RUNS
   One process, two kinds of rig: bare realms for the pure modules, and a
   mounted page (dom_stub + fake IndexedDB) for the coordination. Every
   module can be swapped through its source hook, so the mutation battery
   (scripts/support/mb_mutants.js) can point this harness at a broken copy
   and confirm these checks FAIL. A harness that cannot fail is evidence
   of nothing.
   ═══════════════════════════════════════════════════════════════ */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { createDom, createFakeIdb, createWindow, parseTemplate, findById, materialize } = require('./support/dom_stub.js');

const ROOT = path.join(__dirname, '..');
const js = (...parts) => path.join(ROOT, 'dashboard', 'static', 'js', ...parts);
const SOURCE = {
    model: process.env.NERO_MODEL_SRC || js('embed', 'model.js'),
    assets: process.env.NERO_ASSETS_SRC || js('embed', 'assets.js'),
    assetStore: process.env.NERO_ASSET_STORE_SRC || js('embed', 'asset-store.js'),
    store: process.env.NERO_STORE_SRC || js('embed', 'store.js'),
    validate: process.env.NERO_VALIDATE_SRC || js('embed', 'validate.js'),
    drafts: process.env.NERO_DRAFTS_SRC || js('embed', 'drafts.js'),
    page: process.env.NERO_MB_PAGE_SRC || js('embed', 'message-builder-page.js'),
};
const TEMPLATE_PATH = path.join(ROOT, 'dashboard', 'templates', 'manage', 'message_builder.html');
const LAYOUT_PATH = path.join(ROOT, 'scripts', 'test_message_builder_layout.js');
const TEMPLATE_SRC = fs.readFileSync(TEMPLATE_PATH, 'utf8');

const NS = 'nero_message_builder';
const GUILD = '1111222233334444';          // the guild the page shell renders (a string attribute)
const RECORD_NOW = '2026-09-26T00:00:00.000Z';

let pass = 0, fail = 0;
const failures = [];
function assert(cond, name, extra) {
    if (cond) { pass++; console.log('  PASS', name); }
    else {
        fail++;
        failures.push(name + (extra ? ' — ' + extra : ''));
        console.log('  FAIL', name, extra === undefined ? '' : extra);
    }
}
function section(title) { console.log('\n== ' + title + ' =='); }
function eq(a, b) { return JSON.stringify(a) === JSON.stringify(b); }
function codes(issues) { return (issues || []).map((i) => i.code); }
function find(issues, code) { return (issues || []).filter((i) => i.code === code); }
function rep(ch, n) { let out = ''; while (out.length < n) out += ch; return out; }
function readSource(file) { return fs.readFileSync(file, 'utf8'); }
function codeOnly(src) {
    // Line comments FIRST: the page contains a `//` comment with a `/*` inside
    // it ("the `ui/*` action namespace"), and stripping block comments first
    // would swallow every line up to the next `*/`.
    return String(src)
        .replace(/(^|[^:])\/\/[^\n]*/g, '$1 ')
        .replace(/\/\*[\s\S]*?\*\//g, ' ');
}
const PAGE_SRC = readSource(SOURCE.page);
const VALIDATE_SRC = readSource(SOURCE.validate);
const STORE_SRC = readSource(SOURCE.store);
const MODEL_SRC = readSource(SOURCE.model);
const ASSETS_SRC = readSource(SOURCE.assets);
const ASSET_STORE_SRC = readSource(SOURCE.assetStore);
const CODE = {
    page: codeOnly(PAGE_SRC),
    validate: codeOnly(VALIDATE_SRC),
    store: codeOnly(STORE_SRC),
    model: codeOnly(MODEL_SRC),
    assets: codeOnly(ASSETS_SRC),
    assetStore: codeOnly(ASSET_STORE_SRC),
};
/** The body of one named function (so a whole-file scan cannot be fooled by
 *  a comment, and cannot be widened by an unrelated new function). */
function functionSource(src, name) {
    const code = codeOnly(src);
    const start = code.indexOf('function ' + name + '(');
    if (start === -1) throw new Error('functionSource: no function ' + name);
    let depth = 0, seen = false;
    for (let i = code.indexOf('{', start); i < code.length; i++) {
        if (code[i] === '{') { depth++; seen = true; }
        else if (code[i] === '}') { depth--; if (seen && depth === 0) return code.slice(start, i + 1); }
    }
    throw new Error('functionSource: unbalanced braces in ' + name);
}

// ── the served limits (the shape utils/discord_limits.py renders) ──
function servedLimits(overrides) {
    const table = {
        message: {
            content_max: 2000, embeds_max: 10, embed_total_chars_max: 6000,
            request_bytes_max: 26214400,
        },
        attachments: {
            count_max: 10, total_bytes_max: 26148864,
            file_bytes_advisory: 20971520, file_advisory_is_hard: false,
        },
        embed: {
            title_max: 256, description_max: 4096, fields_max: 25, field_name_max: 256,
            field_value_max: 1024, footer_text_max: 2048, author_name_max: 256,
        },
        components: {
            rows_max: 5, buttons_per_row_max: 5, button_label_max: 80, button_url_max: 512,
            custom_id_max: 100, select_options_max: 25, select_option_label_max: 100,
            select_option_description_max: 100, select_placeholder_max: 150,
        },
    };
    if (overrides) {
        Object.keys(overrides).forEach((block) => {
            if (overrides[block] === null) delete table[block];
            else table[block] = Object.assign({}, table[block], overrides[block]);
        });
    }
    return table;
}
const LIMITS = servedLimits();

// ── byte fixtures (host side: the modules accept any realm's bytes) ──
function bytesFrom(list) { return new Uint8Array(list); }
function pngBytes(seed) {
    const body = [];
    for (let i = 0; i < 24; i++) body.push((String(seed).charCodeAt(i % String(seed).length) + i * 7) & 0xff);
    return bytesFrom([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a].concat(body));
}
function gifBytes(seed) {
    const body = [];
    for (let i = 0; i < 16; i++) body.push((String(seed).charCodeAt(i % String(seed).length) + i * 3) & 0xff);
    return bytesFrom([0x47, 0x49, 0x46, 0x38, 0x39, 0x61].concat(body));
}
function webpBytes(seed) {
    const body = [];
    for (let i = 0; i < 12; i++) body.push((String(seed).charCodeAt(i % String(seed).length) + i * 5) & 0xff);
    return bytesFrom([0x52, 0x49, 0x46, 0x46, 0x00, 0x00, 0x00, 0x00].concat([0x57, 0x45, 0x42, 0x50]).concat(body));
}
/** A stored-entry fixture for the fake IndexedDB (plain octets: JSON-safe). */
function entryFor(ident, value) {
    const octets = Array.prototype.slice.call(value);
    return { v: 1, assetId: ident.assetId, sha256: ident.sha256, byteLength: octets.length, mime: ident.mime, bytes: octets };
}

// ── realms ──
const REALM_GLOBALS = ['Promise', 'Object', 'Array', 'Math', 'Date', 'JSON', 'Number', 'String',
    'RegExp', 'Error', 'TypeError', 'Set', 'Map', 'Symbol', 'Uint8Array', 'ArrayBuffer',
    'isFinite', 'parseInt', 'parseFloat', 'encodeURIComponent', 'decodeURIComponent'];

/** A bare realm with `files` loaded in order (no DOM at all). */
function bareRealm(files) {
    const win = {};
    win.window = win;
    win.console = console;
    const sandbox = { window: win, console: console };
    REALM_GLOBALS.forEach((name) => { sandbox[name] = global[name]; });
    vm.createContext(sandbox);
    (files || []).forEach((file) => {
        vm.runInContext(readSource(file), sandbox, { filename: path.basename(file) });
    });
    return win;
}
function pureRealm() {
    return bareRealm([SOURCE.model, SOURCE.assets, SOURCE.store, SOURCE.validate]);
}
function validateRealm() {
    return bareRealm([SOURCE.model, SOURCE.assets, SOURCE.validate]);
}

// ── the page rig (dom_stub + fake IndexedDB) ──
const TEMPLATE_TREE = parseTemplate(TEMPLATE_SRC);
function moduleList() {
    const shell = path.join(ROOT, 'dashboard', 'static', 'js', 'nav-lifecycle.js');
    return [
        shell,
        SOURCE.model,
        SOURCE.assets,
        SOURCE.assetStore,
        SOURCE.store,
        SOURCE.validate,
        js('embed', 'discord-markdown.js'),
        js('embed', 'preview.js'),
        SOURCE.drafts,
        js('embed', 'views', 'statusbar.js'),
        js('embed', 'views', 'rail.js'),
        js('embed', 'views', 'inspector.js'),
        js('embed', 'views', 'actionbar.js'),
        SOURCE.page,
    ];
}

function makeEnv() {
    const env = { mounted: false };
    const dom = createDom();
    const win = createWindow();
    win.document = dom.document;
    win.__BOT_IDENTITY__ = { name: 'Nero', avatar: null };
    const net = { calls: 0 };
    const sandbox = {
        window: win,
        document: dom.document,
        console: console,
        setTimeout: setTimeout, clearTimeout: clearTimeout,
        setInterval: setInterval, clearInterval: clearInterval,
        Promise: Promise, Object: Object, Array: Array, Math: Math, Date: Date, JSON: JSON,
        Number: Number, String: String, RegExp: RegExp, Error: Error, TypeError: TypeError,
        Set: Set, Map: Map, Symbol: Symbol,
        isFinite: isFinite, parseInt: parseInt, parseFloat: parseFloat,
        encodeURIComponent: encodeURIComponent, decodeURIComponent: decodeURIComponent,
        fetch: function () { net.calls++; return Promise.reject(new Error('the page must not touch the network')); },
        navigator: { clipboard: { writeText: () => Promise.resolve() } },
    };
    vm.createContext(sandbox);
    moduleList().forEach((file) => {
        vm.runInContext(readSource(file), sandbox, { filename: path.basename(file) });
    });
    env.win = win;
    env.sandbox = sandbox;
    env.dom = dom;
    env.net = net;
    env.NERO = win.NERO;
    env.limits = servedLimits();
    env.idb = null;

    const bindIdb = (idb) => {
        env.idb = idb;
        sandbox.indexedDB = idb;
        win.indexedDB = idb;
        env.NERO.indexedDB = idb;
        return idb;
    };
    env.installIdb = (spec) => bindIdb(createFakeIdb(spec || {}));
    env.useIdb = bindIdb;
    env.notice = () => {
        const status = env.el('mb2-bar-status');
        if (!status) return '';
        const found = status.children.find((c) => (c.className || '').split(/\s+/).indexOf('mb2-status-notice') !== -1);
        return found ? found.textContent : '';
    };
    env.el = (id) => dom.document.getElementById(id);
    env.mount = async (opts) => {
        const options = opts || {};
        const root = materialize(findById(TEMPLATE_TREE, 'mb2-root'), dom.document);
        root.setAttribute('data-guild-id', String(GUILD));
        root.setAttribute('data-limits', JSON.stringify(options.limits || env.limits));
        dom.attach(root);
        env.root = root;
        await env.NERO.lifecycle.mount(dom.document);
        await env.settle(options.settleMs == null ? 25 : options.settleMs);
        env.inst = env.NERO.embed.messageBuilderPage.current();
        env.mounted = true;
        return env.inst;
    };
    env.unmount = () => env.NERO.lifecycle.unmount('test');
    env.store = () => env.inst.store;
    env.settle = (ms) => new Promise((resolve) => setTimeout(resolve, ms == null ? 20 : ms));
    env.until = async (cond, ms) => {
        const deadline = Date.now() + (ms || 1000);
        while (Date.now() < deadline) {
            if (cond()) return true;
            await env.settle(5);
        }
        return !!cond();
    };
    env.stats = () => env.inst.assetStore.stats();
    env.facts = () => env.inst.assetFacts;
    env.probes = () => env.inst.ctx.counters.assetProbes || 0;
    env.passes = () => env.inst.ctx.counters.validateRuns || 0;
    env.strip = () => env.el('mb2-strip');
    env.stripText = () => {
        const strip = env.strip();
        return strip ? strip.textContent : '';
    };
    env.issues = () => env.store().getUi().issues || [];
    env.draftKey = (documentId) => env.NERO.embed.drafts.draftKey(GUILD, documentId);
    env.entries = () => (env.idb ? env.idb.snapshot(NS) : null) || {};
    env.writeLog = () => (env.idb ? env.idb.writes : 0);
    return env;
}

// ── document fixtures ──
function blankDoc(overrides) {
    return Object.assign({
        schemaVersion: 2, id: 'doc-integration', guildId: String(GUILD), layout: 'legacy',
        content: '', assets: {},
        embeds: [{
            id: 'emb-1', title: '', url: '', description: '', color: 0x7c5cbf,
            author: { name: '', url: '', icon: null },
            footer: { text: '', icon: null },
            thumbnail: null, image: null, timestamp: '', fields: [],
        }],
    }, overrides || {});
}
function recordFor(A, ident, extra) {
    const built = A.buildRecord(Object.assign({
        assetId: ident.assetId, sha256: ident.sha256, mime: ident.mime,
        bytes: 2048, filename: ident.filename, availability: 'bytes-local',
        createdAt: RECORD_NOW,
    }, extra || {}));
    if (!built.ok || !built.record) throw new Error('record refused: ' + JSON.stringify(built && built.missing));
    return built.record;
}
function uploadRef(ident, extra) {
    return Object.assign({
        kind: 'upload', assetId: ident.assetId, filename: ident.filename,
        mime: ident.mime, bytes: 2048,
    }, extra || {});
}
function fixture(A) {
    const png = A.identify(pngBytes('one'), 'one.png');
    const gif = A.identify(gifBytes('two'), 'two.gif');
    return { png: png, gif: gif, pngFile: pngBytes('one'), gifFile: gifBytes('two') };
}
/** A document whose first embed's image is `ident`, with a proper record. */
function linkedDoc(A, M, ident, extra) {
    let doc = M.setDocumentAsset(blankDoc(), ident.assetId, recordFor(A, ident, extra));
    doc = M.setMedia(doc, 'emb-1', 'image', uploadRef(ident));
    return doc;
}
/** validate(doc, limits?, facts?) through the realm under test. */
function run(realm, doc, limits, facts) {
    return realm.NERO.embed.validate.validate(doc, limits || LIMITS, facts);
}
/** facts from raw store rows, through the module that owns the vocabulary. */
function factsOf(realm, doc, rows) {
    return realm.NERO.embed.assets.assetFacts(doc, rows);
}
function throwsWith(fn) {
    try { fn(); return null; } catch (err) { return err; }
}
/** The facts object a page would build from store rows (one entry point). */
function rowFacts(realm, doc, rows) {
    return realm.NERO.embed.assets.assetFacts(doc, rows);
}
function rowFor(ident, patch) {
    return Object.assign({
        assetId: ident.assetId, present: true, availability: 'bytes-local',
        source: 'indexeddb', byteLength: 2048, sha256: ident.sha256, mime: ident.mime, reason: null,
    }, patch || {});
}

// ═══════════════════════════════════════════════════════════════
section('A. the contract, the boundaries, and what the page must NOT reach for');
// ═══════════════════════════════════════════════════════════════
{
    const realm = bareRealm([SOURCE.model, SOURCE.assets, SOURCE.store, SOURCE.validate, SOURCE.drafts]);
    const A = realm.NERO.embed.assets;
    const M = realm.NERO.embed.model;
    const S = realm.NERO.embed.store;
    const V = realm.NERO.embed.validate;

    ['factState', 'assetFacts', 'noFacts', 'assetView', 'assetBytes',
     'retention', 'describeSize'].forEach((name) => {
        assert(typeof A[name] === 'function', 'assets.js publishes ' + name, typeof A[name]);
    });
    assert(Object.isFrozen(A.FACT_STATES) && A.FACT_STATES.LOCAL === 'bytes-local' &&
           A.FACT_STATES.MISSING === 'bytes-missing' && A.FACT_STATES.UNAVAILABLE === 'bytes-unavailable' &&
           A.FACT_STATES.CORRUPT === 'bytes-corrupt' && A.FACT_STATES.UNKNOWN === 'unknown',
        'the five fact states are frozen and named consistently with the record vocabulary',
        JSON.stringify(A.FACT_STATES));
    assert(typeof M.setDocumentAsset === 'function' && typeof M.removeDocumentAsset === 'function',
        'model.js owns the two asset document patches');
    const actions = Object.keys(S.createReducers(M));
    assert(actions.indexOf('asset/add') !== -1 && actions.indexOf('asset/remove') !== -1,
        'the store declares the asset actions', actions.join(','));
    assert(typeof S.createStore({ scheduler: { setTimeout, clearTimeout } }).historyDocuments === 'function',
        'the store can report what undo can still reach');

    // The template loads the asset layer before both consumers.
    const listed = (TEMPLATE_SRC.match(/js\/embed\/[a-z-]*(?:\/[a-z-]*)?\.js/g) || []);
    assert(listed.indexOf('js/embed/assets.js') !== -1 && listed.indexOf('js/embed/asset-store.js') !== -1,
        'the template loads assets.js and asset-store.js', listed.join(' '));
    assert(listed.indexOf('js/embed/assets.js') < listed.indexOf('js/embed/validate.js') &&
           listed.indexOf('js/embed/asset-store.js') < listed.indexOf('js/embed/message-builder-page.js'),
        'and both before the modules that read them', listed.join(' '));
    assert(listed.length === 13 && eq(listed, [
        'js/embed/model.js', 'js/embed/assets.js', 'js/embed/asset-store.js', 'js/embed/store.js',
        'js/embed/validate.js', 'js/embed/discord-markdown.js', 'js/embed/preview.js', 'js/embed/drafts.js',
        'js/embed/views/statusbar.js', 'js/embed/views/rail.js', 'js/embed/views/inspector.js',
        'js/embed/views/actionbar.js', 'js/embed/message-builder-page.js',
    ]), 'the page module list is thirteen entries, in dependency order', String(listed.length));
    const layout = readSource(LAYOUT_PATH);
    assert(layout.indexOf("js/embed/assets.js") !== -1 && layout.indexOf("js/embed/asset-store.js") !== -1,
        'and the layout harness knows the same two modules (one list, two readers)');

    // The page itself: storage, URLs, bytes and the issue list.
    assert(!/indexedDB|openDatabase|idbStorage|IDBKeyRange/.test(CODE.page),
        'the page opens no database of its own (the adapter owns that)');
    assert(!/\.remove\(|putBytes|assetStore\.remove/.test(CODE.page),
        'the page cannot delete or write a byte (no remove, no putBytes)');
    assert(!/createObjectURL|revokeObjectURL|urlFor|outstanding|blob:/.test(CODE.page),
        'the page mints no object URL and manages none');
    assert(!/sha256Hex|identify\(/.test(CODE.page),
        'the page never hashes a file');
    assert(!/pruneOrphans|retention/.test(CODE.page),
        'the page never prunes: the retention analysis has no automatic caller');
    assert((CODE.page.match(/assetStore\.create\(/g) || []).length === 1 &&
           /urls:\s*null,/.test(CODE.page),
        'the page builds exactly ONE byte store, and asks for no URLs');
    assert((CODE.page.match(/ui\/setIssues/g) || []).length === 1,
        'the page has exactly ONE issue-list writer');
    assert((CODE.page.match(/countValidation\(inst\);/g) || []).length === 1 &&
           (CODE.page.match(/ctx\.counter\('validateRuns'\)/g) || []).length === 1,
        'and exactly one place that counts a validation pass');
    assert((CODE.page.match(/if \(!inst\.assetProbe\)/g) || []).length === 1,
        'one in-flight probe guard, so a burst cannot stack probes');

    // The pure layers stay pure, and the byte store stays a byte store.
    assert(!/indexedDB|createObjectURL|urlFor|sha256Hex|setTimeout|Promise/.test(functionSource(VALIDATE_SRC, 'checkAssets')),
        'the asset rules open no storage, mint no URL, hash nothing and never await');
    assert(!/indexedDB|idbStorage|urlFor|blob:/.test(CODE.store),
        'the store knows nothing about storage');
    assert(!/document\.assets|setDocumentAsset|documentAssetIds|retention/.test(CODE.assetStore),
        'and the byte store knows nothing about documents (it was byte-frozen in 7b)');
    assert(!/indexedDB|NERO\.embed\.assets|createObjectURL/.test(functionSource(MODEL_SRC, 'setDocumentAsset')) &&
           !/indexedDB|NERO\.embed\.assets|createObjectURL/.test(functionSource(MODEL_SRC, 'removeDocumentAsset')),
        'the two model patches touch nothing but the document they are given');
    assert(!/buildRecord|normalizeRecord|RECORD_KEYS/.test(functionSource(MODEL_SRC, 'setDocumentAsset')),
        'and they do not re-implement the record vocabulary (assets.js owns it)');
}

// ═══════════════════════════════════════════════════════════════
section('B. the document contract: one canonical record shape, JSON only, never aliased');
// ═══════════════════════════════════════════════════════════════
{
    const realm = bareRealm([SOURCE.model, SOURCE.assets, SOURCE.store, SOURCE.validate, SOURCE.drafts]);
    const A = realm.NERO.embed.assets;
    const M = realm.NERO.embed.model;
    const D = realm.NERO.embed.drafts;
    const F = fixture(A);
    const rec = recordFor(A, F.png);

    assert(eq(M.blankMessageDocument().assets, {}),
        'a blank document starts with an empty asset map (never undefined)');
    assert(eq(M.normalizeDocument({ embeds: [], content: '' }).assets, {}),
        'and normalizing a document without one invents the same empty map');

    const normalized = M.normalizeDocument(M.setDocumentAsset(blankDoc(), F.png.assetId, rec));
    assert(normalized.assets[F.png.assetId] && normalized.assets[F.png.assetId].filename === 'one.png',
        'normalization keeps the record it was given');
    assert(eq(Object.keys(normalized.assets[F.png.assetId]), A.RECORD_KEYS),
        'in exactly the canonical key order', Object.keys(normalized.assets[F.png.assetId]).join(','));
    assert(M.hashDocument(M.normalizeDocument(normalized)) === M.hashDocument(normalized),
        'and normalizing an already-normalized document changes nothing (save/load is a fixed point)');
    assert(JSON.parse(JSON.stringify(M.normalizeDocument(normalized).assets[F.png.assetId])).bytes === 2048,
        'the record survives the round trip with its numbers intact');

    // setDocumentAsset: a pure patch, key by key.
    const base = blankDoc();
    const added = M.setDocumentAsset(base, F.png.assetId, rec);
    assert(added !== base && eq(base.assets, {}),
        'setDocumentAsset returns a NEW document and leaves the input alone');
    assert(eq(Object.keys(added.assets), [F.png.assetId]) && added.assets[F.png.assetId].assetId === F.png.assetId,
        'the record lands under the id it was addressed with');
    assert(added.assets[F.png.assetId] !== rec,
        'and it is a copy, not the caller\u2019s object');
    const recSnapshot = JSON.stringify(added.assets[F.png.assetId]);
    rec.filename = 'mutated.png';
    rec.bytes = 999;
    assert(JSON.stringify(added.assets[F.png.assetId]) === recSnapshot,
        'so mutating the record afterwards cannot change the document');
    rec.filename = 'one.png';
    rec.bytes = 2048;

    const twice = M.setDocumentAsset(added, F.png.assetId, rec);
    assert(twice === added, 'storing the SAME record again is not an edit (no document, no history entry)');
    const keyed = M.setDocumentAsset(blankDoc(), F.png.assetId, Object.assign({}, rec, { assetId: null }));
    assert(keyed.assets[F.png.assetId].assetId === F.png.assetId,
        'a record with no id of its own takes the key it is filed under');
    assert(M.hashDocument(keyed) === M.hashDocument(added),
        'and the hash only sees the canonical shape (id, not spelling)');

    // Refusals: anything a draft could not hold, or that disagrees with itself.
    const unsafe = {
        'a nested object': { nested: { deep: true } },
        'an array value': { tags: ['a'] },
        'a typed array': { captured: new Uint8Array([1, 2]) },
        'a Blob': { file: new Blob([new Uint8Array([1])], { type: 'image/png' }) },
        'a function': { fn: function () { return 1; } },
        'a Date': { when: new Date(0) },
    };
    Object.keys(unsafe).forEach((label) => {
        const bad = Object.assign({}, rec, unsafe[label]);
        assert(M.setDocumentAsset(added, F.gif.assetId, bad) === added,
            'a record carrying ' + label + ' is refused (the document is unchanged)');
        assert(!A.normalizeRecord(bad),
            'and the record module refuses it as well (' + label + ')');
    });
    // The same values filed under the record's OWN id. (Above, the key and the
    // record disagree, so the id rule refuses them first and the admission rule
    // is never the reason: these run where nothing else can hide it.)
    Object.keys(unsafe).forEach((label) => {
        const own = M.setDocumentAsset(blankDoc(), F.png.assetId, Object.assign({}, rec, unsafe[label]));
        assert(eq(Object.keys(own.assets), []),
            'a record carrying ' + label + ' is refused even when the key and the id agree');
    });
    assert(eq(Object.keys(M.setDocumentAsset(blankDoc(), F.png.assetId,
        Object.assign({}, rec, { filename: { nested: true } })).assets), []),
        'and a CANONICAL key holding a shape a draft cannot save is refused the same way');
    // NaN and undefined are the other way round: the DOCUMENT refuses them (a
    // draft cannot hold them: JSON turns both into null), while the record
    // module canonicalizes them into the "no value" the shape expects.
    [{ label: 'a NaN number', key: 'bytes', patch: { bytes: NaN } },
     { label: 'an undefined value', key: 'width', patch: { width: undefined } }].forEach((one) => {
        assert(M.setDocumentAsset(added, F.gif.assetId, Object.assign({}, rec, one.patch)) === added,
            'a record carrying ' + one.label + ' is refused by the document patch');
        const canonical = A.normalizeRecord(Object.assign({}, rec, one.patch));
        assert(!!canonical && canonical[one.key] === null,
            'while the record module canonicalizes it to null (' + one.label + ')');
    });
    assert(M.setDocumentAsset(added, '', rec) === added &&
           M.setDocumentAsset(added, F.gif.assetId, null) === added &&
           M.setDocumentAsset(added, F.gif.assetId, [rec]) === added &&
           M.setDocumentAsset(added, F.gif.assetId, 'one.png') === added,
        'and so are a missing id, a null, an array and a string record');
    const other = Object.assign({}, recordFor(A, F.png), { assetId: 'a_somewhere_else', filename: 'elsewhere.png' });
    assert(M.setDocumentAsset(added, F.png.assetId, other) === added,
        'a record that names a different file than its key is refused');
    assert(JSON.stringify(added.assets[F.png.assetId]) === recSnapshot &&
           added.assets.a_somewhere_else === undefined,
        'so the document keeps the record it had, under the only key it has');

    // Forward-compatibility: unknown SCALAR keys survive, unknown shapes do not.
    const foreign = Object.assign({}, rec, { capturedUrl: 'https://cdn.example/x.png', pinned: true });
    const foreignDoc = M.setDocumentAsset(blankDoc(), F.png.assetId, foreign);
    assert(foreignDoc.assets[F.png.assetId].capturedUrl === 'https://cdn.example/x.png' &&
           foreignDoc.assets[F.png.assetId].pinned === true,
        'an unknown but JSON-safe key is kept (a rollback cannot break a forward draft)');
    assert(eq(A.assetIssues(foreignDoc), []),
        'and the record still reads clean');
    const smuggled = M.cloneDocument(foreignDoc);
    smuggled.assets[F.png.assetId].captured = new Uint8Array([9, 9]);
    assert(!A.normalizeRecord(smuggled.assets[F.png.assetId]) &&
           eq(A.assetIssues(smuggled).map((i) => i.reason), ['non-json-value']) &&
           eq(A.assetIssues(smuggled)[0].keys, ['captured']),
        'while a byte value smuggled into a saved draft is reported as what it is, and where',
        JSON.stringify(A.assetIssues(smuggled)));
    assert(!!throwsWith(() => D.toStorable(smuggled)),
        'and the draft writer refuses the whole document rather than storing it');

    // removeDocumentAsset: the same contract in reverse.
    const removed = M.removeDocumentAsset(added, F.png.assetId);
    assert(removed !== added && eq(removed.assets, {}) && eq(added.assets[F.png.assetId], Object.assign({}, rec, {})),
        'removeDocumentAsset drops the key and leaves the input alone');
    assert(M.removeDocumentAsset(removed, F.png.assetId) === removed &&
           M.removeDocumentAsset(removed, 'nobody') === removed,
        'and removing something that is not there is a no-op');

    // The document stays JSON: a round-trip is a deep-equal, and the payload
    // still has no idea what an asset is.
    const rich = M.setMedia(M.setDocumentAsset(blankDoc(), F.gif.assetId, recordFor(A, F.gif)),
        'emb-1', 'image', uploadRef(F.gif));
    assert(eq(JSON.parse(JSON.stringify(rich)), rich), 'the document survives a JSON round-trip unchanged');
    const payload = M.toDiscordPayload(rich);
    assert(JSON.stringify(payload).indexOf('assets') === -1 && JSON.stringify(payload).indexOf(F.gif.sha256) === -1,
        'and the Discord payload carries neither the map nor a digest',
        JSON.stringify(payload).slice(0, 160));
    assert(M.toDiscordPayload(rich).embeds[0].image.url === 'attachment://two.gif',
        'the upload reference is still a plain attachment name on the wire');
}

// ═══════════════════════════════════════════════════════════════
section('C. references: a slot points at an id, never at bytes');
// ═══════════════════════════════════════════════════════════════
{
    const realm = pureRealm();
    const A = realm.NERO.embed.assets;
    const M = realm.NERO.embed.model;
    const F = fixture(A);

    let doc = M.setDocumentAsset(blankDoc(), F.png.assetId, recordFor(A, F.png));
    doc = M.setMedia(doc, 'emb-1', 'image', uploadRef(F.png));
    assert(eq(A.documentAssetIds(doc), [F.png.assetId]),
        'a linked image names exactly one asset', JSON.stringify(A.documentAssetIds(doc)));
    const refs = A.refsOf(doc);
    assert(refs.length === 1 && refs[0].path === 'embeds.0.image' && refs[0].embedId === 'emb-1' &&
           refs[0].slot === 'image' && refs[0].assetId === F.png.assetId && refs[0].filename === 'one.png',
        'and the reference says where the slot is, in document order', JSON.stringify(refs));
    assert(JSON.stringify(doc.assets[F.png.assetId]).indexOf('data:') === -1,
        'no bytes, no data URL: the document holds metadata only');

    // One file, four slots: one upload.
    let four = doc;
    four = M.setMedia(four, 'emb-1', 'thumbnail', uploadRef(F.png));
    four = M.setAuthor(four, 'emb-1', { name: 'Author', icon: uploadRef(F.png) });
    four = M.setFooter(four, 'emb-1', { text: 'Footer', icon: uploadRef(F.png) });
    assert(eq(A.documentAssetIds(four), [F.png.assetId]) && A.refsOf(four).length === 4,
        'the same file in four slots is ONE asset and four references',
        A.refsOf(four).map((r) => r.path).join(','));
    assert(eq(A.assetBytes(A.assetView(four)), { count: 1, total: 2048, unknown: [], missing: [], complete: true }),
        'so it is counted once and charged once',
        JSON.stringify(A.assetBytes(A.assetView(four))));

    // Two files: the id list is sorted, so link order cannot change it.
    let ab = M.setDocumentAsset(blankDoc(), F.png.assetId, recordFor(A, F.png));
    ab = M.setDocumentAsset(ab, F.gif.assetId, recordFor(A, F.gif));
    ab = M.setMedia(ab, 'emb-1', 'image', uploadRef(F.png));
    ab = M.setMedia(ab, 'emb-1', 'thumbnail', uploadRef(F.gif));
    let ba = M.setDocumentAsset(blankDoc(), F.gif.assetId, recordFor(A, F.gif));
    ba = M.setDocumentAsset(ba, F.png.assetId, recordFor(A, F.png));
    ba = M.setMedia(ba, 'emb-1', 'thumbnail', uploadRef(F.gif));
    ba = M.setMedia(ba, 'emb-1', 'image', uploadRef(F.png));
    assert(eq(A.documentAssetIds(ab), A.documentAssetIds(ba)) &&
           A.documentAssetIds(ab).length === 2,
        'two files produce a stable, sorted id list whatever order they were linked in',
        JSON.stringify(A.documentAssetIds(ab)));

    // Clearing a slot drops the reference, never the record.
    let cleared = M.setMedia(four, 'emb-1', 'image', null);
    cleared = M.setMedia(cleared, 'emb-1', 'thumbnail', null);
    cleared = M.setAuthor(cleared, 'emb-1', { icon: null });
    cleared = M.setFooter(cleared, 'emb-1', { icon: null });
    assert(eq(A.documentAssetIds(cleared), []) && cleared.assets[F.png.assetId] !== undefined,
        'clearing every slot drops every reference and leaves the record untouched');

    // A URL slot is not a reference, and a legacy attachment:// string stays a
    // string (it is a WIRE value, not an upload).
    let urls = M.setMedia(doc, 'emb-1', 'image', { kind: 'url', url: 'https://example.test/x.png' });
    assert(eq(A.documentAssetIds(urls), []), 'a URL slot references no asset');
    assert(M.mediaToWireUrl(M.mediaFromValue('attachment://legacy.png')) === 'attachment://legacy.png',
        'and a hand-typed attachment:// name stays a wire value (7c does not adopt it)');
    assert(M.mediaFromValue({ kind: 'upload', assetId: F.png.assetId, filename: 'one.png', mime: 'image/png', bytes: 12 })
        .assetId === F.png.assetId,
        'while an upload value keeps its id, name, type and size');

    // An unlinked slot is its own problem, and raw values are not inspected.
    let unlinked = M.setDocumentAsset(blankDoc(), F.png.assetId, recordFor(A, F.png));
    unlinked = M.setMedia(unlinked, 'emb-1', 'image', { kind: 'upload', filename: 'one.png' });
    assert(eq(A.documentAssetIds(unlinked), []) && A.assetView(unlinked).unlinked.length === 1,
        'a slot with no id is reported as unlinked, not as a reference');
    assert(A.assetView(unlinked).orphans.length === 1,
        'and the record it cannot reach is an orphan');

    // The view is read-only, even of a frozen document.
    const frozen = Object.freeze(M.setDocumentAsset(blankDoc(), F.png.assetId, recordFor(A, F.png)));
    assert(A.assetView(frozen).ok === true && Object.isFrozen(frozen),
        'assetView() reads a frozen document without touching it');
    assert(A.assetView({ embeds: 'nonsense', assets: null }).ok === true &&
           eq(A.documentAssetIds({ embeds: null, assets: 5 }), []),
        'and a structurally wrong document is described, never repaired');
}

// ═══════════════════════════════════════════════════════════════
section('D. the store: the asset map is an ordinary document edit');
// ═══════════════════════════════════════════════════════════════
{
    const realm = pureRealm();
    const A = realm.NERO.embed.assets;
    const M = realm.NERO.embed.model;
    const S = realm.NERO.embed.store;
    const F = fixture(A);
    const store = S.createStore({
        document: blankDoc(), scheduler: { setTimeout, clearTimeout },
        now: () => RECORD_NOW, reducers: S.createReducers(M),
    });
    store.markSaved(store.getDocument());
    assert(store.isDirty() === false, 'a fresh store is clean');

    store.dispatch({ type: 'asset/add', assetId: F.png.assetId, record: recordFor(A, F.png) });
    assert(store.getDocument().assets[F.png.assetId] !== undefined && store.isDirty() === true,
        'asset/add reaches the canonical document and makes it dirty');
    assert(store.historyDepth().size === 2, 'as exactly ONE undo entry', String(store.historyDepth().size));
    store.dispatch({ type: 'asset/add', assetId: F.png.assetId, record: recordFor(A, F.png) });
    assert(store.historyDepth().size === 2 && store.getDocument().assets[F.png.assetId] !== undefined,
        'storing the same record again adds no history');

    // The key is the ACTION's id. A record that claims a different file is not
    // quietly re-filed under the name it believes in — the caller asked for
    // 'a_claim', so either it lands there or nothing happens.
    const depthBeforeClaim = store.historyDepth().size;
    store.dispatch({ type: 'asset/add', assetId: 'a_claim', record: recordFor(A, F.gif) });
    assert(store.historyDepth().size === depthBeforeClaim &&
           store.getDocument().assets.a_claim === undefined &&
           store.getDocument().assets[F.gif.assetId] === undefined,
        'asset/add files the record under the id the ACTION names, never the one the record claims',
        JSON.stringify(Object.keys(store.getDocument().assets)));

    store.dispatch({ type: 'embed/setMedia', embedId: 'emb-1', slot: 'image', value: uploadRef(F.png) });
    assert(store.historyDepth().size === 3, 'linking is its own undo entry');
    store.dispatch({ type: 'asset/remove', assetId: F.png.assetId });
    assert(store.getDocument().assets[F.png.assetId] === undefined && store.historyDepth().size === 4,
        'asset/remove drops the record in one more entry');
    store.dispatch({ type: 'asset/remove', assetId: F.png.assetId });
    assert(store.historyDepth().size === 4, 'removing what is already gone is a no-op');

    store.undo();
    assert(store.getDocument().assets[F.png.assetId] !== undefined,
        'undo brings the record back (metadata only: no byte was touched to do it)');
    store.undo();
    assert(store.getDocument().assets[F.png.assetId] !== undefined && store.getDocument().embeds[0].image === null,
        'undo again unlinks the slot while the record stays');
    store.undo();
    assert(eq(store.getDocument().assets, {}) && store.historyDepth().index === 0 && store.isDirty() === false,
        'and undoing back to the saved document leaves the store clean');
    store.redo();
    assert(store.isDirty() === true && store.getDocument().assets[F.png.assetId] !== undefined,
        'redo restores the record and the document is dirty again');

    // historyDocuments(): private clones of everything undo (and redo) can reach.
    store.dispatch({ type: 'embed/setMedia', embedId: 'emb-1', slot: 'image', value: uploadRef(F.png) });
    store.undo();                                   // leave the redo tail live
    const depth = store.historyDepth();
    const docs = store.historyDocuments();
    assert(docs.length === depth.size && depth.size === 3 && depth.index === 1,
        'historyDocuments() reports every entry the stack holds, including the redo tail',
        docs.length + '/' + depth.size + '@' + depth.index);
    assert(M.hashDocument(docs[0]) === M.hashDocument(blankDoc()),
        'oldest first, starting at the baseline the document was saved with');
    assert(M.hashDocument(docs[depth.index]) === M.hashDocument(store.getDocument()),
        'including the document the store is actually holding now');
    assert(M.hashDocument(docs[2]) === M.hashDocument(M.setMedia(store.getDocument(), 'emb-1', 'image', uploadRef(F.png))),
        'and the tail redo would restore (the linked document, not the current one)');
    assert(docs.every((d) => d && typeof d === 'object' && d.assets && Array.isArray(d.embeds)),
        'every entry is a whole document (so a reference walk can read it)');
    docs.forEach((d) => { d.content = 'hijacked'; d.assets = {}; });
    assert(store.getDocument().content === '' && store.getDocument().assets[F.png.assetId] !== undefined,
        'mutating what historyDocuments() returns cannot change the store');
    assert(M.hashDocument(store.historyDocuments()[0]) === M.hashDocument(blankDoc()),
        'and the next call still sees the real history');

    // Without its reducer table the store cannot edit assets at all.
    const bare = S.createStore({ document: blankDoc(), scheduler: { setTimeout, clearTimeout } });
    bare.dispatch({ type: 'asset/add', assetId: F.png.assetId, record: recordFor(A, F.png) });
    assert(eq(bare.getDocument().assets, {}),
        'a store without the reducer table ignores asset/add (documented, not silent)');
}

// ═══════════════════════════════════════════════════════════════
section('E. validation without facts: the document\u2019s own metadata');
// ═══════════════════════════════════════════════════════════════
{
    const realm = validateRealm();
    const A = realm.NERO.embed.assets;
    const M = realm.NERO.embed.model;
    const F = fixture(A);

    const clean = linkedDoc(A, M, F.png, { bytes: 2048 });
    assert(eq(codes(run(realm, clean)), []),
        'a described, referenced file is clean under the served limits', codes(run(realm, clean)).join(','));

    // A reference with no record.
    let orphan = M.setMedia(M.setDocumentAsset(blankDoc(), 'a_ghost', null), 'emb-1', 'image',
        { kind: 'upload', assetId: 'a_ghost', filename: 'ghost.png', mime: 'image/png', bytes: 100 });
    assert(eq(codes(run(realm, orphan)), ['assets.record-missing']),
        'a slot pointing at a file the message does not carry is an error',
        codes(run(realm, orphan)).join(','));
    assert(run(realm, orphan)[0].severity === 'error' &&
           run(realm, orphan)[0].path === 'embeds.0.image',
        'reported at the slot that points nowhere, as an error');

    // A record that cannot be read.
    let broken = M.setDocumentAsset(blankDoc(), 'a_broken', { assetId: 'a_broken' });
    broken = M.setMedia(broken, 'emb-1', 'image', { kind: 'upload', assetId: 'a_broken', filename: 'b.png' });
    assert(eq(codes(run(realm, broken)), ['assets.record-unreadable']),
        'a record that cannot be read is an error', codes(run(realm, broken)).join(','));
    assert(run(realm, broken)[0].path === 'assets.a_broken' &&
           run(realm, broken)[0].message.indexOf('could not be read') !== -1,
        'reported at the record, with a sentence that says so');

    // A non-JSON record: the other unreadable reason, named.
    const poisoned = M.cloneDocument(clean);
    poisoned.assets[F.png.assetId].captured = new Uint8Array([1, 2, 3]);
    const poisonedIssues = run(realm, poisoned);
    assert(poisonedIssues.length === 1 && poisonedIssues[0].code === 'assets.record-unreadable' &&
           poisonedIssues[0].message.indexOf('cannot be saved in a draft') !== -1,
        'a record holding a value a draft cannot save says exactly that',
        JSON.stringify(poisonedIssues.map((i) => i.message)));

    // A record that cannot be read and that NOTHING references: one problem,
    // not two. (A referenced one has its own fixtures; the orphan list is the
    // only place that could call the same entry unused a second time.)
    const unreadableOrphan = M.setDocumentAsset(clean, 'a_bad', { assetId: 'a_bad' });
    const orphanView = A.assetView(unreadableOrphan);
    assert(eq(orphanView.orphans, []) && eq(orphanView.unreadable.map((e) => e.assetId), ['a_bad']),
        'a record that cannot be read is not ALSO called unused',
        JSON.stringify({ orphans: orphanView.orphans, unreadable: orphanView.unreadable.map((e) => e.assetId) }));
    assert(eq(codes(run(realm, unreadableOrphan)), ['assets.record-unreadable']),
        'and the validator reports it once, as an error about the record',
        codes(run(realm, unreadableOrphan)).join(','));

    // An unlinked slot.
    let unlinked = M.setDocumentAsset(blankDoc(), F.png.assetId, recordFor(A, F.png));
    unlinked = M.setMedia(unlinked, 'emb-1', 'image', { kind: 'upload', filename: 'one.png' });
    assert(eq(codes(run(realm, unlinked)), ['assets.unlinked', 'assets.unused']),
        'a slot with no file id is its own error (and its record is unused)',
        codes(run(realm, unlinked)).join(','));

    // A record nothing references.
    const unused = M.setDocumentAsset(blankDoc(), F.png.assetId, recordFor(A, F.png));
    assert(eq(codes(run(realm, unused)), ['assets.unused']) && run(realm, unused)[0].severity === 'warning',
        'a record no slot uses is a warning, not an error', codes(run(realm, unused)).join(','));

    // Count and size, always from the served table.
    let many = blankDoc();
    const idents = [];
    for (let i = 0; i < 4; i++) {
        const ident = i === 0 ? F.png : i === 1 ? F.gif : A.identify(pngBytes('extra-' + i), 'extra-' + i + '.png');
        idents.push(ident);
        many = M.setDocumentAsset(many, ident.assetId, recordFor(A, ident, { bytes: 1000 + i }));
        if (i) many = M.addEmbed(many);          // one embed per streamed file: Discord dedupes by URL
        many = M.setMedia(many, many.embeds[i].id, 'image', uploadRef(ident, { bytes: 1000 + i }));
    }
    assert(eq(codes(run(realm, many)), []),
        'four referenced files are clean under the full served table', codes(run(realm, many)).join(','));
    const tinyCount = servedLimits({ attachments: { count_max: 3 } });
    assert(eq(codes(run(realm, many, tinyCount)), ['assets.too-many']),
        'a served count cap changes the verdict (no number is baked in)',
        codes(run(realm, many, tinyCount)).join(','));
    assert(run(realm, many, tinyCount)[0].message.indexOf('at most 3') !== -1 &&
           run(realm, many, tinyCount)[0].message.indexOf('this one has 4') !== -1,
        'and the message names the served cap and the actual count',
        run(realm, many, tinyCount)[0].message);
    const tinyTotal = servedLimits({ attachments: { total_bytes_max: 2500 } });
    assert(eq(codes(run(realm, many, tinyTotal)), ['assets.total-size']),
        'a served total-size cap does the same for the sum', codes(run(realm, many, tinyTotal)).join(','));
    assert(find(run(realm, many, tinyTotal), 'assets.total-size')[0].message.indexOf(A.describeSize(1000 + 1001 + 1002 + 1003)) !== -1,
        'naming the total in human units, through the module\u2019s own formatter',
        find(run(realm, many, tinyTotal), 'assets.total-size')[0].message);
    assert(eq(codes(run(realm, many, servedLimits({ attachments: { total_bytes_max: 26148864 } }))), []),
        'and the same document passes under a larger served cap (the table is the only limit)');

    // A referenced id with no record AND a total over the served cap: the size
    // sentence is suppressed. The missing file is already the problem; "the
    // files add up to more than Discord accepts" would name a second one, and
    // the number it prints would silently exclude the file it cannot see.
    let missingAndOver = M.setDocumentAsset(blankDoc(), F.gif.assetId, recordFor(A, F.gif, { bytes: 4000 }));
    missingAndOver = M.setMedia(missingAndOver, 'emb-1', 'image', uploadRef(F.gif, { bytes: 4000 }));
    missingAndOver = M.setMedia(missingAndOver, 'emb-1', 'thumbnail',
        { kind: 'upload', assetId: 'a_ghost_size', filename: 'ghost.png' });
    assert(eq(codes(run(realm, missingAndOver, tinyTotal)), ['assets.record-missing']),
        'a total that skips a missing file is not reported as a size problem as well',
        codes(run(realm, missingAndOver, tinyTotal)).join(','));
    assert(run(realm, many, servedLimits({ attachments: null })).length === 1 &&
           run(realm, many, servedLimits({ attachments: null }))[0].code === 'limits.missing',
        'a missing attachment block fails explicitly, exactly like a missing embed key',
        codes(run(realm, many, servedLimits({ attachments: null }))).join(','));

    // A size the document does not know is never guessed.
    let unknownSize = M.setDocumentAsset(blankDoc(), F.png.assetId, recordFor(A, F.png, { bytes: null }));
    unknownSize = M.setMedia(unknownSize, 'emb-1', 'image', uploadRef(F.png, { bytes: null }));
    assert(eq(codes(run(realm, unknownSize, tinyTotal)), ['assets.size-unknown']),
        'a record with no byte count says "not measured", never a total',
        codes(run(realm, unknownSize, tinyTotal)).join(','));
    assert(find(run(realm, unknownSize, tinyTotal), 'assets.total-size').length === 0,
        'and it never claims a total it could not compute');

    // The per-file advisory, and the hard variant.
    const big = recordFor(A, F.gif, { bytes: 22000000 });   // over the advisory, under the total
    let bigDoc = M.setDocumentAsset(blankDoc(), F.gif.assetId, big);
    bigDoc = M.setMedia(bigDoc, 'emb-1', 'image', uploadRef(F.gif));
    assert(eq(codes(run(realm, bigDoc)), ['assets.file-too-large']) &&
           run(realm, bigDoc)[0].severity === 'warning',
        'a file over the advisory is a WARNING (Discord may well accept it)',
        codes(run(realm, bigDoc)).join(','));
    const hard = servedLimits({ attachments: { file_advisory_is_hard: true } });
    assert(run(realm, bigDoc, hard)[0].severity === 'error',
        'and an error only when the served table says the advisory is hard');

    // Filenames.
    const twin = A.identify(pngBytes('clash'), 'clash.png');   // a DIFFERENT file, same name
    let clash = M.setDocumentAsset(blankDoc(), F.png.assetId, recordFor(A, F.png));
    clash = M.setMedia(clash, 'emb-1', 'image', uploadRef(F.png));
    clash = M.setDocumentAsset(clash, twin.assetId, recordFor(A, twin, { filename: 'one.png' }));
    clash = M.addEmbed(clash);
    clash = M.setMedia(clash, clash.embeds[1].id, 'image', uploadRef(twin, { filename: 'one.png' }));
    assert(eq(codes(run(realm, clash)), ['assets.filename-clash']),
        'two different files sharing one name is an error (Discord needs one name per file)',
        codes(run(realm, clash)).join(','));
    let relabelled = M.setDocumentAsset(blankDoc(), F.png.assetId, recordFor(A, F.png));
    relabelled = M.setMedia(relabelled, 'emb-1', 'image', uploadRef(F.png, { filename: 'renamed.png' }));
    assert(eq(codes(run(realm, relabelled)), ['assets.filename-changed']) &&
           run(realm, relabelled)[0].severity === 'warning',
        'a slot whose name disagrees with its record is a warning',
        codes(run(realm, relabelled)).join(','));

    // Extension / type.
    let pdfish = M.setDocumentAsset(blankDoc(), F.gif.assetId, recordFor(A, F.gif, { filename: 'notes.pdf' }));
    pdfish = M.setMedia(pdfish, 'emb-1', 'image', uploadRef(F.gif, { filename: 'notes.pdf' }));
    const pdfIssues = find(run(realm, pdfish), 'assets.extension-not-allowed');
    assert(pdfIssues.length === 1 && pdfIssues[0].severity === 'error' &&
           A.ALLOWED_EXTENSIONS.every((ext) => pdfIssues[0].message.indexOf('.' + ext) !== -1) &&
           pdfIssues[0].message.indexOf('notes.pdf') !== -1,
        'an extension Discord cannot show names the file and the module\u2019s allowed list',
        pdfIssues[0].message);
    let typed = M.setDocumentAsset(blankDoc(), F.png.assetId, recordFor(A, F.png, { mime: 'image/gif' }));
    typed = M.setMedia(typed, 'emb-1', 'image', uploadRef(F.png));
    assert(eq(codes(run(realm, typed)), ['assets.format-mismatch']) &&
           run(realm, typed)[0].message.indexOf('GIF') !== -1,
        'a name and a recorded type that disagree are a warning naming the type',
        codes(run(realm, typed)).join(','));
    let typeless = M.setDocumentAsset(blankDoc(), F.png.assetId, recordFor(A, F.png, { mime: '' }));
    typeless = M.setMedia(typeless, 'emb-1', 'image', uploadRef(F.png));
    assert(eq(codes(run(realm, typeless)), ['assets.mime-unknown']),
        'and a record with no content type at all is its own warning',
        codes(run(realm, typeless)).join(','));

    // Order, determinism, purity.
    let mixed = M.setDocumentAsset(M.setContent(blankDoc(), rep('x', 2100)), 'a_broken', { assetId: 'a_broken' });
    mixed = M.setMedia(mixed, 'emb-1', 'image', { kind: 'upload', assetId: 'a_broken', filename: 'b.png' });
    assert(eq(codes(run(realm, mixed)), ['content.too-long', 'assets.record-unreadable']),
        'the asset rules come LAST: the first issue is still the first problem in reading order',
        codes(run(realm, mixed)).join(','));
    assert(JSON.stringify(run(realm, many)) === JSON.stringify(run(realm, many)) &&
           realm.NERO.embed.validate.signature(run(realm, many)) === realm.NERO.embed.validate.signature(run(realm, many)),
        'the same document produces the same issues and the same signature');
    const before = JSON.stringify(many);
    run(realm, many);
    assert(JSON.stringify(many) === before, 'and validating a document never changes it');
}

// ═══════════════════════════════════════════════════════════════
section('F. validation with facts: five different answers, never a guess');
// ═══════════════════════════════════════════════════════════════
{
    const realm = validateRealm();
    const A = realm.NERO.embed.assets;
    const M = realm.NERO.embed.model;
    const F = fixture(A);
    const doc = linkedDoc(A, M, F.png, { bytes: 2048 });

    assert(A.factState(null) === 'unknown' && A.factState({}) === 'unknown' &&
           A.factState({ present: false }) === 'unknown',
        'a row that says nothing is unknown, not missing');
    assert(A.factState({ present: true }) === 'bytes-local' &&
           A.factState(rowFor(F.png)) === 'bytes-local',
        'a present row reads as bytes-local');
    assert(A.factState({ present: false, reason: 'missing' }) === 'bytes-missing',
        'a miss reads as bytes-missing');
    ['read-failed', 'read-threw', 'open-timeout', 'open-threw', 'no-indexeddb', 'storage-unavailable',
     'destroyed', 'write-failed'].forEach((why) => {
        assert(A.factState({ present: false, reason: why }) === 'bytes-unavailable',
            'and "' + why + '" is unavailable (we could not tell), never missing');
    });
    assert(A.factState({ present: false, reason: 'corrupt' }) === 'bytes-corrupt',
        'a damaged entry is corrupt, which is not a miss either');

    // The facts object: one entry per referenced id, in the document\u2019s order.
    const none = A.assetFacts(doc, null);
    assert(none.supplied === false && eq(none.ids, [F.png.assetId]) &&
           none.states[F.png.assetId] === 'unknown' && eq(none.missingRows, [F.png.assetId]),
        'with no probe every referenced id is unknown and reported as unprobed');
    const stale = A.assetFacts(doc, [rowFor(F.gif)]);
    assert(stale.states[F.png.assetId] === 'unknown',
        'a row for a different asset leaves this one unknown (never a false alarm)');
    const dup = A.assetFacts(doc, [rowFor(F.png, { present: false, reason: 'missing' }), rowFor(F.png)]);
    assert(dup.states[F.png.assetId] === 'bytes-missing',
        'and the first row for an id wins, so the answer is deterministic');

    // No facts at all: silence.
    assert(eq(codes(run(realm, doc, LIMITS, null)), []), 'no facts at all produces no availability issue');
    assert(eq(codes(run(realm, doc, LIMITS, A.assetFacts(doc, []))), []),
        'and neither does an empty probe result');
    assert(eq(codes(run(realm, doc, LIMITS, rowFacts(realm, doc, [rowFor(F.png)]))), []),
        'while a present, matching row is clean',
        codes(run(realm, doc, LIMITS, rowFacts(realm, doc, [rowFor(F.png)]))).join(','));

    // The three failure answers, told apart.
    const missing = rowFacts(realm, doc, [rowFor(F.png, { present: false, reason: 'missing', source: null, byteLength: null, sha256: null, mime: null })]);
    const missingIssues = run(realm, doc, LIMITS, missing);
    assert(eq(codes(missingIssues), ['assets.bytes-missing']) && missingIssues[0].severity === 'warning',
        'missing bytes are a warning that says the file is not stored here',
        codes(missingIssues).join(','));
    assert(missingIssues[0].message.indexOf('no longer stored') !== -1, 'with wording that does not blame the user');
    const degraded = rowFacts(realm, doc, [rowFor(F.png, { present: false, reason: 'no-indexeddb', source: null, byteLength: null, sha256: null, mime: null })]);
    const degradedIssues = run(realm, doc, LIMITS, degraded);
    assert(eq(codes(degradedIssues), ['assets.bytes-unavailable']) && degradedIssues[0].severity === 'warning',
        '"could not check" is a DIFFERENT answer from "not there"', codes(degradedIssues).join(','));
    assert(degradedIssues[0].message.indexOf('could not be checked') !== -1 &&
           degradedIssues[0].message.indexOf('no longer stored') === -1,
        'and it says so, without claiming the bytes are gone');
    const corrupt = rowFacts(realm, doc, [rowFor(F.png, { present: false, reason: 'corrupt', source: null, byteLength: null, sha256: null, mime: null })]);
    assert(eq(codes(run(realm, doc, LIMITS, corrupt)), ['assets.bytes-corrupt']) &&
           run(realm, doc, LIMITS, corrupt)[0].severity === 'error',
        'a damaged entry is an error (the bytes are there and wrong)',
        codes(run(realm, doc, LIMITS, corrupt)).join(','));

    // One problem per asset, however many slots use it.
    let four = M.setMedia(doc, 'emb-1', 'thumbnail', uploadRef(F.png));
    four = M.setAuthor(four, 'emb-1', { name: 'Author', icon: uploadRef(F.png) });
    four = M.setFooter(four, 'emb-1', { text: 'Footer', icon: uploadRef(F.png) });
    assert(find(run(realm, four, LIMITS, missing), 'assets.bytes-missing').length === 1,
        'four slots, one file, ONE bytes-missing issue',
        String(find(run(realm, four, LIMITS, missing), 'assets.bytes-missing').length));

    // The record/row comparison needs no digest: it compares what each side says.
    const wrongDigest = rowFacts(realm, doc, [rowFor(F.png, { sha256: rep('ab', 32) })]);
    assert(eq(codes(run(realm, doc, LIMITS, wrongDigest)), ['assets.bytes-mismatch']) &&
           run(realm, doc, LIMITS, wrongDigest)[0].severity === 'error',
        'a stored copy with a different digest is not the file the record describes',
        codes(run(realm, doc, LIMITS, wrongDigest)).join(','));
    const wrongLength = rowFacts(realm, doc, [rowFor(F.png, { byteLength: 999 })]);
    assert(eq(codes(run(realm, doc, LIMITS, wrongLength)), ['assets.bytes-mismatch']),
        'and so is a stored length that disagrees', codes(run(realm, doc, LIMITS, wrongLength)).join(','));
    const wrongType = rowFacts(realm, doc, [rowFor(F.png, { mime: 'image/webp' })]);
    assert(eq(codes(run(realm, doc, LIMITS, wrongType)), ['assets.mime-mismatch']) &&
           run(realm, doc, LIMITS, wrongType)[0].severity === 'warning',
        'a stored content type that disagrees is a warning', codes(run(realm, doc, LIMITS, wrongType)).join(','));
    const neutral = rowFacts(realm, doc, [rowFor(F.png, { mime: 'application/octet-stream' })]);
    assert(eq(codes(run(realm, doc, LIMITS, neutral)), []),
        'while a stored blob with no image type is not evidence of anything',
        codes(run(realm, doc, LIMITS, neutral)).join(','));

    // Facts are observations: the rules never rewrite them, and never touch bytes.
    const facts = rowFacts(realm, doc, [rowFor(F.png)]);
    const factsBefore = JSON.stringify(facts);
    const docBefore = JSON.stringify(doc);
    run(realm, doc, LIMITS, facts);
    assert(JSON.stringify(facts) === factsBefore && JSON.stringify(doc) === docBefore,
        'validating with facts changes neither the document nor the facts');
}

// ── the page rig helpers (a seeded draft + optional byte record) ──
function pageSeed(env, opts) {
    const options = opts || {};
    const A = env.NERO.embed.assets, M = env.NERO.embed.model, D = env.NERO.embed.drafts;
    const F = fixture(A);
    const ident = options.ident || F.png;
    const file = options.file || F.pngFile;
    const documentId = options.documentId || 'doc-integration';
    let doc = options.document || null;
    if (!doc) {
        doc = M.setDocumentAsset(blankDoc({ id: documentId }), ident.assetId,
            recordFor(A, ident, { bytes: options.recordBytes === undefined ? file.length : options.recordBytes }));
        doc = M.setMedia(doc, 'emb-1', 'image', uploadRef(ident, { bytes: file.length }));
    }
    const record = D.buildRecord({
        guildId: GUILD, documentId: documentId, document: doc, now: RECORD_NOW,
    });
    const seed = { [NS]: { drafts: {}, assets: {}, meta: {} } };
    if (options.withDraft !== false) {
        seed[NS].drafts[D.draftKey(GUILD, documentId)] = record;
        seed[NS].meta[D.metaKey(GUILD, 'last')] = { documentId: documentId, updatedAt: RECORD_NOW };
    }
    if (options.withBytes !== false) seed[NS].assets[ident.assetId] = entryFor(ident, file);
    Object.keys(options.extraAssets || {}).forEach((key) => { seed[NS].assets[key] = options.extraAssets[key]; });
    return { seed: seed, doc: doc, ident: ident, file: file, documentId: documentId, record: record };
}

async function rig(opts) {
    const env = makeEnv();
    const seed = pageSeed(env, opts);
    if (opts && opts.noIdb) {
        // nothing installed: the page runs in memory (degraded storage)
    } else if (opts && opts.idb) {
        env.useIdb(opts.idb);
    } else {
        env.installIdb({ seed: seed.seed });
    }
    await env.mount(opts || {});
    // Boot finishes in two steps — the page paints from the draft, then probes
    // the byte store for the ids it just learned about. Wait for the FACTS to
    // describe the document before a section reads them: under load the probe
    // lands after the first paint (by design), and a section that read early
    // would be reading a half-finished boot, not testing the page.
    await env.until(() => {
        const ids = env.NERO.embed.assets.documentAssetIds(env.store().getDocument());
        return eq(env.facts().ids, ids) && env.passes() >= 1 && (ids.length === 0 || env.probes() >= 1);
    }, 3000);
    await env.settle(20);
    return env;
}

// ═══════════════════════════════════════════════════════════════
section('G. the page coordinates one probe per referenced-id set, and owns no bytes');
// ═══════════════════════════════════════════════════════════════
async function sectionG() {
    const env = await rig({});
    const A = env.NERO.embed.assets;
    const id = env.inst.assetFacts.ids[0];
    assert(env.probes() === 1,
        'booting a draft that references one file costs exactly ONE probe',
        String(env.probes()));
    assert(env.passes() === 1,
        'and one validation pass on top of it (the probe is not a pass)',
        String(env.passes()));
    assert(env.facts().states[id] === 'bytes-local',
        'the facts say the bytes are here', JSON.stringify(env.facts().states));
    assert(env.facts().supplied === true && env.facts().ids.length === 1,
        'and they describe exactly the ids the document references');
    assert(eq(codes(env.issues()), []) && env.strip().hidden === true,
        'a described, present file leaves the strip hidden', codes(env.issues()).join(','));
    const stats = env.stats();
    assert(stats.writes === 0 && stats.deletes === 0 && stats.mints === 0 && stats.revokes === 0,
        'the probe wrote nothing, deleted nothing and minted no URL', JSON.stringify(stats));
    assert(env.inst.assetStore.outstanding().length === 0,
        'and no object URL is outstanding');
    assert(env.net.calls === 0, 'nothing on this path touched the network');

    // Typing costs no probe and no storage read.
    const readsBefore = env.idb.log.filter((e) => e.op === 'get').length;
    const passesBefore = env.passes();
    const probesBefore = env.probes();
    ['x', 'xy', 'xyz'].forEach((text, i) => {
        env.store().dispatch({ type: 'content/set', text: rep(text, 1 + i) });
    });
    await env.until(() => env.passes() > passesBefore, 3000);
    await env.settle(30);
    assert(env.probes() === probesBefore,
        'typing never probes (the referenced ids did not change)', String(env.probes()));
    assert(env.passes() === passesBefore + 1,
        'and a burst of keystrokes is exactly ONE validation pass',
        String(env.passes() - passesBefore));
    assert(env.idb.log.filter((e) => e.op === 'get').length === readsBefore,
        'with no storage read at all');

    // A NEW id set is probed once, in one batch.
    const second = A.identify(pngBytes('two'), 'two.png');
    env.store().dispatch({ type: 'asset/add', assetId: second.assetId,
        record: recordFor(A, second, { bytes: 2048 }) });
    env.store().dispatch({ type: 'embed/setMedia', embedId: 'emb-1', slot: 'thumbnail',
        value: uploadRef(second, { bytes: 2048 }) });
    await env.until(() => env.facts().ids.length === 2, 3000);
    assert(env.probes() === probesBefore + 1 && env.facts().ids.length === 2,
        'a second file is probed once, and the facts now describe both ids',
        JSON.stringify(env.facts().ids));
    assert(env.facts().states[second.assetId] === 'bytes-missing',
        'the new file has no bytes yet, and the facts say exactly that',
        JSON.stringify(env.facts().states));
    assert(eq(codes(env.issues()), ['assets.bytes-missing']),
        'so the strip reports it', codes(env.issues()).join(','));
    assert(env.strip().hidden === false && env.stripText().indexOf('no longer stored') !== -1,
        'with the wording the validator wrote', env.stripText());

    // Undo/redo change the set again — one probe each way, never more.
    const probesAfterLink = env.probes();
    env.store().undo();
    await env.until(() => env.facts().ids.length === 1, 3000);
    await env.settle(25);
    assert(env.probes() === probesAfterLink + 1 && env.facts().ids.length === 1,
        'undo changes the referenced set, so it is probed again',
        env.facts().ids.length + ' ids / ' + env.probes() + ' probes');
    env.store().redo();
    await env.until(() => env.facts().ids.length === 2, 3000);
    await env.settle(25);
    assert(env.probes() === probesAfterLink + 2 && env.facts().ids.length === 2,
        'and redo probes the set it restored',
        env.facts().ids.length + ' ids / ' + env.probes() + ' probes');

    // A slot that points nowhere is a document problem, not a bytes problem.
    env.store().dispatch({ type: 'embed/setMedia', embedId: 'emb-1', slot: 'image',
        value: { kind: 'upload', filename: 'one.png' } });
    await env.until(() => codes(env.issues()).indexOf('assets.unlinked') !== -1, 3000);
    assert(codes(env.issues()).indexOf('assets.unlinked') !== -1,
        'an unlinked slot is reported without asking the byte store anything',
        codes(env.issues()).join(','));
    assert(env.probes() <= probesAfterLink + 3,
        'and the id set the unlinked document references is still covered by ONE probe',
        String(env.probes()));

    // Nothing on the page ever writes or deletes a byte.
    const final = env.stats();
    assert(final.writes === 0 && final.deletes === 0 && final.mints === 0 && final.revokes === 0,
        'after edits, undo, redo and validation the byte store is still untouched',
        JSON.stringify(final));
    assert(final.reads >= env.probes(),
        'the only byte work the page did was reading, per probe',
        JSON.stringify(final));
    const unlinkedRecord = env.store().getDocument().assets[second.assetId];
    assert(!!unlinkedRecord, 'and a record whose slot was unlinked is still in the document (no pruning)');
}

// ═══════════════════════════════════════════════════════════════
section('H. reload and restore over the same database (metadata, bytes and tamper)');
// ═══════════════════════════════════════════════════════════════
async function sectionH() {
    // 1. A clean reload: the document, the facts and the verdicts all come back.
    const first = await rig({});
    const docId = first.inst.assetFacts.ids[0];
    const before = JSON.stringify(first.store().getDocument());
    const beforeHash = first.NERO.embed.model.hashDocument(first.store().getDocument());
    const shared = first.idb;
    await first.unmount();
    assert(first.inst.destroyed === true && first.inst.assetStore.mode().dead === true,
        'teardown destroys the byte store with the page');
    assert(first.inst.assetStore.outstanding().length === 0,
        'and leaves no object URL behind');

    const second = makeEnv();
    second.useIdb(shared);
    await second.mount({});
    assert(second.probes() === 1, 'a reload probes once', String(second.probes()));
    assert(JSON.stringify(second.store().getDocument()) === before &&
           second.NERO.embed.model.hashDocument(second.store().getDocument()) === beforeHash,
        'and the restored document is byte-identical to the saved one');
    assert(second.facts().states[docId] === 'bytes-local',
        'with the same fact about the bytes', JSON.stringify(second.facts().states));
    assert(eq(codes(second.issues()), []), 'and the same (clean) verdict', codes(second.issues()).join(','));

    // 2. A record whose bytes are gone: an explicit miss, never silence.
    const missing = await rig({ withBytes: false });
    await missing.until(() => codes(missing.issues()).length > 0, 3000);
    assert(eq(codes(missing.issues()), ['assets.bytes-missing']) &&
           missing.facts().states[missing.inst.assetFacts.ids[0]] === 'bytes-missing',
        'a record with no stored bytes reports the explicit miss',
        codes(missing.issues()).join(','));
    await missing.unmount();

    // 3. Tamper cases, all seeded: these are what a browser that lost a write
    //    (or a hand-edited database) looks like on the next load.
    const tampered = makeEnv();
    const A = tampered.NERO.embed.assets;
    const F = fixture(A);
    const seed = pageSeed(tampered, {});
    seed.seed[NS].assets[F.png.assetId].bytes = Array.prototype.slice.call(F.pngFile).slice(0, 4);
    tampered.installIdb({ seed: seed.seed });
    await tampered.mount({});
    await tampered.until(() => codes(tampered.issues()).length > 0, 3000);
    assert(eq(codes(tampered.issues()), ['assets.bytes-corrupt']) &&
           tampered.issues()[0].severity === 'error',
        'a stored entry that does not match its own length is corrupt, and says so',
        codes(tampered.issues()).join(','));
    await tampered.unmount();

    // 4. A record that describes a different file than the one stored: caught
    //    without hashing anything (the store keeps the digest next to the bytes).
    const wrongFile = makeEnv();
    const seed2 = pageSeed(wrongFile, { recordBytes: 4 });
    wrongFile.installIdb({ seed: seed2.seed });
    await wrongFile.mount({});
    await wrongFile.until(() => codes(wrongFile.issues()).length > 0, 3000);
    assert(eq(codes(wrongFile.issues()), ['assets.bytes-mismatch']) &&
           wrongFile.issues()[0].severity === 'error',
        'stored bytes that are not the file the record describes are an error',
        codes(wrongFile.issues()).join(','));
    await wrongFile.unmount();
}

// ═══════════════════════════════════════════════════════════════
section('I. degraded storage: "could not check" is never "not there"');
// ═══════════════════════════════════════════════════════════════
async function sectionI() {
    const env = await rig({ noIdb: true });
    const A = env.NERO.embed.assets;
    const M = env.NERO.embed.model;
    assert(env.inst.assetStore.mode().available === false &&
           env.inst.assetStore.mode().mode === 'memory',
        'with no database the byte store runs in memory mode',
        JSON.stringify(env.inst.assetStore.mode()));
    assert(env.notice().indexOf('Draft storage is unavailable') !== -1,
        'and the page says so once, through its one status surface', env.notice());
    assert(env.probes() === 0 && env.inst.assetFacts.ids.length === 0,
        'a page with no document yet has nothing to probe');

    // The document lives in memory: link a file nobody has bytes for.
    const ident = A.identify(pngBytes('degraded'), 'degraded.png');
    env.store().dispatch({ type: 'asset/add', assetId: ident.assetId,
        record: recordFor(A, ident, { bytes: 12, availability: 'bytes-missing' }) });
    env.store().dispatch({ type: 'embed/setMedia', embedId: env.store().getDocument().embeds[0].id,
        slot: 'image', value: uploadRef(ident, { bytes: 12 }) });
    await env.until(() => env.probes() >= 1, 3000);
    assert(env.probes() === 1, 'linking one file probes once', String(env.probes()));
    assert(env.facts().states[ident.assetId] === 'bytes-unavailable',
        'and the answer is "unavailable", never "missing"',
        JSON.stringify(env.facts().states));
    assert(eq(codes(env.issues()), ['assets.bytes-unavailable']),
        'so the message is the honest one', codes(env.issues()).join(','));
    assert(env.issues()[0].message.indexOf('could not be checked') !== -1,
        'naming storage as the reason, not the file', env.issues()[0].message);
    assert(env.stats().writes === 0 && env.stats().deletes === 0,
        'and nothing was written to a storage that is not there');

    // A file the SESSION put in memory is here, and counts as here.
    const memoryFile = A.identify(pngBytes('memory'), 'memory.png');
    const put = await env.inst.assetStore.putBytes(memoryFile.assetId, pngBytes('memory'), { mime: 'image/png' });
    assert(put.ok === true && put.persisted === false && put.source === 'memory',
        'a byte store in memory mode reports the write as session-only',
        JSON.stringify(put));
    env.store().dispatch({ type: 'asset/add', assetId: memoryFile.assetId,
        record: recordFor(A, memoryFile, { bytes: pngBytes('memory').length }) });
    env.store().dispatch({ type: 'embed/setMedia', embedId: env.store().getDocument().embeds[0].id,
        slot: 'thumbnail', value: uploadRef(memoryFile, { bytes: pngBytes('memory').length }) });
    await env.until(() => env.facts().states[memoryFile.assetId] === 'bytes-local', 3000);
    assert(env.facts().states[memoryFile.assetId] === 'bytes-local',
        'and the facts find it (a file this session made IS here)',
        JSON.stringify(env.facts().states));
    assert(env.issues().every((i) => i.path.indexOf(memoryFile.assetId) === -1),
        'so no message is reported at the memory file\u2019s own slot',
        JSON.stringify(env.issues().map((i) => i.code + '@' + i.path)));
    assert(env.facts().states[ident.assetId] === 'bytes-unavailable' &&
           env.facts().states[memoryFile.assetId] === 'bytes-local',
        'while the file nobody has bytes for is still reported as uncheckable, not as gone',
        JSON.stringify(env.facts().states));

    // Teardown is clean, and a destroyed store refuses instead of pretending.
    const store = env.inst.assetStore;
    await env.unmount();
    assert(store.mode().dead === true, 'teardown dies the byte store');
    const afterDeath = await store.survey([memoryFile.assetId]);
    assert(afterDeath.length === 1 && afterDeath[0].present === false && afterDeath[0].reason === 'destroyed',
        'and a probe after teardown refuses instead of serving from a dead instance',
        JSON.stringify(afterDeath));
    assert(A.factState(afterDeath[0]) === 'bytes-unavailable',
        'which the facts read as unavailable (never as a silent miss)');
}

// ═══════════════════════════════════════════════════════════════
section('J. retention: pure, deterministic, fail-closed — and applied by nobody');
// ═══════════════════════════════════════════════════════════════
async function sectionJ() {
    const realm = bareRealm([SOURCE.model, SOURCE.assets, SOURCE.store, SOURCE.validate, SOURCE.drafts]);
    const A = realm.NERO.embed.assets;
    const M = realm.NERO.embed.model;
    const F = fixture(A);
    const other = A.identify(gifBytes('other'), 'other.gif');

    let current = M.setDocumentAsset(blankDoc({ id: 'doc-current' }), F.png.assetId, recordFor(A, F.png));
    current = M.setMedia(current, 'emb-1', 'image', uploadRef(F.png));
    let undone = M.setDocumentAsset(blankDoc({ id: 'doc-undo' }), other.assetId, recordFor(A, other));
    undone = M.setMedia(undone, 'emb-1', 'thumbnail', uploadRef(other));
    const saved = M.normalizeDocument({});
    const records = {};
    records[F.png.assetId] = recordFor(A, F.png);
    records[other.assetId] = recordFor(A, other);
    records.a_stranger = recordFor(A, F.gif);

    const closed = A.retention([], { records: records });
    assert(closed.ok === false && closed.reason === 'no-documents' && eq(closed.keep, []) && closed.plan === null,
        'with no documents it refuses to name a single orphan (fail closed)',
        JSON.stringify({ ok: closed.ok, reason: closed.reason, keep: closed.keep }));

    const plan = A.retention([current, saved, undone], { records: records });
    assert(plan.ok === true && eq(plan.keep, [other.assetId, F.png.assetId].sort()),
        'the keep-set is every id any of the given documents still references',
        JSON.stringify(plan.keep));
    assert(eq(plan.orphans, ['a_stranger']) && eq(plan.plan.pruned, ['a_stranger']),
        'and only the records nothing points at are orphans (the saved snapshot counts as a document)',
        JSON.stringify(plan.orphans));
    assert(eq(plan.plan.kept, [other.assetId, F.png.assetId].sort()),
        'the plan keeps exactly the referenced ids', JSON.stringify(plan.plan.kept));

    const withSession = A.retention([current], { records: records, sessionIds: ['a_just_uploaded', 'a_stranger'] });
    assert(withSession.plan.pruned.indexOf('a_just_uploaded') === -1 &&
           withSession.keep.indexOf('a_just_uploaded') !== -1,
        'a file this session made is never prunable, however the documents read',
        JSON.stringify(withSession.keep));
    const sessionOnly = A.retention([current],
        { records: { a_just_uploaded: recordFor(A, F.gif) }, sessionIds: ['a_just_uploaded'] });
    assert(eq(sessionOnly.orphans, []) && eq(sessionOnly.plan.pruned, []),
        'even when no document references it yet (the upload is either there or it is not)');

    const again = A.retention([current, saved, undone], { records: records });
    assert(JSON.stringify(again) === JSON.stringify(plan), 'and the whole analysis is deterministic');
    assert(eq(Object.keys(records), [F.png.assetId, other.assetId, 'a_stranger']),
        'nothing was removed from the caller\u2019s map (this function subtracts, it does not delete)');
    const sessionKeep = A.retention([current], { records: records, sessionIds: ['a_stranger'] });
    assert(eq(sessionKeep.plan.pruned, [other.assetId]) && sessionKeep.keep.indexOf('a_stranger') !== -1,
        'and a record the session owns is kept even though no document here references it',
        JSON.stringify({ keep: sessionKeep.keep, pruned: sessionKeep.plan.pruned }));

    // Nobody applies the plan: the page keeps metadata for a slot it no longer
    // has, and the byte store is never asked to delete anything.
    const env = await rig({});
    const jid = env.inst.assetFacts.ids[0];
    env.store().dispatch({ type: 'embed/setMedia', embedId: 'emb-1', slot: 'image', value: null });
    await env.until(() => env.store().getDocument().embeds[0].image === null, 500);
    await env.settle(40);
    assert(env.store().getDocument().assets[jid] !== undefined,
        'clearing the slot leaves the record in the document (7c prunes nothing)');
    assert(env.stats().deletes === 0,
        'and the byte store was never asked to delete it', JSON.stringify(env.stats()));
    assert(env.inst.assetStore.remove !== undefined && typeof env.inst.assetStore.remove === 'function',
        'while remove() stays available as the explicit primitive it always was');
    await env.unmount();
}

// ═══════════════════════════════════════════════════════════════
section('K. performance: keystrokes stay byte-free, and the rules stay cheap');
// ═══════════════════════════════════════════════════════════════
async function sectionK() {
    const env = await rig({});
    const A = env.NERO.embed.assets;
    const readsBefore = env.idb.log.filter((e) => e.op === 'get').length;
    const probesBefore = env.probes();
    const passesBefore = env.passes();
    const timings = [];
    for (let i = 1; i <= 20; i++) {
        const t0 = Date.now();
        env.store().dispatch({ type: 'content/set', text: rep('x', i) });
        timings.push(Date.now() - t0);
    }
    await env.until(() => env.passes() > passesBefore, 1000);
    await env.settle(40);
    timings.sort((a, b) => a - b);
    const p95 = timings[Math.max(0, Math.ceil(timings.length * 0.95) - 1)];
    assert(env.probes() === probesBefore, 'twenty keystrokes probe zero times', String(env.probes()));
    assert(env.passes() === passesBefore + 1,
        'and cost exactly ONE validation pass (burst collapse)', String(env.passes() - passesBefore));
    assert(env.idb.log.filter((e) => e.op === 'get').length === readsBefore,
        'with zero storage reads');
    assert(p95 <= 16, 'keystroke p95 is inside the 16 ms bar', p95 + ' ms');
    console.log('    keystrokes: p95 ' + p95 + ' ms, worst ' + timings[timings.length - 1] +
        ' ms (20 dispatches)');

    // The validator over a ten-asset document, measured the way the page pays.
    const M = env.NERO.embed.model;
    let doc = blankDoc({ id: 'doc-perf' });
    const ids = [];
    for (let i = 0; i < 10; i++) {
        const ident = A.identify(pngBytes('perf-' + i), 'perf-' + i + '.png');
        ids.push(ident.assetId);
        doc = M.setDocumentAsset(doc, ident.assetId, recordFor(A, ident, { bytes: 4096 }));
        if (i) doc = M.addEmbed(doc);
        doc = M.setMedia(doc, doc.embeds[i].id, 'image', uploadRef(ident, { bytes: 4096 }));
    }
    const rows = ids.map((assetId, i) => ({
        assetId: assetId, present: true, availability: 'bytes-local', source: 'indexeddb',
        byteLength: 4096, sha256: rep('a', 64), mime: 'image/png', reason: null,
    }));
    const facts = A.assetFacts(doc, rows);
    const runs = [];
    for (let i = 0; i < 40; i++) {
        const t0 = Date.now();
        env.NERO.embed.validate.validate(doc, env.limits, facts);
        runs.push(Date.now() - t0);
    }
    runs.sort((a, b) => a - b);
    const avg = runs.reduce((a, b) => a + b, 0) / runs.length;
    const p95v = runs[Math.ceil(runs.length * 0.95) - 1];
    assert(avg < 20 && p95v < 20,
        'the asset rules cost well under 20 ms on a ten-file document',
        'avg ' + avg.toFixed(2) + ' ms, p95 ' + p95v + ' ms');
    const viewMs = (() => {
        const t0 = Date.now();
        for (let i = 0; i < 40; i++) A.assetBytes(A.assetView(doc));
        return (Date.now() - t0) / 40;
    })();
    assert(viewMs < 5, 'and the view/byte arithmetic under 5 ms', viewMs.toFixed(2) + ' ms');
    const history = [doc].concat(new Array(59).fill(doc));
    const t1 = Date.now();
    for (let i = 0; i < 40; i++) A.retention(history, { records: doc.assets, sessionIds: ids });
    const retentionMs = (Date.now() - t1) / 40;
    assert(retentionMs < 20, 'and a retention pass over 60 documents under 20 ms', retentionMs.toFixed(2) + ' ms');
    console.log('    rules: validate avg ' + avg.toFixed(2) + ' ms / p95 ' + p95v +
        ' ms, view ' + viewMs.toFixed(2) + ' ms, retention ' + retentionMs.toFixed(2) + ' ms');
    await env.unmount();
}

// ═══════════════════════════════════════════════════════════════
section('L. two realms agree, and a destroyed page stays dead');
// ═══════════════════════════════════════════════════════════════
async function sectionL() {
    // Two independently loaded realms, same document, same facts, same answer.
    const build = (realm) => {
        const A = realm.NERO.embed.assets;
        const M = realm.NERO.embed.model;
        const F = fixture(A);
        let doc = M.setDocumentAsset(blankDoc(), F.png.assetId, recordFor(A, F.png, { bytes: 999 }));
        doc = M.setMedia(doc, 'emb-1', 'image', uploadRef(F.png, { bytes: 999 }));
        doc = M.setDocumentAsset(doc, F.gif.assetId, recordFor(A, F.gif, { filename: 'one.png' }));
        doc = M.addEmbed(doc);
        doc = M.setMedia(doc, doc.embeds[1].id, 'image', uploadRef(F.gif, { filename: 'one.png' }));
        const facts = A.assetFacts(doc, [
            { assetId: F.png.assetId, present: false, reason: 'missing' },
            { assetId: F.gif.assetId, present: true, byteLength: 2048, sha256: F.gif.sha256, mime: 'image/gif', reason: null },
        ]);
        const issues = realm.NERO.embed.validate.validate(doc, servedLimits(), facts);
        return { codes: codes(issues), signature: realm.NERO.embed.validate.signature(issues), facts: facts.states };
    };
    const one = build(validateRealm());
    const two = build(validateRealm());
    assert(JSON.stringify(one) === JSON.stringify(two),
        'two realms loaded from the same sources produce the same issues, signature and facts',
        JSON.stringify(one.codes));
    assert(one.codes.length >= 3 && one.codes.indexOf('assets.filename-clash') !== -1 &&
           one.codes.indexOf('assets.bytes-missing') !== -1 && one.codes.indexOf('assets.size-unknown') === -1,
        'and the case is not vacuous (several different rules fired)', one.codes.join(','));

    // Two pages over the same database agree about the bytes.
    const first = await rig({});
    const p1 = { ids: first.inst.assetFacts.ids, states: first.inst.assetFacts.states, issues: codes(first.issues()) };
    const shared = first.idb;
    await first.unmount();
    const second = makeEnv();
    second.useIdb(shared);
    await second.mount({});
    const p2 = { ids: second.inst.assetFacts.ids, states: second.inst.assetFacts.states, issues: codes(second.issues()) };
    assert(JSON.stringify(p1) === JSON.stringify(p2),
        'and two pages over one database agree about the bytes and the verdict',
        JSON.stringify(p1));

    // Lifecycle: destroyed means dead, twice is safe, and nothing runs after.
    const env = await rig({});
    const passesAtDeath = env.passes();
    assert(await env.unmount() === true && env.inst.destroyed === true,
        'unmount destroys the page instance');
    assert(await env.unmount() === false, 'and unmounting again is a no-op, not an error');
    await env.settle(60);
    assert(env.passes() === passesAtDeath, 'no validation pass survives teardown', String(env.passes()));
    assert(env.inst.assetStore.mode().dead === true && env.inst.assetStore.outstanding().length === 0,
        'and the byte store is dead with nothing outstanding');
    const deadRows = await env.inst.assetStore.survey(env.inst.assetFacts.ids);
    const deadFacts = env.NERO.embed.assets.assetFacts(env.store().getDocument(), deadRows);
    assert(deadRows.every((row) => row.present === false && row.reason === 'destroyed') &&
           env.NERO.embed.assets.factState(deadRows[0]) === 'bytes-unavailable',
        'a probe after death refuses, and reads as "could not tell" — never as "gone"',
        JSON.stringify(deadRows));
}

async function main() {
    await sectionG();
    await sectionH();
    await sectionI();
    await sectionJ();
    await sectionK();
    await sectionL();
    console.log('\nmessage-builder asset integration: ' + pass + ' passed, ' + fail + ' failed');
    if (fail) {
        console.log('FAILURES:');
        failures.forEach((f) => console.log('  - ' + f));
    }
    process.exit(fail ? 1 : 0);
}

main().then(null, function (err) {
    console.error('\nHARNESS ERROR (a crash, not an assertion failure):');
    console.error((err && err.stack) || err);
    process.exit(1);
});
