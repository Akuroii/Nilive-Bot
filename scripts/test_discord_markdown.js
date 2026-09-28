#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════
   Message Builder v2 — Phase 1, step 2: the markdown contract.

   v1 IS THE ORACLE. This harness loads the real, untouched
   `embed-composer.js` and the new `embed/discord-markdown.js` side by
   side and compares them over `scripts/fixtures/markdown_corpus.json`
   (169 cases × 4 rendering contexts), byte for byte:

       v2.render(input, {context})  ===  v1's renderer (input)

   Three independent proofs run against the same corpus:

     1. LIVE ORACLE — v1's `renderDiscordMarkup` is called right here,
        now, for every case and context. If v2 drifts from v1 by a
        single character this fails.
     2. GOLDEN SNAPSHOT — `scripts/fixtures/markdown_golden.json` holds
        v1's output as recorded at the time of this step, together with
        a sha256 of the v1 source it came from. The harness re-verifies
        the file against the live oracle on every run, so a stale or
        edited golden cannot silently become "the expected output".
        Regenerate deliberately with:  node scripts/test_discord_markdown.js --write-golden
     3. FOCUSED EDGE TESTS — the Phase 0 fixes (F1 info strings, F2
        fence structure, F3 placeholder leakage), context-awareness,
        determinism/purity, and the negative cases, asserted directly
        rather than only via equivalence.

   The literal surfaces (title / author / footer / field name / button
   label / select option) have a different oracle: v1's preview renders
   them with `esc()` and never markdown, so the oracle for those is
   `EC.esc(input)`.

   Run:  node scripts/test_discord_markdown.js
   No DOM: both modules are plain JS and the harness gives them nothing
   but a `window` object — a renderer that needed the DOM would throw
   here, which is part of the point.
   ═══════════════════════════════════════════════════════════════ */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const crypto = require('crypto');

const WRITE_GOLDEN = process.argv.indexOf('--write-golden') !== -1;

let pass = 0, fail = 0; const failures = [];
function assert(cond, name, extra) {
    if (cond) { pass++; console.log('  PASS', name); }
    else { fail++; failures.push(name + (extra ? ' — ' + extra : '')); console.log('  FAIL', name, extra || ''); }
}
function section(t) { console.log('\n== ' + t + ' =='); }

const JS_DIR = path.join(__dirname, '..', 'dashboard', 'static', 'js');
const FIXTURES = path.join(__dirname, 'fixtures');
const V1_PATH = path.join(JS_DIR, 'embed-composer.js');
const V2_PATH = path.join(JS_DIR, 'embed', 'discord-markdown.js');
const CORPUS_PATH = path.join(FIXTURES, 'markdown_corpus.json');
const GOLDEN_PATH = path.join(FIXTURES, 'markdown_golden.json');

// ── Load both implementations, with no DOM in sight ──────────────
function loadInSandbox(file) {
    const sandbox = { window: {}, console };
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(file, 'utf8'), sandbox);
    return sandbox.window;
}
const v1window = loadInSandbox(V1_PATH);
const v2window = loadInSandbox(V2_PATH);
const V1 = v1window.EmbedComposer;
const MD = v2window.NERO.embed.discordMarkdown;
const v1SourceHash = crypto.createHash('sha256').update(fs.readFileSync(V1_PATH)).digest('hex');

const corpus = JSON.parse(fs.readFileSync(CORPUS_PATH, 'utf8'));

// ── Lookups: one rich fixture, one empty ─────────────────────────
function makeLookups(kind) {
    if (kind === 'empty') {
        return { roles: {}, channels: {}, users: {}, onUserResolve: () => {} };
    }
    return {
        roles: {
            '777': { name: 'Moderator', color: '#5865f2' },
            '778': { name: 'No Colour Role' },
            '779': { name: 'a & b "role"' },
        },
        channels: { '100': { name: 'announcements' }, '101': { name: 'a <b> & "c"' } },
        users: { '200': 'tester', '201': 'a & b' },
        onUserResolve: () => {},
    };
}

const MARKDOWN_CONTEXTS = ['content', 'description', 'fieldValue'];
const LITERAL_CONTEXTS = ['title', 'author', 'footer', 'fieldName', 'buttonLabel', 'selectOption'];

/**
 * What v1's preview does for a literal surface: `esc(value)` on the raw
 * value. This is the oracle for title / author / footer / field name /
 * button label / select option — and v2 must match it byte for byte,
 * control characters included. There is no scrub in this path.
 */
function v1Literal(input) { return V1.esc(input); }

