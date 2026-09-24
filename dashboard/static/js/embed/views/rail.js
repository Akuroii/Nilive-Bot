// ═══════════════════════════════════════════════════════════════
// NERO DASHBOARD — embed/views/rail.js
// Message Builder v2 — the Structure rail (phase 1, step 5b).
//
// WHAT THIS IS
//   A read-only-in-reverse view: it renders the structure of the canonical
//   document (message content → embeds → fields), shows which node is selected,
//   and translates user intent into STORE ACTIONS. It never touches the
//   document itself.
//
// THE RULES IT KEEPS (all asserted by scripts/test_message_builder_rail.js)
//
//   1. ONE SOURCE OF TRUTH. The rail holds no document and no copy of one. Every
//      row is derived from store.getState().document on render, and every
//      mutation leaves as a dispatch:
//        embed/add · embed/duplicate · embed/move · embed/remove
//        field/add · field/move · field/remove · ui/selectNode
//      The harness freezes the store's documents, so an in-place mutation
//      throws instead of quietly working — a bug class the tests catch rather
//      than a convention nobody can check.
//
//   2. IT RENDERS FROM STATE, NOT FROM EVENTS. It subscribes to the store (the
//      document and the selected node) and re-derives on every notification. An
//      edit made anywhere else — the inspector in 5c, a load, undo — shows up
//      here without the rail being told.
//
//   3. ROWS KEEP THEIR IDENTITY. Re-rendering reconciles by node id: a row that
//      still exists is the SAME element afterwards, so focus survives, a typing
//      burst costs no DOM writes, and adding a field creates exactly one row.
//
//   4. SELECTION IS STORE STATE; FOCUS IS VIEW STATE. `ui.selectedNodeId` lives
//      in the store (it is what the inspector will read in 5c). Which row
//      currently has DOM focus, and which embeds are collapsed, are view state:
//      they die with the mount and never touch the document.
//
// KEYBOARD (a real tree, not a list of links)
//   ↑/↓        move focus through the visible rows
//   Home/End   first / last visible row
//   →          expand an embed, or step into its fields
//   ←          collapse an embed, or step out to its embed
//   Enter/Space  select the focused row
//   Delete/Backspace  remove the focused embed or field, then focus the survivor
//   Tab is never intercepted (no focus trap); action buttons inside a row are
//   ordinary buttons in the tab order with labels of their own.
//
// NOT IN THIS FILE (deliberately): the inspector, validation, counters, limits,
// persistence, assets, components/actions/roles, templates, Send.
//
// Consumed by: embed/message-builder-page.js
// Tested by:   scripts/test_message_builder_rail.js (behaviour, this step),
//              scripts/test_message_builder_page.js §L (page integration),
//              scripts/support/mb_mutants.js (this file is a mutation target).
// ═══════════════════════════════════════════════════════════════
window.NERO = window.NERO || {};
window.NERO.embed = window.NERO.embed || {};
window.NERO.embed.views = window.NERO.embed.views || {};

