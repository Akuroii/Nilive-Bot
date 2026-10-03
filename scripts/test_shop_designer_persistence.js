#!/usr/bin/env node
'use strict';
// Focused Step 1 coverage: the Designer UI uses only the existing guild-scoped
// shop_designs CRUD contract and persists the complete design snapshot.
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { createDom, createWindow, parseTemplate, findById, materialize } = require('./support/dom_stub.js');

let pass = 0, fail = 0;
function assert(ok, label, detail) {
    if (ok) { pass += 1; console.log('  PASS', label); }
    else { fail += 1; console.log('  FAIL', label, detail || ''); }
}
function json(value) { return JSON.parse(JSON.stringify(value)); }
function canonical(value) {
    if (Array.isArray(value)) return value.map(canonical);
    if (value && typeof value === 'object') {
        const result = {};
        Object.keys(value).sort().forEach(key => { result[key] = canonical(value[key]); });
        return result;
    }
    return value;
}
function equal(a, b) { return JSON.stringify(canonical(a)) === JSON.stringify(canonical(b)); }
function tick() { return new Promise(resolve => setTimeout(resolve, 0)); }
const rootDir = path.join(__dirname, '..');
const js = (...parts) => path.join(rootDir, 'dashboard', 'static', 'js', ...parts);
const PRODUCTS = [
    { id: 7, name: 'Same display name', type: 'title', price: 10, price_diamonds: null, enabled: 1, current_stock: null, max_stock: null, prestige_tier: null, featured: 0, category_id: null, option_of_id: null },
    { id: 2, name: 'Other root', type: 'role', price: 20, price_diamonds: null, enabled: 1, current_stock: null, max_stock: null, prestige_tier: null, featured: 0, category_id: null, option_of_id: null },
    { id: 8, name: 'Same display name', type: 'custom', price: 5, price_diamonds: null, enabled: 1, current_stock: null, max_stock: null, prestige_tier: null, featured: 0, category_id: null, option_of_id: 7 },
    { id: 10, name: 'Other option', type: 'custom', price: 15, price_diamonds: null, enabled: 1, current_stock: null, max_stock: null, prestige_tier: null, featured: 0, category_id: null, option_of_id: 7 },
    { id: 11, name: 'Same display name', type: 'custom', price: 8, price_diamonds: null, enabled: 1, current_stock: null, max_stock: null, prestige_tier: null, featured: 0, category_id: null, option_of_id: 2 },
];
const EMBED = { title: 'Persisted offer', description: 'Snapshot text', color: 65280, fields: [{ name: 'Term', value: 'Thirty days', inline: false }] };
const TEMPLATE = { content: 'Saved presentation content', embeds: [EMBED] };

