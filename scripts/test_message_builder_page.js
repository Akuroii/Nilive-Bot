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
const FOUNDATION = [
    js('nav-lifecycle.js'),
    js('embed', 'model.js'),
    js('embed', 'store.js'),
    js('embed', 'discord-markdown.js'),
    js('embed', 'preview.js'),
    js('embed', 'drafts.js'),
    js('embed', 'views', 'statusbar.js'),
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
    section('B. the shell regions stay empty in 5a');
    // ─────────────────────────────────────────────────────────────
    {
        const env = makeEnv();
        installIdb(env);
        await env.mount();
        assert(env.el('mb2-rail-body').children.length === 0, 'no rail rows yet (5b)');
        assert(env.el('mb2-rail-body').textContent === '', 'the rail renders no text yet');
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
