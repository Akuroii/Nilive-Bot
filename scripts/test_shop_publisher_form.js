#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════
   Shop Publisher — Phase 1: the page module in a DOM harness.

   Boots the REAL modules in one sandbox — nav-lifecycle.js, embed/model.js,
   embed/discord-markdown.js, embed/preview.js (all frozen, read-only reuse)
   plus dashboard/static/js/shop-publisher.js — against the REAL template
   (dashboard/templates/manage/shoppublisher.html) parsed by the shared
   scripts/support/dom_stub.js double. No browser, no server.

   WHAT THIS HAS TO PROVE

     A. Boot: the catalog loads once, the template picker lists the saved
        presentations, and the product picker groups by the EXISTING `type`
        column only — one <optgroup> per type, every product exactly once,
        no invented category. The fixed token catalog renders.
     B. Selection drives one preview request (template + integer product_id),
        and the API's resolved presentation is handed to the frozen preview
        engine verbatim — the page interprets no token itself.
     C. The purchase action row shows the action that will actually be
        published: the green button (label + emoji) AND the exact custom_id
        shop_buy_<id> of the existing purchase mechanism.
     D. Warnings render with their code + path; resolved tokens render with
        unknown/empty states; the empty/placeholder states reset cleanly.
     E. A stale preview response never overwrites a newer one.
     F. Teardown releases everything (the preview, the listeners) and a second
        visit works.

   Run:  node scripts/test_shop_publisher_form.js
   ═══════════════════════════════════════════════════════════════ */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { createDom, createWindow, parseTemplate, findById, materialize } = require('./support/dom_stub.js');

let pass = 0, fail = 0; const failures = [];
function assert(cond, name, extra) {
    if (cond) { pass++; console.log('  PASS', name); }
    else { fail++; failures.push(name + (extra ? ' — ' + extra : '')); console.log('  FAIL', name, extra || ''); }
}
function section(t) { console.log('\n== ' + t + ' =='); }

const ROOT_DIR = path.join(__dirname, '..');
const js = (...parts) => path.join(ROOT_DIR, 'dashboard', 'static', 'js', ...parts);
const FOUNDATION = [
    js('nav-lifecycle.js'),
    js('embed', 'model.js'),
    js('embed', 'discord-markdown.js'),
    js('embed', 'preview.js'),
    js('shop-publisher.js'),
];
const TEMPLATE_HTML = fs.readFileSync(
    path.join(ROOT_DIR, 'dashboard', 'templates', 'manage', 'shoppublisher.html'), 'utf8');
const TEMPLATE_TREE = parseTemplate(TEMPLATE_HTML);

const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));

// The double keeps innerHTML writes in _html (not parsed into children), so a
// full render text has to collect both surfaces — the same split the preview
// engine itself has (setText vs markup render).
function deepText(node) {
    if (!node) return '';
    if (node.nodeType === 3) return node.nodeValue || '';
    const own = (node._text || '') + ' ' + (node._html || '');
    return own + ' ' + (node.children || []).map(deepText).join(' ');
}

// ── Fixtures (shapes mirror dashboard/api/shop_publisher.py) ───────────────
// Deliberately NOT in server order: grouping must survive a scrambled list,
// collapsing each type into exactly one optgroup (the server pre-sorts for
// display order; the client must never invent or fragment a group).
const PRODUCTS = [
    { id: 7, name: 'Zulu', type: 'title', price: 10, price_diamonds: null, enabled: 1, current_stock: null, max_stock: null, prestige_tier: null, featured: 0 },
    { id: 2, name: 'Alpha', type: 'role', price: 1500, price_diamonds: null, enabled: 1, current_stock: 12, max_stock: 20, prestige_tier: null, featured: 0 },
    { id: 3, name: 'Midnight', type: 'prestige', price: 2500, price_diamonds: null, enabled: 0, current_stock: null, max_stock: null, prestige_tier: 3, featured: 1 },
    { id: 5, name: 'Beta', type: 'role', price: 20, price_diamonds: 5, enabled: 1, current_stock: 0, max_stock: 4, prestige_tier: null, featured: 0 },
];
const TOKENS = [
    { token: '{{product.name}}', key: 'product.name', description: 'The product name.' },
    { token: '{{product.price}}', key: 'product.price', description: 'The formatted price with its currency icon and name.' },
];
function previewBody(productId) {
    return {
        success: true,
        template: 'sale',
        product: { id: productId, name: 'Alpha', type: 'role' },
        preview: {
            content: 'Buy Alpha for 1,500 🪙 Coins!',
            embeds: [{ title: 'Alpha', description: 'The VIP role.' }],
            purchase_action: {
                type: 'button', custom_id: 'shop_buy_' + productId,
                label: 'Buy Alpha', style: 'green', emoji: '🛒',
            },
            warnings: [
                { code: 'unknown_token', path: 'embeds.0.title', message: 'Unknown token {{nope}} at embeds.0.title was left as typed — it is not in the token catalog.' },
                { code: 'validation', path: 'embeds.0.description', message: 'Embed 1 description is 5000 characters; Discord\'s limit is 4096.' },
            ],
            tokens: {
                values: {},
                used: [
                    { token: '{{product.name}}', key: 'product.name', path: 'content', resolved: true, value: 'Alpha' },
                    { token: '{{nope}}', key: 'nope', path: 'embeds.0.title', resolved: false, value: '' },
                    { token: '{{product.description}}', key: 'product.description', path: 'embeds.0.description', resolved: true, value: '' },
                ],
            },
        },
    };
}

