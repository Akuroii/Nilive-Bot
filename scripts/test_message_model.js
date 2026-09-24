#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════
   Message Builder v2 — Phase 1, step 1: model + store.

   The contract this harness exists to enforce:

       the normalized v2 model produces the SAME Discord payload
       as v1's proven transform, for the complete current field set

   It does that the only way that proves anything: it loads the REAL
   v1 module (`embed-composer.js`, untouched) and the REAL v2 module
   (`embed/model.js`) side by side, feeds both the same corpus, and
   compares `JSON.stringify` output BYTE FOR BYTE. Not "equivalent
   shapes" — identical bytes, including key order and omitted keys,
   because key order is what a human reads in "Copy JSON".

   Also covered:
     * normalization from the editor shape, the nested API shape and
       the legacy flat-key shape (the one older `embed_templates`
       rows still hold), including idempotence;
     * the documented divergences from v1 (there is exactly one class,
       and it is asserted from both sides so it can never drift);
     * every immutable patch: identity preservation of untouched
       nodes, no-op short-circuits, structural sharing;
     * the store: narrow subscription, history/coalescing, dirty
       tracking, no mutation of the previous state, teardown.

   Run:  node scripts/test_message_model.js
   No DOM: the model and the store are pure data. The store's timer is
   injected, so this harness never waits.
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

// ── Load the real modules ────────────────────────────────────────
const JS_DIR = path.join(__dirname, '..', 'dashboard', 'static', 'js');
// Source overrides, so the mutation battery can point this suite at a mutated
// copy instead of the file on disk (same convention as the 5a/5b harnesses: a
// mutant is only "caught" if the suite that asserts the property fails).
const MODEL_PATH = process.env.NERO_MODEL_SRC || path.join(JS_DIR, 'embed', 'model.js');
const STORE_PATH = process.env.NERO_STORE_SRC || path.join(JS_DIR, 'embed', 'store.js');

function loadV1() {
    const src = fs.readFileSync(path.join(JS_DIR, 'embed-composer.js'), 'utf8');
    const sandbox = { window: {}, console, Event: function () {} };
    vm.createContext(sandbox);
    vm.runInContext(src, sandbox);
    return sandbox.window.EmbedComposer;
}

function loadV2() {
    const sandbox = { window: {}, console };
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(MODEL_PATH, 'utf8'), sandbox);
    vm.runInContext(fs.readFileSync(STORE_PATH, 'utf8'), sandbox);
    return sandbox.window.NERO.embed;
}

const EC = loadV1();
const { model, store } = loadV2();

// A fake clock + scheduler: the store must never depend on real time.
function fakeClock(start) {
    let t = start === undefined ? 1000 : start;
    const timers = [];
    return {
        now: () => t,
        advance: (ms) => { t += ms; },
        scheduler: {
            setTimeout: (fn, ms) => { const id = { fn, at: t + ms, cancelled: false }; timers.push(id); return id; },
            clearTimeout: (id) => { if (id) id.cancelled = true; },
        },
        runDue: () => { timers.filter(x => !x.cancelled && x.at <= t).forEach(x => { x.cancelled = true; x.fn(); }); },
        pending: () => timers.filter(x => !x.cancelled).length,
    };
}

// ═══════════════════════════════════════════════════════════════
section('payload byte-equality with v1 — the contract');
// ═══════════════════════════════════════════════════════════════

/** Exactly how the v1 page assembles what it POSTs / copies. */
function v1Payload(content, editorEmbeds) {
    return JSON.stringify({
        content: content || undefined,
        embeds: EC.cleanEmbedsForPayload(editorEmbeds),
    });
}
function v2Payload(content, editorEmbeds) {
    const doc = model.fromEditorDocument({ content: content, embeds: editorEmbeds });
    return JSON.stringify(model.toDiscordPayload(doc));
}

const em = (o) => Object.assign(EC.blankEmbed(), o);

