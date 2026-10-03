#!/usr/bin/env node
'use strict';
// Region B in-memory contract: real Shop Designer page module + real V2 editor
// model/store/rail/inspector/validator. No renderer, persistence or write API.
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { createDom, parseTemplate, findById, materialize } = require('./support/dom_stub.js');

let pass = 0, fail = 0;
function assert(ok, label, detail) {
    if (ok) { pass += 1; console.log('  PASS', label); }
    else { fail += 1; console.log('  FAIL', label, detail || ''); }
}
const rootDir = path.join(__dirname, '..');
const js = (...parts) => path.join(rootDir, 'dashboard', 'static', 'js', ...parts);
const limits = {
    message: { content_max: 2000, embeds_max: 10, embed_total_chars_max: 6000, request_bytes_max: 26214400 },
    attachments: { count_max: 10, total_bytes_max: 26148864, file_bytes_advisory: 20971520, file_advisory_is_hard: false },
    embed: { title_max: 256, description_max: 4096, fields_max: 25, field_name_max: 256, field_value_max: 1024, footer_text_max: 2048, author_name_max: 256 },
    components: { rows_max: 5, buttons_per_row_max: 5, button_label_max: 80, button_url_max: 512, custom_id_max: 100, select_options_max: 25, select_option_label_max: 100, select_option_description_max: 100, select_placeholder_max: 150 },
};
const PRODUCTS = [{ id: 7, name: 'Zulu', type: 'title', price: 10, price_diamonds: null, enabled: 1, current_stock: null, max_stock: null, prestige_tier: null, featured: 0, category_id: null }];
const EMBED = { title: 'Welcome', description: 'Template copy', color: 12345, fields: [] };

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
    root.setAttribute('data-limits', JSON.stringify(limits));
    const calls = [];
    const routes = {
        '/api/shop-publisher/designs': { success: true, designs: [] },
        '/api/shop-publisher/catalog': { products: PRODUCTS, templates: ['sample'] },
        '/api/shop-publisher/categories': { categories: [] },
        '/api/embedbuilder/template/sample': { template: { content: 'Template text', embeds: [EMBED] } },
    };
    const ctx = {
        on(target, type, handler) { target.addEventListener(type, handler); },
        fetchJSON(url) { calls.push({ url, method: 'GET' }); return Object.prototype.hasOwnProperty.call(routes, url) ? Promise.resolve(routes[url]) : Promise.reject(Error('unexpected request ' + url)); },
        isDestroyed() { return false; },
    };
    page.init(root, ctx);
    await new Promise(resolve => setTimeout(resolve, 0));

    const mode = root.querySelector('#sd-mode');
    const templateSelect = root.querySelector('#sd-template');
    let content = root.querySelector('#sd-embed-inspector').querySelectorAll('textarea')[0] || null;
    assert(mode && mode.value === 'per_product' && mode.children.map(x => x.getAttribute('value')).join(',') === 'per_product,frame', 'mode selector exposes exactly the two Shop modes with per_product default');
    assert(root.getAttribute('data-limits') === JSON.stringify(limits), 'existing server limits payload is present for V2 validation');
    assert(templateSelect.children.some(option => option.value === 'sample'), 'template names are populated from the existing catalog response');
    const addProduct = root.querySelector('#sd-available').querySelectorAll('button').find(button => button.getAttribute('data-add-product') === '7');
    if (addProduct) root.querySelector('#sd-available').dispatch('click', { target: addProduct });
    assert(root.querySelector('#sd-roster').children.length === 1 && root.querySelector('#sd-roster').children[0].getAttribute('data-roster-id') === '7', 'Region A roster starts with its own root-product ID');

    const v2model = sandbox.window.NERO.embed.model;
    const imported = v2model.normalizeDocument(v2model.fromApiDocument('Round trip', [EMBED], {}));
    const wire = v2model.toDiscordPayload(imported);
    assert(wire.content === 'Round trip' && wire.embeds.length === 1 && wire.embeds[0].title === 'Welcome', 'V2 import, normalization, and wire conversion retain presentation data');

    templateSelect.value = 'sample'; templateSelect.dispatch('change');
    await new Promise(resolve => setTimeout(resolve, 0));
    assert(calls.some(call => call.url === '/api/embedbuilder/template/sample' && call.method === 'GET'), 'selecting a template uses only the existing template GET');
    assert(content && content.value === 'Template text', 'template content is normalized into the existing V2 inspector');
    const embedRow = root.querySelector('#sd-embed-rail').querySelectorAll('[data-rail-row]').find(row => row.getAttribute('data-rail-row') === 'embed');
    if (embedRow) root.querySelector('#sd-embed-rail').dispatch('click', { target: embedRow });
    const titleInput = root.querySelector('#sd-embed-inspector').querySelectorAll('input')[0] || null;
    assert(titleInput !== null, 'V2 inspector is mounted for template embed editing');
    if (titleInput) { titleInput.value = 'Edited title'; titleInput.dispatch('input'); }
    assert(root.querySelector('#sd-validation').textContent.includes('valid') || root.querySelector('#sd-validation').textContent.includes('issue'), 'V2 validator reports against the served limits');
    const contentRow = root.querySelector('#sd-embed-rail').querySelectorAll('[data-rail-row]').find(row => row.getAttribute('data-rail-row') === 'content');
    if (contentRow) root.querySelector('#sd-embed-rail').dispatch('click', { target: contentRow });
    content = root.querySelector('#sd-embed-inspector').querySelectorAll('textarea')[0] || null;
    assert(content !== null, 'existing V2 inspector owns message-content editing');
    if (content) { content.value = 'Edited message'; content.dispatch('input'); }
    mode.value = 'frame'; mode.dispatch('change');
    assert(content.value === 'Edited message' && mode.value === 'frame', 'content and mode controls accept in-memory edits');
    const railButtons = root.querySelectorAll('#sd-embed-rail button');
    const addEmbed = railButtons.find(button => button.getAttribute('data-rail-action') === 'addEmbed');
    if (addEmbed) root.querySelector('#sd-embed-rail').dispatch('click', { target: addEmbed });
    assert(root.querySelector('#sd-embed-rail').querySelectorAll('[data-rail-row]').some(row => row.getAttribute('data-rail-row') === 'embed'), 'existing V2 rail controls the embed list');

    const allowedReads = ['/api/shop-publisher/designs', '/api/shop-publisher/catalog', '/api/shop-publisher/categories', '/api/embedbuilder/template/sample'];
    const forbidden = calls.filter(call => call.method !== 'GET' || allowedReads.indexOf(call.url) === -1);
    assert(forbidden.length === 0, 'only catalog/category/template GETs occur; no persistence, preview, commerce write, or publication request is issued', JSON.stringify(forbidden));
    assert(root.querySelector('#sd-action') !== null && root.querySelector('#sd-preview') !== null, 'Region C action state remains separate from the existing complete-design preview renderer');
    // Product roster remains its own control surface and is not a descendant
    // of either editor mount. Presentation-only interactions do not address it.
    assert(root.querySelector('#sd-embed-inspector').querySelector('#sd-roster') === null && root.querySelector('#sd-roster') !== null, 'presentation editor cannot contain or address products[] roster controls');
    assert(root.querySelector('#sd-roster').children.length === 1 && root.querySelector('#sd-roster').children[0].getAttribute('data-roster-id') === '7', 'presentation template/content/embed/mode edits leave Region A products[] unchanged');

    // Static wiring: the shared V2 rail/inspector are styled by the existing
    // message-builder.css (loaded here, never copied), and nothing the page emits is unstyled.
    const templateSource = fs.readFileSync(path.join(rootDir, 'dashboard/templates/manage/shopdesigner.html'), 'utf8');
    const designerCss = fs.readFileSync(path.join(rootDir, 'dashboard/static/css/shop-designer.css'), 'utf8');
    const publisherCss = fs.readFileSync(path.join(rootDir, 'dashboard/static/css/shop-publisher.css'), 'utf8');
    const designerJs = fs.readFileSync(path.join(rootDir, 'dashboard/static/js/shop-designer.js'), 'utf8');
    const baseSource = fs.readFileSync(path.join(rootDir, 'dashboard/templates/base.html'), 'utf8');
    const mbLink = templateSource.indexOf('css/message-builder.css');
    assert(mbLink !== -1 && mbLink < templateSource.indexOf('css/shop-designer.css'), 'the Designer loads the existing message-builder.css before its own stylesheet');
    assert(baseSource.indexOf('message-builder.css') === -1, 'message-builder.css is still loaded from the page, not globally from base.html');
    assert(/\.sd-shell\s*\{[^}]*--mb2-radius:\s*12px/.test(designerCss), 'the Designer shell provides the --mb2-radius value the shared styles read');
    assert(!/\.mb2-insp-/.test(designerCss) && (designerCss.match(/\.mb2-rail-[a-z-]+/g) || []).join(',') === '.mb2-rail-btn', 'message-builder.css rules are not duplicated (only the disabled-cursor override references the rail)');
    const emitted = new Set();
    designerJs.replace(/make\(doc,\s*'[a-z0-9]+',\s*'([^']*)'/g, (_m, names) => { names.split(/\s+/).forEach(name => { if (/^(sd|sp)-/.test(name)) emitted.add(name); }); return _m; });
    const unstyled = Array.from(emitted).filter(name => !new RegExp('\\.' + name + '(?![a-z0-9_-])').test(designerCss + publisherCss));
    assert(emitted.size > 0 && unstyled.length === 0, 'every sd-/sp- class the Designer emits has a stylesheet rule', unstyled.join(','));
    assert(/\.sd-context,\s*\.sd-inspector-panel\s*\{[^}]*position:\s*sticky[^}]*max-height:[^}]*overflow-y:\s*auto/.test(designerCss), 'sticky side panels are height-bounded and scroll instead of being clipped');
    assert(/@media \(max-width: 1440px\)/.test(designerCss) && !/@media \(max-width: 1320px\)/.test(designerCss), 'the three-column layout collapses before its 1072px content minimum plus the 324px shell chrome');

    page.destroy();
    console.log('\n' + pass + '/' + (pass + fail) + ' checks passed.');
    if (fail) process.exitCode = 1;
}
main().catch(error => { console.error(error); process.exitCode = 1; });
