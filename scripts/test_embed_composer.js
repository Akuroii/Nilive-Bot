#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════
   Minigames v2 — Phase 4 — embed-composer.js unit tests (node)

   Verifies the extracted shared composer:
     * escaping matrix
     * blankEmbed / embedHasContent
     * cleanEmbedForPayload / cleanEmbedsForPayload (shape the
       /send endpoint and minigame template API accept)
     * embedFromApi normalization (API shape → editor shape)
     * renderDiscordMarkup — equivalence against the OLD inline
       implementation that lived in embedbuilder.html (the
       reference behavior) on a corpus of real inputs
     * componentRowsHtml — engine component JSON → action-row HTML
     * renderPreview integration (bot chrome, embeds, components)

   Run:  node scripts/test_embed_composer.js
   No DOM: the module is plain JS; a minimal window shim is
   enough (it only assigns window.EmbedComposer).
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

// ── Load the module with a window shim ───────────────────────────
const src = fs.readFileSync(
    path.join(__dirname, '..', 'dashboard', 'static', 'js', 'embed-composer.js'), 'utf8');
const sandbox = { window: {}, console, Event: function () {} };
vm.createContext(sandbox);
vm.runInContext(src, sandbox);
const EC = sandbox.window.EmbedComposer;

// ═══════════════════════════════════════════════════════════════
// The OLD inline implementation from embedbuilder.html (pre-
// refactor) — the reference. Globals it read are shimmed here.
// ═══════════════════════════════════════════════════════════════
const roleMap = {};
const channelMap = { '100': { name: 'announcements' } };
const userNameCache = { '200': 'tester' };
function oldRenderToken(match) {
    let m;
    const TOKEN_RE = /<(a?):(\w+):(\d+)>|<#(\d+)>|<@&(\d+)>|<@!?(\d+)>/g;
    TOKEN_RE.lastIndex = 0;
    m = TOKEN_RE.exec(match);
    if (!m) return oldEsc(match);
    const [, animFlag, ename, eid, chid, rid, uid] = m;
    if (eid) return `<img class="eb-inline-emoji" src="https://cdn.discordapp.com/emojis/${eid}.${animFlag ? 'gif' : 'png'}" alt=":${oldEsc(ename)}:`;
    if (chid) { const ch = channelMap[chid]; return `<span class="eb-mention">#${oldEsc(ch ? ch.name : chid)}</span>`; }
    if (rid) {
        const role = roleMap[rid]; const label = role ? role.name : rid;
        let style = '';
        if (role && role.color) style = ` style="background:${role.color}33;color:${role.color};"`;
        return `<span class="eb-mention"${style}>@${oldEsc(label)}</span>`;
    }
    if (uid) {
        if (userNameCache[uid]) return `<span class="eb-mention">@${oldEsc(userNameCache[uid])}</span>`;
        return `<span class="eb-mention">@${oldEsc(uid)}</span>`;
    }
    return oldEsc(match);
}
function oldEsc(s) {
    return String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
function oldRenderDiscordMarkup(text) {
    if (!text) return { html: '', isEmojiOnly: false };
    const tokens = [];
    let working = text.replace(/<(a?):(\w+):(\d+)>|<#(\d+)>|<@&(\d+)>|<@!?(\d+)>/g, (match) => {
        tokens.push(match); return `\u0001${tokens.length - 1}\u0002`;
    });
    let isEmojiOnly = false;
    const EMOJI_UNICODE_RE = /\p{Extended_Pictographic}(\u200d\p{Extended_Pictographic})*\ufe0f?/gu;
    const stripped = working.replace(/\u0001\d+\u0002/g, '').replace(EMOJI_UNICODE_RE, '').trim();
    isEmojiOnly = stripped.length === 0 && (tokens.length + (text.match(EMOJI_UNICODE_RE) || []).length) > 0;
    let escaped = oldEsc(working);
    escaped = escaped.replace(/\*\*([\s\S]+?)\*\*/g, '<strong>$1</strong>');
    escaped = escaped.replace(/__([\s\S]+?)__/g, '<u>$1</u>');
    escaped = escaped.replace(/\*([\s\S]+?)\*/g, '<em>$1</em>');
    escaped = escaped.replace(/~~([\s\S]+?)~~/g, '<s>$1</s>');
    if (isEmojiOnly) escaped = escaped.replace(EMOJI_UNICODE_RE, (m) => `<span>${m}</span>`);
    escaped = escaped.replace(/\u0001(\d+)\u0002/g, (m, idx) => oldRenderToken(tokens[parseInt(idx)]));
    return { html: escaped, isEmojiOnly };
}

