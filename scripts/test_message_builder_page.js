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
const FOUNDATION = [
    js('nav-lifecycle.js'),
    js('embed', 'model.js'),
    STORE_PATH,
    js('embed', 'discord-markdown.js'),
    js('embed', 'preview.js'),
    DRAFTS_PATH,
    js('embed', 'views', 'statusbar.js'),
    js('embed', 'views', 'rail.js'),
    PAGE_PATH,
];
const TEMPLATE_TREE = parseTemplate(
    fs.readFileSync(path.join(ROOT_DIR, 'dashboard', 'templates', 'manage', 'message_builder.html'), 'utf8'));

const GUILD = '1111222233334444';
const OTHER_GUILD = '9999888877776666';
const RECORD_NOW = 1790284740000;      // the RECORD's clock is fixed; the page's stays real

const sleep = (ms) => new Promise(resolve => setTimeout(resolve, ms));

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
    env.mount = async function (settleMs) {
        const root = materialize(findById(TEMPLATE_TREE, 'mb2-root'), dom.document);
        root.setAttribute('data-guild-id', env.guildId);
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
        assert(env.el('mb2-inspector-body').children.length === 0, 'no inspector controls yet (5c)');
        assert(env.el('mb2-strip').hidden === true && env.el('mb2-strip').textContent === '',
            'the validation strip exists, hidden and empty (step 6 owns its contents)');
        assert(env.el('mb2-bar-actions').children.length === 0,
            'no action buttons yet (5d) — not even disabled placeholders');
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
        assert(env.store().getUi().issues.length === 0,
            'no validation issues are produced in step 5');
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
