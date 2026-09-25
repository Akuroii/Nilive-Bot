#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════
   scripts/test_message_builder_validate.js
   Message Builder v2 — phase 1, step 6a: the validation engine.

   WHAT THIS SUITE IS ABOUT
   embed/validate.js is a PURE function of (document, served limits). This
   harness holds it to that and nothing else — it loads the REAL module beside
   the REAL model in a vm sandbox, and asserts:

     A. the module, its contract and its distance from the DOM;
     B. the limits table is the ONLY source of every number (a missing or
        unusable table is an explicit failure — never a silent "no limit");
     C. every rule at, below and above its limit, with the exact code, path,
        nodeId, severity and wording — including the client-only rules;
     D. determinism: same input → same output, and NO state between calls;
     E. the paths and node ids the later steps navigate by;
     F. the URL / timestamp mirrors of utils/embed_schema.py, including the
        cases where the client is deliberately stricter;
     G. the severity decisions that are documented divergences (attachment://,
        an empty field name) are asserted, not assumed;
     H. cost: the plan's §8.2 budget for a 10 embeds × 25 fields document.

   WORDING AUTHORITY (approved decision 7): a rule that exists in
   utils/embed_schema.py must produce the server's message word for word. The
   rules marked CLIENT-ONLY below are the ones the send-time gate cannot see;
   they are listed here so the divergence stays visible:
       * content.whitespace-only   — Discord accepts it and shows nothing
       * embed.field.value.missing — only about a field the user named
       * embed.unused              — an embed that silently will not be sent
   Severity divergences (documented in the module header, asserted in G):
       * attachment:// is a warning here, an error on the server
       * an empty field NAME is a warning here, an error on the server
       * an empty document is CLEAN here (no "Nothing to send" at edit time)

   The limits fixture below mirrors utils/discord_limits.limits_payload().
   scripts/test_embed_schema.py asserts the served payload carries every key
   this engine requires, and the gate that ships a step re-checks the two
   tables against each other by running both.

   Run:  node scripts/test_message_builder_validate.js
         NERO_VALIDATE_SRC=/path/to/copy.js  (the mutation battery's hook)
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
function eq(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

const ROOT = path.join(__dirname, '..');
const js = (...parts) => path.join(ROOT, 'dashboard', 'static', 'js', ...parts);
// NERO_VALIDATE_SRC lets scripts/support/mb_mutants.js point this suite at a
// deliberately broken copy of the validator. A harness that cannot fail is
// evidence of nothing.
const VALIDATE_PATH = process.env.NERO_VALIDATE_SRC || js('embed', 'validate.js');
const VALIDATE_SRC = fs.readFileSync(VALIDATE_PATH, 'utf8');

// ── Load the real modules (model + validate) in one sandbox ──────
const sandbox = { window: { NERO: {} }, console: console };
vm.createContext(sandbox);
[js('embed', 'model.js'), VALIDATE_PATH, js('embed', 'views', 'rail.js'), js('embed', 'views', 'inspector.js')]
    .forEach(file => vm.runInContext(fs.readFileSync(file, 'utf8'), sandbox, { filename: path.basename(file) }));
const NERO = sandbox.window.NERO;
const model = NERO.embed.model;
const V = NERO.embed.validate;

if (!V || typeof V.validate !== 'function') {
    console.error('HARNESS ERROR: embed/validate.js did not publish NERO.embed.validate');
    process.exit(1);
}

/**
 * The served limits, mirroring utils/discord_limits.limits_payload().
 * Every rule below reads these numbers; the suite's "the table is the only
 * source" section changes them and expects the outcome to change.
 */
function servedLimits() {
    return {
        message: { content_max: 2000, embeds_max: 10, embed_total_chars_max: 6000, request_bytes_max: 26214400 },
        attachments: { count_max: 10, total_bytes_max: 26148864, file_bytes_advisory: 20971520, file_advisory_is_hard: false },
        embed: {
            title_max: 256, description_max: 4096, fields_max: 25, field_name_max: 256,
            field_value_max: 1024, footer_text_max: 2048, author_name_max: 256,
        },
        components: {
            rows_max: 5, buttons_per_row_max: 5, button_label_max: 80, button_url_max: 512,
            custom_id_max: 100, select_options_max: 25, select_option_label_max: 100,
            select_option_description_max: 100, select_placeholder_max: 150,
        },
    };
}

// ── Fixtures ─────────────────────────────────────────────────────
function doc(overrides) {
    const base = {
        schemaVersion: 2,
        id: 'doc-1',
        guildId: '1111222233334444',
        layout: 'legacy',
        content: '',
        embeds: [embed()],
        rows: [],
        assets: {},
    };
    return Object.assign(base, overrides || {});
}

function embed(overrides) {
    const base = {
        id: 'emb-1', title: '', url: '', description: '', color: 0x7c5cbf,
        author: { name: '', url: '', icon: null },
        footer: { text: '', icon: null },
        thumbnail: null, image: null, timestamp: '', fields: [],
    };
    return Object.assign(base, overrides || {});
}

function field(overrides) {
    return Object.assign({ id: 'fld-1', name: 'Name', value: 'Value', inline: false }, overrides || {});
}

const rep = (ch, n) => Array(n + 1).join(ch);

/** code → the one issue the validator should produce for it. */
function byCode(issues, code) {
    return (issues || []).filter(i => i.code === code);
}
function codes(issues) { return (issues || []).map(i => i.code); }
function paths(issues) { return (issues || []).map(i => i.path); }

const LIMITS = servedLimits();
const run = (document_, limits) => V.validate(document_, limits === undefined ? LIMITS : limits);

// ═══════════════════════════════════════════════════════════════
section('A. the module, its contract, and what it must NOT reach for');
// ═══════════════════════════════════════════════════════════════
{
    ['validate', 'ensureLimits', 'limitsIssue', 'signature', 'isHttpUrl', 'isIsoTimestamp',
     'counts', 'caps']
        .forEach(name => assert(typeof V[name] === 'function', 'the engine publishes ' + name + '()'));
    assert(V.ERROR === 'error' && V.WARNING === 'warning',
        'severity vocabulary is error/warning', V.ERROR + '/' + V.WARNING);
    assert(V.CONTENT_NODE === 'content', 'the message-root node id is content', String(V.CONTENT_NODE));
    assert(V.CONTENT_NODE === NERO.embed.views.rail.CONTENT_NODE &&
           V.CONTENT_NODE === NERO.embed.views.inspector.CONTENT_NODE,
        'and it is the SAME node id the rail and the inspector use (one vocabulary)',
        [NERO.embed.views.rail.CONTENT_NODE, NERO.embed.views.inspector.CONTENT_NODE].join('/'));
    assert(eq(V.REQUIRED_LIMITS.map(k => k.join('.')), [
        'message.content_max', 'message.embeds_max', 'message.embed_total_chars_max',
        'embed.title_max', 'embed.description_max', 'embed.fields_max', 'embed.field_name_max',
        'embed.field_value_max', 'embed.footer_text_max', 'embed.author_name_max',
    ]), 'the required-keys contract is exactly the keys the rules consume',
        V.REQUIRED_LIMITS.map(k => k.join('.')).join(','));

    // A blank document is the state a new page starts in: nothing to report.
    assert(run(doc()).length === 0, 'a blank document produces NO issues (the strip stays hidden)');

    // Purity, statically: the engine owns no DOM, no timers, no storage, no I/O.
    const CODE = VALIDATE_SRC.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/(^|[^:])\/\/[^\n]*/g, '$1 ');
    [['document.', /document\./], ['innerHTML', /innerHTML/], ['setTimeout', /setTimeout/],
     ['setInterval', /setInterval/], ['indexedDB', /indexedDB/], ['localStorage', /localStorage/],
     ['fetch(', /fetch\(/], ['Promise', /Promise/], ['Date.now', /Date\.now/]].forEach(([word, re]) => {
        assert(!re.test(CODE), 'the engine never uses ' + word);
    });
    assert(!/NERO\.embed\.(store|preview|drafts|views)/.test(CODE),
        'and it never reaches for the store, the preview, the draft session or a view');

    // The limits contract is a source-level fact too: no number the server owns
    // may be baked in. Comments are stripped above, and the two legitimate
    // numeric spells in this file are neutralized first — `match[5]` (a regex
    // group index) and `year % 100` (the leap-year rule). Everything else is a
    // copied limit and fails here, which is how a table pasted into the engine
    // is caught even when the tests that use it happen to agree.
    const SERVED_NUMBERS = [2000, 10, 6000, 26214400, 26148864, 20971520,
        256, 4096, 25, 1024, 2048, 5, 80, 512, 100, 150];
    const SCRATCH = CODE.replace(/match\[\d+\]/g, 'match[i]').replace(/%\s*\d+/g, '% n');
    const baked = SERVED_NUMBERS.filter(n => new RegExp('\\b' + n + '\\b').test(SCRATCH));
    assert(baked.length === 0, 'no served limit is hard-coded in the engine', baked.join(','));

    assert(!/maxlength|aria-|classList|createElement/.test(CODE),
        'and it renders nothing (no DOM vocabulary at all)');
}

// ═══════════════════════════════════════════════════════════════
section('B. the limits table is the only source (missing ≠ unlimited)');
// ═══════════════════════════════════════════════════════════════
{
    const check = V.ensureLimits(LIMITS);
    assert(check.ok === true && check.missing.length === 0,
        'the complete served table is usable', JSON.stringify(check.missing));

    assert(V.ensureLimits(null).ok === false && V.ensureLimits(null).missing.length === V.REQUIRED_LIMITS.length,
        'an absent table is unusable and names EVERY missing key',
        String(V.ensureLimits(null).missing.length));
    assert(eq(V.ensureLimits({}).missing, V.REQUIRED_LIMITS.map(k => k.join('.'))),
        'an empty object is unusable (nothing is assumed to be unlimited)');
    assert(eq(V.ensureLimits(undefined).missing, V.ensureLimits(null).missing),
        'undefined is treated exactly like null');

    const partial = servedLimits();
    delete partial.embed.title_max;
    assert(eq(V.ensureLimits(partial).missing, ['embed.title_max']),
        'one missing key is named on its own', JSON.stringify(V.ensureLimits(partial).missing));

    const bad = servedLimits();
    bad.embed.fields_max = '25';
    assert(V.ensureLimits(bad).ok === false, 'a limit that is a string is NOT accepted', JSON.stringify(bad.embed.fields_max));
    bad.embed.fields_max = NaN;
    assert(V.ensureLimits(bad).ok === false, 'NaN is NOT accepted');
    bad.embed.fields_max = -1;
    assert(V.ensureLimits(bad).ok === false, 'a negative limit is NOT accepted');
    bad.embed.fields_max = null;
    assert(V.ensureLimits(bad).ok === false, 'null is NOT accepted');
    bad.embed.fields_max = 0;
    assert(V.ensureLimits(bad).ok === true, 'but 0 IS a limit (an empty message is legal)');

    // The whole point: with no table the engine does not fall silent.
    const huge = doc({ content: rep('x', 5000), embeds: [embed({ title: rep('t', 900) })] });
    const withTable = run(huge);
    const withoutTable = run(huge, null);
    assert(withTable.length >= 2, 'rig: the same document produces several issues WITH the table',
        codes(withTable).join(','));
    assert(withoutTable.length === 1 && withoutTable[0].code === 'limits.missing',
        'and exactly one explicit issue WITHOUT it (never a silent pass)',
        codes(withoutTable).join(','));
    const issue = withoutTable[0];
    assert(issue.severity === 'error', 'the missing-table issue is an error', issue.severity);
    assert(issue.path === '' && issue.nodeId === V.CONTENT_NODE,
        'it has no field path and points at the message root', issue.path + '/' + issue.nodeId);
    assert(/limits did not reach this page/.test(issue.message) && /Reload/.test(issue.message),
        'and it says what happened and what to do', issue.message);
    assert(eq(Object.keys(issue).sort(), ['code', 'message', 'nodeId', 'path', 'severity']),
        'every issue has exactly the five documented keys', Object.keys(issue).sort().join(','));
    assert(eq(V.limitsIssue(), issue), 'limitsIssue() is the same object the validator returns');

    // A partial table is just as dangerous as no table: the ONE missing key
    // would silently disable its rule.
    const partialTable = servedLimits();
    delete partialTable.embed.fields_max;
    const overFields = doc({ content: 'hi', embeds: [embed({ fields: Array.from({ length: 26 }, (_, i) => field({ id: 'fld-' + i })) })] });
    assert(run(overFields, partialTable).length === 1 && run(overFields, partialTable)[0].code === 'limits.missing',
        'a table missing ONE key is refused too, instead of quietly skipping that rule');
}

// ═══════════════════════════════════════════════════════════════
section('C. every rule: at the limit, below it, and above it');
// ═══════════════════════════════════════════════════════════════
{
    // content_max
    assert(byCode(run(doc({ content: rep('x', LIMITS.message.content_max) })), 'content.too-long').length === 0,
        'content exactly at content_max is clean');
    assert(byCode(run(doc({ content: rep('x', LIMITS.message.content_max - 1) })), 'content.too-long').length === 0,
        'content below content_max is clean');
    {
        const issues = run(doc({ content: rep('x', LIMITS.message.content_max + 1) }));
        const hit = byCode(issues, 'content.too-long')[0];
        assert(!!hit, 'content one over content_max is an issue');
        assert(hit && hit.severity === 'error' && hit.path === 'content' && hit.nodeId === 'content',
            'it is an error on the message root', hit && [hit.severity, hit.path, hit.nodeId].join('/'));
        assert(hit && hit.message === 'Message content is 2001 characters; Discord\'s limit is 2000.',
            'with the server\'s wording, word for word', hit && hit.message);
    }

    // embeds_max (+ the server's "one message-level error, do not bury it" rule)
    {
        const many = Array.from({ length: LIMITS.message.embeds_max + 1 }, (_, i) => embed({ id: 'emb-' + i, title: rep('t', 300) }));
        const issues = run(doc({ content: 'hi', embeds: many }));
        assert(codes(issues).length === 1 && issues[0].code === 'embeds.too-many',
            'too many embeds is ONE message-level issue (the per-embed errors are not piled on top)',
            codes(issues).join(','));
        assert(issues[0].severity === 'error' && issues[0].path === 'embeds' && issues[0].nodeId === 'content',
            'it is an error on the message root');
        assert(issues[0].message === 'A message can carry at most 10 embeds; this one has 11.',
            'with the server\'s wording', issues[0].message);
        assert(run(doc({ embeds: Array.from({ length: LIMITS.message.embeds_max }, (_, i) => embed({ id: 'emb-' + i })) })).length === 0,
            'exactly embeds_max embeds are clean');
    }

    // title_max / description_max
    {
        assert(byCode(run(doc({ embeds: [embed({ title: rep('t', 256) })] })), 'embed.title.too-long').length === 0 &&
               byCode(run(doc({ embeds: [embed({ title: rep('t', 257) })] })), 'embed.title.too-long').length === 1,
            'title at 256 is clean, 257 is not');
        const hit = byCode(run(doc({ embeds: [embed({ title: rep('t', 257) })] })), 'embed.title.too-long')[0];
        assert(hit.path === 'embeds.0.title' && hit.nodeId === 'emb-1' && hit.severity === 'error',
            'the title issue carries the wire path and the embed\'s node id', hit.path + '/' + hit.nodeId);
        assert(hit.message === 'Embed 1 title is 257 characters; Discord\'s limit is 256.',
            'server wording', hit.message);

        assert(byCode(run(doc({ embeds: [embed({ description: rep('d', 4096) })] })), 'embed.description.too-long').length === 0 &&
               byCode(run(doc({ embeds: [embed({ description: rep('d', 4097) })] })), 'embed.description.too-long').length === 1,
            'description at 4096 is clean, 4097 is not');
        assert(byCode(run(doc({ embeds: [embed({ description: rep('d', 4097) })] })), 'embed.description.too-long')[0].message ===
               'Embed 1 description is 4097 characters; Discord\'s limit is 4096.',
            'server wording');
    }

    // fields_max, field name/value lengths
    {
        const fieldsAt = n => Array.from({ length: n }, (_, i) => field({ id: 'fld-' + i, name: 'N' + i, value: 'V' + i }));
        assert(byCode(run(doc({ embeds: [embed({ fields: fieldsAt(25) })] })), 'embed.fields.too-many').length === 0 &&
               byCode(run(doc({ embeds: [embed({ fields: fieldsAt(26) })] })), 'embed.fields.too-many').length === 1,
            '25 fields are clean, 26 are not');
        const hit = byCode(run(doc({ embeds: [embed({ fields: fieldsAt(26) })] })), 'embed.fields.too-many')[0];
        assert(hit.path === 'embeds.0.fields' && hit.message === 'Embed 1 has 26 fields; Discord\'s limit is 25.',
            'with the server\'s path and wording', hit.path + ' — ' + hit.message);

        const nameAt = n => doc({ embeds: [embed({ fields: [field({ name: rep('n', n) })] })] });
        assert(byCode(run(nameAt(256)), 'embed.field.name.too-long').length === 0 &&
               byCode(run(nameAt(257)), 'embed.field.name.too-long').length === 1,
            'a 256-character field name is clean, 257 is not');
        const nameHit = byCode(run(nameAt(257)), 'embed.field.name.too-long')[0];
        assert(nameHit.path === 'embeds.0.fields.0.name' && nameHit.nodeId === 'fld-1' && nameHit.severity === 'error',
            'the field-name issue points at the FIELD node, not the embed', nameHit.nodeId);
        assert(nameHit.message === 'Embed 1 field 1 name is 257 characters; Discord\'s limit is 256.',
            'server wording', nameHit.message);

        const valueAt = n => doc({ embeds: [embed({ fields: [field({ value: rep('v', n) })] })] });
        assert(byCode(run(valueAt(1024)), 'embed.field.value.too-long').length === 0 &&
               byCode(run(valueAt(1025)), 'embed.field.value.too-long').length === 1,
            'a 1024-character field value is clean, 1025 is not');
        assert(byCode(run(valueAt(1025)), 'embed.field.value.too-long')[0].message ===
               'Embed 1 field 1 value is 1025 characters; Discord\'s limit is 1024.',
            'server wording');
    }

    // footer / author
    {
        const footerAt = n => doc({ embeds: [embed({ footer: { text: rep('f', n), icon: null } })] });
        assert(byCode(run(footerAt(2048)), 'embed.footer.text.too-long').length === 0 &&
               byCode(run(footerAt(2049)), 'embed.footer.text.too-long').length === 1,
            'footer text at 2048 is clean, 2049 is not');
        assert(byCode(run(footerAt(2049)), 'embed.footer.text.too-long')[0].message ===
               'Embed 1 footer text is 2049 characters; Discord\'s limit is 2048.',
            'server wording');

        const authorAt = n => doc({ embeds: [embed({ author: { name: rep('a', n), url: '', icon: null } })] });
        assert(byCode(run(authorAt(256)), 'embed.author.name.too-long').length === 0 &&
               byCode(run(authorAt(257)), 'embed.author.name.too-long').length === 1,
            'author name at 256 is clean, 257 is not');
        assert(byCode(run(authorAt(257)), 'embed.author.name.too-long')[0].message ===
               'Embed 1 author name is 257 characters; Discord\'s limit is 256.',
            'server wording');
    }

    // the per-embed 6000-character budget (message.embed_total_chars_max)
    {
        // 25 named fields, each 201 characters of name and 200 of value, plus a
        // title and a description — the harness computes the sum the same way
        // the server's embed_char_count() does, so the expected message is
        // derived, not typed in.
        const big = Array.from({ length: 25 }, (_, i) =>
            field({ id: 'fld-' + i, name: 'N' + i + rep('n', 199), value: rep('v', 200) }));
        const title = rep('t', 30);
        const description = rep('d', 40);
        const expectedTotal = title.length + description.length +
            big.reduce((sum, f) => sum + f.name.length + f.value.length, 0);
        const issues = run(doc({ embeds: [embed({ title: title, description: description, fields: big })] }));
        const hit = byCode(issues, 'embed.total-chars')[0];
        assert(!!hit, 'an embed whose text exceeds the combined budget is reported');
        assert(hit && hit.severity === 'error' && hit.path === 'embeds.0' && hit.nodeId === 'emb-1',
            'as an error on the embed itself (the server reports it on the same path)',
            hit && hit.path + '/' + hit.nodeId);
        assert(hit && hit.message === 'Embed 1 holds ' + expectedTotal +
               ' characters in total; Discord\'s per-embed limit across all of its text is 6000.',
            'with the server\'s wording and a total counted over every text field', hit && hit.message);
        assert(expectedTotal > 6000, 'rig: the fixture really is over the budget', String(expectedTotal));

        // At the budget: exactly 6000 characters must be clean, 6001 must not.
        // Every field here is individually legal (title 256, author 256,
        // description 4096, footer ≤ 2048), so the ONLY thing that can fire is
        // the combined budget — twice 4608 characters of fixed text plus the
        // footer that moves.
        const atBudget = n => doc({ embeds: [embed({
            title: rep('t', 256), description: rep('d', 4096),
            author: { name: rep('a', 256), url: '', icon: null },
            footer: { text: rep('f', n), icon: null },
        })] });
        assert(run(atBudget(1392)).length === 0,
            'exactly 6000 characters in one embed is clean, every part of it legal',
            codes(run(atBudget(1392))).join(','));
        const over = byCode(run(atBudget(1393)), 'embed.total-chars');
        assert(over.length === 1, 'one more character is reported', codes(run(atBudget(1393))).join(','));
        assert(over[0].message ===
               'Embed 1 holds 6001 characters in total; Discord\'s per-embed limit across all of its text is 6000.',
            'with the exact count in the message', over[0].message);
    }

    // CLIENT-ONLY: whitespace-only content
    {
        const issues = run(doc({ content: '   \n\t ' }));
        assert(byCode(issues, 'content.whitespace-only').length === 1 &&
               byCode(issues, 'content.too-long').length === 0,
            'whitespace-only content is reported once (and not as a length problem)');
        const hit = byCode(issues, 'content.whitespace-only')[0];
        assert(hit.severity === 'warning' && hit.nodeId === 'content' && hit.path === 'content',
            'as a warning on the message root', [hit.severity, hit.path].join('/'));
        assert(run(doc({ content: 'a', embeds: [embed({ title: 'T' })] })).length === 0,
            'one real character is enough to be clean');
    }

    // CLIENT-ONLY: a blank field / a named field with no value
    {
        const blankField = run(doc({ content: 'hi', embeds: [embed({ fields: [field({ name: '', value: '' })] })] }));
        assert(byCode(blankField, 'embed.field.name.missing').length === 1 &&
               byCode(blankField, 'embed.field.value.missing').length === 0,
            'a field that is entirely empty is reported ONCE (needs a name), never twice',
            codes(blankField).join(','));
        const named = run(doc({ content: 'hi', embeds: [embed({ fields: [field({ name: 'Name', value: '' })] })] }));
        assert(byCode(named, 'embed.field.value.missing').length === 1 &&
               byCode(named, 'embed.field.name.missing').length === 0,
            'a NAMED field with no value is reported as such', codes(named).join(','));
        assert(byCode(named, 'embed.field.value.missing')[0].severity === 'warning',
            'and it is a warning (Discord renders the blank line the user asked for)');
        assert(byCode(blankField, 'embed.field.name.missing')[0].message === 'Embed 1 field 1 needs a name.',
            'the empty-name message is the server\'s wording', byCode(blankField, 'embed.field.name.missing')[0].message);
    }

    // CLIENT-ONLY: an unused embed (and why a blank page is not one)
    {
        assert(run(doc()).length === 0, 'a blank page with its single blank embed is CLEAN (the starting state)');
        const half = run(doc({
            content: 'hi',
            embeds: [embed({ id: 'emb-1', title: 'A' }), embed({ id: 'emb-2' })],
        }));
        assert(byCode(half, 'embed.unused').length === 1 &&
               byCode(half, 'embed.unused')[0].nodeId === 'emb-2',
            'an empty embed beside a used one is a warning about THAT embed',
            codes(half).join(','));
        assert(byCode(half, 'embed.unused')[0].message === 'Embed 2 is empty and will not be sent.',
            'with copy that says what actually happens (toWireEmbeds drops it)',
            byCode(half, 'embed.unused')[0].message);
        const usedOnly = run(doc({ embeds: [embed({ title: 'A' })] }));
        assert(byCode(usedOnly, 'embed.unused').length === 0, 'a document with no spare embed has no such warning');

        // The page starts with exactly one blank embed, so "typing a message"
        // must not turn that starting embed into a permanent warning; it takes a
        // second embed to make an empty one a row the user actually left behind.
        const typedOnly = run(doc({ content: 'hi' }));
        assert(byCode(typedOnly, 'embed.unused').length === 0,
            "a lone blank embed stays silent once the message has content (it is the page's starting embed)",
            codes(typedOnly).join(','));
        const twoBlank = run(doc({ content: 'hi', embeds: [embed(), embed({ id: 'emb-2' })] }));
        assert(byCode(twoBlank, 'embed.unused').length === 2 &&
               byCode(twoBlank, 'embed.unused')[0].nodeId === 'emb-1' &&
               byCode(twoBlank, 'embed.unused')[1].nodeId === 'emb-2',
            'but two embeds make every empty one a warning (both rows point at their own embed)',
            codes(twoBlank).join(','));
    }

    // A rule broken at the same time as the message-level one still reports:
    // the message-level rule stops only the PER-EMBED checks (as the server does),
    // which is what the "too many embeds" case above asserts.
    {
        const both = run(doc({ content: rep('c', 3000), embeds: [embed({ title: rep('t', 300) })] }));
        assert(codes(both).join(',') === 'content.too-long,embed.title.too-long',
            'independent problems are all reported, message first, then embed by embed',
            codes(both).join(','));
    }
}

// ═══════════════════════════════════════════════════════════════
section('D. determinism, and no state between calls');
// ═══════════════════════════════════════════════════════════════
{
    const document_ = doc({
        content: rep('c', 3000),
        embeds: [
            embed({ id: 'emb-1', title: rep('t', 300), fields: [field({ id: 'fld-1', name: '', value: '' })] }),
            embed({ id: 'emb-2', image: { kind: 'url', url: 'ftp://nope/x.png' } }),
        ],
    });
    const first = run(document_);
    const second = run(document_);
    assert(eq(first, second), 'the same document and limits produce exactly the same list');
    assert(first !== second, 'and a NEW list each time (nothing is cached and returned twice)');

    // The order is the contract: message-level first, then embed by embed.
    assert(codes(first).join(',') === 'content.too-long,embed.title.too-long,embed.field.name.missing,embed.image.url-invalid',
        'the order is message → embed → its fields → its media, per embed',
        codes(first).join(','));

    // No accumulation: a second, different document must not see the first one.
    const clean = doc({ content: 'hello', embeds: [embed({ title: 'Hi' })] });
    assert(run(clean).length === 0, 'a clean document after a broken one is clean (no leaked issues)',
        codes(run(clean)).join(','));
    assert(eq(codes(run(document_)), codes(first)), 'and the broken one still reports the same list afterwards');

    // The document is never modified (the store owns it).
    const snapshot = JSON.stringify(document_);
    run(document_);
    assert(JSON.stringify(document_) === snapshot, 'validating a document does not touch it');
}

// ═══════════════════════════════════════════════════════════════
section('E. paths and node ids (what a later step navigates by)');
// ═══════════════════════════════════════════════════════════════
{
    const document_ = doc({
        content: rep('c', 3000),
        embeds: [
            embed({
                id: 'emb-a', title: rep('t', 300), url: 'nope', timestamp: 'yesterday',
                author: { name: '', url: 'nope', icon: null },
                footer: { text: '', icon: null },
                image: { kind: 'url', url: 'ftp://x/y.png' },
                fields: [
                    field({ id: 'fld-a', name: '', value: '' }),
                    field({ id: 'fld-b', name: rep('n', 300), value: rep('v', 1100) }),
                ],
            }),
            embed({ id: 'emb-b', thumbnail: { kind: 'url', url: 'no-scheme' } }),
        ],
    });
    const issues = run(document_);
    const table = issues.map(i => i.code + ' @ ' + i.path + ' → ' + i.nodeId).join('\n');
    assert(eq(issues.map(i => i.code + ' @ ' + i.path), [
        'content.too-long @ content',
        'embed.title.too-long @ embeds.0.title',
        'embed.url.invalid @ embeds.0.url',
        'embed.timestamp.invalid @ embeds.0.timestamp',
        'embed.author.name.required @ embeds.0.author.name',
        'embed.author.url.invalid @ embeds.0.author.url',
        'embed.image.url-invalid @ embeds.0.image.url',
        'embed.field.name.missing @ embeds.0.fields.0.name',
        'embed.field.name.too-long @ embeds.0.fields.1.name',
        'embed.field.value.too-long @ embeds.0.fields.1.value',
        'embed.thumbnail.url-invalid @ embeds.1.thumbnail.url',
    ]), 'the codes and wire paths are the server\'s, in the server\'s order', table);
    assert(eq(issues.map(i => i.nodeId), [
        'content',
        'emb-a', 'emb-a', 'emb-a', 'emb-a', 'emb-a', 'emb-a',
        'fld-a', 'fld-b', 'fld-b',
        'emb-b',
    ]),
        'every issue points at the node it is about (fields → the field, embeds → the embed, message → content)',
        issues.map(i => i.nodeId).join(','));

    // An embed with no id (never produced by the normalizer, but possible from
    // a hand-made document) must not crash or invent a node id.
    const anonymous = run(doc({ content: 'hi', embeds: [{ title: rep('t', 300) }] }));
    assert(byCode(anonymous, 'embed.title.too-long')[0].nodeId === null,
        'an embed with no id reports nodeId null rather than a made-up one',
        String(byCode(anonymous, 'embed.title.too-long')[0].nodeId));
}

// ═══════════════════════════════════════════════════════════════
section('F. the URL / timestamp mirrors of the server');
// ═══════════════════════════════════════════════════════════════
{
    [['https://example.com', true], ['http://example.com/x?y=1', true], ['HTTPS://EXAMPLE.COM', true],
     ['ftp://example.com', false], ['example.com', false], ['//example.com', false], ['', false],
     ['javascript:alert(1)', false], ['http://', false], ['https:// example.com', false],
     ['attachment://x.png', false]].forEach(([value, expected]) => {
        assert(V.isHttpUrl(value) === expected, 'isHttpUrl(' + JSON.stringify(value) + ') === ' + expected);
    });

    [['2026-09-24T12:00:00.000Z', true], ['2026-09-23T18:00:00+00:00', true], ['2026-09-23', true],
     ['2026-09-23T18:00', true], ['2026-09-23 18:00:00', true], ['2026-09-23T18:00:00-05:00', true],
     ['2028-02-29', true], ['2027-02-29', false], ['2026-02-31', false], ['2026-13-01', false],
     ['2026-09-32', false], ['2026-09-23T25:00:00Z', false], ['2026-09-23T18:60:00Z', false],
     ['yesterday', false], ['', false], ['   ', false], ['20260923', false]].forEach(([value, expected]) => {
        assert(V.isIsoTimestamp(value) === expected, 'isIsoTimestamp(' + JSON.stringify(value) + ') === ' + expected);
    });
    assert(V.isIsoTimestamp(new Date(1790186400000).toISOString()) === true,
        'the form the inspector\'s "Now" button writes is always accepted');

    // Through the rules, with the server's messages.
    {
        const bad = run(doc({ embeds: [embed({ url: 'nope' })] }));
        assert(byCode(bad, 'embed.url.invalid').length === 1 &&
               byCode(bad, 'embed.url.invalid')[0].message === 'Embed 1 URL must start with http:// or https://.',
            'a non-http title link is an error with the server\'s wording',
            JSON.stringify(codes(bad)));
        const attachment = run(doc({ embeds: [embed({ url: 'attachment://x.png' })] }));
        assert(byCode(attachment, 'embed.url.invalid').length === 0,
            'an attachment reference on the title link is not an error (the server allows it there)');
        const ts = run(doc({ embeds: [embed({ timestamp: 'soon' })] }));
        assert(byCode(ts, 'embed.timestamp.invalid')[0].message ===
               'Embed 1 timestamp must be an ISO 8601 date/time (e.g. 2026-09-23T18:00:00+00:00).',
            'an invalid timestamp is an error with the server\'s wording',
            byCode(ts, 'embed.timestamp.invalid')[0].message);
        const author = run(doc({ embeds: [embed({ author: { name: '', url: '', icon: { kind: 'url', url: 'https://x/y.png' } } })] }));
        assert(byCode(author, 'embed.author.name.required')[0].message ===
               'Embed 1 author name is required when an author icon or link is set.',
            'an icon without a name is the server\'s error, word for word',
            byCode(author, 'embed.author.name.required')[0].message);
        const footer = run(doc({ embeds: [embed({ footer: { text: '', icon: { kind: 'url', url: 'https://x/y.png' } } })] }));
        assert(byCode(footer, 'embed.footer.text.required')[0].message ===
               'Embed 1 footer text is required when a footer icon is set.',
            'and the footer has the same rule', byCode(footer, 'embed.footer.text.required')[0].message);
    }
}

// ═══════════════════════════════════════════════════════════════
section('G. the documented severity divergences');
// ═══════════════════════════════════════════════════════════════
{
    // attachment:// — a warning here (phase 1 cannot upload), an error server-side.
    const uploaded = doc({ embeds: [embed({ image: { kind: 'upload', filename: 'cat.png', assetId: 'a1' } })] });
    const issues = run(uploaded);
    const hit = byCode(issues, 'embed.image.attachment-missing')[0];
    assert(!!hit, 'an attachment reference in a media slot is reported');
    assert(hit && hit.severity === 'warning',
        'as a WARNING (the approved divergence: no uploads exist in phase 1 to satisfy it)', hit && hit.severity);
    assert(hit && hit.path === 'embeds.0.image.url' && hit.nodeId === 'emb-1',
        'on the media slot\'s wire path', hit && hit.path);
    assert(hit && hit.message === 'Embed 1 image points at the attachment "cat.png", but no file with that name is being uploaded — reattach the file before sending.',
        'with the server\'s wording, so the same problem reads the same way',
        hit && hit.message);

    // A normalized media slot with the reference but no name (the form the
    // normalizer produces for "attachment://" typed by hand into a draft).
    const nameless = run(doc({ embeds: [embed({ thumbnail: { kind: 'url', url: 'attachment://' } })] }));
    assert(byCode(nameless, 'embed.thumbnail.attachment-missing').length === 1 &&
           byCode(nameless, 'embed.thumbnail.attachment-missing')[0].message ===
           'Embed 1 thumbnail references an attachment with no file name.',
        'a reference with no file name is the server\'s other attachment message',
        JSON.stringify(byCode(nameless, 'embed.thumbnail.attachment-missing').map(i => i.message)));
    // A normalized media slot is the only shape this engine accepts (the store
    // normalizes on load); a raw string is not a document the page can hold.
    assert(run(doc({ embeds: [embed({ image: 'ftp://x/y.png' })] })).length === 0,
        'a raw (non-normalized) media string is not inspected — the store normalizes first',
        JSON.stringify(codes(run(doc({ embeds: [embed({ image: 'ftp://x/y.png' })] })))));
    assert(model.mediaToWireUrl(model.mediaFromValue('attachment://')) === 'attachment://',
        'rig: the normalizer keeps a nameless attachment reference as a url asset');

    // The wire floor that makes the empty field NAME a warning rather than an
    // error: a field with a value and no name is sent with the zero-width
    // placeholder (Discord accepts it), and a field with nothing at all is
    // dropped from the wire by embed/model.js — neither is a hard failure at
    // edit time, which is why both are warnings here.
    const wire = model.toWireEmbed(embed({ fields: [field({ name: '', value: 'V' })] }), false);
    assert(wire.fields.length === 1 && wire.fields[0].name === '\u200b' && wire.fields[0].value === 'V',
        'rig: embed/model.js sends the zero-width placeholder for a nameless field',
        JSON.stringify(wire.fields));
    const dropped = model.toWireEmbed(embed({ fields: [field({ name: '', value: '' })] }), false);
    assert(dropped.fields.length === 0,
        'rig: and a field with no name and no value never reaches the wire at all',
        JSON.stringify(dropped.fields));
    assert(byCode(run(doc({ embeds: [embed({ fields: [field({ name: '', value: '' })] })] })), 'embed.field.name.missing')[0].severity === 'warning',
        'so the empty field name is a warning here and an error at send time (documented divergence)');
}

// ═══════════════════════════════════════════════════════════════
section('H. signature: the page\'s change guard');
// ═══════════════════════════════════════════════════════════════
{
    assert(V.signature([]) === '', 'an empty issue list has an empty signature');
    assert(V.signature(null) === '' && V.signature(undefined) === '',
        'and so do the values a caller might actually pass');
    const a = run(doc({ content: rep('c', 3000) }));
    const b = run(doc({ content: rep('c', 3001) }));
    assert(V.signature(a) === V.signature(run(doc({ content: rep('c', 3000) }))),
        'the same issues always sign the same way');
    assert(V.signature(a) !== V.signature(b),
        'a different count signs differently (the strip text would change)', V.signature(a) + ' vs ' + V.signature(b));
    const two = run(doc({ content: rep('c', 3000), embeds: [embed({ title: rep('t', 300) })] }));
    assert(V.signature(two) !== V.signature(a), 'an extra issue signs differently');
    assert(V.signature(two) !== V.signature(two.slice().reverse()),
        'order matters: the first issue is what the strip shows');
}

// ═══════════════════════════════════════════════════════════════
section('I. cost on the largest document phase 1 allows (10 × 25)');
// ═══════════════════════════════════════════════════════════════
{
    const big = doc({
        content: rep('c', 2000),
        embeds: Array.from({ length: 10 }, (_, e) => embed({
            id: 'emb-' + e,
            title: rep('t', 256),
            description: rep('d', 4096),
            fields: Array.from({ length: 25 }, (_, f) => field({
                id: 'fld-' + e + '-' + f, name: 'Field ' + f, value: rep('v', 1024),
            })),
        })),
    });
    assert(run(big).length >= 10, 'rig: the maximum document has issues to find (the fields over budget)',
        String(run(big).length));
    // Warm-up runs are discarded: the first pass over a document this size
    // builds its strings for the first time, and a V8 GC can land on any single
    // run. The plan's §8.2 treats these budgets as measured-but-machine-
    // dependent, so the hard assertions are on the average and the 95th
    // percentile; the worst run is printed for the record.
    const runs = 200;
    for (let i = 0; i < 5; i++) run(big);
    const samples = [];
    for (let i = 0; i < runs; i++) {
        const started = process.hrtime.bigint();
        run(big);
        samples.push(Number(process.hrtime.bigint() - started) / 1e6);
    }
    samples.sort((a, b) => a - b);
    const average = samples.reduce((sum, ms) => sum + ms, 0) / samples.length;
    const p95 = samples[Math.floor(samples.length * 0.95) - 1];
    const worst = samples[samples.length - 1];
    console.log('    10 embeds × 25 fields: ' + average.toFixed(3) + ' ms average, ' +
                p95.toFixed(3) + ' ms p95, ' + worst.toFixed(3) + ' ms worst of ' + runs + ' runs');
    assert(average < 1, 'one pass over the maximum document costs under 1 ms on average',
        average.toFixed(3) + ' ms');
    assert(p95 < 2, 'and 95% of passes cost under 2 ms (far inside the 16 ms keystroke budget)',
        p95.toFixed(3) + ' ms');
}

// ═══════════════════════════════════════════════════════════════
section('J. the 6b readouts: counts() and caps()');
// ═══════════════════════════════════════════════════════════════
{
    // These two are what the inspector's counters and the rail's add caps are
    // painted from. They must be PROJECTIONS of the same measurement the rules
    // run on (so a counter and its rule can never disagree), they must not
    // invent a limit, and they must fail CLOSED on an unusable table.

    // ── the shapes ──
    const document_ = doc({
        content: 'Hello',
        embeds: [embed({
            id: 'emb-a', title: 'Title', description: 'Desc',
            author: { name: 'Author', url: '', icon: null },
            footer: { text: 'Footer', icon: null },
            fields: [field({ id: 'fld-a', name: 'Name', value: 'Value' })],
        })],
    });
    const c = V.counts(document_, LIMITS);
    assert(c.ok === true, 'counts() reports the table was usable');
    assert(eq(Object.keys(c.nodes), ['content', 'emb-a', 'fld-a']),
        'and addresses the document by exactly its node ids', Object.keys(c.nodes).join(','));
    assert(eq(c.nodes['content'].map(e => e.key), ['content']),
        'the message root carries its one counter');
    assert(eq(c.nodes['emb-a'].map(e => e.key),
        ['title', 'description', 'author.name', 'footer.text', 'total', 'fields']),
        'an embed carries its controls, the embed total and the fields count, in a fixed order',
        c.nodes['emb-a'].map(e => e.key).join(','));
    assert(eq(c.nodes['fld-a'].map(e => e.key), ['field.name', 'field.value']),
        'a field carries its two counters');
    assert(c.nodes['content'][0].used === 5 && c.nodes['content'][0].max === LIMITS.message.content_max,
        'a counter is {used, max} against the SERVED number, never a copy',
        JSON.stringify(c.nodes['content'][0]));
    assert(c.nodes['emb-a'].find(e => e.key === 'title').used === 5 &&
           c.nodes['emb-a'].find(e => e.key === 'title').max === LIMITS.embed.title_max,
        'and the same for an embed control');
    assert(c.nodes['emb-a'].find(e => e.key === 'fields').used === 1 &&
           c.nodes['emb-a'].find(e => e.key === 'fields').max === LIMITS.embed.fields_max,
        'the fields counter is used/max too');
    assert(c.message && c.message.embeds && c.message.embeds.used === 1 &&
           c.message.embeds.max === LIMITS.message.embeds_max,
        'and the message-level embeds fact is reported once, not per embed',
        JSON.stringify(c.message));

    // ── the embed total is the SAME arithmetic as the embed.total-chars rule ──
    const parts = embed({
        id: 'emb-b', title: rep('t', 256), description: rep('d', 4096),
        author: { name: rep('a', 256), url: '', icon: null },
        footer: { text: rep('f', 2048), icon: null },
    });
    const sum = 256 + 4096 + 256 + 2048;
    const total = V.counts(doc({ embeds: [parts] }), LIMITS).nodes['emb-b'].find(e => e.key === 'total');
    assert(total.used === sum, 'the embed total sums title+description+author+footer (+fields)',
        total.used + ' vs ' + sum);
    // Every part of that embed is individually legal, and together they are past
    // Discord's per-embed budget — which is exactly why the total is measured
    // rather than left to the parts.
    assert(total.over === true && sum > LIMITS.message.embed_total_chars_max,
        'four individually-legal parts add up past the embed budget, and the total says so',
        total.used + ' vs max ' + total.max);
    assert(byCode(run(doc({ embeds: [parts] })), 'embed.total-chars').length === 1,
        'and the RULE reports it at the same moment (one decision, two surfaces)');
    const smallEmbed = embed({
        id: 'emb-d', title: rep('t', 10), description: rep('d', 20),
        author: { name: rep('a', 5), url: '', icon: null },
        footer: { text: rep('f', 15), icon: null },
        fields: [field({ id: 'fld-d', name: 'N', value: rep('v', 50) })],
    });
    const smallTotal = V.counts(doc({ embeds: [smallEmbed] }), LIMITS).nodes['emb-d'].find(e => e.key === 'total');
    assert(smallTotal.used === 10 + 20 + 5 + 15 + 1 + 50 && smallTotal.over === false,
        'a small embed counts its fields in the total too, and stays clean', JSON.stringify(smallTotal));
    assert(V.caps(doc({ embeds: [smallEmbed] }), LIMITS).fields['emb-d'].used === 1,
        'rig: that embed has exactly one field');

    // ── `over` is the rule's own comparison, at the exact limit ──
    const at = doc({ content: rep('x', LIMITS.message.content_max) });
    const above = doc({ content: rep('x', LIMITS.message.content_max + 1) });
    assert(V.counts(at, LIMITS).nodes['content'][0].over === false,
        'exactly at the limit is NOT over (the limit is inclusive)');
    assert(V.counts(above, LIMITS).nodes['content'][0].over === true, 'one past it is');
    assert(run(at).length === 0 && byCode(run(above), 'content.too-long').length === 1,
        'and the rule agrees at both sides — the counter cannot disagree with it');

    // For every counter key there is a rule with the same boundary. Assert the
    // equivalence directly rather than trusting the shared helper by inspection.
    [['title', 'embed.title.too-long'], ['description', 'embed.description.too-long'],
     ['author.name', 'embed.author.name.too-long'], ['footer.text', 'embed.footer.text.too-long']]
        .forEach(([key, code]) => {
            const atLimit = embed({ id: 'emb-l' });
            const overLimit = embed({ id: 'emb-l' });
            const path = { 'title': 'title', 'description': 'description',
                'author.name': 'author.name', 'footer.text': 'footer.text' }[key];
            const max = { 'title': LIMITS.embed.title_max, 'description': LIMITS.embed.description_max,
                'author.name': LIMITS.embed.author_name_max, 'footer.text': LIMITS.embed.footer_text_max }[key];
            const set = (e, text) => {
                if (key === 'author.name') e.author = { name: text, url: '', icon: null };
                else if (key === 'footer.text') e.footer = { text: text, icon: null };
                else e[key] = text;
                return e;
            };
            const factsAt = V.counts(doc({ embeds: [set(atLimit, rep('x', max))] }), LIMITS).nodes['emb-l']
                .find(e => e.key === key);
            const factsOver = V.counts(doc({ embeds: [set(overLimit, rep('x', max + 1))] }), LIMITS).nodes['emb-l']
                .find(e => e.key === key);
            assert(factsAt.over === false && byCode(run(doc({ embeds: [set(embed({ id: 'emb-l' }), rep('x', max))] })), code).length === 0,
                key + ': at the served limit the counter is clean AND the rule is silent');
            assert(factsOver.over === true && byCode(run(doc({ embeds: [set(embed({ id: 'emb-l' }), rep('x', max + 1))] })), code).length === 1,
                key + ': one character past the limit both flip');
        });

    // ── caps: refused exactly AT the cap, allowed one below ──
    const fieldsAt = (n) => doc({ embeds: [embed({ id: 'emb-f', fields: Array.from({ length: n }, (_, i) => field({ id: 'fld-' + i })) })] });
    assert(V.caps(fieldsAt(LIMITS.embed.fields_max - 1), LIMITS).fields['emb-f'].canAdd === true,
        'one field below the served cap, the embed may still grow');
    const full = V.caps(fieldsAt(LIMITS.embed.fields_max), LIMITS).fields['emb-f'];
    assert(full.canAdd === false && full.used === LIMITS.embed.fields_max && full.max === LIMITS.embed.fields_max,
        'exactly AT the cap the answer is no (adding is refused where the rule is still silent)',
        JSON.stringify(full));
    assert(byCode(run(fieldsAt(LIMITS.embed.fields_max)), 'embed.fields.too-many').length === 0,
        'and the rule is still silent there — a full embed is not an error');
    assert(byCode(run(fieldsAt(LIMITS.embed.fields_max + 1)), 'embed.fields.too-many').length === 1,
        'one FIELD past the cap the rule fires (the two boundaries are one apart, by design)');

    const embedsAt = (n) => doc({ embeds: Array.from({ length: n }, (_, i) => embed({ id: 'emb-' + i })) });
    assert(V.caps(embedsAt(LIMITS.message.embeds_max - 1), LIMITS).embeds.canAdd === true,
        'one embed below the message cap, the message may still grow');
    const fullMsg = V.caps(embedsAt(LIMITS.message.embeds_max), LIMITS).embeds;
    assert(fullMsg.canAdd === false && fullMsg.used === LIMITS.message.embeds_max,
        'at the message cap the answer is no', JSON.stringify(fullMsg));
    assert(byCode(run(embedsAt(LIMITS.message.embeds_max)), 'embeds.too-many').length === 0 &&
           byCode(run(embedsAt(LIMITS.message.embeds_max + 1)), 'embeds.too-many').length === 1,
        'and the message rule starts exactly one above it');
    assert(Object.keys(V.caps(doc(), LIMITS).fields).length === 1,
        'every embed in the document has a field cap entry');
    assert(V.caps(doc(), LIMITS).fields['no-such-embed'] === undefined,
        'and an unknown embed id gets no answer at all (never a default of "yes")');

    // ── a custom table changes the facts (nothing is baked in) ──
    const small = servedLimits();
    small.message.content_max = 7;
    small.embed.title_max = 3;
    small.embed.fields_max = 1;
    small.message.embeds_max = 2;
    const custom = V.counts(doc({ content: '12345678', embeds: [embed({ id: 'emb-s', title: 'abcd' })] }), small);
    assert(custom.nodes['content'][0].max === 7 && custom.nodes['content'][0].over === true,
        'a served content_max of 7 is what the counter measures against',
        JSON.stringify(custom.nodes['content'][0]));
    assert(custom.nodes['emb-s'].find(e => e.key === 'title').over === true,
        'and a title_max of 3 flags a 4-character title');
    // The field counters measure against the FIELD limits, not the embed's: a
    // 3-character name is over a field_name_max of 2 while the same text as a
    // title is legal, so a swapped key cannot hide behind equal default numbers.
    small.embed.field_name_max = 2;
    small.embed.field_value_max = 3;
    const fieldDoc = doc({ embeds: [embed({ id: 'emb-s',
        fields: [field({ id: 'fld-s', name: 'abc', value: 'wxyz' })] })] });
    const fieldCounts = V.counts(fieldDoc, small).nodes['fld-s'];
    const nameCounter = fieldCounts.find(e => e.key === 'field.name');
    const valueCounter = fieldCounts.find(e => e.key === 'field.value');
    assert(nameCounter.max === 2 && valueCounter.max === 3,
        'the field counters use field_name_max / field_value_max (not the title/description numbers)',
        JSON.stringify(fieldCounts));
    assert(nameCounter.over === true && valueCounter.over === true &&
           byCode(run(fieldDoc, small), 'embed.field.name.too-long').length === 1 &&
           byCode(run(fieldDoc, small), 'embed.field.value.too-long').length === 1,
        'and both counters flip exactly where their own rules fire (one decision, two surfaces)');
    const oneField = V.caps(doc({ embeds: [embed({ id: 'emb-s', fields: [field({ id: 'fld-s' })] })] }), small);
    const noField = V.caps(doc({ embeds: [embed({ id: 'emb-s' })] }), small);
    assert(oneField.fields['emb-s'].canAdd === false && oneField.fields['emb-s'].max === 1,
        'a fields_max of 1 closes the add cap for an embed that already has its one field',
        JSON.stringify(oneField.fields));
    assert(noField.fields['emb-s'].canAdd === true,
        'while the same embed with no field yet may still take it', JSON.stringify(noField.fields));
    assert(V.caps(embedsAt(2), small).embeds.canAdd === false &&
           V.caps(embedsAt(2), small).embeds.max === 2,
        'and an embeds_max of 2 closes the message cap at two embeds',
        JSON.stringify(V.caps(embedsAt(2), small).embeds));

    // ── fail closed: no usable table, no numbers ──
    [undefined, null, 'nope', 42, {}, { message: {} },
     Object.assign(servedLimits(), { embed: {} })].forEach((bad, i) => {
        const c2 = V.counts(doc({ content: 'hello' }), bad);
        const k2 = V.caps(doc({ embeds: [embed({})] }), bad);
        assert(c2.ok === false && Object.keys(c2.nodes).length === 0 && c2.message === null,
            'counts() refuses table #' + i + ' outright (no numbers at all)');
        assert(k2.ok === false && k2.embeds.canAdd === false &&
               k2.embeds.used === 0 && Object.keys(k2.fields).length === 0,
            'and caps() answers "cannot add" for table #' + i + ' (fail closed, never unlimited)');
    });

    // ── purity: two calls, two independent results, no state kept ──
    const first = V.counts(document_, LIMITS);
    first.nodes['emb-a'].push({ key: 'mutant', used: 1, max: 1 });
    first.nodes['content'][0].used = 9999;
    const second = V.counts(document_, LIMITS);
    assert(second.nodes['emb-a'].length === 6 && second.nodes['content'][0].used === 5,
        'mutating a returned readout cannot reach the next call (no shared state)');
    assert(eq(second, V.counts(document_, LIMITS)), 'and two calls with the same input are identical');
    assert(eq(V.counts(doc(), LIMITS), V.counts(doc(), LIMITS)),
        'including for a document with nothing in it', JSON.stringify(V.counts(doc(), LIMITS)));
    assert(eq(V.caps(document_, LIMITS), V.caps(document_, LIMITS)), 'caps() is deterministic too');
    assert(run(document_).length === run(document_).length,
        'and measuring does not change what the rules say afterwards');

    // ── ids: the readouts address the SAME nodes the issues do ──
    const bad = doc({
        content: rep('x', LIMITS.message.content_max + 1),
        embeds: [embed({ id: 'emb-z', title: rep('t', LIMITS.embed.title_max + 1),
                         fields: [field({ id: 'fld-z', name: rep('n', LIMITS.embed.field_name_max + 1), value: 'ok' })] })],
    });
    const issueNodes = run(bad).map(i => i.nodeId);
    const countNodes = Object.keys(V.counts(bad, LIMITS).nodes);
    issueNodes.forEach(id => assert(countNodes.indexOf(id) !== -1,
        'every node an issue points at (' + id + ') has a readout under the same id'));
    assert(countNodes.indexOf('content') !== -1 && countNodes.indexOf('emb-z') !== -1 &&
           countNodes.indexOf('fld-z') !== -1, 'and the message, embed and field are all addressed',
        countNodes.join(','));

    // ── cost: the readouts are the same order as one validation pass ──
    const big = doc({
        content: rep('c', 500),
        embeds: Array.from({ length: 10 }, (_, e) => embed({
            id: 'emb-' + e, title: rep('t', 100),
            fields: Array.from({ length: 25 }, (_, f) => field({ id: 'f' + e + '-' + f, value: rep('v', 200) })),
        })),
    });
    for (let i = 0; i < 3; i++) { V.counts(big, LIMITS); V.caps(big, LIMITS); }
    const started = process.hrtime.bigint();
    for (let i = 0; i < 50; i++) { V.counts(big, LIMITS); V.caps(big, LIMITS); }
    const perCall = Number(process.hrtime.bigint() - started) / 1e6 / 50;
    console.log('    counts() + caps() on the maximum document: ' + perCall.toFixed(3) + ' ms per pair');
    assert(perCall < 2, 'measuring the maximum document stays far inside the keystroke budget',
        perCall.toFixed(3) + ' ms');
}

// ═══════════════════════════════════════════════════════════════
console.log('\nmessage-builder validate: ' + pass + ' passed, ' + fail + ' failed');
if (fail) {
    console.log('Failures:');
    failures.forEach(f => console.log(' -', f));
    process.exit(1);
}
console.log('ALL MESSAGE-BUILDER VALIDATE CHECKS PASSED');
