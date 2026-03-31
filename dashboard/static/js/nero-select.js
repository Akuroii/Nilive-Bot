// ═══════════════════════════════════════════════════════════════
// NERO SELECT — nero-select.js
// Smart Select2 pickers for Discord roles and channels.
// Usage in HTML:
//   Role picker:    <select class="nero-role-picker" name="my_role_id" data-value="123456"></select>
//   Channel picker: <select class="nero-channel-picker" name="my_channel_id" data-value="789012"></select>
//   Multi-role:     <select class="nero-role-picker" multiple name="role_ids" data-value="111,222"></select>
// ═══════════════════════════════════════════════════════════════

window.NeroSelect = (function() {

    // Cache so we only fetch once per page load (cleared on HTMX swap)
    let _rolesCache    = null;
    let _channelsCache = null;

    async function fetchRoles() {
        if (_rolesCache) return _rolesCache;
        try {
            const r = await fetch('/api/guild/roles');
            if (!r.ok) return [];
            const d = await r.json();
            _rolesCache = d.results || [];
        } catch(e) { _rolesCache = []; }
        return _rolesCache;
    }

    async function fetchChannels() {
        if (_channelsCache) return _channelsCache;
        try {
            const r = await fetch('/api/guild/channels');
            if (!r.ok) return [];
            const d = await r.json();
            _channelsCache = d.results || [];
        } catch(e) { _channelsCache = []; }
        return _channelsCache;
    }

    // ── Role picker ────────────────────────────────────────────
    function roleTemplate(role) {
        if (role.loading) return $('<span><span class="ns-spinner"></span> Loading roles…</span>');
        if (!role.id || role.id === '') return $('<span style="color:var(--text3)">— None —</span>');

        const colorDot = role.color
            ? `<span class="ns-color-dot" style="background:${role.color}"></span>`
            : `<span class="ns-color-dot" style="background:var(--text3)"></span>`;

        const managed = role.managed
            ? '<span class="ns-badge">BOT</span>'
            : '';

        return $(`<span class="ns-role-option">
            ${colorDot}
            <span class="ns-role-name">${escHtml(role.text)}</span>
            ${managed}
            <span class="ns-id">${role.id}</span>
        </span>`);
    }

    function roleTemplateSelection(role) {
        if (!role.id) return $('<span style="color:var(--text3)">Select role…</span>');
        const color = role.color || 'var(--text3)';
        return $(`<span class="ns-selected-role">
            <span class="ns-color-dot" style="background:${color}"></span>
            <span>${escHtml(role.text)}</span>
        </span>`);
    }

    // ── Channel picker ─────────────────────────────────────────
    function channelTemplate(ch) {
        if (ch.loading) return $('<span><span class="ns-spinner"></span> Loading channels…</span>');
        if (!ch.id || ch.id === '') return $('<span style="color:var(--text3)">— None —</span>');

        const icon = ch.type_icon || '💬';
        const cat  = ch.category
            ? `<span class="ns-id">${escHtml(ch.category)}</span>`
            : '';

        return $(`<span class="ns-ch-option">
            <span class="ns-ch-icon">${icon}</span>
            <span class="ns-ch-name">${escHtml(ch.text)}</span>
            ${cat}
            <span class="ns-id">${ch.id}</span>
        </span>`);
    }

    function channelTemplateSelection(ch) {
        if (!ch.id) return $('<span style="color:var(--text3)">Select channel…</span>');
        const icon = ch.type_icon || '💬';
        return $(`<span class="ns-selected-ch">
            <span>${icon}</span>
            <span>#${escHtml(ch.text)}</span>
        </span>`);
    }

    // ── Build Select2 from pre-fetched data ────────────────────
    function buildRolePicker($el, roles) {
        const isMulti    = $el.prop('multiple');
        const rawValue   = $el.data('value') || $el.val() || '';
        const values     = String(rawValue).split(',').map(v => v.trim()).filter(Boolean);

        // Build option list
        const $empty = $('<option value="">').text('— None —');
        $el.empty();
        if (!isMulti) $el.append($empty);

        roles.forEach(function(r) {
            const $opt = $('<option>')
                .val(r.id)
                .text(r.text)
                .data('color',   r.color)
                .data('managed', r.managed);
            $el.append($opt);
        });

        // Init Select2
        $el.select2({
            theme:              'default',
            width:              '100%',
            allowClear:         !isMulti,
            placeholder:        isMulti ? 'Select roles…' : 'Select role…',
            templateResult:     roleTemplate,
            templateSelection:  roleTemplateSelection,
            dropdownParent:     $el.closest('.modal-overlay').length
                                    ? $el.closest('.modal-overlay')
                                    : $('body'),
        });

        // Set current value(s)
        if (values.length) {
            $el.val(isMulti ? values : values[0]).trigger('change.select2');
        }
    }

    function buildChannelPicker($el, channels) {
        const isMulti  = $el.prop('multiple');
        const rawValue = $el.data('value') || $el.val() || '';
        const values   = String(rawValue).split(',').map(v => v.trim()).filter(Boolean);

        $el.empty();
        if (!isMulti) $el.append($('<option value="">').text('— None —'));

        channels.forEach(function(ch) {
            const $opt = $('<option>')
                .val(ch.id)
                .text(ch.text)
                .data('type_icon', ch.type_icon)
                .data('category',  ch.category)
                .data('type',      ch.type);
            $el.append($opt);
        });

        $el.select2({
            theme:             'default',
            width:             '100%',
            allowClear:        !isMulti,
            placeholder:       isMulti ? 'Select channels…' : 'Select channel…',
            templateResult:    channelTemplate,
            templateSelection: channelTemplateSelection,
            dropdownParent:    $el.closest('.modal-overlay').length
                                   ? $el.closest('.modal-overlay')
                                   : $('body'),
        });

        if (values.length) {
            $el.val(isMulti ? values : values[0]).trigger('change.select2');
        }
    }

    // ── Public: init all pickers inside a container ────────────
    async function initAll(container) {
        container = container || document;
        const $rolePickers    = $(container).find('.nero-role-picker');
        const $channelPickers = $(container).find('.nero-channel-picker');

        if (!$rolePickers.length && !$channelPickers.length) return;

        if ($rolePickers.length) {
            const roles = await fetchRoles();
            $rolePickers.each(function() { buildRolePicker($(this), roles); });
        }

        if ($channelPickers.length) {
            const channels = await fetchChannels();
            $channelPickers.each(function() { buildChannelPicker($(this), channels); });
        }
    }

    // ── Utility ───────────────────────────────────────────────
    function escHtml(str) {
        return String(str || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
    }

    // Clear cache on HTMX swap so fresh data loads for each page
    document.body.addEventListener('htmx:afterSwap', function() {
        _rolesCache    = null;
        _channelsCache = null;
    });

    // Auto-init on DOMContentLoaded
    document.addEventListener('DOMContentLoaded', function() {
        initAll(document);
    });

    return { initAll, fetchRoles, fetchChannels };

})();
