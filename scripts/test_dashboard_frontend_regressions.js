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

// ── 3. Stored/DOM XSS Checks Across Client Templates + Minigame Snowflake ──
function testTemplateEscapingAndMinigameSnowflake() {
    // 3a. trade.html summarizeOffer
    const tradeScript = extractInlineScripts(readFile('dashboard/templates/systems/trade.html'))[0];
    const tradeBox = {
        URLSearchParams,
        console,
        _escapeHtml: escapeHtmlHelper,
        currencyAmount: (k, v) => `${v} ${k}`,
        fetch: async () => ({ ok: true, json: async () => ({ trades: [] }) }),
        document: { getElementById: () => ({ value: '', innerHTML: '', textContent: '', addEventListener() {} }) },
        window: null,
    };
    tradeBox.window = tradeBox;
    vm.createContext(tradeBox);
    vm.runInContext(tradeScript, tradeBox);
    const offerHtml = tradeBox.summarizeOffer({ items: { '<img src=x onerror=alert(1)>': 2 } });
    assert(!offerHtml.includes('<img src=x'), 'trade.html summarizeOffer must escape item names');

    // 3b. tagmissions.html, tagpartners.html, general.html, ledger.html, reactionroles.html, customcommands.html, commands.html
    assert(
        readFile('dashboard/templates/systems/tagmissions.html').includes('${_tmEsc(r.title)}'),
        'tagmissions.html must escape r.title'
    );
    assert(
        readFile('dashboard/templates/systems/tagpartners.html').includes('${_tpEsc(r.partner_label'),
        'tagpartners.html must escape r.partner_label'
    );
    assert(
        readFile('dashboard/templates/config/general.html').includes('${_genEsc(m.text)}'),
        'config/general.html must escape status message text'
    );
    assert(
        readFile('dashboard/templates/systems/ledger.html').includes('${_ledgerEsc(e.source)}'),
        'systems/ledger.html must escape ledger source/reason'
    );
    assert(
        readFile('dashboard/templates/manage/reactionroles.html').includes('${_rrEsc(b.label)}'),
        'manage/reactionroles.html must escape button emoji and label'
    );
    assert(
        readFile('dashboard/templates/manage/customcommands.html').includes('${_ccEsc(getLabel(a))}'),
        'manage/customcommands.html must escape action label'
    );
    assert(
        readFile('dashboard/templates/manage/commands.html').includes('${_cmdEsc(firstAlias)}'),
        'manage/commands.html must escape alias hint'
    );
    assert(
        !readFile('dashboard/templates/manage/commands.html').includes('<code>!${firstAlias}</code>'),
        'manage/commands.html must escape firstAlias in innerHTML'
    );

    // 3c. minigame_builder.html must never run parseInt on mbState.channel_id
    const mbHtml = readFile('dashboard/templates/systems/minigame_builder.html');
    assert(
        !mbHtml.includes('parseInt(mbState.channel_id)'),
        'minigame_builder.html must not corrupt 64-bit channel_id via parseInt'
    );
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
    testTemplateEscapingAndMinigameSnowflake();
    await testNeroSelectResilience();
    testServerSelectDegradedTemplate();
    console.log('All Dashboard frontend regression checks passed.');
})().catch((err) => {
    console.error(err);
    process.exit(1);
});
