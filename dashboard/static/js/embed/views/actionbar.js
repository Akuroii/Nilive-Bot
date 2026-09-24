// ═══════════════════════════════════════════════════════════════
// NERO DASHBOARD — embed/views/actionbar.js
// Message Builder v2 — the ACTION half of the status/action bar.
// Phase 1, step 5d-1: the bar skeleton, Undo/Redo and Copy JSON.
//
// WHAT THIS OWNS
//   The buttons inside #mb2-bar-actions and the dialogs those buttons need.
//   Step 5a declared the container and rendered nothing into it; this file is
//   what fills it. 5d-1 ships the three actions that are implemented:
//
//       Undo · Redo · Copy JSON
//
//   Save now (5d-3) and Discard changes (5d-2) are added by the sub-stages that
//   implement them, in the approved order Undo · Redo · Save now · Copy JSON ·
//   Discard changes — ORDER below exists so inserting one of them later is a
//   declaration, not a restructure.
//
// WHAT THIS DELIBERATELY DOES NOT OWN
//   * THE DOCUMENT. There is no document state in this file, not even a copy of
//     one. Every action ends in a store call (undo/redo) or in a pure read of
//     the store's document (Copy JSON). Nothing here builds a document, holds a
//     snapshot, derives "what the document used to be" or mutates anything: the
//     store stays the single source of truth, and the page stays the only place
//     that knows about the draft session.
//   * THE DIRTY FLAG / WRITE QUEUE / GUARD. Those belong to the store and the
//     draft session. This module reads nothing about persistence and decides
//     nothing about it.
//   * THE PREVIEW. This file never calls the renderer. An undo changes the
//     document THROUGH the store, and the preview follows on its own
//     subscription — which is what keeps one renderer and keyed patches.
//   * NOTICES. Results are reported by calling options.onNotice({tone, text});
//     the page writes that into the ONE live region (#mb2-bar-status, owned by
//     statusbar.js). This file never touches that element and never creates a
//     live region of its own.
//
// ENABLED STATE IS REAL, NEVER DECORATIVE
//   Undo/Redo are enabled exactly when the store says the history can move, and
//   `button.disabled` IS the state (the property is a reflected attribute in a
//   browser, so setting the property is the honest operation — no aria-disabled
//   duplicate to drift out of sync). No button is rendered for a feature that
//   does not exist yet, and no button is rendered permanently dead.
//
// DIALOGS (5d-1: the clipboard fallback)
//   A dialog is built next to the buttons, and is: labelled by its own title and
//   described by its own body, modal to assistive technology, a real focus trap
//   (Tab wraps at both ends), closable with Escape or the backdrop, and focus
//   returns to the button that opened it. Opening a second dialog closes the
//   first, so there is never more than one. Its listeners live on the dialog
//   itself, so removing the dialog removes every handler it added.
//
// NOT IN THIS FILE (step 5 scope): validation, limits, counters, persistence,
// assets/uploads, components/actions/roles, publishing/Send, keyboard shortcuts
// (page-level, deferred to 5e), and anything that renders preview markup.
//
// Consumed by: embed/message-builder-page.js
// Tested by:   scripts/test_message_builder_actionbar.js (behaviour),
//              scripts/test_message_builder_page.js §N (page integration),
//              scripts/support/mb_mutants.js (this file is a mutation target).
// ═══════════════════════════════════════════════════════════════
window.NERO = window.NERO || {};
window.NERO.embed = window.NERO.embed || {};
window.NERO.embed.views = window.NERO.embed.views || {};