function v1Render(input, context, lookups) {
    return V1.renderDiscordMarkup(input, {
        checkEmojiOnly: context === 'content',
        lookups: lookups,
    });
}

// ── Collect every comparison, then print a summary per category ──
const stats = {
    total: 0, matched: 0,
    byCategory: {},                       // category -> {cases, comparisons, mismatches}
    mismatches: [],
};
function record(category, caseId, context, ok, detail) {
    stats.total++;
    const bucket = stats.byCategory[category] || (stats.byCategory[category] = { cases: 0, comparisons: 0, mismatches: 0 });
    bucket.comparisons++;
    if (ok) { stats.matched++; return; }
    bucket.mismatches++;
    if (stats.mismatches.length < 8) stats.mismatches.push(`${caseId} [${context}] ${detail}`);
}

const seenCategories = {};
corpus.cases.forEach(c => { seenCategories[c.category] = (seenCategories[c.category] || 0) + 1; });
Object.keys(seenCategories).forEach(k => { stats.byCategory[k] = { cases: seenCategories[k], comparisons: 0, mismatches: 0 }; });

const goldenCases = {};
const CONTROL_RE = /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/;
let controlCasesSeen = 0;

// ═══════════════════════════════════════════════════════════════
section('live oracle equivalence — v2 vs the real v1 renderer');
// ═══════════════════════════════════════════════════════════════
corpus.cases.forEach(c => {
    const lookups = makeLookups(c.lookups);
    const liveLookups = makeLookups(c.lookups);

    // 1. markdown contexts, byte for byte (html + isEmojiOnly)
    MARKDOWN_CONTEXTS.forEach(context => {
        const oracle = v1Render(c.input, context, liveLookups);
        const actual = MD.render(c.input, { context: context, lookups: lookups, checkEmojiOnly: context === 'content' });
        const ok = oracle.html === actual.html && !!oracle.isEmojiOnly === !!actual.isEmojiOnly;
        record(c.category, c.id, context, ok, ok ? '' : `\n      v1: ${JSON.stringify(oracle.html)}\n      v2: ${JSON.stringify(actual.html)}`);
        if (context === 'content') {
            goldenCases[c.id] = goldenCases[c.id] || {};
            goldenCases[c.id].content = { html: oracle.html, isEmojiOnly: !!oracle.isEmojiOnly };
        } else {
            goldenCases[c.id] = goldenCases[c.id] || {};
            goldenCases[c.id][context] = { html: oracle.html, isEmojiOnly: !!oracle.isEmojiOnly };
        }
    });

    // 2. the literal surfaces: oracle is v1's esc() at the call site
    const literalOracle = v1Literal(c.input);
    goldenCases[c.id].literal = literalOracle;
    LITERAL_CONTEXTS.forEach(context => {
        const actual = MD.render(c.input, { context: context, lookups: lookups });
        const ok = actual.html === v1Literal(c.input) && actual.isEmojiOnly === false;
        record(c.category, c.id, context, ok, ok ? '' : `\n      want: ${JSON.stringify(v1Literal(c.input))}\n      got:  ${JSON.stringify(actual.html)}`);
    });
    // 3. and the same comparison through the renderLiteral convenience,
    //    so the two entry points cannot drift apart
    record(c.category, c.id, 'literal-helper',
        MD.renderLiteral(c.input) === v1Literal(c.input),
        `renderLiteral drift: ${JSON.stringify(MD.renderLiteral(c.input))} vs ${JSON.stringify(v1Literal(c.input))}`);
    if (CONTROL_RE.test(c.input)) controlCasesSeen++;
});

Object.keys(stats.byCategory).forEach(k => {
    const b = stats.byCategory[k];
    console.log(`  ${b.mismatches === 0 ? 'PASS' : 'FAIL'} ${k.padEnd(24)} ${String(b.cases).padStart(3)} cases · ${String(b.comparisons).padStart(4)} comparisons · ${b.mismatches} mismatch(es)`);
});
assert(stats.mismatches.length === 0,
    `every corpus case matches v1 in all 4 contexts (${stats.matched}/${stats.total} comparisons)`,
    stats.mismatches.join('\n    '));

// ═══════════════════════════════════════════════════════════════
section('golden snapshot — the recorded contract');
// ═══════════════════════════════════════════════════════════════
const goldenDoc = {
    generator: 'dashboard/static/js/embed-composer.js :: renderDiscordMarkup (v1, untouched)',
    literal_oracle: 'EmbedComposer.esc(text) — what v1\'s preview does for title/author/footer/field name',
    note: 'Recorded by scripts/test_discord_markdown.js --write-golden. Verified against the live v1 module on every run.',
    source_sha256: v1SourceHash,
    corpus_version: corpus.version,
    case_count: corpus.cases.length,
    cases: goldenCases,
};

