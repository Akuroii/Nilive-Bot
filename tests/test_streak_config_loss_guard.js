/**
 * Behavioural test for the Daily & Streak configuration-loss guard.
 *
 * Extracts the real script from dashboard/templates/systems/economy.html, runs
 * it in a minimal DOM with a controllable fetch(), and asserts that a save is
 * refused whenever the settings read did not succeed — so a failed read can
 * never overwrite the stored streak configuration with values that were never
 * loaded.
 *
 *   node tests/test_streak_config_loss_guard.js      # exit 0 = all pass
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const TEMPLATE = path.join(__dirname, '..', 'dashboard', 'templates', 'systems', 'economy.html');
const html = fs.readFileSync(TEMPLATE, 'utf8');

const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
if (!scripts.length) { console.error('no <script> found in economy.html'); process.exit(2); }
// The streak/daily block is the one that declares loadStreakSettings.
let source = scripts.filter(s => s.includes('loadStreakSettings')).pop();
if (!source) { console.error('loadStreakSettings() not found in any inline script'); process.exit(2); }

// The block also carries four Jinja `tojson` defaults that Flask renders before
// the browser ever sees the page. Render them with representative values so the
// extracted script is valid JavaScript, exactly as served.
source = source.replace(/\{\{\s*currency_defaults\.(\w+)\|tojson\s*\}\}/g, (m, key) => {
  const defaults = { coin_name: 'Coins', coin_emoji: '\u{1FA99}',
                     diamond_name: 'Diamonds', diamond_emoji: '\u{1F48E}' };
  return JSON.stringify(defaults[key] === undefined ? '' : defaults[key]);
});
if (/\{\{|\{%/.test(source)) {
  console.error('unrendered Jinja markup remains in the extracted script');
  process.exit(2);
}

let pass = 0, fail = 0;
function check(name, ok, detail) {
  if (ok) { pass++; console.log(`[PASS] ${name}${detail ? ' :: ' + detail : ''}`); }
  else { fail++; console.log(`[FAIL] ${name}${detail ? ' :: ' + detail : ''}`); }
}

const REWARD_IDS = ['streak-stage-1', 'streak-stage-2', 'streak-stage-3', 'streak-stage-4',
                    'streak-stage-5', 'streak-stage6-min', 'streak-stage6-max'];

const ELEMENT_IDS = [
  ...REWARD_IDS,
  'streak-settings-form', 'streak-settings-save', 'streak-settings-status',
  'streak-receipt-mode', 'streak-receipt-channel', 'streak-config-scope',
  'streak-preview-rows', 'streak-legacy-note', 'streak-channel-wrap',
];

function makeElement(id) {
  return {
    id, value: '', disabled: false, textContent: '', title: '',
    dataset: {}, style: {}, className: '', options: [], children: [], innerHTML: '',
    addEventListener() {}, appendChild(c) { this.children.push(c); },
    querySelectorAll() { return []; },
    reportValidity() { return true; },
    setAttribute(name, value) { this[name] = value; },
    getAttribute(name) { return this[name]; },
    focus() {}, blur() {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
  };
}

function buildSandbox(fetchImpl) {
  const elements = {};
  ELEMENT_IDS.forEach(id => { elements[id] = makeElement(id); });
  // A populated channel picker, as NeroSelect builds it once it resolves the
  // guild's text/announcement channels.
  elements['streak-receipt-channel'].options = [
    { value: '' }, { value: '980000000000000001' }, { value: '980000000000000002' },
  ];

  const document = {
    getElementById: id => elements[id] || (elements[id] = makeElement(id)),
    // The page's tab wiring iterates .tab-content / .tab-btn at load time.
    querySelectorAll: () => [],
    createElement: tag => makeElement(tag),
    addEventListener() {},
    body: makeElement('body'),
    documentElement: makeElement('html'),
  };

  const saved = [];
  const sandbox = {
    document, console, Promise,
    Object, Array, Number, String, Math, JSON, Date, isNaN, parseInt, parseFloat,
    setTimeout: () => 0,          // the select2 wait loops are irrelevant here
    clearTimeout: () => {},
    fetch: fetchImpl,
    // jQuery / Select2 double
    $: (el) => ({ val: v => (v === undefined ? (el && el.value) : (el.value = v)),
                  trigger: () => {} }),
    ajaxSave: async (url, payload, btn, msg) => { saved.push({ url, payload }); return { success: true }; },
    showToast: () => {},
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.__saved = saved;
  return sandbox;
}

function loadPage(fetchImpl) {
  const sandbox = buildSandbox(fetchImpl);
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, { filename: 'economy.html<script>' });
  return sandbox;
}

const flush = () => new Promise(r => setImmediate(r));
const btn = sb => sb.document.getElementById('streak-settings-save');
const status = sb => sb.document.getElementById('streak-settings-status');

function savedSettings(over = {}) {
  return {
    success: true,
    settings: Object.assign({
      reward_table: { stages: [5, 10, 15, 20, 25], stage6_min: 30, stage6_max: 40 },
      receipt_visibility: 'public',
      receipt_channel_id: '980000000000000001',
      receipt_mode_source: 'guild',
    }, over),
  };
}

(async () => {
  // ── 1. Save is disabled before any read completes ────────────────────────
  let sb = loadPage(() => new Promise(() => {}));   // read never resolves
  await flush();
  check('save disabled before the read completes', btn(sb).disabled === true);

  // ── 2. HTTP failure ─────────────────────────────────────────────────────
  sb = loadPage(async () => ({ ok: false, status: 500, json: async () => ({ error: 'boom' }) }));
  await flush(); await flush();
  check('save disabled after an HTTP 500', btn(sb).disabled === true);
  check('failure states the reason', /boom|could not load/i.test(status(sb).textContent),
        JSON.stringify(status(sb).textContent.slice(0, 70)));
  check('failure says saving is disabled so values are not overwritten',
        /saving is disabled/i.test(status(sb).textContent));
  await sb.saveStreakSettings(btn(sb));
  check('save refused after an HTTP failure (zero writes)', sb.__saved.length === 0,
        `writes=${sb.__saved.length}`);
  check('no stored values were replaced by defaults',
        REWARD_IDS.every(id => sb.document.getElementById(id).value === ''),
        JSON.stringify(REWARD_IDS.map(id => sb.document.getElementById(id).value)));

  // ── 3. Network exception ────────────────────────────────────────────────
  sb = loadPage(async () => { throw new Error('network down'); });
  await flush(); await flush();
  check('save disabled after a network exception', btn(sb).disabled === true);
  await sb.saveStreakSettings(btn(sb));
  check('save refused after a network exception (zero writes)', sb.__saved.length === 0,
        `writes=${sb.__saved.length}`);

  // ── 4. Unreadable body / missing settings object ────────────────────────
  sb = loadPage(async () => ({ ok: true, status: 200,
                               json: async () => { throw new Error('not json'); } }));
  await flush(); await flush();
  check('save disabled when the response body is unreadable', btn(sb).disabled === true);
  await sb.saveStreakSettings(btn(sb));
  check('save refused for an unreadable body', sb.__saved.length === 0);

  sb = loadPage(async () => ({ ok: true, status: 200, json: async () => ({ success: true }) }));
  await flush(); await flush();
  check('save disabled when a 200 carries no settings object', btn(sb).disabled === true);
  await sb.saveStreakSettings(btn(sb));
  check('save refused when settings are absent', sb.__saved.length === 0);

  // ── 5. Successful load with a stored configuration ──────────────────────
  sb = loadPage(async () => ({ ok: true, status: 200,
                               json: async () => savedSettings() }));
  await flush(); await flush();
  check('save enabled after a successful read', btn(sb).disabled === false);
  check('stored reward values populate the form (not defaults)',
        Number(sb.document.getElementById('streak-stage-1').value) === 5
        && Number(sb.document.getElementById('streak-stage6-max').value) === 40,
        `stage1=${sb.document.getElementById('streak-stage-1').value} max=${sb.document.getElementById('streak-stage6-max').value}`);
  check('stored receipt mode is restored',
        sb.document.getElementById('streak-receipt-mode').value === 'public',
        sb.document.getElementById('streak-receipt-mode').value);
  await sb.saveStreakSettings(btn(sb));
  check('save proceeds after a successful read', sb.__saved.length === 1,
        `writes=${sb.__saved.length}`);
  check('save submits the loaded values',
        sb.__saved.length === 1
        && String(sb.__saved[0].payload.stages[0]) === '5'
        && String(sb.__saved[0].payload.receipt_channel_id) === '980000000000000001',
        JSON.stringify(sb.__saved[0] && sb.__saved[0].payload));

  // ── 6. Legitimately empty configuration stays savable ───────────────────
  sb = loadPage(async () => ({ ok: true, status: 200, json: async () => savedSettings({
    reward_table: null, receipt_visibility: 'private', receipt_channel_id: null,
  }) }));
  await flush(); await flush();
  check('legitimately empty config still allows saving',
        btn(sb).disabled === false);
  check('empty config is described as "no reward table saved", not an error',
        /no reward table is saved/i.test(sb.document.getElementById('streak-config-scope').textContent),
        JSON.stringify(sb.document.getElementById('streak-config-scope').textContent.slice(0, 70)));
  await sb.saveStreakSettings(btn(sb));
  check('save proceeds for a legitimately empty config', sb.__saved.length === 1,
        `writes=${sb.__saved.length}`);
  check('a blank reward table is submitted as blanks, not fabricated numbers',
        sb.__saved.length === 1 && sb.__saved[0].payload.stages.every(v => v === ''),
        JSON.stringify(sb.__saved[0] && sb.__saved[0].payload.stages));

  // ── 7. Public/Both without a channel is still refused client-side ───────
  sb = loadPage(async () => ({ ok: true, status: 200, json: async () => savedSettings({
    receipt_visibility: 'public', receipt_channel_id: null,
  }) }));
  await flush(); await flush();
  sb.document.getElementById('streak-receipt-channel').value = '';
  await sb.saveStreakSettings(btn(sb));
  check('Public with no channel is refused before the request',
        sb.__saved.length === 0, `writes=${sb.__saved.length}`);

  // ── 8. The channel hint must not call the channel optional for Public ───
  const hint = (html.match(/<div class="form-hint">([^<]*Required for Public[^<]*)<\/div>/) || [])[1] || '';
  check('channel hint states it is required for Public/Both', hint.length > 0,
        hint.slice(0, 90));
  check('channel hint no longer claims the channel is optional for Public/Both',
        !/Optional for Public or Both/i.test(html));

  console.log(`\n===== ${pass}/${pass + fail} checks passed =====`);
  process.exit(fail === 0 ? 0 : 1);
})().catch(e => { console.error(e); process.exit(2); });