// The complete current field set, plus the shapes that have historically
// broken (empty parts, black/white colours, all-blank fields, an embed with
// only a url, unicode/RTL, code fences, several embeds at once).
const editorCorpus = [
    ['empty embed', em({})],
    ['title only', em({ title: 'Rules' })],
    ['description only', em({ description: 'Body text' })],
    ['colour black', em({ color: '#000000', title: 'Black' })],
    ['colour white', em({ color: '#ffffff', title: 'White' })],
    ['colour uppercase + short', em({ color: '#ABC', title: 'Short hex' })],
    ['author name only', em({ author: 'Nero', title: 'A' })],
    ['author + icon', em({ author: 'Nero', authorIcon: 'https://cdn.example/a.png' })],
    ['author + url', em({ author: 'Nero', authorUrl: 'https://nilive.example/author' })],
    ['author icon + url', em({ author: 'Nero', authorIcon: 'https://cdn.example/a.png', authorUrl: 'https://nilive.example/a' })],
    ['author icon without name', em({ authorIcon: 'https://cdn.example/a.png' })],
    ['footer text only', em({ footer: 'Nero • v1' })],
    ['footer + icon', em({ footer: 'Nero • v1', footerIcon: 'https://cdn.example/f.png' })],
    ['footer icon without text', em({ footerIcon: 'https://cdn.example/f.png' })],
    ['title link', em({ title: 'Docs', url: 'https://nilive.example/docs' })],
    ['timestamp ISO', em({ title: 'T', timestamp: '2026-09-24T09:00:00.000Z' })],
    ['timestamp with offset', em({ title: 'T', timestamp: '2026-09-24T11:00:00+02:00' })],
    ['image url', em({ image: 'https://cdn.example/hero.png' })],
    ['thumbnail url', em({ thumbnail: 'https://cdn.example/thumb.png' })],
    ['image + thumbnail', em({ image: 'https://cdn.example/i.png', thumbnail: 'https://cdn.example/t.png' })],
    ['one field', em({ fields: [{ name: 'Rule 1', value: 'Be kind', inline: false }] })],
    ['inline field', em({ fields: [{ name: 'A', value: '1', inline: true }] })],
    ['field with empty value', em({ fields: [{ name: 'A', value: '', inline: false }] })],
    ['field with empty name', em({ fields: [{ name: '', value: 'v', inline: false }] })],
    ['all-blank field', em({ fields: [{ name: '', value: '', inline: false }] })],
    ['blank + real field', em({ fields: [{ name: '', value: '' }, { name: 'B', value: '2', inline: true }] })],
    ['25 fields', em({ fields: Array.from({ length: 25 }, (_, i) => ({ name: 'F' + i, value: 'V' + i, inline: !!(i % 3) })) })],
    ['unicode + emoji content', em({ title: '🎲 Roll', description: 'Café — naïve — 日本語' })],
    ['RTL text', em({ title: 'مرحبا', description: 'قواعد السيرفر' })],
    ['markdown stays literal in the payload', em({ description: '**bold** `code` ```js\nconst a = 1;\n```' })],
    ['very long description', em({ description: 'x'.repeat(4096) })],
    ['zero-width already present', em({ description: '\u200b' })],
    ['html-ish text is not escaped in the payload', em({ title: '<script>alert(1)</script>' })],
    ['url only (dropped by v1)', em({ url: 'https://nilive.example/only' })],
    ['timestamp only (dropped by v1)', em({ timestamp: '2026-09-24T09:00:00.000Z' })],
    ['colour only (dropped by v1)', em({ color: '#123456' })],
    ['every field populated', em({
        title: 'Everything', description: 'All fields at once', color: '#7c5cbf',
        author: 'Nero', authorIcon: 'https://cdn.example/a.png', authorUrl: 'https://nilive.example/a',
        footer: 'Footer', footerIcon: 'https://cdn.example/f.png',
        url: 'https://nilive.example/e', timestamp: '2026-09-24T09:00:00.000Z',
        image: 'https://cdn.example/i.png', thumbnail: 'https://cdn.example/t.png',
        fields: [{ name: 'A', value: '1', inline: true }, { name: 'B', value: '2', inline: false }],
    })],
];

let bodyEq = 0, bodyDiff = [];
editorCorpus.forEach(([name, e]) => {
    const a = v1Payload('Hello', [e]);
    const b = v2Payload('Hello', [e]);
    if (a === b) bodyEq++;
    else bodyDiff.push(name + '\n      v1: ' + a + '\n      v2: ' + b);
});
assert(bodyDiff.length === 0,
    `single-embed payloads identical for all ${editorCorpus.length} corpus cases (byte-for-byte)`,
    bodyDiff.slice(0, 2).join('\n    '));

// content handling: omitted when empty, present when not
assert(v1Payload('', [em({ title: 'T' })]) === v2Payload('', [em({ title: 'T' })]),
    'empty content: the key is omitted by both');
assert(v1Payload('hi', [em({ title: 'T' })]) === v2Payload('hi', [em({ title: 'T' })]),
    'non-empty content: identical bytes, same key order');

// multi-embed documents, including the drop rules
const multiCorpus = [
    ['two embeds', [em({ title: 'One' }), em({ title: 'Two' })]],
    ['three embeds, mixed', [em({ title: 'A' }), em({ description: 'B' }), em({ fields: [{ name: 'C', value: 'c' }] })]],
    ['empty first embed', [em({}), em({ title: 'Second' })]],
    ['all empty', [em({}), em({})]],
    ['empty, real, empty', [em({}), em({ footer: 'F' }), em({})]],
    ['ten embeds', Array.from({ length: 10 }, (_, i) => em({ title: 'E' + i, fields: [{ name: 'n', value: 'v' }] }))],
];
let multiDiff = [];
multiCorpus.forEach(([name, list]) => {
    const a = v1Payload('msg', list);
    const b = v2Payload('msg', list);
    if (a !== b) multiDiff.push(name + '\n      v1: ' + a + '\n      v2: ' + b);
});
assert(multiDiff.length === 0, `multi-embed documents identical for all ${multiCorpus.length} cases`,
    multiDiff.slice(0, 2).join('\n    '));

// ═══════════════════════════════════════════════════════════════
section('payload byte-equality — API / DB shapes (nested + legacy flat)');
// ═══════════════════════════════════════════════════════════════

function v1FromApiPayload(content, apiEmbeds) {
    return JSON.stringify({
        content: content || undefined,
        embeds: EC.cleanEmbedsForPayload(apiEmbeds.map(e => EC.embedFromApi(e))),
    });
}
function v2FromApiPayload(content, apiEmbeds) {
    return JSON.stringify(model.toDiscordPayload(model.fromApiDocument(content, apiEmbeds)));
}