if (WRITE_GOLDEN) {
    fs.writeFileSync(GOLDEN_PATH, JSON.stringify(goldenDoc, null, 2) + '\n');
    console.log('  wrote', path.relative(process.cwd(), GOLDEN_PATH));
    console.log(`\nmarkdown golden regenerated from the live v1 module (sha256 ${v1SourceHash.slice(0, 16)}…)`);
    process.exit(0);
}

const golden = JSON.parse(fs.readFileSync(GOLDEN_PATH, 'utf8'));
assert(golden.case_count === corpus.cases.length,
    `the golden file covers every corpus case (${golden.case_count} recorded / ${corpus.cases.length} in the corpus)`,
    `regenerate with --write-golden if the corpus changed on purpose`);
assert(golden.source_sha256 === v1SourceHash,
    'the golden file was generated from THIS v1 source (sha256 match)',
    `golden: ${golden.source_sha256.slice(0, 16)}…  live: ${v1SourceHash.slice(0, 16)}… — v1 changed; regenerate deliberately or revert it`);

let goldenDrift = [];
corpus.cases.forEach(c => {
    const recorded = golden.cases[c.id];
    if (!recorded) { goldenDrift.push(c.id + ': missing from the golden file'); return; }
    MARKDOWN_CONTEXTS.forEach(context => {
        const live = V1.renderDiscordMarkup(c.input, { checkEmojiOnly: context === 'content', lookups: makeLookups(c.lookups) });
        const rec = recorded[context];
        if (!rec || rec.html !== live.html || !!rec.isEmojiOnly !== !!live.isEmojiOnly) {
            goldenDrift.push(`${c.id} [${context}]: recorded ${JSON.stringify(rec && rec.html)} ≠ live ${JSON.stringify(live.html)}`);
        }
    });
    if (recorded.literal !== V1.esc(c.input)) goldenDrift.push(`${c.id} [literal]: recorded ≠ live esc()`);
});
assert(goldenDrift.length === 0, 'the recorded golden equals the live v1 output for every case and context',
    goldenDrift.slice(0, 3).join('\n    '));

let v2VsGolden = [];
corpus.cases.forEach(c => {
    const lookups = makeLookups(c.lookups);
    MARKDOWN_CONTEXTS.forEach(context => {
        const actual = MD.render(c.input, { context: context, lookups: lookups, checkEmojiOnly: context === 'content' });
        const rec = golden.cases[c.id][context];
        if (actual.html !== rec.html || !!actual.isEmojiOnly !== !!rec.isEmojiOnly) {
            v2VsGolden.push(`${c.id} [${context}]`);
        }
    });
    LITERAL_CONTEXTS.forEach(context => {
        if (MD.render(c.input, { context: context, lookups: lookups }).html !== v1Literal(c.input)) {
            v2VsGolden.push(`${c.id} [${context}]`);
        }
    });
    if (MD.renderLiteral(c.input) !== golden.cases[c.id].literal) v2VsGolden.push(`${c.id} [literal-helper]`);
});
assert(v2VsGolden.length === 0, 'v2 reproduces the recorded golden snapshot', v2VsGolden.slice(0, 3).join(', '));

// ═══════════════════════════════════════════════════════════════
section('F1 — a fence\'s info string is a language, never content');
// ═══════════════════════════════════════════════════════════════
{
    const L = makeLookups();
    const r = MD.render('```js\nconst a = 1;\n```', { context: 'description', lookups: L });
    assert(r.html === '<pre class="eb-code-block"><code>const a = 1;</code></pre>',
        'fence with an info string drops the language', JSON.stringify(r.html));
    assert(r.html.indexOf('js') === -1 || r.html.indexOf('>js') !== -1, 'the language never appears as content');

    const langs = ['python', 'javascript', 'cs', 'c++', 'text', 'json5'];
    const bad = langs.filter(l => MD.render('```' + l + '\nBODY\n```', { context: 'description' })
        .html.indexOf('>' + l + '\n') !== -1 || MD.render('```' + l + '\nBODY\n```', { context: 'description' })
        .html.indexOf('>' + l) === 0);
    assert(bad.length === 0, 'six languages with punctuation/symbols are all dropped', bad.join(', '));

    const noInfo = MD.render('```\nplain\n```', { context: 'description' });
    assert(noInfo.html === '<pre class="eb-code-block"><code>plain</code></pre>',
        'a fence without an info string is unaffected', JSON.stringify(noInfo.html));

    // mid-sentence triple runs have no info line and must keep their body
    // A mid-line triple run is a FENCE in v1 (recorded as a defect), but it has
    // no info line, so its body must survive untouched.
    const mid = MD.render('text ```odd``` text', { context: 'description' });
    assert(mid.html === 'text <pre class="eb-code-block"><code>odd</code></pre> text',
        'a mid-line triple run keeps its body verbatim (no info line to drop)', JSON.stringify(mid.html));

    const blankFirst = MD.render('```\n\nsecond\n```', { context: 'description' });
    assert(blankFirst.html === '<pre class="eb-code-block"><code>second</code></pre>',
        'a blank first line is the (empty) language, not content', JSON.stringify(blankFirst.html));
}

