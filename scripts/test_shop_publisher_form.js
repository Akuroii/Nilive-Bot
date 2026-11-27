#!/usr/bin/env node
'use strict';
// Retirement guard: the old standalone page boot is gone, but the preview
// helpers loaded by Shop Designer still expose the same shared renderers.
const fs = require('fs');
const path = require('path');
const vm = require('vm');

let checks = 0;
function assert(ok, message) {
    checks += 1;
    if (!ok) throw new Error('FAIL: ' + message);
    console.log('PASS:', message);
}

class Element {
    constructor(tag) {
        this.tagName = tag.toUpperCase();
        this.children = [];
        this.attributes = {};
        this.hidden = false;
        this.textContent = '';
        this.className = '';
    }
    appendChild(child) { this.children.push(child); return child; }
    removeChild(child) {
        const i = this.children.indexOf(child);
        if (i !== -1) this.children.splice(i, 1);
        return child;
    }
    get firstChild() { return this.children[0] || null; }
    setAttribute(name, value) { this.attributes[name] = String(value); }
}
const document = {
    createElement(tag) { return new Element(tag); },
    createTextNode(text) { return { nodeType: 3, textContent: String(text), children: [] }; },
};
function text(node) {
    return [node.textContent || '', ...(node.children || []).map(text)].join(' ');
}

const root = path.join(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'dashboard/static/js/shop-publisher.js'), 'utf8');
const registrations = [];
const window = { NERO: { definePage(name) { registrations.push(name); } } };
const sandbox = { window, document, console, Object, Array, String };
vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: 'shop-publisher.js' });

assert(!registrations.includes('shop-publisher'), 'the standalone shop-publisher page module is not registered');
assert(typeof window.NERO.shopPublisherPreview === 'object', 'Shop Designer shared preview API remains available');
for (const name of ['renderPurchase', 'renderWarnings', 'renderTokens']) {
    assert(typeof window.NERO.shopPublisherPreview[name] === 'function', name + ' remains exported for Shop Designer');
}

const shared = window.NERO.shopPublisherPreview;
const purchase = new Element('div');
shared.renderPurchase(document, purchase, {
    kind: 'buttons', entries: [{ custom_id: 'shop_buy_42', label: 'Buy item', emoji: '🛒' }],
});
assert(!purchase.hidden && text(purchase).includes('Buy item') && text(purchase).includes('shop_buy_42'),
    'shared purchase renderer still displays the existing shop_buy_* action contract');

const warnings = new Element('div');
shared.renderWarnings(document, warnings, [{ code: 'unknown_token', path: 'content', message: 'Unresolved token' }]);
assert(warnings.children[0].attributes['data-code'] === 'unknown_token' && text(warnings).includes('Unresolved token'),
    'shared warning renderer remains functional');

const tokens = new Element('div');
shared.renderTokens(document, tokens, { used: [{ token: '{{product.name}}', path: 'content', resolved: true, value: 'Potion' }] });
assert(text(tokens).includes('{{product.name}}') && text(tokens).includes('Potion'),
    'shared token renderer remains functional');

const designerTemplate = fs.readFileSync(path.join(root, 'dashboard/templates/manage/shopdesigner.html'), 'utf8');
assert(designerTemplate.includes('js/shop-publisher.js') && designerTemplate.includes('js/shop-publications.js'),
    'Shop Designer still loads shared publisher renderers and publication lifecycle UI');
console.log('\n' + checks + ' focused checks passed.');
