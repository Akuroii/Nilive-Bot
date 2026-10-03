#!/usr/bin/env node
'use strict';
// Complete-design preview integration: real V2 message renderer and the exact
// Shop Publisher action/warning/token helpers, driven by the in-memory draft.
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
async function tick() { await new Promise(resolve => setTimeout(resolve, 0)); }

const rootDir = path.join(__dirname, '..');
const js = (...parts) => path.join(rootDir, 'dashboard', 'static', 'js', ...parts);
const products = [
    { id: 7, name: 'Zulu', type: 'title', price: 10, price_diamonds: null, enabled: 1, current_stock: null, max_stock: null, prestige_tier: null, featured: 0, category_id: null, option_of_id: null },
    { id: 5, name: 'Alpha', type: 'custom', price: 20, price_diamonds: null, enabled: 1, current_stock: null, max_stock: null, prestige_tier: null, featured: 0, category_id: null, option_of_id: null },
    { id: 8, name: 'Zulu — 7 Days', type: 'custom', price: 5, price_diamonds: null, enabled: 1, current_stock: null, max_stock: null, prestige_tier: null, featured: 0, category_id: null, option_of_id: 7 },
    { id: 10, name: 'Zulu — 30 Days', type: 'custom', price: 15, price_diamonds: null, enabled: 1, current_stock: null, max_stock: null, prestige_tier: null, featured: 0, category_id: null, option_of_id: 7 },
    { id: 11, name: 'Other family option', type: 'custom', price: 8, price_diamonds: null, enabled: 1, current_stock: null, max_stock: null, prestige_tier: null, featured: 0, category_id: null, option_of_id: 5 },
    { id: 12, name: 'Chained option', type: 'custom', price: 2, price_diamonds: null, enabled: 1, current_stock: null, max_stock: null, prestige_tier: null, featured: 0, category_id: null, option_of_id: 8 },
];
const templateDoc = { content: 'Welcome {{product.name}}', embeds: [{ title: 'Card {{product.name}}', description: 'Body text' }] };
function previewResponse(content, title, label) {
    return {
        success: true,
        preview: {
            mode: 'per_product', content,
            embeds: [{ title, description: 'Resolved body' }],
            action: { kind: 'buttons', style: 'green', entries: [{ product_id: 7, custom_id: 'shop_buy_7', label, emoji: '✨', style: 'green', free: false }] },
            warnings: [{ code: 'unknown_token', path: 'content', message: 'An unknown token remains.' }],
            tokens: { values: {}, used: [{ token: '{{product.name}}', path: 'content', resolved: true, value: 'Zulu' }] },
        },
    };
}
function productSelectResponse() {
    return {
        success: true,
        preview: {
            mode: 'per_product', content: 'Select a product', embeds: [],
            action: { kind: 'product_select', component_custom_id: 'shop_buy_sel_42',
                placeholder: 'Pick one', entries: [
                    { product_id: 7, custom_id: 'shop_buy_7', label: 'Zulu choice', description: 'Choose Zulu', emoji: '✨' },
                    { product_id: 5, custom_id: 'shop_buy_5', label: 'Alpha choice', description: 'Choose Alpha', emoji: '⚔️' },
                ] },
            warnings: [], tokens: { values: {}, used: [] },
        },
    };
}
function optionSelectResponse() {
    return {
        success: true,
        preview: {
            mode: 'per_product', content: 'Choose a duration', embeds: [],
            action: { kind: 'option_select', component_custom_id: 'shop_buy_sel_42',
                placeholder: 'Choose duration', entries: [
                    { product_id: 8, custom_id: 'shop_buy_8', label: '7 Days', description: 'One week', emoji: '⏳' },
                    { product_id: 10, custom_id: 'shop_buy_10', label: '30 Days', description: 'One month', emoji: '📅' },
                ] },
            warnings: [], tokens: { values: {}, used: [] },
        },
    };
}

