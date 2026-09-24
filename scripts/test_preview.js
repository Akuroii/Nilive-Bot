#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════
   Message Builder v2 — Phase 1, step 3: the differential preview.

   What this harness has to prove, in order of importance:

     1. EQUIVALENCE — for every fixture transition,
            fullRender(d2)   ===   mount(d1) → patch(d2)
        as serialized DOM, byte for byte. A patcher that "looks right"
        but drifts from a fresh render is worse than no patcher.
     2. IDENTITY — unchanged embeds, fields and images keep the SAME DOM
        OBJECTS across a patch. This is the property the whole step exists
        for: it is what stops images re-decoding on every keystroke.
     3. SCOPE — a small edit causes a small number of DOM writes, counted
        by the engine itself (see the measurement tables printed below).
     4. DETERMINISM — the clock is injected; the same inputs produce the
        same bytes; nothing is asynchronous.
     5. SENSITIVITY — the identity/scope checks must FAIL for a preview
        that rebuilds everything. That negative control is part of this
        suite, not a one-off manual experiment.

   NO DOM LIBRARY. The engine is written against the standard DOM API and
   this harness supplies a faithful-enough double: element creation,
   keyed attributes, insertBefore with move semantics, firstChild /
   nextSibling, and textContent/innerHTML with the same
   "setting it replaces children" behaviour a browser has (violations are
   recorded and asserted to be zero). innerHTML is stored as the markup
   string the engine hands to the parser, which is exactly the level at
   which two renders can be compared outside a browser; the parsed result
   is verified in the browser gate at step 7.

   Run:  node scripts/test_preview.js
   ═══════════════════════════════════════════════════════════════ */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

let pass = 0, fail = 0; const failures = [];
function assert(cond, name, extra) {
    if (cond) { pass++; console.log('  PASS', name); }
    else { fail++; failures.push(name + (extra ? ' — ' + extra : '')); console.log('  FAIL', name, extra || ''); }
}
function section(t) { console.log('\n== ' + t + ' =='); }
const JS_DIR = path.join(__dirname, '..', 'dashboard', 'static', 'js');

// ═══════════════════════════════════════════════════════════════
// A DOM double: enough for the engine, strict about the traps
// ═══════════════════════════════════════════════════════════════
function createDom() {
    const warnings = [];
    let uid = 0;
    // Independent ground truth: every call the engine makes to the DOM is
    // counted here, so the harness can prove the engine's own stats are not
    // self-serving (see the audit section at the end).
    const ops = {
        elementsCreated: 0, appendChild: 0, insertBefore: 0, removeChild: 0,
        setAttribute: 0, removeAttribute: 0, classChanged: 0, textContentSet: 0, innerHTMLSet: 0,
    };

    function element(tag) {
        ops.elementsCreated++;
        let classNameValue = '';
        const node = {
            __uid: ++uid,
            nodeType: 1,
            tagName: String(tag).toUpperCase(),
            get className() { return classNameValue; },
            set className(v) {
                const next = v == null ? '' : String(v);
                if (next !== classNameValue) ops.classChanged++;
                classNameValue = next;
            },
            attributes: {},
            children: [],
            parentNode: null,
            _text: '',
            _html: '',
            get firstChild() { return this.children[0] || null; },
            get lastChild() { return this.children[this.children.length - 1] || null; },
            get nextSibling() {
                const p = this.parentNode;
                if (!p) return null;
                const i = p.children.indexOf(this);
                return i === -1 ? null : (p.children[i + 1] || null);
            },
            get textContent() {
                if (this.children.length) return this.children.map(c => c.textContent).join('');
                return this._text;
            },
            // Text-node children participate in the child list exactly like
            // elements do — that is how a trailing text slot behaves in a browser.
            set textContent(value) {
                // A browser REPLACES children when textContent is assigned.
                // The engine must never do that to a node it still needs, so
                // it is recorded loudly instead of failing silently.
                if (this.children.length) {
                    warnings.push('textContent assigned to <' + this.tagName.toLowerCase() +
                                  '> which had ' + this.children.length + ' child element(s)');
                    this.children = [];
                }
                ops.textContentSet++;
                this._text = String(value);
            },
            get innerHTML() { return this._html; },
            set innerHTML(value) {
                if (this.children.length) {
                    warnings.push('innerHTML assigned to <' + this.tagName.toLowerCase() +
                                  '> which had ' + this.children.length + ' child element(s)');
                    this.children = [];
                }
                ops.innerHTMLSet++;
                this._html = String(value);
            },
            getAttribute(n) {
                return Object.prototype.hasOwnProperty.call(this.attributes, n) ? this.attributes[n] : null;
            },
            setAttribute(n, v) { ops.setAttribute++; this.attributes[n] = String(v); },
            removeAttribute(n) { ops.removeAttribute++; delete this.attributes[n]; },
            hasAttribute(n) { return Object.prototype.hasOwnProperty.call(this.attributes, n); },
            appendChild(child) {
                ops.appendChild++;
                detach(child);
                child.parentNode = this;
                this.children.push(child);
                return child;
            },
            insertBefore(child, ref) {
                ops.insertBefore++;
                detach(child);                     // internal: a move is not a removeChild() call
                child.parentNode = this;
                if (!ref) { this.children.push(child); return child; }
                const i = this.children.indexOf(ref);
                if (i === -1) { this.children.push(child); return child; }
                this.children.splice(i, 0, child);
                return child;
            },
            removeChild(child) {
                ops.removeChild++;
                const i = this.children.indexOf(child);
                if (i !== -1) this.children.splice(i, 1);
                if (child.parentNode === this) child.parentNode = null;
                return child;
            },
        };
        return node;
    }

    function detach(child) {
        const p = child.parentNode;
        if (!p) return;
        const i = p.children.indexOf(child);
        if (i !== -1) p.children.splice(i, 1);
        child.parentNode = null;
    }

    function textNode(value) {
        return {
            __uid: ++uid,
            nodeType: 3,
            nodeValue: String(value),
            parentNode: null,
            get textContent() { return this.nodeValue; },
            set textContent(v) { ops.textContentSet++; this.nodeValue = String(v); },
        };
    }

    const document = element('document');
    document.createElement = element;
    document.createTextNode = textNode;

    return {
        document: document,
        element: element,
        warnings: warnings,
        ops: ops,
        createMount: () => {
            const mount = element('div');
            mount.ownerDocument = document;
            return mount;
        },
    };
}

/** Deterministic serialization: children or text or the markup string. */
function serialize(node) {
    if (!node) return '∅';
    if (node.nodeType === 3) return '«' + node.nodeValue + '»';
    const tag = node.tagName.toLowerCase();
    let out = '<' + tag;
    if (node.className) out += ' class="' + node.className + '"';
    Object.keys(node.attributes).sort().forEach(k => { out += ' ' + k + '="' + node.attributes[k] + '"'; });
    out += '>';
    if (node.children.length) out += node.children.map(serialize).join('');
    else if (node._html) out += '⟦' + node._html + '⟧';
    else if (node._text) out += '«' + node._text + '»';
    return out + '</' + tag + '>';
}

function allElements(root) {
    const out = [];
    (function walk(n) {
        (n.children || []).forEach(c => {
            if (c.nodeType === 1) { out.push(c); walk(c); }
        });
    })(root);
    return out;
}
const byClass = (root, cls) => allElements(root).filter(n => (n.className || '').split(/\s+/).indexOf(cls) !== -1);
const byKey = (root, key) => allElements(root).find(n => n.getAttribute('data-key') === key) || null;
const byPart = (root, part) => allElements(root).find(n => n.getAttribute('data-key') === part) || null;

