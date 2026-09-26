#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════
   Message Builder v2 — phase 2, step 7e: STORED BYTES, MADE VISIBLE.

   WHAT THIS HAS TO PROVE (step 7e: resolution + the static files summary)

     A. THE FOUR SLOTS RESOLVE. A referenced local asset whose bytes this
        session can read is painted by the frozen preview, in the large-image,
        thumbnail, author-icon and footer-icon slots, through the resolver seam
        preview.js already has (`options.resolveImageSrc`), fed by the byte
        store's own URL cache.
     B. ONE MINT PER ASSET. The first resolution mints; every later one is a
        cache hit. Nothing on the render path ever mints or reads.
     C. REPAINTS ARE PROPORTIONAL. A changed resolution repaints once and
        writes only the attributes that changed; an unchanged one writes
        nothing; nothing is ever remounted and node identity survives.
     D. REFUSALS STAY QUIET AND SAFE. Missing bytes, an ambiguous filename, an
        unlinked reference and a browser without object URLs all render NO
        image — never a broken one — and the validator keeps its monopoly on
        saying why.
     E. TYPING COSTS NOTHING. A keystroke burst mints nothing, reads no bytes,
        runs no resolution and writes no preview attribute.
     F. THE PAGE STILL OWNS NO URL. It calls the store's urlFor/cachedUrl and
        nothing else: no createObjectURL, no Blob, no hashing, no revocation.
     G. TEARDOWN IS FINAL. No resolution runs after destroy, and every URL the
        session minted is revoked — by the store, exactly once.
     H. THE FILES SUMMARY is one static line above the preview: referenced
        files counted, known sizes summed, an unmeasured size never hidden, no
        verdict, no live region, and nothing written for unrelated typing.

   Run:  node scripts/test_message_builder_asset_resolution.js
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
    page: process.env.NERO_MB_PAGE_SRC || js('embed', 'message-builder-page.js'),
};
const readSource = (file) => fs.readFileSync(file, 'utf8');
const PAGE_SRC = readSource(SOURCE.page);
const TEMPLATE_PATH = path.join(ROOT, 'dashboard', 'templates', 'manage', 'message_builder.html');
const TEMPLATE_TREE = parseTemplate(readSource(TEMPLATE_PATH));

const GUILD = '1111222233334444';
const NS = 'nero_message_builder';
const IMAGE_STYLE = 'width:16px;height:16px;border-radius:3px;object-fit:cover;flex-shrink:0;';
const FOOTER_STYLE = 'width:20px;height:20px;border-radius:50%;object-fit:cover;flex-shrink:0;';

// ── the realm's globals (a vm has none of its own) ──
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
function pngBytes(seed, size) {
    const body = [];
    const n = size == null ? 24 : size - 8;
    for (let i = 0; i < n; i++) body.push((String(seed).charCodeAt(i % String(seed).length) + i * 7) & 0xff);
    return bytesFrom([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a].concat(body));
}
function gifBytes(seed) {
    const body = [];
    for (let i = 0; i < 16; i++) body.push((String(seed).charCodeAt(i % String(seed).length) + i * 3) & 0xff);
    return bytesFrom([0x47, 0x49, 0x46, 0x38, 0x39, 0x61].concat(body));
}
/** A File, as far as this pipeline is concerned: a name and bytes. */
function fileFor(name, bytes, extra) {
    return Object.assign({
        name: name, size: bytes.length, type: '',
        __bytes: bytes.length ? new Uint8Array(bytes) : new Uint8Array(0),
    }, extra || {});
}

    /** The stub keeps classes on `className` — read them the way it does. */
function hasClass(node, cls) {
    return !!node && String(node.className || '').split(/\s+/).indexOf(cls) !== -1;
}