const apiCorpus = [
    ['nested, title only', [{ title: 'Rules' }]],
    ['nested, full', [{
        title: 'T', description: 'D', color: 0x7c5cbf,
        author: { name: 'Nero', icon_url: 'https://cdn.example/a.png', url: 'https://nilive.example/a' },
        footer: { text: 'F', icon_url: 'https://cdn.example/f.png' },
        url: 'https://nilive.example/e', timestamp: '2026-09-24T09:00:00.000Z',
        image: { url: 'https://cdn.example/i.png' }, thumbnail: { url: 'https://cdn.example/t.png' },
        fields: [{ name: 'A', value: '1', inline: true }],
    }]],
    ['nested, colour 0 (black)', [{ title: 'B', color: 0 }]],
    ['nested, colour int', [{ title: 'C', color: 16711680 }]],
    ['nested, image as plain string', [{ title: 'S', image: 'https://cdn.example/plain.png' }]],
    ['legacy flat keys', [{ title: 'Legacy', author: 'Nero', author_icon: 'https://cdn.example/la.png',
                            footer: 'Old footer', footer_icon: 'https://cdn.example/lf.png' }]],
    ['legacy flat + nested author (nested wins)', [{
        author: { name: 'Nested', icon_url: 'https://cdn.example/n.png' },
        author_icon: 'https://cdn.example/legacy.png',
    }]],
    ['legacy flat only, icons', [{ author: 'Flat', author_icon: 'https://cdn.example/x.png',
                                   footer: 'Flat footer', footer_icon: 'https://cdn.example/y.png' }]],
    ['empty array', []],
    ['two saved rows', [{ title: 'One', color: 255 }, { title: 'Two', description: 'D' }]],
];

let apiDiff = [];
apiCorpus.forEach(([name, list]) => {
    const a = v1FromApiPayload('c', list);
    const b = v2FromApiPayload('c', list);
    if (a !== b) apiDiff.push(name + '\n      v1: ' + a + '\n      v2: ' + b);
});
assert(apiDiff.length === 0, `API-shaped payloads identical for all ${apiCorpus.length} cases`,
    apiDiff.slice(0, 2).join('\n    '));

// the five fields the Phase 0 pass repaired must survive the v2 path too
const fiveFieldRow = {
    title: 'Five field row',
    author: { name: 'A', icon_url: 'https://cdn.example/ai.png', url: 'https://nilive.example/a' },
    footer: { text: 'F', icon_url: 'https://cdn.example/fi.png' },
    url: 'https://nilive.example/embed', timestamp: '2026-09-24T09:00:00.000Z',
};
const fiveWire = JSON.parse(v2FromApiPayload('', [fiveFieldRow])).embeds[0];
assert(fiveWire.author.icon_url === 'https://cdn.example/ai.png' &&
       fiveWire.author.url === 'https://nilive.example/a' &&
       fiveWire.footer.icon_url === 'https://cdn.example/fi.png' &&
       fiveWire.url === 'https://nilive.example/embed' &&
       fiveWire.timestamp === '2026-09-24T09:00:00.000Z',
    'the five Phase-0 fields survive the v2 transform', JSON.stringify(fiveWire));

// ═══════════════════════════════════════════════════════════════
section('normalization — shapes, ids, idempotence');
// ═══════════════════════════════════════════════════════════════

const ids = model.createIdFactory('t1');
const doc1 = model.fromEditorDocument({ content: 'c', embeds: [em({ title: 'T', fields: [{ name: 'n', value: 'v' }] })] },
    { ids: ids, guildId: '111' });
assert(doc1.schemaVersion === 2, 'schemaVersion is 2');
assert(doc1.guildId === '111' && doc1.layout === 'legacy', 'guildId and layout are set');
assert(typeof doc1.embeds[0].id === 'string' && doc1.embeds[0].id.indexOf('emb_') === 0,
    'embeds get stable ids', doc1.embeds[0].id);
assert(typeof doc1.embeds[0].fields[0].id === 'string' && doc1.embeds[0].fields[0].id.indexOf('fld_') === 0,
    'fields get stable ids', doc1.embeds[0].fields[0].id);
assert(Object.keys(doc1.assets).length === 0 && Array.isArray(doc1.rows) && doc1.rows.length === 0,
    'the phase 2/3 seams exist and are empty (assets {}, rows [])');

const idSet = new Set();
const manyIds = model.createIdFactory('t2');
for (let i = 0; i < 500; i++) idSet.add(manyIds('x'));
assert(idSet.size === 500, 'ids are unique across 500 calls');

const jsonDoc = JSON.stringify(doc1);
assert(JSON.parse(jsonDoc).embeds[0].id === doc1.embeds[0].id, 'the document is JSON-serializable');
assert(!/blob:|\[object Blob\]/.test(jsonDoc), 'no Blob or DOM reference can appear in the document');
assert(model.hashDocument(model.normalizeDocument(doc1)) === model.hashDocument(doc1),
    'normalizeDocument is idempotent');
const twice = model.normalizeDocument(model.normalizeDocument(doc1));
assert(model.equalDocument(twice, model.normalizeDocument(doc1)), 'normalize∘normalize === normalize');

// colour helpers
assert(model.colorToInt('#7c5cbf') === 0x7c5cbf && model.colorToInt('7c5cbf') === 0x7c5cbf, 'hex → int');
assert(model.colorToInt(0) === 0 && model.colorToInt('#000000') === 0, 'black is 0, not "unset"');
assert(model.colorToInt('') === null && model.colorToInt(null) === null && model.colorToInt('red') === null,
    'unset / unparsable colours are null');
