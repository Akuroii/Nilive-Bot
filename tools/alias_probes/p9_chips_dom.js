#!/usr/bin/env node
/* PROBE 9 — the alias chip field's actual behaviour, in a real DOM.
 *
 * jsdom + the shipped component and the shipped template markup: typing a
 * word and pressing Space, the × on each chip, Backspace, paste splitting,
 * duplicate/refusal hints, and the conflict ⚠ that comes from
 * /api/commands/alias-registry. Compiled Python proves nothing about this
 * file, so it is driven directly.
 *
 *   node tools/alias_probes/p9_chips_dom.js
 * Needs jsdom:  npm i jsdom   (JSDOM_PATH=/path/to/node_modules/jsdom if it
 * lives outside the project — this repo deliberately does not ship a package.json)
 */
const fs = require('fs');
const path = require('path');

const ROOT = path.resolve(__dirname, '..', '..');
let jsdom;
try {
    jsdom = require(process.env.JSDOM_PATH || 'jsdom');
} catch (e) {
    try {
        jsdom = require('/home/user/.cache/jstest/node_modules/jsdom');
    } catch (e2) {
        console.error('jsdom not found — set JSDOM_PATH (see header).');
        process.exit(2);
    }
}
const { JSDOM } = jsdom;

const TEMPLATE = fs.readFileSync(
    path.join(ROOT, 'dashboard/templates/manage/commands.html'), 'utf8');
const COMPONENT = fs.readFileSync(
    path.join(ROOT, 'dashboard/static/js/nero-alias-input.js'), 'utf8');
// Embed the stylesheet so jsdom parses the chip CSS rules. Section 10
// reads computed style on .na-chip-edit and .na-chip, which requires the
// stylesheet to be in the document — bare HTML alone doesn't propagate
// rules into getComputedStyle.
const CSS = fs.readFileSync(
    path.join(ROOT, 'dashboard/static/css/main.css'), 'utf8');

// Pull the real markup out of the template so this test cannot drift from it.
const blockMatch = TEMPLATE.match(
    /<div class="form-group"[^>]*>\s*<label class="form-label">Aliases<\/label>([\s\S]*?)<\/div>\s*<\/div>/);
if (!blockMatch) {
    console.error('Could not find the Aliases form-group in the template.');
    process.exit(2);
}
const aliasBlock = blockMatch[0].replace(/\{\{\s*cmd\s*\}\}/g, 'kick')
    .replace(/\{\{ cmd \}\}/g, 'kick');

let checks = 0;
const failures = [];

