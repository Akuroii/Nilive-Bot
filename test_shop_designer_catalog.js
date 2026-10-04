#!/usr/bin/env node
'use strict';

// Region A only: exercise the real Designer page module and template with a
// DOM double. No API/server/database or external dependencies.
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { createDom, parseTemplate, findById, materialize } = require('./support/dom_stub.js');

let pass = 0;
let fail = 0;
function assert(condition, label, detail) {
    if (condition) {
        pass += 1;
        console.log('  PASS', label);
    } else {
        fail += 1;
        console.log('  FAIL', label, detail || '');
    }
}
function ids(list) {
    return list.children.map(node => Number(node.getAttribute('data-roster-id')));
}
function buttonWith(node, attr, value) {
    return node.querySelectorAll('button').find(button => button.getAttribute(attr) === String(value));
}
function clickThrough(container, button) {
    container.dispatch('click', { target: button });
}
function text(node) {
    if (!node) return '';
    return (node._text || '') + (node.children || []).map(text).join(' ');
}

const rootDir = path.join(__dirname, '..');
const templatePath = path.join(rootDir, 'dashboard/templates/manage/shopdesigner.html');
const modulePath = path.join(rootDir, 'dashboard/static/js/shop-designer.js');
const templateTree = parseTemplate(fs.readFileSync(templatePath, 'utf8'));
const PRODUCTS = [
    { id: 7, name: 'Zulu', type: 'title', price: 10, price_diamonds: null, enabled: 1, current_stock: null, max_stock: null, prestige_tier: null, featured: 0, category_id: 11 },
    { id: 2, name: 'Alpha', type: 'role', price: 1500, price_diamonds: null, enabled: 1, current_stock: 12, max_stock: 20, prestige_tier: null, featured: 0, category_id: 12 },
    { id: 3, name: 'Midnight', type: 'prestige', price: 2500, price_diamonds: null, enabled: 0, current_stock: null, max_stock: null, prestige_tier: 3, featured: 1, category_id: 999 },
    { id: 5, name: 'Beta', type: 'role', price: 20, price_diamonds: 5, enabled: 1, current_stock: 0, max_stock: 4, prestige_tier: null, featured: 0, category_id: null },
    { id: 8, name: 'Zulu — 7 Days', type: 'custom', price: 5, price_diamonds: null, enabled: 1, current_stock: null, max_stock: null, prestige_tier: null, featured: 0, category_id: 11, option_of_id: 7 },
];
const CATEGORIES = [
    { id: 11, name: 'Cosmetics', emoji: '🎨', product_count: 1 },
    { id: 12, name: 'Roles', emoji: '🛡️', product_count: 1 },
];