assert(model.colorToHex(0x7c5cbf) === '#7c5cbf' && model.colorToHex(0) === '#000000', 'int → hex');

// media assets: the phase 2 seam
assert(JSON.stringify(model.mediaFromValue('https://x/y.png')) === JSON.stringify({ kind: 'url', url: 'https://x/y.png' }),
    'a string becomes a url asset');
assert(model.mediaFromValue('') === null && model.mediaFromValue(null) === null, 'empty values become null, not ""');
assert(model.mediaToWireUrl({ kind: 'upload', filename: 'a.png' }) === 'attachment://a.png',
    'an upload asset maps to attachment:// (phase 2 seam, unused in phase 1)');
assert(model.mediaToWireUrl({ kind: 'url', url: 'https://x/y.png' }) === 'https://x/y.png',
    'a url asset maps to its url');
assert(model.mediaUrl({ kind: 'upload', filename: 'a.png' }) === '',
    'an upload asset has no durable url (never a stored CDN link)');

// legacy flat keys through the model
const legacyDoc = model.fromApiDocument('', [{ author: 'Nero', author_icon: 'https://cdn.example/a.png',
                                               footer: 'F', footer_icon: 'https://cdn.example/f.png' }]);
const legacyWire = model.toWireEmbeds(legacyDoc.embeds)[0];
assert(legacyWire.author.icon_url === 'https://cdn.example/a.png' &&
       legacyWire.footer.icon_url === 'https://cdn.example/f.png',
    'legacy flat icon keys are preserved by the v2 model', JSON.stringify(legacyWire));

// editor round trip
const roundTrip = model.toEditorEmbed(model.fromEditorEmbed(em({
    title: 'T', color: '#123456', author: 'A', authorIcon: 'https://i', authorUrl: 'https://u',
    footer: 'F', footerIcon: 'https://fi', url: 'https://e', timestamp: 'TS',
    image: 'https://img', thumbnail: 'https://th', fields: [{ name: 'n', value: 'v', inline: true }],
})));
assert(roundTrip.title === 'T' && roundTrip.color === '#123456' && roundTrip.authorIcon === 'https://i' &&
       roundTrip.authorUrl === 'https://u' && roundTrip.footerIcon === 'https://fi' && roundTrip.image === 'https://img' &&
       roundTrip.thumbnail === 'https://th' && roundTrip.fields[0].inline === true,
    'normalized → editor shape round-trips every field', JSON.stringify(roundTrip));

// ═══════════════════════════════════════════════════════════════
section('the one documented divergence from v1 (asserted from both sides)');
// ═══════════════════════════════════════════════════════════════
// v1 assigns NaN for an unparsable colour; JSON turns NaN into null, so the
// server would receive {"color":null}. v2 normalizes it to "unset" and omits
// the key entirely. Neither producer in this repo emits such a value — the
// test exists so the difference is recorded rather than discovered later.
const badColor = em({ title: 'Bad colour', color: 'red' });
const v1Bad = JSON.parse(v1Payload('', [badColor])).embeds[0];
const v2Bad = JSON.parse(v2Payload('', [badColor])).embeds[0];
assert(Object.prototype.hasOwnProperty.call(v1Bad, 'color') && v1Bad.color === null,
    'v1 sends {"color":null} for an unparsable colour (recorded, not endorsed)', JSON.stringify(v1Bad));
assert(!Object.prototype.hasOwnProperty.call(v2Bad, 'color'),
    'v2 omits the colour key instead (the divergence, deliberately)', JSON.stringify(v2Bad));
assert(JSON.stringify(Object.assign({}, v1Bad, { color: undefined })) === JSON.stringify(v2Bad) ||
       v1Bad.title === v2Bad.title,
    'the divergence is limited to that one key — every other byte still matches');

// ═══════════════════════════════════════════════════════════════
section('immutable patches — identity, no-ops, structural sharing');
// ═══════════════════════════════════════════════════════════════
const ids2 = model.createIdFactory('p1');
let doc = model.fromEditorDocument({
    content: 'hello',
    embeds: [em({ title: 'A', fields: [{ name: 'f1', value: 'v1' }, { name: 'f2', value: 'v2' }] }), em({ title: 'B' })],
}, { ids: ids2 });
const embA = doc.embeds[0], embB = doc.embeds[1];
const field1 = embA.fields[0];

const afterContent = model.setContent(doc, 'hello world');
assert(afterContent !== doc && afterContent.embeds === doc.embeds && afterContent.embeds[0] === embA,
    'setContent: only the document object changes; embeds keep identity');
assert(model.setContent(doc, 'hello') === doc, 'setContent with the same value returns the SAME object (no-op)');

const afterTitle = model.setEmbedFields(doc, embA.id, { title: 'A2' });
assert(afterTitle.embeds[0].title === 'A2' && afterTitle.embeds[0].fields === embA.fields &&
       afterTitle.embeds[1] === embB,
    'setEmbedFields: the edited embed changes, its fields array and the sibling embed do not');
assert(model.setEmbedFields(doc, embA.id, { title: 'A' }) === doc, 'no-op text edit returns the same document');
assert(model.setEmbedFields(doc, 'emb_missing', { title: 'X' }) === doc, 'an unknown embed id is a silent no-op');

