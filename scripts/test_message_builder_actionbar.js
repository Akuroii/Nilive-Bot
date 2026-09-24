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

    // The discard capability is the PAGE's, so the rig supplies one exactly as
    // the page does: a question and an action, never a persistence API.
    const discards = [];
    // No capability by default (that is the 5d-1 bar); `discard: true` wires the
    // page-shaped one, `discardable()` makes it available.
    const discard = options.discard === true
        ? {
            available() { return !!(options.discardable ? options.discardable() : false); },
            perform() { discards.push(Date.now()); return true; },
        }
        : (options.discard || null);

    const bar = NERO.embed.views.actionbar.create({
        document: dom.document,
        model: options.model || model,
        store: store,
        mount: mount,
        navigator: nav,
        onNotice: function (notice) { notices.push(notice); },
        discard: discard || undefined,
        // The save capability is the page's too: an action, never a session.
        save: options.save || undefined,
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
        dom, NERO, model, store, bar, mount, nav, calls, notices, dispatched, discards,
        discardPrompts: () => discards.length,
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
        // The save control extends this rule rather than weakening it: the bar
        // renders a capability, it does not perform a write. Nothing in this
        // file may name a persistence entry point, or the button would be a
        // second write path wearing a label.
        assert(!/\bsaveNow\b|\.put\(|buildRecord|draftKey|markSaved/.test(CODE),
            'no persistence call of its own: Save now only calls the page\'s capability',
            (CODE.match(/\bsaveNow\b|\.put\(|buildRecord|draftKey|markSaved/) || [''])[0]);
        assert(!/resolveGuard|\.storage\(\)|forceReplace/.test(CODE),
            'and nothing about the guard or the adapter: this step ships no guard UI');
        assert(!/isDirty|hashDocument|\.state\(\)/.test(CODE),
            'the save control reads the facts the page already computed — it never asks the store or the session itself');
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

    // ─────────────────────────────────────────────────────────────
    section('J. Discard changes: the button, the confirmation, the capability');
    // ─────────────────────────────────────────────────────────────
    {
        // Without a capability there is no button: the bar never renders a dead
        // action for something the page has not wired.
        const bare = makeRig();
        assert(bare.keys().indexOf('discard') === -1,
            'a bar with no discard capability renders no discard button', bare.keys().join(','));
        bare.mount.dispatch('click', { type: 'click', target: bare.mount });
        assert(bare.discardPrompts() === 0, 'and nothing can prompt for one');

        // With the capability (the page's shape): the button exists, in order.
        let discardable = false;
        const rig = makeRig({ discard: true, discardable: () => discardable });
        assert(rig.keys().join(',') === 'undo,redo,copy,discard',
            'the discard button comes LAST (it is the destructive one)', rig.keys().join(','));
        const button = rig.button('discard');
        assert(button.getAttribute('type') === 'button' &&
            button.getAttribute('data-mb2-action') === 'discard' &&
            button.textContent === 'Discard changes',
            'it is a real labelled button', String(button.textContent));
        assert(String(button.getAttribute('aria-label')).indexOf('Discard changes') === 0,
            'whose accessible name contains the visible label', String(button.getAttribute('aria-label')));
        assert(button.disabled === true,
            'with nothing to discard it is unavailable');
        // A disabled control can still receive a synthetic click (a stray event,
        // a programmatic dispatch), so the guard has to be in the handler too.
        rig.clickNode(button);
        assert(rig.bar.dialog() === null && rig.discardPrompts() === 0,
            'and a click delivered while it is unavailable neither prompts nor acts',
            String(rig.discardPrompts()));

        discardable = true;
        rig.bar.refresh();
        assert(button.disabled === false, 'and becomes available when there is something to discard');

        // Clicking asks first. Nothing is discarded by a click alone.
        const dispatchesBefore = rig.dispatched.length;
        rig.click('discard');
        const dialog = rig.bar.dialog();
        assert(!!dialog && dialog.key === 'discard', 'clicking it opens the confirmation');
        assert(rig.discardPrompts() === 0, 'and discards nothing yet',
            String(rig.discardPrompts()));
        assert(rig.dialogCount() === 1, 'one dialog, as always', String(rig.dialogCount()));
        assert(dialog.panel.getAttribute('role') === 'dialog' &&
            dialog.panel.getAttribute('aria-modal') === 'true' &&
            dialog.panel.getAttribute('aria-labelledby') === dialog.title.getAttribute('id'),
            'the confirmation is a real modal dialog, labelled by its title');
        assert(/discard/i.test(dialog.title.textContent),
            'the title asks the question', dialog.title.textContent);
        assert(/not deleted/i.test(dialog.body.textContent),
            'and the body says what will NOT happen (the saved draft survives)',
            dialog.body.textContent);
        assert(!!dialog.confirm && dialog.confirm.textContent === 'Discard changes',
            'the confirmation has a destructive button, labelled in words',
            dialog.confirm && String(dialog.confirm.textContent));
        assert(rig.dom.focused() === dialog.close,
            'focus lands on the SAFE control (keep editing), never on the destructive one',
            rig.dom.focused() ? String(rig.dom.focused().textContent) : 'nothing focused');

        // Cancel / Escape / backdrop all mean "do nothing".
        dialog.overlay.dispatch('click', { type: 'click', target: dialog.close });
        assert(rig.bar.dialog() === null && rig.discardPrompts() === 0,
            'cancelling performs nothing', String(rig.discardPrompts()));
        assert(rig.dom.focused() === button, 'and focus returns to the button that asked');
        rig.click('discard');
        const viaEscape = rig.bar.dialog();
        rig.pressKey(viaEscape.close, 'Escape');
        assert(rig.bar.dialog() === null && rig.discardPrompts() === 0,
            'Escape performs nothing either', String(rig.discardPrompts()));
        rig.click('discard');
        const viaBackdrop = rig.bar.dialog();
        viaBackdrop.overlay.dispatch('click', { type: 'click', target: viaBackdrop.overlay });
        assert(rig.discardPrompts() === 0, 'and neither does the backdrop');

        // Confirming runs the capability's action exactly once, and the bar
        // itself still never touches the document.
        rig.click('discard');
        const confirmed = rig.bar.dialog();
        confirmed.overlay.dispatch('click', { type: 'click', target: confirmed.confirm });
        assert(rig.discardPrompts() === 1, 'confirming runs the page\'s discard exactly once',
            String(rig.discardPrompts()));
        assert(rig.bar.dialog() === null, 'and closes the dialog');
        assert(rig.bar.stats().confirms === 1, 'counted as a confirmation',
            String(rig.bar.stats().confirms));
        assert(rig.dispatched.length === dispatchesBefore,
            'the bar dispatched nothing of its own (the page owns the document)',
            String(rig.dispatched.length - dispatchesBefore));
        assert(rig.doc().content === 'Hello **world**',
            'and the document was not built or replaced here', rig.doc().content);

        // Availability can change between opening and confirming: the action is
        // re-checked at the moment it would run.
        rig.click('discard');
        const raced = rig.bar.dialog();
        discardable = false;
        rig.bar.refresh();
        raced.overlay.dispatch('click', { type: 'click', target: raced.confirm });
        assert(rig.discardPrompts() === 1,
            'a discard that became unavailable while the dialog was open does not run',
            String(rig.discardPrompts()));

        // The clipboard fallback is not a confirmation: it must have no
        // destructive button of its own.
        rig.nav.clipboard = { writeText() { return Promise.reject(new Error('denied')); } };
        rig.click('copy');
        await flush();
        const fallback = rig.bar.dialog();
        assert(!!fallback && fallback.key === 'copy-fallback', 'the copy fallback still opens');
        assert(fallback.confirm === null && fallback.actions.children.length === 1,
            'and it offers exactly one action (Close)', String(fallback.actions.children.length));
        fallback.overlay.dispatch('click', { type: 'click', target: fallback.close });

        // Teardown with a dialog open.
        discardable = true;
        rig.bar.refresh();
        rig.click('discard');
        assert(!!rig.bar.dialog(), 'rig: a confirmation is open');
        rig.bar.destroy();
        assert(rig.bar.dialog() === null && rig.mount.children.length === 0,
            'destroy() takes an open confirmation with it',
            String(rig.mount.children.length));
        let threw = null;
        try { discardable = true; rig.bar.refresh(); } catch (e) { threw = e; }
        assert(!threw, 'and renders nothing afterwards', threw && threw.message);
    }
    // ─────────────────────────────────────────────────────────────
    section('K. Save now: the one contextual control (and when there is no retry)');
    // ─────────────────────────────────────────────────────────────
    {
        const describeSave = makeSandbox().NERO.embed.views.actionbar.describeSave;
        const facts = (over) => Object.assign({
            dirty: false, pending: false, retryable: false,
            session: { blocked: null, saving: false, lastError: null, degraded: null, writes: 0 },
        }, over || {});
        const sessionWith = (over) => Object.assign(
            { blocked: null, saving: false, lastError: null, degraded: null, writes: 0 }, over || {});

        // ── the pure mapping: every state, and what it must NOT say ──
        const cases = [
            ['nothing has ever been written', facts(), 'Save now', false, 'clean'],
            ['dirty (something to save)', facts({ dirty: true }), 'Save now', true, 'dirty'],
            ['a write is queued but the store is clean',
                facts({ pending: true }), 'Save now', true, 'dirty'],
            ['a save is in flight',
                facts({ dirty: true, session: sessionWith({ saving: true }) }), 'Saving\u2026', false, 'saving'],
            ['the write was confirmed',
                facts({ session: sessionWith({ writes: 3 }) }), 'Saved', false, 'saved'],
            ['a TRANSIENT failure',
                facts({ dirty: true, retryable: true, session: sessionWith({ lastError: { reason: 'write-error' } }) }),
                'Try saving again', true, 'retry'],
            ['an ENVIRONMENT failure (no IndexedDB)',
                facts({ dirty: true, session: sessionWith({ lastError: { reason: 'no-indexeddb' }, degraded: 'no-indexeddb' }) }),
                'Save now', false, 'unavailable'],
            ['an open timeout',
                facts({ dirty: true, session: sessionWith({ lastError: { reason: 'open-timeout' }, degraded: 'open-timeout' }) }),
                'Save now', false, 'unavailable'],
            ['a document that cannot be serialized',
                facts({ dirty: true, session: sessionWith({ lastError: { reason: 'not-serializable' } }) }),
                'Save now', false, 'unavailable'],
            ['storage known unusable before any write',
                facts({ dirty: true, session: sessionWith({ degraded: 'no-indexeddb' }) }),
                'Save now', false, 'unavailable'],
            ['a protected record (the guard is up)',
                facts({ dirty: true, session: sessionWith({ blocked: 'corrupt-record' }) }),
                'Save now', false, 'blocked'],
            // A transient latch and nothing owed: there is nothing to save, so
            // the control is unavailable — but it is NOT the environment case
            // below, because a retry from here would work.
            ['a transient latch with nothing owed yet',
                facts({ retryable: true, session: sessionWith({ degraded: 'write-error', writes: 0 }) }),
                'Save now', false, 'clean'],
            // …and the same latch WITH something to save: the click is what
            // gives the storage its one chance to come back.
            ['a transient latch with an edit to save',
                facts({ dirty: true, retryable: true, session: sessionWith({ degraded: 'write-error', writes: 0 }) }),
                'Save now', true, 'dirty'],
        ];
        cases.forEach((c) => {
            const got = describeSave(c[1]);
            assert(got.label === c[2] && got.enabled === c[3] && got.state === c[4],
                'mapping — ' + c[0] + ': "' + c[2] + '"' + (c[3] ? ' (available)' : ' (unavailable)'),
                JSON.stringify(got));
        });
        // The requirement, stated on its own so it cannot be softened by a
        // refactor: no failure that a retry cannot fix may ever offer a retry.
        ['no-indexeddb', 'open-timeout', 'open-error', 'upgrade-failed', 'not-serializable'].forEach((reason) => {
            const got = describeSave(facts({
                dirty: true, retryable: false,
                session: sessionWith({ lastError: { reason: reason }, degraded: reason }),
            }));
            assert(got.label !== 'Try saving again' && got.state !== 'retry' && got.enabled === false,
                'a non-retryable failure (' + reason + ') offers NO retry action',
                JSON.stringify(got));
        });
        // …and the honest other half: a retryable one does.
        ['write-timeout', 'write-error', 'write-aborted', 'transaction-failed', 'request-failed'].forEach((reason) => {
            const got = describeSave(facts({
                dirty: true, retryable: true,
                session: sessionWith({ lastError: { reason: reason }, degraded: reason }),
            }));
            assert(got.label === 'Try saving again' && got.enabled === true && got.state === 'retry',
                'a transient failure (' + reason + ') does offer a retry', JSON.stringify(got));
        });
        assert(describeSave().state === 'clean',
            'the mapping tolerates no facts at all (it never throws)');

        // ── the button: only when the page wired it ──
        const bare = makeRig();
        assert(bare.keys().indexOf('save') === -1,
            'a bar with no save capability renders no save button', bare.keys().join(','));
        bare.mount.dispatch('click', { type: 'click', target: bare.mount });
        assert(bare.stats().saves === 0, 'and nothing can start a save');

        // ── the wired button: declared position, honest initial state ──
        const saveCalls = [];
        const rig = makeRig({
            save: { perform() { saveCalls.push(Date.now()); return Promise.resolve({ ok: true }); } },
            discard: true,
        });
        assert(rig.keys().join(',') === 'undo,redo,save,copy,discard',
            'Save now sits between Redo and Copy JSON (the approved order)', rig.keys().join(','));
        const button = rig.button('save');
        assert(button.getAttribute('type') === 'button' &&
            button.getAttribute('data-mb2-action') === 'save',
            'it is a real button of the save action');
        assert(button.textContent === 'Save now' && button.disabled === true,
            'it starts as a DISABLED "Save now": nothing has told it there is anything to save yet',
            String(button.textContent) + '/' + button.disabled);

        // ── transitions through renderSave ──
        const steps = [
            [facts({ dirty: false }), 'Save now', true, 'clean'],
            [facts({ dirty: true }), 'Save now', false, 'dirty'],
            [facts({ dirty: true, session: sessionWith({ saving: true }) }), 'Saving\u2026', true, 'saving'],
            [facts({ session: sessionWith({ writes: 1 }) }), 'Saved', true, 'saved'],
            [facts({ dirty: true, retryable: true, session: sessionWith({ lastError: { reason: 'write-error' } }) }),
                'Try saving again', false, 'retry'],
            [facts({ dirty: true, session: sessionWith({ lastError: { reason: 'no-indexeddb' }, degraded: 'no-indexeddb' }) }),
                'Save now', true, 'unavailable'],
            [facts({ dirty: true }), 'Save now', false, 'dirty'],
            [facts({ session: sessionWith({ writes: 1 }) }), 'Saved', true, 'saved'],
        ];
        steps.forEach((s, i) => {
            rig.bar.renderSave(s[0]);
            assert(button.textContent === s[1] && button.disabled === s[2] &&
                button.getAttribute('data-mb2-save-state') === s[3],
                'step ' + (i + 1) + ': the control reads "' + s[1] + '"' + (s[2] ? ' (unavailable)' : ' (available)'),
                String(button.textContent) + '/' + button.disabled + '/' + button.getAttribute('data-mb2-save-state'));
        });
        assert(String(button.getAttribute('title')).length > 0 &&
            String(button.getAttribute('aria-label')) === button.textContent,
            'the accessible name is the visible label, and the tooltip explains the state',
            String(button.getAttribute('aria-label')) + ' | ' + String(button.getAttribute('title')));

        // ── clicking runs the page's action, exactly once per click ──
        rig.bar.renderSave(facts({ dirty: true }));
        assert(button.disabled === false, 'rig: there is something to save');
        const beforeClick = rig.stats();
        rig.click('save');
        assert(saveCalls.length === 1 && rig.stats().saves === 1,
            'clicking calls the page\'s save exactly once',
            saveCalls.length + '/' + rig.stats().saves);
        assert(rig.stats().savePresses === beforeClick.savePresses + 1,
            'and the press is counted', String(rig.stats().savePresses));
        assert(rig.dispatched.length === 0,
            'the bar dispatched nothing: the page owns the document and the write',
            String(rig.dispatched.length));
        assert(rig.doc().content === 'Hello **world**',
            'and the document is untouched by the bar', rig.doc().content);

        // ── unavailable means unavailable, even for a synthetic click ──
        rig.bar.renderSave(facts({ dirty: true, session: sessionWith({ lastError: { reason: 'no-indexeddb' }, degraded: 'no-indexeddb' }) }));
        rig.clickNode(button);
        assert(rig.stats().savePresses === beforeClick.savePresses + 2 && rig.stats().saves === 1,
            'a click delivered while it is unavailable is SEEN but starts no save',
            rig.stats().savePresses + ' presses / ' + rig.stats().saves + ' saves');
        assert(saveCalls.length === 1, 'and the page\'s action did not run', String(saveCalls.length));
        rig.bar.renderSave(facts({ dirty: true, session: sessionWith({ saving: true }) }));
        rig.clickNode(button);
        assert(saveCalls.length === 1, 'nor does a click while a save is already in flight',
            String(saveCalls.length));
        rig.bar.renderSave(facts({ session: sessionWith({ blocked: 'corrupt-record' }) }));
        rig.clickNode(button);
        assert(saveCalls.length === 1, 'nor over a protected record', String(saveCalls.length));

        // ── listener hygiene: one listener however often the bar renders ──
        for (let i = 0; i < 25; i++) {
            rig.bar.renderSave(facts({ dirty: i % 2 === 0 }));
            rig.bar.refresh();
        }
        rig.bar.renderSave(facts({ dirty: true }));
        const beforeMany = saveCalls.length;
        rig.click('save');
        assert(saveCalls.length === beforeMany + 1,
            'after 25 re-renders one click still means ONE save (no duplicated listeners)',
            String(saveCalls.length - beforeMany));
        rig.click('save');
        rig.click('save');
        assert(saveCalls.length === beforeMany + 3,
            'and every further click means exactly one more', String(saveCalls.length - beforeMany));

        // ── a keystroke must not cost writes ──
        const writesBefore = rig.stats().stateWrites;
        const labelsBefore = rig.stats().saveLabels;
        for (let i = 0; i < 20; i++) rig.bar.renderSave(facts({ dirty: true }));
        assert(rig.stats().stateWrites === writesBefore && rig.stats().saveLabels === labelsBefore,
            'rendering the same state 20 times writes nothing (a keystroke is not a DOM write)',
            (rig.stats().stateWrites - writesBefore) + '/' + (rig.stats().saveLabels - labelsBefore));

        // ── a perform() that throws or rejects must not break the bar ──
        const rough = makeRig({
            save: { perform() { throw new Error('the page exploded'); } },
            discard: true,
        });
        rough.bar.renderSave(facts({ dirty: true }));
        let threw = null;
        try { rough.click('save'); } catch (e) { threw = e; }
        assert(!threw, 'a save that throws does not take the bar down with it', threw && threw.message);
        assert(rough.button('copy').disabled === false && rough.keys().length === 5,
            'and the rest of the bar still works');
        rough.bar.destroy();

        const rejecting = makeRig({
            save: { perform() { return Promise.reject(new Error('nope')); } },
            discard: true,
        });
        rejecting.bar.renderSave(facts({ dirty: true }));
        rejecting.click('save');
        await flush();
        assert(rejecting.button('undo') !== null && rejecting.bar.stats().saves === 1,
            'and a rejected save is swallowed (the session reports its own failures)',
            String(rejecting.bar.stats().saves));
        rejecting.bar.destroy();

        // ── one listener, however many times the bar renders ──
        const clickListeners = () => rig.mount.listeners.filter(l => l.type === 'click').length;
        assert(clickListeners() === 1,
            'the mount carries exactly ONE click listener', String(clickListeners()));
        rig.bar.renderSave(facts({ dirty: true }));
        rig.bar.refresh();
        rig.bar.renderSave(facts({ dirty: true }));
        assert(clickListeners() === 1,
            'and rendering (or re-rendering the save state) never adds another',
            String(clickListeners()));
        const beforeOneListener = saveCalls.length;
        rig.click('save');
        assert(saveCalls.length === beforeOneListener + 1,
            'so one click is still one save after all of that',
            String(saveCalls.length - beforeOneListener));

        // ── teardown ──
        rig.bar.renderSave(facts({ dirty: true }));
        rig.bar.destroy();
        assert(rig.mount.children.length === 0, 'destroy() removes the button',
            String(rig.mount.children.length));
        assert(clickListeners() === 0,
            'and its click listener with it', String(clickListeners()));
        assert(rig.bar.renderSave(facts({ dirty: true })) === false,
            'and rendering afterwards is a no-op, not a crash');
        let afterThrew = null;
        try { rig.clickNode(button); } catch (e) { afterThrew = e; }
        assert(!afterThrew, 'a stray click on the removed node does nothing', afterThrew && afterThrew.message);
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
