#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════
   nav-lifecycle.js — page module registry (node)

   This is the Phase 0 lifecycle fix under test: pages that mount when
   their DOM appears, tear down before the DOM goes away, and leave
   nothing behind when the user navigates in circles.

   It is tested WITHOUT jsdom on purpose. The registry only needs
   addEventListener/removeEventListener, appendChild and attribute
   access, so the harness builds a small DOM double — which also means
   the suite can ASSER T on the exact listener/timer/object-URL counts
   the registry is responsible for (a real DOM would only let us ask
   "did it work?").

   Covered:
     * define → load (in order, once) → init, for a module whose script
       is not in the page yet
     * init runs exactly once per DOM, even though htmx fires
       afterSwap AND load for the same insertion
     * destroy runs BEFORE a swap removes the DOM (beforeSwap), and
       again on pagehide
     * ctx.on / ctx.timeout / ctx.interval / ctx.url / ctx.cleanup are
       all undone, and ctx.fetch is aborted
     * five enter/leave cycles leave listener + timer + URL counts flat
     * a re-executed script (the double-<script> bug that started this)
       does not double-init
     * a module that throws in init is torn down, not left half-wired
     * a script that fails to load is reported and retried next time
     * pages with no module tear down the previous page's module
     * the old page's counters survive into the next report

   Run:  node scripts/test_nav_lifecycle.js
   ═══════════════════════════════════════════════════════════════ */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

let pass = 0, fail = 0; const failures = [];
function assert(cond, name, extra) {
    if (cond) { pass++; console.log('  PASS', name); }
    else { fail++; failures.push(name + (extra ? ' — ' + extra : '')); console.log('  FAIL', name, extra || ''); }
}
function section(t) { console.log('\n== ' + t + ' =='); }
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// ═══════════════════════════════════════════════════════════════
// A DOM small enough to reason about, big enough to run the registry.
// ═══════════════════════════════════════════════════════════════
function makeEnv() {
    const created = [];                       // every element ever made
    const urls = { created: [], revoked: [] };

    function makeEl(tag) {
        const el = {
            tagName: String(tag || 'div').toUpperCase(),
            children: [],
            parentNode: null,
            attributes: {},
            dataset: {},
            listeners: [],                    // {type, handler, opts}
            getAttribute(n) { return n in this.attributes ? this.attributes[n] : null; },
            setAttribute(n, v) { this.attributes[n] = String(v); },
            hasAttribute(n) { return n in this.attributes; },
            appendChild(child) {
                child.parentNode = this;
                this.children.push(child);
                return child;
            },
            removeChild(child) {
                this.children = this.children.filter(c => c !== child);
                child.parentNode = null;
                return child;
            },
            addEventListener(type, handler, opts) {
                this.listeners.push({ type, handler, opts });
                env.listenerAdds++;
            },
            removeEventListener(type, handler, opts) {
                const before = this.listeners.length;
                this.listeners = this.listeners.filter(
                    l => !(l.type === type && l.handler === handler));
                env.listenerRemoves += before - this.listeners.length;
            },
            querySelector(sel) {
                // only '[data-page-module]' is needed by the registry
                if (sel !== '[data-page-module]') throw new Error('unsupported selector ' + sel);
                let found = null;
                (function walk(node) {
                    for (const c of node.children) {
                        if (found) return;
                        if (c.attributes['data-page-module']) { found = c; return; }
                        walk(c);
                    }
                })(this);
                return found;
            },
            contains(node) {
                let cur = node;
                while (cur) {
                    if (cur === this) return true;
                    cur = cur.parentNode;
                }
                return false;
            },
            get textContent() { return this._text || ''; },
            set textContent(v) { this._text = String(v); },
            set src(v) { this.attributes.src = v; this.loadSrc(v); },
            get src() { return this.attributes.src; },
        };
        el.classList = {
            _set: new Set(),
            add(c) { this._set.add(c); },
            remove(c) { this._set.delete(c); },
            contains(c) { return this._set.has(c); },
            toggle(c, on) { on === undefined ? (this._set.has(c) ? this._set.delete(c) : this._set.add(c)) : (on ? this._set.add(c) : this._set.delete(c)); },
        };
        created.push(el);
        return el;
    }

    const document = makeEl('document');
    document.head = makeEl('head');
    document.body = makeEl('body');
    document.documentElement = makeEl('html');
    document.appendChild(document.head);
    document.appendChild(document.body);
    document.readyState = 'complete';
    document.createElement = makeEl;
    document.contains = function (el) {
        let node = el;
        while (node) {
            if (node === document || node === document.documentElement) return true;
            node = node.parentNode;
        }
        return false;
    };

    const window = {
        NERO: undefined,
        console,
        location: { search: '' },
        localStorage: { getItem: () => null, setItem: () => {} },
        performance: { now: () => Number(process.hrtime.bigint() / 1000n) / 1000 },
        listeners: [],
        addEventListener(type, handler) { this.listeners.push({ type, handler }); },
        setTimeout,
        clearTimeout,
        setInterval,
        clearInterval,
        AbortController: typeof AbortController === 'function' ? AbortController : undefined,
        Promise,
        fetch: null,      // injected per-test
    };

    const env = {
        window, document, created, urls,
        listenerAdds: 0, listenerRemoves: 0,
        // The two htmx events the registry hooks, plus a way to fire them.
        fire(type, detail) {
            document.listeners
                .filter(l => l.type === type)
                .forEach(l => l.handler({ type, detail }));
        },
        pageListeners(target) { return target.listeners.length; },
        counts: null,   // set by loadRegistry
        scripts: {},
    };

    // A <script> the registry appends: "load" as soon as src is set, and run
    // the fake source registered for that URL in env.scripts.
    function loadSrc(url) {
        const src = env.scripts[url];
        if (src === undefined) {
            setTimeout(() => this.onerror && this.onerror(new Error('404 ' + url)), 0);
            return;
        }
        if (typeof src === 'function') {
            setTimeout(() => {
                try { src(env.window); this.onload && this.onload(); }
                catch (e) { this.onerror && this.onerror(e); }
            }, 0);
        } else {
            setTimeout(() => this.onload && this.onload(), 0);
        }
    }
    env._loadSrc = loadSrc;
    // every script element created by the registry goes through set src
    const realCreate = document.createElement;
    document.createElement = function (tag) {
        const el = realCreate(tag);
        if (String(tag).toLowerCase() !== 'script') return el;
        Object.defineProperty(el, 'src', {
            set(v) { el.attributes.src = v; loadSrc.call(el, v); },
            get() { return el.attributes.src; },
        });
        return el;
    };

    Object.assign(env, { document, window });
    // URL with counting, shared by the registry and the tests
    env.window.URL = {
        createObjectURL(blob) { const u = 'blob:' + (urls.created.length + 1); urls.created.push({ u, blob }); return u; },
        revokeObjectURL(u) { urls.revoked.push(u); },
    };
    global.URL = env.window.URL;
    return env;
}

