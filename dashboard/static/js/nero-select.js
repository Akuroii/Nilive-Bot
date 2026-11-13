window.NeroSelect = (function () {
    const _optionsCache = {};
    const _inflight = {};
    const _ABSENT_CACHE_TTL_MS = 30000;

    async function fetchOptions(kind) {
        const now = Date.now();
        const cached = _optionsCache[kind];
        if (cached && (!cached.expiresAt || cached.expiresAt > now)) {
            return { results: cached.results, ok: true, botStatus: cached.botStatus };
        }
        if (cached) {
            delete _optionsCache[kind];
        }
        if (_inflight[kind]) {
            return _inflight[kind];
        }
        const url = kind === 'role' ? '/api/guild/roles' : '/api/guild/channels';
        _inflight[kind] = (async () => {
            try {
                const res = await fetch(url);
                const data = await res.json();
                if (!res.ok || (data && data.error)) {
                    return { results: [], ok: false, botStatus: 'unknown' };
                }
                const results = Array.isArray(data && data.results) ? data.results : [];
                const botStatus = kind === 'role'
                    ? ((data && data.bot_status) || 'present')
                    : 'present';
                if (botStatus === 'present') {
                    _optionsCache[kind] = { results, expiresAt: 0, botStatus };
                } else if (botStatus === 'absent') {
                    _optionsCache[kind] = {
                        results,
                        expiresAt: Date.now() + _ABSENT_CACHE_TTL_MS,
                        botStatus,
                    };
                }
                return { results, ok: true, botStatus };
            } catch (e) {
                console.error('NeroSelect fetch failed', e);
                return { results: [], ok: false, botStatus: 'unknown' };
            } finally {
                delete _inflight[kind];
            }
        })();
        return _inflight[kind];
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
        const unassignable = readField(opt, 'unassignable', 'unassignable', 'unassignable');
        const reason = readField(opt, 'reason', 'reason', 'reason');
        const root = el('span', 'ns-role-option');
        const dot = el('span', 'ns-color-dot');
        dot.style.background = color;
        root.appendChild(dot);
        root.appendChild(el('span', 'ns-role-name', text));
        if (unassignable === '1' && reason) {
            root.appendChild(el('span', 'ns-role-badge', reason));
        }
        return $(root);
    }

    function formatChannel(opt) {
        const text = optText(opt);
        if (!opt || opt.id == null || opt.id === '') return el('span', 'ns-ch-name', text);
        const iconKey = readField(opt, 'typeIcon', 'type_icon', 'type-icon') || '#';
        const root = el('span', 'ns-ch-option');
        const iconSpan = el('span', 'ns-ch-icon');

        if (iconKey === '#' || iconKey === 'text' || !iconKey) {
            iconSpan.textContent = '#';
            iconSpan.classList.add('ns-ch-icon-hash');
        } else {
            const spriteMap = {
                'speaker': 'nero-i-speaker',
                'voice': 'nero-i-speaker',
                'megaphone': 'nero-i-megaphone',
                'announcement': 'nero-i-megaphone',
                'mic': 'nero-i-mic',
                'stage': 'nero-i-mic',
                'forum': 'nero-i-file',
                'file': 'nero-i-file',
                'thread': 'nero-i-message',
                'message': 'nero-i-message',
            };
            const symbolId = spriteMap[iconKey];
            if (symbolId) {
                iconSpan.innerHTML = `<svg class="ns-ch-svg" viewBox="0 0 24 24" aria-hidden="true"><use href="#${symbolId}"></use></svg>`;
            } else {
                iconSpan.textContent = iconKey;
            }
        }

        root.appendChild(iconSpan);
        root.appendChild(el('span', 'ns-ch-name', text));
        return $(root);
    }

    // Build the <option> for one API row. The name goes in as escaped text
    // content and the custom display fields are stamped on as data-* attrs
    // (see the note above) so the templates can read them back off
    // opt.element.
    function makeOption(r, selected, assignableOnly) {
        const id = r.id != null ? String(r.id) : '';
        const name = r.text != null ? String(r.text)
            : (r.name != null ? String(r.name) : id);
        const color = r.color != null ? String(r.color) : '';
        const icon = (r.type_icon != null ? r.type_icon
                    : (r.typeIcon != null ? r.typeIcon : '')) || '';
        const isUnassignable = (r.assignable === false);
        const reason = r.unassignable_label || '';

        // In role-assignment pickers (assignableOnly), unassignable roles are
        // disabled to prevent new invalid bindings. However, if an unassignable
        // role is already the current stored value (selected), we keep it enabled
        // so existing configurations remain visible and are never wiped out.
        const shouldDisable = (assignableOnly && isUnassignable && !selected);
        const disabledAttr = shouldDisable ? 'disabled="disabled"' : '';
        const unassignableAttr = isUnassignable ? 'data-unassignable="1"' : 'data-unassignable="0"';
        const reasonAttr = reason ? `data-reason="${esc(reason)}"` : '';

        return `<option value="${esc(id)}" ${selected ? 'selected' : ''} ${disabledAttr} ` +
            `data-color="${esc(color)}" data-type-icon="${esc(icon)}" ${unassignableAttr} ${reasonAttr}>${esc(name)}</option>`;
    }

    // Populate <option> and <optgroup> tags into the select element. Channels
    // are organized by their parent category hierarchy if present.
    function populateOptions($el, kind, results, isSelectedFn, assignableOnly) {
        if (kind === 'channel') {
            const groups = new Map();
            results.forEach(r => {
                const cat = (r.category && String(r.category).trim()) || 'Uncategorized';
                if (!groups.has(cat)) groups.set(cat, []);
                groups.get(cat).push(r);
            });

            // If all channels are uncategorized, render a flat list without a superfluous single group
            if (groups.size <= 1 && groups.has('Uncategorized')) {
                results.forEach(r => {
                    const selected = isSelectedFn(r.id);
                    $el.append(makeOption(r, selected, assignableOnly));
                });
            } else {
                for (const [catName, chList] of groups.entries()) {
                    const $grp = $('<optgroup>').attr('label', catName);
                    chList.forEach(r => {
                        const selected = isSelectedFn(r.id);
                        $grp.append(makeOption(r, selected, assignableOnly));
                    });
                    $el.append($grp);
                }
            }
        } else {
            results.forEach(r => {
                const selected = isSelectedFn(r.id);
                $el.append(makeOption(r, selected, assignableOnly));
            });
        }
    }

    function buildNativeMarkup(kind, results, isSelectedFn, assignableOnly, placeholderText) {
        let html = placeholderText != null ? `<option value="">${esc(placeholderText)}</option>` : '';
        const renderNativeOpt = (r) => {
            const selected = isSelectedFn(r.id);
            const base = makeOption(r, selected, assignableOnly);
            if (r.assignable === false && r.unassignable_label) {
                const suffix = ` — ${esc(r.unassignable_label)}`;
                return base.replace(/<\/option>$/, `${suffix}</option>`);
            }
            return base;
        };
        if (kind === 'channel') {
            const groups = new Map();
            results.forEach(r => {
                const cat = (r.category && String(r.category).trim()) || 'Uncategorized';
                if (!groups.has(cat)) groups.set(cat, []);
                groups.get(cat).push(r);
            });
            if (groups.size <= 1 && groups.has('Uncategorized')) {
                results.forEach(r => { html += renderNativeOpt(r); });
            } else {
                for (const [catName, chList] of groups.entries()) {
                    html += `<optgroup label="${esc(catName)}">` + chList.map(renderNativeOpt).join('') + `</optgroup>`;
                }
            }
        } else {
            results.forEach(r => { html += renderNativeOpt(r); });
        }
        return html;
    }

    async function initOne(el, kind) {
        if (!el || (el.dataset.nsInit === '1' && el.dataset.nsDegraded !== '1') || el.dataset.nsFetching === '1') return;
        const wasInit = el.dataset.nsInit === '1';
        el.dataset.nsFetching = '1';
        const { results, ok, botStatus } = await fetchOptions(kind);
        delete el.dataset.nsFetching;
        if (!ok) return;
        el.dataset.nsInit = '1';
        if (kind === 'role' && botStatus === 'unknown') {
            el.dataset.nsDegraded = '1';
        } else {
            delete el.dataset.nsDegraded;
        }

        const liveVal = wasInit
            ? (typeof $ !== 'undefined' ? $(el).val() : el.value)
            : null;
        const preselect = liveVal != null ? String(liveVal) : (el.dataset.value || '');
        const assignableOnly = (el.dataset.assignableOnly === 'true' || el.classList.contains('nero-assignable-role-picker'));
        const placeholder = kind === 'role' ? 'Select a role...' : 'Select a channel...';

        if (typeof $ === 'undefined') {
            el.innerHTML = buildNativeMarkup(kind, results, id => String(id) === String(preselect), assignableOnly, placeholder);
            if (preselect) el.value = String(preselect);
            else if (wasInit) el.value = '';
            return;
        }

        const $el = $(el);
        if (wasInit && $el.hasClass && $el.hasClass('select2-hidden-accessible') && typeof $el.select2 === 'function') {
            try { $el.select2('destroy'); } catch (e) { /* ignore */ }
        }
        // Build <option> tags so a plain value (no JS) still works
        $el.empty();
        $el.append('<option value=""></option>');
        populateOptions($el, kind, results, id => String(id) === String(preselect), assignableOnly);

        try {
            $el.select2({
                theme: 'default',
                width: '100%',
                allowClear: true,
                placeholder,
                templateResult: kind === 'role' ? formatRole : formatChannel,
                templateSelection: kind === 'role' ? formatRole : formatChannel,
            });
            if (preselect) $el.val(preselect).trigger('change.select2');
            else if (wasInit) $el.val('').trigger('change.select2');
        } catch (e) {
            // select2 itself failed (blocked CDN, version mismatch, etc) —
            // populate readable native <select> with unassignable labels intact.
            console.error('NeroSelect: select2 init failed, using plain <select>', e);
            el.innerHTML = buildNativeMarkup(kind, results, id => String(id) === String(preselect), assignableOnly, placeholder);
            if (preselect) el.value = String(preselect);
            else if (wasInit) el.value = '';
        }
    }

    function initAll(root) {
        root = root || document;
        root.querySelectorAll('.nero-role-picker').forEach(el => initOne(el, 'role'));
        root.querySelectorAll('.nero-channel-picker').forEach(el => initOne(el, 'channel'));
    }

    // ── Lazy multi-select (Commands → per-command Edit panel) ──────────
    async function initMulti(el, kind, preselect) {
        if (!el || (el.dataset.nsInit === '1' && el.dataset.nsDegraded !== '1') || el.dataset.nsFetching === '1') return;
        const wasInit = el.dataset.nsInit === '1';
        el.dataset.nsFetching = '1';
        const { results, ok, botStatus } = await fetchOptions(kind);
        delete el.dataset.nsFetching;
        if (!ok) return;
        el.dataset.nsInit = '1';
        if (kind === 'role' && botStatus === 'unknown') {
            el.dataset.nsDegraded = '1';
        } else {
            delete el.dataset.nsDegraded;
        }
        el.setAttribute('multiple', 'multiple');

        const preselectStr = (preselect != null ? preselect : (wasInit ? getMultiValues(el) : [])).map(String);
        const assignableOnly = (el.dataset.assignableOnly === 'true' || el.classList.contains('nero-assignable-role-picker'));

        if (typeof $ === 'undefined') {
            el.innerHTML = buildNativeMarkup(kind, results, id => preselectStr.includes(String(id)), assignableOnly, null);
            return;
        }

        const $el = $(el);
        if (wasInit && $el.hasClass && $el.hasClass('select2-hidden-accessible') && typeof $el.select2 === 'function') {
            try { $el.select2('destroy'); } catch (e) { /* ignore */ }
        }
        $el.empty();
        populateOptions($el, kind, results, id => preselectStr.includes(String(id)), assignableOnly);

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
            el.innerHTML = buildNativeMarkup(kind, results, id => preselectStr.includes(String(id)), assignableOnly, null);
            if (preselectStr.length) $el.val(preselectStr);
        }
    }

    // Returns the current value as an array of ID strings — used when
    // collecting a command edit panel's fields to POST. Safe to call
    // even if the element was never initialized (returns []).
    function getMultiValues(el) {
        if (!el || el.dataset.nsInit !== '1') return [];
        if (typeof $ === 'undefined') {
            return Array.from(el.selectedOptions || []).map(o => String(o.value));
        }
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

    return {
        initAll,
        initMulti,
        initOne,
        getMultiValues,
        isReady,
        _populateOptions: populateOptions,
        _makeOption: makeOption,
    };
})();

document.addEventListener('DOMContentLoaded', () => window.NeroSelect.initAll(document));
