/* ═══════════════════════════════════════════════════════════════
   Shop Publisher — the page module (Phase 1).

   Phase 1 scope is template selection + product selection + Publisher preview
   of the SERVER-resolved token output + preview warnings + the purchase action
   that will actually be published. There is no publish/send here (Phase 2).

   The token resolver is server-side (utils/shop_publisher.py — fixed,
   deterministic, non-programmable) and shared with the Phase 2 publish path.
   This module interprets NOTHING: it renders what
   GET  /api/shop-publisher/catalog
   POST /api/shop-publisher/preview
   return, and the purchase action row renders the API's purchase_action
   descriptor verbatim (custom_id shop_buy_<id> — the existing purchase
   mechanism cogs/shop.py already dispatches).

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
    // Template picker: plain options, template names only (embed_templates).
    function fillTemplateSelect(doc, select, names) {
        var current = select.value;
        clear(select);
        var placeholder = el(doc, 'option', '', 'Select a template…');
        placeholder.value = '';
        select.appendChild(placeholder);
        (names || []).forEach(function (name) {
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
    function fillProductSelect(doc, select, products) {
        var current = select.value;
        clear(select);
        var placeholder = el(doc, 'option', '', 'Select a product…');
        placeholder.value = '';
        select.appendChild(placeholder);
        var groups = {};
        var order = [];
        (products || []).forEach(function (product) {
            var type = product.type || 'other';
            if (!groups[type]) {
                groups[type] = el(doc, 'optgroup', '');
                groups[type].label = type;
                order.push(type);
            }
            var label = product.name || ('#' + product.id);
            if (product.enabled === 0) label += ' (disabled)';
            var option = el(doc, 'option', '', label);
            option.value = String(product.id);
            groups[type].appendChild(option);
        });
        order.forEach(function (type) { select.appendChild(groups[type]); });
        if (current) select.value = current;
    }

    // ── Purchase action (rendered verbatim from the API descriptor) ──
    function renderPurchase(doc, container, action) {
        clear(container);
        if (!action) {
            container.hidden = true;
            return;
        }
        container.hidden = false;

        container.appendChild(el(doc, 'div', 'sp-purchase-label',
            'Purchase action (published with the message)'));

        var row = el(doc, 'div', 'sp-purchase-row');
        var button = el(doc, 'span', 'sp-buy-button');
        if (action.emoji) button.appendChild(el(doc, 'span', 'sp-buy-emoji', action.emoji));
        button.appendChild(el(doc, 'span', 'sp-buy-text', action.label || ''));
        row.appendChild(button);
        container.appendChild(row);

        // The exact contract Phase 2 publishes: custom_id routes to the
        // EXISTING shop purchase mechanism (cogs/shop.py on_interaction).
        var meta = el(doc, 'div', 'sp-purchase-meta');
        meta.appendChild(doc.createTextNode('Published as a '));
        meta.appendChild(el(doc, 'code', '', action.style || 'green'));
        meta.appendChild(doc.createTextNode(' button with '));
        meta.appendChild(el(doc, 'code', '', 'custom_id: ' + (action.custom_id || '')));
        meta.appendChild(doc.createTextNode(
            ' — handled by the existing shop purchase mechanism.'));
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

            function renderPreview(preview) {
                if (previewApi) {
                    previewApi.updatePayload({
                        content: preview.content || '',
                        embeds: preview.embeds || [],
                    });
                }
                renderPurchase(doc, purchase, preview.purchase_action);
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

            function requestPreview() {
                var template = templateSelect && templateSelect.value;
                var productId = productSelect ? parseInt(productSelect.value, 10) : NaN;
                if (!template || !isFinite(productId)) {
                    resetPreview();
                    setStatus('Select a template and a product to preview.');
                    return;
                }
                var seq = ++sequence;
                setStatus('Resolving preview…');
                ctx.fetchJSON(PREVIEW_URL, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ template: template, product_id: productId }),
                }).then(function (data) {
                    if (ctx.isDestroyed() || seq !== sequence) return;
                    if (!data || !data.success || !data.preview) {
                        setStatus('Preview failed: ' + ((data && data.error) || 'unknown error'), true);
                        return;
                    }
                    renderPreview(data.preview);
                    var warningCount = (data.preview.warnings || []).length;
                    setStatus('Preview resolved — ' + (data.product && data.product.name ? data.product.name : 'product') +
                        (warningCount ? ' · ' + warningCount + ' warning(s)' : ' · no warnings'));
                }).catch(function (err) {
                    if (ctx.isDestroyed() || seq !== sequence) return;
                    setStatus('Preview failed: ' + (err && err.message ? err.message : 'network error'), true);
                });
            }

            ctx.on(templateSelect, 'change', requestPreview);
            ctx.on(productSelect, 'change', requestPreview);

            ctx.fetchJSON(CATALOG_URL).then(function (data) {
                if (ctx.isDestroyed()) return;
                data = data || {};
                fillTemplateSelect(doc, templateSelect, data.templates || []);
                fillProductSelect(doc, productSelect, data.products || []);
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
