// ═══════════════════════════════════════════════════════════════
// scripts/support/dom_stub.js — the DOM + IndexedDB double behind
// scripts/test_message_builder_page.js and
// scripts/test_message_builder_layout.js.
//
// WHY A DOUBLE AND NOT jsdom:
//   the dashboard ships hand-written JS with no build step and no runtime
//   dependency, and CI must stay that way (scripts/run_js_tests.sh runs every
//   scripts/test_*.js with plain node). A double is also STRICTER than a real
//   DOM for the properties this page has to keep: it counts every DOM write, it
//   refuses to be helpful, and it makes "did the page rebuild this node?"
//   answerable by object identity instead of by inspecting the screen.
//
//   It is a superset of the two doubles already in the suite
//   (test_embed_builder_boot.js for lifecycle pages, test_preview.js for the
//   preview engine) so the v2 page can boot the REAL nav-lifecycle.js, store,
//   preview and drafts modules in one sandbox.
//
// NOT A HARNESS: this file is in scripts/support/, not scripts/test_*.js, so
// scripts/run_js_tests.sh does not run it as a suite of its own.
// ═══════════════════════════════════════════════════════════════
'use strict';

/**
 * A DOM double.
 *   ops       — an independent count of every DOM call the code under test
 *               makes, so the module's own stats are never the only evidence.
 *   warnings  — traps a browser would silently accept but that mean the code
 *               replaced children it still needed (textContent / innerHTML on a
 *               node with children).
 */
