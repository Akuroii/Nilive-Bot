window.NeroSelect = (function () {
    const _optionsCache = {};

    async function fetchOptions(kind) {
        if (_optionsCache[kind]) {
            return _optionsCache[kind];
        }
        const url = kind === 'role' ? '/api/guild/roles' : '/api/guild/channels';
        try {
            const res = await fetch(url);
            const data = await res.json();
            const results = data.results || [];
            _optionsCache[kind] = results;
            return results;
        } catch (e) {
            console.error('NeroSelect fetch failed', e);
            return [];
        }
    }

    // ── Display helpers ────────────────────────────────────────────────
    //
    // Select2 4.1.0-rc.0 hands ``templateResult`` / ``templateSelection`` a
    // data object built *only* from the <option>: {id, text, disabled,
    // selected, title, element}. Any extra field the API sent (``color``,
    // ``type_icon``) is silently dropped — the ONLY way a per-option value
    // reaches a template is via the option's own ``data-*`` attributes
    // (``opt.element.dataset`` / ``opt.element.getAttribute``). That is why
    // initOne()/initMulti() below stamp ``data-color`` / ``data-type-icon``
    // onto every <option>: without them every role dot was the grey fallback
    // and every channel icon the generic 💬, no matter what the API returned.
    //
    // The name is read with a hard fallback chain (opt.text -> the <option>'s
    // text content -> the id) so it can never be ``undefined`` or empty, and
    // it is inserted with textContent (never string-interpolated into HTML)
    // so a name containing & / < / > renders as text instead of breaking the
    // markup. Both are what produced the "empty block / square" rendering.

    function esc(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    // Resolve a custom per-option value from the select2 data object.
    // Tries, in order: opt.<camel> (select2 may expose it), opt.<snake>
    // (our own data), opt.element.dataset.<camel> (data-attr, camelCase),
    // opt.element.getAttribute('data-<attr>') (the literal attribute).
    function readField(opt, camel, snake, attr) {
        if (opt == null) return null;
        if (opt[camel] != null && String(opt[camel]).trim() !== '') return opt[camel];
        if (snake && opt[snake] != null && String(opt[snake]).trim() !== '') return opt[snake];
        const el = opt.element;
        if (el) {
            const ds = el.dataset || {};
            if (ds[camel] != null && String(ds[camel]).trim() !== '') return ds[camel];
            if (attr && typeof el.getAttribute === 'function') {
                const v = el.getAttribute('data-' + attr);
                if (v != null && String(v).trim() !== '') return v;
            }
        }
        return null;
    }

    // The display name — never undefined, never empty, never the string
    // "undefined". Fallback: option text content, then the raw id.
    function optText(opt) {
        let t = (opt && opt.text != null) ? String(opt.text) : '';
        if (t.trim() === '' && opt && opt.element &&
            typeof opt.element.textContent === 'string') {
            t = opt.element.textContent;
        }
        if ((t == null || t.trim() === '') && opt && opt.id != null) {
            t = String(opt.id);
        }
        return t == null ? '' : String(t);
    }

    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text != null) node.textContent = text;
        return node;
    }

    function formatRole(opt) {
        const text = optText(opt);
        if (!opt || opt.id == null || opt.id === '') return el('span', 'ns-role-name', text);
        const color = readField(opt, 'color', 'color', 'color') || '#99aab5';
        const root = el('span', 'ns-role-option');
        const dot = el('span', 'ns-color-dot');
        dot.style.background = color;
        root.appendChild(dot);
        root.appendChild(el('span', 'ns-role-name', text));
        return $(root);
    }

    function formatChannel(opt) {
        const text = optText(opt);
        if (!opt || opt.id == null || opt.id === '') return el('span', 'ns-ch-name', text);
        const icon = readField(opt, 'typeIcon', 'type_icon', 'type-icon') || '💬';
        const root = el('span', 'ns-ch-option');
        root.appendChild(el('span', 'ns-ch-icon', icon));
        root.appendChild(el('span', 'ns-ch-name', text));
        return $(root);
    }

    // Build the <option> for one API row. The name goes in as escaped text
    // content and the custom display fields are stamped on as data-* attrs
    // (see the note above) so the templates can read them back off
    // opt.element.
    function makeOption(r, selected) {
        const id = r.id != null ? String(r.id) : '';
        const name = r.text != null ? String(r.text)
            : (r.name != null ? String(r.name) : id);
        const color = r.color != null ? String(r.color) : '';
        const icon = (r.type_icon != null ? r.type_icon
                    : (r.typeIcon != null ? r.typeIcon : '')) || '';
        return `<option value="${esc(id)}" ${selected ? 'selected' : ''} ` +
            `data-color="${esc(color)}" data-type-icon="${esc(icon)}">${esc(name)}</option>`;
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
            const selected = String(r.id) === String(preselect);
            $el.append(makeOption(r, selected));
        });

        try {
            $el.select2({
                theme: 'default',
                width: '100%',
                allowClear: true,
                placeholder: kind === 'role' ? 'Select a role...' : 'Select a channel...',
                templateResult: kind === 'role' ? formatRole : formatChannel,
                templateSelection: kind === 'role' ? formatRole : formatChannel,
            });
            if (preselect) $el.val(preselect).trigger('change.select2');
        } catch (e) {
            // select2 itself failed (blocked CDN, version mismatch, etc) —
            // leave it as a plain <select>. Still functional, just unstyled.
            console.error('NeroSelect: select2 init failed, using plain <select>', e);
            if (preselect) $el.val(preselect);
        }
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
    //
    // IMPORTANT: this must never reject. commands.html's
    // loadCommandSettings() awaits several of these via
    // Promise.allSettled and needs to always be able to reveal the panel
    // afterward — the earlier version let a select2 failure propagate as
    // a rejection, which silently killed the whole panel (stuck on
    // "Loading settings…" forever, Save button never reachable, since it
    // lives inside the still-hidden fields container). The select2 call
    // is now wrapped so a broken/missing select2 degrades to a plain
    // native <select multiple> instead of taking the panel down with it —
    // getMultiValues() below reads via jQuery .val(), which works on a
    // plain <select> exactly the same as a select2-enhanced one.
    async function initMulti(el, kind, preselect) {
        if (!el || el.dataset.nsInit === '1') return;
        el.dataset.nsInit = '1';
        el.setAttribute('multiple', 'multiple');

        const $el = $(el);
        const results = await fetchOptions(kind);
        const preselectStr = (preselect || []).map(String);

        $el.empty();
        results.forEach(r => {
            const selected = preselectStr.includes(String(r.id));
            $el.append(makeOption(r, selected));
        });

        try {
            $el.select2({
                theme: 'default',
                width: '100%',
                closeOnSelect: false,
                placeholder: kind === 'role' ? 'Everyone' : 'All channels',
                templateResult: kind === 'role' ? formatRole : formatChannel,
                templateSelection: kind === 'role' ? formatRole : formatChannel,
            });
            if (preselectStr.length) $el.val(preselectStr).trigger('change.select2');
        } catch (e) {
            console.error('NeroSelect: multi select2 init failed, using plain <select multiple>', e);
            if (preselectStr.length) $el.val(preselectStr);
        }
    }

    // Returns the current value as an array of ID strings — used when
    // collecting a command edit panel's fields to POST. Safe to call
    // even if the element was never initialized (returns []).
    function getMultiValues(el) {
        if (!el || el.dataset.nsInit !== '1') return [];
        const val = $(el).val();
        return val ? val.map(String) : [];
    }

    // Was the picker initialised? Commands.html's save path uses this to
    // refuse a save that would post [] for a picker that never finished
    // its setup (select2 CDN failure, jQuery missing, fetch error). The
    // picker may still be partially functional — it falls back to a plain
    // <select multiple> in initMulti — but we want the user to explicitly
    // confirm the values rather than silently overwrite saved settings
    // with an empty list.
    function isReady(el) {
        return !!(el && el.dataset && el.dataset.nsInit === '1');
    }

    return { initAll, initMulti, getMultiValues, isReady };
})();

document.addEventListener('DOMContentLoaded', () => window.NeroSelect.initAll(document));