async function main() {
    const dom = createDom();
    const pages = {};
    const window = { NERO: { definePage(name, module) { pages[name] = module; } }, __BOT_IDENTITY__: { name: 'Test Bot', avatar: null } };
    const sandbox = { window, document: dom.document, console, setTimeout, clearTimeout, Promise, Object, Array, Math, Date, JSON, Number, String, RegExp, Error, TypeError, Set, Map, Symbol, isFinite, parseInt };
    vm.createContext(sandbox);
    [
        js('embed/model.js'), js('embed/assets.js'), js('embed/store.js'), js('embed/validate.js'),
        js('embed/discord-markdown.js'), js('embed/preview.js'), js('embed/views/rail.js'),
        js('embed/views/inspector.js'), js('shop-publisher.js'), js('shop-designer.js'),
    ].forEach(file => vm.runInContext(fs.readFileSync(file, 'utf8'), sandbox, { filename: path.basename(file) }));
    const shared = window.NERO.shopPublisherPreview;
    let purchaseHelperCalls = 0;
    const originalPurchase = shared.renderPurchase;
    shared.renderPurchase = function () { purchaseHelperCalls += 1; return originalPurchase.apply(this, arguments); };

    const tree = parseTemplate(fs.readFileSync(path.join(rootDir, 'dashboard/templates/manage/shopdesigner.html'), 'utf8'));
    const root = materialize(findById(tree, 'sd-root'), dom.document);
    dom.attach(root);
    const calls = [];
    const pendingPreviews = [];
    const ctx = {
        on(target, type, handler) { target.addEventListener(type, handler); },
        fetchJSON(url, options) {
            options = options || {};
            const method = options.method || 'GET';
            const body = options.body ? JSON.parse(options.body) : null;
            calls.push({ url, method, body });
            if (url === '/api/shop-publisher/designs') return Promise.resolve({ success: true, designs: [] });
            if (url === '/api/shop-publisher/catalog') return Promise.resolve({ products, templates: ['welcome'] });
            if (url === '/api/shop-publisher/categories') return Promise.resolve({ categories: [] });
            if (url === '/api/shop-publisher/products/7/options') return Promise.resolve({
                success: true, root: products[0],
                options: [products[0], products.find(p => p.id === 8), products.find(p => p.id === 10), products.find(p => p.id === 11), products.find(p => p.id === 12)],
            });
            if (url === '/api/embedbuilder/template/welcome') return Promise.resolve({ template: templateDoc });
            if (url === '/api/shop-publisher/preview' && method === 'POST') {
                return new Promise(resolve => pendingPreviews.push({ body, resolve }));
            }
            return Promise.reject(Error('unexpected request ' + method + ' ' + url));
        },
        isDestroyed() { return false; },
    };
    pages['shop-designer'].init(root, ctx);
    await tick();

    root.querySelector('#sd-template').value = 'welcome';
    root.querySelector('#sd-template').dispatch('change');
    await tick();
    const available = root.querySelector('#sd-available');
    const addProduct = available.querySelectorAll('button').find(button => button.getAttribute('data-add-product') === '7');
    available.dispatch('click', { target: addProduct });
    const mapping = root.querySelector('#sd-action-product'); mapping.value = '7';
    root.querySelector('#sd-action-add').dispatch('click');

    root.querySelector('#sd-action-validate').dispatch('click');
    assert(pendingPreviews.length === 1, 'explicit validation requests the existing complete-design preview once');
    assert(root.querySelector('#sd-preview-status').textContent.includes('Loading'), 'preview loading state is visible');
    assert(pendingPreviews[0].body.products.join(',') === '7' && pendingPreviews[0].body.presentation.content === templateDoc.content && pendingPreviews[0].body.action.kind === 'buttons', 'request carries products[], presentation, and Buttons action from current in-memory state');

    const labelInput = root.querySelector('#sd-action-entries').querySelectorAll('input').find(input => input.getAttribute('data-action-field') === 'label');
    labelInput.value = 'New button label';
    root.querySelector('#sd-action-entries').dispatch('input', { target: labelInput });
    assert(root.querySelector('#sd-preview-status').textContent.includes('Draft changed'), 'editing while a response is pending invalidates and clears that preview');
    root.querySelector('#sd-action-validate').dispatch('click');
    assert(pendingPreviews.length === 2 && pendingPreviews[1].body.action.entries[0].label === 'New button label', 'a newer preview request reflects the newer action state');

    pendingPreviews[1].resolve(previewResponse('Resolved welcome Zulu', 'Resolved Card Zulu', 'New button label'));
    await tick(); await tick();
    const messageText = deepText(root.querySelector('#sd-preview-mount'));
    assert(messageText.includes('Resolved welcome Zulu') && messageText.includes('Resolved Card Zulu') && messageText.includes('Resolved body'), 'existing V2 renderer displays the complete returned message content and embed');
    const purchaseText = deepText(root.querySelector('#sd-preview-purchase'));
    assert(purchaseHelperCalls > 0 && purchaseText.includes('New button label') && purchaseText.includes('shop_buy_7'), 'purchase action is rendered by the reused Shop Publisher helper');
    assert(root.querySelector('#sd-preview-warnings').querySelector('[data-code="unknown_token"]') !== null && deepText(root.querySelector('#sd-preview-warnings')).includes('An unknown token remains.'), 'existing warning helper renders returned warning code and text');
    assert(deepText(root.querySelector('#sd-preview-tokens')).includes('{{product.name}}') && deepText(root.querySelector('#sd-preview-tokens')).includes('Zulu'), 'existing token helper renders returned token resolution');
    assert(root.querySelector('#sd-preview-status').textContent.includes('Preview ready'), 'successful complete-design preview status is visible');

    // Complete the older response after the newer one; it must not replace any
    // of the message/action/warning/token output from the latest draft.
    pendingPreviews[0].resolve(previewResponse('STALE MESSAGE', 'STALE CARD', 'Stale button'));
    await tick(); await tick();
    assert(!deepText(root.querySelector('#sd-preview-mount')).includes('STALE MESSAGE') && deepText(root.querySelector('#sd-preview-purchase')).includes('New button label'), 'a stale response cannot overwrite newer message or purchase-action output');

    const allowed = ['/api/shop-publisher/designs', '/api/shop-publisher/catalog', '/api/shop-publisher/categories', '/api/shop-publisher/products/7/options', '/api/embedbuilder/template/welcome', '/api/shop-publisher/preview'];
    const unexpected = calls.filter(call => allowed.indexOf(call.url) === -1 || (call.url === '/api/shop-publisher/preview' && call.method !== 'POST'));
    assert(unexpected.length === 0, 'only existing read routes and the existing preview POST are called', JSON.stringify(unexpected));
    assert(calls.filter(call => call.method === 'POST').every(call => call.url === '/api/shop-publisher/preview'), 'the only POST is the read-only complete-design preview; no persistence, purchase, or publication call occurs');

    // Product Select configuration goes through the same complete Preview and
    // existing static select renderer; only roots may enter the roster.
    const optionAdd = root.querySelector('#sd-available').querySelectorAll('button').find(button => button.getAttribute('data-add-product') === '8');
    assert(!optionAdd && root.querySelector('#sd-roster').children.every(item => item.getAttribute('data-roster-id') !== '8'), 'option rows cannot be added to the Design roster');
    const alphaAdd = root.querySelector('#sd-available').querySelectorAll('button').find(button => button.getAttribute('data-add-product') === '5');
    root.querySelector('#sd-available').dispatch('click', { target: alphaAdd });
    const kindSelect = root.querySelector('#sd-action-kind');
    kindSelect.value = 'product_select'; kindSelect.dispatch('change');
    assert(root.querySelector('#sd-action-product').children.map(option => option.value).join(',') === ',5', 'Product Select choices remain roots from the root-only roster');
    root.querySelector('#sd-action-product').value = '5';
    root.querySelector('#sd-action-add').dispatch('click');
    const productSelectEntries = root.querySelector('#sd-action-entries');
    const alphaEntry = productSelectEntries.querySelectorAll('[data-action-entry]').find(entry => entry.getAttribute('data-action-product-id') === '5');
    const alphaInputs = alphaEntry.querySelectorAll('input');
    const alphaLabel = alphaInputs.find(input => input.getAttribute('data-action-field') === 'label');
    const alphaDescription = alphaInputs.find(input => input.getAttribute('data-action-field') === 'description');
    const alphaEmoji = alphaInputs.find(input => input.getAttribute('data-action-field') === 'emoji');
    alphaLabel.value = 'Alpha choice'; productSelectEntries.dispatch('input', { target: alphaLabel });
    alphaDescription.value = 'Choose Alpha'; productSelectEntries.dispatch('input', { target: alphaDescription });
    alphaEmoji.value = '⚔️'; productSelectEntries.dispatch('input', { target: alphaEmoji });
    root.querySelector('#sd-action-placeholder').value = 'Pick one';
    root.querySelector('#sd-action-placeholder').dispatch('input');
    root.querySelector('#sd-action-validate').dispatch('click');
    assert(pendingPreviews.length === 3, 'Product Select validation makes one request to the existing Preview endpoint');
    const selectBody = pendingPreviews[2].body;
    assert(selectBody.action.kind === 'product_select' && selectBody.action.entries.map(entry => entry.product_id).join(',') === '7,5', 'Product Select Preview request carries ordered root IDs');
    assert(selectBody.action.placeholder === 'Pick one' && selectBody.action.entries[1].label === 'Alpha choice' && selectBody.action.entries[1].description === 'Choose Alpha' && selectBody.action.entries[1].emoji === '⚔️', 'Product Select Preview request carries placeholder and entry presentation overrides');
    pendingPreviews[2].resolve(productSelectResponse());
    await tick(); await tick();
    const selectPreviewText = deepText(root.querySelector('#sd-preview-purchase'));
    assert(selectPreviewText.includes('Pick one') && selectPreviewText.includes('shop_buy_sel_42') && selectPreviewText.includes('shop_buy_7') && selectPreviewText.includes('shop_buy_5'), 'existing Preview renderer displays Product Select placeholder, component ID, and selected-root values', selectPreviewText);
    assert(selectPreviewText.includes('Zulu choice') && selectPreviewText.includes('Alpha choice') && selectPreviewText.includes('Choose Alpha') && selectPreviewText.includes('⚔️'), 'existing Preview renderer displays Product Select labels, descriptions, and emoji');

    const optionKindSelect = root.querySelector('#sd-action-kind');
    optionKindSelect.value = 'option_select'; optionKindSelect.dispatch('change');
    const rootSelect = root.querySelector('#sd-action-option-root');
    assert(rootSelect.children.map(option => option.value).join(',') === ',7,5', 'Option Select roots are anchored only to root products in the roster');
    rootSelect.value = '7'; rootSelect.dispatch('change');
    await tick();
    assert(root.querySelector('#sd-action-product').children.map(option => option.value).join(',') === ',8,10', 'Option Select excludes the root, cross-family option, and chained option');
    root.querySelector('#sd-action-product').value = '8'; root.querySelector('#sd-action-add').dispatch('click');
    root.querySelector('#sd-action-product').value = '10'; root.querySelector('#sd-action-add').dispatch('click');
    const optionEntries = root.querySelector('#sd-action-entries');
    const optionBody = optionEntries.children.map(entry => entry.getAttribute('data-action-product-id')).join(',');
    assert(optionBody === '8,10', 'Option Select entries are direct options in their configured order');
    root.querySelector('#sd-action-placeholder').value = 'Choose duration';
    root.querySelector('#sd-action-placeholder').dispatch('input');
    root.querySelector('#sd-action-validate').dispatch('click');
    assert(pendingPreviews.length === 4, 'Option Select validation uses the existing Preview endpoint');
    const optionRequest = pendingPreviews[3].body;
    assert(optionRequest.action.kind === 'option_select' && optionRequest.products.join(',') === '7,5' && optionRequest.action.entries.map(entry => entry.product_id).join(',') === '8,10', 'Option Select Preview request keeps the root roster separate from direct-option entries');
    pendingPreviews[3].resolve(optionSelectResponse());
    await tick(); await tick();
    const optionPreviewText = deepText(root.querySelector('#sd-preview-purchase'));
    assert(optionPreviewText.includes('Choose duration') && optionPreviewText.includes('shop_buy_sel_42') && optionPreviewText.includes('shop_buy_8') && optionPreviewText.includes('shop_buy_10') && optionPreviewText.includes('7 Days') && optionPreviewText.includes('One week') && optionPreviewText.includes('⏳') && optionPreviewText.includes('30 Days') && optionPreviewText.includes('One month') && optionPreviewText.includes('📅'), 'existing Preview renderer displays the Option Select descriptor, ordered values, labels, descriptions, and emoji', optionPreviewText);

    pages['shop-designer'].destroy();
    assert(root.querySelector('#sd-preview-mount').children.length === 0, 'V2 preview renderer is released on page teardown');
    console.log('\n' + pass + '/' + (pass + fail) + ' checks passed.');
    if (fail) process.exitCode = 1;
}
main().catch(error => { console.error(error); process.exitCode = 1; });
