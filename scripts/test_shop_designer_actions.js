#!/usr/bin/env node
'use strict';
// Region C Buttons-only contract: live roster IDs map to ordered action entries;
// validation goes only through the existing full-design preview API.
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { createDom, parseTemplate, findById, materialize } = require('./support/dom_stub.js');

let pass = 0, fail = 0;
function assert(ok, label, detail) {
    if (ok) { pass += 1; console.log('  PASS', label); }
    else { fail += 1; console.log('  FAIL', label, detail || ''); }
}
function deepText(node) {
    if (!node) return '';
    if (node.nodeType === 3) return node.nodeValue || '';
    return (node._text || '') + ' ' + (node._html || '') + ' ' + (node.children || []).map(deepText).join(' ');
}
const rootDir = path.join(__dirname, '..');
const js = (...parts) => path.join(rootDir, 'dashboard', 'static', 'js', ...parts);
const PRODUCTS = [
    { id: 7, name: 'Zulu', type: 'title', price: 10, price_diamonds: null, enabled: 1, current_stock: null, max_stock: null, prestige_tier: null, featured: 0, category_id: null, option_of_id: null },
    { id: 2, name: 'Alpha', type: 'role', price: 20, price_diamonds: null, enabled: 1, current_stock: null, max_stock: null, prestige_tier: null, featured: 0, category_id: null, option_of_id: null },
    { id: 9, name: 'Other root', type: 'custom', price: 30, price_diamonds: null, enabled: 1, current_stock: null, max_stock: null, prestige_tier: null, featured: 0, category_id: null, option_of_id: null },
    { id: 8, name: 'Zulu — 7 Days', type: 'custom', price: 5, price_diamonds: null, enabled: 1, current_stock: null, max_stock: null, prestige_tier: null, featured: 0, category_id: null, option_of_id: 7 },
    { id: 10, name: 'Zulu — 30 Days', type: 'custom', price: 15, price_diamonds: null, enabled: 1, current_stock: null, max_stock: null, prestige_tier: null, featured: 0, category_id: null, option_of_id: 7 },
    { id: 11, name: 'Other option', type: 'custom', price: 8, price_diamonds: null, enabled: 1, current_stock: null, max_stock: null, prestige_tier: null, featured: 0, category_id: null, option_of_id: 9 },
    { id: 12, name: 'Chained option', type: 'custom', price: 2, price_diamonds: null, enabled: 1, current_stock: null, max_stock: null, prestige_tier: null, featured: 0, category_id: null, option_of_id: 8 },
];
function rosterIds(root) {
    return root.querySelector('#sd-roster').children.map(node => Number(node.getAttribute('data-roster-id')));
}
function addRosterProduct(root, id) {
    const available = root.querySelector('#sd-available');
    const button = available.querySelectorAll('button').find(node => node.getAttribute('data-add-product') === String(id));
    if (button) available.dispatch('click', { target: button });
}
function selectActionProduct(root, id) {
    const select = root.querySelector('#sd-action-product');
    select.value = String(id);
    root.querySelector('#sd-action-add').dispatch('click');
}
async function tick() { await new Promise(resolve => setTimeout(resolve, 0)); }