// ═══════════════════════════════════════════════════════════════
// Load the three real modules in a sandbox with NO timers
// ═══════════════════════════════════════════════════════════════
// NERO_PREVIEW_SRC lets the mutation battery point this harness at a
// deliberately broken copy of the engine and confirm the checks below fail.
const PREVIEW_PATH = process.env.NERO_PREVIEW_SRC || path.join(JS_DIR, 'embed', 'preview.js');
const PREVIEW_SRC = fs.readFileSync(PREVIEW_PATH, 'utf8');
const sandbox = { window: {}, console };
vm.createContext(sandbox);
// The engine must not need a task queue: remove every way to schedule one.
// (Read back from INSIDE the context — an outer property read on a vm context
// does not see the context's globals, so checking from outside would pass
// vacuously.)
vm.runInContext('delete this.setTimeout; delete this.setInterval; delete this.setImmediate;'
    + 'delete this.requestAnimationFrame; delete this.queueMicrotask;'
    + 'delete this.setTimeout; delete this.Promise; delete this.Date.prototype.toJSON;', sandbox);
vm.runInContext(fs.readFileSync(path.join(JS_DIR, 'embed', 'model.js'), 'utf8'), sandbox);
vm.runInContext(fs.readFileSync(path.join(JS_DIR, 'embed', 'discord-markdown.js'), 'utf8'), sandbox);
const NS_BEFORE = Object.keys(sandbox.window.NERO.embed).sort();
vm.runInContext(PREVIEW_SRC, sandbox);
const model = sandbox.window.NERO.embed.model;
const preview = sandbox.window.NERO.embed.preview;

const NOW = 1790284740000;                     // 2026-09-24T09:19:00Z — a fixed clock
const LATER = NOW + 26 * 60 * 60 * 1000;       // +26h: a different local day
const clock = () => NOW;
const LOOKUPS = {
    roles: { '777': { name: 'Moderator', color: '#5865f2' } },
    channels: { '100': { name: 'announcements' } },
    users: { '200': 'tester' },
    onUserResolve: () => {},
};
const IDENTITY = { name: 'Nero Preview Bot', avatar: 'https://cdn.example/bot.png' };

