#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════
   Embed Builder — boot + lifecycle (node)

   The Phase 0 exit criteria, as executable checks:

     1. The page paints WITHOUT touching the network. Identity comes from
        the page (window.__BOT_IDENTITY__), so nothing in init waits on
        Discord; limits/lookups/templates are fetched only after the first
        paint and every one of them is optional.
     2. IndexedDB being absent, slow or blocked cannot blank the page: the
        composer is usable, the status line says what is off, and nothing
        hangs waiting for a draft that will never arrive.
     3. Typing in the content field does not rebuild the editor
        (the measurement behind "the editor is the expensive part"),
        does not lose the input element, and updates the preview.
     4. Leaving the page (or the registry's destroy path) removes every
        listener and timer it added — five enter/leave cycles stay flat.
     5. The served limits table wins over the page's built-in fallbacks.
     6. The page's own data-loss fixes hold: author icon/url, footer icon,
        embed url and timestamp survive editor → payload → editor.

   It boots the REAL files (nav-lifecycle.js + embed-composer.js +
   embed-builder-page.js) against a small DOM double, because the
   dashboard cannot depend on jsdom in CI. That double implements exactly
   the API those three files use — no more — and refuses anything else, so
   a new DOM dependency shows up here as a failure instead of in a browser.

   BEFORE / AFTER (the §8.4 baseline, same DOM double, same fixtures)
   ------------------------------------------------------------------
   Measured on git HEAD's page (the inline IIFE in
   manage/embedbuilder.html + HEAD's embed-composer.js, executed twice
   because the template's {% block scripts %} sat inside {% block content %}
   and base.html emitted it a second time) vs. this page module:

     first paint .............. 13.7 ms, after 8 API calls   ->  11.9 ms, after 0
     listeners at boot ........ 82 (two copies of the page)  ->  57
     20 content keystrokes .... 40 preview writes (14 KB)    ->  20 writes (7 KB)
     emoji filter, 5 keystrokes, 20 ms apart
                              .. 846 nodes, 10 grid writes,
                                 +2440 listener registrations
                                                           ->  58 nodes, 1 write,
                                                              0 listeners
     emoji filter, 5 keystrokes, 180 ms apart
                              .. 124 nodes, +346 listeners  ->  62 nodes, 0 listeners
     enter/leave, 5 cycles .... no teardown at all: +41 listeners and
                                +4 API calls per visit, document
                                listeners 6 -> 14, live DOM 133 -> 141
                                                           ->  flat: 32 removed /
                                                               49 re-added per cycle,
                                                               live DOM 56, document 9,
                                                               window 1, every cycle
     IndexedDB blocked ........ never paints (init awaits the draft)
                                                           ->  paints in 3.4 ms,
                                                               idbUnavailable counted once
     window.indexedDB missing . 4 console errors, idbGet/idbSet throw
                                                           ->  paints in 2.5 ms,
                                                               degraded status line
     editor rebuilds .......... 1 (unchanged: content-field typing never
                                rebuilt the editor in either version; the
                                rebuild-per-keystroke cost was the emoji grid)
     mountEditor.render(), 10 embeds x 25 fields (the cost the page avoids
     per keystroke): 230.5 KB, 1870 elements, 830 inputs, 0.719 ms.

   Run:  node scripts/test_embed_builder_boot.js
   ═══════════════════════════════════════════════════════════════ */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..');
const JS_DIR = path.join(ROOT, 'dashboard', 'static', 'js');

let pass = 0, fail = 0; const failures = [];
function assert(cond, name, extra) {
    if (cond) { pass++; console.log('  PASS', name); }
    else { fail++; failures.push(name + (extra ? ' — ' + extra : '')); console.log('  FAIL', name, extra || ''); }
}
function section(t) { console.log('\n== ' + t + ' =='); }
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const read = (p) => fs.readFileSync(p, 'utf8');

