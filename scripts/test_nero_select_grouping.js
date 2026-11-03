// scripts/test_nero_select_grouping.js
// Acceptance test for NeroSelect Channel Grouping, SVG icon rendering, and Role Assignability

const assert = require('assert');
const fs = require('fs');
const path = require('path');

// Read nero-select.js
const code = fs.readFileSync(path.join(__dirname, '../dashboard/static/js/nero-select.js'), 'utf8');

// Minimal mock DOM & jQuery environment
class MockElement {
    constructor(tagName) {
        this.tagName = (tagName || 'DIV').toUpperCase();
        this.attributes = {};
        this.children = [];
        this.dataset = {};
        this.classList = new Set();
        this.innerHTML = '';
        this.textContent = '';
        this.value = '';
    }
    setAttribute(k, v) { this.attributes[k] = String(v); }
    getAttribute(k) { return this.attributes[k]; }
    appendChild(child) {
        this.children.push(child);
        if (child instanceof MockElement) {
            // naive innerHTML approximation for assertions
            this.innerHTML += `<${child.tagName.toLowerCase()}>${child.innerHTML || child.textContent}</${child.tagName.toLowerCase()}>`;
        }
    }
}

function mockJQuery(selectorOrEl) {
    let el = selectorOrEl;
    if (typeof selectorOrEl === 'string' && selectorOrEl.startsWith('<optgroup>')) {
        el = new MockElement('OPTGROUP');
    } else if (typeof selectorOrEl === 'string' && selectorOrEl.startsWith('<option')) {
        el = new MockElement('OPTION');
    }
    return {
        _el: el,
        attr: function(k, v) {
            if (v === undefined) return el.getAttribute(k);
            el.setAttribute(k, v);
            return this;
        },
        append: function(child) {
            if (typeof child === 'string') {
                // simple parser for <option ...>text</option>
                const match = child.match(/<([a-z0-9]+)([^>]*)>(.*?)<\/\1>/i);
                if (match) {
                    const tag = match[1];
                    const attrs = match[2];
                    const text = match[3];
                    const opt = new MockElement(tag);
                    opt.innerHTML = text;
                    opt.textContent = text;
                    const attrRegex = /([a-z0-9_-]+)="([^"]*)"/gi;
                    let m;
                    while ((m = attrRegex.exec(attrs)) !== null) {
                        opt.setAttribute(m[1], m[2]);
                    }
                    if (attrs.includes('selected')) opt.setAttribute('selected', 'selected');
                    if (attrs.includes('disabled="disabled"')) opt.setAttribute('disabled', 'disabled');
                    el.children.push(opt);
                }
            } else if (child && child._el) {
                el.children.push(child._el);
            } else if (child instanceof MockElement) {
                el.children.push(child);
            }
            return this;
        },
        empty: function() {
            el.children = [];
            el.innerHTML = '';
            return this;
        },
        val: function(v) {
            if (v === undefined) return el.value;
            el.value = v;
            return this;
        },
        select2: function(opts) {
            this._select2Opts = opts;
            return this;
        },
        trigger: function() { return this; }
    };
}

// Global scope injection
global.window = global;
global.$ = mockJQuery;
global.jQuery = mockJQuery;
global.document = {
    createElement: function(tag) { return new MockElement(tag); },
    createTextNode: function(text) { return { textContent: text }; },
    querySelectorAll: function() { return []; },
    addEventListener: function() {}
};

// Execute nero-select.js code in this context
eval(code);

console.log('--- TEST 1: Channel Grouping with Mixed Categories and Uncategorized ---');
{
    const rootEl = new MockElement('SELECT');
    const $el = mockJQuery(rootEl);
    const mixedChannels = [
        { id: '101', name: 'rules', text: 'rules', category: 'Welcome & Info', type_icon: '#' },
        { id: '102', name: 'announcements', text: 'announcements', category: 'Welcome & Info', type_icon: 'megaphone' },
        { id: '201', name: 'General Voice', text: 'General Voice', category: 'Voice Lounges', type_icon: 'speaker' },
        { id: '301', name: 'sandbox', text: 'sandbox', category: '', type_icon: '#' },
        { id: '302', name: 'bot-commands', text: 'bot-commands', category: null, type_icon: '#' }
    ];

    window.NeroSelect._populateOptions($el, 'channel', mixedChannels, id => String(id) === '102', false);

    assert.strictEqual(rootEl.children.length, 3, 'Should have 3 optgroups (Welcome & Info, Voice Lounges, Uncategorized)');
    
    // Group 1: Welcome & Info
    assert.strictEqual(rootEl.children[0].attributes['label'], 'Welcome & Info');
    assert.strictEqual(rootEl.children[0].children.length, 2);
    assert.strictEqual(rootEl.children[0].children[0].textContent, 'rules');
    assert.strictEqual(rootEl.children[0].children[0].attributes['data-type-icon'], '#');
    assert.strictEqual(rootEl.children[0].children[1].textContent, 'announcements');
    assert.strictEqual(rootEl.children[0].children[1].attributes['selected'], 'selected');
    assert.strictEqual(rootEl.children[0].children[1].attributes['data-type-icon'], 'megaphone');

    // Group 2: Voice Lounges
    assert.strictEqual(rootEl.children[1].attributes['label'], 'Voice Lounges');
    assert.strictEqual(rootEl.children[1].children.length, 1);
    assert.strictEqual(rootEl.children[1].children[0].textContent, 'General Voice');
    assert.strictEqual(rootEl.children[1].children[0].attributes['data-type-icon'], 'speaker');

    // Group 3: Uncategorized (All uncategorized channels retained!)
    assert.strictEqual(rootEl.children[2].attributes['label'], 'Uncategorized');
    assert.strictEqual(rootEl.children[2].children.length, 2, 'All uncategorized channels must be present');
    assert.strictEqual(rootEl.children[2].children[0].textContent, 'sandbox');
    assert.strictEqual(rootEl.children[2].children[1].textContent, 'bot-commands');
    console.log('✅ Mixed categorized and uncategorized channels retained!');
}

