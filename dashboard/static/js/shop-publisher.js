/* ═══════════════════════════════════════════════════════════════
   Shop Publisher — the page module (Step 0 contract).

   Step 0 scope is template selection + product selection + Publisher preview
   of the SERVER-resolved design draft + preview warnings + the purchase
   action that will actually be published. There is no publish/send here.

   The token resolver is server-side (utils/shop_publisher.py — fixed,
   deterministic, non-programmable) and shared with the future publish path.
   This module interprets NOTHING: it renders what
   GET  /api/shop-publisher/catalog
   POST /api/shop-publisher/preview
   return, and the purchase action row renders the API's action descriptor
   verbatim (entries carrying custom_id shop_buy_<id> — the existing purchase
   mechanism cogs/shop.py already dispatches).

   The preview request is a Design draft: {presentation{mode,content,embeds},
   products[], action{kind, entries}} — products[] is the ROOT-PRODUCT roster
   (this transitional page always sends the one selected product). The three-
   region Designer workspace supersedes this page in a later step; until then
   this keeps the approved contract exercised end to end.

   Rendering reuses the FROZEN Message Builder preview engine read-only
   (embed/preview.js + embed/discord-markdown.js + embed/model.js, loaded
   before this file via data-page-script). The mount element belongs to
   preview.js; the purchase action row is its sibling.

   Lifecycle: registered through nav-lifecycle.js's NERO.definePage, so every
   listener, in-flight fetch and the preview instance are released through the
   page context on htmx navigation (ctx.on / ctx.fetch / ctx.cleanup). No
   global mutable state survives destroy.
   ═══════════════════════════════════════════════════════════════ */
window.NERO = window.NERO || {};

