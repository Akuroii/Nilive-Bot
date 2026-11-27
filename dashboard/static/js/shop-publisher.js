/* Shared Shop Designer preview renderers.

   The integrated Shop Designer reuses these safe DOM renderers for purchase
   actions, preview warnings, and resolved token details. Publication
   orchestration remains in shop-publications.js; this file owns no page boot.
   ═══════════════════════════════════════════════════════════════ */
window.NERO = window.NERO || {};

(function (NERO) {
    'use strict';

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

})(window.NERO);