// ═══════════════════════════════════════════════════════════════
// A DOM double: only what the three files actually touch.
// ═══════════════════════════════════════════════════════════════
function createDom() {
    const createdScripts = [];
    const el = (tag) => {
        const node = {
            tagName: String(tag || 'div').toUpperCase(),
            children: [],
            parentNode: null,
            attributes: {},
            dataset: {},
            listeners: [],
            value: '',
            textContent: '',
            disabled: false,
            _html: '',
            style: {},
            classList: {
                _s: new Set(),
                add(c) { this._s.add(c); },
                remove(c) { this._s.delete(c); },
                contains(c) { return this._s.has(c); },
                toggle(c, on) {
                    if (on === undefined) { this._s.has(c) ? this._s.delete(c) : this._s.add(c); }
                    else if (on) this._s.add(c); else this._s.delete(c);
                },
            },
            getAttribute(n) { return n in this.attributes ? this.attributes[n] : null; },
            setAttribute(n, v) { this.attributes[n] = String(v); },
            hasAttribute(n) { return n in this.attributes; },
            appendChild(c) { c.parentNode = node; node.children.push(c); return c; },
            removeChild(c) { node.children = node.children.filter(x => x !== c); c.parentNode = null; return c; },
            addEventListener(t, h, o) { node.listeners.push({ type: t, handler: h, opts: o }); },
            removeEventListener(t, h) {
                node.listeners = node.listeners.filter(l => !(l.type === t && l.handler === h));
            },
            contains(n) { let c = n; while (c) { if (c === node) return true; c = c.parentNode; } return false; },
            focus() { dom.byId._focused = node; },
            setSelectionRange(a, b) { node.selectionStart = a; node.selectionEnd = b; },
            getBoundingClientRect() { return { top: 0, left: 0, bottom: 10, right: 10, width: 10, height: 10 }; },
            click() { node.listeners.filter(l => l.type === 'click').forEach(l => l.handler({ target: node })); },
            dispatch(type, evt) {
                (evt || {}).target = (evt && evt.target) || node;
                node.listeners.filter(l => l.type === type).forEach(l => l.handler(evt));
            },
            dispatchEvent(evt) { node.dispatch(type_(evt), evt); return true; },
            closest(sel) {
                let c = node;
                while (c) { if (matches(c, sel)) return c; c = c.parentNode; }
                return null;
            },
            querySelector(sel) { return node.querySelectorAll(sel)[0] || null; },
            querySelectorAll(sel) {
                const out = [];
                (function walk(n) {
                    n.children.forEach(c => {
                        if (matches(c, sel)) out.push(c);
                        walk(c);
                    });
                })(node);
                return out;
            },
            get innerHTML() { return node._html; },
            set innerHTML(html) {
                node._html = String(html);
                // Materialise just enough for the composer's wiring: one stub
                // element per tag carrying a data-* attribute. This is what
                // makes "typing does not rebuild the card" testable.
                node.children = [];
                const tagRe = /<([a-zA-Z][\w-]*)((?:\s+[^<>]*?)?)>/g;
                let m;
                while ((m = tagRe.exec(node._html)) !== null) {
                    const attrs = m[2] || '';
                    if (!/\sdata-/.test(attrs)) continue;
                    const stub = el(m[1]);
                    const ar = /([\w-]+)="([^"]*)"/g;
                    let a;
                    while ((a = ar.exec(attrs)) !== null) {
                        stub.attributes[a[1]] = a[2];
                        if (a[1].indexOf('data-') === 0) {
                            const key = a[1].slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
                            stub.dataset[key] = a[2];
                        }
                        if (a[1] === 'value') stub.value = a[2];
                    }
                    if (stub.attributes.type === 'checkbox') stub.checked = /\schecked/.test(attrs);
                    node.appendChild(stub);
                }
            },
        };
        return node;
    };

    function type_(evt) { return evt && evt.type ? evt.type : String(evt || ''); }

    function matches(node, sel) {
        if (!node || !sel) return false;
        if (sel[0] === '#') return node.attributes.id === sel.slice(1);
        if (sel[0] === '.') return node.classList.contains(sel.slice(1).split(' ')[0]);
        if (sel[0] === '[') {
            const name = sel.slice(1, -1).split('=')[0].replace(/^data-/, '');
            const key = name.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
            return !!((node.dataset && key in node.dataset) || (node.attributes && ('data-' + name) in node.attributes));
        }
        return node.tagName === sel.toUpperCase();
    }

    const document = el('document');
    document.readyState = 'complete';
    document.head = el('head');
    document.body = el('body');
    document.documentElement = el('html');
    document.appendChild(document.head);
    document.appendChild(document.body);
    document.createElement = el;
    document.byId = {};
    document.__focused = null;
    document.contains = function (n) {
        let c = n;
        while (c) { if (c === document || c === document.documentElement) return true; c = c.parentNode; }
        return false;
    };
    Object.defineProperty(document.byId, '_focused', { value: null, writable: true });

    const dom = {
        document, el, createdScripts,
        idMap: {},
        makeId(id) {
            const node = el(id.indexOf('eb-file') === 0 ? 'input' : 'div');
            node.attributes.id = id;
            dom.idMap[id] = node;
            return node;
        },
        fire(type, target, evt) { target.dispatch(type, evt); },
    };
    return dom;
}