// ═══════════════════════════════════════════════════════════════
section('F2 — fenced blocks are structurally correct (CSS is step 5)');
// ═══════════════════════════════════════════════════════════════
{
    const longLine = 'const aVeryLongVariableName = ' + 'x'.repeat(400) + ';';
    const r = MD.render('```js\n' + longLine + '\n```', { context: 'description' });
    assert(r.html === '<pre class="eb-code-block"><code>' + longLine + '</code></pre>',
        'one block, one code element, the long line intact (nothing is wrapped or truncated here)');
    assert((r.html.match(/<pre class="eb-code-block">/g) || []).length === 1 &&
           (r.html.match(/<\/code><\/pre>/g) || []).length === 1,
        'exactly one <pre class="eb-code-block"><code>…</code></pre> wrapper');
    const multi = MD.render('```js\nx\n```\n```js\ny\n```', { context: 'description' });
    assert(multi.html === '<pre class="eb-code-block"><code>x</code></pre>\n<pre class="eb-code-block"><code>y</code></pre>',
        'two fences produce two sibling blocks in order', JSON.stringify(multi.html));
    const inline = MD.render('use `npm start` here', { context: 'description' });
    assert(inline.html === 'use <code class="eb-code">npm start</code> here',
        'inline code is a <code class="eb-code">, not a block', JSON.stringify(inline.html));
}

// ═══════════════════════════════════════════════════════════════
section('F3 — no placeholder or control-character leakage');
// ═══════════════════════════════════════════════════════════════
{
    const CTRL = /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/;
    const leakCases = [
        ['nested runs around markdown (the F3 repro)', '`` ``` `**x**` ``` ``'],
        ['fence inside double backticks', '`` ```x``` ``'],
        ['placeholder-shaped input', 'a\u00030\u0004b'],
        ['token placeholder-shaped input', '`a\u0001b`'],
        ['all controls', '\u0000\u0001\u0002\u0003\u0004\u0005'],
        ['control inside a fence', '```\n\u00030000\u0004\n```'],
        ['control inside a token', '<@200\u0003>'],
        ['ESC sequence', '\u001b[31mred\u001b[0m'],
        ['many tick runs', '`'.repeat(20) + 'x' + '`'.repeat(20)],
        ['many fences', '```'.repeat(50)],
    ];
    let leaked = [];
    leakCases.forEach(([name, input]) => {
        // Markup surfaces only: that is where v1 strips controls and scrubs
        // its placeholders. The literal surfaces must reproduce v1's esc()
        // exactly, control characters included — asserted separately below.
        MARKDOWN_CONTEXTS.forEach(context => {
            const r = MD.render(input, { context: context, lookups: makeLookups() });
            if (CTRL.test(r.html)) leaked.push(`${name} [${context}]`);
            if (/\u0003\d{4}\u0004/.test(r.html) || /\u0001\d{4}\u0002/.test(r.html)) leaked.push(`${name} [${context}] placeholder`);
        });
    });
    assert(leaked.length === 0, 'no control character or placeholder survives in any context', leaked.join(', '));

    const nested = MD.render('`` ``` `**x**` ``` ``', { context: 'description' });
    assert(nested.html === '<code class="eb-code">``` `**x**` ```</code>',
        'the nested-run case keeps its content instead of a placeholder', JSON.stringify(nested.html));
    const forged = MD.render('a\u00030\u0004b', { context: 'description' });
    assert(forged.html === 'a0b', 'a forged placeholder loses its control characters, keeps the digits',
        JSON.stringify(forged.html));
    const inFence = MD.render('```\n\u00030000\u0004\n```', { context: 'description' });
    assert(inFence.html === '<pre class="eb-code-block"><code>0000</code></pre>',
        'a fenced body containing placeholder characters is scrubbed too', JSON.stringify(inFence.html));

    // The scrub's real job: names that came from the API, not from the user.
    // User text is cleaned before anything runs, but a channel/role/user name
    // is inserted by renderToken AFTER that, and only the final scrub stops a
    // control character inside one of those from reaching the DOM.
    const hostileLookups = {
        roles: { '777': { name: 'role\u0003name', color: '#5865f2' } },
        channels: { '100': { name: 'chan\u0004name' } },
        users: { '200': 'user\u0002name' },
        onUserResolve: () => {},
    };
    const fromLookups = MD.render('<@&777> <#100> <@200>', { context: 'description', lookups: hostileLookups });
    assert(!CTRL.test(fromLookups.html),
        'a control character inside an API-sourced role/channel/user name never reaches the output',
        JSON.stringify(fromLookups.html));
    assert(fromLookups.html.indexOf('rolename') !== -1 && fromLookups.html.indexOf('channame') !== -1 &&
           fromLookups.html.indexOf('username') !== -1,
        'the visible part of those names survives the scrub', JSON.stringify(fromLookups.html));
}