// Load nav-lifecycle.js into the fake environment.
function loadRegistry(env) {
    const src = fs.readFileSync(
        path.join(__dirname, '..', 'dashboard', 'static', 'js', 'nav-lifecycle.js'), 'utf8');
    const sandbox = {
        window: env.window,
        document: env.document,
        console,
        setTimeout, clearTimeout, setInterval, clearInterval,
        Promise, Error, Object, Array, Math, Date, JSON, Number, String, RegExp,
        AbortController: env.window.AbortController,
        URL: env.window.URL,
        fetch: (...args) => env.window.fetch(...args),
    };
    // the file reads `window.NERO`, `window.location`, `window.localStorage`
    vm.createContext(sandbox);
    vm.runInContext(src, sandbox, { filename: 'nav-lifecycle.js' });
    return env.window.NERO;
}

// Build a page root element and put it in a #content-area double.
function addPageRoot(env, { name = 'demo-page', scripts = '', area = null } = {}) {
    const rootEl = env.document.createElement('div');
    rootEl.setAttribute('data-page-module', name);
    if (scripts) rootEl.setAttribute('data-page-script', scripts);
    (area || env.area || env.document.body).appendChild(rootEl);
    return rootEl;
}

function makeArea(env) {
    const area = env.document.createElement('div');
    area.setAttribute('id', 'content-area');
    env.document.body.appendChild(area);
    env.area = area;
    return area;
}