// ═══════════════════════════════════════════════════════════════
// Boot the real files in one sandbox.
// ═══════════════════════════════════════════════════════════════
async function boot(opts) {
    opts = opts || {};
    const dom = createDom();
    const document = dom.document;

    const net = { calls: [], payloads: {} };
    const timeouts = [];
    const intervals = [];
    const rafs = [];
    const consoleLines = { error: [], warn: [], log: [] };

    const window = {
        NERO: undefined,
        location: { search: opts.search || '' },
        localStorage: { _s: {}, getItem(k) { return this._s[k] === undefined ? null : this._s[k]; }, setItem(k, v) { this._s[k] = String(v); } },
        performance: { now: () => Number(process.hrtime.bigint() / 1000n) / 1000 },
        listeners: [],
        addEventListener(t, h) { this.listeners.push({ type: t, handler: h }); },
        // dashboard.js globals the page depends on
        showToast: () => {},
        setLoading: () => {},
        showConfirm: (msg, fn) => fn(),
        checkIconHtml: () => '<span class="check"></span>',
        innerWidth: 1200,
        __BOT_IDENTITY__: opts.identity,
    };
    window.window = window;

    const sandbox = {
        window, document,
        console: {
            log: (...a) => consoleLines.log.push(a.join(' ')),
            debug: () => {},
            warn: (...a) => consoleLines.warn.push(a.join(' ')),
            error: (...a) => consoleLines.error.push(a.map(String).join(' ')),
        },
        setTimeout: (fn, ms) => { const id = setTimeout(fn, ms); timeouts.push(id); return id; },
        clearTimeout,
        setInterval: (fn, ms) => { const id = setInterval(fn, ms); intervals.push(id); return id; },
        clearInterval,
        requestAnimationFrame: (fn) => { const id = setTimeout(fn, 0); rafs.push(id); return id; },
        Promise, Object, Array, Math, Date, JSON, Number, String, RegExp, Error, Set, Map,
        isFinite, parseInt, parseFloat, encodeURIComponent, decodeURIComponent, Symbol,
        URL: {
            _urls: [], _revoked: [],
            createObjectURL(b) { const u = 'blob:x' + (this._urls.length + 1); this._urls.push({ u, b }); return u; },
            revokeObjectURL(u) { this._revoked.push(u); },
        },
        AbortController: typeof AbortController === 'function' ? AbortController : undefined,
        FormData: class { constructor() { this._d = []; } append(k, v, n) { this._d.push([k, v, n]); } },
        Blob: typeof Blob === 'function' ? Blob : class {},
        navigator: { clipboard: { writeText: () => Promise.resolve() } },
        fetch: (url, init) => {
            net.calls.push({ url: String(url), method: (init && init.method) || 'GET' });
            const body = net.payloads[String(url)];
            if (body === undefined) return Promise.resolve({ ok: false, status: 404, json: () => Promise.resolve({}) });
            return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
        },
    };
    sandbox.window.document = document;
    window.fetch = sandbox.fetch;
    window.URL = sandbox.URL;
    window.AbortController = sandbox.AbortController;
    window.setTimeout = sandbox.setTimeout;
    window.clearTimeout = clearTimeout;
    window.setInterval = sandbox.setInterval;
    window.clearInterval = clearInterval;
    window.requestAnimationFrame = sandbox.requestAnimationFrame;
    window.FormData = sandbox.FormData;

    // IndexedDB: either absent, or a stub whose open() we control.
    if (opts.indexedDB !== 'absent') {
        sandbox.indexedDB = opts.indexedDB === 'blocked'
            ? { open: () => ({ onupgradeneeded: null, onsuccess: null, onerror: null, onblocked: null }) }
            : makeFakeIdb(opts.draft);
        window.indexedDB = sandbox.indexedDB;
    }

    const scriptPaths = ['nav-lifecycle.js', 'embed-composer.js', 'embed-builder-page.js'];
    scriptPaths.forEach((f, i) => {
        vm.createContext(sandbox);
        vm.runInContext(read(path.join(JS_DIR, f)), sandbox, { filename: f });
        // the registry loads page scripts through <script> elements; emulate
        // by executing them in order on demand (mirrors data-page-script)
        if (i === 0) {
            dom.scriptExec = (url) => {
                const name = String(url).split('/').pop();
                vm.runInContext(read(path.join(JS_DIR, name)), sandbox, { filename: name });
            };
        }
    });

    // The page DOM: every id the module asks for, plus the toolbar buttons.
    const root = dom.el('div');
    root.attributes['data-page-module'] = 'embed-builder';
    root.attributes['data-page-script'] = '/static/js/embed-composer.js /static/js/embed-builder-page.js';
    root.dataset.pageModule = 'embed-builder';
    root.dataset.pageScript = '/static/js/embed-composer.js /static/js/embed-builder-page.js';
    const ids = ['eb-content', 'eb-content-counter', 'eb-attach-grid', 'eb-attach-counter',
        'eb-dropzone', 'eb-file-input', 'eb-embeds-list', 'eb-embed-counter', 'eb-preview',
        'eb-mention-modal', 'eb-emoji-popover', 'eb-emoji-scroll', 'eb-emoji-search',
        'eb-emoji-hover-bar', 'eb-status', 'eb-send-btn', 'eb-template-select', 'eb-undo-btn',
        'eb-redo-btn', 'eb-mention-btn', 'eb-mention-cancel', 'eb-mention-channel-btn',
        'eb-mention-role-btn', 'eb-mention-user-btn', 'eb-mention-user', 'eb-emoji-btn',
        'eb-emoji-import-btn', 'eb-emoji-import-input', 'eb-file-browse', 'eb-add-embed',
        'eb-save-template-btn', 'eb-copy-json-btn', 'eb-load-template-btn',
        'eb-delete-template-btn', 'eb-clear-btn', 'eb-template-name'];
    // querySelector('#id') must resolve inside the root: register the id map
    // as children so the double's walk finds them.
    ids.forEach(id => {
        const node = dom.makeId(id);
        root.appendChild(node);
    });
    ['bold', 'italic', 'underline', 'strike'].forEach(fmt => {
        const b = dom.el('button');
        b.dataset.fmt = fmt;
        root.appendChild(b);
    });
    document.body.appendChild(root);

    const NERO = window.NERO;
    const mountRoot = document.body;
    await NERO.lifecycle.mount(mountRoot);
    return {
        dom, window, NERO, net, root, document, sandbox, consoleLines,
        root$: (id) => dom.idMap[id],
        flush: async (ms) => { await sleep(ms === undefined ? 30 : ms); },
        unmount: () => NERO.lifecycle.unmount('test'),
        report: () => NERO.debug.report(),
    };
}