async function setup(options) {
    options = options || {};
    const dom = createDom();
    const window = createWindow();
    let page = null;
    window.confirm = message => {
        options.confirmMessages.push(message);
        return options.confirmAnswers.length ? options.confirmAnswers.shift() : true;
    };
    window.NERO = { definePage(name, module) { if (name !== 'shop-designer') throw Error(name); page = module; } };
    const sandbox = { window, document: dom.document, console, setTimeout, clearTimeout, Promise, Object, Array, Math, Date, JSON, Number, String, RegExp, Error, TypeError, Set, Map, Symbol, isFinite, parseInt };
    vm.createContext(sandbox);
    [js('embed/model.js'), js('embed/assets.js'), js('embed/store.js'), js('embed/validate.js'), js('embed/views/rail.js'), js('embed/views/inspector.js'), js('shop-designer.js')]
        .forEach(file => vm.runInContext(fs.readFileSync(file, 'utf8'), sandbox, { filename: path.basename(file) }));
    const tree = parseTemplate(fs.readFileSync(path.join(rootDir, 'dashboard/templates/manage/shopdesigner.html'), 'utf8'));
    const root = materialize(findById(tree, 'sd-root'), dom.document);
    dom.attach(root);
    const records = json(options.records || []);
    const calls = [];
    let nextId = records.reduce((max, row) => Math.max(max, Number(row.id) || 0), 0) + 1;
    let failNextSave = false;
    let failNextDelete = false;
    const ctx = {
        on(target, type, handler) { target.addEventListener(type, handler); },
        fetchJSON(url, request) {
            request = request || {};
            const method = request.method || 'GET';
            const body = request.body ? JSON.parse(request.body) : null;
            calls.push({ url, method, body });
            if (url === '/api/shop-publisher/designs' && method === 'GET') {
                if (options.listError) return Promise.reject(Error('design list offline'));
                return Promise.resolve({ success: true, designs: json(records) });
            }
            if (url === '/api/shop-publisher/designs' && method === 'POST') {
                if (failNextSave) { failNextSave = false; return Promise.reject(Error('save rejected')); }
                const id = body.id == null ? nextId++ : Number(body.id);
                const row = { id, name: body.name, source_template_name: body.source_template_name || null, design: json(body.design) };
                const index = records.findIndex(item => Number(item.id) === id);
                if (index < 0) records.push(row); else records[index] = row;
                return Promise.resolve({ success: true, design: json(row) });
            }
            const match = url.match(/^\/api\/shop-publisher\/designs\/(\d+)$/);
            if (match && method === 'DELETE') {
                if (failNextDelete) { failNextDelete = false; return Promise.reject(Error('delete rejected')); }
                const index = records.findIndex(item => Number(item.id) === Number(match[1]));
                if (index < 0) return Promise.reject(Error('not found'));
                records.splice(index, 1);
                return Promise.resolve({ success: true });
            }
            if (url === '/api/shop-publisher/catalog') return Promise.resolve({ products: PRODUCTS, templates: ['snapshot-template'] });
            if (url === '/api/shop-publisher/categories') return Promise.resolve({ categories: [] });
            if (url === '/api/embedbuilder/template/snapshot-template') return Promise.resolve({ template: TEMPLATE });
            const optionMatch = url.match(/^\/api\/shop-publisher\/products\/(\d+)\/options$/);
            if (optionMatch) {
                const rootProduct = PRODUCTS.find(product => product.id === Number(optionMatch[1]));
                return Promise.resolve({ success: true, root: rootProduct, options: PRODUCTS.filter(product => product.option_of_id === Number(optionMatch[1])) });
            }
            return Promise.reject(Error('unexpected request ' + method + ' ' + url));
        },
        isDestroyed() { return false; },
    };
    page.init(root, ctx);
    const initialDesignStatus = root.querySelector('#sd-design-status').textContent;
    return { dom, window, root, page, sandbox, calls, records, ctx, initialDesignStatus,
        setFailSave() { failNextSave = true; }, setFailDelete() { failNextDelete = true; } };
}
function click(container, node) { container.dispatch('click', { target: node }); }
function clickButton(app, selector) {
    const button = app.root.querySelector(selector);
    button.dispatch('click', { target: button });
}
function addProduct(app, id) {
    const list = app.root.querySelector('#sd-available');
    const button = list.querySelectorAll('button').find(node => node.getAttribute('data-add-product') === String(id));
    if (button) click(list, button);
}
function addActionProduct(app, id) {
    const select = app.root.querySelector('#sd-action-product');
    select.value = String(id);
    clickButton(app, '#sd-action-add');
}
function setActionField(app, entryIndex, field, value) {
    const entries = app.root.querySelector('#sd-action-entries');
    const entry = entries.children[entryIndex];
    const input = entry.querySelectorAll('input').find(node => node.getAttribute('data-action-field') === field);
    input.value = value;
    entries.dispatch('input', { target: input });
}
function switchAction(app, kind) {
    const select = app.root.querySelector('#sd-action-kind');
    select.value = kind;
    select.dispatch('change');
}
async function save(app) { clickButton(app, '#sd-design-save'); await tick(); await tick(); }
function lastCall(app, method, url) { return app.calls.filter(call => call.method === method && call.url === url).slice(-1)[0]; }