// ═══════════════════════════════════════════════════════════════
// 1. Module definition + first mount
// ═══════════════════════════════════════════════════════════════
async function basicTests() {
    section('define → load → init');
    const env = makeEnv();
    const NERO = loadRegistry(env);
    makeArea(env);

    assert(typeof NERO.definePage === 'function' && typeof NERO.lifecycle.mount === 'function',
        'registry exposes definePage + lifecycle.mount');

    const initCalls = [];
    env.scripts['/static/js/demo.js'] = (win) => {
        win.NERO.definePage('demo-page', {
            init(root, ctx) { initCalls.push(root); ctx.counter('init'); },
        });
    };

    addPageRoot(env, { scripts: '/static/js/demo.js' });
    await NERO.lifecycle.mount(env.area);
    assert(initCalls.length === 1, 'the module script was loaded and init ran once', 'calls=' + initCalls.length);
    assert(NERO.debug.report().counters.init === 1, 'ctx.counters collected the init counter');

    // htmx fires afterSwap AND load for the same insertion
    await NERO.lifecycle.mount(env.area);
    await NERO.lifecycle.mount(env.area);
    assert(initCalls.length === 1, 'duplicate htmx events do not re-init the same DOM',
        'calls=' + initCalls.length);
    assert(NERO.debug.report().stats.skipped >= 2, 'the skips are visible in the report');
}

// ═══════════════════════════════════════════════════════════════
// 2. Teardown: listeners, timers, object URLs, fetch
// ═══════════════════════════════════════════════════════════════
async function teardownTests() {
    section('destroy releases everything the page took');
    const env = makeEnv();
    const NERO = loadRegistry(env);
    const area = makeArea(env);

    let aborted = false;
    let tick = 0;
    const state = {};
    env.window.fetch = (url, init) => new Promise((resolve, reject) => {
        if (init && init.signal) {
            init.signal.addEventListener('abort', () => { aborted = true; reject(new Error('aborted')); });
        }
    });
    env.scripts['/static/js/demo.js'] = (win) => {
        win.NERO.definePage('demo-page', {
            init(root, ctx) {
                state.ctx = ctx;
                ctx.on(env.document, 'keydown', () => {});
                ctx.on(root, 'click', () => {});
                ctx.timeout(() => { tick++; }, 5);
                ctx.interval(() => { tick++; }, 5);
                ctx.url(new Blob());
                ctx.cleanup(() => { state.cleaned = true; });
                ctx.fetch('/api/something').catch(() => {});
            },
        });
    };
    addPageRoot(env, { scripts: '/static/js/demo.js' });
    await NERO.lifecycle.mount(area);
    await sleep(2);

    const held = NERO.debug.report().held;
    assert(held.listeners === 2, 'the context is holding both listeners', JSON.stringify(held));
    assert(held.timers === 1 && held.intervals === 1, 'and the timer + interval');
    assert(held.objectUrls === 1, 'and the object URL');

    const documentListenersBefore = env.pageListeners(env.document);
    env.fire('htmx:beforeSwap', { target: area });
    const after = NERO.debug.report();
    assert(state.cleaned === true, 'ctx.cleanup callbacks ran');
    assert(after.held === null, 'nothing left mounted');
    assert(aborted, 'the in-flight fetch was aborted');
    assert(env.urls.revoked.length === 1, 'the object URL was revoked');
    assert(env.pageListeners(env.document) === documentListenersBefore - 1,
        'the document listener was removed',
        `before=${documentListenersBefore} after=${env.pageListeners(env.document)}`);

    const tickedAt = tick;
    await sleep(25);
    assert(tick === tickedAt, 'the interval stopped ticking', `tick ${tickedAt} → ${tick}`);
    assert(state.ctx.isDestroyed() === true, 'the context reports itself destroyed');
}

