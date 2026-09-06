#!/usr/bin/env node
/* PROBE 13 — the role / channel pickers render real data, not fallbacks.
 *
 * The 2026-08-28 dashboard bug: in the Commands edit panel and Category
 * restrictions, selecting a role or channel showed a blank / "weird square"
 * instead of the name + colour dot / type icon. Two independent causes:
 *
 *   1. DATA LOSS (nero-select.js) — Select2 4.1.0-rc.0 hands its templates a
 *      data object built only from the <option> ({id, text, disabled,
 *      selected, title, element}). The API's ``color`` / ``type_icon`` fields
 *      were dropped, so every role dot rendered the grey fallback and every
 *      channel icon the generic 💬. Fix: stamp ``data-color`` /
 *      ``data-type-icon`` on each <option> and read them back off
 *      ``opt.element.dataset`` (with an opt.text -> option-text -> id fallback
 *      so the name is never undefined/empty, inserted as textContent so a
 *      name with &/< never breaks the markup).
 *
 *   2. CLIPPING (main.css) — the multi-select pill padding override shrank the
 *      left inset below the absolutely-positioned × button, hiding the first
 *      characters of every selected name. (Covered by the CSS, not by DOM.)
 *
 * This probe loads REAL jQuery + REAL Select2 4.1.0-rc.0 in jsdom (not the
 * stub p11 uses) and asserts the rendered output. It self-SKIPS (exit 0) when
 * jsdom / jquery / select2 are not installed, so it never breaks the minimal
 * documented setup — run it where the deps exist:
 *
 *   cd <any dir> && npm i jsdom jquery select2@4.1.0-rc.0
 *   JSDOM_PATH=$(pwd)/node_modules/jsdom node tools/alias_probes/p13_picker_render.js
 *
 * Exit code 0 = every check passed (or cleanly skipped).
 */
'use strict';
const fs = require('fs');
const path = require('path');
const os = require('os');

const ROOT = path.resolve(__dirname, '..', '..');

// ── dependency resolution ────────────────────────────────────────
// Find a node_modules dir that has ALL of jsdom, jquery, select2, then load
// the real browser builds of jquery + select2 into a jsdom window.
function candidateDirs() {
    const dirs = [];
    if (process.env.JSDOM_PATH) {
        // JSDOM_PATH may be a module path (.../node_modules/jsdom) or a
        // node_modules dir itself.
        dirs.push(path.dirname(process.env.JSDOM_PATH));
        dirs.push(process.env.JSDOM_PATH);
    }
    dirs.push(
        path.join(os.homedir(), '.cache', 'jstest', 'node_modules'),
        '/tmp/node_modules',
        '/tmp/jstest/node_modules',
        path.resolve(process.cwd(), 'node_modules'),
    );
    return dirs;
}

function findDeps() {
    for (const dir of candidateDirs()) {
        if (!dir || !fs.existsSync(dir)) continue;
        const jsdom = path.join(dir, 'jsdom');
        const jq = [path.join(dir, 'jquery', 'dist', 'jquery.min.js'),
                    path.join(dir, 'jquery', 'dist', 'jquery.js')];
        const s2 = path.join(dir, 'select2', 'dist', 'js', 'select2.js');
        const jqFile = jq.find(f => fs.existsSync(f));
        if (fs.existsSync(jsdom) && jqFile && fs.existsSync(s2)) {
            return { jsdom: path.join(dir, 'jsdom'), jquery: jqFile, select2: s2 };
        }
    }
    return null;
}

const deps = findDeps();
if (!deps) {
    console.log('\n[SKIP] p13_picker_render — jsdom + jquery + select2 not all ' +
        'found (set JSDOM_PATH to a node_modules dir that has them). Nothing to assert; skipping.');
    process.exit(0);
}

const { JSDOM } = require(deps.jsdom);
const JQ = fs.readFileSync(deps.jquery, 'utf8');
const S2 = fs.readFileSync(deps.select2, 'utf8');
const SEL = fs.readFileSync(path.join(ROOT, 'dashboard/static/js/nero-select.js'), 'utf8');