// ── Environment ────────────────────────────────────────────────────────────
function makeEnv(opts) {
    opts = opts || {};
    const dom = createDom();
    const win = createWindow();
    win.document = dom.document;
    win.__BOT_IDENTITY__ = { name: 'Nero', avatar: 'https://cdn.example/a.png' };

    // Controllable fetch: exact URL routing + optional manual settlement, so
    // "a stale response never overwrites a newer one" is deterministic.
    const calls = [];
    const pending = [];
    const routes = opts.routes || {};

    function fetchStub(input, init) {
        const url = String(input);
        const call = { url, init: init || {}, body: (init && init.body) ? JSON.parse(init.body) : null };
        calls.push(call);
        const route = routes[url.split('?')[0]];
        if (!route) {
            return Promise.resolve({ ok: false, status: 404, json: async () => ({ success: false, error: 'no route ' + url }) });
        }
        const record = { call, settled: false, resolve: null };
        const promise = new Promise((resolve) => {
            record.resolve = (result) => {
                record.settled = true;
                resolve({
                    ok: (result.status || 200) < 400,
                    status: result.status || 200,
                    json: async () => result.body,
                });
            };
        });
        pending.push(record);
        const result = route(call);
        if (result && result.hold) {
            record.hold = true;              // settled later by the test
        } else {
            record.resolve(result || { status: 200, body: { success: true } });
        }
        return promise;
    }

    const consoleLines = { error: [], warn: [] };
    const sandbox = {
        window: win,
        document: dom.document,
        console: {
            log: () => {}, debug: () => {},
            warn: (...a) => consoleLines.warn.push(a.join(' ')),
            error: (...a) => consoleLines.error.push(a.join(' ')),
        },
        setTimeout, clearTimeout, setInterval, clearInterval,
        Promise, Object, Array, Math, Date, JSON, Number, String, RegExp, Error, TypeError, Set, Map, Symbol,
        isFinite, parseInt, parseFloat, encodeURIComponent, decodeURIComponent,
        fetch: fetchStub,
    };
    vm.createContext(sandbox);
    FOUNDATION.forEach(file => {
        vm.runInContext(fs.readFileSync(file, 'utf8'), sandbox, { filename: path.basename(file) });
    });

    const env = {
        dom, win, sandbox, calls, pending, consoleLines, NERO: win.NERO, routes,
        root: null,
        el: (id) => dom.document.getElementById(id),
        text: (id) => (dom.document.getElementById(id) || { textContent: '' }).textContent,
        mount: async function (settleMs) {
            // A second visit replaces the previous tree the way htmx would,
            // so getElementById can only ever find the live page's nodes.
            while (dom.document.body.firstChild) {
                dom.document.body.removeChild(dom.document.body.firstChild);
            }
            const root = materialize(findById(TEMPLATE_TREE, 'sp-root'), dom.document);
            env.root = root;
            dom.attach(root);
            const mounting = env.NERO.lifecycle.mount(dom.document);
            await mounting;
            await sleep(settleMs == null ? 20 : settleMs);
            return env;
        },
        unmount: () => env.NERO.lifecycle.unmount('test'),
        settle: (ms) => sleep(ms == null ? 20 : ms),
        change: async function (id, value, settleMs) {
            const select = env.el(id);
            select.value = String(value);
            select.dispatch('change');
            await env.settle(settleMs);
        },
        release: function (filter) {
            // Settle held responses in registration order (optionally a subset).
            const held = pending.filter(p => p.hold && !p.settled && (!filter || filter(p.call)));
            held.forEach(p => {
                const route = routes[p.call.url.split('?')[0]];
                p.resolve(route(p.call) || { status: 200, body: {} });
            });
        },
        optionValues: (select) => {
            const out = [];
            (function walk(n) {
                (n.children || []).forEach(c => {
                    if (c.tagName === 'OPTION') out.push(c.value);
                    else walk(c);
                });
            })(select);
            return out;
        },
        optgroups: (select) => select.children.filter(c => c.tagName === 'OPTGROUP'),
    };
    return env;
}

