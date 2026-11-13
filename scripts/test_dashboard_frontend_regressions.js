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

async function testNeroSelectBotStatusCacheAndRetry() {
    const code = readFile('dashboard/static/js/nero-select.js');

    function makePicker(assignableOnly = true, initialValue = '') {
        const appended = [];
        return {
            dataset: { value: initialValue, assignableOnly: assignableOnly ? 'true' : 'false' },
            classList: { contains: () => false },
            innerHTML: '',
            value: null,
            appended,
            select2Active: false,
            select2InitCount: 0,
            select2DestroyCount: 0,
            triggerEvents: [],
            setAttribute() {},
        };
    }

    function makeJqueryStub() {
        const jq = (el) => {
            const api = {
                empty() { el.appended.length = 0; return api; },
                append(html) {
                    const str = String(html);
                    el.appended.push(str);
                    if (el.value === null && str.includes('value=""')) {
                        el.value = '';
                    }
                    return api;
                },
                select2(arg) {
                    if (arg === 'destroy') {
                        el.select2DestroyCount += 1;
                        el.select2Active = false;
                    } else {
                        el.select2InitCount += 1;
                        el.select2Active = true;
                    }
                    return api;
                },
                val(v) {
                    if (arguments.length === 0) return el.value;
                    el.value = v == null ? null : (Array.isArray(v) ? v.slice() : String(v));
                    return api;
                },
                trigger(ev) { el.triggerEvents.push(ev); return api; },
                hasClass(cls) { return cls === 'select2-hidden-accessible' ? !!el.select2Active : false; },
            };
            return api;
        };
        return jq;
    }

    // Sequence A: HTTP 200 bot_status="unknown" -> HTTP 200 bot_status="present"
    // Must NOT poison _optionsCache['role'], and must recover without a browser reload
    // both on the next picker initialization AND when re-initializing a degraded Select2 picker
    // (exercising select2('destroy')/rebuild, preserving selected role, respecting intentionally
    // cleared selections without restoring dataset.value, and avoiding duplicate options).
    {
        let fetchCalls = 0;
        let currentStatus = 'unknown';
        const SAVED_ROLE_ID = '1088248319982006322';
        const REWARD_ROLE_ID = '1088248319982006323';
        const picker1 = makePicker(true, SAVED_ROLE_ID);
        const clearedPicker = makePicker(true, SAVED_ROLE_ID);
        const picker2 = makePicker(true);
        const picker3 = makePicker(true);

        const sandbox = {
            Date: { now: () => 1000 },
            console: { error() {}, warn() {}, log() {} },
            $: makeJqueryStub(),
            fetch: async () => {
                fetchCalls += 1;
                const isUnknown = currentStatus === 'unknown';
                return {
                    ok: true,
                    status: 200,
                    json: async () => ({
                        bot_status: isUnknown ? 'unknown' : 'present',
                        results: [
                            {
                                id: SAVED_ROLE_ID,
                                name: 'SavedReward',
                                assignable: !isUnknown,
                                unassignable_reason: isUnknown ? 'bot_status_unknown' : null,
                                unassignable_label: isUnknown ? "Unable to verify Nero's server permissions right now" : null,
                            },
                            {
                                id: REWARD_ROLE_ID,
                                name: 'Rewardable',
                                assignable: !isUnknown,
                                unassignable_reason: isUnknown ? 'bot_status_unknown' : null,
                                unassignable_label: isUnknown ? "Unable to verify Nero's server permissions right now" : null,
                            },
                        ],
                    }),
                };
            },
            document: { addEventListener() {}, querySelectorAll: () => [] },
            window: null,
        };
        sandbox.window = sandbox;
        vm.createContext(sandbox);
        vm.runInContext(code, sandbox, { filename: 'nero-select.js' });

        await sandbox.window.NeroSelect.initOne(picker1, 'role');
        assert.strictEqual(fetchCalls, 1);
        assert.strictEqual(picker1.dataset.nsDegraded, '1', 'Unknown bot_status must mark picker degraded');
        assert.strictEqual(picker1.select2InitCount, 1);
        assert.strictEqual(picker1.select2Active, true);
        assert.strictEqual(picker1.value, SAVED_ROLE_ID, 'Initial preselected role must be set on Select2');
        assert.strictEqual(picker1.appended.length, 3, 'Initial render has 1 placeholder + 2 role options');
        assert(picker1.appended.some(h => h.includes('Rewardable') && h.includes('disabled')),
            'First picker under bot_status=unknown must keep non-selected assignable-only role disabled');

        // Initialize a second picker with SAVED_ROLE_ID in dataset.value under bot_status="unknown",
        // then intentionally clear its live selection while leaving dataset.value unchanged.
        await sandbox.window.NeroSelect.initOne(clearedPicker, 'role');
        assert.strictEqual(fetchCalls, 2, 'bot_status=unknown is uncached across degraded inits');
        assert.strictEqual(clearedPicker.dataset.nsDegraded, '1');
        assert.strictEqual(clearedPicker.value, SAVED_ROLE_ID);
        sandbox.$(clearedPicker).val('').trigger('change.select2');
        assert.strictEqual(clearedPicker.value, '');
        assert.strictEqual(clearedPicker.dataset.value, SAVED_ROLE_ID);

        // Clear dataset.value on picker1 so retry proves $(el).val() preserves its live selection
        picker1.dataset.value = '';

        // Discord recovers without a full browser reload: next picker fetches fresh and gets assignable roles
        currentStatus = 'present';
        await sandbox.window.NeroSelect.initOne(picker2, 'role');
        assert.strictEqual(fetchCalls, 3, 'bot_status=unknown must not poison _optionsCache across subsequent pickers');
        assert.strictEqual(picker2.dataset.nsDegraded, undefined);
        const p2Opt = picker2.appended.find(h => h.includes('Rewardable'));
        assert(p2Opt && !p2Opt.includes('disabled'),
            'Second picker after recovery must receive usable assignable roles');

        // Subsequent picker uses the healthy cached 'present' response without extra Discord calls
        await sandbox.window.NeroSelect.initOne(picker3, 'role');
        assert.strictEqual(fetchCalls, 3, 'Healthy bot_status=present response must be cached');

        // Re-running initOne/initAll on the degraded first picker destroys & rebuilds Select2,
        // preserves its selected role, and does not duplicate <option> elements.
        await sandbox.window.NeroSelect.initOne(picker1, 'role');
        assert.strictEqual(fetchCalls, 3, 'Degraded picker retry reuses fresh present cache');
        assert.strictEqual(picker1.dataset.nsDegraded, undefined, 'Retry must clear degraded flag');
        assert.strictEqual(picker1.select2DestroyCount, 1, 'Retry must destroy prior Select2 instance before rebuild');
        assert.strictEqual(picker1.select2InitCount, 2, 'Retry must re-initialize Select2');
        assert.strictEqual(picker1.select2Active, true);
        assert.strictEqual(picker1.value, SAVED_ROLE_ID, 'Retry must preserve selected role value');
        assert.strictEqual(picker1.appended.length, 3, 'Retry must not duplicate existing <option> tags');
        const savedMatches = picker1.appended.filter(h => h.includes(`value="${SAVED_ROLE_ID}"`));
        assert.strictEqual(savedMatches.length, 1, 'Selected role option must appear exactly once');
        assert(savedMatches[0].includes('selected'), 'Selected role option must remain marked selected');
        const p1Upgraded = picker1.appended.find(h => h.includes('Rewardable'));
        assert(p1Upgraded && !p1Upgraded.includes('disabled'),
            'Degraded picker must upgrade to usable assignable roles on retry without browser reload');

        // Re-running initOne on clearedPicker must NOT restore dataset.value when live value was ''
        await sandbox.window.NeroSelect.initOne(clearedPicker, 'role');
        assert.strictEqual(fetchCalls, 3);
        assert.strictEqual(clearedPicker.dataset.nsDegraded, undefined);
        assert.strictEqual(clearedPicker.select2DestroyCount, 1);
        assert.strictEqual(clearedPicker.select2InitCount, 2);
        assert.strictEqual(clearedPicker.value, '',
            'Intentionally cleared live selection must not restore old dataset.value on retry');
        assert.strictEqual(clearedPicker.appended.length, 3,
            'Retry on cleared picker must not duplicate <option> tags');
        assert(!clearedPicker.appended.some(h => h.includes('selected')),
            'No role option may be marked selected after user intentionally cleared selection');
    }

    // Sequence B: Confirmed bot_status="absent" uses a short TTL cache to avoid hammering Discord
    // on every picker init, then re-checks cleanly once the TTL expires without page reload.
    {
        let nowMs = 10000;
        let fetchCalls = 0;
        let currentStatus = 'absent';
        const pA1 = makePicker(true);
        const pA2 = makePicker(true);
        const pA3 = makePicker(true);

        const sandbox = {
            Date: { now: () => nowMs },
            console: { error() {}, warn() {}, log() {} },
            $: makeJqueryStub(),
            fetch: async () => {
                fetchCalls += 1;
                const assignable = currentStatus === 'present';
                return {
                    ok: true,
                    status: 200,
                    json: async () => ({
                        bot_status: currentStatus,
                        results: [{
                            id: '1088248319982006323',
                            name: 'Rewardable',
                            assignable,
                            unassignable_reason: assignable ? null : 'bot_missing',
                            unassignable_label: assignable ? null : 'Nero not found in server',
                        }],
                    }),
                };
            },
            document: { addEventListener() {}, querySelectorAll: () => [] },
            window: null,
        };
        sandbox.window = sandbox;
        vm.createContext(sandbox);
        vm.runInContext(code, sandbox, { filename: 'nero-select.js' });

        await sandbox.window.NeroSelect.initOne(pA1, 'role');
        assert.strictEqual(fetchCalls, 1);

        nowMs += 5000; // Within 30s TTL -> must reuse short-lived absent cache
        await sandbox.window.NeroSelect.initOne(pA2, 'role');
        assert.strictEqual(fetchCalls, 1, 'Confirmed absent status must short-cache to avoid repeated Discord calls per picker');

        nowMs += 30000; // Past 30s TTL and bot was invited -> refreshes without reload
        currentStatus = 'present';
        await sandbox.window.NeroSelect.initOne(pA3, 'role');
        assert.strictEqual(fetchCalls, 2, 'Expired absent cache must re-fetch on next picker init');
        const pA3Opt = pA3.appended.find(h => h.includes('Rewardable'));
        assert(pA3Opt && !pA3Opt.includes('disabled'),
            'Picker initialized after absent cache expiry and bot invite must receive assignable roles');
    }

    // Sequence C: Two simultaneous picker initializations share one in-flight /api/guild/roles request,
    // and failed requests clear _inflight cleanly so subsequent retries are not stuck.
    {
        let fetchCalls = 0;
        let shouldFail = true;
        let releaseFetch = null;
        const cFail1 = makePicker(true);
        const cFail2 = makePicker(true);
        const cOk1 = makePicker(true);
        const cOk2 = makePicker(true);

        const sandbox = {
            Date: { now: () => 20000 },
            console: { error() {}, warn() {}, log() {} },
            $: makeJqueryStub(),
            fetch: () => {
                fetchCalls += 1;
                return new Promise((resolve, reject) => {
                    releaseFetch = () => {
                        if (shouldFail) {
                            reject(new Error('network blip'));
                            return;
                        }
                        resolve({
                            ok: true,
                            status: 200,
                            json: async () => ({
                                bot_status: 'present',
                                results: [{
                                    id: '1088248319982006323',
                                    name: 'ConcurrentRole',
                                    assignable: true,
                                    unassignable_reason: null,
                                    unassignable_label: null,
                                }],
                            }),
                        });
                    };
                });
            },
            document: { addEventListener() {}, querySelectorAll: () => [] },
            window: null,
        };
        sandbox.window = sandbox;
        vm.createContext(sandbox);
        vm.runInContext(code, sandbox, { filename: 'nero-select.js' });

        // First simultaneous pair fails -> shares 1 in-flight request and clears _inflight in finally
        const failPair = Promise.all([
            sandbox.window.NeroSelect.initOne(cFail1, 'role'),
            sandbox.window.NeroSelect.initOne(cFail2, 'role'),
        ]);
        assert.strictEqual(fetchCalls, 1, 'Simultaneous failed inits must share 1 in-flight fetch');
        releaseFetch();
        await failPair;
        assert.strictEqual(cFail1.dataset.nsInit, undefined);
        assert.strictEqual(cFail2.dataset.nsInit, undefined);

        // Next simultaneous pair succeeds -> proves _inflight was cleared and shares 1 request
        shouldFail = false;
        const okPair = Promise.all([
            sandbox.window.NeroSelect.initOne(cOk1, 'role'),
            sandbox.window.NeroSelect.initOne(cOk2, 'role'),
        ]);
        assert.strictEqual(fetchCalls, 2, 'Simultaneous picker initializations must share exactly one in-flight /api/guild/roles request');
        releaseFetch();
        await okPair;
        assert.strictEqual(fetchCalls, 2);
        assert.strictEqual(cOk1.dataset.nsInit, '1');
        assert.strictEqual(cOk2.dataset.nsInit, '1');
        assert(cOk1.appended.some(h => h.includes('ConcurrentRole') && !h.includes('disabled')));
        assert(cOk2.appended.some(h => h.includes('ConcurrentRole') && !h.includes('disabled')));
    }
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
    await testNeroSelectBotStatusCacheAndRetry();
    testServerSelectDegradedTemplate();
    console.log('All Dashboard frontend regression checks passed.');
})().catch((err) => {
    console.error(err);
    process.exit(1);
});
