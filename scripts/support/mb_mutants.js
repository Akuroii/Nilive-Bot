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
    model: {
        file: path.join(ROOT, 'dashboard', 'static', 'js', 'embed', 'model.js'),
        env: 'NERO_MODEL_SRC',
        label: 'dashboard/static/js/embed/model.js',
    },
    validate: {
        file: path.join(ROOT, 'dashboard', 'static', 'js', 'embed', 'validate.js'),
        env: 'NERO_VALIDATE_SRC',
        label: 'dashboard/static/js/embed/validate.js',
        harness: path.join(ROOT, 'scripts', 'test_message_builder_validate.js'),
    },
    assets: {
        file: path.join(ROOT, 'dashboard', 'static', 'js', 'embed', 'assets.js'),
        env: 'NERO_ASSETS_SRC',
        label: 'dashboard/static/js/embed/assets.js',
        harness: path.join(ROOT, 'scripts', 'test_message_builder_assets.js'),
    },
    assetstore: {
        file: path.join(ROOT, 'dashboard', 'static', 'js', 'embed', 'asset-store.js'),
        env: 'NERO_ASSET_STORE_SRC',
        label: 'dashboard/static/js/embed/asset-store.js',
        harness: path.join(ROOT, 'scripts', 'test_message_builder_asset_store.js'),
    },
};
const HARNESS = path.join(ROOT, 'scripts', 'test_message_builder_page.js');
// Step 7c's slice is asserted across five modules at once, so its mutants are
// judged by the integration harness (which can swap any of the five sources).
const INTEGRATION = path.join(ROOT, 'scripts', 'test_message_builder_asset_integration.js');
const HARNESS_VALIDATE = path.join(ROOT, 'scripts', 'test_message_builder_validate.js');
// Step 7d's control and pipeline have their own harness: it drives the real
// file input through the DOM and judges what the document, the history and the
// byte store look like afterwards, so the 7d mutants are judged there.
const UPLOAD = path.join(ROOT, 'scripts', 'test_message_builder_asset_upload.js');
// Step 7e's resolution seam and files summary are judged by their own harness:
// it drives the real picks through the frozen renderer's resolver option, so it
// can tell a resolved URL from an unresolved reference.
const RESOLUTION = path.join(ROOT, 'scripts', 'test_message_builder_asset_resolution.js');

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
        edits: [[
            "        inst.unsubs.push(inst.store.subscribe(\n            function (s) { return s.document; },\n            function () { paintPreview(inst); renderFiles(inst); }\n        ));",
            "        inst.unsubs.push(inst.store.subscribe(\n            function (s) { return s.ui; },\n            function () { paintPreview(inst); renderFiles(inst); }\n        ));",
        ]],
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
            "            const state = store.getState();\n            const items = derive(state.document, collapsed, limits);",
            "            const state = store.getState();\n            if (!render.__own) render.__own = JSON.parse(JSON.stringify(state.document));\n            const items = derive(render.__own, collapsed, limits);",
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
    // ── step 6a: the validation engine ────────────────────────────────
    {
        id: 'V1',
        target: 'validate',
        why: 'a missing limit stops being an error (the table is treated as unlimited)',
        edits: [[
            "        return { ok: missing.length === 0, missing: missing };",
            "        return { ok: true, missing: [] };",
        ]],
    },
    {
        id: 'V2',
        target: 'validate',
        why: 'a served limit is hard-coded in the engine instead of read from the table',
        edits: [[
            "        if (textLen(e.title) > limits.embed.title_max) {",
            "        if (textLen(e.title) > 256) {",
        ]],
    },
    {
        id: 'V3',
        target: 'validate',
        why: 'the issue list is module state instead of per-call state (one document\'s issues leak into the next)',
        edits: [[
            "        const issues = [];",
            "        /* module-level scratch */",
        ], [
            "    // ── The entry point ──────────────────────────────────────────",
            "    const issues = [];\n    // ── The entry point ──────────────────────────────────────────",
        ]],
    },
    {
        id: 'V4',
        target: 'validate',
        why: 'every issue is reported as an error (warnings stop being warnings)',
        edits: [[
            "        issues.push({\n            code: code,\n            path: path,\n            nodeId: nodeId,\n            severity: severity,\n            message: message,\n        });",
            "        issues.push({\n            code: code,\n            path: path,\n            nodeId: nodeId,\n            severity: ERROR,\n            message: message,\n        });",
        ]],
    },
    {
        id: 'V5',
        target: 'validate',
        why: 'an issue points at the wire path instead of the node it is about',
        edits: [[
            "            nodeId: nodeId,\n            severity: severity,",
            "            nodeId: path,\n            severity: severity,",
        ]],
    },
    {
        id: 'V6',
        target: 'validate',
        why: 'the per-embed character budget rejects an embed that is exactly at it',
        edits: [[
            "        if (total > limits.message.embed_total_chars_max) {",
            "        if (total >= limits.message.embed_total_chars_max) {",
        ]],
    },
    {
        id: 'V7',
        target: 'validate',
        why: 'an attachment reference is reported as a hard error although phase 1 cannot upload',
        edits: [[
            "            push(issues, slot.code + '.attachment-missing', slot.path, slot.nodeId, WARNING, message);",
            "            push(issues, slot.code + '.attachment-missing', slot.path, slot.nodeId, ERROR, message);",
        ]],
    },
    {
        id: 'V8',
        target: 'validate',
        why: 'an empty embed is reported even when it is the page\'s own single blank one',
        edits: [[
            "        if (context.embedCount > 1 && context.messageHasContent && !model.embedHasContent(e)) {",
            "        if (context.messageHasContent && !model.embedHasContent(e)) {",
        ]],
    },
    {
        id: 'V9',
        target: 'validate',
        why: 'the timestamp check trusts Date.parse (2026-02-31 rolls over and passes)',
        edits: [[
            "        if (month < 1 || month > 12) return false;\n        if (day < 1 || day > daysInMonth(year, month)) return false;",
            "        if (month < 1 || month > 12) return false;",
        ]],
    },
    {
        id: 'V10',
        target: 'validate',
        why: 'too many embeds no longer stops the per-embed checks (the message-level error gets buried)',
        edits: [[
            "                'A message can carry at most ' + limits.message.embeds_max + ' embeds; this one has ' + embeds.length + '.');\n            return issues;",
            "                'A message can carry at most ' + limits.message.embeds_max + ' embeds; this one has ' + embeds.length + '.');",
        ]],
    },
    // ── step 6a: the page's half ─────────────────────────────────────
    {
        id: 'VP1',
        target: 'page',
        why: 'validation runs synchronously on every keystroke instead of once per burst',
        edits: [[
            "        inst.validateTimer = setTimer(inst, function () {\n            inst.validateTimer = null;\n            validateNow(inst);\n        }, VALIDATE_IDLE_MS);\n        return true;",
            "        inst.validateTimer = null;\n        validateNow(inst);\n        return true;",
        ]],
    },
    {
        id: 'VP2',
        target: 'page',
        why: 'the strip is rewritten on every pass, whether or not its content changed',
        edits: [[
            "        if (signature !== inst.issueSignature) {\n            inst.issueSignature = signature;",
            "        {\n            inst.issueSignature = signature;",
        ], [
            "        if (el.textContent !== text) el.textContent = text;\n        if (el.getAttribute('class') !== className) el.setAttribute('class', className);",
            "        el.textContent = text;\n        if (el.getAttribute('class') !== className) el.setAttribute('class', className);",
        ]],
    },
    {
        id: 'VP3',
        target: 'page',
        why: 'the store is told the issues again on every pass (a new list for identical issues)',
        edits: [[
            "        if (signature !== inst.issueSignature) {\n            inst.issueSignature = signature;",
            "        {\n            inst.issueSignature = signature;",
        ]],
    },
    {
        id: 'VP4',
        target: 'page',
        why: 'the issues never reach the store (dispatched to the wrong slice)',
        edits: [[
            "            inst.store.dispatch({ type: 'ui/setIssues', issues: issues });",
            "            inst.store.dispatch({ type: 'ui/setMode', mode: 'embeds' });",
        ]],
    },
    {
        id: 'VP5',
        target: 'page',
        why: 'a loaded document is not validated until the next edit (a broken draft looks clean)',
        edits: [[
            "        validateNow(inst);\n        return value;",
            "        return value;",
        ]],
    },
    {
        id: 'VP6',
        target: 'page',
        why: 'teardown leaves the scheduled pass armed',
        edits: [[
            "        if (inst.validateTimer !== null) {\n            cancelTimer(inst, inst.validateTimer);\n            inst.validateTimer = null;\n        }\n        inst.unsubs.splice(0).forEach(function (off) {",
            "        inst.unsubs.splice(0).forEach(function (off) {",
        ]],
    },
    {
        id: 'VP7',
        target: 'page',
        why: 'the strip is never hidden again once it has spoken',
        edits: [[
            "            if (el.getAttribute('class') !== STRIP_CLASS) el.setAttribute('class', STRIP_CLASS);\n            if (!el.hidden) el.hidden = true;",
            "            if (el.getAttribute('class') !== STRIP_CLASS) el.setAttribute('class', STRIP_CLASS);\n            /* the strip is never hidden again */",
        ]],
    },
    {
        id: 'VP8',
        target: 'page',
        why: 'the strip drops the count and shows only the first issue',
        edits: [[
            "        const text = label + ' — ' + issues[0].message +",
            "        const text = issues[0].message +",
        ]],
    },

    // ── step 6b: the measurements (validate.js) ──────────────────────
    {
        id: 'V11',
        target: 'validate',
        why: 'a counter calls "over" one character early (>= instead of >, against the rule)',
        edits: [[
            "        return { key: entry.key, used: entry.used, max: entry.max, over: entry.used > entry.max };",
            "        return { key: entry.key, used: entry.used, max: entry.max, over: entry.used >= entry.max };",
        ]],
    },
    {
        id: 'V12',
        target: 'validate',
        why: 'the field cap allows adding AT the cap (the control would never be disabled for a full embed)',
        edits: [[
            "                    canAdd: fields.length < limits.embed.fields_max,",
            "                    canAdd: fields.length <= limits.embed.fields_max,",
        ]],
    },
    {
        id: 'V13',
        target: 'validate',
        why: 'the message cap allows adding AT the cap',
        edits: [[
            "                canAdd: m.message.embeds.used < m.message.embeds.max,",
            "                canAdd: m.message.embeds.used <= m.message.embeds.max,",
        ]],
    },
    {
        id: 'V14',
        target: 'validate',
        why: 'a counter measures the title against the description limit (the numbers come from the wrong key)',
        edits: [[
            "                    { key: 'title', used: textLen(e.title), max: limits.embed.title_max },",
            "                    { key: 'title', used: textLen(e.title), max: limits.embed.description_max },",
        ]],
    },
    {
        id: 'V15',
        target: 'validate',
        why: 'caps() fails OPEN on an unusable table (a page with no limits would allow every add)',
        edits: [[
            "            return { ok: false, embeds: { used: 0, max: 0, canAdd: false }, fields: fields };",
            "            return { ok: false, embeds: { used: 0, max: 0, canAdd: true }, fields: fields };",
        ]],
    },
    {
        id: 'V16',
        target: 'validate',
        why: 'the embed character budget stops being measured (the total counter disappears)',
        edits: [[
            "                    { key: 'total', used: embedCharCount(e, fields), max: limits.message.embed_total_chars_max },",
            "                    { key: 'total', used: 0, max: limits.message.embed_total_chars_max },",
        ]],
    },
    {
        id: 'V17',
        target: 'validate',
        why: 'the field counters measure against the embed limits (a name is counted like a title)',
        edits: [[
            "                        { key: 'field.name', used: textLen(f.name), max: limits.embed.field_name_max },",
            "                        { key: 'field.name', used: textLen(f.name), max: limits.embed.title_max },",
        ]],
    },
    // ── step 6b: the page hands the table over ──────────────────────
    {
        id: 'VP9',
        target: 'page',
        why: 'the views are handed no limits at all (counters blank and every add permanently disabled)',
        edits: [[
            "        inst.rail = f.rail.create({\n            document: doc,\n            store: inst.store,\n            mount: els.railBody,\n            limits: inst.limits,\n        });",
            "        inst.rail = f.rail.create({\n            document: doc,\n            store: inst.store,\n            mount: els.railBody,\n        });",
        ]],
    },
    {
        id: 'VP10',
        target: 'page',
        why: 'the views are handed a COPY of the limits (a second limits state that can drift)',
        edits: [[
            "            mount: els.railBody,\n            limits: inst.limits,\n        });",
            "            mount: els.railBody,\n            limits: Object.assign({}, inst.limits),\n        });",
        ]],
    },
    // ── step 6b: the rail's caps and badges ─────────────────────────
    {
        id: 'R10',
        target: 'rail',
        why: 'the add-embed control is never disabled (the embeds cap is decoration)',
        edits: [[
            "                applyDisabled(row, { addEmbed: vm.canAddEmbed === false });",
            "                applyDisabled(row, { addEmbed: false });",
        ]],
    },
    {
        id: 'R11',
        target: 'rail',
        why: 'the add-field control is never disabled (the fields cap is decoration)',
        edits: [[
            "                addField: vm.canAddField === false,",
            "                addField: false,",
        ]],
    },
    {
        id: 'R12',
        target: 'rail',
        why: 'a warning-only node gets the error tone (the badge overstates the problem)',
        edits: [[
            "                const tone = entry.error ? 'error' : 'warning';",
            "                const tone = 'error';",
        ]],
    },
    {
        id: 'R13',
        target: 'rail',
        why: 'the badge digits are written on every paint, change-guard or not',
        edits: [[
            "                if (row.badgeNum.textContent !== String(count)) stats.badgeWrites++;",
            "                stats.badgeWrites++;",
        ]],
    },
    {
        id: 'R14',
        target: 'rail',
        why: 'every issue is attributed to the message root (badges lose their node)',
        edits: [[
            "                const id = String(issue.nodeId);",
            "                const id = String(CONTENT_NODE);",
        ]],
    },
    {
        id: 'R15',
        target: 'rail',
        why: 'an issue change re-renders the whole rail (row identity and focus die for a number)',
        edits: [[
            "        unsubs.push(store.subscribe(function (s) { return s.ui.issues; },\n            function () { paintBadges(issuesByNode()); }));",
            "        unsubs.push(store.subscribe(function (s) { return s.ui.issues; },\n            function () { render(); }));",
        ]],
    },
    {
        id: 'R16',
        target: 'rail',
        why: 'the add-embed guard is gone (a programmatic add can push past the cap)',
        edits: [[
            "            if (!capFacts(store.getState().document, limits).canAddEmbed) return;\n            const created = dispatchAndPick({ type: 'embed/add' }, 'embed');",
            "            const created = dispatchAndPick({ type: 'embed/add' }, 'embed');",
        ]],
    },
    {
        id: 'R17',
        target: 'rail',
        why: 'the badge carries no meaning for assistive tech (only a stray number)',
        edits: [[
            "                setText(row.badgeText, plural);",
            "                setText(row.badgeText, String(count));",
        ]],
    },
    // ── step 6b: the inspector's readouts ───────────────────────────
    {
        id: 'N12',
        target: 'inspector',
        why: 'the counters are painted once and never updated again',
        edits: [[
            "        function paintCounts(sel, panel) {",
            "        function paintCounts(sel, panel) {\n            if (stats.renders > 1) return;",
        ]],
    },
    {
        id: 'N13',
        target: 'inspector',
        why: 'the over-state never reaches the counter (a value past its limit looks clean)',
        edits: [[
            "                setClass(counters[key], 'mb2-count-over', !!(entry && entry.over));",
            "                setClass(counters[key], 'mb2-count-over', false);",
        ]],
    },
    {
        id: 'N14',
        target: 'inspector',
        why: 'the counter is written on every render, change-guard or not',
        edits: [[
            "                setText(counters[key], entry ? entry.used + ' / ' + entry.max : '');",
            "                counters[key].textContent = entry ? entry.used + ' / ' + entry.max : '';",
        ]],
    },
    {
        id: 'N15',
        target: 'inspector',
        why: 'the inspector parses the limits attribute itself (a second parser, a second limits state)',
        edits: [[
            "        const limits = options.limits || null;",
            "        const limits = JSON.parse((options.document && options.document.getElementById('mb2-root').getAttribute('data-limits')) || 'null');",
        ]],
    },
    {
        id: 'N16',
        target: 'inspector',
        why: 'the field cap is never applied (the add button stays live on a full embed)',
        edits: [[
            "                setDisabled(panel.addField, !(cap && cap.canAdd));",
            "                setDisabled(panel.addField, false);",
        ]],
    },
    {
        id: 'N17',
        target: 'inspector',
        why: 'the add-field action no longer checks the cap (a click past the cap goes through)',
        edits: [[
            "                    const cap = capFacts(store.getState().document).fields[sel.embed.id];\n                    if (!cap || !cap.canAdd) return false;   // 6b: at the cap the button is off",
            "                    /* the cap is not checked here */",
        ]],
    },

    // ── step 7a: the asset core ─────────────────────────────────────
    {
        id: 'AS1',
        target: 'assets',
        why: 'the SHA-256 padding length is wrong, so identity stops matching the real digest',
        edits: [[
            "        const padding = ((56 - (afterOne % 64)) + 64) % 64;",
            "        const padding = ((64 - (afterOne % 64)) + 64) % 64;",
        ]],
    },
    {
        id: 'AS2',
        target: 'assets',
        why: 'the asset id is built from the tail of the digest instead of its head',
        edits: [[
            "        return 'a_' + hex.slice(0, 16);",
            "        return 'a_' + hex.slice(-16);",
        ]],
    },
    {
        id: 'AS3',
        target: 'assets',
        why: 'any RIFF container is called WebP (a WAV would be accepted as an image)',
        edits: [[
            "        if (startsWith(bytes, RIFF_SIGNATURE) && ascii(bytes, 8, 4) === 'WEBP') {\n            return MIME_WEBP;\n        }",
            "        if (startsWith(bytes, RIFF_SIGNATURE)) {\n            return MIME_WEBP;\n        }",
        ]],
    },
    {
        id: 'AS4',
        target: 'assets',
        why: 'the GIF version byte is not checked any more',
        edits: [[
            "        if (startsWith(bytes, GIF_SIGNATURE) &&\n            (bytes[4] === 0x37 || bytes[4] === 0x39) && bytes[5] === 0x61) {",
            "        if (startsWith(bytes, GIF_SIGNATURE) && bytes[5] === 0x61) {",
        ]],
    },
    {
        id: 'AS5',
        target: 'assets',
        why: 'a filename keeps its directory parts (../../etc/passwd survives as a path)',
        edits: [[
            "        const base = baseName(name).toLowerCase().replace(/\\.[^.]*$/, function (tail, at) {",
            "        const base = String(name).toLowerCase().replace(/\\.[^.]*$/, function (tail, at) {",
        ]],
    },
    {
        id: 'AS6',
        target: 'assets',
        why: 'a leading dot is allowed, so a dotfile can be stored',
        edits: [[
            "            .replace(/^[.-]+/, '')            // never a dotfile, never a leading dash",
            "            .replace(/^-+/, '')               // never a leading dash",
        ]],
    },
    {
        id: 'AS7',
        target: 'assets',
        why: 'the filename length cap is not applied',
        edits: [[
            "        const cut = stem.slice(0, Math.max(1, room)).replace(/[.-]+$/, '');\n        return cut || FALLBACK_STEM;",
            "        return stem;",
        ]],
    },
    {
        id: 'AS8',
        target: 'assets',
        why: 'a two-way filename collision is not de-collided (two files, one name)',
        edits: [[
            "            collisions = Object.keys(groups).filter(name => groups[name].length > 1).sort();",
            "            collisions = Object.keys(groups).filter(name => groups[name].length > 2).sort();",
        ]],
    },
    {
        id: 'AS9',
        target: 'assets',
        why: 'the conflict winner depends on input order rather than a fixed rule',
        edits: [[
            "                if (filename < byId[id].filename) byId[id] = entry;",
            "                if (filename > byId[id].filename) byId[id] = entry;",
        ]],
    },
    {
        id: 'AS10',
        target: 'assets',
        why: 'the file cap is a hard-coded number instead of the served one',
        edits: [[
            "        const countMax = table.count_max;",
            "        const countMax = 10;",
        ]],
    },
    {
        id: 'AS11',
        target: 'assets',
        why: 'the limits table is treated as usable even when its keys are missing (fails OPEN)',
        edits: [[
            "        return { ok: missing.length === 0, reason: missing.length ? 'limits-unusable' : null, missing: missing };",
            "        return { ok: true, reason: null, missing: missing };",
        ]],
    },
    {
        id: 'AS12',
        target: 'assets',
        why: 'one more file may be added AT the cap',
        edits: [[
            "            count: { used: used, max: countMax, over: used > countMax, canAdd: used < countMax },",
            "            count: { used: used, max: countMax, over: used > countMax, canAdd: used <= countMax },",
        ]],
    },
    {
        id: 'AS13',
        target: 'assets',
        why: 'the served "this cap is hard" flag is ignored, so a blocking size never blocks',
        edits: [[
            "            blocked: oversized && advisory.isHard,",
            "            blocked: false,",
        ]],
    },
    {
        id: 'AS14',
        target: 'assets',
        why: 'pruneOrphans deletes from the map it was given instead of building a new one',
        edits: [[
            "            } else {\n                pruned.push(key);\n            }",
            "            } else {\n                delete source[key];\n                pruned.push(key);\n            }",
        ]],
    },
    {
        id: 'AS15',
        target: 'assets',
        why: 'an unknown availability claims the bytes are present (the optimistic default)',
        edits: [[
            "        const availability = AVAILABILITY.indexOf(fields.availability) !== -1\n            ? fields.availability : 'bytes-missing';",
            "        const availability = fields.availability === 'bytes-missing' ? 'bytes-missing' : 'bytes-local';",
        ]],
    },
    {
        id: 'AS16',
        target: 'assets',
        why: 'a record rebuilt from storage drops keys this build does not know about',
        edits: [[
            "        Object.keys(value).sort().forEach(key => {\n            if (RECORD_KEYS.indexOf(key) !== -1) return;\n            out[key] = value[key];\n        });\n        return out;",
            "        return out;",
        ]],
    },
    {
        id: 'AS17',
        target: 'assets',
        why: 'a name claiming a format an embed cannot show is accepted',
        edits: [[
            "        if (declaredExt && !Object.prototype.hasOwnProperty.call(MIME_BY_EXTENSION, declaredExt)) {",
            "        if (false) {",
        ]],
    },
    {
        id: 'AS18',
        target: 'assets',
        why: 'a name that disagrees with its bytes is accepted (a JPEG called .png)',
        edits: [[
            "        if (declaredExt && MIME_BY_EXTENSION[declaredExt] !== mime) {",
            "        if (false) {",
        ]],
    },
    {
        id: 'AS19',
        target: 'assets',
        why: 'refsOf() counts a pasted URL as an asset reference',
        edits: [[
            "                if (!value || typeof value !== 'object' || value.kind !== 'upload') return;",
            "                if (!value || typeof value !== 'object') return;",
        ]],
    },
    {
        id: 'AS20',
        target: 'assets',
        why: 'a typed-array view is read whole, ignoring its byteOffset (a Buffer pool leaks in)',
        edits: [[
            "            const view = new Uint8Array(value.buffer, value.byteOffset, value.byteLength);\n            return view;",
            "            return new Uint8Array(value.buffer);",
        ]],
    },
    {
        id: 'AS21',
        target: 'assets',
        why: 'module-level state is introduced (a mutable table)',
        edits: [[
            "    const PREFIX_LENGTHS = [6, 8, 16];",
            "    let PREFIX_LENGTHS = [6, 8, 16];",
        ]],
    },
    {
        id: 'AS22',
        target: 'assets',
        why: 'the limits table is cached in module state instead of read from the argument',
        edits: [[
            "        const table = limits && limits.attachments;",
            "        const table = attachmentLimits.__cache || (attachmentLimits.__cache = limits && limits.attachments);",
        ]],
    },
    {
        id: 'AS23',
        target: 'assets',
        why: 'an empty file stops being an explicit "no bytes" refusal',
        edits: [[
            "        if (!bytes || !bytes.length) {\n            return {\n                ok: false, reason: 'no-bytes', declaredExt: filenameExtension(name),",
            "        if (!bytes) {\n            return {\n                ok: false, reason: 'no-bytes', declaredExt: filenameExtension(name),",
        ]],
    },
    {
        id: 'AS24',
        target: 'assets',
        why: 'the refusal stops saying what the file actually is',
        edits: [[
            "        if (ascii(bytes, 0, 5) === '%PDF-') return 'a PDF document';",
            "        if (ascii(bytes, 0, 5) === '%PDF-') return 'a file';",
        ]],
    },
    {
        id: 'AS25',
        target: 'assets',
        why: 'the de-collision prefix stops coming from the content hash, so two assets share one name',
        edits: [[
            "        const source = /^[0-9a-f]+$/.test(sha) ? sha : fallback;",
            "        const source = '0'.repeat(16);",
        ]],
    },

    // ── step 7b: the asset byte store + object URLs ────────────────
    {
        id: 'ASB1',
        target: 'assetstore',
        why: "the stored bytes are the caller's buffer, so a later write into it changes the asset",
        edits: [[
            "        const copy = new Uint8Array(view.length);\n        copy.set(view);\n        return copy.buffer;",
            "        return view.buffer;",
        ]],
    },
    {
        id: 'ASB2',
        target: 'assetstore',
        why: "an empty file is stored as if it were an image",
        edits: [[
            "                if (!view || !view.length) return Promise.resolve(refuse(assetId, 'no-bytes'));",
            "                if (!view) return Promise.resolve(refuse(assetId, 'no-bytes'));",
        ]],
    },
    {
        id: 'ASB3',
        target: 'assetstore',
        why: "the id is trusted instead of computed, so one id can hold any bytes",
        edits: [[
            "                const expected = core.assetIdFromSha(sha256);\n                if (!expected || expected !== assetId) {",
            "                const expected = assetId;\n                if (!expected) {",
        ]],
    },
    {
        id: 'ASB4',
        target: 'assetstore',
        why: "two different assets of the same length count as the same asset",
        edits: [[
            "                    const duplicate = !!(found.ok && found.entry.sha256 === sha256 &&\n                        found.entry.byteLength === entry.byteLength);",
            "                    const duplicate = !!(found.ok &&\n                        found.entry.byteLength === entry.byteLength);",
        ]],
    },
    {
        id: 'ASB5',
        target: 'assetstore',
        why: "a duplicate is rewritten every time instead of being recognised",
        edits: [[
            "                    const duplicate = !!(found.ok && found.entry.sha256 === sha256 &&\n                        found.entry.byteLength === entry.byteLength);",
            "                    const duplicate = false;",
        ]],
    },
    {
        id: 'ASB6',
        target: 'assetstore',
        why: "bytes that only reached memory are reported as already saved",
        edits: [[
            "                        if (found.source !== 'memory') return described;",
            "                        return described;",
        ]],
    },
    {
        id: 'ASB7',
        target: 'assetstore',
        why: "a failed read is reported as a missing asset (the two states become one)",
        edits: [[
            "                        if (!available()) return { ok: false, reason: storageReason() };\n                        stats.misses++;\n                        return { ok: false, reason: 'missing' };",
            "                        stats.misses++;\n                        return { ok: false, reason: 'missing' };",
        ]],
    },
    {
        id: 'ASB8',
        target: 'assetstore',
        why: "a truncated entry is served as if it were the asset",
        edits: [[
            "        if (raw.byteLength !== view.length) return null;          // truncated or padded: not the bytes we stored",
            "        if (false) return null;                                   // truncated or padded: not the bytes we stored",
        ]],
    },
    {
        id: 'ASB9',
        target: 'assetstore',
        why: "an entry with an unusable digest is accepted",
        edits: [[
            "        if (typeof raw.sha256 !== 'string' || !HEX_RE.test(raw.sha256)) return null;",
            "        if (typeof raw.sha256 !== 'string') return null;",
        ]],
    },
    {
        id: 'ASB10',
        target: 'assetstore',
        why: "an entry stored under another key is served (the key stops meaning anything)",
        edits: [[
            "        if (raw.assetId !== assetId) return null;                 // a key pointing at someone else's record",
            "        if (false) return null;                                   // a key pointing at someone else's record",
        ]],
    },
    {
        id: 'ASB11',
        target: 'assetstore',
        why: "getBytes hands out the store's own bytes, so a caller can corrupt the asset",
        edits: [[
            "                    const copy = new Uint8Array(view.length);\n                    copy.set(view);",
            "                    const copy = view;",
        ]],
    },
    {
        id: 'ASB12',
        target: 'assetstore',
        why: "a verified read serves bytes that no longer hash to their id",
        edits: [[
            "                        if (actual !== found.entry.sha256) {",
            "                        if (false) {",
        ]],
    },
    {
        id: 'ASB13',
        target: 'assetstore',
        why: "a missing asset is answered with a fabricated URL (the broken image this design forbids)",
        edits: [[
            "                        stats.urlMisses++;\n                        return refuse(assetId, found.reason);",
            "                        stats.urlMisses++;\n                        return { ok: true, assetId: assetId, url: 'blob:placeholder', minted: true, cached: false, mime: NEUTRAL_MIME };",
        ]],
    },
    {
        id: 'ASB14',
        target: 'assetstore',
        why: "the URL is re-minted on every lookup of unchanged bytes",
        edits: [[
            "                    const cached = urlCache[assetId];\n                    if (cached && cached.mime === mime) {",
            "                    const cached = urlCache[assetId];\n                    if (false) {",
        ]],
    },
    {
        id: 'ASB15',
        target: 'assetstore',
        why: "re-typing an asset leaves the old URL alive next to the new one",
        edits: [[
            "                    if (cached) releaseAsset(assetId);       // a different blob type: the old URL was another resource",
            "                    if (false) releaseAsset(assetId);        // a different blob type: the old URL was another resource",
        ]],
    },
    {
        id: 'ASB16',
        target: 'assetstore',
        why: "release drops the URL from the cache without revoking it (a leaked object URL)",
        edits: [[
            "            revokeUrl(cached.url);\n            delete urlCache[assetId];\n            return { ok: true, assetId: assetId, revoked: true };",
            "            delete urlCache[assetId];\n            return { ok: true, assetId: assetId, revoked: true };",
        ]],
    },
    {
        id: 'ASB17',
        target: 'assetstore',
        why: "releaseAll revokes only the first outstanding URL",
        edits: [[
            "            ids.forEach((id) => {\n                revokeUrl(urlCache[id].url);\n                delete urlCache[id];\n            });",
            "            ids.slice(0, 1).forEach((id) => {\n                revokeUrl(urlCache[id].url);\n                delete urlCache[id];\n            });",
        ]],
    },
    {
        id: 'ASB18',
        target: 'assetstore',
        why: "teardown revokes nothing",
        edits: [[
            "                const released = releaseAllUrls();",
            "                const released = { ok: true, revoked: 0, assetIds: [] };",
        ]],
    },
    {
        id: 'ASB19',
        target: 'assetstore',
        why: "a destroyed store keeps working (a zombie serving from a closed adapter)",
        edits: [[
            "                dead = true;\n                // `retained` is how many byte entries this instance still holds.\n                // A teardown that leaves one behind has leaked a whole image, and\n                // saying the number out loud is what makes that checkable instead\n                // of hoped for.\n                return {\n                    ok: true, revoked: released.revoked, assetIds: released.assetIds,\n                    dropped: dropped, retained: Object.keys(memory).length,\n                };",
            "                dead = false;\n                // `retained` is how many byte entries this instance still holds.\n                // A teardown that leaves one behind has leaked a whole image, and\n                // saying the number out loud is what makes that checkable instead\n                // of hoped for.\n                return {\n                    ok: true, revoked: released.revoked, assetIds: released.assetIds,\n                    dropped: dropped, retained: Object.keys(memory).length,\n                };",
        ]],
    },
    {
        id: 'ASB20',
        target: 'assetstore',
        why: "teardown keeps the session bytes (an unpersisted asset outlives its session)",
        edits: [[
            "                dropped.forEach((id) => { delete memory[id]; });",
            "                dropped.forEach((id) => { });",
        ]],
    },
    {
        id: 'ASB21',
        target: 'assetstore',
        why: "the adapter is pointed at the document store, so bytes and documents share one store",
        edits: [[
            "            primaryStore: STORE_NAME,",
            "            primaryStore: drafts.V2_STORES[0],",
        ]],
    },
    {
        id: 'ASB22',
        target: 'assetstore',
        why: "storage is treated as usable without asking it",
        edits: [[
            "            return typeof storage.isAvailable === 'function' ? !!storage.isAvailable() : true;",
            "            return true;",
        ]],
    },
    {
        id: 'ASB23',
        target: 'assetstore',
        why: "mode() claims persistence even when there is none",
        edits: [[
            "                    mode: has ? 'indexeddb' : 'memory',",
            "                    mode: 'indexeddb',",
        ]],
    },
    {
        id: 'ASB24',
        target: 'assetstore',
        why: "the stored content type is passed through unnormalised",
        edits: [[
            "                    mime: normaliseMime(opts && opts.mime),",
            "                    mime: (opts && opts.mime) || '',",
        ]],
    },
    {
        id: 'ASB25',
        target: 'assetstore',
        why: "a persisted asset keeps a second copy in memory (two answers to where the bytes are)",
        edits: [[
            "                        if (w.persisted) {\n                            // Storage has it: this instance does not need a\n                            // second copy for the rest of the session.\n                            delete memory[assetId];\n                        } else {\n                            memory[assetId] = entry;\n                        }",
            "                        memory[assetId] = entry;",
        ]],
    },
    {
        id: 'ASB26',
        target: 'assetstore',
        why: "survey() reports every asset as available",
        edits: [[
            "                        availability: found.ok ? 'bytes-local' : 'bytes-missing',",
            "                        availability: 'bytes-local',",
        ]],
    },
    {
        id: 'ASB27',
        target: 'assetstore',
        why: "the URL factory is handed the store's own bytes instead of a copy",
        edits: [[
            "                    const forUrl = new Uint8Array(view.length);\n                    forUrl.set(view);",
            "                    const forUrl = view;",
        ]],
    },

    // ── step 7c: asset metadata, facts, retention and the page probe ──
    {
        id: 'C1',
        target: 'model',
        harness: INTEGRATION,
        why: 'a record is stored BY REFERENCE, so the caller keeps a handle on the document',
        edits: [[
            '        const copy = {};\n        Object.keys(record).forEach((key) => { copy[key] = record[key]; });\n',
            '        const copy = record;\n',
        ]],
    },
    {
        id: 'C2',
        target: 'model',
        harness: INTEGRATION,
        why: 'a record describing a DIFFERENT file is accepted under this key',
        edits: [[
            '        if (record.assetId != null && String(record.assetId) !== id) return doc;\n',
            '        /* the id check is gone */\n',
        ]],
    },
    {
        id: 'C3',
        target: 'model',
        harness: INTEGRATION,
        why: 're-storing the SAME record counts as an edit (a history entry for nothing)',
        edits: [[
            '        if (stableStringify(next[id]) === stableStringify(copy)) return doc;   // same record: no edit, no history entry\n',
            '        if (stableStringify(next[id]) === stableStringify({ })) return doc;   // same record: no edit, no history entry\n',
        ]],
    },
    {
        id: 'C4',
        target: 'model',
        harness: INTEGRATION,
        why: 'a value a draft cannot store (a Blob, an image element) is admitted into the document',
        edits: [[
            '            if (!jsonScalar(record[keys[i]])) return false;\n',
            '            if (!jsonScalar(record[keys[i]]) && keys[i] === "__never__") return false;\n',
        ]],
    },
    {
        id: 'C5',
        target: 'model',
        harness: INTEGRATION,
        why: 'removing a record the document does not have still counts as an edit',
        edits: [[
            '        if (!Object.prototype.hasOwnProperty.call(map, id)) return doc;\n',
            '        if (!map) return doc;\n',
        ]],
    },
    {
        id: 'C6',
        target: 'store',
        harness: INTEGRATION,
        why: 'the record map is keyed by the value the CALLER claims, not by the action',
        edits: [[
            '            \'asset/add\': (state, a) => ({ document: m.setDocumentAsset(state.document, a.assetId, a.record) }),\n',
            '            \'asset/add\': (state, a) => ({ document: m.setDocumentAsset(state.document, a.record && a.record.assetId, a.record) }),\n',
        ]],
    },
    {
        id: 'C7',
        target: 'store',
        harness: INTEGRATION,
        why: 'historyDocuments() hands out the store’s own documents (a caller can rewrite undo)',
        edits: [[
            '            return history.map(entry => model.cloneDocument(entry.document));\n',
            '            return history.map(entry => entry.document);\n',
        ]],
    },
    {
        id: 'C8',
        target: 'store',
        harness: INTEGRATION,
        why: 'historyDocuments() hides the redo tail, so a retention decision forgets what redo can restore',
        edits: [[
            '            return history.map(entry => model.cloneDocument(entry.document));\n',
            '            return history.slice(0, historyIndex + 1).map(entry => model.cloneDocument(entry.document));\n',
        ]],
    },
    {
        id: 'C9',
        target: 'validate',
        harness: HARNESS_VALIDATE,
        why: 'the attachment keys are no longer required, so a partial table silently measures nothing',
        edits: [[
            '        [\'attachments\', \'count_max\'],\n        [\'attachments\', \'total_bytes_max\'],\n',
            '',
        ]],
    },
    {
        id: 'C10',
        target: 'validate',
        harness: INTEGRATION,
        why: 'the count/size rules use numbers baked into the source instead of the served table',
        edits: [[
            '        const measured = assets.checkLimits(sizes.count, sizes.total, limits);\n',
            '        const measured = assets.checkLimits(sizes.count, sizes.total, { attachments: { count_max: 2, total_bytes_max: 8 } });\n',
        ]],
    },
    {
        id: 'C11',
        target: 'validate',
        harness: INTEGRATION,
        why: 'the count rule measures the wrong half of the pair',
        edits: [[
            '            if (measured.count.over) {\n',
            '            if (measured.bytes.over) {\n',
        ]],
    },
    {
        id: 'C12',
        target: 'validate',
        harness: INTEGRATION,
        why: 'a record with NO bytes at all is also reported as a size problem',
        edits: [[
            '            } else if (!sizes.missing.length && measured.bytes.over) {\n',
            '            } else if (measured.bytes.over) {\n',
        ]],
    },
    {
        id: 'C13',
        target: 'validate',
        harness: INTEGRATION,
        why: 'a slot whose file name changed is reported only when the names AGREE',
        edits: [[
            '            if (!record || !ref.filename || ref.filename === record.filename) return;\n',
            '            if (!record || !ref.filename || ref.filename !== record.filename) return;\n',
        ]],
    },
    {
        id: 'C14',
        target: 'validate',
        harness: INTEGRATION,
        why: 'a damaged stored entry is reported as merely absent',
        edits: [[
            '            push(issues, \'assets.bytes-corrupt\', path, nodeId, ERROR,\n',
            '            push(issues, \'assets.bytes-missing\', path, nodeId, WARNING,\n',
        ]],
    },
    {
        id: 'C15',
        target: 'validate',
        harness: INTEGRATION,
        why: 'a stored copy with the WRONG digest is called a match',
        edits: [[
            '        if (record.sha256 && row.sha256 && String(row.sha256) !== String(record.sha256)) {\n',
            '        if (record.sha256 && row.sha256 && String(row.sha256) === String(record.sha256)) {\n',
        ]],
    },
    {
        id: 'C16',
        target: 'validate',
        harness: INTEGRATION,
        why: 'the per-file advisory is treated as a hard block',
        edits: [[
            '            verdict.blocked ? ERROR : WARNING,\n',
            '            ERROR,\n',
        ]],
    },
    {
        id: 'C17',
        target: 'validate',
        harness: INTEGRATION,
        why: 'an unreadable record is not reported at all',
        edits: [[
            '        view.unreadable.forEach(function (entry) {\n',
            '        [].forEach(function (entry) {\n',
        ]],
    },
    {
        id: 'C18',
        target: 'validate',
        harness: INTEGRATION,
        why: 'an upload VALUE is judged by name again, on top of the asset rules',
        edits: [[
            '        if (slot.upload) return;\n',
            '        /* the asset rules no longer own upload values */\n',
        ]],
    },
    {
        id: 'C19',
        target: 'assets',
        harness: INTEGRATION,
        why: 'a reason the rules do not know is read as a MISS instead of "could not tell"',
        edits: [[
            '        if (reason) return FACT_STATES.UNAVAILABLE;\n',
            '        if (reason) return FACT_STATES.MISSING;\n',
        ]],
    },
    {
        id: 'C20',
        target: 'assets',
        harness: INTEGRATION,
        why: 'a damaged entry is read as absent',
        edits: [[
            '        if (CORRUPT_REASONS.indexOf(reason) !== -1) return FACT_STATES.CORRUPT;\n',
            '        if (CORRUPT_REASONS.indexOf(reason) !== -1) return FACT_STATES.MISSING;\n',
        ]],
    },
    {
        id: 'C21',
        target: 'assets',
        harness: INTEGRATION,
        why: 'a referenced id with NO row is called missing (an unobserved asset becomes a broken one)',
        edits: [[
            '                states[id] = FACT_STATES.UNKNOWN;\n',
            '                states[id] = FACT_STATES.MISSING;\n',
        ]],
    },
    {
        id: 'C22',
        target: 'assets',
        harness: INTEGRATION,
        why: 'retention forgets the assets this session made, so an undoable file could be pruned',
        edits: [[
            '        (Array.isArray(opts.sessionIds) ? opts.sessionIds : []).forEach((id) => {\n',
            '        [].forEach((id) => {\n',
        ]],
    },
    {
        id: 'C23',
        target: 'assets',
        harness: INTEGRATION,
        why: 'retention answers "keep everything" when it cannot prove ownership (fail-open)',
        edits: [[
            '            return { ok: false, reason: \'no-documents\', keep: [], refs: [], orphans: [], plan: null };\n',
            '            return { ok: true, reason: null, keep: [], refs: [], orphans: [], plan: null };\n',
        ]],
    },
    {
        id: 'C24',
        target: 'assets',
        harness: INTEGRATION,
        why: 'a record with a nested value is accepted (it cannot survive a draft write)',
        edits: [[
            '            .filter((key) => RECORD_KEYS.indexOf(key) === -1 && !jsonScalar(value[key]))\n',
            '            .filter((key) => RECORD_KEYS.indexOf(key) === -1 && typeof value[key] === "function")\n',
        ]],
    },
    {
        id: 'C25',
        target: 'assets',
        harness: INTEGRATION,
        why: 'a referenced id with no record is DROPPED from the measurement, so the total looks clean',
        edits: [[
            '            if (!record) { missing.push(id); return; }\n',
            '            if (!record) return;\n',
        ]],
    },
    {
        id: 'C26',
        target: 'assets',
        harness: INTEGRATION,
        why: 'a record that cannot be read is ALSO called unused (one entry, two problems)',
        edits: [[
            '            .filter((id) => ids.indexOf(id) === -1 && !broken[id]);\n',
            '            .filter((id) => ids.indexOf(id) === -1 );\n',
        ]],
    },
    {
        id: 'C27',
        target: 'assetstore',
        harness: INTEGRATION,
        why: 'a probe reports every id as present (missing bytes never surface)',
        edits: [[
            '                        present: found.ok,\n',
            '                        present: true,\n',
        ]],
    },
    {
        id: 'C28',
        target: 'assetstore',
        harness: INTEGRATION,
        why: 'a probe drops the reason, so a miss cannot be told from "could not tell"',
        edits: [[
            '                        reason: found.ok ? null : found.reason,\n',
            '                        reason: null,\n',
        ]],
    },
    {
        id: 'C29',
        target: 'page',
        harness: INTEGRATION,
        why: 'the validator is run without the facts, so byte problems are never reported',
        edits: [[
            '        const issues = validator.validate(doc, inst.limits, inst.assetFacts);\n',
            '        const issues = validator.validate(doc, inst.limits);\n',
        ]],
    },
    {
        id: 'C30',
        target: 'page',
        harness: INTEGRATION,
        why: 'the page never probes: the facts never describe the document on screen',
        edits: [[
            '        if (ids.join(\',\') !== factsSignature) {\n',
            '        if (ids.join(\',\') === factsSignature && ids.length < 0) {\n',
        ]],
    },
    {
        id: 'C31',
        target: 'page',
        harness: INTEGRATION,
        why: 'probes are not counted, so a probe storm is invisible',
        edits: [[
            '        countProbe(inst);\n',
            '        void 0;\n',
        ]],
    },
    {
        id: 'C32',
        target: 'page',
        harness: INTEGRATION,
        why: 'the probe asks about no ids at all (every referenced asset stays unknown)',
        edits: [[
            '        return Promise.resolve(inst.assetStore.survey(ids)).then(function (rows) {\n',
            '        return Promise.resolve(inst.assetStore.survey([])).then(function (rows) {\n',
        ]],
    },
    {
        id: 'C33',
        target: 'page',
        harness: INTEGRATION,
        why: 'the facts are computed against a different document than the one on screen',
        edits: [[
            '            inst.assetFacts = NERO.embed.assets.assetFacts(doc, rows);\n',
            '            inst.assetFacts = NERO.embed.assets.assetFacts({ embeds: [], assets: {} }, rows);\n',
        ]],
    },
    {
        id: 'C34',
        target: 'page',
        harness: INTEGRATION,
        why: 'the page stops being the single issue-list writer (nothing paints the strip)',
        edits: [[
            '            inst.store.dispatch({ type: \'ui/setIssues\', issues: issues });\n',
            '            void 0;\n',
        ]],
    },


    // ── step 7d: the local-file pick. Judged by the upload harness, which drives
    //    the control through the DOM and then judges the document, the history,
    //    the byte store and the control's own words.
    //    U1..U6 are the write path: the right id, bytes before document, and no
    //    swallowing. U7/U8 are the transaction's shape (one undo step, record
    //    before reference). U9/U10 are the two deletions 7d must never do.
    //    U11..U13 are the "hard-coded or swallowed" family. U14..U16 are the
    //    async boundary. U17..U19 are the control's own promises.
    {
        id: 'U1',
        target: 'page',
        harness: UPLOAD,
        why: "the record is stored under a DIFFERENT asset id than the bytes and the reference",
        edits: [[
            "                inst.store.dispatch({\n                    type: 'asset/add', assetId: ident.assetId, record: built.record,\n                    meta: { coalesceKey: coalesce },\n                });\n                applySlotValue(inst, embedId, key, {\n                    kind: 'upload', assetId: ident.assetId, filename: built.record.filename,\n                    mime: built.record.mime, bytes: built.record.bytes,\n                }, coalesce);",
            "                const wrongRecord = Object.assign({}, built.record, { assetId: ident.assetId + '-wrong' });\n                const coalesce = 'upload:' + embedId + ':' + key;\n                inst.store.dispatch({\n                    type: 'asset/add', assetId: ident.assetId, record: wrongRecord,\n                    meta: { coalesceKey: coalesce },\n                });\n                applySlotValue(inst, embedId, key, {\n                    kind: 'upload', assetId: ident.assetId, filename: built.record.filename,\n                    mime: built.record.mime, bytes: built.record.bytes,\n                }, coalesce);",
        ]],
    },
    {
        id: 'U2',
        target: 'page',
        harness: UPLOAD,
        why: "a pick stores the bytes under an id that is not the one the rules minted",
        edits: [[
            "        return Promise.resolve(inst.assetStore.putBytes(ident.assetId, buffer, { mime: ident.mime }))",
            "        return Promise.resolve(inst.assetStore.putBytes(ident.assetId + '-x', buffer, { mime: ident.mime }))",
        ]],
    },
    {
        id: 'U3',
        target: 'page',
        harness: UPLOAD,
        why: "the slot is hard-coded: an image pick writes the thumbnail",
        edits: [[
            "                slot: key === 'media.image' ? 'image' : 'thumbnail',",
            "                slot: 'thumbnail',",
        ]],
    },
    {
        id: 'U4',
        target: 'page',
        harness: UPLOAD,
        why: "the identity rules are skipped (the file is taken on trust)",
        edits: [[
            "        const ident = A.identify(buffer, file && file.name);\n        if (!ident || !ident.ok) {",
            "        const ident = A.identify(buffer, file && file.name);\n        if (false) {",
        ]],
    },
    {
        id: 'U5',
        target: 'page',
        harness: UPLOAD,
        why: "the bytes are never stored (the document is edited anyway)",
        edits: [[
            "        return Promise.resolve(inst.assetStore.putBytes(ident.assetId, buffer, { mime: ident.mime }))",
            "        return Promise.resolve({ ok: true, persisted: true, byteLength: buffer.length,\n            mime: ident.mime }).then(function (stored) {",
        ]],
    },
    {
        id: 'U6',
        target: 'page',
        harness: UPLOAD,
        why: "a FAILED put is swallowed and the document is edited without bytes",
        edits: [[
            "                if (!stored || !stored.ok) {\n                    setNotice(inst, {\n                        tone: 'danger',\n                        text: 'That file could not be stored in this browser, so nothing was added to the message.',\n                    });\n                    finishUpload(inst, token);\n                    return null;\n                }",
            "                if (!stored) return null;",
        ]],
    },
    {
        id: 'U7',
        target: 'page',
        harness: UPLOAD,
        why: "the two edits stop sharing a coalesce key (a pick becomes TWO undo steps)",
        edits: [[
            "        const meta = { coalesceKey: coalesce };",
            "        const meta = {};   // mutant: no coalesce key",
        ]],
    },
    {
        id: 'U8',
        target: 'page',
        harness: UPLOAD,
        why: "the reference is written BEFORE the record it points at",
        edits: [[
            "                const coalesce = 'upload:' + embedId + ':' + key;\n                inst.store.dispatch({\n                    type: 'asset/add', assetId: ident.assetId, record: built.record,\n                    meta: { coalesceKey: coalesce },\n                });\n                applySlotValue(inst, embedId, key, {\n                    kind: 'upload', assetId: ident.assetId, filename: built.record.filename,\n                    mime: built.record.mime, bytes: built.record.bytes,\n                }, coalesce);",
            "                const coalesce = 'upload:' + embedId + ':' + key;\n                applySlotValue(inst, embedId, key, {\n                    kind: 'upload', assetId: ident.assetId, filename: built.record.filename,\n                    mime: built.record.mime, bytes: built.record.bytes,\n                }, coalesce);\n                inst.store.dispatch({\n                    type: 'asset/add', assetId: ident.assetId, record: built.record,\n                    meta: { coalesceKey: coalesce },\n                });",
        ]],
    },
    {
        id: 'U9',
        target: 'page',
        harness: UPLOAD,
        why: "removing one reference deletes a record another slot still uses",
        edits: [[
            "        const stillReferenced = NERO.embed.assets.documentAssetIds(inst.store.getDocument())\n            .indexOf(assetId) !== -1;\n        if (!stillReferenced) {\n            inst.store.dispatch({\n                type: 'asset/remove', assetId: assetId, meta: { coalesceKey: coalesce },\n            });\n        }\n        return true;",
            "        const stillReferenced = NERO.embed.assets.documentAssetIds(inst.store.getDocument())\n            .indexOf(assetId) !== -1;\n        if (true) {\n            inst.store.dispatch({\n                type: 'asset/remove', assetId: assetId, meta: { coalesceKey: coalesce },\n            });\n        }\n        return true;",
        ]],
    },
    {
        id: 'U10',
        target: 'page',
        harness: UPLOAD,
        why: "removing a reference DELETES THE BYTES too",
        edits: [[
            "        const stillReferenced = NERO.embed.assets.documentAssetIds(inst.store.getDocument())\n            .indexOf(assetId) !== -1;\n        if (!stillReferenced) {\n            inst.store.dispatch({\n                type: 'asset/remove', assetId: assetId, meta: { coalesceKey: coalesce },\n            });\n        }\n        return true;",
            "        const stillReferenced = NERO.embed.assets.documentAssetIds(inst.store.getDocument())\n            .indexOf(assetId) !== -1;\n        if (!stillReferenced) {\n            inst.store.dispatch({\n                type: 'asset/remove', assetId: assetId, meta: { coalesceKey: coalesce },\n            });\n        }\n        inst.assetStore.remove(assetId);\n        return true;",
        ]],
    },
    {
        id: 'U11',
        target: 'page',
        harness: UPLOAD,
        why: "acceptance is a hard-coded extension test instead of the identity rules",
        edits: [[
            "        const ident = A.identify(buffer, file && file.name);\n        if (!ident || !ident.ok) {",
            "        const ident = A.identify(buffer, file && file.name);\n        const looksLikeAnImage = /\\.(gif|jpe?g|png|webp)$/i.test(String(file && file.name));\n        if (!looksLikeAnImage) {",
        ]],
    },
    {
        id: 'U12',
        target: 'page',
        harness: UPLOAD,
        why: "a hard-coded size limit refuses a file the served limits allow",
        edits: [[
            "        const ident = A.identify(buffer, file && file.name);\n        if (!ident || !ident.ok) {",
            "        const ident = A.identify(buffer, file && file.name);\n        if (file && file.size > 20971520) {\n            setNotice(inst, { tone: 'warn', text: 'That file is too large to attach.' });\n            finishUpload(inst, token);\n            return null;\n        }\n        if (!ident || !ident.ok) {",
        ]],
    },
    {
        id: 'U13',
        target: 'page',
        harness: UPLOAD,
        why: "a failed READ is turned into an empty file and the pipeline continues",
        edits: [[
            "            if (!read.ok) {\n                setNotice(inst, { tone: 'warn', text: 'That file could not be read, so nothing was added.' });\n                finishUpload(inst, token);\n                return null;\n            }",
            "            if (!read.ok) {\n                read = { ok: true, buffer: new Uint8Array(0) };\n            }",
        ]],
    },
    {
        id: 'U14',
        target: 'page',
        harness: UPLOAD,
        why: "a read that lands after the slot changed still overwrites the edit the user made",
        edits: [[
            "            const now = uploadSlot(inst, embedId, key);\n            if (!now || NERO.embed.model.stableStringify(now.value) !== startedWith) {",
            "            const now = uploadSlot(inst, embedId, key);\n            if (!now) {",
        ]],
    },
    {
        id: 'U15',
        target: 'page',
        harness: UPLOAD,
        why: "a read that lands after teardown still mutates the dead document",
        edits: [[
            "        readUpload(inst, request.file).then(function (read) {\n            if (inst.destroyed || inst.uploadToken !== token) return null;\n            const now = uploadSlot(inst, embedId, key);\n            if (!now || NERO.embed.model.stableStringify(now.value) !== startedWith) {",
            "        readUpload(inst, request.file).then(function (read) {\n            if (inst.destroyed || inst.uploadToken !== token) return null;\n            const now = uploadSlot(inst, embedId, key);\n            if (!now) return null;",
        ]],
    },
    {
        id: 'U16',
        target: 'page',
        harness: UPLOAD,
        why: "the pick mints its OWN id instead of using the one the identity rules derived (the same bytes would become two records)",
        // REDEFINED, not deleted: this mutant used to drop the page's own
        // post-teardown guard in uploadPick. Hand-checked against a mutated copy,
        // that is EQUIVALENT — the inspector stops delivering events to a
        // destroyed view, so the page's second guard is defence in depth and
        // removing it alone cannot change behaviour. The reachable form of
        // "work outlives the page" is U15 (a read in flight across teardown),
        // which is caught. The category is therefore stated as what the byte
        // store's id check exists to stop: an invented identity, i.e. a second
        // record for bytes that already have one.
        edits: [[
            "        const ident = A.identify(buffer, file && file.name);\n        if (!ident || !ident.ok) {",
            "        const ident = Object.assign({}, A.identify(buffer, file && file.name), {\n            assetId: 'a_' + Math.random().toString(16).slice(2) });\n        if (!ident || !ident.ok) {",
        ]],
    },
    {
        id: 'U17',
        target: 'inspector',
        harness: UPLOAD,
        why: "the control marks a HEALTHY slot invalid instead of saying nothing",
        edits: [[
            "            const has = node.getAttribute('aria-invalid');\n            if (on && has !== 'true') { node.setAttribute('aria-invalid', 'true'); stats.attrWrites++; }\n            else if (!on && has !== null) { node.removeAttribute('aria-invalid'); stats.attrWrites++; }",
            "            const has = node.getAttribute('aria-invalid');\n            if (on) { node.setAttribute('aria-invalid', 'true'); stats.attrWrites++; }\n            else if (has !== null) { node.setAttribute('aria-invalid', 'false'); stats.attrWrites++; }",
        ]],
    },
    {
        id: 'U18',
        target: 'inspector',
        harness: UPLOAD,
        why: "the state line is never wired to the input",
        edits: [[
            "            input.setAttribute('aria-describedby', stateId);",
            "            /* mutant: no describedby */",
        ]],
    },
    {
        id: 'U19',
        target: 'inspector',
        harness: UPLOAD,
        why: "Remove is offered on a slot with no file",
        edits: [[
            "                setHidden(slot.remove, !state.removable);",
            "                setHidden(slot.remove, false);",
        ]],
    },
    // ── Step 7e: resolution and the files summary ───────────────────
    //
    // The resolver seam has no coverage before 7e, so this block is what keeps
    // the resolution honest: it may read the store's URL cache but never mint on
    // the render path, it follows REFERENCES (never the record map), it fails
    // closed on an ambiguous name, it resolves ahead so typing costs nothing, it
    // repaints only when the answer changed, it never owns, invents or revokes a
    // URL, and the summary counts files, sums only what is known, hides nothing.
    //
    // EQUIVALENCE NOTE (7e, reported like U16): the obvious "drop the destroyed
    // guard" mutant for "resolution after teardown" is NOT reachable — the only
    // input a pass reads is the store's URL cache, and the store clears it when it
    // is destroyed, so a late pass computes the same empty answer. The OUTCOME is
    // asserted behaviourally instead (the 7e harness: no pass after teardown, every
    // URL revoked by the store, the dead mount stays empty), and W14 covers the
    // reachable neighbour of the same idea: a resolver frozen at mount time.
    {
        id: 'W1',
        target: 'page',
        harness: RESOLUTION,
        why: "the resolver answers nothing, so a stored file never reaches the preview",
        edits: [[
            "            return name ? (resolutionFor(inst)[name] || '') : '';",
            "            return name ? '' : '';",
        ]],
    },
    {
        id: 'W2',
        target: 'page',
        harness: RESOLUTION,
        why: "an ambiguous filename fails OPEN: two files sharing one name resolve to whichever was seen first",
        edits: [[
            "            if (entry.ambiguous) return;         // never guess between two files",
            "            if (false) return;                   // never guess between two files",
        ]],
    },
    {
        id: 'W3',
        target: 'page',
        harness: RESOLUTION,
        why: "the resolver echoes the attachment reference instead of the URL the store minted",
        edits: [[
            "            const url = inst.assetStore.cachedUrl(entry.id);\n            if (url) map[name] = url;",
            "            const url = inst.assetStore.cachedUrl(entry.id);\n            if (url) map[name] = 'attachment://' + name;",
        ]],
    },
    {
        id: 'W4',
        target: 'page',
        harness: RESOLUTION,
        why: "the render path MINTS instead of reading the cache (a promise reaches a synchronous resolver)",
        edits: [[
            "            const url = inst.assetStore.cachedUrl(entry.id);\n            if (url) map[name] = url;",
            "            const url = inst.assetStore.urlFor(entry.id, {});\n            if (url) map[name] = url;",
        ]],
    },
    {
        id: 'W5',
        target: 'page',
        harness: RESOLUTION,
        why: "every document change (so every keystroke) runs a resolution pass",
        edits: [[
            "            function () { paintPreview(inst); renderFiles(inst); }",
            "            function () { paintPreview(inst); renderFiles(inst); resolveAssets(inst); }",
        ]],
    },
    {
        id: 'W6',
        target: 'page',
        harness: RESOLUTION,
        why: "the repaint is skipped exactly when the resolution changed",
        edits: [[
            "        const signature = resolutionSignature(resolutionFor(inst));\n        if (signature === inst.resolutionSignature) return false;\n        countResolveRepaint(inst);",
            "        const signature = resolutionSignature(resolutionFor(inst));\n        if (signature !== inst.resolutionSignature) return false;\n        countResolveRepaint(inst);",
        ]],
    },
    {
        id: 'W7',
        target: 'page',
        harness: RESOLUTION,
        why: "the summary counts slots instead of files, so one file in two slots reads as two files",
        edits: [[
            "        const bytes = A.assetBytes(A.assetView(inst.store.getDocument()));",
            "        const bytes = A.assetBytes(A.assetView(inst.store.getDocument()));\n        bytes.count = A.assetView(inst.store.getDocument()).refs.length;",
        ]],
    },
    {
        id: 'W8',
        target: 'page',
        harness: RESOLUTION,
        why: "the summary drops the unmeasured count, so an unmeasured file reads as measured",
        edits: [[
            "        if (unmeasured) parts.push(unmeasured === 1 ? '1 unmeasured' : unmeasured + ' unmeasured');",
            "        if (false) parts.push(unmeasured === 1 ? '1 unmeasured' : unmeasured + ' unmeasured');",
        ]],
    },
    {
        id: 'W9',
        target: 'page',
        harness: RESOLUTION,
        why: "the summary is rewritten on every document change, guard or not",
        edits: [[
            "        const text = parts.join(' · ');\n        if (el.textContent !== text) el.textContent = text;",
            "        const text = parts.join(' · ');\n        el.textContent = text;",
        ]],
    },
    {
        id: 'W10',
        target: 'page',
        harness: RESOLUTION,
        why: "the summary dresses a number in a validation tone, turning a count into a verdict",
        edits: [[
            "        const text = parts.join(' · ');\n        if (el.textContent !== text) el.textContent = text;\n        if (el.hidden) el.hidden = false;",
            "        const text = parts.join(' · ');\n        if (el.textContent !== text) el.textContent = text;\n        el.setAttribute('class', 'mb2-files mb2-tone-danger');\n        if (el.hidden) el.hidden = false;",
        ]],
    },
    {
        id: 'W11',
        target: 'page',
        harness: RESOLUTION,
        why: "the page reaches for createObjectURL and builds its own Blob",
        edits: [[
            "            const url = inst.assetStore.cachedUrl(entry.id);\n            if (url) map[name] = url;",
            "            const url = inst.win.URL.createObjectURL(new inst.win.Blob(['x'], { type: 'image/png' }));\n            if (url) map[name] = url;",
        ]],
    },
    {
        id: 'W12',
        target: 'page',
        harness: RESOLUTION,
        why: "the pass asks the store for a URL it already has (a cache hit where no work was needed)",
        edits: [[
            "            return states[id] === A.FACT_STATES.LOCAL && inst.assetStore.cachedUrl(id) === null;",
            "            return states[id] === A.FACT_STATES.LOCAL;",
        ]],
    },
    {
        id: 'W13',
        target: 'page',
        harness: RESOLUTION,
        why: "the pass resolves the RECORD map instead of the files the document references " +
            "(an unreferenced record is asked about)",
        edits: [[
            "        const wanted = view.ids.filter(function (id) {\n            return states[id] === A.FACT_STATES.LOCAL && inst.assetStore.cachedUrl(id) === null;\n        });",
            "        const wanted = Object.keys(view.records).filter(function (id) {\n            return inst.assetStore.cachedUrl(id) === null;\n        });",
        ]],
    },
    {
        id: 'W14',
        target: 'page',
        harness: RESOLUTION,
        why: "the resolver is a snapshot taken at mount instead of a live lookup (a re-attached file never appears)",
        edits: [[
            "    function resolverFor(inst) {\n        return function resolveImageSrc(raw) {\n            if (inst.destroyed || !inst.assetStore) return '';\n            const name = attachmentFilename(raw);\n            return name ? (resolutionFor(inst)[name] || '') : '';\n        };\n    }",
            "    function resolverFor(inst) {\n        const snapshot = resolutionFor(inst);\n        return function resolveImageSrc(raw) {\n            if (inst.destroyed || !inst.assetStore) return '';\n            const name = attachmentFilename(raw);\n            return name ? (snapshot[name] || '') : '';\n        };\n    }",
        ]],
    },
    {
        id: 'W15',
        target: 'page',
        harness: RESOLUTION,
        why: "an unreferenced file has its URL released mid-session (an undo would flash no image)",
        edits: [[
            "        if (!wanted.length) { applyResolution(inst, null); return null; }",
            "        if (!wanted.length) {\n            Object.keys(view.records).forEach(function (id) {\n                if (view.ids.indexOf(id) === -1) inst.assetStore.release(id);\n            });\n            applyResolution(inst, null);\n            return null;\n        }",
        ]],
    },
    {
        id: 'W16',
        target: 'page',
        harness: RESOLUTION,
        why: "the pass asks the store for bytes its own observation already called gone",
        edits: [[
            "            return states[id] === A.FACT_STATES.LOCAL && inst.assetStore.cachedUrl(id) === null;",
            "            return inst.assetStore.cachedUrl(id) === null;",
        ]],
    },
    {
        id: 'W17',
        target: 'page',
        harness: RESOLUTION,
        why: "the page invents a blob: URL of its own instead of reading the store cache",
        edits: [[
            "            const url = inst.assetStore.cachedUrl(entry.id);\n            if (url) map[name] = url;",
            "            const url = 'blob:' + entry.id;\n            if (url) map[name] = url;",
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
            NERO_VALIDATE_SRC: process.env.NERO_VALIDATE_SRC,
            NERO_ASSETS_SRC: process.env.NERO_ASSETS_SRC,
            NERO_ASSET_STORE_SRC: process.env.NERO_ASSET_STORE_SRC,
            NERO_MODEL_SRC: process.env.NERO_MODEL_SRC,
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