function check(label, condition, detail) {
    checks += 1;
    const ok = !!condition;
    console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${label}` +
        (detail !== undefined ? `  -> ${JSON.stringify(detail).slice(0, 140)}` : ''));
    if (!ok) failures.push(label);
}

const REGISTRY = {
    success: true,
    aliases: {
        kk: { command: 'ban', scope: 'server', enabled: 1 },
    },
    prefix_commands: ['help', 'reload', 'sync'],
    slash_commands: ['kick', 'ban'],
    custom_commands: ['cc'],
    trigger_words: ['k'],
    pending_resync: false,
    last_sync: { at: 1, registered: 1, skipped: [], error: null },
};

async function main() {
    console.log('\n=== 0. markup sanity ===');
    check('template carries the chip host', /data-alias-input/.test(aliasBlock));
    check('…with a hidden input holding the comma string',
        /<input type="hidden" name="aliases-kick"/.test(aliasBlock));

    const dom = new JSDOM(`<!doctype html><html><head><style>${CSS}</style></head><body>${aliasBlock}</body></html>`, {
        runScripts: 'outside-only',
        pretendToBeVisual: true,
    });
    const { window } = dom;
    window.fetch = async (url) => ({
        ok: true,
        json: async () => REGISTRY,
    });
    window.eval(COMPONENT);
    window.document.dispatchEvent(
        new window.Event('DOMContentLoaded', { bubbles: true }));
    await flush(window);

    const host = window.document.querySelector('[data-alias-input]');
    const input = host.querySelector('input[name]');
    const text = host.querySelector('.na-input');
    const hint = host.querySelector('.na-hint');
    const chips = () => Array.from(host.querySelectorAll('.na-chip'));
    const chipWords = () => chips().map(c => c.querySelector('.na-chip-text').textContent);
    const value = () => input.value;

    console.log('\n=== 1. the field is a chip box, not a text input ===');
    check('component initialised', host.dataset.naInit === '1');
    check('shows a text box for typing', !!text);
    check('no visible comma-separated input left', !host.querySelector('input.form-input'));

    console.log('\n=== 2. type `k`, press Space → chip ===');
    type(window, text, 'k');
    key(window, text, ' ');
    await flush(window);
    check('a chip appeared', chips().length === 1, chipWords());
    check('chip text is the alias', chipWords()[0] === 'k');
    check('hidden input carries it', value() === 'k', value());
    check('the box was emptied for the next word', text.value === '');
    check('single character accepted (no minimum length)', chips().length === 1);
    check('Space did not type a space into the box', !/\s/.test(text.value));
    check('every chip has a × button', !!chips()[0].querySelector('.na-chip-x'));

    console.log('\n=== 3. a second chip, then remove them one at a time ===');
    type(window, text, 'kk');
    key(window, text, 'Enter');
    await flush(window);
    check('two chips now', chipWords().join(',') === 'k,kk', chipWords());
    check('hidden value joined', value() === 'k,kk', value());
    const kkChip = chips()[1];
    check('the word another command claims gets ⚠',
        kkChip.classList.contains('na-chip--warn') && /\/ban/.test(kkChip.title),
        kkChip.title);
    check('…and is still kept (advisory, not a block)', chipWords().length === 2);

    kkChip.querySelector('.na-chip-x').click();
    await flush(window);
    check('clicking × removed just that chip', chipWords().join(',') === 'k', chipWords());
    check('hidden input followed', value() === 'k', value());

    console.log('\n=== 4. Backspace on an empty box pops the last chip ===');
    key(window, text, 'Backspace');
    await flush(window);
    check('chip popped', chips().length === 0, chipWords());
    check('hidden input cleared', value() === '', value());

    console.log('\n=== 5. refusals explain themselves ===');
    type(window, text, 'my alias');
    key(window, text, ' ');
    await flush(window);
    check('multi-word rejected with a spaces-specific message',
        chips().length === 0 && /contain spaces/.test(hint.textContent),
        hint.textContent);
    check('the bad word stays selected for fixing', text.value.length > 0, text.value);

    type(window, text, 'k!');
    key(window, text, ' ');
    await flush(window);
    check('punctuation rejected', chips().length === 0 && /letters/.test(hint.textContent),
        hint.textContent);
    check('the bad word stays in the box to be fixed, not lost',
        text.value === 'k!', text.value);

    type(window, text, 'k'.repeat(33));
    key(window, text, ' ');
    await flush(window);
    check('over-length is truncated to 32 with an explanatory note',
        chipWords().length === 1 && chipWords()[0].length === 32 &&
        /cut at 32/.test(hint.textContent),
        { word: chipWords()[0], hint: hint.textContent });

    console.log('\n=== 6. what IS allowed ===');
    host._neroAlias.setValues([]);
    await flush(window);
    check('setValues([]) clears the field', chips().length === 0, chipWords());
    type(window, text, 'AB');
    key(window, text, ' ');
    await flush(window);
    check('case normalised to lowercase', chipWords()[0] === 'ab', chipWords());
    type(window, text, 'k_x');
    key(window, text, ' ');
    await flush(window);
    check('underscore accepted', chipWords().includes('k_x'), chipWords());
    type(window, text, 'ض');
    key(window, text, ' ');
    await flush(window);
    check('non-latin single char accepted', chipWords().includes('ض'), chipWords());
    type(window, text, '!bang');
    key(window, text, ' ');
    await flush(window);
    check('a pasted leading ! is stripped, not rejected',
        chipWords().includes('bang'), chipWords());
    type(window, text, 'ab');
    key(window, text, ' ');
    await flush(window);
    check('duplicate refused with a hint',
        chipWords().filter(w => w === 'ab').length === 1 && /already/.test(hint.textContent),
        hint.textContent);
    check('…and the accidental second Space did NOT delete the existing chip',
        chipWords().includes('ab'), chipWords());

    console.log('\n=== 7. paste of several words splits into chips ===');
    const before = chipWords().length;
    const dt = new window.Event('paste');
    dt.clipboardData = { getData: () => 'one, two three\nfour' };
    text.dispatchEvent(dt);
    await flush(window);
    check('four more chips from one paste',
        chipWords().slice(before).join(',') === 'one,two,three,four',
        chipWords().slice(before));

    console.log('\n=== 8. save path reads the chips ===');
    const payload = value();
    const parsed = payload.split(',').map(a => a.trim()).filter(Boolean);
    check('the hidden input holds every chip, comma-joined',
        parsed.length === 8, { payload, n: parsed.length });
    check('legacy comma-split (the code the page already uses) round-trips',
        parsed.join(',') === chipWords().join(','), { parsed, chipWords: chipWords() });
    const serverSide = parsed.filter(a => a.replace(/[-_]/g, '').length > 0)
        .map(a => a.normalize('NFKC'));
    check('nothing in the payload would be rejected by the server',
        serverSide.every(a => a.length <= 32 && /^[\p{L}\p{N}_-]+$/u.test(a)),
        serverSide);

    console.log('\n=== 9. pre-existing aliases render as chips on load ===');
    host._neroAlias.setValues(['k', 'kick']);
    await flush(window);
    check('setValues renders', chipWords().join(',') === 'k,kick', chipWords());
    check('NeroAlias.getValues agrees',
        window.NeroAlias.getValues(host).join(',') === 'k,kick');
    const kChip = chips()[0];
    kChip.click();
    await flush(window);
    check('clicking a chip drops it back into the box to re-edit',
        text.value === 'k' && chipWords().join(',') === 'kick',
        { box: text.value, chips: chipWords() });
    check('…with a hint about how to put it back',
        /Space puts it back/.test(hint.textContent), hint.textContent);
    type(window, text, 'k');
    key(window, text, ' ');
    await flush(window);
    check('pressing Space again restores the chip (it moves to the end, which '
          + 'is cosmetic — the server keeps submission order)',
        chipWords().slice().sort().join(',') === 'k,kick', chipWords());
    const lastWord = chipWords()[chipWords().length - 1];
    chips()[chips().length - 1].querySelector('.na-chip-x').click();
    await flush(window);
    check('× removes exactly the chip it was on',
        chipWords().length === 1 && chipWords()[0] !== lastWord,
        { removed: lastWord, left: chipWords() });

    console.log('\n=== 10. edit affordance: chip is keyboard-accessible, has a hint ===');
    // Reset to a clean two-chip state for the affordance tests.
    host._neroAlias.setValues(['aa', 'bb']);
    await flush(window);
    const [aChip, bChip] = chips();
    // Markup: the chip itself is focusable and labelled for screen readers.
    check('chip is in the tab order (tabIndex=0)', aChip.tabIndex === 0);
    check('chip carries role=button for screen readers',
        aChip.getAttribute('role') === 'button');
    check('chip aria-label mentions the word and how to edit',
        /Edit alias aa/.test(aChip.getAttribute('aria-label') || ''),
        aChip.getAttribute('aria-label'));
    // A small pencil element is in the DOM, hidden from assistive tech.
    const pencil = aChip.querySelector('.na-chip-edit');
    check('a .na-chip-edit pencil element is present', !!pencil);
    check('the pencil is hidden from assistive tech',
        pencil && pencil.getAttribute('aria-hidden') === 'true');
    // The pencil is absolutely positioned so it never contributes to the
    // chip's resting width. A future regression that puts it back in the
    // flex flow (display: inline-block / inline) would widen the chip at
    // rest and break this assertion.
    check('the pencil uses position: absolute (zero resting-width contribution)',
        window.getComputedStyle(pencil).position === 'absolute',
        window.getComputedStyle(pencil).position);
    check('the chip is the positioning context for the pencil (position: relative)',
        window.getComputedStyle(aChip).position === 'relative',
        window.getComputedStyle(aChip).position);
    // The × button still has its own aria-label and is its own focus stop.
    const xBtn = aChip.querySelector('.na-chip-x');
    check('the × button keeps its own aria-label',
        /Remove alias aa/.test(xBtn.getAttribute('aria-label') || ''),
        xBtn.getAttribute('aria-label'));

    // Keyboard activation: focusing the chip and pressing Enter re-edits.
    aChip.focus();
    key(window, aChip, 'Enter');
    await flush(window);
    check('Enter on a focused chip re-edits it',
        text.value === 'aa' && chipWords().join(',') === 'bb',
        { box: text.value, chips: chipWords() });
    // Restore the chip.
    type(window, text, 'aa');
    key(window, text, ' ');
    await flush(window);

    // Keyboard activation: Space on a focused chip also re-edits.
    const freshChips = chips();
    const c0 = freshChips[0];
    c0.focus();
    key(window, c0, ' ');
    await flush(window);
    check('Space on a focused chip re-edits it',
        text.value.length > 0 && chips().length === 1,
        { box: text.value, chips: chipWords() });
    // Restore again.
    type(window, text, c0.querySelector('.na-chip-text').textContent);
    key(window, text, ' ');
    await flush(window);

    // The × button's own keyboard activation still works through the chip's
    // keydown handler. Because the handler early-returns when e.target is
    // not the chip, the × button's native button-Enter semantics fire
    // instead. Simulate by dispatching a click on × after focusing it.
    const beforeRemove = chipWords().length;
    xBtn.focus();
    const click = new window.MouseEvent('click', { bubbles: true });
    xBtn.dispatchEvent(click);
    await flush(window);
    check('clicking × from a focused state still removes just that chip',
        chipWords().length === beforeRemove - 1, chipWords());

    console.log(`\n${'='.repeat(60)}`);
    if (failures.length) {
        console.log('FAILED:\n  - ' + failures.join('\n  - '));
        process.exit(1);
    }
    console.log(`${checks - failures.length}/${checks} checks passed`);
}

function type(window, el, value) {
    el.value = value;
    el.dispatchEvent(new window.Event('input', { bubbles: true }));
}

function key(window, el, k) {
    const ev = new window.Event('keydown', { bubbles: true, cancelable: true });
    Object.defineProperty(ev, 'key', { value: k });
    el.dispatchEvent(ev);
    return ev;
}

function flush(window) {
    // let the component's promise chains (registry fetch) settle
    return new Promise(resolve => setTimeout(resolve, 5));
}

main().catch(e => { console.error(e); process.exit(2); });