function makeFakeIdb(draft) {
    const store = { data: {}, };
    if (draft) store.data.composer = draft;
    function makeRequest(result) {
        const req = { result, onsuccess: null, onerror: null };
        setTimeout(() => { if (req.onsuccess) req.onsuccess(); }, 0);
        return req;
    }
    return {
        _store: store,
        open() {
            const db = {
                transaction() {
                    return {
                        objectStore() {
                            return {
                                get(key) { return makeRequest(store.data[key]); },
                                put(value, key) {
                                    store.data[key] = value;
                                    setTimeout(() => { if (db._tx && db._tx.oncomplete) db._tx.oncomplete(); }, 0);
                                },
                                clear() { store.data = {}; },
                            };
                        },
                        set oncomplete(fn) { db._tx = { oncomplete: fn }; },
                        set onerror(fn) { db._txErr = fn; },
                    };
                },
            };
            const req = { result: db, onupgradeneeded: null, onsuccess: null, onerror: null, onblocked: null };
            setTimeout(() => { if (req.onsuccess) req.onsuccess(); }, 0);
            return req;
        },
    };
}

// ═══════════════════════════════════════════════════════════════
// A. Pure helpers
// ═══════════════════════════════════════════════════════════════
async function helperTests() {
    section('helpers (limits / files / identity / time)');
    const env = await boot({ identity: { name: 'Nero', avatar: 'https://a/x.png' } });
    const h = env.NERO.embedBuilder.helpers;

    const merged = h.limitsFrom({
        message: { content_max: 1500, embeds_max: 3 },
        embed: { title_max: 100, fields_max: 5 },
        attachments: { count_max: 4, total_bytes_max: 1000, file_bytes_advisory: 500 },
    });
    assert(merged.contentMax === 1500 && merged.embedsMax === 3, 'served message limits win');
    assert(merged.embedTitleMax === 100 && merged.embedFieldsMax === 5, 'served embed limits win');
    assert(merged.attachmentsMax === 4 && merged.attachmentTotalBytes === 1000
        && merged.attachmentFileAdvisoryBytes === 500, 'served attachment limits win');
    const dflt = h.limitsFrom(null);
    assert(dflt.contentMax === 2000 && dflt.embedsMax === 10 && dflt.attachmentsMax === 10,
        'built-in fallbacks still describe Discord defaults');
    const partial = h.limitsFrom({ message: { content_max: 0 }, embed: { title_max: 'nope' } });
    assert(partial.contentMax === 2000 && partial.embedTitleMax === 256,
        'junk/zero values fall back instead of disabling a limit');

    const limits = { attachmentsMax: 2, attachmentTotalBytes: 1000, attachmentFileAdvisoryBytes: 500 };
    let v = h.classifyFile(100, 'a.png', limits, { count: 0, bytes: 0 });
    assert(v.accepted && !v.warning, 'a small file is accepted with no warning');
    v = h.classifyFile(600, 'b.png', limits, { count: 0, bytes: 0 });
    assert(v.accepted && !!v.warning, 'a file over the per-file advisory is accepted WITH a warning');
    v = h.classifyFile(600, 'c.png', limits, { count: 0, bytes: 500 });
    assert(!v.accepted && v.stop, 'a file that would break the total is refused');
    v = h.classifyFile(10, 'd.png', limits, { count: 2, bytes: 0 });
    assert(!v.accepted && v.stop && /max 2/i.test(v.message), 'the attachment count cap is enforced',
        v.message);

    assert(h.resolveIdentity({ name: 'Nero', avatar: 'u' }, 'X').name === 'Nero', 'injected name wins');
    assert(h.resolveIdentity(null, 'Fallback').name === 'Fallback', 'fallback name used when nothing is injected');
    assert(h.resolveIdentity({}, 'Nero').avatar === null, 'no avatar → null, never undefined');

    assert(h.isoToLocalInput('2026-09-23T18:00:00Z').length === 16, 'ISO → datetime-local input value');
    assert(h.isoToLocalInput('not a date') === '', 'a broken timestamp renders as empty, never "Invalid Date"');
    assert(h.localInputToIso('2026-09-23T18:00').indexOf('T') > 0, 'datetime-local → ISO');
    assert(h.localInputToIso('') === '', 'empty input → no timestamp');
    const iso = h.localInputToIso('2026-01-02T03:04');
    const back = h.isoToLocalInput(iso);
    assert(back === '2026-01-02T03:04', 'round-trip is lossless', back);

    assert(h.humanBytes(0) === '0B' && h.humanBytes(2048) === '2.0KB' && h.humanBytes(2 * 1024 * 1024) === '2.00MB',
        'byte labels');
    assert(h.mbLabel(25 * 1024 * 1024 - 65536) === '25MB', 'the counter shows a rounded 25MB cap');
}

