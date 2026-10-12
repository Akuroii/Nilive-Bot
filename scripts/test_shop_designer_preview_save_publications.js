#!/usr/bin/env node
'use strict';
// Regression tests for: (1) Preview failures showing the server's reason instead of "HTTP 400",
// (2) Save failures doing the same, (3) the Publications panel following Save/Delete selection
// changes made in code (select.value assignments fire no DOM event).
// Real template + real designer module + real publications module; only transport is mocked.
const fs = require('fs'), path = require('path'), vm = require('vm');
const { createDom, parseTemplate, findById, materialize } = require('./support/dom_stub.js');
let pass = 0, fail = 0;
function assert(ok, label, detail) { if (ok) { pass++; console.log('  PASS', label); } else { fail++; console.log('  FAIL', label, detail || ''); } }
const tick = () => new Promise(r => setTimeout(r, 0));
const rootDir = path.join(__dirname, '..');
const js = f => path.join(rootDir, 'dashboard/static/js', f);
const PRODUCTS = [{id: 7, name: 'Alpha', type: 'role', price: 1500, price_diamonds: null, enabled: 1, current_stock: 12, max_stock: 20,
    prestige_tier: null, featured: 0, category_id: null, option_of_id: null}];
const TEMPLATE = {content: 'Buy {{product.name}}', embeds: [{title: '{{product.name}}'}]};
const res = (status, body) => ({ok: status >= 200 && status < 300, status,
    json: () => body instanceof Error ? Promise.reject(body) : Promise.resolve(body)});

async function boot() {
    const dom = createDom(); const pages = {};
    const window = {NERO: {definePage(n, m) { pages[n] = m; }}, __BOT_IDENTITY__: {name: 'Bot', avatar: null}, __CSRF_TOKEN__: 'csrf'};
    const state = {preview: res(200, {success: true}), save: null, savedId: 41, publicationsCalls: [], designs: [], deleteReply: res(200, {success: true})};
    const sandbox = {window, document: dom.document, console, setTimeout, clearTimeout, Promise, Object, Array, Math, Date, JSON, Number, String, RegExp, Error, TypeError, Set, Map, Symbol, parseInt, isFinite};
    sandbox.fetch = (url, init) => {           // used by shop-publications.js
        init = init || {};
        const m = /\/designs\/(\d+)\/publications$/.exec(url);
        if (m && (init.method || 'GET') === 'GET') { state.publicationsCalls.push(m[1]); return Promise.resolve(res(200, {success: true,
            publications: [{id: 900 + Number(m[1]), channel_id: 555, status: 'published', message_id: 777}]})); }
        return Promise.reject(new Error('unexpected publications fetch ' + url));
    };
    window.fetch = sandbox.fetch;
    vm.createContext(sandbox);
    ['embed/model.js', 'embed/assets.js', 'embed/store.js', 'embed/validate.js', 'embed/discord-markdown.js', 'embed/preview.js',
     'embed/views/rail.js', 'embed/views/inspector.js', 'shop-publisher.js', 'shop-designer.js'].forEach(f => vm.runInContext(fs.readFileSync(js(f), 'utf8'), sandbox, {filename: f}));
    const tree = parseTemplate(fs.readFileSync(path.join(rootDir, 'dashboard/templates/manage/shopdesigner.html'), 'utf8'));
    const root = materialize(findById(tree, 'sd-root'), dom.document); dom.attach(root);
    const changeEvents = []; const select = root.querySelector('#sd-design-select');
    select.addEventListener('change', () => changeEvents.push(select.value));
    const ctx = {
        on(t, ty, h) { t.addEventListener(ty, h); }, isDestroyed: () => false,
        fetchJSON(url) {
            if (url === '/api/shop-publisher/designs') return Promise.resolve({success: true, designs: state.designs});
            if (url === '/api/shop-publisher/catalog') return Promise.resolve({products: PRODUCTS, templates: ['welcome'], tokens: []});
            if (url === '/api/shop-publisher/categories') return Promise.resolve({categories: []});
            if (url === '/api/embedbuilder/template/welcome') return Promise.resolve({template: TEMPLATE});
            return Promise.reject(new Error('unexpected ' + url));
        },
        fetch(url, init) {
            init = init || {}; const method = init.method || 'GET';
            if (url === '/api/shop-publisher/preview') { const r = state.preview; return r instanceof Error ? Promise.reject(r) : Promise.resolve(r); }
            if (url === '/api/shop-publisher/designs' && method === 'POST') {
                if (state.save) { const r = state.save; return r instanceof Error ? Promise.reject(r) : Promise.resolve(r); }
                const body = JSON.parse(init.body); const id = body.id == null ? state.savedId : Number(body.id);
                const row = {id, name: body.name, source_template_name: body.source_template_name || null, design: body.design};
                state.designs = state.designs.filter(d => d.id !== id).concat([row]);
                return Promise.resolve(res(200, {success: true, design: row}));
            }
            const del = /\/designs\/(\d+)$/.exec(url);
            if (del && method === 'DELETE') {
                if (state.deleteReply.ok) state.designs = state.designs.filter(d => d.id !== Number(del[1]));
                return Promise.resolve(state.deleteReply);
            }
            return this.fetchJSON(url, init).then(b => res(200, b));
        },
    };
    pages['shop-designer'].init(root, ctx);
    vm.runInContext(fs.readFileSync(js('shop-publications.js'), 'utf8'), sandbox, {filename: 'shop-publications.js'});
    await tick();
    root.querySelector('#sd-template').value = 'welcome'; root.querySelector('#sd-template').dispatch('change'); await tick();
    const add = root.querySelector('#sd-available').querySelectorAll('button').find(b => b.getAttribute('data-add-product') === '7');
    root.querySelector('#sd-available').dispatch('click', {target: add});
    root.querySelector('#sd-action-product').value = '7'; root.querySelector('#sd-action-add').dispatch('click');
    return {root, state, changeEvents, select, dom};
}
const q = (a, s) => a.root.querySelector(s);
const text = n => !n ? '' : (n._text || '') + (n.children || []).map(text).join(' ');
async function validate(a) { q(a, '#sd-action-validate').dispatch('click'); await tick(); await tick(); }
async function save(a, name) { q(a, '#sd-design-name').value = name; q(a, '#sd-design-name').dispatch('input'); q(a, '#sd-design-save').dispatch('click'); for (let i = 0; i < 6; i++) await tick(); }

