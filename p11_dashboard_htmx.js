#!/usr/bin/env node
/* PROBE 11 — full alias lifecycle in the real dashboard, including the
 * path that the previous investigation proved broken in production:
 *
 *     htmx navigation → /commands page swap → host un-initialised
 *                                              → user clicks Edit
 *                                              → empty chip box
 *
 * And the second bug it proved would silently wipe role data:
 *
 *     picker fails to init → save path posts []
 *
 * Uses jsdom + the real templates / scripts. Fails fast on any of the
 * nine acceptance criteria the user listed.
 *
 *   node tools/alias_probes/p11_dashboard_htmx.js
 *
 * Needs jsdom. Same conventions as p9.
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

const TEMPLATE       = fs.readFileSync(path.join(ROOT, 'dashboard/templates/manage/commands.html'), 'utf8');
const ALIAS_JS       = fs.readFileSync(path.join(ROOT, 'dashboard/static/js/nero-alias-input.js'), 'utf8');
const SELECT_JS      = fs.readFileSync(path.join(ROOT, 'dashboard/static/js/nero-select.js'), 'utf8');
const DASHBOARD_JS   = fs.readFileSync(path.join(ROOT, 'dashboard/static/js/dashboard.js'), 'utf8');

// Inline page-level script (defines toggleEditPanel, loadCommandSettings,
// saveCommandSettings, refreshRowChips, ajaxSave, showToast, setLoading…).
// The script block is server-rendered by Jinja, so the CATEGORY_COMMANDS
// table on lines 652-654 carries `{% for %}` tags that the browser never
// sees but the probe would. Replace the table with an empty literal so
// the script parses.
const pageScriptMatch = TEMPLATE.match(/<script>([\s\S]*?)<\/script>/);
if (!pageScriptMatch) { console.error('no inline <script> in template'); process.exit(2); }
const PAGE_SCRIPT = pageScriptMatch[1].replace(
    /const\s+CATEGORY_COMMANDS\s*=\s*\{[\s\S]*?\};/,
    'const CATEGORY_COMMANDS = {};');

// Extract the single command row markup from the template. The row
// contains nested {% for param %} / {% if %} blocks, so a non-greedy
// regex would stop at the first inner {% endfor %}. Walk the source
// counting open / close tags to find the matching close.
function extractCommandRow() {
    const openRe = /\{%\s*for\s+cmd\s+in\s+cmds\s*%\}/;
    const start = TEMPLATE.search(openRe);
    if (start < 0) throw new Error('command list for-loop not found in template');
    let depth = 0, end = -1;
    // `set` has no `endset` in Jinja, so we don't count it as an opener
    // for the depth balance — we just strip it later.
    const tagRe = /\{%\s*(for|endfor|if|endif)\b[^%]*%\}/g;
    tagRe.lastIndex = start;
    let m;
    while ((m = tagRe.exec(TEMPLATE))) {
        const tok = m[1];
        if (tok === 'for' || tok === 'if') depth++;
        else if (tok === 'endfor' || tok === 'endif') {
            depth--;
            if (depth === 0) { end = m.index + m[0].length; break; }
        }
    }
    if (end < 0) throw new Error('did not find matching endfor for cmd row');
    let row = TEMPLATE.slice(start, end);
    row = row.replace(/\{%\s*set\s[^%]*%\}/g, '')
             .replace(/\{%\s*if[^%]*%\}/g, '')
             .replace(/\{%\s*endif\s*%\}/g, '')
             .replace(/\{%\s*for\s+param[^%]*%\}/g, '')
             .replace(/\{%\s*for\s+cmd\s+in\s+cmds\s*%\}/, '')
             .replace(/\{%\s*endfor\s*%\}/g, '')
             .replace(/\{\{ cmd \}\}/g, 'kick')
             .replace(/\{\{[^}]*\}\}/g, '');
    return row;
}

let checks = 0;
const failures = [];
function check(label, ok, detail) {
    checks += 1;
    const status = ok ? 'PASS' : 'FAIL';
    console.log(`  [${status}] ${label}` +
        (detail !== undefined ? `  -> ${JSON.stringify(detail).slice(0, 180)}` : ''));
    if (!ok) failures.push(label);
}

function flush() { return new Promise(r => setTimeout(r, 10)); }

function key(window, el, k) {
    const ev = new window.Event('keydown', { bubbles: true, cancelable: true });
    Object.defineProperty(ev, 'key', { value: k });
    el.dispatchEvent(ev);
    return ev;
}
function type(window, el, v) {
    el.value = v;
    el.dispatchEvent(new window.Event('input', { bubbles: true }));
}

// Stubbed response for /api/commands/settings/<cmd>. Tuned per scenario.
function makeWindow({ aliases, roles = [] } = {}) {
    const rowHtml = extractCommandRow();
    const dom = new JSDOM(
        `<!doctype html><html><body><div id="content-area">${rowHtml}</div></body></html>`,
        { runScripts: 'outside-only', pretendToBeVisual: true,
          url: 'http://localhost:5000/commands' });
    const { window } = dom;
    window.HTMLElement.prototype.scrollIntoView = function () {};
    window.fetch = async (url, init) => {
        const s = String(url);
        if (s.includes('/api/commands/settings/')) {
            if (init && init.method === 'POST') {
                // Capture the POST body the save path actually emits.
                // The real dashboard.js ajaxSave uses fetch(), so this
                // is what we'll see in production.
                window.__lastAjax = { url: s, payload: JSON.parse(init.body) };
                return { ok: true, json: async () => ({ success: true, warnings: [] }) };
            }
            return { ok: true, json: async () => ({
                command_name: 'kick', enabled: 1, aliases: aliases || [],
                enabled_roles: roles, disabled_roles: [],
                enabled_channels: [], disabled_channels: [],
                cooldown_seconds: 5,
            }) };
        }
        return { ok: true, json: async () => ({}) };
    };
    // jQuery / select2 stub — NeroSelect needs it. We never let the
    // multi pickers actually attach select2 because the probe is about
    // the alias lifecycle, but the select2 calls must not throw.
    const $ = function (sel) {
        const arr = sel && sel.nodeType ? [sel]
                  : typeof sel === 'string' ? Array.from(window.document.querySelectorAll(sel))
                  : Array.isArray(sel) ? sel : [];
        const api = {
            length: arr.length, empty(){ arr.forEach(e => { e.innerHTML = ''; }); return api; },
            append(h){
                arr.forEach(e => {
                    if (typeof h === 'string') {
                        const t = window.document.createElement('template');
                        t.innerHTML = h;
                        e.appendChild(t.content.cloneNode(true));
                    }
                });
                return api;
            },
            val(v){
                if (v === undefined) {
                    if (!arr.length) return undefined;
                    return arr[0].multiple
                        ? Array.from(arr[0].selectedOptions).map(o => o.value)
                        : (arr[0].value || '');
                }
                arr.forEach(e => {
                    if (e.multiple) {
                        const vals = Array.isArray(v) ? v : [v];
                        Array.from(e.options).forEach(o => {
                            o.selected = vals.map(String).includes(String(o.value));
                        });
                    } else { e.value = v; }
                });
                return api;
            },
            trigger(name){ arr.forEach(e => e.dispatchEvent(new window.Event(name.split('.')[0], {bubbles:true}))); return api; },
            find(sel){ return $(arr.flatMap(e => Array.from(e.querySelectorAll(sel)))); },
            attr(k, v){ if (v === undefined) return arr[0] && arr[0].getAttribute(k); arr.forEach(e => e.setAttribute(k, v)); return api; },
            addClass(c){ arr.forEach(e => e.classList.add(...c.split(' '))); return api; },
            removeClass(c){ arr.forEach(e => e.classList.remove(...c.split(' '))); return api; },
            hasClass(c){ return arr.length>0 && arr[0].classList.contains(c); },
            forEach(fn){ arr.forEach(fn); return api; },
            get(i){ return arr[i]; },
        };
        return api;
    };
    $.fn = { select2() { return this; } };
    window.$ = $; window.jQuery = $;
    return window;
}

function installProbes(window) {
    // dashboard.js defines real showToast / setLoading / ajaxSave as
    // function declarations on the global object. After loading it we
    // need to re-stub the ones we care about so the probe can observe
    // the save path's behaviour.
    window.__CSRF_TOKEN__ = 'probe';
    window.setLoading = function (btn, on) { if (btn) btn.dataset.loading = on ? '1' : ''; };
    window.showToast = function (msg, kind) { (window.__toasts = window.__toasts || []).push({msg, kind}); };
    window.ajaxSave = async function (url, payload) {
        // The real ajaxSave is a thin fetch wrapper; capture the body.
        window.__lastAjax = { url, payload };
        return { success: true, warnings: [] };
    };
}

function loadAll(window) {
    // Eval the page script first so the helper functions are defined
    // when NeroSelect / NeroAlias initialise and (later) call them.
    window.eval(PAGE_SCRIPT);
    window.eval(ALIAS_JS);
    window.eval(SELECT_JS);
    // dashboard.js — in production this is loaded via <script src=>. We
    // include the same code the production page runs so the afterSwap /
    // htmx:load / htmx:historyRestore wiring is exercised exactly as it
    // would be in the browser.
    window.eval(DASHBOARD_JS);
    installProbes(window);
}

async function scenario1_directLoad() {
    console.log('\n=== 1. direct page load → chips render and save works ===');
    const w = makeWindow({ aliases: ['k', 'move', 'm'] });
    loadAll(w);
    w.document.dispatchEvent(new w.Event('DOMContentLoaded', { bubbles: true }));
    await flush();

    const host = w.document.getElementById('edit-aliases-host-kick');
    check('host is initialised by DOMContentLoaded', !!(host && host._neroAlias));
    check('host has the chip-box class',
        host && host.classList.contains('na-box'));
    check('host has a .na-input (text box for typing)', !!(host && host.querySelector('.na-input')));

    // Open the panel — the page calls loadCommandSettings, which fetches
    // /api/commands/settings/kick and runs setValues.
    w.eval('loadCommandSettings("kick")');
    await flush();
    const chips = () => Array.from(host.querySelectorAll('.na-chip')).map(c => c.querySelector('.na-chip-text').textContent);
    check('3 chips render from the saved aliases', chips().join(',') === 'k,move,m', chips());
    check('NeroAlias.getValues agrees',
        w.NeroAlias.getValues(host).join(',') === 'k,move,m');

    // Add a new alias.
    const text = host.querySelector('.na-input');
    type(w, text, 'xx');
    key(w, text, ' ');
    await flush();
    check('after Space: 4 chips (xx added)', chips().join(',') === 'k,move,m,xx', chips());

    // Remove the original "k" chip.
    const kChip = Array.from(host.querySelectorAll('.na-chip'))
        .find(c => c.querySelector('.na-chip-text').textContent === 'k');
    kChip.querySelector('.na-chip-x').click();
    await flush();
    check('after × on k: 3 chips remain', chips().join(',') === 'move,m,xx', chips());

    // Save. Wrap in try/catch so a downstream error in the post-save
    // UI hooks (filterCommands, refreshAliasStatus) does not prevent
    // us from reading __lastAjax.
    try {
        await w.eval('saveCommandSettings("kick", null)');
    } catch (e) {
        // ignore post-save side-effect errors
    }
    await flush();
    const last = w.__lastAjax || {};
    check('save POSTs to /api/commands/settings/kick',
        String(last.url || '').endsWith('/api/commands/settings/kick'));
    check('POST payload has the right aliases',
        last.payload && Array.isArray(last.payload.aliases)
        && last.payload.aliases.join(',') === 'move,m,xx', last.payload && last.payload.aliases);
    check('POST payload is not silently overwriting with []',
        last.payload && last.payload.aliases && last.payload.aliases.length > 0);
}

async function scenario2_htmxSwap() {
    console.log('\n=== 2. htmx navigation → host is initialised by afterSwap ===');
    // Start on a different page (Overview, say). The user clicks a
    // sidebar link which fires an htmx navigation that swaps in
    // /commands. With the fix, the afterSwap handler initialises the
    // new hosts. Without the fix, host._neroAlias is undefined and
    // loadCommandSettings() would render an empty box.
    const w = makeWindow({ aliases: ['k', 'move', 'm'] });
    loadAll(w);
    // Simulate the initial Overview page (no command hosts present).
    w.document.getElementById('content-area').innerHTML = '<p>Overview</p>';
    w.document.dispatchEvent(new w.Event('DOMContentLoaded', { bubbles: true }));
    await flush();

    // htmx swaps the Commands page into #content-area. Fire the
    // afterSwap event the page wires up.
    const row = extractCommandRow();
    w.document.getElementById('content-area').innerHTML = row;
    const evt = new w.CustomEvent('htmx:afterSwap', {
        bubbles: true, detail: { target: w.document.getElementById('content-area') },
    });
    w.document.dispatchEvent(evt);
    await flush();

    const host = w.document.getElementById('edit-aliases-host-kick');
    check('after htmx:afterSwap: host is initialised (the fix)',
        !!(host && host._neroAlias));
    check('after htmx:afterSwap: host has the chip-box class',
        host && host.classList.contains('na-box'));
    check('after htmx:afterSwap: a .na-input is present',
        !!(host && host.querySelector('.na-input')));

    // Open the Edit panel — must show saved chips.
    w.eval('loadCommandSettings("kick")');
    await flush();
    const chipWords = () => Array.from(host.querySelectorAll('.na-chip'))
        .map(c => c.querySelector('.na-chip-text').textContent);
    check('after loadCommandSettings on a swapped-in host: 3 chips',
        chipWords().join(',') === 'k,move,m', chipWords());
}

async function scenario3_rolePickerFailSafe() {
    console.log('\n=== 3. role picker failsafe: init failure does NOT silently save [] ===');
    // The previous investigation's repro path: select2 unavailable →
    // initMulti's catch block falls through to a plain <select>, which
    // is fine; OR a network failure prevents initMulti from being
    // awaited at all. We simulate the latter by replacing NeroSelect
    // with a stub whose initMulti does nothing for the roles picker.
    const w = makeWindow({ aliases: ['k'], roles: ['111', '222'] });
    loadAll(w);
    // Replace NeroSelect with a stub that simulates the failure mode:
    // initMulti is a no-op for the enabled-roles picker (so nsInit
    // never gets set), but works for the other three.
    w.eval(`
        window.NeroSelect = (function () {
            return {
                initAll(){},
                initMulti(el, kind, pre) {
                    if (!el) return;
                    if (el.dataset.nsInit === '1') return;
                    if (el.id && el.id.includes('enabled-roles')) {
                        // simulate the failure: leave nsInit unset
                        return;
                    }
                    el.dataset.nsInit = '1';
                    el.setAttribute('multiple','multiple');
                    const items = (kind === 'role')
                        ? [{id:'111',text:'Owner'},{id:'222',text:'Mod'}]
                        : [{id:'1',text:'general'}];
                    el.innerHTML = '';
                    items.forEach(it => {
                        const o = document.createElement('option');
                        o.value = it.id; o.textContent = it.text;
                        el.appendChild(o);
                    });
                },
                getMultiValues(el) {
                    if (!el || el.dataset.nsInit !== '1') return [];
                    return Array.from(el.selectedOptions).map(o => o.value);
                },
                isReady(el) { return !!(el && el.dataset && el.dataset.nsInit === '1'); },
            };
        })();
    `);
    w.document.dispatchEvent(new w.Event('DOMContentLoaded', { bubbles: true }));
    await flush();

    // Open the panel — loadCommandSettings will detect the failure and
    // mark nsReady='0' on the failed picker.
    w.eval('loadCommandSettings("kick")');
    await flush();
    const failedPicker = w.document.getElementById('edit-enabled-roles-kick');
    check('failed picker is NOT marked nsInit=1', failedPicker && failedPicker.dataset.nsInit !== '1');
    check('failed picker is NOT marked nsReady=1', failedPicker && failedPicker.dataset.nsReady !== '1');
    check('a sibling picker that did init IS nsInit=1',
        w.document.getElementById('edit-disabled-roles-kick').dataset.nsInit === '1');

    // Save — must refuse and toast.
    try {
        await w.eval('saveCommandSettings("kick", null)');
    } catch (e) { /* ignore */ }
    await flush();
    const last = w.__lastAjax;
    check('save did NOT post (toast shown instead)', !last);
    const toasts = w.__toasts || [];
    check('a toast was shown', toasts.length > 0);
    check('the toast mentions the failed picker',
        toasts.some(t => /enabled-roles/.test(t.msg || '')),
        toasts.map(t => t.msg));
    check('the toast is the error variant (kind=error)',
        toasts.some(t => t.kind === 'error'),
        toasts.map(t => t.kind));
}