// Real API shapes, as dashboard/api/core.py returns them (id, name, text,
// color / type_icon) — plus edge cases: a null colour, a missing icon, and a
// name full of HTML-special characters.
const ROLES = { results: [
    { id: '1001', name: 'Owner', text: 'Owner', color: '#ed4245', position: 10, managed: false },
    { id: '1002', name: 'Moderator', text: 'Moderator', color: '#57f287', position: 5, managed: false },
    { id: '1003', name: 'No Color', text: 'No Color', color: null, position: 1, managed: false },
    { id: '1004', name: 'a<b & "x"', text: 'a<b & "x"', color: '#000000', position: 0, managed: false },
] };
const CHANNELS = { results: [
    { id: '2001', name: 'general', text: 'general', type_icon: '💬', category: 'Chat', type: 'text' },
    { id: '2002', name: 'voice-lounge', text: 'voice-lounge', type_icon: '🔊', category: 'Voice', type: 'voice' },
    { id: '2003', name: 'announcements', text: 'announcements', type_icon: '📢', category: null, type: 'announcement' },
    { id: '2004', name: 'no icon', text: 'no icon', type_icon: null, category: null, type: 'text' },
] };

let checks = 0;
const failures = [];
function check(label, ok, detail) {
    checks += 1;
    console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${label}` +
        (detail !== undefined ? `  -> ${JSON.stringify(detail).slice(0, 200)}` : ''));
    if (!ok) failures.push(label);
}
// jsdom normalises inline `#rrggbb` to `rgb(r, g, b)` — compare as colours.
function colorEq(a, b) {
    const norm = (c) => {
        if (!c) return '';
        c = String(c).trim().toLowerCase();
        if (c[0] === '#') {
            let h = c.slice(1);
            if (h.length === 3) h = h.split('').map(x => x + x).join('');
            return [0, 2, 4].map(i => parseInt(h.slice(i, i + 2), 16)).join(',');
        }
        const m = c.match(/rgba?\(([^)]+)\)/);
        if (m) return m[1].split(',').slice(0, 3).map(s => parseInt(s.trim(), 10)).join(',');
        return c;
    };
    return norm(a) === norm(b);
}