const LOOKUPS = {
    roles: roleMap,
    channels: channelMap,
    users: userNameCache,
    onUserResolve: null,
};

// ═══════════════════════════════════════════════════════════════
section('escaping matrix');
assert(EC.esc('a<b>&"\'') === 'a&lt;b&gt;&amp;&quot;&#39;', 'esc all five specials',
    'got: ' + EC.esc('a<b>&"\''));
assert(EC.esc(null) === '' && EC.esc(undefined) === '', 'esc null/undefined → empty');
assert(EC.esc(42) === '42', 'esc number → string');
assert(EC.attr('x"y') === 'x&quot;y', 'attr escapes quotes');

// ═══════════════════════════════════════════════════════════════
section('blankEmbed / embedHasContent');
const b = EC.blankEmbed();
assert(b.title === '' && b.description === '' && b.color === '#7c5cbf' && Array.isArray(b.fields) && b.fields.length === 0,
    'blankEmbed defaults');
assert(EC.embedHasContent(b) === false, 'blank embed is empty');
assert(EC.embedHasContent({ ...b, description: 'x' }) === true, 'description counts');
assert(EC.embedHasContent({ ...b, fields: [{ name: 'n', value: 'v' }] }) === true, 'fields count');

// ═══════════════════════════════════════════════════════════════
section('cleanEmbedForPayload / cleanEmbedsForPayload');
const cleaned = EC.cleanEmbedForPayload({
    title: 'T', description: 'D', color: '#5865f2', author: 'A',
    footer: 'F', thumbnail: 'https://t', image: 'https://i',
    fields: [ { name: 'n1', value: 'v1', inline: true }, { name: '', value: '', inline: false } ],
});
assert(cleaned.color === parseInt('5865f2', 16), 'color hex → int', 'got ' + cleaned.color);
assert(cleaned.author.name === 'A' && cleaned.footer.text === 'F', 'author/footer wrapped');
assert(cleaned.image.url === 'https://i' && cleaned.thumbnail.url === 'https://t', 'image/thumbnail wrapped');
assert(cleaned.fields.length === 1 && cleaned.fields[0].inline === true, 'empty field dropped, inline kept');
assert(EC.cleanEmbedsForPayload([EC.blankEmbed()]).length === 0, 'empty embeds filtered out');
assert(EC.cleanEmbedsForPayload([b, { ...b, title: 'x' }]).length === 1, 'non-empty kept');
const noColor = EC.cleanEmbedForPayload({ title: 'x' });
assert(noColor.color === undefined, 'no color key when color empty');

// ═══════════════════════════════════════════════════════════════
section('embedFromApi normalization');
const norm = EC.embedFromApi({
    color: parseInt('7c5cbf', 16),
    author: { name: 'Auth' }, footer: { text: 'Foot' },
    image: { url: 'https://i' }, thumbnail: { url: 'https://t' },
    fields: [{ name: 'n', value: 'v', inline: 0 }],
});
assert(norm.color === '#7c5cbf', 'int color → hex string', 'got ' + norm.color);
assert(norm.author === 'Auth' && norm.footer === 'Foot', 'dict author/footer → strings');
assert(norm.image === 'https://i' && norm.thumbnail === 'https://t', 'dict urls → strings');
assert(norm.fields[0].inline === false, 'inline coerced to bool');
const normMissing = EC.embedFromApi({});
assert(normMissing.color === '#7c5cbf' && normMissing.fields.length === 0, 'missing fields → defaults');
assert(EC.embedsFromApi([]).length === 1 && EC.embedsFromApi([])[0].color === '#7c5cbf',
    'empty API list → one blank embed');

// ═══════════════════════════════════════════════════════════════
section('renderDiscordMarkup — equivalence with the old implementation');
const corpus = [
    '',
    'plain text',
    '**bold** and *italic* and __under__ and ~~strike~~',
    'mix **b *i* b** end',
    'channel <#100> unresolved <#9999999>',
    'role <@&777> and user <@200> unresolved <@99999999999999999>',
    'emoji <a:spin:123> and <:static:456>',
    'unicode 🎲 and mixed <#100> 🎲',
    '🎲',                          // emoji-only line
    '<:only:111>',                  // token-only line
    'no tokens at all',
    '<@200> said **hi** <#100> 🎲',
    'quote "q" & amp <tag> test',
    'multi\nline **bold** here',
];
let eq = true, firstDiff = '';
for (const input of corpus) {
    const oldR = oldRenderDiscordMarkup(input);
    const newR = EC.renderDiscordMarkup(input, { checkEmojiOnly: true, lookups: LOOKUPS });
    if (oldR.html !== newR.html || oldR.isEmojiOnly !== newR.isEmojiOnly) {
        eq = false;
        firstDiff = JSON.stringify(input) + '\n    old: ' + JSON.stringify(oldR) + '\n    new: ' + JSON.stringify(newR);
        break;
    }
}
assert(eq, 'markup identical to old impl on ' + corpus.length + ' inputs', firstDiff);