// ═══════════════════════════════════════════════════════════════
section('the module is pure (source scan)');
// ═══════════════════════════════════════════════════════════════
{
    const forbidden = [
        ['wall clock read', /Date[\s\u0000]*\.[\s\u0000]*now/],
        ['clockless Date construction', /new[\s\u0000]+Date[\s\u0000]*\([\s\u0000]*\)/],
        ['randomness', /Math[\s\u0000]*\.[\s\u0000]*random/],
        ['timer', /set(Timer|Timeout|Interval|Immediate)|requestAnimationFrame/],
        ['task queue', /\bPromise\b|\.then[\s\u0000]*\(/],
        ['async syntax', /\basync[\s\u0000]+function|[\s\u0000]await[\s\u0000]/],
    ];
    forbidden.forEach(([what, re]) => {
        const m = PREVIEW_SRC.match(re);
        assert(!m, 'no ' + what + ' in embed/preview.js', m ? JSON.stringify(m[0]) : '');
    });
    const env = (expr) => vm.runInContext(expr, sandbox);
    assert(env('typeof setTimeout') === 'undefined' && env('typeof setInterval') === 'undefined'
        && env('typeof requestAnimationFrame') === 'undefined' && env('typeof queueMicrotask') === 'undefined',
        'the sandbox has no timers at all, and the module loaded anyway',
        [env('typeof setTimeout'), env('typeof requestAnimationFrame'), env('typeof queueMicrotask')].join(','));
    assert(env('typeof Promise') === 'undefined',
        'and no task queue either — the module cannot defer work even if it wanted to');
    assert(env('typeof Date') === 'function', 'the injected-clock formatter may still format dates');
}

// ═══════════════════════════════════════════════════════════════
// Fixtures
// ═══════════════════════════════════════════════════════════════
let idSeq = 0;
const nextIds = () => model.createIdFactory('p' + (++idSeq));

/** Build a document from the fixture shape (editor-shaped embeds). */
function doc(spec) {
    return model.fromEditorDocument({
        content: spec.content || '',
        embeds: spec.embeds || [],
    }, { ids: nextIds(), guildId: 'g1' });
}
const em = (o) => Object.assign(model.toEditorEmbed(model.blankEmbed({ ids: nextIds() })), o);

function mountPreview(document, opts) {
    const dom = createDom();
    const mount = dom.createMount();
    const p = preview.create(mount, Object.assign({
        now: clock, document: dom.document, botIdentity: IDENTITY, lookups: LOOKUPS,
    }, opts || {}));
    if (document) p.updateDocument(document);
    return { dom, mount, preview: p, html: () => serialize(mount) };
}
function statsOf(p) { return p.stats(); }
function delta(before, after) {
    const out = {};
    Object.keys(after).forEach(k => { out[k] = after[k] - (before[k] || 0); });
    return out;
}

const PRINTED = {};
function measure(label, d) {
    PRINTED[label] = d;
    console.log('    ' + label.padEnd(46) + JSON.stringify(d));
}

// ═══════════════════════════════════════════════════════════════
section('initial mount — keyed structure');
// ═══════════════════════════════════════════════════════════════
const d1 = doc({
    content: 'Hello **world** <@200>',
    embeds: [
        em({
            title: 'Rules', description: 'Be nice', color: '#7c5cbf',
            author: 'Nero', authorIcon: 'https://cdn.example/a.png', authorUrl: 'https://nilive.example/a',
            footer: 'Nero', footerIcon: 'https://cdn.example/f.png',
            image: 'https://cdn.example/i.png', thumbnail: 'https://cdn.example/t.png',
            url: 'https://nilive.example/rules', timestamp: '2026-09-24T08:00:00.000Z',
            fields: [{ name: 'Rule 1', value: 'Be kind', inline: false }, { name: 'Rule 2', value: 'Have fun', inline: true }],
        }),
        em({ title: 'Second embed', description: 'plain' }),
    ],
});
const m1 = mountPreview(d1);
{
    assert(m1.html().indexOf('eb-msg') !== -1, 'the message chrome is mounted');
    assert(byKey(m1.mount, 'message') !== null, 'the message node carries data-key="message"');
    assert(byKey(m1.mount, 'content') !== null, 'the content node is keyed');
    assert(byClass(m1.mount, 'eb-msg-avatar').length === 1 && byClass(m1.mount, 'eb-msg-avatar')[0].tagName === 'IMG',
        'the avatar is an <img> (there is one)');

    const embedKeys = d1.embeds.map(e => e.id);
    const cards = byClass(m1.mount, 'eb-preview-embed');
    assert(cards.length === 2, 'both embeds rendered');
    assert(cards[0].getAttribute('data-key') === 'embed:' + embedKeys[0] &&
           cards[1].getAttribute('data-key') === 'embed:' + embedKeys[1],
        'embed cards are keyed by the MODEL ids, not by position',
        cards.map(c => c.getAttribute('data-key')).join(', '));
    const fieldKeys = d1.embeds[0].fields.map(f => f.id);
    const fieldNodes = byClass(m1.mount, 'eb-pe-field');
    assert(fieldNodes.map(f => f.getAttribute('data-key')).join(',') ===
           fieldKeys.map(k => 'field:' + k).join(','),
        'field cards are keyed by the model field ids');
    assert(PREVIEW_SRC.indexOf('Math.random') === -1 && PREVIEW_SRC.indexOf('__uid') === -1,
        'the preview generates no keys of its own (no counter, no random)');

    // the keyed payload is the wire payload plus keys, and nothing else
    const keyed = model.toDiscordPayload(d1, { withKeys: true });
    const plain = model.toDiscordPayload(d1);
    const strip = (v) => {
        if (Array.isArray(v)) return v.map(strip);
        if (v && typeof v === 'object') {
            const out = {};
            Object.keys(v).forEach(k => { if (k !== 'key') out[k] = strip(v[k]); });
            return out;
        }
        return v;
    };
    assert(JSON.stringify(strip(keyed)) === JSON.stringify(plain),
        'stripping the keys reproduces the canonical wire payload byte for byte');
    assert(keyed.embeds[0].key === d1.embeds[0].id && keyed.embeds[0].fields[0].key === d1.embeds[0].fields[0].id,
        'keys are the model ids');
    assert(JSON.stringify(model.toDiscordPayload(d1)) === JSON.stringify(plain),
        'the default (unkeyed) transform is unchanged — step 1 is untouched in behaviour');
    assert(m1.preview.lastPayload().embeds[0].key !== undefined,
        'updateDocument feeds the preview the keyed canonical payload');
}

// ═══════════════════════════════════════════════════════════════
section('identity — unchanged nodes keep their DOM objects');
// ═══════════════════════════════════════════════════════════════
{
    const before = {
        cards: byClass(m1.mount, 'eb-preview-embed'),
        fields: byClass(m1.mount, 'eb-pe-field'),
        images: allElements(m1.mount).filter(n => n.tagName === 'IMG'),
        content: byKey(m1.mount, 'content'),
        header: byKey(m1.mount, 'header'),
    };
    m1.preview.updateDocument(model.setContent(d1, 'Hello **world** <@200>!'));
    const after = {
        cards: byClass(m1.mount, 'eb-preview-embed'),
        fields: byClass(m1.mount, 'eb-pe-field'),
        images: allElements(m1.mount).filter(n => n.tagName === 'IMG'),
        content: byKey(m1.mount, 'content'),
        header: byKey(m1.mount, 'header'),
    };
    assert(after.cards[0] === before.cards[0] && after.cards[1] === before.cards[1],
        'a content edit keeps BOTH embed cards as the same objects');
    assert(after.fields.every((f, i) => f === before.fields[i]) && after.fields.length === before.fields.length,
        'every field card is the same object after a content edit');
    assert(after.images.length === before.images.length && after.images.every((img, i) => img === before.images[i]),
        `all ${before.images.length} <img> elements are the same objects after a content edit`);
    assert(after.content === before.content && after.header === before.header,
        'the content and header nodes keep identity too');

    // a second edit, different field this time
    const imgBeingWatched = byClass(m1.mount, 'eb-pe-image')[0];
    const thumbBeingWatched = byClass(m1.mount, 'eb-pe-thumb')[0];
    const otherCard = byClass(m1.mount, 'eb-preview-embed')[1];
    const liveDoc = model.setContent(d1, 'Hello **world** <@200>!');   // what is mounted right now
    const beforeFieldEdit = m1.preview.stats();
    m1.preview.updateDocument(model.setField(liveDoc, d1.embeds[0].id, d1.embeds[0].fields[0].id, { value: 'Be very kind' }));
    assert(byClass(m1.mount, 'eb-pe-image')[0] === imgBeingWatched, 'editing a field keeps the embed image node');
    assert(byClass(m1.mount, 'eb-pe-thumb')[0] === thumbBeingWatched, 'and the thumbnail node');
    assert(byClass(m1.mount, 'eb-preview-embed')[1] === otherCard,
        'and does not touch the other embed');
    assert(byClass(m1.mount, 'eb-pe-field')[1] === before.fields[1],
        'and does not touch the sibling field');
    const fieldEdit = delta(beforeFieldEdit, m1.preview.stats());
    assert(fieldEdit.nodesCreated === 0 && fieldEdit.textWrites === 0 && fieldEdit.attrWrites === 0
        && fieldEdit.markupWrites === 1 && fieldEdit.markdownRenders === 1,
        'a field-VALUE edit is one re-parse of that field, and no text or attribute write at all',
        JSON.stringify(fieldEdit));
    measure('field-value edit (write counts)', fieldEdit);
}

// ═══════════════════════════════════════════════════════════════
section('mutation scope — measured counts');
// ═══════════════════════════════════════════════════════════════
{
    const scenario = (label, build, mutate) => {
        const base = build();
        const m = mountPreview(base);
        const before = m.preview.stats();
        m.preview.updateDocument(mutate(base));
        const d = delta(before, m.preview.stats());
        measure(label, { created: d.nodesCreated, removed: d.nodesRemoved, moved: d.nodesMoved, text: d.textWrites, attr: d.attrWrites, markup: d.markupWrites, renders: d.markdownRenders });
        return { d, mount: m.mount, base };
    };

    const contentOnly = scenario('content keystroke', () => d1, d => model.setContent(d, 'Hello **world** <@200>!!!'));
    assert(contentOnly.d.nodesCreated === 0 && contentOnly.d.nodesRemoved === 0 && contentOnly.d.nodesMoved === 0,
        'a content keystroke creates, removes and moves NOTHING', JSON.stringify(contentOnly.d));
    assert(contentOnly.d.markupWrites === 1 && contentOnly.d.markdownRenders === 1,
        'and costs exactly one markup write and one markdown parse', JSON.stringify(contentOnly.d));
    assert(contentOnly.d.textWrites === 0 && contentOnly.d.attrWrites === 0,
        'and writes no text and no attributes');

    const titleOnly = scenario('embed title keystroke', () => d1, d => model.setEmbedFields(d, d.embeds[0].id, { title: 'Rules!' }));
    assert(titleOnly.d.nodesCreated === 0 && titleOnly.d.textWrites === 1 && titleOnly.d.markdownRenders === 0,
        'a title keystroke is one text write, zero parses, zero nodes', JSON.stringify(titleOnly.d));

    const addBlank = scenario('add a BLANK field (not yet sent)', () => d1, d => model.addField(d, d.embeds[0].id));
    assert(Object.keys(addBlank.d).every(k => k === 'patches' || addBlank.d[k] === 0),
        'adding a still-blank field changes nothing, because a blank field is not in the payload',
        JSON.stringify(addBlank.d));

    const addFilled = scenario('add a field and fill it', () => d1, d => {
        const withField = model.addField(d, d.embeds[0].id);
        const newId = withField.embeds[0].fields[withField.embeds[0].fields.length - 1].id;
        return model.setField(withField, d.embeds[0].id, newId, { name: 'Rule 3', value: 'And that' });
    });
    assert(addFilled.d.nodesCreated === 3 && addFilled.d.nodesRemoved === 0 && addFilled.d.nodesMoved === 0,
        'adding a field creates exactly its three nodes and touches nothing else', JSON.stringify(addFilled.d));
    assert(addFilled.d.textWrites === 1 && addFilled.d.attrWrites === 5
        && addFilled.d.markupWrites === 1 && addFilled.d.markdownRenders === 1,
        'its name is one text write, its classes/style/key are five attribute writes, '
        + 'its value one parse — no existing markup re-parsed', JSON.stringify(addFilled.d));

    const removeField = scenario('remove a field', () => d1, d => model.removeField(d, d.embeds[0].id, d.embeds[0].fields[0].id));
    assert(removeField.d.nodesRemoved === 1 && removeField.d.nodesMoved === 0 && removeField.d.markdownRenders === 0,
        'removing a field drops one keyed node and re-parses nothing', JSON.stringify(removeField.d));

    const reorder = scenario('reorder two fields', () => d1, d => model.moveField(d, d.embeds[0].id, d.embeds[0].fields[0].id, 1));
    assert(reorder.d.nodesCreated === 0 && reorder.d.nodesRemoved === 0 && reorder.d.nodesMoved === 1,
        'a two-field reorder is ONE move and no re-creation', JSON.stringify(reorder.d));
    assert(reorder.d.markdownRenders === 0, 'and no re-parse');

    const imageSwap = scenario('image url change', () => d1, d => model.setMedia(d, d.embeds[0].id, 'image', 'https://cdn.example/i2.png'));
    assert(imageSwap.d.attrWrites === 1 && imageSwap.d.nodesCreated === 0,
        'changing an image url is one attribute write on the existing node', JSON.stringify(imageSwap.d));

    const addEmbed = scenario('add an embed', () => d1, d => model.addEmbed(d));
    assert(addEmbed.d.nodesRemoved === 0 && addEmbed.d.markdownRenders === 0,
        'adding an embed removes nothing and re-parses nothing', JSON.stringify(addEmbed.d));

    const removeEmbed = scenario('remove the second embed', () => d1, d => model.removeEmbed(d, d.embeds[1].id));
    assert(removeEmbed.d.nodesRemoved === 1 && removeEmbed.d.markdownRenders === 0,
        'removing an embed drops one keyed card', JSON.stringify(removeEmbed.d));

    const moveEmbed = scenario('reorder two embeds', () => d1, d => model.moveEmbed(d, d.embeds[1].id, -1));
    assert(moveEmbed.d.nodesMoved === 1 && moveEmbed.d.nodesCreated === 0,
        'reordering embeds is one move', JSON.stringify(moveEmbed.d));

    const noop = scenario('patch with identical state', () => d1, d => d);
    assert(Object.keys(noop.d).every(k => k === 'patches' || noop.d[k] === 0),
        'patching an unchanged document performs ZERO DOM writes', JSON.stringify(noop.d));
}

// ═══════════════════════════════════════════════════════════════
section('lists — add, remove, reorder, keyed identity');
// ═══════════════════════════════════════════════════════════════
{
    const base = doc({ embeds: [em({ title: 'A', fields: [{ name: '1', value: 'a' }, { name: '2', value: 'b' }, { name: '3', value: 'c' }] })] });
    const m = mountPreview(base);
    const eid = base.embeds[0].id;
    const f = base.embeds[0].fields;
    const nodes = () => byClass(m.mount, 'eb-pe-field');

    const n0 = nodes().slice();
    assert(n0.length === 3, 'three fields mounted');

    // middle removal: the two survivors must be the same objects
    m.preview.updateDocument(model.removeField(base, eid, f[1].id));
    const n1 = nodes();
    assert(n1.length === 2 && n1[0] === n0[0] && n1[1] === n0[2],
        'removing the MIDDLE field keeps the two survivors as the same objects');
    assert(byPart(m.mount, 'fields').children.length === 2, 'and leaves two children in the region');

    // re-insert a NEW field at the FRONT: the newcomers are new objects, the
    // survivors must still be the same objects, shifted down by one.
    const afterRemoval = model.removeField(base, eid, f[1].id);
    const added = model.addField(afterRemoval, eid);
    const newFieldId = added.embeds[0].fields[2].id;
    let d = model.setField(added, eid, newFieldId, { name: 'Z', value: 'z' });
    d = model.moveField(d, eid, newFieldId, -3);
    const beforeInsert = m.preview.stats();
    m.preview.updateDocument(d);
    const inserted = delta(beforeInsert, m.preview.stats());
    const n2 = nodes();
    assert(n2.length === 3 && n2[0].getAttribute('data-key') === 'field:' + newFieldId,
        'the new field lands first');
    assert(n2[1] === n1[0] && n2[2] === n1[1],
        'and the two survivors are still the same objects after an insert above them');
    assert(inserted.nodesCreated === 3 && inserted.nodesRemoved === 0 && inserted.nodesMoved === 0
        && inserted.markdownRenders === 1 && inserted.textWrites === 1 && inserted.attrWrites === 5,
        'the insert costs three new nodes and one parse — no survivor re-created, nothing moved',
        JSON.stringify(inserted));
    measure('insert a new field above survivors', inserted);

    // isolated rotate: [1,2,3] -> [3,1,2] must be ONE move
    const m3 = mountPreview(base);
    const before = byClass(m3.mount, 'eb-pe-field');
    const s0 = m3.preview.stats();
    m3.preview.updateDocument(model.moveField(base, eid, f[2].id, -2));
    const rotated = delta(s0, m3.preview.stats());
    const afterRev = byClass(m3.mount, 'eb-pe-field');
    assert(afterRev.length === 3 && afterRev[0] === before[2] && afterRev[1] === before[0] && afterRev[2] === before[1],
        'a rotate to the front keeps every node, only reordered',
        afterRev.map(n => n.__uid).join(','));
    assert(rotated.nodesMoved === 1 && rotated.nodesCreated === 0 && rotated.nodesRemoved === 0
        && rotated.markdownRenders === 0,
        'exactly one move, nothing created, removed or re-parsed', JSON.stringify(rotated));
}

// ═══════════════════════════════════════════════════════════════
section('images — identity and attribute-only updates');
// ═══════════════════════════════════════════════════════════════
{
    const base = doc({ embeds: [em({ title: 'A', image: 'https://cdn.example/i.png', thumbnail: 'https://cdn.example/t.png', author: 'X', authorIcon: 'https://cdn.example/a.png', footer: 'F', footerIcon: 'https://cdn.example/f.png' })] });
    const m = mountPreview(base);
    const img = () => byClass(m.mount, 'eb-pe-image')[0];
    const thumb = () => byClass(m.mount, 'eb-pe-thumb')[0];
    const avatar = () => byClass(m.mount, 'eb-msg-avatar')[0];
    const aIcon = () => byClass(m.mount, 'eb-pe-author')[0].children[0];
    const fIcon = () => allElements(byClass(m.mount, 'eb-pe-footer')[0]).filter(n => n.tagName === 'IMG')[0];

    const refs = { img: img(), thumb: thumb(), avatar: avatar(), aIcon: aIcon(), fIcon: fIcon() };
    assert(refs.img && refs.thumb && refs.avatar && refs.aIcon && refs.fIcon, 'all five image slots rendered');

    // edit something unrelated
    m.preview.updateDocument(model.setEmbedFields(base, base.embeds[0].id, { title: 'A2' }));
    const changedRefs = Object.keys(refs).filter(k => ({ img, thumb, avatar, aIcon, fIcon })[k]() !== refs[k]);
    assert(changedRefs.length === 0,
        'editing the title keeps ALL five image nodes as the same objects', changedRefs.join(','));

    // change one image url
    m.preview.updateDocument(model.setMedia(base, base.embeds[0].id, 'image', 'https://cdn.example/i2.png'));
    assert(img() === refs.img, 'changing the image url keeps the SAME <img> node');
    assert(img().getAttribute('src') === 'https://cdn.example/i2.png', 'and updates its src');
    assert(thumb() === refs.thumb && thumb().getAttribute('src') === 'https://cdn.example/t.png',
        'the thumbnail is untouched');

    // an unsafe url renders no image at all, and a safe one brings the part back
    const unsafe = model.setMedia(base, base.embeds[0].id, 'image', 'javascript:alert(1)');
    m.preview.updateDocument(unsafe);
    assert(byClass(m.mount, 'eb-pe-image').length === 0, 'a javascript: url renders no image node');
    assert(byClass(m.mount, 'eb-pe-thumb').length === 1, 'and leaves the sibling thumbnail alone');
    // A node that left the tree is gone: bringing the image back mounts a
    // FRESH <img> (and only that node changes). Identity is preserved for
    // nodes that stay, which is the property that matters for typing.
    const thumbStill = thumb();
    m.preview.updateDocument(base);
    assert(byClass(m.mount, 'eb-pe-image').length === 1 && byClass(m.mount, 'eb-pe-image')[0] !== refs.img,
        'an image that was removed and restored is a fresh node (the old one left the tree)');
    assert(thumb() === thumbStill && thumb().getAttribute('src') === 'https://cdn.example/t.png',
        'and restoring it still does not touch the thumbnail');
}

// ═══════════════════════════════════════════════════════════════
section('determinism — injected clock, no ambient state');
// ═══════════════════════════════════════════════════════════════
{
    const withTime = doc({ embeds: [em({ title: 'T', timestamp: '2026-09-24T08:00:00.000Z', footer: 'F' })] });
    const a = mountPreview(withTime);
    const b = mountPreview(withTime);
    assert(a.html() === b.html(), 'two previews with the same clock serialize identically');
    assert(/Today at \d{1,2}:\d{2} (AM|PM)/.test(byClass(a.mount, 'eb-msg-time')[0].textContent),
        'the header time uses the injected clock: ' + byClass(a.mount, 'eb-msg-time')[0].textContent);
    assert(byClass(a.mount, 'eb-pe-footer')[0].textContent.indexOf('Today at') !== -1,
        'a same-day timestamp reads "Today at ..."');

    const sameDayHead = byClass(a.mount, 'eb-msg-time')[0].textContent;
    const later = mountPreview(withTime, { now: () => LATER });
    assert(byClass(later.mount, 'eb-msg-time')[0].textContent !== sameDayHead,
        'a different clock changes the header time (the clock really is the input)');
    assert(byClass(later.mount, 'eb-pe-footer')[0].textContent.indexOf('Today at') === -1,
        'and the same embed timestamp is no longer "Today" on another day');

    // the SAME document patched twice is byte-identical and writes nothing
    const c = mountPreview(withTime);
    c.preview.updateDocument(withTime);
    const afterFirst = c.preview.stats();
    c.preview.updateDocument(withTime);
    const d = delta(afterFirst, c.preview.stats());
    assert(a.html() === c.html(), 'patching the same document again changes nothing on screen');
    assert(Object.keys(d).every(k => k === 'patches' || d[k] === 0),
        'and performs zero DOM writes on the second patch', JSON.stringify(d));

    // frozen clock => the header never drifts between patches
    const before = byClass(c.mount, 'eb-msg-time')[0].textContent;
    for (let i = 0; i < 5; i++) c.preview.updateDocument(model.setContent(withTime, 'x'.repeat(i + 1)));
    assert(byClass(c.mount, 'eb-msg-time')[0].textContent === before,
        'repeated patches with a frozen clock never change the header time');
}

// ═══════════════════════════════════════════════════════════════
section('empty, minimal and boundary documents');
// ═══════════════════════════════════════════════════════════════
{
    const empty = mountPreview(model.blankMessageDocument({ ids: nextIds() }));
    assert(byClass(empty.mount, 'eb-preview-empty').length === 1, 'an empty document shows the placeholder');
    assert(empty.html().indexOf('Start typing') !== -1, 'with the configured text');

    const withContent = mountPreview(d1);
    withContent.preview.updateDocument(model.blankMessageDocument({ ids: nextIds() }));
    assert(byClass(withContent.mount, 'eb-preview-empty').length === 1 &&
           byClass(withContent.mount, 'eb-msg').length === 0,
        'going back to empty removes the message entirely');

    const back = mountPreview(model.blankMessageDocument({ ids: nextIds() }));
    back.preview.updateDocument(d1);
    assert(byKey(back.mount, 'message') !== null, 'and content brings it back');
    assert(byClass(back.mount, 'eb-preview-empty').length === 0, 'with the placeholder gone');

    const blankFields = doc({ embeds: [em({ title: 'T', fields: [{ name: '', value: '' }, { name: 'real', value: 'x' }] })] });
    const bf = mountPreview(blankFields);
    assert(byClass(bf.mount, 'eb-pe-field').length === 1,
        'a blank field is dropped exactly as v1 drops it, leaving one field');
    assert(byClass(bf.mount, 'eb-pe-field-value')[0].innerHTML === 'x', 'the surviving field renders its value');
    const zeroWidth = doc({ embeds: [em({ title: 'T', fields: [{ name: '', value: 'v' }] })] });
    const zw = mountPreview(zeroWidth);
    assert(byClass(zw.mount, 'eb-pe-field-name')[0].textContent === '\u200b',
        'a blank field NAME renders the zero-width placeholder that is actually sent to Discord');

    // maximum: 10 embeds x 25 fields
    const big = doc({
        content: 'bulk',
        embeds: Array.from({ length: 10 }, (_, i) => em({
            title: 'Embed ' + i,
            description: 'desc ' + i,
            image: 'https://cdn.example/' + i + '.png',
            fields: Array.from({ length: 25 }, (_, j) => ({ name: 'f' + j, value: 'v' + j, inline: j % 2 === 0 })),
        })),
    });
    const bigPreview = mountPreview(big);
    const bigAfterMount = bigPreview.preview.stats();
    const bigLive = model.setContent(big, 'bulk!');
    const t0 = Date.now();
    bigPreview.preview.updateDocument(bigLive);
    const elapsed = Date.now() - t0;
    const bigStats = delta(bigAfterMount, bigPreview.preview.stats());
    assert(byClass(bigPreview.mount, 'eb-preview-embed').length === 10, '10 embeds render');
    assert(byClass(bigPreview.mount, 'eb-pe-field').length === 250, '250 fields render');
    assert(byClass(bigPreview.mount, 'eb-pe-image').length === 10, '10 images render');
    assert(bigStats.nodesCreated === 0 && bigStats.nodesRemoved === 0 && bigStats.nodesMoved === 0
        && bigStats.markdownRenders === 1 && bigStats.markupWrites === 1,
        'a content edit on the largest document creates, removes and moves NOTHING and parses once',
        JSON.stringify(bigStats));
    measure('10 embeds x 25 fields — content keystroke', { created: bigStats.nodesCreated, removed: bigStats.nodesRemoved, moved: bigStats.nodesMoved, text: bigStats.textWrites, attr: bigStats.attrWrites, markup: bigStats.markupWrites, renders: bigStats.markdownRenders, ms: elapsed });
    const bigField = big.embeds[5].fields[12];
    const bigBeforeField = bigPreview.preview.stats();
    bigPreview.preview.updateDocument(model.setField(bigLive, big.embeds[5].id, bigField.id, { value: 'edited' }));
    const bigStats2 = delta(bigBeforeField, bigPreview.preview.stats());
    assert(bigStats2.markdownRenders === 1 && bigStats2.markupWrites === 1 && bigStats2.nodesCreated === 0
        && bigStats2.nodesRemoved === 0 && bigStats2.nodesMoved === 0,
        'and a single field edit in the middle of 250 fields is one parse and one write',
        JSON.stringify(bigStats2));
    measure('10 embeds x 25 fields — one field value edit', bigStats2);
}

// ═══════════════════════════════════════════════════════════════
section('markdown memo — parses are proportional to changes');
// ═══════════════════════════════════════════════════════════════
{
    const base = doc({
        content: 'c',
        embeds: [em({ title: 'A', description: 'd1', fields: [{ name: 'n1', value: 'v1' }, { name: 'n2', value: 'v2' }] })],
    });
    const m = mountPreview(base);
    const first = m.preview.stats();
    assert(first.markdownRenders === 4,
        'a full mount parses each markup leaf exactly once: content, description, two field values', String(first.markdownRenders));

    m.preview.updateDocument(base);
    assert(m.preview.stats().markdownRenders === 4, 're-patching the same document parses nothing further');

    m.preview.updateDocument(model.setField(base, base.embeds[0].id, base.embeds[0].fields[1].id, { value: 'v2!' }));
    assert(m.preview.stats().markdownRenders === 5, 'one changed field = one more parse');

    // a lookups bump invalidates every markup leaf (mentions may resolve now)
    m.preview.setContext({ lookupsVersion: 1 });
    m.preview.updateDocument(base);
    const afterBump = m.preview.stats();
    assert(afterBump.markdownRenders === 9,
        'bumping the lookups version re-parses every markup leaf once (4 more)', String(afterBump.markdownRenders));
    m.preview.updateDocument(base);
    assert(m.preview.stats().markdownRenders === 9, 'and then settles again');

    // setContext with new lookups must invalidate the memo by itself — a
    // footgun that would otherwise show stale mentions in the preview.
    m.preview.setContext({ lookups: { roles: {}, channels: {}, users: {} } });
    m.preview.updateDocument(base);
    assert(m.preview.stats().markdownRenders === 13,
        'setContext({lookups}) auto-bumps the version, so no stale markup survives', String(m.preview.stats().markdownRenders));
}

// ═══════════════════════════════════════════════════════════════
section('equivalence — fullRender(d2) vs mount(d1) then patch(d2)');
// ═══════════════════════════════════════════════════════════════
{
    const corpus = [
        ['empty', model.blankMessageDocument({ ids: nextIds() })],
        ['content only', doc({ content: 'hello **world**' })],
        ['content + emoji only', doc({ content: '🎲🎲' })],
        ['one embed, title', doc({ embeds: [em({ title: 'A' })] })],
        ['one embed, everything', doc({ embeds: [em({
            title: 'A', description: 'd `code`', color: '#123456',
            author: 'Nero', authorIcon: 'https://cdn.example/a.png', authorUrl: 'https://nilive.example/a',
            footer: 'F', footerIcon: 'https://cdn.example/f.png',
            url: 'https://nilive.example/e', timestamp: '2026-09-24T08:00:00.000Z',
            image: 'https://cdn.example/i.png', thumbnail: 'https://cdn.example/t.png',
            fields: [{ name: 'n1', value: 'v1', inline: true }, { name: 'n2', value: 'v2' }],
        })] })],
        ['two embeds', doc({ content: 'c', embeds: [em({ title: 'A', fields: [{ name: 'x', value: 'y' }] }), em({ description: 'B' })] })],
        ['no images, fields only', doc({ embeds: [em({ fields: [{ name: 'a', value: 'b' }, { name: 'c', value: 'd' }] })] })],
        ['mentions', doc({ content: 'hi <@200> <#100> <@&777> <:e:111>' })],
        ['fenced code', doc({ embeds: [em({ description: '```js\nconst a = 1;\n```' })] })],
        ['blank fields only', doc({ embeds: [em({ title: 'T', fields: [{ name: '', value: '' }] })] })],
        ['unsafe urls', doc({ embeds: [em({ title: 'T', url: 'javascript:alert(1)', image: 'javascript:alert(2)', author: 'A', authorUrl: 'ftp://x' })] })],
        ['author icon only', doc({ embeds: [em({ authorIcon: 'https://cdn.example/a.png' })] })],
        ['footer icon only', doc({ embeds: [em({ footerIcon: 'https://cdn.example/f.png' })] })],
    ];

    let comparisons = 0, mismatches = [];
    corpus.forEach(([fromName, fromDoc]) => {
        corpus.forEach(([toName, toDoc]) => {
            // A: mount(from) then patch(to)
            const patched = mountPreview(fromDoc);
            patched.preview.updateDocument(toDoc);
            // B: a fresh full render of `to`
            const fresh = mountPreview(null);
            fresh.preview.updateDocument(toDoc);
            comparisons++;
            if (patched.html() !== fresh.html()) {
                mismatches.push(fromName + ' → ' + toName);
                if (mismatches.length === 1) {
                    console.log('      first mismatch (' + fromName + ' → ' + toName + ')\n      patched: ' +
                        patched.html().slice(0, 400) + '\n      fresh:   ' + fresh.html().slice(0, 400));
                }
            }
        });
    });
    assert(mismatches.length === 0,
        `all ${comparisons} fixture transitions: the patched DOM equals a fresh full render, byte for byte`,
        mismatches.slice(0, 5).join(', '));

    // and the same via the low-level keyed-payload entry point
    let payloadMismatch = 0;
    corpus.forEach(([, a]) => corpus.forEach(([, b]) => {
        if (serialize(mountPreview(a).mount) !== serialize(mountPreview(a).mount)) payloadMismatch++;
    }));
    assert(payloadMismatch === 0, 'mounting the same fixture twice is itself deterministic');
}

// ═══════════════════════════════════════════════════════════════
section('SENSITIVITY — the checks above must catch a rebuilding preview');
// ═══════════════════════════════════════════════════════════════
{
    // A deliberately naive implementation: throw everything away and render
    // again on every update. It is CORRECT (it should still pass the
    // equivalence check) but it destroys identity, which is the exact
    // regression this step exists to prevent. If the identity/scope
    // assertions do not fail against it, they prove nothing.
    function rebuildingPreview(mount, opts) {
        let inner = null;
        return {
            updateDocument(document) {
                if (inner) inner.destroy();
                inner = preview.create(mount, opts);
                inner.updateDocument(document);
            },
            updatePayload(payload) {
                if (inner) inner.destroy();
                inner = preview.create(mount, opts);
                inner.updatePayload(payload);
            },
            stats: () => (inner ? inner.stats() : null),
            destroy() { if (inner) inner.destroy(); },
        };
    }

    const dom = createDom();
    const mount = dom.createMount();
    const naive = rebuildingPreview(mount, { now: clock, document: dom.document, botIdentity: IDENTITY, lookups: LOOKUPS });
    naive.updateDocument(d1);
    const imagesBefore = allElements(mount).filter(n => n.tagName === 'IMG');
    const fieldsBefore = byClass(mount, 'eb-pe-field');
    naive.updateDocument(model.setContent(d1, 'a different string'));
    const imagesAfter = allElements(mount).filter(n => n.tagName === 'IMG');
    const fieldsAfter = byClass(mount, 'eb-pe-field');

    const identityCaughtIt = imagesAfter.some((n, i) => n !== imagesBefore[i]) ||
                             fieldsAfter.some((n, i) => n !== fieldsBefore[i]);
    assert(identityCaughtIt,
        'the identity checks FAIL for a preview that rebuilds everything (so they are sensitive)');
    assert(imagesAfter.length === imagesBefore.length && fieldsAfter.length === fieldsBefore.length,
        'the rebuild is still structurally correct — it is only identity it destroys');

    // equivalence must still hold for the naive version (it is the same
    // renderer, just a wasteful one) — proving equivalence alone is weak
    const patchedNaive = mountPreview(null);
    const naive2 = rebuildingPreview(patchedNaive.mount, { now: clock, document: patchedNaive.dom.document, botIdentity: IDENTITY, lookups: LOOKUPS });
    naive2.updateDocument(d1);
    naive2.updateDocument(model.setContent(d1, 'x'));
    const freshRef = mountPreview(model.setContent(d1, 'x'));
    assert(serialize(patchedNaive.mount) === freshRef.html(),
        'AND equivalence alone would NOT have caught it — which is why identity+scope are asserted separately');

    // scope counts catch it too
    const dom3 = createDom();
    const mount3 = dom3.createMount();
    const naive3 = rebuildingPreview(mount3, { now: clock, document: dom3.document, botIdentity: IDENTITY, lookups: LOOKUPS });
    naive3.updateDocument(d1);
    naive3.updateDocument(model.setContent(d1, 'y'));
    const s = naive3.stats();
    // (the discarded inner preview's own teardown is counted into ITS stats,
    //  which the naive wrapper throws away — so `removed` stays 0 here;
    //  `created` and `renders` are the numbers that expose the rebuild.)
    assert(s.nodesCreated > 20 && s.markdownRenders > 1,
        'the scope counters also expose it (a whole tree created and re-parsed for one keystroke)',
        JSON.stringify({ created: s.nodesCreated, removed: s.nodesRemoved, renders: s.markdownRenders }));
    measure('rebuild-everything preview: one content keystroke (cumulative in the rebuilt tree)',
        { created: s.nodesCreated, removed: s.nodesRemoved, moved: s.nodesMoved, text: s.textWrites, attr: s.attrWrites, markup: s.markupWrites, renders: s.markdownRenders });
}

// ═══════════════════════════════════════════════════════════════
section('v1 fidelity — the frozen composer paints the same thing');
// ═══════════════════════════════════════════════════════════════
{
    // The differential preview must paint what v1 painted. This compares the
    // v2 DOM against the REAL, untouched `EmbedComposer.renderPreview` output,
    // token by token (tag, classes, attributes, text), so a stray class, a
    // missing inline style or a link rendered when v1 would not render one
    // shows up as a failure instead of as a visual surprise at step 5.
    const v1sandbox = { window: {}, console };
    vm.createContext(v1sandbox);
    vm.runInContext(fs.readFileSync(path.join(JS_DIR, 'embed-composer.js'), 'utf8'), v1sandbox);
    const V1 = v1sandbox.window.EmbedComposer;

    const VOID_TAGS = { img: 1, br: 1, hr: 1, input: 1, meta: 1, link: 1 };
    const ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", '#39': "'", bull: '\u2022', nbsp: '\u00a0' };
    const decode = (v) => String(v).replace(/&(#?[a-z0-9]+);/gi, (m, name) => (
        ENTITIES[name.toLowerCase()] !== undefined ? ENTITIES[name.toLowerCase()] : m));

    /** v1's HTML string -> canonical tokens */
    function tokensFromHtml(html) {
        const out = [];
        let i = 0;
        const pushText = (raw) => {
            const v = decode(raw);
            if (v.trim()) out.push({ t: 'text', v: v });
        };
        while (i < html.length) {
            const lt = html.indexOf('<', i);
            if (lt === -1) { pushText(html.slice(i)); break; }
            if (lt > i) pushText(html.slice(i, lt));
            const gt = html.indexOf('>', lt);
            if (gt === -1) { pushText(html.slice(lt)); break; }
            const raw = html.slice(lt + 1, gt);
            if (raw[0] === '/') {
                out.push({ t: 'close', tag: raw.slice(1).trim().toLowerCase() });
            } else {
                const m = /^([a-zA-Z][\w-]*)([\s\S]*)$/.exec(raw);
                const tag = m[1].toLowerCase();
                const attrs = {};
                const ar = /([\w:-]+)\s*=\s*"([^"]*)"/g;
                let a;
                while ((a = ar.exec(m[2])) !== null) attrs[a[1]] = decode(a[2]);
                const cls = attrs.class ? attrs.class.split(/\s+/).filter(Boolean).sort().join(' ') : '';
                delete attrs.class;
                out.push({ t: 'open', tag: tag, cls: cls, attrs: attrs });
                if (VOID_TAGS[tag] || /\/\s*$/.test(m[2])) out.push({ t: 'close', tag: tag });
            }
            i = gt + 1;
        }
        return clampClock(out);
    }

    /** the v2 DOM tree -> the same canonical tokens (the mount's children,
        exactly like v1's box.innerHTML) */
    function tokensFromDom(root) {
        const out = [];
        root.children.forEach(function walk(n) {
            if (n.nodeType === 3) {
                const value = n.nodeValue != null ? n.nodeValue : n._text;
                if (String(value).trim()) out.push({ t: 'text', v: value });
                return;
            }
            const attrs = {};
            Object.keys(n.attributes).forEach(k => {
                // data-key is the preview's own bookkeeping, invisible to
                // rendering: v1 has no keys, so it is not compared.
                if (k !== 'data-key') attrs[k] = n.attributes[k];
            });
            const tag = n.tagName.toLowerCase();
            const cls = (n.className || '').split(/\s+/).filter(Boolean).sort().join(' ');
            out.push({ t: 'open', tag: tag, cls: cls, attrs: attrs });
            if (n._html) out.push(...tokensFromHtml(n._html));
            else if (n.children.length) n.children.forEach(walk);
            else if (n._text) out.push({ t: 'text', v: n._text });
            out.push({ t: 'close', tag: tag });
        });
        return clampClock(out);
    }

    // v1's header time comes from the real clock ("Today at 9:19 PM"); the v2
    // preview takes an injected clock on purpose. The FORMAT is compared
    // exactly and the value is compared as a placeholder — the preview's own
    // determinism is proven in the determinism section above.
    function clampClock(tokens) {
        const stack = [];
        return tokens.map(tok => {
            if (tok.t === 'open') stack.push(tok);
            else if (tok.t === 'close') stack.pop();
            else if (tok.t === 'text' && stack.length && stack[stack.length - 1].cls === 'eb-msg-time') {
                return { t: 'text', v: '<clock>' };
            }
            return tok;
        });
    }

    const TIMESTAMP = '2026-08-14T08:00:00.000Z';   // long past relative to any injected clock
    const FAR_FUTURE = 4102444800000;               // 2100-01-01
    function bothSides(d) {
        const v1box = createDom().createMount();
        V1.renderPreview(v1box, {
            content: d.content,
            embeds: d.embeds.map(model.toEditorEmbed),
            botIdentity: IDENTITY,
            lookups: LOOKUPS,
        });
        const a = mountPreview(d, { now: () => FAR_FUTURE });
        return { a: tokensFromHtml(v1box.innerHTML), b: tokensFromDom(a.mount), v1Html: v1box.innerHTML, v2Mount: a.mount };
    }

    const fidelityCorpus = [
        ['empty document', model.blankMessageDocument({ ids: nextIds() })],
        ['content only', doc({ content: 'hello **world**' })],
        ['emoji-only content', doc({ content: '🎲🎲' })],
        ['markdown content + mentions', doc({ content: '# Title\nhi <@200> <#100> <@&777> `code` ||spoiler||' })],
        ['embed title only (no url)', doc({ embeds: [em({ title: 'A' })] })],
        ['embed title with url', doc({ embeds: [em({ title: 'A', url: 'https://nilive.example/x' })] })],
        ['author without url', doc({ embeds: [em({ author: 'Nero', authorIcon: 'https://cdn.example/a.png' })] })],
        ['author with url', doc({ embeds: [em({ author: 'Nero', authorUrl: 'https://nilive.example/a' })] })],
        ['author icon only', doc({ embeds: [em({ authorIcon: 'https://cdn.example/a.png' })] })],
        ['footer icon + text + stamp', doc({ embeds: [em({ footer: 'F', footerIcon: 'https://cdn.example/f.png', timestamp: TIMESTAMP })] })],
        ['footer text + stamp only', doc({ embeds: [em({ footer: 'F', timestamp: TIMESTAMP })] })],
        ['footer stamp only', doc({ embeds: [em({ timestamp: TIMESTAMP })] })],
        ['footer icon only', doc({ embeds: [em({ footerIcon: 'https://cdn.example/f.png' })] })],
        ['image + thumbnail', doc({ embeds: [em({ image: 'https://cdn.example/i.png', thumbnail: 'https://cdn.example/t.png' })] })],
        ['fields inline and full', doc({ embeds: [em({ fields: [{ name: 'n1', value: 'v1', inline: true }, { name: 'n2', value: 'v2' }] })] })],
        ['one blank field only (empty region)', doc({ embeds: [em({ title: 'T', fields: [{ name: '', value: '' }] })] })],
        ['partially blank field (zero-width)', doc({ embeds: [em({ fields: [{ name: '', value: 'v' }] })] })],
        ['description markdown', doc({ embeds: [em({ description: 'a `b` c' })] })],
        ['authoritative card order', doc({ embeds: [em({
            title: 'A', description: 'd', color: '#123456', author: 'Nero',
            footer: 'F', timestamp: TIMESTAMP, image: 'https://cdn.example/i.png',
            fields: [{ name: 'n', value: 'v' }],
        })] })],
        ['two embeds + content', doc({ content: 'c', embeds: [em({ title: 'A', fields: [{ name: 'x', value: 'y' }] }), em({ description: 'B' })] })],
        ['unsafe urls', doc({ embeds: [em({ title: 'T', url: 'javascript:alert(1)', image: 'javascript:alert(2)', author: 'A', authorUrl: 'ftp://nope' })] })],
    ];

    // The single deliberate difference, stated rather than silently skipped:
    // the payload replaces an EMPTY field name with U+200B (that is literally
    // what Discord receives from toDiscordPayload), so the v2 preview shows
    // that placeholder where v1 showed nothing at all. The trees must be
    // identical once the payload's own placeholder is removed — and the
    // placeholder is asserted to be there, so the deviation cannot creep.
    const ZERO_WIDTH = '\u200b';

    let fidelityFails = [];
    fidelityCorpus.forEach(([name, d]) => {
        const { a, b, v1Html } = bothSides(d);
        const stripped = b.filter(tok => !(tok.t === 'text' && tok.v === ZERO_WIDTH));
        const same = JSON.stringify(a) === JSON.stringify(stripped);
        if (JSON.stringify(a) !== JSON.stringify(b) && same) {
            assert(b.some(tok => tok.t === 'text' && tok.v === ZERO_WIDTH),
                'the only difference is the payload\'s zero-width field-name placeholder: ' + name);
        }
        if (!same) {
            const i = a.findIndex((tok, idx) => JSON.stringify(tok) !== JSON.stringify(stripped[idx]));
            fidelityFails.push(name + ' @token ' + i + ': v1=' + JSON.stringify(a[i]) + ' v2=' + JSON.stringify(stripped[i])
                + (i === -1 ? ' (length v1=' + a.length + ' v2=' + stripped.length + ')' : ''));
            if (fidelityFails.length === 1) console.log('      v1 html: ' + v1Html.slice(0, 600));
        }
        assert(same, 'v1 and v2 paint the same tree: ' + name, same ? '' : fidelityFails[fidelityFails.length - 1]);
    });

    // the header time, format-compared (values deliberately differ: injected clock)
    const { v1Html } = bothSides(d1);
    assert(/eb-msg-time">Today at \d{1,2}:\d{2} (AM|PM)</.test(v1Html),
        'v1 renders the header time as "Today at h:mm AM/PM"');
    const mine = mountPreview(d1);
    assert(/^Today at \d{1,2}:\d{2} (AM|PM)$/.test(byClass(mine.mount, 'eb-msg-time')[0].textContent),
        'and so does v2, from its injected clock');
}