(async function () {
    console.log('A. Preview shows the server\'s reason');
    let a = await boot();
    a.state.preview = res(400, {success: false, error: 'Preview request invalid.', problems: [{message: 'No product 7 in this guild.'}]});
    await validate(a);
    assert(q(a, '#sd-preview-status').textContent === 'No product 7 in this guild.', 'a 400 with problems[] shows the first problem, not "HTTP 400"', q(a, '#sd-preview-status').textContent);
    assert(q(a, '#sd-action-validation').textContent === 'No product 7 in this guild.', 'the action validation line shows it too');
    a.state.preview = res(400, {success: false, error: 'presentation.content must be text.', problems: []});
    await validate(a);
    assert(q(a, '#sd-preview-status').textContent === 'presentation.content must be text.', 'a 400 with only error shows the error text', q(a, '#sd-preview-status').textContent);
    a.state.preview = res(502, new Error('not json'));
    await validate(a);
    assert(q(a, '#sd-preview-status').textContent === 'HTTP 502', 'a non-JSON error body falls back to the status code', q(a, '#sd-preview-status').textContent);
    a.state.preview = res(200, new Error('proxy html'));
    await validate(a);
    assert(/unreadable response/.test(q(a, '#sd-preview-status').textContent) && !/ready/.test(q(a, '#sd-preview-status').textContent), 'a 200 with an unreadable body is an error, never "Preview ready"', q(a, '#sd-preview-status').textContent);
    a.state.preview = res(200, {success: false, error: 'Server said no.'});
    await validate(a);
    assert(q(a, '#sd-preview-status').textContent === 'Server said no.', 'success:false with 200 still shows its error');
    a.state.preview = new Error('network down');
    await validate(a);
    assert(q(a, '#sd-preview-status').textContent === 'network down', 'a transport failure shows its message and is not a preview');
    assert(/No preview yet/.test(q(a, '#sd-preview-warnings').textContent), 'every failed preview leaves no stale result behind');

    console.log('B. Save shows the server\'s reason');
    a = await boot();
    a.state.save = res(409, {success: false, error: 'A design named “X” already exists.'});
    await save(a, 'X');
    assert(q(a, '#sd-design-status').textContent === 'A design named “X” already exists.', 'a 409 shows the server reason, not "HTTP 409"', q(a, '#sd-design-status').textContent);
    assert(a.select.value === '' && a.changeEvents.length === 0, 'a failed save selects nothing and announces nothing');
    a.state.save = res(500, new Error('html'));
    await save(a, 'X');
    assert(q(a, '#sd-design-status').textContent === 'HTTP 500', 'an unreadable error body falls back to the status code');
    assert(!q(a, '#sd-design-save').disabled, 'Save is re-enabled after a failure');

    console.log('C. Save → Publications selection');
    a = await boot(); a.state.savedId = 41;
    await save(a, 'First');
    assert(a.select.value === '41', 'Save selects the saved design');
    assert(a.changeEvents.length === 1 && a.changeEvents[0] === '41', 'Save announces exactly one change, carrying the new design id', JSON.stringify(a.changeEvents));
    assert(a.state.publicationsCalls.join() === '41', 'the Publications panel loads exactly that design\'s publications', a.state.publicationsCalls.join());
    assert(q(a, '#sd-publication-list').children.length === 1 && /message 777/.test(text(q(a, '#sd-publication-list').children[0])), 'its records are listed');
    assert(q(a, '#sd-publication-publish').disabled === false, 'Publish becomes available once that design\'s publication list has loaded');
    await save(a, 'First edited');
    assert(a.changeEvents.length === 1 && a.state.publicationsCalls.length === 1, 'saving the same design again reloads nothing (selection did not change)', JSON.stringify(a.changeEvents));

    console.log('D. Delete clears the Publications context');
    a = await boot(); await save(a, 'To delete');
    const beforeDelete = a.changeEvents.length;
    q(a, '#sd-design-delete').dispatch('click'); await tick(); await tick();
    assert(q(a, '#sd-design-status').textContent === 'Design deleted.', 'delete succeeds', q(a, '#sd-design-status').textContent);
    assert(a.changeEvents.length === beforeDelete + 1 && a.changeEvents[a.changeEvents.length - 1] === '', 'deleting the selected design announces an empty selection');
    assert(q(a, '#sd-publication-list').children.length === 0, 'the deleted design\'s publication rows are gone from the panel');
    assert(q(a, '#sd-publication-publish').disabled === true, 'Publish is disabled with no design selected');
    a = await boot(); await save(a, 'Blocked');
    a.state.deleteReply = res(409, {success: false, code: 'design_has_publications', error: 'This Design has Publications. Unpublish them first.'});
    const n = a.changeEvents.length;
    q(a, '#sd-design-delete').dispatch('click'); await tick(); await tick();
    assert(a.select.value === '41' && a.changeEvents.length === n, 'a blocked delete keeps the selection and announces nothing');
    console.log('\n' + pass + '/' + (pass + fail) + ' checks passed.'); process.exit(fail ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
