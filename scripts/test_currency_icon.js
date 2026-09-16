#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════
   Currency icons — the client-side twin (dashboard.js)

   The dashboard builds several currency labels in JS rather than
   server-side: currencyLabel() / currencyAmount() feed innerHTML on
   the Leveling and Trade pages, and the minigame reward pickers use
   the text twin because an <option> may only contain text.

   The class of bug this locks down: a configured currency icon can be
   a Discord custom emoji (`<:name:id>` / the animated `<a:name:id>`).
   Interpolated straight into innerHTML, a browser parses that as the
   start of an <a> tag and the icon silently disappears. So the icon
   must become the image Discord's emoji CDN serves by ID (animated →
   .gif, static → .png), while a unicode emoji stays plain text.

   Run:  node scripts/test_currency_icon.js
   No DOM library: dashboard.js is loaded into a vm sandbox with the
   handful of browser APIs it touches at load time.
   ═══════════════════════════════════════════════════════════════ */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');

let pass = 0, fail = 0; const failures = [];
function assert(cond, name, extra) {
    if (cond) { pass++; console.log('  PASS', name); }
    else { fail++; failures.push(name + (extra ? ' — ' + extra : ''));
           console.log('  FAIL', name, extra || ''); }
}
function section(t) { console.log('\n== ' + t + ' =='); }

const SRC = fs.readFileSync(
    path.join(__dirname, '..', 'dashboard', 'static', 'js', 'dashboard.js'),
    'utf8');

