#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════
   Message Builder v2 — phase 1, step 5d-1: the action bar.

   WHAT THIS HAS TO PROVE

     A. THE SURFACE — the bar renders exactly the actions that exist, in the
        approved order (Undo · Redo · Copy JSON in 5d-1), each a real <button>
        with a text label whose accessible name contains it.
     B. ENABLED STATE IS REAL — the buttons follow store.canUndo()/canRedo(),
        the element's own `disabled` property is the state, a disabled button
        cannot act, and a keystroke that does not move the ends of the history
        writes nothing at all.
     C. UNDO/REDO ARE THE STORE'S — a click moves the canonical document; the
        action bar never dispatches an edit of its own, never keeps a document,
        and reflects a change made anywhere else without being told.
     D. COPY JSON — the clipboard receives the pretty-printed Discord payload
        built from the store's document, the result is announced through the
        page's ONE live region (an onNotice callback, not a region of its own),
        and a second click while a write is in flight is ignored.
     E. FAILURE IS A STATE — a rejected write, an absent clipboard API and a
        model that cannot serialize each end somewhere honest: the JSON in a
        read-only, already-focused textarea, or a plain-words notice.
     F. DIALOG SEMANTICS — role=dialog, modal, labelled by its own title and
        described by its own body, exactly one at a time, Tab wraps at both
        ends, Escape and the backdrop close it, focus returns to the button that
        opened it, and a click inside the panel does not close it.
     G. TEARDOWN — destroy() unsubscribes, removes its buttons and its dialog,
        is idempotent, and a store change afterwards writes nothing.
     H. BOUNDARIES — no persistence, no validation, no renderer, no document
        mutation, no global document/window reads, no keyboard shortcuts: the
        module is loaded WITHOUT preview.js and drafts.js in the sandbox at all.
     I. IDENTITY — rendering never rebuilds a button, and a mounted dialog does
        not add a second one.

   Run:  node scripts/test_message_builder_actionbar.js
   ═══════════════════════════════════════════════════════════════ */
'use strict';
const fs = require('fs');
const path = require('path');
const vm = require('vm');
const { createDom } = require('./support/dom_stub.js');

let pass = 0, fail = 0; const failures = [];
function assert(cond, name, extra) {
    if (cond) { pass++; console.log('  PASS', name); }
    else { fail++; failures.push(name + (extra ? ' — ' + extra : '')); console.log('  FAIL', name, extra || ''); }
}
let currentSection = '(none)';
function section(t) { currentSection = t; console.log('\n== ' + t + ' =='); }

const ROOT_DIR = path.join(__dirname, '..');
const js = (...p) => path.join(ROOT_DIR, 'dashboard', 'static', 'js', ...p);
// NERO_ACTIONBAR_SRC lets the mutation battery (scripts/support/mb_mutants.js)
// point this harness at a deliberately broken copy of the module.
const ACTIONBAR_PATH = process.env.NERO_ACTIONBAR_SRC || js('embed', 'views', 'actionbar.js');
const ACTIONBAR_SOURCE = fs.readFileSync(ACTIONBAR_PATH, 'utf8');

/** Comments describe the boundaries; only CODE can breach them. */
function stripComments(src) {
    return src.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/(^|[^:])\/\/[^\n]*/g, '$1 ');
}
const CODE = stripComments(ACTIONBAR_SOURCE);
// The `window.NERO...` banner and the IIFE's closing argument are the module's
// registration, not global reads inside the implementation (every module here
// has the same wrapper).
const BODY = CODE
    .replace(/^window\.NERO[^\n]*\n/gm, '')
    .replace(/\}\)\(window\.NERO\);/g, '');

// ── sandbox: model + store + actionbar, and NOTHING else ─────────
// preview.js and drafts.js are deliberately absent: if the action bar reached
// for either, it would throw here rather than pass unnoticed.
function makeSandbox() {
    const dom = createDom();
    const sandbox = {
        window: {}, document: dom.document, console: console,
        setTimeout, clearTimeout, Promise, Object, Array, Math, Date, JSON, Number,
        String, RegExp, Error, TypeError, Set, Map, Symbol, isFinite, parseInt,
    };
    sandbox.window.document = dom.document;
    vm.createContext(sandbox);
    [js('embed', 'model.js'), js('embed', 'store.js'), ACTIONBAR_PATH].forEach(file => {
        vm.runInContext(fs.readFileSync(file, 'utf8'), sandbox, { filename: path.basename(file) });
    });
    return { dom, sandbox, NERO: sandbox.window.NERO };
}

