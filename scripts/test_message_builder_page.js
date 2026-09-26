#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════
   Message Builder v2 — phase 1, step 5a: the page.

   WHAT THIS HAS TO PROVE (step 5a scope: route shell + bootstrap +
   foundation loading + the draft load boundary + initial preview mount +
   lifecycle teardown)

     A. The page boots with ZERO network calls, the shell and the status line
        are on screen before storage resolves, and the preview is mounted
        exactly once.
     B. The regions the template declares are the regions the page uses, and
        5a fills none of them (no rail rows, no inspector controls, no action
        buttons, no validator): all of that is 5b/5c/5d/6.
     C. LOADING A DRAFT IS NOT AN EDIT. After a load the store holds the loaded
        payload byte-for-byte, is clean, has no undo entry, schedules no write
        and re-renders nothing.
     D. A record this build must not touch (corrupt/future) is preserved
        byte-for-byte, with the write guard LEFT UP — an edit plus an explicit
        save still writes nothing.
     E. STORAGE FAILURE IS NEVER "THERE IS NO DRAFT". With no IndexedDB, and
        with an IndexedDB that never answers, the page boots, edits in memory,
        reports the degraded state and never writes anything — including the
        last-draft pointer.
     F. One write per typing burst; a blank document is never written; the
        pointer is written once, only after a successful save.
     G. The differential preview is the only renderer, its nodes survive a
        patch, and its clock does not drift.
     H. The status bar derives "unsaved" from the STORE and never claims
        "Saved" while a write is still pending.
     I. Teardown releases everything (subscriptions, listeners, timers, the
        preview, the session), survives an htmx navigation, and a second visit
        re-loads the draft the first one saved.
     J. Hygiene: the v1 database is never opened, and the page keeps exactly one
        document object — the store's.
     K. Persistence must never confirm an unwritten document (the in-flight save
        race, step 5a follow-up).
     L. The structure rail on the page (step 5b): rows from the store, selection
        in both directions, undo/redo reflected, teardown clean.
     M. The inspector on the page (step 5c): the panel follows the selection,
        every edit reaches the canonical document, the preview patches once.
     N. The action bar on the page (step 5d): Undo/Redo drive the store, Copy
        JSON hands the page's own payload to the clipboard and reports through
        the one status region, a failed copy opens the fallback dialog, and
        teardown empties the container and releases the subscription.
     Q. A resolved failure stops being reported (step 5d-3c): undo/discard back
        to what storage holds clears the failure, an in-flight write does not,
        and the races around that rule hold.
     R. Validation (step 6a): the page owns #mb2-strip; the SERVED limits reach
        the page and are the only numbers used; a document change schedules ONE
        pass per burst and a load validates immediately; issues land in the
        store's ui.issues and nowhere else; the strip is hidden while clean and
        written only when its content actually changes; a missing or partial
        limits table is an explicit failure rather than a silent pass; the strip
        never enters the undo stack, never gains children and never becomes a
        second live region.

   Run:  node scripts/test_message_builder_page.js
   ═══════════════════════════════════════════════════════════════ */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { createDom, createFakeIdb, createWindow, parseTemplate, findById, materialize } = require('./support/dom_stub.js');

let pass = 0, fail = 0; const failures = [];
function assert(cond, name, extra) {
    if (cond) { pass++; console.log('  PASS', name); }
    else { fail++; failures.push(name + (extra ? ' — ' + extra : '')); console.log('  FAIL', name, extra || ''); }
}
function section(t) { console.log('\n== ' + t + ' =='); }

const ROOT_DIR = path.join(__dirname, '..');
const js = (...parts) => path.join(ROOT_DIR, 'dashboard', 'static', 'js', ...parts);
// NERO_MB_PAGE_SRC lets the mutation battery (scripts/support/mb_mutants.js)
// point this harness at a deliberately broken copy of the page module and
// confirm the checks below actually fail. A harness that cannot fail is
// evidence of nothing.
const PAGE_PATH = process.env.NERO_MB_PAGE_SRC || js('embed', 'message-builder-page.js');
const DRAFTS_PATH = process.env.NERO_DRAFTS_SRC || js('embed', 'drafts.js');
const STORE_PATH = process.env.NERO_STORE_SRC || js('embed', 'store.js');
const VALIDATE_PATH = process.env.NERO_VALIDATE_SRC || js('embed', 'validate.js');
const ACTIONBAR_PATH = process.env.NERO_ACTIONBAR_SRC || js('embed', 'views', 'actionbar.js');
const FOUNDATION = [
    js('nav-lifecycle.js'),
    js('embed', 'model.js'),
    js('embed', 'assets.js'),
    js('embed', 'asset-store.js'),
    STORE_PATH,
    VALIDATE_PATH,
    js('embed', 'discord-markdown.js'),
    js('embed', 'preview.js'),
    DRAFTS_PATH,
    js('embed', 'views', 'statusbar.js'),
    js('embed', 'views', 'rail.js'),
    js('embed', 'views', 'inspector.js'),
    ACTIONBAR_PATH,
    PAGE_PATH,
];
const TEMPLATE_TREE = parseTemplate(
    fs.readFileSync(path.join(ROOT_DIR, 'dashboard', 'templates', 'manage', 'message_builder.html'), 'utf8'));

const GUILD = '1111222233334444';
const OTHER_GUILD = '9999888877776666';
const RECORD_NOW = 1790284740000;      // the RECORD's clock is fixed; the page's stays real

/**
 * The served limits the page is given, mirroring
 * utils/discord_limits.limits_payload() — the table the route now renders into
 * data-limits. Section R changes numbers in it (proving the page reads THIS
 * table and has no copy of its own) and section R5 withholds it entirely.
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
const SERVED_LIMITS = servedLimits();

const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));
const rep = (ch, n) => String(ch).repeat(n);
/**
 * Count textContent writes to ONE element. The 6a claims are about the STRIP,
 * and other regions legitimately write their own text now (6b's counters), so a
 * global DOM-ops total would be measuring other people's work. This wraps the
 * element's own accessors instead, which is what "the strip was written once"
 * actually means.
 */
function watchText(el) {
    const desc = Object.getOwnPropertyDescriptor(el, 'textContent');
    let writes = 0;
    Object.defineProperty(el, 'textContent', {
        configurable: true,
        get() { return desc.get.call(el); },
        set(value) { writes++; desc.set.call(el, value); },
    });
    return function () { return writes; };
}
// The page's validateTimer is {owner, id} or null. A mutant can leave a
// raw realm Timeout behind instead, and JSON.stringify() cannot express one
// (it is circular) — so descriptions go through this.
function timerNote(t) {
    if (t === null || t === undefined) return String(t);
    if (typeof t === 'object') return '{owner:' + (t.owner || '?') + '}';
    return typeof t;
}

// ── Fixtures ─────────────────────────────────────────────────────
function blankDocument() {
    return {
        id: 'doc-1',
        guildId: GUILD,
        layout: 'legacy',
        content: '',
        embeds: [{
            id: 'emb-1', title: '', url: '', description: '', color: 0x7c5cbf,
            author: { name: '', url: '', icon: null },
            footer: { text: '', icon: null },
            thumbnail: null, image: null, timestamp: '', fields: [],
        }],
        rows: [],
        assets: {},
    };
}

function filledDocument() {
    const doc = blankDocument();
    doc.id = 'doc-filled';
    doc.content = 'Hello **world** and <@123456789012345678>';
    doc.embeds[0].title = 'A title';
    doc.embeds[0].description = 'Some *markdown*';
    doc.embeds[0].fields = [
        { id: 'fld-1', name: 'One', value: '1', inline: true },
        { id: 'fld-2', name: 'Two', value: '2', inline: false },
    ];
    return doc;
}

// ── Environment ──────────────────────────────────────────────────
function makeEnv(opts) {
    opts = opts || {};
    const dom = createDom();
    const win = createWindow();
    win.document = dom.document;
    win.__BOT_IDENTITY__ = opts.identity === undefined
        ? { name: 'Nero', avatar: 'https://cdn.example/avatar.png' }
        : opts.identity;

    const net = { calls: 0 };
    const consoleLines = { error: [], warn: [] };
    const sandbox = {
        window: win,
        document: dom.document,
        console: {
            log: () => {}, debug: () => {},
            warn: (...a) => consoleLines.warn.push(a.join(' ')),
            error: (...a) => consoleLines.error.push(a.join(' ')),
        },
        setTimeout, clearTimeout, setInterval, clearInterval,
        Promise, Object, Array, Math, Date, JSON, Number, String, RegExp, Error, TypeError, Set, Map, Symbol,
        isFinite, parseInt, parseFloat, encodeURIComponent, decodeURIComponent,
        fetch: () => { net.calls++; return Promise.reject(new Error('the page must not touch the network')); },
        navigator: { clipboard: { writeText: () => Promise.resolve() } },
    };
    vm.createContext(sandbox);
    FOUNDATION.forEach(file => {
        vm.runInContext(fs.readFileSync(file, 'utf8'), sandbox, { filename: path.basename(file) });
    });

    const env = { dom, win, sandbox, net, consoleLines, NERO: win.NERO, idb: null, guildId: GUILD };
    env.limits = opts.limits === undefined ? SERVED_LIMITS : opts.limits;
    env.mount = async function (settleMs) {
        const root = materialize(findById(TEMPLATE_TREE, 'mb2-root'), dom.document);
        root.setAttribute('data-guild-id', env.guildId);
        // The route renders the served limits into the shell (step 6a, L1); the
        // harness gives the page the same thing — a JSON string, or whatever a
        // test deliberately sets (a broken string, or null for "absent").
        if (typeof env.limits === 'string') root.setAttribute('data-limits', env.limits);
        else if (env.limits) root.setAttribute('data-limits', JSON.stringify(env.limits));
        env.root = root;
        dom.attach(root);
        env.before = { docListeners: dom.document.listeners.length, winListeners: win.listeners.length };
        const mounting = env.NERO.lifecycle.mount(dom.document);
        // Read synchronously: this is what "painted before storage resolves" means.
        env.early = {
            status: env.el('mb2-bar-status').textContent,
            previewChildren: env.el('mb2-mount').children.length,
            stores: !!env.NERO.embed.messageBuilderPage.current(),
        };
        await mounting;
        await sleep(settleMs == null ? 25 : settleMs);
        env.inst = env.NERO.embed.messageBuilderPage.current();
        return env;
    };
    env.unmount = () => env.NERO.lifecycle.unmount('test');
    env.el = (id) => dom.document.getElementById(id);
    env.statusText = () => env.el('mb2-bar-status').textContent;
    env.pill = () => {
        const status = env.el('mb2-bar-status');
        const found = status.children.find(c => (c.className || '').split(/\s+/).indexOf('mb2-status-pill') !== -1);
        return found ? found.textContent : '';
    };
    env.detail = () => {
        const status = env.el('mb2-bar-status');
        const found = status.children.find(c => (c.className || '').split(/\s+/).indexOf('mb2-status-detail') !== -1);
        return found ? found.textContent : '';
    };
    env.notice = () => {
        const status = env.el('mb2-bar-status');
        const found = status.children.find(c => (c.className || '').split(/\s+/).indexOf('mb2-status-notice') !== -1);
        return found ? found.textContent : '';
    };
    env.puts = (store) => (env.idb ? env.idb.log.filter(e => e.op === 'put-committed' && (!store || e.store === store)) : []);
    env.adapterStats = () => (env.session() && env.session().storage ? env.session().storage().stats() : null);
    env.draftsData = () => (env.idb.snapshot('nero_message_builder').drafts || {});
    env.metaData = () => (env.idb.snapshot('nero_message_builder').meta || {});
    env.record = (key) => env.draftsData()[key];
    env.settle = (ms) => sleep(ms == null ? 25 : ms);
    /**
     * Wait for a condition instead of guessing a duration. The 6b sections use
     * this: a debounced validation pass is asynchronous, and asserting on the
     * paint after a fixed sleep makes the check a race against the machine's
     * load. The condition is evaluated until it holds (or the budget runs out),
     * then the caller asserts — so a slow machine slows the test, it does not
     * fail it.
     */
    env.until = async function (predicate, ms) {
        const deadline = Date.now() + (ms == null ? 2000 : ms);
        while (Date.now() < deadline) {
            if (predicate()) return true;
            await sleep(10);
        }
        return false;
    };
    env.store = () => env.inst && env.inst.store;
    env.session = () => env.inst && env.inst.session;
    env.payload = (document_) => env.NERO.embed.model.stableStringify(
        env.NERO.embed.model.toDiscordPayload(document_ || env.store().getDocument()));
    return env;
}

/** Install a fake IndexedDB (optionally pre-seeded) before the page boots. */
function installIdb(env, spec) {
    const idb = createFakeIdb(spec || {});
    env.sandbox.indexedDB = idb;
    env.idb = idb;
    env.win.indexedDB = idb;
    return idb;
}

/** Build a v2 record the way the persistence boundary would have written it. */
function makeRecord(env, document_, opts) {
    opts = opts || {};
    const drafts = env.NERO.embed.drafts;
    const guildId = opts.guildId === undefined ? GUILD : opts.guildId;
    const documentId = opts.documentId || (document_ && document_.id) || 'doc-1';
    const record = drafts.buildRecord({
        guildId: guildId,
        documentId: documentId,
        document: document_,
        now: RECORD_NOW,
        previous: opts.previous || null,
    });
    if (opts.mutate) opts.mutate(record);
    return record;
}

/**
 * Make the NEXT readwrite transaction fail, the way a disk that has just gone
 * bad does — the fake has no such switch, and a page-level test of a FAILED
 * save needs one. Everything else is the real adapter talking to the real fake.
 */
function armWriteFailure(idb) {
    const state = { on: false };
    const wrapper = {
        open(name, version) {
            const req = idb.open(name, version);
            let real = null;
            Object.defineProperty(req, 'onsuccess', {
                configurable: true,
                get() { return real; },
                set(fn) {
                    real = fn && function (ev) {
                        const db = req.result;
                        const tx = db.transaction.bind(db);
                        db.transaction = function (storeName, mode) {
                            if (state.on && mode === 'readwrite') {
                                throw new Error('forced: the write transaction failed');
                            }
                            return tx(storeName, mode);
                        };
                        return fn(ev);
                    };
                },
            });
            return req;
        },
    };
    return { state: state, wrapper: wrapper };
}





function seedSpec(env, records, opts) {
    opts = opts || {};
    const drafts = env.NERO.embed.drafts;
    const spec = { seed: { nero_message_builder: { drafts: {}, assets: {}, meta: {} } } };
    records.forEach(record => { spec.seed.nero_message_builder.drafts[record.key] = record; });
    if (opts.pointer !== false && records.length) {
        const first = records[0];
        spec.seed.nero_message_builder.meta[drafts.metaKey(first.guildId === undefined ? GUILD : first.guildId, 'last')] =
            { documentId: first.documentId, updatedAt: RECORD_NOW };
    }
    return spec;
}

