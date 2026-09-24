// ═══════════════════════════════════════════════════════════════
// NERO DASHBOARD — embed/message-builder-page.js
// Message Builder v2 — phase 1, step 5a: the page module.
//
// It wires the already-approved foundations together and NOTHING else:
//
//      embed/model.js            normalized MessageDocument (source of truth)
//      embed/store.js            document + history + selection + dirty flag
//      embed/discord-markdown.js the renderer (used BY the preview, not here)
//      embed/preview.js          the differential preview engine
//      embed/drafts.js           the persistence boundary (draft session)
//      embed/views/statusbar.js  the status half of the status/action bar
//      embed/views/actionbar.js  the action half (buttons + their dialogs)
//
// THE THREE RULES THIS FILE EXISTS TO KEEP
//
//  1. ONE SOURCE OF TRUTH. The page holds no document state. It dispatches
//     actions at the store and renders what the store and the session report.
//     Loading a draft means "the loaded document becomes the store's document",
//     never "the page remembers a document of its own".
//
//  2. THE PREVIEW IS THE ONLY RENDERER. It is created once per mount and then
//     updated with updateDocument(); nothing here (and nothing that will be
//     added in 5b/5c/5d) builds preview markup by hand. That is what keeps
//     typing from rebuilding nodes and images.
//
//  3. LOADING IS NOT EDITING. A loaded draft is canonicalized into the store
//     with `history: false` and immediately marked saved: no undo entry, no
//     dirty flag, no scheduled write, no extra paint, and the payload the store
//     holds stays byte-equivalent to the payload that was persisted.
//
// BOOT ORDER (each step matters, and the harness asserts each one)
//     paint shell → create statusbar → create store → create rail →
//     create inspector → create actionbar → create session →
//     subscribe → session.attach(store)  [BEFORE load, see below] →
//     session.bindLifecycle(window) → render status → resume() [async].
//
//     attach() before load(): attach() clears the session's saved-hash when it
//     attaches to a store it believes is clean. Attaching first means a draft
//     that is then loaded keeps the hash it was loaded with; attaching after
//     would make the freshly loaded draft look like an unsaved edit and schedule
//     a write for content that is already on disk.
//
// STORAGE SAFETY (the invariant that must survive every failure mode)
//     A storage failure is NEVER "there is no draft". If the first read cannot
//     be made, the page says so, keeps the document in memory, writes nothing
//     and never touches the last-draft pointer. If the stored record is
//     unusable (corrupt, foreign, from a newer build), it is PRESERVED
//     byte-for-byte: the session's write guard is left up (start() would clear
//     it, so it is not called on that path), the page edits an in-memory blank,
//     and every save attempt is refused until the user explicitly replaces it
//     (the dialog lands in 5d).
//
// NOT IN 5a: rail rows, inspector controls, validation, limits, counters, Send,
// assets, components/actions/roles, library/revisions. The rail landed in 5b,
// the inspector in 5c and the action bar (buttons + dialogs) in 5d.
// Consumed by: manage/message_builder.html (data-page-module="message-builder")
// Tested by:   scripts/test_message_builder_page.js
// ═══════════════════════════════════════════════════════════════
window.NERO = window.NERO || {};
window.NERO.embed = window.NERO.embed || {};