function defaultRoutes() {
    return {
        '/api/shop-publisher/catalog': () => ({ status: 200, body: { templates: ['banner', 'sale'], products: PRODUCTS, tokens: TOKENS } }),
        '/api/shop-publisher/preview': (call) => ({ status: 200, body: previewBody(call.body.product_id) }),
    };
}

// ═══════════════════════════════════════════════════════════════════════════
// A. Boot + pickers
// ═══════════════════════════════════════════════════════════════════════════
async function bootTests() {
    section('A. Boot: pickers grouped by the existing type only');
    const env = makeEnv({ routes: defaultRoutes() });
    await env.mount();

    assert(env.calls.filter(c => c.url === '/api/shop-publisher/catalog').length === 1,
        'the catalog is fetched exactly once per visit');
    assert(JSON.stringify(env.optionValues(env.el('sp-template'))) === JSON.stringify(['', 'banner', 'sale']),
        'the template picker lists every saved presentation', JSON.stringify(env.optionValues(env.el('sp-template'))));

    const select = env.el('sp-product');
    const groups = env.optgroups(select);
    const groupLabels = groups.map(g => g.label);
    assert(groupLabels.length === 3, 'exactly one optgroup per type — no invented category', JSON.stringify(groupLabels));
    assert(JSON.stringify(groupLabels) === JSON.stringify(['title', 'role', 'prestige']),
        'groups follow the list order (the server pre-sorts; the client never re-categorizes)', JSON.stringify(groupLabels));
    const byLabel = {};
    groups.forEach(g => {
        byLabel[g.label] = g.children.filter(c => c.tagName === 'OPTION').map(o => o.textContent);
    });
    assert(JSON.stringify(byLabel.role) === JSON.stringify(['Alpha', 'Beta']),
        'both role products sit inside the single role group', JSON.stringify(byLabel));
    assert(JSON.stringify(byLabel.title) === JSON.stringify(['Zulu']) &&
        JSON.stringify(byLabel.prestige) === JSON.stringify(['Midnight (disabled)']),
        'every product appears exactly once, inside its type group');
    assert(byLabel.prestige[0] === 'Midnight (disabled)',
        'a disabled product is visible but marked, so its preview warning is reachable');
    const allOptions = env.optionValues(select).filter(v => v !== '');
    assert(JSON.stringify(allOptions.sort()) === JSON.stringify(['2', '3', '5', '7']),
        'option values are the product ids', JSON.stringify(allOptions));

    const catalog = env.text('sp-token-catalog');
    assert(catalog.indexOf('{{product.name}}') !== -1 && catalog.indexOf('{{product.price}}') !== -1,
        'the fixed token catalog reference renders');
    assert(env.text('sp-status').indexOf('Pick a template') !== -1,
        'the status line asks for a selection');
    assert(env.el('sp-purchase').hidden === true, 'no purchase action before a preview');
    // The double's parseTemplate keeps elements but drops authored text, so
    // the static placeholder is asserted against the real template SOURCE
    // (the post-reset state below proves the page rewrites the same text).
    assert(TEMPLATE_HTML.split('id="sp-warnings"')[1]
        .indexOf('Select a template and product to see warnings.') !== -1,
        'warnings start in the placeholder state (authored in the template)');

    // B/C/D share this env through the preview flow below.
    await previewFlowTests(env);
    await guardTests(env);

    env.unmount();
    await teardownTests(env);
}