// ── the smallest browser that dashboard.js will load in ───────────────
function escapeHtml(s) {
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function load(currency) {
    const sandbox = {
        console,
        Headers: class Headers {
            constructor(h) { Object.assign(this, h || {}); }
            set(k, v) { this[k] = v; }
        },
        window: {
            fetch: () => Promise.resolve({}),
            __CSRF_TOKEN__: 'tok',
            __CHECK_ICON__: '✅',
            __CURRENCY__: currency,
            location: { href: 'http://x/' },
        },
        document: {
            addEventListener() {},
            removeEventListener() {},
            createElement() {
                const el = { _t: '', style: {}, classList: { add() {}, remove() {} } };
                Object.defineProperty(el, 'textContent', {
                    get() { return this._t; },
                    set(v) { this._t = v; this._html = escapeHtml(v); },
                });
                Object.defineProperty(el, 'innerHTML', {
                    get() { return this._html !== undefined ? this._html : ''; },
                    set(v) { this._html = v; },
                });
                return el;
            },
            getElementById: () => null,
            querySelector: () => null,
            querySelectorAll: () => [],
            documentElement: { classList: { add() {}, remove() {}, toggle() {} } },
            body: { appendChild() {} },
        },
        setTimeout, clearTimeout, setInterval, clearInterval,
        fetch: () => Promise.resolve({}),
        localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    };
    sandbox.window.document = sandbox.document;
    sandbox.globalThis = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(SRC, sandbox, { filename: 'dashboard.js' });
    return sandbox;
}

const STATIC_ID = '1549831102078787001';
const ANIM_ID = '1549831102078787002';

const CONFIGS = {
    'defaults': { coins: { key: 'balance', name: 'Coins', emoji: '🪙' },
                  diamonds: { key: 'diamonds', name: 'Diamonds', emoji: '💎' } },
    'unicode': { coins: { key: 'balance', name: 'Moon', emoji: '🌙' },
                 diamonds: { key: 'diamonds', name: 'Gems', emoji: '💠' } },
    'static custom': {
        coins: { key: 'balance', name: 'Moon', emoji: `<:moon:${STATIC_ID}>` },
        diamonds: { key: 'diamonds', name: 'Gems', emoji: `<:gem:${STATIC_ID}>` } },
    'animated custom': {
        coins: { key: 'balance', name: 'Moon', emoji: `<a:moon:${ANIM_ID}>` },
        diamonds: { key: 'diamonds', name: 'Gems', emoji: `<a:gem:${ANIM_ID}>` } },
};

// A Discord token swallowed as markup becomes a start tag named after the
// token itself. That — not "does the string contain <img>" — is the bug.
const TOKEN_TAG = /^a?:\w+:\d+$/;
function startTags(html) {
    return [...html.matchAll(/<\s*([^\s/>]+)/g)].map(m => m[1]);
}
function swallowedAsTag(html) {
    return startTags(html).filter(t => TOKEN_TAG.test(t));
}
function visibleText(html) {
    return html.replace(/<[^>]*>/g, '');
}
function hasRawTokenText(html) {
    const t = visibleText(html);
    return t.includes('<a:') || t.includes('<:');
}

// ═══════════════════════════════════════════════════════════════
section('1. currencyEmojiHtml() — unicode / static / animated');
{
    const s = load(CONFIGS['animated custom']);
    const cases = [
        ['unicode 🪙', '🪙', null, false],
        ['unicode 🌙', '🌙', null, false],
        ['arabic text', 'قمر', null, false],
        ['static custom', `<:moon:${STATIC_ID}>`, STATIC_ID, false],
        ['animated custom', `<a:moon:${ANIM_ID}>`, ANIM_ID, true],
    ];
    for (const [label, raw, id, animated] of cases) {
        const out = s.currencyEmojiHtml(raw);
        if (id === null) {
            assert(!out.includes('<img') && out.includes(raw),
                `${label} -> passes through as escaped text`, out);
        } else {
            const ext = animated ? 'gif' : 'png';
            assert(out.includes(`<img`) &&
                   out.includes(`cdn.discordapp.com/emojis/${id}.${ext}`),
                `${label} -> a CDN <img> on the .${ext} asset`, out);
            assert(!out.includes('<a:') && !out.includes('<:'),
                `${label} -> no raw markup in the output`, out);
        }
        assert(swallowedAsTag(out).length === 0,
            `${label} -> a browser does not swallow it as a tag`,
            swallowedAsTag(out).join(','));
        assert(!hasRawTokenText(out), `${label} -> no visible token text`);
    }
    assert(s.currencyEmojiHtml('') === '' && s.currencyEmojiHtml(undefined) === '',
        'empty -> empty string');
    assert(s.currencyEmojiHtml('<b>') === '&lt;b&gt;',
        'literal text is escaped, not treated as markup',
        s.currencyEmojiHtml('<b>'));
    assert(s.currencyEmojiHtml(`<:_:${STATIC_ID}>`)
             .includes(`cdn.discordapp.com/emojis/${STATIC_ID}.png`),
        'an ID-only emoji still renders an image');
    assert(s.currencyEmojiHtml('🪙').includes('<img') === false,
        'a unicode emoji never becomes an image');
}

section('2. currencyEmojiText() — the <option> twin');
{
    const s = load(CONFIGS['animated custom']);
    assert(s.currencyEmojiText('🪙') === '🪙', 'unicode passes through');
    assert(s.currencyEmojiText(`<:moon:${STATIC_ID}>`) === 'moon',
        'static custom -> its NAME, never the token');
    assert(s.currencyEmojiText(`<a:moon:${ANIM_ID}>`) === 'moon',
        'animated custom -> its NAME, never the token');
    assert(s.currencyEmojiText(`<:_:${STATIC_ID}>`) === '',
        'the `_` placeholder name reads as "no icon"');
    assert(s.currencyEmojiText('') === '', 'empty -> empty');
    for (const raw of [`<:moon:${STATIC_ID}>`, `<a:moon:${ANIM_ID}>`]) {
        const out = s.currencyEmojiText(raw);
        assert(!out.includes('<') && !out.includes('>'),
            `no markup leaks from ${raw.slice(0, 12)}…`, out);
    }
}

section('3. currencyLabel() / currencyAmount() — the HTML sinks');
for (const [name, cfg] of Object.entries(CONFIGS)) {
    const s = load(cfg);
    const isCustom = name.endsWith('custom');
    const id = name === 'animated custom' ? ANIM_ID : (isCustom ? STATIC_ID : null);

    for (const key of ['coins', 'diamonds']) {
        const info = cfg[key];
        const label = s.currencyLabel(key);
        const amount = s.currencyAmount(key, 1250);

        assert(swallowedAsTag(label).length === 0,
            `[${name}] currencyLabel('${key}') is not swallowed as a tag`,
            swallowedAsTag(label).join(','));
        assert(!hasRawTokenText(label),
            `[${name}] currencyLabel('${key}') shows no raw token text`, label);
        assert(label.includes(info.name),
            `[${name}] currencyLabel('${key}') keeps the configured name`, label);

        assert(swallowedAsTag(amount).length === 0,
            `[${name}] currencyAmount('${key}') is not swallowed as a tag`);
        assert(!hasRawTokenText(amount),
            `[${name}] currencyAmount('${key}') shows no raw token text`, amount);
        assert(amount.includes('1,250') && amount.includes(info.name),
            `[${name}] currencyAmount('${key}') keeps amount + name formatting`,
            amount);

        if (isCustom) {
            const ext = name === 'animated custom' ? 'gif' : 'png';
            assert(label.includes(`cdn.discordapp.com/emojis/${id}.${ext}`),
                `[${name}] currencyLabel('${key}') icon is a CDN image`, label);
            assert(amount.includes(`cdn.discordapp.com/emojis/${id}.${ext}`),
                `[${name}] currencyAmount('${key}') icon is a CDN image`, amount);
        } else {
            assert(label.includes(info.emoji),
                `[${name}] currencyLabel('${key}') renders the unicode emoji`,
                label);
        }
    }

    // labelFirst is prose — no icon either way, and unchanged behaviour.
    const lf = s.currencyAmount('coins', 1250, true);
    assert(lf === `${cfg.coins.name} 1,250`,
        `[${name}] labelFirst keeps 'Name 1,250' with no icon`, lf);

    // A non-currency key must fall through untouched — it must never be
    // labelled with a currency name or icon.
    assert(s.currencyLabel('xp') === 'xp', `[${name}] 'xp' falls through`,
        s.currencyLabel('xp'));
    assert(s.currencyNameFor('role') === 'role',
        `[${name}] currencyNameFor('role') falls through`);

    // 'balance' is the stored column key for the primary currency.
    assert(s.currencyLabel('balance').includes(cfg.coins.name),
        `[${name}] the stored key 'balance' resolves like 'coins'`);
}

section('4. currencyLabelText() — what the <option> pickers get');
for (const [name, cfg] of Object.entries(CONFIGS)) {
    const s = load(cfg);
    for (const key of ['coins', 'diamonds']) {
        const out = s.currencyLabelText(key);
        assert(!out.includes('<img'),
            `[${name}] currencyLabelText('${key}') has no <img>`, out);
        assert(!out.includes('<') && !out.includes('>'),
            `[${name}] currencyLabelText('${key}') is plain text`, out);
        assert(out.includes(cfg[key].name),
            `[${name}] currencyLabelText('${key}') keeps the name`, out);
    }
    const coinText = s.currencyLabelText('coins');
    if (name.endsWith('custom')) {
        assert(coinText.startsWith('moon'),
            `[${name}] the custom icon degrades to its emoji name`, coinText);
    } else {
        assert(coinText.startsWith(cfg.coins.emoji),
            `[${name}] a unicode icon is kept as-is`, coinText);
    }
}

section('5. minigame reward pickers use the TEXT variant');
{
    // REWARD_TYPE_LABELS feeds an <option> list on both minigames pages;
    // an <img> inside an <option> is dropped by every browser.
    for (const f of ['minigames.html', 'minigame_builder.html']) {
        const src = fs.readFileSync(
            path.join(__dirname, '..', 'dashboard', 'templates', 'systems', f),
            'utf8');
        const m = src.match(/const REWARD_TYPE_LABELS = \{([^}]*)\}/);
        assert(m && /coins:\s*currencyLabelText\('coins'\)/.test(m[1]),
            `${f}: coins label comes from currencyLabelText()`,
            m ? m[1].slice(0, 90) : 'not found');
        assert(m && /diamonds:\s*currencyLabelText\('diamonds'\)/.test(m[1]),
            `${f}: diamonds label comes from currencyLabelText()`);
        assert(m && !/[^t]currencyLabel\(/.test(m[1]),
            `${f}: it does not use the HTML variant in a picker`);
    }
    // The Leveling table cell and the Trade summary are innerHTML sinks,
    // so those keep the icon-bearing variant.
    const lev = fs.readFileSync(path.join(
        __dirname, '..', 'dashboard', 'templates', 'systems', 'leveling.html'),
        'utf8');
    assert(/<td>\$\{currencyLabel\(r\.currency\)\}<\/td>/.test(lev),
        'leveling.html: the rewards table cell still uses currencyLabel()');
    const trade = fs.readFileSync(path.join(
        __dirname, '..', 'dashboard', 'templates', 'systems', 'trade.html'),
        'utf8');
    assert(/currencyAmount\('coins'/.test(trade) &&
           /currencyAmount\('diamonds'/.test(trade),
        'trade.html: the offer summary still uses currencyAmount()');
}

section('6. the JS and Python renderers agree');
{
    const py = fs.readFileSync(path.join(
        __dirname, '..', 'dashboard', 'utils', 'currency_ctx.py'), 'utf8');
    const js = SRC;
    assert(py.includes('cdn.discordapp.com') || py.includes('emoji_cdn_url'),
        'python side resolves a CDN url');
    assert(js.includes('cdn.discordapp.com/emojis/'),
        'js side resolves the same CDN host');
    assert(/'gif' : 'png'/.test(js),
        'js picks .gif for animated and .png for static');
    // The Python side delegates the extension to utils/emoji.emoji_cdn_url,
    // which icon_html calls with the token's animated flag.
    const emojiPy = fs.readFileSync(path.join(
        __dirname, '..', 'utils', 'emoji.py'), 'utf8');
    assert(/ext="gif" if animated else "png"/.test(emojiPy)
           && /emoji_cdn_url\(emoji_id, animated\)/.test(py),
        'python picks the same extension, via emoji_cdn_url');
    assert(/m\[2\] === '_'/.test(js) && /name == "_"/.test(py),
        'both treat the `_` placeholder name as "no icon"');
    assert(js.includes('nero-currency-icon') && py.includes('nero-currency-icon'),
        'both tag the image with the same class the CSS sizes');
}

console.log(`\ncurrency-icon-js: ${pass} passed, ${fail} failed`);
if (fail) { console.log('Failures:'); failures.forEach(f => console.log(' -', f)); process.exit(1); }
console.log('ALL CURRENCY-ICON JS TESTS PASSED');
