/**
 * Behavioural test for the MVP configuration-loss guard.
 *
 * Extracts the real script from dashboard/templates/systems/mvp.html, runs it
 * in a minimal DOM with a controllable fetch(), and asserts that a save is
 * refused whenever the configuration read did not succeed.
 *
 *   node tests/test_config_loss_guard.js      # exit 0 = all pass
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const TEMPLATE = path.join(__dirname, '..', 'dashboard', 'templates', 'systems', 'mvp.html');
const html = fs.readFileSync(TEMPLATE, 'utf8');

// The page's script is the last <script> block and is wrapped in an IIFE.
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
if (!scripts.length) { console.error('no <script> found'); process.exit(2); }
const source = scripts[scripts.length - 1];

let pass = 0, fail = 0;
function check(name, ok, detail) {
  if (ok) { pass++; console.log(`[PASS] ${name}${detail ? ' :: ' + detail : ''}`); }
  else { fail++; console.log(`[FAIL] ${name}${detail ? ' :: ' + detail : ''}`); }
}

function makeElement(id) {
  return {
    id, value: '', disabled: false, textContent: '', title: '',
    dataset: {}, style: {}, className: '',
    options: [], children: [], innerHTML: '',
    addEventListener() {}, appendChild(c) { this.children.push(c); },
    querySelectorAll() { return []; },
  };
}

const ELEMENT_IDS = [
  'mvp-cycle', 'mvp-chat-weight', 'mvp-voice-weight', 'mvp-ranks', 'mvp-min-words',
  'mvp-rank-rewards', 'mvp-reward-preview', 'mvp-min-hint', 'mvp-config-scope',
  'mvp-save-btn', 'mvp-save-status', 'mvp-role-picker', 'mvp-channel-picker',
  'tab-content-scores', 'tab-scores', 'tab-content-history', 'tab-history',
  'tab-content-config', 'tab-config',
];

function buildSandbox(fetchImpl) {
  const elements = {};
  ELEMENT_IDS.forEach(id => { elements[id] = makeElement(id); });

  const document = {
    getElementById: id => elements[id] || (elements[id] = makeElement(id)),
    // The page builds the per-rank reward inputs with renderRankRewards() and
    // then reads them back with querySelectorAll('.mvp-rank-reward'), so the
    // double has to model that one selector or the save path can never validate.
    querySelectorAll: (sel) => {
      if (sel !== '.mvp-rank-reward') return [];
      const box = elements['mvp-rank-rewards'];
      if (!box) return [];
      // renderRankRewards() builds: box -> row -> input.mvp-rank-reward
      const out = [];
      const visit = (node) => {
        if (!node) return;
        if (String(node.className || '').includes('mvp-rank-reward')) out.push(node);
        (node.children || []).forEach(visit);
      };
      box.children.forEach(visit);
      return out;
    },
    createElement: tag => makeElement(tag),
    addEventListener() {},
  };

  const savedCalls = [];
  const sandbox = {
    document,
    console,
    setTimeout: (fn) => { try { fn(); } catch (_) {} return 0; },
    Promise,
    Object, Array, Number, String, Math, JSON, Date, isNaN, parseInt, parseFloat,
    fetch: fetchImpl,
    window: {},
    globalThis: {},
    // jQuery / Select2 double (the page only uses .val()/.trigger()).
    $: (el) => ({ val: (v) => (v === undefined ? (el && el.value) : (el.value = v)),
                  trigger: () => {} }),
    ajaxSave: async (url, payload, btn, msg) => { savedCalls.push({ url, payload }); return { success: true }; },
    showToast: () => {},
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  sandbox.__savedCalls = savedCalls;
  return sandbox;
}

function loadPage(fetchImpl) {
  const sandbox = buildSandbox(fetchImpl);
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, { filename: 'mvp.html<script>' });
  return sandbox;
}

const flush = () => new Promise(r => setImmediate(r));

(async () => {
  // ── 1. failed read (HTTP 500) ────────────────────────────────────────────
  let sb = loadPage(async () => ({ ok: false, status: 500, json: async () => ({}) }));
  await flush(); await flush();
  check('save button disabled after a failed read', sb.document.getElementById('mvp-save-btn').disabled === true);
  check('failure is reported to the operator, not shown as defaults',
        /could not load/i.test(sb.document.getElementById('mvp-config-scope').textContent),
        JSON.stringify(sb.document.getElementById('mvp-config-scope').textContent.slice(0, 80)));
  await sb.saveMVPConfig(sb.document.getElementById('mvp-save-btn'));
  check('save is refused after a failed read (no write attempted)', sb.__savedCalls.length === 0,
        `writes=${sb.__savedCalls.length}`);

  // ── 2. network exception ─────────────────────────────────────────────────
  sb = loadPage(async () => { throw new Error('network down'); });
  await flush(); await flush();
  check('save button disabled after a network exception',
        sb.document.getElementById('mvp-save-btn').disabled === true);
  await sb.saveMVPConfig(sb.document.getElementById('mvp-save-btn'));
  check('save is refused after a network exception', sb.__savedCalls.length === 0,
        `writes=${sb.__savedCalls.length}`);

  // ── 3. successful read with saved values ─────────────────────────────────
  const savedCfg = {
    config: { cycle_hours: 12, chat_word_weight: 1.5, voice_minute_weight: 3,
              winner_ranks: 2, rank_rewards: [100, 50], min_activity_words: 25 },
  };
  sb = loadPage(async () => ({ ok: true, status: 200, json: async () => savedCfg }));
  await flush(); await flush();
  check('save button enabled after a successful read',
        sb.document.getElementById('mvp-save-btn').disabled === false);
  check('stored values populate the form (not defaults)',
        String(sb.document.getElementById('mvp-cycle').value) === '12',
        `cycle=${sb.document.getElementById('mvp-cycle').value}`);
  await sb.saveMVPConfig(sb.document.getElementById('mvp-save-btn'));
  check('save proceeds after a successful read', sb.__savedCalls.length === 1,
        `writes=${sb.__savedCalls.length} status=${JSON.stringify(sb.document.getElementById('mvp-save-status').textContent)} ranks=${JSON.stringify(sb.document.getElementById('mvp-ranks').value)} rewards=${sb.document.getElementById('mvp-rank-rewards').children.length}`);
  check('save carries the loaded cycle value, not a default',
        sb.__savedCalls.length === 1 && String(sb.__savedCalls[0].payload.cycle_hours) === '12',
        JSON.stringify(sb.__savedCalls[0] && sb.__savedCalls[0].payload));

  // ── 4. successful read, nothing saved yet (legitimately empty) ───────────
  sb = loadPage(async () => ({ ok: true, status: 200, json: async () => ({ config: {} }) }));
  await flush(); await flush();
  check('legitimately empty config still allows saving (defaults are genuine)',
        sb.document.getElementById('mvp-save-btn').disabled === false);
  const scopeText = sb.document.getElementById('mvp-config-scope').textContent;
  check('empty config is described as "no settings saved", not as an error',
        /no mvp settings are saved/i.test(scopeText), JSON.stringify(scopeText.slice(0, 70)));
  await sb.saveMVPConfig(sb.document.getElementById('mvp-save-btn'));
  check('save proceeds for a legitimately empty config', sb.__savedCalls.length === 1,
        `writes=${sb.__savedCalls.length}`);

  console.log(`\n===== ${pass}/${pass + fail} checks passed =====`);
  process.exit(fail === 0 ? 0 : 1);
})().catch(e => { console.error(e); process.exit(2); });