// ═══════════════════════════════════════════════════════════════
section('context-awareness — the same input, different surfaces');
// ═══════════════════════════════════════════════════════════════
{
    const bold = '**not bold in a title**';
    assert(MD.render(bold, { context: 'content' }).html === '<strong>not bold in a title</strong>',
        'content: markdown IS rendered');
    assert(MD.render(bold, { context: 'description' }).html === '<strong>not bold in a title</strong>',
        'description: markdown IS rendered');
    assert(MD.render(bold, { context: 'fieldValue' }).html === '<strong>not bold in a title</strong>',
        'field value: markdown IS rendered');
    LITERAL_CONTEXTS.forEach(context => {
        assert(MD.render(bold, { context: context }).html === V1.esc(bold),
            `${context}: markdown is NOT rendered (v1 preview's esc() path)`, MD.render(bold, { context: context }).html);
    });

    const mention = 'hi <@200>';
    assert(MD.render(mention, { context: 'content', lookups: makeLookups() }).html ===
           'hi <span class="eb-mention">@tester</span>', 'content: mentions resolve');
    assert(MD.render(mention, { context: 'title', lookups: makeLookups() }).html === 'hi &lt;@200&gt;',
        'title: a mention stays literal text (v1 esc() at the call site)');

    // the emoji-only flag belongs to the content surface only, which is how
    // v1's call sites use it (checkEmojiOnly is passed for content alone)
    assert(MD.render('🎲', { context: 'content', checkEmojiOnly: true }).isEmojiOnly === true,
        'content: emoji-only detection is active');
    ['description', 'fieldValue'].forEach(context => {
        assert(MD.render('🎲', { context: context, checkEmojiOnly: true }).isEmojiOnly === false,
            `${context}: the emoji-only flag is never set (v1 passes no checkEmojiOnly here)`);
    });
    LITERAL_CONTEXTS.forEach(context => {
        assert(MD.render('🎲', { context: context, checkEmojiOnly: true }).isEmojiOnly === false,
            `${context}: literal surfaces have no emoji-only mode`);
    });

    const annotated = MD.render('🎲', { context: 'content', checkEmojiOnly: true });
    assert(annotated.html === '<span>🎲</span>', 'emoji-only content is wrapped for the large-size treatment',
        JSON.stringify(annotated.html));
}

