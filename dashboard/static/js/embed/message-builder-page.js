// ═══════════════════════════════════════════════════════════════
// NERO DASHBOARD — embed/message-builder-page.js
// Message Builder v2 — phase 1, step 5a: the page module.
//
// It wires the already-approved foundations together and NOTHING else:
//
//      embed/model.js            normalized MessageDocument (source of truth)
//      embed/store.js            document + history + selection + dirty flag
//      embed/validate.js         pure rules over (document, served limits)
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
// STEP 6a — VALIDATION (one pass per edit burst, one region, no new state)
//     The page owns #mb2-strip and is the only writer of it. The flow is
//     exactly what the step-6 proposal approved and nothing wider:
//
//         served limits (data-limits, L1)
//           → embed/validate.validate(document, limits)   [pure]
//           → store.dispatch(ui/setIssues)                [the ONE list]
//           → #mb2-strip                                  [this file]
//
//     * The limits table is SERVER-RENDERED into the page. There is no client
//       copy and no fallback table: if it is missing or unusable the validator
//       says so explicitly (one issue) instead of quietly assuming "no limit".
//     * Validation runs OFF the keystroke path: an edit schedules one pass
//       (VALIDATE_IDLE_MS) and the pass that runs cancels the schedule it came
//       from, so a burst of keystrokes costs exactly one pass. Loading a
//       document validates immediately instead — a restored draft with
//       problems must not look clean, not even for 150 ms.
//     * The strip is hidden while there is nothing to say, and every write to
//       it is change-guarded (text, tone class and `hidden` are each written
//       only when they differ), so an unchanged result costs zero DOM writes.
//     * Issues are read back from `store.ui.issues` — the store is the only
//       place they live. The page keeps no list of its own, only the change
//       signature that decides whether the store needs telling again.
//     * Step 6b adds no state here at all: the views are handed this same
//       `limits` object and read their counters/caps/badges from the validator's
//       measurement and the store's issue list. The page still owns the limits
//       PARSE (readLimits) and the strip; it does not paint a counter or a badge.
//
// BOOT ORDER (each step matters, and the harness asserts each one)
//     paint shell → create statusbar → create store → create rail →
//     create inspector → create actionbar → create session → read the served
//     limits → subscribe (document → validate, issues → strip) →
//     session.attach(store)  [BEFORE load, see below] →
//     session.bindLifecycle(window) → render status → resume() [async] →
//     one immediate validation pass.
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
// the inspector in 5c, the action bar (buttons + dialogs) in 5d and the
// validator + strip in 6a. Counters and the rail's limit feedback are 6b.
// Consumed by: manage/message_builder.html (data-page-module="message-builder")
// Tested by:   scripts/test_message_builder_page.js
// ═══════════════════════════════════════════════════════════════
window.NERO = window.NERO || {};
window.NERO.embed = window.NERO.embed || {};

