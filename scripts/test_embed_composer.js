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

// Phase 0: templates saved before this change are bare embed dicts using the
// FLAT keys the Discord-side cog reads (author/author_icon/footer/_icon).
// Reading only the nested API shape dropped those icons the moment a legacy
// template was loaded into the editor.
const legacy = EC.embedFromApi({
    title: 'Legacy Welcome', color: '#7c5cbf',
    author: 'Legacy Author', author_icon: 'https://cdn/author.png',
    footer: 'old footer', footer_icon: 'https://cdn/footer.png',
});
assert(legacy.author === 'Legacy Author' && legacy.authorIcon === 'https://cdn/author.png',
    'legacy flat author_icon → authorIcon', 'got ' + JSON.stringify([legacy.author, legacy.authorIcon]));
assert(legacy.footer === 'old footer' && legacy.footerIcon === 'https://cdn/footer.png',
    'legacy flat footer_icon → footerIcon', 'got ' + JSON.stringify([legacy.footer, legacy.footerIcon]));
const nestedWins = EC.embedFromApi({
    author: { name: 'A', icon_url: 'https://nested/a.png' }, author_icon: 'https://flat/a.png',
    footer: { text: 'F', icon_url: 'https://nested/f.png' }, footer_icon: 'https://flat/f.png',
});
assert(nestedWins.authorIcon === 'https://nested/a.png' && nestedWins.footerIcon === 'https://nested/f.png',
    'nested API shape wins over a flat key', 'got ' + JSON.stringify([nestedWins.authorIcon, nestedWins.footerIcon]));
assert(EC.embedFromApi({ author: 'A' }).authorIcon === '' && EC.embedFromApi({ footer: 'F' }).footerIcon === '',
    'no icon key anywhere → empty icon field');
const payloadShape = EC.cleanEmbedForPayload({
    author: 'A', authorIcon: 'https://i', authorUrl: 'https://u',
    footer: 'F', footerIcon: 'https://f', url: 'https://t', timestamp: 'TS',
});
assert(payloadShape.author && payloadShape.author.icon_url === 'https://i' && payloadShape.author.url === 'https://u',
    'payload keeps the Discord-shaped author object (icon + url inside it)',
    'got ' + JSON.stringify(payloadShape.author));
const payloadEmpty = EC.cleanEmbedForPayload({ author: '' });
assert(payloadEmpty.author === undefined && payloadEmpty.footer === undefined,
    'unset author/footer are omitted from the payload, not emitted empty');

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
// Code spans / fenced blocks.
//
// These assert the INTENDED rendering (Discord's rules), not "whatever the
// previous implementation produced" — the equivalence corpus above pins the
// shared behaviour, this section defines the behaviour code must have. Every
// case here was wrong in at least one of the two earlier implementations:
// the pre-Phase-0 renderer ran markdown inside code (`**x**` came out bold),
// and the first Phase-0 pass turned a fenced block into `<pre>js\n…`, let a
// long line widen the preview box, and leaked its own placeholder characters
// for `` ```x``` ``.
// ═══════════════════════════════════════════════════════════════
section('renderDiscordMarkup — code spans and fenced blocks');
const md = (s) => EC.renderDiscordMarkup(s, { lookups: LOOKUPS }).html;

// the info string is the language, never content
assert(md('```js\nconst a = 1;\n```') === '<pre class="eb-code-block"><code>const a = 1;</code></pre>',
    'fence with an info string: the language is dropped', md('```js\nconst a = 1;\n```'));
assert(md('```\nplain fence\n```') === '<pre class="eb-code-block"><code>plain fence</code></pre>',
    'fence without an info string', md('```\nplain fence\n```'));
assert(md('```markdown\n**not bold**\n```') === '<pre class="eb-code-block"><code>**not bold**</code></pre>',
    'markdown inside a fence stays literal', md('```markdown\n**not bold**\n```'));

// the three-by-three rule that comes with dedented fences (CommonMark): for
// a single-line body, drop the newline after the info string, not the text
assert(md('```js\nconst a = 1;```') === '<pre class="eb-code-block"><code>const a = 1;</code></pre>',
    'fence whose body does not end in a newline', md('```js\nconst a = 1;```'));
assert(md('```js\n```') === '<pre class="eb-code-block"><code></code></pre>',
    'empty fence', md('```js\n```'));
