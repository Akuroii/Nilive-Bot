// ═══════════════════════════════════════════════════════════════
// NERO DASHBOARD — embed/views/inspector.js
// Message Builder v2 — the property inspector (phase 1, step 5c).
//
// WHAT THIS IS
//   The editing surface for whatever the rail has selected: the message
//   content, an embed's properties, or one field. It is a DIRECT view over the
//   canonical document — the same relationship the rail has, with inputs
//   instead of rows.
//
// THE RULES IT KEEPS (all asserted by scripts/test_message_builder_inspector.js)
//
//   1. NO SECOND DOCUMENT STATE. This file holds no document, no copy of one,
//      and no per-control "last known value" cache. Every render derives from
//      store.getState(); every keystroke leaves as a dispatch and comes back
//      through the store's subscription:
//
//          input → store action → store state → render
//
//      The harness freezes every document the store publishes, so an in-place
//      write throws instead of quietly working, and a second subscription
//      proves the DOM follows the STORE and not the edit that caused it.
//
//   2. NO FORM ENGINE. There is no field registry, no schema, no declarative
//      control spec, and no loop over a property table: each control in the
//      panels below is created by its own explicit call, with its own label,
//      its own id and its own store action. Four tiny builders (text, textarea,
//      checkbox, button) wrap createElement — that is all the abstraction there
//      is, and it is DOM plumbing, not a framework.
//
//   3. NO VALIDATION RULES. It never decides whether a value is legal: the rules
//      live in embed/validate.js and their results in store.ui.issues (the page
//      paints the strip). What 6b adds here is a READOUT — each control shows
//      its own `used / max` from the same measurement the validator runs — plus
//      the field cap on the add button. No error paths, no aria-invalid,
//      no maxlength. Step 6 owns all of it. Native input behaviour only.
//
//   4. NO PERSISTENCE, NO PREVIEW. The inspector never touches drafts.js and
//      never calls the renderer: an edit reaches the preview the only way any
//      other edit does — through the store's document slice, which the page's
//      preview subscription already listens to. The harness asserts this by
//      running the inspector with no preview at all, and the mutation battery
//      guards the boundary.
//
//   5. SELECTION IS NOT OURS. The inspector reads ui.selectedNodeId from the
//      store and never keeps its own idea of what is selected. Where it offers
//      navigation (a field row, "select this embed"), it dispatches the same
//      ui/selectNode the rail dispatches, so the rail follows.
//
// THE ACTION SURFACE IT USES (nothing else; no document/load, no model writes)
//   content/set        {text}                       ← message content
//   embed/set          {embedId, patch}             ← title, description, url, timestamp
//   embed/setColor     {embedId, color}             ← colour (hex from <input type="color">)
//   embed/setAuthor    {embedId, patch}             ← author name / url / icon
//   embed/setFooter    {embedId, patch}             ← footer text / icon
//   embed/setMedia     {embedId, slot, value}       ← image / thumbnail url
//   field/add          {embedId}
//   field/set          {embedId, fieldId, patch}    ← name / value / inline
//   field/remove       {embedId, fieldId}
//   ui/selectNode      {nodeId}                     ← navigation only
//   Every text control carries meta.coalesceKey, so a typing burst stays ONE
//   undo step (the store's rule, not a second history of ours).
//
// NOT IN THIS FILE (deliberately): the validation RULES and the limits TABLE
// (it receives the served table and asks validate.counts()/caps() for numbers;
// it stores neither), persistence,
// assets/uploads, components/actions/roles, templates, publishing, preview
// rendering, and any structural action the rail already owns (add/duplicate/
// move/remove an EMBED is structure; adding and removing a FIELD is content, so
// it lives here next to the field list).
//
// Consumed by: embed/message-builder-page.js
// Tested by:   scripts/test_message_builder_inspector.js (behaviour, this step),
//              scripts/test_message_builder_page.js §M (page integration),
//              scripts/support/mb_mutants.js (this file is a mutation target).
// ═══════════════════════════════════════════════════════════════
window.NERO = window.NERO || {};
window.NERO.embed = window.NERO.embed || {};
window.NERO.embed.views = window.NERO.embed.views || {};