(function (NERO) {
    'use strict';

    var CATALOG_URL = '/api/shop-publisher/catalog';
    var PREVIEW_URL = '/api/shop-publisher/preview';
    // The existing Embed Builder read route (frozen boundary, LEVEL_OWNER) —
    // NOT a new loader route. The catalog serves names only (explicit final
    // Step 0 contract — Q2#12 CLOSED 2026-09-30: keeps the payload light at
    // ~100-template scale; only the selected document is fetched). This
    // route is the Step 0 TRANSITIONAL presentation source only — not the
    // final Shop load/snapshot architecture; that stays deferred to the
    // Step 1+ design discussion.
    var TEMPLATE_URL = '/api/embedbuilder/template/';
    // Slice 2 Category integration: the Slice 1 contracts, reused as-is.
    // Membership is presentation metadata and NEVER implies publication.
    var CATEGORIES_URL = '/api/shop-publisher/categories';
    var ASSIGN_URL = '/api/shop-publisher/products/category';
    // Design Draft Persistence: saved drafts hold orchestration/presentation
    // only. The saved presentation is the design's OWN snapshot (normalized
    // at save); source_template_name is provenance and never dereferenced.
    var DESIGNS_URL = '/api/shop-publisher/designs';

    // ── Small DOM helpers (createElement + textContent only) ─────
    function el(doc, tag, className, text) {
        var node = doc.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function clear(node) {
        while (node.firstChild) node.removeChild(node.firstChild);
    }

    // ── Pickers ─────────────────────────────────────────────────
    // Template picker: plain options, template names (embed_templates) —
    // names-only catalog (explicit final Step 0 contract, Q2#12 closed
    // 2026-09-30). The template DOCUMENT is read per selection from the
    // Embed Builder's existing read route (Step 0 transitional source; the
    // permanent load/snapshot policy is deferred to Step 1+).
    function fillTemplateSelect(doc, select, names) {
        var current = select.value;
        clear(select);
        var placeholder = el(doc, 'option', '', 'Select a template…');
        placeholder.value = '';
        select.appendChild(placeholder);
        (names || []).forEach(function (item) {
            var name = typeof item === 'string' ? item : item.name;
            var option = el(doc, 'option', '', name);
            option.value = name;
            select.appendChild(option);
        });
        if (current) select.value = current;
    }

    // Product picker: grouped by the EXISTING `type` column and nothing else
    // (locked decision — no new category system). One <optgroup> per type,
    // groups in the server's V1 order (products arrive pre-sorted by
    // type_group_key); each product appears exactly once.
    // ── Category pickers (Slice 2) ────────────────────────────────
    // The product list groups by Shop Category (the Slice 1 link) with an
    // Uncategorized bucket; `type` is display metadata (a badge), never the
    // grouping mechanism. A link to a missing category falls into the
    // Uncategorized bucket — grouping only, no warning machinery.
    function categoryLabel(category) {
        return (category.emoji ? category.emoji + ' ' : '') +
            (category.name || ('#' + category.id));
    }

    function fillCategorySelect(doc, select, categories, products) {
        var current = select.value;
        clear(select);
        var all = el(doc, 'option', '', 'All products');
        all.value = '';
        select.appendChild(all);
        var counts = {};
        var uncategorized = 0;
        (products || []).forEach(function (product) {
            if (product.category_id == null) {
                uncategorized += 1;
            } else {
                counts[product.category_id] = (counts[product.category_id] || 0) + 1;
            }
        });
        (categories || []).forEach(function (category) {
            var option = el(doc, 'option', '', categoryLabel(category) +
                ' (' + (counts[category.id] || 0) + ')');
            option.value = String(category.id);
            select.appendChild(option);
        });
        var none = el(doc, 'option', '', 'Uncategorized (' + uncategorized + ')');
        none.value = 'none';
        select.appendChild(none);
        var found = false;
        for (var i = 0; i < select.children.length; i += 1) {
            if (select.children[i].value === current) found = true;
        }
        select.value = (current && found) ? current : '';
    }

    function fillAssignSelect(doc, select, categories) {
        var current = select.value;
        clear(select);
        var none = el(doc, 'option', '', 'Uncategorized');
        none.value = '';
        select.appendChild(none);
        (categories || []).forEach(function (category) {
            var option = el(doc, 'option', '', categoryLabel(category));
            option.value = String(category.id);
            select.appendChild(option);
        });
        var found = false;
        for (var i = 0; i < select.children.length; i += 1) {
            if (select.children[i].value === current) found = true;
        }
        select.value = (current && found) ? current : '';
    }

    function fillProductSelect(doc, select, products, categories, filter) {
        var current = select.value;
        clear(select);
        var placeholder = el(doc, 'option', '', 'Select a product…');
        placeholder.value = '';
        select.appendChild(placeholder);
        var groups = {};
        var order = [];
        (categories || []).forEach(function (category) {
            var key = String(category.id);
            groups[key] = el(doc, 'optgroup', '');
            groups[key].label = category.name || ('#' + category.id);
            order.push(key);
        });
        groups.none = el(doc, 'optgroup', '');
        groups.none.label = 'Uncategorized';
        order.push('none');
        var currentProduct = null;
        (products || []).forEach(function (product) {
            // This picker supplies products[] (the root roster), so Options
            // are never offered as standalone products. The catalog and
            // category assignment data remain unchanged.
            if (product.option_of_id !== null && product.option_of_id !== undefined) return;
            var key = product.category_id == null ? 'none' : String(product.category_id);
            if (!groups[key]) key = 'none';
            if (String(product.id) === String(current)) currentProduct = { product: product, key: key };
            if (filter && filter !== '' && filter !== key) return;
            var label = product.name || ('#' + product.id);
            if (product.enabled === 0) label += ' (disabled)';
            if (product.type) label += ' · ' + product.type;
            var option = el(doc, 'option', '', label);
            option.value = String(product.id);
            groups[key].appendChild(option);
        });
        order.forEach(function (key) {
            if (groups[key].children.length) select.appendChild(groups[key]);
        });
        // Keep a valid current root visibly selected when a category filter
        // excludes it. This keeps the picker, draft, Preview, and Save aligned.
        if (currentProduct && filter && filter !== '' && filter !== currentProduct.key) {
            var selectedGroup = el(doc, 'optgroup', '');
            selectedGroup.label = 'Current selection (outside filter)';
            var selected = currentProduct.product;
            var selectedLabel = selected.name || ('#' + selected.id);
            if (selected.enabled === 0) selectedLabel += ' (disabled)';
            if (selected.type) selectedLabel += ' · ' + selected.type;
            var selectedOption = el(doc, 'option', '', selectedLabel);
            selectedOption.value = String(selected.id);
            selectedGroup.appendChild(selectedOption);
            select.appendChild(selectedGroup);
        }
        var found = false;
        for (var i = 0; i < select.children.length; i += 1) {
            var child = select.children[i];
            if (child.tagName === 'OPTION' && child.value === current) found = true;
            for (var j = 0; j < (child.children ? child.children.length : 0); j += 1) {
                if (child.children[j].value === current) found = true;
            }
        }
        select.value = (current && found) ? current : '';
    }

    // ── Purchase action (rendered verbatim from the API descriptor) ──
    // One descriptor, three kinds — all resolve to the EXISTING shop_buy_<id>
    // mechanism. Buttons render as button chips; select kinds render a
    // faithful option list (placeholder + options) alongside the exact
    // custom ids that will be published.
    function renderPurchase(doc, container, action) {
        clear(container);
        if (!action || !action.entries || !action.entries.length) {
            container.hidden = true;
            return;
        }
        container.hidden = false;

        container.appendChild(el(doc, 'div', 'sp-purchase-label',
            'Purchase action (published with the message)'));

        var row = el(doc, 'div', 'sp-purchase-row');
        if (action.kind === 'buttons') {
            action.entries.forEach(function (entry) {
                var button = el(doc, 'span', 'sp-buy-button');
                if (entry.free) button.className += ' sp-buy-free';
                if (entry.emoji) button.appendChild(el(doc, 'span', 'sp-buy-emoji', entry.emoji));
                button.appendChild(el(doc, 'span', 'sp-buy-text', entry.label || ''));
                row.appendChild(button);
            });
        } else {
            var select = el(doc, 'div', 'sp-select-mock');
            select.appendChild(el(doc, 'div', 'sp-select-placeholder',
                (action.placeholder || 'Select…') + ' ▾'));
            action.entries.forEach(function (entry) {
                var option = el(doc, 'div', 'sp-select-option');
                option.appendChild(el(doc, 'span', 'sp-buy-text',
                    (entry.emoji ? entry.emoji + ' ' : '') + (entry.label || '')));
                if (entry.description) {
                    option.appendChild(el(doc, 'span', 'sp-select-desc', entry.description));
                }
                select.appendChild(option);
            });
            row.appendChild(select);
        }
        container.appendChild(row);

        // The exact contract the publish step reuses: custom ids route to the
        // EXISTING shop purchase mechanism (cogs/shop.py on_interaction).
        var meta = el(doc, 'div', 'sp-purchase-meta');
        if (action.kind === 'buttons') {
            action.entries.forEach(function (entry, index) {
                if (index) meta.appendChild(doc.createTextNode(' · '));
                meta.appendChild(el(doc, 'code', '', 'custom_id: ' + (entry.custom_id || '')));
            });
            meta.appendChild(doc.createTextNode(
                ' — green buttons handled by the existing shop purchase mechanism.'));
        } else {
            meta.appendChild(el(doc, 'code', '', 'custom_id: ' + (action.component_custom_id || '')));
            meta.appendChild(doc.createTextNode(' with option values '));
            action.entries.forEach(function (entry, index) {
                if (index) meta.appendChild(doc.createTextNode(', '));
                meta.appendChild(el(doc, 'code', '', entry.custom_id || ''));
            });
            meta.appendChild(doc.createTextNode(
                ' — every option routes to the existing shop purchase mechanism.'));
        }
        container.appendChild(meta);
    }

    // ── Warnings ────────────────────────────────────────────────
    function renderWarnings(doc, container, warnings) {
        clear(container);
        if (!warnings || !warnings.length) {
            container.appendChild(el(doc, 'div', 'sp-ok', 'No warnings — this preview is clean.'));
            return;
        }
        warnings.forEach(function (warning) {
            var item = el(doc, 'div', 'sp-warning');
            item.setAttribute('data-code', warning.code || '');
            item.appendChild(el(doc, 'span', 'sp-warning-code', warning.code || 'warning'));
            if (warning.path) {
                item.appendChild(el(doc, 'span', 'sp-warning-path', warning.path));
            }
            item.appendChild(el(doc, 'span', 'sp-warning-message', warning.message || ''));
            container.appendChild(item);
        });
    }

    // ── Resolved tokens ─────────────────────────────────────────
    function renderTokens(doc, container, tokens) {
        clear(container);
        var used = (tokens && tokens.used) || [];
        if (!used.length) {
            container.appendChild(el(doc, 'div', 'sp-muted',
                'This template uses no tokens.'));
            return;
        }
        var table = el(doc, 'table', '');
        var head = el(doc, 'thead', '');
        var headRow = el(doc, 'tr', '');
        ['Token', 'Path', 'Resolved value'].forEach(function (label) {
            headRow.appendChild(el(doc, 'th', '', label));
        });
        head.appendChild(headRow);
        table.appendChild(head);
        var body = el(doc, 'tbody', '');
        used.forEach(function (entry) {
            var row = el(doc, 'tr', entry.resolved ? '' : 'sp-token-unknown');
            row.appendChild(el(doc, 'td', 'sp-token-name', entry.token));
            row.appendChild(el(doc, 'td', 'sp-token-name', entry.path || ''));
            var valueCell = el(doc, 'td', '');
            if (!entry.resolved) {
                valueCell.appendChild(el(doc, 'span', 'sp-token-empty', 'unknown token (left as typed)'));
            } else if (entry.value === '') {
                valueCell.appendChild(el(doc, 'span', 'sp-token-empty', '(empty)'));
            } else {
                valueCell.appendChild(doc.createTextNode(entry.value));
            }
            row.appendChild(valueCell);
            body.appendChild(row);
        });
        table.appendChild(body);
        container.appendChild(table);
    }

    // Shared by Shop Designer's complete-design preview. These are the
    // existing Shop Publisher renderers, not a second purchase/action view.
    NERO.shopPublisherPreview = {
        renderPurchase: renderPurchase,
        renderWarnings: renderWarnings,
        renderTokens: renderTokens,
    };

    // ── Fixed token catalog reference ───────────────────────────
    function renderCatalog(doc, container, tokens) {
        clear(container);
        if (!tokens || !tokens.length) {
            container.appendChild(el(doc, 'div', 'sp-muted', 'Catalog unavailable.'));
            return;
        }
        var table = el(doc, 'table', '');
        var head = el(doc, 'thead', '');
        var headRow = el(doc, 'tr', '');
        ['Token', 'Meaning'].forEach(function (label) {
            headRow.appendChild(el(doc, 'th', '', label));
        });
        head.appendChild(headRow);
        table.appendChild(head);
        var body = el(doc, 'tbody', '');
        tokens.forEach(function (entry) {
            var row = el(doc, 'tr', '');
            row.appendChild(el(doc, 'td', 'sp-token-name', entry.token));
            row.appendChild(el(doc, 'td', '', entry.description || ''));
            body.appendChild(row);
        });
        table.appendChild(body);
        container.appendChild(table);
    }

    // ── The page module ─────────────────────────────────────────
    NERO.definePage('shop-publisher', {
        init: function (root, ctx) {
            var doc = root.ownerDocument || document;
            var templateSelect = root.querySelector('#sp-template');
            var productSelect = root.querySelector('#sp-product');
            var categorySelect = root.querySelector('#sp-category');
            var assignSelect = root.querySelector('#sp-product-category');
            var typeBadge = root.querySelector('#sp-product-type');
            var designSelect = root.querySelector('#sp-design');
            var designName = root.querySelector('#sp-design-name');
            var saveBtn = root.querySelector('#sp-design-save');
            var loadBtn = root.querySelector('#sp-design-load');
            var deleteBtn = root.querySelector('#sp-design-delete');
            var dirtyMark = root.querySelector('#sp-dirty');
            var status = root.querySelector('#sp-status');
            var mount = root.querySelector('#sp-preview-mount');
            var purchase = root.querySelector('#sp-purchase');
            var warningsBox = root.querySelector('#sp-warnings');
            var tokensBox = root.querySelector('#sp-tokens');
            var catalogBox = root.querySelector('#sp-token-catalog');

            // The frozen preview engine owns the mount's children.
            var previewApi = null;
            if (NERO.embed && NERO.embed.preview && mount) {
                previewApi = NERO.embed.preview.create(mount, {
                    now: function () { return Date.now(); },
                    botIdentity: (typeof window !== 'undefined' && window.__BOT_IDENTITY__) || { name: 'Bot', avatar: null },
                    emptyText: 'Select a template and product to preview the published message.',
                });
                ctx.cleanup(function () { previewApi.destroy(); });
            }

            function setStatus(text, isError) {
                if (!status) return;
                status.textContent = text || '';
                status.className = 'sp-status' + (isError ? ' sp-status-error' : '');
            }

            // One in-flight preview at a time: a stale response can never
            // overwrite a newer one (sequence guard), and ctx.fetch aborts
            // whatever is still running when the page goes away.
            var sequence = 0;
            // Slice 2 category state: the product rows (with their additive
            // category_id link) and the Slice 1 category list. Assignment
            // posts to the Slice 1 endpoint only; a stale assignment response
            // can never overwrite a newer one (assignmentSequence).
            var products = [];
            var categories = [];
            var assignmentSequence = 0;
            var lastAssignValue = '';

            // ── Design Draft Persistence state ────────────────────────
            // draftState IS the in-memory Step 0 Design draft
            // ({presentation, products, action}). dirty = actual unsaved
            // DRAFT changes only (template/presentation, roster, action,
            // name) — never network/loading/status transitions. A loaded
            // design previews from its OWN presentation snapshot verbatim;
            // source_template_name stays provenance and is never dereferenced
            // after save. No autosave, no revisions.
            var savedDesigns = [];
            var currentDesignId = null;
            var draftState = null;
            var draftSourceName = '';        // provenance (template name)
            var dirty = false;

            function selectedProductId() {
                return productSelect ? parseInt(productSelect.value, 10) : NaN;
            }

            function updateTypeBadge() {
                if (!typeBadge) return;
                var productId = selectedProductId();
                var product = null;
                (products || []).forEach(function (p) {
                    if (p.id === productId) product = p;
                });
                if (product && product.type) {
                    typeBadge.textContent = product.type;
                    typeBadge.hidden = false;
                } else {
                    typeBadge.textContent = '';
                    typeBadge.hidden = true;
                }
            }

            function syncAssignSelect() {
                if (!assignSelect) return;
                var productId = selectedProductId();
                var value = '';
                (products || []).forEach(function (p) {
                    if (p.id === productId && p.category_id != null) {
                        value = String(p.category_id);
                    }
                });
                assignSelect.value = value;
                lastAssignValue = value;
            }

            function regroupProductSelect() {
                fillProductSelect(doc, productSelect, products, categories,
                    categorySelect ? categorySelect.value : '');
                fillCategorySelect(doc, categorySelect, categories, products);
                syncAssignSelect();
                updateTypeBadge();
            }

            function assignCategory() {
                if (!assignSelect) return;
                var productId = selectedProductId();
                if (!isFinite(productId)) {
                    assignSelect.value = lastAssignValue;
                    setStatus('Select a product before assigning a category.', true);
                    return;
                }
                var next = assignSelect.value;   // '' = unassign
                var previous = lastAssignValue;
                if (next === previous) return;
                var seq = ++assignmentSequence;
                setStatus('Saving category…');
                ctx.fetchJSON(ASSIGN_URL, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        product_id: productId,
                        category_id: next === '' ? null : parseInt(next, 10),
                    }),
                }).then(function (data) {
                    if (ctx.isDestroyed() || seq !== assignmentSequence) return;
                    if (!data || !data.success) {
                        throw new Error((data && data.error) || 'unknown error');
                    }
                    var newLink = next === '' ? null : parseInt(next, 10);
                    (products || []).forEach(function (p) {
                        if (p.id === productId) p.category_id = newLink;
                    });
                    lastAssignValue = next;
                    regroupProductSelect();
                    setStatus('Category updated.');
                }).catch(function (err) {
                    if (ctx.isDestroyed() || seq !== assignmentSequence) return;
                    assignSelect.value = previous;   // revert the selection
                    lastAssignValue = previous;
                    setStatus('Could not assign the category: ' +
                        (err && err.message ? err.message : 'network error'), true);
                });
            }

            // ── Design draft helpers ────────────────────────────────
            function setDirty(next) {
                dirty = !!next;
                if (dirtyMark) dirtyMark.hidden = !dirty;
            }

            function ensureDraft() {
                if (!draftState) {
                    draftState = {
                        presentation: null,
                        products: [],
                        action: { kind: 'buttons', entries: [] },
                    };
                }
                return draftState;
            }

            function fillDesignSelect() {
                if (!designSelect) return;
                var current = designSelect.value;
                clear(designSelect);
                var placeholder = el(doc, 'option', '', 'Select a saved design…');
                placeholder.value = '';
                designSelect.appendChild(placeholder);
                (savedDesigns || []).forEach(function (record) {
                    var option = el(doc, 'option', '', record.name || ('#' + record.id));
                    option.value = String(record.id);
                    designSelect.appendChild(option);
                });
                var found = false;
                for (var i = 0; i < designSelect.children.length; i += 1) {
                    if (designSelect.children[i].value === current) found = true;
                }
                designSelect.value = (current && found) ? current : '';
            }

            function upsertDesignRecord(record) {
                var replaced = false;
                savedDesigns = (savedDesigns || []).map(function (d) {
                    if (d.id === record.id) { replaced = true; return record; }
                    return d;
                });
                if (!replaced) savedDesigns.push(record);
            }

            function saveDesign() {
                var productId = selectedProductId();
                var name = designName ? String(designName.value || '').trim() : '';
                if (!name) {
                    setStatus('Enter a design name before saving.', true);
                    return;
                }
                if (!draftState || !draftState.presentation ||
                    !draftState.products || !draftState.products.length) {
                    setStatus('Select a template and a product before saving.', true);
                    return;
                }
                // Explicit Save only — full overwrite of the current design.
                var body = {
                    name: name,
                    source_template_name: draftSourceName || null,
                    design: draftState,
                };
                if (currentDesignId != null) body.id = currentDesignId;
                setStatus('Saving design…');
                ctx.fetchJSON(DESIGNS_URL, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                }).then(function (data) {
                    if (ctx.isDestroyed()) return;
                    if (!data || !data.success || !data.design) {
                        throw new Error((data && data.error) || 'unknown error');
                    }
                    currentDesignId = data.design.id;
                    upsertDesignRecord(data.design);
                    fillDesignSelect();
                    if (designSelect) designSelect.value = String(currentDesignId);
                    setDirty(false);
                    setStatus('Design saved.');
                }).catch(function (err) {
                    if (ctx.isDestroyed()) return;
                    setStatus('Design save failed: ' +
                        (err && err.message ? err.message : 'network error'), true);
                });
            }

            function loadDesign() {
                var designId = designSelect ? parseInt(designSelect.value, 10) : NaN;
                if (!isFinite(designId)) {
                    setStatus('Select a saved design to load.', true);
                    return;
                }
                if (dirty && typeof window.confirm === 'function' &&
                        !window.confirm('Discard the unsaved changes to the current design and load this saved design?')) {
                    setStatus('Load cancelled. Your unsaved changes are still here.');
                    return;
                }
                var record = null;
                (savedDesigns || []).forEach(function (d) {
                    if (d.id === designId) record = d;
                });
                if (!record || !record.design || !record.design.presentation) {
                    setStatus('Design load failed: the saved draft is unreadable.', true);
                    return;
                }
                // Apply the saved draft verbatim — the presentation is the
                // design's OWN snapshot. No template fetch happens on load;
                // source_template_name is provenance only.
                currentDesignId = record.id;
                draftSourceName = record.source_template_name || '';
                draftState = JSON.parse(JSON.stringify(record.design));
                if (designName) designName.value = record.name || '';
                if (templateSelect) templateSelect.value = draftSourceName;
                var roster = draftState.products || [];
                if (productSelect && roster.length) productSelect.value = String(roster[0]);
                syncAssignSelect();
                updateTypeBadge();
                setDirty(false);
                requestPreview();
            }

            function deleteDesign() {
                var designId = designSelect ? parseInt(designSelect.value, 10) : NaN;
                if (!isFinite(designId)) {
                    setStatus('Select a saved design to delete.', true);
                    return;
                }
                var deletingCurrent = Number(currentDesignId) === designId;
                if (deletingCurrent && dirty && typeof window.confirm === 'function' &&
                        !window.confirm('Delete the active saved design and discard its unsaved changes?')) {
                    setStatus('Delete cancelled. Your unsaved changes are still here.');
                    return;
                }
                // Capture the active draft at request start. If the user edits
                // it while DELETE is in flight, the success callback must not
                // clear that newer in-memory work along with the deleted row.
                var draftSignature = deletingCurrent ? JSON.stringify({
                    id: currentDesignId,
                    draft: draftState,
                    source_template_name: draftSourceName,
                    name: designName ? designName.value : '',
                    template: templateSelect ? templateSelect.value : '',
                    product: productSelect ? productSelect.value : '',
                }) : null;
                setStatus('Deleting design…');
                ctx.fetchJSON(DESIGNS_URL + '/' + designId, {
                    method: 'DELETE',
                }).then(function (data) {
                    if (ctx.isDestroyed()) return;
                    if (!data || !data.success) {
                        throw new Error((data && data.error) || 'unknown error');
                    }
                    savedDesigns = (savedDesigns || []).filter(function (d) {
                        return d.id !== designId;
                    });
                    fillDesignSelect();
                    if (designSelect) designSelect.value = '';
                    // Deleting a different saved design must not discard the
                    // current in-memory draft. Only reset when its own saved
                    // record was deleted.
                    var activeDraftChanged = deletingCurrent && JSON.stringify({
                        id: currentDesignId,
                        draft: draftState,
                        source_template_name: draftSourceName,
                        name: designName ? designName.value : '',
                        template: templateSelect ? templateSelect.value : '',
                        product: productSelect ? productSelect.value : '',
                    }) !== draftSignature;
                    if (deletingCurrent && activeDraftChanged) {
                        // The saved record is gone, but keep edits made after
                        // the request began as a new unsaved draft. Never leave
                        // the deleted Design id attached to it.
                        currentDesignId = null;
                        setDirty(true);
                    } else if (deletingCurrent) {
                        currentDesignId = null;
                        draftState = null;
                        draftSourceName = '';
                        if (designName) designName.value = '';
                        if (templateSelect) templateSelect.value = '';
                        if (productSelect) productSelect.value = '';
                        syncAssignSelect();
                        updateTypeBadge();
                        setDirty(false);
                        resetPreview();
                    }
                    setStatus(deletingCurrent
                        ? (activeDraftChanged
                            ? 'Design deleted. Newer draft edits were kept as an unsaved design.'
                            : 'Design deleted.')
                        : 'Design deleted. Current draft was kept.');
                }).catch(function (err) {
                    if (ctx.isDestroyed()) return;
                    setStatus('Design delete failed: ' +
                        (err && err.message ? err.message : 'network error'), true);
                });
            }

            function renderPreview(preview) {
                if (previewApi) {
                    previewApi.updatePayload({
                        content: preview.content || '',
                        embeds: preview.embeds || [],
                    });
                }
                renderPurchase(doc, purchase, preview.action);
                renderWarnings(doc, warningsBox, preview.warnings || []);
                renderTokens(doc, tokensBox, preview.tokens || {});
            }

            function resetPreview() {
                sequence += 1;
                if (previewApi) previewApi.updatePayload({ content: '', embeds: [] });
                renderPurchase(doc, purchase, null);
                renderWarnings(doc, warningsBox, null);
                renderTokens(doc, tokensBox, null);
                if (warningsBox) {
                    clear(warningsBox);
                    warningsBox.appendChild(el(doc, 'div', 'sp-muted',
                        'Select a template and product to see warnings.'));
                }
                if (tokensBox) {
                    clear(tokensBox);
                    tokensBox.appendChild(el(doc, 'div', 'sp-muted', 'No resolution yet.'));
                }
            }

            function onTemplateChange() {
                // Template/presentation change — a draft-affecting change.
                var template = templateSelect ? templateSelect.value : '';
                var seq = ++sequence;
                if (!template) {
                    setDirty(true);
                    draftSourceName = '';
                    if (draftState) draftState.presentation = null;
                    requestPreview();
                    return;
                }
                // The template document is read from the Embed Builder's
                // existing read route (Step 0 transitional presentation
                // source) INTO the in-memory draft; it may be edited before
                // Save persists it as the design's own snapshot.
                ctx.fetchJSON(TEMPLATE_URL + encodeURIComponent(template)).then(function (tdata) {
                    if (ctx.isDestroyed() || seq !== sequence) return;
                    var tdoc = tdata && tdata.template;
                    if (!tdoc) {
                        setStatus('Preview failed: the template could not be loaded.', true);
                        return;
                    }
                    draftSourceName = template;
                    ensureDraft().presentation = Object.assign({}, tdoc, { mode: 'per_product' });
                    setDirty(true);
                    requestPreview();
                }).catch(function (err) {
                    if (ctx.isDestroyed() || seq !== sequence) return;
                    setStatus('Preview failed: ' + (err && err.message ? err.message : 'network error'), true);
                });
            }

            function onProductChange() {
                syncAssignSelect();
                updateTypeBadge();
                // Roster change — a draft-affecting change. The presentation
                // is untouched (a loaded design keeps its own snapshot).
                setDirty(true);
                var productId = selectedProductId();
                var draft = ensureDraft();
                draft.products = isFinite(productId) ? [productId] : [];
                if (isFinite(productId)) {
                    draft.action = { kind: 'buttons', entries: [{ product_id: productId }] };
                }
                requestPreview();
            }

            function requestPreview() {
                // A PURE preview over the in-memory draft — the same Step 0
                // preview_design() + build_purchase_action() path the publish
                // step must reuse (Preview == Publish identity).
                if (!draftState || !draftState.presentation ||
                    !draftState.products || !draftState.products.length) {
                    resetPreview();
                    setStatus('Select a template and a product to preview.');
                    return;
                }
                var seq = ++sequence;
                setStatus('Resolving preview…');
                ctx.fetchJSON(PREVIEW_URL, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(draftState),
                }).then(function (data) {
                    if (data === null || ctx.isDestroyed() || seq !== sequence) return;
                    if (!data || !data.success || !data.preview) {
                        var reason = (data && data.error) || 'unknown error';
                        if (data && data.problems && data.problems.length) {
                            reason = data.problems[0].message || reason;
                        }
                        setStatus('Preview failed: ' + reason, true);
                        return;
                    }
                    renderPreview(data.preview);
                    var warningCount = (data.preview.warnings || []).length;
                    setStatus('Preview resolved — ' + (draftSourceName || 'draft') +
                        (warningCount ? ' · ' + warningCount + ' warning(s)' : ' · no warnings'));
                }).catch(function (err) {
                    if (ctx.isDestroyed() || seq !== sequence) return;
                    setStatus('Preview failed: ' + (err && err.message ? err.message : 'network error'), true);
                });
            }

            ctx.on(templateSelect, 'change', onTemplateChange);
            ctx.on(productSelect, 'change', onProductChange);
            ctx.on(categorySelect, 'change', function () {
                regroupProductSelect();
                requestPreview();
            });
            ctx.on(assignSelect, 'change', assignCategory);
            // Dirty state: name is persisted design state, so editing it is a
            // draft-affecting change. Network/loading/status transitions never
            // touch dirty. beforeunload guards only while actual unsaved draft
            // changes exist — no navigation framework beyond this.
            ctx.on(designName, 'change', function () { setDirty(true); });
            ctx.on(saveBtn, 'click', saveDesign);
            ctx.on(loadBtn, 'click', loadDesign);
            ctx.on(deleteBtn, 'click', deleteDesign);
            ctx.on(window, 'beforeunload', function (e) {
                if (!dirty) return;
                if (e && e.preventDefault) e.preventDefault();
                if (e) e.returnValue = '';
            });
            // HTMX swaps the dashboard content without firing beforeunload.
            // Guard that in-app navigation path too, and stop the lifecycle
            // listener from unmounting this dirty draft when the user stays.
            ctx.on(doc, 'htmx:beforeSwap', function (event) {
                var target = event && event.detail && event.detail.target;
                if (target && target !== root && !(target.contains && target.contains(root))) return;
                if (!dirty || typeof window.confirm !== 'function' ||
                        window.confirm('You have unsaved Shop Publisher changes. Leave this page and discard them?')) return;
                if (event.preventDefault) event.preventDefault();
                if (event.detail) event.detail.shouldSwap = false;
                if (event.stopImmediatePropagation) event.stopImmediatePropagation();
            }, true);

            Promise.all([
                ctx.fetchJSON(CATALOG_URL),
                ctx.fetchJSON(CATEGORIES_URL),
                ctx.fetchJSON(DESIGNS_URL),
            ]).then(function (results) {
                if (ctx.isDestroyed()) return;
                var data = results[0] || {};
                var catData = results[1] || {};
                var designData = results[2] || {};
                products = data.products || [];
                categories = (catData.categories || []);
                savedDesigns = (designData.designs || []);
                fillTemplateSelect(doc, templateSelect, data.templates || []);
                fillCategorySelect(doc, categorySelect, categories, products);
                fillProductSelect(doc, productSelect, products, categories, '');
                fillAssignSelect(doc, assignSelect, categories);
                fillDesignSelect();
                syncAssignSelect();
                updateTypeBadge();
                renderCatalog(doc, catalogBox, data.tokens || []);
                setStatus('Pick a template and a product to build the preview.');
            }).catch(function (err) {
                if (ctx.isDestroyed()) return;
                renderCatalog(doc, catalogBox, null);
                setStatus('Could not load the picker catalog: ' +
                    (err && err.message ? err.message : 'network error'), true);
            });
        },
        destroy: function () {
            // Everything allocated goes through ctx (listeners, fetches, the
            // preview cleanup registered above) — nav-lifecycle releases them
            // after this hook returns.
        },
    });
})(window.NERO);