async function main() {
    const dom = createDom();
    let page = null;
    const window = { NERO: { definePage(name, module) { if (name !== 'shop-designer') throw Error(name); page = module; } } };
    const sandbox = { window, document: dom.document, console, setTimeout, clearTimeout, Promise, Object, Array, Math, Date, JSON, Number, String, RegExp, Error, TypeError, Set, Map, Symbol, isFinite, parseInt };
    vm.createContext(sandbox);
    [js('embed/model.js'), js('embed/assets.js'), js('embed/store.js'), js('embed/validate.js'), js('embed/views/rail.js'), js('embed/views/inspector.js'), js('shop-designer.js')]
        .forEach(file => vm.runInContext(fs.readFileSync(file, 'utf8'), sandbox, { filename: path.basename(file) }));
    const template = parseTemplate(fs.readFileSync(path.join(rootDir, 'dashboard/templates/manage/shopdesigner.html'), 'utf8'));
    const root = materialize(findById(template, 'sd-root'), dom.document);
    dom.attach(root);

    const calls = [];
    const routes = {
        '/api/shop-publisher/designs': { success: true, designs: [] },
        '/api/shop-publisher/catalog': { products: PRODUCTS, templates: [] },
        '/api/shop-publisher/categories': { categories: [] },
        '/api/shop-publisher/products/7/options': {
            success: true, root: PRODUCTS.find(product => product.id === 7),
            options: [PRODUCTS.find(product => product.id === 7), PRODUCTS.find(product => product.id === 8), PRODUCTS.find(product => product.id === 10), PRODUCTS.find(product => product.id === 11), PRODUCTS.find(product => product.id === 12)],
        },
        '/api/shop-publisher/preview': request => {
            const products = request.action.entries.map(entry => ({
                product_id: entry.product_id,
                custom_id: 'shop_buy_' + entry.product_id,
                label: entry.label || (request.action.kind === 'product_select' ? PRODUCTS.find(p => p.id === entry.product_id).name : 'Buy ' + PRODUCTS.find(p => p.id === entry.product_id).name),
                description: entry.description || '',
                emoji: entry.emoji || '',
                style: 'green',
            }));
            return { success: true, preview: { action: request.action.kind === 'buttons'
                ? { kind: 'buttons', entries: products }
                : { kind: request.action.kind, component_custom_id: 'shop_buy_sel_0', placeholder: request.action.placeholder || 'Select…', entries: products } } };
        },
    };
    const ctx = {
        on(target, type, handler) { target.addEventListener(type, handler); },
        fetchJSON(url, options) {
            options = options || {};
            const method = options.method || 'GET';
            const call = { url, method, body: options.body ? JSON.parse(options.body) : null };
            calls.push(call);
            if (!Object.prototype.hasOwnProperty.call(routes, url)) return Promise.reject(Error('unexpected request ' + url));
            const route = routes[url];
            return Promise.resolve(typeof route === 'function' ? route(call.body) : route);
        },
        isDestroyed() { return false; },
    };
    page.init(root, ctx);
    await tick();

    assert(root.querySelector('#sd-action') !== null, 'Buttons action editor is mounted');
    assert(root.querySelector('#sd-action-product') !== null && root.querySelector('#sd-action-validate') !== null, 'editor exposes roster mapping and explicit existing-pipeline validation');
    assert(root.querySelector('#sd-action-kind') !== null && root.querySelector('#sd-action-kind').children.map(option => option.getAttribute('value')).join(',') === 'buttons,product_select,option_select', 'Buttons, Product Select, and separate Option Select kinds are exposed', root.querySelector('#sd-action-kind').children.map(option => option.getAttribute('value')).join(','));
    assert(root.querySelector('#sd-action-style') === null, 'button style remains fixed green');

    addRosterProduct(root, 7);
    addRosterProduct(root, 2);
    addRosterProduct(root, 8);
    const rosterBefore = rosterIds(root).join(',');
    assert(rosterBefore === '7,2', 'Region A adds root products only; option rows cannot enter products[]');
    assert(!root.querySelector('#sd-available').querySelectorAll('button').some(button => button.getAttribute('data-add-product') === '8'), 'option rows are absent from the roster add controls');
    assert(root.querySelector('#sd-action-product').children.map(option => option.value).join(',') === ',7,2', 'action mapping choices are derived only from rostered roots');

    selectActionProduct(root, 7);
    selectActionProduct(root, 2);
    let entries = root.querySelector('#sd-action-entries').children;
    assert(entries.length === 2 && entries.map(entry => Number(entry.getAttribute('data-action-product-id'))).join(',') === '7,2', 'adding action entries maps products without changing roster order');
    assert(root.querySelector('#sd-action-product').children.map(option => option.value).join(',') === '', 'already-mapped root products cannot be duplicated as action entries');

    const labelInput = entries[0].querySelectorAll('input').find(input => input.getAttribute('data-action-field') === 'label');
    const emojiInput = entries[0].querySelectorAll('input').find(input => input.getAttribute('data-action-field') === 'emoji');
    labelInput.value = 'Special {{product.name}}'; root.querySelector('#sd-action-entries').dispatch('input', { target: labelInput });
    emojiInput.value = '✨'; root.querySelector('#sd-action-entries').dispatch('input', { target: emojiInput });
    assert(labelInput.value === 'Special {{product.name}}' && emojiInput.value === '✨', 'optional custom label and emoji are editable');

    const moveUp = entries[1].querySelectorAll('button').find(button => button.getAttribute('data-action-operation') === 'up');
    root.querySelector('#sd-action-entries').dispatch('click', { target: moveUp });
    entries = root.querySelector('#sd-action-entries').children;
    assert(entries.map(entry => Number(entry.getAttribute('data-action-product-id'))).join(',') === '2,7', 'action entries reorder independently of products[]');
    assert(rosterIds(root).join(',') === rosterBefore, 'reordering action entries leaves Region A products[] unchanged');

    root.querySelector('#sd-action-validate').dispatch('click');
    await tick();
    const validationCall = calls.find(call => call.url === '/api/shop-publisher/preview');
    assert(validationCall && validationCall.method === 'POST', 'validation calls only the existing complete-design preview endpoint');
    assert(validationCall && JSON.stringify(validationCall.body.products) === JSON.stringify([7, 2]), 'validation and preview receive the unchanged root-only Region A roster');
    assert(validationCall && validationCall.body.action.kind === 'buttons' && validationCall.body.action.entries.length === 2, 'existing pipeline receives only a buttons action with mapped entries');
    assert(validationCall && validationCall.body.action.entries[0].product_id === 2 && validationCall.body.action.entries[1].product_id === 7, 'validation body preserves the independent action-entry order');
    assert(validationCall && validationCall.body.action.entries[1].label === 'Special {{product.name}}' && validationCall.body.action.entries[1].emoji === '✨', 'custom label and emoji pass through the existing action contract');
    assert(validationCall && validationCall.body.action.entries[0].label === '' && validationCall.body.action.entries[0].emoji === '', 'blank optional fields remain blank so the existing builder supplies its defaults');
    assert(validationCall && validationCall.body.presentation.mode === 'per_product' && validationCall.body.presentation.content === '' && validationCall.body.presentation.embeds.length === 0, 'action edits leave Region B presentation state unchanged');
    assert(validationCall && validationCall.body.action.entries.every(entry => Object.keys(entry).sort().join(',') === 'emoji,label,product_id'), 'the in-memory entry shape contains only product_id, label, and emoji');
    assert(validationCall && validationCall.body.action.entries.every(entry => !Object.prototype.hasOwnProperty.call(entry, 'style') && !Object.prototype.hasOwnProperty.call(entry, 'description')), 'Buttons do not gain Product Select style/description configuration');
    assert(root.querySelector('#sd-action-validation').textContent.includes('Valid') && root.querySelector('#sd-action-validation').textContent.includes('green button'), 'existing pipeline result reports validated green button construction');

    const remove = entries[0].querySelectorAll('button').find(button => button.getAttribute('data-action-operation') === 'remove');
    root.querySelector('#sd-action-entries').dispatch('click', { target: remove });
    entries = root.querySelector('#sd-action-entries').children;
    assert(entries.length === 1 && Number(entries[0].getAttribute('data-action-product-id')) === 7, 'removing an action entry changes only the action mapping');
    assert(rosterIds(root).join(',') === rosterBefore, 'add/edit/remove/reorder action controls never mutate, reorder, or duplicate products[]');

    // Removing a rostered product keeps its action entry untouched but marks it.
    assert(!deepText(entries[0]).includes('Not in roster'), 'a rostered product has no orphan badge');
    const rosterList = root.querySelector('#sd-roster');
    const rosterButton = (id, action) => rosterList.querySelectorAll('button').find(button => button.getAttribute('data-product-id') === String(id) && button.getAttribute('data-roster-action') === action);
    rosterList.dispatch('click', { target: rosterButton(7, 'remove') });
    entries = root.querySelector('#sd-action-entries').children;
    assert(rosterIds(root).join(',') === '2' && entries.length === 1 && Number(entries[0].getAttribute('data-action-product-id')) === 7, 'removing a roster product does not cascade into the action entries');
    assert(deepText(entries[0]).includes('Not in roster'), 'an action entry whose product left the roster shows a display-only badge');
    addRosterProduct(root, 7);
    rosterList.dispatch('click', { target: rosterButton(7, 'up') });
    entries = root.querySelector('#sd-action-entries').children;
    assert(rosterIds(root).join(',') === rosterBefore && !deepText(entries[0]).includes('Not in roster'), 're-adding the product clears the badge and the original roster order is restored');

    // Product Select uses only explicit root choices from the existing roster.
    const kindSelect = root.querySelector('#sd-action-kind');
    kindSelect.value = 'product_select'; kindSelect.dispatch('change');
    assert(root.querySelector('#sd-action-product').children.map(option => option.value).join(',') === ',2', 'Product Select choices exclude option rows and remain limited to rostered roots');
    const previewCountBeforeMinimum = calls.filter(call => call.url === '/api/shop-publisher/preview').length;
    root.querySelector('#sd-action-validate').dispatch('click');
    assert(calls.filter(call => call.url === '/api/shop-publisher/preview').length === previewCountBeforeMinimum && root.querySelector('#sd-action-validation').textContent.includes('at least 2'), 'Product Select enforces its two-entry minimum before preview');
    selectActionProduct(root, 2);
    const productSelectEntries = root.querySelector('#sd-action-entries');
    const selectedEntry = productSelectEntries.querySelectorAll('[data-action-entry]').find(entry => entry.getAttribute('data-action-product-id') === '2');
    const controls = selectedEntry.querySelectorAll('input');
    const selectLabel = controls.find(input => input.getAttribute('data-action-field') === 'label');
    const description = controls.find(input => input.getAttribute('data-action-field') === 'description');
    const emoji = controls.find(input => input.getAttribute('data-action-field') === 'emoji');
    selectLabel.value = 'Choose {{product.name}}'; productSelectEntries.dispatch('input', { target: selectLabel });
    description.value = 'Select the {{product.name}}'; productSelectEntries.dispatch('input', { target: description });
    emoji.value = '🗡️'; productSelectEntries.dispatch('input', { target: emoji });
    const placeholder = root.querySelector('#sd-action-placeholder');
    placeholder.value = 'Pick one'; placeholder.dispatch('input');
    root.querySelector('#sd-action-validate').dispatch('click');
    await tick();
    const selectCall = calls.filter(call => call.url === '/api/shop-publisher/preview').slice(-1)[0];
    assert(selectCall.body.action.kind === 'product_select' && selectCall.body.action.entries.length === 2, 'Product Select preview request carries the selected action kind and entries');
    assert(selectCall.body.action.entries.map(entry => entry.product_id).join(',') === '7,2', 'Product Select entry ordering follows action.entries');
    assert(selectCall.body.action.entries[1].label === 'Choose {{product.name}}' && selectCall.body.action.entries[1].description === 'Select the {{product.name}}' && selectCall.body.action.entries[1].emoji === '🗡️', 'label, description, and emoji are wired into the preview request');
    assert(selectCall.body.action.placeholder === 'Pick one', 'placeholder is wired into the preview request');
    assert(root.querySelector('#sd-action-validation').textContent.includes('Product Select choice'), 'Product Select validation status uses select wording');

    // Options are selected only through the configured root's direct family;
    // they never enter products[].
    kindSelect.value = 'option_select'; kindSelect.dispatch('change');
    const rootPicker = root.querySelector('#sd-action-option-root');
    assert(rootPicker.children.map(option => option.value).join(',') === ',7,2', 'Option Select anchor choices are only roots already in products[]');
    rootPicker.value = '7'; rootPicker.dispatch('change');
    await tick();
    const familyCall = calls.find(call => call.url === '/api/shop-publisher/products/7/options');
    assert(familyCall && familyCall.method === 'GET', 'the selected roster root loads its direct family through the existing read endpoint');
    assert(root.querySelector('#sd-action-product').children.map(option => option.value).join(',') === ',8,10', 'only direct options of the selected family are offered; root, other-family option, and chained option are excluded');
    const noOptionPreviewCount = calls.filter(call => call.url === '/api/shop-publisher/preview').length;
    root.querySelector('#sd-action-validate').dispatch('click');
    assert(calls.filter(call => call.url === '/api/shop-publisher/preview').length === noOptionPreviewCount && root.querySelector('#sd-action-validation').textContent.includes('at least 2 direct options'), 'Option Select enforces its minimum before Preview');
    root.querySelector('#sd-action-product').value = '8'; root.querySelector('#sd-action-add').dispatch('click');
    root.querySelector('#sd-action-product').value = '10'; root.querySelector('#sd-action-add').dispatch('click');
    const optionEntries = root.querySelector('#sd-action-entries');
    assert(optionEntries.children.map(entry => entry.getAttribute('data-action-product-id')).join(',') === '8,10', 'Option Select entry order follows the user-added action.entries order');
    const optionEntry = optionEntries.children.find(entry => entry.getAttribute('data-action-product-id') === '10');
    const optionInputs = optionEntry.querySelectorAll('input');
    const optionLabel = optionInputs.find(input => input.getAttribute('data-action-field') === 'label');
    const optionDescription = optionInputs.find(input => input.getAttribute('data-action-field') === 'description');
    const optionEmoji = optionInputs.find(input => input.getAttribute('data-action-field') === 'emoji');
    optionLabel.value = 'Thirty days'; optionEntries.dispatch('input', { target: optionLabel });
    optionDescription.value = 'A month'; optionEntries.dispatch('input', { target: optionDescription });
    optionEmoji.value = '📅'; optionEntries.dispatch('input', { target: optionEmoji });
    placeholder.value = 'Choose duration'; placeholder.dispatch('input');
    root.querySelector('#sd-action-validate').dispatch('click');
    await tick();
    const optionCall = calls.filter(call => call.url === '/api/shop-publisher/preview').slice(-1)[0];
    assert(optionCall.body.action.kind === 'option_select' && optionCall.body.products.join(',') === '7,2', 'Option Select Preview uses option entries under the selected roster root without adding a root entry');
    assert(optionCall.body.action.entries.map(entry => entry.product_id).join(',') === '8,10', 'Option Select Preview preserves entry ordering');
    assert(optionCall.body.action.entries[1].label === 'Thirty days' && optionCall.body.action.entries[1].description === 'A month' && optionCall.body.action.entries[1].emoji === '📅' && optionCall.body.action.placeholder === 'Choose duration', 'Option Select Preview request includes label, description, emoji, and placeholder');

    const allowed = ['/api/shop-publisher/designs', '/api/shop-publisher/catalog', '/api/shop-publisher/categories', '/api/shop-publisher/products/7/options', '/api/shop-publisher/preview'];
    const unexpected = calls.filter(call => allowed.indexOf(call.url) === -1 ||
        (call.url === '/api/shop-publisher/preview' && call.method !== 'POST') ||
        (call.url === '/api/shop-publisher/designs' && call.method !== 'GET'));
    assert(unexpected.length === 0, 'no purchase execution, publication, or unexpected API request was made', JSON.stringify(unexpected));
    assert(calls.filter(call => call.url === '/api/shop-publisher/designs').length === 1 &&
        !calls.some(call => call.url.indexOf('/publish') !== -1 || call.url.indexOf('/purchase') !== -1 || call.url.indexOf('/shop_buy') !== -1),
        'Designer performs only its startup saved-design list read; no write, publication, or purchase endpoint is called');

    page.destroy();
    console.log('\n' + pass + '/' + (pass + fail) + ' checks passed.');
    if (fail) process.exitCode = 1;
}
main().catch(error => { console.error(error); process.exitCode = 1; });