function deepFreeze(value, seen) {
    seen = seen || new Set();
    if (value === null || typeof value !== 'object' || seen.has(value)) return value;
    seen.add(value);
    Object.freeze(value);
    Object.getOwnPropertyNames(value).forEach(key => deepFreeze(value[key], seen));
    return value;
}

const CLOCK = 1790284740000;               // 2026-09-24T12:39:00.000Z

function filledDocument(NERO, text) {
    const model = NERO.embed.model;
    const base = model.blankMessageDocument();
    return model.normalizeDocument(Object.assign({}, base, {
        content: text || 'Hello **world**',
        embeds: [Object.assign({}, base.embeds[0], {
            title: 'Title one',
            description: 'Description one',
            color: 0x5865f2,
            fields: [
                { id: 'fld_a', name: 'A', value: '1', inline: false },
                { id: 'fld_b', name: 'B', value: '2', inline: true },
            ],
        })],
    }));
}

const flush = () => new Promise(resolve => setTimeout(resolve, 0));

/**
 * A store + an action bar, with every published document frozen (so an in-place
 * write throws instead of quietly working) and a programmable clipboard. The
 * mount is attached to the document body because focus() is only meaningful on
 * an attached node — the page mounts the bar inside the real page, so the rig
 * does the same.
 */
function makeRig(options) {
    options = options || {};
    const { dom, NERO } = makeSandbox();
    const model = NERO.embed.model;
    const storeMod = NERO.embed.store;
    const document_ = options.document || filledDocument(NERO);

    const store = storeMod.createStore({
        document: document_,
        ui: { selectedNodeId: 'content' },
        reducers: storeMod.createReducers(model),
        now: () => CLOCK,
        scheduler: { setTimeout: () => 0, clearTimeout: () => {} },
    });

    const freeze = (d) => deepFreeze(d);
    freeze(store.getDocument());
    store.subscribe((state) => freeze(state.document));

    const dispatched = [];
    const rawDispatch = store.dispatch;
    store.dispatch = (action) => { dispatched.push(action); return rawDispatch(action); };

    const calls = [];            // every string handed to the clipboard
    const notices = [];          // every notice the bar reported to the page
    const nav = {
        clipboard: {
            writeText(text) { calls.push(String(text)); return Promise.resolve(); },
        },
    };

    const mount = dom.document.createElement('div');
    mount.setAttribute('id', 'mb2-bar-actions');
    dom.attach(mount);

    const bar = NERO.embed.views.actionbar.create({
        document: dom.document,
        model: options.model || model,
        store: store,
        mount: mount,
        navigator: nav,
        onNotice: function (notice) { notices.push(notice); },
    });

    function pressKey(target, key, opts) {
        opts = opts || {};
        const overlay = bar.dialog() && bar.dialog().overlay;
        const event = {
            type: 'keydown', key: key, shiftKey: !!opts.shift, target: target,
            prevented: 0,
            preventDefault() { this.prevented++; },
        };
        if (overlay) overlay.dispatch('keydown', event);
        return event;
    }

    return {
        dom, NERO, model, store, bar, mount, nav, calls, notices, dispatched,
        keyFrozen: () => Object.isFrozen(store.getDocument().embeds[0]),
        doc: () => store.getDocument(),
        payload: () => model.stableStringify(model.toDiscordPayload(store.getDocument())),
        pretty: () => JSON.stringify(model.toDiscordPayload(store.getDocument()), null, 2),
        stats: () => bar.stats(),
        button: (key) => bar.button(key),
        keys: () => bar.keys(),
        labels: () => bar.keys().map(k => (bar.button(k) || {}).textContent),
        click: (key) => {
            const node = bar.button(key);
            assert(!!node, 'rig: found the ' + key + ' button');
            if (node) mount.dispatch('click', { type: 'click', target: node });
            return node;
        },
        clickNode: (node) => mount.dispatch('click', { type: 'click', target: node }),
        openDialogRejected: async () => {
            nav.clipboard = { writeText() { return Promise.reject(new Error('permission denied')); } };
            bar.button('copy') && mount.dispatch('click', { type: 'click', target: bar.button('copy') });
            await flush();
            return bar.dialog();
        },
        pressKey: pressKey,
        dialogCount: () => mount.querySelectorAll('[role="dialog"]').length,
        ids: () => {
            const out = [];
            const all = mount.querySelectorAll('*');
            for (let i = 0; i < all.length; i++) out.push(all[i].getAttribute('id'));
            return out.filter(Boolean);
        },
    };
}

