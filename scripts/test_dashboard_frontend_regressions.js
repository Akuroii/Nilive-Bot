/**
 * Frontend regression suite for Dashboard stabilization:
 * 1. Embed Builder (manage/embedbuilder.html): String Select Menu option rendering
 *    must not throw TDZ ReferenceError on `roleOptionsHtml`, and must escape role names.
 * 2. Support Tickets (manage/tickets.html): `loadCategories` + `editCategory` must escape
 *    untrusted strings, handle quotes safely, and never throw `ReferenceError: escapeHtml`.
 * 3. Stored/DOM XSS escaping across client-rendered templates (tagmissions, tagpartners,
 *    general status messages, ledger, trade summarizeOffer, reactionroles, customcommands).
 * 4. Minigame Builder (systems/minigame_builder.html): 19-digit channel_id snowflake
 *    must not be corrupted by `parseInt(...)`.
 * 5. NeroSelect (nero-select.js): failed fetch must not poison `_optionsCache`, and
 *    native `<select>` fallback must work cleanly when jQuery or Select2 is absent.
 */
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.join(__dirname, '..');

function readFile(rel) {
    return fs.readFileSync(path.join(ROOT, rel), 'utf8');
}

function extractInlineScripts(html) {
    const scripts = [];
    const re = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi;
    let m;
    while ((m = re.exec(html)) !== null) {
        let code = m[1];
        // Strip Jinja expressions inside templates so raw scripts parse in Node
        code = code.replace(/\{\{[\s\S]*?\}\}/g, 'null');
        code = code.replace(/\{%[\s\S]*?%\}/g, '');
        scripts.push(code);
    }
    return scripts;
}

