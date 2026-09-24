#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════
   scripts/support/mb_mutants.js — the Message Builder v2 mutation battery.

   WHY THIS EXISTS
   A harness that passes is only evidence if it can also FAIL. Step 4 shipped a
   349-check suite that did not catch a real data-loss bug, and the lesson was
   written into the process: every step's harness is proven by breaking the
   module it tests, one behaviour at a time, and confirming the harness notices.

   HOW IT WORKS
   Each mutant is a text substitution on the REAL source file, written to a
   temporary copy OUTSIDE the repository. The harness is then run against that
   copy through its source hook (NERO_MB_PAGE_SRC), and the mutant counts as
   caught only when the harness exits non-zero.

     node scripts/support/mb_mutants.js            # all mutants
     node scripts/support/mb_mutants.js M3 M7      # a subset

   The pristine file is hashed before and after the run: the battery must never
   leave the working tree changed.
   ═══════════════════════════════════════════════════════════════ */
'use strict';
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');
const { spawnSync } = require('child_process');

const ROOT = path.join(__dirname, '..', '..');
const TARGETS = {
    page: {
        file: path.join(ROOT, 'dashboard', 'static', 'js', 'embed', 'message-builder-page.js'),
        env: 'NERO_MB_PAGE_SRC',
        label: 'dashboard/static/js/embed/message-builder-page.js',
    },
    drafts: {
        file: path.join(ROOT, 'dashboard', 'static', 'js', 'embed', 'drafts.js'),
        env: 'NERO_DRAFTS_SRC',
        label: 'dashboard/static/js/embed/drafts.js',
    },
    store: {
        file: path.join(ROOT, 'dashboard', 'static', 'js', 'embed', 'store.js'),
        env: 'NERO_STORE_SRC',
        label: 'dashboard/static/js/embed/store.js',
    },
};
const HARNESS = path.join(ROOT, 'scripts', 'test_message_builder_page.js');

/**
 * A mutant is { id, target, why, edits: [[find, replace], …] }.
 * `target` names the file under test (default: the page module); the harness is
 * pointed at the mutated copy through that target's env hook.
 * A replacement that does not apply is a FAILED mutant (the source moved), not
 * a silent pass — that is how a battery rots.
 */
const MUTANTS = [
    {
        id: 'M1',
        why: 'loading a draft is pushed onto the undo stack',
        edits: [["meta: { history: false }", "meta: { history: true }"]],
    },
    {
        id: 'M2',
        why: 'a loaded draft is left looking dirty (the store is never told)',
        edits: [["inst.store.markSaved(inst.store.getDocument());", "/* markSaved removed */"]],
    },
    {
        id: 'M3',
        why: 'a preserved record is not protected: the guard is cleared before editing it',
        edits: [[
            "        // do NOT call session.start() here: it clears the write guard, and this\n        // path exists precisely so the preserved record cannot be overwritten.\n        setCanonical(inst, NERO.embed.model.blankMessageDocument());",
            "        inst.session.start(NERO.embed.model.blankMessageDocument(), { guildId: inst.guildId });\n        setCanonical(inst, inst.session.document());",
        ]],
    },
    {
        id: 'M4',
        why: 'the last-draft pointer is rewritten on every save instead of once',
        edits: [["if (inst.pointerWritten || inst.destroyed || !inst.session) return;",
                 "if (inst.destroyed || !inst.session) return;"]],
    },
    {
        id: 'M5',
        why: 'the preview is not subscribed to the store (it only ever paints once)',
        edits: [["function (s) { return s.document; }", "function (s) { return s.ui; }"]],
    },
    {
        id: 'M6',
        why: 'the store is created without its reducer table (every dispatch is a no-op)',
        edits: [[
            "            now: Date.now,\n            reducers: f.store.createReducers(f.model),\n        });",
            "            now: Date.now,\n        });",
        ]],
    },
    {
        id: 'M7',
        why: 'the preview clock is read on every render instead of fixed for the page',
        edits: [["now: function () { return inst.startedAt; },", "now: Date.now,"]],
    },
    // ── the persistence boundary (drafts.js) — the in-flight save race ──
    {
        id: 'M9',
        target: 'drafts',
        why: 'an in-flight save confirms the CURRENT document instead of the written snapshot',
        edits: [[
            "                    if (boundStore && boundStore.markSavedHash) {\n                        boundStore.markSavedHash(record.documentHash);\n                    }",
            "                    if (boundStore && boundStore.markSaved) boundStore.markSaved(currentDocument);",
        ]],
    },
    {
        id: 'M10',
        target: 'drafts',
        why: 'a skipped ("clean") save does not notify, so the bar can stay stale',
        edits: [[
            "                notify();\n                return Promise.resolve({ ok: true, skipped: true, reason: 'clean', key: key() });",
            "                return Promise.resolve({ ok: true, skipped: true, reason: 'clean', key: key() });",
        ]],
    },
    {
        id: 'M11',
        target: 'drafts',
        why: 'an edit during an in-flight write is not re-queued (the clearing is dropped)',
        edits: [[
            "                if (inFlight) pendingAfterFlight = true;\n                cancelScheduled();\n                return false;",
            "                cancelScheduled();\n                return false;",
        ]],
    },
    // ── the store ──
    {
        id: 'M12',
        target: 'store',
        why: 'markSavedHash never records anything (the store never learns what was written)',
        edits: [[
            "            const next = String(hash);\n            if (savedHash === next) return false;\n            const wasDirty = isDirty();\n            savedHash = next;",
            "            const next = String(hash);\n            if (savedHash === next) return false;\n            const wasDirty = isDirty();",
        ]],
    },
    {
        id: 'M13',
        target: 'store',
        why: 'markSavedHash replaces the document (a copy of the current one) instead of leaving it alone',
        edits: [[
            "            const wasDirty = isDirty();\n            savedHash = next;\n            if (isDirty() !== wasDirty) notify({ type: '@save/markHash' });\n            return true;",
            "            const wasDirty = isDirty();\n            savedHash = next;\n            state = Object.assign({}, state, { document: model.cloneDocument(state.document) });\n            if (isDirty() !== wasDirty) notify({ type: '@save/markHash' });\n            return true;",
        ]],
    },
    {
        id: 'M14',
        target: 'store',
        why: 'markSavedHash claims the CURRENT document is what was written (clears dirty regardless of the hash)',
        edits: [[
            "            const wasDirty = isDirty();\n            savedHash = next;",
            "            const wasDirty = isDirty();\n            savedHash = model.hashDocument(state.document);",
        ]],
    },
    {
        id: 'M8',
        why: 'the session attaches AFTER the load, so the loaded draft looks like an edit',
        edits: [[
            "        inst.session.attach(inst.store);\n        if (win && win.addEventListener) inst.session.bindLifecycle(win);",
            "        if (win && win.addEventListener) inst.session.bindLifecycle(win);",
        ], [
            "        inst.store.markSaved(inst.store.getDocument());\n        if (inst.preview) inst.preview.updateDocument(inst.store.getDocument());\n        else mountPreview(inst);",
            "        inst.store.markSaved(inst.store.getDocument());\n        inst.session.attach(inst.store);\n        if (inst.preview) inst.preview.updateDocument(inst.store.getDocument());\n        else mountPreview(inst);",
        ]],
    },
];