async function main() {
    const dom = createDom();
    let pageModule = null;
    const window = {
        NERO: {
            definePage: function (name, module) {
                if (name !== 'shop-designer') throw new Error('unexpected page module ' + name);
                pageModule = module;
            },
        },
    };
    const calls = [];
    const routes = {
        '/api/shop-publisher/designs': { success: true, designs: [] },
        '/api/shop-publisher/catalog': { products: PRODUCTS, templates: ['ignored'], tokens: ['ignored'] },
        '/api/shop-publisher/categories': { success: true, categories: CATEGORIES, uncategorized: 1 },
    };
    const sandbox = {
        window, document: dom.document, console,
        Promise, Object, Array, Math, JSON, Number, String, RegExp, Error,
        parseInt, isFinite,
    };
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(modulePath, 'utf8'), sandbox, { filename: modulePath });
    const root = materialize(findById(templateTree, 'sd-root'), dom.document);
    dom.attach(root);
    const ctx = {
        on: function (target, type, handler) { target.addEventListener(type, handler); },
        fetchJSON: function (url) {
            calls.push({ url, method: 'GET' });
            if (!Object.prototype.hasOwnProperty.call(routes, url)) {
                return Promise.reject(new Error('unexpected request ' + url));
            }
            return Promise.resolve(routes[url]);
        },
        isDestroyed: function () { return false; },
    };
    pageModule.init(root, ctx);
    await new Promise(resolve => setTimeout(resolve, 0));

    const available = root.querySelector('#sd-available');
    const roster = root.querySelector('#sd-roster');
    const filter = root.querySelector('#sd-category');
    assert(calls.length === 3 && calls.every(call => call.method === 'GET'),
        'uses only existing saved-design, catalog, and category GET APIs', JSON.stringify(calls));
    assert(calls.some(call => call.url === '/api/shop-publisher/designs') &&
        calls.some(call => call.url === '/api/shop-publisher/catalog') &&
        calls.some(call => call.url === '/api/shop-publisher/categories'),
        'loads the saved-design list and both live catalog sources');
    assert(available.children.length === 4, 'only root products are initially browsable in the roster catalog');
    assert(!buttonWith(available, 'data-add-product', 8), 'option rows are not offered as independently addable roster products');
    assert(root.querySelector('#sd-catalog-status').textContent === '4 live root products', 'catalog count reports only root products');
    available.dispatch('click', { target: { disabled: false, getAttribute: name => name === 'data-add-product' ? '8' : null } });
    assert(ids(roster).length === 0, 'the roster handler rejects an option ID even if an add event is constructed directly');
    assert(text(available).includes('Coins: 1500') && text(available).includes('Stock: 12/20') &&
        text(available).includes('Disabled') && text(available).includes('Type: role'),
        'metadata is displayed live and read-only');
    assert(filter.children.map(option => option.value).join(',') === ',11,12,none',
        'filter has All, each existing category, and Uncategorized');

    filter.value = '11';
    filter.dispatch('change');
    assert(available.children.length === 1 &&
        Number(available.children[0].querySelector('[data-add-product]').getAttribute('data-add-product')) === 7,
        'category filter shows only linked products');
    filter.value = 'none';
    filter.dispatch('change');
    assert(available.children.length === 2,
        'uncategorized filter includes null and stale category links');

    filter.value = '';
    filter.dispatch('change');
    clickThrough(available, buttonWith(available, 'data-add-product', 2));
    clickThrough(available, buttonWith(available, 'data-add-product', 7));
    assert(ids(roster).join(',') === '2,7',
        'selection appends root product IDs in ordered roster order', ids(roster).join(','));
    assert(buttonWith(available, 'data-add-product', 2).disabled,
        'already-rostered products are visibly disabled to prevent duplicates');
    filter.value = '11';
    filter.dispatch('change');
    assert(ids(roster).join(',') === '2,7', 'category filtering is view-only and does not change the roster');
    filter.value = '';
    filter.dispatch('change');
    clickThrough(roster, buttonWith(roster.children[1], 'data-roster-action', 'up'));
    assert(ids(roster).join(',') === '7,2', 'Move up reorders only the product ID roster');
    clickThrough(roster, buttonWith(roster.children[0], 'data-roster-action', 'down'));
    assert(ids(roster).join(',') === '2,7', 'Move down restores the ordered roster');
    clickThrough(roster, buttonWith(roster.children[0], 'data-roster-action', 'remove'));
    assert(ids(roster).join(',') === '7', 'Remove deletes the selected ID from the roster');
    clickThrough(available, buttonWith(available, 'data-add-product', 7));
    assert(ids(roster).join(',') === '7', 'duplicate add cannot create a duplicate product ID');
    assert(root.querySelector('#sd-roster-count').textContent === '1 product',
        'roster count reflects its current IDs');

    // Row selection is display-only: clicking a row selects it for the context
    // inspector without touching the roster, draft status, or any request.
    const rowName = list => list.children[0].children[0].children[0].children[0];
    const hasClass = (node, name) => node.className.split(/\s+/).indexOf(name) !== -1;
    const selectedCount = list => list.children.filter(node => hasClass(node, 'sd-selected')).length;
    const inspectorText = () => text(root.querySelector('#sd-context-inspector'));
    const dirtyBefore = root.querySelector('#sd-dirty').hidden;
    const previewStatusBefore = root.querySelector('#sd-preview-status').textContent;
    const rosterBeforeSelect = ids(roster).join(',');
    available.dispatch('click', { target: rowName(available) });
    const pickedId = Number(available.children[0].querySelector('[data-add-product]').getAttribute('data-add-product'));
    assert(hasClass(available.children[0], 'sd-selected') && available.children[0].getAttribute('aria-current') === 'true' && selectedCount(available) === 1,
        'clicking a catalog row selects exactly that row', selectedCount(available));
    assert(inspectorText().includes('Product ID: ' + pickedId), 'the context inspector shows the selected product', inspectorText());
    roster.dispatch('click', { target: rowName(roster) });
    const rosterPickedId = Number(roster.children[0].getAttribute('data-roster-id'));
    assert(hasClass(roster.children[0], 'sd-selected') && selectedCount(roster) === 1 && selectedCount(available) === (
        available.children.some(node => Number(node.getAttribute('data-available-id')) === rosterPickedId) ? 1 : 0),
        'clicking a roster row moves the selection and highlights the same product in both lists');
    assert(inspectorText().includes('Product ID: ' + rosterPickedId), 'the inspector follows the newly selected roster row');
    assert(ids(roster).join(',') === rosterBeforeSelect && root.querySelector('#sd-dirty').hidden === dirtyBefore &&
        root.querySelector('#sd-preview-status').textContent === previewStatusBefore,
        'row selection never changes the roster, dirty state, or preview status');
    const actionKindForSelection = root.querySelector('#sd-action-kind');
    actionKindForSelection.value = 'product_select'; actionKindForSelection.dispatch('change');
    assert(selectedCount(available) === 0 && selectedCount(roster) === 0, 'a non-product inspector context clears the row highlight');
    actionKindForSelection.value = 'buttons'; actionKindForSelection.dispatch('change');

    const forbiddenRequests = calls.filter(call =>
        call.method !== 'GET' || call.url === '/api/shop-publisher/preview' || /products\/category/.test(call.url));
    assert(forbiddenRequests.length === 0,
        'Region A makes no persistence writes, preview requests, or category mutations');
    assert(root.querySelector('#sd-action') !== null && root.querySelector('#sd-action-validate') !== null &&
        root.querySelector('#sd-preview') !== null,
        'Region C action controls remain separate from the existing complete-design preview renderer');
    assert(root.querySelector('#sd-presentation') !== null && root.querySelector('#sd-mode') !== null,
        'Region B presentation controls are separate from the Region A roster');

    console.log('\n' + pass + '/' + (pass + fail) + ' checks passed.');
    if (fail) process.exitCode = 1;
}

main().catch(error => { console.error(error); process.exitCode = 1; });
