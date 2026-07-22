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

    // ── Lazy multi-select (Commands → per-command Edit panel) ──────────
    //
    // Deliberately NOT scanned by initAll()/DOMContentLoaded — a page
    // like Commands can render 90+ command rows, and every row's Edit
    // panel carries 4 of these (enabled/disabled roles, enabled/disabled
    // channels). Auto-initializing all of them on load would fire
    // ~350+ /api/guild/roles + /api/guild/channels requests before the
    // user ever opens a single panel. Callers (commands.html) invoke
    // this explicitly the first time a given panel is expanded, so the
    // cost is paid once per command a Dark actually edits, not once per
    // command that merely exists.
    //
    // `preselect` is an array of ID strings (already parsed from the
    // stored JSON column) rather than a single data-value string, since
    // these pickers are multi-select. Same dedupe guard
    // (dataset.nsInit) as initOne so re-opening an already-initialized
    // panel doesn't refetch or re-wrap Select2 around itself.
    async function initMulti(el, kind, preselect) {
        if (!el || el.dataset.nsInit === '1') return;
        el.dataset.nsInit = '1';
        el.setAttribute('multiple', 'multiple');

        const $el = $(el);
        const results = await fetchOptions(kind);
        const preselectStr = (preselect || []).map(String);

        $el.empty();
        results.forEach(r => {
            const selected = preselectStr.includes(String(r.id)) ? 'selected' : '';
            $el.append(`<option value="${r.id}" ${selected}>${r.text}</option>`);
        });

        $el.select2({
            theme: 'default',
            width: '100%',
            closeOnSelect: false,
            placeholder: kind === 'role' ? 'None selected — applies to everyone' : 'None selected — applies to all channels',
            templateResult: kind === 'role' ? formatRole : formatChannel,
            templateSelection: kind === 'role' ? formatRole : formatChannel,
        });

        if (preselectStr.length) $el.val(preselectStr).trigger('change.select2');
    }

    // Returns the current value as an array of ID strings — used when
    // collecting a command edit panel's fields to POST. Safe to call
    // even if the element was never initialized (returns []).
    function getMultiValues(el) {
        if (!el || el.dataset.nsInit !== '1') return [];
        const val = $(el).val();
        return val ? val.map(String) : [];
    }

    return { initAll, initMulti, getMultiValues };
})();

document.addEventListener('DOMContentLoaded', () => window.NeroSelect.initAll(document));