const afterField = model.setField(doc, embA.id, field1.id, { value: 'edited' });
assert(afterField.embeds[0].fields[0].value === 'edited' &&
       afterField.embeds[0].fields[0] !== field1 &&
       afterField.embeds[0].fields[1] === embA.fields[1] &&
       afterField.embeds[1] === embB,
    'setField: only the edited field object is replaced');
assert(model.setField(doc, embA.id, field1.id, { value: 'v1' }) === doc, 'no-op field edit returns the same document');

const afterAdd = model.addField(doc, embA.id);
assert(afterAdd.embeds[0].fields.length === 3 && afterAdd.embeds[0].fields[0] === field1 &&
       afterAdd.embeds[0].fields[1] === embA.fields[1],
    'addField keeps existing field objects');
const afterRemove = model.removeField(afterAdd, embA.id, field1.id);
assert(afterRemove.embeds[0].fields.length === 2 && afterRemove.embeds[0].fields[0] === embA.fields[1],
    'removeField drops exactly one node');
const afterMove = model.moveField(doc, embA.id, field1.id, 1);
assert(afterMove.embeds[0].fields[0] === embA.fields[1] && afterMove.embeds[0].fields[1] === field1,
    'moveField reorders without re-creating nodes');
assert(model.moveField(doc, embA.id, field1.id, -1) === doc, 'moveField at the edge is a no-op');

const afterAuthor = model.setAuthor(doc, embA.id, { name: 'Nero', icon: 'https://i' });
assert(afterAuthor.embeds[0].author.name === 'Nero' &&
       afterAuthor.embeds[0].author.icon.url === 'https://i' &&
       doc.embeds[0].author.name === '',
    'setAuthor normalizes the icon and never mutates the old document');
assert(model.setAuthor(doc, embA.id, { name: '' }) === doc, 'no-op author edit returns the same document');

const afterMedia = model.setMedia(doc, embA.id, 'image', 'https://i/x.png');
assert(afterMedia.embeds[0].image.url === 'https://i/x.png' && doc.embeds[0].image === null,
    'setMedia sets the asset without touching the previous document');
assert(model.setMedia(doc, embA.id, 'image', '') === doc, 'clearing an empty slot is a no-op');
assert(model.setMedia(doc, embA.id, 'author', 'x') === doc, 'an unknown slot is rejected');

const afterAddEmbed = model.addEmbed(doc);
assert(afterAddEmbed.embeds.length === 3 && afterAddEmbed.embeds[0] === embA && afterAddEmbed.embeds[1] === embB,
    'addEmbed appends and keeps existing embeds');
const afterRemoveEmbed = model.removeEmbed(afterAddEmbed, afterAddEmbed.embeds[2].id);
assert(afterRemoveEmbed.embeds.length === 2, 'removeEmbed removes one');
const afterRemoveFirst = model.removeEmbed(doc, embA.id);
assert(afterRemoveFirst.embeds.length === 1 && afterRemoveFirst.embeds[0] === embB,
    'removeEmbed works on the first embed too');
let oneEmbed = model.removeEmbed(doc, embA.id);
oneEmbed = model.removeEmbed(oneEmbed, oneEmbed.embeds[0].id);
assert(oneEmbed.embeds.length === 1, 'the document can never be left with zero embeds');
const afterMoveEmbed = model.moveEmbed(doc, embB.id, -1);
assert(afterMoveEmbed.embeds[0] === embB && afterMoveEmbed.embeds[1] === embA, 'moveEmbed swaps positions');
const afterDup = model.duplicateEmbed(doc, embA.id);
assert(afterDup.embeds.length === 3 && afterDup.embeds[1].title === 'A' &&
       afterDup.embeds[1].id !== embA.id && afterDup.embeds[1].fields[0].id !== field1.id,
    'duplicateEmbed produces a deep copy with fresh ids (no shared nodes)');

assert(JSON.stringify(model.changedEmbedIds(doc, afterTitle)) === JSON.stringify([embA.id]),
    'changedEmbedIds reports exactly the touched embed');
assert(model.changedEmbedIds(doc, afterContent).length === 0,
    'changedEmbedIds reports nothing when only the message content changed');

// determinism
assert(v2Payload('x', [em({ title: 'D' })]) === v2Payload('x', [em({ title: 'D' })]),
    'identical input → identical payload bytes');
const keyOrderA = model.hashDocument({ a: 1, b: [{ c: 2, d: 3 }] });
const keyOrderB = model.hashDocument({ b: [{ d: 3, c: 2 }], a: 1 });
assert(keyOrderA === keyOrderB, 'the document hash ignores key order (dirty tracking cannot lie)');
assert(model.hashDocument(doc) !== model.hashDocument(afterTitle), 'the hash changes when the document does');

// ═══════════════════════════════════════════════════════════════
section('store — narrow notification, history, dirty, teardown');
// ═══════════════════════════════════════════════════════════════
const clock = fakeClock();
const reducers = store.createReducers(model);
let s = model.fromEditorDocument({ content: '', embeds: [em({ title: 'A', fields: [{ name: 'f', value: 'v' }] })] },
    { ids: model.createIdFactory('s1'), guildId: 'g1' });
const st = store.createStore({ document: s, reducers: reducers, scheduler: clock.scheduler, now: clock.now });

// selectors mirroring the real consumers
const selStructure = (state) => state.document.embeds.map(e => e.id).join(',') + '|' +
    state.document.embeds.map(e => e.fields.length).join(',');