(function (NERO) {
    'use strict';

    const MODULE = 'message-builder';

    /**
     * How long after the last document change the validation pass runs. It is a
     * burst-collapser, not a debounce on correctness: the pass reads whatever
     * the document is at the moment it runs, so nothing that happened during
     * the burst can be missed.
     */
    const VALIDATE_IDLE_MS = 150;

    /** The strip's base class; the tone class is added/removed beside it. */
    const STRIP_CLASS = 'mb2-strip';

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
    files: 'mb2-files',          // 7e: the static files summary, above the preview
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
        if (!f.validate || !f.validate.validate) throw new Error('message-builder needs embed/validate.js loaded first');
        if (!f.preview || !f.preview.create) throw new Error('message-builder needs embed/preview.js loaded first');
        if (!f.drafts || !f.drafts.create) throw new Error('message-builder needs embed/drafts.js loaded first');
        // 7c: the asset layer is a foundation too — assets.js owns what a
        // record/reference IS, asset-store.js owns the bytes. Without them the
        // page cannot answer "are the files this document names actually here?",
        // and guessing that answer is not an option.
        if (!f.assets || !f.assets.assetFacts) throw new Error('message-builder needs embed/assets.js loaded first');
        if (!f.assetStore || !f.assetStore.create) throw new Error('message-builder needs embed/asset-store.js loaded first');
        if (!views.statusbar || !views.statusbar.create) throw new Error('message-builder needs embed/views/statusbar.js loaded first');
        if (!views.rail || !views.rail.create) throw new Error('message-builder needs embed/views/rail.js loaded first');
        if (!views.inspector || !views.inspector.create) throw new Error('message-builder needs embed/views/inspector.js loaded first');
        if (!views.actionbar || !views.actionbar.create) throw new Error('message-builder needs embed/views/actionbar.js loaded first');
        return {
            model: f.model, store: f.store, validate: f.validate, preview: f.preview, drafts: f.drafts,
            assets: f.assets, assetStore: f.assetStore,
            statusbar: views.statusbar, rail: views.rail, inspector: views.inspector,
            actionbar: views.actionbar,
        };
    }

    /**
     * The served limits table (approved transport L1), read from the page root:
     * the server rendered `utils/discord_limits.limits_payload()` into
     * data-limits, so there is ONE authority and no client copy of the numbers.
     *
     * A table that is absent or unparsable is NOT "no limits": it is reported as
     * a failure. The value returned here is either a parsed object (whose keys
     * embed/validate.js then checks) or null, which the validator turns into the
     * one explicit issue it has for exactly this case.
     */
    function readLimits(root) {
        const raw = root && typeof root.getAttribute === 'function' ? root.getAttribute('data-limits') : null;
        if (!raw) return { limits: null, error: 'the page carried no limits table' };
        let parsed = null;
        try {
            parsed = JSON.parse(raw);
        } catch (e) {
            return { limits: null, error: 'the limits table was not valid JSON' };
        }
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
            return { limits: null, error: 'the limits table was not an object' };
        }
        return { limits: parsed, error: null };
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
        // The served limits: read once, at mount, from the shell the server
        // rendered. Nothing fetches them later and nothing else may hold a copy.
        const servedLimits = readLimits(root);

        const els = {
            root: root,
            strip: requireElement(root, doc, ID.strip),
            rail: requireElement(root, doc, ID.rail),
            railBody: requireElement(root, doc, ID.railBody),
            inspectorBody: requireElement(root, doc, ID.inspectorBody),
            inspector: requireElement(root, doc, ID.inspector),
            preview: requireElement(root, doc, ID.preview),
            mount: requireElement(root, doc, ID.mount),
            files: requireElement(root, doc, ID.files),
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
            // ── step 6a: validation state (nothing of the document lives here) ──
            // `limits` is the served table or null; `limitsError` is why it is
            // null. `issueSignature` is the change guard: the page dispatches
            // only when the issue list would actually be different, and never
            // keeps a list of its own — the store owns `ui.issues`.
            limits: servedLimits.limits,
            limitsError: servedLimits.error,
            issueSignature: f.validate.signature([]),
            validateTimer: null,
            // ── step 7c: asset facts (observations, never document state) ──
            // `assetFacts` is the LAST probe's answer, keyed by the id set it
            // describes; `assetProbe` is the in-flight probe (one at a time);
            // `assetProbes` counts them through the registry's ctx, the same
            // way validateRuns counts passes.
            assetFacts: f.assets.noFacts({ embeds: [], assets: {} }),
            assetProbe: null,
            // ── step 7d: the file pick in flight (never document state) ──
            // `uploadToken` is the pick that currently owns the pipeline: a newer
            // pick replaces it, and any older read that lands afterwards is
            // discarded without touching the document.
            uploadToken: null,
            // ── step 7e: resolution state (never document state) ──
            // `resolutionSignature` is what the preview last PAINTED: the
            // filename→URL pairs the resolver could answer with at that moment.
            // A pass compares against it and repaints only when the answer
            // actually changed, so an unchanged resolution costs zero DOM writes.
            // `assetPass` is the pass in flight (one at a time) and `assetAgain`
            // is a trigger that arrived while it ran — absorbed, never dropped.
            resolutionSignature: null,
            assetPass: null,
            assetAgain: false,
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
        // 6b: the views are handed the SAME limits table the validator is given
        // (the object read once from data-limits at mount). By reference, never
        // copied: the rail paints its add caps and the inspector its counters from
        // the one authority, so there is no second table to keep in step.
        inst.rail = f.rail.create({
            document: doc,
            store: inst.store,
            mount: els.railBody,
            limits: inst.limits,
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
            limits: inst.limits,
            // One clock for the whole page life — the same one the preview
            // header uses, so "Now" cannot disagree with the rendered time.
            now: function () { return inst.startedAt; },
            // 7d: the local-file control. The page owns the pipeline (read →
            // identify → store → record → dispatch) and the words; the view owns
            // the control and paints exactly what these answer. `accept` is a
            // picker hint built from the asset module's own table — the decision
            // is assets.identify() on the bytes.
            upload: {
                accept: uploadAccept(f),
                stateFor: function (request) { return uploadStateFor(inst, request); },
                onPick: function (request) { return uploadPick(inst, request); },
                onRemove: function (request) { return uploadRemove(inst, request); },
            },
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
            // The draft session is the page's to expose, not the bar's to hold:
            // the bar gets a question and an action, and never a persistence API.
            discard: {
                available: function () { return hasDiscardableChanges(inst); },
                perform: function () { return discardChanges(inst); },
            },
            // Same shape for Save now: an action, never a persistence API. The
            // page performs it; the bar only renders the capability.
            save: {
                perform: function () { return saveNow(inst); },
            },
        });
        inst.session = f.drafts.create({ guildId: inst.guildId, now: Date.now });

        // The byte store, on the SAME v2 database through the SAME adapter the
        // session uses (asset-store.bytesStorage): one persistence boundary, one
        // retry/timeout/degrade policy, one place that knows the database name.
        // This page owns no bytes and no object URLs: it hands the store the
        // browser's own URL factory (7e) and from then on only ever calls
        // `urlFor` (resolve ahead of the render) and `cachedUrl` (the synchronous
        // render-path read). A window without that capability gets `null` here,
        // which the store reports as `urls-unavailable` — an honest "no picture"
        // instead of a URL nobody could revoke.
        inst.assetStore = f.assetStore.create({
            scheduler: storageScheduler(inst),
            urls: f.assetStore.browserUrls(inst.win),
        });

        inst.unsubs.push(inst.session.onState(function (snapshot) { onSessionState(inst, snapshot); }));
        // Coarse store listener on purpose: it fires for markSaved/undo/redo as
        // well as edits, and the statusbar's writes are change-guarded, so an
        // unchanged status costs zero DOM writes on a keystroke. (The rail has
        // its own selector-based subscriptions and is not re-rendered by this.)
        inst.unsubs.push(inst.store.subscribe(function () { renderStatus(inst); }));
        // The validation half of the same idea, on the store's slice selectors:
        // a document change SCHEDULES one pass (never one per keystroke), and a
        // changed issue list is what paints the strip. Both callbacks only read
        // and render — nothing here dispatches from inside a notification pass.
        inst.unsubs.push(inst.store.subscribe(
            function (s) { return s.document; },
            function () { scheduleValidation(inst); }
        ));
        inst.unsubs.push(inst.store.subscribe(
            function (s) { return s.ui.issues; },
            function () { renderStrip(inst); }
        ));

        inst.session.attach(inst.store);
        if (win && win.addEventListener) inst.session.bindLifecycle(win);

        renderStatus(inst);
        resume(inst);          // async, never rejects, never blocks the paint
        return inst;
    }

    function renderStatus(inst) {
        if (!inst || inst.destroyed || !inst.statusbar) return;
        // The two facts the UI cannot derive for free: both hash the document.
        // They are computed ONCE here and handed to every surface that needs
        // them, so asking the same question three times per keystroke (and
        // paying for three document hashes) never happens.
        const dirty = inst.store ? inst.store.isDirty() : false;             // store-owned
        const sessionState = inst.session ? inst.session.state() : null;     // session-owned
        const pending = !!(inst.session && inst.session.pendingSave && inst.session.pendingSave());
        inst.statusbar.render({
            dirty: dirty,
            session: sessionState,
            pending: pending,
            notice: inst.notice,
        });
        // The bar's own derived state (which actions exist and are available)
        // depends on the SESSION too — a confirmed save changes what "discard"
        // would go back to — and this is the one place both owners are observed.
        // Its writes are change-guarded, so this costs nothing per keystroke.
        if (inst.actionbar && inst.actionbar.refresh) inst.actionbar.refresh();
        if (inst.actionbar && inst.actionbar.renderSave) {
            inst.actionbar.renderSave({
                dirty: dirty,
                pending: pending,
                session: sessionState,
                // The retryability question is the session's, and the session
                // asks the storage adapter — the bar never keeps its own list.
                retryable: !!(inst.session && inst.session.retryable && inst.session.retryable()),
            });
        }
    }

    /**
     * SAVE NOW — the manual half of the ONE persistence path.
     *
     * This calls the session's own saveNow(), with no arguments and no force:
     * a manual save is exactly the write the idle timer performs, so a click
     * cannot write something the automatic path would not, cannot skip a gate
     * (destroyed / blocked / clean / in-flight) and cannot start a second
     * write while one is in flight — saveNow() returns the SAME promise for a
     * save already running. The result is deliberately not interpreted here:
     * the session records the outcome in its state, and renderStatus() renders
     * it, so there is one description of what happened rather than two.
     */
    function saveNow(inst) {
        if (!inst || inst.destroyed || !inst.session) return Promise.resolve({ ok: false, reason: 'no-session' });
        return inst.session.saveNow();
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

    /**
     * Is there anything to discard? A discard puts the store back to the last
     * document persistence CONFIRMED, so it only means something when the store
     * has drifted from that document. The saved baseline is the session's; the
     * drift is the store's (its dirty flag is exactly "not what storage holds").
     * With nothing persisted yet there is no version to go back to, so the
     * action stays unavailable rather than restoring a blank over an edit.
     */
    function hasDiscardableChanges(inst) {
        if (!inst || inst.destroyed || !inst.session || !inst.session.savedDocument) return false;
        if (!inst.session.savedDocument()) return false;
        return inst.store.isDirty();
    }

    /**
     * THE DISCARD: the canonical document becomes the last persisted one, under
     * the SAME draft identity. Deliberately nothing else happens — no write is
     * scheduled (the restored document is what storage already holds, so the
     * session's own dirty check skips it), no pointer is touched (the pointer
     * only ever moves on a successful write), and no undo entry is created
     * (setCanonical replaces the baseline, which is the whole point: undoing a
     * discard must not be possible, because the state it discarded was never
     * saved).
     */
    function discardChanges(inst) {
        if (!inst || inst.destroyed || !inst.session) return false;
        const saved = inst.session.savedDocument ? inst.session.savedDocument() : null;
        if (!saved) return false;
        if (!inst.store.isDirty()) return false;            // nothing drifted: no-op
        setCanonical(inst, saved);
        setNotice(inst, {
            tone: 'info',
            text: 'Changes discarded — back to the last saved version (' +
                  (inst.session.documentId() || 'this draft') + ').',
        });
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
        // The one validation pass that is NOT a burst: whatever document the
        // page ended up with (a restored draft, a preserved-record blank, an
        // in-memory new one) is checked immediately, and this pass consumes the
        // schedule the load itself queued — one run, not two.
        validateNow(inst);
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

    // ── Validation and the strip (step 6a) ───────────────────────
    /**
     * A timer that dies with the page. The registry's ctx owns timers for the
     * whole page life (it clears them on unmount), so a mounted page is not a
     * second timer owner; a page mounted by hand, without a registry, uses the
     * realm's own setTimeout/clearTimeout and destroy() cancels them. The handle
     * records WHO owns it, so a timer is always cancelled through the same API
     * that created it.
     */
    function setTimer(inst, fn, ms) {
        if (inst.ctx && typeof inst.ctx.timeout === 'function') {
            return { owner: 'ctx', id: inst.ctx.timeout(fn, ms) };
        }
        if (typeof setTimeout !== 'function') return null;
        return { owner: 'realm', id: setTimeout(fn, ms) };
    }

    function cancelTimer(inst, timer) {
        if (!timer) return false;
        if (timer.owner === 'ctx') {
            if (inst.ctx && typeof inst.ctx.clearTimeout === 'function') inst.ctx.clearTimeout(timer.id);
            return true;
        }
        if (typeof clearTimeout === 'function') clearTimeout(timer.id);
        return true;
    }

    function countValidation(inst) {
        if (inst.ctx && typeof inst.ctx.counter === 'function') inst.ctx.counter('validateRuns');
    }

    /** Probes, counted the same way passes are — one number per burst. */
    function countProbe(inst) {
        if (inst.ctx && typeof inst.ctx.counter === 'function') inst.ctx.counter('assetProbes');
    }

    /**
     * The scheduler the byte store's adapter uses for its open timeout. The
     * registry's ctx owns timers for the whole page life when the page is
     * mounted through it; a hand-mounted page falls back to the realm's own,
     * which is exactly what drafts.create() does for the session.
     */
    function storageScheduler(inst) {
        const ctx = inst.ctx;
        return {
            setTimeout: function (fn, ms) {
                return (ctx && typeof ctx.timeout === 'function') ? ctx.timeout(fn, ms) : setTimeout(fn, ms);
            },
            clearTimeout: function (id) {
                return (ctx && typeof ctx.clearTimeout === 'function') ? ctx.clearTimeout(id) : clearTimeout(id);
            },
        };
    }

    /**
     * Ask the byte store what it knows about the ids a document references,
     * and cache the answer as FACTS. The document is passed in as the snapshot
     * the probe belongs to, so the facts describe the ids that were actually
     * looked up — if the user edits while the probe is in flight, the next
     * pass sees a different id set and probes again.
     *
     * A probe never writes, never hashes and never mints: it reads what the
     * store holds and reports why when it cannot. A store that cannot answer
     * is an answer (every fact becomes `bytes-unavailable`), which is why this
     * promise does not need a failure branch that invents one.
     */
    function probeAssets(inst, doc, ids) {
        if (!inst.assetStore) return Promise.resolve(null);
        countProbe(inst);
        return Promise.resolve(inst.assetStore.survey(ids)).then(function (rows) {
            if (inst.destroyed) return null;
            inst.assetFacts = NERO.embed.assets.assetFacts(doc, rows);
                // 7e: the facts are what say which referenced files are readable,
                // so this is the moment to resolve them — AHEAD of the render, so
                // the paint that follows (and every later patch) finds its URL in
                // the store's cache instead of waiting for one.
                resolveAssets(inst);
            return inst.assetFacts;
        });
    }

    /**
     * One validation pass, synchronously: read the store's document, run the
     * pure validator against the SERVED limits, and tell the store only if the
     * result differs from what it already holds. It also cancels any pending
     * pass — a scheduled pass has nothing left to do once this one has run.
     */
    function validateNow(inst) {
        if (!inst || inst.destroyed || !inst.store) return null;
        if (inst.validateTimer !== null) {
            cancelTimer(inst, inst.validateTimer);
            inst.validateTimer = null;
        }
        const doc = inst.store.getDocument();

        // 7c: the byte rules need an OBSERVATION of the ids this document
        // references, and this page is the only thing that can ask for one. So
        // a pass whose document names a different id set than the facts on
        // hand probes first and validates when the answer lands. One probe per
        // change of that set — not per keystroke, not per render — and the
        // pass it replaces never counted itself as a run, so a burst still
        // costs exactly one pass and at most one probe.
        const ids = NERO.embed.assets.documentAssetIds(doc);
        const factsSignature = inst.assetFacts && Array.isArray(inst.assetFacts.ids)
            ? inst.assetFacts.ids.join(',') : null;
        if (ids.join(',') !== factsSignature) {
            if (!inst.assetProbe) {
                inst.assetProbe = probeAssets(inst, doc, ids).then(function () {
                    inst.assetProbe = null;
                    if (!inst.destroyed) validateNow(inst);
                }, function () {
                    inst.assetProbe = null;      // an unexpected throw is treated as "no facts", never as a fact
                    if (!inst.destroyed) validateNow(inst);
                });
            }
            return null;
        }

        const validator = NERO.embed.validate;
        const issues = validator.validate(doc, inst.limits, inst.assetFacts);
        countValidation(inst);
        const signature = validator.signature(issues);
        if (signature !== inst.issueSignature) {
            inst.issueSignature = signature;
            // The store's slice is the ONE issue list; this is the only write.
            // `ui/*` never enters the undo stack (the store only records
            // history for document changes), and the subscription above paints
            // the strip from the same list it now holds.
            inst.store.dispatch({ type: 'ui/setIssues', issues: issues });
        }
        return issues;
    }

    /** Collapse an edit burst into ONE pass. */
    function scheduleValidation(inst) {
        if (!inst || inst.destroyed) return false;
        if (inst.validateTimer !== null) cancelTimer(inst, inst.validateTimer);
        inst.validateTimer = setTimer(inst, function () {
            inst.validateTimer = null;
            validateNow(inst);
        }, VALIDATE_IDLE_MS);
        return true;
    }

    /**
     * Paint #mb2-strip from `store.ui.issues`. This is the page's one job for
     * the region: no view writes it, nothing else creates a live region, and
     * every write is change-guarded — an unchanged issue list costs zero DOM
     * writes (the text is only assigned when it differs, the tone class only
     * when it differs, and `hidden` only on a real transition).
     *
     * The class is read and written through the ATTRIBUTE, because the base
     * class is the one the template declares (`class="mb2-strip"`): the guard
     * then compares like with like on the first render too, instead of writing
     * a class the markup already had.
     */
    function renderStrip(inst) {
        if (!inst || inst.destroyed || !inst.els || !inst.els.strip) return false;
        const el = inst.els.strip;
        const issues = (inst.store && inst.store.getUi() ? inst.store.getUi().issues : null) || [];

        if (!issues.length) {
            // Clean: the region goes away rather than announcing an empty line.
            if (el.textContent !== '') el.textContent = '';
            if (el.getAttribute('class') !== STRIP_CLASS) el.setAttribute('class', STRIP_CLASS);
            if (!el.hidden) el.hidden = true;
            return false;
        }

        const hasError = issues.some(function (issue) { return issue.severity === 'error'; });
        const label = issues.length === 1 ? '1 problem' : issues.length + ' problems';
        const text = label + ' — ' + issues[0].message +
            (issues.length > 1 ? ' (' + (issues.length - 1) + ' more)' : '');
        const className = STRIP_CLASS + (hasError ? ' mb2-tone-danger' : ' mb2-tone-warn');

        if (el.textContent !== text) el.textContent = text;
        if (el.getAttribute('class') !== className) el.setAttribute('class', className);
        if (el.hidden) el.hidden = false;
        return true;
    }

    // ── The files summary (step 7e) ───────────────────────────────
    /**
     * ONE STATIC LINE about what the message will carry: how many files it
     * references, how big they are together, and — when a size is not measured —
     * that one of them is unmeasured rather than quietly dropped from the total.
     *
     * It shows NUMBERS, never verdicts: whether a file is acceptable is the
     * validator's answer and it is printed in the strip. This line must not
     * disagree with that verdict or repeat it. It counts the FILES the document
     * references (two slots using one file are one file) and sums only the sizes
     * that are known, both read from the asset module — no second accounting
     * lives here, and nothing is inferred from the preview's own state.
     *
     * It is not a live region: the strip announces, this line only states. Every
     * write is change-guarded, so an unrelated keystroke costs zero DOM writes,
     * and an empty document hides the line instead of showing "0 files".
     */
    function renderFiles(inst) {
        if (!inst || inst.destroyed || !inst.els || !inst.els.files || !inst.store) return false;
        const el = inst.els.files;
        const A = NERO.embed.assets;
        const bytes = A.assetBytes(A.assetView(inst.store.getDocument()));
        const unmeasured = bytes.unknown.length + bytes.missing.length;
        if (!bytes.count) {
            // Nothing referenced: the line goes away rather than stating a zero.
            if (el.textContent !== '') el.textContent = '';
            if (!el.hidden) el.hidden = true;
            return false;
        }
        const parts = [bytes.count === 1 ? '1 file' : bytes.count + ' files'];
        // A known size is shown even when it is zero; an unmeasured one is never
        // folded into the total, and never silently omitted either.
        if (bytes.count - unmeasured > 0) parts.push(A.describeSize(bytes.total));
        if (unmeasured) parts.push(unmeasured === 1 ? '1 unmeasured' : unmeasured + ' unmeasured');
        const text = parts.join(' · ');
        if (el.textContent !== text) el.textContent = text;
        if (el.hidden) el.hidden = false;
        return true;
    }


    // ── Local files (step 7d: client-side only) ───────────────────
    /**
     * WHAT A PICK IS ALLOWED TO DO, in order, once:
     *
     *   File → read once → assets.identify() → assetStore.putBytes()
     *        → assets.buildRecord() → asset/add → the slot's media edit
     *
     * Every step before the document is a GATE: a refusal at any of them leaves
     * the document exactly as it was (an edit that could not be stored is not an
     * edit). The two dispatches carry ONE coalesce key, so a pick is one undo
     * step, and the record always lands BEFORE the reference — the document never
     * holds a reference to a file it cannot describe.
     *
     * What this code never does: mint an object URL, write bytes itself, hash a
     * file itself, delete bytes, apply retention, or invent a record. Identity
     * comes from assets.js, bytes from asset-store.js, the shape from
     * assets.buildRecord(), and the limits from the served table alone.
     */
    const UPLOAD_SLOTS = {
        'media.image': { field: 'image', pathSuffix: 'image' },
        'media.thumbnail': { field: 'thumbnail', pathSuffix: 'thumbnail' },
        'author.icon': { field: 'author', pathSuffix: 'author.icon' },
        'footer.icon': { field: 'footer', pathSuffix: 'footer.icon' },
    };

    /** The picker's hint, built from the module that owns the extension table. */
    function uploadAccept(f) {
        return f.assets.ALLOWED_EXTENSIONS.map(function (ext) { return '.' + ext; }).join(',');
    }

    /** The slot a control key names, resolved against the CURRENT document. */
    function uploadSlot(inst, embedId, key) {
        const spec = UPLOAD_SLOTS[key];
        if (!spec || !inst || !inst.store) return null;
        const doc = inst.store.getDocument();
        const embeds = (doc && doc.embeds) || [];
        for (let i = 0; i < embeds.length; i++) {
            if (embeds[i].id !== embedId) continue;
            const embed = embeds[i];
            const value = spec.field === 'image' ? embed.image
                : spec.field === 'thumbnail' ? embed.thumbnail
                : spec.field === 'author' ? (embed.author && embed.author.icon)
                : (embed.footer && embed.footer.icon);
            return {
                embed: embed, value: value || null,
                path: 'embeds.' + i + '.' + spec.pathSuffix,
            };
        }
        return null;
    }

    /** The validator's FIRST issue at one path (its own order, its own words). */
    function firstIssueAt(issues, path) {
        for (let i = 0; i < issues.length; i++) {
            if (issues[i] && issues[i].path === path) return issues[i];
        }
        return null;
    }

    /**
     * What the control under one slot should say. The page answers because the
     * page owns the vocabulary: the record (assets.js), the size words
     * (assets.describeSize), the type words (assets.mimeLabel) and the VALIDATOR's
     * own sentence when it has one for this slot. The view paints what it is
     * given and translates nothing.
     */
    function uploadStateFor(inst, request) {
        const key = request && request.key;
        const slot = uploadSlot(inst, request && request.embedId, key);
        const ui = inst.store && inst.store.getUi ? inst.store.getUi() : null;
        const issues = (ui && Array.isArray(ui.issues) ? ui.issues : []);
        const issue = slot ? firstIssueAt(issues, slot.path) : null;
        const value = slot ? slot.value : null;
        const attached = !!(value && value.kind === 'upload' && value.assetId);
        if (issue) {
            return { text: issue.message, invalid: issue.severity === 'error', removable: attached };
        }
        if (!attached) return { text: 'No file attached', invalid: false, removable: false };
        const doc = inst.store.getDocument();
        const record = doc.assets ? doc.assets[String(value.assetId)] : null;
        const A = NERO.embed.assets;
        const parts = [(record && record.filename) || value.filename || 'the file'];
        if (record && typeof record.bytes === 'number') parts.push(A.describeSize(record.bytes));
        if (record && record.mime) parts.push(A.mimeLabel(record.mime));
        return { text: 'Attached: ' + parts.join(' · '), invalid: false, removable: true };
    }

    /**
     * Read the chosen file ONCE, as bytes. The reader is the realm's own
     * FileReader; a realm without one, or a read that fails, is an explicit
     * refusal — never a silent half-upload.
     */
    function readUpload(inst, file) {
        return new Promise(function (resolve) {
            const Ctor = (inst.win && inst.win.FileReader) ||
                (typeof FileReader !== 'undefined' ? FileReader : null);
            if (!Ctor) { resolve({ ok: false, reason: 'no-reader' }); return; }
            let reader = null;
            try {
                reader = new Ctor();
                reader.onload = function () {
                    const buffer = reader.result;
                    if (!buffer) { resolve({ ok: false, reason: 'read-failed' }); return; }
                    resolve({ ok: true, buffer: buffer });
                };
                reader.onerror = function () { resolve({ ok: false, reason: 'read-failed' }); };
                reader.readAsArrayBuffer(file);
            } catch (e) {
                resolve({ ok: false, reason: 'read-failed' });
            }
        });
    }

    /**
     * The pick is over (landed, refused or thrown): the token goes back to null
     * so "a pick is in flight" is a state with an end. A read that arrives after
     * this — a superseded one — no longer matches and is discarded.
     */
    function finishUpload(inst, token) {
        if (inst && inst.uploadToken === token) inst.uploadToken = null;
    }

    /** Uploads, counted the way passes and probes are — one number per pick. */
    function countUpload(inst) {
        if (inst.ctx && typeof inst.ctx.counter === 'function') inst.ctx.counter('uploads');
    }

    /**
     * `remove:<key>` — take the file off one slot. The REFERENCE goes first, the
     * RECORD only when nothing in the document points at it any more, and the
     * BYTES never: a file nothing references is an orphan the retention analysis
     * is allowed to describe and nobody is allowed to delete in 7d. Both edits
     * share one coalesce key, so removing is one undo step like attaching.
     */
    function uploadRemove(inst, request) {
        if (!inst || inst.destroyed || !inst.store) return false;
        const key = request && request.key;
        const slot = uploadSlot(inst, request && request.embedId, key);
        if (!slot || !slot.value || slot.value.kind !== 'upload' || !slot.value.assetId) return false;
        const assetId = String(slot.value.assetId);
        const coalesce = 'upload:' + request.embedId + ':' + key;
        applySlotValue(inst, request.embedId, key, null, coalesce);
        const stillReferenced = NERO.embed.assets.documentAssetIds(inst.store.getDocument())
            .indexOf(assetId) !== -1;
        if (!stillReferenced) {
            inst.store.dispatch({
                type: 'asset/remove', assetId: assetId, meta: { coalesceKey: coalesce },
            });
        }
        return true;
    }

    /** The slot's media edit, whichever of the four slots it is. */
    function applySlotValue(inst, embedId, key, value, coalesce) {
        const meta = { coalesceKey: coalesce };
        if (key === 'media.image' || key === 'media.thumbnail') {
            inst.store.dispatch({
                type: 'embed/setMedia', embedId: embedId,
                slot: key === 'media.image' ? 'image' : 'thumbnail',
                value: value, meta: meta,
            });
            return;
        }
        if (key === 'author.icon') {
            inst.store.dispatch({ type: 'embed/setAuthor', embedId: embedId, patch: { icon: value }, meta: meta });
            return;
        }
        if (key === 'footer.icon') {
            inst.store.dispatch({ type: 'embed/setFooter', embedId: embedId, patch: { icon: value }, meta: meta });
        }
    }

    /**
     * The pipeline, from the byte store's answer to the document. The record is
     * built by the module that owns the shape, stored first and referenced
     * second, and NOTHING is dispatched unless the bytes are in.
     */
    function applyUpload(inst, embedId, key, file, buffer, token, replaced) {
        const A = NERO.embed.assets;
        const ident = A.identify(buffer, file && file.name);
        if (!ident || !ident.ok) {
            setNotice(inst, {
                tone: 'warn',
                text: ident && ident.message ? ident.message : 'That file cannot be used as an image.',
            });
            finishUpload(inst, token);
            return null;
        }
        return Promise.resolve(inst.assetStore.putBytes(ident.assetId, buffer, { mime: ident.mime }))
            .then(function (stored) {
                if (inst.destroyed || inst.uploadToken !== token) return null;
                if (!stored || !stored.ok) {
                    setNotice(inst, {
                        tone: 'danger',
                        text: 'That file could not be stored in this browser, so nothing was added to the message.',
                    });
                    finishUpload(inst, token);
                    return null;
                }
                const built = A.buildRecord({
                    assetId: ident.assetId, sha256: ident.sha256, mime: ident.mime,
                    bytes: typeof stored.byteLength === 'number' ? stored.byteLength : null,
                    originalName: file && file.name ? String(file.name) : '',
                    filename: ident.filename, availability: 'bytes-local',
                    createdAt: new Date().toISOString(),
                });
                if (!built || !built.ok) {
                    setNotice(inst, {
                        tone: 'danger',
                        text: 'That file could not be described well enough to attach, so nothing was added.',
                    });
                    finishUpload(inst, token);
                    return null;
                }
                const coalesce = 'upload:' + embedId + ':' + key;
                inst.store.dispatch({
                    type: 'asset/add', assetId: ident.assetId, record: built.record,
                    meta: { coalesceKey: coalesce },
                });
                applySlotValue(inst, embedId, key, {
                    kind: 'upload', assetId: ident.assetId, filename: built.record.filename,
                    mime: built.record.mime, bytes: built.record.bytes,
                }, coalesce);
                // Replacing a file is a ref REMOVAL like any other: if the id
                // the slot used to hold has no reference left, its record goes
                // too — same rule as Remove, same coalesce key, so the whole
                // pick is still ONE undo step. A record another slot still uses
                // is never touched, and bytes are never deleted. Without this,
                // a replaced file would leave an `assets.unused` warning the
                // user has no way to clear.
                if (replaced && replaced !== ident.assetId &&
                        NERO.embed.assets.documentAssetIds(inst.store.getDocument()).indexOf(replaced) === -1) {
                    inst.store.dispatch({
                        type: 'asset/remove', assetId: replaced, meta: { coalesceKey: coalesce },
                    });
                }
                // 7d/7e: a re-attached file can be the SAME id — identity is the
                // content, so picking the same bytes again addresses the same
                // asset. The id set then does not move, and the last probe's
                // answer ("those bytes are missing") would stand: a lie the moment
                // the bytes landed, both in the strip and in the resolution. Only a
                // CONTRADICTED observation is thrown away; a fresh id is covered by
                // the probe the id-set change already causes.
                const observed = inst.assetFacts && inst.assetFacts.states
                    ? inst.assetFacts.states[ident.assetId] : undefined;
                if (observed !== undefined && observed !== A.FACT_STATES.LOCAL) staleAssetFacts(inst);
                if (stored.persisted === false) {
                    // The store's OWN answer: the bytes are here for this session
                    // only. Never phrased as saved, because they are not.
                    setNotice(inst, {
                        tone: 'warn',
                        text: 'That file is kept for this session only — this browser’s storage is not available right now.',
                    });
                }
                finishUpload(inst, token);
                return ident.assetId;
            });
    }

    /**
     * A chosen local file. One pick at a time is honoured by SUPERSEDING: the
     * newest pick owns a token, and a read that finishes after a newer pick (or
     * after the slot changed, or after the page died) is discarded without
     * touching the document — the bytes it may already have stored are
     * content-addressed duplicates at worst, and 7d deletes nothing.
     */
    function uploadPick(inst, request) {
        if (!inst || inst.destroyed || !inst.assetStore) return false;
        const key = request && request.key;
        if (!UPLOAD_SLOTS[key] || !request || !request.file) return false;
        const slot = uploadSlot(inst, request.embedId, key);
        if (!slot) return false;
        countUpload(inst);
        const token = {};
        inst.uploadToken = token;
        const embedId = request.embedId;
        const startedWith = NERO.embed.model.stableStringify(slot.value);
        readUpload(inst, request.file).then(function (read) {
            if (inst.destroyed || inst.uploadToken !== token) return null;
            const now = uploadSlot(inst, embedId, key);
            if (!now || NERO.embed.model.stableStringify(now.value) !== startedWith) {
                setNotice(inst, {
                    tone: 'warn',
                    text: 'That file was not added because the slot changed while it was being read.',
                });
                finishUpload(inst, token);
                return null;
            }
            if (!read.ok) {
                setNotice(inst, { tone: 'warn', text: 'That file could not be read, so nothing was added.' });
                finishUpload(inst, token);
                return null;
            }
            const replaced = now.value && now.value.kind === 'upload' && now.value.assetId
                ? String(now.value.assetId) : null;
            return applyUpload(inst, embedId, key, request.file, read.buffer, token, replaced);
        }).catch(function () {
            if (!inst.destroyed && inst.uploadToken === token) {
                setNotice(inst, { tone: 'danger', text: 'That file could not be added. Nothing was changed.' });
            }
            finishUpload(inst, token);
            return null;
        });
        return true;
    }

    // ── Resolution (step 7e: stored bytes, made visible) ─────────────
    /**
     * The reference the resolver seam hands back is the FULL `attachment://name`
     * string (exactly what v1's own resolver received), so the filename is
     * everything after the scheme and nothing else.
     */
    const ATTACHMENT_PREFIX = 'attachment://';

    function attachmentFilename(raw) {
        const text = raw == null ? '' : String(raw);
        return text.slice(0, ATTACHMENT_PREFIX.length).toLowerCase() === ATTACHMENT_PREFIX
            ? text.slice(ATTACHMENT_PREFIX.length) : '';
    }

    /** Resolution passes, and the repaints they caused — one number each. */
    function countResolve(inst) {
        if (inst.ctx && typeof inst.ctx.counter === 'function') inst.ctx.counter('assetResolves');
    }

    function countResolveRepaint(inst) {
        if (inst.ctx && typeof inst.ctx.counter === 'function') inst.ctx.counter('assetRepaints');
    }

    /**
     * WHAT THE RESOLVER WOULD ANSWER RIGHT NOW: `filename → blob: URL`, built from
     * the live document and the byte store's own URL cache.
     *
     * The filename is the only key the preview can hand back (`attachment://name`
     * carries no id), so it is the key here too — and that is exactly why an
     * ambiguous name fails CLOSED: when two different assets claim one filename
     * (the validator's `assets.filename-clash`), neither resolves and the slot
     * shows no image, rather than whichever file happened to be found first. A
     * reference with no asset id has nothing to resolve and is skipped the same
     * way the validator reports it as unlinked.
     *
     * Synchronous and side-effect free on purpose: it reads the document, the
     * records and the store's CACHE, and never probes, reads or mints — the URL
     * was resolved ahead of the render, which is the whole contract with
     * `cachedUrl`.
     */
    function resolutionFor(inst) {
        const A = NERO.embed.assets;
        const map = {};
        if (!inst.store || !inst.assetStore) return map;
        const byName = {};                       // filename → { id, ambiguous }
        A.assetView(inst.store.getDocument()).refs.forEach(function (ref) {
            const name = ref.filename;
            if (!name || !ref.assetId) return;   // nothing to resolve BY NAME
            const entry = byName[name] || (byName[name] = { id: ref.assetId, ambiguous: false });
            if (entry.id !== ref.assetId) entry.ambiguous = true;
        });
        Object.keys(byName).forEach(function (name) {
            const entry = byName[name];
            if (entry.ambiguous) return;         // never guess between two files
            const url = inst.assetStore.cachedUrl(entry.id);
            if (url) map[name] = url;
        });
        return map;
    }

    /** The resolver's answer as ONE comparable value, collision-free. */
    function resolutionSignature(map) {
        return JSON.stringify(Object.keys(map).sort().map(function (name) {
            return [name, map[name]];
        }));
    }

    /**
     * Paint the preview, and remember what the resolver answered for that paint.
     * Both the document subscription and the resolution pass come through here,
     * so the signature always describes what is ON SCREEN — which is what makes
     * "repaint only when the resolution actually changed" true rather than
     * approximate. The renderer's own patch is change-guarded, so a paint with
     * nothing new to say writes nothing at all.
     */
    function paintPreview(inst) {
        if (!inst || inst.destroyed || !inst.preview || !inst.store) return false;
        inst.preview.updateDocument(inst.store.getDocument());
        inst.resolutionSignature = resolutionSignature(resolutionFor(inst));
        return true;
    }

    /**
     * The synchronous resolver the preview calls for every `attachment://`
     * reference. It is created ONCE per page life (the renderer captures it at
     * `create`), so it reads live state on every call instead of a snapshot: by
     * the time it is asked, the URLs are already in the store's cache.
     */
    function resolverFor(inst) {
        return function resolveImageSrc(raw) {
            if (inst.destroyed || !inst.assetStore) return '';
            const name = attachmentFilename(raw);
            return name ? (resolutionFor(inst)[name] || '') : '';
        };
    }

    /**
     * Paint the resolution if — and only if — it differs from what the preview
     * already shows. A refused mint (a browser without object URLs, bytes that
     * turned out to be missing or corrupt) is not a resolution change: the slot
     * simply has no URL, which is what an unresolved reference looks like too,
     * and the strip stays the only thing that says why.
     */
    function applyResolution(inst, results) {
        if (!inst || inst.destroyed) return false;
        // The one thing the strip cannot say, because it is not a validation
        // verdict: this browser cannot produce object URLs at all, so a stored
        // file cannot be shown, however healthy its bytes are. The store's own
        // refusal is the evidence, and the notice is change-guarded like every
        // other write to that region.
        const noUrls = (results || []).some(function (r) {
            return r && r.ok === false && r.reason === 'urls-unavailable';
        });
        if (noUrls) {
            setNotice(inst, {
                tone: 'warn',
                text: 'This browser cannot show attached files in the preview, so the pictures stay hidden.',
            });
        }
        if (!inst.preview || !inst.store) return false;
        const signature = resolutionSignature(resolutionFor(inst));
        if (signature === inst.resolutionSignature) return false;
        countResolveRepaint(inst);
        inst.preview.updateDocument(inst.store.getDocument());
        inst.resolutionSignature = signature;
        return true;
    }

    /**
     * THE RESOLUTION PASS. Given an observation that says which referenced files
     * this browser can actually read, it asks the byte store for a URL for each
     * one that has no cached URL yet — resolved AHEAD of the render, so the
     * render path itself never awaits anything — and then repaints only if the
     * resolver's answer changed.
     *
     * Costs, stated so they stay true: one pass per probe (plus one after a pick
     * that could not move the id set), at most one mint per asset per session (a
     * cached URL is never re-minted — that part belongs to the 7d store), ZERO
     * mints or byte reads for typing, and zero DOM writes when nothing changed. A
     * pass that is already running absorbs new triggers instead of stacking, and
     * no pass runs after teardown.
     */
    function resolveAssets(inst) {
        if (!inst || inst.destroyed || !inst.store || !inst.assetStore) return null;
        if (inst.assetPass) { inst.assetAgain = true; return inst.assetPass; }
        const A = NERO.embed.assets;
        const view = A.assetView(inst.store.getDocument());
        const states = (inst.assetFacts && inst.assetFacts.states) || {};
        countResolve(inst);
        // Only an OBSERVATION can say the bytes are readable. A missing one mints
        // nothing: the next probe brings the answer, and guessing here would turn
        // "not looked yet" into "not there".
        const wanted = view.ids.filter(function (id) {
            return states[id] === A.FACT_STATES.LOCAL && inst.assetStore.cachedUrl(id) === null;
        });
        if (!wanted.length) { applyResolution(inst, null); return null; }
        const pending = wanted.map(function (id) {
            const record = view.records[id];
            return inst.assetStore.urlFor(id, { mime: record ? record.mime : null });
        });
        function finish(results) {
            inst.assetPass = null;
            if (inst.destroyed) return null;
            applyResolution(inst, results);
            if (inst.assetAgain) {               // a trigger arrived mid-pass
                inst.assetAgain = false;
                resolveAssets(inst);
            }
            return null;
        }
        inst.assetPass = Promise.all(pending).then(finish, function () { return finish(null); });
        return inst.assetPass;
    }

    /**
     * The observation on hand no longer describes this id: bytes for an asset the
     * document already references just landed in the store, and the last probe
     * (correctly, at the time) had said they were not there. The next pass probes
     * again, so neither the strip nor the resolution keeps repeating an answer
     * that stopped being true.
     */
    function staleAssetFacts(inst) {
        inst.assetFacts = NERO.embed.assets.noFacts({ embeds: [], assets: {} });
        scheduleValidation(inst);
    }
    function mountPreview(inst) {
        const preview = NERO.embed.preview.create(inst.els.mount, {
            // One clock per page life: the header time cannot drift while the
            // page is open, and the same document renders the same bytes.
            now: function () { return inst.startedAt; },
            botIdentity: readIdentity(inst.win),
            lookups: {},
            lookupsVersion: 0,
            // 7e: the ONE seam the frozen renderer offers for a stored file.
            // Everything it knows about assets comes back through this function,
            // synchronously and from the store’s URL cache.
            resolveImageSrc: resolverFor(inst),
        });
        inst.preview = preview;
        paintPreview(inst);       // first paint: one build, resolution recorded with it
        renderFiles(inst);        // and the summary describes the same document
        inst.unsubs.push(inst.store.subscribe(
            function (s) { return s.document; },
            function () { paintPreview(inst); renderFiles(inst); }
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
        // 7d: an in-flight file read belongs to the page that started it. The
        // token is dropped so the read's own continuation sees a page that no
        // longer wants it (the `destroyed` check is the second guard).
        inst.uploadToken = null;
        // A scheduled pass is page work like any other: it dies with the page,
        // so a teardown during a burst leaves no timer behind (the registry
        // clears its own timers too, and the destroyed flag stops a callback
        // that was already in flight).
        if (inst.validateTimer !== null) {
            cancelTimer(inst, inst.validateTimer);
            inst.validateTimer = null;
        }
        inst.unsubs.splice(0).forEach(function (off) {
            try { off(); } catch (e) { /* an unsubscribe must never block teardown */ }
        });
        if (inst.inspector) { try { inst.inspector.destroy(); } catch (e) { /* already gone */ } }
        if (inst.rail) { try { inst.rail.destroy(); } catch (e) { /* already gone */ } }
        if (inst.actionbar) { try { inst.actionbar.destroy(); } catch (e) { /* already gone */ } }
        if (inst.session) { try { inst.session.destroy(); } catch (e) { /* reported above */ } }
        // The byte store goes with the page: its adapter connection closes and
        // any in-flight probe resolves into a destroyed instance (which the
        // probe's own `.then` checks) rather than into a re-render.
        if (inst.assetStore) { try { inst.assetStore.destroy(); } catch (e) { /* already gone */ } }
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