console.log('--- TEST 2: Fallback for Flat / Uncategorized Server ---');
{
    const flatChannels = [
        { id: '101', name: 'general', text: 'general', category: '', type_icon: '#' },
        { id: '102', name: 'memes', text: 'memes', category: null, type_icon: '#' }
    ];

    const rootEl = new MockElement('SELECT');
    const $el = mockJQuery(rootEl);

    window.NeroSelect._populateOptions($el, 'channel', flatChannels, id => String(id) === '101', false);

    assert.strictEqual(rootEl.children.length, 2, 'Flat list should render options directly without optgroup');
    assert.strictEqual(rootEl.children[0].tagName, 'OPTION');
    assert.strictEqual(rootEl.children[0].textContent, 'general');
    assert.strictEqual(rootEl.children[0].attributes['selected'], 'selected');
    assert.strictEqual(rootEl.children[1].tagName, 'OPTION');
    assert.strictEqual(rootEl.children[1].textContent, 'memes');
    console.log('✅ Flat uncategorized fallback passed!');
}

console.log('--- TEST 3: Multi-Select Channel Options Grouping ---');
{
    const multiChannels = [
        { id: '101', name: 'mod-chat', text: 'mod-chat', category: 'Staff', type_icon: '#' },
        { id: '102', name: 'admin-log', text: 'admin-log', category: 'Staff', type_icon: '#' },
        { id: '201', name: 'lounge', text: 'lounge', category: 'Public', type_icon: 'speaker' }
    ];

    const rootEl = new MockElement('SELECT');
    const $el = mockJQuery(rootEl);
    const selectedIds = ['101', '201'];

    window.NeroSelect._populateOptions($el, 'channel', multiChannels, id => selectedIds.includes(String(id)), false);

    assert.strictEqual(rootEl.children.length, 2, 'Should have 2 optgroups (Staff, Public)');
    // Check multi-selection
    assert.strictEqual(rootEl.children[0].children[0].attributes['selected'], 'selected');
    assert.strictEqual(rootEl.children[0].children[1].attributes['selected'], undefined);
    assert.strictEqual(rootEl.children[1].children[0].attributes['selected'], 'selected');
    console.log('✅ Multi-select channel grouping passed!');
}

console.log('--- TEST 4: Role Assignability Disabled Guard & Current Value Preservation ---');
{
    const roles = [
        { id: '1', name: 'Member', text: 'Member', assignable: true, color: '#10b981' },
        { id: '2', name: 'Admin', text: 'Admin', assignable: false, unassignable_label: 'Higher than bot', color: '#ef4444' },
        { id: '3', name: 'Owner', text: 'Owner', assignable: false, unassignable_label: 'Higher than bot', color: '#ef4444' }
    ];

    // Case A: Picker with assignableOnly=true, preselected='2' (current stored value is unassignable)
    const rootElA = new MockElement('SELECT');
    const $elA = mockJQuery(rootElA);

    window.NeroSelect._populateOptions($elA, 'role', roles, id => String(id) === '2', true);

    assert.strictEqual(rootElA.children.length, 3);
    // Role 1 is assignable -> enabled
    assert.strictEqual(rootElA.children[0].attributes['disabled'], undefined);
    // Role 2 is unassignable BUT selected -> must NOT be disabled (preserved!)
    assert.strictEqual(rootElA.children[1].attributes['selected'], 'selected');
    assert.strictEqual(rootElA.children[1].attributes['disabled'], undefined);
    assert.strictEqual(rootElA.children[1].attributes['data-unassignable'], '1');
    // Role 3 is unassignable and NOT selected -> must be disabled!
    assert.strictEqual(rootElA.children[2].attributes['disabled'], 'disabled');
    assert.strictEqual(rootElA.children[2].attributes['data-unassignable'], '1');

    console.log('✅ Role assignability & preserved selection passed!');
}

console.log('\nAll NeroSelect tests passed successfully!');
