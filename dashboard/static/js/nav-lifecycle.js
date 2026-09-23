// ═══════════════════════════════════════════════════════════════
// NERO DASHBOARD — nav-lifecycle.js
//
// Page modules with a real lifecycle: init once per DOM, destroy on the
// way out.
//
// WHY THIS FILE EXISTS
// --------------------
// Every page script in this dashboard used to be an inline `<script>`
// emitted at the BOTTOM of the page (base.html `{% block scripts %}`,
// which sits outside `#content-area`). htmx navigates by fetching the
// full page, selecting `#content-area` (hx-select) and swapping its
// innerHTML — so that script never arrives with the fragment, never
// re-executes, and a page that needs JS comes back inert until a hard
// refresh. The Embed Builder hit exactly that, plus two more:
//
//   * manage/embedbuilder.html declared `{% block scripts %}` INSIDE its
//     `{% block content %}`. Jinja renders a nested block override where
//     the child puts it AND where the parent declares it, so the builder's
//     ~1000-line script was emitted TWICE per page load and executed
//     twice: two independent copies of the page state, two listeners per
//     element, two IndexedDB restores.
//   * On an htmx navigation the fragment's `<script src>` is re-created
//     dynamically (`base.html`'s afterSwap handler does that to make
//     swapped scripts run at all), and a dynamically inserted external
//     script loads ASYNC — so the inline script that followed it ran
//     before `window.EmbedComposer` existed and threw on line 1.
//
// The fix is not "refresh the page" and not "re-run the script and hope".
// It is:
//
//   1. Page JavaScript lives in a file (never inline in the fragment),
//      so it is loaded once and can be re-mounted without re-parsing.
//   2. A page announces itself in the DOM:
//        <div data-page-module="embed-builder"
//             data-page-script="/static/js/embed-composer.js /static/js/embed-builder-page.js">
//      The registry loads those scripts (in order, once, cached) and then
//      calls the module's init(root, ctx).
//   3. `destroy()` runs BEFORE the DOM is swapped away, and the `ctx`
//      handed to init tracks every listener, timer, blob URL and fetch,
//      so teardown is structural instead of a checklist somebody forgets.
//
// WHAT IT DOES NOT DO
// -------------------
// dashboard.js's `reInitDashboardComponents()` (NeroSelect / NeroAlias)
// stays exactly where it is and keeps owning those widgets; this registry
// complements it and runs after it, so a page module can rely on its
// pickers already being initialised. Nothing here touches htmx's
// configuration, the CSRF patch, or any existing page that declares no
// module — for those pages this file is inert.
//
// Consumed by: dashboard/templates/base.html (one script tag).
// Tested by:  scripts/test_nav_lifecycle.js (no DOM required — pass one in).
// ═══════════════════════════════════════════════════════════════
window.NERO = window.NERO || {};