// ═══════════════════════════════════════════════════════════════════════════
// B. Preview → resolved presentation + purchase action + warnings + tokens
// ═══════════════════════════════════════════════════════════════════════════
async function previewFlowTests(env) {
    section('B/C/D. Preview: resolved presentation, purchase action, warnings, tokens');
    await env.change('sp-template', 'sale');
    assert(env.calls.filter(c => c.url === '/api/shop-publisher/preview').length === 0,
        'a half-selection makes no preview request');
    assert(env.text('sp-status').indexOf('Select a template and a product') !== -1,
        'the half-selection is explained in the status line');

    await env.change('sp-product', 2);
    const previews = env.calls.filter(c => c.url === '/api/shop-publisher/preview');
    assert(previews.length === 1, 'selecting both sides makes exactly one preview request');
    assert(previews[0].init.method === 'POST' && previews[0].init.headers['Content-Type'] === 'application/json',
        'the preview request is a JSON POST');
    assert(previews[0].body && previews[0].body.template === 'sale' && previews[0].body.product_id === 2,
        'the request carries the template name and an INTEGER product_id', JSON.stringify(previews[0].body));

    // The resolved presentation went to the frozen preview engine verbatim.
    const mountText = deepText(env.el('sp-preview-mount'));
    assert(mountText.indexOf('Buy Alpha for 1,500 🪙 Coins!') !== -1,
        'the resolved content renders in the preview mount', mountText);
    assert(mountText.indexOf('The VIP role.') !== -1, 'the resolved embed description renders');

    // C. The purchase action that WILL be published.
    const purchase = env.el('sp-purchase');
    assert(purchase.hidden === false, 'the purchase action row is shown with the preview');
    const button = purchase.querySelectorAll('.sp-buy-button')[0];
    assert(!!button, 'the published button is rendered');
    assert(button.textContent.indexOf('Buy Alpha') !== -1 && button.textContent.indexOf('🛒') !== -1,
        'the button carries the published label and emoji', button.textContent);
    const meta = purchase.querySelectorAll('.sp-purchase-meta')[0].textContent;
    assert(meta.indexOf('custom_id: shop_buy_2') !== -1,
        'the exact custom_id of the existing purchase mechanism is shown', meta);
    assert(meta.indexOf('green') !== -1 && meta.indexOf('existing shop purchase mechanism') !== -1,
        'the meta names the published style and the existing mechanism');

    // D. Warnings.
    const warnings = env.el('sp-warnings').querySelectorAll('.sp-warning');
    assert(warnings.length === 2, 'every warning renders', String(warnings.length));
    assert(warnings[0].getAttribute('data-code') === 'unknown_token' &&
        warnings[0].textContent.indexOf('embeds.0.title') !== -1 &&
        warnings[0].textContent.indexOf('Unknown token') !== -1,
        'warnings carry their code, path and message');
    assert(warnings[1].getAttribute('data-code') === 'validation' &&
        warnings[1].textContent.indexOf('4096') !== -1,
        'resolved-payload validation warnings render');

    // D. Resolved tokens table. (The double's selector is a single
    // tag/class/id — pick body rows by parent, not a descendant selector.)
    const tokenRows = env.el('sp-tokens').querySelectorAll('tr')
        .filter(r => r.parentNode && r.parentNode.tagName === 'TBODY');
    assert(tokenRows.length === 3, 'one row per token occurrence', String(tokenRows.length));
    assert(tokenRows[0].textContent.indexOf('{{product.name}}') !== -1 &&
        tokenRows[0].textContent.indexOf('Alpha') !== -1,
        'a resolved occurrence shows token, path and value');
    assert((tokenRows[1].className || '').indexOf('sp-token-unknown') !== -1 &&
        tokenRows[1].textContent.indexOf('unknown token') !== -1,
        'an unknown occurrence is visibly marked');
    assert(tokenRows[2].textContent.indexOf('(empty)') !== -1,
        'an empty resolution is visibly marked');

    assert(env.text('sp-status').indexOf('warning(s)') !== -1,
        'the status line reports the warning count');
}