// ═══════════════════════════════════════════════════════════════
section('determinism and purity');
// ═══════════════════════════════════════════════════════════════
{
    const input = 'hi <@200> **bold** `code` ```js\nx\n``` 🎲';
    const a = MD.render(input, { context: 'content', checkEmojiOnly: true, lookups: makeLookups() });
    const b = MD.render(input, { context: 'content', checkEmojiOnly: true, lookups: makeLookups() });
    assert(a.html === b.html && a.isEmojiOnly === b.isEmojiOnly, 'the same input renders the same bytes twice');

    // no state may leak between calls: interleave a very different input
    MD.render('```js\nSOMETHING ELSE\n``` <@999> **x**', { context: 'description', lookups: makeLookups() });
    const c = MD.render(input, { context: 'content', checkEmojiOnly: true, lookups: makeLookups() });
    assert(c.html === a.html, 'an intervening call changes nothing (no shared regex state, no cache)');

    // the options object and the lookups must not be mutated
    const lookups = makeLookups();
    const before = JSON.stringify({ roles: lookups.roles, channels: lookups.channels, users: lookups.users });
    const opts = { context: 'content', lookups: lookups, checkEmojiOnly: true };
    MD.render(input, opts);
    assert(JSON.stringify({ roles: lookups.roles, channels: lookups.channels, users: lookups.users }) === before,
        'the lookups object is not mutated');
    assert(Object.keys(opts).length === 3 && opts.context === 'content', 'the options object is not mutated');

    // deep-frozen inputs: a pure renderer must not need to write to its input
    const frozenLookups = makeLookups();
    Object.freeze(frozenLookups.roles); Object.freeze(frozenLookups.channels);
    Object.freeze(frozenLookups.users); Object.freeze(frozenLookups);
    let threw = null;
    try { MD.render(input, { context: 'content', checkEmojiOnly: true, lookups: frozenLookups }); }
    catch (err) { threw = String(err.message); }
    assert(threw === null, 'a deep-frozen lookups object renders without throwing', threw || '');

    // an undefined lookups object is legal (unresolved tokens render raw)
    const noLookups = MD.render('hi <@200> <#100> <@&777>', { context: 'description' });
    assert(noLookups.html === 'hi <span class="eb-mention">@200</span> <span class="eb-mention">#100</span> <span class="eb-mention">@777</span>',
        'unresolved tokens render as raw ids, exactly like Discord', JSON.stringify(noLookups.html));

    // the onUserResolve hook is called for an unresolved user (v1's contract)
    const seen = [];
    MD.render('hi <@123456>', { context: 'description', lookups: { roles: {}, channels: {}, users: {}, onUserResolve: (id) => seen.push(id) } });
    assert(seen.length === 1 && seen[0] === '123456', 'onUserResolve is called once with the raw id', JSON.stringify(seen));
    const seen2 = [];
    MD.render('hi <@200>', { context: 'description', lookups: { roles: {}, channels: {}, users: { '200': 'tester' }, onUserResolve: (id) => seen2.push(id) } });
    assert(seen2.length === 0, 'onUserResolve is NOT called for a resolved user');

    // context handling
    assert(MD.render('x').context === 'content', 'an omitted context defaults to content');
    assert(MD.renderLiteral(null) === '' && MD.render(null, { context: 'content' }).html === '',
        'null renders as empty, not "null"');
    assert(MD.render(undefined, { context: 'content' }).html === '', 'undefined renders as empty');
    let unknown = null;
    try { MD.render('x', { context: 'bogus' }); } catch (err) { unknown = String(err.message); }
    assert(unknown !== null && /unknown markdown context/.test(unknown),
        'an unknown context throws instead of silently applying the wrong rules', unknown || '(no throw)');

    // no DOM: this sandbox had no document/timers at all, and the module loaded
    assert(typeof v2window.document === 'undefined' && typeof v2window.setTimeout === 'undefined',
        'the module loaded and rendered in a sandbox with no DOM and no timers');
}

// ═══════════════════════════════════════════════════════════════
section('focused behaviour spot-checks (mirrors of the corpus, readable)');
// ═══════════════════════════════════════════════════════════════
{
    const md = (s, c) => MD.render(s, { context: c || 'description', lookups: makeLookups() }).html;
    assert(md('**bold**') === '<strong>bold</strong>', 'bold');
    assert(md('*italic*') === '<em>italic</em>', 'italic');
    assert(md('__underline__') === '<u>underline</u>', 'underline (Discord uses __ for underline, not bold)');
    assert(md('~~strike~~') === '<s>strike</s>', 'strikethrough');
    // INHERITED DEFECT (recorded, not endorsed): v1's bold pass runs first and
    // leaves a stray asterisk, so ***x*** comes out with crossed tags. v2
    // matches it byte for byte because equivalence is this step's contract;
    // the corpus tags it as a known gap so a later phase can fix it visibly.
    assert(md('***both***') === '<strong><em>both</strong></em>',
        'bold+italic produces v1\'s crossed tags (inherited defect, recorded)', md('***both***'));
    assert(md('`**x**`') === '<code class="eb-code">**x**</code>', 'markdown inside code stays literal');
    assert(md('<script>alert(1)</script>') === '&lt;script&gt;alert(1)&lt;/script&gt;', 'HTML is escaped');
    assert(md('a & b') === 'a &amp; b', 'ampersands are escaped');
    assert(md('"q"') === '&quot;q&quot;', 'quotes are escaped');

    // token rendering, all four kinds
    assert(md('<@200>') === '<span class="eb-mention">@tester</span>', 'user mention resolves');
    assert(md('<@!200>') === '<span class="eb-mention">@tester</span>', 'the legacy <@!id> form resolves too');
    assert(md('<#100>') === '<span class="eb-mention">#announcements</span>', 'channel mention resolves');
    assert(md('<@&777>') === '<span class="eb-mention" style="background:#5865f233;color:#5865f2;">@Moderator</span>',
        'role mention with a colour gets the inline style', md('<@&777>'));
    assert(md('<@&778>') === '<span class="eb-mention">@No Colour Role</span>', 'a colourless role gets no style');
    assert(md('<:static:111>') === '<img class="eb-inline-emoji" src="https://cdn.discordapp.com/emojis/111.png" alt=":static:',
        'custom emoji renders as an image (the inherited unterminated alt included)', md('<:static:111>'));
    assert(md('<a:spin:222>').indexOf('.gif') !== -1, 'animated emoji uses the gif url');
    assert(md('<@200>') === md('<@!200>'), 'both user forms produce identical output');
}