(function (NERO) {
    'use strict';

    // The message-root node id. It is not a model id (there is no model node for
    // "the message content"), so it lives in the view vocabulary and the page
    // uses this constant to select it at boot.
    const CONTENT_NODE = 'content';

    const SNIPPET = 48;

    function snippet(text) {
        const clean = String(text == null ? '' : text).replace(/\s+/g, ' ').trim();
        if (!clean) return '';
        return clean.length > SNIPPET ? clean.slice(0, SNIPPET - 1) + '…' : clean;
    }

    function embedLabel(embed, index) {
        const title = snippet(embed && embed.title);
        const fields = (embed && embed.fields) ? embed.fields.length : 0;
        const parts = ['Embed ' + (index + 1)];
        if (title) parts.push(title);
        else parts.push('(no title)');
        if (fields) parts.push(fields === 1 ? '1 field' : fields + ' fields');
        return parts.join(' — ');
    }

    function fieldLabel(field, index) {
        const name = snippet(field && field.name);
        return 'Field ' + (index + 1) + (name ? ' — ' + name : ' — (no name)');
    }

    function contentLabel(document_) {
        const text = snippet(document_ && document_.content);
        return text ? 'Message content — ' + text : 'Message content';
    }

    /**
     * The flat row list behind the DOM. Flat (with aria-level) rather than
     * nested, because that is what a tree needs to be navigable by keyboard and
     * it makes reconciliation a single ordered list.
     */
    function derive(document_, collapsed) {
        const items = [{
            id: CONTENT_NODE,
            type: 'content',
            level: 1,
            posinset: 1,
            setsize: 1,
            label: contentLabel(document_),
            embedId: null,
            fieldId: null,
        }];
        const embeds = (document_ && document_.embeds) || [];
        embeds.forEach(function (embed, i) {
            const fields = embed.fields || [];
            const open = !collapsed.has(embed.id);
            items.push({
                id: embed.id,
                type: 'embed',
                level: 2,
                posinset: i + 1,
                setsize: embeds.length,
                label: embedLabel(embed, i),
                embedId: embed.id,
                fieldId: null,
                expandable: fields.length > 0,
                expanded: open && fields.length > 0,
                canMoveUp: i > 0,
                canMoveDown: i < embeds.length - 1,
                canRemove: embeds.length > 1,
            });
            if (!open) return;
            fields.forEach(function (field, j) {
                items.push({
                    id: field.id,
                    type: 'field',
                    level: 3,
                    posinset: j + 1,
                    setsize: fields.length,
                    label: fieldLabel(field, j),
                    embedId: embed.id,
                    fieldId: field.id,
                    canMoveUp: j > 0,
                    canMoveDown: j < fields.length - 1,
                    canRemove: true,
                });
            });
        });
        return items;
    }

    /**
     * create({ document, mount, store, label })
     *
     * `mount` becomes the tree element itself (the template's #mb2-rail-body),
     * so rows are its direct children and the page CSS that targets
     * `.mb2-rail-body > *` keeps working at narrow widths.
     */
    function create(options) {
        options = options || {};
        const doc = options.document;
        const store = options.store;
        const mount = options.mount;

        if (!doc || typeof doc.createElement !== 'function') {
            throw new TypeError('rail.create needs options.document');
        }
        if (!store || typeof store.dispatch !== 'function' || typeof store.subscribe !== 'function') {
            throw new TypeError('rail.create needs options.store');
        }
        if (!mount || typeof mount.appendChild !== 'function') {
            throw new TypeError('rail.create needs options.mount');
        }

        const stats = {
            renders: 0,
            nodesCreated: 0,
            nodesRemoved: 0,
            nodesReused: 0,
            attrWrites: 0,
            textWrites: 0,
            actions: 0,
        };

        const rows = new Map();          // node id -> row record
        const collapsed = new Set();     // embed ids the user folded away
        let focusedId = null;
        let pendingFocus = null;
        let destroyed = false;

        mount.setAttribute('role', 'tree');
        mount.setAttribute('aria-label', options.label || 'Message structure');

        // ── tiny DOM helpers (write only when the value differs) ──────
        function setAttr(node, name, value) {
            const next = String(value);
            if (node.getAttribute(name) !== next) {
                node.setAttribute(name, next);
                stats.attrWrites++;
            }
        }

        function setText(node, value) {
            if (node.textContent !== value) {
                node.textContent = value;
                stats.textWrites++;
            }
        }

        function setClass(node, name, on) {
            const has = node.classList ? node.classList.contains(name) : false;
            if (on && !has) node.classList.add(name);
            else if (!on && has) node.classList.remove(name);
        }

        // ── row construction ─────────────────────────────────────────
        function makeButton(row, action, label, ariaLabel, title) {
            const button = doc.createElement('button');
            button.className = 'mb2-rail-btn';
            button.setAttribute('type', 'button');
            button.setAttribute('data-rail-action', action);
            button.setAttribute('aria-label', ariaLabel);
            button.setAttribute('title', title || ariaLabel);
            setText(button, label);
            row.actions.appendChild(button);
            row.buttons[action] = button;
            stats.nodesCreated++;
            return button;
        }

        function buildRow() {
            const node = doc.createElement('div');
            node.className = 'mb2-rail-row';
            node.setAttribute('data-rail-row', '');
            node.setAttribute('role', 'treeitem');
            const label = doc.createElement('span');
            label.className = 'mb2-rail-label';
            const actions = doc.createElement('span');
            actions.className = 'mb2-rail-actions';
            node.appendChild(label);
            node.appendChild(actions);
            stats.nodesCreated += 3;
            return { node: node, label: label, actions: actions, buttons: {}, last: {} };
        }

        function updateRow(row, vm) {
            setText(row.label, vm.label);
            setAttr(row.node, 'data-node-id', vm.id);
            setAttr(row.node, 'data-rail-row', vm.type);
            setAttr(row.node, 'aria-level', vm.level);
            setAttr(row.node, 'aria-posinset', vm.posinset);
            setAttr(row.node, 'aria-setsize', vm.setsize);

            // Disclosure semantics belong to embeds that actually have fields.
            if (vm.type === 'embed' && vm.expandable) {
                setAttr(row.node, 'aria-expanded', vm.expanded ? 'true' : 'false');
            } else if (row.node.hasAttribute('aria-expanded')) {
                row.node.removeAttribute('aria-expanded');
                stats.attrWrites++;
            }
            setClass(row.node, 'mb2-rail-row-' + vm.type, true);
            setClass(row.node, 'mb2-rail-row-selected', false);   // set in paintSelection

            // Order of the action buttons is fixed per row type.
            if (vm.type === 'content') return;
            const wanted = vm.type === 'embed'
                ? ['addField', 'duplicate', 'up', 'down', 'remove']
                : ['up', 'down', 'remove'];
            wanted.forEach(function (action) {
                if (row.buttons[action]) return;
                if (action === 'addField') {
                    makeButton(row, action, '+', 'Add field to ' + vm.label, 'Add field');
                } else if (action === 'duplicate') {
                    makeButton(row, action, '⧉', 'Duplicate ' + vm.label, 'Duplicate embed');
                } else if (action === 'up') {
                    makeButton(row, action, '↑', 'Move ' + vm.label + ' up', 'Move up');
                } else if (action === 'down') {
                    makeButton(row, action, '↓', 'Move ' + vm.label + ' down', 'Move down');
                } else {
                    makeButton(row, action, '✕', 'Remove ' + vm.label, 'Remove');
                }
            });
            const disabled = {
                up: vm.canMoveUp === false,
                down: vm.canMoveDown === false,
                remove: vm.canRemove === false,
            };
            ['up', 'down', 'remove'].forEach(function (action) {
                const button = row.buttons[action];
                if (!button) return;
                const next = !!disabled[action];
                if (button.disabled !== next) button.disabled = next;
            });
        }

        // ── painting ─────────────────────────────────────────────────
        function paintSelection(selected) {
            rows.forEach(function (row, id) {
                const isSelected = id === selected;
                setAttr(row.node, 'aria-selected', isSelected ? 'true' : 'false');
                setClass(row.node, 'mb2-rail-row-selected', isSelected);
            });
        }

        function paintTabIndex() {
            let focusTarget = focusedId;
            if (!focusTarget || !rows.has(focusTarget)) focusTarget = currentSelection();
            if (!focusTarget || !rows.has(focusTarget)) {
                const first = rows.keys().next();
                focusTarget = first.done ? null : first.value;
            }
            rows.forEach(function (row, id) {
                setAttr(row.node, 'tabindex', id === focusTarget ? 0 : -1);
            });
            return focusTarget;
        }

        function currentSelection() {
            const ui = store.getUi ? store.getUi() : null;
            return (ui && ui.selectedNodeId) || null;
        }

        function render() {
            if (destroyed) return;
            const state = store.getState();
            const items = derive(state.document, collapsed);
            const wanted = new Map();
            items.forEach(function (vm) { wanted.set(vm.id, true); });

            // remove rows that no longer exist (before inserting, so positions
            // are computed against the final child list)
            Array.from(rows.keys()).forEach(function (id) {
                if (wanted.has(id)) return;
                const row = rows.get(id);
                row.node.remove();
                stats.nodesRemoved++;
                rows.delete(id);
                if (focusedId === id) focusedId = null;
            });

            items.forEach(function (vm, index) {
                let row = rows.get(vm.id);
                if (row) stats.nodesReused++;
                else {
                    row = buildRow();
                    rows.set(vm.id, row);
                }
                updateRow(row, vm);
                const atIndex = mount.children[index];
                if (atIndex !== row.node) mount.insertBefore(row.node, atIndex || null);
            });

            stampRows(items);
            paintSelection(currentSelection());
            paintTabIndex();
            applyFocus();
            stats.renders++;
        }

        /**
         * Each row records which embed it belongs to and who its parent is, so
         * keyboard navigation can reason about siblings and parents from the DOM
         * it just built rather than re-deriving the document a second time.
         */
        function stampRows(items) {
            const parents = new Map();
            let currentEmbed = null;
            items.forEach(function (vm) {
                if (vm.type === 'content') parents.set(vm.id, null);
                else if (vm.type === 'embed') { currentEmbed = vm.id; parents.set(vm.id, CONTENT_NODE); }
                else parents.set(vm.id, currentEmbed);
            });
            rows.forEach(function (row, id) {
                row.parentId = parents.has(id) ? parents.get(id) : null;
                const vm = items.find(function (item) { return item.id === id; });
                row.embedId = vm ? vm.embedId : null;
                row.fieldId = vm ? vm.fieldId : null;
                row.canRemove = vm ? vm.canRemove !== false : false;
            });
        }

        /** Focus is applied after the DOM exists and only when we asked for it. */
        function applyFocus() {
            if (!pendingFocus) return;
            const row = rows.get(pendingFocus);
            pendingFocus = null;
            if (!row) return;
            focusedId = row.node.getAttribute('data-node-id');
            row.node.focus();
        }

        /** Move DOM focus (and the roving tab stop) without changing selection. */
        function focusRow(id, opts) {
            const row = rows.get(id);
            if (!row) return false;
            focusedId = id;
            if (!opts || opts.tabindex !== false) paintTabIndex();
            row.node.focus();
            return true;
        }

        /** Visible rows in visual order — read from the DOM the rail just built. */
        function visibleIds() {
            const ids = [];
            mount.children.forEach(function (node) {
                const id = node.getAttribute('data-node-id');
                if (id) ids.push(id);
            });
            return ids;
        }

        function select(id) {
            if (!id) return false;
            if (currentSelection() === id) return false;
            store.dispatch({ type: 'ui/selectNode', nodeId: id });
            return true;
        }

        // ── structural actions ───────────────────────────────────────
        function idsIn(document_) {
            const ids = { embeds: new Set(), fields: new Set() };
            ((document_ && document_.embeds) || []).forEach(function (embed) {
                ids.embeds.add(embed.id);
                (embed.fields || []).forEach(function (field) { ids.fields.add(field.id); });
            });
            return ids;
        }

        /** Dispatch, then report the id the action just created (if any). */
        function dispatchAndPick(action, kind) {
            const before = idsIn(store.getState().document);
            store.dispatch(action);
            stats.actions++;
            const after = idsIn(store.getState().document);
            const pool = kind === 'field' ? after.fields : after.embeds;
            let created = null;
            pool.forEach(function (id) { if (!before[kind === 'field' ? 'fields' : 'embeds'].has(id)) created = id; });
            return created;
        }

        function addEmbed() {
            const created = dispatchAndPick({ type: 'embed/add' }, 'embed');
            if (created) { select(created); pendingFocus = created; render(); }
        }

        function addField(embedId) {
            const created = dispatchAndPick({ type: 'field/add', embedId: embedId }, 'field');
            if (created) {
                collapsed.delete(embedId);        // the new field must be visible
                select(created);
                pendingFocus = created;
                render();
            }
        }

        function duplicate(embedId) {
            const created = dispatchAndPick({ type: 'embed/duplicate', embedId: embedId }, 'embed');
            if (created) { select(created); pendingFocus = created; render(); }
        }

        function moveEmbed(embedId, delta) {
            store.dispatch({ type: 'embed/move', embedId: embedId, delta: delta });
            stats.actions++;
            pendingFocus = embedId;      // the row moved; the focus goes with it
            render();
        }

        function moveField(embedId, fieldId, delta) {
            store.dispatch({ type: 'field/move', embedId: embedId, fieldId: fieldId, delta: delta });
            stats.actions++;
            pendingFocus = fieldId;
            render();
        }

        /**
         * Where focus goes after a removal, decided BEFORE the row disappears:
         * the next sibling, else the previous sibling, else the parent embed (or
         * the message root). Never nothing — losing focus to <body> is how a
         * keyboard user gets teleported back to the top of the page.
         */
        function survivorAfter(removingId) {
            const ids = visibleIds();
            const index = ids.indexOf(removingId);
            const row = rows.get(removingId);
            const type = row ? row.node.getAttribute('data-rail-row') : null;
            const parentId = row ? row.parentId : null;
            const nextId = ids.slice(index + 1).find(function (id) {
                const candidate = rows.get(id);
                return candidate && candidate.parentId === parentId;
            });
            if (nextId) return nextId;
            const previousId = ids.slice(0, index).reverse().find(function (id) {
                const candidate = rows.get(id);
                return candidate && candidate.parentId === parentId;
            });
            if (previousId) return previousId;
            if (type === 'field') return parentId || CONTENT_NODE;
            return CONTENT_NODE;
        }

        function remove(nodeId) {
            const row = rows.get(nodeId);
            if (!row) return false;
            const type = row.node.getAttribute('data-rail-row');
            if (type === 'content') return false;
            const before = store.getState().document;
            const embed = ((before && before.embeds) || []).find(function (e) { return e.id === row.embedId; });
            if (!embed) return false;
            if (type === 'embed' && before.embeds.length <= 1) return false;   // the model refuses too

            const survivor = survivorAfter(nodeId);
            if (type === 'embed') store.dispatch({ type: 'embed/remove', embedId: row.embedId });
            else store.dispatch({ type: 'field/remove', embedId: row.embedId, fieldId: row.fieldId });
            stats.actions++;
            select(survivor);
            pendingFocus = survivor;
            render();
            return true;
        }

        // ── events ───────────────────────────────────────────────────
        function rowOf(target) {
            let node = target;
            while (node) {
                if (node.getAttribute && node.getAttribute('data-rail-row') !== null) return node;
                node = node.parentNode;
            }
            return null;
        }

        function onRowClick(event) {
            const rowNode = rowOf(event.target);
            if (!rowNode || destroyed) return;
            const actionNode = actionOf(event.target);
            const id = rowNode.getAttribute('data-node-id');
            if (actionNode) {
                runAction(actionNode.getAttribute('data-rail-action'), id);
                return;
            }
            select(id);
            focusRow(id);
        }

        function actionOf(target) {
            let node = target;
            while (node) {
                if (node.getAttribute && node.getAttribute('data-rail-action')) return node;
                node = node.parentNode;
            }
            return null;
        }

        function runAction(action, nodeId) {
            const row = rows.get(nodeId);
            if (!row) return;
            const embedId = row.embedId;
            if (action === 'addField') addField(embedId);
            else if (action === 'duplicate') duplicate(embedId);
            else if (action === 'up') {
                if (row.fieldId) moveField(embedId, row.fieldId, -1);
                else moveEmbed(embedId, -1);
            } else if (action === 'down') {
                if (row.fieldId) moveField(embedId, row.fieldId, 1);
                else moveEmbed(embedId, 1);
            } else if (action === 'remove') remove(nodeId);
        }

        function onKeyDown(event) {
            if (destroyed) return;
            const rowNode = rowOf(event.target);
            if (!rowNode) return;
            const id = rowNode.getAttribute('data-node-id');
            const type = rowNode.getAttribute('data-rail-row');
            const ids = visibleIds();
            const index = ids.indexOf(id);
            const onTheRowItself = event.target === rowNode;
            const key = event.key;

            if (key === 'ArrowDown') {
                prevent(event);
                if (index < ids.length - 1) focusRow(ids[index + 1]);
            } else if (key === 'ArrowUp') {
                prevent(event);
                if (index > 0) focusRow(ids[index - 1]);
            } else if (key === 'Home') {
                prevent(event);
                if (ids.length) focusRow(ids[0]);
            } else if (key === 'End') {
                prevent(event);
                if (ids.length) focusRow(ids[ids.length - 1]);
            } else if (key === 'ArrowRight') {
                if (type === 'embed' && rowNode.getAttribute('aria-expanded') === 'false') {
                    prevent(event);
                    collapsed.delete(id);
                    render();
                } else if (index < ids.length - 1) {
                    const child = rows.get(ids[index + 1]);
                    if (child && child.parentId === id) { prevent(event); focusRow(child.node.getAttribute('data-node-id')); }
                }
            } else if (key === 'ArrowLeft') {
                if (type === 'embed' && rowNode.getAttribute('aria-expanded') === 'true') {
                    prevent(event);
                    collapsed.add(id);
                    render();
                } else {
                    const row = rows.get(id);
                    if (row && row.parentId) { prevent(event); focusRow(row.parentId); }
                }
            } else if (key === 'Enter' || key === ' ' || key === 'Spacebar') {
                // Only when the row itself has focus: Enter/Space on one of the
                // row's buttons is that button's own activation.
                if (onTheRowItself) {
                    prevent(event);
                    select(id);
                }
            } else if (key === 'Delete' || key === 'Backspace') {
                prevent(event);
                remove(id);
            }
        }

        function prevent(event) {
            if (event && typeof event.preventDefault === 'function') event.preventDefault();
        }

        mount.addEventListener('click', onRowClick);
        mount.addEventListener('keydown', onKeyDown);

        const unsubs = [];
        unsubs.push(store.subscribe(function (s) { return s.document; }, function () { render(); }));
        unsubs.push(store.subscribe(function (s) { return s.ui.selectedNodeId; }, function () { render(); }));

        // First paint: the rail is derived from whatever the store already holds.
        render();

        function destroy() {
            if (destroyed) return false;
            destroyed = true;
            unsubs.splice(0).forEach(function (off) { try { off(); } catch (e) { /* fine */ } });
            mount.removeEventListener('click', onRowClick);
            mount.removeEventListener('keydown', onKeyDown);
            Array.from(rows.values()).forEach(function (row) { row.node.remove(); });
            rows.clear();
            mount.removeAttribute('role');
            mount.removeAttribute('aria-label');
            return true;
        }

        return {
            CONTENT_NODE: CONTENT_NODE,
            render: render,
            destroy: destroy,
            select: select,
            focus: focusRow,
            remove: remove,
            addEmbed: addEmbed,
            addField: addField,
            duplicate: duplicate,
            expand: function (embedId) { collapsed.delete(embedId); render(); },
            collapse: function (embedId) { collapsed.add(embedId); render(); },
            isCollapsed: function (embedId) { return collapsed.has(embedId); },
            focusedId: function () { return focusedId; },
            rowIds: function () { return visibleIds(); },
            rowNode: function (id) { const row = rows.get(id); return row ? row.node : null; },
            isSelected: function (id) { const row = rows.get(id); return row ? row.node.getAttribute('aria-selected') === 'true' : false; },
            stats: function () { return Object.assign({}, stats); },
        };
    }

    NERO.embed.views.rail = {
        create: create,
        derive: derive,
        CONTENT_NODE: CONTENT_NODE,
    };
})(window.NERO);