// ── the rig ──
function makeEnv() {
    const env = { reads: [], held: [], actions: [], mints: [], revokes: [] };
    const dom = createDom();
    const win = createWindow();
    win.document = dom.document;
    win.__BOT_IDENTITY__ = { name: 'Nero', avatar: null };
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

    // The realm's FileReader (the stub has no File support by design): reads
    // land on a timer, so "a pick is in flight" is a state the tests can hold.
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

    // The realm's object-URL factory. Every mint and revoke is recorded, which
    // is how the tests can tell the store's cache from the page's behaviour.
    let mintSeq = 0;
    env.installUrlFactory = function () {
        win.Blob = function Blob(parts, opts) { this.parts = parts; this.type = opts && opts.type; };
        win.URL = {
            createObjectURL(blob) {
                const url = 'blob:nero/' + (++mintSeq);
                env.mints.push({ url, type: blob && blob.type });
                return url;
            },
            revokeObjectURL(url) { env.revokes.push(url); },
        };
    };
    env.removeUrlFactory = function () { delete win.Blob; delete win.URL; };
    env.installUrlFactory();

    [js('nav-lifecycle.js'), SOURCE.model, SOURCE.assets, SOURCE.assetStore, SOURCE.store,
     SOURCE.validate, js('embed', 'discord-markdown.js'), js('embed', 'preview.js'), SOURCE.drafts,
     js('embed', 'views', 'statusbar.js'), js('embed', 'views', 'rail.js'),
     js('embed', 'views', 'inspector.js'), js('embed', 'views', 'actionbar.js'), SOURCE.page]
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
        env.store().subscribe(function (state, action) { env.actions.push(action && action.type); });
        env.showEmbed();
        return env.inst;
    };
    env.unmount = () => env.NERO.lifecycle.unmount('test');
    env.page = () => env.NERO.embed.messageBuilderPage.current();
    env.store = () => env.inst && env.inst.store;
    env.doc = () => env.store().getDocument();
    env.issues = () => env.store().getUi().issues || [];
    env.counters = () => (env.inst.ctx && env.inst.ctx.counters) || {};
    env.stats = () => env.inst.assetStore.stats();
    env.previewStats = () => env.inst.preview.stats();
    env.notice = () => {
        const status = env.el('mb2-bar-status');
        if (!status) return '';
        const found = (status.children || []).find((c) => hasClass(c, 'mb2-status-notice'));
        return found ? found.textContent : '';
    };
    env.descendants = function (node, out) {
        out = out || [];
        (node && node.children || []).forEach(function (child) { out.push(child); env.descendants(child, out); });
        return out;
    };
    env.byClass = (node, cls) => env.descendants(node).filter((n) => hasClass(n, cls));
    env.showEmbed = function () {
        const embed = env.doc().embeds[0];
        env.store().dispatch({ type: 'ui/selectNode', nodeId: embed.id });
    };
    // ── the four preview slots, found the way preview.js builds them ──
    env.mountEl = () => env.el('mb2-mount');
    env.imgLarge = () => env.byClass(env.mountEl(), 'eb-pe-image')[0] || null;
    env.imgThumb = () => env.byClass(env.mountEl(), 'eb-pe-thumb')[0] || null;
    env.imgAuthor = function () {
        const author = env.byClass(env.mountEl(), 'eb-pe-author')[0];
        const first = author && author.children && author.children[0];
        return first && first.tagName === 'IMG' ? first : null;
    };
    env.imgFooter = function () {
        const footer = env.byClass(env.mountEl(), 'eb-pe-footer')[0];
        if (!footer) return null;
        return env.descendants(footer).filter((n) => n.tagName === 'IMG')[0] || null;
    };
    env.imgs = () => env.descendants(env.mountEl()).filter((n) => n.tagName === 'IMG');
    env.srcOf = (img) => (img ? String(img.getAttribute('src') || '') : '');
    env.slotSrc = function (key) {
        if (key === 'media.image') return env.srcOf(env.imgLarge());
        if (key === 'media.thumbnail') return env.srcOf(env.imgThumb());
        if (key === 'author.icon') return env.srcOf(env.imgAuthor());
        return env.srcOf(env.imgFooter());
    };
    // ── the control ──
    env.input = (key) => env.descendants(env.el('mb2-inspector-body'))
        .filter((n) => typeof n.getAttribute === 'function' &&
            n.getAttribute('data-insp-upload') === key)[0] || null;
    env.pick = function (key, file) {
        const input = env.input(key);
        if (!input) throw new Error('no upload control for ' + key);
        input.files = file ? [file] : [];
        env.el('mb2-inspector-body').dispatch('change', { type: 'change', target: input });
        return input;
    };
    env.clickRemove = function (key) {
        const btn = env.descendants(env.el('mb2-inspector-body'))
            .filter((n) => typeof n.getAttribute === 'function' &&
                n.getAttribute('data-insp-action') === 'remove:' + key)[0];
        if (!btn) throw new Error('no remove button for ' + key);
        env.el('mb2-inspector-body').dispatch('click', { type: 'click', target: btn });
        return btn;
    };
    /** The four slot values, read the way the document holds them. */
    env.slotValue = function (key) {
        const embed = env.doc().embeds[0];
        if (key === 'media.image') return embed.image;
        if (key === 'media.thumbnail') return embed.thumbnail;
        if (key === 'author.icon') return embed.author ? embed.author.icon : null;
        return embed.footer ? embed.footer.icon : null;
    };
    env.record = (assetId) => env.doc().assets[String(assetId)] || null;
    env.resolveRuns = () => env.counters().assetResolves || 0;
    env.resolveRepaints = () => env.counters().assetRepaints || 0;
    /**
     * Wait for the page to be QUIET: no file read in flight, no resolution pass
     * running, no validation pass pending. That is the page's own definition of
     * "settled", so the assertions below never race a timer.
     */
    env.settled = async function (ms) {
        await env.until(function () {
            const inst = env.inst;
            return !!inst && inst.validateTimer === null && inst.uploadToken === null && !inst.assetPass;
        }, ms == null ? 3000 : ms);
        await env.settle(10);
    };
    /** Wait until a resolution has landed for the given predicate. */
    env.awaitResolution = function (cond, ms) {
        return env.until(function () { return !!cond() && !env.inst.assetProbe; }, ms == null ? 3000 : ms);
    };
    /**
     * Count the writes a single element receives. The stub defines
     * `textContent`/`hidden` as own accessors, so wrapping one here counts what
     * the PAGE wrote to this node and nothing else — a global DOM-op counter
     * would also count the inspector and the status bar.
     */
    env.watchText = function (el) {
        const desc = Object.getOwnPropertyDescriptor(el, 'textContent');
        const state = { writes: 0 };
        if (desc && desc.set) {
            Object.defineProperty(el, 'textContent', {
                configurable: true, enumerable: desc.enumerable === true,
                get: function () { return desc.get.call(el); },
                set: function (value) { state.writes += 1; desc.set.call(el, value); },
            });
        }
        return state;
    };
    env.summaryEl = () => env.el('mb2-files');
    env.summaryText = () => { const el = env.summaryEl(); return el ? String(el.textContent || '') : '(no container)'; };
    return env;
}