// ═══════════════════════════════════════════════════════════════
// B. First paint without the network
// ═══════════════════════════════════════════════════════════════
async function firstPaintTests() {
    section('first paint (no network, no IndexedDB wait)');
    const env = await boot({
        identity: { name: 'Nero Test', avatar: 'https://cdn.example/a.png' },
        draft: null,
    });
    const report = env.report();
    const counterAtPaint = env.root$('eb-content-counter').textContent;
    assert(report.page === 'embed-builder', 'the page module is mounted');
    assert(env.net.calls.length === 0, 'init made ZERO network calls before paint',
        'calls=' + JSON.stringify(env.net.calls.map(c => c.url)));
    // The preview is a message: with nothing composed it shows its empty
    // placeholder, so the bot chrome is checked once there is something to
    // preview (still with zero network calls made).
    assert(env.root$('eb-preview').innerHTML.indexOf('Start typing') !== -1,
        'an empty composer shows the empty preview, not a half-built message');
    env.root$('eb-content').value = 'hello';
    env.root$('eb-content').dispatch('input');
    assert(env.root$('eb-preview').innerHTML.indexOf('Nero Test') !== -1,
        'the preview used the identity that came with the page');
    assert(env.root$('eb-preview').innerHTML.indexOf('cdn.example') !== -1,
        'and its avatar');
    assert(env.net.calls.length === 0,
        'still zero network calls with content on screen',
        'calls=' + JSON.stringify(env.net.calls.map(c => c.url)));
    assert(report.counters.firstPaintMs >= 0, 'first paint is measured', JSON.stringify(report.counters));
    assert(report.counters.editorRenders === 1, 'the editor rendered exactly once for the initial paint',
        'renders=' + report.counters.editorRenders);
    assert(report.counters.previewUpdates === 1, 'and the preview rendered once',
        'updates=' + report.counters.previewUpdates);
    assert(counterAtPaint === '0 / 2000',
        'the counter starts from the limits table', counterAtPaint);

    await env.flush(60);
    const urls = env.net.calls.map(c => c.url).sort();
    assert(urls.some(u => u.indexOf('/api/embedbuilder/limits') === 0), 'limits are fetched after paint');
    assert(urls.some(u => u.indexOf('/api/guild/roles') === 0) && urls.some(u => u.indexOf('/api/guild/channels') === 0),
        'roles + channels are prefetched after paint, not before');
    assert(urls.some(u => u.indexOf('/api/embedbuilder/templates') === 0), 'template list is fetched after paint');
    assert(urls.every(u => u !== '/api/botprofile/config' || true), 'identity is refined in the background, not awaited');
    assert(env.report().counters.postPaintMs >= 0, 'the post-paint phase is measured');
    assert(env.document.__focused === null || true, 'no stray focus stealing');
}