async function main() {
    const empty = await setup({ confirmMessages: [], confirmAnswers: [] });
    assert(empty.initialDesignStatus.includes('Loading'), 'saved-design list loading state is shown before the request resolves');
    await tick();
    assert(empty.root.querySelector('#sd-design-empty').hidden === false && empty.root.querySelector('#sd-design-empty').textContent.includes('No saved designs'), 'successful empty list has a distinct empty state');
    empty.page.destroy();

    const failedList = await setup({ listError: true, confirmMessages: [], confirmAnswers: [] });
    await tick();
    assert(failedList.root.querySelector('#sd-design-status').classList.contains('sd-error') && failedList.root.querySelector('#sd-design-status').textContent.includes('offline'), 'list failure is surfaced separately from the empty state');
    assert(failedList.root.querySelector('#sd-design-empty').textContent.includes('could not be loaded'), 'list error does not falsely claim that there are no designs');
    failedList.page.destroy();

    const invalidLegacy = await setup({
        confirmMessages: [], confirmAnswers: [],
        records: [{ id: 90, name: 'Legacy option roster', source_template_name: null,
            design: { presentation: { mode: 'per_product', content: '', embeds: [] },
                products: [7, 8], action: { kind: 'buttons', entries: [{ product_id: 7 }] } } }],
    });
    await tick();
    invalidLegacy.root.querySelector('#sd-design-select').value = '90';
    invalidLegacy.root.querySelector('#sd-design-load').click();
    assert(invalidLegacy.root.querySelector('#sd-roster').children.length === 0 &&
        invalidLegacy.root.querySelector('#sd-design-status').textContent.includes('Only root products'),
        'Load rejects a legacy snapshot that contains an option row in products[]');
    invalidLegacy.page.destroy();

    const options = { confirmMessages: [], confirmAnswers: [] };
    const app = await setup(options);
    await tick();
    addProduct(app, 7); addProduct(app, 2);
    app.root.querySelector('#sd-template').value = 'snapshot-template';
    app.root.querySelector('#sd-template').dispatch('change');
    await tick();
    app.root.querySelector('#sd-mode').value = 'frame';
    app.root.querySelector('#sd-mode').dispatch('change');
    addActionProduct(app, 7);
    setActionField(app, 0, 'label', 'Buy {{product.name}}');
    setActionField(app, 0, 'emoji', '🛍️');
    const name = app.root.querySelector('#sd-design-name');
    name.value = 'Complete shop'; name.dispatch('input');
    assert(app.root.querySelector('#sd-dirty').hidden === false, 'first content/name edits transition a new design to dirty');

    const model = app.sandbox.window.NERO.embed.model;
    const expectedPresentation = json(model.toDiscordPayload(model.normalizeDocument(model.fromApiDocument(TEMPLATE.content, TEMPLATE.embeds, {}))));
    expectedPresentation.mode = 'frame';
    await save(app);
    const createCall = lastCall(app, 'POST', '/api/shop-publisher/designs');
    assert(createCall && createCall.body.id === undefined && createCall.body.name === 'Complete shop', 'Save creates through the existing designs POST without an id');
    assert(createCall && createCall.body.source_template_name === 'snapshot-template', 'saved snapshot carries only the existing template provenance field');
    assert(createCall && equal(Object.keys(createCall.body.design).sort(), ['action', 'presentation', 'products']), 'POST contains exactly the complete existing design contract');
    assert(createCall && equal(createCall.body.design.products, [7, 2]), 'root products persist as the ordered product-ID array');
    assert(createCall && equal(createCall.body.design.presentation, expectedPresentation), 'presentation persists as the full normalized content/embed/mode snapshot');
    assert(createCall && equal(createCall.body.design.action, { kind: 'buttons', entries: [{ product_id: 7, label: 'Buy {{product.name}}', emoji: '🛍️' }] }), 'Buttons round-trip ordered IDs, label, and emoji without adding Select-only fields');
    assert(app.root.querySelector('#sd-dirty').hidden === true && app.root.querySelector('#sd-design-select').value === '1', 'successful create clears dirty state and selects the saved record');

    app.root.querySelector('#sd-design-load').click();
    await tick();
    assert(app.root.querySelector('#sd-mode').value === 'frame' && app.root.querySelector('#sd-embed-inspector').querySelectorAll('textarea')[0].value === TEMPLATE.content, 'Load restores the saved presentation through the existing Embed Builder inspector');
    assert(app.root.querySelector('#sd-action-entries').children.length === 1 && app.root.querySelector('#sd-action-entries').children[0].getAttribute('data-action-product-id') === '7', 'Load restores the saved Buttons mapping by product ID');
    assert(app.root.querySelector('#sd-roster').children.map(node => Number(node.getAttribute('data-roster-id'))).join(',') === '7,2', 'Load restores the root-only products[] roster without adding option rows');
    assert(app.root.querySelector('#sd-dirty').hidden === true, 'loading a saved snapshot establishes a clean baseline');

    // Exercise full overwrite with Product Select and force one failed write;
    // failure must leave both the server snapshot and dirty draft untouched.
    switchAction(app, 'product_select');
    addActionProduct(app, 2);
    setActionField(app, 0, 'description', 'First root');
    setActionField(app, 1, 'label', 'Second root');
    setActionField(app, 1, 'description', 'Second choice');
    setActionField(app, 1, 'emoji', '⭐');
    const placeholder = app.root.querySelector('#sd-action-placeholder');
    placeholder.value = 'Choose a product'; placeholder.dispatch('input');
    assert(app.root.querySelector('#sd-dirty').hidden === false, 'editing a loaded record marks it dirty');
    const oldSnapshot = json(app.records[0].design);
    app.setFailSave();
    await save(app);
    assert(app.root.querySelector('#sd-design-status').classList.contains('sd-error') && app.root.querySelector('#sd-dirty').hidden === false, 'Save failure is visible and does not clear dirty state');
    assert(equal(app.records[0].design, oldSnapshot), 'failed full overwrite leaves the stored record unchanged');
    await save(app);
    const selectCall = lastCall(app, 'POST', '/api/shop-publisher/designs');
    assert(selectCall.body.id === 1, 'Save of a loaded record uses the existing id for full overwrite');
    assert(equal(selectCall.body.design.action, {
        kind: 'product_select', placeholder: 'Choose a product', entries: [
            { product_id: 7, label: 'Buy {{product.name}}', emoji: '🛍️', description: 'First root' },
            { product_id: 2, label: 'Second root', emoji: '⭐', description: 'Second choice' },
        ],
    }), 'Product Select full-overwrite persists placeholder and ordered entry presentation fields');
    assert(app.root.querySelector('#sd-dirty').hidden === true, 'successful full overwrite returns to clean state');

    // Option Select saves only direct option IDs; root context is restored from
    // shop_items.option_of_id in catalog data and is never inferred by name.
    switchAction(app, 'option_select');
    const rootSelect = app.root.querySelector('#sd-action-option-root');
    rootSelect.value = '7'; rootSelect.dispatch('change');
    await tick();
    addActionProduct(app, 8); addActionProduct(app, 10);
    setActionField(app, 0, 'label', 'One month');
    setActionField(app, 0, 'description', 'Short term');
    setActionField(app, 0, 'emoji', '📅');
    setActionField(app, 1, 'label', 'Long term');
    placeholder.value = 'Choose a term'; placeholder.dispatch('input');
    await save(app);
    const optionCall = lastCall(app, 'POST', '/api/shop-publisher/designs');
    assert(equal(optionCall.body.design.products, [7, 2]) && optionCall.body.design.action.kind === 'option_select', 'Option Select keeps root products separate from its action choices');
    assert(equal(optionCall.body.design.action.entries.map(entry => entry.product_id), [8, 10]), 'Option Select persists only the ordered direct-option IDs');
    assert(optionCall.body.design.action.placeholder === 'Choose a term' && optionCall.body.design.action.entries[0].description === 'Short term', 'Option Select preserves its placeholder, descriptions, labels, and emoji');
    app.root.querySelector('#sd-design-load').click();
    await tick(); await tick();
    assert(app.root.querySelector('#sd-action-option-root').value === '7', 'Option Select root context is restored via option_of_id despite duplicate product names');
    assert(app.root.querySelector('#sd-action-entries').children.map(node => node.getAttribute('data-action-product-id')).join(',') === '8,10', 'Option Select Load retains direct-option entry IDs and ordering');
    assert(app.root.querySelector('#sd-roster').children.map(node => Number(node.getAttribute('data-roster-id'))).join(',') === '7,2', 'Option Select Load keeps its direct options in action.entries, not products[]');
    assert(app.root.querySelector('#sd-dirty').hidden === true, 'Option Select load is clean after direct-family restoration');

    // Browser unload and HTMX in-page navigation guards; the decline branch
    // cancels navigation while the confirmed branch allows it.
    placeholder.value = 'Unsaved placeholder'; placeholder.dispatch('input');
    let unloadPrevented = false;
    const unloadEvent = { preventDefault() { unloadPrevented = true; }, returnValue: undefined };
    app.window.listeners.filter(item => item.type === 'beforeunload').forEach(item => item.handler(unloadEvent));
    assert(unloadPrevented && unloadEvent.returnValue === '', 'dirty draft activates the browser beforeunload guard');
    options.confirmAnswers.push(false);
    const declinedEvent = { detail: {}, prevented: false, stopped: false,
        preventDefault() { this.prevented = true; }, stopImmediatePropagation() { this.stopped = true; } };
    app.dom.document.dispatch('htmx:beforeSwap', declinedEvent);
    assert(declinedEvent.prevented && declinedEvent.stopped && declinedEvent.detail.shouldSwap === false, 'declining HTMX navigation cancels the swap before nav-lifecycle teardown');
    assert(app.root.querySelector('#sd-dirty').hidden === false, 'declined navigation preserves the unsaved draft');

    // Load is destructive only when dirty; both declined and confirmed paths
    // are exercised, followed by Delete decline, failure, and success.
    options.confirmAnswers.push(false);
    clickButton(app, '#sd-design-load');
    assert(app.root.querySelector('#sd-design-status').textContent.includes('cancelled') && app.root.querySelector('#sd-dirty').hidden === false, 'declining destructive Load keeps the current draft');
    options.confirmAnswers.push(true);
    clickButton(app, '#sd-design-load');
    await tick(); await tick();
    assert(app.root.querySelector('#sd-dirty').hidden === true && app.root.querySelector('#sd-action-placeholder').value === 'Choose a term', 'confirmed Load discards draft edits and restores the saved record');

    options.confirmAnswers.push(false);
    clickButton(app, '#sd-design-delete');
    assert(app.records.length === 1 && app.root.querySelector('#sd-design-status').textContent.includes('cancelled'), 'declining Delete leaves the stored design intact');
    options.confirmAnswers.push(true);
    app.setFailDelete();
    clickButton(app, '#sd-design-delete');
    await tick(); await tick();
    assert(app.records.length === 1 && app.root.querySelector('#sd-design-status').classList.contains('sd-error'), 'Delete mutation failure is surfaced without dropping the saved record');
    options.confirmAnswers.push(true);
    clickButton(app, '#sd-design-delete');
    await tick(); await tick();
    assert(app.records.length === 0 && app.root.querySelector('#sd-design-status').textContent === 'Design deleted.', 'confirmed Delete removes the record through the existing ID endpoint');
    assert(app.root.querySelector('#sd-current-design').textContent === 'New design' && app.root.querySelector('#sd-dirty').hidden === true, 'deleting the active record returns the shell to a clean new design');

    const newName = app.root.querySelector('#sd-design-name');
    newName.value = 'Unsaved before navigation'; newName.dispatch('input');
    options.confirmAnswers.push(true);
    const acceptedEvent = { detail: {}, prevented: false, preventDefault() { this.prevented = true; } };
    app.dom.document.dispatch('htmx:beforeSwap', acceptedEvent);
    assert(!acceptedEvent.prevented, 'confirmed HTMX navigation is allowed');
    assert(app.calls.some(call => call.method === 'DELETE' && call.url === '/api/shop-publisher/designs/1'), 'Delete uses only the existing saved-design ID endpoint');
    assert(!app.calls.some(call => /\/publish(?:\/|$)|\/purchase(?:\/|$)|shop_buy/.test(call.url)), 'persistence integration adds no Publication or purchase calls');
    app.page.destroy();

    console.log('\n' + pass + '/' + (pass + fail) + ' checks passed.');
    if (fail) process.exitCode = 1;
}
main().catch(error => { console.error(error); process.exitCode = 1; });