async function scenario4_historyRestore() {
    console.log('\n=== 4. htmx back/forward → historyRestore hook also re-inits ===');
    const w = makeWindow({ aliases: ['k'] });
    loadAll(w);
    // Empty page, then a swap, then a historyRestore.
    w.document.getElementById('content-area').innerHTML = '<p>Dashboard</p>';
    w.document.dispatchEvent(new w.Event('DOMContentLoaded', { bubbles: true }));
    await flush();
    w.document.getElementById('content-area').innerHTML = extractCommandRow();
    w.document.dispatchEvent(new w.CustomEvent('htmx:historyRestore', { bubbles: true }));
    await flush();
    const host = w.document.getElementById('edit-aliases-host-kick');
    check('after historyRestore: host is initialised', !!(host && host._neroAlias));
    w.eval('loadCommandSettings("kick")');
    await flush();
    const chips = () => Array.from(host.querySelectorAll('.na-chip'))
        .map(c => c.querySelector('.na-chip-text').textContent);
    check('after historyRestore + loadCommandSettings: 1 chip visible',
        chips().length === 1 && chips()[0] === 'k', chips());
}

async function main() {
    await scenario1_directLoad();
    await scenario2_htmxSwap();
    await scenario3_rolePickerFailSafe();
    await scenario4_historyRestore();

    console.log(`\n${'='.repeat(60)}`);
    if (failures.length) {
        console.log(`FAILED:\n  - ${failures.join('\n  - ')}`);
        process.exit(1);
    }
    console.log(`${checks - failures.length}/${checks} checks passed`);
}

main().catch(e => { console.error(e); process.exit(2); });