(function (NERO) {
    'use strict';

    // Declared order (approved): Save now sits between Redo and Copy JSON, and
    // Discard changes is last because it is the destructive one.
    const ORDER = { undo: 10, redo: 20, save: 30, copy: 40, discard: 90 };

    const TITLE_ID = 'mb2-dialog-title';
    const BODY_ID = 'mb2-dialog-body';

    /**
     * The ONE contextual save control, as a pure mapping from the facts the
     * page supplies to what the button says and whether it can act.
     *
     * The facts are: store dirty-ness (the store owns it), the session's
     * persistence snapshot (the session owns it), whether a write is queued,
     * and whether the current failure is RETRYABLE — a question the session
     * answers from the storage adapter's own list, so this file never keeps a
     * second copy of what "retryable" means.
     *
     * The rule that matters most: "Try saving again" is offered ONLY for a
     * failure a retry could actually fix. A missing IndexedDB, an open
     * timeout/error or a failed upgrade cannot be fixed by clicking, so the
     * control stays a disabled "Save now" — never a retry that is known not to
     * work. Pure and exported so the mapping is testable without a bar and
     * cannot drift from what renders.
     */
    function describeSave(view) {
        view = view || {};
        const session = view.session || {};
        const retryable = !!view.retryable;
        const owed = !!view.dirty || !!view.pending;

        if (session.blocked) {
            // The guard is absolute: nothing may be written over the record
            // this page refused to touch, so the control is not offered.
            return {
                state: 'blocked', label: 'Save now', enabled: false,
                title: 'Saving is unavailable — the saved draft is protected',
            };
        }
        if (session.saving) {
            return {
                state: 'saving', label: 'Saving\u2026', enabled: false,
                title: 'A save is in progress',
            };
        }
        if (session.lastError) {
            if (retryable) {
                return {
                    state: 'retry', label: 'Try saving again', enabled: true,
                    title: 'The last save failed — try it again',
                };
            }
            // A failure a retry cannot fix gets no retry affordance at all.
            return {
                state: 'unavailable', label: 'Save now', enabled: false,
                title: 'Saving is unavailable — ' + reasonPhrase(session.lastError),
            };
        }
        if (session.degraded && !retryable) {
            // Storage is known to be unusable: an enabled button would be an
            // affordance that cannot work.
            return {
                state: 'unavailable', label: 'Save now', enabled: false,
                title: 'Saving is unavailable — draft storage cannot be opened',
            };
        }
        if (owed) {
            return {
                state: 'dirty', label: 'Save now', enabled: true,
                title: 'Save this draft now',
            };
        }
        if (session.writes > 0) {
            return {
                state: 'saved', label: 'Saved', enabled: false,
                title: 'This draft is stored',
            };
        }
        return {
            state: 'clean', label: 'Save now', enabled: false,
            title: 'Nothing to save yet',
        };
    }

    /** The failure, in words that fit in a tooltip. Never invents a reason. */
    function reasonPhrase(lastError) {
        const reason = lastError && lastError.reason ? String(lastError.reason) : 'the last save failed';
        if (reason === 'not-serializable') return 'this document cannot be turned into JSON';
        return reason.replace(/-/g, ' ');
    }

    function create(options) {
        options = options || {};
        const doc = options.document;
        const store = options.store;
        const mount = options.mount;
        const model = options.model || (NERO.embed.model || null);
        const onNotice = typeof options.onNotice === 'function' ? options.onNotice : null;
        // What "discard" MEANS is not this file's business. The page owns the
        // draft session, so it hands the bar two functions: is there anything to
        // discard, and do it. The bar owns the button, the confirmation and the
        // dialog — never the restore itself, and never a persistence call.
        const discard = options.discard || null;
        const canDiscard = discard && typeof discard.available === 'function' ? discard.available : null;
        const runDiscard = discard && typeof discard.perform === 'function' ? discard.perform : null;
        // Saving is the page's to perform and nobody else's: the bar gets one
        // function that ends in the session's own saveNow(), exactly as it gets
        // one function for discard. It is never handed the session, a storage
        // handle, or anything it could use to write on its own initiative.
        const runSave = options.save && typeof options.save.perform === 'function'
            ? options.save.perform : null;

        if (!doc || typeof doc.createElement !== 'function') {
            throw new TypeError('actionbar.create needs options.document');
        }
        if (!store || typeof store.subscribe !== 'function' ||
            typeof store.canUndo !== 'function' || typeof store.undo !== 'function') {
            throw new TypeError('actionbar.create needs options.store');
        }
        if (!mount || typeof mount.appendChild !== 'function') {
            throw new TypeError('actionbar.create needs options.mount (#mb2-bar-actions)');
        }
        if (!model || typeof model.toDiscordPayload !== 'function') {
            throw new TypeError('actionbar.create needs the document model (embed/model.js)');
        }

        const stats = {
            renders: 0,
            buttonsCreated: 0,
            stateWrites: 0,        // disabled/aria writes — change-guarded, so a keystroke costs none
            undos: 0,
            redos: 0,
            copyAttempts: 0,
            copied: 0,
            copyFailures: 0,
            savePresses: 0,
            saves: 0,
            saveLabels: 0,
            discardPrompts: 0,
            discards: 0,
            confirms: 0,
            dialogsOpened: 0,
            dialogsClosed: 0,
            focusMoves: 0,
        };

        const buttons = {};        // key -> { node, order }
        const unsubs = [];
        let dialog = null;         // the open dialog, or null
        let copying = false;       // a clipboard write is in flight
        let destroyed = false;

        // ── DOM plumbing (write only when the value differs) ──────
        function el(tag, className, text) {
            const node = doc.createElement(tag);
            if (className) node.className = className;
            if (text !== undefined && text !== null) node.textContent = String(text);
            return node;
        }

        function attr(node, name) {
            return node && typeof node.getAttribute === 'function' ? node.getAttribute(name) : null;
        }

        function prevent(event) {
            if (event && typeof event.preventDefault === 'function') event.preventDefault();
        }

        function focusNode(node) {
            if (!node || typeof node.focus !== 'function') return false;
            stats.focusMoves++;
            node.focus();
            return true;
        }

        function notify(tone, text) {
            if (!onNotice) return;
            try { onNotice({ tone: tone, text: text }); } catch (e) { /* a notice must never break an action */ }
        }

        function errorText(err) {
            if (!err) return 'unknown error';
            const message = err.message ? String(err.message) : String(err);
            return message || 'unknown error';
        }

        // ── Buttons ──────────────────────────────────────────────
        /** DOM order of the mounted buttons (read back, never assumed). */
        function keys() {
            const out = [];
            const children = mount.children;
            for (let i = 0; i < children.length; i++) {
                const key = attr(children[i], 'data-mb2-action');
                if (key) out.push(key);
            }
            return out;
        }

        /**
         * Insert by ORDER rather than by call order: that is what lets 5d-2/5d-3
         * add Discard/Save now without rewriting where the existing buttons go.
         */
        function addButton(spec) {
            const node = el('button', 'mb2-bar-btn', spec.label);
            node.setAttribute('type', 'button');
            node.setAttribute('data-mb2-action', spec.key);
            // The accessible name keeps the visible label as a substring, so
            // voice control ("click Copy JSON") still matches (WCAG 2.5.3).
            node.setAttribute('aria-label', spec.ariaLabel || spec.label);
            node.disabled = false;
            stats.buttonsCreated++;

            const children = mount.children;
            let ref = null;
            for (let i = 0; i < children.length; i++) {
                const child = children[i];
                for (const key in buttons) {
                    if (buttons[key].node === child && buttons[key].order > spec.order) { ref = child; break; }
                }
                if (ref) break;
            }
            if (ref && typeof mount.insertBefore === 'function') mount.insertBefore(node, ref);
            else mount.appendChild(node);
            buttons[spec.key] = { node: node, order: spec.order };
            return node;
        }

        function setDisabled(node, next) {
            const value = !!next;
            if (node && node.disabled !== value) {
                node.disabled = value;
                stats.stateWrites++;
            }
        }

        /**
         * Runs on every store notification, so it writes nothing unless the two
         * booleans actually changed — a keystroke that does not move the ends of
         * the history costs zero DOM writes here.
         */
        function render() {
            if (destroyed) return false;
            stats.renders++;
            setDisabled(buttons.undo.node, !store.canUndo());
            setDisabled(buttons.redo.node, !store.canRedo());
            setDisabled(buttons.copy.node, copying);
            // Availability comes from the page (it owns the saved baseline); a
            // capability that is not wired at all renders no button.
            if (buttons.discard && canDiscard) setDisabled(buttons.discard.node, !canDiscard());
            return true;
        }

        /**
         * The save control, rendered from facts the PAGE supplies — because the
         * page is the only place both owners (store dirty-ness, session
         * persistence) are observed together, and because deriving them here
         * would mean hashing the document a second and third time per keystroke
         * for no reason. Called by the page on every session/store change; every
         * write below is change-guarded, so a keystroke that does not move the
         * state costs nothing.
         */
        function renderSave(view) {
            if (destroyed || !buttons.save) return false;
            stats.renders++;
            const spec = describeSave(view);
            const node = buttons.save.node;
            if (node.textContent !== spec.label) {
                node.textContent = spec.label;
                stats.saveLabels++;
            }
            if (attr(node, 'data-mb2-save-state') !== spec.state) {
                node.setAttribute('data-mb2-save-state', spec.state);
                stats.stateWrites++;
            }
            if (attr(node, 'title') !== spec.title) {
                // The tooltip is also the accessible DESCRIPTION of a control
                // that is often disabled — which is exactly when a user needs
                // to be told why.
                node.setAttribute('title', spec.title);
                node.setAttribute('aria-label', spec.label);
                stats.stateWrites++;
            }
            setDisabled(node, !spec.enabled);
            return true;
        }

        /**
         * Save now. The bar owns the button, not the write: this calls the one
         * function the page handed it, which ends in the session's own
         * saveNow() — the same write the idle timer performs, so there is still
         * exactly one persistence path and no way for a click to force, bypass
         * or duplicate one. The result is not interpreted here: the session
         * notifies its observers and the page re-renders from the new facts.
         */
        function pressSave() {
            if (destroyed || !runSave) return false;
            stats.savePresses++;
            // A disabled control can still receive a synthetic click, so the
            // guard lives in the handler too — a click can never start a write
            // the bar is currently saying is unavailable.
            if (buttons.save && buttons.save.node.disabled) return false;
            stats.saves++;
            try {
                const inflight = runSave();
                if (inflight && typeof inflight.catch === 'function') {
                    // The session reports its own failures in its state; this
                    // only stops an unanswered promise from being unhandled.
                    inflight.catch(function () { });
                }
            } catch (e) { /* a save that throws must not break the bar */ }
            return true;
        }

        // ── Dialogs ──────────────────────────────────────────────
        /**
         * Build one dialog. The caller may append its own content into
         * `dialog.content` (which sits between the body text and the buttons)
         * and then call dialog.startFocus() so the first element the user needs
         * has focus.
         */
        function openDialog(spec) {
            closeDialog('replaced');                 // never more than one
            const overlay = el('div', 'mb2-dialog-overlay');
            overlay.setAttribute('data-mb2-dialog', spec.key);

            const panel = el('div', 'mb2-dialog');
            panel.setAttribute('role', 'dialog');
            panel.setAttribute('aria-modal', 'true');
            panel.setAttribute('aria-labelledby', TITLE_ID);
            panel.setAttribute('aria-describedby', BODY_ID);

            const title = el('h3', 'mb2-dialog-title', spec.title);
            title.setAttribute('id', TITLE_ID);
            const body = el('p', 'mb2-dialog-body', spec.body);
            body.setAttribute('id', BODY_ID);
            const content = el('div', 'mb2-dialog-content');
            const actions = el('div', 'mb2-dialog-actions');
            // The SAFE control comes first: it is what startFocus() lands on, so
            // opening a dialog never puts the destructive action under the
            // pointer or under Enter.
            const close = el('button', 'mb2-dialog-btn', spec.cancelLabel || 'Close');
            close.setAttribute('type', 'button');
            close.setAttribute('data-mb2-dialog-action', 'close');
            actions.appendChild(close);
            if (spec.confirm) {
                const go = el('button', 'mb2-dialog-btn mb2-dialog-btn-danger', spec.confirm.label);
                go.setAttribute('type', 'button');
                go.setAttribute('data-mb2-dialog-action', spec.confirm.action || 'confirm');
                actions.appendChild(go);
            }

            panel.appendChild(title);
            panel.appendChild(body);
            panel.appendChild(content);
            panel.appendChild(actions);
            overlay.appendChild(panel);

            // The dialog's own listeners: removing the overlay removes them all.
            overlay.addEventListener('click', onDialogClick);
            overlay.addEventListener('keydown', onDialogKeydown);
            mount.appendChild(overlay);

            const handlers = {};
            if (spec.confirm && typeof spec.confirm.run === 'function') {
                handlers[spec.confirm.action || 'confirm'] = spec.confirm.run;
            }
            dialog = {
                key: spec.key,
                overlay: overlay,
                panel: panel,
                title: title,
                body: body,
                content: content,
                actions: actions,
                close: close,
                confirm: spec.confirm ? actions.children[actions.children.length - 1] : null,
                handlers: handlers,
                invoker: spec.invoker || null,
            };
            stats.dialogsOpened++;
            return dialog;
        }

        /**
         * What Tab can reach inside the open dialog, in DOCUMENT order — built
         * by an explicit walk rather than by one selector list, so the order the
         * focus trap wraps around is the order the user sees, everywhere.
         */
        const FOCUSABLE = ['BUTTON', 'TEXTAREA', 'INPUT', 'SELECT', 'A'];
        function focusables() {
            if (!dialog) return [];
            const out = [];
            (function walk(node) {
                const children = node.children;
                for (let i = 0; i < children.length; i++) {
                    const child = children[i];
                    const tag = String(child.tagName || '').toUpperCase();
                    if (FOCUSABLE.indexOf(tag) !== -1 && !child.disabled) {
                        if (tag !== 'A' || attr(child, 'href')) out.push(child);
                    }
                    walk(child);
                }
            })(dialog.panel);
            return out;
        }

        function startFocus() {
            if (!dialog) return false;
            const list = focusables();
            return list.length ? focusNode(list[0]) : false;
        }

        function closeDialog(reason) {
            if (!dialog) return false;
            const closing = dialog;
            dialog = null;
            if (closing.overlay.parentNode) closing.overlay.parentNode.removeChild(closing.overlay);
            stats.dialogsClosed++;
            // Focus goes back where it came from — but only while the bar is
            // still alive: after destroy() the invoking button may be gone.
            if (!destroyed && closing.invoker && closing.invoker.parentNode) focusNode(closing.invoker);
            return true;
        }

        function onDialogClick(event) {
            if (destroyed || !dialog || !event) return;
            if (event.target === dialog.overlay) { closeDialog('backdrop'); return; }
            const action = attr(event.target, 'data-mb2-dialog-action');
            if (!action) return;
            if (action === 'close') { closeDialog('close'); return; }
            const handler = dialog.handlers[action];
            if (!handler) return;
            // Confirmations run AFTER the dialog is gone: the bar must not hold
            // an open modal while the page changes state underneath it.
            closeDialog('confirm');
            stats.confirms++;
            handler();
        }

        function onDialogKeydown(event) {
            if (destroyed || !dialog || !event) return;
            const key = event.key;
            if (key === 'Escape' || key === 'Esc') { prevent(event); closeDialog('escape'); return; }
            if (key !== 'Tab') return;
            const list = focusables();
            if (!list.length) return;
            const index = list.indexOf(event.target);
            if (index === -1) return;            // focus is outside the dialog: leave the browser alone
            const last = list.length - 1;
            if (!event.shiftKey && index === last) { prevent(event); focusNode(list[0]); }
            else if (event.shiftKey && index === 0) { prevent(event); focusNode(list[last]); }
        }

        // ── Undo / Redo ──────────────────────────────────────────
        function undo() {
            if (destroyed || !store.canUndo()) return false;   // a disabled button is not an action
            stats.undos++;
            store.undo();
            return true;
        }

        function redo() {
            if (destroyed || !store.canRedo()) return false;
            stats.redos++;
            store.redo();
            return true;
        }

        // ── Copy JSON ────────────────────────────────────────────
        /**
         * A pure read: the payload comes from the model, the document from the
         * store, and the JSON is pretty-printed because this ends up on a
         * clipboard for a human to paste. (Canonical/deterministic
         * serialization is an internal model concern — stableStringify — and is
         * NOT what the user is handed here.)
         */
        function payloadText() {
            return JSON.stringify(model.toDiscordPayload(store.getDocument()), null, 2);
        }

        function clipboardApi() {
            const nav = options.navigator || null;
            return nav && nav.clipboard ? nav.clipboard : null;
        }

        function copyJson() {
            if (destroyed || copying) return false;
            stats.copyAttempts++;
            let text;
            try {
                text = payloadText();
            } catch (err) {
                notify('danger', 'The message could not be turned into JSON (' + errorText(err) + ').');
                return false;
            }
            const clipboard = clipboardApi();
            if (!clipboard || typeof clipboard.writeText !== 'function') {
                stats.copyFailures++;
                openCopyFallback(text, 'this browser does not offer the clipboard API');
                return false;
            }
            let result;
            try {
                result = clipboard.writeText(text);
            } catch (err) {
                stats.copyFailures++;
                openCopyFallback(text, errorText(err));
                return false;
            }
            setCopying(true);
            Promise.resolve(result).then(function () {
                if (destroyed) return;
                setCopying(false);
                stats.copied++;
                notify('ok', 'JSON copied to the clipboard.');
            }, function (err) {
                if (destroyed) return;
                setCopying(false);
                stats.copyFailures++;
                openCopyFallback(text, errorText(err));
            });
            return true;
        }

        function setCopying(next) {
            const value = !!next;
            if (copying === value) return;
            copying = value;
            setDisabled(buttons.copy.node, copying);
        }

        /**
         * The clipboard is not the only way to get the JSON out, so a failed
         * copy is not a dead end: the payload is shown in a read-only textarea
         * (already focused, so Ctrl/Cmd+C works immediately) and the reason is
         * stated in plain words.
         */
        function openCopyFallback(text, reason) {
            const open = openDialog({
                key: 'copy-fallback',
                title: 'Copy the JSON manually',
                body: 'The clipboard could not be used (' + reason +
                      '). The JSON is below — select it and press Ctrl+C, or Cmd+C on a Mac.',
                invoker: buttons.copy ? buttons.copy.node : null,
            });
            const area = el('textarea', 'mb2-dialog-json');
            area.setAttribute('id', 'mb2-dialog-json');
            area.setAttribute('readonly', 'readonly');
            area.setAttribute('rows', '12');
            area.setAttribute('spellcheck', 'false');
            area.setAttribute('aria-label', 'The message JSON');
            area.value = text;
            open.content.appendChild(area);
            startFocus();
            notify('warn', 'The clipboard is unavailable — the JSON is in the dialog so you can copy it by hand.');
            return open;
        }

        // ── Discard changes ──────────────────────────────────────
        /**
         * The one irreversible-ish action in the bar, so it asks first. The
         * dialog says what will happen and what will NOT (the stored copy is
         * not deleted), the safe control is focused, and Escape or the backdrop
         * cancels without running anything.
         */
        function confirmDiscard() {
            if (destroyed || !runDiscard) return false;
            if (canDiscard && !canDiscard()) return false;      // a disabled button is not an action
            stats.discardPrompts++;
            openDialog({
                key: 'discard',
                title: 'Discard changes?',
                body: 'The message goes back to the last saved version. Anything typed since then ' +
                      'is lost. The saved draft itself is not deleted.',
                cancelLabel: 'Keep editing',
                invoker: buttons.discard ? buttons.discard.node : null,
                confirm: {
                    label: 'Discard changes',
                    run: function () {
                        // Re-checked at the moment it would run: a save can
                        // confirm itself while the dialog is open, and then
                        // there is nothing left to discard.
                        if (canDiscard && !canDiscard()) return false;
                        stats.discards++;
                        runDiscard();
                        return true;
                    },
                },
            });
            startFocus();       // the cancel button — never the destructive one
            return true;
        }

        // ── Wiring ───────────────────────────────────────────────
        function onMountClick(event) {
            if (destroyed || !event) return;
            const key = attr(event.target, 'data-mb2-action');
            if (key === 'undo') undo();
            else if (key === 'redo') redo();
            else if (key === 'copy') copyJson();
            else if (key === 'save') pressSave();
            else if (key === 'discard') confirmDiscard();
        }

        addButton({ key: 'undo', order: ORDER.undo, label: 'Undo', ariaLabel: 'Undo the last change' });
        addButton({ key: 'redo', order: ORDER.redo, label: 'Redo', ariaLabel: 'Redo the last undone change' });
        if (runSave) {
            // Starts disabled and says so: until the page has supplied the
            // facts, the bar does not know whether there is anything to save,
            // and a control that has not been told anything must not claim it
            // can act.
            const saveButton = addButton({
                key: 'save', order: ORDER.save, label: 'Save now',
                ariaLabel: 'Save now',
            });
            saveButton.disabled = true;
        }
        addButton({ key: 'copy', order: ORDER.copy, label: 'Copy JSON', ariaLabel: 'Copy JSON to the clipboard' });
        if (runDiscard) {
            addButton({
                key: 'discard', order: ORDER.discard, label: 'Discard changes',
                ariaLabel: 'Discard changes and go back to the last saved version',
            });
        }

        mount.addEventListener('click', onMountClick);
        // Coarse on purpose: the two booleans are derived from the store's
        // history, so any store event is a reason to re-check them, and the
        // writes themselves are change-guarded (a keystroke writes nothing).
        unsubs.push(store.subscribe(function () { render(); }));
        render();

        function destroy() {
            if (destroyed) return false;
            destroyed = true;
            closeDialog('destroy');
            unsubs.splice(0).forEach(function (off) {
                try { off(); } catch (e) { /* an unsubscribe must never block teardown */ }
            });
            if (typeof mount.removeEventListener === 'function') {
                mount.removeEventListener('click', onMountClick);
            }
            for (const key in buttons) {
                const node = buttons[key].node;
                if (node.parentNode) node.parentNode.removeChild(node);
            }
            return true;
        }

        return {
            ORDER: ORDER,
            render: render,
            /** Re-check the derived state (the page calls this on session changes). */
            refresh: function () { return render(); },
            /** The save control, from facts the page owns (see renderSave). */
            renderSave: renderSave,
            saveState: function (view) { return describeSave(view); },
            destroy: destroy,
            /** The mounted button for `key` (null when it does not exist yet). */
            button: function (key) { return buttons[key] ? buttons[key].node : null; },
            /** Keys in DOM order — read back from the mount, never assumed. */
            keys: keys,
            /** The open dialog (a test/debug seam, like drafts.storage()). */
            dialog: function () {
                if (!dialog) return null;
                return {
                    key: dialog.key,
                    overlay: dialog.overlay,
                    panel: dialog.panel,
                    title: dialog.title,
                    body: dialog.body,
                    content: dialog.content,
                    actions: dialog.actions,
                    close: dialog.close,
                    confirm: dialog.confirm,
                };
            },
            stats: function () { return Object.assign({}, stats); },
        };
    }

    NERO.embed.views.actionbar = {
        create: create,
        ORDER: ORDER,
        describeSave: describeSave,
    };
})(window.NERO);