// ═══════════════════════════════════════════════════════════════
async function runAll() {
    // ─────────────────────────────────────────────────────────────
    section('A. the surface: the actions that exist, in the approved order');
    // ─────────────────────────────────────────────────────────────
    {
        const rig = makeRig();
        assert(!!rig.bar, 'actionbar.create returns an instance');
        assert(rig.keys().join(',') === 'undo,redo,copy',
            'the bar renders Undo · Redo · Copy JSON in that order', rig.keys().join(','));
        assert(rig.mount.children.length === 3,
            'and nothing else (no button for a feature that does not exist yet)',
            String(rig.mount.children.length));
        assert(rig.labels().join('|') === 'Undo|Redo|Copy JSON',
            'each button carries its text label', rig.labels().join('|'));

        rig.keys().forEach(key => {
            const node = rig.button(key);
            assert(node.getAttribute('type') === 'button',
                'the ' + key + ' button is type=button (it must never submit a form)');
            assert(node.getAttribute('data-mb2-action') === key,
                'the ' + key + ' button declares its action', String(node.getAttribute('data-mb2-action')));
            const label = String(node.textContent || '').toLowerCase();
            const name = String(node.getAttribute('aria-label') || '').toLowerCase();
            assert(name.indexOf(label) !== -1,
                'the ' + key + ' accessible name contains its visible label', name);
        });

        assert(rig.NERO.embed.views.actionbar.ORDER.undo < rig.NERO.embed.views.actionbar.ORDER.redo &&
            rig.NERO.embed.views.actionbar.ORDER.redo < rig.NERO.embed.views.actionbar.ORDER.copy,
            'the declared order keeps Save now and Discard changes insertable later');

        const ids = rig.ids();
        assert(ids.length === new Set(ids).size, 'every id in the bar is unique', ids.join(','));
        assert(rig.mount.querySelectorAll('[aria-live]').length === 0,
            'the bar declares no live region of its own (the page owns the one status region)');
        assert(rig.stats().buttonsCreated === 3, 'three buttons were created in total',
            String(rig.stats().buttonsCreated));
    }

    // ─────────────────────────────────────────────────────────────
    section('B. enabled state is real (and costs nothing when it does not change)');
    // ─────────────────────────────────────────────────────────────
    {
        const rig = makeRig();
        assert(rig.keyFrozen(), 'rig: the published document is deep-frozen (in-place writes throw)');
        assert(rig.button('undo').disabled === true && rig.button('redo').disabled === true,
            'a freshly loaded document has nothing to undo and nothing to redo');

        rig.clickNode(rig.button('redo'));
        assert(rig.stats().redos === 0 && rig.store.canRedo() === false,
            'clicking a disabled button is not an action');

        rig.store.dispatch({ type: 'content/set', text: 'typed once' });
        assert(rig.button('undo').disabled === false, 'an edit enables Undo');
        assert(rig.button('redo').disabled === true, 'and Redo stays disabled (nothing was undone)');

        // The same edit typed again: the document changes, the history does not.
        const writes = rig.stats().stateWrites;
        const renders = rig.stats().renders;
        rig.store.dispatch({ type: 'content/set', text: 'typed twice', meta: { coalesceKey: 'content' } });
        assert(rig.doc().content === 'typed twice', 'the burst edit landed in the document');
        assert(rig.stats().stateWrites === writes,
            'and wrote no button state (the change guard is doing its job)',
            String(rig.stats().stateWrites - writes));
        assert(rig.stats().renders > renders, 'although the bar did re-check the history');

        // A UI-only store change (selection) is not the bar's business either.
        const writes2 = rig.stats().stateWrites;
        rig.store.dispatch({ type: 'ui/selectNode', nodeId: rig.doc().embeds[0].id });
        assert(rig.stats().stateWrites === writes2, 'selecting a node writes no button state');

        // The document is frozen: a mutation anywhere would have thrown above,
        // and the store still hands out exactly the object it published.
        assert(Object.isFrozen(rig.doc()), 'the canonical document is still frozen after the edits');
    }

    // ─────────────────────────────────────────────────────────────
    section('C. undo/redo are the store\'s history, not the bar\'s');
    // ─────────────────────────────────────────────────────────────
    {
        const rig = makeRig();
        const dispatchesBefore = rig.dispatched.length;
        rig.click('undo');
        assert(rig.stats().undos === 0 && rig.doc().content === 'Hello **world**',
            'with nothing to undo, the click changes nothing');
        assert(rig.dispatched.length === dispatchesBefore,
            'and the bar dispatched nothing (an edit is not how history moves)');

        rig.store.dispatch({ type: 'content/set', text: 'first edit' });
        rig.store.dispatch({ type: 'content/set', text: 'second edit', meta: { coalesceKey: null } });
        const dispatchesAfterEdits = rig.dispatched.length;
        rig.click('undo');
        assert(rig.doc().content === 'first edit', 'Undo steps the canonical document back',
            rig.doc().content);
        assert(rig.dispatched.length === dispatchesAfterEdits,
            'without dispatching a single action of its own',
            String(rig.dispatched.length - dispatchesAfterEdits));
        assert(rig.button('redo').disabled === false, 'and Redo becomes available');

        rig.click('redo');
        assert(rig.doc().content === 'second edit', 'Redo moves it forward again', rig.doc().content);

        // A change made anywhere else lands in the buttons without being told.
        rig.store.dispatch({ type: 'content/set', text: 'from somewhere else' });
        assert(rig.button('undo').disabled === false, 'an external edit keeps Undo enabled');
        rig.store.undo();
        assert(rig.button('redo').disabled === false && rig.button('undo').disabled === false,
            'an external undo updates both ends of the history (store → view)');
        assert(rig.stats().undos === 1 && rig.stats().redos === 1,
            'and the bar counted only the clicks it handled',
            rig.stats().undos + '/' + rig.stats().redos);
    }

    // ─────────────────────────────────────────────────────────────
    section('D. Copy JSON: the payload, the clipboard, the notice');
    // ─────────────────────────────────────────────────────────────
    {
        const rig = makeRig();
        const payloadBefore = rig.payload();
        const dirtyBefore = rig.store.isDirty();
        const dispatchesBefore = rig.dispatched.length;

        rig.click('copy');
        await flush();

        assert(rig.calls.length === 1, 'the clipboard received exactly one write',
            String(rig.calls.length));
        const text = rig.calls[0];
        assert(text === rig.pretty(),
            'and it is the pretty-printed Discord payload of the store\'s document',
            text ? text.slice(0, 60) : String(text));
        assert(text.indexOf('\n  "') !== -1, 'pretty-printed, for a human to paste');
        assert(JSON.parse(text).content === 'Hello **world**' &&
            JSON.parse(text).embeds[0].title === 'Title one',
            'the payload is the document\'s, not a default or a blank');
        assert(rig.payload() === payloadBefore, 'copying did not change the document');
        assert(rig.store.isDirty() === dirtyBefore, 'nor its dirty state');
        assert(rig.dispatched.length === dispatchesBefore, 'and it dispatched nothing');
        assert(rig.notices.length === 1 && rig.notices[0].tone === 'ok' &&
            /copied/i.test(rig.notices[0].text),
            'the result is reported through the page notice channel',
            JSON.stringify(rig.notices[0] || null));
        assert(rig.bar.dialog() === null, 'a successful copy opens no dialog');
        assert(rig.button('copy').disabled === false, 'and leaves the button usable');

        // A second copy works, one call and one notice at a time.
        rig.click('copy');
        await flush();
        assert(rig.calls.length === 2 && rig.notices.length === 2,
            'a second copy is a second call and a second notice');

        // In flight: the button is disabled, and a second click is ignored.
        let release = null;
        rig.nav.clipboard = {
            writeText(value) {
                rig.calls.push(String(value));
                return new Promise(resolve => { release = resolve; });
            },
        };
        rig.click('copy');
        assert(rig.calls.length === 3, 'the write started', String(rig.calls.length));
        assert(rig.button('copy').disabled === true,
            'the Copy button is disabled while the write is in flight');
        rig.click('copy');
        assert(rig.calls.length === 3, 'and a second click while in flight is ignored',
            String(rig.calls.length));
        release();
        await flush();
        assert(rig.button('copy').disabled === false, 'the button comes back once the write resolves');
        assert(rig.notices.length === 3 && rig.notices[2].tone === 'ok',
            'with the notice for that write only', String(rig.notices.length));
        assert(rig.bar.dialog() === null, 'no dialog was opened for a write that succeeded');
    }

    // ─────────────────────────────────────────────────────────────
    section('E. a failed copy is a state, not a dead end');
    // ─────────────────────────────────────────────────────────────
    {
        const rig = makeRig();
        const dialog = await rig.openDialogRejected();
        assert(!!dialog && dialog.key === 'copy-fallback',
            'a rejected clipboard write opens the fallback dialog');
        const areas = dialog.panel.querySelectorAll('textarea');
        assert(areas.length === 1, 'the dialog carries one text field', String(areas.length));
        const area = areas[0];
        assert(area.value === rig.pretty(), 'holding exactly the JSON that failed to copy');
        assert(area.getAttribute('readonly') !== null,
            'and it is read-only (this is output, not an input)');
        assert(area.getAttribute('aria-label'), 'with an accessible name');
        assert(rig.dom.focused() === area, 'focused on open, so Ctrl/Cmd+C works immediately');
        assert(rig.notices.length === 1 && rig.notices[0].tone === 'warn' &&
            /clipboard/i.test(rig.notices[0].text),
            'and the reason is reported in plain words', JSON.stringify(rig.notices[0] || null));
        assert(/permission denied/.test(dialog.body.textContent),
            'including what the clipboard actually said', dialog.body.textContent);

        // A browser with no clipboard API at all is the same story.
        const rig2 = makeRig();
        rig2.nav.clipboard = null;
        rig2.click('copy');
        assert(!!rig2.bar.dialog(), 'no clipboard API also opens the fallback');
        assert(rig2.bar.dialog().panel.querySelectorAll('textarea')[0].value === rig2.pretty(),
            'with the JSON in it');
        assert(rig2.calls.length === 0, 'and nothing was attempted on a clipboard that does not exist');

        // A document the model cannot serialize must not throw at the user.
        const rig3 = makeRig({
            model: {
                toDiscordPayload() { throw new Error('circular structure'); },
            },
        });
        let threw = null;
        try { rig3.click('copy'); } catch (e) { threw = e; }
        assert(!threw, 'a serializer that throws is caught, not propagated', threw && threw.message);
        assert(rig3.bar.dialog() === null, 'no JSON dialog for a payload that was never built');
        assert(rig3.notices.length === 1 && rig3.notices[0].tone === 'danger' &&
            /circular structure/.test(rig3.notices[0].text),
            'the failure is announced in plain words instead', JSON.stringify(rig3.notices[0] || null));
    }

    // ─────────────────────────────────────────────────────────────
    section('F. dialog semantics: one at a time, labelled, trapped, escapable');
    // ─────────────────────────────────────────────────────────────
    {
        const rig = makeRig();
        const dialog = await rig.openDialogRejected();
        const panel = dialog.panel;
        assert(panel.getAttribute('role') === 'dialog', 'the dialog declares role=dialog');
        assert(panel.getAttribute('aria-modal') === 'true', 'and is modal to assistive tech');
        assert(panel.getAttribute('aria-labelledby') === dialog.title.getAttribute('id') &&
            dialog.title.getAttribute('id') === 'mb2-dialog-title',
            'labelled by its own title', String(panel.getAttribute('aria-labelledby')));
        assert(panel.getAttribute('aria-describedby') === dialog.body.getAttribute('id') &&
            dialog.body.getAttribute('id') === 'mb2-dialog-body',
            'and described by its own body text');
        assert(!!dialog.title.textContent && dialog.title.textContent.length > 3,
            'the title says what the dialog is for', dialog.title.textContent);
        const ids = rig.ids();
        assert(ids.length === new Set(ids).size, 'ids stay unique while the dialog is open', ids.join(','));
        assert(rig.dialogCount() === 1, 'exactly one dialog is mounted', String(rig.dialogCount()));

        // Opening again replaces it: never two.
        rig.click('copy');
        await flush();
        assert(rig.dialogCount() === 1, 'opening a second time leaves exactly one dialog mounted',
            String(rig.dialogCount()));
        assert(rig.mount.children.length === 4, 'the bar is three buttons and one overlay',
            String(rig.mount.children.length));

        const dialog2 = rig.bar.dialog();
        const area = dialog2.panel.querySelectorAll('textarea')[0];
        const close = dialog2.close;
        assert(area && close && dialog2.panel.contains(close),
            'rig: the dialog has a text field and a close button');

        // Tab wraps at both ends and is left alone in the middle.
        const fromLast = rig.pressKey(close, 'Tab');
        assert(fromLast.prevented === 1 && rig.dom.focused() === area,
            'Tab from the last control wraps to the first');
        const backToFirst = rig.pressKey(area, 'Tab', { shift: true });
        assert(backToFirst.prevented === 1 && rig.dom.focused() === close,
            'Shift+Tab from the first control wraps to the last');
        const middle = rig.pressKey(area, 'Tab');
        assert(middle.prevented === 0,
            'a Tab that does not need wrapping is left to the browser');

        // A click inside the panel is not a dismissal.
        dialog2.overlay.dispatch('click', { type: 'click', target: dialog2.title });
        assert(!!rig.bar.dialog(), 'clicking inside the panel does not close it');

        // Escape closes it and returns focus to the invoking button.
        const escape = rig.pressKey(area, 'Escape');
        assert(escape.prevented === 1, 'Escape is consumed by the dialog');
        assert(rig.bar.dialog() === null, 'Escape closes the dialog');
        assert(rig.dialogCount() === 0, 'and removes it from the DOM', String(rig.dialogCount()));
        assert(rig.mount.children.length === 3, 'leaving the three buttons behind',
            String(rig.mount.children.length));
        assert(rig.dom.focused() === rig.button('copy'),
            'focus returns to the button that opened it');

        // The backdrop and the close button are the other two ways out.
        await rig.openDialogRejected();
        const third = rig.bar.dialog();
        third.overlay.dispatch('click', { type: 'click', target: third.overlay });
        assert(rig.bar.dialog() === null, 'clicking the backdrop closes the dialog');
        assert(rig.dom.focused() === rig.button('copy'), 'and focus comes back too');

        await rig.openDialogRejected();
        const fourth = rig.bar.dialog();
        fourth.overlay.dispatch('click', { type: 'click', target: fourth.close });
        assert(rig.bar.dialog() === null, 'the Close button closes the dialog');
        assert(rig.dialogCount() === 0, 'no dialog is left behind', String(rig.dialogCount()));
    }

    // ─────────────────────────────────────────────────────────────
    section('G. teardown releases everything it took');
    // ─────────────────────────────────────────────────────────────
    {
        const rig = makeRig();
        const listenersBefore = rig.store._subscriberCounts().listeners;
        assert(listenersBefore >= 1, 'the bar subscribes to the store',
            String(listenersBefore));
        await rig.openDialogRejected();
        assert(!!rig.bar.dialog(), 'rig: a dialog is open before teardown');

        assert(rig.bar.destroy() === true, 'destroy() reports that it did something');
        assert(rig.mount.children.length === 0, 'and empties the bar',
            String(rig.mount.children.length));
        assert(rig.bar.dialog() === null, 'the open dialog is gone with it');
        assert(rig.store._subscriberCounts().listeners === listenersBefore - 1,
            'and the subscription is released',
            String(rig.store._subscriberCounts().listeners));

        assert(rig.bar.destroy() === false, 'destroy() is idempotent');

        let threw = null;
        try {
            rig.store.dispatch({ type: 'content/set', text: 'after teardown' });
            rig.store.undo();
            rig.clickNode(rig.mount);
        } catch (e) { threw = e; }
        assert(!threw, 'a store change after teardown neither throws nor writes',
            threw && threw.message);
        assert(rig.mount.children.length === 0, 'the bar stays empty');
        assert(rig.stats().buttonsCreated === 3, 'and nothing was rebuilt');
    }

    // ─────────────────────────────────────────────────────────────
    section('H. boundaries: what this file may not touch');
    // ─────────────────────────────────────────────────────────────
    {
        const rig = makeRig();
        assert(!rig.NERO.embed.preview && !rig.NERO.embed.drafts,
            'the module runs with neither the preview nor the draft session loaded');

        assert(!/indexedDB|localStorage|sessionStorage/.test(CODE),
            'the bar never opens storage (persistence belongs to drafts.js)');
        assert(!/NERO\.embed\.(preview|drafts)/.test(CODE),
            'it never reaches for the renderer or the session');
        assert(!/mb2-bar-status|aria-live|role="status"/.test(CODE),
            'it never writes into the status region and creates no live region of its own');
        assert(!/maxlength|maxLength|\bvalidat|truncat|\bcounters?\b|\blimits?\b/i.test(CODE),
            'no validation vocabulary leaked in (the strip belongs to step 6)');
        assert(!/(getState\(\)|state)\.[A-Za-z_.]+\s*(=[^=]|\+=|-=)/.test(CODE),
            'it never assigns into store state');
        assert(!/getDocument\(\)\s*\.\s*[A-Za-z_$]+\s*=[^=]/.test(CODE),
            'and never writes into the document it read');
        assert(!/\b(cloneDocument|stableStringify|normalizeDocument|blankMessageDocument)\b/.test(CODE),
            'no document is built or copied here — the store owns the only one');
        assert(!/\bwindow\./.test(BODY) && !/\bdocument\.(getElementById|querySelector|body|createElement)\b/.test(BODY),
            'no global window/document reads: the document comes from options');
        assert(!/key === 'z'|key === 'y'|ctrlKey|metaKey/i.test(CODE),
            'no undo/redo accelerators in the action bar (page-level, deferred to 5e)');
        assert(!/openDialog\(/.test(BODY.replace(/function openDialog[\s\S]*?\n        \}/, '')) ||
            /onDialogClick|onDialogKeydown/.test(BODY),
            'dialogs are managed by the bar\'s own handlers');

        // Every mounted action has a handler behind it.
        ['undo', 'redo', 'copy'].forEach(key => {
            assert(new RegExp("key === '" + key + "'").test(CODE),
                'the ' + key + ' button is wired to a handler');
        });
    }

    // ─────────────────────────────────────────────────────────────
    section('I. identity: rendering does not rebuild anything');
    // ─────────────────────────────────────────────────────────────
    {
        const rig = makeRig();
        const snapshot = [];
        for (let i = 0; i < rig.mount.children.length; i++) snapshot.push(rig.mount.children[i]);

        rig.bar.render();
        rig.bar.render();
        rig.store.dispatch({ type: 'content/set', text: 'still the same buttons' });
        rig.store.undo();

        const after = [];
        for (let i = 0; i < rig.mount.children.length; i++) after.push(rig.mount.children[i]);
        assert(after.length === snapshot.length && after.every((node, i) => node === snapshot[i]),
            'the same button nodes survive every render',
            after.length + '/' + snapshot.length);

        const created = rig.stats().buttonsCreated;
        rig.click('copy');
        await flush();
        assert(rig.stats().buttonsCreated === created,
            'a copy adds no button', String(rig.stats().buttonsCreated));
        assert(rig.mount.children.length === 3, 'and no other node either',
            String(rig.mount.children.length));

        // The unsupported-action path: an event with no target, or an unknown
        // one, is ignored rather than guessed at.
        const before = rig.stats();
        rig.mount.dispatch('click', { type: 'click' });
        rig.mount.dispatch('click', { type: 'click', target: rig.dom.document.createElement('button') });
        rig.mount.dispatch('click', { type: 'click', target: rig.mount });
        const now = rig.stats();
        assert(now.undos === before.undos && now.redos === before.redos &&
            now.copyAttempts === before.copyAttempts,
            'unrelated clicks are ignored', JSON.stringify(now));
    }
}

let aborted = null;
runAll().catch(err => {
    aborted = err;
    fail++;
    failures.push('section ' + currentSection + ' aborted: ' + ((err && err.message) || String(err)));
    console.log('\n  FAIL  section ' + currentSection + ' aborted before its checks completed');
    console.log('        ' + ((err && err.stack) || String(err)).split('\n').slice(0, 4).join('\n        '));
}).then(() => {
    console.log('\nmessage-builder actionbar: ' + pass + ' passed, ' + fail + ' failed');
    if (fail) {
        console.log('Failures:');
        failures.forEach(f => console.log(' -', f));
        process.exit(1);
    }
    console.log('ALL MESSAGE-BUILDER ACTIONBAR CHECKS PASSED');
});