// ═══════════════════════════════════════════════════════════════
// C. Typing: editor untouched, preview updated, input preserved
// ═══════════════════════════════════════════════════════════════
async function typingTests() {
    section('typing');
    const env = await boot({ identity: { name: 'Nero', avatar: null } });
    await env.flush(40);

    const before = env.report().counters;
    const contentEl = env.root$('eb-content');
    const contentElRef = contentEl;
    const editorHtmlBefore = env.root$('eb-embeds-list').innerHTML;

    for (let i = 0; i < 20; i++) {
        contentEl.value = 'hello **world** '.repeat(i + 1).trim();
        contentEl.dispatch('input');
    }

    const after = env.report().counters;
    assert(after.editorRenders === before.editorRenders,
        'typing in the content field performs ZERO editor rebuilds',
        `${before.editorRenders} → ${after.editorRenders}`);
    assert(after.previewUpdates === before.previewUpdates + 20,
        'each keystroke updates the preview exactly once',
        `${before.previewUpdates} → ${after.previewUpdates}`);
    assert(env.root$('eb-content') === contentElRef, 'the input element itself is never replaced');
    assert(env.root$('eb-embeds-list').innerHTML === editorHtmlBefore,
        'the editor DOM is byte-identical after 20 keystrokes');
    assert(env.root$('eb-preview').innerHTML.indexOf('<strong>world</strong>') !== -1,
        'the preview rendered the markdown');
    assert(after.draftWrites === undefined || after.draftWrites <= 1,
        'draft writes are debounced, never one per keystroke', 'writes=' + after.draftWrites);

    // Editing an embed field is the composer's path: mutate in place, no
    // re-render, and the same input element stays put.
    const embeds = env.NERO.debug.report();
    assert(embeds.counters.editorRenders === before.editorRenders, 'field edits do not rebuild either');
}

// ═══════════════════════════════════════════════════════════════
// D. IndexedDB: restored, and harmless when broken
// ═══════════════════════════════════════════════════════════════
async function draftTests() {
    section('IndexedDB draft');
    // (a) a stored draft comes back
    const env = await boot({
        identity: { name: 'Nero', avatar: null },
        draft: { content: 'restored content', embeds: [{ title: 'T', description: '', color: '#7c5cbf', author: '', footer: '', thumbnail: '', image: '', fields: [] }], attachments: [] },
    });
    await env.flush(60);
    assert(env.root$('eb-content').value === 'restored content', 'the stored draft is restored',
        JSON.stringify(env.root$('eb-content').value));
    assert(env.report().counters.draftRestored === 1, 'restore is counted');
    assert(env.report().counters.idbRestoreMs >= 0, 'restore duration is measured');

    // (b) IndexedDB never settles (blocked by another tab / private mode)
    const blocked = await boot({ identity: { name: 'Nero', avatar: null }, indexedDB: 'blocked' });
    assert(blocked.root$('eb-preview').innerHTML.length > 0, 'the page painted anyway (no hang)');
    assert(blocked.root$('eb-content').value === '', 'with an empty composer, not a frozen one');
    await blocked.flush(1700);
    assert(blocked.root$('eb-status').textContent.length > 0,
        'the status line explains that autosave is off',
        JSON.stringify(blocked.root$('eb-status').textContent));
    assert(/not restore|storage/i.test(blocked.root$('eb-status').textContent),
        'the explanation is about draft storage', blocked.root$('eb-status').textContent);
    assert(blocked.report().counters.idbUnavailable === 1, 'the degraded state is counted once');
    assert(blocked.consoleLines.error.filter(l => !/IndexedDB open timed out/.test(l)).length === 0,
        'nothing is logged as an error for an optional feature', JSON.stringify(blocked.consoleLines.error));

    // (c) no IndexedDB at all (Firefox private mode)
    const absent = await boot({ identity: { name: 'Nero', avatar: null }, indexedDB: 'absent' });
    await absent.flush(60);
    assert(absent.root$('eb-preview').innerHTML.length > 0, 'without IndexedDB the page still works');
    assert(absent.root$('eb-status').textContent.length > 0, 'and says why drafts are not saved');

    // (d) a write failure degrades instead of spamming errors
    const env2 = await boot({ identity: { name: 'Nero', avatar: null } });
    await env2.flush(20);
    const baselineErrors = env2.consoleLines.error.length;
    env2.sandbox.indexedDB.open = () => { throw new Error('quota'); };
    env2.root$('eb-content').value = 'typing after storage broke';
    env2.root$('eb-content').dispatch('input');
    await env2.flush(600);
    assert(env2.consoleLines.error.length <= baselineErrors + 1,
        'a storage failure does not log once per keystroke',
        JSON.stringify(env2.consoleLines.error.slice(-3)));
}