function sha1(file) {
    return crypto.createHash('sha1').update(fs.readFileSync(file)).digest('hex');
}

function main() {
    const only = process.argv.slice(2);
    const selected = only.length ? MUTANTS.filter(m => only.indexOf(m.id) !== -1) : MUTANTS;
    if (!selected.length) {
        console.error('no such mutant: ' + only.join(', '));
        process.exit(2);
    }

    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'mb-mutants-'));
    const before = {};
    const original = {};
    Object.keys(TARGETS).forEach(key => {
        before[key] = sha1(TARGETS[key].file);
        original[key] = fs.readFileSync(TARGETS[key].file, 'utf8');
    });
    let caught = 0;
    const missed = [];

    console.log('message-builder mutation battery — ' + selected.length + ' mutants');
    Object.keys(TARGETS).forEach(key => {
        console.log('  ' + key.padEnd(7) + TARGETS[key].label + ' (' + before[key].slice(0, 12) + ')');
    });
    console.log('battery: scripts/test_message_builder_page.js\n');

    selected.forEach(mutant => {
        const targetKey = mutant.target || 'page';
        const target = TARGETS[targetKey];
        if (!target) { missed.push(mutant.id + ' (unknown target)'); return; }
        let source = original[targetKey];
        const applied = [];
        for (const [find, replace] of mutant.edits) {
            if (source.indexOf(find) === -1) {
                applied.push(false);
                continue;
            }
            source = source.replace(find, replace);
            applied.push(true);
        }
        if (applied.some(ok => !ok)) {
            console.log('  ' + mutant.id + ' NOT APPLIED — the source moved; fix the battery, not the module');
            console.log('      ' + mutant.why);
            missed.push(mutant.id + ' (not applied)');
            return;
        }

        const file = path.join(tmpDir, mutant.id + '.js');
        fs.writeFileSync(file, source);
        const envPatch = { NERO_MB_PAGE_SRC: process.env.NERO_MB_PAGE_SRC, NERO_DRAFTS_SRC: process.env.NERO_DRAFTS_SRC };
        envPatch[target.env] = file;
        const run = spawnSync(process.execPath, [HARNESS], {
            env: Object.assign({}, process.env, envPatch),
            encoding: 'utf8',
            timeout: 120000,
        });
        const failed = run.status !== 0;
        if (failed) caught++;
        else missed.push(mutant.id);
        const firstFailure = (run.stdout || '').split('\n').filter(l => l.indexOf('  FAIL') === 0)[0] || '';
        console.log('  ' + (failed ? 'CAUGHT  ' : 'MISSED  ') + mutant.id + '  [' + targetKey + '] ' + mutant.why);
        if (failed) {
            const count = ((run.stdout || '').match(/  FAIL /g) || []).length;
            console.log('      ' + count + ' failing check(s)' + (firstFailure ? ' — e.g.' + firstFailure.replace('  FAIL ', ' ') : ''));
            if (run.stdout && run.stdout.indexOf('HARNESS ERROR') !== -1) {
                console.log('      (detected as a harness error rather than a named check)');
            }
        }
    });

    const after = {};
    let unchanged = true;
    Object.keys(TARGETS).forEach(key => {
        after[key] = sha1(TARGETS[key].file);
        if (after[key] !== before[key]) unchanged = false;
    });
    console.log('\ncaught ' + caught + '/' + selected.length + ' mutants' +
        (missed.length ? ' — MISSED: ' + missed.join(', ') : ''));
    console.log('sources unchanged by the run: ' + (unchanged ? 'yes' : 'NO — working tree changed!'));
    try { fs.rmSync(tmpDir, { recursive: true, force: true }); } catch (e) { /* best effort */ }
    if (missed.length || !unchanged) process.exit(1);
    console.log('MUTATION BATTERY: EVERY MUTANT CAUGHT');
}

main();