// ═══════════════════════════════════════════════════════════════
section('negative and edge cases (not the happy path)');
// ═══════════════════════════════════════════════════════════════
{
    const md = (s) => MD.render(s, { context: 'description', lookups: makeLookups() }).html;
    assert(md('') === '', 'empty string');
    assert(md('   ') === '   ', 'whitespace only is preserved as typed');
    assert(md('\n\n\n') === '\n\n\n', 'newlines are preserved');
    assert(md('`unclosed') === '`unclosed', 'an unclosed inline run stays as typed', JSON.stringify(md('`unclosed')));
    assert(md('```\nunclosed fence') === '```\nunclosed fence', 'an unclosed fence stays as typed', JSON.stringify(md('```\nunclosed fence')));
    assert(md('**unclosed') === '**unclosed', 'unbalanced bold stays as typed');
    assert(md('****') === '<em>*</em>*', 'INHERITED DEFECT: four asterisks fall into the italic pass');
    assert(md('``') === '``', 'an empty double run stays as typed');
    assert(md('a ` b') === 'a ` b', 'a lone backtick between spaces stays literal');
    assert(md('```js```') === '<pre class="eb-code-block"><code>js</code></pre>',
        'INHERITED DEFECT: a mid-line triple run is a fence in v1, not an inline span', md('```js```'));
    assert(md('text ``` odd ``` text') === 'text <pre class="eb-code-block"><code> odd </code></pre> text',
        'INHERITED DEFECT: that fence lands mid-sentence', md('text ``` odd ``` text'));
    assert(md('****') === '<em>*</em>*',
        'INHERITED DEFECT: **** is eaten by the italic pass', md('****'));
    assert(md('\\*escaped\\*') === '\\<em>escaped\\</em>',
        'INHERITED DEFECT: backslash escapes are not handled', md('\\*escaped\\*'));

    // known gaps: both implementations leave Discord features literal.
    // These asserts are the tripwire for turning them on later.
    assert(md('||spoiler||') === '||spoiler||', 'GAP: spoilers are not implemented (recorded)');
    assert(md('> quote') === '&gt; quote', 'GAP: blockquotes are not implemented (the > is escaped)');
    assert(md('# Header') === '# Header', 'GAP: headers are not implemented');
    assert(md('-# subtext') === '-# subtext', 'GAP: subtext is not implemented');
    assert(md('- item') === '- item', 'GAP: lists are not implemented');
    assert(md('[x](https://y)') === '[x](https://y)', 'GAP: masked links are not implemented');
    assert(md('<t:1758600000:R>') === '&lt;t:1758600000:R&gt;', 'GAP: timestamps are not implemented');
    assert(md('https://x/y') === 'https://x/y', 'GAP: bare URLs are not auto-linked (Discord would)');
    assert(md('| a | b |') === '| a | b |', 'GAP: tables are not implemented');
    assert(md('\\*escaped\\*') === '\\<em>escaped\\</em>',
        'GAP + INHERITED DEFECT: backslash escapes are not implemented, and the asterisks still italicize');

    // the corpus must carry every gap, so nothing is forgotten silently
    const gapIds = corpus.cases.filter(c => c.knownGap).map(c => c.id);
    assert(gapIds.length >= 25, `the corpus records ${gapIds.length} known gaps explicitly`);
    const defects = corpus.cases.filter(c => c.knownGap && c.knownDefect);
    assert(defects.length >= 5,
        `${defects.length} of those gaps are INHERITED FIDELITY DEFECTS (v1 behaviour that Discord does not do), each with the exact bytes recorded`);
    const gapsWithoutNote = corpus.cases.filter(c => c.knownGap && !c.knownGap.length);
    assert(gapsWithoutNote.length === 0, 'every known gap names the Discord feature it is missing');
}

