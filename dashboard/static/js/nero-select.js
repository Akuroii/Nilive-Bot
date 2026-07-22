window.NeroSelect = (function () {
    async function fetchOptions(kind) {
        const url = kind === 'role' ? '/api/guild/roles' : '/api/guild/channels';
        try {
            const res = await fetch(url);
            const data = await res.json();
            return data.results || [];
        } catch (e) {
            console.error('NeroSelect fetch failed', e);
            return [];
        }
    }

    function formatRole(opt) {
        if (!opt.id) return opt.text;
        const color = opt.color || '#99aab5';
        return $(`<span class="ns-role-option">
            <span class="ns-color-dot" style="background:${color}"></span>
            <span class="ns-role-name">${opt.text}</span>
        </span>`);
    }

    function formatChannel(opt) {
        if (!opt.id) return opt.text;
        return $(`<span class="ns-ch-option">
            <span class="ns-ch-icon">${opt.type_icon || '💬'}</span>
            <span class="ns-ch-name">${opt.text}</span>
        </span>`);
    }

    async function initOne(el, kind) {
        if (el.dataset.nsInit === '1') return; // don't double-init
        el.dataset.nsInit = '1';

        const $el = $(el);
        const results = await fetchOptions(kind);
        const preselect = el.dataset.value || '';

        // Build <option> tags so a plain value (no JS) still works
        $el.empty();
        $el.append('<option value=""></option>');
        results.forEach(r => {
            const selected = String(r.id) === String(preselect) ? 'selected' : '';
            $el.append(`<option value="${r.id}" ${selected}>${r.text}</option>`);
        });

        $el.select2({
            theme: 'default',
            width: '100%',
            allowClear: true,
            placeholder: kind === 'role' ? 'Select a role...' : 'Select a channel...',
            templateResult: kind === 'role' ? formatRole : formatChannel,
            templateSelection: kind === 'role' ? formatRole : formatChannel,
        });

        if (preselect) $el.val(preselect).trigger('change.select2');
    }

    function initAll(root) {
        root = root || document;
        root.querySelectorAll('.nero-role-picker').forEach(el => initOne(el, 'role'));
        root.querySelectorAll('.nero-channel-picker').forEach(el => initOne(el, 'channel'));
    }

    return { initAll };
})();

document.addEventListener('DOMContentLoaded', () => window.NeroSelect.initAll(document));
