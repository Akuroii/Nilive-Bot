/**
 * Page-script regressions for the Dashboard Prestige and MVP forms.
 *
 * Both pages used `parseFloat(x) || default` / `parseInt(x) || default`, which
 * silently turned a typed 0 (or a blank field) into 1.0 / 2.0 / 6 and saved it,
 * and the MVP page sent "no role/channel" when saved before its pickers loaded.
 *
 * Run: node scripts/test_dashboard_streak_prestige_mvp.js
 */
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..');
const read = rel => fs.readFileSync(path.join(ROOT, rel), 'utf8');
let passed = 0;
function test(name, fn) {
    return Promise.resolve().then(fn).then(
        () => { passed++; console.log('  PASS  ' + name); },
        err => { console.error('  FAIL  ' + name + '\n' + (err && err.stack || err)); process.exitCode = 1; });
}

// ── tiny DOM ────────────────────────────────────────────────────────────
function makeDom() {
    const byId = new Map();
    const mk = (id) => ({
        id, value: '', innerHTML: '', textContent: '', checked: false, className: '', type: '',
        style: {}, dataset: {}, options: [], children: [],
        classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
        addEventListener() {}, focus() { this.focused = true; },
        appendChild(c) { this.children.push(c); return c; },
        querySelectorAll(sel) {
            if (sel !== '.mvp-rank-reward') return [];
            return this.children.flatMap(r => r.children || []).filter(c => /mvp-rank-reward/.test(c.className));
        },
    });
    const document = {
        getElementById(id) {
            if (!byId.has(id)) {
                const el = mk(id);
                Object.defineProperty(el, 'innerHTML', {
                    get() { return this._html || ''; },
                    set(v) { this._html = v; if (v === '') this.children = []; },
                });
                byId.set(id, el);
            }
            return byId.get(id);
        },
        createElement() { return mk(null); },
        querySelectorAll(sel) {
            if (sel === '.mvp-rank-reward') return document.getElementById('mvp-rank-rewards').querySelectorAll(sel);
            if (sel === '.prestige-mult') return document._mults || [];
            return [];
        },
    };
    return { document, byId: { get: id => document.getElementById(id) } };
}

// ── MVP page ────────────────────────────────────────────────────────────
function loadMvpPage(config) {
    const html = read('dashboard/templates/systems/mvp.html');
    const script = /<script>([\s\S]*?)<\/script>/.exec(html)[1];
    const { document, byId } = makeDom();
    const sent = [], toasts = [], timers = [];
    const sandbox = {
        document, console, parseInt, parseFloat, Number, Array, Math, String, Object, Promise,
        setTimeout: (fn) => { timers.push(fn); return 0; },
        fetch: async () => ({ ok: true, json: async () => ({ config }) }),
        ajaxSave: async (url, payload) => { sent.push({ url, payload }); return { success: true }; },
        showToast: (m, t) => toasts.push({ m, t }),
        $: (el) => ({
            val(v) { if (v === undefined) return el._val || null; el._val = v; return this; },
            trigger() { return this; },
        }),
    };
    sandbox.window = sandbox;
    vm.runInNewContext(script, sandbox);
    return { sandbox, document, byId, sent, toasts, timers };
}
const settle = () => new Promise(r => setImmediate(r));

function fillValid(page, over = {}) {
    const v = Object.assign({ cycle: '6', chat: '1', voice: '2', ranks: '2', min: '40' }, over);
    page.byId.get('mvp-cycle').value = v.cycle;
    page.byId.get('mvp-chat-weight').value = v.chat;
    page.byId.get('mvp-voice-weight').value = v.voice;
    page.byId.get('mvp-ranks').value = v.ranks;
    page.byId.get('mvp-min-words').value = v.min;
    page.sandbox.renderRankRewards([100, 50]);
}