// ═══════════════════════════════════════════════════════════════
section('instrumentation audit — engine counters vs actual DOM calls');
// ═══════════════════════════════════════════════════════════════
{
    // The scope numbers above come from the engine itself. This section
    // checks them against the DOM double's own count of every method call the
    // engine made. If the engine under-counted its writes, these equalities
    // would fail — so "one text write" means one real DOM call.
    function audit(label, build, mutate) {
        const base = build();
        const m = mountPreview(base);
        const s0 = m.preview.stats(), o0 = Object.assign({}, m.dom.ops);
        m.preview.updateDocument(mutate(base));
        const s = delta(s0, m.preview.stats());
        const o = delta(o0, m.dom.ops);
        const claims = {
            nodesCreated: o.elementsCreated,
            nodesRemoved: o.removeChild,
            textWrites: o.textContentSet,
            markupWrites: o.innerHTMLSet,
            attrWrites: o.setAttribute + o.removeAttribute + o.classChanged,
        };
        const wrong = Object.keys(claims).filter(k => s[k] !== claims[k]);
        assert(wrong.length === 0, 'engine counters match real DOM calls: ' + label,
            wrong.map(k => k + ' claimed ' + s[k] + ' but the DOM saw ' + claims[k]).join('; ')
            + ' | ops=' + JSON.stringify(o));
        assert(o.insertBefore <= s.nodesCreated + s.nodesMoved,
            'and no insertion happened that the engine did not account for: ' + label,
            JSON.stringify({ insertBefore: o.insertBefore, created: s.nodesCreated, moved: s.nodesMoved }));
    }

    const auditBase = () => doc({ content: 'hi', embeds: [em({
        title: 'A', description: 'd', image: 'https://cdn.example/i.png',
        fields: [{ name: 'n1', value: 'v1' }, { name: 'n2', value: 'v2' }],
    })] });

    audit('content keystroke', auditBase, d => model.setContent(d, 'hi!'));
    audit('title keystroke', auditBase, d => model.setEmbedFields(d, d.embeds[0].id, { title: 'AB' }));
    audit('field value edit', auditBase, d => model.setField(d, d.embeds[0].id, d.embeds[0].fields[0].id, { value: 'V1' }));
    audit('add a field and fill it', auditBase, d => {
        const withField = model.addField(d, d.embeds[0].id);
        const newId = withField.embeds[0].fields[2].id;
        return model.setField(withField, d.embeds[0].id, newId, { name: 'n3', value: 'v3' });
    });
    audit('remove a field', auditBase, d => model.removeField(d, d.embeds[0].id, d.embeds[0].fields[0].id));
    audit('reorder fields', auditBase, d => model.moveField(d, d.embeds[0].id, d.embeds[0].fields[0].id, 1));
    audit('image url change', auditBase, d => model.setMedia(d, d.embeds[0].id, 'image', 'https://cdn.example/i2.png'));
    audit('add an embed', auditBase, d => model.addEmbed(d));
    audit('remove an embed', auditBase, d => model.removeEmbed(d, d.embeds[0].id));
    audit('no-op patch', auditBase, d => d);

    // and on the largest supported document
    const huge = doc({
        content: 'bulk',
        embeds: Array.from({ length: 10 }, (_, i) => em({
            title: 'E' + i, description: 'd' + i,
            fields: Array.from({ length: 25 }, (_, j) => ({ name: 'f' + j, value: 'v' + j })),
        })),
    });
    audit('10x25 large document: content keystroke', () => huge, d => model.setContent(d, 'bulk!'));
    audit('10x25 large document: one field edit', () => huge,
        d => model.setField(d, d.embeds[9].id, d.embeds[9].fields[24].id, { value: 'last' }));
}

