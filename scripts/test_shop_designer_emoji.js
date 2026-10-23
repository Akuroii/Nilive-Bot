#!/usr/bin/env node
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { createDom, parseTemplate, findById, materialize } = require('./support/dom_stub.js');
let pass = 0, fail = 0;
function assert(ok, label, detail) { if (ok) { pass++; console.log('  PASS', label); } else { fail++; console.log('  FAIL', label, detail || ''); } }
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const rootDir = path.join(__dirname, '..');
const products = [
    { id: 2, name: 'Alpha', type: 'role', price: 10, enabled: 1, category_id: null, option_of_id: null, app_emoji: '' },
    { id: 7, name: 'Beta', type: 'item', price: 20, enabled: 1, category_id: null, option_of_id: null, app_emoji: '' },
];

async function main() {
    const dom = createDom(); let page = null; const calls = [];
    const replies = { prepare: [], reconcile: [], delete: [] };
    const window = {
        NERO: { definePage(name, module) { page = module; } },
        confirm: () => true,
        prompt: () => 'wrong',
    };
    const sandbox = { window, document: dom.document, console, setTimeout, clearTimeout,
        Promise, Object, Array, Math, JSON, Number, String, RegExp, Error, parseInt, isFinite };
    vm.createContext(sandbox);
    const modulePath = path.join(rootDir, 'dashboard/static/js/shop-designer.js');
    vm.runInContext(fs.readFileSync(modulePath, 'utf8'), sandbox, { filename: modulePath });
    const tree = parseTemplate(fs.readFileSync(path.join(rootDir, 'dashboard/templates/manage/shopdesigner.html'), 'utf8'));
    const root = materialize(findById(tree, 'sd-root'), dom.document); dom.attach(root);
    function response(body, status = 200) {
        return { ok: status >= 200 && status < 300, status, json: () => Promise.resolve(body) };
    }
    const ctx = {
        on(target, type, handler) { target.addEventListener(type, handler); },
        isDestroyed: () => false,
        fetchJSON(url) {
            calls.push({ url, method: 'GET' });
            if (url === '/api/shop-publisher/designs') return Promise.resolve({ success: true, designs: [] });
            if (url === '/api/shop-publisher/catalog') return Promise.resolve({ products, templates: [], tokens: [] });
            if (url === '/api/shop-publisher/categories') return Promise.resolve({ categories: [] });
            return Promise.reject(new Error('unexpected GET ' + url));
        },
        fetch(url, init) {
            const body = JSON.parse(init.body || '{}'); calls.push({ url, method: init.method || 'GET', body });
            if (url.endsWith('/emojis/prepare')) return Promise.resolve(response(replies.prepare.shift()));
            if (url.endsWith('/emojis/reconcile')) return Promise.resolve(response(replies.reconcile.shift()));
            if (url.endsWith('/emojis/delete-duplicate')) return Promise.resolve(response(replies.delete.shift()));
            return Promise.resolve(response({ success: false, error: 'unexpected ' + url }, 500));
        },
    };
    page.init(root, ctx); await tick();
    assert(calls.filter(call => call.url.includes('/emojis/')).length === 0,
        'startup and roster loading never prepare, reconcile, or delete emojis');

    const available = root.querySelector('#sd-available');
    const add = available.querySelector('[data-add-product="2"]');
    available.dispatch('click', { target: add });
    const selected = root.querySelector('#sd-emoji-prepare-selected');
    assert(!selected.disabled, 'adding/selecting a roster product enables explicit selected preparation');
    replies.prepare.push({ success: true, results: [{ product_id: 2, status: 'ready', code: 'created',
        content_hash: 'a'.repeat(64), emoji_id: '5001', emoji: '<:shop_one:5001>', duplicates: [] }] });
    selected.dispatch('click', { target: selected }); await tick(); await tick();
    const prepare = calls.find(call => call.url.endsWith('/emojis/prepare'));
    assert(prepare && prepare.body.product_ids.join(',') === '2', 'selected preparation sends only the selected product ID');
    assert(prepare.body.retry_uncertain === false && prepare.body.acknowledge_duplicate_risk === false,
        'ordinary preparation does not acknowledge or retry uncertain creation');
    assert(!('url' in prepare.body) && !('icon_url' in prepare.body) && !('source_url' in prepare.body),
        'browser preparation sends no source URL target');
    assert(root.querySelector('#sd-roster').textContent.includes('Product image emoji prepared'),
        'ready result updates the roster preparation badge');

    root.querySelector('#sd-emoji-retry-uncertain').checked = true;
    replies.prepare.push({ success: true, results: [] });
    root.querySelector('#sd-emoji-prepare-all').dispatch('click', { target: root.querySelector('#sd-emoji-prepare-all') });
    await tick(); await tick();
    const rosterPrepare = calls.filter(call => call.url.endsWith('/emojis/prepare'))[1];
    assert(rosterPrepare.body.product_ids.join(',') === '2', 'roster preparation uses the current roster IDs');
    assert(rosterPrepare.body.retry_uncertain === true && rosterPrepare.body.acknowledge_duplicate_risk === true,
        'uncertain retry requires the owner checkbox and confirmation');

    replies.reconcile.push({ success: true, results: [{ product_id: 2, status: 'uncertain', code: 'create_uncertain',
        content_hash: 'a'.repeat(64), emoji_id: null, duplicates: ['5001', '5002'], message: 'Reconciliation required.' }] });
    root.querySelector('#sd-emoji-reconcile').dispatch('click', { target: root.querySelector('#sd-emoji-reconcile') });
    await tick(); await tick();
    const reconcile = calls.find(call => call.url.endsWith('/emojis/reconcile'));
    assert(reconcile && Object.keys(reconcile.body).join(',') === 'product_ids',
        'reconciliation sends product IDs only and no retry acknowledgement');
    const duplicate = root.querySelector('[data-duplicate-id="5002"]');
    assert(duplicate, 'a reported duplicate gets a specific, non-automatic deletion control');
    duplicate.dispatch('click', { target: duplicate }); await tick();
    assert(calls.filter(call => call.url.endsWith('/emojis/delete-duplicate')).length === 0,
        'an incorrect exact confirmation sends no deletion');

    window.prompt = () => 'DELETE 5002';
    replies.delete.push({ success: false, error: 'Duplicate deletion is uncertain; recheck Discord.' });
    duplicate.dispatch('click', { target: duplicate }); await tick(); await tick();
    assert(root.querySelector('#sd-emoji-status').textContent.includes('uncertain') &&
        calls.filter(call => call.url.endsWith('/emojis/reconcile')).length === 1,
        'an unconfirmed 2xx deletion envelope is not reported as successful');

    replies.delete.push({ success: true, deleted_emoji_id: '5002' });
    replies.reconcile.push({ success: true, results: [{ product_id: 2, status: 'ready', code: 'reconciled',
        content_hash: 'a'.repeat(64), emoji_id: '5001', emoji: '<:shop_one:5001>', duplicates: [] }] });
    duplicate.dispatch('click', { target: duplicate }); await tick(); await tick(); await tick();
    const deletion = calls.filter(call => call.url.endsWith('/emojis/delete-duplicate')).pop();
    assert(deletion && deletion.body.emoji_id === '5002' && deletion.body.confirmation === 'DELETE 5002',
        'only the exactly confirmed duplicate ID is sent for deletion');
    assert(deletion.body.content_hash === 'a'.repeat(64) && !('product_id' in deletion.body),
        'deletion targets the confirmed asset hash and emoji ID, not an arbitrary URL or product mutation');
    assert(calls.filter(call => call.url.endsWith('/emojis/reconcile')).length === 2,
        'successful duplicate deletion is followed by read-only reconciliation');
    assert(calls.filter(call => call.url.endsWith('/preview')).length === 0,
        'emoji operations never disguise themselves as preview requests');

    console.log('\n' + pass + '/' + (pass + fail) + ' checks passed.');
    if (fail) process.exitCode = 1;
}
main().catch(error => { console.error(error); process.exitCode = 1; });