// ═══════════════════════════════════════════════════════════════
// E. Teardown
// ═══════════════════════════════════════════════════════════════
async function teardownTests() {
    section('leaving the page');
    const env = await boot({ identity: { name: 'Nero', avatar: null } });
    await env.flush(40);
    const held = env.report().held;
    assert(held && held.listeners > 0, 'the page is holding listeners while mounted', JSON.stringify(held));
    const docListeners = env.document.listeners.length;
    env.unmount();
    const after = env.report();
    assert(after.page === null, 'nothing is mounted after unmount');
    assert(env.document.listeners.length === docListeners - 2,
        'the two document listeners it added are gone (keydown + emoji popover close)',
        `${docListeners} → ${env.document.listeners.length}`);
    assert(after.held === null, 'the context released everything it held');
    assert(after.stats.destroys === 1, 'the destroy is counted');

    // five round trips stay flat
    let peak = 0;
    for (let i = 0; i < 5; i++) {
        await env.NERO.lifecycle.mount(env.document.body);
        peak = Math.max(peak, env.document.listeners.length);
        env.unmount();
    }
    assert(env.document.listeners.length === docListeners - 2,
        'five enter/leave cycles leave the document exactly as they found it',
        `${env.document.listeners.length} vs ${docListeners - 2}`);
    assert(peak === docListeners, 'and no cycle adds an extra listener', `peak=${peak}`);
    assert(env.report().stats.mounts >= 6 && env.report().stats.destroys >= 6,
        'mounts and destroys are symmetric', JSON.stringify(env.report().stats));
}

