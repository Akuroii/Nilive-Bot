#!/usr/bin/env node
'use strict';
// Token Catalog panel inside Shop Designer: real template + real page module, fed the REAL server catalog.
const fs = require('fs'), path = require('path'), vm = require('vm'), { execFileSync } = require('child_process');
const { createDom, parseTemplate, findById, materialize } = require('./support/dom_stub.js');
let pass = 0, fail = 0;
function assert(c, label, d) { if (c) { pass++; console.log('  PASS', label); } else { fail++; console.log('  FAIL', label, d || ''); } }
const rootDir = path.join(__dirname, '..');
const payload = JSON.parse(execFileSync('python3', ['-c',
    'import sys,json; sys.path.insert(0,"."); import utils.shop_publisher as SP; print(json.dumps(SP.token_catalog_payload(), ensure_ascii=False))'],
    {cwd: rootDir, encoding: 'utf8', env: Object.assign({}, process.env, {PYTHONPATH: rootDir})}));
const templateSrc = fs.readFileSync(path.join(rootDir, 'dashboard/templates/manage/shopdesigner.html'), 'utf8');
const tree = parseTemplate(templateSrc);
const modulePath = path.join(rootDir, 'dashboard/static/js/shop-designer.js');
const text = n => !n ? '' : (n._text || '') + (n.children || []).map(text).join(' ');

async function boot(catalogResponse, navigatorStub) {
    const dom = createDom(); let pageModule = null;
    const window = {NERO: {definePage: (n, m) => { pageModule = m; }}};
    if (navigatorStub) window.navigator = navigatorStub;
    const sandbox = {window, document: dom.document, console, Promise, Object, Array, Math, JSON, Number, String, RegExp, Error, parseInt, isFinite};
    vm.createContext(sandbox); vm.runInContext(fs.readFileSync(modulePath, 'utf8'), sandbox, {filename: modulePath});
    const root = materialize(findById(tree, 'sd-root'), dom.document); dom.attach(root);
    const routes = {'/api/shop-publisher/designs': {success: true, designs: []},
        '/api/shop-publisher/categories': {success: true, categories: [], uncategorized: 0}};
    const ctx = {on: (t, ty, h) => t.addEventListener(ty, h), isDestroyed: () => false,
        fetchJSON: url => url === '/api/shop-publisher/catalog'
            ? (catalogResponse instanceof Error ? Promise.reject(catalogResponse) : Promise.resolve(catalogResponse))
            : (routes[url] ? Promise.resolve(routes[url]) : Promise.reject(new Error('unexpected ' + url)))};
    pageModule.init(root, ctx); await new Promise(r => setTimeout(r, 0));
    return {root, list: root.querySelector('#sd-token-list'), search: root.querySelector('#sd-token-search'), status: root.querySelector('#sd-token-status')};
}
const entries = list => list.children.filter(c => /sd-token-entry/.test(c.className || ''));
const groups = list => list.children.filter(c => /sd-token-group-title/.test(c.className || '')).map(c => c.textContent);
const names = list => entries(list).map(e => text(e.children[0]).replace(/\s*Copy\s*$/, '').trim());