(function (NERO) {
    'use strict';

    var pages = NERO.pages = NERO.pages || {};   // name -> { init, destroy }
    var scripts = {};                            // url -> Promise (load once)
    var mounted = null;                          // { name, root, ctx, def }
    var sequence = 0;                            // guards async mounts
    var stats = { mounts: 0, destroys: 0, skipped: 0, errors: 0,
                  lastPage: null, lastCounters: null };
    var lastError = null;
    var log = [];                                // dev-only ring buffer

    // ── Dev switch ────────────────────────────────────────────────
    // Off unless asked for: ?debug=1 in the URL, or
    // localStorage.nero_debug === '1'. Production stays silent.
    var debugEnabled = (function () {
        try {
            if (typeof window.location === 'object' && window.location &&
                /[?&]debug=1\b/.test(window.location.search || '')) return true;
            return window.localStorage && window.localStorage.getItem('nero_debug') === '1';
        } catch (e) { return false; }
    })();

    function debugEvent(kind, data) {
        if (!debugEnabled) return;
        log.push({ t: Date.now(), kind: kind, data: data || null });
        if (log.length > 200) log.shift();
        if (window.console && console.debug) {
            console.debug('[nero:' + kind + ']', data || '');
        }
    }

    function now() {
        if (window.performance && typeof window.performance.now === 'function') {
            return window.performance.now();
        }
        return Date.now();
    }

    // ── Script loading ────────────────────────────────────────────
    // One <script> per URL, cached forever: navigating back to a page must
    // not re-fetch (and must not re-parse) its module.
    function loadScript(url) {
        if (scripts[url]) return scripts[url];
        scripts[url] = new Promise(function (resolve, reject) {
            var el = document.createElement('script');
            el.src = url;
            el.async = false;           // keep declaration order inside the list
            el.onload = function () { debugEvent('script', url); resolve(url); };
            el.onerror = function () {
                delete scripts[url];    // allow a retry on the next mount
                reject(new Error('failed to load ' + url));
            };
            (document.head || document.body || document.documentElement).appendChild(el);
        });
        return scripts[url];
    }

    function loadScripts(urls) {
        return urls.reduce(function (chain, url) {
            return chain.then(function () { return loadScript(url); });
        }, Promise.resolve());
    }

    function scriptUrlsFor(rootEl) {
        var raw = (rootEl.getAttribute('data-page-script') || '').trim();
        return raw ? raw.split(/\s+/).filter(Boolean) : [];
    }

    function pageRootWithin(scope) {
        if (!scope || !scope.querySelector) return null;
        if (scope.getAttribute && scope.getAttribute('data-page-module')) return scope;
        return scope.querySelector('[data-page-module]');
    }

    // ── The per-mount context ─────────────────────────────────────
    // Everything a page module allocates goes through here, so destroy()
    // cannot miss anything: listeners are removed, timers cleared, blob
    // URLs revoked and in-flight fetches aborted — in that order, always.
    function createContext(name, root) {
        var listeners = [];
        var timers = [];
        var intervals = [];
        var objectUrls = [];
        var cleanups = [];
        var controller = (typeof AbortController === 'function') ? new AbortController() : null;
        var destroyed = false;

        var ctx = {
            name: name,
            root: root,
            counters: {},
            marks: {},
            signal: controller ? controller.signal : null,
            isDestroyed: function () { return destroyed; },

            on: function (target, type, handler, opts) {
                if (!target || !target.addEventListener) return handler;
                target.addEventListener(type, handler, opts);
                listeners.push({ target: target, type: type, handler: handler, opts: opts });
                return handler;
            },
            off: function (target, type, handler, opts) {
                if (!target || !target.removeEventListener) return;
                target.removeEventListener(type, handler, opts);
                listeners = listeners.filter(function (l) {
                    return !(l.target === target && l.type === type && l.handler === handler);
                });
            },
            timeout: function (fn, ms) {
                var id = setTimeout(function () {
                    timers = timers.filter(function (t) { return t !== id; });
                    if (!destroyed) fn();
                }, ms);
                timers.push(id);
                return id;
            },
            clearTimeout: function (id) {
                clearTimeout(id);
                timers = timers.filter(function (t) { return t !== id; });
            },
            interval: function (fn, ms) {
                var id = setInterval(function () { if (!destroyed) fn(); }, ms);
                intervals.push(id);
                return id;
            },
            clearInterval: function (id) {
                clearInterval(id);
                intervals = intervals.filter(function (t) { return t !== id; });
            },
            // A blob: URL that dies with the page.
            url: function (blob) {
                var url = null;
                try {
                    url = URL.createObjectURL(blob);
                } catch (e) {
                    console.error('[nero] createObjectURL failed', e);
                    return null;
                }
                objectUrls.push(url);
                return url;
            },
            revoke: function (url) {
                if (!url) return;
                try { URL.revokeObjectURL(url); } catch (e) { /* already gone */ }
                objectUrls = objectUrls.filter(function (u) { return u !== url; });
            },
            // Fetches the page starts are cancelled when the page goes away.
            fetch: function (input, init) {
                init = init || {};
                if (controller && !init.signal) init.signal = controller.signal;
                return fetch(input, init);
            },
            fetchJSON: function (input, init) {
                return ctx.fetch(input, init).then(function (res) {
                    if (!res.ok) throw new Error('HTTP ' + res.status);
                    return res.json();
                });
            },
            cleanup: function (fn) { if (typeof fn === 'function') cleanups.push(fn); },
            counter: function (key, delta) {
                ctx.counters[key] = (ctx.counters[key] || 0) + (delta === undefined ? 1 : delta);
                return ctx.counters[key];
            },
            mark: function (name) {
                var t = now();
                ctx.marks[name] = t;
                return t;
            },
            debug: function (kind, data) { debugEvent(kind, data); },

            _destroy: function () {
                destroyed = true;
                if (controller) { try { controller.abort(); } catch (e) { /* fine */ } }
                timers.forEach(clearTimeout);
                intervals.forEach(clearInterval);
                listeners.slice().forEach(function (l) {
                    try { l.target.removeEventListener(l.type, l.handler, l.opts); } catch (e) { /* fine */ }
                });
                objectUrls.slice().forEach(function (u) {
                    try { URL.revokeObjectURL(u); } catch (e) { /* fine */ }
                });
                cleanups.slice().forEach(function (fn) {
                    try { fn(); } catch (e) { console.error('[nero] cleanup failed', e); }
                });
                timers = []; intervals = []; listeners = []; objectUrls = []; cleanups = [];
                return { listeners: listeners.length };
            },
            _sizes: function () {
                return {
                    listeners: listeners.length,
                    timers: timers.length,
                    intervals: intervals.length,
                    objectUrls: objectUrls.length,
                    cleanups: cleanups.length,
                };
            },
        };
        return ctx;
    }

    // ── Mount / unmount ───────────────────────────────────────────
    function unmount(reason) {
        if (!mounted) return false;
        var current = mounted;
        mounted = null;
        try {
            if (current.def && typeof current.def.destroy === 'function') {
                current.def.destroy(current.root, current.ctx);
            }
        } catch (e) {
            stats.errors++;
            lastError = 'destroy(' + current.name + '): ' + e.message;
            console.error('[nero] destroy failed for ' + current.name, e);
        }
        var sizes = current.ctx._sizes();
        // Keep the counters of the page that just left: the acceptance
        // evidence for "typing does not rebuild the editor" is read AFTER
        // navigating away as often as while the page is open.
        stats.lastCounters = Object.assign({}, current.ctx.counters);
        stats.lastPage = current.name;
        current.ctx._destroy();
        stats.destroys++;
        debugEvent('unmount', { name: current.name, reason: reason, held: sizes });
        return true;
    }

    function mount(scope) {
        var rootEl = pageRootWithin(scope) || pageRootWithin(document);
        if (!rootEl) {
            // Navigated to a page with no module (almost every page). The
            // previous module must still be torn down — htmx already
            // replaced its DOM.
            unmount('no-module');
            return Promise.resolve(false);
        }
        if (mounted && mounted.root === rootEl && !mounted.ctx.isDestroyed()) {
            stats.skipped++;
            debugEvent('mount-skip', { name: mounted.name });
            return Promise.resolve(false);
        }
        // A different page root, or the same page re-swapped: the old
        // instance must go first, whatever its state.
        unmount('remount');

        var name = rootEl.getAttribute('data-page-module');
        var def = pages[name];
        var urls = scriptUrlsFor(rootEl);
        var mySeq = ++sequence;
        var t0 = now();

        function begin() {
            def = pages[name];   // may have been defined by the script we just loaded
            if (!def || typeof def.init !== 'function') {
                stats.errors++;
                lastError = 'no module registered for "' + name + '"';
                console.error('[nero] no page module named "' + name + '"');
                rootEl.setAttribute('data-page-error', 'no-module');
                return false;
            }
            var ctx = createContext(name, rootEl);
            mounted = { name: name, root: rootEl, ctx: ctx, def: def };
            stats.mounts++;
            ctx.mark('initStart');
            try {
                def.init(rootEl, ctx);
            } catch (e) {
                // A page that throws on init still got a ctx: tear it down
                // rather than leaving half a page wired to nothing.
                stats.errors++;
                lastError = 'init(' + name + '): ' + (e && e.message);
                rootEl.setAttribute('data-page-error', 'init-failed');
                console.error('[nero] init failed for ' + name, e);
                unmount('init-failed');
                return false;
            }
            ctx.mark('initEnd');
            ctx.counters.initMs = Math.round(ctx.marks.initEnd - ctx.marks.initStart);
            ctx.counters.loadMs = Math.round(ctx.marks.initStart - t0);
            debugEvent('mount', { name: name, loadMs: ctx.counters.loadMs, initMs: ctx.counters.initMs });
            return true;
        }

        if (!def && urls.length) {
            return loadScripts(urls).then(function () {
                if (mySeq !== sequence) return false;            // a newer navigation won
                if (!document.contains(rootEl)) return false;     // already swapped away
                return begin();
            }, function (err) {
                stats.errors++;
                lastError = 'script load: ' + err.message;
                rootEl.setAttribute('data-page-error', 'script-load-failed');
                console.error('[nero] ' + err.message);
                return false;
            });
        }
        return Promise.resolve(begin());
    }

    // ── htmx + document wiring ────────────────────────────────────
    // beforeSwap: the DOM is about to be replaced — release everything
    // first, while the elements still exist.
    document.addEventListener('htmx:beforeSwap', function (evt) {
        if (!mounted) return;
        var target = evt.detail && evt.detail.target;
        if (!target || target === mounted.root || target.contains(mounted.root)) {
            unmount('beforeSwap');
        }
    });
    document.addEventListener('htmx:afterSwap', function (evt) {
        mount(evt.detail && evt.detail.target);
    });
    document.addEventListener('htmx:load', function (evt) {
        // Fired for content inserted outside the afterSwap flow (boosted
        // links). Mount is a no-op when this page root is already mounted.
        mount((evt.detail && (evt.detail.elt || evt.detail.target)) || document);
    });
    document.addEventListener('htmx:historyRestore', function () {
        mount(document);
    });
    // htmx leaves the old content in place when a request fails, so the old
    // module stays mounted and usable — nothing to destroy. Recorded so the
    // dev HUD can tell "nothing happened" from "nothing happened and it
    // should have".
    ['htmx:responseError', 'htmx:sendError', 'htmx:timeout'].forEach(function (type) {
        document.addEventListener(type, function (evt) {
            debugEvent('request-failed', { type: type, page: mounted && mounted.name });
            if (mounted) mounted.ctx.counter('requestFailures');
            var path = evt.detail && evt.detail.pathInfo && evt.detail.pathInfo.requestPath;
            if (path) lastError = 'htmx request failed: ' + path;
        });
    });
    window.addEventListener('pagehide', function () { unmount('pagehide'); });

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function () { mount(document); });
    } else {
        mount(document);   // script is at the end of <body>: DOM is ready
    }

    // ── Public surface ────────────────────────────────────────────
    NERO.definePage = function (name, def) {
        // Defining twice (a re-executed script) replaces quietly instead of
        // stacking: only the registry decides when init runs.
        pages[name] = def || {};
        debugEvent('define', name);
    };
    NERO.lifecycle = {
        mount: mount,
        unmount: unmount,
        isDebug: function () { return debugEnabled; },
        enableDebug: function () { debugEnabled = true; },
    };
    NERO.debug = {
        report: function () {
            return {
                debug: debugEnabled,
                page: mounted ? mounted.name : null,
                counters: mounted ? Object.assign({}, mounted.ctx.counters)
                                  : Object.assign({}, stats.lastCounters || {}),
                marks: mounted ? Object.assign({}, mounted.ctx.marks) : {},
                held: mounted ? mounted.ctx._sizes() : null,
                stats: Object.assign({}, stats),
                loadedScripts: Object.keys(scripts),
                lastError: lastError,
                log: log.slice(-40),
            };
        },
        log: function () { return log.slice(); },
        lastError: function () { return lastError; },
    };
})(window.NERO);