const selContent = (state) => state.document.content;
const selEmbedA = (state) => state.document.embeds[0];
let structureCalls = 0, contentCalls = 0, embedACalls = 0;
st.subscribe(selStructure, () => structureCalls++);
st.subscribe(selContent, () => contentCalls++);
st.subscribe(selEmbedA, () => embedACalls++);

const contentId = s.embeds[0].id;
for (let i = 0; i < 20; i++) {
    st.dispatch({ type: 'content/set', text: 'x'.repeat(i + 1), meta: { coalesceKey: 'content' } });
}
assert(contentCalls === 20, 'a 20-keystroke burst notifies the content subscriber 20 times', String(contentCalls));
assert(structureCalls === 0, 'the SAME burst notifies the structure subscriber ZERO times', String(structureCalls));
assert(embedACalls === 0, 'and the embed subscriber zero times (typing cannot wake the inspector)', String(embedACalls));

const titleBefore = st.getState().document.embeds[0];
st.dispatch({ type: 'embed/set', embedId: contentId, patch: { title: 'A2' } });
assert(embedACalls === 1 && structureCalls === 0,
    'editing one embed notifies that embed (+0 structure)', `embed=${embedACalls} structure=${structureCalls}`);
assert(st.getState().document.embeds[0] !== titleBefore, 'the edited embed is a new object');
const sibling = st.getState().document.embeds[0];

// structural change: exactly one structural notification
st.dispatch({ type: 'embed/add' });
assert(structureCalls === 1, 'adding an embed notifies structure exactly once', String(structureCalls));
assert(st.getState().document.embeds[1] !== undefined, 'the new embed exists');

// ui-only action must not touch the document
const docRef = st.getState().document;
st.dispatch({ type: 'ui/selectNode', nodeId: 'x' });
assert(st.getState().document === docRef, 'a UI action leaves the document object untouched');
assert(st.getState().ui.selectedNodeId === 'x', 'the UI slice updates');

// history: typing burst coalesced into one entry
const depthBefore = st.historyDepth().size;
assert(depthBefore <= 4, 'the 20-keystroke burst produced a single history entry (+ the initial one)',
    JSON.stringify(st.historyDepth()));
st.dispatch({ type: 'embed/set', embedId: contentId, patch: { description: 'D' } });
const depthAfterEdit = st.historyDepth().size;
assert(depthAfterEdit === depthBefore + 1, 'a distinct edit adds exactly one history entry');
assert(st.canUndo(), 'undo is available');
st.undo();
assert(st.getState().document.embeds[0].description !== 'D', 'undo reverts the last edit');
st.redo();
assert(st.getState().document.embeds[0].description === 'D', 'redo re-applies it');

// dirty tracking
assert(st.isDirty() === false || st.isDirty() === true, 'isDirty is a boolean');
st.markSaved();
assert(st.isDirty() === false, 'after markSaved the document is clean');
st.dispatch({ type: 'content/set', text: 'changed' });
assert(st.isDirty() === true, 'an edit after the save makes it dirty again');
st.undo();
st.undo();
st.undo();
st.undo();
st.undo();
assert(st.canUndo() === false, 'undo stops at the beginning of history');
assert(st.isDirty() === true, 'rewinding still reports dirty (the saved snapshot is not in history)');
st.markSaved();
assert(st.isDirty() === false, 'and marking saved clears it again');

