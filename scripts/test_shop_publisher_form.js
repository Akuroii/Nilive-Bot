#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════
   Shop Publisher — Step 0: the page module in a DOM harness.

   Boots the REAL modules in one sandbox — nav-lifecycle.js, embed/model.js,
   embed/discord-markdown.js, embed/preview.js (all frozen, read-only reuse)
   plus dashboard/static/js/shop-publisher.js — against the REAL template
   (dashboard/templates/manage/shoppublisher.html) parsed by the shared
   scripts/support/dom_stub.js double. No browser, no server.

   WHAT THIS HAS TO PROVE

     A. Boot: the catalog loads once, the template picker lists the saved
        presentations, and the product picker groups by Shop Category with an
        Uncategorized bucket — `type` stays display metadata (a badge + the
        option label), never the grouping mechanism. The category picker
        filters; assignment posts to the Slice 1 endpoint and regroups on
        success / reverts on failure. The fixed token catalog renders.
     B. Selection drives one preview request carrying a DESIGN DRAFT
        ({presentation{mode,content,embeds}, products[], action{kind, entries}})
        — products[] is the root-product roster — and the API's resolved
        presentation is handed to the frozen preview engine verbatim; the page
        interprets no token itself.
     C. The purchase action row shows the action that will actually be
        published: button entries (label + emoji) AND the exact custom_ids
        shop_buy_<id> of the existing purchase mechanism; select kinds render
        their option list and the option VALUES' custom ids.
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
    { id: 7, name: 'Zulu', type: 'title', price: 10, price_diamonds: null, enabled: 1, current_stock: null, max_stock: null, prestige_tier: null, featured: 0, category_id: 11 },
    { id: 2, name: 'Alpha', type: 'role', price: 1500, price_diamonds: null, enabled: 1, current_stock: 12, max_stock: 20, prestige_tier: null, featured: 0, category_id: 12 },
    { id: 3, name: 'Midnight', type: 'prestige', price: 2500, price_diamonds: null, enabled: 0, current_stock: null, max_stock: null, prestige_tier: 3, featured: 1, category_id: 12 },
    { id: 5, name: 'Beta', type: 'role', price: 20, price_diamonds: 5, enabled: 1, current_stock: 0, max_stock: 4, prestige_tier: null, featured: 0, category_id: null },
    { id: 6, name: 'Alpha · 7 days', type: 'role', price: 500, price_diamonds: null, enabled: 1, current_stock: null, max_stock: null, prestige_tier: null, featured: 0, category_id: 12, option_of_id: 2 },
];
// Slice 1 GET /api/shop-publisher/categories rows (name/emoji/order used by
// the pickers; counts are derived client-side from the product rows).
const CATEGORIES = [
    { id: 11, name: 'Cosmetics', emoji: '🎨', enabled: 1, sort_order: 0, product_count: 0, created_at: '2026-10-02 09:00:00', updated_at: '2026-10-02 09:00:00' },
    { id: 12, name: 'Roles', emoji: '🛡️', enabled: 1, sort_order: 1, product_count: 0, created_at: '2026-10-02 09:00:00', updated_at: '2026-10-02 09:00:00' },
];
// Saved Design drafts (Design Draft Persistence): orchestration/presentation
// only. Each `design.presentation` is the design's OWN snapshot;
// source_template_name is provenance only and never dereferenced after save.
const DESIGNS = [
    {
        id: 3, name: 'Main', source_template_name: 'banner',
        design: {
            presentation: { mode: 'per_product', content: 'Loaded {{product.name}}', embeds: [{ title: 'Loaded title' }] },
            products: [2],
            action: { kind: 'buttons', entries: [{ product_id: 2 }] },
        },
        created_at: '2026-10-02 09:00:00', updated_at: '2026-10-02 09:00:00',
    },
    {
        id: 4, name: 'Other', source_template_name: 'sale',
        design: {
            presentation: { mode: 'frame', content: 'Other draft', embeds: [] },
            products: [5],
            action: { kind: 'buttons', entries: [{ product_id: 5 }] },
        },
        created_at: '2026-10-02 09:00:00', updated_at: '2026-10-02 09:00:00',
    },
];
const TEMPLATES = ['banner', 'sale'];
const BANNER_DOC = { content: 'Welcome!', embeds: [] };
const SALE_DOC = { content: 'Buy {{product.name}} for {{product.price_display}}!', embeds: [{ title: '{{product.name}}' }] };
const TOKENS = [
    { token: '{{product.name}}', key: 'product.name', label: 'Name', group: 'Product', description: 'The product name.', aliases: ['{{name}}'] },
    { token: '{{product.price_display}}', key: 'product.price_display', label: 'Price (full)', group: 'Price', description: 'The all-in-one price line.', aliases: [] },
];