// ═══════════════════════════════════════════════════════════════
async function main() {
    // ─────────────────────────────────────────────────────────────
    section('A. boot: no network, shell first, one preview paint');
    // ─────────────────────────────────────────────────────────────
    {
        const env = makeEnv();
        installIdb(env);
        await env.mount();
        assert(env.net.calls === 0, 'boot makes zero network calls', String(env.net.calls));
        assert(env.early.status.length > 0 && env.early.status.indexOf('mb2') === -1,
            'the status line is rendered synchronously (before storage resolves)', JSON.stringify(env.early.status));
        assert(env.early.previewChildren === 0,
            'nothing is painted into the preview before the canonical document exists',
            String(env.early.previewChildren));
        assert(!!env.inst && !!env.inst.store && !!env.inst.session, 'the page instance exists with a store and a session');
        assert(env.root.getAttribute('data-page-error') === null,
            'the registry recorded no page error', String(env.root.getAttribute('data-page-error')));
        assert(env.el('mb2-mount').children.length === 1, 'the preview is mounted once',
            String(env.el('mb2-mount').children.length));
        assert(env.inst.preview.stats().patches === 1, 'exactly one preview paint at boot (one document, one render)',
            String(env.inst.preview.stats().patches));
        assert(env.inst.ctx.counters.initMs >= 0 && typeof env.inst.ctx.counters.initMs === 'number',
            'the registry measured init without it blocking');
        assert(env.consoleLines.error.length === 0, 'boot logs no errors', env.consoleLines.error.join(' | '));
        assert(env.dom.warnings.length === 0, 'no DOM trap was tripped', env.dom.warnings.join(' | '));
    }

    // ─────────────────────────────────────────────────────────────
    section('B. the shell regions and what fills them');
    // ─────────────────────────────────────────────────────────────
    {
        const env = makeEnv();
        installIdb(env);
        await env.mount();
        assert(env.el('mb2-rail-body').children.length === 2,
            'the rail shows the message root and the blank document s embed (5b)',
            String(env.el('mb2-rail-body').children.length));
        assert(/Message content/.test(env.el('mb2-rail-body').textContent),
            'and it is derived from the canonical document');
        assert(env.el('mb2-inspector-body').children.length === 1 &&
               env.el('mb2-inspector-body').getAttribute('data-insp-view') === 'content',
            'the inspector shows exactly the content panel (5c)',
            String(env.el('mb2-inspector-body').children.length));
        // 6a: the page now OWNS the strip. A blank document is clean, so the
        // region stays exactly as the template declared it — hidden, empty and
        // childless — and the store's issue list is the empty one it booted
        // with (no dispatch at all, see section R). The old 5a assertion said
        // "step 6 owns its contents"; this says what step 6 actually did.
        assert(env.el('mb2-strip').hidden === true && env.el('mb2-strip').textContent === '' &&
               env.el('mb2-strip').children.length === 0 &&
               env.el('mb2-strip').getAttribute('class') === 'mb2-strip',
            'the validation strip is hidden, empty, childless and untinted for a clean document',
            [env.el('mb2-strip').hidden, JSON.stringify(env.el('mb2-strip').textContent),
             env.el('mb2-strip').getAttribute('class')].join(' | '));
        assert(env.store().getUi().issues.length === 0 &&
               env.inst.validateTimer === null,
            'and the page validated the blank document once, with nothing left scheduled',
            [env.store().getUi().issues.length, 'issues', timerNote(env.inst.validateTimer)].join(' '));
        assert(env.inst.ctx.counters.validateRuns === 1,
            'exactly one validation run at boot (the load does not schedule a second)',
            String(env.inst.ctx.counters.validateRuns));
        // 5d fills the container — and only with actions that exist. This is the
        // one 5a placeholder that legitimately flips: it asserted the container
        // was empty until the buttons were real.
        const barButtons = env.el('mb2-bar-actions').children;
        assert(barButtons.length === 5 &&
            barButtons.map(b => b.getAttribute('data-mb2-action')).join(',') === 'undo,redo,save,copy,discard',
            'the action container holds the real actions (5d), in order',
            barButtons.map(b => b.getAttribute('data-mb2-action')).join(','));
        assert(env.el('mb2-rail').getAttribute('aria-labelledby') === 'mb2-rail-title' &&
               env.el('mb2-inspector').getAttribute('aria-labelledby') === 'mb2-inspector-title',
            'the regions the page touches keep their labelling');
        const status = env.el('mb2-bar-status');
        assert(status.children.length === 3, 'the status region holds pill + detail + notice',
            String(status.children.length));
    }

    // ─────────────────────────────────────────────────────────────
    section('C. loading a draft is not an edit');
    // ─────────────────────────────────────────────────────────────
    {
        // C1 — no pointer: a brand-new draft, nothing written.
        const env = makeEnv();
        installIdb(env);
        await env.mount();
        const store = env.store();
        const session = env.session();
        assert(store.isDirty() === false, 'a new draft starts clean');
        assert(store.canUndo() === false && store.historyDepth().size === 1,
            'a new draft has no undo entry', JSON.stringify(store.historyDepth()));
        assert(session.pendingSave() === false, 'nothing is scheduled to be written');
        assert(env.puts().length === 0, 'booting a new draft writes nothing', String(env.puts().length));
        assert(/^mb_/.test(session.documentId()), 'the session minted a draft identity', String(session.documentId()));
        assert(session.key() === env.NERO.embed.drafts.draftKey(GUILD, session.documentId()),
            'the draft key is guild-scoped and document-scoped', String(session.key()));
        assert(env.statusText().indexOf('No changes yet') !== -1, 'the bar says there is nothing to save yet',
            env.statusText());

        // C2 — an existing record: loaded byte-for-byte, clean, no write, no undo.
        const env2 = makeEnv();
        const record = makeRecord(env2, filledDocument(), { documentId: 'doc-filled' });
        installIdb(env2, seedSpec(env2, [record]));
        await env2.mount();
        const store2 = env2.store();
        const model = env2.NERO.embed.model;
        assert(model.stableStringify(model.toDiscordPayload(store2.getDocument())) ===
               model.stableStringify(model.toDiscordPayload(record.document)),
            'the loaded payload is byte-equivalent to the persisted canonical payload');
        assert(store2.getDocument().id === 'doc-filled' && store2.getDocument().embeds[0].id === 'emb-1' &&
               store2.getDocument().embeds[0].fields[0].id === 'fld-1',
            'the document, embed and field identities survive the load');
        assert(store2.isDirty() === false, 'a loaded draft is clean (the store marked it saved)');
        assert(env2.session().state().dirty === false, 'and the session agrees at rest — no phantom unsaved state',
            JSON.stringify(env2.session().state()));
        assert(store2.canUndo() === false && store2.historyDepth().size === 1,
            'loading did not push an undo entry', JSON.stringify(store2.historyDepth()));
        assert(env2.session().pendingSave() === false, 'loading did not schedule a write');
        assert(env2.puts().length === 0, 'loading wrote nothing', String(env2.puts().length));
        assert(env2.inst.preview.stats().patches === 1, 'loading painted the preview exactly once',
            String(env2.inst.preview.stats().patches));
        assert(env2.session().documentId() === 'doc-filled', 'the session adopted the loaded draft identity',
            String(env2.session().documentId()));
        assert(env2.session().state().revision === 1, 'the loaded revision is reported',
            String(env2.session().state().revision));

        // C3 — a repairable record: loaded, reported, still clean, still no write.
        const env3 = makeEnv();
        // A REPAIRED load means the stored bytes are missing something the model
        // can fill in (ids, an empty embeds array, the schema version). buildRecord
        // normalizes, so the damage is done to the stored record afterwards — which
        // is also the only way a v2 record can ever look like this.
        const repaired = makeRecord(env3, filledDocument(), {
            documentId: 'doc-repaired',
            mutate: (record) => { delete record.document.embeds[0].id; },
        });
        installIdb(env3, seedSpec(env3, [repaired]));
        await env3.mount();
        assert(env3.store().getDocument().embeds[0].id && env3.store().getDocument().embeds[0].id !== undefined,
            'a repaired load fills in the missing identity');
        assert(env3.store().isDirty() === false, 'a repaired load is still clean');
        assert(env3.puts().length === 0, 'a repaired load writes nothing back', String(env3.puts().length));
        assert(/recovered/i.test(env3.notice()), 'the bar reports that the draft was recovered', env3.notice());
    }

    // ─────────────────────────────────────────────────────────────
    section('D. a preserved record is never touched');
    // ─────────────────────────────────────────────────────────────
    {
        const RECORD_VERSION = (() => {
            const probe = makeEnv();
            return probe.NERO.embed.drafts.RECORD_VERSION;
        })();
        const cases = [
            // A record the validator CAN classify: it returns a verdict and the
            // session raises its write guard.
            ['corrupt', (record) => { record.document = 'not-a-document'; }],
            ['foreign', (record) => { record.namespace = 'some_other_app'; }],
            ['future', (record) => { record.schemaVersion = RECORD_VERSION + 1; }],
        ];
        for (const [label, mutate] of cases) {
            const env = makeEnv();
            const record = makeRecord(env, filledDocument(), { documentId: 'doc-' + label, mutate });
            const before = JSON.stringify(record);
            installIdb(env, seedSpec(env, [record]));
            const key = record.key;
            await env.mount();
            assert(JSON.stringify(env.record(key)) === before, label + ': the record is byte-identical after boot',
                JSON.stringify(env.record(key)));
            assert(env.puts().length === 0, label + ': boot wrote nothing', String(env.puts().length));
            assert(env.session().state().state === 'blocked',
                label + ': the write guard is up', env.session().state().state);
            assert(env.session().documentId() === 'doc-' + label,
                label + ': the guard is scoped to the preserved draft', String(env.session().documentId()));
            assert(env.store().getDocument() !== null && env.store().isDirty() === false,
                label + ': an in-memory document exists and starts clean');
            assert(env.notice().length > 0 && /untouched/i.test(env.notice()),
                label + ': the bar explains why nothing will be saved', env.notice());

            // An edit plus an explicit save must still write nothing, and the
            // edit must survive in memory.
            env.store().dispatch({ type: 'content/set', text: 'typed over a preserved record' });
            assert(env.store().isDirty() === true, label + ': the edit is dirty in the store',
                JSON.stringify(env.store().getDocument().content));
            const result = await env.session().saveNow();
            assert(result.ok === false && result.blocked === true,
                label + ': saveNow is refused while the record is guarded', JSON.stringify(result));
            assert(env.puts().length === 0, label + ': the refused save wrote nothing', String(env.puts().length));
            assert(JSON.stringify(env.record(key)) === before, label + ': the record is still byte-identical');
            assert(env.store().getDocument().content === 'typed over a preserved record',
                label + ': the refused edit is still in memory (nothing was dropped)');
            assert(env.session().resolveGuard('keep') === false, label + ': resolveGuard("keep") resolves nothing');
            assert(env.puts().length === 0 && JSON.stringify(env.record(key)) === before,
                label + ': keep left the record untouched');
        }

        // The one shape the persistence layer cannot even classify: a stored
        // document whose embeds is not an array. load() rejects instead of
        // returning a verdict, so there is no guard to raise — the page must
        // therefore never let a save reach that key.
        const env = makeEnv();
        const broken = makeRecord(env, filledDocument(), {
            documentId: 'doc-unreadable',
            mutate: (record) => { record.document.embeds = 'not-an-array'; },
        });
        const key = broken.key;
        const before = JSON.stringify(broken);
        installIdb(env, seedSpec(env, [broken]));
        await env.mount();
        assert(JSON.stringify(env.record(key)) === before, 'unreadable: the record is byte-identical after boot',
            JSON.stringify(env.record(key)));
        assert(env.puts().length === 0, 'unreadable: boot wrote nothing', String(env.puts().length));
        assert(env.session().documentId() !== 'doc-unreadable',
            'unreadable: the page adopted a NEW draft identity, so no save can reach the unreadable record',
            String(env.session().documentId()));
        assert(/could not be read/i.test(env.notice()) && /untouched/i.test(env.notice()),
            'unreadable: the bar says the draft could not be read and was left untouched', env.notice());
        assert(env.store().getDocument() !== null, 'unreadable: the page still has a document to edit');
        env.store().dispatch({ type: 'content/set', text: 'a new message' });
        await env.session().saveNow();
        await env.settle();
        assert(JSON.stringify(env.record(key)) === before,
            'unreadable: saving the new message did not touch the unreadable record');
        assert(env.puts('drafts').length === 1 && env.puts('drafts')[0].key !== key,
            'unreadable: the write landed under a different key, never on top of it',
            JSON.stringify(env.puts('drafts').map(p => p.key)));
    }

    section('E. storage failure is never "no draft"');
    // ─────────────────────────────────────────────────────────────
    {
        // E1 — no IndexedDB at all.
        const env = makeEnv({ indexedDB: 'absent' });
        await env.mount();
        assert(env.inst && env.store() && env.session(), 'the page boots with no IndexedDB');
        assert(env.net.calls === 0, 'and still makes no network call');
        const session = env.session();
        const state = session.state();
        assert(state.degraded, 'the degraded reason is reported', JSON.stringify(state));
        assert(env.store().isDirty() === false, 'a fresh in-memory draft is clean');
        assert(env.pill() === 'Editing in memory', 'the bar says editing happens in memory', env.pill());
        assert(/storage/i.test(env.detail() + env.notice()), 'the bar explains that nothing will be saved',
            env.detail() + ' | ' + env.notice());
        assert(!/no draft|nothing saved yet/i.test(env.notice()), 'the failure is not reported as "no draft exists"',
            env.notice());

        // Editing still works, in memory, and still paints the preview.
        env.store().dispatch({ type: 'content/set', text: 'in-memory edit' });
        assert(env.store().getDocument().content === 'in-memory edit', 'editing works with storage down');
        assert(env.inst.preview.stats().patches === 2, 'the preview still updates',
            String(env.inst.preview.stats().patches));
        const saved = await session.saveNow();
        assert(saved.ok === false, 'a save attempt fails rather than pretending', JSON.stringify(saved));
        assert(env.puts().length === 0, 'nothing was written', String(env.puts().length));
        const stats = env.adapterStats();
        assert(stats && stats.puts === 0 && stats.writes === 0,
            'the storage adapter recorded no write attempt of any kind', JSON.stringify(stats));
        assert(env.store().getDocument().content === 'in-memory edit',
            'the edit is still in memory after the failed save');
        assert(env.pill() === 'Save failed' || env.pill() === 'Editing in memory',
            'the bar reports the failure', env.pill());

        // E2 — IndexedDB exists but never answers (the 1.5 s open timeout).
        const env2 = makeEnv();
        installIdb(env2, { blockOpen: true });
        await env2.mount(1900);
        const idb2 = env2.idb;
        assert(idb2.log.some(e => e.op === 'open-blocked'), 'the storage open was attempted and nothing answered');
        assert(env2.inst && env2.store(), 'the page is still usable after a storage timeout');
        assert(env2.session().state().degraded, 'the timeout is reported as degraded storage',
            JSON.stringify(env2.session().state()));
        assert(env2.puts().length === 0, 'the timed-out storage was not written to');
        assert(env2.adapterStats().puts === 0,
            'the adapter recorded no put during the timeout', JSON.stringify(env2.adapterStats()));
        assert(Object.keys(env2.metaData()).length === 0,
            'the last-draft pointer was NOT written while storage was unavailable');
        assert(env2.store().getDocument() !== null && env2.store().isDirty() === false,
            'an in-memory document is available to edit');
        assert(/storage is unavailable/i.test(env2.notice()),
            'the timeout is not reported as an empty draft', env2.notice());
        env2.store().dispatch({ type: 'content/set', text: 'typed while storage was down' });
        assert(env2.store().getDocument().content === 'typed while storage was down',
            'the edit survives in memory');
    }

    // ─────────────────────────────────────────────────────────────
    section('F. writes: one per burst, never a blank, pointer once');
    // ─────────────────────────────────────────────────────────────
    {
        const env = makeEnv();
        installIdb(env);
        await env.mount();
        const store = env.store();
        const session = env.session();

        // A blank document is never written, not even on an explicit save.
        const blankSave = await session.saveNow();
        assert(blankSave.ok === true && blankSave.skipped === true && blankSave.reason === 'clean',
            'saving an untouched blank document is skipped', JSON.stringify(blankSave));
        assert(env.puts().length === 0, 'and writes nothing', String(env.puts().length));

        // A typing burst collapses into ONE write.
        for (let i = 1; i <= 5; i++) store.dispatch({ type: 'content/set', text: 'x'.repeat(i) });
        assert(session.pendingSave() === true, 'a burst schedules one pending write');
        assert(env.puts().length === 0, 'nothing is written per keystroke', String(env.puts().length));
        await env.settle(1700);
        assert(env.puts('drafts').length === 1, 'the burst produced exactly one draft write',
            String(env.puts('drafts').length));
        assert(env.puts('meta').length === 1, 'the last-draft pointer was written once (after the first success)',
            String(env.puts('meta').length));
        assert(session.pendingSave() === false, 'nothing is left pending');
        assert(env.store().isDirty() === false, 'the store reports the document as saved',
            JSON.stringify(env.store().getDocument().content));
        assert(env.pill() === 'Saved', 'the bar says Saved', env.pill());
        const stored = env.record(session.key());
        assert(!!stored && stored.document.content === 'xxxxx',
            'the stored record holds the final text', stored && stored.document.content);
        assert(stored.revision === 1, 'the first write is revision 1', String(stored.revision));
        assert(env.metaData()[env.NERO.embed.drafts.metaKey(GUILD, 'last')].documentId === session.documentId(),
            'the pointer names the draft that was written');

        // A second save does not rewrite the pointer. (The settle matters: a
        // pointer write is issued from the save's completion callback, so
        // asserting before the transaction commits would pass either way.)
        store.dispatch({ type: 'content/set', text: 'again' });
        await session.saveNow();
        await env.settle();
        assert(env.puts('meta').length === 1, 'the pointer is not rewritten on every save',
            String(env.puts('meta').length));
        assert(env.puts('drafts').length === 2, 'the second save wrote the draft once',
            String(env.puts('drafts').length));
        assert(env.record(session.key()).revision === 2, 'the revision advances',
            String(env.record(session.key()).revision));
    }

    // ─────────────────────────────────────────────────────────────
    section('G. preview: one renderer, stable nodes, stable clock');
    // ─────────────────────────────────────────────────────────────
    {
        // A document WITH content: the top-level node is the message bubble, and
        // editing its text must patch that node rather than build a new one.
        const env = makeEnv();
        const record = makeRecord(env, filledDocument(), { documentId: 'doc-filled' });
        installIdb(env, seedSpec(env, [record]));
        await env.mount();
        const mount = env.el('mb2-mount');
        const messageNode = mount.children[0];
        assert(messageNode && messageNode.getAttribute('data-key') === 'message',
            'a document with content renders the message node',
            messageNode && messageNode.getAttribute('data-key'));

        const before = env.inst.preview.stats();
        env.store().dispatch({ type: 'content/set', text: 'first edit' });
        const after = env.inst.preview.stats();
        assert(mount.children[0] === messageNode,
            'a content edit updates the existing preview node instead of rebuilding it');
        assert(after.nodesCreated === before.nodesCreated,
            'a text edit creates zero new nodes', String(after.nodesCreated - before.nodesCreated));
        assert(after.patches === before.patches + 1, 'exactly one patch per document change',
            String(after.patches - before.patches));

        // A status-only notification must not touch the renderer or the DOM.
        const rendererCalls = after.markdownRenders;
        env.store().markSaved();
        assert(env.inst.preview.stats().markdownRenders === rendererCalls,
            'marking the document saved does not re-render any markdown');
        assert(mount.children[0] === messageNode, 'and does not touch the preview DOM');

        // The preview's empty state, documented rather than hidden: a blank
        // document renders a "start typing" hint, so the FIRST keystroke swaps
        // that hint for the message bubble exactly once. Every keystroke after
        // that patches the bubble.
        const env2 = makeEnv();
        installIdb(env2);
        await env2.mount();
        const mount2 = env2.el('mb2-mount');
        const hint = mount2.children[0];
        assert(!!hint && hint.getAttribute('data-key') === 'empty',
            'a blank document renders the empty-state node', hint && hint.getAttribute('data-key'));
        env2.store().dispatch({ type: 'content/set', text: 'now there is content' });
        assert(mount2.children[0] !== hint && mount2.children[0].getAttribute('data-key') === 'message',
            'the first content swaps the empty state for the message node (one designed swap, not a rebuild)',
            mount2.children[0] && mount2.children[0].getAttribute('data-key'));
        const bubble = mount2.children[0];
        const stats2 = env2.inst.preview.stats();
        env2.store().dispatch({ type: 'content/set', text: 'and more' });
        assert(mount2.children[0] === bubble, 'further edits patch that node in place');
        assert(env2.inst.preview.stats().nodesCreated === stats2.nodesCreated,
            'and create nothing new', String(env2.inst.preview.stats().nodesCreated - stats2.nodesCreated));

        const clock = env.inst.preview.context().now;
        assert(typeof clock === 'function' && clock() === clock(),
            'the preview clock is a fixed function (the header time cannot drift)');
        assert(clock() === env.inst.startedAt, 'the preview uses the page-life clock', String(clock()));

        // The page never reaches for the renderer directly.
        const pageSrc = fs.readFileSync(PAGE_PATH, 'utf8');
        const statusSrc = fs.readFileSync(js('embed', 'views', 'statusbar.js'), 'utf8');
        assert(pageSrc.indexOf('discordMarkdown') === -1 && statusSrc.indexOf('discordMarkdown') === -1,
            'no page/view module calls the markdown renderer directly');
        assert(pageSrc.indexOf('innerHTML') === -1 && statusSrc.indexOf('innerHTML') === -1,
            'no page/view module writes markup through innerHTML');
    }

    section('H. the status bar is honest about dirty state');
    // ─────────────────────────────────────────────────────────────
    {
        const env = makeEnv();
        installIdb(env);
        await env.mount();
        const statusbar = env.NERO.embed.views.statusbar;
        const base = { session: { revision: 2, documentId: 'mb_1', writes: 1, state: 'clean' }, dirty: false, pending: false };
        // A VIEW, not a describe() result: describe() must be called exactly once.
        const viewFor = (patch, view) => Object.assign({}, base, view || {},
            { session: Object.assign({}, base.session, patch) });

        assert(statusbar.describe(viewFor({ blocked: 'future' })).state === 'blocked',
            'describe: a guarded record outranks everything',
            statusbar.describe(viewFor({ blocked: 'future' })).state);
        assert(statusbar.describe(viewFor({ lastError: { reason: 'write-error' } })).state === 'error',
            'describe: a failure outranks saving',
            statusbar.describe(viewFor({ lastError: { reason: 'write-error' } })).state);
        assert(statusbar.describe(viewFor({ saving: true }, { dirty: true })).label === 'Saving…',
            'describe: saving outranks unsaved',
            statusbar.describe(viewFor({ saving: true }, { dirty: true })).label);
        assert(statusbar.describe(viewFor({ degraded: 'no-indexeddb' }, { dirty: true })).state === 'degraded',
            'describe: degraded storage outranks a plain unsaved state',
            statusbar.describe(viewFor({ degraded: 'no-indexeddb' }, { dirty: true })).state);
        assert(statusbar.describe(Object.assign({}, base, { dirty: true })).state === 'dirty',
            'describe: a dirty store is reported as unsaved');
        assert(statusbar.describe(Object.assign({}, base, { dirty: false, pending: true })).state === 'pending',
            'describe: a queued write with a clean store still counts as unsaved');
        assert(statusbar.describe(base).state === 'saved',
            'describe: clean + a completed write is Saved');
        assert(statusbar.describe(Object.assign({}, base, { session: { revision: 0, documentId: 'mb_1', writes: 0 } })).state === 'clean',
            'describe: nothing written yet is not "Saved"');
        assert(statusbar.describe(Object.assign({}, base, { dirty: true })).label === 'Unsaved changes' &&
               statusbar.describe(base).label === 'Saved',
            'describe: dirty and saved have different labels');

        // The page derives the flag from the store, and the bar follows it.
        assert(env.pill() === 'No changes yet', 'a fresh page shows no changes', env.pill());
        env.store().dispatch({ type: 'content/set', text: 'dirty now' });
        assert(env.store().isDirty() === true && env.pill() === 'Unsaved changes',
            'a store edit turns the bar unsaved', env.pill());
        // The store flag alone is not the whole truth: an edit is still queued for
        // writing, so the bar keeps saying unsaved until the write lands.
        env.store().markSaved();
        assert(env.store().isDirty() === false, 'marking saved clears the store s dirty flag');
        assert(env.pill() === 'Unsaved changes',
            'a pending write still counts as unsaved, even with a clean store flag', env.pill());
        await env.session().saveNow();
        await env.settle();
        assert(env.pill() === 'Saved', 'once the write lands the bar says Saved', env.pill());
    }

    // ─────────────────────────────────────────────────────────────
    section('H2. never "Saved" while a write is still pending');
    // ─────────────────────────────────────────────────────────────
    {
        const env = makeEnv();
        const idb = installIdb(env);
        await env.mount();
        const store = env.store();
        const session = env.session();

        idb.hold();                                   // the next write cannot complete yet
        store.dispatch({ type: 'content/set', text: 'first' });
        const inflight = session.saveNow();           // starts the held write
        store.dispatch({ type: 'content/set', text: 'second' });   // edit during the write
        idb.release();
        await inflight;
        await env.settle();
        assert(env.pill() !== 'Saved',
            'after a write completes with a newer edit outstanding, the bar does NOT say Saved', env.pill());
        assert(env.pill() === 'Unsaved changes' || env.pill() === 'Saving…',
            'it says unsaved instead', env.pill());
        await session.saveNow();                      // flush the newer edit
        await env.settle();
        assert(env.pill() === 'Saved', 'once the newer edit is written it says Saved', env.pill());
        assert(env.record(session.key()).document.content === 'second',
            'the newest edit is what is stored', env.record(session.key()).document.content);
    }

    // ─────────────────────────────────────────────────────────────
    section('I. teardown, navigation and a second visit');
    // ─────────────────────────────────────────────────────────────
    {
        const env = makeEnv();
        installIdb(env);
        await env.mount();
        const store = env.store();
        const session = env.session();
        const preview = env.inst.preview;

        env.unmount();
        assert(store._subscriberCounts().selectors === 0 && store._subscriberCounts().listeners === 0,
            'teardown clears every store subscriber', JSON.stringify(store._subscriberCounts()));
        assert(preview.isDestroyed() === true, 'the preview was destroyed');
        assert(env.el('mb2-bar-status').children.length === 0, 'the status bar removed its nodes');
        assert(env.dom.document.listeners.length === env.before.docListeners,
            'no document listener leaked', env.dom.document.listeners.length + ' vs ' + env.before.docListeners);
        assert(env.win.listeners.length === env.before.winListeners,
            'no window listener leaked (lifecycle handlers are removed)', env.win.listeners.length + ' vs ' + env.before.winListeners);
        assert(session.pendingSave() === false, 'no write timer is left behind');
        const again = await session.destroy();
        assert(again.reason === 'already-destroyed', 'destroy is idempotent', JSON.stringify(again));
        assert(env.NERO.embed.messageBuilderPage.current() === null, 'the page released its instance');

        // pagehide after teardown must be inert (the lifecycle listeners are gone).
        env.win.dispatch('pagehide');
        env.win.dispatch('visibilitychange');
        assert(session.state().writes === 0 || session.state().writes >= 0, 'pagehide after teardown is inert');

        // A second visit boots cleanly and does not leak listeners across cycles.
        for (let i = 0; i < 3; i++) {
            await env.mount();
            assert(env.dom.document.listeners.length === env.before.docListeners,
                'cycle ' + (i + 1) + ': document listeners stay flat',
                String(env.dom.document.listeners.length));
            assert(env.el('mb2-mount').children.length === 1, 'cycle ' + (i + 1) + ': one preview');
            env.unmount();
            assert(env.win.listeners.length === env.before.winListeners,
                'cycle ' + (i + 1) + ': window listeners stay flat', String(env.win.listeners.length));
        }

        // An htmx navigation must run destroy before the DOM is swapped away.
        const env2 = makeEnv();
        installIdb(env2);
        await env2.mount();
        const store2 = env2.store();
        env2.dom.document.dispatch('htmx:beforeSwap', { detail: { target: env2.dom.document.body } });
        assert(store2._subscriberCounts().selectors === 0, 'htmx:beforeSwap tears the page down');
        assert(env2.NERO.embed.messageBuilderPage.current() === null, 'htmx navigation released the instance');

        // Refresh recovery: what the first session saved, the next one loads.
        const env3 = makeEnv();
        installIdb(env3);
        await env3.mount();
        env3.store().dispatch({ type: 'content/set', text: 'survives a reload' });
        await env3.session().saveNow();
        await env3.settle();
        const savedId = env3.session().documentId();
        env3.unmount();
        await env3.mount();
        assert(env3.session().documentId() === savedId, 'the second visit reopens the same draft',
            String(env3.session().documentId()));
        assert(env3.store().getDocument().content === 'survives a reload',
            'the saved content came back', env3.store().getDocument().content);
        assert(env3.store().isDirty() === false, 'the reopened draft is clean');
        assert(env3.puts('drafts').length === 1, 'reopening it does not rewrite it',
            String(env3.puts('drafts').length));
    }

    // ─────────────────────────────────────────────────────────────
    section('J. hygiene: one document, one database, no v1');
    // ─────────────────────────────────────────────────────────────
    {
        const env = makeEnv();
        const idb = installIdb(env);
        const record = makeRecord(env, filledDocument(), { documentId: 'doc-filled' });
        // Re-seed by installing a fresh fake with the record.
        const seeded = createFakeIdb(seedSpec(env, [record]));
        env.sandbox.indexedDB = seeded;
        env.idb = seeded;
        await env.mount();
        const store = env.store();
        const session = env.session();

        assert(session.document() === store.getDocument(),
            'the session and the store hold the SAME document object (no second copy of the state)');
        store.dispatch({ type: 'content/set', text: 'after load' });
        assert(session.document() === store.getDocument(),
            'and they still share it after an edit');
        assert(Object.prototype.hasOwnProperty.call(session, 'dirty') === false &&
               typeof session.state().dirty === 'boolean',
            'the session exposes its persistence dirty flag, and the page displays the store s');
        assert(session.isDirty() === store.isDirty(),
            'at rest the two dirty flags agree (store is authoritative for display)');

        const opened = Array.from(new Set(seeded.log.filter(e => e.op === 'open').map(e => e.db)));
        assert(opened.length === 1 && opened[0] === 'nero_message_builder',
            'only the v2 database was opened', opened.join(','));
        assert(seeded.log.every(e => e.db !== 'nero_embedbuilder'),
            'the v1 database never appears in the storage log');
        assert(seeded.storeNames('nero_message_builder').join(',') === 'assets,drafts,meta',
            'the v2 stores are created as approved', seeded.storeNames('nero_message_builder').join(','));
        assert(store.getDocument().rows.length === 0 && Object.keys(store.getDocument().assets).length === 0,
            'components and assets stay untouched in step 5');
        const pageSrc = fs.readFileSync(PAGE_PATH, 'utf8');
        assert(pageSrc.indexOf('toDiscordPayload') === -1,
            'the page builds no payload in 5a (Copy JSON arrives in 5d)');
        // 6a replaces the 5a "no validation issues are produced in step 5"
        // placeholder with the real rule: the store's list IS the validator's
        // output for the document the store holds — the edit above scheduled one
        // pass, and that pass produced it.
        await env.settle(250);
        const expected = env.NERO.embed.validate.validate(store.getDocument(), env.limits);
        assert(JSON.stringify(env.store().getUi().issues) === JSON.stringify(expected),
            'the issue list is the validator\'s own result for the canonical document',
            JSON.stringify(env.store().getUi().issues));
        assert(env.inst.ctx.counters.validateRuns === 2,
            'and the edit above cost exactly one validation pass (boot 1 + burst 1)',
            String(env.inst.ctx.counters.validateRuns));
    }

    // ─────────────────────────────────────────────────────────────
    section('K. the in-flight save race (persistence must not confirm an unwritten document)');
    // ─────────────────────────────────────────────────────────────
    // The bug this section pins down: saveNow() snapshots the document, writes
    // it ASYNCHRONOUSLY, and used to then confirm the CURRENT document to the
    // store. If the user typed during the write, that confirmation was a lie —
    // either the store's dirty flag was cleared and its saved hash moved onto
    // content that existed only in memory, or the store's document was rewound
    // to the older snapshot. The store now learns only WHICH HASH reached
    // storage (store.markSavedHash); its document, history and undo stack are
    // never touched by a write.
    {
        const hash = (d) => env.NERO.embed.model.hashDocument(d);

        // ── K1: the race, and the stronger invariant it must now hold ──
        const env = makeEnv();
        const idb = installIdb(env);
        await env.mount();
        const store = env.store();
        const session = env.session();
        const model = env.NERO.embed.model;

        // 1 ─ the store holds A
        store.dispatch({ type: 'content/set', text: 'A' });
        const A = store.getDocument();
        const depthBefore = store.historyDepth().size;

        // 2 ─ saveNow() begins persisting A, held open so the edit below lands
        //     strictly inside the write
        idb.hold();
        const inflight = session.saveNow();
        await env.settle(30);
        assert(env.record(session.key()) === undefined,
            'K1.2: nothing is committed while the write is held open');

        // 3 ─ the user edits to B before the write completes
        store.dispatch({ type: 'content/set', text: 'B' });
        const B = store.getDocument();
        assert(B !== A && store.isDirty() === true, 'K1.3: the store now holds B and is dirty',
            'sameObject=' + (B === A) + ' dirty=' + store.isDirty());
        const depthAfterEdit = store.historyDepth().size;

        // 4 ─ the write of A succeeds
        idb.release();
        await inflight;
        await env.settle(60);
        const committed = env.record(session.key());
        assert(!!committed && committed.document.content === 'A' && committed.revision === 1 &&
               committed.documentHash === hash(A),
            'K1.4: storage contains A (revision 1, hash describes A)',
            JSON.stringify({ content: committed && committed.document.content,
                revision: committed && committed.revision,
                hashIsA: committed && committed.documentHash === hash(A) }));

        // 5 ─ the stronger invariant
        assert(store.getDocument() === B,
            'K1.5: the current document is STILL the exact B object the user typed into',
            'sameObject=' + (store.getDocument() === B) + ' content=' + store.getDocument().content);
        assert(store.getDocument().content === 'B', 'K1.5: and it still reads B',
            store.getDocument().content);
        assert(store.isDirty() === true,
            'K1.5: the store stays DIRTY — B is newer than the persisted snapshot',
            'store.isDirty()=' + store.isDirty());
        assert(store.savedDocumentHash() === hash(A),
            'K1.5: the store s saved hash describes exactly what was written (A)',
            'savedDocumentHash === hash(A): ' + (store.savedDocumentHash() === hash(A)) +
            ', === hash(B): ' + (store.savedDocumentHash() === hash(B)));
        assert(store.historyDepth().size === depthAfterEdit,
            'K1.5: the write added no history entry',
            store.historyDepth().size + ' vs ' + depthAfterEdit);
        assert(store.historyDepth().size >= depthBefore && store.canUndo() === true,
            'K1.5: no undo entry was lost (the edit history is intact)',
            JSON.stringify(store.historyDepth()) + ' canUndo=' + store.canUndo());
        assert(session.savedHash() === hash(A),
            'K1.5b: the persistence boundary s saved hash also describes A',
            'savedHash === hash(A): ' + (session.savedHash() === hash(A)));

        // 6 ─ the session/UI must not report the document as fully saved
        assert(session.isDirty() === true && session.state().state !== 'saved' && env.pill() !== 'Saved',
            'K1.6: the session and the bar report unsaved work',
            JSON.stringify({ sessionDirty: session.isDirty(), state: session.state().state, pill: env.pill() }));

        // 7 ─ the coalesced save persists B
        await env.settle(1700);
        const afterB = env.record(session.key());
        assert(!!afterB && afterB.document.content === 'B' && afterB.revision === 2 &&
               afterB.documentHash === hash(B),
            'K1.7: the coalesced save persisted B (revision 2, hash describes B)',
            JSON.stringify({ content: afterB && afterB.document.content,
                revision: afterB && afterB.revision, hashIsB: afterB && afterB.documentHash === hash(B) }));

        // 8 + 9 ─ only now is it clean, describing B
        assert(store.savedDocumentHash() === hash(B),
            'K1.8: the store s saved hash now describes B',
            store.savedDocumentHash() === hash(B));
        assert(store.isDirty() === false && session.isDirty() === false,
            'K1.8: only after B is written is the document clean',
            JSON.stringify({ storeDirty: store.isDirty(), sessionDirty: session.isDirty() }));
        assert(session.savedHash() === hash(B) && session.state().revision === 2,
            'K1.9: the boundary s saved hash and the revision describe B, not A',
            JSON.stringify({ session: session.savedHash() === hash(B), revision: session.state().revision }));
        assert(store.getDocument() === B,
            'K1.9: all the way through, the store s document object was never replaced');
        assert(env.puts('drafts').length === 2, 'K1: exactly two draft writes for two documents',
            String(env.puts('drafts').length));
        assert(env.pill() === 'Saved', 'K1.8: the bar says Saved once B is on disk', env.pill());

        // ── K2: the equal-hash case — marking it saved is CORRECT ──
        const env2 = makeEnv();
        const idb2 = installIdb(env2);
        await env2.mount();
        const store2 = env2.store();
        const session2 = env2.session();
        store2.dispatch({ type: 'content/set', text: 'x' });
        idb2.hold();
        const flight2 = session2.saveNow();
        await env2.settle(30);
        store2.dispatch({ type: 'content/set', text: 'y' });     // a real edit ...
        store2.dispatch({ type: 'content/set', text: 'x' });     // ... reverted to the same content
        const reverted = store2.getDocument();
        idb2.release();
        await flight2;
        await env2.settle(60);
        assert(env2.NERO.embed.model.hashDocument(store2.getDocument()) ===
               env2.NERO.embed.model.hashDocument(env2.record(session2.key()).document),
            'K2: the in-memory document is hash-identical to the persisted snapshot');
        assert(store2.isDirty() === false,
            'K2: hash-identical content IS marked saved (the boundary does not over-protect)',
            'store.isDirty()=' + store2.isDirty());
        assert(store2.getDocument() === reverted,
            'K2: and the document object is still the one the user was editing');
        assert(session2.isDirty() === false, 'K2: the session agrees it is saved');
        await env2.settle(1700);
        assert(env2.puts('drafts').length === 1,
            'K2: no redundant second write happens', String(env2.puts('drafts').length));
        assert(env2.record(session2.key()).document.content === 'x',
            'K2: storage holds the reverted content exactly once');
        // Fixed in this checkpoint: the skipped ("clean") save resolves without
        // writing anything, but the pending write it cancelled is visible state
        // — so it notifies, and the bar can no longer stay stale.
        assert(env2.pill() === 'Saved',
            'K2: after a skipped save the bar reflects the settled state (not stale)',
            env2.pill());

        // ── K3: tearing the page down inside the race loses nothing ──
        const env3 = makeEnv();
        const idb3 = installIdb(env3);
        await env3.mount();
        env3.store().dispatch({ type: 'content/set', text: 'A' });
        idb3.hold();
        const flight3 = env3.session().saveNow();
        await env3.settle(30);
        env3.store().dispatch({ type: 'content/set', text: 'B' });
        idb3.release();
        await flight3;
        await env3.settle(30);
        const id3 = env3.session().documentId();
        env3.unmount();                                   // pagehide / htmx navigation
        await env3.settle(60);
        const flushed = env3.idb.snapshot('nero_message_builder').drafts['v2:' + GUILD + ':' + id3];
        assert(!!flushed && flushed.document.content === 'B' && flushed.revision === 2,
            'K3: leaving the page mid-race still flushes B (no data loss)',
            JSON.stringify({ content: flushed && flushed.document.content,
                revision: flushed && flushed.revision }));

        // ── K4: the coincidental-hash case (was the stale-hash limitation) ──
        // The editor is blanked back to exactly what the store hashed at boot
        // while A is in flight. The old code could read clean here; now the
        // store is told hash(A) is on disk while holding the blank document, so
        // it is dirty — and the document itself stays authoritative.
        const env4 = makeEnv();
        const idb4 = installIdb(env4);
        await env4.mount();
        const model4 = env4.NERO.embed.model;
        env4.store().dispatch({ type: 'content/set', text: 'A' });
        const A4 = env4.store().getDocument();
        idb4.hold();
        const flight4 = env4.session().saveNow();
        await env4.settle(30);
        env4.store().dispatch({ type: 'content/set', text: '' });   // back to blank
        const blanked = env4.store().getDocument();
        idb4.release();
        await flight4;
        await env4.settle(60);
        const stored4 = env4.record(env4.session().key());
        assert(!!stored4 && stored4.document.content === 'A',
            'K4: storage holds A while the editor is blank',
            JSON.stringify(stored4 && stored4.document.content));
        assert(env4.store().getDocument() === blanked && blanked.content === '',
            'K4: the blank document the user is editing is still the store s document');
        assert(env4.store().savedDocumentHash() === model4.hashDocument(A4),
            'K4: the store s saved hash describes A, the snapshot that was written',
            'savedDocumentHash === hash(A): ' + (env4.store().savedDocumentHash() === model4.hashDocument(A4)));
        assert(env4.store().isDirty() === true,
            'K4: the store reads DIRTY, not a stale coincidence',
            'store.isDirty()=' + env4.store().isDirty() + ' pill=' + env4.pill());
        assert(env4.session().isDirty() === true,
            'K4: and the session agrees there is unwritten work',
            JSON.stringify({ sessionDirty: env4.session().isDirty(), state: env4.session().state().state }));
        await env4.settle(1700);
        assert(env4.record(env4.session().key()).document.content === '',
            'K4: the blanking edit is persisted once the burst settles (a user CAN clear a saved draft)',
            JSON.stringify(env4.record(env4.session().key()).document.content));
        assert(env4.store().isDirty() === false && env4.session().isDirty() === false,
            'K4: and only then is everything clean',
            JSON.stringify({ storeDirty: env4.store().isDirty(), sessionDirty: env4.session().isDirty() }));

        // ── K5: the notification itself ──────────────────────────────
        // A pending write is visible state ("the newest edit is not written
        // yet"). When it resolves without writing anything, subscribers must
        // hear about it — otherwise the bar keeps claiming unsaved work.
        const env5 = makeEnv();
        const idb5 = installIdb(env5);
        await env5.mount();
        env5.store().dispatch({ type: 'content/set', text: 'saved once' });
        await env5.session().saveNow();
        await env5.settle();
        assert(env5.pill() === 'Saved', 'K5: the document is saved first', env5.pill());
        env5.session().schedule();                        // a write is queued ...
        // ... and a render happens (a real UI action: the page already selected
        // the message root at boot, so this selects the embed instead — a
        // same-value selection is a no-op reducer and would render nothing).
        env5.store().dispatch({
            type: 'ui/selectNode',
            nodeId: env5.store().getDocument().embeds[0].id,
        });
        assert(env5.pill() === 'Unsaved changes',
            'K5: a queued write reads as unsaved work', env5.pill());
        await env5.settle(1700);                          // ... and resolves as "clean"
        assert(env5.session().pendingSave() === false, 'K5: the queued write resolved');
        assert(env5.pill() === 'Saved',
            'K5: the bar is updated when a skipped save clears the queue (no stale status)',
            env5.pill());
        assert(idb5.log.filter(e => e.op === 'put-committed' && e.store === 'drafts').length === 1,
            'K5: and nothing was written for the skipped save');
    }

    // ─────────────────────────────────────────────────────────────
    section('L. the rail on the page');
    // ─────────────────────────────────────────────────────────────
    {
        const env = makeEnv();
        const record = makeRecord(env, filledDocument(), { documentId: 'doc-filled' });
        installIdb(env, seedSpec(env, [record]));
        await env.mount();
        const rail = env.inst.rail;
        const mount = env.el('mb2-rail-body');
        const model = env.NERO.embed.model;
        const doc = env.store().getDocument();

        assert(!!rail, 'the page created a rail');
        assert(mount.getAttribute('role') === 'tree', 'and turned the region into a tree');
        assert(env.inst.ctx.counters.initMs >= 0, 'mounting it did not break the registry s timing');

        // rows for the loaded document
        const ids = Array.from(mount.children).map(n => n.getAttribute('data-node-id'));
        assert(ids[0] === env.NERO.embed.views.rail.CONTENT_NODE, 'the message root is first', ids[0]);
        assert(ids.indexOf(doc.embeds[0].id) !== -1, 'the loaded embed has a row');
        assert(ids.indexOf(doc.embeds[0].fields[0].id) !== -1, 'so do its fields');
        assert(ids.indexOf(doc.embeds[0].fields[1].id) !== -1, 'all of them', ids.join(','));

        // the boot selection is the message root, and the rail shows it
        assert(env.store().getUi().selectedNodeId === env.NERO.embed.views.rail.CONTENT_NODE,
            'the page selects the message root at boot', String(env.store().getUi().selectedNodeId));
        const contentRow = mount.children[0];
        assert(contentRow.getAttribute('aria-selected') === 'true', 'and the rail reflects it');
        // ... and that boot selection is UI state, not a document edit: loading a
        // draft must stay fully non-mutating, undo history included.
        assert(env.store().canUndo() === false, 'the boot selection is not an undo entry');
        assert(env.store().historyDepth().size === 1, 'and the history is still a single state',
            String(env.store().historyDepth().size));

        // the preview still paints once, and the rail did not change that
        assert(env.inst.preview.stats().patches === 1,
            'the rail added no preview paints', String(env.inst.preview.stats().patches));

        // a rail action reaches the document and the preview patches it
        const before = env.inst.preview.stats();
        const embedId = doc.embeds[0].id;
        let addFieldButton = null;
        mount.children.forEach(row => {
            if (row.getAttribute('data-node-id') !== embedId) return;
            row.children.forEach(child => {
                if ((child.className || '').indexOf('mb2-rail-actions') === -1) return;
                child.children.forEach(button => {
                    if (button.getAttribute('data-rail-action') === 'addField') addFieldButton = button;
                });
            });
        });
        assert(!!addFieldButton, 'the embed row exposes an add-field button');
        mount.dispatch('click', { type: 'click', target: addFieldButton });
        assert(env.store().getDocument().embeds[0].fields.length === 3,
            'clicking it added a field through the store',
            String(env.store().getDocument().embeds[0].fields.length));
        assert(env.inst.preview.stats().patches === before.patches + 1,
            'and the preview patched itself once',
            String(env.inst.preview.stats().patches - before.patches));
        assert(env.store().isDirty() === true, 'the edit is dirty');
        assert(env.session().pendingSave() === true, 'and a save is queued (the rail does not bypass persistence)');

        // undo/redo from anywhere still drives the rail: the rows must always
        // mirror the canonical document, never a view-side copy of it.
        const railApi = env.NERO.embed.views.rail;
        function rowIdsNow() {
            return Array.from(mount.children).map(n => n.getAttribute('data-node-id'));
        }
        function derivedIds() {
            return railApi.derive(env.store().getDocument(), new Set()).map(vm => vm.id);
        }
        assert(rowIdsNow().join(',') === derivedIds().join(','),
            'the rows mirror the document after the edit', rowIdsNow().join(','));
        const rowsAfterAdd = rowIdsNow().length;

        env.store().undo();
        assert(env.store().getDocument().embeds[0].fields.length === 2, 'undo removed the field');
        assert(rowIdsNow().join(',') === derivedIds().join(','),
            'and the rail followed the undo', rowIdsNow().join(','));
        assert(rowIdsNow().length === rowsAfterAdd - 1, 'the row is gone',
            String(rowIdsNow().length));

        env.store().redo();
        assert(rowIdsNow().join(',') === derivedIds().join(','),
            'and the rail followed the redo', rowIdsNow().join(','));
        assert(rowIdsNow().length === rowsAfterAdd, 'the row is back', String(rowIdsNow().length));

        // teardown
        const selectorsBefore = env.store()._subscriberCounts().selectors;
        assert(selectorsBefore >= 4, 'the page has the rail s subscriptions plus its own',
            String(selectorsBefore));
        env.unmount();
        assert(env.el('mb2-rail-body').children.length === 0, 'teardown empties the rail');
        assert(env.el('mb2-rail-body').getAttribute('role') === null, 'and removes its tree semantics');
        assert(env.store()._subscriberCounts().selectors === 0, 'every subscription is gone');

        // revision-neutral: a load still leaves a clean document
        const env2 = makeEnv();
        const record2 = makeRecord(env2, filledDocument(), { documentId: 'doc-loaded' });
        installIdb(env2, seedSpec(env2, [record2]));
        await env2.mount();
        assert(env2.store().isDirty() === false,
            'the rail did not make the freshly loaded draft dirty');
        assert(env2.puts().length === 0, 'and it caused no write', String(env2.puts().length));
        assert(env2.store().getDocument().embeds[0].fields.length ===
               record2.document.embeds[0].fields.length,
            'its rows describe the loaded document, not a default');
    }

    // ─────────────────────────────────────────────────────────────
    section('M. the inspector on the page');
    // ─────────────────────────────────────────────────────────────
    {
        const env = makeEnv();
        const record = makeRecord(env, filledDocument(), { documentId: 'doc-inspected' });
        installIdb(env, seedSpec(env, [record]));
        await env.mount();

        const inspector = env.inst.inspector;
        const body = env.el('mb2-inspector-body');
        assert(!!inspector, 'the page created an inspector');
        assert(body.getAttribute('data-insp-view') === 'content',
            'it opens on the message root the page selected at boot',
            String(body.getAttribute('data-insp-view')));
        assert(body.children.length === 1, 'with one panel mounted', String(body.children.length));

        const content = inspector.control('content');
        assert(!!content && content.getAttribute('data-insp') === 'content',
            'the content control is there');
        assert(content.value === record.document.content,
            'and it shows the loaded draft, not a default',
            String(content.value));

        // Typing is a store edit: the document, the preview and the status bar
        // all move, and the rail is untouched (it is not the editing surface).
        const patchesBefore = env.inst.preview.stats().patches;
        const rowsBefore = env.el('mb2-rail-body').children.length;
        content.value = 'Typed on the page';
        body.dispatch('input', { type: 'input', target: content });
        assert(env.store().getDocument().content === 'Typed on the page',
            'typing reaches the canonical document', env.store().getDocument().content);
        assert(env.inst.preview.stats().patches === patchesBefore + 1,
            'the preview patched once, through the store subscription',
            String(env.inst.preview.stats().patches - patchesBefore));
        assert(env.store().isDirty() === true, 'the edit is dirty');
        assert(env.session().pendingSave() === true, 'and a save is queued');
        assert(env.el('mb2-rail-body').children.length === rowsBefore,
            'the rail did not gain rows for a content edit');

        // Selecting another node is UI state: no preview paint, new panel.
        const embedId = env.store().getDocument().embeds[0].id;
        const patchesAfterEdit = env.inst.preview.stats().patches;
        env.store().dispatch({ type: 'ui/selectNode', nodeId: embedId });
        assert(env.inst.preview.stats().patches === patchesAfterEdit,
            'selecting a node does not repaint the preview',
            String(env.inst.preview.stats().patches - patchesAfterEdit));
        assert(body.getAttribute('data-insp-view') === 'embed',
            'and the inspector shows the embed panel', String(body.getAttribute('data-insp-view')));
        const title = inspector.control('title');
        assert(title.value === record.document.embeds[0].title,
            'with the loaded embed s title', String(title.value));

        // An edit from the inspector and one from the rail both land in the same
        // place: the rail s add-field button changes the inspector s field list.
        const fieldsBefore = env.store().getDocument().embeds[0].fields.length;
        title.value = 'Renamed by the inspector';
        body.dispatch('input', { type: 'input', target: title });
        assert(env.store().getDocument().embeds[0].title === 'Renamed by the inspector',
            'the embed title is editable from the page');
        const embedRowLabel = (function () {
            let found = '';
            env.el('mb2-rail-body').children.forEach(row => {
                if (row.getAttribute('data-node-id') !== embedId) return;
                row.children.forEach(child => {
                    if ((child.className || '').indexOf('mb2-rail-label') !== -1) found = child.textContent;
                });
            });
            return found;
        })();
        assert(embedRowLabel.indexOf('Renamed by the inspector') !== -1,
            'the rail relabelled the renamed embed (store → rail)', embedRowLabel);

        let addFieldButton = null;
        env.el('mb2-rail-body').children.forEach(row => {
            if (row.getAttribute('data-node-id') !== embedId) return;
            row.children.forEach(child => {
                if ((child.className || '').indexOf('mb2-rail-actions') === -1) return;
                child.children.forEach(button => {
                    if (button.getAttribute('data-rail-action') === 'addField') addFieldButton = button;
                });
            });
        });
        env.el('mb2-rail-body').dispatch('click', { type: 'click', target: addFieldButton });
        assert(env.store().getDocument().embeds[0].fields.length === fieldsBefore + 1,
            'a rail action adds a field to the same document',
            String(env.store().getDocument().embeds[0].fields.length));
        const newFieldId = env.store().getDocument().embeds[0].fields[fieldsBefore].id;
        assert(env.store().getUi().selectedNodeId === newFieldId,
            'and the rail selected the new field (5b)');
        assert(body.getAttribute('data-insp-view') === 'field',
            'so the inspector follows it without being told (store → view)',
            String(body.getAttribute('data-insp-view')));

        const fieldValue = inspector.control('field.value');
        fieldValue.value = 'value from the page';
        body.dispatch('input', { type: 'input', target: fieldValue });
        assert(env.store().getDocument().embeds[0].fields[fieldsBefore].value === 'value from the page',
            'field values are editable from the page');

        // Undo is reflected in the control that made the change.
        env.store().undo();
        assert(fieldValue.value === '', 'undo reflects into the field input',
            String(fieldValue.value));

        // Going back to the embed shows a row per field, straight from the store.
        env.store().dispatch({ type: 'ui/selectNode', nodeId: embedId });
        const rowButtons = [];
        (function collect(node) {
            (node.children || []).forEach(child => {
                if (child.getAttribute && child.getAttribute('data-insp-action') === 'selectField') {
                    rowButtons.push(child);
                }
                collect(child);
            });
        })(body);
        assert(rowButtons.length === fieldsBefore + 1,
            'the embed panel lists every field in the document',
            String(rowButtons.length));
        assert(rowButtons.some(b => b.getAttribute('data-field-id') === newFieldId),
            'including the one the rail just added');

        // Teardown releases the inspector s DOM and its subscriptions.
        const selectorsBefore = env.store()._subscriberCounts().selectors;
        assert(selectorsBefore >= 6, 'the page holds the rail s and inspector s subscriptions',
            String(selectorsBefore));
        env.unmount();
        assert(env.el('mb2-inspector-body').children.length === 0, 'teardown empties the inspector');
        assert(env.el('mb2-inspector-body').getAttribute('data-insp-view') === null,
            'and removes its view marker');
        assert(env.store()._subscriberCounts().selectors === 0, 'every subscription is gone');
    }

    // ─────────────────────────────────────────────────────────────
    section('N. the action bar on the page');
    // ─────────────────────────────────────────────────────────────
    {
        const env = makeEnv();
        const record = makeRecord(env, filledDocument(), { documentId: 'doc-bar' });
        installIdb(env, seedSpec(env, [record]));
        // The bar reads the clipboard from the window, so this watches exactly
        // what the page hands the browser — and can switch it to a failure.
        const copied = [];
        env.win.navigator = {
            clipboard: {
                writeText(text) { copied.push(String(text)); return Promise.resolve(); },
            },
        };
        await env.mount();

        const bar = env.inst.actionbar;
        const actions = env.el('mb2-bar-actions');
        assert(!!bar, 'the page created an action bar');
        assert(actions.children.length === 5, 'and rendered its actions into the container',
            String(actions.children.length));
        assert(bar.keys().join(',') === 'undo,redo,save,copy,discard', 'in the approved order',
            bar.keys().join(','));
        assert(bar.button('undo').disabled === true && bar.button('redo').disabled === true,
            'a freshly loaded draft has nothing to undo or redo');
        assert(env.notice() === '', 'and booting invents no notice', env.notice());
        // The bar must not add a live region of its own: the page has exactly the
        // two the shell declares (the step-6 validation strip and the bar status).
        assert(env.root.querySelectorAll('[aria-live]').length === 2,
            'the page still declares exactly two live regions',
            String(env.root.querySelectorAll('[aria-live]').length));

        // An edit made anywhere on the page enables Undo: same store, one bar.
        const content = env.inst.inspector.control('content');
        content.value = 'Edited from the inspector';
        env.el('mb2-inspector-body').dispatch('input', { type: 'input', target: content });
        assert(env.store().isDirty() === true, 'the edit is dirty');
        assert(bar.button('undo').disabled === false, 'and the bar knows Undo is available');

        const patches = env.inst.preview.stats().patches;
        actions.dispatch('click', { type: 'click', target: bar.button('undo') });
        assert(env.store().getDocument().content === record.document.content,
            'Undo restores the loaded draft exactly', JSON.stringify(env.store().getDocument().content));
        assert(env.inst.preview.stats().patches === patches + 1,
            'the preview patched once, through the store subscription (never from the bar)',
            String(env.inst.preview.stats().patches - patches));
        assert(content.value === record.document.content, 'the inspector followed the undo',
            String(content.value));
        assert(bar.button('redo').disabled === false, 'and Redo became available');
        assert(env.store().isDirty() === false,
            'the restored document is the one on disk, so the store is clean again');

        // The edit and its undo must not leave anything new in storage: either
        // the queued write is skipped as clean or it writes the same payload.
        await env.settle(700);
        const storedKeys = Object.keys(env.draftsData());
        assert(storedKeys.length === 1 &&
            env.NERO.embed.model.stableStringify(env.draftsData()[storedKeys[0]].document) ===
            env.NERO.embed.model.stableStringify(record.document),
            'storage still holds exactly the loaded draft, byte for byte', storedKeys.join(','));
        assert(env.session().pendingSave() === false, 'and nothing is queued any more');
        assert(env.pill() === 'Saved' || env.pill() === 'No changes yet',
            'the status line agrees with the store, not with the bar', env.pill());

        // Copy JSON: the page's own payload, pretty-printed, announced once.
        actions.dispatch('click', { type: 'click', target: bar.button('copy') });
        await sleep(0);
        const expected = JSON.stringify(
            env.NERO.embed.model.toDiscordPayload(env.store().getDocument()), null, 2);
        assert(copied.length === 1, 'the clipboard received exactly one write', String(copied.length));
        assert(copied[0] === expected, 'and it is the payload of the page document, pretty-printed',
            String(copied[0]).slice(0, 40));
        assert(env.notice().indexOf('copied') !== -1,
            'the result is announced in the page\'s one status region', env.notice());
        assert(env.root.querySelectorAll('[aria-live]').length === 2,
            'and no extra live region appeared for it',
            String(env.root.querySelectorAll('[aria-live]').length));

        // A clipboard that refuses: the JSON stays reachable, Escape gets out.
        env.win.navigator.clipboard = { writeText() { return Promise.reject(new Error('denied')); } };
        actions.dispatch('click', { type: 'click', target: bar.button('copy') });
        await sleep(0);
        const dialog = bar.dialog();
        assert(!!dialog, 'a refused copy opens the fallback dialog on the page');
        const area = dialog.panel.querySelectorAll('textarea')[0];
        assert(!!area && area.value === expected, 'holding the same JSON',
            area ? String(area.value).slice(0, 40) : 'no text field');
        assert(env.dom.focused() === area, 'focused, so the user can copy it by hand');
        dialog.overlay.dispatch('keydown', {
            type: 'keydown', key: 'Escape', target: area, preventDefault() {},
        });
        assert(bar.dialog() === null, 'Escape closes the dialog');
        assert(env.dom.focused() === bar.button('copy'), 'and focus returns to the Copy JSON button');

        // Teardown: the container is emptied and the subscription released.
        assert(env.store()._subscriberCounts().listeners >= 1,
            'the bar subscribes to the store',
            String(env.store()._subscriberCounts().listeners));
        env.unmount();
        assert(env.el('mb2-bar-actions').children.length === 0, 'teardown empties the action bar');
        assert(env.store()._subscriberCounts().listeners === 0 && env.store()._subscriberCounts().selectors === 0,
            'and every subscription is gone',
            JSON.stringify(env.store()._subscriberCounts()));
    }

    // ─────────────────────────────────────────────────────────────
    section('O. discard on the page: back to the persisted version, never a write');
    // ─────────────────────────────────────────────────────────────
    {
        const env = makeEnv();
        const record = makeRecord(env, filledDocument(), { documentId: 'doc-discard' });
        const idb = installIdb(env, seedSpec(env, [record]));
        await env.mount();

        const bar = env.inst.actionbar;
        const store = env.store();
        const session = env.session();
        const actions = env.el('mb2-bar-actions');
        const button = bar.button('discard');
        assert(!!button, 'the page wired a discard action into the bar');
        assert(button.disabled === true,
            'a freshly loaded draft has nothing to discard (the store matches storage)');

        // An edit makes it available.
        const content = env.inst.inspector.control('content');
        content.value = 'edited after the load';
        env.el('mb2-inspector-body').dispatch('input', { type: 'input', target: content });
        assert(store.isDirty() === true, 'the edit is dirty');
        assert(button.disabled === false, 'so discarding becomes available');

        const writesBefore = env.puts('drafts').length;
        const pointerBefore = JSON.stringify(env.metaData());
        const documentIdBefore = session.documentId();
        const depthBefore = store.historyDepth();

        // Confirm through the real dialog.
        actions.dispatch('click', { type: 'click', target: button });
        const dialog = bar.dialog();
        assert(!!dialog && dialog.key === 'discard', 'clicking it asks for confirmation first');
        assert(store.getDocument().content === 'edited after the load',
            'and nothing has been restored while the dialog is open',
            store.getDocument().content);
        dialog.overlay.dispatch('click', { type: 'click', target: dialog.confirm });

        assert(store.getDocument().content === record.document.content,
            'confirming restores the persisted document exactly',
            JSON.stringify(store.getDocument().content));
        assert(env.payload(store.getDocument()) === env.payload(record.document),
            'byte for byte (same canonical payload)');
        assert(session.documentId() === documentIdBefore,
            'under the SAME draft identity (the id is untouched)',
            String(session.documentId()));
        assert(store.isDirty() === false, 'the store is clean again');
        assert(env.session().isDirty() === false, 'and so is the session');
        assert(store.canUndo() === false && store.historyDepth().size === 1 &&
            store.historyDepth().index === 0,
            'no undo entry was created — a discard cannot be undone, and cannot reach the discarded edit',
            JSON.stringify(store.historyDepth()));
        assert(depthBefore.size > 1, 'rig: the edit had created history', JSON.stringify(depthBefore));

        await env.settle(700);
        assert(env.puts('drafts').length === writesBefore,
            'NO persistence write happened',
            String(env.puts('drafts').length - writesBefore));
        assert(session.pendingSave() === false, 'and none is queued');
        assert(JSON.stringify(env.metaData()) === pointerBefore,
            'the last-draft pointer was not rewritten');
        assert(env.record(session.key()).documentHash === record.documentHash,
            'and the stored record is still the loaded one');
        assert(env.notice().indexOf('discarded') !== -1,
            'the result is reported in the page\'s one status region', env.notice());
        assert(button.disabled === true, 'with nothing left to discard the action goes unavailable');
        assert(env.pill() === 'Saved' || env.pill() === 'No changes yet',
            'and the status line agrees: the document is what storage holds', env.pill());

        // The preview followed the store, as always: it was not told anything.
        assert(env.inst.preview.stats().patches > 0, 'the preview patched through the store',
            String(env.inst.preview.stats().patches));
        env.unmount();
    }

    // ─────────────────────────────────────────────────────────────
    section('O2. the in-flight save must not survive a discard');
    // ─────────────────────────────────────────────────────────────
    {
        const env = makeEnv();
        const loaded = filledDocument();
        loaded.content = 'saved A';
        const record = makeRecord(env, loaded, { documentId: 'doc-race' });
        const idb = installIdb(env, seedSpec(env, [record]));
        await env.mount();

        const store = env.store();
        const session = env.session();
        const bar = env.inst.actionbar;
        const model = env.NERO.embed.model;
        const hash = (d) => model.hashDocument(d);

        assert(session.documentId() === 'doc-race', 'rig: the loaded draft is the session\'s identity',
            String(session.documentId()));
        assert(bar.button('discard').disabled === true, 'rig: nothing to discard yet');

        // The user edits to B and a save of B starts...
        store.dispatch({ type: 'content/set', text: 'B' });
        const B = store.getDocument();
        const depthAfterEdit = store.historyDepth();
        idb.hold();
        const inFlight = session.saveNow();
        await env.settle(30);
        assert(env.record(session.key()).document.content === 'saved A',
            'rig: the write of B is in flight and nothing has landed yet',
            env.record(session.key()).document.content);

        // ...and is discarded while it is still in the air.
        const pointerKey = Object.keys(env.metaData())[0];
        const pointerTarget = env.metaData()[pointerKey].documentId;
        assert(pointerTarget === 'doc-race', 'rig: the pointer names this draft',
            String(pointerTarget));
        env.el('mb2-bar-actions').dispatch('click', { type: 'click', target: bar.button('discard') });
        bar.dialog().overlay.dispatch('click', { type: 'click', target: bar.dialog().confirm });
        assert(store.getDocument().content === 'saved A',
            'the discard restored the persisted version (A) while the write of B was in flight',
            store.getDocument().content);
        assert(store.getDocument() !== B,
            'and it is not the same object as the unsaved B');
        assert(store.isDirty() === false, 'the store is clean right after the discard');
        assert(store.historyDepth().size === 1,
            'and the discard replaced the history baseline', JSON.stringify(store.historyDepth()));

        // Now the old write completes. It describes a document the user has
        // already discarded, so it must not become the store's document.
        idb.release();
        await inFlight;
        await env.settle(60);

        const committed = env.record(session.key());
        assert(committed.document.content === 'B' && committed.documentHash === hash(B),
            'rig: storage now holds B (the write that was already in the air landed)',
            JSON.stringify({ content: committed.document.content, hashIsB: committed.documentHash === hash(B) }));

        assert(store.getDocument().content === 'saved A',
            'THE INVARIANT: completing the old save does NOT replace the restored document',
            store.getDocument().content);
        assert(store.getDocument() !== B, 'and does not hand the store the discarded B object');
        assert(store.isDirty() === true,
            'the store is DIRTY, honestly: what it holds (A) is not what storage holds (B)',
            'dirty=' + store.isDirty());
        assert(store.savedDocumentHash() === hash(B),
            'the store s saved hash describes what was actually written (B)');
        assert(store.historyDepth().size === 1 && store.canUndo() === false,
            'the completion created no undo entry',
            JSON.stringify(store.historyDepth()));
        assert(store.historyDepth().size <= depthAfterEdit.size,
            'and left no trace of the discarded edit reachable by undo',
            JSON.stringify(store.historyDepth()));
        assert(env.metaData()[pointerKey].documentId === pointerTarget,
            'and the completion cannot REPOINT the last-draft pointer at anything else',
            String(env.metaData()[pointerKey].documentId));
        assert(env.metaData()[pointerKey].documentId === session.documentId(),
            'it still names the draft the session is editing',
            String(env.metaData()[pointerKey].documentId));
        assert(session.savedDocument().content === 'B',
            'the session\'s baseline is now B — the last thing that really persisted',
            session.savedDocument().content);
        assert(env.pill() === 'Unsaved changes',
            'and the bar says so (the document disagrees with storage)', env.pill());
        assert(bar.button('discard').disabled === false,
            'so a second discard is available (it would go back to B)');

        // A second discard lands on B: the honest successor state.
        env.el('mb2-bar-actions').dispatch('click', { type: 'click', target: bar.button('discard') });
        bar.dialog().overlay.dispatch('click', { type: 'click', target: bar.dialog().confirm });
        assert(store.getDocument().content === 'B',
            'discarding again restores what is actually persisted', store.getDocument().content);
        assert(store.isDirty() === false && store.canUndo() === false,
            'and the store is clean and undiscardable-again-free',
            JSON.stringify(store.historyDepth()));
        env.unmount();
    }

    // ─────────────────────────────────────────────────────────────
    section('O3. discard while a save is pending, then edit again');
    // ─────────────────────────────────────────────────────────────
    {
        const env = makeEnv();
        const loaded = filledDocument();
        loaded.content = 'saved A';
        const record = makeRecord(env, loaded, { documentId: 'doc-race-2' });
        const idb = installIdb(env, seedSpec(env, [record]));
        await env.mount();

        const store = env.store();
        const session = env.session();
        const bar = env.inst.actionbar;

        store.dispatch({ type: 'content/set', text: 'B' });
        idb.hold();
        const inFlight = session.saveNow();
        await env.settle(30);
        const writesWhileHeld = env.puts('drafts').length;

        bar.button('discard') && env.el('mb2-bar-actions').dispatch('click', { type: 'click', target: bar.button('discard') });
        bar.dialog().overlay.dispatch('click', { type: 'click', target: bar.dialog().confirm });
        assert(store.getDocument().content === 'saved A', 'the discard restored A',
            store.getDocument().content);

        // ...and the user types again BEFORE the old write finishes.
        store.dispatch({ type: 'content/set', text: 'C' });
        assert(store.getDocument().content === 'C', 'and the new edit is C');
        assert(store.isDirty() === true, 'which is unsaved');

        idb.release();
        await inFlight;
        await env.settle(60);

        assert(store.getDocument().content === 'C',
            'the completion left the newest edit exactly as the user typed it',
            store.getDocument().content);
        assert(store.isDirty() === true,
            'the store stays dirty (B persisted, C is newer)', 'dirty=' + store.isDirty());
        assert(session.pendingSave() === true || env.puts('drafts').length > writesWhileHeld,
            'and the newest edit is still owed a write',
            'pending=' + session.pendingSave() + ' writes=' + env.puts('drafts').length);

        await env.settle(1700);
        assert(env.record(session.key()).document.content === 'C',
            'which happens: storage ends up holding C',
            env.record(session.key()).document.content);
        assert(store.isDirty() === false && session.isDirty() === false,
            'and then both are clean');
        assert(store.getDocument().content === 'C', 'with the document untouched by all of it');
        assert(session.savedDocument().content === 'C',
            'and the baseline is what was last persisted',
            session.savedDocument().content);
        env.unmount();
    }

    // ─────────────────────────────────────────────────────────────
    section('O4. discard is refused where it would be meaningless or unsafe');
    // ─────────────────────────────────────────────────────────────
    {
        // A fresh page with no stored draft: there is no persisted version, so
        // discarding must not exist as an action at all.
        const env = makeEnv();
        installIdb(env, { seed: { nero_message_builder: { drafts: {}, assets: {}, meta: {} } } });
        await env.mount();
        assert(env.store().isDirty() === false, 'rig: a new draft starts clean');
        const button = env.inst.actionbar.button('discard');
        assert(!!button && button.disabled === true,
            'nothing has ever been persisted, so discard is unavailable');
        const before = env.store().getDocument();
        env.el('mb2-bar-actions').dispatch('click', { type: 'click', target: button });
        assert(env.inst.actionbar.dialog() === null,
            'and clicking it does not even ask (a disabled action is not an action)');
        assert(env.store().getDocument() === before, 'the document is untouched');
        env.unmount();

        // A PRESERVED record: the page never established a saved version, so a
        // discard must not hand the user a document that was never readable.
        const env2 = makeEnv();
        const corrupt = makeRecord(env2, filledDocument(), {
            documentId: 'doc-broken',
            mutate: (record) => { record.document = 'not-a-document'; },
        });
        installIdb(env2, seedSpec(env2, [corrupt]));
        await env2.mount();
        const button2 = env2.inst.actionbar.button('discard');
        assert(env2.session().guard() !== null, 'rig: the record is preserved (guard is up)');
        assert(!!button2 && button2.disabled === true,
            'a preserved record leaves discard unavailable');
        assert(env2.session().savedDocument() === null,
            'and the session has no saved baseline to offer');
        assert(env2.record(env2.session().key()).document === 'not-a-document',
            'the preserved record is still exactly as it was',
            JSON.stringify(env2.record(env2.session().key())));
        env2.unmount();
    }

    // ─────────────────────────────────────────────────────────────
    // ─────────────────────────────────────────────────────────────
    section('P. Save now on the page: the seven states, end to end');
    // ─────────────────────────────────────────────────────────────
    {
        const env = makeEnv();
        const idb = installIdb(env);
        const armed = armWriteFailure(idb);            // the disk can be broken on demand
        env.sandbox.indexedDB = armed.wrapper;
        env.win.indexedDB = armed.wrapper;
        await env.mount();

        const bar = env.inst.actionbar;
        const store = env.store();
        const session = env.session();
        const actions = env.el('mb2-bar-actions');
        const button = bar.button('save');
        assert(!!button, 'the page wired a save action into the bar');
        assert(button.getAttribute('data-mb2-action') === 'save' &&
            button.textContent === 'Save now' && button.disabled === true,
            '1. CLEAN: a fresh page has nothing to save, so the control is an unavailable "Save now"',
            String(button.textContent) + '/' + button.disabled);

        // ── 2. dirty → the control becomes available ──
        store.dispatch({ type: 'content/set', text: 'typed' });
        assert(button.disabled === false && button.textContent === 'Save now' &&
            button.getAttribute('data-mb2-save-state') === 'dirty',
            '2. DIRTY: typing makes it an available "Save now"',
            String(button.textContent) + '/' + button.disabled + '/' + button.getAttribute('data-mb2-save-state'));
        assert(env.pill() === 'Unsaved changes', 'and the status line agrees', env.pill());

        // ── 3. the click writes through the session's own path ──
        const putsBefore = env.puts('drafts').length;
        const revisionBefore = session.state().revision;
        idb.hold();                                   // the write cannot complete yet
        actions.dispatch('click', { type: 'click', target: button });
        assert(session.state().saving === true,
            'rig: a write is in flight', JSON.stringify(session.state()));
        assert(button.disabled === true &&
            button.getAttribute('data-mb2-save-state') === 'saving' &&
            /^Saving/.test(button.textContent),
            '3. SAVING: while the write is in flight the control says so and cannot be pressed again',
            String(button.textContent) + '/' + button.disabled);
        assert(env.pill() === 'Saving\u2026', 'and the status pill says the same thing', env.pill());
        // A second press while saving must not start a second write.
        actions.dispatch('click', { type: 'click', target: button });
        idb.release();
        await env.settle(60);
        assert(env.puts('drafts').length === putsBefore + 1,
            'exactly ONE write happened for the burst (a click during a save adds none)',
            String(env.puts('drafts').length - putsBefore));
        assert(session.state().revision === revisionBefore + 1,
            'the revision advanced once, through the normal write path',
            String(session.state().revision));

        // ── 4. saved ──
        assert(button.disabled === true && button.textContent === 'Saved' &&
            button.getAttribute('data-mb2-save-state') === 'saved',
            '4. SAVED: after persistence the control reads "Saved" and is unavailable',
            String(button.textContent) + '/' + button.disabled);
        assert(store.isDirty() === false && session.state().writes === 1,
            'the store is clean and the session counted the write',
            String(session.state().writes));
        assert(env.pill() === 'Saved', 'and the status line says Saved', env.pill());

        // ── 5. a failed save: retryable → "Try saving again" ──
        store.dispatch({ type: 'content/set', text: 'second edit' });
        armed.state.on = true;                        // the disk is broken now
        actions.dispatch('click', { type: 'click', target: button });
        await env.settle(60);
        const failed = session.state();
        assert(failed.state === 'error' && failed.lastError &&
            failed.lastError.reason === 'transaction-failed',
            '5. FAILED: the write failed and the session says why',
            JSON.stringify(failed.lastError));
        assert(session.retryable() === true,
            'the session reports it as retryable (asked, not acted on)');
        assert(button.disabled === false && button.textContent === 'Try saving again' &&
            button.getAttribute('data-mb2-save-state') === 'retry',
            'so the control offers the ONE retry action',
            String(button.textContent) + '/' + button.disabled);
        assert(store.isDirty() === true && env.pill() === 'Save failed',
            'the edit is still there, still unsaved', env.pill());

        // ── 6. the retry actually saves (recovery included) ──
        const recoveriesBefore = env.adapterStats().recoveries;
        armed.state.on = false;                       // the disk is fine again
        actions.dispatch('click', { type: 'click', target: button });
        await env.settle(80);
        assert(session.state().state === 'saved' && button.textContent === 'Saved',
            '6. RETRY: pressing it saves for real', String(session.state().state));
        assert(env.adapterStats().recoveries === recoveriesBefore + 1,
            'with exactly one storage recovery (the Step A path, unchanged)',
            String(env.adapterStats().recoveries));
        assert(env.record(session.key()).document.content === 'second edit',
            'and the newest content is what storage holds',
            JSON.stringify(env.record(session.key()).document.content));
        assert(session.state().revision === revisionBefore + 2,
            'the revision advanced normally', String(session.state().revision));

        // ── 7. reload: what the button saved survives ──
        const savedId = session.documentId();
        const writesAtReload = env.puts('drafts').length;
        env.unmount();
        await env.mount();
        assert(env.session().documentId() === savedId, '7. RELOAD: the same draft reopens',
            String(env.session().documentId()));
        assert(env.store().getDocument().content === 'second edit',
            'with what the save button persisted', env.store().getDocument().content);
        assert(env.store().isDirty() === false, 'and it reopens clean');
        assert(env.puts('drafts').length === writesAtReload,
            'reopening rewrites nothing', String(env.puts('drafts').length));
        // The reopened page has written nothing YET this page life, so both
        // surfaces describe it the same way: nothing is owed and nothing was
        // written here. The save control mirrors the status line rather than
        // inventing a second vocabulary for "it is on disk".
        const reopened = env.inst.actionbar.button('save');
        assert(reopened.textContent === 'Save now' && reopened.disabled === true &&
            reopened.getAttribute('data-mb2-save-state') === 'clean',
            'and both surfaces agree nothing is owed: a disabled "Save now"',
            String(reopened.textContent) + '/' + reopened.getAttribute('data-mb2-save-state'));
        assert(env.pill() === 'No changes yet',
            'with the status line saying the same thing', env.pill());
        env.unmount();
    }

    // ─────────────────────────────────────────────────────────────
    section('P2. a failure no retry can fix offers NO retry action');
    // ─────────────────────────────────────────────────────────────
    {
        const cases = [
            ['no IndexedDB at all', null],
            ['an IndexedDB that refuses to open', { throwOnOpen: true }],
        ];
        for (const pair of cases) {
            const env = makeEnv();
            installIdb(env, pair[1] || {});
            if (!pair[1]) {
                env.sandbox.indexedDB = null;          // win.indexedDB keeps the double
                env.win.indexedDB = null;
            }
            await env.mount();
            const button = env.inst.actionbar.button('save');
            const session = env.session();
            env.store().dispatch({ type: 'content/set', text: 'worth saving' });
            assert(button.disabled === true && button.textContent === 'Save now',
                'with ' + pair[0] + ', the control never becomes available',
                String(button.textContent) + '/' + button.disabled);

            // Even when a write IS attempted (the idle timer, or a forced one),
            // the failure is not retryable and the control must not pretend it is.
            await session.saveNow();
            assert(session.retryable() === false,
                'the session reports the failure as NOT retryable (' + pair[0] + ')');
            assert(button.textContent !== 'Try saving again' &&
                button.getAttribute('data-mb2-save-state') === 'unavailable' &&
                button.disabled === true,
                'and no retry action is offered for it',
                String(button.textContent) + '/' + button.getAttribute('data-mb2-save-state'));
            const presses = env.inst.actionbar.stats().saves;
            env.el('mb2-bar-actions').dispatch('click', { type: 'click', target: button });
            assert(env.inst.actionbar.stats().saves === presses,
                'and clicking it starts no save', String(env.inst.actionbar.stats().saves));
            assert(env.store().isDirty() === true,
                'the edit is still in memory, still unsaved (nothing was lost)');
            assert(env.puts('drafts').length === 0, 'and nothing was written',
                String(env.puts('drafts').length));
            env.unmount();
        }
    }


    // ─────────────────────────────────────────────────────────────
    section('P3. what a keystroke costs: the save control must not add work');
    // ─────────────────────────────────────────────────────────────
    // The save control needs the same two facts the status bar needs (store
    // dirty-ness and the session snapshot), and both of them hash the whole
    // document. Measured at 5d-3a — before this step existed — the page paid
    // exactly 10 hashDocument() calls per keystroke; the save control was built
    // to share those facts rather than ask for its own, so the number must not
    // grow. This is the guard: a control that recomputed them would show up
    // here immediately.
    {
        const env = makeEnv();
        installIdb(env, seedSpec(env, [makeRecord(env, filledDocument(), { documentId: 'doc-filled' })]));
        await env.mount();
        const modelRef = env.NERO.embed.model;
        const realHash = modelRef.hashDocument;
        let hashes = 0;
        try {
            modelRef.hashDocument = function (d) { hashes++; return realHash.call(this, d); };
            const content = env.inst.inspector.control('content');
            for (let i = 0; i < 10; i++) {
                content.value = 'typing ' + i;
                env.el('mb2-inspector-body').dispatch('input', { type: 'input', target: content });
            }
        } finally {
            modelRef.hashDocument = realHash;
        }
        const per = hashes / 10;
        console.log('    hashDocument() calls per keystroke: ' + per.toFixed(2) + ' (baseline 10)');
        assert(per <= 10,
            'a keystroke costs no more document hashing than it did before the save control existed',
            per.toFixed(2));
        assert(env.inst.actionbar.button('save').textContent === 'Save now' &&
            env.inst.actionbar.button('save').disabled === false,
            'rig: the document is dirty, so the save control was rendered from those same facts',
            String(env.inst.actionbar.button('save').textContent));
        env.unmount();
    }

    // ─────────────────────────────────────────────────────────────
    section('Q. manual-save races: a resolved failure stops being reported');
    // ─────────────────────────────────────────────────────────────
    // 5d-3c, end to end on the real page. The defect: a write fails, the user
    // Undoes (or Discards) back to the version storage holds, and the status bar
    // went on saying "Save failed" with an enabled "Try saving again" that could
    // never clear — the session's own state already said "saved". A failure
    // describes work that is still OWED, so with nothing owed the one save path
    // stops reporting it. These are the manual-save races around that rule: what
    // it must resolve, what it must NOT resolve, and what it must never touch.
    {
        const RECORD_VERSION = (() => {
            const probe = makeEnv();
            return probe.NERO.embed.drafts.RECORD_VERSION;
        })();

        /** One page, one stored draft, and a disk that can be broken on demand. */
        const qPage = async (content) => {
            const env = makeEnv();
            const loaded = filledDocument();
            loaded.content = content || 'saved A';
            const record = makeRecord(env, loaded, { documentId: 'doc-q' });
            const idb = installIdb(env, seedSpec(env, [record]));
            const armed = armWriteFailure(idb);
            env.sandbox.indexedDB = armed.wrapper;
            env.win.indexedDB = armed.wrapper;
            await env.mount();
            env.q = { record: record, loaded: loaded, armed: armed, idb: idb };
            return env;
        };
        const click = (env, name) => env.el('mb2-bar-actions').dispatch('click', { type: 'click', target: env.inst.actionbar.button(name) });
        const saveInfo = (env) => {
            const b = env.inst.actionbar.button('save');
            return { label: b.textContent, disabled: b.disabled === true, state: b.getAttribute('data-mb2-save-state') };
        };
        const edit = (env, text) => env.store().dispatch({ type: 'content/set', text: text });
        /** Break the disk, edit, and fail exactly one save: where every Q case starts. */
        const failOnce = async (env, text) => {
            edit(env, text || 'typed B');
            env.q.armed.state.on = true;
            click(env, 'save');
            await env.settle(60);
            return env.session().state();
        };
        /** Discard through the real dialog (the bar's only path to restoring). */
        const discardThroughDialog = (env) => {
            click(env, 'discard');
            const dialog = env.inst.actionbar.dialog();
            dialog.overlay.dispatch('click', { type: 'click', target: dialog.confirm });
            return dialog;
        };

        // ── Q1. a transient failure resolved by UNDO back to the saved version ──
        {
            const env = await qPage('saved A');
            const store = env.store(), session = env.session();
            assert(store.getDocument().content === 'saved A' && session.state().state === 'saved',
                'Q1 rig: the stored draft is open and nothing is owed', session.state().state);

            const failed = await failOnce(env, 'typed B');
            assert(failed.state === 'error' && failed.lastError && failed.lastError.reason === 'transaction-failed',
                'Q1 rig: the write failed', JSON.stringify(failed.lastError));
            assert(saveInfo(env).state === 'retry' && saveInfo(env).disabled === false && env.pill() === 'Save failed',
                'Q1 rig: the failure is reported with the ONE retry action offered', JSON.stringify(saveInfo(env)));

            // The user takes the edit back: the draft IS what storage holds.
            assert(store.undo() === true, 'Q1: the edit is undone');
            assert(store.isDirty() === false && session.isDirty() === false && store.getDocument().content === 'saved A',
                'Q1 rig: the draft is back to the stored version');
            assert(session.state().state === 'saved' && session.state().lastError !== null,
                'Q1 rig: the session already says "saved" while still recording the failure — the 5d-3b defect',
                JSON.stringify(session.state().lastError));
            assert(saveInfo(env).state === 'retry' && saveInfo(env).disabled === false,
                'Q1 rig: and the control still offers a retry with nothing left to retry', JSON.stringify(saveInfo(env)));

            const before = {
                puts: env.puts('drafts').length, writes: session.state().writes,
                revision: session.state().revision, depth: JSON.stringify(store.historyDepth()),
            };
            click(env, 'save');                                   // press the retry
            await env.settle(60);
            const after = session.state();
            assert(after.lastError === null,
                'Q1: the resolved failure is no longer reported', JSON.stringify(after.lastError));
            assert(after.state === 'saved' && after.dirty === false && session.isDirty() === false,
                'Q1: the session says saved, and nothing is owed', after.state);
            assert(env.pill() !== 'Save failed', 'Q1: the status region no longer claims a failure', env.pill());
            assert(env.pill() === 'Editing in memory',
                'Q1: it reports what is still true — the storage latch the failed write left behind (priority rules unchanged)',
                env.pill());
            assert(saveInfo(env).state === 'unavailable' && saveInfo(env).label === 'Save now' &&
                saveInfo(env).disabled === true,
                'Q1: the dead retry affordance is gone; what is left is the disabled explanation of that latch (D3)',
                JSON.stringify(saveInfo(env)));
            assert(env.puts('drafts').length === before.puts && after.writes === before.writes &&
                after.revision === before.revision,
                'Q1: resolving wrote nothing at all',
                JSON.stringify({ puts: env.puts('drafts').length, writes: after.writes }));
            assert(JSON.stringify(store.historyDepth()) === before.depth,
                'Q1: and added nothing to history', JSON.stringify(store.historyDepth()));
            assert(env.record(session.key()).document.content === 'saved A' &&
                env.record(session.key()).documentHash === env.q.record.documentHash,
                'Q1: storage still holds exactly the version it held',
                env.record(session.key()).document.content);
            env.unmount();
        }

        // ── Q2. the same failure resolved by DISCARD back to the saved version ──
        {
            const env = await qPage('saved B');
            const store = env.store(), session = env.session();
            await failOnce(env, 'typed C');
            assert(session.state().lastError !== null && saveInfo(env).state === 'retry',
                'Q2 rig: a failed write, with the retry offered');

            assert(env.inst.actionbar.button('discard').disabled === false,
                'Q2 rig: there is a stored version to go back to');
            discardThroughDialog(env);
            assert(store.getDocument().content === 'saved B' && store.isDirty() === false,
                'Q2 rig: the discard restored the stored version', store.getDocument().content);
            assert(session.state().lastError !== null && saveInfo(env).state === 'retry',
                'Q2 rig: the failure is still on record with nothing owed (the same defect, reached by discarding)');

            const putsBefore = env.puts('drafts').length;
            const writesBefore = session.state().writes;
            click(env, 'save');
            await env.settle(60);
            assert(session.state().lastError === null && session.state().state === 'saved',
                'Q2: pressing the retry resolves the failure — one press, no write, nothing owed',
                JSON.stringify(session.state().lastError));
            assert(env.pill() !== 'Save failed' && saveInfo(env).state === 'unavailable',
                'Q2: the status region stops claiming a failure and the dead retry is gone',
                env.pill() + '/' + saveInfo(env).state);
            assert(env.puts('drafts').length === putsBefore && session.state().writes === writesBefore,
                'Q2: and nothing was written', String(env.puts('drafts').length - putsBefore));
            assert(env.record(session.key()).document.content === 'saved B',
                'Q2: storage is untouched', env.record(session.key()).document.content);
            env.unmount();
        }

        // ── Q3. an edit while the failure is on record: the retry writes the NEWEST content ──
        {
            const env = await qPage('saved A');
            const store = env.store(), session = env.session();
            await failOnce(env, 'typed B');
            edit(env, 'typed B2');                                 // the user keeps typing
            env.q.armed.state.on = false;                           // the disk is fine again
            const putsBefore = env.puts('drafts').length;
            const writesBefore = session.state().writes;

            click(env, 'save');
            await env.settle(80);
            assert(env.puts('drafts').length === putsBefore + 1 && session.state().writes === writesBefore + 1,
                'Q3: the retry wrote exactly once',
                String(env.puts('drafts').length - putsBefore));
            assert(env.record(session.key()).document.content === 'typed B2',
                'Q3: and what it wrote is the NEWEST content, not the snapshot that failed',
                env.record(session.key()).document.content);
            assert(session.state().lastError === null && session.state().state === 'saved' &&
                store.isDirty() === false,
                'Q3: the failure is resolved by the write landing, and nothing is owed',
                JSON.stringify(session.state().lastError));
            assert(env.pill() === 'Saved' && saveInfo(env).state === 'saved' &&
                saveInfo(env).label === 'Saved' && saveInfo(env).disabled === true,
                'Q3: both surfaces agree it is saved', env.pill() + '/' + JSON.stringify(saveInfo(env)));
            assert(session.retryable() === false, 'Q3: and the retry is no longer on offer');
            env.unmount();
        }

        // ── Q4. a discard while the retry's write is still in the air ──
        {
            const env = await qPage('saved A');
            const store = env.store(), session = env.session();
            await failOnce(env, 'typed B');
            env.q.armed.state.on = false;                           // the disk is fine again
            env.q.idb.hold();                                       // ...but the write cannot land yet
            click(env, 'save');
            await env.settle(30);
            assert(session.state().saving === true,
                'Q4 rig: the retry started a write that is still in flight');

            discardThroughDialog(env);
            assert(store.getDocument().content === 'saved A' && store.isDirty() === false,
                'Q4: the discard restored the stored version while that write was in the air',
                store.getDocument().content);

            env.q.idb.release();
            await env.settle(80);
            const after = session.state();
            assert(after.saving === false && after.lastError === null,
                'Q4: the write landed with nothing left in flight and no failure reported',
                JSON.stringify(after.lastError));
            assert(store.getDocument().content === 'saved A',
                'Q4: the stale completion did NOT replace the restored document',
                store.getDocument().content);
            assert(env.record(session.key()).document.content === 'typed B',
                'Q4 rig: storage holds what actually landed (the edit that was in the air)',
                env.record(session.key()).document.content);
            assert(store.isDirty() === true && session.isDirty() === true && env.pill() === 'Unsaved changes',
                'Q4: so the page is honestly dirty again — what it shows is not what storage holds',
                env.pill());
            assert(saveInfo(env).state === 'dirty' && saveInfo(env).disabled === false,
                'Q4: which is exactly what the control now says', JSON.stringify(saveInfo(env)));
            assert(env.inst.actionbar.button('discard').disabled === false,
                'Q4: and a second discard is available (it would go back to what really persisted)');
            env.unmount();
        }

        // ── Q5. a press while the idle write is still scheduled: one write, not two ──
        {
            const env = await qPage('saved A');
            const session = env.session();
            edit(env, 'typed B');
            assert(session.pendingSave() === true, 'Q5 rig: the idle write is scheduled');
            click(env, 'save');                                    // the same path, now
            await env.settle(60);
            assert(env.puts('drafts').length === 1 && session.state().writes === 1,
                'Q5: the press wrote once', String(env.puts('drafts').length));
            assert(session.pendingSave() === false, 'Q5: and the queued write was cancelled, not duplicated');
            await env.settle(1600);                                // longer than the idle interval
            assert(env.puts('drafts').length === 1 && session.state().writes === 1,
                'Q5: nothing fired afterwards — a cancelled idle write cannot double-write',
                String(env.puts('drafts').length));
            assert(env.pill() === 'Saved', 'Q5: and the page says saved', env.pill());
            env.unmount();
        }

        // ── Q6. two presses in one tick: one write ──
        {
            const env = await qPage('saved A');
            const session = env.session(), bar = env.inst.actionbar;
            edit(env, 'typed B');
            const revisionBefore = session.state().revision;
            env.q.idb.hold();
            click(env, 'save');
            click(env, 'save');                                    // the second press lands in the same tick
            await env.settle(30);
            assert(session.state().saving === true, 'Q6 rig: a write is in flight');
            assert(bar.stats().savePresses === 2 && bar.stats().saves === 1,
                'Q6: both presses are counted, but the second one started NO second save',
                JSON.stringify({ presses: bar.stats().savePresses, saves: bar.stats().saves }));
            env.q.idb.release();
            await env.settle(80);
            assert(env.puts('drafts').length === 1 && session.state().writes === 1 &&
                session.state().revision === revisionBefore + 1,
                'Q6: exactly one write landed for the burst',
                JSON.stringify({ puts: env.puts('drafts').length, writes: session.state().writes }));
            assert(session.state().lastError === null && env.pill() === 'Saved',
                'Q6: and it is saved, with no failure invented', env.pill());
            env.unmount();
        }

        // ── Q7. the retry fails again: the failure stays reported ──
        {
            const env = await qPage('saved A');
            const store = env.store(), session = env.session();
            await failOnce(env, 'typed B');
            const putsBefore = env.puts('drafts').length;
            click(env, 'save');                                    // the disk is still broken
            await env.settle(60);
            assert(session.state().state === 'error' &&
                session.state().lastError && session.state().lastError.reason === 'transaction-failed',
                'Q7: a retry that fails again keeps the failure on record',
                JSON.stringify(session.state().lastError));
            assert(saveInfo(env).state === 'retry' && saveInfo(env).disabled === false &&
                env.pill() === 'Save failed',
                'Q7: so the retry stays on offer and the status region keeps saying so',
                JSON.stringify(saveInfo(env)));
            assert(store.isDirty() === true && session.isDirty() === true,
                'Q7: the edit is still owed, and still in memory');
            assert(env.puts('drafts').length === putsBefore,
                'Q7: nothing was committed', String(env.puts('drafts').length - putsBefore));
            assert(env.record(session.key()).document.content === 'saved A',
                'Q7: and storage still holds the last version that really landed',
                env.record(session.key()).document.content);
            env.unmount();
        }

        // ── Q8. a failure no retry can fix: no retry is offered, and the rule still resolves it ──
        {
            const env = await qPage('saved A');
            const store = env.store(), session = env.session();
            edit(env, 'typed B');
            assert(saveInfo(env).state === 'dirty' && saveInfo(env).disabled === false,
                'Q8 rig: the edit made the save available', JSON.stringify(saveInfo(env)));
            // How §24 makes a document that cannot be stored: the asset registry
            // is preserved as-is, so a Blob there reaches the persistence gate.
            session.document().assets = { a1: { blob: new Blob(['x'], { type: 'image/png' }), filename: 'one.png' } };
            const putsBefore = env.puts('drafts').length;
            click(env, 'save');
            await env.settle(60);
            assert(session.state().lastError && session.state().lastError.reason === 'not-serializable',
                'Q8 rig: the write failed for a reason no retry can fix',
                JSON.stringify(session.state().lastError));
            assert(session.retryable() === false && saveInfo(env).state === 'unavailable' &&
                saveInfo(env).label === 'Save now' && saveInfo(env).disabled === true,
                'Q8: the control switched to the disabled explanation — never "Try saving again"',
                JSON.stringify(saveInfo(env)));
            assert(env.pill() === 'Save failed', 'Q8: and the status region reports the failure', env.pill());
            assert(env.puts('drafts').length === putsBefore && session.state().writes === 0,
                'Q8: nothing reached storage — this was never the disk\'s fault',
                String(env.puts('drafts').length - putsBefore));
            assert(store.isDirty() === true, 'Q8: the edit is still in memory');

            // Undo takes the draft (and the poison) back to the stored version,
            // and the SAME rule resolves a non-retryable failure (D2).
            store.undo();
            assert(store.isDirty() === false && session.isDirty() === false,
                'Q8 rig: the draft is back to the stored version');
            await session.saveNow();                               // the save path, as the idle timer would run it
            assert(session.state().lastError === null && session.state().state === 'saved',
                'Q8: the same rule clears it — whether a retry could work is not what decides',
                JSON.stringify(session.state().lastError));
            assert(env.pill() !== 'Save failed' && env.pill() === 'No changes yet',
                'Q8: with nothing owed and nothing written, the page says exactly that',
                env.pill());
            assert(saveInfo(env).state === 'clean' && saveInfo(env).disabled === true,
                'Q8: and the control is the disabled "nothing to save", not a stale failure',
                JSON.stringify(saveInfo(env)));
            assert(env.puts('drafts').length === putsBefore &&
                env.record(session.key()).document.content === 'saved A',
                'Q8: storage is untouched', env.record(session.key()).document.content);
            env.unmount();
        }

        // ── Q9. a preserved record: the guard outranks the rule ──
        {
            const env = makeEnv();
            const record = makeRecord(env, filledDocument(), {
                documentId: 'doc-qguard',
                mutate: (r) => { r.schemaVersion = RECORD_VERSION + 1; },
            });
            installIdb(env, seedSpec(env, [record]));
            await env.mount();
            const store = env.store(), session = env.session();
            assert(session.state().state === 'blocked' && session.guard() !== null,
                'Q9 rig: the preserved record raised the write guard');
            assert(saveInfo(env).state === 'blocked' && saveInfo(env).disabled === true,
                'Q9: the control is the disabled explanation of the guard (unchanged, D3)',
                JSON.stringify(saveInfo(env)));
            assert(session.state().lastError === null, 'Q9 rig: a guarded session has no failure to report');
            const refused = await session.saveNow();
            assert(refused.ok === false && refused.blocked === true,
                'Q9: a save is refused before the skip path, so the resolution rule cannot fire here',
                JSON.stringify(refused));
            assert(session.state().lastError === null && session.isDirty() === false,
                'Q9: and nothing was invented: no failure, no dirty state');
            edit(env, 'typed over a guarded record');
            click(env, 'save');                                    // a disabled action is not an action
            const refused2 = await session.saveNow();
            assert(refused2.blocked === true && session.state().state === 'blocked' &&
                session.state().lastError === null,
                'Q9: an edit over a guarded record is still refused, and still not reported as a failure',
                JSON.stringify(refused2));
            assert(store.isDirty() === true && env.puts().length === 0,
                'Q9: the edit is in memory and nothing was written');
            assert(JSON.stringify(env.record(record.key)) === JSON.stringify(record),
                'Q9: the preserved record is byte-identical');
            env.unmount();
        }

        // ── Q10. an environment failure: latched, never re-probed, and still resolvable ──
        {
            const env = makeEnv();
            installIdb(env);
            env.sandbox.indexedDB = null;                          // no IndexedDB at all
            env.win.indexedDB = null;
            await env.mount();
            const store = env.store(), session = env.session();
            assert(env.pill() === 'Editing in memory', 'Q10 rig: the page boots in memory', env.pill());
            edit(env, 'worth saving');
            await session.saveNow();
            assert(session.state().lastError && session.state().lastError.reason === 'no-indexeddb',
                'Q10 rig: the save failed because there is no storage',
                JSON.stringify(session.state().lastError));
            assert(session.retryable() === false && env.pill() === 'Save failed',
                'Q10: a failure no retry can fix is reported, with no retry offered',
                JSON.stringify(saveInfo(env)));
            assert(session.storage().stats().opens === 0 && session.storage().reason() === 'no-indexeddb',
                'Q10: and nothing re-probed the environment to find that out',
                JSON.stringify(session.storage().stats()));

            // Nothing left to save (a draft with no content that was never
            // persisted): the same rule resolves it, and the environment stays
            // latched — the resolution is NOT a storage probe.
            edit(env, '');
            assert(session.isDirty() === false, 'Q10 rig: nothing is owed any more');
            const opensBefore = session.storage().stats().opens;
            await session.saveNow();
            assert(session.state().lastError === null && session.state().state === 'clean',
                'Q10: the environment failure stops being reported once nothing is owed',
                JSON.stringify(session.state().lastError));
            assert(env.pill() === 'Editing in memory' && env.pill() !== 'Save failed',
                'Q10: the page goes back to what is true — editing in memory', env.pill());
            assert(session.storage().stats().opens === opensBefore &&
                session.storage().reason() === 'no-indexeddb',
                'Q10: an environment failure is still latched and never auto-re-probed',
                JSON.stringify(session.storage().stats()));
            assert(session.storage().stats().puts === 0 && env.puts().length === 0,
                'Q10: and nothing was written, then or now');
            assert(store.isDirty() === false, 'Q10: the document is the empty one it started as');
            env.unmount();
        }

        // ── Q11. teardown: the final flush waits for the write, it does not add one ──
        {
            const env = await qPage('saved A');
            const session = env.session();
            await failOnce(env, 'typed B');
            env.q.armed.state.on = false;
            env.q.idb.hold();
            click(env, 'save');                                    // the retry's write is in the air
            await env.settle(30);
            assert(session.state().saving === true, 'Q11 rig: a write is in flight');
            const putsBefore = env.puts('drafts').length;
            env.unmount();                                         // destroy flushes what is owed
            await env.settle(30);
            assert(env.puts('drafts').length === putsBefore,
                'Q11: teardown did NOT start a second write for the same edit',
                String(env.puts('drafts').length - putsBefore));
            env.q.idb.release();
            await env.settle(80);
            assert(env.puts('drafts').length === putsBefore + 1 && session.state().writes === 1,
                'Q11: exactly one write landed — the one that was already in flight',
                JSON.stringify({ puts: env.puts('drafts').length, writes: session.state().writes }));
            assert(session.state().lastError === null && session.state().saving === false,
                'Q11: it succeeded, and the failure it replaced is gone',
                JSON.stringify(session.state().lastError));
            assert(env.record(session.key()).document.content === 'typed B',
                'Q11: storage holds the edit that was in the air',
                env.record(session.key()).document.content);
        }

        // ── Q11b. teardown onto a broken disk: it reports its own failure ──
        {
            const env = await qPage('saved A');
            const session = env.session();
            await failOnce(env, 'typed B');                         // the disk stays broken
            const putsBefore = env.puts('drafts').length;
            env.unmount();                                         // destroy flushes what is owed
            await env.settle(80);
            assert(env.puts('drafts').length === putsBefore,
                'Q11b: the final flush wrote nothing (the disk is still broken)',
                String(env.puts('drafts').length - putsBefore));
            assert(session.state().lastError && session.state().lastError.reason === 'transaction-failed',
                'Q11b: and it reports its own failure rather than pretending the draft is safe',
                JSON.stringify(session.state().lastError));
            assert(env.record(session.key()).document.content === 'saved A',
                'Q11b: storage still holds the last version that really landed',
                env.record(session.key()).document.content);
        }

        // ── Q12. hygiene: the step adds no surface, no node and no noise ──
        {
            const env = await qPage('saved A');
            const store = env.store(), session = env.session(), bar = env.inst.actionbar;
            const surface = Object.keys(session).sort();
            const expected = [
                'attach', 'bindLifecycle', 'changed', 'destroy', 'document', 'documentId', 'guildId',
                'guard', 'importFromV1', 'isDirty', 'key', 'listDrafts', 'load', 'meta', 'newDocumentId',
                'onState', 'pendingSave', 'resolveGuard', 'retryable', 'saveNow', 'savedDocument',
                'savedHash', 'schedule', 'start', 'state', 'storage', 'use',
            ].sort();
            assert(JSON.stringify(surface) === JSON.stringify(expected),
                'Q12: the session exposes exactly the surface it did before this step — no new state, no second dirty flag',
                JSON.stringify(surface));

            const failed = await failOnce(env, 'typed B');
            assert(failed.lastError !== null, 'Q12 rig: the cycle starts with a failure');
            store.undo();
            click(env, 'save');
            await env.settle(60);
            assert(session.state().lastError === null, 'Q12 rig: and it resolves with the one retry action');

            assert(JSON.stringify(Object.keys(session).sort()) === JSON.stringify(expected),
                'Q12: the resolution created no new session surface either');
            assert(env.el('mb2-bar-status').children.length === 3,
                'Q12: the status region is still one pill + one detail + one notice — no second live region',
                String(env.el('mb2-bar-status').children.length));
            assert(env.el('mb2-bar-actions').children.length === 5,
                'Q12: the bar still offers exactly the five actions of 5d-3b',
                String(env.el('mb2-bar-actions').children.length));
            assert(bar.stats().savePresses === 2 && bar.stats().saves === 2,
                'Q12: each press was counted once, and each one invoked the save path once',
                JSON.stringify({ presses: bar.stats().savePresses, saves: bar.stats().saves }));
            assert(env.consoleLines.error.length === 0,
                'Q12: nothing was logged as an error during the whole cycle',
                JSON.stringify(env.consoleLines.error));
            assert(env.net.calls === 0, 'Q12: and nothing touched the network');
            env.unmount();
        }

        console.log('    (Q: the resolved-failure rule and its races, end to end)');
    }

    // ─────────────────────────────────────────────────────────────
    section('R. validation: the served limits, the burst rule and the strip (step 6a)');
    // ─────────────────────────────────────────────────────────────
    // The flow this step approved, end to end and nothing wider:
    //
    //   data-limits (served) → validate(document, limits) → store.ui.issues → #mb2-strip
    //
    // What is asserted here is the page's half: WHEN the engine runs, that the
    // result goes to the store's ONE issue list, that the strip is written from
    // that list only when it changes, and that a missing or partial limits table
    // is an explicit failure rather than a quiet "nothing to check".
    {
        // ── R1: boot ──
        const env = makeEnv();
        installIdb(env);
        await env.mount();
        const strip = env.el('mb2-strip');
        assert(strip.hidden === true && strip.textContent === '' && strip.children.length === 0,
            'R1: a clean boot leaves the strip hidden, empty and childless');
        assert(env.inst.ctx.counters.validateRuns === 1,
            'R1: exactly one validation pass at boot', String(env.inst.ctx.counters.validateRuns));
        assert(env.inst.limits && env.inst.limits.embed.title_max === 256 && env.inst.limitsError === null,
            'R1: the served table was read from the page shell (data-limits)',
            JSON.stringify({ error: env.inst.limitsError }));
        assert(env.net.calls === 0, 'R1: and no network call was made to get it');
        assert(env.store().getUi().issues.length === 0,
            'R1: the store holds the empty issue list it booted with (no dispatch for "still clean")');

        // ── R2: one pass per burst, and the result is the validator's ──
        const content = env.inst.inspector.control('content');
        const type = (text) => {
            content.value = text;
            env.el('mb2-inspector-body').dispatch('input', { type: 'input', target: content });
        };
        for (let i = 0; i < 10; i++) type(rep('x', 1900 + i));      // ten keystrokes, still legal
        await env.settle(250);
        assert(env.inst.ctx.counters.validateRuns === 2,
            'R2: ten keystrokes cost ONE validation pass (boot 1 → burst 2)',
            String(env.inst.ctx.counters.validateRuns));
        assert(strip.hidden === true && strip.textContent === '',
            'R2: and a legal burst leaves the strip exactly as it was',
            JSON.stringify({ hidden: strip.hidden, text: strip.textContent }));

        // The same burst, now over the limit: the strip appears with the count,
        // the server's wording and the danger tone.
        const stripWrites = watchText(strip);
        const before = stripWrites();
        type(rep('x', env.limits.message.content_max + 1));
        await env.settle(250);
        assert(env.inst.ctx.counters.validateRuns === 3,
            'R2: the over-limit keystroke cost one more pass (still per burst, not per keystroke)',
            String(env.inst.ctx.counters.validateRuns));
        assert(strip.hidden === false && strip.textContent ===
               '1 problem — Message content is 2001 characters; Discord\'s limit is 2000.',
            'R2: the strip shows the count and the issue, with the server\'s wording',
            JSON.stringify(strip.textContent));
        assert(strip.getAttribute('class') === 'mb2-strip mb2-tone-danger',
            'R2: an error tints the strip danger (the status bar\'s own tone vocabulary)',
            strip.getAttribute('class'));
        assert(stripWrites() - before === 1,
            'R2: appearing cost exactly one text write (to the strip itself)',
            String(stripWrites() - before));
        assert(env.inst.ctx.counters.validateRuns === 3 &&
               JSON.stringify(env.store().getUi().issues) ===
               JSON.stringify(env.NERO.embed.validate.validate(env.store().getDocument(), env.limits)),
            'R2: and the store\'s list is exactly what the engine returned for that document',
            JSON.stringify(env.store().getUi().issues));

        // ── R3: updates when the issue changes, disappears when it is fixed ──
        const count = stripWrites;
        let mark = count();
        type(rep('x', env.limits.message.content_max + 2));
        await env.settle(250);
        assert(strip.textContent === '1 problem — Message content is 2002 characters; Discord\'s limit is 2000.' &&
               count() - mark === 1,
            'R3: a changed issue message rewrites the strip once',
            JSON.stringify({ text: strip.textContent, writes: count() - mark }));
        mark = count();
        type(rep('x', env.limits.message.content_max));
        await env.settle(250);
        assert(strip.hidden === true && strip.textContent === '' && strip.getAttribute('class') === 'mb2-strip' &&
               count() - mark === 1,
            'R3: fixing it hides the strip again (the text is cleared once)',
            JSON.stringify({ hidden: strip.hidden, text: strip.textContent, writes: count() - mark }));
        mark = count();
        type(rep('x', env.limits.message.content_max - 1));   // a real change, still legal
        await env.settle(250);
        assert(count() - mark === 0 && env.inst.ctx.counters.validateRuns === 6,
            'R3: an unchanged result writes nothing at all (change-guarded, but the pass still ran)',
            JSON.stringify({ writes: count() - mark, runs: env.inst.ctx.counters.validateRuns }));
        assert(env.dom.warnings.length === 0, 'R3: and nothing was ever overwritten in the strip',
            env.dom.warnings.join(' | '));

        // ── R4: the page uses the SERVED numbers, not copies ──
        const store = env.store();
        const depth = store.historyDepth().size;
        const beforeIssues = store.getUi().issues;
        type('a legal 10 characters');
        await env.settle(250);
        assert(store.getUi().issues === beforeIssues,
            'R4: a pass that changes nothing does not even dispatch (the list reference is untouched)');
        assert(store.historyDepth().size === depth,
            'R4: and ui/setIssues never enters the undo stack', String(store.historyDepth().size));
        env.unmount();
    }
    {
        // A different limits table must change the OUTCOME — the only proof that
        // the numbers are read and not baked into the page or the engine.
        const limits = servedLimits();
        limits.message.content_max = 20;
        limits.embed.title_max = 5;
        const env = makeEnv({ limits: limits });
        installIdb(env);
        await env.mount();
        const content = env.inst.inspector.control('content');
        content.value = rep('x', 21);
        env.el('mb2-inspector-body').dispatch('input', { type: 'input', target: content });
        await env.settle(250);
        assert(env.el('mb2-strip').textContent ===
               '1 problem — Message content is 21 characters; Discord\'s limit is 20.',
            'R4: a 21-character message is over a served limit of 20 (the page read the table it was given)',
            JSON.stringify(env.el('mb2-strip').textContent));
        // The page's own blank document mints its ids, so read the embed id from
        // the store rather than assuming one.
        const embedId = env.store().getDocument().embeds[0].id;
        env.inst.store.dispatch({ type: 'ui/selectNode', nodeId: embedId });
        const title = env.inst.inspector.control('title');
        assert(!!title, 'R4 rig: the embed panel exposes its title control');
        title.value = '123456';                       // 6 characters against a served title_max of 5
        env.el('mb2-inspector-body').dispatch('input', { type: 'input', target: title });
        await env.settle(250);
        assert(env.el('mb2-strip').textContent ===
               '2 problems — Message content is 21 characters; Discord\'s limit is 20. (1 more)',
            'R4: two issues are summarised as a count plus the first one, in the engine\'s order',
            JSON.stringify(env.el('mb2-strip').textContent));
        env.unmount();
    }
    {
        // ── R5: no table, or an incomplete one, is an explicit failure ──
        const cases = [
            ['the attribute is missing entirely', null],
            ['the attribute is not JSON', '{not json'],
            ['the attribute is not an object', '[1,2,3]'],
        ];
        for (const [why, value] of cases) {
            const env = makeEnv({ limits: value });
            installIdb(env);
            await env.mount();
            const strip = env.el('mb2-strip');
            assert(env.inst.limits === null && typeof env.inst.limitsError === 'string',
                'R5 (' + why + '): the page records that it has no limits table',
                JSON.stringify({ limits: env.inst.limits, error: env.inst.limitsError }));
            assert(strip.hidden === false && /limits did not reach this page/.test(strip.textContent) &&
                   strip.getAttribute('class') === 'mb2-strip mb2-tone-danger',
                'R5 (' + why + '): and the strip says so instead of implying the message is fine',
                JSON.stringify(strip.textContent));
            assert(env.store().getUi().issues.length === 1 &&
                   env.store().getUi().issues[0].code === 'limits.missing',
                'R5 (' + why + '): with the one explicit issue in the store\'s list',
                JSON.stringify(env.store().getUi().issues));
            assert(env.consoleLines.error.length === 0,
                'R5 (' + why + '): nothing was thrown or logged', env.consoleLines.error.join(' | '));
            env.unmount();
        }
        // A table that is present but missing ONE key is just as unusable: that
        // key's rule would silently stop existing.
        const partial = servedLimits();
        delete partial.embed.fields_max;
        const env = makeEnv({ limits: partial });
        installIdb(env);
        await env.mount();
        const partialIssues = env.store().getUi().issues;
        assert(env.inst.limits && env.inst.limits.embed.fields_max === undefined &&
               partialIssues.length === 1 && partialIssues[0].code === 'limits.missing' &&
               /limits did not reach this page/.test(env.el('mb2-strip').textContent),
            'R5 (one key missing): the engine refuses the table rather than skipping that rule',
            JSON.stringify(env.store().getUi().issues));
        env.unmount();
    }
    {
        // ── R6: a load validates immediately (a broken draft must not look clean) ──
        const env = makeEnv();
        const document_ = filledDocument();
        document_.embeds[0].title = rep('t', 300);          // over the served 256
        installIdb(env, seedSpec(env, [makeRecord(env, document_, { documentId: 'doc-filled' })]));
        await env.mount();
        assert(env.inst.ctx.counters.validateRuns === 1 && env.inst.validateTimer === null,
            'R6: a restored draft is validated once, immediately (nothing left scheduled)',
            [env.inst.ctx.counters.validateRuns, 'runs, timer', timerNote(env.inst.validateTimer)].join(' '));
        assert(env.el('mb2-strip').hidden === false &&
               env.el('mb2-strip').textContent ===
               '1 problem — Embed 1 title is 300 characters; Discord\'s limit is 256.',
            'R6: so the strip already describes the loaded draft right after mount',
            JSON.stringify(env.el('mb2-strip').textContent));
        env.unmount();
    }
    {
        // ── R7: teardown cancels a scheduled pass ──
        const env = makeEnv();
        installIdb(env);
        await env.mount();
        const content = env.inst.inspector.control('content');
        content.value = rep('x', 2100);
        env.el('mb2-inspector-body').dispatch('input', { type: 'input', target: content });
        assert(env.inst.validateTimer !== null, 'R7 rig: the burst scheduled a pass');
        const runs = env.inst.ctx.counters.validateRuns;
        env.unmount();
        assert(env.inst.validateTimer === null, 'R7: teardown cancels the pending pass instead of leaving it armed');
        await env.settle(300);
        assert(env.inst.ctx.counters.validateRuns === runs,
            'R7: and nothing validated after the page was gone',
            JSON.stringify({ before: runs, after: env.inst.ctx.counters.validateRuns }));
        assert(env.consoleLines.error.length === 0,
            'R7: with no error from a callback that fired too late', env.consoleLines.error.join(' | '));
    }
    {
        // ── R8: mounted by hand (no registry ctx), the page still validates ──
        // The module takes its timer from the registry when there is one; this
        // is the other branch, and it is the branch a "just call init(root)"
        // integration would hit. It must behave identically — including reading
        // the served limits from the shell it was handed.
        const env = makeEnv();
        installIdb(env);
        const root = materialize(findById(TEMPLATE_TREE, 'mb2-root'), env.dom.document);
        root.setAttribute('data-guild-id', GUILD);
        root.setAttribute('data-limits', JSON.stringify(servedLimits()));
        env.dom.attach(root);
        const inst = env.NERO.embed.messageBuilderPage.init(root, null);
        await sleep(60);
        assert(inst && inst.limits && inst.limits.embed.title_max === 256 && inst.ctx === null,
            'R8: a hand-mounted page reads the served limits the same way, with no registry behind it',
            JSON.stringify({ limits: !!inst.limits, ctx: inst.ctx }));
        const content = inst.inspector.control('content');
        content.value = rep('x', 2001);
        root.querySelector('#mb2-inspector-body').dispatch('input', { type: 'input', target: content });
        assert(inst.validateTimer !== null, 'R8 rig: the burst armed a pass on the realm\'s own timer');
        await sleep(250);
        const strip = root.querySelector('#mb2-strip');
        assert(strip.hidden === false && /2001 characters/.test(strip.textContent),
            'R8: and its own burst still validates and paints the strip',
            JSON.stringify({ hidden: strip.hidden, text: strip.textContent }));
        assert(inst.validateTimer === null, 'R8: the hand-mounted timer was consumed, not left armed');
        assert(env.NERO.embed.messageBuilderPage.destroy() === true,
            'R8: tearing the hand-mounted page down returns true');
        await sleep(250);
        assert(env.consoleLines.error.length === 0,
            'R8: with nothing logged as an error', env.consoleLines.error.join(' | '));
    }
    {
        // ── R9: hygiene — one region, one writer, one list ──
        const env = makeEnv();
        installIdb(env);
        await env.mount();
        const inst = env.inst;
        const content = inst.inspector.control('content');
        for (let i = 0; i < 5; i++) {
            content.value = rep('y', 2001 + i);
            env.el('mb2-inspector-body').dispatch('input', { type: 'input', target: content });
        }
        await env.settle(250);
        // Walk the whole mounted shell: the page may not introduce a live region
        // of its own, so the only two that exist are the ones the template
        // declares (the strip and the bar status).
        const live = [];
        (function walk(node) {
            if (node.getAttribute && node.getAttribute('aria-live')) live.push(node.getAttribute('id'));
            (node.children || []).forEach(walk);
        })(env.root);
        assert(live.length === 2 && live.indexOf('mb2-strip') !== -1 && live.indexOf('mb2-bar-status') !== -1,
            'R9: the page has exactly the two live regions the template declares (no third)',
            live.join(','));
        assert(env.el('mb2-strip').children.length === 0,
            'R9: the strip is text only — the page never builds markup inside it',
            String(env.el('mb2-strip').children.length));
        assert(env.el('mb2-bar-status').children.length === 3,
            'R9: and the status region is untouched by validation (one pill + detail + notice)',
            String(env.el('mb2-bar-status').children.length));
        assert(env.el('mb2-bar-actions').children.length === 5,
            'R9: the action bar still offers the five approved actions (no Validate button)',
            String(env.el('mb2-bar-actions').children.length));
        assert(Object.prototype.hasOwnProperty.call(inst, 'issues') === false,
            'R9: the page keeps NO issue list of its own — the store\'s ui.issues is the one');
        const pageSrc = fs.readFileSync(PAGE_PATH, 'utf8');
        assert((pageSrc.match(/type: 'ui\/setIssues'/g) || []).length === 1,
            'R9: exactly one dispatch site for issues in the whole page module',
            String((pageSrc.match(/type: 'ui\/setIssues'/g) || []).length));
        assert(env.net.calls === 0, 'R9: and validation never touches the network');
        assert(env.inst.ctx.counters.validateRuns === 2,
            'R9: five keystrokes, one pass', String(env.inst.ctx.counters.validateRuns));
        // 6b: the views were handed the page's ONE limits object, and neither of
        // them parses the attribute or keeps a table of its own.
        const railSrc = fs.readFileSync(process.env.NERO_RAIL_SRC || js('embed', 'views', 'rail.js'), 'utf8');
        const inspSrc = fs.readFileSync(process.env.NERO_INSPECTOR_SRC || js('embed', 'views', 'inspector.js'), 'utf8');
        const codeOnly = (src) => src
            .replace(/\/\*[\s\S]*?\*\//g, ' ')
            .replace(/(^|[^:])\/\/[^\n]*/g, '$1 ');
        [['rail', railSrc], ['inspector', inspSrc]].forEach(([name, src]) => {
            assert(!/JSON\.parse|data-limits/.test(codeOnly(src)),
                'R9: the ' + name + ' never parses the limits table (the page is the only parser)');
            assert(!/ui\/setIssues/.test(src), 'R9: and the ' + name + ' never dispatches an issue list');
            assert(!/aria-live/.test(src), 'R9: and never creates a live region');
        });
        assert((pageSrc.match(/limits: inst\.limits/g) || []).length === 2,
            'R9: the page hands the same limits object to both views (by reference, twice)',
            String((pageSrc.match(/limits: inst\.limits/g) || []).length));
        assert((pageSrc.match(/getAttribute\('data-limits'\)/g) || []).length === 1,
            'R9: and reads the attribute exactly once, at mount',
            String((pageSrc.match(/getAttribute\('data-limits'\)/g) || []).length));
        env.unmount();
    }

    // ─────────────────────────────────────────────────────────────
    section('S. the 6b facts on the page: counters, caps and badges');
    // ─────────────────────────────────────────────────────────────
    const railRow = (env, id) => env.el('mb2-rail-body').children
        .find(row => row.getAttribute('data-node-id') === id) || null;
    const railChild = (row, cls) => (row ? row.children : [])
        .find(child => (child.className || '').split(/\s+/).indexOf(cls) !== -1) || null;
    const railButton = (row, action) => {
        let found = null;
        (row ? row.children : []).forEach(child => {
            if ((child.className || '').indexOf('mb2-rail-actions') === -1) return;
            child.children.forEach(button => {
                if (button.getAttribute('data-rail-action') === action) found = button;
            });
        });
        return found;
    };
    const badgeOf = (env, id) => {
        const badge = railChild(railRow(env, id), 'mb2-rail-badge');
        return badge && badge.hidden === false ? badge : null;
    };
    const issuesFor = (env, id) => env.store().getUi().issues.filter(i => i.nodeId === id);

    {
        // ── S1: boot — a loaded draft shows its facts immediately ──
        const env = makeEnv();
        installIdb(env, seedSpec(env, [makeRecord(env, filledDocument(), { documentId: 'doc-filled' })]));
        await env.mount();
        const doc = env.store().getDocument();
        const embedId = doc.embeds[0].id;
        assert(env.inst.inspector.count('content').textContent ===
               doc.content.length + ' / ' + env.limits.message.content_max,
            'the content counter shows the loaded draft against the served content_max',
            env.inst.inspector.count('content').textContent);
        assert(env.inst.inspector.count('content').getAttribute('aria-hidden') === 'true' &&
               env.inst.inspector.count('content').getAttribute('aria-live') === null,
            'and it is decoration (aria-hidden), never a live region');
        assert(railRow(env, 'content').textContent.indexOf('1 / ' + env.limits.message.embeds_max) !== -1,
            'the rail shows the embeds fact from the same served table',
            railRow(env, 'content').textContent);
        assert(railButton(railRow(env, 'content'), 'addEmbed').disabled === false,
            'and the add-embed control is live below the cap');
        assert(railButton(railRow(env, embedId), 'addField').disabled === false,
            'so is the embed add-field control (2 of 25 fields)');
        assert(badgeOf(env, 'content') === null && badgeOf(env, embedId) === null,
            'a valid loaded draft carries no badge on any row');

        // ── S2: over the limit — the strip and the badge say the same thing ──
        const content = env.inst.inspector.control('content');
        content.value = rep('x', env.limits.message.content_max + 1);
        env.el('mb2-inspector-body').dispatch('input', { type: 'input', target: content });
        assert(await env.until(() => env.el('mb2-strip').hidden === false),
            'rig: the debounced pass reported the problem');
        assert(env.inst.inspector.count('content').textContent ===
               (env.limits.message.content_max + 1) + ' / ' + env.limits.message.content_max,
            'the counter follows the keystroke', env.inst.inspector.count('content').textContent);
        assert(env.inst.inspector.count('content').className.indexOf('mb2-count-over') !== -1,
            'and goes into its over-state', env.inst.inspector.count('content').className);
        assert(env.el('mb2-strip').hidden === false &&
               /2001 characters/.test(env.el('mb2-strip').textContent),
            'the strip reports the same problem', env.el('mb2-strip').textContent);
        const badge = badgeOf(env, 'content');
        assert(!!badge && badge.getAttribute('data-badge-tone') === 'error' &&
               /1 problem/.test(badge.textContent),
            'and the rail row carries an error badge for that node',
            badge ? badge.textContent : '(no badge)');
        assert(issuesFor(env, 'content').length === 1 && /1 problem/.test(badge.textContent),
            'ONE list, two surfaces: the badge counts exactly what ui.issues holds',
            String(issuesFor(env, 'content').length));
        assert(badgeOf(env, embedId) === null, 'and no other row got a badge for it');

        // fixing it clears both surfaces in the same pass
        content.value = rep('x', env.limits.message.content_max);
        env.el('mb2-inspector-body').dispatch('input', { type: 'input', target: content });
        assert(await env.until(() => env.el('mb2-strip').hidden === true),
            'rig: the pass ran again and found nothing');
        assert(badgeOf(env, 'content') === null,
            'fixing it hides the strip AND the badge');
        assert(env.inst.inspector.count('content').className === 'mb2-count',
            'and the counter leaves its over-state', env.inst.inspector.count('content').className);

        // ── S3: the by-reference table — no second copy anywhere ──
        // The page parses data-limits ONCE and hands that object to both views.
        // If a view had copied it, or cached a number, changing the page's own
        // object would not move its counter or its cap. It does.
        const limits = env.inst.limits;
        const keep = { embeds: limits.message.embeds_max, title: limits.embed.title_max };
        try {
            limits.message.embeds_max = 1;             // the loaded draft already has 1
            limits.embed.title_max = 7;                // and its title is 7 characters
            env.inst.rail.render();
            env.store().dispatch({ type: 'ui/selectNode', nodeId: embedId });
            env.inst.inspector.render();
            assert(railButton(railRow(env, 'content'), 'addEmbed').disabled === true,
                'a change to the page OWN limits object moves the rail cap (passed by reference)');
            assert(railRow(env, 'content').textContent.indexOf('1 / 1') !== -1,
                'and the embeds fact with it', railRow(env, 'content').textContent);
            assert(env.inst.inspector.count('title').textContent === '7 / 7',
                'the inspector counter reads the same object',
                env.inst.inspector.count('title').textContent);
            limits.embed.title_max = 3;
            env.inst.inspector.render();
            assert(env.inst.inspector.count('title').textContent === '7 / 3' &&
                   env.inst.inspector.count('title').className.indexOf('mb2-count-over') !== -1,
                'and an over-limit title follows the same object too',
                env.inst.inspector.count('title').textContent + ' '
                    + env.inst.inspector.count('title').className);
        } finally {
            limits.message.embeds_max = keep.embeds;
            limits.embed.title_max = keep.title;
            env.inst.rail.render();
            env.store().dispatch({ type: 'ui/selectNode', nodeId: 'content' });
            env.inst.inspector.render();
        }
        assert(railButton(railRow(env, 'content'), 'addEmbed').disabled === false,
            'restoring the numbers restores the control (it is a fact, not a mode)');
        env.unmount();
    }
    {
        // ── S4: a custom served table drives counters, caps and badges ──
        const limits = servedLimits();
        limits.message.content_max = 20;
        limits.embed.title_max = 5;
        limits.embed.fields_max = 1;
        limits.message.embeds_max = 2;
        const env = makeEnv({ limits: limits });
        installIdb(env);
        await env.mount();
        const embedId = env.store().getDocument().embeds[0].id;
        const content = env.inst.inspector.control('content');
        assert(env.inst.inspector.count('content').textContent === '0 / 20',
            'the counter reads the served content_max of 20',
            env.inst.inspector.count('content').textContent);
        assert(railRow(env, 'content').textContent.indexOf('1 / 2') !== -1,
            'the embeds cap is the served 2', railRow(env, 'content').textContent);
        content.value = rep('c', 21);
        env.el('mb2-inspector-body').dispatch('input', { type: 'input', target: content });
        assert(await env.until(() => badgeOf(env, 'content') !== null),
            'rig: the over-limit message was reported');
        assert(/is 21 characters/.test(env.el('mb2-strip').textContent) &&
               badgeOf(env, 'content') !== null,
            'a 21-character message is over the served 20 (strip + badge)',
            env.el('mb2-strip').textContent);
        env.store().dispatch({ type: 'ui/selectNode', nodeId: embedId });
        const title = env.inst.inspector.control('title');
        assert(env.inst.inspector.count('title').textContent === '0 / 5',
            'the title counter reads the served 5', env.inst.inspector.count('title').textContent);
        title.value = 'sixsix';
        env.el('mb2-inspector-body').dispatch('input', { type: 'input', target: title });
        assert(await env.until(() => badgeOf(env, embedId) !== null),
            'rig: the over-limit title was reported');
        assert(issuesFor(env, embedId).length >= 1,
            'a 6-character title badges the EMBED row',
            badgeOf(env, embedId) ? badgeOf(env, embedId).textContent : '(no badge)');
        assert(env.el('mb2-strip').textContent.indexOf('problem') !== -1 &&
               env.store().getUi().issues.length >= 2,
            'the store holds every issue while the strip shows the first',
            String(env.store().getUi().issues.length));
        // the fields cap, end to end: one field is the whole allowance
        assert(env.inst.inspector.count('fields').textContent === '0 / 1',
            'the fields fact is the served 1', env.inst.inspector.count('fields').textContent);
        const addField = railButton(railRow(env, embedId), 'addField');
        assert(addField.disabled === false, 'rig: the rail add-field control starts live');
        env.el('mb2-rail-body').dispatch('click', { type: 'click', target: addField });
        assert(env.store().getDocument().embeds[0].fields.length === 1,
            'clicking it adds the one allowed field',
            String(env.store().getDocument().embeds[0].fields.length));
        // The rail selected the new field (5b), so the inspector is showing that
        // panel now — read the embed's facts back by selecting it again.
        env.store().dispatch({ type: 'ui/selectNode', nodeId: embedId });
        assert(env.inst.inspector.count('fields').textContent === '1 / 1',
            'and the fact reads 1 / 1', env.inst.inspector.count('fields').textContent);
        assert(railButton(railRow(env, embedId), 'addField').disabled === true &&
               env.inst.inspector.count('fields').className.indexOf('mb2-count') === 0,
            'the control is disabled at the cap (and a full embed is not an ERROR: no over-state)',
            String(railButton(railRow(env, embedId), 'addField').disabled));
        env.el('mb2-rail-body').dispatch('click', { type: 'click', target: railButton(railRow(env, embedId), 'addField') });
        assert(env.store().getDocument().embeds[0].fields.length === 1,
            'and clicking the disabled control adds nothing',
            String(env.store().getDocument().embeds[0].fields.length));
        assert(env.net.calls === 0, 'none of this touched the network');
        env.unmount();
    }
    {
        // ── S5: an unusable table fails closed on every 6b surface ──
        const env = makeEnv({ limits: 'not json at all' });
        installIdb(env);
        await env.mount();
        const embedId = env.store().getDocument().embeds[0].id;
        assert(env.inst.inspector.count('content').textContent === '',
            'no limits table, no counter text (never a guessed number)',
            JSON.stringify(env.inst.inspector.count('content').textContent));
        assert(railButton(railRow(env, 'content'), 'addEmbed').disabled === true &&
               railButton(railRow(env, embedId), 'addField').disabled === true,
            'and every add control is disabled (fail closed, never unlimited)');
        assert(railRow(env, 'content').textContent.indexOf(' / ') === -1,
            'with no embeds fact invented', railRow(env, 'content').textContent);
        const badge = badgeOf(env, 'content');
        assert(!!badge && badge.getAttribute('data-badge-tone') === 'error',
            'the one explicit limits issue is badged on the message root',
            badge ? badge.textContent : '(no badge)');
        assert(env.el('mb2-strip').hidden === false &&
               /limits did not reach this page/.test(env.el('mb2-strip').textContent),
            'and the strip explains it (6a) — the badge is the same issue, not a second one',
            env.el('mb2-strip').textContent);
        assert(env.consoleLines.error.length === 0, 'nothing was thrown or logged as an error',
            env.consoleLines.error.join(' | '));
        env.unmount();
    }
    {
        // ── S6: teardown with facts and a badge on screen ──
        const env = makeEnv();
        installIdb(env, seedSpec(env, [makeRecord(env, filledDocument(), { documentId: 'doc-filled' })]));
        await env.mount();
        const content = env.inst.inspector.control('content');
        content.value = rep('x', env.limits.message.content_max + 1);
        env.el('mb2-inspector-body').dispatch('input', { type: 'input', target: content });
        assert(await env.until(() => badgeOf(env, 'content') !== null),
            'rig: a badge is showing before teardown');
        const stripWrites = watchText(env.el('mb2-strip'));
        const runs = env.inst.ctx.counters.validateRuns;
        env.unmount();
        assert(env.el('mb2-rail-body').children.length === 0, 'the rail (and its badges) is off the tree');
        assert(env.el('mb2-inspector-body').children.length === 0, 'the inspector (and its counters) too');
        // The fragment (strip included) leaves with the page in a real navigation;
        // what this proves is that nothing writes to it after teardown, and that
        // no late validation pass repaints a region that is already gone.
        await env.settle(300);   // more than one validation interval, on purpose
        assert(stripWrites() === 0, 'the strip is not written after teardown',
            String(stripWrites()));
        assert(env.inst.ctx.counters.validateRuns === runs,
            'and no validation pass runs after it', String(env.inst.ctx.counters.validateRuns));
        assert(env.consoleLines.error.length === 0, 'teardown logged no error',
            env.consoleLines.error.join(' | '));
    }

    console.log('\nmessage-builder page: ' + pass + ' passed, ' + fail + ' failed');
    if (fail) {
        console.log('Failures:');
        failures.forEach(f => console.log(' -', f));
        process.exit(1);
    }
    console.log('ALL MESSAGE-BUILDER PAGE CHECKS PASSED');
}

main().catch(err => {
    console.error('\nHARNESS ERROR (not an assertion failure):', err && err.stack || err);
    process.exit(1);
});