async function main() {
    // ═══════════════════════════════════════════════════════════════
    section('A. the four slots resolve: a stored file becomes a picture');
    // ═══════════════════════════════════════════════════════════════
    {
        const env = makeEnv();
        env.useIdb();
        await env.mount();
        const A = env.NERO.embed.assets;
        assert(typeof env.inst.assetStore.cachedUrl === 'function' && typeof env.inst.assetStore.urlFor === 'function',
            'the byte store exposes the URL contract 7e consumes');
        assert(env.mints.length === 0, 'nothing was minted before a file existed');

        const KEYS = ['media.image', 'media.thumbnail', 'author.icon', 'footer.icon'];
        // Author and footer need their text before they mean anything to a
        // reader — set it first so the slots are judged in a valid document.
        env.store().dispatch({ type: 'embed/setAuthor', embedId: env.doc().embeds[0].id, patch: { name: 'Nero' } });
        env.store().dispatch({ type: 'embed/setFooter', embedId: env.doc().embeds[0].id, patch: { text: 'Notes' } });
        await env.settled();
        const expect = {};
        for (const key of KEYS) {
            const file = fileFor(key.replace('.', '-') + '.png', pngBytes(key));
            const id = A.identify(file.__bytes, file.name).assetId;
            env.pick(key, file);
            await env.awaitResolution(() => env.slotSrc(key).indexOf('blob:') === 0);
            await env.settled();
            expect[key] = { id: id, name: file.name, bytes: file.__bytes.length };
        }

        for (const key of KEYS) {
            const src = env.slotSrc(key);
            assert(src.indexOf('blob:') === 0, 'the ' + key + ' slot renders a blob: URL', src || '(no image)');
        }
        const srcs = KEYS.map((k) => env.slotSrc(k));
        assert(new Set(srcs).size === 4, 'four distinct files produce four distinct URLs', srcs.join(' '));
        assert(env.mints.length === 4 && env.stats().mints === 4,
            'exactly one mint per asset, four assets', env.mints.length + ' vs ' + env.stats().mints);
        for (const key of Object.keys(expect)) {
            const id = expect[key].id;
            const url = env.slotSrc(key);
            const minted = env.mints.filter((m) => m.url === url);
            assert(minted.length === 1 && minted[0].type === 'image/png',
                'the URL for ' + key + ' carries the file\'s own content type',
                JSON.stringify(minted));
            assert(env.inst.assetStore.cachedUrl(id) === url,
                'and the store\'s cache is the one holding it (' + key + ')');
        }
        assert(env.imgs().length === 4, 'all four slots render an <img>', String(env.imgs().length));
        assert(env.issues().length === 0, 'and the message validates clean',
            JSON.stringify(env.issues().map((i) => i.code)));
        env.unmount();
    }

    // ═══════════════════════════════════════════════════════════════
    section('B. one mint per asset; later resolutions are cache hits');
    // ═══════════════════════════════════════════════════════════════
    {
        const env = makeEnv();
        env.useIdb();
        await env.mount();
        const A = env.NERO.embed.assets;
        const file = fileFor('one.png', pngBytes('one'));
        const id = A.identify(file.__bytes, 'one.png').assetId;
        env.pick('media.image', file);
        await env.awaitResolution(() => env.slotSrc('media.image').indexOf('blob:') === 0);
        await env.settled();
        const firstUrl = env.slotSrc('media.image');
        const readsAfterFirst = env.stats().reads;
        assert(env.mints.length === 1 && env.stats().mints === 1, 'the pick minted exactly once',
            String(env.mints.length));

        // The same asset referenced from a SECOND slot: same id, same URL, no mint.
        env.pick('media.thumbnail', file);
        await env.awaitResolution(() => env.slotSrc('media.thumbnail').indexOf('blob:') === 0);
        await env.settled();
        assert(firstUrl.indexOf('blob:') === 0 && env.slotSrc('media.thumbnail') === firstUrl,
            'a second slot on the same bytes reuses the same URL',
            env.slotSrc('media.thumbnail') + ' vs ' + firstUrl);
        assert(env.mints.length === 1 && env.stats().mints === 1 && env.stats().urlMisses === 0,
            'and the store minted nothing more for those bytes (and refused nothing)',
            JSON.stringify({ mints: env.stats().mints, misses: env.stats().urlMisses }));

        // Removing the other slot and re-picking must still not re-mint.
        env.clickRemove('media.thumbnail');
        await env.settled();
        env.pick('media.thumbnail', file);
        await env.awaitResolution(() => env.slotSrc('media.thumbnail') === firstUrl);
        await env.settled();
        assert(env.mints.length === 1, 'and re-referencing it still mints nothing',
            String(env.mints.length));
        assert(env.srcOf(env.imgLarge()) === firstUrl && env.srcOf(env.imgThumb()) === firstUrl,
            'both slots show the same picture again',
            JSON.stringify([env.srcOf(env.imgLarge()), env.srcOf(env.imgThumb())]));

        // A third file arrives, and with it a fresh observation. The pass behind
        // it has ONE id with a cached URL (the first file) and ONE without: the
        // page must ask about the second only. A record nothing references must
        // not be resolved either, and nothing about either file may be refused.
        const orphan = A.buildRecord({
            assetId: 'sha-orphan', sha256: 'a'.repeat(64), mime: 'image/png', bytes: 2048,
            originalName: 'orphan.png', filename: 'orphan.png', availability: 'bytes-local',
            createdAt: new Date().toISOString(),
        });
        assert(orphan && orphan.ok, 'rig: an unreferenced record can be built', orphan && orphan.reason);
        env.store().dispatch({ type: 'asset/add', assetId: orphan.record.assetId, record: orphan.record });
        env.store().dispatch({ type: 'embed/setFooter', embedId: env.doc().embeds[0].id, patch: { text: 'Notes' } });
        const three = fileFor('three.png', pngBytes('three', 3072));
        env.pick('footer.icon', three);
        await env.awaitResolution(() => env.stats().mints === 2);
        await env.settled();
        assert(env.stats().mints === 2 && env.mints.length === 2,
            'two referenced files, two mints', String(env.stats().mints));
        assert(env.stats().urlHits === 0,
            'the file whose URL was already cached is never asked about again (no store hit logged)',
            String(env.stats().urlHits));
        assert(env.stats().urlMisses === 0,
            'and nothing is asked about a file the document does not reference (the orphan record)',
            String(env.stats().urlMisses));

        // A repaint caused by an unrelated edit must not go back to the bytes:
        // the URL is already in the store's cache, and reading it again would be
        // a second read for a picture that is already on screen.
        const readsNow = env.stats().reads;
        const mintsNow = env.stats().mints;
        const body = env.el('mb2-inspector-body');
        const title = env.descendants(body)
            .filter((n) => n.getAttribute && n.getAttribute('data-insp') === 'title')[0];
        assert(!!title, 'rig: the title control is there');
        title.value = 'Retitled';
        body.dispatch('input', { type: 'input', target: title });
        await env.settled();
        assert(env.stats().reads === readsNow && env.stats().mints === mintsNow,
            'an unrelated repaint reads no bytes and mints nothing',
            JSON.stringify({ reads: env.stats().reads - readsNow, mints: env.stats().mints - mintsNow }));
        env.unmount();
    }

    // ═══════════════════════════════════════════════════════════════
    section('C. repaints are proportional: only what changed, never a remount');
    // ═══════════════════════════════════════════════════════════════
    {
        const env = makeEnv();
        env.useIdb();
        await env.mount();
        const A = env.NERO.embed.assets;

        // Start with a plain URL image in one slot (no asset involved), so the
        // resolution repaint has a node that must survive it untouched.
        env.inst.store.dispatch({
            type: 'embed/setMedia', embedId: env.doc().embeds[0].id, slot: 'thumbnail',
            value: { kind: 'url', url: 'https://cdn.example/t.png' },
        });
        await env.settled();
        const thumbNode = env.imgThumb();
        assert(!!thumbNode && env.srcOf(thumbNode) === 'https://cdn.example/t.png',
            'rig: the thumbnail renders a plain URL image', env.srcOf(thumbNode));

        const file = fileFor('big.png', pngBytes('big', 4096));
        const id = A.identify(file.__bytes, 'big.png').assetId;
        const before = env.previewStats();
        env.pick('media.image', file);
        await env.awaitResolution(() => env.slotSrc('media.image').indexOf('blob:') === 0);
        await env.settled();
        const after = env.previewStats();

        assert(env.imgThumb() === thumbNode,
            'the untouched thumbnail is the SAME node after a resolution repaint');
        assert(env.srcOf(thumbNode) === 'https://cdn.example/t.png', 'with its src untouched');
        assert(after.nodesCreated === before.nodesCreated + 1,
            'exactly one node was created for the picture that appeared',
            String(after.nodesCreated - before.nodesCreated));
        assert(after.nodesRemoved === before.nodesRemoved,
            'and nothing was removed', String(after.nodesRemoved - before.nodesRemoved));
        assert(env.resolveRepaints() >= 1 && env.resolveRuns() >= 1,
            'the page reported a resolution pass and a repaint',
            JSON.stringify({ runs: env.resolveRuns(), repaints: env.resolveRepaints() }));

        // UNCHANGED RESOLUTION. The document changes (a title), the picture does
        // not: no resolution pass runs, no URL is asked for, and the node that
        // holds the picture is the same object afterwards.
        const repaintsBefore = env.resolveRepaints();
        const mintsBefore = env.mints.length;
        const imgNode = env.imgLarge();
        const title = env.descendants(env.el('mb2-inspector-body'))
            .filter((n) => n.getAttribute && n.getAttribute('data-insp') === 'title')[0];
        title.value = 'An unrelated edit';
        env.el('mb2-inspector-body').dispatch('input', { type: 'input', target: title });
        await env.settled();
        assert(env.resolveRepaints() === repaintsBefore && env.mints.length === mintsBefore,
            'an edit that does not change the resolution asks for no repaint and no URL',
            JSON.stringify({ repaints: env.resolveRepaints() - repaintsBefore, mints: env.mints.length - mintsBefore }));
        assert(env.imgLarge() === imgNode && env.slotSrc('media.image').indexOf('blob:') === 0,
            'and the picture keeps both its node and its URL');

        // The frozen seam's own guarantee, checked here on the 7e document:
        // patching the same document twice writes nothing the second time.
        const p1 = env.previewStats();
        env.inst.preview.updateDocument(env.store().getDocument());
        const p2 = env.previewStats();
        assert(p2.attrWrites === p1.attrWrites && p2.textWrites === p1.textWrites &&
               p2.markupWrites === p1.markupWrites && p2.nodesCreated === p1.nodesCreated,
            'a second identical update writes no DOM and re-parses no markdown',
            JSON.stringify({ attr: p2.attrWrites - p1.attrWrites, markup: p2.markupWrites - p1.markupWrites }));
        env.unmount();
    }

    // ═══════════════════════════════════════════════════════════════
    section('D. refusals: no image, never a broken image, no invented words');
    // ═══════════════════════════════════════════════════════════════
    {
        // (1) Bytes that are not in this browser: the document still describes
        //     the file, the preview shows nothing, and the strip explains.
        const env = makeEnv();
        const idb = env.useIdb();
        await env.mount();
        const A = env.NERO.embed.assets;
        const file = fileFor('kept.png', pngBytes('kept'));
        const id = A.identify(file.__bytes, 'kept.png').assetId;
        env.pick('media.image', file);
        await env.awaitResolution(() => env.slotSrc('media.image').indexOf('blob:') === 0);
        await env.settled();
        await env.inst.session.saveNow();
        await env.settle(30);
        await env.unmount();

        const reload = makeEnv();
        reload.useIdb();
        const snap = idb.snapshot(NS);
        reload.useIdb({ seed: { [NS]: { drafts: snap.drafts, meta: snap.meta, assets: {} } } });
        await reload.mount();
        await reload.settled();
        assert(!!reload.record(id), 'rig: the reloaded draft still describes the file');
        assert(reload.imgLarge() === null && reload.slotSrc('media.image') === '',
            'no bytes in this browser means NO image node, not a broken one');
        assert(reload.issues().some((i) => i.code === 'assets.bytes-missing'),
            'and the validator is the one saying why',
            JSON.stringify(reload.issues().map((i) => i.code)));
        assert(reload.notice() === '', 'the page adds no notice of its own', reload.notice());
        assert(reload.mints.length === 0, 'and it never asked for a URL', String(reload.mints.length));
        assert(reload.stats().urlMisses === 0,
            'nor did it ask the store about bytes its own observation already called gone',
            String(reload.stats().urlMisses));

        // …and the SAME file can be put back. Identity is the content, so this is
        // the same asset id: the referenced-id set does not move, so the page
        // must not go on believing the probe that said those bytes were gone.
        const again = fileFor('kept.png', pngBytes('kept'));
        assert(A.identify(again.__bytes, 'kept.png').assetId === id, 'rig: the same bytes are the same asset');
        reload.pick('media.image', again);
        await reload.awaitResolution(() => reload.slotSrc('media.image').indexOf('blob:') === 0);
        await reload.settled();
        assert(reload.slotSrc('media.image').indexOf('blob:') === 0,
            're-attaching the same file makes the picture appear',
            reload.slotSrc('media.image') || '(no image)');
        assert(reload.mints.length === 1, 'with exactly one mint', String(reload.mints.length));
        assert(!reload.issues().some((i) => i.code === 'assets.bytes-missing'),
            'and the stale "bytes missing" observation is not repeated by the strip',
            JSON.stringify(reload.issues().map((i) => i.code)));
        reload.unmount();

        // (2) An ambiguous filename fails closed.
        const twice = makeEnv();
        twice.useIdb();
        await twice.mount();
        const first = fileFor('same.png', pngBytes('amb-a'));
        const second = fileFor('same.png', pngBytes('amb-b'));
        twice.pick('media.image', first);
        await twice.awaitResolution(() => twice.slotSrc('media.image').indexOf('blob:') === 0);
        // Rename the second file to collide with the first: same name, other bytes.
        twice.pick('media.thumbnail', second);
        await twice.awaitResolution(() => twice.slotSrc('media.thumbnail').indexOf('blob:') === 0);
        await twice.settled();
        const clash = twice.issues().filter((i) => i.code === 'assets.filename-clash');
        assert(clash.length >= 1, 'rig: two different files with one name is the validator\'s error',
            JSON.stringify(twice.issues().map((i) => i.code)));
        assert(twice.slotSrc('media.image') === '' && twice.slotSrc('media.thumbnail') === '',
            'an ambiguous name resolves to NO image in either slot',
            JSON.stringify([twice.slotSrc('media.image'), twice.slotSrc('media.thumbnail')]));
        assert(twice.imgs().length === 0, 'so no picture is shown for an ambiguous pair');
        twice.unmount();

        // (3) An unlinked reference (no assetId) is never resolved.
        const unlinked = makeEnv();
        unlinked.useIdb();
        await unlinked.mount();
        unlinked.store().dispatch({
            type: 'embed/setMedia', embedId: unlinked.doc().embeds[0].id, slot: 'image',
            value: { kind: 'upload', assetId: null, filename: 'ghost.png', mime: 'image/png', bytes: 12 },
        });
        await unlinked.settled();
        assert(unlinked.doc().embeds[0].image.filename === 'ghost.png',
            'rig: the slot holds an unlinked attachment reference');
        assert(unlinked.imgLarge() === null && unlinked.mints.length === 0,
            'an unlinked reference resolves to nothing and mints nothing',
            String(unlinked.mints.length));
        assert(unlinked.issues().some((i) => i.code === 'assets.unlinked'),
            'while the validator names it', JSON.stringify(unlinked.issues().map((i) => i.code)));
        unlinked.unmount();

        // (4) A window with no object-URL capability: still no image, and the
        //     page says the one thing the validator cannot say.
        const noUrls = makeEnv();
        noUrls.removeUrlFactory();
        noUrls.useIdb();
        await noUrls.mount();
        const file4 = fileFor('cap.png', pngBytes('cap'));
        noUrls.pick('media.image', file4);
        await noUrls.settled();
        await noUrls.settle(40);
        assert(noUrls.imgLarge() === null, 'a browser without object URLs shows no image');
        assert(noUrls.notice().length > 0 && /preview|attached/i.test(noUrls.notice()),
            'and the page says the capability is missing', noUrls.notice());
        assert(noUrls.issues().length === 0 || !noUrls.issues().some((i) => i.code === 'assets.bytes-missing'),
            'without pretending the bytes are gone',
            JSON.stringify(noUrls.issues().map((i) => i.code)));
        noUrls.unmount();
    }

    // ═══════════════════════════════════════════════════════════════
    section('E. typing costs nothing: no mints, no reads, no resolution, no writes');
    // ═══════════════════════════════════════════════════════════════
    {
        const env = makeEnv();
        env.useIdb();
        await env.mount();
        const A = env.NERO.embed.assets;
        const file = fileFor('typed.png', pngBytes('typed'));
        env.pick('media.image', file);
        await env.awaitResolution(() => env.slotSrc('media.image').indexOf('blob:') === 0);
        await env.settled();

        const body = env.el('mb2-inspector-body');
        const field = (key) => env.descendants(body)
            .filter((n) => n.getAttribute && n.getAttribute('data-insp') === key)[0] || null;
        const type = async (input, value) => {
            input.value = value;
            body.dispatch('input', { type: 'input', target: input });
            await env.settled();
        };

        // WARM-UP. The frozen preview shows one empty-state node until the
        // message has something in it; that transition is not a remount, so it
        // is spent before the counters are read.
        const title = field('title');
        assert(!!title, 'rig: the title control is there');
        await type(title, 'T');
        env.store().dispatch({ type: 'ui/selectNode', nodeId: 'content' });
        await env.settle(10);
        const content = field('content');
        assert(!!content, 'rig: the content control is there');
        await type(content, 'M');

        const mints = env.mints.length;
        const reads = env.stats().reads;
        const resolves = env.resolveRuns();
        const before = env.previewStats();
        const imgNode = env.imgLarge();

        for (let i = 0; i < 14; i++) await type(title, 'Title ' + i);
        env.store().dispatch({ type: 'ui/selectNode', nodeId: 'content' });
        await env.settle(10);
        for (let i = 0; i < 14; i++) await type(field('content'), 'Some **markdown** ' + i);

        const after = env.previewStats();
        assert(after.nodesCreated === before.nodesCreated && after.nodesRemoved === before.nodesRemoved,
            '28 keystrokes neither create nor remove a preview node',
            JSON.stringify({ created: after.nodesCreated - before.nodesCreated,
                removed: after.nodesRemoved - before.nodesRemoved }));
        assert(env.mints.length === mints, 'a keystroke burst mints no URL',
            String(env.mints.length - mints));
        assert(env.stats().reads === reads, 'and reads no asset bytes',
            String(env.stats().reads - reads));
        assert(env.resolveRuns() === resolves, 'and runs no resolution pass',
            String(env.resolveRuns() - resolves));
        assert(env.imgLarge() === imgNode && env.slotSrc('media.image').indexOf('blob:') === 0,
            'and the picture keeps both its node and its URL', env.slotSrc('media.image'));
        assert(after.markdownRenders > before.markdownRenders,
            'rig: the burst really did render markdown (the test is not vacuous)',
            String(after.markdownRenders - before.markdownRenders));
        env.unmount();
    }

    // ═══════════════════════════════════════════════════════════════
    section('F. the page still owns no URL: the store mints, caches and revokes');
    // ═══════════════════════════════════════════════════════════════
    {
        const code = PAGE_SRC
            .replace(/(^|[^:])\/\/[^\n]*/g, '$1 ')
            .replace(/\/\*[\s\S]*?\*\//g, ' ');
        const hits = (re) => (code.match(re) || []).length;
        assert(hits(/createObjectURL|revokeObjectURL/g) === 0,
            'the page never calls createObjectURL/revokeObjectURL', String(hits(/createObjectURL|revokeObjectURL/g)));
        assert(hits(/new\s+Blob|BlobCtor|window\.Blob/g) === 0,
            'the page never creates a Blob', String(hits(/new\s+Blob|BlobCtor|window\.Blob/g)));
        assert(hits(/sha256Hex|crypto\.subtle|digest\(/g) === 0,
            'the page never hashes', String(hits(/sha256Hex|crypto\.subtle|digest\(/g)));
        assert(hits(/urlCache/g) === 0, 'the page owns no URL cache of its own', String(hits(/urlCache/g)));
        assert(hits(/\.urlFor\(/g) === hits(/assetStore\.urlFor\(/g) && hits(/assetStore\.urlFor\(/g) >= 1,
            'every URL it mints comes from the byte store\'s urlFor',
            JSON.stringify({ all: hits(/\.urlFor\(/g), store: hits(/assetStore\.urlFor\(/g) }));
        assert(hits(/\.cachedUrl\(/g) === hits(/assetStore\.cachedUrl\(/g) && hits(/assetStore\.cachedUrl\(/g) >= 1,
            'and every URL it reads comes from the store\'s cache',
            JSON.stringify({ all: hits(/\.cachedUrl\(/g), store: hits(/assetStore\.cachedUrl\(/g) }));
        assert(hits(/\.release\(|releaseAll\(/g) === 0,
            'and never releases a URL — lifetime belongs to the store',
            String(hits(/\.release\(|releaseAll\(/g)));
        assert(hits(/pruneOrphans|retention\s*[.(]/g) === 0,
            'and applies no retention/GC (D4 is deferred, not smuggled in)',
            String(hits(/pruneOrphans|retention\s*[.(]/g)));
    }

    // ═══════════════════════════════════════════════════════════════
    section('G. teardown: no resolution after destroy, every URL revoked');
    // ═══════════════════════════════════════════════════════════════
    {
        const env = makeEnv();
        env.useIdb();
        await env.mount();
        const A = env.NERO.embed.assets;
        const file = fileFor('bye.png', pngBytes('bye'));
        env.pick('media.image', file);
        await env.awaitResolution(() => env.slotSrc('media.image').indexOf('blob:') === 0);
        await env.settled();
        const minted = env.mints.map((m) => m.url);
        assert(minted.length === 1 && env.revokes.length === 0,
            'one URL is live before teardown', JSON.stringify({ minted: minted, revoked: env.revokes }));

        const resolvesAtDeath = env.resolveRuns();
        const mintsAtDeath = env.mints.length;
        await env.unmount();
        assert(env.revokes.slice().sort().join(',') === minted.slice().sort().join(','),
            'teardown revoked exactly the URLs the session minted',
            JSON.stringify(env.revokes));
        assert(env.NERO.embed.messageBuilderPage.current() === null ||
               env.NERO.embed.messageBuilderPage.current() === undefined,
            'and the page instance is gone');

        // A resolution that arrives after death must change nothing.
        await env.settle(60);
        assert(env.resolveRuns() === resolvesAtDeath && env.mints.length === mintsAtDeath,
            'no resolution ran after teardown',
            JSON.stringify({ runs: env.resolveRuns() - resolvesAtDeath, mints: env.mints.length - mintsAtDeath }));
        assert(env.mountEl().children.length === 0,
            'and the dead mount stays empty: no late resolution rebuilds it',
            String(env.mountEl().children.length));
        assert(await env.unmount() === false, 'unmounting twice is still a no-op');
    }

    // ═══════════════════════════════════════════════════════════════
    section('H. the files summary: one honest, static line');
    // ═══════════════════════════════════════════════════════════════
    {
        const env = makeEnv();
        env.useIdb();
        await env.mount();
        const A = env.NERO.embed.assets;
        const summary = env.summaryEl();
        const embedId = () => env.doc().embeds[0].id;
        assert(!!summary, 'the template declares the summary container');
        assert(summary.hidden === true && summary.textContent === '',
            'an empty document shows no line at all', JSON.stringify(summary.textContent));

        // One stored file, and then the same file in a second slot: ONE file.
        const one = fileFor('one.png', pngBytes('one', 4096));
        env.pick('media.image', one);
        await env.awaitResolution(() => env.slotSrc('media.image').indexOf('blob:') === 0);
        await env.settled();
        assert(String(summary.getAttribute('class')).indexOf('mb2-tone') === -1,
            'and the summary never wears a verdict tone', String(summary.getAttribute('class')));
        assert(summary.hidden === false && summary.textContent === '1 file \u00b7 4 KB',
            'one stored file reads as one file and its size', JSON.stringify(summary.textContent));
        env.pick('media.thumbnail', one);
        await env.awaitResolution(() => env.slotSrc('media.thumbnail') === env.slotSrc('media.image'));
        await env.settled();
        assert(summary.textContent === '1 file \u00b7 4 KB',
            'two slots using one file are still ONE file', summary.textContent);

        // A second, different file: the known sizes are summed.
        const two = fileFor('two.png', pngBytes('two', 5120));
        env.pick('author.icon', two);
        await env.awaitResolution(() => env.stats().mints === 2);
        await env.settled();
        assert(summary.textContent === '2 files \u00b7 9 KB',
            'two files read as two, with the sizes summed', JSON.stringify(summary.textContent));
        assert(summary.textContent.indexOf('.png') === -1 && (summary.children || []).length === 0,
            'the line has no rows, no names and no per-file detail', summary.textContent);

        // An unmeasured file is never hidden — and never folded into the total.
        env.store().dispatch({
            type: 'embed/setMedia', embedId: embedId(), slot: 'image',
            value: { kind: 'upload', assetId: 'sha-not-here', filename: 'ghost.png', mime: 'image/png', bytes: null },
        });
        await env.settled();
        assert(summary.textContent === '3 files \u00b7 9 KB \u00b7 1 unmeasured',
            'a referenced file with no record is counted AND reported as unmeasured',
            JSON.stringify(summary.textContent));

        // A record that exists but carries no byte count is unmeasured too.
        const built = A.buildRecord({
            assetId: 'sha-unknown-size', sha256: 'f'.repeat(64), mime: 'image/png', bytes: null,
            originalName: 'nosize.png', filename: 'nosize.png', availability: 'bytes-local',
            createdAt: new Date().toISOString(),
        });
        assert(built && built.ok, 'rig: a record may exist without a measured size', built && built.reason);
        env.store().dispatch({ type: 'asset/add', assetId: built.record.assetId, record: built.record });
        env.store().dispatch({
            type: 'embed/setMedia', embedId: embedId(), slot: 'thumbnail',
            value: { kind: 'upload', assetId: built.record.assetId, filename: built.record.filename,
                mime: built.record.mime, bytes: built.record.bytes },
        });
        await env.settled();
        assert(summary.textContent === '3 files \u00b7 5 KB \u00b7 2 unmeasured',
            'the known total counts only what is known; both unmeasured files are named as such',
            JSON.stringify(summary.textContent));

        // Change-guarded: typing writes it zero times, a real change writes once.
        const writes = env.watchText(summary);
        const body = env.el('mb2-inspector-body');
        const title = env.descendants(body)
            .filter((n) => n.getAttribute && n.getAttribute('data-insp') === 'title')[0];
        assert(!!title, 'rig: the title control is there');
        for (let i = 0; i < 20; i++) {
            title.value = 'Typing ' + i;
            body.dispatch('input', { type: 'input', target: title });
        }
        await env.settled();
        assert(writes.writes === 0,
            'twenty keystrokes write the summary ZERO times (nothing it states changed)',
            String(writes.writes));
        assert(String(summary.getAttribute('class')).indexOf('mb2-tone') === -1,
            'no tone appears even when the line reports something missing',
            String(summary.getAttribute('class')));
        env.clickRemove('media.image');
        await env.settled();
        assert(writes.writes === 1,
            'and a change it does state writes it exactly once', String(writes.writes));
        assert(summary.textContent === '2 files \u00b7 5 KB \u00b7 1 unmeasured',
            'with the new numbers', summary.textContent);
        env.unmount();
    }

    // ═══════════════════════════════════════════════════════════════
    section('I. declared by the template, static, and never a live region');
    // ═══════════════════════════════════════════════════════════════
    {
        const files = findById(TEMPLATE_TREE, 'mb2-files');
        const mount = findById(TEMPLATE_TREE, 'mb2-mount');
        const region = findById(TEMPLATE_TREE, 'mb2-preview-region');
        assert(!!files && !!mount && !!region, 'the template declares the region, the mount and the summary');
        assert(files.parent === mount.parent,
            'the summary is a SIBLING of the mount, never inside it (the mount belongs to preview.js)');
        assert(files.tag === 'p' && files.attrs.id === 'mb2-files' && files.attrs.class === 'mb2-files',
            'it is one paragraph with the id and class the stylesheet targets', JSON.stringify(files.attrs));

        const live = [];
        (function walk(node) {
            (node.children || []).forEach(function (child) {
                if (child.attrs['aria-live'] || child.attrs.role === 'status') live.push(child.attrs.id || child.tag);
                walk(child);
            });
        })(TEMPLATE_TREE);
        assert(live.length === 2 && live.indexOf('mb2-strip') !== -1 && live.indexOf('mb2-bar-status') !== -1,
            'the page still has exactly TWO live regions, and the summary is not one of them', live.join(','));
        assert(!files.attrs['aria-live'] && !files.attrs.role && files.attrs.hidden !== undefined,
            'it is static text that starts hidden', JSON.stringify(files.attrs));

        // The page module's declared id surface must include it: the layout
        // harness asserts that agreement for every id, so the name cannot drift
        // between the module and the markup.
        const probe = { window: { NERO: { definePage: function () {} } }, console: console,
            setTimeout: setTimeout, clearTimeout: clearTimeout };
        vm.createContext(probe);
        vm.runInContext(PAGE_SRC, probe, { filename: 'message-builder-page.js' });
        const PAGE_ID = (probe.window.NERO.embed.messageBuilderPage || {}).ID || {};
        assert(PAGE_ID.files === 'mb2-files' && files.attrs.id === PAGE_ID.files,
            'the id the page module resolves is the id the template declares', String(PAGE_ID.files));
        const css = readSource(path.join(ROOT, 'dashboard', 'static', 'css', 'message-builder.css'));
        const start = css.indexOf('.mb2-files {');
        assert(start !== -1, 'the stylesheet styles the summary');
        const block = css.slice(start, css.indexOf('.mb2-files[hidden]') + 40);
        assert(!/--danger|--warning|--success/.test(block),
            'and gives it no validation tone: the numbers are the whole message', block.slice(0, 120));
    }

    console.log('\nmessage-builder asset resolution: ' + pass + ' passed, ' + fail + ' failed');
    if (fail) {
        console.log('\nFailures:');
        failures.forEach((f) => console.log(' - ' + f));
        process.exit(1);
    }
    console.log('ALL ASSET-RESOLUTION CHECKS PASSED');
}

main().then(() => {}, (err) => { console.error('HARNESS ERROR', err && err.stack || err); process.exit(1); });
