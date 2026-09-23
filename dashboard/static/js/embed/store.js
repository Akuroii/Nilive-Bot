/* ═══════════════════════════════════════════════════════════════
   Message Builder v2 — the store.

   Phase 1, step 1. The only thing that may change the document, and
   the only thing that tells the rest of the page *what* changed.

   Three problems this solves, all of them v1 bugs by construction
   rather than by accident:

   1. ONE WRITER. v1 mutates `state.embeds[i].title = …` from a dozen
      event handlers and then calls `renderPreview()`; nothing can say
      which nodes were affected, so the preview rebuilds everything.
      Here a change arrives as an action, a reducer produces a new
      document with structural sharing, and every untouched embed and
      field keeps its object identity — which is precisely the
      information the patching preview needs.

   2. NARROW NOTIFICATION. `subscribe(selector, fn)` fires only when
      the selector's result changes identity. The rail subscribes to
      structure, the inspector to its selected node, the preview to the
      payload input, the status line to save state. Typing therefore
      cannot wake the rail or the inspector, which is what makes
      "typing performs zero editor renders" a structural property
      instead of a performance hope.

   3. HISTORY + DIRTY in one place, on document slices only: UI state
      (selection, open panels) is deliberately not undoable, and the
      dirty flag compares the current document hash with the hash of
      the last saved/loaded document — no scattered `dirty = true`.

   NO DOM, NO STORAGE, NO TIMERS that this file owns: persistence is
   scheduled by the caller through the injected scheduler, which is
   also how the tests stay synchronous.

   Consumed by: the v2 page module (phase 1 steps 5+), the tests.
   Tested by: scripts/test_message_model.js.
   ═══════════════════════════════════════════════════════════════ */
window.NERO = window.NERO || {};
window.NERO.embed = window.NERO.embed || {};