function createDom() {
    const warnings = [];
    let uid = 0;
    const ops = {
        elementsCreated: 0, textNodesCreated: 0, appendChild: 0, insertBefore: 0,
        removeChild: 0, replaceChild: 0, setAttribute: 0, removeAttribute: 0,
        classChanged: 0, textContentSet: 0, innerHTMLSet: 0,
    };

    function element(tag) {
        ops.elementsCreated++;
        let classNameValue = '';
        let classListCache = null;

        const node = {
            __uid: ++uid,
            nodeType: 1,
            tagName: String(tag || 'div').toUpperCase(),
            children: [],
            parentNode: null,
            ownerDocument: null,
            attributes: {},
            dataset: {},
            style: {},
            listeners: [],
            value: '',
            checked: false,
            disabled: false,
            readOnly: false,
            tabIndex: 0,
            selectionStart: 0,
            selectionEnd: 0,
            _text: '',
            _html: '',

            get nodeName() { return this.tagName; },
            get id() { return this.attributes.id || ''; },
            set id(v) { this.attributes.id = String(v); },
            get hidden() { return Object.prototype.hasOwnProperty.call(this.attributes, 'hidden'); },
            set hidden(v) { if (v) this.attributes.hidden = ''; else delete this.attributes.hidden; },

            get className() { return classNameValue; },
            set className(v) {
                const next = v == null ? '' : String(v);
                if (next !== classNameValue) ops.classChanged++;
                classNameValue = next;
            },
            get classList() {
                if (classListCache) return classListCache;
                const self = this;
                const read = () => self.className.split(/\s+/).filter(Boolean);
                classListCache = {
                    contains(c) { return read().indexOf(c) !== -1; },
                    add(c) { const list = read(); if (list.indexOf(c) === -1) self.className = list.concat([c]).join(' '); },
                    remove(c) { self.className = read().filter(x => x !== c).join(' '); },
                    toggle(c, on) { if (on === undefined ? this.contains(c) : !on) this.remove(c); else this.add(c); },
                    toString() { return read().join(' '); },
                };
                return classListCache;
            },

            get firstChild() { return this.children[0] || null; },
            get lastChild() { return this.children[this.children.length - 1] || null; },
            get nextSibling() {
                const p = this.parentNode;
                if (!p) return null;
                const i = p.children.indexOf(this);
                return i === -1 ? null : (p.children[i + 1] || null);
            },
            get previousSibling() {
                const p = this.parentNode;
                if (!p) return null;
                const i = p.children.indexOf(this);
                return i <= 0 ? null : (p.children[i - 1] || null);
            },

            get textContent() {
                if (this.children.length) return this.children.map(c => c.textContent).join('');
                return this._text;
            },
            set textContent(value) {
                if (this.children.length) {
                    warnings.push('textContent assigned to <' + this.tagName.toLowerCase() + '> with ' +
                                  this.children.length + ' child element(s)');
                    this.children = [];
                }
                ops.textContentSet++;
                this._text = String(value);
            },
            get innerHTML() { return this._html; },
            set innerHTML(value) {
                if (this.children.length) {
                    warnings.push('innerHTML assigned to <' + this.tagName.toLowerCase() + '> with ' +
                                  this.children.length + ' child element(s)');
                    this.children = [];
                }
                ops.innerHTMLSet++;
                this._html = String(value);
            },

            getAttribute(n) {
                return Object.prototype.hasOwnProperty.call(this.attributes, n) ? this.attributes[n] : null;
            },
            setAttribute(n, v) {
                ops.setAttribute++;
                const name = String(n);
                this.attributes[name] = v === true ? '' : String(v);
                if (name.indexOf('data-') === 0) {
                    const key = name.slice(5).replace(/-([a-z])/g, (_, c) => c.toUpperCase());
                    this.dataset[key] = this.attributes[name];
                }
                if (name === 'hidden' && this.attributes[name] === '') this.attributes[name] = '';
            },
            removeAttribute(n) {
                ops.removeAttribute++;
                delete this.attributes[String(n)];
            },
            hasAttribute(n) { return Object.prototype.hasOwnProperty.call(this.attributes, String(n)); },

            appendChild(child) {
                ops.appendChild++;
                detach(child);
                child.parentNode = this;
                this.children.push(child);
                return child;
            },
            insertBefore(child, ref) {
                ops.insertBefore++;
                detach(child);
                child.parentNode = this;
                if (!ref) { this.children.push(child); return child; }
                const i = this.children.indexOf(ref);
                if (i === -1) this.children.push(child);
                else this.children.splice(i, 0, child);
                return child;
            },
            removeChild(child) {
                ops.removeChild++;
                const i = this.children.indexOf(child);
                if (i !== -1) this.children.splice(i, 1);
                if (child.parentNode === this) child.parentNode = null;
                return child;
            },
            replaceChild(next, old) {
                ops.replaceChild++;
                const i = this.children.indexOf(old);
                if (i !== -1) {
                    detach(next);
                    next.parentNode = this;
                    this.children[i] = next;
                }
                old.parentNode = null;
                return old;
            },
            remove() { if (this.parentNode) this.parentNode.removeChild(this); },

            addEventListener(type, handler, opts) {
                this.listeners.push({ type: type, handler: handler, opts: opts });
            },
            removeEventListener(type, handler) {
                this.listeners = this.listeners.filter(l => !(l.type === type && l.handler === handler));
            },
            dispatch(type, evt) {
                const event = evt || {};
                if (!event.type) event.type = type;
                if (!event.target) event.target = this;
                this.listeners.filter(l => l.type === type).slice()
                    .forEach(l => l.handler(event));
                return event;
            },
            dispatchEvent(evt) { return this.dispatch(evt && evt.type, evt), true; },
            click() { this.dispatch('click'); },
            focus() { if (this.parentNode && this.parentNode.ownerDocument) this.parentNode.ownerDocument.__focused = this; },
            blur() { },
            setSelectionRange(a, b) { this.selectionStart = a; this.selectionEnd = b; },
            getBoundingClientRect() { return { top: 0, left: 0, bottom: 10, right: 10, width: 10, height: 10 }; },

            contains(n) {
                let c = n;
                while (c) { if (c === this) return true; c = c.parentNode; }
                return false;
            },
            matches(sel) { return matches(this, sel); },
            closest(sel) {
                let c = this;
                while (c) { if (matches(c, sel)) return c; c = c.parentNode; }
                return null;
            },
            querySelector(sel) { return this.querySelectorAll(sel)[0] || null; },
            querySelectorAll(sel) {
                const out = [];
                const seen = new Set();
                queryGroups(sel).forEach(one => {
                    (function walk(n) {
                        n.children.forEach(c => {
                            if (matches(c, one) && !seen.has(c.__uid)) { seen.add(c.__uid); out.push(c); }
                            walk(c);
                        });
                    })(this);
                });
                return out;
            },
        };
        return node;
    }

    function queryGroups(sel) { return String(sel || '').split(',').map(s => s.trim()).filter(Boolean); }

    function matches(node, sel) {
        if (!node || !sel || node.nodeType !== 1) return false;
        sel = String(sel).trim();
        if (sel[0] === '#') return node.getAttribute('id') === sel.slice(1);
        if (sel[0] === '.') {
            const cls = sel.slice(1);
            return String(node.className || '').split(/\s+/).indexOf(cls) !== -1;
        }
        if (sel[0] === '[') {
            const m = /^\[([\w-]+)(?:([~^$*|]?=)"?([^"\]]*)"?)?\]$/.exec(sel);
            if (!m) return false;
            if (!node.hasAttribute(m[1])) return false;
            if (m[2] === undefined) return true;
            const value = String(node.getAttribute(m[1]));
            if (m[2] === '=') return value === m[3];
            if (m[2] === '*=') return value.indexOf(m[3]) !== -1;
            if (m[2] === '^=') return value.indexOf(m[3]) === 0;
            if (m[2] === '$=') return value.slice(-m[3].length) === m[3];
            if (m[2] === '~=') return value.split(/\s+/).indexOf(m[3]) !== -1;
            return false;
        }
        return node.tagName === sel.toUpperCase();
    }

    function detach(child) {
        const p = child.parentNode;
        if (!p) return;
        const i = p.children.indexOf(child);
        if (i !== -1) p.children.splice(i, 1);
        child.parentNode = null;
    }

    function textNode(value) {
        ops.textNodesCreated++;
        return {
            __uid: ++uid,
            nodeType: 3,
            nodeValue: String(value),
            parentNode: null,
            get textContent() { return this.nodeValue; },
            set textContent(v) { ops.textContentSet++; this.nodeValue = String(v); },
        };
    }

    const document = element('document');
    document.readyState = 'complete';
    document.hidden = false;
    document.visibilityState = 'visible';
    document.__focused = null;
    document.createElement = function (tag) {
        const node = element(tag);
        node.ownerDocument = document;
        return node;
    };
    document.createTextNode = textNode;
    document.head = document.createElement('head');
    document.body = document.createElement('body');
    document.documentElement = document.createElement('html');
    document.appendChild(document.documentElement);
    document.documentElement.appendChild(document.head);
    document.documentElement.appendChild(document.body);
    document.getElementById = function (id) { return document.querySelector('#' + String(id)); };
    document.contains = function (n) {
        let c = n;
        while (c) { if (c === document || c === document.documentElement) return true; c = c.parentNode; }
        return false;
    };
    document.body.ownerDocument = document;
    document.documentElement.ownerDocument = document;
    document.ownerDocument = document;

    return {
        document: document,
        element: function (tag) { return document.createElement(tag); },
        textNode: textNode,
        ops: ops,
        warnings: warnings,
        /** Attach an already-built shell under the document body. */
        attach: function (root) { document.body.appendChild(root); return root; },
        focused: function () { return document.__focused; },
    };
}