(async () => {
    console.log('\n== MVP page ==');

    await test('a saved weight of 0 is displayed as 0, not replaced by a default', async () => {
        const p = loadMvpPage({ cycle_hours: 8, chat_word_weight: 0, voice_minute_weight: 0, winner_ranks: 2,
                                rank_rewards: [5, 0], min_activity_words: 0 });
        await settle();
        assert.strictEqual(String(p.byId.get('mvp-chat-weight').value), '0');
        assert.strictEqual(String(p.byId.get('mvp-voice-weight').value), '0');
        assert.strictEqual(String(p.byId.get('mvp-cycle').value), '8');
    });

    await test('an unconfigured guild shows defaults and says nothing is saved yet', async () => {
        const p = loadMvpPage({});
        await settle();
        assert.strictEqual(String(p.byId.get('mvp-cycle').value), '6');
        assert.strictEqual(String(p.byId.get('mvp-chat-weight').value), '1');
        assert.match(p.byId.get('mvp-config-scope').textContent, /No MVP settings are saved for this server/);
    });

    await test('a typed 0 weight is sent as 0', async () => {
        const p = loadMvpPage({}); await settle();
        fillValid(p, { chat: '0', voice: '0' });
        await p.sandbox.saveMVPConfig({});
        assert.strictEqual(p.sent.length, 1);
        assert.strictEqual(p.sent[0].payload.chat_word_weight, 0);
        assert.strictEqual(p.sent[0].payload.voice_minute_weight, 0);
        assert.strictEqual(JSON.stringify(p.sent[0].payload.rank_rewards), '[100,50]');
    });

    for (const [label, over, re] of [
        ['blank cycle hours', { cycle: '' }, /Cycle hours/],
        ['cycle hours 500', { cycle: '500' }, /Cycle hours/],
        ['fractional ranks', { ranks: '2.5' }, /Winner ranks/],
        ['11 ranks', { ranks: '11' }, /Winner ranks/],
        ['blank chat weight', { chat: '' }, /Chat word weight/],
        ['negative minimum', { min: '-3' }, /Minimum activity/],
    ]) {
        await test('rejects ' + label + ' without calling the API', async () => {
            const p = loadMvpPage({}); await settle();
            fillValid(p, over);
            await p.sandbox.saveMVPConfig({});
            assert.strictEqual(p.sent.length, 0);
            assert.match(p.toasts[0].m, re);
        });
    }

    await test('a blank rank reward is an error, not a silent 0', async () => {
        const p = loadMvpPage({}); await settle();
        fillValid(p);
        p.byId.get('mvp-rank-rewards').children[1].children[1].value = '';
        await p.sandbox.saveMVPConfig({});
        assert.strictEqual(p.sent.length, 0);
        assert.match(p.toasts[0].m, /Rank 2 reward/);
    });

    await test('saving before the pickers populate keeps the saved role and channel', async () => {
        const p = loadMvpPage({ mvp_role_id: '777', announce_channel_id: '888', winner_ranks: 2,
                                rank_rewards: [1, 1], cycle_hours: 6, chat_word_weight: 1, voice_minute_weight: 2 });
        await settle();
        fillValid(p);                       // pickers have no options yet (timer not fired)
        await p.sandbox.saveMVPConfig({});
        assert.strictEqual(p.sent[0].payload.mvp_role_id, '777');
        assert.strictEqual(p.sent[0].payload.announce_channel_id, '888');
    });

    await test('clearing a populated picker still clears the role', async () => {
        const p = loadMvpPage({ mvp_role_id: '777', winner_ranks: 2, rank_rewards: [1, 1] });
        await settle();
        fillValid(p);
        p.byId.get('mvp-role-picker').options = [{ value: '777' }];   // populated, admin chose "none"
        await p.sandbox.saveMVPConfig({});
        assert.strictEqual(p.sent[0].payload.mvp_role_id, null);
    });

    await test('tie wording and live reward preview are shown', async () => {
        const html = read('dashboard/templates/systems/mvp.html');
        assert.match(html, /every tied member receives that rank's Coins reward/);
        const p = loadMvpPage({ winner_ranks: 3, rank_rewards: [100, 50, 25] }); await settle();
        const text = p.byId.get('mvp-reward-preview').textContent;
        assert.match(text, /Rank 1: 100 Coins/);
        assert.match(text, /Rank 3: 25 Coins/);
        assert.match(text, /tied member/);
    });

    await test('minimum-activity hint converts words to voice minutes with the form weights', async () => {
        const p = loadMvpPage({ chat_word_weight: 0.5, voice_minute_weight: 4, min_activity_words: 40, winner_ranks: 1, rank_rewards: [1] });
        await settle();
        assert.match(p.byId.get('mvp-min-hint').textContent, /40 words = 20\.0 points = about 5\.0 voice minutes/);
    });

    // ── Prestige page ───────────────────────────────────────────────────
    console.log('\n== Prestige page ==');
    const lv = read('dashboard/templates/systems/leveling.html');
    const sliceFrom = (a, b) => lv.slice(lv.indexOf(a), lv.indexOf(b));

    function prestigeSandbox(values, fetchImpl) {
        const { document, byId } = makeDom();
        document._mults = values.map(([t, cur, v]) => ({ dataset: { tier: String(t), cur }, value: v, focus() { this.focused = true; } }));
        const calls = [];
        const sb = {
            document, console, parseInt, Number, String, Promise, JSON,
            PRESTIGE_TIER_NAMES: { 1: 'I', 2: 'II', 3: 'III', 4: 'IV', 5: 'V', 6: 'VI' },
            currencyName: k => ({ coins: 'Coins', diamonds: 'Diamonds' }[k]),
            checkIconHtml: () => '✓', showToast() {}, loadPrestigeConfig() { calls.push('reload'); },
            setTimeout: () => 0,
            fetch: async (u, o) => { calls.push(JSON.parse(o.body)); return fetchImpl ? fetchImpl() : { json: async () => ({ success: true }) }; },
        };
        vm.runInNewContext(sliceFrom('async function savePrestigeConfig()', 'async function loadPrestigeRoles()') +
                           '\nthis.savePrestigeConfig = savePrestigeConfig;', sb);
        byId.get('prestige-enabled').checked = true;
        return { sb, calls, status: byId.get('prestige-config-status') };
    }
    const all = (over = {}) => [1, 2, 3, 4, 5, 6].flatMap(t => ['coins', 'diamonds'].map(c => [t, c, over[t + c] ?? '1']));

    await test('a typed 0 multiplier is rejected, not saved as 1.0', async () => {
        const p = prestigeSandbox(all({ '5coins': '0' }));
        await p.sb.savePrestigeConfig();
        assert.strictEqual(p.calls.filter(c => c !== 'reload').length, 0);
        assert.match(p.status.textContent, /Prestige V Coins multiplier must be greater than 0/);
    });

    await test('a blank multiplier is rejected', async () => {
        const p = prestigeSandbox(all({ '6diamonds': '' }));
        await p.sb.savePrestigeConfig();
        assert.strictEqual(p.calls.length, 0);
        assert.match(p.status.textContent, /Prestige VI Diamonds/);
    });

    await test('a valid decimal is sent exactly, with the enabled flag', async () => {
        const p = prestigeSandbox(all({ '5coins': '1.25', '6diamonds': '1.2' }));
        await p.sb.savePrestigeConfig();
        const body = p.calls[0];
        assert.strictEqual(body.tiers[5].coins, 1.25);
        assert.strictEqual(body.tiers[6].diamonds, 1.2);
        assert.strictEqual(body.enabled, true);
        assert.strictEqual(Object.keys(body.tiers).length, 6);
        assert.ok(p.calls.includes('reload'), 'form reloads persisted values after saving');
    });

    await test('a server rejection is shown and nothing claims success', async () => {
        const p = prestigeSandbox(all(), () => ({ json: async () => ({ success: false, error: 'Tier 5 coins multiplier must be greater than 0 and at most 100.' }) }));
        await p.sb.savePrestigeConfig();
        assert.match(p.status.textContent, /Tier 5 coins/);
        assert.ok(!p.calls.includes('reload'));
    });

    await test('a network/parse failure is reported instead of throwing', async () => {
        const p = prestigeSandbox(all(), () => { throw new Error('boom'); });
        await p.sb.savePrestigeConfig();
        assert.match(p.status.textContent, /Nothing was changed/);
    });

    await test('V and VI values render with the Booster label and constraints', async () => {
        const { document, byId } = makeDom();
        const sb = { document, window: { __CURRENCY__: {} }, console };
        vm.runInNewContext(sliceFrom('const PRESTIGE_TIER_NAMES', 'async function loadPrestigeConfig()') +
                           '\nthis.render = renderPrestigeMultipliers;', sb);
        sb.render({ 5: { coins: 1.2, diamonds: 1.1 }, 6: { coins: 1.3, diamonds: 1.2 } });
        const out = byId.get('prestige-multipliers').innerHTML;
        assert.match(out, /★ V<\/div>/);
        assert.match(out, /★ VI \(Booster\)/);
        for (const v of ['1.2', '1.1', '1.3']) assert.ok(out.includes(`value="${v}"`), v);
        assert.match(out, /min="0\.01" max="100"/);
    });

    console.log(`\n${passed} JS checks passed${process.exitCode ? ' (with failures)' : ''}`);
})();
