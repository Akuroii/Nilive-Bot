#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════
   Message Builder v2 — phase 2, step 7d: choosing a LOCAL FILE.

   WHAT THIS HAS TO PROVE (step 7d scope: the four media slots can take a
   local file from the user, and give it back — client-side only)

     A. THE CONTROL. Four labelled, keyboard-operable file inputs (one per
        media slot), each with a state line wired by aria-describedby, a Remove
        button that exists only while a file is attached, and no live region of
        its own. Its `accept` is the asset module's own table — a picker hint,
        never the decision.
     B. ONE PICK = ONE RECORD + ONE REFERENCE + ONE UNDO STEP. The bytes go in
        through the byte store, the record is built by the module that owns the
        shape, the record lands BEFORE the reference, and the whole pick is a
        single history entry.
     C. UNDO/REDO ARE BYTE-SAFE. Undo takes the reference and the record away
        and leaves the bytes; redo brings both back byte-for-byte; nothing ever
        deletes or revokes anything.
     D. EVERY REFUSAL LEAVES THE DOCUMENT ALONE. A file the identity rules
        refuse, a failed read, no file at all, a refused put: no document
        change, one honest sentence, and no history entry.
     E. ONE FILE, TWO SLOTS. Same bytes → same id → ONE record; removing one
        reference keeps the record, removing the last one drops the record and
        keeps the bytes.
     F. DEGRADED STORAGE IS THE STORE'S OWN ANSWER. Session-only bytes are
        reported as session-only (never as saved), work in-session, and are
        reported as missing by the validator after a reload.
     G. TYPING COSTS NOTHING. A keystroke burst reads no file, stores nothing,
        probes nothing; only a real pick enters the pipeline.
     H. ACCESSIBILITY IS THE VALIDATOR'S. The state line shows the validator's
        OWN sentence, aria-invalid follows errors only, and the page keeps its
        two live regions — no third.
     I. RACES AND TEARDOWN. A newer pick supersedes an older read, a slot that
        changed mid-read is refused, and a destroyed page mutates nothing.

   Run:  node scripts/test_message_builder_asset_upload.js
   ═══════════════════════════════════════════════════════════════ */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { createDom, createFakeIdb, createWindow, parseTemplate, findById, materialize } = require('./support/dom_stub.js');