(function (NERO) {
    'use strict';

    const model = NERO.embed.model;

    /**
     * createStore({
     *   document,                       // the normalized MessageDocument
     *   ui,                             // ephemeral UI slice (not undoable)
     *   reducers,                       // { 'embed/setTitle': (state, action) => partial|null, … }
     *   historyLimit: 60,
     *   scheduler,                      // { setTimeout, clearTimeout } — injectable for tests
     *   idleMs: 1500,
     *   onChange: fn(document, action)  // optional observer (drafts, later)
     * })
     */
    function createStore(options) {
        options = options || {};
        if (!model) throw new Error('embed/model.js must load before embed/store.js');

        const reducers = options.reducers || {};
        const historyLimit = options.historyLimit || 60;
        const scheduler = options.scheduler || {
            setTimeout: (fn, ms) => setTimeout(fn, ms),
            clearTimeout: (id) => clearTimeout(id),
        };
        const idleMs = options.idleMs == null ? 1500 : options.idleMs;
        const now = typeof options.now === 'function' ? options.now : Date.now;

        let state = {
            document: options.document || model.blankMessageDocument(),
            ui: Object.assign({ issues: [], selectedNodeId: null, mode: 'embeds' }, options.ui || {}),
        };

        let savedHash = model.hashDocument(state.document);
        let history = [{ hash: savedHash, document: model.cloneDocument(state.document) }];
        let historyIndex = 0;
        let dispatching = false;        // inside a reducer
        let notifying = false;          // inside a notification pass
        let destroyed = false;
        let idleHandle = null;
        const queue = [];               // dispatches issued by subscribers

        const subscriptions = [];       // { selector, fn, last }
        const listeners = [];           // fn(state, action) — coarse observers

        // ── Subscription plumbing ─────────────────────────────────
        function subscribe(selector, fn) {
            if (typeof selector === 'function' && fn === undefined) {
                listeners.push(selector);
                return function unsubscribe() {
                    const i = listeners.indexOf(selector);
                    if (i !== -1) listeners.splice(i, 1);
                };
            }
            const entry = { selector: selector, fn: fn, last: selector(state) };
            subscriptions.push(entry);
            return function unsubscribe() {
                const i = subscriptions.indexOf(entry);
                if (i !== -1) subscriptions.splice(i, 1);
            };
        }

        function select(selector) { return selector(state); }

        function notify(action) {
            // One pass, deduplicated: a subscriber registered twice runs
            // once, and a subscriber whose slice is unchanged is skipped.
            const seen = [];
            for (let i = 0; i < subscriptions.length; i++) {
                const entry = subscriptions[i];
                if (seen.indexOf(entry) !== -1) continue;
                seen.push(entry);
                const next = entry.selector(state);
                if (next !== entry.last) {
                    const prev = entry.last;
                    entry.last = next;
                    entry.fn(next, prev, action);
                }
            }
            for (let i = 0; i < listeners.length; i++) listeners[i](state, action);
        }

        // ── History ───────────────────────────────────────────────
        function pushHistory(action) {
            if (action && action.meta && action.meta.history === false) return;
            const hash = model.hashDocument(state.document);

            // Coalescing: a typing burst is ONE undo step. The reducer
            // declares the key ("content:emb_1:title"); while the key is
            // unchanged and the burst is fresh, the existing entry is
            // replaced instead of appended.
            const key = action && action.meta && action.meta.coalesceKey;
            if (key && history.length) {
                const top = history[historyIndex];
                const fresh = top && top.coalesceKey === key &&
                    (now() - (top.at || 0)) < 1200;
                if (fresh) {
                    top.document = model.cloneDocument(state.document);
                    top.hash = hash;
                    top.at = now();
                    return;
                }
            }

            if (history[historyIndex] && history[historyIndex].hash === hash) return;
            history = history.slice(0, historyIndex + 1);
            history.push({
                hash: hash,
                document: model.cloneDocument(state.document),
                coalesceKey: key || null,
                at: now(),
            });
            if (history.length > historyLimit + 1) history = history.slice(history.length - (historyLimit + 1));
            historyIndex = history.length - 1;
        }

        // ── Dispatch ──────────────────────────────────────────────
        /**
         * Two rules, both deliberate:
         *   * a REDUCER must be pure — dispatching from inside one throws,
         *     because the document would be halfway through a change;
         *   * a SUBSCRIBER may dispatch (the validation pass will want to
         *     publish its issue list after a document change), and those
         *     actions are QUEUED and applied in order once the current
         *     notification pass has finished. Deterministic, no recursion,
         *     no half-updated listeners.
         */
        function dispatch(action) {
            if (destroyed) return state;
            if (!action || !action.type) throw new Error('dispatch needs { type }');
            if (dispatching) throw new Error('re-entrant dispatch from a reducer: reducers must be pure');
            if (notifying) { queue.push(action); return state; }

            let current = action;
            while (current) {
                applyOne(current);
                current = queue.length ? queue.shift() : null;
            }
            return state;
        }

        function applyOne(action) {
            const before = state;
            let next = state;
            const reducer = reducers[action.type];

            dispatching = true;
            try {
                if (reducer) {
                    const partial = reducer(state, action);
                    if (partial) next = Object.assign({}, state, partial);
                }
            } finally {
                dispatching = false;
            }
            if (next === before) return;            // unknown action or a no-op reducer

            state = next;
            if (state.document !== before.document) {
                pushHistory(action);
                if (typeof options.onChange === 'function') options.onChange(state.document, action);
            }

            notifying = true;
            try {
                notify(action);
            } finally {
                notifying = false;
            }
        }

        /**
         * Apply several actions as ONE undo step (e.g. load a document and
         * reset the selection). Each action still notifies its subscribers,
         * so a later-phase caller can watch partial progress.
         */
        function batch(actions) {
            const list = Array.isArray(actions) ? actions : [];
            let out = state;
            list.forEach((action, i) => {
                const last = i === list.length - 1;
                out = dispatch(Object.assign({}, action, {
                    meta: Object.assign({}, action.meta, { history: last }),
                }));
            });
            return out;
        }

        // ── Undo / redo (document only) ───────────────────────────
        function undo() {
            if (historyIndex <= 0) return false;
            historyIndex -= 1;
            state = Object.assign({}, state, { document: model.cloneDocument(history[historyIndex].document) });
            notify({ type: '@history/undo' });
            return true;
        }

        function redo() {
            if (historyIndex >= history.length - 1) return false;
            historyIndex += 1;
            state = Object.assign({}, state, { document: model.cloneDocument(history[historyIndex].document) });
            notify({ type: '@history/redo' });
            return true;
        }

        function canUndo() { return historyIndex > 0; }
        function canRedo() { return historyIndex < history.length - 1; }
        function historyDepth() { return { size: history.length, index: historyIndex }; }

        // ── Save state / dirty tracking ───────────────────────────
        function markSaved(document) {
            const target = document || state.document;
            savedHash = model.hashDocument(target);
            if (document) state = Object.assign({}, state, { document: document });
            notify({ type: '@save/mark' });
        }

        function isDirty() { return model.hashDocument(state.document) !== savedHash; }
        function savedDocumentHash() { return savedHash; }

        // ── Idle scheduling (the draft writer's hook, step 4) ─────
        // Not a timer owner: the caller passes the callback, and the
        // store only guarantees "after the burst, once".
        function scheduleIdle(fn) {
            if (idleHandle !== null) scheduler.clearTimeout(idleHandle);
            idleHandle = scheduler.setTimeout(function () {
                idleHandle = null;
                fn();
            }, idleMs);
        }

        function flushIdle() {
            if (idleHandle !== null) {
                scheduler.clearTimeout(idleHandle);
                idleHandle = null;
                return true;
            }
            return false;
        }

        function destroy() {
            destroyed = true;
            flushIdle();
            subscriptions.length = 0;
            listeners.length = 0;
        }

        return {
            getState: () => state,
            getDocument: () => state.document,
            getUi: () => state.ui,
            dispatch: dispatch,
            batch: batch,
            subscribe: subscribe,
            select: select,
            undo: undo,
            redo: redo,
            canUndo: canUndo,
            canRedo: canRedo,
            historyDepth: historyDepth,
            markSaved: markSaved,
            isDirty: isDirty,
            savedDocumentHash: savedDocumentHash,
            scheduleIdle: scheduleIdle,
            flushIdle: flushIdle,
            destroy: destroy,
            _subscriberCounts: () => ({ selectors: subscriptions.length, listeners: listeners.length }),
        };
    }

    // ── The reducer table the v2 page uses ────────────────────────
    // One entry per editor intent, each delegating to a pure model patch.
    // Actions carry ids, never indexes; `meta.coalesceKey`/`history`
    // control undo granularity.
    function createReducers(m) {
        m = m || model;
        return {
            'content/set': (state, a) => ({ document: m.setContent(state.document, a.text) }),
            'embed/set': (state, a) => ({ document: m.setEmbedFields(state.document, a.embedId, a.patch) }),
            'embed/setText': (state, a) => ({ document: m.setEmbedText(state.document, a.embedId, a.key, a.value) }),
            'embed/setColor': (state, a) => ({ document: m.setColor(state.document, a.embedId, a.color) }),
            'embed/setAuthor': (state, a) => ({ document: m.setAuthor(state.document, a.embedId, a.patch) }),
            'embed/setFooter': (state, a) => ({ document: m.setFooter(state.document, a.embedId, a.patch) }),
            'embed/setMedia': (state, a) => ({ document: m.setMedia(state.document, a.embedId, a.slot, a.value) }),
            'embed/add': (state, a) => ({ document: m.addEmbed(state.document, a) }),
            'embed/remove': (state, a) => ({ document: m.removeEmbed(state.document, a.embedId) }),
            'embed/move': (state, a) => ({ document: m.moveEmbed(state.document, a.embedId, a.delta) }),
            'embed/duplicate': (state, a) => ({ document: m.duplicateEmbed(state.document, a.embedId) }),
            'field/add': (state, a) => ({ document: m.addField(state.document, a.embedId) }),
            'field/remove': (state, a) => ({ document: m.removeField(state.document, a.embedId, a.fieldId) }),
            'field/move': (state, a) => ({ document: m.moveField(state.document, a.embedId, a.fieldId, a.delta) }),
            'field/set': (state, a) => ({ document: m.setField(state.document, a.embedId, a.fieldId, a.patch) }),
            'ui/selectNode': (state, a) => (state.ui.selectedNodeId === a.nodeId
                ? null : { ui: Object.assign({}, state.ui, { selectedNodeId: a.nodeId }) }),
            'ui/setMode': (state, a) => (state.ui.mode === a.mode
                ? null : { ui: Object.assign({}, state.ui, { mode: a.mode }) }),
            'ui/setIssues': (state, a) => ({ ui: Object.assign({}, state.ui, { issues: a.issues || [] }) }),
            'document/load': (state, a) => ({ document: m.normalizeDocument(a.document) }),
        };
    }

    NERO.embed.store = {
        createStore: createStore,
        createReducers: createReducers,
    };
})(window.NERO);