// ═══════════════════════════════════════════════════════════════
// 3. Five enter/leave cycles: nothing accumulates
// ═══════════════════════════════════════════════════════════════
async function cycleTests() {
    section('5× sidebar round-trip');
    const env = makeEnv();
    const NERO = loadRegistry(env);
    const area = makeArea(env);
    let scriptLoads = 0;

    env.scripts['/static/js/demo.js'] = (win) => {
        scriptLoads++;
        win.NERO.definePage('demo-page', {
            init(root, ctx) {
                ctx.on(env.document, 'keydown', () => {});
                ctx.on(env.document, 'click', () => {});
                ctx.timeout(() => {}, 1000);
            },
        });
    };

    const docListeners = () => env.pageListeners(env.document);
    const baseline = docListeners();
    const samples = [];

    for (let i = 0; i < 5; i++) {
        // entering the builder: htmx swaps in the fragment, then afterSwap fires
        addPageRoot(env, { scripts: '/static/js/demo.js' });
        await NERO.lifecycle.mount(area);
        samples.push({ cycle: i + 1, mounted: docListeners() - baseline, loads: scriptLoads });
        // leaving it: htmx swaps to a page with no module
        env.fire('htmx:beforeSwap', { target: area });
        env.area.children = [];
        await NERO.lifecycle.mount(area);
    }

    assert(scriptLoads === 1, 'the page script is fetched once, not once per visit', 'loads=' + scriptLoads);
    assert(samples.every(s => s.mounted === 2), 'exactly two document listeners while mounted, every cycle',
        JSON.stringify(samples));
    assert(docListeners() === baseline, 'back to the baseline listener count after leaving',
        `baseline=${baseline} now=${docListeners()}`);
    const report = NERO.debug.report();
    assert(report.stats.mounts === 5 && report.stats.destroys === 5,
        'five mounts, five destroys', JSON.stringify(report.stats));
    assert(report.held === null, 'nothing held at the end');
    assert(report.counters && report.counters.initMs >= 0,
        'the last page counters survive for the report', JSON.stringify(report.counters));
}