// ═══════════════════════════════════════════════════════════════════════════
// E. Stale responses + reset behavior
// ═══════════════════════════════════════════════════════════════════════════
async function guardTests(env) {
    section('E. Stale responses never overwrite a newer preview');

    // Hold every preview response; fire two selections; release OLD first.
    env.routes['/api/shop-publisher/preview'] = (call) => ({
        hold: call.body.product_id === 2,   // the FIRST request (product 2) is slow
        status: 200,
        body: Object.assign(previewBody(call.body.product_id), {
            product: { id: call.body.product_id, name: 'P' + call.body.product_id, type: 'role' },
        }),
    });

    await env.change('sp-product', 2, 5);            // slow (held)
    await env.change('sp-product', 5, 5);            // newer (auto-settles)
    await env.settle();
    let meta = env.el('sp-purchase').querySelectorAll('.sp-purchase-meta')[0].textContent;
    assert(meta.indexOf('shop_buy_5') !== -1, 'the newer preview (product 5) is on screen', meta);

    env.release();                                    // now the OLD response lands
    await env.settle();
    meta = env.el('sp-purchase').querySelectorAll('.sp-purchase-meta')[0].textContent;
    assert(meta.indexOf('shop_buy_5') !== -1 && meta.indexOf('shop_buy_2') === -1,
        'a stale preview response cannot overwrite the newer one', meta);

    // Reset: dropping back to a half-selection clears the purchase action.
    await env.change('sp-product', '');
    assert(env.el('sp-purchase').hidden === true, 'no selection hides the purchase action');
    assert(env.text('sp-warnings').indexOf('Select a template') !== -1, 'warnings return to the placeholder');
    assert(env.text('sp-tokens').indexOf('No resolution yet') !== -1, 'tokens return to the placeholder');

    // API errors are status-line errors, not crashes.
    env.routes['/api/shop-publisher/preview'] = () => ({ status: 200, body: { success: false, error: 'Template nope was not found.' } });
    await env.change('sp-product', 2);
    assert(env.text('sp-status').indexOf('Template nope was not found') !== -1,
        'an unsuccessful response is reported on the status line');

    env.routes['/api/shop-publisher/preview'] = () => ({ status: 500, body: { success: false, error: 'boom' } });
    await env.change('sp-product', 5);
    assert(env.text('sp-status').indexOf('Preview failed') !== -1,
        'a failed request is reported on the status line');
}

// ═══════════════════════════════════════════════════════════════════════════
// F. Teardown + second visit
// ═══════════════════════════════════════════════════════════════════════════
async function teardownTests(env) {
    section('F. Teardown releases everything; a second visit works');
    const selectsStillWired = env.el('sp-template') && env.el('sp-template').listeners.length;
    // nav-lifecycle removes ctx listeners on unmount: the selects' own
    // listener arrays must be back to zero (they started at zero).
    assert(selectsStillWired === 0,
        'the picker listeners were released on unmount', String(selectsStillWired));
    assert(env.NERO.debug.report().stats.mounts === 1 && env.NERO.debug.report().stats.destroys === 1,
        'one mount, one clean destroy');

    // Second visit: the registry re-inits and the catalog is refetched.
    env.routes['/api/shop-publisher/catalog'] = () => ({
        status: 200,
        body: { templates: ['sale'], products: PRODUCTS.slice(1), tokens: TOKENS },
    });
    await env.mount();
    const catalogCalls = env.calls.filter(c => c.url === '/api/shop-publisher/catalog').length;
    assert(catalogCalls === 2, 'a second visit refetches the catalog', String(catalogCalls));
    assert(JSON.stringify(env.optionValues(env.el('sp-template'))) === JSON.stringify(['', 'sale']),
        'the second visit renders the fresh catalog');
    assert(env.NERO.debug.report().stats.mounts === 2, 'the second mount registered');
    env.unmount();
    assert(env.NERO.debug.report().stats.destroys === 2, 'the second destroy was clean too');
}

// ── Catalog failure is reported, not fatal ─────────────────────────────────
async function catalogFailureTests() {
    section('G. A failing catalog call is a status error');
    const env = makeEnv({
        routes: {
            '/api/shop-publisher/catalog': () => ({ status: 500, body: { success: false } }),
        },
    });
    await env.mount();
    assert(env.text('sp-status').indexOf('Could not load the picker catalog') !== -1,
        'the status line reports the catalog failure', env.text('sp-status'));
    assert(env.text('sp-token-catalog').indexOf('Catalog unavailable') !== -1,
        'the catalog panel degrades visibly');
    env.unmount();
}

(async () => {
    await bootTests();
    await catalogFailureTests();
    console.log('\n' + '='.repeat(60));
    if (fail) {
        console.log(pass + '/' + (pass + fail) + ' checks — FAILURES:');
        failures.forEach(f => console.log('  - ' + f));
        process.exitCode = 1;
    } else {
        console.log(pass + '/' + (pass + fail) + ' checks passed.');
    }
})().catch(error => { console.error(error); process.exitCode = 1; });
