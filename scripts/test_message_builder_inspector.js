#!/usr/bin/env node
/* ═══════════════════════════════════════════════════════════════
   Message Builder v2 — phase 1, step 5c: the property inspector.

   WHAT THIS HAS TO PROVE

     A. THE ACTION SURFACE — every keystroke leaves as a store action from the
        declared vocabulary, carrying ids (never indexes), with a coalesce key
        so a typing burst stays one undo step. Nothing else is dispatched, and
        `document/load` is never used to smuggle an edit in.
     B. THE MUTATION BOUNDARY — the store's documents are deep-frozen for the
        whole harness and the freeze is proven live before any check relies on
        it, so an in-place write throws instead of quietly working. A second
        store subscription proves the DOM follows the STORE: an edit made
        anywhere else lands here without the inspector being told.
     C. EVERY EDITABLE PROPERTY — content, embed title/description/url/
        timestamp/colour, author name/url/icon, footer text/icon, image,
        thumbnail, and field name/value/inline: each control changes exactly the
        canonical document field it claims to, in the shape the model normalizes
        to, and clearing a value clears it.
     D. SELECTION — the inspector shows what the store says is selected, in both
        directions: a rail selection swaps the panel, and the inspector's own
        navigation (field rows) dispatches ui/selectNode so the rail follows.
     E. EXTERNAL CHANGE — undo, redo and a document replacement are reflected in
        the inputs, and a render with nothing new writes nothing.
     F. BOUNDARIES — no persistence, no renderer calls, no validation: the
        module is loaded WITHOUT preview.js/drafts.js in the sandbox at all, and
        its source (comments stripped) contains no validation vocabulary.
     G. ACCESSIBILITY — every control is labelled by a matching <label for>,
        every button has an accessible name, ids are unique, groups are
        fieldsets.
     H. TEARDOWN — destroy() unsubscribes, removes its listeners, empties the
        mount, and is idempotent.
     I. IDENTITY/PERF — a keystroke creates no nodes and rewrites no values;
        swapping embeds reuses the same inputs; the field list reconciles by id.

   Run:  node scripts/test_message_builder_inspector.js
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
const INSPECTOR_PATH = process.env.NERO_INSPECTOR_SRC || js('embed', 'views', 'inspector.js');
const RAIL_PATH = process.env.NERO_RAIL_SRC || js('embed', 'views', 'rail.js');
const INSPECTOR_SOURCE = fs.readFileSync(INSPECTOR_PATH, 'utf8');

/** Comments describe the boundaries; only CODE can breach them. */
function stripComments(src) {
    return src.replace(/\/\*[\s\S]*?\*\//g, ' ').replace(/(^|[^:])\/\/[^\n]*/g, '$1 ');
}
const CODE = stripComments(INSPECTOR_SOURCE);

// ── sandbox: model + store + rail + inspector, and NOTHING else ──
// preview.js and drafts.js are deliberately absent: if the inspector reached
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
    // validate.js since 6b: the inspector's counters and the field cap are ITS
    // measurement, so the harness must load the real thing (and a mutant that
    // breaks it has to show up here).
    [js('embed', 'model.js'), js('embed', 'assets.js'), js('embed', 'store.js'), js('embed', 'validate.js'),
     RAIL_PATH, INSPECTOR_PATH].forEach(file => {
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
 * The limits table the server renders into the page, mirroring
 * utils/discord_limits.limits_payload() (the page harness carries the same
 * fixture; scripts/test_embed_schema.py pins the KEYS to the real payload).
 */
function servedLimits() {
    return {
        message: { content_max: 2000, embeds_max: 10, embed_total_chars_max: 6000, request_bytes_max: 26214400 },
        attachments: { count_max: 10, total_bytes_max: 26148864, file_bytes_advisory: 20971520, file_advisory_is_hard: false },
        embed: {
            title_max: 256, description_max: 4096, fields_max: 25, field_name_max: 256,
            field_value_max: 1024, footer_text_max: 2048, author_name_max: 256,
        },
        components: {
            rows_max: 5, buttons_per_row_max: 5, button_label_max: 80, button_url_max: 512,
            custom_id_max: 100, select_options_max: 25, select_option_label_max: 100,
            select_option_description_max: 100, select_placeholder_max: 150,
        },
    };
}

const CLOCK = 1790284740000;               // 2026-09-24T12:39:00.000Z
const FIXTURE_TS = '2026-01-02T03:04:05.000Z';

function filledDocument(NERO, text) {
    const model = NERO.embed.model;
    const base = model.blankMessageDocument();
    return model.normalizeDocument(Object.assign({}, base, {
        content: text || 'Hello',
        embeds: [Object.assign({}, base.embeds[0], {
            title: 'Title one',
            description: 'Description one',
            url: 'https://example.com/one',
            timestamp: '2026-01-02T03:04:05.000Z',
            color: 0x5865f2,
            author: { name: 'Author one', url: 'https://example.com/author', icon: null },
            footer: { text: 'Footer one', icon: null },
            fields: [
                { id: 'fld_a', name: 'A', value: '1', inline: false },
                { id: 'fld_b', name: 'B', value: '2', inline: true },
            ],
        })],
    }));
}

/**
 * A store + an inspector, with every published document frozen and every
 * dispatch recorded. `rig.mount` is where events are delivered (the DOM double
 * has no bubbling, so an event goes to the mount carrying the real target).
 */
function makeRig(options) {
    options = options || {};
    const { dom, NERO } = makeSandbox();
    const model = NERO.embed.model;
    const storeMod = NERO.embed.store;
    const document_ = options.document || filledDocument(NERO);

    const store = storeMod.createStore({
        document: document_,
        // The page selects the message root at boot (5b wiring); the rig starts
        // from that same state so the harness tests the real configuration.
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

    const mount = dom.document.createElement('div');
    mount.setAttribute('id', 'mb2-inspector-body');

    // 6b: the page hands the inspector the served limits by reference; a test
    // that wants the fail-closed branch passes `limits: null`.
    const limits = Object.prototype.hasOwnProperty.call(options, 'limits') ? options.limits : servedLimits();
    const inspector = NERO.embed.views.inspector.create({
        document: dom.document,
        model: model,
        store: store,
        mount: mount,
        limits: limits,
        now: () => CLOCK,
    });

    return {
        dom, NERO, model, store, inspector, mount, dispatched, limits,
        doc: () => store.getDocument(),
        // deltas: never assert absolutes that drift
        created: () => inspector.stats().nodesCreated,
        writes: () => inspector.stats().valueWrites,
        renders: () => inspector.stats().renders,
        textWrites: () => inspector.stats().textWrites,
        disabledWrites: () => inspector.stats().disabledWrites,
        keys: () => dispatched.map(a => a.type),
        last: () => dispatched[dispatched.length - 1],
        input: (key) => inspector.control(key),
        type: (keyOrNode, value) => {
            const node = typeof keyOrNode === 'string' ? inspector.control(keyOrNode) : keyOrNode;
            node.value = value;
            mount.dispatch('input', { type: 'input', target: node });
            return node;
        },
        toggle: (keyOrNode, on) => {
            const node = typeof keyOrNode === 'string' ? inspector.control(keyOrNode) : keyOrNode;
            node.checked = !!on;
            mount.dispatch('change', { type: 'change', target: node });
            return node;
        },
        panel: () => inspector.panel(),
        click: (action) => {
            const node = findAction(mount, action);
            assert(!!node, 'rig: found the ' + action + ' button');
            if (node) mount.dispatch('click', { type: 'click', target: node });
            return node;
        },
        select: (nodeId) => store.dispatch({ type: 'ui/selectNode', nodeId: nodeId }),
    };
}

function walk(node, out) {
    out = out || [];
    (node.children || []).forEach(child => { out.push(child); walk(child, out); });
    return out;
}

function findAction(node, action) {
    const all = walk(node);
    for (let i = 0; i < all.length; i++) {
        if (all[i].getAttribute && all[i].getAttribute('data-insp-action') === action) return all[i];
    }
    return null;
}

function runAll() {
    // ═══════════════════════════════════════════════════════════════
    section('A. the action surface: declared actions, ids, coalescing');
    // ─────────────────────────────────────────────────────────────
    {
        const rig = makeRig();
        const storeActions = Object.keys(rig.NERO.embed.store.createReducers(rig.NERO.embed.model));
        const embedId = rig.doc().embeds[0].id;
        const fieldId = rig.doc().embeds[0].fields[0].id;

        assert(rig.inspector.view() === 'content', 'the boot selection shows the content panel',
            String(rig.inspector.view()));
        rig.type('content', 'Hello there');
        rig.select(embedId);
        rig.type('title', 'T2');
        rig.type('url', 'https://example.com/two');
        rig.type('author.name', 'A2');
        rig.type('footer.text', 'F2');
        rig.type('media.image', 'https://example.com/i.png');
        rig.select(fieldId);
        rig.type('field.name', 'A renamed');
        rig.toggle('field.inline', true);
        rig.select(embedId);
        rig.click('addField');

        const types = rig.keys();
        assert(types.every(t => storeActions.indexOf(t) !== -1),
            'every dispatched action is declared by the store reducers',
            types.filter(t => storeActions.indexOf(t) === -1).join(','));
        assert(types.indexOf('document/load') === -1,
            'no edit is smuggled in as a whole-document replacement');
        ['content/set', 'embed/set', 'embed/setAuthor', 'embed/setFooter', 'embed/setMedia',
         'field/set', 'field/add', 'ui/selectNode'].forEach(t => {
            assert(types.indexOf(t) !== -1, 'the inspector uses ' + t, types.join(','));
        });

        const valueActions = rig.dispatched.filter(a => a.type !== 'ui/selectNode');
        const scoped = valueActions.filter(a => a.type !== 'content/set');
        assert(scoped.every(a => typeof a.embedId === 'string' && a.embedId.length),
            'every embed/field action carries the embed id (ids, never indexes)',
            scoped.filter(a => typeof a.embedId !== 'string').map(a => a.type).join(','));
        assert(valueActions.every(a => !('index' in a) && !('at' in a) && !('position' in a)),
            'no action refers to a position');
        assert(rig.dispatched.filter(a => a.type === 'field/set').every(a => typeof a.fieldId === 'string'),
            'every field action carries the field id');
        const text = valueActions.filter(a => ['content/set', 'embed/set', 'embed/setAuthor', 'embed/setFooter',
            'embed/setMedia', 'field/set'].indexOf(a.type) !== -1);
        assert(text.every(a => a.meta && typeof a.meta.coalesceKey === 'string' && a.meta.coalesceKey.length),
            'every text edit declares a coalesce key (a burst is one undo step)',
            JSON.stringify(text.map(a => a.meta).slice(0, 3)));
    }

    // ═══════════════════════════════════════════════════════════════
    section('B. the mutation boundary and the single source of truth');
    // ─────────────────────────────────────────────────────────────
    {
        const rig = makeRig();
        let threw = false;
        try { rig.doc().content = 'in place'; } catch (e) { threw = true; }
        assert(threw, 'the frozen rig is live: writing to the store s document throws');

        // An edit made anywhere else must land in the inspector untouched.
        const embedId = rig.doc().embeds[0].id;
        const before = rig.writes();
        rig.store.dispatch({ type: 'embed/set', embedId: embedId, patch: { title: 'From elsewhere' } });
        rig.select(embedId);
        assert(rig.input('title').value === 'From elsewhere',
            'an edit made outside the inspector appears here (no mirror)',
            rig.input('title').value);
        assert(rig.writes() > before, 'and it arrived by rendering from the store');

        // The inspector's own edit must reach the document through the store.
        rig.type('title', 'From the inspector');
        assert(rig.doc().embeds[0].title === 'From the inspector',
            'typing reaches the canonical document');
        assert(rig.input('title').value === rig.doc().embeds[0].title,
            'and the input shows the store s value');

        // A rejected (no-op) edit changes nothing at all.
        const docBefore = rig.doc();
        const writesBefore = rig.writes();
        const dispatchesBefore = rig.dispatched.length;
        rig.type('title', 'From the inspector');
        assert(rig.doc() === docBefore, 're-typing the same value is a no-op in the document');
        assert(rig.dispatched.length === dispatchesBefore,
            'and it is not even dispatched (the guard reads, never stores)',
            String(rig.dispatched.length - dispatchesBefore));
        assert(rig.writes() === writesBefore, 'and it costs no DOM writes');
    }

    // ═══════════════════════════════════════════════════════════════
    section('C. every editable property');
    // ─────────────────────────────────────────────────────────────
    {
        const rig = makeRig();
        const model = rig.NERO.embed.model;
        const embedId = rig.doc().embeds[0].id;
        const fieldId = rig.doc().embeds[0].fields[0].id;

        // content
        rig.type('content', 'New content');
        assert(rig.doc().content === 'New content', 'message content is editable');
        assert(rig.input('content').value === 'New content', 'the textarea shows it');
        rig.type('content', '');
        assert(rig.doc().content === '', 'content can be cleared');

        rig.select(embedId);
        assert(rig.inspector.view() === 'embed', 'selecting an embed shows the embed panel');
        assert(rig.mount.children.length === 1 && rig.mount.children[0] === rig.panel(),
            'exactly one panel is mounted at a time', String(rig.mount.children.length));
        const embedPanel = rig.panel();
        const titleNode = rig.inspector.control('title');

        // scalars
        rig.type('title', 'T2');
        assert(rig.doc().embeds[0].title === 'T2', 'title');
        rig.type('description', 'D2');
        assert(rig.doc().embeds[0].description === 'D2', 'description');
        rig.type('url', 'https://example.com/two');
        assert(rig.doc().embeds[0].url === 'https://example.com/two', 'title link');
        rig.type('timestamp', '2026-12-31T23:59:00.000Z');
        assert(rig.doc().embeds[0].timestamp === '2026-12-31T23:59:00.000Z', 'timestamp');
        rig.type('timestamp', '');
        assert(rig.doc().embeds[0].timestamp === '', 'timestamp can be cleared');

        // colour
        assert(rig.input('color').value === model.colorToHex(rig.doc().embeds[0].color),
            'the colour input shows the document colour', rig.input('color').value);
        rig.type('color', '#00ff88');
        assert(rig.doc().embeds[0].color === 0x00ff88, 'colour is stored as an int',
            String(rig.doc().embeds[0].color));
        assert(rig.last().type === 'embed/setColor', 'through embed/setColor', rig.last().type);

        // author
        rig.type('author.name', 'Ada');
        assert(rig.doc().embeds[0].author.name === 'Ada', 'author name');
        rig.type('author.url', 'https://example.com/ada');
        assert(rig.doc().embeds[0].author.url === 'https://example.com/ada', 'author link');
        rig.type('author.icon', 'https://example.com/ada.png');
        assert(JSON.stringify(rig.doc().embeds[0].author.icon) ===
            JSON.stringify({ kind: 'url', url: 'https://example.com/ada.png' }),
            'author icon is normalized to a media asset',
            JSON.stringify(rig.doc().embeds[0].author.icon));
        assert(rig.last().type === 'embed/setAuthor', 'through embed/setAuthor', rig.last().type);
        rig.type('author.icon', '');
        assert(rig.doc().embeds[0].author.icon === null, 'clearing the author icon clears the asset');
        assert(rig.doc().embeds[0].author.name === 'Ada', 'and leaves the rest of the author alone');

        // footer
        rig.type('footer.text', 'beep boop');
        assert(rig.doc().embeds[0].footer.text === 'beep boop', 'footer text');
        rig.type('footer.icon', 'https://example.com/f.png');
        assert(rig.doc().embeds[0].footer.icon.url === 'https://example.com/f.png', 'footer icon');
        assert(rig.last().type === 'embed/setFooter', 'through embed/setFooter', rig.last().type);
        rig.type('footer.icon', '');
        assert(rig.doc().embeds[0].footer.icon === null, 'clearing the footer icon clears the asset');

        // media
        rig.type('media.image', 'https://example.com/big.png');
        assert(rig.doc().embeds[0].image.url === 'https://example.com/big.png', 'large image url');
        assert(rig.last().type === 'embed/setMedia' && rig.last().slot === 'image',
            'through embed/setMedia {slot:image}');
        rig.type('media.thumbnail', 'https://example.com/thumb.png');
        assert(rig.doc().embeds[0].thumbnail.url === 'https://example.com/thumb.png', 'thumbnail url');
        assert(rig.last().slot === 'thumbnail', 'through embed/setMedia {slot:thumbnail}');
        rig.type('media.image', '');
        assert(rig.doc().embeds[0].image === null, 'clearing the image clears the slot');
        assert(rig.doc().embeds[0].thumbnail !== null, 'and leaves the thumbnail alone');

        // fields
        rig.select(fieldId);
        assert(rig.inspector.view() === 'field', 'selecting a field shows the field panel');
        assert(rig.mount.children.length === 1 && rig.panel() !== embedPanel,
            'and the embed panel came off the page', String(rig.mount.children.length));
        rig.select(embedId);
        assert(rig.panel() === embedPanel,
            'going back reuses the same panel and the same inputs (nothing rebuilt)');
        assert(rig.inspector.control('title') === titleNode,
            'the title input survived the round trip');
        rig.select(fieldId);
        const context = rig.inspector.control('context');
        assert(/^Field 1 in /.test(context.textContent), 'the panel says which field it is',
            context.textContent);
        assert(context.textContent.indexOf(rig.doc().embeds[0].title) !== -1,
            'and which embed it belongs to');
        rig.type('field.name', 'Renamed');
        assert(rig.doc().embeds[0].fields[0].name === 'Renamed', 'field name');
        rig.type('field.value', 'value two');
        assert(rig.doc().embeds[0].fields[0].value === 'value two', 'field value');
        assert(rig.last().type === 'field/set' && rig.last().fieldId === fieldId,
            'through field/set with the field id');
        rig.toggle('field.inline', true);
        assert(rig.doc().embeds[0].fields[0].inline === true, 'inline is editable');
        assert(rig.last().patch.inline === true, 'and normalizes to a boolean');
        rig.toggle('field.inline', false);
        assert(rig.doc().embeds[0].fields[0].inline === false, 'and can be turned off');
        rig.type('field.value', '');
        assert(rig.doc().embeds[0].fields[0].value === '', 'field value can be cleared');
        assert(rig.doc().embeds[0].fields[1].value === '2', 'the sibling field is untouched');
    }

    // ═══════════════════════════════════════════════════════════════
    section('D. selection: the store owns it, both directions');
    // ─────────────────────────────────────────────────────────────
    {
        const rig = makeRig();
        const embedId = rig.doc().embeds[0].id;
        const fieldId = rig.doc().embeds[0].fields[0].id;

        // rail → inspector
        const railMount = rig.dom.document.createElement('div');
        const railView = rig.NERO.embed.views.rail.create({
            document: rig.dom.document, store: rig.store, mount: railMount,
        });
        rig.store.dispatch({ type: 'ui/selectNode', nodeId: embedId });
        assert(rig.inspector.view() === 'embed', 'a store selection swaps the inspector to the embed');
        assert(railView.isSelected(embedId), 'and the rail agrees');

        // inspector → store → rail
        const fieldRowButton = findAction(rig.mount, 'selectField');
        assert(!!fieldRowButton, 'the embed panel lists its fields');
        rig.mount.dispatch('click', { type: 'click', target: fieldRowButton });
        assert(rig.store.getUi().selectedNodeId === fieldId,
            'clicking a field row dispatches ui/selectNode',
            String(rig.store.getUi().selectedNodeId));
        assert(rig.inspector.view() === 'field', 'the inspector follows its own navigation');
        assert(railView.isSelected(fieldId), 'and the rail follows it too');

        // back to the embed from the field panel
        const embedButton = findAction(rig.mount, 'selectEmbed');
        rig.mount.dispatch('click', { type: 'click', target: embedButton });
        assert(rig.store.getUi().selectedNodeId === embedId, 'the embed shortcut selects the embed');
        assert(rig.inspector.view() === 'embed', 'and the panel follows');

        // no selection
        rig.store.dispatch({ type: 'ui/selectNode', nodeId: null });
        assert(rig.inspector.view() === 'none', 'no selection shows the empty panel');
        assert(/Select the message content/.test(rig.inspector.control('message').textContent),
            'with an instruction', rig.inspector.control('message').textContent);
        rig.mount.dispatch('click', { type: 'click', target: findAction(rig.mount, 'selectContent') });
        assert(rig.store.getUi().selectedNodeId === 'content', 'its button selects the content root');
        assert(rig.inspector.view() === 'content', 'and the content panel appears');

        // a stale selection (the node was removed elsewhere)
        rig.store.dispatch({ type: 'ui/selectNode', nodeId: 'fld_nope' });
        assert(rig.inspector.view() === 'none', 'a selection that no longer exists shows the empty panel');
        assert(/no longer there/.test(rig.inspector.control('message').textContent),
            'and says so', rig.inspector.control('message').textContent);
        assert(rig.inspector.selection().kind === 'unknown', 'the selection is reported as unknown');

        railView.destroy();
    }

    // ═══════════════════════════════════════════════════════════════
    section('E. structural content actions, and external change');
    // ─────────────────────────────────────────────────────────────
    {
        const rig = makeRig();
        const embedId = rig.doc().embeds[0].id;
        rig.store.dispatch({ type: 'ui/selectNode', nodeId: embedId });

        // add a field (through the store, from the inspector's own button)
        const before = rig.doc().embeds[0].fields.length;
        rig.click('addField');
        assert(rig.doc().embeds[0].fields.length === before + 1,
            'the add button adds a field to the canonical document',
            String(rig.doc().embeds[0].fields.length));
        assert(rig.last().type === 'field/add' && rig.last().embedId === embedId,
            'through field/add');
        const list = walk(rig.mount).filter(n => n.getAttribute('data-insp-action') === 'selectField');
        assert(list.length === before + 1, 'and the list shows a row for it', String(list.length));

        // remove one (the first)
        const firstRemove = walk(rig.mount).filter(n => n.getAttribute('data-insp-action') === 'removeField')[0];
        const removeId = firstRemove.getAttribute('data-field-id');
        rig.mount.dispatch('click', { type: 'click', target: firstRemove });
        assert(rig.doc().embeds[0].fields.length === before, 'the remove button removes that field');
        assert(rig.last().type === 'field/remove' && rig.last().fieldId === removeId,
            'through field/remove with the row s field id');
        assert(rig.doc().embeds[0].fields.every(f => f.id !== removeId), 'and it is the one that went');

        // the "Now" button uses the injected clock (never a second clock)
        rig.click('now');
        assert(rig.doc().embeds[0].timestamp === new Date(CLOCK).toISOString(),
            'the Now button uses the injected clock',
            rig.doc().embeds[0].timestamp);
        assert(rig.input('timestamp').value === new Date(CLOCK).toISOString(),
            'and the input shows it');

        // undo / redo are reflected in the inputs
        rig.store.undo();
        assert(rig.input('timestamp').value === FIXTURE_TS,
            'undo puts the previous timestamp back in the input',
            rig.input('timestamp').value);
        rig.store.redo();
        assert(rig.input('timestamp').value === new Date(CLOCK).toISOString(),
            'redo puts it back');

        // a whole-document replacement (a different draft being adopted)
        const other = filledDocument(rig.NERO, 'Replaced');
        other.embeds[0].id = embedId;
        rig.store.dispatch({ type: 'document/load', document: other, meta: { history: false } });
        assert(rig.input('title').value === 'Title one',
            'a replaced document repaints the inputs', rig.input('title').value);
        rig.store.dispatch({ type: 'ui/selectNode', nodeId: 'content' });
        assert(rig.input('content').value === 'Replaced',
            'including the content panel', rig.input('content').value);

        // a render with nothing new must write nothing
        const writes = rig.writes();
        const created = rig.created();
        rig.inspector.render();
        assert(rig.writes() === writes, 'a no-op render writes no input values');
        assert(rig.created() === created, 'and creates no nodes');
    }

    // ═══════════════════════════════════════════════════════════════
    section('F. boundaries: persistence, the renderer, validation');
    // ─────────────────────────────────────────────────────────────
    {
        assert(!/preview/.test(CODE), 'the code never mentions the preview',
            (CODE.match(/.{0,30}preview.{0,30}/) || [''])[0]);
        assert(!/updateDocument|\.patch\(|reconcile/.test(CODE),
            'and never calls a renderer');
        assert(!/draft|session|markSaved|savedDocumentHash|pendingSave|idb|IndexedDB/i.test(CODE),
            'and never touches persistence',
            (CODE.match(/.{0,30}(draft|session|markSaved).{0,30}/i) || [''])[0]);
        // 6b: this file now SHOWS numbers, so the blanket bans on
        // 'validate'/'counter' are replaced by the boundary that actually
        // matters — it never decides legality, never owns the issue list, never
        // parses the limits table and never paints a live region.
        ['aria-invalid', 'maxlength', 'minlength', 'pattern=', 'Nerrored'].forEach(word => {
            assert(CODE.indexOf(word) === -1, 'no inline error vocabulary: ' + word);
        });
        // The rules and their wording stay in embed/validate.js: this file must
        // not know an issue code or a sentence, only how to ask for numbers.
        ['too-long', 'too-many', 'url-invalid', 'attachment-missing', 'limits.missing',
         'setIssues', 'ui.issues', "Discord's limit is"].forEach(word => {
            assert(CODE.indexOf(word) === -1, 'no rule or issue vocabulary: ' + word);
        });
        assert(!/validate\.validate\s*\(/.test(CODE),
            'it never RUNS the rules — it only asks for counts and caps');
        assert((CODE.match(/validate\.counts\s*\(/g) || []).length >= 1 &&
               (CODE.match(/validate\.caps\s*\(/g) || []).length >= 1,
            'and it gets its numbers from that one measurement');
        assert(!/JSON\.parse|data-limits/.test(CODE),
            'it never parses the limits table itself (the page is the only parser)');
        assert(!/mb2-strip|aria-live/.test(CODE), 'and never paints a live region of its own');
        assert(!/\bcover\b|256|1024|4096|6000/.test(CODE.replace(/[a-f0-9]{6,}/gi, '')),
            'and no field limits are baked in');
        assert(!/model\.set[A-Z]/.test(CODE),
            'the inspector never calls a model setter itself (the store does)',
            (CODE.match(/model\.set[A-Z]\w*/) || [''])[0]);
        const modelCalls = CODE.match(/model\.[a-zA-Z]+\s*\(/g) || [];
        assert(modelCalls.length > 0 &&
               modelCalls.every(c => c === 'model.colorToHex(' || c === 'model.mediaUrl('),
            'the model is called for display formatting only', modelCalls.join(','));
        assert(!/innerHTML/.test(CODE), 'no innerHTML anywhere');
    }

    // ═══════════════════════════════════════════════════════════════
    section('G. accessibility of the controls');
    // ─────────────────────────────────────────────────────────────
    {
        const rig = makeRig();
        const embedId = rig.doc().embeds[0].id;
        const fieldId = rig.doc().embeds[0].fields[0].id;

        function audit(panelName, expectControls) {
            const all = walk(rig.mount);
            const inputs = all.filter(n => n.tagName === 'INPUT' || n.tagName === 'TEXTAREA');
            if (expectControls !== false) {
                assert(inputs.length > 0, panelName + ': the panel has controls', String(inputs.length));
            }
            assert(inputs.every(input => {
                const id = input.getAttribute('id');
                if (!id) return false;
                return all.some(n => n.tagName === 'LABEL' && n.getAttribute('for') === id);
            }), panelName + ': every control has a <label for>');
            const buttons = all.filter(n => n.tagName === 'BUTTON');
            assert(buttons.every(b => (b.getAttribute('aria-label') || b.textContent || '').trim().length > 0),
                panelName + ': every button has an accessible name');
            const ids = all.map(n => n.getAttribute('id')).filter(Boolean);
            assert(new Set(ids).size === ids.length, panelName + ': ids are unique', ids.join(','));
            assert(all.filter(n => n.getAttribute('data-insp')).every(n => {
                const id = n.getAttribute('id');
                return all.some(m => m.tagName === 'LABEL' && m.getAttribute('for') === id);
            }), panelName + ': every data-insp control is labelled');
        }

        assert(rig.mount.getAttribute('data-insp-view') === 'content', 'the view is announced on the mount');
        audit('content');
        rig.store.dispatch({ type: 'ui/selectNode', nodeId: embedId });
        audit('embed');
        rig.store.dispatch({ type: 'ui/selectNode', nodeId: fieldId });
        audit('field');
        rig.store.dispatch({ type: 'ui/selectNode', nodeId: null });
        audit('empty', false);

        const groups = walk(rig.mount).filter(n => n.getAttribute('data-insp-action') === 'selectContent');
        assert(groups.length === 1, 'the empty panel offers one way back');
        rig.store.dispatch({ type: 'ui/selectNode', nodeId: embedId });
        const fieldsets = walk(rig.mount).filter(n => n.tagName === 'FIELDSET');
        assert(fieldsets.length >= 3, 'the embed panel groups its controls in fieldsets',
            String(fieldsets.length));
        assert(fieldsets.every(f => walk(f).some(n => n.tagName === 'LEGEND')),
            'every fieldset has a legend');
    }


    // ═══════════════════════════════════════════════════════════════
    section('K. the 6b readouts: counters and the field cap');
    // ═══════════════════════════════════════════════════════════════
    {
        const rep = (ch, n) => String(ch).repeat(n);

        // ── the counters exist, and they measure against the SERVED table ──
        const rig = makeRig();
        const embedId = rig.doc().embeds[0].id;
        const fieldId = rig.doc().embeds[0].fields[0].id;
        assert(!!rig.inspector.count('content'), 'the content panel has a counter');
        rig.select(embedId);
        ['title', 'description', 'author.name', 'footer.text', 'total', 'fields'].forEach(key => {
            assert(!!rig.inspector.count(key), 'the embed panel has a ' + key + ' counter');
        });
        assert(rig.inspector.count('url') === null && rig.inspector.count('color') === null,
            'and none for the properties that have no served limit (a counter needs a real max)');
        rig.select(fieldId);
        assert(!!rig.inspector.count('field.name') && !!rig.inspector.count('field.value'),
            'the field panel has its two counters');

        rig.select(embedId);
        const title = rig.input('title');
        assert(rig.inspector.count('title').textContent === '9 / 256',
            'the counter reads used / max against the served title_max (Title one is 9 characters)',
            rig.inspector.count('title').textContent);
        assert(rig.inspector.count('title').getAttribute('data-count') === 'title',
            'and it is keyed by the same name the measurement uses');
        assert(rig.inspector.count('title').getAttribute('aria-hidden') === 'true' &&
               rig.inspector.count('title').getAttribute('data-insp') === null,
            'it is decoration, not a control (aria-hidden, no data-insp)');

        // ── typing keeps it in step, and the over-state is the rule's own ──
        rig.type(title, rep('t', 256));
        assert(rig.inspector.count('title').textContent === '256 / 256',
            'exactly at the limit the counter shows max / max', rig.inspector.count('title').textContent);
        assert(rig.inspector.count('title').className.indexOf('mb2-count-over') === -1,
            'and it is NOT over (the limit is inclusive)', rig.inspector.count('title').className);
        assert(rig.NERO.embed.validate.validate(rig.doc(), rig.limits)
               .filter(i => i.code === 'embed.title.too-long').length === 0,
            'rig: and the rule is silent at exactly the limit');
        rig.type(title, rep('t', 257));
        assert(rig.inspector.count('title').textContent === '257 / 256',
            'one past it the counter says so', rig.inspector.count('title').textContent);
        assert(rig.inspector.count('title').className.indexOf('mb2-count-over') !== -1,
            'and the over-state is applied', rig.inspector.count('title').className);
        assert(rig.NERO.embed.validate.validate(rig.doc(), rig.limits)
               .filter(i => i.code === 'embed.title.too-long').length === 1,
            'while the RULE reports the same boundary (counter and rule cannot disagree)');
        rig.store.undo();
        assert(rig.inspector.count('title').className.indexOf('mb2-count-over') === -1,
            'undo takes the over-state away again', rig.inspector.count('title').className);

        // ── only the counter that moved is written ──
        const writes = rig.textWrites();
        rig.type('description', 'a description');
        // Two counters move, not one, and that is the point: the description's own
        // counter AND the embed's character budget, which contains it. Nothing
        // else in the panel is written.
        assert(rig.textWrites() - writes === 2,
            'typing in one control writes that control s counter and the embed total, nothing else',
            String(rig.textWrites() - writes));
        const writesTitle = rig.inspector.count('title').textContent;
        rig.type('description', 'a description 2');
        assert(rig.inspector.count('title').textContent === writesTitle,
            'and the untouched title counter still reads the same');
        const writes2 = rig.textWrites();
        rig.inspector.render();
        assert(rig.textWrites() === writes2, 'a no-op render writes no counter');

        // ── the embed-wide facts: total characters and the fields cap ──
        assert(/^\d+ \/ 6000$/.test(rig.inspector.count('total').textContent),
            'the embed panel shows the embed character budget',
            rig.inspector.count('total').textContent);
        assert(rig.inspector.count('fields').textContent === '2 / 25',
            'and the fields fact beside the add button',
            rig.inspector.count('fields').textContent);
        const addField = findAction(rig.mount, 'addField');
        assert(!!addField && addField.disabled === false,
            'with 2 of 25 fields the add button is live');

        // ── at the cap: disabled, and the guard refuses a click ──
        const tight = servedLimits();
        tight.embed.fields_max = 2;
        const capped = makeRig({ limits: tight });
        const cappedEmbed = capped.doc().embeds[0].id;
        capped.select(cappedEmbed);
        assert(capped.inspector.count('fields').textContent === '2 / 2',
            'rig: the served cap is 2 and the embed has 2 fields',
            capped.inspector.count('fields').textContent);
        const cappedBtn = findAction(capped.mount, 'addField');
        assert(cappedBtn.disabled === true,
            'at fields_max the add-field control is disabled', String(cappedBtn.disabled));
        const before = capped.dispatched.length;
        capped.mount.dispatch('click', { type: 'click', target: cappedBtn });
        assert(capped.dispatched.length === before &&
               capped.doc().embeds[0].fields.length === 2,
            'and a click on the disabled control does nothing (no dispatch, no field)',
            String(capped.doc().embeds[0].fields.length));

        // one field below the cap it is live again
        const roomy = servedLimits();
        roomy.embed.fields_max = 3;
        const room = makeRig({ limits: roomy });
        room.select(room.doc().embeds[0].id);
        assert(findAction(room.mount, 'addField').disabled === false,
            'one field below the cap the control is live');
        room.click('addField');
        assert(room.doc().embeds[0].fields.length === 3 &&
               findAction(room.mount, 'addField').disabled === true,
            'adding the last allowed field turns it off in the same pass');
        assert(room.inspector.count('fields').textContent === '3 / 3',
            'and the fact moved with it', room.inspector.count('fields').textContent);

        // ── an unusable table: no numbers, no adds (fail closed) ──
        const bare = makeRig({ limits: null });
        bare.select(bare.doc().embeds[0].id);
        assert(bare.inspector.count('title').textContent === '',
            'with no limits table the counter is blank (no invented number)',
            JSON.stringify(bare.inspector.count('title').textContent));
        assert(bare.inspector.count('title').className === 'mb2-count' &&
               bare.inspector.count('fields').className === 'mb2-count mb2-count-fields',
            'the fields counter carries the extra class its stylesheet rule hooks (beside the add button)',
            bare.inspector.count('fields').className);
        assert(bare.inspector.count('title').className === 'mb2-count',
            'and it carries no over-state', bare.inspector.count('title').className);
        assert(findAction(bare.mount, 'addField').disabled === true,
            'and the field cap fails closed (disabled), never unlimited');
        const bareActs = bare.dispatched.length;
        bare.click('addField');
        assert(bare.dispatched.length === bareActs, 'so the action refuses', String(bare.dispatched.length));

        // ── a loaded document repaints the counters without a keystroke ──
        const loaded = makeRig();
        const other = filledDocument(loaded.NERO);
        other.embeds[0].id = loaded.doc().embeds[0].id;
        other.embeds[0].title = rep('t', 100);
        loaded.store.dispatch({ type: 'document/load', document: other, meta: { history: false } });
        loaded.select(other.embeds[0].id);
        assert(loaded.inspector.count('title').textContent === '100 / 256',
            'a loaded document repaints the counters (no keystroke needed)',
            loaded.inspector.count('title').textContent);

        // ── teardown ──
        assert(loaded.inspector.destroy() === true, 'the inspector tears down');
        const panels = loaded.mount.children.length;
        assert(panels === 0, 'and its panels (with their counters) are off the tree');
        const w = loaded.inspector.stats().textWrites;
        loaded.store.dispatch({ type: 'content/set', text: 'after destroy' });
        assert(loaded.inspector.stats().textWrites === w,
            'no counter is written after teardown', String(loaded.inspector.stats().textWrites - w));
        assert(loaded.dom.warnings.length === 0, 'nothing warned during the counter work',
            loaded.dom.warnings.join(' | '));
    }

    // ═══════════════════════════════════════════════════════════════
    section('H. teardown');
    // ─────────────────────────────────────────────────────────────
    {
        const rig = makeRig();
        const before = rig.store._subscriberCounts().selectors;
        assert(before >= 2, 'the inspector subscribed to the store', String(before));
        const listenerCount = (rig.mount.listeners || []).length;
        assert(listenerCount >= 3, 'it listens for input, change and click', String(listenerCount));

        assert(rig.inspector.destroy() === true, 'destroy reports it did something');
        assert(rig.store._subscriberCounts().selectors === 0, 'no subscription survives');
        assert((rig.mount.listeners || []).length === 0, 'no listener survives');
        assert(rig.mount.children.length === 0, 'the mount is empty again');
        assert(rig.mount.getAttribute('data-insp-view') === null, 'and its view marker is gone');

        const dispatchedBefore = rig.dispatched.length;
        rig.mount.dispatch('input', { type: 'input', target: { getAttribute: () => 'content', value: 'x' } });
        rig.mount.dispatch('click', { type: 'click', target: { getAttribute: () => 'addField' } });
        assert(rig.dispatched.length === dispatchedBefore, 'and events after destroy do nothing');

        assert(rig.inspector.destroy() === false, 'destroy is idempotent');
        rig.store.dispatch({ type: 'ui/selectNode', nodeId: rig.doc().embeds[0].id });
        assert(rig.mount.children.length === 0, 'the store no longer repaints it');
    }

    // ═══════════════════════════════════════════════════════════════
    section('I. identity and cost of a keystroke');
    // ─────────────────────────────────────────────────────────────
    {
        const rig = makeRig();
        const embedId = rig.doc().embeds[0].id;
        rig.store.dispatch({ type: 'ui/selectNode', nodeId: embedId });

        const created = rig.created();
        const writes = rig.writes();
        const fieldRowsBefore = walk(rig.mount).filter(n => n.getAttribute('data-insp-action') === 'selectField');
        const nodeIdentity = fieldRowsBefore.map(n => n);

        const title = rig.input('title');
        for (let i = 0; i < 8; i++) rig.type(title, 'Typing ' + i);
        assert(rig.created() === created, 'a typing burst creates no nodes',
            String(rig.created() - created));
        assert(rig.writes() === writes, 'and rewrites no values (the caret stays put)',
            String(rig.writes() - writes));
        assert(title.value === 'Typing 7', 'the input holds what was typed');
        assert(rig.doc().embeds[0].title === 'Typing 7', 'and so does the document');

        // the field list reconciles by id: unrelated edits keep the same rows
        const rowsNow = walk(rig.mount).filter(n => n.getAttribute('data-insp-action') === 'selectField');
        assert(rowsNow.length === nodeIdentity.length && rowsNow.every((n, i) => n === nodeIdentity[i]),
            'editing the title does not rebuild the field rows');

        // swapping embeds reuses the inputs
        const titleNode = title;
        rig.store.dispatch({ type: 'embed/add' });
        const newEmbedId = rig.doc().embeds[1].id;
        rig.store.dispatch({ type: 'ui/selectNode', nodeId: newEmbedId });
        assert(rig.input('title') === titleNode, 'switching embeds reuses the same input node');
        assert(rig.input('title').value === '', 'and shows the new embed s values',
            rig.input('title').value);
        rig.store.dispatch({ type: 'ui/selectNode', nodeId: embedId });
        assert(rig.input('title').value === 'Typing 7', 'switching back shows the first embed again');

        // undo granularity: one burst is one undo step
        const depth = rig.store.historyDepth().size;
        for (let i = 0; i < 5; i++) rig.type('description', 'burst ' + i);
        assert(rig.store.historyDepth().size === depth + 1,
            'a burst in one control is ONE history entry',
            JSON.stringify(rig.store.historyDepth()));
        rig.store.undo();
        assert(rig.doc().embeds[0].description === 'Description one',
            'and one undo reverts the whole burst', rig.doc().embeds[0].description);
    }

    // ═══════════════════════════════════════════════════════════════
    section('J. control shapes (a checkbox cannot double-dispatch)');
    // ─────────────────────────────────────────────────────────────
    {
        const rig = makeRig();
        const fieldId = rig.doc().embeds[0].fields[0].id;
        rig.store.dispatch({ type: 'ui/selectNode', nodeId: fieldId });
        const box = rig.input('field.inline');
        box.checked = true;
        const before = rig.dispatched.length;
        rig.mount.dispatch('input', { type: 'input', target: box });
        rig.mount.dispatch('change', { type: 'change', target: box });
        assert(rig.dispatched.length === before + 1,
            'a checkbox that fires input AND change dispatches once',
            String(rig.dispatched.length - before));
        assert(rig.doc().embeds[0].fields[0].inline === true, 'and the value lands');
        assert(!!box.checked === true, 'the box stays checked');

        // The property path, not just the attribute: a browser reads `input.type`.
        assert(box.type === 'checkbox', 'the checkbox carries the type PROPERTY a browser reads',
            String(box.type));
        // With the attribute gone, the property alone must still drive the control
        // (this is what a real browser does; the DOM double only stores attributes).
        box.removeAttribute('type');
        box.checked = false;
        rig.mount.dispatch('change', { type: 'change', target: box });
        assert(rig.doc().embeds[0].fields[0].inline === false,
            'and it still toggles with the type attribute removed (property-driven)',
            String(rig.doc().embeds[0].fields[0].inline));

        // Cycling panels must never leave more than one mounted.
        const ids = [];
        ['content', rig.doc().embeds[0].id, fieldId, 'content', fieldId].forEach(id => {
            rig.select(id);
            ids.push(rig.mount.children.length);
        });
        assert(ids.every(n => n === 1), 'seven panel switches, one panel at a time',
            ids.join(','));

        // an event with no target, or an unknown target, is ignored
        const after = rig.dispatched.length;
        rig.mount.dispatch('input', { type: 'input' });
        rig.mount.dispatch('input', { type: 'input', target: rig.dom.document.createElement('div') });
        rig.mount.dispatch('click', { type: 'click', target: rig.dom.document.createElement('button') });
        assert(rig.dispatched.length === after, 'unrelated events are ignored');
    }
}

let aborted = null;
try {
    runAll();
} catch (err) {
    aborted = err;
    fail++;
    failures.push('section ' + currentSection + ' aborted: ' + ((err && err.message) || String(err)));
    console.log('\n  FAIL  section ' + currentSection + ' aborted before its checks completed');
    console.log('        ' + ((err && err.stack) || String(err)).split('\n').slice(0, 4).join('\n        '));
}

console.log('\nmessage-builder inspector: ' + pass + ' passed, ' + fail + ' failed');
if (fail) {
    console.log('Failures:');
    failures.forEach(f => console.log(' -', f));
    process.exit(1);
}
console.log('ALL MESSAGE-BUILDER INSPECTOR CHECKS PASSED');
