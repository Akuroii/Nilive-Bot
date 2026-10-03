/* Shop Designer — Region A roster and Region B presentation editor.
   Presentation edits remain in-memory and are isolated from products[]. */
window.NERO = window.NERO || {};

(function (NERO) {
    'use strict';

    var CATALOG_URL = '/api/shop-publisher/catalog';
    var CATEGORIES_URL = '/api/shop-publisher/categories';
    var TEMPLATE_URL = '/api/embedbuilder/template/';
    var PREVIEW_URL = '/api/shop-publisher/preview';
    var DESIGNS_URL = '/api/shop-publisher/designs';

    function make(doc, tag, className, text) {
        var node = doc.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    function clear(node) {
        while (node.firstChild) node.removeChild(node.firstChild);
    }

    function categoryName(category) {
        return (category.emoji ? category.emoji + ' ' : '') +
            (category.name || ('#' + category.id));
    }

    function formatProductMeta(product) {
        var fields = [];
        fields.push('Type: ' + (product.type || 'other'));
        if (product.price_diamonds !== null && product.price_diamonds !== undefined) fields.push('Diamonds: ' + product.price_diamonds);
        if (product.price !== null && product.price !== undefined) fields.push('Coins: ' + product.price);
        fields.push(product.max_stock === null || product.max_stock === undefined ? 'Stock: unlimited' :
            'Stock: ' + (product.current_stock == null ? 0 : product.current_stock) + '/' + product.max_stock);
        if (product.prestige_tier !== null && product.prestige_tier !== undefined) fields.push('Prestige tier: ' + product.prestige_tier);
        fields.push(product.enabled === 0 || product.enabled === false ? 'Disabled' : 'Enabled');
        return fields;
    }

    NERO.definePage('shop-designer', {
        init: function (root, ctx) {
            var doc = root.ownerDocument || document;
            var categorySelect = root.querySelector('#sd-category');
            var available = root.querySelector('#sd-available');
            var rosterList = root.querySelector('#sd-roster');
            var rosterEmpty = root.querySelector('#sd-roster-empty');
            var rosterCount = root.querySelector('#sd-roster-count');
            var catalogStatus = root.querySelector('#sd-catalog-status');
            var statusLine = root.querySelector('#sd-status');
            var templateSelect = root.querySelector('#sd-template');
            var modeSelect = root.querySelector('#sd-mode');
            var railMount = root.querySelector('#sd-embed-rail');
            var inspectorMount = root.querySelector('#sd-embed-inspector');
            var validationLine = root.querySelector('#sd-validation');
            var actionKindSelect = root.querySelector('#sd-action-kind');
            var actionProductSelect = root.querySelector('#sd-action-product');
            var actionEntryLabel = root.querySelector('#sd-action-entry-label');
            var actionAddButton = root.querySelector('#sd-action-add');
            var actionTitle = root.querySelector('#sd-action-title');
            var actionHelp = root.querySelector('#sd-action-help');
            var actionOptionRootRow = root.querySelector('#sd-action-option-root-row');
            var actionOptionRootSelect = root.querySelector('#sd-action-option-root');
            var actionSelectSettings = root.querySelector('#sd-action-select-settings');
            var actionPlaceholderInput = root.querySelector('#sd-action-placeholder');
            var actionEntryList = root.querySelector('#sd-action-entries');
            var actionValidationLine = root.querySelector('#sd-action-validation');
            var previewMount = root.querySelector('#sd-preview-mount');
            var previewPurchase = root.querySelector('#sd-preview-purchase');
            var previewWarnings = root.querySelector('#sd-preview-warnings');
            var previewTokens = root.querySelector('#sd-preview-tokens');
            var previewStatus = root.querySelector('#sd-preview-status');
            var designSelect = root.querySelector('#sd-design-select');
            var designNameInput = root.querySelector('#sd-design-name');
            var designSaveButton = root.querySelector('#sd-design-save');
            var designLoadButton = root.querySelector('#sd-design-load');
            var designDeleteButton = root.querySelector('#sd-design-delete');
            var designDirtyMark = root.querySelector('#sd-dirty');
            var designStatus = root.querySelector('#sd-design-status');
            var designEmpty = root.querySelector('#sd-design-empty');
            var currentDesignLabel = root.querySelector('#sd-current-design');
            var contextInspector = root.querySelector('#sd-context-inspector');

            // Region A's roster remains precisely its existing state object.
            var draft = { products: [] };
            // Region B presentation is a separate sibling. The V2 store below
            // owns only its transient normalized editor document.
            draft.presentation = { mode: 'per_product', content: '', embeds: [] };
            draft.action = { kind: 'buttons', entries: [] };
            var catalogProducts = [];
            var categories = [];
            var categoryFilter = '';
            var destroyed = false;
            var editorStore = null;
            var rail = null;
            var inspector = null;
            var unsubscribeDocument = null;
            var loadSequence = 0;
            var actionValidationSequence = 0;
            var optionFamilySequence = 0;
            var optionRootId = null;
            var optionFamily = [];
            var previewApi = null;
            var shopPreviewViews = NERO.shopPublisherPreview || null;
            var limits = {};
            var savedDesigns = [];
            var currentDesignId = null;
            var draftSourceName = '';
            var dirty = false;
            var baselineSnapshot = null;
            var editRevision = 0;
            var suppressDirty = false;
            var catalogLoaded = false;
            var designListLoaded = false;
            var designListRequestSequence = 0;
            var saveRequestSequence = 0;
            var deleteRequestSequence = 0;
            var applyPresentationDocument = null;
            var selectedInspectorContext = null;
            var selectedProductId = null;

            try { limits = JSON.parse(root.getAttribute('data-limits') || '{}'); } catch (_e) { limits = {}; }
            if (modeSelect && !modeSelect.value) modeSelect.value = 'per_product';

            var model = NERO.embed && NERO.embed.model;
            var storeModule = NERO.embed && NERO.embed.store;
            var validator = NERO.embed && NERO.embed.validate;
            var views = NERO.embed && NERO.embed.views;
            if (NERO.embed && NERO.embed.preview && previewMount) {
                previewApi = NERO.embed.preview.create(previewMount, {
                    now: function () { return Date.now(); },
                    botIdentity: (typeof window !== 'undefined' && window.__BOT_IDENTITY__) || { name: 'Bot', avatar: null },
                    emptyText: 'Configure a complete Shop design to preview the message.',
                });
            }

            function setStatus(message, isError) {
                if (!statusLine) return;
                statusLine.textContent = message || '';
                statusLine.className = isError ? 'sd-status sd-error' : 'sd-status';
            }

            function setDesignStatus(message, isError) {
                if (!designStatus) return;
                designStatus.textContent = message || '';
                designStatus.className = isError ? 'sd-design-status sd-error' : 'sd-design-status';
            }

            function cloneJson(value) {
                return value == null ? value : JSON.parse(JSON.stringify(value));
            }

            function stableStringify(value) {
                if (Array.isArray(value)) return '[' + value.map(stableStringify).join(',') + ']';
                if (value && typeof value === 'object') {
                    return '{' + Object.keys(value).sort().map(function (key) {
                        return JSON.stringify(key) + ':' + stableStringify(value[key]);
                    }).join(',') + '}';
                }
                return JSON.stringify(value);
            }

            function currentDesignConfig() {
                var action = draft.action || { kind: 'buttons', entries: [] };
                var entries = (Array.isArray(action.entries) ? action.entries : []).map(function (entry) {
                    var saved = {};
                    ['product_id', 'label', 'description', 'emoji', 'style'].forEach(function (key) {
                        if (Object.prototype.hasOwnProperty.call(entry, key)) saved[key] = cloneJson(entry[key]);
                    });
                    return saved;
                });
                var savedAction = { kind: action.kind || 'buttons', entries: entries };
                if (Object.prototype.hasOwnProperty.call(action, 'placeholder')) savedAction.placeholder = action.placeholder;
                return {
                    presentation: cloneJson(draft.presentation || { mode: 'per_product', content: '', embeds: [] }),
                    products: Array.isArray(draft.products) ? draft.products.slice() : [],
                    action: savedAction,
                };
            }

            function currentDesignSnapshot() {
                return {
                    name: String(designNameInput && designNameInput.value || '').trim(),
                    source_template_name: draftSourceName || null,
                    design: currentDesignConfig(),
                };
            }

            function hasMeaningfulDraft() {
                var presentation = draft.presentation || {};
                var action = draft.action || {};
                return !!(String(designNameInput && designNameInput.value || '').trim() || draftSourceName ||
                    (draft.products && draft.products.length) ||
                    (presentation.mode && presentation.mode !== 'per_product') || presentation.content ||
                    (presentation.embeds && presentation.embeds.length) ||
                    (action.entries && action.entries.length) || action.placeholder);
            }

            function refreshDirtyState() {
                var next = baselineSnapshot !== null
                    ? stableStringify(currentDesignSnapshot()) !== baselineSnapshot
                    : hasMeaningfulDraft();
                if (suppressDirty) return;
                dirty = !!next;
                if (designDirtyMark) {
                    designDirtyMark.hidden = !dirty;
                    designDirtyMark.textContent = dirty ? 'Unsaved changes' : 'All changes saved';
                }
                if (currentDesignLabel) {
                    currentDesignLabel.textContent = currentDesignId == null
                        ? (dirty ? 'New unsaved design' : 'New design')
                        : 'Editing saved design #' + currentDesignId;
                }
            }

            function setSavedBaseline(record) {
                currentDesignId = record && Number.isInteger(Number(record.id)) ? Number(record.id) : null;
                baselineSnapshot = record ? stableStringify({
                    name: String(record.name || '').trim(),
                    source_template_name: record.source_template_name || null,
                    design: record.design,
                }) : null;
                refreshDirtyState();
            }

            function confirmAction(message) {
                return typeof window.confirm !== 'function' || window.confirm(message);
            }

            function setContextInspector(title, lines) {
                selectedProductId = null;
                applyProductSelection();
                if (!contextInspector) return;
                clear(contextInspector);
                contextInspector.appendChild(make(doc, 'strong', '', title));
                (lines || []).forEach(function (line) {
                    contextInspector.appendChild(make(doc, 'div', 'sd-inspector-meta', line));
                });
            }

            function updateDesignControls() {
                var selected = designSelect && designSelect.value;
                var hasSelected = !!selected && isFinite(parseInt(selected, 10));
                if (designLoadButton) designLoadButton.disabled = !hasSelected || !catalogLoaded || !designListLoaded;
                if (designDeleteButton) designDeleteButton.disabled = !hasSelected || !designListLoaded;
            }

            function renderSavedDesigns() {
                if (!designSelect) return;
                var selected = designSelect.value;
                clear(designSelect);
                var prompt = make(doc, 'option', '', savedDesigns.length ? 'Select a saved design…' : 'No saved designs');
                prompt.value = '';
                designSelect.appendChild(prompt);
                (savedDesigns || []).forEach(function (record) {
                    var option = make(doc, 'option', '', record.name || ('Design #' + record.id));
                    option.value = String(record.id);
                    designSelect.appendChild(option);
                });
                var found = savedDesigns.some(function (record) { return String(record.id) === selected; });
                designSelect.value = found ? selected : '';
                if (designEmpty) {
                    designEmpty.hidden = savedDesigns.length > 0;
                    designEmpty.textContent = savedDesigns.length
                        ? '' : 'No saved designs yet. Configure a design and choose Save design.';
                }
                updateDesignControls();
            }

            function upsertSavedDesign(record) {
                // Invalidate a possibly in-flight initial list response so it
                // cannot overwrite a just-completed Save with stale rows.
                designListRequestSequence += 1;
                designListLoaded = true;
                if (designSelect) designSelect.disabled = false;
                var found = false;
                savedDesigns = (savedDesigns || []).map(function (existing) {
                    if (Number(existing.id) !== Number(record.id)) return existing;
                    found = true;
                    return record;
                });
                if (!found) savedDesigns.push(record);
                savedDesigns.sort(function (a, b) {
                    var byName = String(a.name || '').localeCompare(String(b.name || ''));
                    return byName || Number(a.id) - Number(b.id);
                });
                renderSavedDesigns();
            }

            function loadSavedDesignList() {
                var requestId = ++designListRequestSequence;
                designListLoaded = false;
                if (designSelect) {
                    designSelect.disabled = true;
                    clear(designSelect);
                    var loading = make(doc, 'option', '', 'Loading saved designs…');
                    loading.value = '';
                    designSelect.appendChild(loading);
                }
                if (designEmpty) designEmpty.hidden = true;
                setDesignStatus('Loading saved designs…');
                updateDesignControls();
                ctx.fetchJSON(DESIGNS_URL).then(function (data) {
                    if (destroyed || ctx.isDestroyed() || requestId !== designListRequestSequence) return;
                    if (!data || !Array.isArray(data.designs)) throw new Error((data && data.error) || 'Saved designs could not be loaded.');
                    savedDesigns = data.designs;
                    designListLoaded = true;
                    if (designSelect) designSelect.disabled = false;
                    renderSavedDesigns();
                    setDesignStatus(savedDesigns.length ? savedDesigns.length + ' saved design(s) available.' : 'No saved designs yet.');
                }).catch(function (error) {
                    if (destroyed || ctx.isDestroyed() || requestId !== designListRequestSequence) return;
                    savedDesigns = [];
                    designListLoaded = false;
                    if (designSelect) designSelect.disabled = true;
                    renderSavedDesigns();
                    if (designEmpty) {
                        designEmpty.hidden = false;
                        designEmpty.textContent = 'Saved designs could not be loaded. Retry by refreshing this page.';
                    }
                    setDesignStatus(error && error.message ? error.message : 'Saved designs could not be loaded.', true);
                });
            }

            function clearPreviewOutput() {
                if (previewApi) previewApi.updatePayload({ content: '', embeds: [] });
                if (shopPreviewViews && previewPurchase) shopPreviewViews.renderPurchase(doc, previewPurchase, null);
                if (previewWarnings) {
                    clear(previewWarnings);
                    previewWarnings.appendChild(make(doc, 'div', 'sp-muted', 'No preview yet.'));
                }
                if (previewTokens) {
                    clear(previewTokens);
                    previewTokens.appendChild(make(doc, 'div', 'sp-muted', 'No resolution yet.'));
                }
            }

            function invalidatePreview(message) {
                actionValidationSequence += 1;
                clearPreviewOutput();
                if (previewStatus) previewStatus.textContent = message || 'Draft changed. Validate action to refresh the complete-design preview.';
                if (!suppressDirty) {
                    editRevision += 1;
                    refreshDirtyState();
                }
            }

            function renderCompletePreview(preview) {
                if (previewApi) {
                    previewApi.updatePayload({
                        content: preview.content || '',
                        embeds: preview.embeds || [],
                    });
                }
                if (shopPreviewViews) {
                    shopPreviewViews.renderPurchase(doc, previewPurchase, preview.action);
                    shopPreviewViews.renderWarnings(doc, previewWarnings, preview.warnings || []);
                    shopPreviewViews.renderTokens(doc, previewTokens, preview.tokens || {});
                }
            }

            function productById(id) {
                for (var i = 0; i < catalogProducts.length; i += 1) if (catalogProducts[i].id === id) return catalogProducts[i];
                return null;
            }
            function isRootProduct(product) {
                return !!product && (product.option_of_id === null || product.option_of_id === undefined);
            }
            // Display-only: mirrors the product shown in the context inspector onto
            // the catalog/roster rows. It never touches draft, preview or dirty state.
            function applyProductSelection() {
                [[available, 'data-available-id'], [rosterList, 'data-roster-id']].forEach(function (pair) {
                    var list = pair[0];
                    if (!list || !list.children) return;
                    for (var i = 0; i < list.children.length; i += 1) {
                        var row = list.children[i];
                        if (!row || !row.getAttribute) continue;
                        var raw = row.getAttribute(pair[1]);
                        if (raw === null || raw === undefined) continue;
                        var on = selectedProductId !== null && parseInt(raw, 10) === selectedProductId;
                        if (row.classList) { if (on) row.classList.add('sd-selected'); else row.classList.remove('sd-selected'); }
                        if (on) row.setAttribute('aria-current', 'true');
                        else if (row.removeAttribute) row.removeAttribute('aria-current');
                    }
                });
            }
            function rowIdFromTarget(target, container, attr) {
                var node = target;
                while (node && node !== container) {
                    if (node.getAttribute) {
                        var raw = node.getAttribute(attr);
                        if (raw !== null && raw !== undefined) { var id = parseInt(raw, 10); return isFinite(id) ? id : null; }
                    }
                    node = node.parentNode;
                }
                return null;
            }
            function showProductContext(id) {
                var product = productById(id);
                selectedInspectorContext = { type: 'product', id: id };
                var details = ['Product ID: ' + id];
                if (product) {
                    details.push('Type: ' + (product.type || 'other'));
                    details.push(product.option_of_id == null ? 'Root product' : 'Direct option of product ID ' + product.option_of_id);
                    details.push(product.enabled === 0 || product.enabled === false ? 'Disabled in catalog' : 'Enabled in catalog');
                } else details.push('Product is not present in the current live catalog.');
                setContextInspector(product ? (product.name || ('Product #' + id)) : ('Product #' + id), details);
                selectedProductId = id;
                applyProductSelection();
            }
            function showActionContext(index) {
                var entry = draft.action.entries[index];
                if (!entry) return;
                selectedInspectorContext = { type: 'action', index: index, id: entry.product_id };
                var lines = ['Action kind: ' + draft.action.kind, 'Product ID: ' + entry.product_id];
                if (draft.action.kind === 'option_select') lines.push('Root product ID: ' + (optionRootId == null ? 'not selected' : optionRootId));
                lines.push('Entry position: ' + (index + 1));
                setContextInspector('Purchase action entry', lines);
            }
            function categoryById(id) {
                for (var i = 0; i < categories.length; i += 1) if (categories[i].id === id) return categories[i];
                return null;
            }
            function isRostered(id) { return draft.products.indexOf(id) !== -1; }
            function productCategoryKey(product) {
                if (product.category_id === null || product.category_id === undefined) return 'none';
                return categoryById(product.category_id) ? String(product.category_id) : 'none';
            }

            function renderCategoryFilter() {
                var prior = categoryFilter;
                clear(categorySelect);
                var all = make(doc, 'option', '', 'All categories'); all.value = ''; categorySelect.appendChild(all);
                var counts = {}; var uncategorized = 0;
                catalogProducts.forEach(function (product) {
                    if (!isRootProduct(product)) return;
                    var key = productCategoryKey(product);
                    if (key === 'none') uncategorized += 1; else counts[key] = (counts[key] || 0) + 1;
                });
                categories.forEach(function (category) {
                    var key = String(category.id); var option = make(doc, 'option', '', categoryName(category) + ' (' + (counts[key] || 0) + ')');
                    option.value = key; categorySelect.appendChild(option);
                });
                var none = make(doc, 'option', '', 'Uncategorized (' + uncategorized + ')'); none.value = 'none'; categorySelect.appendChild(none);
                var exists = prior === '' || prior === 'none' || categories.some(function (category) { return String(category.id) === prior; });
                categoryFilter = exists ? prior : ''; categorySelect.value = categoryFilter;
            }

            function renderAvailable() {
                clear(available);
                var visible = catalogProducts.filter(function (product) {
                    return isRootProduct(product) && (!categoryFilter || productCategoryKey(product) === categoryFilter);
                });
                if (!visible.length) { available.appendChild(make(doc, 'li', 'sd-empty', 'No root products in this category.')); return; }
                visible.forEach(function (product) {
                    var item = make(doc, 'li', 'sd-product'); item.setAttribute('data-available-id', product.id); var main = make(doc, 'div', 'sd-product-main');
                    var identity = make(doc, 'div', 'sd-product-identity');
                    identity.appendChild(make(doc, 'div', 'sd-product-name', product.name || ('#' + product.id)));
                    identity.appendChild(make(doc, 'div', 'sd-product-id', 'Product ID ' + product.id));
                    var metadata = make(doc, 'div', 'sd-product-meta');
                    formatProductMeta(product).forEach(function (field) { metadata.appendChild(make(doc, 'span', 'sd-product-meta-item', field)); });
                    var key = productCategoryKey(product); var category = key === 'none' ? null : categoryById(product.category_id);
                    metadata.appendChild(make(doc, 'span', 'sd-category', 'Category: ' + (category ? categoryName(category) : 'Uncategorized')));
                    identity.appendChild(metadata); main.appendChild(identity);
                    var actions = make(doc, 'div', 'sd-product-actions');
                    var add = make(doc, 'button', 'btn btn-secondary', isRostered(product.id) ? 'Added' : 'Add');
                    add.type = 'button'; add.disabled = isRostered(product.id); add.setAttribute('data-add-product', product.id);
                    add.setAttribute('aria-label', (isRostered(product.id) ? 'Already added ' : 'Add ') + (product.name || ('product ' + product.id)));
                    actions.appendChild(add); main.appendChild(actions); item.appendChild(main); available.appendChild(item);
                });
                applyProductSelection();
            }

            function renderRoster() {
                clear(rosterList); rosterEmpty.hidden = draft.products.length > 0;
                rosterCount.textContent = draft.products.length + (draft.products.length === 1 ? ' product' : ' products');
                draft.products.forEach(function (id, index) {
                    var product = productById(id); var item = make(doc, 'li', 'sd-roster-item'); item.setAttribute('data-roster-id', id);
                    var main = make(doc, 'div', 'sd-roster-main'); main.appendChild(make(doc, 'span', 'sd-roster-index', String(index + 1) + '.'));
                    var identity = make(doc, 'div', 'sd-roster-name'); identity.appendChild(make(doc, 'div', 'sd-product-name', product ? (product.name || ('#' + id)) : ('Product #' + id)));
                    identity.appendChild(make(doc, 'div', 'sd-product-id', 'Root product ID ' + id));
                    if (product) { var meta = make(doc, 'div', 'sd-product-meta'); formatProductMeta(product).forEach(function (field) { meta.appendChild(make(doc, 'span', 'sd-product-meta-item', field)); }); identity.appendChild(meta); }
                    main.appendChild(identity); var actions = make(doc, 'div', 'sd-roster-actions');
                    [{ action: 'up', label: '↑', title: 'Move up', disabled: index === 0 }, { action: 'down', label: '↓', title: 'Move down', disabled: index === draft.products.length - 1 }, { action: 'remove', label: 'Remove', title: 'Remove from roster', disabled: false }].forEach(function (control) {
                        var button = make(doc, 'button', 'btn btn-secondary sd-roster-button', control.label); button.type = 'button'; button.disabled = control.disabled;
                        button.title = control.title; button.setAttribute('data-roster-action', control.action); button.setAttribute('data-product-id', id); actions.appendChild(button);
                    });
                    main.appendChild(actions); item.appendChild(main); rosterList.appendChild(item);
                });
                applyProductSelection();
            }
            function renderCatalog() { renderAvailable(); renderRoster(); renderActionEntries(); }

            function actionEntryIndex(id) {
                for (var i = 0; i < draft.action.entries.length; i += 1) {
                    if (draft.action.entries[i].product_id === id) return i;
                }
                return -1;
            }

            function isProductSelect() { return draft.action.kind === 'product_select'; }
            function isOptionSelect() { return draft.action.kind === 'option_select'; }
            function isSelectAction() { return isProductSelect() || isOptionSelect(); }

            function updateActionControls() {
                var productSelectMode = isProductSelect();
                var optionSelectMode = isOptionSelect();
                if (actionKindSelect) actionKindSelect.value = draft.action.kind;
                if (actionTitle) actionTitle.textContent = optionSelectMode ? 'Option Select' : (productSelectMode ? 'Product Select' : 'Purchase buttons');
                if (actionHelp) actionHelp.textContent = optionSelectMode
                    ? 'Choose one rostered root, then configure only its direct options. The root itself is not a choice.'
                    : (productSelectMode
                        ? 'Let a member choose one rostered root product. Choices are select-only configuration; no purchase is executed.'
                        : 'Map rostered products to green buttons. This only configures the in-memory action and never executes a purchase.');
                if (actionAddButton) actionAddButton.textContent = optionSelectMode ? 'Add direct option' : (productSelectMode ? 'Add product choice' : 'Add button');
                if (actionEntryLabel) actionEntryLabel.textContent = optionSelectMode ? 'Direct option of selected root' : (productSelectMode ? 'Rostered root product' : 'Rostered product');
                if (actionOptionRootRow) actionOptionRootRow.hidden = !optionSelectMode;
                if (actionSelectSettings) actionSelectSettings.hidden = !isSelectAction();
                if (actionPlaceholderInput) actionPlaceholderInput.value = draft.action.placeholder || '';
                if (actionEntryList) actionEntryList.setAttribute('aria-label',
                    optionSelectMode ? 'Ordered direct-option Select entries' : (productSelectMode ? 'Ordered Product Select entries' : 'Ordered purchase button entries'));
            }

            function rosterRootIds() {
                return draft.products.filter(function (id) {
                    return isRootProduct(productById(id));
                });
            }

            function renderActionEntries() {
                var productSelectMode = isProductSelect();
                var optionSelectMode = isOptionSelect();
                updateActionControls();

                if (actionOptionRootSelect) {
                    clear(actionOptionRootSelect);
                    var rootPrompt = make(doc, 'option', '', rosterRootIds().length ? 'Choose a rostered root…' : 'Add root products to the roster first');
                    rootPrompt.value = '';
                    actionOptionRootSelect.appendChild(rootPrompt);
                    rosterRootIds().forEach(function (id) {
                        var product = productById(id);
                        var option = make(doc, 'option', '', product ? (product.name || ('#' + id)) : ('Product #' + id));
                        option.value = String(id);
                        actionOptionRootSelect.appendChild(option);
                    });
                    if (optionRootId !== null && rosterRootIds().indexOf(optionRootId) === -1) optionRootId = null;
                    actionOptionRootSelect.value = optionRootId === null ? '' : String(optionRootId);
                }

                var availableIds;
                if (optionSelectMode) {
                    availableIds = optionFamily.filter(function (product) {
                        return product && Number(product.option_of_id) === optionRootId;
                    }).map(function (product) { return product.id; });
                } else {
                    availableIds = draft.products.filter(function (id) {
                        var product = productById(id);
                        // Buttons keep their previous roster mapping. Both Select
                        // kinds expose only explicit roots or direct options.
                        return !productSelectMode || isRootProduct(product);
                    });
                }

                clear(actionProductSelect);
                var promptText = availableIds.length
                    ? (optionSelectMode ? 'Choose a direct option…' : (productSelectMode ? 'Choose a rostered root product…' : 'Choose a rostered product…'))
                    : (optionSelectMode ? (optionRootId === null ? 'Choose a root first' : 'This root has no direct options') : (productSelectMode ? 'Add root products to the roster first' : 'Add products to the roster first'));
                var prompt = make(doc, 'option', '', promptText);
                prompt.value = '';
                actionProductSelect.appendChild(prompt);
                availableIds.forEach(function (id) {
                    if (actionEntryIndex(id) !== -1) return;
                    var product = productById(id);
                    if (optionSelectMode) {
                        for (var j = 0; j < optionFamily.length; j += 1) {
                            if (optionFamily[j].id === id) product = optionFamily[j];
                        }
                    }
                    var option = make(doc, 'option', '', product ? (product.name || ('#' + id)) : ('Product #' + id));
                    option.value = String(id);
                    actionProductSelect.appendChild(option);
                });
                actionProductSelect.disabled = !availableIds.length || draft.action.entries.length >= 25 || actionProductSelect.children.length < 2;

                clear(actionEntryList);
                draft.action.entries.forEach(function (entry, index) {
                    var product = productById(entry.product_id);
                    if (optionSelectMode) {
                        for (var k = 0; k < optionFamily.length; k += 1) {
                            if (optionFamily[k].id === entry.product_id) product = optionFamily[k];
                        }
                    }
                    var item = make(doc, 'li', 'sd-action-entry');
                    item.setAttribute('data-action-entry', String(index));
                    item.setAttribute('data-action-product-id', String(entry.product_id));
                    var heading = make(doc, 'div', 'sd-action-entry-heading');
                    heading.appendChild(make(doc, 'span', 'sd-action-index', String(index + 1) + '.'));
                    heading.appendChild(make(doc, 'strong', 'sd-action-product', product ? (product.name || ('#' + entry.product_id)) : ('Product #' + entry.product_id)));
                    heading.appendChild(make(doc, 'span', 'sd-action-product-id', (optionSelectMode ? 'Direct option ID ' : 'Root product ID ') + entry.product_id));
                    if (!optionSelectMode && !isRostered(entry.product_id)) {
                        var orphan = make(doc, 'span', 'sd-action-orphan', 'Not in roster');
                        orphan.title = 'This product is no longer in the roster. The action entry itself is unchanged.';
                        heading.appendChild(orphan);
                    }
                    item.appendChild(heading);

                    var labelLabel = make(doc, 'label', 'sd-action-field-label', isSelectAction() ? (optionSelectMode ? 'Option label (optional)' : 'Select label (optional)') : 'Button label (optional)');
                    var labelInput = make(doc, 'input', 'form-input sd-action-input');
                    labelInput.type = 'text'; labelInput.value = entry.label || '';
                    labelInput.placeholder = optionSelectMode ? '{{product.name}} — {{product.price}} {{product.currency}}' : (productSelectMode ? '{{product.name}}' : 'Buy {{product.name}}');
                    labelInput.setAttribute('data-action-field', 'label');
                    labelInput.setAttribute('aria-label', (isSelectAction() ? 'Select label for ' : 'Button label for ') + (product ? product.name : ('product ' + entry.product_id)));
                    labelLabel.appendChild(labelInput); item.appendChild(labelLabel);

                    if (isSelectAction()) {
                        var descriptionLabel = make(doc, 'label', 'sd-action-field-label', 'Description (optional)');
                        var descriptionInput = make(doc, 'input', 'form-input sd-action-input');
                        descriptionInput.type = 'text'; descriptionInput.value = entry.description || '';
                        descriptionInput.placeholder = 'Optional select description';
                        descriptionInput.setAttribute('data-action-field', 'description');
                        descriptionInput.setAttribute('aria-label', 'Select description for ' + (product ? product.name : ('product ' + entry.product_id)));
                        descriptionLabel.appendChild(descriptionInput); item.appendChild(descriptionLabel);
                    }

                    var emojiLabel = make(doc, 'label', 'sd-action-field-label', 'Emoji (optional)');
                    var emojiInput = make(doc, 'input', 'form-input sd-action-input');
                    emojiInput.type = 'text'; emojiInput.value = entry.emoji || '';
                    emojiInput.placeholder = 'Emoji';
                    emojiInput.setAttribute('data-action-field', 'emoji');
                    emojiInput.setAttribute('aria-label', (isSelectAction() ? 'Select emoji for ' : 'Button emoji for ') + (product ? product.name : ('product ' + entry.product_id)));
                    emojiLabel.appendChild(emojiInput); item.appendChild(emojiLabel);

                    var controls = make(doc, 'div', 'sd-action-controls');
                    [
                        { action: 'up', label: '↑', title: 'Move action up', disabled: index === 0 },
                        { action: 'down', label: '↓', title: 'Move action down', disabled: index === draft.action.entries.length - 1 },
                        { action: 'remove', label: 'Remove', title: 'Remove action entry', disabled: false },
                    ].forEach(function (control) {
                        var button = make(doc, 'button', 'btn btn-secondary sd-action-button', control.label);
                        button.type = 'button'; button.disabled = control.disabled; button.title = control.title;
                        button.setAttribute('data-action-operation', control.action);
                        button.setAttribute('data-action-index', String(index));
                        controls.appendChild(button);
                    });
                    item.appendChild(controls); actionEntryList.appendChild(item);
                });
            }

            function loadOptionFamily(rootId) {
                var sequence = ++optionFamilySequence;
                optionFamily = [];
                renderActionEntries();
                if (rootId === null) return;
                ctx.fetchJSON('/api/shop-publisher/products/' + rootId + '/options').then(function (data) {
                    if (destroyed || ctx.isDestroyed() || sequence !== optionFamilySequence || optionRootId !== rootId) return;
                    if (!data || !data.success || !data.root || data.root.id !== rootId) throw new Error('The selected root family could not be loaded.');
                    optionFamily = Array.isArray(data.options) ? data.options.filter(function (row) {
                        return row && Number(row.option_of_id) === rootId;
                    }) : [];
                    renderActionEntries();
                    if (actionValidationLine) actionValidationLine.textContent = optionFamily.length
                        ? 'Loaded ' + optionFamily.length + ' direct options. Select at least 2.'
                        : 'This root has no direct options to select.';
                }).catch(function (error) {
                    if (destroyed || ctx.isDestroyed() || sequence !== optionFamilySequence) return;
                    optionFamily = [];
                    renderActionEntries();
                    if (actionValidationLine) actionValidationLine.textContent = error && error.message ? error.message : 'Could not load this root’s options.';
                });
            }

            function validateActionDraft() {
                var sequence = ++actionValidationSequence;
                var productSelectMode = isProductSelect();
                var optionSelectMode = isOptionSelect();
                var selectMode = productSelectMode || optionSelectMode;
                var minEntries = selectMode ? 2 : 1;
                if (optionSelectMode && (optionRootId === null || !isRostered(optionRootId))) {
                    clearPreviewOutput();
                    var rootRequired = 'Choose a rostered root product for Option Select.';
                    if (actionValidationLine) actionValidationLine.textContent = rootRequired;
                    if (previewStatus) previewStatus.textContent = rootRequired;
                    return;
                }
                if (!draft.products.length || draft.action.entries.length < minEntries) {
                    clearPreviewOutput();
                    var incomplete = !draft.products.length
                        ? 'Add root products to Region A and configure an action before previewing.'
                        : (optionSelectMode
                            ? 'Add at least 2 direct options of the selected root before previewing.'
                            : (productSelectMode
                                ? 'Add at least 2 rostered root products as Product Select choices before previewing.'
                                : 'Add at least one rostered product as a button before previewing.'));
                    if (actionValidationLine) actionValidationLine.textContent = incomplete;
                    if (previewStatus) previewStatus.textContent = incomplete;
                    return;
                }
                if (actionValidationLine) actionValidationLine.textContent = 'Resolving complete design through the existing Shop pipeline…';
                if (previewStatus) previewStatus.textContent = 'Loading complete-design preview…';
                clearPreviewOutput();
                var action = {
                    kind: draft.action.kind,
                    entries: draft.action.entries.map(function (entry) {
                        var built = { product_id: entry.product_id, label: entry.label || '', emoji: entry.emoji || '' };
                        if (selectMode) built.description = entry.description || '';
                        return built;
                    }),
                };
                if (selectMode) action.placeholder = draft.action.placeholder || '';
                ctx.fetchJSON(PREVIEW_URL, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        presentation: {
                            mode: draft.presentation.mode,
                            content: draft.presentation.content,
                            embeds: draft.presentation.embeds,
                        },
                        products: draft.products.slice(),
                        action: action,
                    }),
                }).then(function (data) {
                    if (destroyed || ctx.isDestroyed() || sequence !== actionValidationSequence) return;
                    if (!data || !data.success || !data.preview) {
                        var message = data && data.error ? data.error : 'Complete-design preview failed.';
                        if (data && data.problems && data.problems.length) message = data.problems[0].message || message;
                        clearPreviewOutput();
                        if (actionValidationLine) actionValidationLine.textContent = message;
                        if (previewStatus) previewStatus.textContent = message;
                        return;
                    }
                    renderCompletePreview(data.preview);
                    var built = data.preview.action || {};
                    var entries = built.entries || [];
                    var warnings = data.preview.warnings || [];
                    var kindLabel = optionSelectMode ? 'Option Select choice' : (productSelectMode ? 'Product Select choice' : 'green button');
                    if (actionValidationLine) {
                        actionValidationLine.textContent = warnings.length
                            ? 'Built ' + entries.length + ' ' + kindLabel + (entries.length === 1 ? '' : 's') + ' with ' + warnings.length + ' warning(s): ' + (warnings[0].message || 'review the Shop validation warnings.')
                            : 'Valid — the existing builder produced ' + entries.length + ' ' + kindLabel + (entries.length === 1 ? '' : 's') + '.';
                    }
                    if (previewStatus) previewStatus.textContent = 'Preview ready' + (warnings.length ? ' · ' + warnings.length + ' warning(s)' : ' · no warnings');
                }).catch(function (error) {
                    if (destroyed || ctx.isDestroyed() || sequence !== actionValidationSequence) return;
                    clearPreviewOutput();
                    var message = error && error.message ? error.message : 'Complete-design preview request failed.';
                    if (actionValidationLine) actionValidationLine.textContent = message;
                    if (previewStatus) previewStatus.textContent = message;
                });
            }

            function addActionEntry() {
                var rawId = actionProductSelect.value;
                var id = parseInt(rawId, 10);
                if (!rawId || !isFinite(id) || actionEntryIndex(id) !== -1 || draft.action.entries.length >= 25) return;
                var product = productById(id);
                if (isProductSelect() && (!isRostered(id) || !product || product.option_of_id !== null)) return;
                if (isOptionSelect() && (!optionRootId || !isRostered(optionRootId) || !optionFamily.some(function (row) { return row.id === id && Number(row.option_of_id) === optionRootId; }))) return;
                if (!isSelectAction() && !isRostered(id)) return;
                var entry = { product_id: id, label: '', emoji: '' };
                if (isSelectAction()) entry.description = '';
                draft.action.entries.push(entry);
                renderActionEntries();
                showActionContext(draft.action.entries.length - 1);
                invalidatePreview();
                if (actionValidationLine) actionValidationLine.textContent = isOptionSelect()
                    ? 'Direct option added. Add at least 2 options of this root before validating.'
                    : (isProductSelect() ? 'Product choice added. Add at least 2 root choices before validating.' : 'Button added. Validate when the mapping is ready.');
            }

            function syncPresentation() {
                var payload = model.toDiscordPayload(editorStore.getDocument());
                var nextContent = payload.content || '';
                var nextEmbeds = payload.embeds || [];
                var nextMode = modeSelect.value === 'frame' ? 'frame' : 'per_product';
                var changed = draft.presentation.content !== nextContent ||
                    draft.presentation.mode !== nextMode ||
                    JSON.stringify(draft.presentation.embeds) !== JSON.stringify(nextEmbeds);
                // Keep the required keys even when the canonical wire payload
                // omits empty content or filters an empty placeholder embed.
                draft.presentation.content = nextContent;
                draft.presentation.embeds = nextEmbeds;
                draft.presentation.mode = nextMode;
                if (changed) invalidatePreview();
            }

            function validateDocument(document_) {
                if (!validator || !validator.validate) return [];
                return validator.validate(document_, limits);
            }

            if (model && storeModule && views && views.rail && views.inspector) {
                var initial = model.fromApiDocument('', [], {});
                editorStore = storeModule.createStore({ document: model.normalizeDocument(initial), reducers: storeModule.createReducers(model) });
                rail = views.rail.create({ document: doc, store: editorStore, mount: railMount, limits: limits, label: 'Presentation embeds' });
                inspector = views.inspector.create({ document: doc, store: editorStore, mount: inspectorMount, model: model, limits: limits });
                function applyDocument(document_) {
                    editorStore.dispatch({ type: 'document/load', document: model.normalizeDocument(document_), meta: { history: false } });
                    editorStore.dispatch({ type: 'ui/selectNode', nodeId: 'content' });
                    syncPresentation();
                    var issues = validateDocument(editorStore.getDocument());
                    editorStore.dispatch({ type: 'ui/setIssues', issues: issues });
                    if (validationLine) validationLine.textContent = issues.length ? issues.length + ' validation issue(s); review the selected fields.' : 'Presentation is valid under the current Discord limits.';
                }
                applyPresentationDocument = applyDocument;
                unsubscribeDocument = editorStore.subscribe(function (state) { return state.document; }, function (document_) {
                    syncPresentation();
                    var issues = validateDocument(document_);
                    editorStore.dispatch({ type: 'ui/setIssues', issues: issues });
                    if (validationLine) validationLine.textContent = issues.length ? issues.length + ' validation issue(s); review the selected fields.' : 'Presentation is valid under the current Discord limits.';
                });
                applyDocument(initial);
                ctx.on(modeSelect, 'change', syncPresentation);
                ctx.on(templateSelect, 'change', function () {
                    var name = templateSelect.value; var seq = ++loadSequence;
                    if (!name) {
                        draftSourceName = '';
                        editRevision += 1;
                        applyDocument(model.fromApiDocument('', [], {}));
                        draft.presentation.mode = modeSelect.value || 'per_product';
                        refreshDirtyState();
                        setStatus('Blank presentation selected.');
                        return;
                    }
                    ctx.fetchJSON(TEMPLATE_URL + encodeURIComponent(name)).then(function (data) {
                        if (destroyed || ctx.isDestroyed() || seq !== loadSequence) return;
                        var template = data && data.template;
                        if (!template) { setStatus('Template could not be loaded.', true); return; }
                        // The existing template read route returns content/embeds.
                        var source = template.embeds !== undefined || template.content !== undefined ? template : { content: '', embeds: [template] };
                        draftSourceName = name;
                        editRevision += 1;
                        applyDocument(model.normalizeDocument(model.fromApiDocument(source.content, source.embeds, {})));
                        syncPresentation();
                        refreshDirtyState();
                        setStatus('Template loaded into the in-memory presentation.');
                    }).catch(function (error) { if (!destroyed && !ctx.isDestroyed() && seq === loadSequence) setStatus('Template could not be loaded: ' + (error.message || 'network error'), true); });
                });
            } else if (validationLine) {
                validationLine.textContent = 'Embed editor modules did not load.';
            }

            ctx.on(categorySelect, 'change', function () { categoryFilter = categorySelect.value || ''; renderAvailable(); });
            ctx.on(available, 'click', function (event) {
                var button = event.target; if (!button || !button.getAttribute) return;
                var rawId = button.getAttribute('data-add-product');
                if (rawId === null || rawId === undefined) {
                    var availableRowId = rowIdFromTarget(button, available, 'data-available-id');
                    if (availableRowId !== null && productById(availableRowId)) showProductContext(availableRowId);
                    return;
                }
                if (button.disabled) return;
                var id = parseInt(rawId, 10); var product = productById(id);
                if (!isFinite(id) || isRostered(id) || !isRootProduct(product)) return;
                draft.products.push(id); renderCatalog(); showProductContext(id); invalidatePreview(); setStatus('Added ' + (product.name || ('product ' + id)) + ' to the root roster.');
            });
            ctx.on(rosterList, 'click', function (event) {
                var button = event.target; if (!button || !button.getAttribute) return;
                var action = button.getAttribute('data-roster-action'); var id = parseInt(button.getAttribute('data-product-id'), 10); var index = draft.products.indexOf(id);
                if (!action) {
                    var rosterRowId = rowIdFromTarget(button, rosterList, 'data-roster-id');
                    if (rosterRowId !== null) showProductContext(rosterRowId);
                    return;
                }
                if (index < 0) return;
                if (action === 'remove') draft.products.splice(index, 1);
                else if (action === 'up' && index > 0) { var before = draft.products[index - 1]; draft.products[index - 1] = draft.products[index]; draft.products[index] = before; }
                else if (action === 'down' && index < draft.products.length - 1) { var after = draft.products[index + 1]; draft.products[index + 1] = draft.products[index]; draft.products[index] = after; }
                else return;
                renderCatalog(); showProductContext(id); invalidatePreview(); setStatus('Product roster updated.');
            });
            renderActionEntries();
            ctx.on(actionKindSelect, 'change', function () {
                var previousKind = draft.action.kind;
                var nextKind = actionKindSelect.value === 'product_select' || actionKindSelect.value === 'option_select'
                    ? actionKindSelect.value : 'buttons';
                if (previousKind !== nextKind && (previousKind === 'option_select' || nextKind === 'option_select')) {
                    draft.action.entries = [];
                    optionFamilySequence += 1;
                    optionFamily = [];
                }
                draft.action.kind = nextKind;
                if (!isOptionSelect()) optionRootId = null;
                renderActionEntries();
                selectedInspectorContext = { type: 'action-kind', kind: nextKind };
                setContextInspector('Purchase action', ['Action kind: ' + nextKind, 'Edit entries in the center workspace.']);
                invalidatePreview();
                if (actionValidationLine) actionValidationLine.textContent = isOptionSelect()
                    ? 'Choose a rostered root, then select at least 2 of its direct options.'
                    : (isProductSelect() ? 'Product Select requires at least 2 rostered root choices.' : 'Buttons require at least one rostered product.');
            });
            ctx.on(actionOptionRootSelect, 'change', function () {
                var rawRootId = actionOptionRootSelect.value;
                var selectedRoot = rawRootId ? parseInt(rawRootId, 10) : NaN;
                if (rawRootId && (!isFinite(selectedRoot) || !isRostered(selectedRoot) || rosterRootIds().indexOf(selectedRoot) === -1)) return;
                optionRootId = rawRootId ? selectedRoot : null;
                if (optionRootId !== null) showProductContext(optionRootId);
                // Entry ids are family-specific. Changing the anchor clears
                // the old family's options rather than mixing family choices.
                draft.action.entries = [];
                optionFamily = [];
                renderActionEntries();
                invalidatePreview();
                loadOptionFamily(optionRootId);
                if (actionValidationLine && optionRootId === null) actionValidationLine.textContent = 'Choose a rostered root product.';
            });
            ctx.on(actionPlaceholderInput, 'input', function () {
                draft.action.placeholder = actionPlaceholderInput.value;
                invalidatePreview();
                if (actionValidationLine) actionValidationLine.textContent = isOptionSelect()
                    ? 'Option Select placeholder changed. Validate to refresh the preview.'
                    : 'Product Select placeholder changed. Validate to refresh the preview.';
            });
            ctx.on(actionAddButton, 'click', addActionEntry);
            ctx.on(root.querySelector('#sd-action-validate'), 'click', validateActionDraft);
            ctx.on(actionEntryList, 'input', function (event) {
                var input = event.target;
                if (!input || !input.getAttribute) return;
                var field = input.getAttribute('data-action-field');
                if (field !== 'label' && field !== 'description' && field !== 'emoji') return;
                var parent = input.parentNode;
                while (parent && parent !== actionEntryList && !parent.hasAttribute('data-action-entry')) parent = parent.parentNode;
                if (!parent || parent === actionEntryList) return;
                var index = parseInt(parent.getAttribute('data-action-entry'), 10);
                if (!isFinite(index) || !draft.action.entries[index]) return;
                draft.action.entries[index][field] = input.value;
                showActionContext(index);
                invalidatePreview();
                if (actionValidationLine) actionValidationLine.textContent = isOptionSelect()
                    ? 'Option Select configuration changed. Validate to check it with the existing Shop pipeline.'
                    : (isProductSelect()
                        ? 'Product Select configuration changed. Validate to check it with the existing Shop pipeline.'
                        : 'Button configuration changed. Validate to check it with the existing Shop pipeline.');
            });
            ctx.on(actionEntryList, 'click', function (event) {
                var button = event.target;
                if (!button || !button.getAttribute) return;
                var entryNode = button;
                while (entryNode && entryNode !== actionEntryList && !entryNode.hasAttribute('data-action-entry')) entryNode = entryNode.parentNode;
                if (entryNode && entryNode !== actionEntryList) showActionContext(parseInt(entryNode.getAttribute('data-action-entry'), 10));
                var operation = button.getAttribute('data-action-operation');
                var index = parseInt(button.getAttribute('data-action-index'), 10);
                if (!operation || !isFinite(index) || !draft.action.entries[index]) return;
                if (operation === 'remove') draft.action.entries.splice(index, 1);
                else if (operation === 'up' && index > 0) {
                    var before = draft.action.entries[index - 1]; draft.action.entries[index - 1] = draft.action.entries[index]; draft.action.entries[index] = before;
                } else if (operation === 'down' && index < draft.action.entries.length - 1) {
                    var after = draft.action.entries[index + 1]; draft.action.entries[index + 1] = draft.action.entries[index]; draft.action.entries[index] = after;
                } else return;
                renderActionEntries();
                if (draft.action.entries[index]) showActionContext(index);
                else if (draft.action.entries.length) showActionContext(draft.action.entries.length - 1);
                else setContextInspector('Purchase action', ['Action kind: ' + draft.action.kind, 'No action entries configured.']);
                invalidatePreview();
                if (actionValidationLine) actionValidationLine.textContent = isSelectAction()
                    ? 'Select entry order changed. Validate when the mapping is ready.'
                    : 'Button order changed. Validate when the mapping is ready.';
            });

            function inferOptionRoot(action) {
                if (!action || action.kind !== 'option_select' || !action.entries || !action.entries.length) return null;
                var rootId = null;
                for (var i = 0; i < action.entries.length; i += 1) {
                    var option = productById(Number(action.entries[i].product_id));
                    if (!option || option.option_of_id == null) return null;
                    var candidate = Number(option.option_of_id);
                    if (rootId !== null && rootId !== candidate) return null;
                    rootId = candidate;
                }
                return rootId !== null && rosterRootIds().indexOf(rootId) !== -1 ? rootId : null;
            }

            function restoreSavedDesign(record) {
                if (!record || !record.design || !Array.isArray(record.design.products) || !record.design.presentation || !record.design.action) {
                    setDesignStatus('This saved design does not contain a complete supported design snapshot.', true);
                    return false;
                }
                for (var rosterIndex = 0; rosterIndex < record.design.products.length; rosterIndex += 1) {
                    var rosterProduct = productById(Number(record.design.products[rosterIndex]));
                    if (rosterProduct && !isRootProduct(rosterProduct)) {
                        setDesignStatus('This saved design contains an option in products[]. Only root products may be rostered.', true);
                        return false;
                    }
                }
                suppressDirty = true;
                editRevision += 1;
                draftSourceName = record.source_template_name || '';
                draft.products = record.design.products.slice();
                draft.action = cloneJson(record.design.action);
                if (!Array.isArray(draft.action.entries)) draft.action.entries = [];
                if (!draft.action.kind) draft.action.kind = 'buttons';
                draft.presentation = cloneJson(record.design.presentation);
                modeSelect.value = draft.presentation.mode === 'frame' ? 'frame' : 'per_product';
                if (designNameInput) designNameInput.value = record.name || '';
                if (templateSelect) templateSelect.value = draftSourceName;
                optionFamilySequence += 1;
                optionFamily = [];
                optionRootId = inferOptionRoot(draft.action);
                if (applyPresentationDocument && model) {
                    applyPresentationDocument(model.fromApiDocument(
                        draft.presentation.content || '',
                        draft.presentation.embeds || [],
                        {}
                    ));
                }
                renderCatalog();
                setSavedBaseline(record);
                if (designSelect) designSelect.value = String(record.id);
                suppressDirty = false;
                refreshDirtyState();
                clearPreviewOutput();
                invalidatePreview('Loaded saved design. Validate the action to rebuild its preview.');
                if (optionRootId !== null) loadOptionFamily(optionRootId);
                else if (draft.action.kind === 'option_select' && draft.action.entries.length && actionValidationLine) {
                    actionValidationLine.textContent = 'Option Select entries are retained, but their root is not present in the live direct-option relationships.';
                }
                if (draft.products.length) showProductContext(draft.products[0]);
                else if (draft.action.entries.length) showActionContext(0);
                else setContextInspector('Design loaded', ['Configure products and actions in the center workspace.']);
                updateDesignControls();
                return true;
            }

            function resetToNewDesign() {
                suppressDirty = true;
                editRevision += 1;
                draft.products = [];
                draft.presentation = { mode: 'per_product', content: '', embeds: [] };
                draft.action = { kind: 'buttons', entries: [] };
                draftSourceName = '';
                currentDesignId = null;
                baselineSnapshot = null;
                optionRootId = null;
                optionFamily = [];
                optionFamilySequence += 1;
                if (designNameInput) designNameInput.value = '';
                if (templateSelect) templateSelect.value = '';
                if (modeSelect) modeSelect.value = 'per_product';
                if (applyPresentationDocument && model) applyPresentationDocument(model.fromApiDocument('', [], {}));
                renderCatalog();
                suppressDirty = false;
                refreshDirtyState();
                invalidatePreview('New design. Configure products and an action to begin.');
                setContextInspector('New design', ['Configure products and actions in the center workspace.']);
                updateDesignControls();
            }

            function selectedSavedDesign() {
                if (!designSelect) return null;
                var id = parseInt(designSelect.value, 10);
                for (var i = 0; i < savedDesigns.length; i += 1) if (Number(savedDesigns[i].id) === id) return savedDesigns[i];
                return null;
            }

            ctx.on(designSelect, 'change', updateDesignControls);
            ctx.on(designNameInput, 'input', function () {
                editRevision += 1;
                refreshDirtyState();
            });
            ctx.on(designSaveButton, 'click', function () {
                if (!designNameInput || !String(designNameInput.value || '').trim()) {
                    setDesignStatus('Enter a design name before saving.', true);
                    return;
                }
                if (designSaveButton) designSaveButton.disabled = true;
                setDesignStatus(currentDesignId == null ? 'Creating saved design…' : 'Saving full design changes…');
                var revision = editRevision;
                var body = currentDesignSnapshot();
                if (currentDesignId !== null) body.id = currentDesignId;
                var requestSequence = ++saveRequestSequence;
                ctx.fetchJSON(DESIGNS_URL, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body),
                }).then(function (data) {
                    if (destroyed || ctx.isDestroyed() || requestSequence !== saveRequestSequence) return;
                    if (!data || !data.success || !data.design) throw new Error((data && (data.error || data.message)) || 'Design could not be saved.');
                    var record = data.design;
                    upsertSavedDesign(record);
                    if (designSelect) designSelect.value = String(record.id);
                    if (revision === editRevision) {
                        restoreSavedDesign(record);
                        setDesignStatus('Design saved.');
                    } else {
                        setSavedBaseline(record);
                        refreshDirtyState();
                        setDesignStatus('Saved the submitted snapshot; newer edits remain unsaved.');
                    }
                    updateDesignControls();
                }).catch(function (error) {
                    if (destroyed || ctx.isDestroyed() || requestSequence !== saveRequestSequence) return;
                    setDesignStatus(error && error.message ? error.message : 'Design could not be saved.', true);
                }).then(function () {
                    if (!destroyed && !ctx.isDestroyed() && requestSequence === saveRequestSequence && designSaveButton) designSaveButton.disabled = false;
                });
            });
            ctx.on(designLoadButton, 'click', function () {
                var record = selectedSavedDesign();
                if (!record) { setDesignStatus('Choose a saved design to load.', true); return; }
                if (dirty && !confirmAction('Discard the unsaved changes to the current design and load “' + (record.name || 'this design') + '”?')) {
                    setDesignStatus('Load cancelled. Your unsaved changes are still here.');
                    return;
                }
                if (!restoreSavedDesign(record)) return;
                setDesignStatus('Loaded “' + (record.name || 'saved design') + '”.');
            });
            ctx.on(designDeleteButton, 'click', function () {
                var record = selectedSavedDesign();
                if (!record) { setDesignStatus('Choose a saved design to delete.', true); return; }
                var message = 'Delete “' + (record.name || 'this design') + '”? This cannot be undone.';
                if (dirty && Number(record.id) === Number(currentDesignId)) message += ' Its unsaved changes will also be discarded.';
                if (!confirmAction(message)) { setDesignStatus('Delete cancelled.'); return; }
                designDeleteButton.disabled = true;
                setDesignStatus('Deleting saved design…');
                var deletingId = Number(record.id);
                var requestSequence = ++deleteRequestSequence;
                ctx.fetchJSON(DESIGNS_URL + '/' + deletingId, { method: 'DELETE' }).then(function (data) {
                    if (destroyed || ctx.isDestroyed() || requestSequence !== deleteRequestSequence) return;
                    if (!data || !data.success) throw new Error((data && (data.error || data.message)) || 'Design could not be deleted.');
                    designListRequestSequence += 1;
                    savedDesigns = savedDesigns.filter(function (item) { return Number(item.id) !== deletingId; });
                    if (designSelect) designSelect.value = '';
                    renderSavedDesigns();
                    if (Number(currentDesignId) === deletingId) resetToNewDesign();
                    setDesignStatus('Design deleted.');
                }).catch(function (error) {
                    if (destroyed || ctx.isDestroyed() || requestSequence !== deleteRequestSequence) return;
                    setDesignStatus(error && error.message ? error.message : 'Design could not be deleted.', true);
                }).then(function () {
                    if (!destroyed && !ctx.isDestroyed() && requestSequence === deleteRequestSequence) updateDesignControls();
                });
            });
            if (window && typeof window.addEventListener === 'function') ctx.on(window, 'beforeunload', function (event) {
                if (!dirty) return;
                if (event.preventDefault) event.preventDefault();
                event.returnValue = '';
                return '';
            });
            if (doc && typeof doc.addEventListener === 'function') ctx.on(doc, 'htmx:beforeSwap', function (event) {
                var target = event && event.detail && event.detail.target;
                if (target && target !== root && !(target.contains && target.contains(root))) return;
                if (!dirty) return;
                if (confirmAction('You have unsaved Shop design changes. Leave this page and discard them?')) return;
                if (event && event.preventDefault) event.preventDefault();
                if (event && event.detail) event.detail.shouldSwap = false;
                // nav-lifecycle.js also listens for beforeSwap and unmounts
                // synchronously. Run in capture and stop that listener on a
                // declined guard, otherwise the editor state would be torn
                // down even though htmx's swap was cancelled.
                if (event && event.stopImmediatePropagation) event.stopImmediatePropagation();
            }, true);

            loadSavedDesignList();
            Promise.all([ctx.fetchJSON(CATALOG_URL), ctx.fetchJSON(CATEGORIES_URL)]).then(function (responses) {
                if (ctx.isDestroyed()) return;
                var catalog = responses[0] || {}; var categoryData = responses[1] || {};
                catalogProducts = Array.isArray(catalog.products) ? catalog.products : [];
                categories = Array.isArray(categoryData.categories) ? categoryData.categories : [];
                renderCategoryFilter(); renderCatalog();
                catalogLoaded = true;
                updateDesignControls();
                var rootProductCount = catalogProducts.filter(isRootProduct).length;
                if (catalogStatus) catalogStatus.textContent = rootProductCount + ' live root products';
                var names = Array.isArray(catalog.templates) ? catalog.templates : [];
                names.forEach(function (name) { var option = make(doc, 'option', '', typeof name === 'string' ? name : name.name); option.value = typeof name === 'string' ? name : name.name; templateSelect.appendChild(option); });
                setStatus('Catalog loaded. Product details are live and read-only.');
            }).catch(function () {
                if (ctx.isDestroyed()) return;
                catalogProducts = []; categories = []; renderCategoryFilter(); renderCatalog();
                catalogLoaded = true;
                updateDesignControls();
                if (catalogStatus) catalogStatus.textContent = 'Could not load the catalog.';
                setStatus('Could not load products/categories. Refresh and try again.', true);
            });

            this._cleanup = function () {
                destroyed = true; loadSequence += 1; actionValidationSequence += 1; optionFamilySequence += 1;
                if (unsubscribeDocument) unsubscribeDocument();
                if (previewApi) previewApi.destroy();
                if (rail) rail.destroy(); if (inspector) inspector.destroy(); if (editorStore) editorStore.destroy();
            };
        },
        destroy: function () { if (this._cleanup) this._cleanup(); },
    });
})(window.NERO);