let pass = 0, fail = 0; const failures = [];
function assert(cond, name, extra) {
    if (cond) { pass++; console.log('  PASS', name); }
    else { fail++; failures.push(name + (extra ? ' — ' + extra : '')); console.log('  FAIL', name, extra || ''); }
}
function section(t) { console.log('\n== ' + t + ' =='); }
function eq(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

const ROOT = path.join(__dirname, '..');
const js = (...parts) => path.join(ROOT, 'dashboard', 'static', 'js', ...parts);
// Source hooks: the mutation battery points these at deliberately broken copies
// of the modules this harness judges (see scripts/support/mb_mutants.js).
const SOURCE = {
    model: process.env.NERO_MODEL_SRC || js('embed', 'model.js'),
    assets: process.env.NERO_ASSETS_SRC || js('embed', 'assets.js'),
    assetStore: process.env.NERO_ASSET_STORE_SRC || js('embed', 'asset-store.js'),
    store: process.env.NERO_STORE_SRC || js('embed', 'store.js'),
    validate: process.env.NERO_VALIDATE_SRC || js('embed', 'validate.js'),
    drafts: process.env.NERO_DRAFTS_SRC || js('embed', 'drafts.js'),
    inspector: process.env.NERO_INSPECTOR_SRC || js('embed', 'views', 'inspector.js'),
    page: process.env.NERO_MB_PAGE_SRC || js('embed', 'message-builder-page.js'),
};
const readSource = (file) => fs.readFileSync(file, 'utf8');
const PAGE_SRC = readSource(SOURCE.page);
const INSPECTOR_SRC = readSource(SOURCE.inspector);
const TEMPLATE_TREE = parseTemplate(readSource(
    path.join(ROOT, 'dashboard', 'templates', 'manage', 'message_builder.html')));

const GUILD = '1111222233334444';
const NS = 'nero_message_builder';

// ── The realm's globals (a vm has none of its own) ──
const REALM_GLOBALS = ['Promise', 'Object', 'Array', 'Math', 'Date', 'JSON', 'Number', 'String',
    'RegExp', 'Error', 'TypeError', 'Set', 'Map', 'Symbol', 'isFinite', 'parseInt', 'parseFloat',
    'encodeURIComponent', 'decodeURIComponent', 'Uint8Array', 'ArrayBuffer'];

/** The served limits the page reads (mirrors utils/discord_limits.py). */
function servedLimits() {
    return {
        message: { content_max: 2000, embeds_max: 10, embed_total_chars_max: 6000, request_bytes_max: 26214400 },
        attachments: {
            count_max: 10, total_bytes_max: 26148864,
            file_bytes_advisory: 20971520, file_advisory_is_hard: false,
        },
        embed: {
            title_max: 256, description_max: 4096, fields_max: 25, field_name_max: 256,
            field_value_max: 1024, footer_text_max: 2048, author_name_max: 256,
        },
    };
}

// ── byte fixtures (real headers: assets.identify() sniffs them) ──
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
function textBytes(text) {
    const out = [];
    for (let i = 0; i < String(text).length; i++) out.push(String(text).charCodeAt(i) & 0xff);
    return bytesFrom(out);
}
/** A File, as far as this pipeline is concerned: a name and bytes. */
function fileFor(name, bytes, extra) {
    return Object.assign({
        name: name, size: bytes.length, type: '',
        __bytes: bytes.length ? new Uint8Array(bytes) : new Uint8Array(0),
    }, extra || {});
}

// ── the rig ──
function makeEnv() {
    const env = { reads: [], held: [], actions: [] };
    const dom = createDom();
    const win = createWindow();
    win.document = dom.document;
    win.__BOT_IDENTITY__ = { name: 'Nero', avatar: null };
    // 7e: this rig is a browser that can turn stored bytes into a picture.
    // The resolution itself is proved in test_message_builder_asset_resolution.js;
    // here the capability only has to exist so the pick path below is judged in a
    // normal browser instead of in a degraded one.
    let mintSeq = 0;
    win.Blob = function Blob(parts, opts) { this.parts = parts; this.type = opts && opts.type; };
    win.URL = {
        createObjectURL(blob) {
            mintSeq += 1;
            return 'blob:upload-rig/' + mintSeq + (blob && blob.type ? '#' + blob.type : '');
        },
        revokeObjectURL() {},
    };
    const net = { calls: 0 };
    const sandbox = {
        window: win, document: dom.document, console: console,
        setTimeout: setTimeout, clearTimeout: clearTimeout,
        setInterval: setInterval, clearInterval: clearInterval,
        fetch: function () { net.calls++; return Promise.reject(new Error('the page must not touch the network')); },
        navigator: { clipboard: { writeText: () => Promise.resolve() } },
    };
    REALM_GLOBALS.forEach((name) => { sandbox[name] = global[name]; });
    vm.createContext(sandbox);

    // The realm's FileReader. Reads land on a timer (like the real one), and
    // `holdReads` keeps them in flight so the races below are deterministic
    // rather than a guess about timing.
    function FileReader() {
        const self = this;
        self.result = null;
        self.onload = null;
        self.onerror = null;
        self.readAsArrayBuffer = function (file) {
            env.reads.push(file && file.name);
            const deliver = function () {
                if (file && file.__fail) { if (self.onerror) self.onerror({ target: self }); return; }
                self.result = file && file.__bytes ? file.__bytes : null;
                if (self.onload) self.onload({ target: self });
            };
            if (env.holdReads) { env.held.push(deliver); return; }
            setTimeout(deliver, 0);
        };
    }
    win.FileReader = FileReader;
    sandbox.FileReader = FileReader;

    [js('nav-lifecycle.js'), SOURCE.model, SOURCE.assets, SOURCE.assetStore, SOURCE.store,
     SOURCE.validate, js('embed', 'discord-markdown.js'), js('embed', 'preview.js'), SOURCE.drafts,
     js('embed', 'views', 'statusbar.js'), js('embed', 'views', 'rail.js'), SOURCE.inspector,
     js('embed', 'views', 'actionbar.js'), SOURCE.page]
        .forEach((file) => vm.runInContext(readSource(file), sandbox, { filename: path.basename(file) }));

    env.win = win;
    env.sandbox = sandbox;
    env.dom = dom;
    env.net = net;
    env.NERO = win.NERO;
    env.limits = servedLimits();
    env.idb = null;
    env.root = null;

    env.useIdb = function (spec) {
        const idb = createFakeIdb(spec || {});
        env.idb = idb;
        sandbox.indexedDB = idb;
        win.indexedDB = idb;
        env.NERO.indexedDB = idb;
        return idb;
    };
    env.el = (id) => dom.document.getElementById(id);
    env.settle = (ms) => new Promise((resolve) => setTimeout(resolve, ms == null ? 20 : ms));
    env.until = async function (cond, ms) {
        const deadline = Date.now() + (ms == null ? 3000 : ms);
        while (Date.now() < deadline) {
            if (cond()) return true;
            await env.settle(5);
        }
        return !!cond();
    };
    env.mount = async function (opts) {
        const options = opts || {};
        const root = materialize(findById(TEMPLATE_TREE, 'mb2-root'), dom.document);
        root.setAttribute('data-guild-id', String(GUILD));
        root.setAttribute('data-limits', JSON.stringify(options.limits || env.limits));
        dom.attach(root);
        env.root = root;
        await env.NERO.lifecycle.mount(dom.document);
        await env.settle(options.settleMs == null ? 25 : options.settleMs);
        env.inst = env.NERO.embed.messageBuilderPage.current();
        env.actionLog = [];
        env.store().subscribe(function (state, action) {
            env.actions.push(action && action.type);
            env.actionLog.push(action);
        });
        env.removals = (assetId) => env.actionLog
            .filter((a) => a && a.type === 'asset/remove' && String(a.assetId) === String(assetId));
        env.showEmbed();
        return env.inst;
    };
    env.unmount = () => env.NERO.lifecycle.unmount('test');
    env.page = () => env.NERO.embed.messageBuilderPage.current();
    env.store = () => env.inst && env.inst.store;
    env.doc = () => env.store().getDocument();
    env.hash = () => env.NERO.embed.model.hashDocument(env.doc());
    env.issues = () => env.store().getUi().issues || [];
    env.counters = () => (env.inst.ctx && env.inst.ctx.counters) || {};
    env.stats = () => env.inst.assetStore.stats();
    env.notice = () => {
        const status = env.el('mb2-bar-status');
        if (!status) return '';
        const found = status.children.find((c) => (c.className || '').split(/\s+/).indexOf('mb2-status-notice') !== -1);
        return found ? found.textContent : '';
    };
    env.stripText = () => { const s = env.el('mb2-strip'); return s ? s.textContent : ''; };

    // ── the control, found the way a user finds it ──
    env.inspectorBody = () => env.el('mb2-inspector-body');
    env.descendants = function (node, out) {
        out = out || [];
        (node && node.children || []).forEach(function (child) { out.push(child); env.descendants(child, out); });
        return out;
    };
    env.byAttr = (attr) => env.descendants(env.inspectorBody())
        .filter((n) => typeof n.getAttribute === 'function' && n.getAttribute(attr) !== null);
    env.input = (key) => env.byAttr('data-insp-upload')
        .filter((n) => n.getAttribute('data-insp-upload') === key)[0] || null;
    env.state = (key) => env.byAttr('data-insp-filestate')
        .filter((n) => n.getAttribute('data-insp-filestate') === key)[0] || null;
    env.removeBtn = (key) => env.byAttr('data-insp-remove')
        .filter((n) => n.getAttribute('data-insp-remove') === key)[0] || null;
    env.showEmbed = function () {
        const embed = env.doc().embeds[0];
        env.store().dispatch({ type: 'ui/selectNode', nodeId: embed.id });
    };
    /** Choose a file the way the browser does: files first, then one change. */
    env.pick = function (key, file) {
        const input = env.input(key);
        if (!input) throw new Error('no upload control for ' + key);
        input.files = file ? [file] : [];
        env.inspectorBody().dispatch('change', { type: 'change', target: input });
        return input;
    };
    env.clickRemove = function (key) {
        const btn = env.removeBtn(key);
        if (!btn) throw new Error('no remove button for ' + key);
        env.inspectorBody().dispatch('click', { type: 'click', target: btn });
        return btn;
    };
    /** Let every held read land. */
    env.releaseReads = function () { env.held.splice(0).forEach((fn) => fn()); };
    /** Wait until the doc, the probe and the debounced pass all agree. */
    env.settled = async function (ms) {
        const A = env.NERO.embed.assets;
        const ok = await env.until(function () {
            if (!env.inst || !env.store()) return false;
            const ids = A.documentAssetIds(env.doc()).join(',');
            const facts = env.inst.assetFacts && env.inst.assetFacts.ids ? env.inst.assetFacts.ids.join(',') : null;
            const inFlight = env.inst.uploadToken !== null;
            const inst = env.inst;
            return ids === facts && !inst.assetProbe && inst.validateTimer === null && !inFlight;
        }, ms == null ? 3000 : ms);
        await env.settle(10);
        return ok;
    };
    /**
     * A pick is over when its document change has landed (or its refusal has
     * been announced) AND the in-flight token is back to null. Asserting before
     * that is asserting on a race, not on behaviour — this is the await that
     * makes the sections below deterministic.
     */
    env.awaitPick = function (cond, ms) {
        return env.until(function () { return !!cond() && env.inst.uploadToken === null; }, ms == null ? 3000 : ms);
    };
    /** The four slot values, read the way the document holds them. */
    env.slotValue = function (key) {
        const embed = env.doc().embeds[0];
        if (key === 'media.image') return embed.image;
        if (key === 'media.thumbnail') return embed.thumbnail;
        if (key === 'author.icon') return embed.author ? embed.author.icon : null;
        return embed.footer ? embed.footer.icon : null;
    };
    env.record = function (assetId) { return env.doc().assets[String(assetId)] || null; };
    /** Images the preview has actually rendered (7d expects none for an upload). */
    env.imgsInPreview = function () {
        const mount = env.el('mb2-mount');
        return env.descendants(mount).filter((n) => n.tagName === 'IMG').length;
    };
    env.uploadCount = () => env.counters().uploads || 0;
    env.probes = () => env.counters().assetProbes || 0;
    return env;
}

async function main() {
    // ═══════════════════════════════════════════════════════════════
    section('A. the control: four labelled file inputs, and nothing that lies');
    // ═══════════════════════════════════════════════════════════════
    {
        const env = makeEnv();
        env.useIdb();
        await env.mount();
        const A = env.NERO.embed.assets;
        const KEYS = ['media.image', 'media.thumbnail', 'author.icon', 'footer.icon'];

        assert(KEYS.every((k) => !!env.input(k)), 'every media slot has a file control',
            KEYS.filter((k) => !env.input(k)).join(','));
        assert(KEYS.every((k) => !!env.state(k)), 'and a state line');
        assert(KEYS.every((k) => !!env.removeBtn(k)), 'and a Remove button');
        assert(env.byAttr('data-insp-upload').length === 4, 'exactly four file inputs (no more)',
            String(env.byAttr('data-insp-upload').length));

        const wanted = A.ALLOWED_EXTENSIONS.map((e) => '.' + e).join(',');
        assert(KEYS.every((k) => env.input(k).getAttribute('accept') === wanted),
            'accept is the asset module\\u2019s own table, and only a hint',
            env.input('media.image').getAttribute('accept'));
        assert(env.input('media.image').getAttribute('accept') === '.gif,.jpeg,.jpg,.png,.webp',
            'which is the documented list (a second allow-list would show up here)');

        assert(KEYS.every((k) => env.input(k).getAttribute('data-insp') === null),
            'a file input is never routed through the URL path (its value is not a URL)');
        assert(KEYS.every((k) => env.input(k).getAttribute('type') === 'file'),
            'and it is a real file input');

        // Labels: a real <label for>, so clicking the text opens the picker.
        const labelled = KEYS.every(function (k) {
            const id = env.input(k).getAttribute('id');
            return env.descendants(env.inspectorBody()).some(function (n) {
                return n.tagName === 'LABEL' && n.getAttribute('for') === id;
            });
        });
        assert(labelled, 'every file input has a label pointing at it');

        // aria-describedby → the state line that is actually painted.
        const described = KEYS.every(function (k) {
            const target = env.input(k).getAttribute('aria-describedby');
            const state = env.state(k);
            return !!target && !!state && state.getAttribute('id') === target;
        });
        assert(described, 'and aria-describedby names its own state line');

        assert(KEYS.every((k) => env.state(k).textContent === 'No file attached'),
            'a slot with no file says exactly that',
            env.state('media.image').textContent);
        assert(KEYS.every((k) => env.removeBtn(k).hidden === true),
            'Remove is not shown while there is nothing to remove');
        assert(KEYS.every((k) => env.input(k).getAttribute('aria-invalid') === null),
            'and nothing is marked invalid before a rule has run');

        // The Remove control is a real button (keyboard-reachable by default).
        const remove = env.removeBtn('media.image');
        assert(remove.tagName === 'BUTTON' && remove.getAttribute('type') === 'button' &&
               !!remove.getAttribute('aria-label'),
            'Remove is a button with an accessible name');

        // No third live region: the strip and the status bar are still it.
        const live = env.descendants(env.dom.document.body || env.root)
            .filter((n) => typeof n.getAttribute === 'function' &&
                (n.getAttribute('aria-live') !== null || n.getAttribute('role') === 'alert'));
        assert(live.length === 2 && live.every((n) => ['mb2-strip', 'mb2-bar-status'].indexOf(n.getAttribute('id')) !== -1),
            'the page keeps exactly two live regions (strip + status), no third',
            live.map((n) => n.getAttribute('id')).join(','));
        assert(!/aria-live|role="alert"|role='alert'/.test(INSPECTOR_SRC),
            'and the control declares none of its own');

        env.unmount();
    }

    // ═══════════════════════════════════════════════════════════════
    section('B. one pick = one record, one reference, ONE undo step');
    // ═══════════════════════════════════════════════════════════════
    {
        const env = makeEnv();
        env.useIdb();
        await env.mount();
        const A = env.NERO.embed.assets;
        const M = env.NERO.embed.model;
        const before = env.hash();
        const depth = env.store().historyDepth().size;
        const puts = env.stats().puts;
        env.actions.length = 0;

        const file = fileFor('one.png', pngBytes('one'));
        env.pick('media.image', file);
        assert(env.reads.length === 1 && env.reads[0] === 'one.png',
            'the file is read ONCE', env.reads.join(','));
        await env.awaitPick(() => !!env.record(A.identify(file.__bytes, 'one.png').assetId));
        await env.settled();

        const ident = A.identify(file.__bytes, 'one.png');
        const doc = env.doc();
        const record = doc.assets[ident.assetId];
        assert(!!record, 'the document now carries a record for the file');
        assert(!!record && eq(Object.keys(record), A.RECORD_KEYS), 'in the canonical key order',
            Object.keys(record || {}).join(','));
        assert(!!record && record.filename === 'one.png' && record.mime === 'image/png' &&
               record.bytes === file.__bytes.length && record.availability === 'bytes-local',
            'describing the file it actually is', JSON.stringify(record));
        assert(!!record && record.sha256 === ident.sha256, 'with the digest the identity rules computed');

        const value = doc.embeds[0].image;
        assert(!!value && value.kind === 'upload' && value.assetId === ident.assetId &&
               value.filename === 'one.png' && value.mime === 'image/png' &&
               value.bytes === file.__bytes.length,
            'and the slot references it by id', JSON.stringify(value));

        assert(env.hash() !== before, 'the document changed (and is therefore dirty)');
        assert(env.store().isDirty() === true, 'the store says so');
        assert(env.store().historyDepth().size === depth + 1,
            'ONE undo step for the whole pick',
            depth + ' -> ' + env.store().historyDepth().size);
        assert(eq(env.actions, ['asset/add', 'embed/setMedia']),
            'and the record landed BEFORE the reference (never a dangling slot)',
            env.actions.join(','));

        const stored = await env.inst.assetStore.getBytes(ident.assetId);
        assert(stored.ok && stored.byteLength === file.__bytes.length,
            'the bytes are in the byte store', stored.reason);
        assert(env.stats().puts === puts + 1, 'exactly one put');
        assert(env.stats().deletes === 0 && env.stats().revokes === 0,
            'and nothing was deleted or revoked');
        assert(env.net.calls === 0, 'and nothing touched the network');

        await env.settled();
        assert(env.issues().length === 0, 'a complete, stored file validates clean',
            JSON.stringify(env.issues().map((i) => i.code)));
        // 7e: the stored file is now RESOLVED into the slot — one picture, and
        // never a broken image, because an unresolved reference renders nothing
        // at all rather than an empty <img>.
        assert(env.imgsInPreview() === 1,
            'the preview renders the stored file it was just given',
            String(env.imgsInPreview()));
        assert(env.el('mb2-mount').textContent.indexOf('broken') === -1,
            'and nothing on screen calls the slot broken');
        assert(env.uploadCount() === 1, 'the pick is counted once', String(env.uploadCount()));

        // The control now tells the truth about it.
        assert(env.state('media.image').textContent ===
               'Attached: one.png · ' + A.describeSize(file.__bytes.length) + ' · ' + A.mimeLabel('image/png'),
            'the state line describes the attached file',
            env.state('media.image').textContent);
        assert(env.removeBtn('media.image').hidden === false,
            'and Remove is now offered');
        assert(env.input('media.image').value === '',
            'the picker\u2019s own value is cleared, so the SAME file can be picked again');
        assert(env.notice() === '', 'no failure was reported', env.notice());

        // Re-picking the same file is a real event, not a silent no-op.
        const docAgain = env.store().historyDepth().size;
        env.pick('media.image', file);
        await env.until(() => env.reads.length === 2 && env.inst.uploadToken === null, 1500);
        assert(env.reads.length === 2 && env.store().historyDepth().size === docAgain,
            'choosing the same file again is read again, and is not an edit');

        env.unmount();
    }

    // ═══════════════════════════════════════════════════════════════
    section('B2. what one pick costs (measured, not asserted by feel)');
    // ═══════════════════════════════════════════════════════════════
    {
        const env = makeEnv();
        env.useIdb();
        await env.mount();
        // One megabyte of PNG: the read, the digest and the store write are the
        // whole cost of a pick, and it happens ONCE per file — never per
        // keystroke, never per render.
        const big = pngBytes('big');
        const body = new Uint8Array(1024 * 1024);
        body.set(big);
        const file = fileFor('big.png', body);
        const t0 = Date.now();
        env.pick('media.image', file);
        await env.awaitPick(() => !!env.slotValue('media.image'));
        const ms = Date.now() - t0;
        await env.settled();
        console.log('    one 1 MiB pick, end to end: ' + ms + ' ms');
        assert(ms < 2000, 'a 1 MiB pick finishes inside two seconds (stub realm)', String(ms));
        assert(env.probes() === 1 && env.uploadCount() === 1,
            'for exactly one probe and one pipeline run',
            [env.probes(), env.uploadCount()].join(','));
        assert(env.issues().length === 0, 'with a clean document at the end',
            JSON.stringify(env.issues().map((i) => i.code)));
        // And the same document typed into afterwards still costs nothing.
        const puts = env.stats().puts;
        const reads = env.reads.length;
        const title = env.descendants(env.inspectorBody())
            .filter((n) => n.getAttribute && n.getAttribute('data-insp') === 'title')[0];
        for (let i = 0; i < 10; i++) {
            title.value = 'x'.repeat(i);
            env.inspectorBody().dispatch('input', { type: 'input', target: title });
        }
        await env.settled();
        assert(env.stats().puts === puts && env.reads.length === reads,
            'and typing after an upload still costs zero reads and zero writes');
        env.unmount();
    }

    // ═══════════════════════════════════════════════════════════════
    section('C. undo and redo move the metadata, never the bytes');
    // ═══════════════════════════════════════════════════════════════
    {
        const env = makeEnv();
        env.useIdb();
        await env.mount();
        const A = env.NERO.embed.assets;
        const file = fileFor('one.png', pngBytes('undo'));
        env.pick('media.image', file);
        await env.awaitPick(() => !!env.record(A.identify(file.__bytes, 'one.png').assetId));
        await env.settled();
        const ident = A.identify(file.__bytes, 'one.png');
        const uploaded = env.hash();
        const puts = env.stats().puts;

        assert(env.store().undo() === true, 'undo works after a pick');
        await env.until(() => env.slotValue('media.image') === null);
        await env.settled();
        assert(env.doc().embeds[0].image === null, 'undo takes the reference away');
        assert(env.doc().assets[ident.assetId] === undefined, 'and the record with it');
        assert(env.state('media.image').textContent === 'No file attached',
            'the control says so', env.state('media.image').textContent);
        assert(env.removeBtn('media.image').hidden === true, 'and hides Remove again');
        const still = await env.inst.assetStore.getBytes(ident.assetId);
        assert(still.ok && still.byteLength === file.__bytes.length,
            'the BYTES are untouched by undo', still.reason);
        assert(env.stats().deletes === 0 && env.stats().puts === puts,
            'no delete, no second put');

        assert(env.store().redo() === true, 'redo works too');
        await env.until(() => !!env.slotValue('media.image'));
        await env.settled();
        assert(env.hash() === uploaded, 'and brings the document back byte-for-byte');
        assert(env.doc().assets[ident.assetId] !== undefined &&
               (env.slotValue('media.image') || {}).assetId === ident.assetId,
            'record and reference together');
        assert((await env.inst.assetStore.getBytes(ident.assetId)).ok,
            'and the same bytes are still the ones stored');

        env.unmount();
    }

    // ═══════════════════════════════════════════════════════════════
    section('D. every refusal leaves the document exactly as it was');
    // ═══════════════════════════════════════════════════════════════
    {
        const env = makeEnv();
        env.useIdb();
        await env.mount();
        const A = env.NERO.embed.assets;

        async function refuse(label, file, expectNotice, extra) {
            const before = env.hash();
            const depth = env.store().historyDepth().size;
            const puts = env.stats().puts;
            env.pick('media.image', file);
            if (extra) extra();
            await env.settled();
            // The notice is what proves the refusal FINISHED — a previous case's
            // sentence is not allowed to stand in for this one's.
            if (expectNotice) await env.until(() => env.notice() === expectNotice && env.inst.uploadToken === null, 2000);
            assert(env.hash() === before && env.store().historyDepth().size === depth &&
                   env.stats().puts === puts,
                label + ': the document, the history and the store are untouched',
                env.hash() === before ? '' : 'document changed');
            if (expectNotice) {
                assert(env.notice() === expectNotice, label + ': refused in the module\\u2019s own words',
                    env.notice());
            }
        }
        // Identify's refusals: each sentence comes from assets.js, not from here.
        const pdf = A.identify(textBytes('%PDF-1.4'), 'doc.pdf');
        await refuse('an extension an embed cannot show', fileFor('doc.pdf', textBytes('%PDF-1.4')),
            pdf.message);
        const mismatch = A.identify(gifBytes('mismatch'), 'one.png');
        await refuse('a name that disagrees with the bytes',
            fileFor('one.png', gifBytes('mismatch')), mismatch.message);
        const empty = A.identify(new Uint8Array(0), 'empty.png');
        await refuse('an empty file', fileFor('empty.png', new Uint8Array(0)), empty.message);
        const notImage = A.identify(textBytes('hello there'), 'notes.png');
        await refuse('a file that is not an image at all',
            fileFor('notes.png', textBytes('hello there')), notImage.message);

        // A failed read.
        await refuse('a read that failed', fileFor('one.png', pngBytes('read'), { __fail: true }),
            'That file could not be read, so nothing was added.');

        // A refused put: the byte store's own answer, and no document change.
        const realPut = env.inst.assetStore.putBytes;
        env.inst.assetStore.putBytes = () => Promise.resolve({ ok: false, reason: 'id-mismatch' });
        await refuse('a byte store that refused the file', fileFor('two.png', pngBytes('two')),
            'That file could not be stored in this browser, so nothing was added to the message.');
        env.inst.assetStore.putBytes = realPut;
        assert((await env.inst.assetStore.getBytes(A.identify(pngBytes('two'), 'two.png').assetId)).ok === false,
            'and that refused file really is not in the store');

        // No file at all: not an event worth a notice, and certainly not an edit.
        // (A fresh page, so the previous refusals' sentences have nowhere to live.)
        const quiet = makeEnv();
        quiet.useIdb();
        await quiet.mount();
        const before = quiet.hash();
        const depthBefore = quiet.store().historyDepth().size;
        quiet.pick('media.image', null);
        await quiet.settled();
        assert(quiet.hash() === before && quiet.notice() === '' &&
               quiet.store().historyDepth().size === depthBefore,
            'cancelling the picker changes nothing and says nothing', quiet.notice());
        quiet.unmount();

        // A file that CLAIMS to be enormous: the control has no size gate, so
        // the real bytes decide. (A second limits authority would refuse here.)
        env.pick('media.image', fileFor('huge.png', pngBytes('huge'), { size: 99 * 1024 * 1024 }));
        await env.awaitPick(() => !!env.slotValue('media.image'));
        await env.settled();
        const hugeId = A.identify(pngBytes('huge'), 'huge.png').assetId;
        assert((env.slotValue('media.image') || {}).assetId === hugeId,
            'a file whose self-reported size is huge still attaches (no client-side gate)',
            JSON.stringify(env.slotValue('media.image')));
        assert(env.issues().length === 0,
            'and the validator reports nothing, because the bytes are small',
            JSON.stringify(env.issues().map((i) => i.code)));
        assert(!/file_bytes_advisory|20971520|file\.size\s*[<>]|size\s*>\s*\d/.test(PAGE_SRC),
            'the page never compares a file against a hard-coded size');

        env.unmount();
    }

    // ═══════════════════════════════════════════════════════════════
    section('E. one file, two slots: one record, two references, no deletions');
    // ═══════════════════════════════════════════════════════════════
    {
        const env = makeEnv();
        env.useIdb();
        await env.mount();
        const A = env.NERO.embed.assets;
        const file = fileFor('shared.png', pngBytes('shared'));
        const ident = A.identify(file.__bytes, 'shared.png');

        env.pick('media.image', file);
        await env.awaitPick(() => !!env.slotValue('media.image'));
        await env.settled();
        const depth = env.store().historyDepth().size;
        env.actions.length = 0;
        env.pick('media.thumbnail', file);
        await env.awaitPick(() => {
            const thumb = env.doc().embeds[0].thumbnail;
            return !!thumb && thumb.kind === 'upload';
        });
        await env.settled();

        assert(Object.keys(env.doc().assets).length === 1,
            'the same bytes give ONE record', JSON.stringify(Object.keys(env.doc().assets)));
        const imageRef = env.doc().embeds[0].image || {};
        const thumbRef = env.doc().embeds[0].thumbnail || {};
        assert(imageRef.assetId === ident.assetId && thumbRef.assetId === ident.assetId,
            'and two slots point at the same id', JSON.stringify([imageRef.assetId, thumbRef.assetId]));
        assert(env.store().historyDepth().size === depth + 1,
            'the second slot is ONE more undo step');
        assert(env.stats().duplicates >= 1, 'and the store recognised the bytes it already had',
            String(env.stats().duplicates));
        assert(env.issues().length === 0, 'two references to one file are clean');

        // Removing one reference keeps the record (and always the bytes).
        env.actions.length = 0;
        env.clickRemove('media.thumbnail');
        await env.until(() => env.slotValue('media.thumbnail') === null);
        await env.settled();
        assert(env.doc().embeds[0].thumbnail === null, 'Remove clears the slot it was used on');
        assert(env.doc().assets[ident.assetId] !== undefined,
            'the record stays while another slot still uses it');
        assert(env.actions.indexOf('asset/remove') === -1,
            'so no metadata was deleted', env.actions.join(','));
        assert((await env.inst.assetStore.getBytes(ident.assetId)).ok, 'and the bytes are here');
        assert(env.state('media.image').textContent.indexOf('Attached: shared.png') === 0,
            'the other slot still says what it holds', env.state('media.image').textContent);

        // Removing the LAST reference drops the record — never the bytes.
        const depth2 = env.store().historyDepth().size;
        env.actions.length = 0;
        env.clickRemove('media.image');
        await env.until(() => env.slotValue('media.image') === null && !env.record(ident.assetId));
        await env.settled();
        assert(eq(env.actions, ['embed/setMedia', 'asset/remove']),
            'the reference goes first, then the record', env.actions.join(','));
        assert(env.doc().assets[ident.assetId] === undefined,
            'the record is gone once nothing points at it');
        assert(env.store().historyDepth().size === depth2 + 1,
            'and removing is ONE undo step as well');
        const orphan = await env.inst.assetStore.getBytes(ident.assetId);
        assert(orphan.ok, 'while the BYTES stay exactly where they were (7d deletes nothing)',
            orphan.reason);
        assert(env.stats().deletes === 0 && env.stats().revokes === 0,
            'nothing deleted, nothing revoked');
        assert(env.state('media.image').textContent === 'No file attached',
            'and the control is back to "no file"');

        // Undo brings it all back without touching the store.
        env.store().undo();
        await env.until(() => !!env.slotValue('media.image') && !!env.record(ident.assetId));
        await env.settled();
        assert(env.doc().embeds[0].image && env.doc().assets[ident.assetId] !== undefined,
            'undo restores the reference and the record together');
        assert((await env.inst.assetStore.getBytes(ident.assetId)).ok, 'and the bytes never moved');

        env.unmount();
    }

    // ═══════════════════════════════════════════════════════════════
    section('E2. replacing a file obeys the same rule as Remove');
    // ═══════════════════════════════════════════════════════════════
    {
        const env = makeEnv();
        env.useIdb();
        await env.mount();
        const A = env.NERO.embed.assets;
        const shared = fileFor('shared.png', pngBytes('shared'));
        const sharedId = A.identify(shared.__bytes, 'shared.png').assetId;
        env.pick('media.image', shared);
        await env.awaitPick(() => !!env.record(sharedId));
        env.pick('media.thumbnail', shared);
        await env.awaitPick(() => {
            const t = env.slotValue('media.thumbnail');
            return !!t && t.assetId === sharedId;
        });
        await env.settled();
        assert(env.issues().length === 0, 'two slots on one file start clean',
            JSON.stringify(env.issues().map((i) => i.code)));

        // Replace ONE of them. The other slot still holds the old file, so its
        // record — and its bytes — have to survive.
        const other = fileFor('other.png', pngBytes('other'));
        const otherId = A.identify(other.__bytes, 'other.png').assetId;
        // The store merges edits that share a slot key inside its 1200 ms window
        // (5d rule, untouched by 7d). A person replacing a file does not do it
        // inside that window, and the invariant under test is about the
        // replacement itself: wait it out, then count.
        await env.settle(1300);
        const depth = env.store().historyDepth().size;
        env.actionLog.length = 0;
        env.pick('media.thumbnail', other);
        await env.awaitPick(() => {
            const t = env.slotValue('media.thumbnail');
            return !!t && t.assetId === otherId;
        });
        await env.settled();
        assert(!!env.record(sharedId), 'a record another slot still uses survives a replacement');
        assert(env.removals(sharedId).length === 0, 'and nothing was deleted',
            JSON.stringify(env.actionLog.map((a) => a.type)));
        assert(env.store().historyDepth().size === depth + 1, 'the replacement is ONE undo step');
        assert((await env.inst.assetStore.getBytes(sharedId)).ok, 'the old bytes are still here');
        assert(env.issues().length === 0, 'and the message is clean — no orphan warning',
            JSON.stringify(env.issues().map((i) => i.code)));

        // Replace the LAST reference to the old file: now its record goes too.
        const last = fileFor('last.png', pngBytes('last'));
        const lastId = A.identify(last.__bytes, 'last.png').assetId;
        const depth2 = env.store().historyDepth().size;
        env.actionLog.length = 0;
        env.pick('media.image', last);
        await env.awaitPick(() => {
            const v = env.slotValue('media.image');
            return !!v && v.assetId === lastId;
        });
        await env.settled();
        assert(env.record(sharedId) === null,
            'the replaced file\u2019s record is dropped once nothing points at it');
        assert(env.removals(sharedId).length === 1,
            'by one asset/remove, after the reference moved', JSON.stringify(env.actionLog.map((a) => a.type)));
        assert(env.store().historyDepth().size === depth2 + 1, 'and that is ONE undo step');
        assert((await env.inst.assetStore.getBytes(sharedId)).ok,
            'while its BYTES are still exactly where they were');
        assert(env.stats().deletes === 0 && env.stats().revokes === 0,
            'nothing in the store was deleted or revoked');
        assert((env.slotValue('media.thumbnail') || {}).assetId === otherId,
            'the other slot\u2019s newer file is untouched');
        assert(env.issues().length === 0, 'and the message is still clean',
            JSON.stringify(env.issues().map((i) => i.code)));

        // One undo brings the old file back — reference AND record, together.
        env.store().undo();
        await env.until(() => !!env.record(sharedId));
        await env.settled();
        assert((env.slotValue('media.image') || {}).assetId === sharedId && !!env.record(sharedId),
            'undo restores the replaced file as one step (reference and record)');
        assert((await env.inst.assetStore.getBytes(sharedId)).ok, 'and its bytes never moved');
        assert(env.issues().length === 0, 'the restored state is clean too');

        env.unmount();
    }

    // ═══════════════════════════════════════════════════════════════
    section('F. degraded storage: the store\u2019s own answer, never a claim');
    // ═══════════════════════════════════════════════════════════════
    {
        const env = makeEnv();
        env.useIdb({ blockOpen: true });          // storage never answers
        await env.mount();
        const A = env.NERO.embed.assets;
        const file = fileFor('session.png', pngBytes('session'));
        const ident = A.identify(file.__bytes, 'session.png');

        // Boot adopts the document only when the open attempt gives up (5d-era
        // behaviour, untouched by 7d), so wait for that notice: the page is
        // interactive from the first paint, and anything done before the adopt
        // is replaced along with it — typing included (see the 7d report).
        await env.until(() => /storage is unavailable/.test(env.notice()), 4000);
        env.showEmbed();
        env.pick('media.image', file);
        await env.until(() => env.notice() ===
            'That file is kept for this session only \u2014 this browser\u2019s storage is not available right now.', 2000);
        await env.settled();

        assert(env.doc().assets[ident.assetId] !== undefined &&
               (env.slotValue('media.image') || {}).assetId === ident.assetId,
            'a file can still be attached while storage is unavailable');
        assert((await env.inst.assetStore.getBytes(ident.assetId)).ok,
            'because the store keeps it for this session');
        assert(env.notice() === 'That file is kept for this session only — this browser’s storage is not available right now.',
            'and says exactly that', env.notice());
        assert(env.state('media.image').textContent.indexOf('Attached: session.png') === 0 &&
               !/saved|stored|persisted/i.test(env.state('media.image').textContent),
            'the control describes the file without claiming it was saved',
            env.state('media.image').textContent);
        assert(env.issues().length === 0 || env.issues().every((i) => i.code !== 'assets.bytes-missing'),
            'and in this session the bytes really are usable',
            JSON.stringify(env.issues().map((i) => i.code)));

        // The page is torn down; the session-only bytes go with it, and a fresh
        // page on the same (dead) storage has no document that mentions them.
        env.unmount();
        const reload = makeEnv();
        reload.useIdb({ blockOpen: true });
        await reload.mount();
        assert(reload.doc().embeds[0].image === null &&
               Object.keys(reload.doc().assets).length === 0,
            'a reload finds no draft, so the slot is empty — never a phantom file');
        assert(reload.state('media.image').textContent === 'No file attached',
            'and the control says so', reload.state('media.image').textContent);
        reload.unmount();
    }

    // ═══════════════════════════════════════════════════════════════
    section('G. reload: bytes present, and bytes gone');
    // ═══════════════════════════════════════════════════════════════
    {
        // (1) everything persisted: the reload is clean.
        const env = makeEnv();
        const idb = env.useIdb();
        await env.mount();
        const A = env.NERO.embed.assets;
        const file = fileFor('kept.png', pngBytes('kept'));
        const ident = A.identify(file.__bytes, 'kept.png');
        env.pick('media.image', file);
        await env.awaitPick(() => !!env.record(ident.assetId));
        await env.settled();
        await env.inst.session.saveNow();
        await env.settled();

        env.unmount();
        const reload = makeEnv();
        reload.useIdb();
        // Re-open the SAME database the first page wrote (the fake keeps its
        // contents in the instance it was given).
        reload.sandbox.indexedDB = idb;
        reload.win.indexedDB = idb;
        reload.NERO.indexedDB = idb;
        await reload.mount();
        assert(reload.doc().assets[ident.assetId] !== undefined &&
               (reload.slotValue('media.image') || {}).assetId === ident.assetId,
            'the saved draft brings the record and the reference back');
        assert((await reload.inst.assetStore.getBytes(ident.assetId)).ok,
            'and the bytes were persisted with it');
        assert(reload.state('media.image').textContent.indexOf('Attached: kept.png') === 0,
            'so the control describes it', reload.state('media.image').textContent);
        reload.unmount();

        // (2) the bytes are gone (evicted / another browser): the document still
        //     describes the file, and the VALIDATOR's sentence is what is shown.
        const evicted = makeEnv();
        evicted.useIdb();                       // a fresh store: the bytes are not here
        const snap = idb.snapshot(NS);          // the draft the first page saved
        // Keep the document record and DROP the byte entry: this page is a
        // browser that has the metadata but not the bytes (evicted, or another
        // machine). A JSON view of the bytes would not be corrupt bytes — it
        // would be a fixture mistake.
        // The 'assets' store exists (a real browser always creates it) but is
        // empty: that is what "the bytes are not here" means.
        const docOnly = { drafts: snap.drafts, meta: snap.meta, assets: {} };
        assert(!!docOnly.drafts && Object.keys(docOnly.drafts).length > 0,
            'the saved draft really is in the store to be seeded back');
        evicted.useIdb({ seed: { [NS]: docOnly } });
        await evicted.mount();
        assert(evicted.doc().assets[ident.assetId] !== undefined,
            'a draft can describe a file whose bytes are not in this browser');
        assert((evicted.slotValue('media.image') || {}).assetId === ident.assetId,
            'and the slot still points at it (a stale file is still a described file)');
        await evicted.settled();
        const missing = evicted.issues().filter((i) => i.code === 'assets.bytes-missing');
        assert(missing.length === 1, 'and the validator says so',
            JSON.stringify(evicted.issues().map((i) => i.code)));
        assert(evicted.state('media.image').textContent === missing[0].message,
            'the control shows THAT sentence, verbatim',
            evicted.state('media.image').textContent);
        assert(evicted.input('media.image').getAttribute('aria-invalid') === null,
            'a warning is not an error, so nothing is marked invalid');
        assert(evicted.removeBtn('media.image').hidden === false,
            'and the stale file can be removed');

        // Choosing a NEW file over the missing one heals it, in one step.
        const replacement = fileFor('fresh.png', pngBytes('fresh'));
        const freshId = A.identify(replacement.__bytes, 'fresh.png').assetId;
        const depth = evicted.store().historyDepth().size;
        evicted.pick('media.image', replacement);
        await evicted.awaitPick(() => !!evicted.record(freshId));
        await evicted.settled();
        assert((evicted.slotValue('media.image') || {}).assetId === freshId,
            'picking a new file replaces the reference',
            JSON.stringify(evicted.slotValue('media.image')));
        assert(evicted.store().historyDepth().size === depth + 1, 'in one undo step');
        assert(evicted.issues().length === 0, 'and the message is clean again',
            JSON.stringify(evicted.issues().map((i) => i.code)));
        assert(evicted.state('media.image').textContent.indexOf('Attached: fresh.png') === 0,
            'the control describes the new file');
        evicted.unmount();
    }

    // ═══════════════════════════════════════════════════════════════
    section('H. typing costs nothing: the pipeline is behind the picker');
    // ═══════════════════════════════════════════════════════════════
    {
        const env = makeEnv();
        env.useIdb();
        await env.mount();
        await env.settled();
        const puts = env.stats().puts;
        const reads = env.reads.length;
        const probes = env.probes();
        const uploads = env.uploadCount();
        const created = env.inst.inspector.stats().nodesCreated;
        const passes = env.counters().validateRuns || 0;

        const title = env.descendants(env.inspectorBody())
            .filter((n) => n.getAttribute && n.getAttribute('data-insp') === 'title')[0];
        assert(!!title, 'the title control is there to type into');
        for (let i = 0; i < 20; i++) {
            title.value = 'Hello ' + i;
            env.inspectorBody().dispatch('input', { type: 'input', target: title });
        }
        await env.settled();
        assert(env.stats().puts === puts, 'twenty keystrokes stored nothing');
        assert(env.reads.length === reads, 'read no file');
        assert(env.probes() === probes, 'probed nothing');
        assert(env.uploadCount() === uploads, 'and entered the upload pipeline zero times');
        assert((env.counters().validateRuns || 0) === passes + 1,
            'the burst is still exactly one validation pass',
            String((env.counters().validateRuns || 0) - passes));
        assert(env.inst.inspector.stats().nodesCreated === created,
            'and the file controls were not rebuilt');
        env.unmount();
    }

    // ═══════════════════════════════════════════════════════════════
    section('I. accessibility: the validator decides, the control reports');
    // ═══════════════════════════════════════════════════════════════
    {
        const env = makeEnv();
        env.useIdb();
        await env.mount();
        const A = env.NERO.embed.assets;
        const file = fileFor('fine.png', pngBytes('fine'));
        const ident = A.identify(file.__bytes, 'fine.png');
        env.pick('media.image', file);
        await env.awaitPick(() => !!env.record(ident.assetId));
        await env.settled();
        assert(env.input('media.image').getAttribute('aria-invalid') === null,
            'a healthy file is not marked invalid');

        // Break it the way a lost record does: drop the metadata, keep the slot.
        env.store().dispatch({ type: 'asset/remove', assetId: ident.assetId });
        await env.until(() => !env.record(ident.assetId));
        await env.settled();
        const issue = env.issues().filter((i) => i.code === 'assets.record-missing')[0];
        assert(!!issue && issue.severity === 'error', 'the validator reports a dangling reference',
            JSON.stringify(env.issues().map((i) => i.code)));
        assert(env.input('media.image').getAttribute('aria-invalid') === 'true',
            'and the control is marked invalid from THAT answer');
        assert(env.state('media.image').textContent === issue.message,
            'with the validator\u2019s own sentence, not a rewritten one',
            env.state('media.image').textContent);
        assert(env.state('media.image').textContent.indexOf('Add the file again') !== -1,
            'which is the sentence the rules wrote');

        // Undo the damage: the marker goes away with the issue.
        env.store().undo();
        await env.until(() => !!env.record(ident.assetId));
        await env.settled();
        assert(env.input('media.image').getAttribute('aria-invalid') === null,
            'fixing the document clears aria-invalid (never left behind)');
        assert(env.state('media.image').textContent.indexOf('Attached: fine.png') === 0,
            'and the state line is a description again');

        // The strip is still the only place a problem is ANNOUNCED.
        assert(env.el('mb2-strip').getAttribute('role') === 'status' &&
               env.el('mb2-strip').getAttribute('aria-live') === 'polite',
            'the strip is still the page\u2019s live region');
        assert(env.state('media.image').getAttribute('aria-live') === null,
            'and the state line is not a second one');
        env.unmount();
    }

    // ═══════════════════════════════════════════════════════════════
    section('J. races and teardown: nothing lands where it should not');
    // ═══════════════════════════════════════════════════════════════
    {
        // (1) A newer pick supersedes an older read.
        const env = makeEnv();
        env.useIdb();
        await env.mount();
        const A = env.NERO.embed.assets;
        env.holdReads = true;
        const first = fileFor('first.png', pngBytes('first'));
        const second = fileFor('second.png', pngBytes('second'));
        env.pick('media.image', first);
        env.pick('media.image', second);
        assert(env.held.length === 2, 'both reads are in flight', String(env.held.length));
        env.holdReads = false;
        env.releaseReads();
        await env.awaitPick(() => !!env.slotValue('media.image'));
        await env.settled();
        const secondId = A.identify(second.__bytes, 'second.png').assetId;
        const firstId = A.identify(first.__bytes, 'first.png').assetId;
        assert((env.slotValue('media.image') || {}).assetId === secondId,
            'the newest pick is the one that lands', JSON.stringify(env.slotValue('media.image')));
        assert(env.doc().assets[firstId] === undefined,
            'and the superseded file left no record');
        assert(env.store().historyDepth().size === 2, 'one undo step for the whole race',
            String(env.store().historyDepth().size));
        env.unmount();

        // (2) The slot changed while the read was in flight.
        const raced = makeEnv();
        raced.useIdb();
        await raced.mount();
        raced.holdReads = true;
        const pending = fileFor('pending.png', pngBytes('pending'));
        raced.pick('media.image', pending);
        const typed = raced.descendants(raced.inspectorBody())
            .filter((n) => n.getAttribute && n.getAttribute('data-insp') === 'media.image')[0];
        typed.value = 'https://example.test/typed.png';
        raced.inspectorBody().dispatch('input', { type: 'input', target: typed });
        const afterTyping = raced.hash();
        raced.releaseReads();
        await raced.settled();
        assert(raced.hash() === afterTyping, 'a read that lands after the slot changed changes nothing');
        const typedValue = raced.slotValue('media.image') || {};
        assert(typedValue.kind === 'url' && typedValue.url === 'https://example.test/typed.png',
            'the user\u2019s own edit is what stands', JSON.stringify(typedValue));
        assert(raced.doc().assets[A.identify(pending.__bytes, 'pending.png').assetId] === undefined,
            'and no record was written');
        assert(raced.notice().indexOf('the slot changed while it was being read') !== -1,
            'the refusal is explained', raced.notice());
        raced.unmount();

        // (3) The page dies while the read is in flight.
        const dead = makeEnv();
        dead.useIdb();
        await dead.mount();
        dead.holdReads = true;
        const doomed = fileFor('doomed.png', pngBytes('doomed'));
        dead.pick('media.image', doomed);
        const hashAtDeath = dead.hash();
        dead.unmount();
        dead.releaseReads();
        await dead.settle(30);
        assert(dead.hash() === hashAtDeath, 'a read that outlives the page mutates nothing');
        assert(dead.notice() === '', 'and says nothing after death', dead.notice());

        // (4) A click — or a whole file pick — after teardown does nothing at all.
        const gone = makeEnv();
        gone.useIdb();
        await gone.mount();
        const goneFile = fileFor('gone.png', pngBytes('gone'));
        gone.pick('media.image', goneFile);
        await gone.awaitPick(() => !!gone.record(gone.NERO.embed.assets.identify(goneFile.__bytes, 'gone.png').assetId));
        await gone.settled();
        const before = gone.hash();
        const button = gone.removeBtn('media.image');
        const deadInput = gone.input('media.image');
        const deadBody = gone.inspectorBody();
        const deadReads = gone.reads.length;
        const deadUploads = gone.uploadCount();
        const deadPuts = gone.stats().puts;
        gone.unmount();

        deadBody.dispatch('click', { type: 'click', target: button });
        await gone.settle(30);
        assert(gone.hash() === before, 'a Remove click after teardown changes nothing');

        // The strongest form of the same rule: a pick the dead page is handed
        // must not even START — no read, no counter, no store call. Work that
        // begins after teardown is work that outlives its owner.
        deadInput.files = [fileFor('zombie.png', pngBytes('zombie'))];
        deadBody.dispatch('change', { type: 'change', target: deadInput });
        await gone.settle(60);
        assert(gone.hash() === before, 'a pick after teardown changes nothing');
        assert(gone.reads.length === deadReads && gone.uploadCount() === deadUploads &&
               gone.stats().puts === deadPuts,
            'and the dead page does not even read the file',
            [gone.reads.length, gone.uploadCount(), gone.stats().puts].join(','));
        assert(gone.notice() === '', 'or say anything after death', gone.notice());

        env.unmount();
    }

    console.log('\nmessage-builder asset upload: ' + pass + ' passed, ' + fail + ' failed');
    if (fail) {
        console.log('\nFailures:');
        failures.forEach((f) => console.log(' - ' + f));
        process.exit(1);
    }
    console.log('ALL ASSET-UPLOAD CHECKS PASSED');
}

main().then(() => {}, (err) => { console.error('HARNESS ERROR', err && err.stack || err); process.exit(1); });