// ═══════════════════════════════════════════════════════════════
// 4. The double-script bug, failure modes, and pages without modules
// ═══════════════════════════════════════════════════════════════
async function edgeTests() {
    section('edge cases');

    // (a) a re-executed script must not double-init
    {
        const env = makeEnv(); const NERO = loadRegistry(env); const area = makeArea(env);
        let inits = 0;
        env.scripts['/static/js/demo.js'] = (win) => {
            win.NERO.definePage('demo-page', { init() { inits++; } });
        };
        addPageRoot(env, { scripts: '/static/js/demo.js' });
        await NERO.lifecycle.mount(area);
        // simulate the page script being executed a second time (the bug the
        // old inline-in-content block produced on every hard load)
        const again = new Function('win', env.scripts['/static/js/demo.js'].toString().replace(/^[^{]*{/, '').replace(/}[^}]*$/, ''));
        env.scripts['/static/js/demo.js'](env.window);
        void again;
        await NERO.lifecycle.mount(area);
        assert(inits === 1, 're-defining the page module does not re-run init', 'inits=' + inits);
    }

    // (b) init throws → torn down, reported, page not left half-wired
    {
        const env = makeEnv(); const NERO = loadRegistry(env); const area = makeArea(env);
        env.scripts['/static/js/bad.js'] = (win) => {
            win.NERO.definePage('bad-page', {
                init(root, ctx) { ctx.on(env.document, 'keydown', () => {}); throw new Error('boom'); },
            });
        };
        const root = addPageRoot(env, { name: 'bad-page', scripts: '/static/js/bad.js' });
        const before = env.pageListeners(env.document);
        await NERO.lifecycle.mount(area);
        assert(env.pageListeners(env.document) === before, 'the listener it registered before throwing was removed');
        assert(root.getAttribute('data-page-error') === 'init-failed', 'the DOM records why the page is dead');
        assert(/boom/.test(NERO.debug.lastError()), 'the error is reported, not swallowed',
            NERO.debug.lastError());
    }

    // (c) script that fails to load → reported, and retried on the next mount
    {
        const env = makeEnv(); const NERO = loadRegistry(env); const area = makeArea(env);
        const root = addPageRoot(env, { scripts: '/static/js/missing.js' });
        await NERO.lifecycle.mount(area);
        assert(root.getAttribute('data-page-error') === 'script-load-failed', 'a failed load is recorded');
        assert(NERO.debug.report().stats.errors >= 1, 'and counted');
        // the file appears, the user navigates back
        env.scripts['/static/js/missing.js'] = (win) => {
            win.NERO.definePage('demo-page', { init() { win.__mounted = true; } });
        };
        await NERO.lifecycle.mount(area);
        assert(env.window.__mounted === true, 'the retry succeeds once the script exists');
    }

    // (d) navigating to a page with no module tears the old one down
    {
        const env = makeEnv(); const NERO = loadRegistry(env); const area = makeArea(env);
        let destroyed = 0;
        NERO.definePage('demo-page', { init() {}, destroy() { destroyed++; } });
        addPageRoot(env);
        await NERO.lifecycle.mount(area);
        env.area.children = [];           // htmx swapped in a module-less page
        await NERO.lifecycle.mount(area);
        assert(destroyed === 1, 'the previous module was destroyed on the way out', 'n=' + destroyed);
        assert(NERO.debug.report().page === null, 'no page is mounted now');
    }

    // (e) pagehide (tab close / browser navigation)
    {
        const env = makeEnv(); const NERO = loadRegistry(env); const area = makeArea(env);
        let destroyed = 0;
        NERO.definePage('demo-page', { init() {}, destroy() { destroyed++; } });
        addPageRoot(env);
        await NERO.lifecycle.mount(area);
        env.window.listeners.filter(l => l.type === 'pagehide').forEach(l => l.handler());
        assert(destroyed === 1, 'pagehide destroys the mounted module');
    }

    // (f) a swap that only replaces part of the page keeps the module alive
    {
        const env = makeEnv(); const NERO = loadRegistry(env); const area = makeArea(env);
        let destroyed = 0;
        NERO.definePage('demo-page', { init() {}, destroy() { destroyed++; } });
        const root = addPageRoot(env);
        await NERO.lifecycle.mount(area);
        const inner = env.document.createElement('div');
        root.appendChild(inner);
        env.fire('htmx:beforeSwap', { target: inner });
        await NERO.lifecycle.mount(inner);
        assert(destroyed === 0, 'an inner htmx swap does not tear the page down', 'n=' + destroyed);
        assert(NERO.debug.report().page === 'demo-page', 'the module is still mounted');
    }

    // (g) two page scripts load in declaration order
    {
        const env = makeEnv(); const NERO = loadRegistry(env); const area = makeArea(env);
        const order = [];
        env.scripts['/static/js/one.js'] = (win) => { order.push('one'); win.__one = true; };
        env.scripts['/static/js/two.js'] = (win) => {
            order.push('two');
            win.NERO.definePage('demo-page', { init() { win.__initOrder = order.slice(); } });
        };
        addPageRoot(env, { scripts: '/static/js/one.js /static/js/two.js' });
        await NERO.lifecycle.mount(area);
        assert(order.join(',') === 'one,two', 'scripts execute in the declared order', order.join(','));
        assert(env.window.__initOrder.join(',') === 'one,two', 'init runs after both');
    }

    // (h) after a swap that lands while a load is still in flight
    {
        const env = makeEnv(); const NERO = loadRegistry(env); const area = makeArea(env);
        env.scripts['/static/js/slow.js'] = (win) => {
            win.NERO.definePage('slow-page', { init() { win.__slowMounted = true; } });
        };
        env.scripts['/static/js/fast.js'] = (win) => {
            win.NERO.definePage('fast-page', { init() { win.__fastMounted = true; } });
        };
        addPageRoot(env, { name: 'slow-page', scripts: '/static/js/slow.js' });
        const p = NERO.lifecycle.mount(area);
        // the user navigates on before the script lands: htmx swaps #content-area
        env.area.children = [];
        addPageRoot(env, { name: 'fast-page', scripts: '/static/js/fast.js' });
        await NERO.lifecycle.mount(area);
        await p;
        assert(env.window.__fastMounted === true && !env.window.__slowMounted,
            'the stale mount is dropped, the new one wins',
            `slow=${env.window.__slowMounted} fast=${env.window.__fastMounted}`);
    }
}

(async function main() {
    console.log('nav-lifecycle.js — page module registry verification');
    console.log('='.repeat(60));
    await basicTests();
    await teardownTests();
    await cycleTests();
    await edgeTests();

    console.log('\n' + '='.repeat(60));
    if (fail) {
        console.log(`RESULT: ${pass} passed, ${fail} FAILED`);
        failures.forEach(f => console.log('  - ' + f));
        process.exit(1);
    }
    console.log(`RESULT: all ${pass} checks passed`);
})();