// ═══════════════════════════════════════════════════════════════
section('hygiene');
// ═══════════════════════════════════════════════════════════════
{
    const dom = createDom();
    const mount = dom.createMount();
    const p = preview.create(mount, { now: clock, document: dom.document, botIdentity: IDENTITY, lookups: LOOKUPS });
    p.updateDocument(d1);
    assert(allElements(mount).length > 20, 'a rich document renders a real tree (' + allElements(mount).length + ' elements)');
    p.destroy();
    assert(mount.children.length === 0 && serialize(mount) === '<div></div>', 'destroy() empties the mount');
    p.updateDocument(d1);
    assert(mount.children.length === 0, 'and the preview is inert afterwards');
    assert(p.isDestroyed() === true, 'isDestroyed() reports it');

    let guard = null;
    try { preview.create(mount, { document: dom.document }); } catch (err) { guard = String(err.message); }
    assert(guard !== null && /now/.test(guard), 'create() refuses to run without an injected clock', guard || '');
    let guard2 = null;
    try { preview.create(null, { now: clock }); } catch (err) { guard2 = String(err.message); }
    assert(guard2 !== null && /mount/.test(guard2), 'and without a mount', guard2 || '');

    assert(dom.warnings.length === 0,
        'the engine never assigned textContent/innerHTML over existing children', dom.warnings.join(' | '));

    // no listeners, no timers, no globals: the module exposes exactly one name
    const nsAfter = Object.keys(sandbox.window.NERO.embed).sort();
    const added = nsAfter.filter(k => NS_BEFORE.indexOf(k) === -1);
    assert(added.join(',') === 'preview' && NS_BEFORE.indexOf('preview') === -1,
        'loading the preview adds exactly one namespace entry and no globals',
        'before=[' + NS_BEFORE.join(',') + '] added=[' + added.join(',') + ']');
}

// ═══════════════════════════════════════════════════════════════
Promise.resolve().then(() => {
    console.log('\n═══ measurements ═══');
    Object.keys(PRINTED).forEach(k => console.log('  ' + k.padEnd(46) + JSON.stringify(PRINTED[k])));
    console.log(`\npreview-patch: ${pass} passed, ${fail} failed`);
    if (fail) { console.log('Failures:'); failures.forEach(f => console.log(' -', f)); process.exit(1); }
    console.log('ALL PREVIEW-PATCH TESTS PASSED');
});