(async function () {
    console.log('A. template');
    assert(/id="sd-token-catalog-panel"/.test(templateSrc) && /<details[^>]*id="sd-token-catalog-panel"/.test(templateSrc), 'panel is a <details> inside the Designer template');
    assert(!/<details[^>]*sd-token-catalog-panel[^>]*\bopen\b/.test(templateSrc), 'panel is collapsed by default');
    assert(/&#123;&#123;product\.name&#125;&#125;/.test(templateSrc) && !/<code>\{\{product\.name\}\}<\/code>/.test(templateSrc),
        'usage example uses entities so Jinja cannot evaluate it');

    console.log('B. renders the real catalog exactly');
    const a = await boot({products: [], templates: [], tokens: payload});
    assert(payload.length >= 10, 'real catalog loaded from the server code', String(payload.length));
    assert(entries(a.list).length === payload.length, 'one entry per catalog token');
    assert(JSON.stringify(names(a.list)) === JSON.stringify(payload.map(t => t.token)), 'canonical token names and server order preserved');
    const firstSeen = []; payload.forEach(t => { if (firstSeen.indexOf(t.group) === -1) firstSeen.push(t.group); });
    assert(JSON.stringify(groups(a.list)) === JSON.stringify(firstSeen), 'groups appear in the server order: ' + firstSeen.join(', '));
    const bodyText = entries(a.list).map(e => text(e));
    assert(payload.every((t, i) => bodyText[i].indexOf(t.description) !== -1), 'every description is rendered verbatim (no rewording)');
    assert(payload.some(t => t.description.indexOf('1,500') !== -1) && bodyText.join('\n').indexOf('1,500') !== -1, 'grouped-price example "1,500" preserved');
    assert(payload.some(t => t.description.indexOf('𝐅𝐫𝐞𝐞') !== -1) && bodyText.join('\n').indexOf('𝐅𝐫𝐞𝐞') !== -1, 'the exact 𝐅𝐫𝐞𝐞 convention preserved');
    assert(payload.every((t, i) => !t.aliases.length || bodyText[i].indexOf('Also accepts: ' + t.aliases.join(', ')) !== -1), 'aliases shown for every token that has them');
    assert(a.status.textContent === payload.length + ' tokens', 'status reports the count', a.status.textContent);

    console.log('C. search');
    a.search.value = 'PRICE'; a.search.dispatch('input');
    const priced = names(a.list);
    assert(priced.length > 0 && priced.length < payload.length && priced.every(n => payload.find(t => t.token === n)), 'case-insensitive search narrows the list');
    assert(a.status.textContent === priced.length + ' of ' + payload.length + ' tokens', 'status shows N of M', a.status.textContent);
    const aliasHit = payload.find(t => t.aliases.length);
    a.search.value = aliasHit.aliases[0]; a.search.dispatch('input');
    assert(names(a.list).indexOf(aliasHit.token) !== -1, 'searching an alias finds its canonical token');
    a.search.value = 'zzzz-no-such-token'; a.search.dispatch('input');
    assert(entries(a.list).length === 0 && /No tokens match/.test(a.status.textContent), 'no match shows a clear message');
    a.search.value = ''; a.search.dispatch('input');
    assert(entries(a.list).length === payload.length, 'clearing the search restores everything');

    console.log('D. copy');
    const copied = []; const clip = {writeText: t => { copied.push(t); return Promise.resolve(); }};
    const b = await boot({products: [], templates: [], tokens: payload}, {clipboard: clip});
    entries(b.list)[0].querySelectorAll('button')[0].click(); await new Promise(r => setTimeout(r, 0));
    assert(copied.length === 1 && copied[0] === payload[0].token, 'Copy writes the exact canonical token', JSON.stringify(copied));
    assert(b.status.textContent === 'Copied ' + payload[0].token + '.', 'status confirms the copy');
    const c = await boot({products: [], templates: [], tokens: payload}, {clipboard: {writeText: () => Promise.reject(new Error('denied'))}});
    entries(c.list)[0].querySelectorAll('button')[0].click(); await new Promise(r => setTimeout(r, 0));
    assert(/Copy is unavailable/.test(c.status.textContent), 'a denied clipboard is reported honestly, not as success');
    const d0 = await boot({products: [], templates: [], tokens: payload});
    entries(d0.list)[0].querySelectorAll('button')[0].click(); await new Promise(r => setTimeout(r, 0));
    assert(/Copy is unavailable/.test(d0.status.textContent), 'no clipboard API is reported honestly');

    console.log('E. robustness');
    const e = await boot(new Error('network'));
    assert(entries(e.list).length === 0 && e.status.textContent === 'Token catalog unavailable.', 'a failed catalog load shows "unavailable" and does not throw');
    const f = await boot({products: [], templates: [], tokens: 'nonsense'});
    assert(entries(f.list).length === 0 && f.status.textContent === 'Token catalog unavailable.', 'a malformed tokens field is handled safely');
    const g = await boot({products: [], templates: [], tokens: ['ignored', null, 7, {token: 42}, {token: '{{ok}}', label: 'OK', group: 'G', description: '<img src=x onerror=alert(1)>', aliases: 'bad'}]});
    assert(entries(g.list).length === 1 && names(g.list)[0] === '{{ok}}', 'malformed entries are skipped, valid ones kept');
    assert(text(entries(g.list)[0]).indexOf('<img src=x onerror=alert(1)>') !== -1 && !g.list.querySelectorAll('img').length, 'descriptions are inserted as text, never as HTML');
    console.log('\n' + pass + '/' + (pass + fail) + ' checks passed.'); process.exit(fail ? 1 : 0);
})().catch(err => { console.error(err); process.exit(1); });
