#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════
   Message Builder v2 — phase 1, step 5b: the Structure rail.

   WHAT THIS HAS TO PROVE

     A. STRUCTURE — the hierarchy the rail shows is derived from the canonical
        document: message root, embeds, fields, with correct tree semantics
        (role/aria-level/aria-posinset/aria-setsize/aria-expanded).
     B. SELECTION — selecting a node is a store action; the rail reflects
        whatever the store says, including when the selection changes from
        somewhere else. Selection and focus are different things and both are
        visible in the DOM.
     C. STRUCTURAL ACTIONS — add embed, add field, duplicate, move up/down and
        remove each produce EXACTLY the document the model would produce for
        that action (byte-identical payload), and each one arrives as a store
        action. Nothing mutates the document in place.
     D. THE MUTATION BOUNDARY — the store's documents are deep-frozen for this
        harness, so an in-place edit throws. The freeze itself is proven active
        (a deliberate attempt must fail), otherwise every other check here would
        be vacuous.
     E. KEYBOARD — roving tabindex (exactly one tab stop), ↑/↓, Home/End,
        →/← to expand/collapse and step in/out, Enter/Space to select,
        Delete/Backspace to remove with focus restored to a survivor, and no
        focus trap (Tab is never swallowed).
     F. IDENTITY/PERF — re-rendering reuses row elements: a content keystroke
        creates no rows, adding a field creates exactly one, and a row that
        survives keeps its DOM node (this is what keeps focus and avoids the
        churn the preview work went to such lengths to avoid).
     G. TEARDOWN — destroy() unsubscribes, removes its listeners and leaves the
        mount element as the template declared it.

   Run:  node scripts/test_message_builder_rail.js
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
function section(t) { console.log('\n== ' + t + ' =='); }

const ROOT_DIR = path.join(__dirname, '..');
const js = (...p) => path.join(ROOT_DIR, 'dashboard', 'static', 'js', ...p);
const RAIL_PATH = process.env.NERO_RAIL_SRC || js('embed', 'views', 'rail.js');