assert(md('```python\nx\n```\n```python\ny\n```')
    === '<pre class="eb-code-block"><code>x</code></pre>\n<pre class="eb-code-block"><code>y</code></pre>',
    'two fences keep their order', md('```python\nx\n```\n```python\ny\n```'));
assert(md('```\nnever closed') === '```\nnever closed',
    'an unclosed fence stays exactly as typed', md('```\nnever closed'));

// long lines: the block is its own box and the CSS above wraps it (the
// browser gate measures the box's width; this pins the markup it hooks onto).
// No `"`/`&`/`<` in the payload: those are escaped before the code pass, so
// the body is the escaped text by design, not the raw line.
const longLine = 'const aVeryLongVariableName = ' + 'x'.repeat(400) + ';';
const longHtml = md('```js\n' + longLine + '\n```');
assert(longHtml.indexOf('<pre class="eb-code-block"><code>') === 0 && longHtml.indexOf(longLine) > 0
    && longHtml.indexOf('</code></pre>') === longHtml.length - '</code></pre>'.length,
    'a long fenced line stays inside one <pre class="eb-code-block"><code>', longHtml.slice(0, 80));

// inline spans
assert(md('use `npm start` here') === 'use <code class="eb-code">npm start</code> here',
    'single-backtick inline code', md('use `npm start` here'));
assert(md('`` `x` ``') === '<code class="eb-code">`x`</code>',
    'double-backtick inline code holding a backtick', md('`` `x` ``'));
assert(md('`` a ` b ``') === '<code class="eb-code">a ` b</code>',
    'double-backtick span with an inner single run', md('`` a ` b ``'));
// a code span is at least as long as its opener, and may close on a LONGER
// run — otherwise `` `x` `` is two broken halves instead of one span
assert(md('`` ``` `x` ``` ``') === '<code class="eb-code">``` `x` ```</code>',
    'double-backtick span closed by a longer run', md('`` ``` `x` ``` ``'));
assert(md('a `` b') === 'a `` b', 'a run that never closes is left alone', md('a `` b'));

// markdown next to, and around, code
assert(md('**bold `code` bold**') === '<strong>bold <code class="eb-code">code</code> bold</strong>',
    'inline code inside bold keeps both', md('**bold `code` bold**'));
assert(md('`**x**` and **y**') === '<code class="eb-code">**x**</code> and <strong>y</strong>',
    'code next to live markdown', md('`**x**` and **y**'));
assert(md('`a` and `b`') === '<code class="eb-code">a</code> and <code class="eb-code">b</code>',
    'two inline spans keep their order', md('`a` and `b`'));
assert(md('`a` `b` `c`') === '<code class="eb-code">a</code> <code class="eb-code">b</code> <code class="eb-code">c</code>',
    'three inline spans keep their order', md('`a` `b` `c`'));
assert(md('`a` **b** `c`') === '<code class="eb-code">a</code> <strong>b</strong> <code class="eb-code">c</code>',
    'spans around other markdown keep their order', md('`a` **b** `c`'));
assert(md('x `**y**` z') === 'x <code class="eb-code">**y**</code> z',
    'markdown inside a span is literal, text around it is not', md('x `**y**` z'));
// KNOWN pre-existing deviation, deliberately NOT changed in Phase 0: a
// mention/emoji TOKEN inside a code span still resolves (Discord shows the
// token as literal text). Token stashing happens before the code pass — it
// dates from before this work and behaves the same at HEAD — and fixing it
// changes rendering for every consumer of this module, so it belongs to the
// Phase 1 markdown corpus, not to a stability commit. What is asserted here
// is the part these fixes own: the code span is still one span, the token
// stays inside it, and nothing leaks.
const tokenInCode = md('`<@200>` vs <@200>');
assert(tokenInCode.indexOf('<code class="eb-code">') === 0
    && tokenInCode.indexOf('</code>') > 0
    && tokenInCode.indexOf('</code>') > tokenInCode.indexOf('<@200>'.slice(0, 4))
    && tokenInCode.split('<code class="eb-code">').length === 2,
    'a token inside code stays inside its code span (resolution inside code is a known deviation)',
    tokenInCode);