/**
 * Build the page shell from a flat list of {tag, attrs} — the harness derives
 * that list from the REAL template file, so the double can never drift into
 * being more generous than the markup.
 */
function buildTree(spec, document) {
    const nodes = [];
    spec.forEach(item => {
        const node = document.createElement(item.tag);
        Object.keys(item.attrs || {}).forEach(name => node.setAttribute(name, item.attrs[name]));
        nodes.push(node);
    });
    return nodes;
}

// ═══════════════════════════════════════════════════════════════
// Tiny template parser + materializer.
// The harnesses read the REAL Jinja template from disk and build the page from
// it, so the DOM double can never be more generous than the markup: a renamed
// id or a moved region fails the test instead of being papered over. Stack
// based, void-aware, and deliberately not more than that.
// ═══════════════════════════════════════════════════════════════
const VOID_TAGS = new Set(['link', 'meta', 'input', 'br', 'img', 'hr', 'source', 'track',
    'wbr', 'area', 'base', 'col', 'embed']);

function parseAttributes(raw) {
    const attrs = {};
    const re = /([\w:.-]+)(?:\s*=\s*("([^"]*)"|'([^']*)'|([^\s"'>]+)))?/g;
    let m;
    while ((m = re.exec(raw || '')) !== null) {
        const name = m[1].toLowerCase();
        if (!name || name === '/') continue;
        attrs[name] = m[3] !== undefined ? m[3] : (m[4] !== undefined ? m[4] : (m[5] !== undefined ? m[5] : ''));
    }
    return attrs;
}