function makeWindow() {
    const dom = new JSDOM(`<!doctype html><html><body>
        <select id="rp" class="nero-role-picker" data-value="1002"></select>
        <select id="cp" class="nero-channel-picker" data-value="2002"></select>
        <select id="mr" multiple></select>
        <select id="mc" multiple></select>
    </body></html>`, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://localhost:5000/commands' });
    const { window } = dom;
    window.HTMLElement.prototype.scrollIntoView = function () {};
    window.fetch = async (url) => {
        const s = String(url);
        if (s.includes('/roles')) return { ok: true, json: async () => ROLES };
        if (s.includes('/channels')) return { ok: true, json: async () => CHANNELS };
        return { ok: true, json: async () => ({}) };
    };
    window.eval(JQ);
    window.eval(S2);
    window.eval(SEL);
    return window;
}

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

async function main() {
    const w = makeWindow();
    const doc = w.document;
    const $ = w.jQuery;

    // initAll() runs the single pickers; the page calls initMulti lazily.
    doc.dispatchEvent(new w.Event('DOMContentLoaded', { bubbles: true }));
    w.eval(`NeroSelect.initMulti(document.getElementById('mr'), 'role', ['1001','1004']);
            NeroSelect.initMulti(document.getElementById('mc'), 'channel', ['2002','2003','2004']);`);
    await sleep(300);

    console.log('\n=== 1. the <option> carries the custom display data ===');
    const op = doc.querySelector('#rp option[value="1002"]');
    check('role <option> stamps data-color', op && op.getAttribute('data-color') === '#57f287',
        op && op.outerHTML);
    check('role <option> text is the name', op && op.textContent === 'Moderator', op && op.textContent);
    const oc = doc.querySelector('#cp option[value="2003"]');
    check('channel <option> stamps data-type-icon', oc && oc.getAttribute('data-type-icon') === '📢',
        oc && oc.outerHTML);
    const ocSp = doc.querySelector('#rp option[value="1004"]');
    check('special-char role name is valid escaped markup',
        ocSp && ocSp.textContent === 'a<b & "x"', ocSp && ocSp.outerHTML);

    console.log('\n=== 2. single-select selection (templateSelection) ===');
    const rRendered = doc.querySelector('#rp + .select2-container .select2-selection__rendered');
    check('role name renders', rRendered && rRendered.textContent.includes('Moderator'),
        rRendered && rRendered.textContent);
    const rDot = rRendered && rRendered.querySelector('.ns-color-dot');
    check('role dot uses the REAL colour (not the grey fallback)',
        rDot && colorEq(rDot.style.background, '#57f287') && !colorEq(rDot.style.background, '#99aab5'),
        rDot && rDot.getAttribute('style'));
    const cRendered = doc.querySelector('#cp + .select2-container .select2-selection__rendered');
    check('channel name renders', cRendered && cRendered.textContent.includes('voice-lounge'),
        cRendered && cRendered.textContent);
    const cIcon = cRendered && cRendered.querySelector('.ns-ch-icon');
    check('channel icon uses the REAL type icon (🔊, not the generic 💬)',
        cIcon && cIcon.textContent === '🔊', cIcon && cIcon.textContent);

    console.log('\n=== 3. multi-select pills (the reported Commands / Category surface) ===');
    function choiceTexts(sel) {
        return Array.from(doc.querySelectorAll(sel + ' + .select2-container .select2-selection__choice'))
            .map(c => c.textContent.replace(/\s+/g, ' ').trim());
    }
    const mrTexts = choiceTexts('#mr');
    check('multi role pills show their names',
        mrTexts.some(t => t.includes('Owner')) && mrTexts.some(t => t.includes('a<b & "x"')), mrTexts);
    check('no pill name is "undefined" or blank',
        mrTexts.length > 0 && mrTexts.every(t => !/undefined/.test(t) && t.replace(/[×\s]/g, '').length > 0),
        mrTexts);
    const mcTexts = choiceTexts('#mc');
    check('multi channel pills show their names',
        mcTexts.some(t => t.includes('voice-lounge')) && mcTexts.some(t => t.includes('announcements')), mcTexts);
    const mcPills = Array.from(doc.querySelectorAll('#mc + .select2-container .select2-selection__choice'));
    const pillFor = (name) => mcPills.find(p => p.textContent.includes(name));
    check('each pill carries its OWN correct icon (voice-lounge 🔊)',
        pillFor('voice-lounge') && pillFor('voice-lounge').querySelector('.ns-ch-icon').textContent === '🔊');
    check('each pill carries its OWN correct icon (announcements 📢)',
        pillFor('announcements') && pillFor('announcements').querySelector('.ns-ch-icon').textContent === '📢');
    check('channel missing type_icon falls back to 💬',
        pillFor('no icon') && pillFor('no icon').querySelector('.ns-ch-icon').textContent === '💬');

    console.log('\n=== 4. dropdown rows (templateResult) ===');
    // jsdom never completes select2's async result rendering (the
    // .select2-dropdown container appears but options don't), so drive the
    // registered templateResult directly with the exact data shape
    // SelectAdapter.item() hands it: {id, text, disabled, selected, title,
    // element} — the same shape (and same functions) the open dropdown uses.
    function callTemplate(id, optValue) {
        const s2 = $(doc.getElementById(id)).data('select2');
        const opts = (s2.options && s2.options.options) || s2.options || {};
        const optionEl = doc.querySelector(`#${id} option[value="${optValue}"]`);
        const data = { id: optionEl.value, text: optionEl.text,
                       disabled: optionEl.disabled, selected: optionEl.selected,
                       title: optionEl.title || '', element: optionEl };
        return opts.templateResult(data).wrap('<div>').parent().html();
    }
    const rpRowHtml = callTemplate('rp', '1002');
    const rpDotStyle = (rpRowHtml.match(/background:\s*([^;"']+)/) || [])[1] || '';
    check('templateResult renders the role row with name + real colour',
        rpRowHtml.includes('Moderator') && colorEq(rpDotStyle, '#57f287') && !colorEq(rpDotStyle, '#99aab5'),
        rpRowHtml);
    const rpRow2 = callTemplate('rp', '1004');
    check('templateResult keeps a special-char name intact as text',
        rpRow2.includes('a&lt;b &amp; "x"') && !rpRow2.includes('>a<b'), rpRow2);
    const mcRowHtml = callTemplate('mc', '2003');
    check('templateResult renders the channel row with name + real icon',
        mcRowHtml.includes('announcements') && mcRowHtml.includes('📢'), mcRowHtml);
    const mcRow2 = callTemplate('mc', '2004');
    check('templateResult falls back to 💬 when a channel has no icon',
        mcRow2.includes('no icon') && mcRow2.includes('💬'), mcRow2);

    console.log(`\n${'='.repeat(60)}`);
    if (failures.length) {
        console.log(`FAILED:\n  - ${failures.join('\n  - ')}`);
        process.exit(1);
    }
    console.log(`${checks}/${checks} checks passed`);
}

main().catch(e => { console.error(e); process.exit(2); });