(function (NERO) {
    'use strict';

    const MODULE = 'message-builder';

    // Ids this module is allowed to depend on. The template owns them and
    // scripts/test_message_builder_layout.js asserts the two agree, so a rename
    // on one side fails the build instead of the page.
    const ID = {
        root: 'mb2-root',
        strip: 'mb2-strip',
        rail: 'mb2-rail',
        railBody: 'mb2-rail-body',
        inspector: 'mb2-inspector',
        inspectorBody: 'mb2-inspector-body',
        preview: 'mb2-preview-region',
        mount: 'mb2-mount',
        bar: 'mb2-bar',
        status: 'mb2-bar-status',
        actions: 'mb2-bar-actions',
    };

    let current = null;          // the live instance (mount/destroy + tests)

    function foundation() {
        const f = NERO.embed || {};
        const views = f.views || {};
        if (!f.model) throw new Error('message-builder needs embed/model.js loaded first');
        if (!f.store || !f.store.createStore) throw new Error('message-builder needs embed/store.js loaded first');
        if (!f.preview || !f.preview.create) throw new Error('message-builder needs embed/preview.js loaded first');
        if (!f.drafts || !f.drafts.create) throw new Error('message-builder needs embed/drafts.js loaded first');
        if (!views.statusbar || !views.statusbar.create) throw new Error('message-builder needs embed/views/statusbar.js loaded first');
        if (!views.rail || !views.rail.create) throw new Error('message-builder needs embed/views/rail.js loaded first');
        if (!views.inspector || !views.inspector.create) throw new Error('message-builder needs embed/views/inspector.js loaded first');
        if (!views.actionbar || !views.actionbar.create) throw new Error('message-builder needs embed/views/actionbar.js loaded first');
        return {
            model: f.model, store: f.store, preview: f.preview, drafts: f.drafts,
            statusbar: views.statusbar, rail: views.rail, inspector: views.inspector,
            actionbar: views.actionbar,
        };
    }

    function ownerDocument(root) {
        if (root && root.ownerDocument) return root.ownerDocument;
        if (typeof document !== 'undefined' && document) return document;
        throw new Error('message-builder cannot find a document');
    }

    function find(root, doc, id) {
        if (root && typeof root.querySelector === 'function') {
            const found = root.querySelector('#' + id);
            if (found) return found;
        }
        if (doc && typeof doc.getElementById === 'function') return doc.getElementById(id);
        return null;
    }

    function requireElement(root, doc, id) {
        const el = find(root, doc, id);
        if (!el) throw new Error('message-builder: the page is missing #' + id);
        return el;
    }

    /** The draft namespace is keyed by the SESSION's guild (server-rendered). */
    function readGuildId(root) {
        const raw = root && root.getAttribute ? root.getAttribute('data-guild-id') : '';
        const value = raw == null ? '' : String(raw).trim();
        return value || null;
    }

    /**
     * Server-rendered identity (base.html emits window.__BOT_IDENTITY__ when the
     * route passes one). Validated here rather than trusted: a preview must not
     * render `undefined` as the author name, and an img src must stay http(s).
     */
    function readIdentity(win) {
        const raw = win && win.__BOT_IDENTITY__;
        if (!raw || typeof raw !== 'object') return { name: 'Bot', avatar: null };
        const name = typeof raw.name === 'string' && raw.name.trim() ? raw.name.trim() : 'Bot';
        const avatar = typeof raw.avatar === 'string' && /^https?:\/\//i.test(raw.avatar.trim())
            ? raw.avatar.trim() : null;
        return { name: name, avatar: avatar };
    }

    // ── The page ─────────────────────────────────────────────────
    function init(root, ctx) {
        if (!root) throw new Error('message-builder init needs the page root element');
        if (current) destroy();                        // a re-mount must never stack
        const f = foundation();
        const doc = ownerDocument(root);
        const win = (typeof window !== 'undefined' && window) || null;

        const els = {
            root: root,
            strip: requireElement(root, doc, ID.strip),
            rail: requireElement(root, doc, ID.rail),
            railBody: requireElement(root, doc, ID.railBody),
            inspectorBody: requireElement(root, doc, ID.inspectorBody),
            inspector: requireElement(root, doc, ID.inspector),
            preview: requireElement(root, doc, ID.preview),
            mount: requireElement(root, doc, ID.mount),
            bar: requireElement(root, doc, ID.bar),
            status: requireElement(root, doc, ID.status),
            actions: requireElement(root, doc, ID.actions),
        };

        const inst = {
            module: MODULE,
            root: root,
            ctx: ctx || null,
            doc: doc,
            win: win,
            els: els,
            guildId: readGuildId(root),
            startedAt: Date.now(),          // one clock for the whole page life
            destroyed: false,
            booted: false,
            notice: null,
            pointerWritten: false,
            lastWrites: 0,
            unsubs: [],
            store: null,
            session: null,
            preview: null,
            statusbar: null,
            rail: null,
            inspector: null,
            actionbar: null,
        };
        current = inst;

        // Shell first: the bar has something true to say from the very first
        // render ("No changes yet"), and nothing below blocks on storage.
        inst.statusbar = f.statusbar.create({ document: doc, status: els.status, actions: els.actions });
        // The reducer table is NOT optional: without it every dispatch is a
        // silent no-op, and the page would appear to load a document while the
        // store still held the blank it was constructed with.
        inst.store = f.store.createStore({
            now: Date.now,
            reducers: f.store.createReducers(f.model),
        });
        // The rail is a pure view over the store: it renders the structure and
        // dispatches actions. It is created before the draft loads so the page
        // shows the still-empty document immediately and repaints itself when
        // the stored one arrives (the rail subscribes to the store).
        inst.rail = f.rail.create({
            document: doc,
            store: inst.store,
            mount: els.railBody,
        });
        // The inspector is the editing view over the same store: it renders the
        // selected node's properties and each input is a store action. Like the
        // rail it is created before the draft loads, so the page is usable
        // immediately and repaints itself when the stored document arrives.
        inst.inspector = f.inspector.create({
            document: doc,
            model: f.model,
            store: inst.store,
            mount: els.inspectorBody,
            // One clock for the whole page life — the same one the preview
            // header uses, so "Now" cannot disagree with the rendered time.
            now: function () { return inst.startedAt; },
        });
        if (!inst.store.getUi().selectedNodeId) {
            // Boot state: the message root is what the inspector will show.
            inst.store.dispatch({ type: 'ui/selectNode', nodeId: f.rail.CONTENT_NODE });
        }
        // The action bar is the third view over the same store: its buttons end
        // in store calls (undo/redo) or in a pure read (Copy JSON). It reports
        // what it did through onNotice — the page's ONE status region — so the
        // bar itself owns no messaging and no document state.
        inst.actionbar = f.actionbar.create({
            document: doc,
            model: f.model,
            store: inst.store,
            mount: els.actions,
            // The clipboard is read live from the window: a browser can be
            // missing it entirely (a non-secure context), which is a state the
            // bar handles rather than a reason to fail at boot.
            navigator: win ? win.navigator : null,
            onNotice: function (notice) { setNotice(inst, notice); },
        });
        inst.session = f.drafts.create({ guildId: inst.guildId, now: Date.now });

        inst.unsubs.push(inst.session.onState(function (snapshot) { onSessionState(inst, snapshot); }));
        // Coarse store listener on purpose: it fires for markSaved/undo/redo as
        // well as edits, and the statusbar's writes are change-guarded, so an
        // unchanged status costs zero DOM writes on a keystroke. (The rail has
        // its own selector-based subscriptions and is not re-rendered by this.)
        inst.unsubs.push(inst.store.subscribe(function () { renderStatus(inst); }));

        inst.session.attach(inst.store);
        if (win && win.addEventListener) inst.session.bindLifecycle(win);

        renderStatus(inst);
        resume(inst);          // async, never rejects, never blocks the paint
        return inst;
    }

    function renderStatus(inst) {
        if (!inst || inst.destroyed || !inst.statusbar) return;
        inst.statusbar.render({
            // The store owns document dirty-ness — this is the only place the
            // UI asks that question.
            dirty: inst.store ? inst.store.isDirty() : false,
            // The session owns the persistence lifecycle (saving/saved/error/
            // blocked/degraded/revision/timestamps).
            session: inst.session ? inst.session.state() : null,
            pending: !!(inst.session && inst.session.pendingSave && inst.session.pendingSave()),
            notice: inst.notice,
        });
    }

    /**
     * The ONE way anything on this page says something to the user outside the
     * status pill: a notice line inside #mb2-bar-status (the page's only live
     * region besides the step-6 validation strip). A view reports results by
     * calling back here; it never touches the element and never creates a
     * region of its own — two live regions announcing the same thing is how a
     * screen-reader user hears everything twice.
     */
    function setNotice(inst, notice) {
        if (!inst || inst.destroyed) return false;
        const text = notice && notice.text ? String(notice.text) : '';
        if (!text) return false;                  // nothing to say: never invent an empty notice
        inst.notice = { tone: (notice && notice.tone) || 'info', text: text };
        renderStatus(inst);
        return true;
    }

    function onSessionState(inst, snapshot) {
        if (!inst.destroyed) {
            const writes = snapshot && Number.isInteger(snapshot.writes) ? snapshot.writes : 0;
            if (writes > inst.lastWrites) {
                inst.lastWrites = writes;
                rememberPointer(inst);      // only ever after a SUCCESSFUL write
            }
        }
        renderStatus(inst);
    }

    /**
     * Remember which draft to reopen. Gated three ways on purpose: only after a
     * successful write, only once per page life, and never while storage is
     * degraded — the pointer is the one piece of state that could make a real
     * draft unreachable, so it is never written on a guess.
     */
    function rememberPointer(inst) {
        if (inst.pointerWritten || inst.destroyed || !inst.session) return;
        const storage = inst.session.storage ? inst.session.storage() : null;
        if (storage && typeof storage.isAvailable === 'function' && !storage.isAvailable()) return;
        const documentId = inst.session.documentId ? inst.session.documentId() : null;
        if (!documentId) return;
        inst.pointerWritten = true;
        const pending = inst.session.meta.rememberDocument(inst.guildId, documentId);
        if (pending && typeof pending.catch === 'function') pending.catch(function () { /* reported through state */ });
    }

    // ── Boot: find the draft (or start one) ──────────────────────
    function resume(inst) {
        const session = inst.session;
        const storage = session.storage ? session.storage() : null;
        const unusable = function () {
            return !!(storage && typeof storage.isAvailable === 'function' && !storage.isAvailable());
        };

        if (unusable()) {
            // Unusable from the first moment. This is NOT "there is no draft":
            // nothing is read, nothing is written, no pointer is touched.
            return finish(inst, adoptNew(inst, 'storage-unavailable'));
        }

        return Promise.resolve()
            .then(function () { return session.meta.lastDocumentId(inst.guildId); })
            .then(function (id) {
                if (unusable()) return adoptNew(inst, 'storage-unavailable');
                if (!id) return adoptNew(inst, 'no-draft');
                return session.load({ documentId: id, guildId: inst.guildId }).then(function (result) {
                    return handleLoad(inst, result);
                });
            })
            .then(function () { return finish(inst, inst); })
            .catch(function (err) {
                // load() is documented to return a verdict rather than throw, so
                // reaching here means the stored record is malformed enough to
                // break the reader itself — the one outcome the page cannot
                // classify. It does the only safe thing available: keep a
                // document in memory, adopt a NEW draft identity (so no save can
                // reach the record that could not be read) and say so plainly.
                return finish(inst, adoptNew(inst, 'unreadable', err));
            });
    }

    function finish(inst, value) {
        inst.booted = true;
        renderStatus(inst);
        return value;
    }

    function handleLoad(inst, result) {
        const status = result && result.status;
        if (result && result.ok) {
            adopt(inst, result.document);
            if (status === 'repaired') {
                const repairs = (result.repairs && result.repairs.length) ? ' (' + result.repairs.join(', ') + ')' : '';
                inst.notice = { tone: 'warn', text: 'Your draft was recovered — missing parts were filled in' + repairs + '.' };
            }
            return inst;
        }
        if (status === 'empty') return adoptNew(inst, 'no-record');
        if (status === 'unavailable') return adoptNew(inst, 'storage-unavailable');
        if (status === 'no-document-id') return adoptNew(inst, 'no-identity');
        // corrupt | foreign | unsupported | future — the record is preserved.
        return adoptGuarded(inst, result);
    }

    function adoptNew(inst, why, err) {
        inst.session.start(NERO.embed.model.blankMessageDocument(), { guildId: inst.guildId });
        if (why === 'storage-unavailable') {
            inst.notice = {
                tone: 'warn',
                text: 'Draft storage is unavailable — you can keep editing, but nothing will be saved until it is back.',
            };
        } else if (why === 'unreadable') {
            inst.notice = {
                tone: 'danger',
                text: 'Your saved draft could not be read (' + ((err && err.message) || 'unknown error') +
                      ') and was left untouched. This page is editing a new message, so saving cannot overwrite it.',
            };
        }
        return setCanonical(inst, inst.session.document());
    }

    function adoptGuarded(inst, result) {
        // do NOT call session.start() here: it clears the write guard, and this
        // path exists precisely so the preserved record cannot be overwritten.
        setCanonical(inst, NERO.embed.model.blankMessageDocument());
        const status = (result && result.status) || 'corrupt';
        const guarded = (NERO.embed.views.statusbar.GUARDED || {})[status] ||
            'The saved draft was left untouched';
        inst.notice = { tone: 'danger', text: guarded + '. You can keep editing here, but nothing will be saved over it.' };
        return inst;
    }

    /**
     * The ONE way a document becomes the canonical one: into the store, then
     * marked saved. `history:false` keeps the load out of the undo stack, and
     * marking it saved is what makes "loaded" mean "in sync with storage" — no
     * dirty flag, no scheduled write, and (because the store's hash is taken
     * from this exact document) no phantom change on the next keystroke.
     */
    function setCanonical(inst, document_) {
        const model = NERO.embed.model;
        const normalized = model.normalizeDocument(document_);
        inst.store.dispatch({ type: 'document/load', document: normalized, meta: { history: false } });
        inst.store.markSaved(inst.store.getDocument());
        if (inst.preview) inst.preview.updateDocument(inst.store.getDocument());
        else mountPreview(inst);
        return inst.store.getDocument();
    }

    /** Also the document/load path for a REPLACEMENT, kept separate for tests. */
    function adopt(inst, document_) {
        return setCanonical(inst, document_);
    }

    function mountPreview(inst) {
        const preview = NERO.embed.preview.create(inst.els.mount, {
            // One clock per page life: the header time cannot drift while the
            // page is open, and the same document renders the same bytes.
            now: function () { return inst.startedAt; },
            botIdentity: readIdentity(inst.win),
            lookups: {},
            lookupsVersion: 0,
        });
        inst.preview = preview;
        preview.updateDocument(inst.store.getDocument());      // first paint: one build
        inst.unsubs.push(inst.store.subscribe(
            function (s) { return s.document; },
            function (document_) { if (!inst.destroyed) preview.updateDocument(document_); }
        ));
        return preview;
    }

    // ── Teardown ─────────────────────────────────────────────────
    // Called by nav-lifecycle as destroy(root, ctx) before the DOM is swapped
    // away. Everything the page allocated is released here: subscriptions, the
    // session (which flushes a pending write, detaches and removes its
    // lifecycle listeners), the preview and the store.
    function destroy(root, ctx) {
        const inst = current;
        if (!inst) return false;
        current = null;
        inst.destroyed = true;
        inst.unsubs.splice(0).forEach(function (off) {
            try { off(); } catch (e) { /* an unsubscribe must never block teardown */ }
        });
        if (inst.inspector) { try { inst.inspector.destroy(); } catch (e) { /* already gone */ } }
        if (inst.rail) { try { inst.rail.destroy(); } catch (e) { /* already gone */ } }
        if (inst.actionbar) { try { inst.actionbar.destroy(); } catch (e) { /* already gone */ } }
        if (inst.session) { try { inst.session.destroy(); } catch (e) { /* reported above */ } }
        if (inst.preview) { try { inst.preview.destroy(); } catch (e) { /* already gone */ } }
        if (inst.store) { try { inst.store.destroy(); } catch (e) { /* already gone */ } }
        if (inst.statusbar) { try { inst.statusbar.destroy(); } catch (e) { /* already gone */ } }
        return true;
    }

    if (typeof NERO.definePage !== 'function') {
        throw new Error('message-builder-page needs nav-lifecycle.js loaded before it');
    }
    NERO.definePage(MODULE, { init: init, destroy: destroy });

    // Public surface: the registry uses init/destroy; current() is the dev/test
    // seam (same idea as drafts.js exposing storage() and pendingSave()).
    NERO.embed.messageBuilderPage = {
        MODULE: MODULE,
        ID: ID,
        init: init,
        destroy: destroy,
        current: function () { return current; },
    };
})(window.NERO);