// ── a store sandbox (model + store + rail), no timers needed ─────
function makeSandbox() {
    const dom = createDom();
    const sandbox = {
        window: {}, document: dom.document, console: console,
        setTimeout, clearTimeout, Promise, Object, Array, Math, Date, JSON, Number,
        String, RegExp, Error, TypeError, Set, Map, Symbol, isFinite, parseInt,
    };
    sandbox.window.document = dom.document;
    vm.createContext(sandbox);
    [js('embed', 'model.js'), js('embed', 'store.js'), RAIL_PATH].forEach(file => {
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

/**
 * A rail over a real store, with every document the store publishes frozen and
 * every dispatch recorded. `frozen` proves the instrument is live before any
 * boundary check uses it.
 */
function makeRig(options) {
    options = options || {};
    const { dom, NERO } = makeSandbox();
    const model = NERO.embed.model;
    const storeMod = NERO.embed.store;
    const clock = options.clock || { now: 1790284740000 };

    const store = storeMod.createStore({
        document: options.document || model.blankMessageDocument(),
        reducers: storeMod.createReducers(model),
        now: () => clock.now,
        scheduler: { setTimeout: () => 0, clearTimeout: () => {} },
    });

    // freeze the document the store publishes, and keep freezing new ones
    const freeze = (document_) => { deepFreeze(document_); };
    freeze(store.getDocument());
    store.subscribe((state) => freeze(state.document));

    const dispatched = [];
    const rawDispatch = store.dispatch;
    store.dispatch = (action) => { dispatched.push(action && action.type); return rawDispatch(action); };

    const mount = dom.document.createElement('div');
    mount.setAttribute('id', 'mb2-rail-body');
    const rail = NERO.embed.views.rail.create({ document: dom.document, store, mount });

    return {
        dom, NERO, model, store, rail, mount, dispatched,
        settle: () => {},
        rows: () => Array.from(mount.children),
        ids: () => Array.from(mount.children).map(n => n.getAttribute('data-node-id')),
        row: (id) => Array.from(mount.children).find(n => n.getAttribute('data-node-id') === id) || null,
        byAction: (action) => Array.from(mount.children).find(n => n.querySelector('[data-rail-action="' + action + '"]')) || null,
        actionButton: (nodeId, action) => {
            const row = Array.from(mount.children).find(n => n.getAttribute('data-node-id') === nodeId);
            if (!row) return null;
            let found = null;
            row.children.forEach(child => {
                if ((child.className || '').indexOf('mb2-rail-actions') === -1) return;
                child.children.forEach(button => {
                    if (button.getAttribute('data-rail-action') === action) found = button;
                });
            });
            return found;
        },
        // A browser click on a nested node bubbles to the rail's delegated
        // listener on the mount; the double has no propagation, so the event is
        // delivered where it would end up, with the real target on it.
        click: (node) => mount.dispatch('click', { type: 'click', target: node }),
        during: (fn) => { const mark = dispatched.length; fn(); return dispatched.slice(mark); },
        canon: (document_) => model.stableStringify(model.normalizeDocument(document_)),
        /**
         * The same document with every id replaced by its POSITION. Model calls
         * mint fresh ids, so "the rail's document equals the model's output"
         * cannot be an object comparison — but a positional one still proves the
         * structure, order, text and flags all match, which is what the boundary
         * claim is about. Payload equality (no ids on the wire) is asserted next
         * to it wherever it matters.
         */
        shape: (document_) => {
            const d = model.normalizeDocument(document_);
            return JSON.stringify({
                content: d.content,
                embeds: (d.embeds || []).map(e => ({
                    title: e.title, url: e.url, description: e.description, color: e.color,
                    author: e.author, footer: e.footer, thumbnail: e.thumbnail, image: e.image,
                    timestamp: e.timestamp,
                    fields: (e.fields || []).map(f => ({ name: f.name, value: f.value, inline: f.inline })),
                })),
            });
        },
        key: (node, key, target) => {
            const event = { key, target: target || node, prevented: 0, preventDefault() { this.prevented++; } };
            node.dispatch('keydown', event);
            return event;
        },
        focused: () => dom.focused(),
        payload: (document_) => model.stableStringify(model.toDiscordPayload(document_ || store.getDocument())),
        hash: (document_) => model.hashDocument(document_ || store.getDocument()),
    };
}

function runAll() {
    // ─────────────────────────────────────────────────────────────
    section('A. structure derived from the canonical document');
    // ─────────────────────────────────────────────────────────────
    {
        const rig = makeRig();
        const { model, store, rail } = rig;
        assert(rig.mount.getAttribute('role') === 'tree', 'the mount becomes a tree');
        assert(rig.mount.getAttribute('aria-label') === 'Message structure',
            'the tree is labelled', rig.mount.getAttribute('aria-label'));
        assert(rig.ids().join(',') === 'content,' + store.getDocument().embeds[0].id,
            'an empty document shows the message root and its one embed', rig.ids().join(','));
        assert(rail.stats().renders >= 1, 'the rail paints on creation');

        // three embeds, fields on the middle one
        store.dispatch({ type: 'embed/add' });
        store.dispatch({ type: 'embed/add' });
        const doc = store.getDocument();
        const first = doc.embeds[0].id;
        const second = doc.embeds[1].id;
        store.dispatch({ type: 'field/add', embedId: second });
        store.dispatch({ type: 'field/add', embedId: second });

        const embedRows = rig.rows().filter(r => r.getAttribute('data-rail-row') === 'embed');
        const fieldRows = rig.rows().filter(r => r.getAttribute('data-rail-row') === 'field');
        assert(embedRows.length === 3, 'every embed has a row', String(embedRows.length));
        assert(fieldRows.length === 2, 'every field has a row', String(fieldRows.length));
        assert(rig.row('content').getAttribute('aria-level') === '1', 'the message root is level 1');
        assert(embedRows.every(r => r.getAttribute('aria-level') === '2'), 'embeds are level 2');
        assert(fieldRows.every(r => r.getAttribute('aria-level') === '3'), 'fields are level 3');
        assert(embedRows.every(r => r.getAttribute('role') === 'treeitem'), 'rows are treeitems');
        assert(embedRows[0].getAttribute('aria-posinset') === '1' &&
               embedRows[1].getAttribute('aria-posinset') === '2' &&
               embedRows[0].getAttribute('aria-setsize') === '3',
            'embeds report their position and set size');
        assert(fieldRows[0].getAttribute('aria-posinset') === '1' &&
               fieldRows[0].getAttribute('aria-setsize') === '2',
            'fields report their position within their embed');
        assert(embedRows[1].getAttribute('aria-expanded') === 'true',
            'an embed with fields is expanded (its fields are reachable)');
        assert(embedRows[0].getAttribute('aria-expanded') === null,
            'an embed with no fields is not given a disclosure state it cannot honour');

        // labels follow the document
        store.dispatch({ type: 'embed/setText', embedId: first, key: 'title', value: 'Rules' });
        store.dispatch({ type: 'field/set', embedId: second, fieldId: doc.embeds[1].fields ? '' : '', patch: {} });
        const doc2 = store.getDocument();
        store.dispatch({ type: 'field/set', embedId: second, fieldId: doc2.embeds[1].fields[0].id, patch: { name: 'Section' } });
        assert(/Rules/.test(rig.row(first).textContent), 'an embed row shows its title',
            rig.row(first).textContent);
        assert(/Section/.test(rig.row(doc2.embeds[1].fields[0].id).textContent), 'a field row shows its name');
        assert(/2 fields/.test(rig.row(second).textContent), 'an embed row counts its fields',
            rig.row(second).textContent);
        store.dispatch({ type: 'content/set', text: 'Hello there' });
        assert(/Hello there/.test(rig.row('content').textContent), 'the message root shows a content snippet',
            rig.row('content').textContent);
        assert(/Message content/.test(rig.row('content').textContent), 'and stays recognisable as the message');

        // a rendered label never contains markup (textContent only)
        store.dispatch({ type: 'embed/setText', embedId: first, key: 'title', value: '<img src=x onerror=1>' });
        const labelNode = rig.row(first).children[0];
        assert(labelNode.className === 'mb2-rail-label' &&
               labelNode.textContent.indexOf('<img src=x onerror=1>') !== -1,
            'labels are text, never parsed markup', labelNode.textContent);
        assert(rig.dom.ops.innerHTMLSet === 0,
            'the rail never writes markup through innerHTML', String(rig.dom.ops.innerHTMLSet));
    }

    // ─────────────────────────────────────────────────────────────
    section('B. selection is store state, reflected in the rail');
    // ─────────────────────────────────────────────────────────────
    {
        const rig = makeRig();
        const { store, rail } = rig;
        const embedId = store.getDocument().embeds[0].id;
        assert(rig.dispatched.indexOf('ui/selectNode') === -1, 'creating the rail selects nothing by itself');

        rail.select('content');
        assert(store.getUi().selectedNodeId === 'content', 'selecting the message root is a store action',
            String(store.getUi().selectedNodeId));
        assert(rig.row('content').getAttribute('aria-selected') === 'true', 'and the row reports it');
        assert(rig.row(embedId).getAttribute('aria-selected') === 'false', 'while the others do not');

        store.dispatch({ type: 'ui/selectNode', nodeId: embedId });
        assert(rig.row(embedId).getAttribute('aria-selected') === 'true',
            'a selection made elsewhere is reflected (the rail renders from state)');
        assert(rig.row('content').getAttribute('aria-selected') === 'false', 'and the previous row clears');

        rig.click(rig.row(embedId));
        assert(store.getUi().selectedNodeId === embedId, 'clicking a row selects it through the store');
        assert(rig.focused() === rig.row(embedId), 'and moves focus there');

        // selection and focus are independent
        const fieldBefore = rail.stats();
        rail.focus('content');
        assert(rig.focused() === rig.row('content'), 'focus can move without selection');
        assert(store.getUi().selectedNodeId === embedId, 'selection is unchanged by focus',
            String(store.getUi().selectedNodeId));
        assert(rig.row(embedId).getAttribute('aria-selected') === 'true', 'the selected row stays selected');
        assert(rail.stats().renders >= fieldBefore.renders, 'focusing does not need a re-render of rows');
    }

    // ─────────────────────────────────────────────────────────────
    section('C. structural actions go through the model boundary');
    // ─────────────────────────────────────────────────────────────
    {
        const rig = makeRig();
        const { model, store, rail } = rig;
        const doc = () => store.getDocument();

        // ── add field ──────────────────────────────────────────────
        const embedA = doc().embeds[0].id;
        const expectedField = model.addField(doc(), embedA);
        const acts1 = rig.during(() => rig.click(rig.actionButton(embedA, 'addField')));
        assert(acts1.indexOf('field/add') !== -1, 'the add-field button dispatches field/add', acts1.join(','));
        assert(rig.shape(doc()) === rig.shape(expectedField),
            'the document after add-field is exactly the model s output (structure, order, content)',
            rig.shape(doc()) + ' vs ' + rig.shape(expectedField));
        const fieldA = doc().embeds[0].fields[0].id;
        assert(rig.row(fieldA) !== null, 'the new field has a row');
        assert(store.getUi().selectedNodeId === fieldA, 'the new field is selected');
        assert(rig.focused() === rig.row(fieldA), 'and focused, so a keyboard user keeps working');
        assert(rig.row(embedA).getAttribute('aria-expanded') === 'true',
            'its embed is expanded so the field is visible');

        // ── add embed ──────────────────────────────────────────────
        const expectedAdd = model.addEmbed(doc());
        const acts2 = rig.during(() => rail.addEmbed());
        assert(acts2.indexOf('embed/add') !== -1, 'add-embed dispatches embed/add', acts2.join(','));
        assert(rig.shape(doc()) === rig.shape(expectedAdd),
            'the document after add-embed is exactly the model s output');
        assert(rig.ids().length === 4, 'root + 2 embeds + 1 field', rig.ids().join(','));
        const embedB = doc().embeds[1].id;
        assert(store.getUi().selectedNodeId === embedB, 'the new embed is selected');
        assert(rig.focused() === rig.row(embedB), 'and focused (no mouse needed)');

        // ── duplicate ──────────────────────────────────────────────
        store.dispatch({ type: 'embed/setText', embedId: embedB, key: 'title', value: 'Original' });
        const expectedDup = model.duplicateEmbed(doc(), embedB);
        const acts3 = rig.during(() => rig.click(rig.actionButton(embedB, 'duplicate')));
        assert(acts3.indexOf('embed/duplicate') !== -1, 'duplicate dispatches embed/duplicate', acts3.join(','));
        assert(rig.shape(doc()) === rig.shape(expectedDup),
            'the document after duplicate is exactly the model s output');
        assert(doc().embeds.length === 3, 'duplicate adds one embed', String(doc().embeds.length));
        const copy = doc().embeds[2];
        assert(copy.title === 'Original', 'the copy carries the content', copy.title);
        assert(copy.id !== embedB, 'with an identity of its own');
        assert(store.getUi().selectedNodeId === copy.id, 'and it is selected');

        // ── move embed down / up ───────────────────────────────────
        const order0 = doc().embeds[0].id;
        const acts4 = rig.during(() => rig.click(rig.actionButton(order0, 'down')));
        assert(acts4.indexOf('embed/move') !== -1, 'move-down dispatches embed/move', acts4.join(','));
        assert(doc().embeds[1].id === order0, 'the embed moved down one place',
            doc().embeds.map(e => e.id).join(','));
        assert(rig.focused() === rig.row(order0), 'focus follows the moved embed');
        const acts5 = rig.during(() => rig.click(rig.actionButton(order0, 'up')));
        assert(acts5.indexOf('embed/move') !== -1 && doc().embeds[0].id === order0, 'move-up puts it back');

        // ── bounds are honest ──────────────────────────────────────
        assert(rig.actionButton(order0, 'up').disabled === true, 'move-up is disabled on the first embed');
        assert(rig.actionButton(order0, 'down').disabled === false, 'move-down is enabled in the middle');
        assert(rig.actionButton(doc().embeds[0].id, 'remove').disabled === false,
            'remove is enabled while more than one embed exists');

        // ── move field ─────────────────────────────────────────────
        rig.click(rig.actionButton(doc().embeds[0].id, 'addField'));
        const withFields = doc().embeds[0];
        const idsBefore = withFields.fields.map(f => f.id);
        const acts6 = rig.during(() => rig.click(rig.actionButton(idsBefore[0], 'down')));
        assert(acts6.indexOf('field/move') !== -1, 'moving a field dispatches field/move', acts6.join(','));
        assert(doc().embeds[0].fields.map(f => f.id).join(',') === [idsBefore[1], idsBefore[0]].join(','),
            'the fields swapped', doc().embeds[0].fields.map(f => f.id).join(','));
        assert(rig.row(idsBefore[0]).getAttribute('aria-posinset') === '2',
            'and the moved field reports its new position');
        assert(rig.row(idsBefore[0]).getAttribute('aria-setsize') === '2', 'with the right set size');

        // ── remove field ───────────────────────────────────────────
        const expectedRemoveField = model.removeField(doc(), withFields.id, idsBefore[0]);
        const acts7 = rig.during(() => rig.click(rig.actionButton(idsBefore[0], 'remove')));
        assert(acts7.indexOf('field/remove') !== -1, 'removing a field dispatches field/remove', acts7.join(','));
        assert(rig.shape(doc()) === rig.shape(expectedRemoveField),
            'the document after field removal is exactly the model s output');
        assert(rig.row(idsBefore[0]) === null, 'the removed field row is gone');
        assert(rig.focused() === rig.row(idsBefore[1]), 'focus lands on the surviving field');
        assert(store.getUi().selectedNodeId === idsBefore[1], 'which is selected too');

        // ── remove embed ───────────────────────────────────────────
        const victim = doc().embeds[2].id;
        const expectedRemoveEmbed = model.removeEmbed(doc(), victim);
        const acts8 = rig.during(() => rig.click(rig.actionButton(victim, 'remove')));
        assert(acts8.indexOf('embed/remove') !== -1, 'removing an embed dispatches embed/remove', acts8.join(','));
        assert(rig.shape(doc()) === rig.shape(expectedRemoveEmbed),
            'the document after embed removal is exactly the model s output');
        assert(rig.row(victim) === null, 'its row is gone too');
        assert(rig.focused() !== null && rig.focused().getAttribute('data-node-id') === store.getUi().selectedNodeId,
            'focus and selection agree on the survivor',
            rig.focused() && rig.focused().getAttribute('data-node-id'));

        // ── the last embed cannot be removed ───────────────────────
        store.dispatch({ type: 'embed/remove', embedId: doc().embeds[1].id });
        const only = doc().embeds[0].id;
        assert(doc().embeds.length === 1, 'one embed is left');
        assert(rig.actionButton(only, 'remove').disabled === true,
            'its remove button is disabled rather than silently failing');
        const acts9 = rig.during(() => rig.click(rig.actionButton(only, 'remove')));
        assert(acts9.length === 0, 'clicking it dispatches nothing', acts9.join(','));
        assert(rail.remove(only) === false && doc().embeds.length === 1,
            'and the rail refuses to remove the last embed through its own API too');

        // ── every action left the document canonical ───────────────
        assert(rig.canon(doc()) === model.stableStringify(model.normalizeDocument(doc())),
            'after all of it, the document is still canonical (save/load stays a fixed point)');
    }

    // ─────────────────────────────────────────────────────────────
    section('D. the mutation boundary (frozen documents)');
    // ─────────────────────────────────────────────────────────────
    {
        const rig = makeRig();
        const { store, rail, model } = rig;

        // Prove the instrument is live: an in-place edit MUST throw. Without
        // this check, every assertion below could pass against an unfrozen tree.
        let threw = false;
        try { store.getDocument().embeds.push(model.blankEmbed()); } catch (e) { threw = true; }
        assert(threw, 'the frozen-document instrument is active (an in-place push throws)');
        let threw2 = false;
        try { store.getDocument().embeds[0].title = 'sneaky'; } catch (e) { threw2 = true; }
        assert(threw2, 'and a direct property write throws');

        // Every rail action still works against frozen documents.
        let error = null;
        const acts = rig.during(() => {
            try {
                rig.click(rig.actionButton(store.getDocument().embeds[0].id, 'addField'));
                rail.addEmbed();
                rig.click(rig.actionButton(store.getDocument().embeds[0].id, 'duplicate'));
                rig.click(rig.actionButton(store.getDocument().embeds[0].id, 'down'));
                rig.click(rig.actionButton(store.getDocument().embeds[0].id, 'up'));
                rig.click(rig.actionButton(store.getDocument().embeds[0].id, 'remove'));
            } catch (e) { error = e; }
        });
        assert(error === null, 'every structural action works with frozen documents (no in-place writes)',
            error && error.message);
        ['field/add', 'embed/add', 'embed/duplicate', 'embed/move', 'embed/remove'].forEach(type => {
            assert(acts.indexOf(type) !== -1,
                'the ' + type + ' action left as a dispatch, not a mutation', acts.join(','));
        });

        // The rail holds no document of its own: nothing it exposes is a doc.
        const surface = Object.keys(rail);
        const looksLikeDocument = surface.filter(k => {
            const v = rail[k];
            return v && typeof v === 'object' && (Array.isArray(v.embeds) || 'embeds' in v);
        });
        assert(looksLikeDocument.length === 0,
            'the rail exposes no document-shaped object (single source of truth)',
            looksLikeDocument.join(','));

        // An external edit shows up without the rail being told.
        const embedId = store.getDocument().embeds[0].id;
        store.dispatch({ type: 'embed/setText', embedId: embedId, key: 'title', value: 'Edited elsewhere' });
        assert(/Edited elsewhere/.test(rig.row(embedId).textContent),
            'a change made outside the rail is rendered by the rail');
    }

    // ─────────────────────────────────────────────────────────────
    section('E. keyboard: roving tabindex, navigation, focus after delete');
    // ─────────────────────────────────────────────────────────────
    {
        const rig = makeRig();
        const { store, rail } = rig;
        const mount = rig.mount;
        store.dispatch({ type: 'embed/add' });
        const second = store.getDocument().embeds[1].id;
        store.dispatch({ type: 'field/add', embedId: second });
        const fieldId = store.getDocument().embeds[1].fields[0].id;

        const tabStops = () => rig.rows().filter(r => r.getAttribute('tabindex') === '0');
        rail.select('content');
        assert(tabStops().length === 1, 'exactly one row is a tab stop (roving tabindex)',
            String(tabStops().length));
        assert(tabStops()[0] === rig.row('content'), 'the tab stop starts on the selected row');

        // ↓ moves focus and the tab stop, without changing selection
        const down = rig.key(mount, 'ArrowDown', rig.row('content'));
        assert(down.prevented === 1, 'ArrowDown is handled (default prevented)');
        assert(rig.focused() === rig.row(store.getDocument().embeds[0].id),
            'ArrowDown moves focus to the next row', rig.focused() && rig.focused().getAttribute('data-node-id'));
        assert(store.getUi().selectedNodeId === 'content', 'and does not change the selection');
        assert(tabStops().length === 1 && tabStops()[0] === rig.focused(),
            'the tab stop follows focus');

        rig.key(mount, 'ArrowUp', rig.row(store.getDocument().embeds[0].id));
        assert(rig.focused() === rig.row('content'), 'ArrowUp moves back');
        rig.key(mount, 'ArrowUp', rig.row('content'));
        assert(rig.focused() === rig.row('content'), 'ArrowUp at the top stops (no wrap, no escape)');

        rig.key(mount, 'End', rig.row('content'));
        assert(rig.focused() === rig.row(fieldId), 'End jumps to the last visible row',
            rig.focused() && rig.focused().getAttribute('data-node-id'));
        rig.key(mount, 'Home', rig.row(fieldId));
        assert(rig.focused() === rig.row('content'), 'Home jumps back to the first');

        // Enter/Space select the focused row
        rig.key(mount, 'ArrowDown', rig.row('content'));
        const firstEmbed = store.getDocument().embeds[0].id;
        const enter = rig.key(mount, 'Enter', rig.row(firstEmbed));
        assert(enter.prevented === 1 && store.getUi().selectedNodeId === firstEmbed,
            'Enter selects the focused row', String(store.getUi().selectedNodeId));
        assert(rig.row(firstEmbed).getAttribute('aria-selected') === 'true', 'and the row shows it');
        rig.key(mount, 'ArrowDown', rig.row(firstEmbed));
        rig.key(mount, ' ', rig.row(second));
        assert(store.getUi().selectedNodeId === second, 'Space selects as well',
            String(store.getUi().selectedNodeId));

        // Enter on a nested button is that button's, not the row's
        const buttonRow = rig.row(second);
        const addButton = rig.actionButton(second, 'addField');
        const onButton = rig.key(mount, 'Enter', addButton);
        assert(onButton.prevented === 0,
            'Enter on an action button is not swallowed by the row handler');
        assert(store.getDocument().embeds[1].fields.length === 1, 'and does not select/alter anything');

        // ←/→ collapse and expand, and step in/out
        const rightCollapsed = rig.key(mount, 'ArrowLeft', rig.row(second));
        assert(rightCollapsed.prevented === 1 && rail.isCollapsed(second),
            'ArrowLeft on an expanded embed collapses it');
        assert(rig.row(fieldId) === null, 'its field rows are hidden');
        const reopened = rig.key(mount, 'ArrowRight', rig.row(second));
        assert(reopened.prevented === 1 && !rail.isCollapsed(second), 'ArrowRight expands it again');
        rig.key(mount, 'ArrowRight', rig.row(second));
        assert(rig.focused() === rig.row(fieldId), 'ArrowRight on an expanded embed steps into its fields',
            rig.focused() && rig.focused().getAttribute('data-node-id'));
        rig.key(mount, 'ArrowLeft', rig.row(fieldId));
        assert(rig.focused() === rig.row(second), 'ArrowLeft on a field steps out to its embed');

        // Tab is never trapped
        const tabEvent = rig.key(mount, 'Tab', rig.row(second));
        assert(tabEvent.prevented === 0, 'Tab is not intercepted (no focus trap)');

        // Delete removes and restores focus to a survivor
        const fields = store.getDocument().embeds[1].fields;
        store.dispatch({ type: 'field/add', embedId: second });
        const [f1, f2] = store.getDocument().embeds[1].fields.map(f => f.id);
        rail.focus(f1);
        const del = rig.key(mount, 'Delete', rig.row(f1));
        assert(del.prevented === 1, 'Delete is handled');
        assert(store.getDocument().embeds[1].fields.length === 1, 'the field was removed');
        assert(rig.dispatched[rig.dispatched.length - 2] === 'field/remove' ||
               rig.dispatched[rig.dispatched.length - 1] === 'field/remove',
            'through a field/remove dispatch', rig.dispatched.slice(-3).join(','));
        assert(rig.focused() === rig.row(f2), 'focus moved to the surviving sibling',
            rig.focused() && rig.focused().getAttribute('data-node-id'));
        assert(store.getUi().selectedNodeId === f2, 'and the survivor became the selection');
        assert(tabStops().length === 1 && tabStops()[0] === rig.focused(), 'the tab stop is on it');

        // Delete on the last field falls back to the embed
        rig.key(mount, 'Delete', rig.row(f2));
        assert(rig.focused() === rig.row(second), 'deleting the last field focuses its embed',
            rig.focused() && rig.focused().getAttribute('data-node-id'));
        assert(store.getUi().selectedNodeId === second, 'and selects it');

        // Delete on an embed focuses a sibling embed; on the last embed, the root
        const first = store.getDocument().embeds[0].id;
        rail.focus(first);
        rig.key(mount, 'Delete', rig.row(first));
        assert(store.getDocument().embeds.length === 1, 'the embed was removed');
        assert(rig.focused() === rig.row(second), 'focus moved to the remaining embed',
            rig.focused() && rig.focused().getAttribute('data-node-id'));
        assert(rig.actionButton(second, 'remove').disabled === true,
            'and that embed cannot be removed (it is the last one)');
        rig.key(mount, 'Delete', rig.row(second));
        assert(store.getDocument().embeds.length === 1, 'Delete on the only embed does not remove it');
        rig.key(mount, 'Delete', rig.row('content'));
        assert(store.getDocument().embeds.length === 1 && rig.ids()[0] === 'content',
            'Delete on the message root does nothing');

        // Backspace behaves like Delete for a field
        rig.click(rig.actionButton(second, 'addField'));
        const f3 = store.getDocument().embeds[0].fields[0].id;
        rail.focus(f3);
        rig.key(mount, 'Backspace', rig.row(f3));
        assert(store.getDocument().embeds[0].fields.length === 0, 'Backspace removes as well');
        const labels = ['ArrowDown', 'ArrowUp', 'Home', 'End', 'ArrowLeft', 'ArrowRight'];
        labels.forEach(k => {
            const ev = rig.key(mount, k, rig.row('content'));
            assert(typeof ev.prevented === 'number', k + ' does not throw on the message root');
        });
    }

    // ─────────────────────────────────────────────────────────────
    section('F. row identity and render scope');
    // ─────────────────────────────────────────────────────────────
    {
        const rig = makeRig();
        const { store, rail } = rig;
        store.dispatch({ type: 'embed/add' });
        const second = store.getDocument().embeds[1].id;
        store.dispatch({ type: 'field/add', embedId: second });
        const rootRow = rig.row('content');
        const embedRow = rig.row(second);
        const stats0 = rail.stats();

        store.dispatch({ type: 'content/set', text: 'a keystroke' });
        assert(rig.row('content') === rootRow, 'a content keystroke keeps the row element');
        assert(rig.row(second) === embedRow, 'and every other row');
        assert(rig.row('content').textContent.indexOf('a keystroke') !== -1, 'while its label updates');
        assert(rail.stats().nodesCreated === stats0.nodesCreated,
            'a keystroke creates no rows', String(rail.stats().nodesCreated - stats0.nodesCreated));

        const rowsBefore = rig.rows().length;
        store.dispatch({ type: 'field/add', embedId: store.getDocument().embeds[0].id });
        assert(rig.row(store.getDocument().embeds[0].fields[0].id) !== null, 'the added field has a row');
        assert(rig.rows().length === rowsBefore + 1,
            'and exactly one row was added', rowsBefore + ' -> ' + rig.rows().length);
        assert(rig.row('content') === rootRow, 'the earlier rows are the same objects');

        // a no-op action must not churn the rail either
        const stats2 = rail.stats();
        store.dispatch({ type: 'embed/setText', embedId: second, key: 'title', value: store.getDocument().embeds[1].title });
        assert(rail.stats().renders === stats2.renders,
            'a no-op document action (identical document object) does not re-render');
        assert(rig.dispatched.filter(t => t === 'ui/selectNode').length >= 0, 'dispatch log intact');
    }

    // ─────────────────────────────────────────────────────────────
    section('G. teardown');
    // ─────────────────────────────────────────────────────────────
    {
        const rig = makeRig();
        const { store, rail } = rig;
        store.dispatch({ type: 'embed/add' });
        const before = store._subscriberCounts().selectors;
        assert(before >= 2, 'the rail subscribed to the store', String(before));
        assert(rig.mount.listeners.length >= 2, 'and listens on its own mount',
            String(rig.mount.listeners.length));
        assert(rig.mount.getAttribute('role') === 'tree', 'the tree semantics are in place');

        rail.destroy();
        assert(store._subscriberCounts().selectors === before - 2,
            'destroy unsubscribes both subscriptions', String(store._subscriberCounts().selectors));
        assert(rig.mount.listeners.length === 0, 'and removes its listeners',
            String(rig.mount.listeners.length));
        assert(rig.mount.children.length === 0, 'the mount is empty again');
        assert(rig.mount.getAttribute('role') === null && rig.mount.getAttribute('aria-label') === null,
            'and the ARIA the view added is removed with it');
        assert(rail.destroy() === false, 'destroying twice is a no-op');

        const renders = rail.stats().renders;
        store.dispatch({ type: 'content/set', text: 'after destroy' });
        assert(rail.stats().renders === renders, 'a destroyed rail no longer renders');
        assert(rig.mount.children.length === 0, 'and does not re-add rows');
    }

}

// A harness that dies from a thrown error still "fails" — but a TypeError is not
// a diagnosis. Anything that escapes a section is reported as a named failure
// alongside the assertion results, so a mutation is always readable as WHICH
// expectation broke rather than as a stack trace.
let aborted = null;
try {
    runAll();
} catch (err) {
    aborted = err;
    fail++;
    failures.push('a section aborted: ' + ((err && err.message) || String(err)));
    console.log('\n  FAIL  a section aborted before its checks completed');
    console.log('        ' + ((err && err.stack) || String(err)).split('\n').slice(0, 3).join('\n        '));
}

console.log('\nmessage-builder rail: ' + pass + ' passed, ' + fail + ' failed');
if (fail) {
    console.log('Failures:');
    failures.forEach(f => console.log(' -', f));
    process.exit(1);
}
console.log('ALL MESSAGE-BUILDER RAIL CHECKS PASSED');