function escapeHtmlHelper(str) {
    return String(str == null ? '' : str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

// ── 1. Embed Builder TDZ & Role Name XSS Check ─────────────────────────────
async function testEmbedBuilderSelectMenuRender() {
    const html = readFile('dashboard/templates/manage/embedbuilder.html');
    const script = extractInlineScripts(html)[0];
    assert(script, 'embedbuilder.html inline script must exist');

    const elements = new Map();
    function getEl(id) {
        if (!elements.has(id)) {
            const listeners = {};
            elements.set(id, {
                id,
                value: '',
                checked: false,
                innerHTML: '',
                textContent: '',
                style: {},
                dataset: {},
                classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
                addEventListener(ev, fn) { (listeners[ev] = listeners[ev] || []).push(fn); },
                click() { (listeners.click || []).forEach(fn => fn({ preventDefault() {}, target: this })); },
                querySelectorAll() { return []; },
                querySelector() { return null; },
                focus() {},
                scrollIntoView() {},
            });
        }
        return elements.get(id);
    }

    const sandbox = {
        console,
        AbortController,
        addEventListener: () => {},
        removeEventListener: () => {},
        setTimeout: () => 1,
        clearTimeout: () => {},
        localStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
        showToast: () => {},
        fetch: async (url) => {
            if (url === '/api/guild/roles') {
                return {
                    ok: true,
                    json: async () => ({
                        results: [
                            { id: '1088248319982006321', text: '<img src=x onerror=alert(1)>', name: '<img src=x onerror=alert(1)>', assignable: true },
                        ],
                    }),
                };
            }
            return { ok: true, json: async () => ({ results: [], emojis: [], templates: [] }) };
        },
        EmbedComposer: null,
        document: {
            body: { addEventListener: () => {} },
            createElement: () => ({ style: {}, classList: { add() {}, remove() {} }, appendChild() {}, setAttribute() {}, addEventListener() {} }),
            getElementById: getEl,
            querySelector: () => null,
            querySelectorAll: () => [],
            addEventListener: () => {},
        },
        window: null,
    };
    sandbox.window = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(readFile('dashboard/static/js/embed-composer.js'), sandbox, { filename: 'embed-composer.js' });
    vm.runInContext(script, sandbox, { filename: 'embedbuilder.inline.js' });
    await new Promise(r => setImmediate(r));
    await new Promise(r => setImmediate(r));

    getEl('eb-json-input').value = JSON.stringify({
        content: '',
        embeds: [],
        buttons: [],
        select_menus: [{
            placeholder: 'Pick a role',
            options: [{
                label: 'Danger Role',
                role_id: '1088248319982006321',
                required_role_id: '1088248319982006321',
                description: 'Test',
                emoji: '',
            }],
        }],
    });
    getEl('eb-json-apply-btn').click();

    const rendered = getEl('eb-selects-list').innerHTML;
    assert(rendered.includes('Danger Role'), 'EmbedBuilder select menu option must render without TDZ crash');
    assert(!rendered.includes('<img src=x onerror=alert(1)>'), 'EmbedBuilder must escape roleMap[required_role_id].name');
}

// ── 2. Support Tickets XSS + ReferenceError: escapeHtml Check ──────────────
async function testTicketsCategoryRenderAndEdit() {
    const html = readFile('dashboard/templates/manage/tickets.html');
    const script = extractInlineScripts(html)[0];

    const elements = new Map();
    function getEl(id) {
        if (!elements.has(id)) {
            elements.set(id, {
                id,
                value: '',
                checked: false,
                innerHTML: '',
                textContent: '',
                style: {},
                dataset: {},
                scrollIntoView() {},
                addEventListener() {},
            });
        }
        return elements.get(id);
    }

    const maliciousCat = {
        id: 7,
        emoji: '<svg onload=alert(1)>',
        name: `VIP's "Admin" <img src=x onerror=alert(2)>`,
        description: 'Desc',
        required_role_id: '1088248319982006321',
        closer_roles: ['<script>alert(3)</script>'],
        welcome_message: 'Hello',
        button_color: 'blurple',
        enabled: 1,
    };

    const sandbox = {
        console,
        _escapeHtml: escapeHtmlHelper,
        setTimeout: () => 1,
        clearTimeout: () => {},
        scrollTo: () => {},
        showToast: () => {},
        showConfirm: () => {},
        $: () => ({ val: () => ({ trigger: () => {} }) }),
        fetch: async () => ({ ok: true, json: async () => ({ categories: [maliciousCat], tickets: [], transcripts: [] }) }),
        document: {
            getElementById: getEl,
            querySelector: () => ({ click() {}, classList: { add() {}, remove() {} } }),
            querySelectorAll: () => [],
            addEventListener: () => {},
        },
        window: null,
    };
    sandbox.window = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(script, sandbox, { filename: 'tickets.inline.js' });

    await sandbox.loadCategories();
    const tbodyHtml = getEl('cat-tbody').innerHTML;
    assert(!tbodyHtml.includes('<svg onload=alert(1)>'), 'tickets.html must escape category emoji');
    assert(!tbodyHtml.includes('<img src=x onerror=alert(2)>'), 'tickets.html must escape category name');
    assert(!tbodyHtml.includes('<script>alert(3)</script>'), 'tickets.html must escape closer_roles');
    assert(!tbodyHtml.includes('JSON.stringify'), 'tickets.html must not inline-serialize raw category JSON');

    // editCategoryById must NOT throw ReferenceError: escapeHtml is not defined
    sandbox.editCategoryById(7);
    const titleHtml = getEl('cat-form-title').innerHTML;
    assert(titleHtml.includes('Editing'), 'editCategory must update cat-form-title without ReferenceError');
    assert(!titleHtml.includes('<img src=x onerror=alert(2)>'), 'editCategory must escape category name');
}

// ── 3. Behavioral Stored/DOM XSS Execution Checks + Minigame Snowflake ─────
async function testTemplateEscapingAndMinigameSnowflake() {
    function makeElMap() {
        const els = new Map();
        return (id) => {
            if (!els.has(id)) {
                els.set(id, {
                    id,
                    value: '',
                    checked: false,
                    innerHTML: '',
                    textContent: '',
                    style: {},
                    dataset: {},
                    children: [],
                    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
                    addEventListener() {},
                    querySelector: () => ({ checked: false, value: '' }),
                    querySelectorAll: () => [],
                });
            }
            return els.get(id);
        };
    }

    // 3a. trade.html summarizeOffer (behavioral execution)
    {
        const tradeScript = extractInlineScripts(readFile('dashboard/templates/systems/trade.html'))[0];
        const getEl = makeElMap();
        const box = {
            URLSearchParams,
            console,
            _escapeHtml: escapeHtmlHelper,
            currencyAmount: (k, v) => `${v} ${k}`,
            fetch: async () => ({ ok: true, json: async () => ({ trades: [] }) }),
            document: { getElementById: getEl },
            window: null,
        };
        box.window = box;
        vm.createContext(box);
        vm.runInContext(tradeScript, box);
        const offerHtml = box.summarizeOffer({ items: { '<img src=x onerror=alert(1)>': '<svg onload=alert(2)>' } });
        assert(!offerHtml.includes('<img'), 'trade.html summarizeOffer must escape item names');
        assert(!offerHtml.includes('<svg'), 'trade.html summarizeOffer must escape item quantities');
        assert(offerHtml.includes('&lt;img'), 'trade.html summarizeOffer must emit escaped HTML entities');
    }

    // 3b. tagmissions.html tmLoad (behavioral execution)
    {
        const tmScript = extractInlineScripts(readFile('dashboard/templates/systems/tagmissions.html'))[0];
        const getEl = makeElMap();
        const box = {
            console,
            currencyNameFor: (t) => t === 'coins' ? 'Coins <script>alert(9)</script>' : t,
            NeroSelect: { initAll() {} },
            $: () => ({ val: () => '' }),
            showToast: () => {},
            showConfirm: () => {},
            fetch: async () => ({
                ok: true,
                json: async () => ([{
                    id: 11,
                    title: '<img src=x onerror=alert("tm")>',
                    reward_type: 'coins',
                    reward_amount: '100"><svg onload=alert(1)>',
                    status: '<svg onload=alert("status")>',
                    starts_at: '<script>1</script>',
                    ends_at: '"><img src=y>',
                    participant_count: 3,
                }]),
            }),
            document: { getElementById: getEl, addEventListener() {} },
            window: null,
        };
        box.window = box;
        vm.createContext(box);
        vm.runInContext(tmScript, box);
        await box.tmLoad();
        const html = getEl('tm-list-body').innerHTML;
        assert(!html.includes('<img'), 'tagmissions.html tmLoad must escape title and timestamps');
        assert(!html.includes('<svg'), 'tagmissions.html tmLoad must escape status and reward_amount');
        assert(!html.includes('<script>'), 'tagmissions.html tmLoad must escape currency label & timestamps');
        assert(html.includes('&lt;img src=x onerror=alert(&quot;tm&quot;)&gt;'), 'tagmissions.html must render escaped title');
    }

    // 3c. tagpartners.html tpLoad (behavioral execution)
    {
        const tpScript = extractInlineScripts(readFile('dashboard/templates/systems/tagpartners.html'))[0];
        const getEl = makeElMap();
        const box = {
            console,
            currencyNameFor: () => 'Coins',
            checkIconHtml: () => '<span>OK</span>',
            NeroSelect: { initAll() {} },
            $: () => ({ val: () => '' }),
            showToast: () => {},
            showConfirm: () => {},
            fetch: async () => ({
                ok: true,
                json: async () => ([{
                    id: 5,
                    partner_label: '<svg onload=alert("tp")>',
                    partner_guild_id: '123"><img src=x onerror=alert(1)>',
                    reward_type: 'coins',
                    reward_amount: 500,
                    enabled: 1,
                }]),
            }),
            document: { getElementById: getEl, addEventListener() {} },
            window: null,
        };
        box.window = box;
        vm.createContext(box);
        vm.runInContext(tpScript, box);
        await box.tpLoad();
        const html = getEl('tp-list-body').innerHTML;
        assert(!html.includes('<svg'), 'tagpartners.html tpLoad must escape partner_label');
        assert(!html.includes('<img'), 'tagpartners.html tpLoad must escape partner_guild_id');
        assert(html.includes('&lt;svg onload=alert(&quot;tp&quot;)&gt;'), 'tagpartners.html must render escaped label');
    }

    // 3d. config/general.html loadStatusMessages (behavioral execution)
    {
        const genScript = extractInlineScripts(readFile('dashboard/templates/config/general.html'))[0];
        const getEl = makeElMap();
        const box = {
            console,
            $: () => ({ val: () => '' }),
            showToast: () => {},
            fetch: async () => ({
                ok: true,
                json: async () => ([{
                    id: 9,
                    type: '<svg onload=alert(1)>',
                    text: 'Playing <img src=x onerror=alert("status")>',
                }]),
            }),
            document: { getElementById: getEl, addEventListener() {} },
            window: null,
        };
        box.window = box;
        vm.createContext(box);
        vm.runInContext(genScript, box);
        await box.loadStatusMessages();
        const html = getEl('status-list').innerHTML;
        assert(!html.includes('<svg'), 'config/general.html must escape status type');
        assert(!html.includes('<img'), 'config/general.html must escape status text');
        assert(html.includes('&lt;img src=x onerror=alert(&quot;status&quot;)&gt;'), 'config/general.html must render escaped status text');
    }

    // 3e. systems/ledger.html loadLedger (behavioral execution, stripping Jinja blocks)
    {
        const rawLedger = extractInlineScripts(readFile('dashboard/templates/systems/ledger.html'))[0]
            .replace(/\{%[\s\S]*?%\}/g, '');
        const getEl = makeElMap();
        const box = {
            URLSearchParams,
            console,
            userIdentityHtml: (uid) => `<span>${escapeHtmlHelper(uid)}</span>`,
            currencyNameFor: () => 'Coins',
            showConfirm: () => {},
            showToast: () => {},
            fetch: async () => ({
                ok: true,
                json: async () => ({
                    user_map: {},
                    entries: [{
                        id: 42,
                        user_id: '1088248319982006339',
                        currency: 'balance',
                        amount: 100,
                        balance_after: 500,
                        type: '<script>alert("t")</script>',
                        source: '<img src=x onerror=alert("s")>',
                        reason: '<svg onload=alert("r")>',
                        created_at: '2026-11-12T00:00:00',
                        reversed: 0,
                    }],
                }),
            }),
            document: { getElementById: getEl, addEventListener() {} },
            window: null,
        };
        box.window = box;
        vm.createContext(box);
        vm.runInContext(rawLedger, box);
        await box.loadLedger();
        const html = getEl('ledger-tbody').innerHTML;
        assert(!html.includes('<script>'), 'ledger.html must escape entry type');
        assert(!html.includes('<img'), 'ledger.html must escape entry source');
        assert(!html.includes('<svg'), 'ledger.html must escape entry reason');
        assert(html.includes('&lt;svg onload=alert(&quot;r&quot;)&gt;'), 'ledger.html must render escaped reason');
    }

    // 3f. manage/reactionroles.html addButton/renderButtons & loadPanels (behavioral execution)
    {
        const rrScript = extractInlineScripts(readFile('dashboard/templates/manage/reactionroles.html'))[0];
        const getEl = makeElMap();
        const box = {
            console,
            showToast: () => {},
            navigator: { clipboard: { writeText() {} } },
            fetch: async () => ({
                ok: true,
                json: async () => ({ panels: [{ title: '<img src=x onerror=alert("panel")>', buttons: 2 }] }),
            }),
            document: { getElementById: getEl, addEventListener() {} },
            window: null,
        };
        box.window = box;
        vm.createContext(box);
        vm.runInContext(rrScript, box);
        await box.loadPanels();
        assert(!getEl('panels-list').innerHTML.includes('<img'), 'reactionroles.html loadPanels must escape panel title');
        assert(getEl('panels-list').innerHTML.includes('&lt;img'), 'reactionroles.html loadPanels must render escaped title');

        getEl('btn-label').value = '<svg onload=alert("lbl")>';
        getEl('btn-role').value = '123"><img src=x onerror=alert("role")>';
        getEl('btn-emoji').value = '<script>e</script>';
        getEl('btn-color').value = 'blurple';
        getEl('btn-required').value = '<img src=y onerror=alert("req")>';
        box.addButton();
        const btnListHtml = getEl('buttons-list').innerHTML;
        const prevBtnHtml = getEl('prev-buttons').innerHTML;
        assert(!btnListHtml.includes('<svg') && !btnListHtml.includes('<img') && !btnListHtml.includes('<script>'),
            'reactionroles.html renderButtons must escape label, emoji, role, and required role');
        assert(!prevBtnHtml.includes('<svg') && !prevBtnHtml.includes('<script>'),
            'reactionroles.html preview buttons must escape label and emoji');
    }

    // 3g. manage/customcommands.html addAction/renderActions (behavioral execution)
    {
        const ccScript = extractInlineScripts(readFile('dashboard/templates/manage/customcommands.html'))[0];
        const getEl = makeElMap();
        const box = {
            console,
            showToast: () => {},
            fetch: async () => ({ ok: true, json: async () => ({}) }),
            document: { getElementById: getEl, addEventListener() {} },
            window: null,
        };
        box.window = box;
        vm.createContext(box);
        vm.runInContext(ccScript, box);
        getEl('action-select').value = 'add_role:custom';
        getEl('custom-action-value').value = '<img src=x onerror=alert("cc")>';
        box.addAction();
        const actionsHtml = getEl('actions-list').innerHTML;
        assert(!actionsHtml.includes('<img'), 'customcommands.html renderActions must escape custom action value');
        assert(actionsHtml.includes('&lt;img src=x onerror=alert(&quot;cc&quot;)&gt;'), 'customcommands.html must render escaped action');
    }

    // 3h. manage/commands.html loadCommandSettings aliasHint (behavioral execution)
    {
        const cmdScripts = extractInlineScripts(readFile('dashboard/templates/manage/commands.html'));
        const mainCmdScript = cmdScripts.find(s => s.includes('async function loadCommandSettings'))
            .replace(/\{%[\s\S]*?%\}/g, '')
            .replace(/\{\{[\s\S]*?\}\}/g, '""');
        const getEl = makeElMap();
        getEl('usage-ping').dataset.params = 'target"><img src=x onerror=alert(2)>';
        const box = {
            console,
            setTimeout: () => 1,
            clearTimeout: () => {},
            NeroSelect: { initMulti: async () => {} },
            showToast: () => {},
            fetch: async () => ({
                ok: true,
                json: async () => ({
                    aliases: JSON.stringify(['<svg onload=alert("alias")>']),
                }),
            }),
            document: {
                getElementById: getEl,
                querySelector: () => null,
                querySelectorAll: () => [],
                addEventListener() {},
            },
            window: null,
        };
        box.window = box;
        vm.createContext(box);
        vm.runInContext(mainCmdScript, box);
        await box.loadCommandSettings('ping');
        const hintHtml = getEl('usage-alias-ping').innerHTML;
        assert(!hintHtml.includes('<svg'), 'commands.html must escape firstAlias in usage hint');
        assert(!hintHtml.includes('<img'), 'commands.html must escape params in usage hint');
        assert(hintHtml.includes('&lt;svg onload=alert(&quot;alias&quot;)&gt;'), 'commands.html must render escaped alias');
    }

    // 3i. minigame_builder.html onSpawnInput + buildPayload preserves 64-bit snowflake channel_id as exact string
    {
        const mbScripts = extractInlineScripts(readFile('dashboard/templates/systems/minigame_builder.html'));
        const mbScript = mbScripts.find(s => s.includes('function buildPayload()'))
            .replace(/\{\{\s*template_id\s*\|\s*tojson\s*\}\}/g, 'null');
        const getEl = makeElMap();
        getEl('mb-name').value = 'Snowflake Trivia';
        getEl('mb-category').value = '3';
        getEl('mb-enabled').checked = true;
        getEl('mb-autospawn').checked = true;
        const box = {
            AbortController,
            console,
            setTimeout: () => 1,
            clearTimeout: () => {},
            addEventListener: () => {},
            removeEventListener: () => {},
            currencyLabelText: (k) => k,
            NeroSelect: { initAll: async () => {} },
            EmbedComposer: {
                blankEmbed: () => ({}),
                emptyEmbed: () => ({}),
                mountEditor: () => ({ render() {} }),
                cleanEmbedForPayload: (e) => e,
                renderEmbedCardHtml: () => '',
                renderDiscordEmbedHtml: () => '',
                renderPreview: () => {},
            },
            $: (sel) => ({
                val: () => (sel === '#mb-channel' ? '1088248319982006331' : ''),
                trigger: () => {},
            }),
            fetch: async () => ({ ok: true, json: async () => ({ config: {}, tree: [], roles: [] }) }),
            document: {
                body: { addEventListener() {} },
                getElementById: getEl,
                querySelector: () => null,
                querySelectorAll: () => [],
                addEventListener() {},
            },
            window: null,
        };
        box.window = box;
        vm.createContext(box);
        vm.runInContext(mbScript, box);
        box.pickType('math');
        box.onSpawnInput();
        const payload = box.buildPayload();
        assert.strictEqual(payload.channel_id, '1088248319982006331', 'buildPayload() must preserve exact 64-bit channel_id string');
    }
}

// ── 4. NeroSelect Error Cache & Native <select> Fallback (No jQuery) ───────
async function testNeroSelectResilience() {
    const code = readFile('dashboard/static/js/nero-select.js');
    let callCount = 0;
    let failFirst = true;

    const selectEl = {
        dataset: { value: '1088248319982006321', assignableOnly: 'true' },
        classList: { contains: () => false },
        innerHTML: '',
        value: '',
        options: [],
        children: [],
        appendChild(child) { this.children.push(child); },
        setAttribute() {},
    };

    const sandbox = {
        console: { error() {}, warn() {}, log() {} },
        fetch: async () => {
            callCount += 1;
            if (failFirst) {
                return { ok: false, status: 503, json: async () => ({ results: [], error: 'Discord 503' }) };
            }
            return {
                ok: true,
                status: 200,
                json: async () => ({
                    results: [
                        { id: '1088248319982006321', name: 'ValidVIP', assignable: true },
                        { id: '1088248319982006999', name: 'HighRole', assignable: false, unassignable_label: 'Above @Nero' },
                    ],
                }),
            };
        },
        document: {
            addEventListener() {},
            querySelectorAll: () => [selectEl],
            createElement(tag) {
                return { tagName: tag.toUpperCase(), children: [], setAttribute() {}, appendChild(c) { this.children.push(c); } };
            },
        },
        window: null,
    };
    sandbox.window = sandbox;
    vm.createContext(sandbox);
    vm.runInContext(code, sandbox, { filename: 'nero-select.js' });

    // First attempt fails (503) AND jQuery ($) is undefined -> must NOT throw or permanently cache []
    await sandbox.window.NeroSelect.initOne(selectEl, 'role');
    assert.strictEqual(sandbox.window.NeroSelect.isReady(selectEl), false, 'Failed fetch must not mark picker ready');

    // Second attempt succeeds without jQuery -> native fallback populates options & preserves disabled reason
    failFirst = false;
    await sandbox.window.NeroSelect.initOne(selectEl, 'role');
    assert.strictEqual(callCount, 2, 'Failed fetch must not be cached across retries');
    assert.strictEqual(sandbox.window.NeroSelect.isReady(selectEl), true, 'Successful native fallback marks picker ready');
    assert(selectEl.innerHTML.includes('ValidVIP'), 'Native fallback must populate role options without jQuery');
    assert(selectEl.innerHTML.includes('Above @Nero'), 'Native fallback must include unassignable reason');
}

function testServerSelectDegradedTemplate() {
    const html = readFile('dashboard/templates/server_select.html');
    assert(html.includes('data.degraded'), 'server_select.html must inspect degraded discovery flag');
    assert(html.includes("s.category === 'indeterminate'"), 'server_select.html must avoid labeling indeterminate guilds as Bot not installed');
    assert(html.includes('Status temporarily unverified'), 'server_select.html must show neutral unavailable copy for indeterminate guilds');
}

(async function main() {
    await testEmbedBuilderSelectMenuRender();
    await testTicketsCategoryRenderAndEdit();
    await testTemplateEscapingAndMinigameSnowflake();
    await testNeroSelectResilience();
    testServerSelectDegradedTemplate();
    console.log('All Dashboard frontend regression checks passed.');
})().catch((err) => {
    console.error(err);
    process.exit(1);
});