function previewBody(productId, kind) {
    const entries = kind === 'product_select'
        ? [
            { product_id: productId, custom_id: 'shop_buy_' + productId, label: 'Alpha', description: '1,500 🪙 Coins', emoji: '', style: '', free: false },
            { product_id: 5, custom_id: 'shop_buy_5', label: 'Beta', description: '5 💎 Diamonds', emoji: '', style: '', free: false },
        ]
        : [{ product_id: productId, custom_id: 'shop_buy_' + productId, label: 'Buy Alpha', description: '', emoji: '🛒', style: 'green', free: false }];
    return {
        success: true,
        preview: {
            mode: 'per_product',
            content: 'Buy Alpha for 1,500 🪙 Coins!',
            embeds: [{ title: 'Alpha', description: 'The VIP role.' }],
            action: kind === 'product_select'
                ? { kind: 'product_select', placeholder: 'Select…', component_custom_id: 'shop_buy_sel_0', entries: entries }
                : { kind: 'buttons', style: 'green', entries: entries },
            warnings: [
                { code: 'unknown_token', path: 'embeds.0.title', message: 'Unknown token {{nope}} at embeds.0.title was left as typed — it is not in the token catalog.' },
                { code: 'validation', path: 'embeds.0.description', message: 'Embed 1 description is 5000 characters; Discord\'s limit is 4096.' },
            ],
            tokens: {
                values: {},
                used: [
                    { token: '{{product.name}}', key: 'product.name', canonical: 'product.name', path: 'content', resolved: true, known: true, value: 'Alpha' },
                    { token: '{{nope}}', key: 'nope', canonical: '', path: 'embeds.0.title', resolved: false, known: false, value: '' },
                    { token: '{{product.description}}', key: 'product.description', canonical: 'product.description', path: 'embeds.0.description', resolved: true, known: true, value: '' },
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
        '/api/shop-publisher/catalog': () => ({ status: 200, body: { templates: TEMPLATES, products: PRODUCTS, tokens: TOKENS } }),
        // The template DOCUMENT rides on the Embed Builder's existing read
        // route (names-only catalog contract — the intentional Step 0
        // transitional presentation source).
        '/api/embedbuilder/template/banner': () => ({ status: 200, body: { template: BANNER_DOC } }),
        '/api/embedbuilder/template/sale': () => ({ status: 200, body: { template: SALE_DOC } }),
        // Slice 1 Category contracts, reused as-is (D1: catalog rows carry
        // the product→category mapping; this list supplies names/order).
        '/api/shop-publisher/categories': () => ({
            status: 200,
            body: { success: true, categories: CATEGORIES, uncategorized: 1 },
        }),
        '/api/shop-publisher/products/category': () => ({ status: 200, body: { success: true } }),
        // Design Draft Persistence: GET = full records; POST = create or
        // full-overwrite update (id echoed — 99 for creates).
        '/api/shop-publisher/designs': (call) => {
            if ((call.init.method || 'GET') === 'POST') {
                return {
                    status: 200,
                    body: {
                        success: true,
                        design: {
                            id: call.body.id || 99,
                            name: call.body.name,
                            source_template_name: call.body.source_template_name,
                            design: call.body.design,
                            created_at: '2026-10-02 09:00:00',
                            updated_at: '2026-10-02 09:00:00',
                        },
                    },
                };
            }
            return { status: 200, body: { success: true, designs: DESIGNS } };
        },
        '/api/shop-publisher/designs/3': () => ({ status: 200, body: { success: true } }),
        '/api/shop-publisher/designs/4': () => ({ status: 200, body: { success: true } }),
        '/api/shop-publisher/preview': (call) => ({ status: 200, body: previewBody(call.body.products[0]) }),
    };
}

// ═══════════════════════════════════════════════════════════════════════════
// A. Boot + pickers
// ═══════════════════════════════════════════════════════════════════════════
async function bootTests() {
    section('A. Boot: category picker, category grouping, type badge');
    const env = makeEnv({ routes: defaultRoutes() });
    await env.mount();

    assert(env.calls.filter(c => c.url === '/api/shop-publisher/catalog').length === 1,
        'the catalog is fetched exactly once per visit');
    assert(env.calls.filter(c => c.url === '/api/shop-publisher/categories').length === 1,
        'the Slice 1 category list is fetched exactly once per visit');
    assert(JSON.stringify(env.optionValues(env.el('sp-template'))) === JSON.stringify(['', 'banner', 'sale']),
        'the template picker lists every saved presentation', JSON.stringify(env.optionValues(env.el('sp-template'))));

    // The category picker: All / categories in Slice 1 order with derived
    // counts / the uncategorized bucket.
    const catSelect = env.el('sp-category');
    const catOptions = catSelect.children.filter(c => c.tagName === 'OPTION').map(o => o.textContent);
    assert(JSON.stringify(env.optionValues(catSelect)) === JSON.stringify(['', '11', '12', 'none']),
        'the category picker offers All / categories / the uncategorized bucket',
        JSON.stringify(env.optionValues(catSelect)));
    assert(JSON.stringify(catOptions) === JSON.stringify([
        'All products', '🎨 Cosmetics (1)', '🛡️ Roles (3)', 'Uncategorized (1)']),
        'category options carry emoji + name + derived counts', JSON.stringify(catOptions));
    assert(JSON.stringify(env.optionValues(env.el('sp-product-category'))) === JSON.stringify(['', '11', '12']),
        'the assignment select offers Uncategorized + every category');

    // The product picker groups by category (Uncategorized bucket included);
    // `type` survives only as display metadata on each option.
    const select = env.el('sp-product');
    const groups = env.optgroups(select);
    const groupLabels = groups.map(g => g.label);
    assert(JSON.stringify(groupLabels) === JSON.stringify(['Cosmetics', 'Roles', 'Uncategorized']),
        'products group by category with an Uncategorized bucket', JSON.stringify(groupLabels));
    const byLabel = {};
    groups.forEach(g => {
        byLabel[g.label] = g.children.filter(c => c.tagName === 'OPTION').map(o => o.textContent);
    });
    assert(JSON.stringify(byLabel.Cosmetics) === JSON.stringify(['Zulu · title']),
        'each product appears exactly once, inside its category group', JSON.stringify(byLabel));
    assert(JSON.stringify(byLabel.Roles) === JSON.stringify(['Alpha · role', 'Midnight (disabled) · prestige']),
        'the category link is the grouping mechanism', JSON.stringify(byLabel));
    assert(JSON.stringify(byLabel.Uncategorized) === JSON.stringify(['Beta · role']),
        'products without a category land in the Uncategorized bucket');
    assert(byLabel.Roles[1].indexOf('(disabled)') !== -1,
        'a disabled product is visible but marked, so its preview warning is reachable');
    assert(byLabel.Roles[0].indexOf('role') !== -1,
        'the product type survives as display metadata, not the grouping');
    const allOptions = env.optionValues(select).filter(v => v !== '');
    assert(JSON.stringify(allOptions) === JSON.stringify(['7', '2', '3', '5']),
        'option values are the root product ids in category order', JSON.stringify(allOptions));
    assert(!allOptions.includes('6'),
        'an Option row never appears as a standalone/root product choice');
    assert(env.el('sp-product-type').hidden === true,
        'the type badge stays hidden until a product is chosen');

    const catalog = env.text('sp-token-catalog');
    assert(catalog.indexOf('{{product.name}}') !== -1 && catalog.indexOf('{{product.price_display}}') !== -1,
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
    await categoryAssignmentTests(env);
    await categoryFilterTests(env);
    await previewFlowTests(env);
    await selectActionTests(env);
    await guardTests(env);

    env.unmount();
    await teardownTests(env);
}

// ═══════════════════════════════════════════════════════════════════════════
// A2. Product category assignment (Slice 1 endpoint reused as-is)
// ═══════════════════════════════════════════════════════════════════════════
async function categoryAssignmentTests(env) {
    section('A2. Category assignment posts to the Slice 1 endpoint');
    await env.change('sp-product', 2);
    assert(env.el('sp-product-category').value === '12',
        'the assignment select shows the product\'s current category');
    assert(env.text('sp-product-type') === 'role' && env.el('sp-product-type').hidden === false,
        'the type badge shows display metadata for the selected product');

    // Unassign: POST category_id: null (the endpoint's documented unassign).
    await env.change('sp-product-category', '');
    const calls = () => env.calls.filter(c => c.url === '/api/shop-publisher/products/category');
    assert(calls().length === 1, 'assignment POSTs exactly once per change');
    assert(calls()[0].init.method === 'POST', 'assignment is a POST');
    assert(calls()[0].body.product_id === 2 && calls()[0].body.category_id === null,
        'unassign posts category_id: null', JSON.stringify(calls()[0].body));
    assert(env.text('sp-status').indexOf('Category updated.') !== -1,
        'a successful assignment reports success');
    assert(JSON.stringify(env.optgroups(env.el('sp-product')).map(g => g.label))
        === JSON.stringify(['Cosmetics', 'Roles', 'Uncategorized']),
        'the picker regroups after the assignment',
        JSON.stringify(env.optgroups(env.el('sp-product')).map(g => g.label)));
    const roleAfter = env.optgroups(env.el('sp-product')).find(g => g.label === 'Roles');
    assert(JSON.stringify(roleAfter.children.filter(c => c.tagName === 'OPTION').map(o => o.textContent))
        === JSON.stringify(['Midnight (disabled) · prestige']),
        'the unassigned product left its category group',
        JSON.stringify(roleAfter.children.map(o => o.textContent)));
    const noneAfter = env.optgroups(env.el('sp-product')).find(g => g.label === 'Uncategorized');
    assert(JSON.stringify(noneAfter.children.filter(c => c.tagName === 'OPTION').map(o => o.textContent))
        === JSON.stringify(['Alpha · role', 'Beta · role']),
        'the unassigned product landed in the Uncategorized bucket',
        JSON.stringify(noneAfter.children.map(o => o.textContent)));
    let catOptions = env.el('sp-category').children
        .filter(c => c.tagName === 'OPTION').map(o => o.textContent);
    assert(JSON.stringify(catOptions) === JSON.stringify([
        'All products', '🎨 Cosmetics (1)', '🛡️ Roles (2)', 'Uncategorized (2)']),
        'category counts follow the assignment', JSON.stringify(catOptions));

    // Assign: the local state updates and the groups regroup.
    await env.change('sp-product-category', '11');
    assert(calls()[1].body.product_id === 2 && calls()[1].body.category_id === 11,
        'assigning posts the parsed category id', JSON.stringify(calls()[1].body));
    const cosGroup = env.optgroups(env.el('sp-product')).find(g => g.label === 'Cosmetics');
    assert(JSON.stringify(cosGroup.children.filter(c => c.tagName === 'OPTION').map(o => o.textContent))
        === JSON.stringify(['Zulu · title', 'Alpha · role']),
        'the product moved into its new category group');
    assert(env.el('sp-product-category').value === '11',
        'the assignment select holds the new value');

    // Failure: the existing status error appears and the selection reverts.
    env.routes['/api/shop-publisher/products/category'] = () => ({
        status: 200, body: { success: false, error: 'Category not found.' },
    });
    await env.change('sp-product-category', '12');
    assert(env.text('sp-status').indexOf('Could not assign the category') !== -1,
        'a failed assignment reports through the status line', env.text('sp-status'));
    assert(env.el('sp-product-category').value === '11',
        'the selection reverts to the previous category on failure');

    // Restore the boot mapping so the shared env and fixtures stay pristine.
    env.routes['/api/shop-publisher/products/category'] = () => ({ status: 200, body: { success: true } });
    await env.change('sp-product-category', '12');
    assert(env.el('sp-product-category').value === '12',
        'the original mapping is restored');
    assert(JSON.stringify(env.optgroups(env.el('sp-product')).map(g => g.label))
        === JSON.stringify(['Cosmetics', 'Roles', 'Uncategorized']),
        'the picker groups match the boot state again');
}

// ═══════════════════════════════════════════════════════════════════════════
// A3. The category picker filters the product list
// ═══════════════════════════════════════════════════════════════════════════
async function categoryFilterTests(env) {
    section('A3. The category filter narrows the product picker');
    await env.change('sp-product', '');
    await env.change('sp-category', '11');
    assert(JSON.stringify(env.optionValues(env.el('sp-product'))) === JSON.stringify(['', '7']),
        'the Cosmetics filter keeps only its member', JSON.stringify(env.optionValues(env.el('sp-product'))));
    assert(JSON.stringify(env.optgroups(env.el('sp-product')).map(g => g.label))
        === JSON.stringify(['Cosmetics']),
        'empty groups hide in the filtered view',
        JSON.stringify(env.optgroups(env.el('sp-product')).map(g => g.label)));
    await env.change('sp-category', 'none');
    assert(JSON.stringify(env.optionValues(env.el('sp-product'))) === JSON.stringify(['', '5']),
        'the uncategorized filter keeps only uncategorized products');
    assert(JSON.stringify(env.optgroups(env.el('sp-product')).map(g => g.label))
        === JSON.stringify(['Uncategorized']),
        'the uncategorized filter shows only its bucket');
    await env.change('sp-category', '12');
    assert(JSON.stringify(env.optionValues(env.el('sp-product'))) === JSON.stringify(['', '2', '3']),
        'a category filter keeps exactly its linked products');
    await env.change('sp-category', '');
    assert(JSON.stringify(env.optionValues(env.el('sp-product'))) === JSON.stringify(['', '7', '2', '3', '5']),
        'All products restores every product in category order');

    // A root outside the active filter stays visibly selected in a dedicated
    // group, so the draft/Preview/Save cannot silently point elsewhere.
    await env.change('sp-product', 5);
    await env.change('sp-category', '11');
    assert(env.el('sp-product').value === '5',
        'a filtered-out root remains visibly selected',
        'value=' + JSON.stringify(env.el('sp-product').value));
    assert(JSON.stringify(env.optgroups(env.el('sp-product')).map(g => g.label))
        === JSON.stringify(['Cosmetics', 'Current selection (outside filter)']),
        'the outside-filter selection is explicitly labeled',
        JSON.stringify(env.optgroups(env.el('sp-product')).map(g => g.label)));
    assert(env.el('sp-product-category').value === '' && env.el('sp-product-type').hidden === false,
        'the assignment control and type badge continue to describe the selected root',
        'assign=' + JSON.stringify(env.el('sp-product-category').value) +
        ' type=' + JSON.stringify(env.text('sp-product-type')));
    await env.change('sp-category', '');
    await env.change('sp-product', 5);
    await env.change('sp-category', 'none');
    assert(env.el('sp-product').value === '5',
        'a selection still inside the filter survives the change');
    await env.change('sp-category', '');
    await env.change('sp-product', '');
    assert(JSON.stringify(env.optionValues(env.el('sp-product'))) === JSON.stringify(['', '7', '2', '3', '5']),
        'the shared env ends this section in the boot shape');
}

// ═══════════════════════════════════════════════════════════════════════════
// B. Preview → design draft + resolved presentation + action + warnings
// ═══════════════════════════════════════════════════════════════════════════
async function previewFlowTests(env) {
    section('B/C/D. Preview: design draft, resolved presentation, purchase action');
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
    const body = previews[0].body;
    assert(env.calls.some(c => c.url === '/api/embedbuilder/template/sale'),
        'the template document is read from the Embed Builder\'s existing route (intentional Step 0 transitional source)');
    assert(body && JSON.stringify(body.products) === JSON.stringify([2]) &&
        body.action && body.action.kind === 'buttons' &&
        body.action.entries[0].product_id === 2,
        'the request carries a design draft: root-product roster + button action',
        JSON.stringify(body));
    assert(body.presentation && body.presentation.mode === 'per_product' &&
        body.presentation.content === SALE_DOC.content &&
        JSON.stringify(body.presentation.embeds) === JSON.stringify(SALE_DOC.embeds),
        'the presentation snapshot comes from the loaded template document',
        JSON.stringify(body.presentation));

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
// C2. Select kinds render as the select that will be published
// ═══════════════════════════════════════════════════════════════════════════
async function selectActionTests(env) {
    section('C2. A select purchase action renders its options and values');
    env.routes['/api/shop-publisher/preview'] = (call) => ({
        status: 200, body: previewBody(call.body.products[0], 'product_select'),
    });
    await env.change('sp-product', 5);

    const purchase = env.el('sp-purchase');
    assert(purchase.querySelectorAll('.sp-select-mock').length === 1,
        'the select control is rendered');
    const options = purchase.querySelectorAll('.sp-select-option');
    assert(options.length === 2, 'one row per select option', String(options.length));
    assert(options[0].textContent.indexOf('Alpha') !== -1 &&
        options[1].textContent.indexOf('Beta') !== -1,
        'the options carry their resolved labels');
    assert(options[0].textContent.indexOf('1,500 🪙 Coins') !== -1,
        'the options carry their resolved descriptions', options[0].textContent);
    const meta = purchase.querySelectorAll('.sp-purchase-meta')[0].textContent;
    assert(meta.indexOf('custom_id: shop_buy_sel_0') !== -1 &&
        meta.indexOf('shop_buy_5') !== -1,
        'the meta shows the component custom_id and the option VALUES', meta);
    assert(meta.indexOf('existing shop purchase mechanism') !== -1,
        'select options route to the same existing mechanism');
}

// ═══════════════════════════════════════════════════════════════════════════
// E. Stale responses + reset behavior
// ═══════════════════════════════════════════════════════════════════════════
async function guardTests(env) {
    section('E. Stale responses never overwrite a newer preview');

    // Hold every preview response; fire two selections; release OLD first.
    env.routes['/api/shop-publisher/preview'] = (call) => ({
        hold: call.body.products[0] === 2,   // the FIRST request (product 2) is slow
        status: 200,
        body: Object.assign(previewBody(call.body.products[0]), {
            preview: Object.assign(previewBody(call.body.products[0]).preview, {
                action: {
                    kind: 'buttons', style: 'green',
                    entries: [{
                        product_id: call.body.products[0],
                        custom_id: 'shop_buy_' + call.body.products[0],
                        label: 'Buy P' + call.body.products[0], description: '',
                        emoji: '🛒', style: 'green', free: false,
                    }],
                },
            }),
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
    env.routes['/api/shop-publisher/preview'] = () => ({
        status: 200,
        body: { success: false, error: 'products[] must list at least one root product id.', problems: [{ code: 'empty_roster', path: 'products', message: 'products[] must list at least one root product id.' }] },
    });
    await env.change('sp-product', 2);
    assert(env.text('sp-status').indexOf('products[] must list') !== -1,
        'a rejected draft is reported on the status line', env.text('sp-status'));

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
        body: { templates: TEMPLATES.slice(1), products: PRODUCTS.slice(1), tokens: TOKENS },
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

// ═══════════════════════════════════════════════════════════════════════════
// H. Design Draft Persistence: Save/Load/Delete + actual-draft dirty state
// ═══════════════════════════════════════════════════════════════════════════
async function designTests() {
    section('H. Design drafts: Save/Load/Delete over the in-memory draft');
    const env = makeEnv({ routes: defaultRoutes() });
    await env.mount();

    // Boot: the saved-design picker fills from one list call; dirty is clean.
    assert(env.calls.filter(c => c.url === '/api/shop-publisher/designs'
        && (c.init.method || 'GET') === 'GET').length === 1,
        'the saved-design list is fetched exactly once per visit');
    assert(JSON.stringify(env.optionValues(env.el('sp-design'))) === JSON.stringify(['', '3', '4']),
        'the saved-design picker lists every design', JSON.stringify(env.optionValues(env.el('sp-design'))));
    assert(env.el('sp-dirty').hidden === true, 'the dirty marker starts hidden');
    const beforeunload = () => env.win.listeners.filter(l => l.type === 'beforeunload');
    assert(beforeunload().length === 1, 'a beforeunload guard is registered');
    let prevented = 0;
    beforeunload()[0].handler({ preventDefault: () => { prevented += 1; } });
    assert(prevented === 0, 'the guard is inert while the draft is clean');

    // Draft-affecting changes set dirty; status transitions never do.
    await env.change('sp-template', 'banner');
    await env.change('sp-product', 2);
    assert(env.el('sp-dirty').hidden === false,
        'template/presentation + roster changes set dirty');
    await env.settle();
    assert(env.el('sp-dirty').hidden === false,
        'preview/loading/status transitions never change dirty');

    // Save: the full draft + provenance goes out; success clears dirty.
    await env.change('sp-design-name', 'Main save');
    assert(env.el('sp-dirty').hidden === false, 'the design name is persisted state — changing it sets dirty');
    env.el('sp-design-save').dispatch('click');
    await env.settle();
    const posts = () => env.calls.filter(c => c.url === '/api/shop-publisher/designs'
        && c.init.method === 'POST');
    assert(posts().length === 1, 'Save POSTs exactly once');
    assert(posts()[0].body.name === 'Main save' && posts()[0].body.id === undefined,
        'a first save creates — name goes along, no id');
    assert(posts()[0].body.source_template_name === 'banner',
        'provenance (source_template_name) is stored, never dereferenced');
    const sent = posts()[0].body.design;
    assert(sent.presentation.mode === 'per_product' && sent.products[0] === 2
        && sent.action.entries[0].product_id === 2,
        'the body is the full Step 0 draft ({presentation, products, action})',
        JSON.stringify(sent));
    assert(env.text('sp-status').indexOf('Design saved.') !== -1, 'save success is reported');
    assert(env.el('sp-dirty').hidden === true, 'a successful Save clears dirty');
    assert(env.el('sp-design').value === '99', 'the saved design becomes the current one');

    // A later save is a FULL OVERWRITE of the same design (id goes along).
    await env.change('sp-product', 5);
    env.el('sp-design-save').dispatch('click');
    await env.settle();
    assert(posts()[1].body.id === 99, 'saves after the first overwrite the current design');
    assert(posts()[1].body.design.products[0] === 5,
        'the overwrite carries the whole draft, not a patch');

    // Save failure: status error; dirty stays as it was.
    await env.change('sp-product', 2);
    env.routes['/api/shop-publisher/designs'] = () => ({ status: 200, body: { success: false, error: 'nope' } });
    env.el('sp-design-save').dispatch('click');
    await env.settle();
    assert(env.text('sp-status').indexOf('Design save failed') !== -1, 'save failure is a status error');
    assert(env.el('sp-dirty').hidden === false, 'a failed Save leaves dirty as it was');
    env.routes['/api/shop-publisher/designs'] = defaultRoutes()['/api/shop-publisher/designs'];

    // Load: the stored snapshot previews verbatim — NO template fetch.
    const templateFetches = () => env.calls.filter(c => c.url.indexOf('/api/embedbuilder/template/') === 0).length;
    const previews = () => env.calls.filter(c => c.url === '/api/shop-publisher/preview'
        && c.init.method === 'POST');
    const fetchesBefore = templateFetches();
    await env.change('sp-design', '3');
    env.el('sp-design-load').dispatch('click');
    await env.settle();
    assert(templateFetches() === fetchesBefore,
        'Load never re-fetches the source template (provenance only)');
    assert(previews()[previews().length - 1].body.presentation.content === 'Loaded {{product.name}}',
        "the preview runs over the design's OWN presentation snapshot");
    assert(previews()[previews().length - 1].body.products[0] === 2,
        'the loaded roster previews as stored');
    assert(env.el('sp-dirty').hidden === true, 'a successful Load clears dirty');
    assert(env.el('sp-design-name').value === 'Main', 'Load restores the saved design name');

    // Editing after Load mutates the draft but never the snapshot's source.
    await env.change('sp-product', 5);
    assert(env.el('sp-dirty').hidden === false, 'a roster change after Load sets dirty');
    assert(previews()[previews().length - 1].body.presentation.content === 'Loaded {{product.name}}',
        'the snapshot survives roster edits (no refetch, no mutation)');
    assert(templateFetches() === fetchesBefore, 'roster edits never touch the template source');

    // A template change replaces the presentation with a fresh fetch.
    await env.change('sp-template', 'sale');
    await env.settle();
    assert(templateFetches() === fetchesBefore + 1, 'a template change loads the document again');
    assert(previews()[previews().length - 1].body.presentation.content !== 'Loaded {{product.name}}',
        'the new template document becomes the draft presentation');

    // Delete success clears the current draft state AND dirty state.
    await env.change('sp-design', '3');
    env.el('sp-design-delete').dispatch('click');
    await env.settle();
    const deletes = () => env.calls.filter(c => c.init.method === 'DELETE');
    assert(deletes().length === 1 && deletes()[0].url === '/api/shop-publisher/designs/3',
        'Delete targets the selected design');
    assert(env.text('sp-status').indexOf('Design deleted.') !== -1, 'delete success is reported');
    assert(env.el('sp-template').value === '' && env.el('sp-product').value === ''
        && env.el('sp-design-name').value === '',
        'a successful Delete clears the current draft state');
    assert(env.el('sp-dirty').hidden === true, 'a successful Delete clears dirty');
    assert(JSON.stringify(env.optionValues(env.el('sp-design'))) === JSON.stringify(['', '4', '99']),
        'the deleted design leaves the picker',
        JSON.stringify(env.optionValues(env.el('sp-design'))));
    assert(env.el('sp-purchase').hidden === true, 'the preview resets with the cleared draft');

    // Delete failure leaves everything exactly as it was.
    await env.change('sp-design', '4');
    env.routes['/api/shop-publisher/designs/4'] = () => ({ status: 200, body: { success: false, error: 'nope' } });
    env.el('sp-design-delete').dispatch('click');
    await env.settle();
    assert(env.text('sp-status').indexOf('Design delete failed') !== -1, 'delete failure is a status error');
    assert(env.el('sp-design').value === '4', 'a failed Delete leaves the state untouched');

    // The beforeunload guard fires only with actual unsaved draft changes.
    await env.change('sp-product', 2);
    prevented = 0;
    beforeunload()[0].handler({ preventDefault: () => { prevented += 1; } });
    assert(prevented === 1, 'the guard blocks silent loss only while dirty');

    env.unmount();
}

async function deleteInFlightTests() {
    section('K. Active Design Delete preserves edits made in flight');
    const env = makeEnv({ routes: defaultRoutes() });
    await env.mount();
    await env.change('sp-design', '3');
    env.el('sp-design-load').click();
    assert(env.el('sp-dirty').hidden === true,
        'the active Design is clean before Delete begins');

    let deleteRequests = 0;
    env.routes['/api/shop-publisher/designs/3'] = () => {
        deleteRequests += 1;
        return deleteRequests === 1
            ? { hold: true }
            : { status: 200, body: { success: true } };
    };
    env.el('sp-design-delete').click();
    assert(deleteRequests === 1, 'Delete starts for the active clean Design');

    await env.change('sp-product', 5);
    assert(env.el('sp-dirty').hidden === false,
        'the draft can be edited while Delete is in flight');
    env.release();
    await env.settle();

    assert(env.el('sp-dirty').hidden === false && env.el('sp-product').value === '5',
        'successful Delete keeps the newer draft intact and dirty');
    assert(env.el('sp-design-name').value === 'Main' &&
        env.text('sp-status').indexOf('Newer draft edits were kept') !== -1,
        'the active edited Design is retained as an unsaved draft');
    assert(!env.optionValues(env.el('sp-design')).includes('3'),
        'the deleted Design is absent from the saved-design list');

    env.el('sp-design-save').click();
    await env.settle();
    const save = env.calls.filter(c => c.url === '/api/shop-publisher/designs'
        && c.init.method === 'POST').slice(-1)[0];
    assert(save && save.body.id === undefined && save.body.design.products[0] === 5,
        'the preserved draft saves as new state, not by restoring the deleted Design id');
    env.unmount();
}

async function legacyFilterDraftTests() {
    section('J. Legacy Publisher filtered selection stays aligned with Preview and Save');
    const env = makeEnv({ routes: defaultRoutes() });
    await env.mount();
    await env.change('sp-template', 'banner');
    await env.change('sp-product', 5);
    await env.change('sp-category', '11');
    assert(env.el('sp-product').value === '5' &&
        env.optgroups(env.el('sp-product')).some(group => group.label === 'Current selection (outside filter)'),
        'the active root remains visibly selected outside its category filter');
    const preview = env.calls.filter(c => c.url === '/api/shop-publisher/preview').slice(-1)[0];
    assert(preview && preview.body.products[0] === 5,
        'Preview still uses the root visibly retained by the filter');
    await env.change('sp-design-name', 'Filtered draft');
    env.el('sp-design-save').click();
    await env.settle();
    const save = env.calls.filter(c => c.url === '/api/shop-publisher/designs' && c.init.method === 'POST').slice(-1)[0];
    assert(save && save.body.design.products[0] === 5,
        'Save preserves the same visible and previewed root');
    env.unmount();
}

async function legacyDraftProtectionTests() {
    section('I. Legacy Publisher dirty-draft protection');
    const env = makeEnv({ routes: defaultRoutes() });
    await env.mount();
    const confirmations = [];
    let confirmResult = true;
    env.win.confirm = (message) => { confirmations.push(message); return confirmResult; };

    // Load a saved design, make it dirty, then delete a different saved row.
    await env.change('sp-design', '3');
    env.el('sp-design-load').click();
    await env.change('sp-product', 5);
    assert(env.el('sp-dirty').hidden === false, 'active saved draft becomes dirty after an edit');
    await env.change('sp-design', '4');
    env.el('sp-design-delete').click();
    await env.settle();
    assert(env.el('sp-design-name').value === 'Main' && env.el('sp-product').value === '5',
        'deleting a different saved design preserves the active draft');
    assert(env.el('sp-dirty').hidden === false,
        'deleting a different design does not clear active dirty state');
    const saves = env.calls.filter(c => c.url === '/api/shop-publisher/designs' && c.init.method === 'POST');
    env.el('sp-design-save').click();
    await env.settle();
    const lastSave = env.calls.filter(c => c.url === '/api/shop-publisher/designs' && c.init.method === 'POST').slice(-1)[0];
    assert(lastSave && lastSave.body.id === 3 && lastSave.body.design.products[0] === 5,
        'the preserved active draft still saves over its own Design');
    assert(saves.length === 0, 'the preservation test starts without earlier saves');

    // Load cancellation leaves the dirty draft and preview intact.
    await env.change('sp-product', 2);
    await env.change('sp-design', '3');
    confirmResult = false;
    const previewCount = () => env.calls.filter(c => c.url === '/api/shop-publisher/preview').length;
    const beforeLoad = previewCount();
    env.el('sp-design-load').click();
    await env.settle();
    assert(confirmations.some(message => message.indexOf('unsaved changes') !== -1),
        'Load asks before discarding unsaved changes');
    assert(env.el('sp-product').value === '2' && env.el('sp-dirty').hidden === false
        && previewCount() === beforeLoad,
        'declining Load preserves the dirty draft and existing preview');

    // HTMX navigation is guarded separately from beforeunload.
    let prevented = 0, stopped = 0;
    const beforeSwap = env.dom.document.listeners.find(listener =>
        listener.type === 'htmx:beforeSwap' && listener.opts === true);
    assert(!!beforeSwap, 'an in-app HTMX navigation guard is registered');
    beforeSwap.handler({
        detail: { target: env.root },
        preventDefault: () => { prevented += 1; },
        stopImmediatePropagation: () => { stopped += 1; },
    });
    assert(prevented === 1 && stopped === 1,
        'declining in-app navigation cancels the swap before teardown');

    // Confirmed active-design deletion is also explicit when its draft is dirty.
    confirmResult = false;
    await env.change('sp-design', '3');
    const deleteCount = () => env.calls.filter(c => c.url === '/api/shop-publisher/designs/3'
        && c.init.method === 'DELETE').length;
    env.el('sp-design-delete').click();
    await env.settle();
    assert(deleteCount() === 0 && env.el('sp-dirty').hidden === false,
        'declining deletion of the active dirty Design preserves it');
    env.unmount();
}

(async () => {
    await bootTests();
    await catalogFailureTests();
    await designTests();
    await deleteInFlightTests();
    await legacyFilterDraftTests();
    await legacyDraftProtectionTests();
    console.log('\n' + '='.repeat(60));
    if (fail) {
        console.log(pass + '/' + (pass + fail) + ' checks — FAILURES:');
        failures.forEach(f => console.log('  - ' + f));
        process.exitCode = 1;
    } else {
        console.log(pass + '/' + (pass + fail) + ' checks passed.');
    }
})().catch(error => { console.error(error); process.exitCode = 1; });
