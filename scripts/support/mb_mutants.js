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
    rail: {
        file: path.join(ROOT, 'dashboard', 'static', 'js', 'embed', 'views', 'rail.js'),
        env: 'NERO_RAIL_SRC',
        label: 'dashboard/static/js/embed/views/rail.js',
        harness: path.join(ROOT, 'scripts', 'test_message_builder_rail.js'),
    },
    inspector: {
        file: path.join(ROOT, 'dashboard', 'static', 'js', 'embed', 'views', 'inspector.js'),
        env: 'NERO_INSPECTOR_SRC',
        label: 'dashboard/static/js/embed/views/inspector.js',
        harness: path.join(ROOT, 'scripts', 'test_message_builder_inspector.js'),
    },
    actionbar: {
        file: path.join(ROOT, 'dashboard', 'static', 'js', 'embed', 'views', 'actionbar.js'),
        env: 'NERO_ACTIONBAR_SRC',
        label: 'dashboard/static/js/embed/views/actionbar.js',
        harness: path.join(ROOT, 'scripts', 'test_message_builder_actionbar.js'),
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
        edits: [[
            "        const preview = NERO.embed.preview.create(inst.els.mount, {\n            // One clock per page life: the header time cannot drift while the\n            // page is open, and the same document renders the same bytes.\n            now: function () { return inst.startedAt; },",
            "        const preview = NERO.embed.preview.create(inst.els.mount, {\n            // One clock per page life: the header time cannot drift while the\n            // page is open, and the same document renders the same bytes.\n            now: Date.now,",
        ]],
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
    // ── the structure rail ──
    {
        id: 'R1',
        target: 'rail',
        why: 'removing a field mutates the document in place instead of dispatching',
        edits: [[
            "            if (type === 'embed') store.dispatch({ type: 'embed/remove', embedId: row.embedId });\n            else store.dispatch({ type: 'field/remove', embedId: row.embedId, fieldId: row.fieldId });",
            "            if (type === 'embed') {\n                const doc = store.getDocument();\n                doc.embeds = doc.embeds.filter(function (e) { return e.id !== row.embedId; });\n            } else {\n                const doc = store.getDocument();\n                doc.embeds.forEach(function (e) {\n                    if (e.id === row.embedId) e.fields = e.fields.filter(function (f) { return f.id !== row.fieldId; });\n                });\n            }",
        ]],
    },
    {
        id: 'R2',
        target: 'rail',
        why: 'adding an embed writes into the document instead of dispatching',
        edits: [[
            "            const before = idsIn(store.getState().document);\n            store.dispatch(action);",
            "            const before = idsIn(store.getState().document);\n            store.getDocument().embeds.push({ id: 'emb_direct_' + Math.random(), title: '', url: '', description: '', color: 0x7c5cbf, author: { name: '', url: '', icon: null }, footer: { text: '', icon: null }, thumbnail: null, image: null, timestamp: '', fields: [] });",
        ]],
    },
    {
        id: 'R3',
        target: 'rail',
        why: 'the rail keeps its own copy of the document and renders from that',
        edits: [[
            "            const state = store.getState();\n            const items = derive(state.document, collapsed);",
            "            const state = store.getState();\n            if (!render.__own) render.__own = JSON.parse(JSON.stringify(state.document));\n            const items = derive(render.__own, collapsed);",
        ]],
    },
    {
        id: 'R4',
        target: 'rail',
        why: 'selection is view state instead of a store action',
        edits: [[
            "            if (currentSelection() === id) return false;\n            store.dispatch({ type: 'ui/selectNode', nodeId: id });\n            return true;",
            "            if (currentSelection() === id) return false;\n            return true;",
        ]],
    },
    {
        id: 'R5',
        target: 'rail',
        why: 'rows are rebuilt on every render (identity and focus are lost)',
        edits: [[
            "                let row = rows.get(vm.id);\n                if (row) stats.nodesReused++;\n                else {\n                    row = buildRow();\n                    rows.set(vm.id, row);\n                }",
            "                let row = rows.get(vm.id);\n                if (row) stats.nodesReused++;\n                row = buildRow();\n                rows.set(vm.id, row);",
        ]],
    },
    {
        id: 'R6',
        target: 'rail',
        why: 'delete does not restore focus to a survivor',
        edits: [[
            "            select(survivor);\n            pendingFocus = survivor;\n            render();",
            "            select(survivor);\n            render();",
        ]],
    },
    {
        id: 'R7',
        target: 'rail',
        why: 'the roving tabindex is never updated (every row is a tab stop)',
        edits: [[
            "                setAttr(row.node, 'tabindex', id === focusTarget ? 0 : -1);",
            "                setAttr(row.node, 'tabindex', 0);",
        ]],
    },
    {
        id: 'R8',
        target: 'rail',
        why: 'the rail never subscribes to the store (it renders only once)',
        edits: [[
            "        unsubs.push(store.subscribe(function (s) { return s.document; }, function () { render(); }));",
            "            /* no subscription */",
        ]],
    },
    {
        id: 'R9',
        target: 'rail',
        why: 'destroy() leaks its store subscriptions (a re-mounted rail stacks renders)',
        edits: [[
            "            unsubs.splice(0).forEach(function (off) { try { off(); } catch (e) { /* fine */ } });",
            "            /* subscriptions leaked */",
        ]],
    },
    {
        id: 'S1',
        target: 'store',
        harness: path.join(ROOT, 'scripts', 'test_message_model.js'),
        why: 'a replacement document (meta.history:false) no longer re-seeds the undo baseline',
        edits: [[
            "                history = [{\n                    hash: model.hashDocument(state.document),\n                    document: model.cloneDocument(state.document),\n                    coalesceKey: null,\n                    at: now(),\n                }];\n                historyIndex = 0;\n                return;",
            "                return;",
        ]],
    },
    {
        id: 'S2',
        target: 'store',
        why: 'a replacement keeps whatever came before it in the stack (only the index resets)',
        edits: [[
            "                history = [{\n                    hash: model.hashDocument(state.document),\n                    document: model.cloneDocument(state.document),\n                    coalesceKey: null,\n                    at: now(),\n                }];\n                historyIndex = 0;\n                return;",
            "                history = history.slice(0, historyIndex + 1);\n                history.push({\n                    hash: model.hashDocument(state.document),\n                    document: model.cloneDocument(state.document),\n                    coalesceKey: null,\n                    at: now(),\n                });\n                historyIndex = history.length - 2;\n                return;",
        ]],
    },
    // ── the inspector ──
    {
        id: 'N1',
        target: 'inspector',
        why: 'an edit writes into the document in place instead of dispatching',
        edits: [[
            "            dispatch({\n                type: 'embed/set',\n                embedId: sel.embed.id,",
            "            sel.embed[key] = String(value);\n            return true;\n            dispatch({\n                type: 'embed/set',\n                embedId: sel.embed.id,",
        ]],
    },
    {
        id: 'N2',
        target: 'inspector',
        why: 'the inspector stops dispatching altogether (a private document)',
        edits: [[
            "        function dispatch(action) {\n            stats.dispatches++;\n            return store.dispatch(action);\n        }",
            "        function dispatch(action) {\n            stats.dispatches++;\n            return true;\n        }",
        ]],
    },
    {
        id: 'N3',
        target: 'inspector',
        why: 'the inspector renders from its own copy of the document (a mirror)',
        edits: [[
            "        function selection() {\n            const state = store.getState();",
            "        function selection() {\n            if (!selection.__mirror) selection.__mirror = model.cloneDocument(store.getState().document);\n            const state = { document: selection.__mirror, ui: store.getState().ui };",
        ]],
    },
    {
        id: 'N4',
        target: 'inspector',
        why: 'the inspector drives the renderer itself instead of leaving it to the store',
        edits: [[
            "        function dispatch(action) {\n            stats.dispatches++;\n            return store.dispatch(action);\n        }",
            "        function dispatch(action) {\n            stats.dispatches++;\n            if (NERO.embed.preview && NERO.embed.preview.create) {\n                NERO.embed.preview.create(mount, { now: function () { return Date.now(); } });\n            }\n            return store.dispatch(action);\n        }",
        ]],
    },
    {
        id: 'N5',
        target: 'inspector',
        why: 'validation leaks in: a hard-coded field limit on every text control',
        edits: [[
            "            input.setAttribute('type', opts.type || 'text');",
            "            input.setAttribute('type', opts.type || 'text');\n            input.setAttribute('maxlength', '256');",
        ]],
    },
    {
        id: 'N6',
        target: 'inspector',
        why: 'the inspector never subscribes to the store (it renders once)',
        edits: [[
            "        unsubs.push(store.subscribe(function (s) { return s.ui.selectedNodeId; }, function () { render(); }));\n        unsubs.push(store.subscribe(function (s) { return s.document; }, function () { render(); }));",
            "        /* no subscriptions */",
        ]],
    },
    {
        id: 'N7',
        target: 'inspector',
        why: 'destroy() leaks its store subscriptions',
        edits: [[
            "            unsubs.splice(0).forEach(function (off) { try { off(); } catch (e) { /* already off */ } });",
            "            /* subscriptions leaked */",
        ]],
    },
    {
        id: 'N8',
        target: 'inspector',
        why: 'the inspector keeps its own selection and ignores later store changes',
        edits: [[
            "            const id = (state.ui && state.ui.selectedNodeId) || null;",
            "            if (!selection.__own) selection.__own = (state.ui && state.ui.selectedNodeId) || null;\n            const id = selection.__own;",
        ]],
    },
    {
        id: 'N9',
        target: 'inspector',
        why: 'the panel swap appends the new panel without detaching the old one',
        edits: [[
            "            if (previous && previous.node.parentNode) previous.node.parentNode.removeChild(previous.node);",
            "            if (false && previous) previous.node.parentNode.removeChild(previous.node);",
        ]],
    },
    {
        id: 'N10',
        target: 'inspector',
        why: 'a field row is rebuilt on every render (focus and identity are lost)',
        edits: [[
            "                let row = fieldRows.get(field.id);\n                if (!row) {\n                    row = buildFieldRow();\n                    fieldRows.set(field.id, row);\n                }",
            "                const row = buildFieldRow();\n                fieldRows.set(field.id, row);",
        ]],
    },
    {
        id: 'N11',
        target: 'inspector',
        why: 'every render overwrites the input the user is typing into (the caret jumps)',
        edits: [[
            "            if (node.value !== next) {\n                node.value = next;\n                stats.valueWrites++;\n            }",
            "            node.value = next;\n            stats.valueWrites++;",
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
    // ── the action bar (step 5d-1) ──
    {
        id: 'A1',
        target: 'actionbar',
        why: 'Undo writes into the document in place instead of asking the store to move its history',
        edits: [[
            "            stats.undos++;\n            store.undo();",
            "            stats.undos++;\n            store.getDocument().content = '';\n            return true;",
        ]],
    },
    {
        id: 'A2',
        target: 'actionbar',
        why: 'the bar never subscribes to the store (its buttons freeze at their first state)',
        edits: [[
            "        unsubs.push(store.subscribe(function () { render(); }));",
            "        /* no subscription */",
        ]],
    },
    {
        id: 'A3',
        target: 'actionbar',
        why: 'Undo is always enabled (a button that lies about what the history can do)',
        edits: [[
            "            setDisabled(buttons.undo.node, !store.canUndo());",
            "            setDisabled(buttons.undo.node, false);",
        ]],
    },
    {
        id: 'A4',
        target: 'actionbar',
        why: 'destroy() leaks its store subscription (a re-mounted page stacks renders)',
        edits: [[
            "            unsubs.splice(0).forEach(function (off) {\n                try { off(); } catch (e) { /* an unsubscribe must never block teardown */ }\n            });",
            "            /* subscriptions leaked */",
        ]],
    },
    {
        id: 'A5',
        target: 'actionbar',
        why: 'destroy() leaves its buttons mounted (the next mount renders into a full container)',
        edits: [[
            "            for (const key in buttons) {\n                const node = buttons[key].node;\n                if (node.parentNode) node.parentNode.removeChild(node);\n            }",
            "            /* buttons left mounted */",
        ]],
    },
    {
        id: 'A6',
        target: 'actionbar',
        why: 'the dialog has no keydown handler (Escape does not close it and Tab is not trapped)',
        edits: [[
            "            overlay.addEventListener('keydown', onDialogKeydown);",
            "            /* no keydown handler */",
        ]],
    },
    {
        id: 'A7',
        target: 'actionbar',
        why: 'closing a dialog does not return focus to the button that opened it',
        edits: [[
            "            if (!destroyed && closing.invoker && closing.invoker.parentNode) focusNode(closing.invoker);",
            "            if (false) focusNode(closing.invoker);",
        ]],
    },
    {
        id: 'A8',
        target: 'actionbar',
        why: 'closing a dialog leaves its overlay in the DOM',
        edits: [[
            "            if (closing.overlay.parentNode) closing.overlay.parentNode.removeChild(closing.overlay);",
            "            /* overlay leaked */",
        ]],
    },
    {
        id: 'A9',
        target: 'actionbar',
        why: 'the clipboard fallback opens without the JSON the user needs',
        edits: [[
            "            area.value = text;",
            "            area.value = '';",
        ]],
    },
    {
        id: 'A10',
        target: 'actionbar',
        why: 'a successful copy says nothing to the user',
        edits: [[
            "                notify('ok', 'JSON copied to the clipboard.');",
            "                /* nothing said */",
        ]],
    },
    {
        id: 'A11',
        target: 'actionbar',
        why: 'the in-flight guard is gone (a second click starts a second clipboard write)',
        edits: [[
            "            if (destroyed || copying) return false;",
            "            if (destroyed) return false;",
        ]],
    },
    {
        id: 'A12',
        target: 'actionbar',
        why: 'opening a dialog does not close the previous one (two dialogs stack)',
        edits: [[
            "            closeDialog('replaced');                 // never more than one",
            "            /* no replacement */",
        ]],
    },
    // ── discard (step 5d-2) ──
    {
        id: 'A13',
        target: 'actionbar',
        why: 'the discard button ignores whether there is anything to discard',
        edits: [[
            "            if (buttons.discard && canDiscard) setDisabled(buttons.discard.node, !canDiscard());",
            "            /* availability ignored */",
        ]],
    },
    {
        id: 'A14',
        target: 'actionbar',
        why: 'the discard button acts even when the page says there is nothing to discard',
        edits: [[
            "            if (canDiscard && !canDiscard()) return false;      // a disabled button is not an action",
            "            if (false) return false;",
        ]],
    },
    {
        id: 'A15',
        target: 'actionbar',
        why: 'the confirmation opens with focus on the destructive control',
        edits: [[
            "            startFocus();       // the cancel button — never the destructive one",
            "            /* focus left where it was */",
        ]],
    },
    {
        id: 'A16',
        target: 'actionbar',
        why: 'confirming the discard closes the dialog without running the action',
        edits: [[
            "                        stats.discards++;\n                        runDiscard();\n                        return true;",
            "                        stats.discards++;\n                        return true;",
        ]],
    },
    // ── the saved baseline (drafts.js) and the first real consumer of it (the page) ──
    {
        id: 'D1',
        target: 'drafts',
        harness: path.join(ROOT, 'scripts', 'test_drafts.js'),
        why: 'the baseline is taken from the document in memory instead of the one that was written',
        edits: [[
            "                    lastSavedDocument = model.cloneDocument(record.document);",
            "                    lastSavedDocument = model.cloneDocument(currentDocument);",
        ]],
    },
    {
        id: 'D2',
        target: 'drafts',
        harness: path.join(ROOT, 'scripts', 'test_drafts.js'),
        why: 'savedDocument() hands out the session\'s own object (a caller can corrupt it)',
        edits: [[
            "            savedDocument: () => (lastSavedDocument ? model.cloneDocument(lastSavedDocument) : null),",
            "            savedDocument: () => lastSavedDocument,",
        ]],
    },
    {
        id: 'D3',
        target: 'drafts',
        harness: path.join(ROOT, 'scripts', 'test_drafts.js'),
        why: 'a NEW draft identity inherits the previous one\'s saved document',
        edits: [[
            "            lastSavedDocument = null;      // a NEW identity has nothing persisted yet",
            "            /* the old baseline is kept */",
        ]],
    },
    {
        id: 'D4',
        target: 'drafts',
        harness: path.join(ROOT, 'scripts', 'test_drafts.js'),
        why: 'use() keeps the previous baseline for a document it does not describe',
        edits: [[
            "                lastSavedDocument = null;  // the saved identity is being reset",
            "                /* the old baseline is kept */",
        ]],
    },
    {
        id: 'D5',
        target: 'drafts',
        harness: path.join(ROOT, 'scripts', 'test_drafts.js'),
        why: 'attach() leaves a baseline behind that the attached store does not describe',
        edits: [[
            "                lastSavedDocument = null;  // attaching re-establishes what \"saved\" means",
            "                /* the old baseline is kept */",
        ]],
    },
    {
        id: 'D6',
        target: 'drafts',
        harness: path.join(ROOT, 'scripts', 'test_drafts.js'),
        why: 'a successful load does not establish a baseline',
        edits: [[
            "                lastSavedDocument = verdict.document ? model.cloneDocument(verdict.document) : null;",
            "                /* no baseline from a load */",
        ]],
    },
    {
        id: 'P1',
        why: 'discarding marks the store saved without restoring the persisted document',
        edits: [[
            "        setCanonical(inst, saved);",
            "        inst.store.markSavedHash(inst.session.savedHash());",
        ]],
    },
    {
        id: 'P2',
        why: 'discard is offered even when the store already matches storage',
        edits: [[
            "        return inst.store.isDirty();",
            "        return true;",
        ]],
    },
    // ── recovery (step 5d-3 Step A) ──
    {
        id: 'D7',
        target: 'drafts',
        harness: path.join(ROOT, 'scripts', 'test_drafts.js'),
        why: 'recover() drops the database handle but forgets to clear the latch, so the retry stays degraded',
        edits: [[
            "            db = null;                   // never trust a handle that just failed a transaction\n            openPromise = null;\n            degraded = null;",
            "            db = null;                   // never trust a handle that just failed a transaction\n            openPromise = null;",
        ]],
    },
    {
        id: 'D8',
        target: 'drafts',
        harness: path.join(ROOT, 'scripts', 'test_drafts.js'),
        why: 'recover() re-probes an ENVIRONMENT failure (an open timeout is retried forever)',
        edits: [[
            "            if (TRANSIENT.indexOf(degraded) === -1) return false; // an environment failure is final",
            "            if (false) return false;",
        ]],
    },
    {
        id: 'D9',
        target: 'drafts',
        harness: path.join(ROOT, 'scripts', 'test_drafts.js'),
        why: 'recover() keeps the torn database handle (a recovered write reuses the failed connection)',
        edits: [[
            "            db = null;                   // never trust a handle that just failed a transaction",
            "            /* the failed handle is kept */",
        ]],
    },
    // ── the save control (step 5d-3 Step B) ──
    {
        id: 'A17',
        target: 'actionbar',
        harness: path.join(ROOT, 'scripts', 'test_message_builder_actionbar.js'),
        why: 'the save control offers a retry for a failure no retry can fix',
        edits: [[
            "            if (retryable) {",
            "            if (true) {",
        ]],
    },
    {
        id: 'A18',
        target: 'actionbar',
        harness: path.join(ROOT, 'scripts', 'test_message_builder_actionbar.js'),
        why: 'the save control ignores a write that is already in flight (a second save is offered)',
        edits: [[
            "        if (session.saving) {",
            "        if (false) {",
        ]],
    },
    {
        id: 'A19',
        target: 'actionbar',
        harness: path.join(ROOT, 'scripts', 'test_message_builder_actionbar.js'),
        why: 'a click on an unavailable save control still starts a save',
        edits: [[
            "            if (buttons.save && buttons.save.node.disabled) return false;",
            "            if (false) return false;",
        ]],
    },
    {
        id: 'A20',
        target: 'actionbar',
        harness: path.join(ROOT, 'scripts', 'test_message_builder_actionbar.js'),
        why: 'the save control claims "Saved" for a draft that was never written',
        edits: [[
            "        if (session.writes > 0) {",
            "        if (true) {",
        ]],
    },
    {
        id: 'A21',
        target: 'actionbar',
        harness: path.join(ROOT, 'scripts', 'test_message_builder_actionbar.js'),
        why: 'the save control is offered even though storage is known to be unusable',
        edits: [[
            "        if (session.degraded && !retryable) {",
            "        if (false) {",
        ]],
    },
    {
        id: 'A22',
        target: 'actionbar',
        harness: path.join(ROOT, 'scripts', 'test_message_builder_actionbar.js'),
        why: 'the save control rewrites its label on every render (a keystroke costs a DOM write)',
        edits: [[
            "            if (node.textContent !== spec.label) {\n                node.textContent = spec.label;\n                stats.saveLabels++;\n            }",
            "            node.textContent = spec.label;\n            stats.saveLabels++;",
        ]],
    },
    {
        id: 'D10',
        target: 'drafts',
        harness: path.join(ROOT, 'scripts', 'test_drafts.js'),
        why: 'retryable() answers yes for ANY failure, including the ones retrying cannot fix',
        edits: [[
            "            if (typeof storage.retryable !== 'function') return false;\n            return !!storage.retryable();",
            "            return true;",
        ]],
    },
    {
        id: 'A23',
        target: 'actionbar',
        harness: path.join(ROOT, 'scripts', 'test_message_builder_actionbar.js'),
        why: 'the save control claims "Saved" while storage is known to be unusable (the latch is ignored once anything was written)',
        edits: [[
            "        if (session.degraded && !retryable) {",
            "        if (session.writes > 0) {\n            return { state: 'saved', label: 'Saved', enabled: false, title: 'This draft is stored' };\n        }\n        if (session.degraded && !retryable) {",
        ]],
    },
    // ── the resolved failure (step 5d-3 Step C) ──
    {
        id: 'D11',
        target: 'drafts',
        harness: path.join(ROOT, 'scripts', 'test_drafts.js'),
        why: 'a failure is never resolved (a dead retry keeps the status region on "Save failed" forever)',
        edits: [[
            "                if (lastError && !inFlight) lastError = null;",
            "                /* the failure is kept */",
        ]],
    },
    {
        id: 'D12',
        target: 'drafts',
        harness: path.join(ROOT, 'scripts', 'test_drafts.js'),
        why: 'a failure is cleared while its write is still in flight (the outcome is unknown)',
        edits: [[
            "                if (lastError && !inFlight) lastError = null;",
            "                if (lastError) lastError = null;",
        ]],
    },
    {
        id: 'P4',
        target: 'page',
        harness: path.join(ROOT, 'scripts', 'test_message_builder_page.js'),
        why: 'the page wires a save button that does not save (the capability is not connected)',
        edits: [[
            "            save: {\n                perform: function () { return saveNow(inst); },\n            },",
            "            save: {\n                perform: function () { return null; },\n            },",
        ]],
    },
    {
        id: 'P5',
        target: 'page',
        harness: path.join(ROOT, 'scripts', 'test_message_builder_page.js'),
        why: 'a manual save forces a write even when nothing is owed (a press rewrites the stored draft)',
        edits: [[
            "    function saveNow(inst) {\n        if (!inst || inst.destroyed || !inst.session) return Promise.resolve({ ok: false, reason: 'no-session' });\n        return inst.session.saveNow();",
            "    function saveNow(inst) {\n        if (!inst || inst.destroyed || !inst.session) return Promise.resolve({ ok: false, reason: 'no-session' });\n        return inst.session.saveNow({ force: true });",
        ]],
    },
    {
        id: 'P3',
        why: 'the discard is pushed onto the undo stack (the discarded edit stays reachable)',
        edits: [[
            "        setCanonical(inst, saved);",
            "        inst.store.dispatch({ type: 'document/load', document: NERO.embed.model.normalizeDocument(saved), meta: { history: true } });\n        inst.store.markSaved(inst.store.getDocument());",
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

    // ── Preflight: no ambiguous anchors ──
    // A mutation is applied with String.replace, i.e. to the FIRST occurrence.
    // When a later edit duplicates an anchor (it happens: two call sites with
    // the same line), the mutant silently starts mutating the wrong one and
    // reports MISSED — which looks like a gap in the tests rather than a stale
    // mutant. Ambiguity is therefore a battery defect and fails loudly here.
    const ambiguous = [];
    MUTANTS.forEach(mutant => {
        const target = TARGETS[mutant.target || 'page'];
        if (!target) return;
        const source = original[mutant.target || 'page'];
        mutant.edits.forEach(([find]) => {
            const first = source.indexOf(find);
            if (first === -1) return;                         // reported per-mutant
            if (source.indexOf(find, first + 1) !== -1) {
                ambiguous.push(mutant.id + ' [' + (mutant.target || 'page') + '] "' +
                    find.split('\n')[0].trim().slice(0, 60) + '…"');
            }
        });
    });
    if (ambiguous.length) {
        console.error('PREFLIGHT FAILED — these anchors match more than one place, so the');
        console.error('mutation would land on whichever comes first:');
        ambiguous.forEach(a => console.error('  ' + a));
        console.error('Widen the anchor with context until it is unique.');
        process.exit(2);
    }

    // ── Preflight: every harness must be GREEN before any mutant runs ──    // A harness that already fails would mark every mutant "caught" for free,
    // which is the one way this battery could lie. So the unmutated baseline
    // runs first and aborts the whole battery if it is not clean.
    const harnesses = Object.keys(TARGETS)
        .map(key => TARGETS[key].harness || HARNESS)
        .concat(MUTANTS.map(m => m.harness).filter(Boolean))
        .filter((h, i, all) => all.indexOf(h) === i);
    const baselines = harnesses.map(h => spawnSync(process.execPath, [h], {
        cwd: ROOT, encoding: 'utf8', env: Object.assign({}, process.env),
    }));
    const redBaselines = harnesses.filter((h, i) => baselines[i].status !== 0);
    if (redBaselines.length) {
        console.error('PREFLIGHT FAILED — the battery cannot judge a mutation while a harness is red:');
        redBaselines.forEach(h => console.error('  ' + path.relative(ROOT, h) + ' is failing'));
        console.error('Fix the harness (or the code it tests) first: a mutant "caught" by an');
        console.error('already-broken harness proves nothing.');
        process.exit(2);
    }

    console.log('message-builder mutation battery — ' + selected.length + ' mutants');
    console.log('preflight: ' + harnesses.map(h => path.basename(h)).join(', ') + ' all green');
    Object.keys(TARGETS).forEach(key => {
        console.log('  ' + key.padEnd(7) + TARGETS[key].label + ' (' + before[key].slice(0, 12) + ')');
    });
    console.log('battery harnesses: ' + Object.keys(TARGETS)
        .map(key => key + ' -> ' + path.basename(TARGETS[key].harness || HARNESS))
        .join(', ') + '\n');

    selected.forEach(mutant => {
        const targetKey = mutant.target || 'page';
        const target = TARGETS[targetKey];
        if (!target) { missed.push(mutant.id + ' (unknown target)'); return; }
        // Which harness judges this mutant: the mutant's own choice (when the
        // property is asserted in a more specific suite), else its target's.
        const harness = mutant.harness || target.harness || HARNESS;
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
        const envPatch = {
            NERO_MB_PAGE_SRC: process.env.NERO_MB_PAGE_SRC,
            NERO_DRAFTS_SRC: process.env.NERO_DRAFTS_SRC,
            NERO_STORE_SRC: process.env.NERO_STORE_SRC,
            NERO_RAIL_SRC: process.env.NERO_RAIL_SRC,
            NERO_INSPECTOR_SRC: process.env.NERO_INSPECTOR_SRC,
            NERO_ACTIONBAR_SRC: process.env.NERO_ACTIONBAR_SRC,
        };
        envPatch[target.env] = file;
        const run = spawnSync(process.execPath, [harness], {
            env: Object.assign({}, process.env, envPatch),
            encoding: 'utf8',
            timeout: 120000,
        });
        const failed = run.status !== 0;
        if (failed) caught++;
        else missed.push(mutant.id);
        const firstFailure = (run.stdout || '').split('\n').filter(l => l.indexOf('  FAIL') === 0)[0] || '';
        console.log('  ' + (failed ? 'CAUGHT  ' : 'MISSED  ') + mutant.id + '  [' + targetKey + '] ' + mutant.why);
        if (!failed) console.log('      run against ' + path.basename(harness));
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