// placeholders: the alphabet the renderer uses internally must never show up
const CTRL = /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/;
const leakCases = [
    '`` ``` `**x**` ``` ``',      // re-stashed its own placeholder before this pass
    '`` ```x``` ``',
    'a\u00030\u0004b',            // a placeholder that is genuinely in the input
    '`a\u0001b`',
    'x\u0007y',
    '\u0003 \u0004',
    '`code` \u0000 `more`',
];
leakCases.forEach((input, i) => {
    const out = md(input);
    assert(!CTRL.test(out), 'no control character leaks into the output (#'.concat(i, ')'),
        JSON.stringify(out));
});
// \u0003 0 \u0004 — the placeholder SHAPE, typed by a user. The control
// characters go; the digits between them are just text.
assert(md('a\u00030\u0004b') === 'a0b', 'a placeholder-looking input is scrubbed, not resolved',
    JSON.stringify(md('a\u00030\u0004b')));
assert(md('`` ``` `**x**` ``` ``') === '<code class="eb-code">``` `**x**` ```</code>',
    'the nested-run case keeps its content instead of a placeholder',
    md('`` ``` `**x**` ``` ``'));
// a body with a control character cannot smuggle a placeholder back in
assert(!CTRL.test(md('```\n' + '\u0003' + '0000' + '\u0004' + '\n```')),
    'a fenced body containing placeholder characters is scrubbed');

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
section('renderPreview — attachment slots are never silently dropped');
// Node has URL but no createObjectURL; stub it so the fallback cache path
// runs (the regression: one fresh blob: URL per attachment PER RENDER).
let _mints = 0, _revoked = 0;
sandbox.URL = {
    createObjectURL: (b) => { _mints++; return `blob:stub-${b && b.__id}`; },
    revokeObjectURL: () => { _revoked++; },
};
const blobA = { __id: 'a' };
const attImg = { id: 'x1', name: 'a.png', type: 'image/png', size: 10, blob: blobA };
const attTxt = { id: 'x2', name: 'log.txt', type: 'text/plain', size: 10 };

EC.renderPreview(box, { content: 'hi', embeds: [EC.blankEmbed()], attachments: [attImg, attTxt], lookups: LOOKUPS });
const firstHtml = box.innerHTML;
EC.renderPreview(box, { content: 'hi again', embeds: [EC.blankEmbed()], attachments: [attImg, attTxt], lookups: LOOKUPS });
assert(firstHtml.includes('blob:stub-a') && firstHtml.includes('<img'), 'image attachment uses a blob url in the preview');
assert(firstHtml.includes('badge') && firstHtml.includes('log.txt'), 'non-image attachment falls back to a badge');
assert(_mints === 1, 'fallback cache mints ONE url across two renders (no per-keystroke leak)', `mints=${_mints}`);

// A page that owns its attachment lifecycle wins, and the module must not
// mint anything of its own behind the page's back.
_mints = 0; let resolverCalls = 0;
const attOwned = { id: 'x3', name: 'b.png', type: 'image/png', blob: blobA, _previewUrl: 'blob:page-owned' };
EC.renderPreview(box, {
    content: 'c', embeds: [EC.blankEmbed()], attachments: [attOwned], lookups: LOOKUPS,
    attachmentPreviewUrl: (a) => { resolverCalls++; return a._previewUrl || null; },
});
assert(box.innerHTML.includes('blob:page-owned'), 'attachmentPreviewUrl resolver is used');
assert(resolverCalls === 1 && _mints === 0, 'resolver path bypasses the module cache entirely');

// An async preview in flight must still occupy a slot — the file is neither
// missing nor failed, and "the attachment disappeared" starts here.
EC.renderPreview(box, {
    content: 'c', embeds: [EC.blankEmbed()], lookups: LOOKUPS,
    attachments: [{ id: 'x4', name: 'c.png', type: 'image/png', _previewPending: true }],
    attachmentPreviewUrl: () => null,
});
assert(box.innerHTML.includes('data-preview-state="pending"') && box.innerHTML.includes('c.png'),
    'pending attachment keeps a visible slot instead of vanishing');
sandbox.URL = URL; // restore

// ═══════════════════════════════════════════════════════════════
Promise.resolve().then(() => {
console.log(`\nembed-composer: ${pass} passed, ${fail} failed`);
if (fail) { console.log('Failures:'); failures.forEach(f => console.log(' -', f)); process.exit(1); }
console.log('ALL EMBED-COMPOSER TESTS PASSED');
});