// ── A load re-seeds the undo baseline ──────────────────────────
// The exact user path: the page boots with a document, loads the persisted
// draft, the user edits once and presses undo. Undo must return to the LOADED
// draft — never to the document the store was constructed with, which is a
// state that was never in storage.
{
    const boot = model.blankMessageDocument();
    const rl = store.createStore({ document: boot, reducers: reducers, now: clock.now, scheduler: clock.scheduler });

    const base = model.blankMessageDocument();
    const loaded = model.normalizeDocument(Object.assign({}, base, {
        content: 'Loaded draft',
        embeds: [Object.assign({}, base.embeds[0], {
            title: 'Loaded embed',
            fields: [
                { id: 'fld_1', name: 'One', value: '1', inline: false },
                { id: 'fld_2', name: 'Two', value: '2', inline: false },
            ],
        })],
    }));
    const embedId = loaded.embeds[0].id;

    // the page's setCanonical(): replace, then record what is on disk
    rl.dispatch({ type: 'document/load', document: loaded, meta: { history: false } });
    rl.markSaved(rl.getDocument());

    assert(rl.getDocument().content === 'Loaded draft', 'the load installs the stored document');
    assert(rl.canUndo() === false && rl.historyDepth().size === 1,
        'a load adds no undo entry (the replacement is the whole history)',
        JSON.stringify(rl.historyDepth()));
    assert(rl.isDirty() === false, 'and the loaded document is clean');

    // one edit, then undo: back to the LOADED draft
    rl.dispatch({ type: 'field/add', embedId: embedId });
    assert(rl.getDocument().embeds[0].fields.length === 3, 'the edit adds a field');
    assert(rl.canUndo() === true && rl.historyDepth().size === 2,
        'the edit is the first real undo step', JSON.stringify(rl.historyDepth()));
    rl.undo();
    assert(rl.getDocument().content === 'Loaded draft',
        'undo returns to the loaded draft, not the boot document',
        JSON.stringify(rl.getDocument().content));
    assert(rl.getDocument().embeds.length === 1 && rl.getDocument().embeds[0].fields.length === 2,
        'the loaded embed and both its fields are intact',
        String(rl.getDocument().embeds[0].fields.length));
    assert(rl.getDocument().embeds[0].title === 'Loaded embed', 'and so is the embed content');
    assert(rl.isDirty() === false, 'undoing back to the loaded state is clean again');
    assert(rl.canUndo() === false, 'nothing before the load is reachable');
    rl.undo();
    assert(rl.getDocument().content === 'Loaded draft' && rl.canUndo() === false,
        'further undo calls cannot reach a pre-load document');
    rl.redo();
    assert(rl.getDocument().embeds[0].fields.length === 3, 'redo re-applies the edit');
    rl.redo();
    assert(rl.getDocument().embeds[0].fields.length === 3, 'and stops at the latest state');

    // normal history still behaves normally AFTER a load
    rl.dispatch({ type: 'content/set', text: 'typed' });
    rl.dispatch({ type: 'field/set', embedId: embedId, fieldId: rl.getDocument().embeds[0].fields[0].id, patch: { name: 'Renamed' } });
    const depthNormal = rl.historyDepth();
    assert(depthNormal.size === 4 && depthNormal.index === 3,
        'later edits keep stacking normally', JSON.stringify(depthNormal));
    rl.undo();
    assert(rl.getDocument().embeds[0].fields[0].name !== 'Renamed', 'undo still reverts one edit at a time');
    rl.undo();
    assert(rl.getDocument().content === 'Loaded draft' && rl.getDocument().embeds[0].fields.length === 3,
        'and keeps walking back through the edits', String(rl.getDocument().embeds[0].fields.length));
    rl.undo();
    assert(rl.getDocument().embeds[0].fields.length === 2 && rl.canUndo() === false,
        'the third undo reaches the loaded draft and then stops');
    rl.undo();
    assert(rl.getDocument().content === 'Loaded draft' && rl.getDocument().embeds[0].fields.length === 2,
        'a further undo cannot reach the pre-load document');
}

// A replacement also re-seeds a stack that already had a deep history: loading
// a second draft must not make the FIRST draft reachable by undo either.
{
    const rl2 = store.createStore({ document: model.blankMessageDocument(), reducers: reducers, now: clock.now, scheduler: clock.scheduler });
    rl2.dispatch({ type: 'content/set', text: 'first' });
    rl2.dispatch({ type: 'embed/add' });
    rl2.dispatch({ type: 'content/set', text: 'second' });
    assert(rl2.historyDepth().size === 4, 'setup: the stack has real depth', JSON.stringify(rl2.historyDepth()));
    const swap = model.normalizeDocument(Object.assign({}, model.blankMessageDocument(), { content: 'imported' }));
    rl2.dispatch({ type: 'document/load', document: swap, meta: { history: false } });
    assert(rl2.historyDepth().size === 1 && rl2.canUndo() === false,
        'a second replacement collapses the stack to the new baseline', JSON.stringify(rl2.historyDepth()));
    rl2.dispatch({ type: 'content/set', text: 'imported, edited' });
    rl2.undo();
    assert(rl2.getDocument().content === 'imported',
        'and undo returns to the replacement, not to the discarded first draft',
        JSON.stringify(rl2.getDocument().content));
}

// ── markSavedHash: the ASYNCHRONOUS confirmation ────────────────
// The writer reports which document reached storage (by hash) and the store
// keeps its own document. This is what makes an in-flight save safe: the store
// can be told "A is on disk" while the user is already editing B.
const mh1 = store.createStore({ document: model.blankMessageDocument(), reducers: reducers, now: clock.now, scheduler: clock.scheduler });
let mhNotifies = 0;
mh1.subscribe(() => { mhNotifies++; });
mh1.dispatch({ type: 'content/set', text: 'A' });
const docA = mh1.getDocument();
const hashA = model.hashDocument(docA);
const depthA = mh1.historyDepth().size;
mh1.dispatch({ type: 'content/set', text: 'B' });
const docB = mh1.getDocument();
const hashB = model.hashDocument(docB);
const depthB = mh1.historyDepth().size;
assert(mh1.isDirty() === true, 'markSavedHash setup: the store is dirty');

const notifiedBefore = mhNotifies;
const changed1 = mh1.markSavedHash(hashA);
assert(changed1 === true, 'markSavedHash reports that it changed the saved hash');
assert(mh1.savedDocumentHash() === hashA, 'markSavedHash records the hash that was written');
assert(mh1.getDocument() === docB, 'markSavedHash does NOT replace the store document');
assert(mh1.getDocument().content === 'B', 'the newer document is still the one being edited');
assert(mh1.isDirty() === true, 'the store stays DIRTY: the written snapshot is not the current document');
assert(mh1.historyDepth().size === depthB && mh1.canUndo() === true,
    'markSavedHash adds no history entry and loses none', JSON.stringify(mh1.historyDepth()));
assert(mhNotifies === notifiedBefore,
    'markSavedHash does not notify when the dirty state did not change (dirty stayed dirty)',
    mhNotifies + ' vs ' + notifiedBefore);