(function (NERO) {
    'use strict';

    // The message-root node id — the rail owns the vocabulary, the inspector
    // reads it (same constant, same meaning: "the message content").
    const CONTENT_NODE = 'content';

    function create(options) {
        options = options || {};
        const doc = options.document;
        const store = options.store;
        const mount = options.mount;
        const model = options.model || (NERO.embed.model || null);
        const now = typeof options.now === 'function' ? options.now : function () { return Date.now(); };
        // The served limits table, passed BY REFERENCE from the page: the same
        // object the validator is given. The inspector keeps no copy of it, and
        // it never parses data-limits itself (the page is the only parser).
        const limits = options.limits || null;

        if (!doc || typeof doc.createElement !== 'function') {
            throw new TypeError('inspector.create needs options.document');
        }
        if (!store || typeof store.dispatch !== 'function' || typeof store.subscribe !== 'function') {
            throw new TypeError('inspector.create needs options.store');
        }
        if (!mount || typeof mount.appendChild !== 'function') {
            throw new TypeError('inspector.create needs options.mount');
        }
        if (!model || typeof model.colorToHex !== 'function' || typeof model.mediaUrl !== 'function') {
            throw new TypeError('inspector.create needs the document model (embed/model.js)');
        }

        const stats = {
            renders: 0,
            nodesCreated: 0,
            nodesRemoved: 0,
            panelSwaps: 0,
            valueWrites: 0,
            textWrites: 0,
            attrWrites: 0,
            classWrites: 0,
            disabledWrites: 0,
            dispatches: 0,
            rowsCreated: 0,
        };

        const panels = {};                 // 'content' | 'embed' | 'field' | 'none'
        const fieldRows = new Map();       // field id -> row record (the embed panel's list)
        const unsubs = [];
        let visibleKey = 'none-setup';
        let destroyed = false;

        // The mount starts empty; `data-insp-view` is how the outside world (and
        // the tests) can see which panel is showing without inspecting children.
        mount.setAttribute('data-insp-view', 'none');

        // ── DOM plumbing (write only when the value differs) ─────────
        function el(tag, className, text) {
            const node = doc.createElement(tag);
            if (className) node.className = className;
            if (text !== undefined && text !== null) node.textContent = String(text);
            stats.nodesCreated++;
            return node;
        }

        function setText(node, value) {
            const next = String(value == null ? '' : value);
            if (node.textContent !== next) {
                node.textContent = next;
                stats.textWrites++;
            }
        }

        function setAttr(node, name, value) {
            const next = String(value);
            if (node.getAttribute(name) !== next) {
                node.setAttribute(name, next);
                stats.attrWrites++;
            }
        }

        /**
         * The caret rule. A controlled input must show the store's value, but
         * rewriting `value` on the element the user is typing into would move
         * the caret to the end on every keystroke. Since the store echoes back
         * exactly what was typed, "write only when it differs" is both correct
         * and caret-safe; an EXTERNAL change (undo, a load) differs, so it still
         * lands.
         */
        function setValue(node, value) {
            const next = String(value == null ? '' : value);
            if (node.value !== next) {
                node.value = next;
                stats.valueWrites++;
            }
        }

        function setChecked(node, on) {
            const next = !!on;
            if (node.checked !== next) {
                node.checked = next;
                stats.valueWrites++;
            }
        }

        function setClass(node, name, on) {
            const has = node.classList ? node.classList.contains(name) : false;
            if (on && !has) { node.classList.add(name); stats.classWrites++; }
            else if (!on && has) { node.classList.remove(name); stats.classWrites++; }
        }

        /** A disabled control is a real `disabled` property, written only when it changes. */
        function setDisabled(node, on) {
            const next = !!on;
            if (!node || node.disabled === next) return;
            node.disabled = next;
            stats.disabledWrites++;
        }

        // ── 6b: the numbers beside the controls ──────────────────────
        // Both readers are projections of the validator's ONE measurement pass
        // (embed/validate.js), fed with the served limits: the inspector does not
        // own a limit, does not compare anything itself, and keeps no cache — a
        // counter cannot drift from the rule it belongs to, because it is the
        // same comparison on the same helpers. Unusable limits ⇒ no numbers
        // (counters blank, add button disabled): fail closed, never "unlimited".
        function countFacts(document_) {
            const validate = NERO.embed.validate;
            if (!validate || typeof validate.counts !== 'function') return { ok: false, nodes: {} };
            const facts = validate.counts(document_, limits);
            return facts && facts.ok ? facts : { ok: false, nodes: {} };
        }

        function capFacts(document_) {
            const validate = NERO.embed.validate;
            const closed = { embeds: { used: 0, max: 0, canAdd: false }, fields: {} };
            if (!validate || typeof validate.caps !== 'function') return closed;
            const caps = validate.caps(document_, limits);
            return caps && caps.ok ? caps : closed;
        }

        /**
         * A counter element under one control, registered in the panel that owns
         * it. It is DECORATION: aria-hidden, not focusable, no live region — a
         * counter that announced every keystroke would talk over the user, and
         * the strip is where a problem is announced. `key` is the measurement's
         * own vocabulary (`content`, `title`, `author.name`, `total`, …), so
         * nothing is translated between measuring and painting.
         */
        function attachCount(bucket, wrap, key, extra) {
            const span = el('span', extra ? 'mb2-count ' + extra : 'mb2-count');
            span.setAttribute('aria-hidden', 'true');
            span.setAttribute('data-count', key);
            wrap.appendChild(span);
            bucket[key] = span;
            return span;
        }

        // ── four explicit control builders ───────────────────────────
        function textField(parent, id, labelText, key, opts) {
            opts = opts || {};
            const wrap = el('div', 'mb2-insp-field');
            const label = el('label', 'mb2-insp-label', labelText);
            label.setAttribute('for', id);
            const input = el('input', 'mb2-insp-input');
            input.type = opts.type || 'text';
            input.setAttribute('type', opts.type || 'text');
            input.setAttribute('id', id);
            input.setAttribute('data-insp', key);
            input.setAttribute('autocomplete', 'off');
            if (opts.placeholder) input.setAttribute('placeholder', opts.placeholder);
            wrap.appendChild(label);
            wrap.appendChild(input);
            parent.appendChild(wrap);
            return input;
        }

        function textAreaField(parent, id, labelText, key, rows) {
            const wrap = el('div', 'mb2-insp-field');
            const label = el('label', 'mb2-insp-label', labelText);
            label.setAttribute('for', id);
            const area = el('textarea', 'mb2-insp-input mb2-insp-textarea');
            area.setAttribute('id', id);
            area.setAttribute('data-insp', key);
            if (rows) area.setAttribute('rows', rows);
            wrap.appendChild(label);
            wrap.appendChild(area);
            parent.appendChild(wrap);
            return area;
        }

        function checkField(parent, id, labelText, key) {
            const wrap = el('label', 'mb2-insp-check');
            wrap.setAttribute('for', id);
            const box = el('input', 'mb2-insp-checkbox');
            box.type = 'checkbox';
            box.setAttribute('type', 'checkbox');
            box.setAttribute('id', id);
            box.setAttribute('data-insp', key);
            wrap.appendChild(box);
            wrap.appendChild(el('span', 'mb2-insp-checktext', labelText));
            parent.appendChild(wrap);
            return box;
        }

        function actionButton(parent, action, labelText, ariaLabel, className) {
            const button = el('button', className || 'mb2-insp-btn', labelText);
            button.setAttribute('type', 'button');
            button.setAttribute('data-insp-action', action);
            button.setAttribute('aria-label', ariaLabel || labelText);
            button.setAttribute('title', ariaLabel || labelText);
            parent.appendChild(button);
            return button;
        }

        function group(parent, legendText) {
            const set = el('fieldset', 'mb2-insp-group');
            set.appendChild(el('legend', 'mb2-insp-legend', legendText));
            parent.appendChild(set);
            return set;
        }

        // ── Node identification (derived, never cached) ──────────────
        /**
         * What is selected right now, resolved against the CURRENT document.
         * A selection that no longer exists is reported as 'unknown' rather than
         * remembered: the rail removes nodes, and an inspector that kept its own
         * target would go on editing something that is gone.
         */
        function selection() {
            const state = store.getState();
            const id = (state.ui && state.ui.selectedNodeId) || null;
            if (!id) return null;
            if (id === CONTENT_NODE) return { kind: 'content', id: CONTENT_NODE };
            const embeds = (state.document && state.document.embeds) || [];
            for (let i = 0; i < embeds.length; i++) {
                const embed = embeds[i];
                if (embed.id === id) {
                    return { kind: 'embed', id: id, embed: embed, embedIndex: i };
                }
                const fields = embed.fields || [];
                for (let j = 0; j < fields.length; j++) {
                    if (fields[j].id === id) {
                        return {
                            kind: 'field', id: id, embed: embed, embedIndex: i,
                            field: fields[j], fieldIndex: j,
                        };
                    }
                }
            }
            return { kind: 'unknown', id: id };
        }

        // ── Panels (one per selection kind, built once, reused) ──────
        // Panels are built lazily, kept alive when hidden (a panel swap is an
        // appendChild, so input nodes survive: focus, caret and identity), and
        // each is a literal sequence of explicit controls.

        function buildContentPanel() {
            const node = el('div', 'mb2-insp-panel');
            const content = textAreaField(node, 'mb2-insp-content', 'Message content', 'content', 8);
            content.setAttribute('placeholder', 'What the message says. Discord markdown is supported.');
            const counters = {};
            attachCount(counters, content.parentNode, 'content');
            node.appendChild(el('p', 'mb2-insp-hint',
                'Discord markdown is supported. Mention channels or users with <#id> and <@id>.'));
            return { node: node, controls: { content: content }, counters: counters };
        }

        function buildEmbedPanel() {
            const node = el('div', 'mb2-insp-panel');
            const controls = {};

            controls.title = textField(node, 'mb2-insp-title', 'Title', 'title');
            controls.description = textAreaField(node, 'mb2-insp-description', 'Description', 'description', 6);
            controls.url = textField(node, 'mb2-insp-url', 'Title link', 'url',
                { type: 'url', placeholder: 'https://example.com' });
            controls.timestamp = textField(node, 'mb2-insp-timestamp', 'Timestamp', 'timestamp',
                { placeholder: '2026-09-24T12:00:00.000Z' });
            // The timestamp row gets a button that fills it from the page's clock
            // (the same injected clock the preview header uses — no second clock).
            controls.timestamp.parentNode.className = 'mb2-insp-field mb2-insp-row';
            actionButton(controls.timestamp.parentNode, 'now', 'Now', 'Set the timestamp to now', 'mb2-insp-btn mb2-insp-now');
            controls.color = textField(node, 'mb2-insp-color', 'Colour', 'color', { type: 'color' });
            controls.color.className = 'mb2-insp-input mb2-insp-color';

            const author = group(node, 'Author');
            controls['author.name'] = textField(author, 'mb2-insp-author-name', 'Name', 'author.name');
            controls['author.url'] = textField(author, 'mb2-insp-author-url', 'Link', 'author.url',
                { type: 'url', placeholder: 'https://example.com' });
            controls['author.icon'] = textField(author, 'mb2-insp-author-icon', 'Icon URL', 'author.icon',
                { type: 'url', placeholder: 'https://example.com/icon.png' });

            const footer = group(node, 'Footer');
            controls['footer.text'] = textField(footer, 'mb2-insp-footer-text', 'Text', 'footer.text');
            controls['footer.icon'] = textField(footer, 'mb2-insp-footer-icon', 'Icon URL', 'footer.icon',
                { type: 'url', placeholder: 'https://example.com/icon.png' });

            const media = group(node, 'Media');
            controls['media.image'] = textField(media, 'mb2-insp-image', 'Large image URL', 'media.image',
                { type: 'url', placeholder: 'https://example.com/image.png' });
            controls['media.thumbnail'] = textField(media, 'mb2-insp-thumbnail', 'Thumbnail URL', 'media.thumbnail',
                { type: 'url', placeholder: 'https://example.com/thumb.png' });

            const fields = group(node, 'Fields');
            const list = el('ul', 'mb2-insp-fields');
            fields.appendChild(list);
            // 6b: the add button is disabled exactly at fields_max, and the
            // fields fact ("2 / 25") sits next to it, so a full embed is visible
            // as full rather than as a button that mysteriously does nothing.
            const addField = actionButton(fields, 'addField', '+ Add field',
                'Add a field to this embed', 'mb2-insp-btn');

            // The counters, one per control that actually has a served limit.
            // url/timestamp/color/icons have no limit in the served table, so
            // they get none: a counter needs a real maximum or it is a lie.
            const counters = {};
            attachCount(counters, controls.title.parentNode, 'title');
            attachCount(counters, controls.description.parentNode, 'description');
            attachCount(counters, controls['author.name'].parentNode, 'author.name');
            attachCount(counters, controls['footer.text'].parentNode, 'footer.text');
            const total = el('span', 'mb2-count mb2-count-total');
            total.setAttribute('aria-hidden', 'true');
            total.setAttribute('data-count', 'total');
            fields.appendChild(total);
            counters.total = total;
            // The fields counter sits BESIDE the add-field button rather than
            // under it (the button is the thing it explains), so it carries the
            // one extra class the stylesheet hooks: .mb2-count-fields.
            attachCount(counters, fields, 'fields', 'mb2-count-fields');

            return {
                node: node, controls: controls, fieldList: list,
                addField: addField, counters: counters,
            };
        }

        function buildFieldRow() {
            const row = el('li', 'mb2-insp-fieldrow');
            const select = actionButton(row, 'selectField', '', 'Edit field', 'mb2-insp-fieldbtn');
            const remove = actionButton(row, 'removeField', '✕', 'Remove field', 'mb2-insp-btn mb2-insp-remove');
            stats.rowsCreated++;
            return { li: row, select: select, remove: remove };
        }

        function fieldRowLabel(field, index) {
            const name = String((field && field.name) || '').replace(/\s+/g, ' ').trim();
            return 'Field ' + (index + 1) + (name ? ' — ' + name : ' — (no name)');
        }

        function updateFieldRow(row, field, index) {
            const label = fieldRowLabel(field, index);
            setText(row.select, label);
            setAttr(row.select, 'data-field-id', field.id);
            setAttr(row.select, 'aria-label', 'Edit ' + label);
            setAttr(row.remove, 'data-field-id', field.id);
            setAttr(row.remove, 'aria-label', 'Remove ' + label);
            setAttr(row.remove, 'title', 'Remove ' + label);
        }

        function syncFieldList(list, embed) {
            const fields = embed.fields || [];
            const wanted = new Set();
            fields.forEach(function (field, index) {
                wanted.add(field.id);
                let row = fieldRows.get(field.id);
                if (!row) {
                    row = buildFieldRow();
                    fieldRows.set(field.id, row);
                }
                updateFieldRow(row, field, index);
                if (list.children[index] !== row.li) {
                    list.insertBefore(row.li, list.children[index] || null);
                }
            });
            Array.from(fieldRows.keys()).forEach(function (id) {
                if (wanted.has(id)) return;
                const row = fieldRows.get(id);
                if (row.li.parentNode) row.li.parentNode.removeChild(row.li);
                fieldRows.delete(id);
                stats.nodesRemoved++;
            });
        }

        function buildFieldPanel() {
            const node = el('div', 'mb2-insp-panel');
            const contextRow = el('div', 'mb2-insp-row mb2-insp-contextrow');
            const context = el('div', 'mb2-insp-context');
            contextRow.appendChild(context);
            // Navigation only: it dispatches the same ui/selectNode the rail does,
            // so the rail's selected row and this panel move together.
            actionButton(contextRow, 'selectEmbed', 'Embed', 'Select this field’s embed in the structure',
                'mb2-insp-btn mb2-insp-linkbtn');
            node.appendChild(contextRow);
            const controls = { context: context };
            controls['field.name'] = textField(node, 'mb2-insp-field-name', 'Name', 'field.name');
            controls['field.value'] = textAreaField(node, 'mb2-insp-field-value', 'Value', 'field.value', 3);
            const counters = {};
            attachCount(counters, controls['field.name'].parentNode, 'field.name');
            attachCount(counters, controls['field.value'].parentNode, 'field.value');
            controls['field.inline'] = checkField(node, 'mb2-insp-field-inline', 'Show on the same line as the next field', 'field.inline');
            node.appendChild(el('p', 'mb2-insp-hint',
                'Move or remove this field from the structure panel on the left.'));
            return { node: node, controls: controls, counters: counters };
        }

        function buildEmptyPanel() {
            const node = el('div', 'mb2-insp-panel');
            const message = el('p', 'mb2-insp-empty');
            node.appendChild(message);
            actionButton(node, 'selectContent', 'Edit message content', 'Select the message content', 'mb2-insp-btn');
            return { node: node, controls: { message: message } };
        }

        const BUILDERS = {
            content: buildContentPanel,
            embed: buildEmbedPanel,
            field: buildFieldPanel,
            none: buildEmptyPanel,
        };

        function ensurePanel(key) {
            if (!panels[key]) panels[key] = BUILDERS[key]();
            return panels[key];
        }

        function showPanel(key) {
            const panel = ensurePanel(key);
            const previous = panels[visibleKey];
            // Exactly ONE panel is ever mounted: the previous one is detached
            // (not discarded — it keeps its nodes, so flipping the selection
            // back and forth rebuilds nothing and reuses the same inputs).
            if (previous && previous.node.parentNode) previous.node.parentNode.removeChild(previous.node);
            mount.appendChild(panel.node);
            setAttr(mount, 'data-insp-view', key);
            visibleKey = key;
            stats.panelSwaps++;
        }

        // ── Dispatch helpers (ids only; values are normalized by the model) ──
        function dispatch(action) {
            stats.dispatches++;
            return store.dispatch(action);
        }

        function dispatchScalar(sel, key, value) {
            if (String(sel.embed[key] || '') === String(value)) return false;
            dispatch({
                type: 'embed/set',
                embedId: sel.embed.id,
                patch: buildPatch(key, value),
                meta: { coalesceKey: 'embed:' + sel.embed.id + ':' + key },
            });
            return true;
        }

        // An explicit patch — not a generic "set a path" helper: which keys are
        // settable, and how, is decided here, per control.
        function buildPatch(key, value) {
            if (key === 'title') return { title: String(value) };
            if (key === 'description') return { description: String(value) };
            if (key === 'url') return { url: String(value) };
            if (key === 'timestamp') return { timestamp: String(value) };
            throw new Error('inspector: unknown embed key ' + key);
        }

        function dispatchAuthor(sel, key, value) {
            const current = key === 'icon' ? model.mediaUrl(sel.embed.author.icon) : String(sel.embed.author[key] || '');
            if (current === String(value)) return false;
            const patch = key === 'name' ? { name: String(value) }
                : key === 'url' ? { url: String(value) }
                : { icon: String(value) };
            dispatch({
                type: 'embed/setAuthor',
                embedId: sel.embed.id,
                patch: patch,
                meta: { coalesceKey: 'author:' + sel.embed.id + ':' + key },
            });
            return true;
        }

        function dispatchFooter(sel, key, value) {
            const current = key === 'icon' ? model.mediaUrl(sel.embed.footer.icon) : String(sel.embed.footer[key] || '');
            if (current === String(value)) return false;
            const patch = key === 'text' ? { text: String(value) } : { icon: String(value) };
            dispatch({
                type: 'embed/setFooter',
                embedId: sel.embed.id,
                patch: patch,
                meta: { coalesceKey: 'footer:' + sel.embed.id + ':' + key },
            });
            return true;
        }

        function dispatchMedia(sel, slot, value) {
            if (model.mediaUrl(sel.embed[slot]) === String(value)) return false;
            dispatch({
                type: 'embed/setMedia',
                embedId: sel.embed.id,
                slot: slot,
                value: String(value),
                meta: { coalesceKey: 'media:' + sel.embed.id + ':' + slot },
            });
            return true;
        }

        function dispatchColor(sel, value) {
            if (model.colorToHex(sel.embed.color) === String(value)) return false;
            dispatch({
                type: 'embed/setColor',
                embedId: sel.embed.id,
                color: String(value),
                meta: { coalesceKey: 'color:' + sel.embed.id },
            });
            return true;
        }

        function dispatchField(sel, key, value) {
            if (key === 'inline') {
                if (!!value === !!sel.field.inline) return false;
                dispatch({
                    type: 'field/set',
                    embedId: sel.embed.id,
                    fieldId: sel.field.id,
                    patch: { inline: !!value },
                    meta: { coalesceKey: 'field:' + sel.field.id + ':inline' },
                });
                return true;
            }
            if (String(sel.field[key] || '') === String(value)) return false;
            dispatch({
                type: 'field/set',
                embedId: sel.embed.id,
                fieldId: sel.field.id,
                patch: key === 'name' ? { name: String(value) } : { value: String(value) },
                meta: { coalesceKey: 'field:' + sel.field.id + ':' + key },
            });
            return true;
        }

        // ── The one place a control key becomes a store action ───────
        // A switch, not a table: every key is written out with the action it
        // sends. Unknown keys fall through and do nothing.
        function apply(key, value) {
            const sel = selection();
            if (!sel) return false;
            switch (key) {
                case 'content':
                    if (String(store.getState().document.content || '') === String(value)) return false;
                    dispatch({ type: 'content/set', text: String(value), meta: { coalesceKey: 'content' } });
                    return true;
                case 'title':
                case 'description':
                case 'url':
                case 'timestamp':
                    if (sel.kind !== 'embed') return false;
                    return dispatchScalar(sel, key, value);
                case 'color':
                    if (sel.kind !== 'embed') return false;
                    return dispatchColor(sel, value);
                case 'author.name':
                    if (sel.kind !== 'embed') return false;
                    return dispatchAuthor(sel, 'name', value);
                case 'author.url':
                    if (sel.kind !== 'embed') return false;
                    return dispatchAuthor(sel, 'url', value);
                case 'author.icon':
                    if (sel.kind !== 'embed') return false;
                    return dispatchAuthor(sel, 'icon', value);
                case 'footer.text':
                    if (sel.kind !== 'embed') return false;
                    return dispatchFooter(sel, 'text', value);
                case 'footer.icon':
                    if (sel.kind !== 'embed') return false;
                    return dispatchFooter(sel, 'icon', value);
                case 'media.image':
                    if (sel.kind !== 'embed') return false;
                    return dispatchMedia(sel, 'image', value);
                case 'media.thumbnail':
                    if (sel.kind !== 'embed') return false;
                    return dispatchMedia(sel, 'thumbnail', value);
                case 'field.name':
                    if (sel.kind !== 'field') return false;
                    return dispatchField(sel, 'name', value);
                case 'field.value':
                    if (sel.kind !== 'field') return false;
                    return dispatchField(sel, 'value', value);
                case 'field.inline':
                    if (sel.kind !== 'field') return false;
                    return dispatchField(sel, 'inline', value);
                default:
                    return false;
            }
        }

        // ── Events ──────────────────────────────────────────────────
        function controlValue(node) {
            // `.type` in a browser, the attribute in a harness that never
            // reflects it — the checkbox contract must hold in both.
            const kind = node.type || (typeof node.getAttribute === 'function' && node.getAttribute('type')) || 'text';
            if (kind === 'checkbox') return !!node.checked;
            return node.value === undefined ? '' : String(node.value);
        }

        function onControl(event) {
            if (destroyed) return;
            const target = event && event.target;
            if (!target || typeof target.getAttribute !== 'function') return;
            const key = target.getAttribute('data-insp');
            if (!key) return;
            apply(key, controlValue(target));
        }

        function runAction(action, node) {
            if (destroyed) return false;
            const sel = selection();
            const fieldId = node && typeof node.getAttribute === 'function'
                ? node.getAttribute('data-field-id') : null;
            switch (action) {
                case 'selectContent':
                    dispatch({ type: 'ui/selectNode', nodeId: CONTENT_NODE });
                    return true;
                case 'selectField':
                    if (!fieldId) return false;
                    dispatch({ type: 'ui/selectNode', nodeId: fieldId });
                    return true;
                case 'selectEmbed':
                    if (!sel || !sel.embed) return false;
                    dispatch({ type: 'ui/selectNode', nodeId: sel.embed.id });
                    return true;
                case 'addField': {
                    if (!sel || !sel.embed) return false;
                    const cap = capFacts(store.getState().document).fields[sel.embed.id];
                    if (!cap || !cap.canAdd) return false;   // 6b: at the cap the button is off
                    dispatch({ type: 'field/add', embedId: sel.embed.id });
                    return true;
                }
                case 'removeField':
                    if (!sel || !sel.embed || !fieldId) return false;
                    dispatch({ type: 'field/remove', embedId: sel.embed.id, fieldId: fieldId });
                    return true;
                case 'now': {
                    if (!sel || sel.kind !== 'embed') return false;
                    const clock = now();
                    const date = clock instanceof Date ? clock : new Date(clock);
                    dispatchScalar(sel, 'timestamp', date.toISOString());
                    return true;
                }
                default:
                    return false;
            }
        }

        function onClick(event) {
            if (destroyed) return;
            const target = event && event.target;
            if (!target || typeof target.getAttribute !== 'function') return;
            const action = target.getAttribute('data-insp-action');
            if (!action) return;
            runAction(action, target);
        }

        /**
         * Paint the visible panel's counters (6b). The facts are keyed by NODE, so
         * the panel only has to know which node it is showing — it does not walk
         * the document, and a hidden panel's counters are left exactly as they
         * were (nothing writes to a panel nobody can see). Both the text and the
         * over-state are change-guarded, so a keystroke that changes no counter
         * costs no DOM write.
         */
        function paintCounts(sel, panel) {
            const counters = panel && panel.counters;
            if (!counters) return;
            const list = sel && sel.id ? countFacts(store.getState().document).nodes[sel.id] : null;
            const byKey = new Map();
            (list || []).forEach(function (entry) { byKey.set(entry.key, entry); });
            Object.keys(counters).forEach(function (key) {
                const entry = byKey.get(key) || null;
                setText(counters[key], entry ? entry.used + ' / ' + entry.max : '');
                setClass(counters[key], 'mb2-count-over', !!(entry && entry.over));
            });
        }

        // ── Render ──────────────────────────────────────────────────
        function render() {
            if (destroyed) return;
            stats.renders++;
            const sel = selection();
            const key = sel && (sel.kind === 'content' || sel.kind === 'embed' || sel.kind === 'field')
                ? sel.kind : 'none';
            if (key !== visibleKey) showPanel(key);
            const panel = panels[key];

            if (key === 'content') {
                setValue(panel.controls.content, store.getState().document.content || '');
                paintCounts(sel, panel);
                return;
            }

            if (key === 'embed') {
                const embed = sel.embed;
                setValue(panel.controls.title, embed.title || '');
                setValue(panel.controls.description, embed.description || '');
                setValue(panel.controls.url, embed.url || '');
                setValue(panel.controls.timestamp, embed.timestamp || '');
                setValue(panel.controls.color, model.colorToHex(embed.color));
                setValue(panel.controls['author.name'], embed.author.name || '');
                setValue(panel.controls['author.url'], embed.author.url || '');
                setValue(panel.controls['author.icon'], model.mediaUrl(embed.author.icon));
                setValue(panel.controls['footer.text'], embed.footer.text || '');
                setValue(panel.controls['footer.icon'], model.mediaUrl(embed.footer.icon));
                setValue(panel.controls['media.image'], model.mediaUrl(embed.image));
                setValue(panel.controls['media.thumbnail'], model.mediaUrl(embed.thumbnail));
                syncFieldList(panel.fieldList, embed);
                const cap = capFacts(store.getState().document).fields[embed.id];
                setDisabled(panel.addField, !(cap && cap.canAdd));
                paintCounts(sel, panel);
                return;
            }

            if (key === 'field') {
                const embed = sel.embed;
                const title = String(embed.title || '').replace(/\s+/g, ' ').trim();
                setText(panel.controls.context,
                    'Field ' + (sel.fieldIndex + 1) + ' in ' + (title || '(untitled embed)'));
                setValue(panel.controls['field.name'], sel.field.name || '');
                setValue(panel.controls['field.value'], sel.field.value || '');
                setChecked(panel.controls['field.inline'], sel.field.inline);
                paintCounts(sel, panel);
                return;
            }

            setText(panel.controls.message, sel
                ? 'The selected part of the message is no longer there. Pick something in the structure panel.'
                : 'Select the message content, an embed or a field in the structure panel to edit it.');
        }

        // ── Wiring (the same selector subscriptions the rail uses) ───
        mount.addEventListener('input', onControl);
        mount.addEventListener('change', onControl);
        mount.addEventListener('click', onClick);
        unsubs.push(store.subscribe(function (s) { return s.ui.selectedNodeId; }, function () { render(); }));
        unsubs.push(store.subscribe(function (s) { return s.document; }, function () { render(); }));

        // First paint: derived from whatever the store already holds.
        render();

        function destroy() {
            if (destroyed) return false;
            destroyed = true;
            unsubs.splice(0).forEach(function (off) { try { off(); } catch (e) { /* already off */ } });
            mount.removeEventListener('input', onControl);
            mount.removeEventListener('change', onControl);
            mount.removeEventListener('click', onClick);
            Object.keys(panels).forEach(function (key) {
                const node = panels[key].node;
                if (node && node.parentNode) node.parentNode.removeChild(node);
                delete panels[key];
            });
            fieldRows.clear();
            visibleKey = null;
            mount.removeAttribute('data-insp-view');
            return true;
        }

        function control(key) {
            const panel = panels[visibleKey];
            if (!panel) return null;
            return panel.controls[key] || null;
        }

        return {
            CONTENT_NODE: CONTENT_NODE,
            render: render,
            destroy: destroy,
            selection: selection,
            view: function () { return visibleKey; },
            panel: function () { const p = panels[visibleKey]; return p ? p.node : null; },
            control: control,
            /** The counter element for a key of the VISIBLE panel (6b), or null. */
            count: function (key) {
                const panel = panels[visibleKey];
                return panel && panel.counters ? (panel.counters[key] || null) : null;
            },
            stats: function () { return Object.assign({}, stats); },
        };
    }

    NERO.embed.views.inspector = {
        create: create,
        CONTENT_NODE: CONTENT_NODE,
    };
})(window.NERO);