// ═══════════════════════════════════════════════════════════════
section('the corpus itself');
// ═══════════════════════════════════════════════════════════════
{
    assert(corpus.cases.length >= 150, `the corpus is substantial (${corpus.cases.length} cases)`);
    const ids = corpus.cases.map(c => c.id);
    assert(new Set(ids).size === ids.length, 'every case id is unique');
    const cats = Object.keys(stats.byCategory);
    assert(cats.length >= 8, `the corpus spans ${cats.length} categories`);
    const emptyCases = corpus.cases.filter(c => typeof c.input !== 'string' || c.input.length === 0);
    assert(emptyCases.length >= 1, 'the corpus includes the empty-input case (whitespace-unicode category)');
    assert(corpus.cases.some(c => c.input.indexOf('\n') !== -1), 'the corpus includes multi-line inputs');
    assert(corpus.cases.some(c => /[\u0600-\u06ff]/.test(c.input)), 'the corpus includes RTL text');
    assert(corpus.cases.some(c => /[\u0000-\u001f]/.test(c.input)), 'the corpus includes control characters');
    assert(corpus.cases.some(c => c.input.length > 300), 'the corpus includes a long input');
    const contextCoverage = corpus.contexts ? Object.keys(corpus.contexts) : [];
    assert(contextCoverage.length === 4, 'the four rendering contexts are documented in the corpus file',
        contextCoverage.join(', '));
}

// ═══════════════════════════════════════════════════════════════
section('divergences from v1 (recorded, not hidden)');
// ═══════════════════════════════════════════════════════════════
{
    // D1 is REMOVED: the literal surfaces now reproduce v1's esc() byte for
    // byte, control characters included. This is the explicit proof.
    assert(controlCasesSeen >= 8,
        `the corpus exercises the literal path with control characters in ${controlCasesSeen} cases`,
        'the corpus must contain control-character inputs for this proof to mean anything');
    const literalMismatch = corpus.cases.filter(c => MD.renderLiteral(c.input) !== V1.esc(c.input));
    assert(literalMismatch.length === 0,
        `D1 REMOVED — renderLiteral equals v1's esc() for all ${corpus.cases.length} corpus cases, controls included`,
        literalMismatch.slice(0, 3).map(c => c.id).join(', '));
    const literalWithControls = corpus.cases.filter(c => CONTROL_RE.test(c.input));
    assert(literalWithControls.every(c => CONTROL_RE.test(MD.renderLiteral(c.input))),
        'control characters are PRESERVED on literal surfaces, exactly as v1 preserves them ' +
        `(${literalWithControls.length} cases)`);
    assert(literalWithControls.every(c => CONTROL_RE.test(V1.esc(c.input))),
        'and the v1 oracle demonstrably preserves them too (so this is equality, not convenience)');
    assert(stats.mismatches.length === 0,
        'no divergences at all: markup contexts AND literal surfaces are byte-identical to v1');
    const realCalls = ['content', 'description', 'fieldValue'];
    let flagDivergence = [];
    realCalls.forEach(context => {
        const want = context === 'content';
        if (MD.render('🎲', { context: context, checkEmojiOnly: true }).isEmojiOnly !== want) flagDivergence.push(context);
    });
    assert(flagDivergence.length === 0,
        'D3: the emoji-only flag is granted to the content surface and no other, matching v1\'s call sites');
}

// ═══════════════════════════════════════════════════════════════
Promise.resolve().then(() => {
    console.log(`\ndiscord-markdown: ${pass} passed, ${fail} failed`);
    console.log(`corpus: ${corpus.cases.length} cases × 4 contexts = ${stats.total} oracle comparisons ` +
                `(${stats.matched} matched)`);
    console.log('categories:');
    Object.keys(stats.byCategory).sort().forEach(k => {
        const b = stats.byCategory[k];
        console.log(`  ${k.padEnd(24)} ${String(b.cases).padStart(3)} cases · ${String(b.comparisons).padStart(4)} comparisons`);
    });
    console.log(`known gaps recorded: ${corpus.cases.filter(c => c.knownGap).length}`);
    if (fail) { console.log('Failures:'); failures.forEach(f => console.log(' -', f)); process.exit(1); }
    console.log('ALL DISCORD-MARKDOWN TESTS PASSED');
});