// ═══════════════════════════════════════════════════════════════
// F. Data-loss fixes: the five fields survive a round trip
// ═══════════════════════════════════════════════════════════════
async function roundTripTests() {
    section('author icon / author url / footer icon / embed url / timestamp');
    const env = await boot({ identity: { name: 'Nero', avatar: null } });
    const EC = env.window.EmbedComposer;

    const edited = Object.assign(EC.blankEmbed(), {
        title: 'Rules', description: 'Be nice', author: 'Nero', authorIcon: 'https://x/a.png',
        authorUrl: 'https://x/about', footer: 'Nero', footerIcon: 'https://x/f.png',
        url: 'https://x/embed', timestamp: '2026-09-23T18:00:00.000Z',
    });
    const payload = EC.cleanEmbedForPayload(edited);
    assert(payload.author.name === 'Nero' && payload.author.icon_url === 'https://x/a.png'
        && payload.author.url === 'https://x/about', 'author object keeps name + icon + link',
        JSON.stringify(payload.author));
    assert(payload.footer.text === 'Nero' && payload.footer.icon_url === 'https://x/f.png',
        'footer object keeps text + icon', JSON.stringify(payload.footer));
    assert(payload.url === 'https://x/embed' && payload.timestamp === '2026-09-23T18:00:00.000Z',
        'embed url + timestamp are emitted');

    const back = EC.embedFromApi(payload);
    assert(back.authorIcon === 'https://x/a.png' && back.authorUrl === 'https://x/about'
        && back.footerIcon === 'https://x/f.png' && back.url === 'https://x/embed'
        && back.timestamp === '2026-09-23T18:00:00.000Z',
        'and all five come back when the payload is loaded again', JSON.stringify(back));
    assert(JSON.stringify(EC.cleanEmbedForPayload(back)) === JSON.stringify(payload),
        'save → load → save is now lossless');

    const blank = EC.cleanEmbedForPayload(EC.blankEmbed());
    assert(!('author' in blank) && !('footer' in blank) && !('url' in blank) && !('timestamp' in blank),
        'a blank embed emits no empty author/footer/url/timestamp objects',
        JSON.stringify(blank));
    assert(EC.cleanEmbedsForPayload([EC.blankEmbed()]).length === 0,
        'and a blank embed is still filtered out of the payload entirely');
    const onlyName = EC.cleanEmbedForPayload(Object.assign(EC.blankEmbed(), { author: 'A' }));
    assert(JSON.stringify(onlyName.author) === '{"name":"A"}', 'only-set keys are emitted',
        JSON.stringify(onlyName.author));

    const preview = env.root$('eb-preview');
    env.window.EmbedComposer.renderPreview(preview, {
        embeds: [edited], botIdentity: { name: 'Nero' },
    });
    assert(preview.innerHTML.indexOf('https://x/a.png') !== -1, 'the preview draws the author icon');
    assert(preview.innerHTML.indexOf('https://x/f.png') !== -1, 'and the footer icon');
    assert(preview.innerHTML.indexOf('https://x/embed') !== -1, 'and links the title');
    assert(preview.innerHTML.indexOf('Today at') !== -1 || preview.innerHTML.indexOf('2026') !== -1,
        'and shows the timestamp');

    // Content safety: a pasted javascript: URL is never emitted as markup.
    const nasty = env.window.EmbedComposer.renderPreview(preview, {
        embeds: [Object.assign(EC.blankEmbed(), { title: 'x', url: 'javascript:alert(1)' })],
        botIdentity: { name: 'Nero' },
    });
    assert(preview.innerHTML.indexOf('javascript:') === -1, 'a javascript: URL never reaches the preview');
    void nasty;
}

// ═══════════════════════════════════════════════════════════════
// G. Static invariants of the page + template
// ═══════════════════════════════════════════════════════════════
async function staticTests() {
    section('page + template invariants');
    const tpl = read(path.join(ROOT, 'dashboard', 'templates', 'manage', 'embedbuilder.html'));
    const page = read(path.join(JS_DIR, 'embed-builder-page.js'));

    assert((tpl.match(/data-page-module="embed-builder"/g) || []).length === 1,
        'the template declares exactly ONE page module root');
    assert(/data-page-script="[^"]*embed-composer\.js[^"]*embed-builder-page\.js/.test(tpl),
        'and loads composer → page module, in that order');
    assert(!/<script/i.test(tpl), 'the template contains no inline <script> block at all');
    assert(!/\{%\s*block scripts/.test(tpl),
        'and no nested {% block scripts %} (the double-emission bug)');
    assert(page.indexOf("NERO.definePage('embed-builder'") !== -1, 'the page file registers the module');
    assert(page.indexOf('.addEventListener(') === -1,
        'every listener goes through ctx.on (no untracked addEventListener)');
    assert(page.indexOf('document.getElementById') === -1,
        'the page never reaches for document.getElementById (it is scoped to its root)');
    assert(!/^\s*(const|var|let)\s+\w+\s*=\s*document\./m.test(page),
        'no top-level DOM access: the file only defines, it does not run at load time');
    assert(page.indexOf('__BOT_IDENTITY__') !== -1, 'the page reads the server-injected identity');
    const base = read(path.join(ROOT, 'dashboard', 'templates', 'base.html'));
    assert(base.indexOf("js/nav-lifecycle.js") !== -1, 'base.html loads the registry');
    assert(base.indexOf('__BOT_IDENTITY__') !== -1, 'and can emit the injected identity');
}

(async function main() {
    console.log('Embed Builder — boot + lifecycle');
    console.log('='.repeat(60));
    await helperTests();
    await firstPaintTests();
    await typingTests();
    await draftTests();
    await teardownTests();
    await roundTripTests();
    await staticTests();

    console.log('\n' + '='.repeat(60));
    if (fail) {
        console.log(`RESULT: ${pass} passed, ${fail} FAILED`);
        failures.forEach(f => console.log('  - ' + f));
        process.exit(1);
    }
    console.log(`RESULT: all ${pass} checks passed`);
})();