function parseTemplate(html) {
    const src = String(html).replace(/\{#[\s\S]*?#\}/g, '').replace(/<!--[\s\S]*?-->/g, '');
    const root = { tag: '_root', attrs: {}, children: [], parent: null };
    const stack = [root];
    const re = /<(\/?)([a-zA-Z][\w-]*)((?:"[^"]*"|'[^']*'|[^>"'])*?)(\/?)>/g;
    let m;
    while ((m = re.exec(src)) !== null) {
        const tag = m[2].toLowerCase();
        if (m[1] === '/') {
            for (let i = stack.length - 1; i > 0; i--) {
                if (stack[i].tag === tag) { stack.length = i; break; }
            }
            continue;
        }
        const node = { tag: tag, attrs: parseAttributes(m[3]), children: [], parent: stack[stack.length - 1] };
        stack[stack.length - 1].children.push(node);
        if (!VOID_TAGS.has(tag) && m[4] !== '/') stack.push(node);
    }
    return root;
}

function flatten(node, out) {
    out = out || [];
    node.children.forEach(child => { out.push(child); flatten(child, out); });
    return out;
}

function findById(node, id) {
    return flatten(node).find(n => n.attrs.id === id) || null;
}

function ancestorsOf(node) {
    const out = [];
    let c = node && node.parent;
    while (c) { out.push(c); c = c.parent; }
    return out;
}

/** Build real double-DOM nodes from a parsed template subtree. */
function materialize(node, document) {
    const el = document.createElement(node.tag);
    Object.keys(node.attrs || {}).forEach(name => el.setAttribute(name, node.attrs[name]));
    (node.children || []).forEach(child => el.appendChild(materialize(child, document)));
    return el;
}

// ═══════════════════════════════════════════════════════════════
// IndexedDB double.
// ═══════════════════════════════════════════════════════════════
/**
 * createFakeIdb({
 *   seed:     { dbName: { storeName: { key: value } } }   pre-existing data
 *   blockOpen:true      open() never settles (the "storage never answers" case)
 *   throwOnOpen:true    open() throws synchronously
 *   holdWrites:true     put()/get() requests queue until release()
 * })
 *
 * It records every operation in `log` and exposes `writes` (successful puts),
 * `snapshot(dbName)` (deep copy, for byte-comparison against expectations) and
 * `storeNames(dbName)` / `version(dbName)`.
 */
function createFakeIdb(spec) {
    spec = spec || {};
    const dbs = {};
    const log = [];
    const held = [];
    let holding = !!spec.holdWrites;
    let writes = 0;

    function cloneDb(name, version) {
        dbs[name] = { version: version || 1, stores: {} };
        return dbs[name];
    }
    Object.keys(spec.seed || {}).forEach(name => {
        const entry = cloneDb(name, 1);
        Object.keys(spec.seed[name]).forEach(storeName => {
            entry.stores[storeName] = { data: Object.assign({}, spec.seed[name][storeName]) };
        });
    });

    function schedule(fn) {
        if (holding) { held.push(fn); return; }
        setTimeout(fn, 0);
    }

    function makeRequest(tx, work, describe) {
        const req = { result: undefined, error: null, onsuccess: null, onerror: null };
        if (tx) tx.__pending++;
        log.push(describe);
        schedule(function () {
            try {
                req.result = work();
            } catch (err) {
                req.error = err;
                if (tx) tx.__pending--;
                if (req.onerror) req.onerror({ target: req });
                return;
            }
            if (tx) tx.__pending--;
            if (req.onsuccess) req.onsuccess({ target: req });
        });
        return req;
    }

    function transactionFor(dbName, storeName, mode) {
        const entry = dbs[dbName];
        if (!entry) throw new Error('no such database: ' + dbName);
        if (!entry.stores[storeName]) throw new Error('no such object store: ' + storeName);
        const tx = { mode: mode, __pending: 0, oncomplete: null, onerror: null, onabort: null };
        const store = entry.stores[storeName];

        tx.objectStore = function (name) {
            if (name !== storeName) throw new Error('transaction bound to ' + storeName + ', asked for ' + name);
            return {
                get(key) {
                    return makeRequest(tx, () => store.data[key], { op: 'get', db: dbName, store: storeName, key: key });
                },
                getAll() {
                    return makeRequest(tx, () => Object.keys(store.data).map(k => store.data[k]),
                        { op: 'getAll', db: dbName, store: storeName });
                },
                put(value, key) {
                    return makeRequest(tx, () => {
                        store.data[key] = value;
                        writes++;
                        log.push({ op: 'put-committed', db: dbName, store: storeName, key: key });
                        return key;
                    }, { op: 'put', db: dbName, store: storeName, key: key });
                },
                delete(key) {
                    return makeRequest(tx, () => { delete store.data[key]; return true; },
                        { op: 'delete', db: dbName, store: storeName, key: key });
                },
            };
        };

        // The transaction completes after the requests it was given: the adapter
        // calls objectStore() synchronously right after transaction(), so by the
        // time the first timer runs the request count is known.
        const drain = function () {
            if (tx.__pending > 0) { setTimeout(drain, 0); return; }
            if (tx.oncomplete) tx.oncomplete({ target: tx });
        };
        schedule(drain);
        return tx;
    }

    return {
        log: log,
        get writes() { return writes; },
        open(name, version) {
            log.push({ op: 'open', db: name, version: version });
            const req = { result: null, error: null, onupgradeneeded: null, onsuccess: null, onerror: null, onblocked: null };
            if (spec.blockOpen) { log.push({ op: 'open-blocked', db: name }); return req; }
            if (spec.throwOnOpen) throw new Error('IndexedDB refused to open ' + name);
            setTimeout(function () {
                const has = !!dbs[name];
                const existing = has ? dbs[name] : cloneDb(name, version || 1);
                const upgrade = !has || (version || 1) > existing.version;
                if (upgrade) existing.version = Math.max(existing.version, version || 1);
                req.result = {
                    name: name,
                    version: existing.version,
                    objectStoreNames: {
                        contains: (storeName) => !!existing.stores[storeName],
                        get length() { return Object.keys(existing.stores).length; },
                    },
                    createObjectStore(storeName) {
                        log.push({ op: 'createObjectStore', db: name, store: storeName });
                        if (!existing.stores[storeName]) existing.stores[storeName] = { data: {} };
                        return { name: storeName };
                    },
                    transaction: (storeName, mode) => transactionFor(name, storeName, mode || 'readonly'),
                    close() { log.push({ op: 'close', db: name }); },
                };
                if (upgrade && req.onupgradeneeded) req.onupgradeneeded({ target: req });
                if (req.onsuccess) req.onsuccess({ target: req });
            }, 0);
            return req;
        },
        release() { holding = false; held.splice(0).forEach(fn => setTimeout(fn, 0)); },
        hold() { holding = true; },
        // { storeName: { key: record } } — the plain shape, no internal wrapper.
        snapshot(name) {
            const out = {};
            const db = dbs[name];
            if (!db) return out;
            Object.keys(db.stores).forEach(storeName => {
                out[storeName] = JSON.parse(JSON.stringify(db.stores[storeName].data));
            });
            return out;
        },
        storeNames(name) { return dbs[name] ? Object.keys(dbs[name].stores).sort() : []; },
        version(name) { return dbs[name] ? dbs[name].version : 0; },
        has(name) { return !!dbs[name]; },
    };
}

/** The window double: lifecycle events are fired by the harness on purpose. */
function createWindow() {
    const win = {
        NERO: undefined,
        listeners: [],
        addEventListener(type, handler) { win.listeners.push({ type: type, handler: handler }); },
        removeEventListener(type, handler) {
            win.listeners = win.listeners.filter(l => !(l.type === type && l.handler === handler));
        },
        dispatch(type) {
            const evt = { type: type };
            win.listeners.filter(l => l.type === type).slice().forEach(l => l.handler(evt));
            return evt;
        },
        count(type) { return win.listeners.filter(l => l.type === type).length; },
        localStorage: {
            _s: {},
            getItem(k) { return this._s[k] === undefined ? null : this._s[k]; },
            setItem(k, v) { this._s[k] = String(v); },
        },
        location: { search: '', pathname: '/embed-builder/v2' },
        innerWidth: 1440,
        performance: { now: () => Number(process.hrtime.bigint()) / 1000 },
    };
    win.window = win;
    return win;
}

module.exports = {
    createDom, buildTree, createFakeIdb, createWindow,
    parseTemplate, flatten, findById, ancestorsOf, materialize,
};