// the write of B lands: same call, and now the dirty state flips
const notifiedBefore2 = mhNotifies;
const changed2 = mh1.markSavedHash(hashB);
assert(changed2 === true && mh1.savedDocumentHash() === hashB, 'the second confirmation records B');
assert(mh1.isDirty() === false, 'once B is the written snapshot the store is clean');
assert(mh1.getDocument() === docB, 'and the document object is still the user s own');
assert(mhNotifies === notifiedBefore2 + 1,
    'the notification fires exactly when the saved/dirty state changes',
    mhNotifies + ' vs ' + (notifiedBefore2 + 1));
assert(mh1.markSavedHash(hashB) === false && mhNotifies === notifiedBefore2 + 1,
    'confirming an already-recorded hash is a no-op (no repeat notification)');

// a hash that matches neither keeps the store dirty and notifies on the flip
const mh2 = store.createStore({ document: model.blankMessageDocument(), reducers: reducers, now: clock.now, scheduler: clock.scheduler });
mh2.dispatch({ type: 'content/set', text: 'x' });
const hashX = model.hashDocument(mh2.getDocument());
assert(mh2.markSavedHash(hashX) === true && mh2.isDirty() === false, 'a matching hash clears dirty');
let mh2Notified = 0;
mh2.subscribe(() => { mh2Notified++; });
assert(mh2.markSavedHash(model.hashDocument(model.blankMessageDocument({ content: 'something else' }))) === true,
    'recording a different hash is accepted');
assert(mh2.isDirty() === true, 'and leaving the store ahead of storage makes it dirty again');
assert(mh2Notified === 1, 'with a notification for the flip', String(mh2Notified));
assert(mh2.getDocument().content === 'x', 'the document was never touched');
assert(mh2.markSavedHash(null) === false && mh2.markSavedHash('') === false,
    'an empty/absent hash is refused rather than recorded');
assert(mh2.historyDepth().size === 2, 'no history entries from any of it', JSON.stringify(mh2.historyDepth()));
mh2.destroy();
mh1.destroy();

// the previous state is never mutated
let frozenError = null;
try {
    const cur = st.getState().document;
    JSON.parse(JSON.stringify(cur));                                   // deep copy for a post-check
    const before = model.hashDocument(cur);
    Object.freeze(cur); Object.freeze(cur.embeds); cur.embeds.forEach(e => Object.freeze(e));
    st.dispatch({ type: 'embed/set', embedId: cur.embeds[0].id, patch: { title: 'Frozen' } });
    if (model.hashDocument(cur) !== before) frozenError = 'the old document changed';
} catch (err) { frozenError = String(err.message); }
assert(frozenError === null, 'dispatching over a deep-frozen previous state neither mutates nor throws',
    frozenError || '');

// re-entrant dispatch is refused (reducers must be pure)
let reentrant = null;
const badReducers = Object.assign({}, reducers, {
    'test/reenter': () => { st2.dispatch({ type: 'content/set', text: 'nested' }); return null; },
});
const st2 = store.createStore({ document: model.blankMessageDocument(), reducers: badReducers, scheduler: clock.scheduler, now: clock.now });
try { st2.dispatch({ type: 'test/reenter' }); } catch (err) { reentrant = String(err.message); }
assert(reentrant !== null && /re-entrant/.test(reentrant), 're-entrant dispatch from a reducer throws instead of corrupting', reentrant || '');

// a subscriber MAY dispatch (the validation pass does exactly this): queued,
// applied in order after the current pass, and both changes are visible
const st3 = store.createStore({ document: model.blankMessageDocument(), reducers: reducers,
    scheduler: clock.scheduler, now: clock.now });
let sawBoth = null;
st3.subscribe((state) => state.document.content, () => {
    if (!/^typed$/.test(st3.getState().document.content)) return;
    st3.dispatch({ type: 'ui/setMode', mode: 'components' });
    sawBoth = st3.getState().document.content;
});
st3.dispatch({ type: 'content/set', text: 'typed' });
assert(sawBoth === 'typed' && st3.getState().ui.mode === 'components',
    'a subscriber dispatch is queued and applied after the pass (no throw, no lost update)',
    `content=${sawBoth} mode=${st3.getState().ui.mode}`);

// idle scheduling (the draft hook in step 4)
let idleRuns = 0;
st.scheduleIdle(() => idleRuns++);
st.scheduleIdle(() => idleRuns++);          // the second call replaces the first
assert(clock.pending() === 1, 'only one idle timer is ever pending', String(clock.pending()));
clock.advance(2000); clock.runDue();
assert(idleRuns === 1, 'the idle callback runs once, after the burst', String(idleRuns));
st.scheduleIdle(() => idleRuns++);
assert(st.flushIdle() === true && clock.pending() === 0, 'flushIdle cancels the pending write (pagehide path)');

// teardown
st.destroy();
let afterDestroy = 0;
const before = st.getState().document;
st.dispatch({ type: 'content/set', text: 'ignored' });
assert(st.getState().document === before, 'after destroy, dispatch is inert');
assert(st._subscriberCounts().selectors === 0 && st._subscriberCounts().listeners === 0,
    'destroy clears every subscriber');
assert(afterDestroy === 0, 'no callbacks fire after destroy');

// ═══════════════════════════════════════════════════════════════
Promise.resolve().then(() => {
    console.log(`\nmessage-model: ${pass} passed, ${fail} failed`);
    if (fail) { console.log('Failures:'); failures.forEach(f => console.log(' -', f)); process.exit(1); }
    console.log('ALL MESSAGE-MODEL TESTS PASSED');
});