// targeted behaviors (belt & braces beyond equivalence)
assert(EC.renderDiscordMarkup('**b**', { lookups: LOOKUPS }).html === '<strong>b</strong>', 'bold');
assert(EC.renderDiscordMarkup('🎲', { checkEmojiOnly: true, lookups: LOOKUPS }).isEmojiOnly === true,
    'emoji-only detection');
assert(EC.renderDiscordMarkup('🎲 text', { checkEmojiOnly: true, lookups: LOOKUPS }).isEmojiOnly === false,
    'emoji + text is NOT emoji-only');
const ch = EC.renderDiscordMarkup('go <#100>', { lookups: LOOKUPS }).html;
assert(ch === 'go <span class="eb-mention">#announcements</span>', 'channel mention resolved', ch);

// ═══════════════════════════════════════════════════════════════
section('componentRowsHtml — engine component JSON → action rows');
const engineRow = [
    { type: 2, label: '1', style: 2, disabled: true, custom_id: 'qc_0' },
    { type: 2, label: '2', style: 2, disabled: true, custom_id: 'qc_1' },
];
const html = EC.componentRowsHtml([engineRow]);
assert((html.match(/eb-comp-btn/g) || []).length === 2, 'two buttons');
assert(html.includes('eb-cbtn-secondary'), 'secondary class mapped');
assert((html.match(/ disabled/g) || []).length === 2, 'disabled state rendered');
assert(EC.componentRowsHtml([]) === '' && EC.componentRowsHtml(null) === '', 'empty → no HTML');
const styled = EC.componentRowsHtml([[
    { label: 'A', style: 1, disabled: false },
    { label: 'B', style: 3, disabled: true, emoji: '🎡' },
    { label: 'C', style: 4, disabled: false },
    { label: '<script>', style: 2, disabled: false },
]]);
assert(styled.includes('eb-cbtn-primary') && styled.includes('eb-cbtn-success') && styled.includes('eb-cbtn-danger'),
    'style 1/3/4 mapped');
assert(styled.includes('🎡'), 'emoji rendered');
assert(!styled.includes('<script>') && styled.includes('&lt;script&gt;'), 'labels escaped');
// <a:name:id> emoji token → CDN img
const tokHtml = EC.componentRowsHtml([[{ label: 'x', style: 2, disabled: false, emoji: '<a:spin:123>' }]]);
assert(tokHtml.includes('https://cdn.discordapp.com/emojis/123.gif'), 'animated token → gif url');
const statTok = EC.componentRowsHtml([[{ label: 'x', style: 2, disabled: false, emoji: '<:still:456>' }]]);
assert(statTok.includes('https://cdn.discordapp.com/emojis/456.png'), 'static token → png url');

// ═══════════════════════════════════════════════════════════════
section('renderPreview — full message chrome');
// renderPreview needs a box element; a minimal DOM shim is enough
// because it only sets .innerHTML.
const box = { innerHTML: '' };
EC.renderPreview(box, {
    content: 'hello <#100> **world**',
    embeds: [{ ...EC.blankEmbed(), title: 'T', description: 'D **b**', fields: [{ name: 'n', value: 'v' }] }],
    botIdentity: { name: 'Nero', avatar: 'https://a.png' },
    lookups: LOOKUPS,
    components: [[{ label: 'Join', style: 1, disabled: false, emoji: '🎡' }]],
});
assert(box.innerHTML.includes('eb-msg-avatar') && box.innerHTML.includes('Nero'), 'bot chrome');
assert(box.innerHTML.includes('#announcements'), 'mention resolved in preview');
assert(box.innerHTML.includes('<strong>b</strong>'), 'embed desc markdown');
assert(box.innerHTML.includes('eb-comp-btn') && box.innerHTML.includes('Join'), 'component rows in preview');
assert(box.innerHTML.includes('eb-pe-field'), 'fields rendered');

// empty state
EC.renderPreview(box, { content: '', embeds: [EC.blankEmbed()], lookups: LOOKUPS });
assert(box.innerHTML.includes('eb-preview-empty'), 'empty placeholder shown');

// ═══════════════════════════════════════════════════════════════
console.log(`\nembed-composer: ${pass} passed, ${fail} failed`);
if (fail) { console.log('Failures:'); failures.forEach(f => console.log(' -', f)); process.exit(1); }
console.log('ALL EMBED-COMPOSER TESTS PASSED');
