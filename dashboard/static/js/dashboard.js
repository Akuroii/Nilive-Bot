// ═══════════════════════════════════════════════════════════════
// NERO DASHBOARD — dashboard.js  (Phase 2 upgrade + CSRF hardening)
// ═══════════════════════════════════════════════════════════════

// ── CSRF TOKEN INJECTION (Critical security fix, this pass) ────
// api_bp now rejects any non-GET /api/* request that doesn't carry
// a matching X-CSRF-Token header (see dashboard/api.py before_request
// hook). Rather than touch every individual fetch() call across every
// template (dozens of them, high risk of missing one), the two
// request paths the dashboard actually uses — raw fetch() and HTMX —
// are patched centrally, once, here.
//
// window.__CSRF_TOKEN__ is set by base.html from the session-stored
// token (see dashboard/auth.py create_session).
var lastApiReadFailureAt = window._lastApiReadFailureAt || 0;
function reportApiReadFailure(status) {
    // Several widgets may request data at once; one actionable notice is
    // enough, rather than stacking a toast for every failed endpoint.
    if (Date.now() - lastApiReadFailureAt < 6000) return;
    lastApiReadFailureAt = Date.now();
    window._lastApiReadFailureAt = lastApiReadFailureAt;
    const message = status === 'network'
        ? 'Dashboard data could not be reached.'
        : 'A dashboard data request failed.';
    showToast(message, 'error', 12000, {
        label: 'Retry',
        onClick: function() { window.location.reload(); },
    });
}

(function patchFetchForCsrf() {
    const originalFetch = window.fetch.bind(window);
    window.fetch = function(input, init) {
        init = init || {};
        const method = (init.method || 'GET').toUpperCase();
        const requestMethod = (init.method ||
            (typeof Request !== 'undefined' && input instanceof Request ? input.method : 'GET')).toUpperCase();
        let apiRead = false;
        try {
            const rawUrl = typeof input === 'string' ? input : (input && input.url) || String(input);
            const url = new URL(rawUrl, window.location.href);
            apiRead = requestMethod === 'GET' && url.origin === window.location.origin && url.pathname.startsWith('/api/');
        } catch (e) { /* non-URL request: retain the original fetch behavior */ }

        if (method !== 'GET' && method !== 'HEAD') {
            const token = window.__CSRF_TOKEN__ || '';
            if (init.headers instanceof Headers) {
                init.headers.set('X-CSRF-Token', token);
            } else {
                init.headers = Object.assign({}, init.headers, { 'X-CSRF-Token': token });
            }
        }

        const guildId = window.__GUILD_ID__;
        if (guildId) {
            if (init.headers instanceof Headers) {
                init.headers.set('X-Guild-ID', String(guildId));
            } else {
                init.headers = Object.assign({}, init.headers, { 'X-Guild-ID': String(guildId) });
            }
        }

        const request = originalFetch(input, init);
        if (!apiRead) return request;
        return request.then(function(response) {
            if (response.status >= 500) reportApiReadFailure(response.status);
            return response;
        }, function(error) {
            if (!error || error.name !== 'AbortError') reportApiReadFailure('network');
            throw error;
        });
    };
})();

document.addEventListener('htmx:configRequest', function(evt) {
    evt.detail.headers['X-CSRF-Token'] = window.__CSRF_TOKEN__ || '';
    if (window.__GUILD_ID__) {
        evt.detail.headers['X-Guild-ID'] = String(window.__GUILD_ID__);
    }
});

// ── CHECK INDICATOR ───────────────────────────────────────────
// Server-rendered from utils/emoji.py (see base.html / check_icon.py).
// Returned as HTML so callers must use it in innerHTML — it is either an
// <img> pointing at Discord's emoji CDN or a plain unicode check. Constant,
// never user data.
function checkIconHtml() {
    return window.__CHECK_ICON__ || '';
}

// ── CURRENCY (name + icon) ────────────────────────────────────
// window.__CURRENCY__ is written by base.html from the server-side
// resolver (utils/currency.py via dashboard/utils/currency_ctx.py), so
// client-side labels use the SAME configured names/icons as the Jinja
// markup. Economy owns these values; nothing here defaults them — if the
// global is missing, callers get an empty label and must render the amount
// without a currency word rather than inventing one.
function currencyInfo(key) {
    const cur = window.__CURRENCY__ || {};
    const info = cur[key] || {};
    return { name: info.name || '', emoji: info.emoji || '' };
}

// Stored rows carry an internal key, not a display name:
// transaction_ledger.currency, purchase_history.currency_paid and reward
// rows use 'balance' (the primary currency) / 'diamonds' / 'xp'. The map
// below is what makes 'balance' and 'coins' answer the same thing, and what
// lets a NON-currency key ('xp', 'role', 'item') fall through untouched —
// it must never be labelled with a currency name.
const CURRENCY_KEY_MAP = {
    balance: 'coins', coins: 'coins', diamonds: 'diamonds',
};

function currencyInfoByKey(key) {
    const bucket = CURRENCY_KEY_MAP[key];
    return bucket ? currencyInfo(bucket) : { name: '', emoji: '' };
}

// A configured currency icon as HTML.
//
// `<:name:id>` / `<a:name:id>` is Discord *message* markup — dropped into
// innerHTML a browser parses it as the start of an <a> tag and the icon
// silently disappears, so a custom icon becomes the image Discord's public
// emoji CDN serves by ID (animated → .gif, static → .png, so animated
// custom emoji animate here too). A unicode emoji is plain text a browser
// renders natively and passes through escaped.
//
// JS twin of dashboard/utils/currency_ctx.py:icon_html — the two must stay
// in step. Returns HTML: callers already interpolate these labels into
// innerHTML.
function currencyEmojiHtml(emoji) {
    const raw = (emoji || '').trim();
    if (!raw) return '';
    const m = raw.match(/^<(a?):(\w+):(\d+)>$/);
    if (!m) return _escapeHtml(raw);
    const ext = m[1] === 'a' ? 'gif' : 'png';
    return `<img src="https://cdn.discordapp.com/emojis/${m[3]}.${ext}" `
         + `class="nero-currency-icon" alt="" width="18" height="18" `
         + `loading="lazy" onerror="this.remove()">`;
}

// The same icon as PLAIN TEXT, for the few places HTML cannot help with —
// an `<option>` may contain text only, and a browser drops an `<img>`
// inside one, leaving the row with no icon at all. A custom emoji yields
// its NAME (never the raw token); the `_` placeholder name used for an
// ID-only emoji reads as "no icon". Twin of currency_ctx.py:icon_text.
function currencyEmojiText(emoji) {
    const raw = (emoji || '').trim();
    if (!raw) return '';
    const m = raw.match(/^<(a?):(\w+):(\d+)>$/);
    if (!m) return raw;
    return m[2] === '_' ? '' : m[2];
}

// '🌙 Moon' — the inline currency label used in tables and toasts. A
// non-currency key is returned as-is. Returns HTML (the icon may be an
// <img>), so use it where innerHTML is assigned. For an `<option>`, or
// anywhere else that takes text, use currencyLabelText().
function currencyLabel(key) {
    const info = currencyInfoByKey(key);
    return `${currencyEmojiHtml(info.emoji)} ${info.name}`.trim() || key;
}

// Text-only twin of currencyLabel(), for `<option>` elements and any
// other text sink. Safe to assign to textContent.
function currencyLabelText(key) {
    const info = currencyInfoByKey(key);
    return `${currencyEmojiText(info.emoji)} ${info.name}`.trim() || key;
}

// The configured display name for a stored currency key; anything that is
// not a currency key ('xp', 'role'…) keeps its own key as the label.
function currencyNameFor(key) {
    return currencyInfoByKey(key).name || key;
}

// '🌙 1,250 Moon' — amount + label, the format
// utils/currency.currency_amount produces server-side. Pass
// labelFirst=true for 'Moon 1,250' (prose/titles). The icon may be an
// <img>, so the result is HTML — assign it via innerHTML.
function currencyAmount(key, amount, labelFirst) {
    const info = currencyInfoByKey(key);
    const name = info.name || key;
    const shown = Number(amount || 0).toLocaleString();
    return labelFirst
        ? `${name} ${shown}`.trim()
        : `${currencyEmojiHtml(info.emoji)} ${shown} ${name}`.trim();
}

// ── THEME TOGGLE ──────────────────────────────────────────────
function syncThemeToggle() {
    const button = document.getElementById('theme-toggle');
    if (button) {
        button.setAttribute('aria-pressed', String(document.documentElement.getAttribute('data-theme') === 'light'));
    }
}
function toggleTheme() {
    const html = document.documentElement;
    const next = html.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    html.setAttribute('data-theme', next);
    try { localStorage.setItem('nero-theme', next); } catch (e) { /* private browsing */ }
    syncThemeToggle();
}
(function initTheme() {
    try {
        const saved = localStorage.getItem('nero-theme');
        if (saved) document.documentElement.setAttribute('data-theme', saved);
    } catch (e) { /* the default theme remains usable without storage */ }
    syncThemeToggle();
})();

// ── MOBILE SIDEBAR ────────────────────────────────────────────
function applySidebarState(open, options) {
    const sidebar = document.getElementById('sidebar');
    const overlay = document.getElementById('sidebar-overlay');
    const button = document.getElementById('sidebar-toggle');
    const main = document.querySelector('.main-content');
    if (!sidebar) return;

    const mobile = window.matchMedia && window.matchMedia('(max-width: 768px)').matches;
    if (!mobile) {
        sidebar.classList.remove('open');
        sidebar.inert = false;
        sidebar.setAttribute('aria-hidden', 'false');
        if (overlay) {
            overlay.classList.remove('active');
            overlay.inert = true;
            overlay.setAttribute('aria-hidden', 'true');
        }
        if (main) main.inert = false;
        if (button) {
            button.setAttribute('aria-expanded', 'false');
            button.setAttribute('aria-label', 'Open navigation');
        }
        document.body.classList.remove('sidebar-open');
        return;
    }

    sidebar.classList.toggle('open', !!open);
    sidebar.inert = !open;
    sidebar.setAttribute('aria-hidden', String(!open));
    if (overlay) {
        overlay.classList.toggle('active', !!open);
        overlay.inert = !open;
        overlay.setAttribute('aria-hidden', String(!open));
    }
    if (main) main.inert = !!open;
    if (button) {
        button.setAttribute('aria-expanded', String(!!open));
        button.setAttribute('aria-label', open ? 'Close navigation' : 'Open navigation');
    }
    document.body.classList.toggle('sidebar-open', !!open);

    if (open) {
        requestAnimationFrame(function() {
            sidebar.querySelector('.nav-link')?.focus({ preventScroll: true });
        });
    } else if (options && options.focusPage) {
        document.getElementById('page-title')?.focus({ preventScroll: true });
    } else if (!options || options.restoreFocus !== false) {
        button?.focus({ preventScroll: true });
    }
}

function toggleSidebar() {
    const sidebar = document.getElementById('sidebar');
    if (!sidebar) return;
    applySidebarState(!sidebar.classList.contains('open'));
}
function closeSidebar(options) {
    applySidebarState(false, options || {});
}
function syncSidebarViewport() {
    // A viewport change should never leave an invisible drawer or inert content.
    applySidebarState(false, { restoreFocus: false });
}
(function initMobileSidebar() {
    const media = window.matchMedia ? window.matchMedia('(max-width: 768px)') : null;
    if (media) {
        if (media.addEventListener) media.addEventListener('change', syncSidebarViewport);
        else if (media.addListener) media.addListener(syncSidebarViewport);
    }
    document.addEventListener('keydown', function(event) {
        const sidebar = document.getElementById('sidebar');
        if (!sidebar || !sidebar.classList.contains('open')) return;

        if (event.key === 'Escape') {
            event.preventDefault();
            closeSidebar();
            return;
        }
        if (event.key !== 'Tab' || !(window.matchMedia && window.matchMedia('(max-width: 768px)').matches)) return;

        const focusable = Array.from(sidebar.querySelectorAll(
            'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])'
        )).filter(function(element) {
            return !element.hidden && element.getAttribute('aria-hidden') !== 'true' && element.getClientRects().length > 0;
        });
        if (!focusable.length) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        const focusOutsideDrawer = !sidebar.contains(document.activeElement);
        if ((event.shiftKey && (document.activeElement === first || focusOutsideDrawer)) ||
            (!event.shiftKey && (document.activeElement === last || focusOutsideDrawer))) {
            event.preventDefault();
            (event.shiftKey ? last : first).focus();
        }
    });
    syncSidebarViewport();
})();

// ── TOAST SYSTEM (4 types) ────────────────────────────────────
const TOAST_CONFIG = {
    success: { icon: checkIconHtml(), bg: 'var(--success)',  fg: '#000', dur: 3000 },
    error:   { icon: '<svg class="toast-svg" viewBox="0 0 24 24" aria-hidden="true"><use href="#nero-i-alert"></use></svg>', bg: 'var(--danger)',   fg: '#fff', dur: 5000 },
    warning: { icon: '<svg class="toast-svg" viewBox="0 0 24 24" aria-hidden="true"><use href="#nero-i-alert"></use></svg>', bg: 'var(--warning)',  fg: '#000', dur: 4000 },
    info:    { icon: '<svg class="toast-svg" viewBox="0 0 24 24" aria-hidden="true"><use href="#nero-i-info"></use></svg>', bg: 'var(--accent)',   fg: '#fff', dur: 3000 },
    // Legacy aliases
    danger:  { icon: '<svg class="toast-svg" viewBox="0 0 24 24" aria-hidden="true"><use href="#nero-i-alert"></use></svg>', bg: 'var(--danger)',   fg: '#fff', dur: 5000 },
};

function showToast(message, type = 'success', duration, action) {
    const cfg = TOAST_CONFIG[type] || TOAST_CONFIG.success;
    const dur = duration || cfg.dur;

    let container = document.getElementById('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        document.body.appendChild(container);
    }
    container.setAttribute('aria-live', 'polite');
    container.setAttribute('aria-relevant', 'additions text');

    const toast = document.createElement('div');
    toast.className = 'toast-item';
    toast.setAttribute('role', type === 'error' || type === 'danger' ? 'alert' : 'status');
    const icon = document.createElement('span');
    icon.className = 'toast-icon';
    icon.setAttribute('aria-hidden', 'true');
    // Icons are fixed application markup; messages may include server text and
    // must be inserted as text, never parsed as HTML.
    icon.innerHTML = cfg.icon;
    const msg = document.createElement('span');
    msg.className = 'toast-msg';
    msg.textContent = String(message ?? '');
    const close = document.createElement('button');
    close.type = 'button';
    close.className = 'toast-close';
    close.setAttribute('aria-label', 'Dismiss notification');
    close.textContent = '✕';
    close.addEventListener('click', () => toast.remove());
    toast.append(icon, msg, close);
    toast.style.setProperty('--toast-bg', cfg.bg);
    toast.style.setProperty('--toast-fg', cfg.fg);
    if (action && typeof action.onClick === 'function') {
        const actionButton = document.createElement('button');
        actionButton.type = 'button';
        actionButton.className = 'toast-action';
        actionButton.textContent = action.label || 'Retry';
        actionButton.addEventListener('click', action.onClick);
        toast.insertBefore(actionButton, toast.querySelector('.toast-close'));
    }
    container.appendChild(toast);

    // Animate in
    requestAnimationFrame(() => toast.classList.add('show'));

    // Auto-dismiss
    clearTimeout(toast._t);
    toast._t = setTimeout(() => {
        toast.classList.remove('show');
        setTimeout(() => toast.remove(), 300);
    }, dur);
}

// ── LOADING STATE HELPERS ─────────────────────────────────────
function setLoading(btn, isLoading, loadText = 'Saving…') {
    if (!btn) return;
    if (isLoading) {
        btn._origText  = btn.innerHTML;
        btn._origDis   = btn.disabled;
        btn._origAriaBusy = btn.getAttribute('aria-busy');
        btn.disabled   = true;
        btn.setAttribute('aria-busy', 'true');
        btn.innerHTML  = `<span class="btn-spinner" aria-hidden="true"></span> ${loadText}`;
        btn.classList.add('loading');
    } else {
        btn.disabled   = btn._origDis || false;
        if (btn._origAriaBusy === null || btn._origAriaBusy === undefined) btn.removeAttribute('aria-busy');
        else btn.setAttribute('aria-busy', btn._origAriaBusy);
        btn.innerHTML  = btn._origText || btn.innerHTML;
        btn.classList.remove('loading');
    }
}

// ── TRACKED SETTINGS FORM STATE ───────────────────────────────
// Opt-in scopes keep existing forms and handlers intact while providing a
// shared dirty/saving/saved status and a safe navigation warning.
const TRACKED_SCOPE_SELECTOR = '[data-ux-track-dirty]';
const trackedScopeState = new WeakMap();

function trackedScopeSnapshot(scope) {
    if (!scope || !scope.querySelectorAll) return '';
    const fields = Array.from(scope.querySelectorAll('input, select, textarea'))
        .filter(field => !field.closest('[data-ux-ignore-dirty]'))
        .filter(field => !['button', 'submit', 'reset', 'hidden'].includes((field.type || '').toLowerCase()));
    return JSON.stringify(fields.map((field, index) => {
        let value;
        if (field.type === 'checkbox' || field.type === 'radio') {
            value = { checked: field.checked, value: field.value };
        } else if (field.tagName === 'SELECT' && field.multiple) {
            value = Array.from(field.selectedOptions).map(option => option.value);
        } else {
            value = field.value;
        }
        return [field.name || field.id || `field-${index}`, field.type || field.tagName, value];
    }));
}

function trackedScopeStatus(scope, message, stateName) {
    const state = trackedScopeState.get(scope);
    if (state && state.statusTimer) {
        clearTimeout(state.statusTimer);
        state.statusTimer = null;
    }
    const status = scope && scope.querySelector('[data-ux-save-state]');
    if (!status) return;
    status.textContent = message || '';
    if (stateName) status.dataset.state = stateName;
    else delete status.dataset.state;
}

function initTrackedScopes(root) {
    root = root || document;
    const scopes = [];
    if (root.matches && root.matches(TRACKED_SCOPE_SELECTOR)) scopes.push(root);
    if (root.querySelectorAll) scopes.push(...root.querySelectorAll(TRACKED_SCOPE_SELECTOR));
    scopes.forEach(scope => {
        if (trackedScopeState.has(scope)) return;
        trackedScopeState.set(scope, {
            baseline: trackedScopeSnapshot(scope),
            baselineCaptured: false,
            dirty: false,
            statusTimer: null,
        });
        scope.dataset.uxDirty = 'false';
    });
}

function trackedScopeFor(element) {
    return element && element.closest ? element.closest(TRACKED_SCOPE_SELECTOR) : null;
}

function syncTrackedScope(scope) {
    const state = trackedScopeState.get(scope);
    if (!state) return;
    const dirty = trackedScopeSnapshot(scope) !== state.baseline;
    state.dirty = dirty;
    scope.dataset.uxDirty = dirty ? 'true' : 'false';
    const status = scope.querySelector('[data-ux-save-state]');
    if (dirty && (!status || status.dataset.state !== 'saving')) trackedScopeStatus(scope, 'Unsaved changes', 'dirty');
    else if (!dirty && (!status || status.dataset.state !== 'saving')) trackedScopeStatus(scope, '', '');
}

function markTrackedScopeSaved(scope, submittedSnapshot) {
    const state = trackedScopeState.get(scope);
    if (!state) return;
    state.baseline = submittedSnapshot == null ? trackedScopeSnapshot(scope) : submittedSnapshot;
    state.baselineCaptured = true;
    state.dirty = trackedScopeSnapshot(scope) !== state.baseline;
    scope.dataset.uxDirty = state.dirty ? 'true' : 'false';
    if (state.dirty) {
        trackedScopeStatus(scope, 'Unsaved changes', 'dirty');
        return;
    }
    trackedScopeStatus(scope, 'Saved', 'saved');
    state.statusTimer = setTimeout(() => {
        const status = scope.querySelector('[data-ux-save-state]');
        if (!state.dirty && status && status.dataset.state === 'saved') trackedScopeStatus(scope, '', '');
    }, 4000);
}

function dirtyTrackedScopes() {
    return Array.from(document.querySelectorAll(`${TRACKED_SCOPE_SELECTOR}[data-ux-dirty="true"]`));
}

initTrackedScopes(document);
document.addEventListener('focusin', function(event) {
    const scope = trackedScopeFor(event.target);
    const state = scope && trackedScopeState.get(scope);
    if (state && !state.baselineCaptured) {
        // Lazy capture lets settings populated asynchronously settle before a
        // user first edits them; normal server-rendered fields keep their same baseline.
        state.baseline = trackedScopeSnapshot(scope);
        state.baselineCaptured = true;
    }
}, true);
function handleTrackedFieldChange(event) {
    const scope = trackedScopeFor(event.target);
    if (!scope) return;
    const state = trackedScopeState.get(scope);
    if (!state) return;
    if (!event.isTrusted && !state.baselineCaptured) return;
    if (!state.baselineCaptured) {
        state.baseline = trackedScopeSnapshot(scope);
        state.baselineCaptured = true;
    }
    syncTrackedScope(scope);
}
document.addEventListener('input', handleTrackedFieldChange, true);
document.addEventListener('change', handleTrackedFieldChange, true);
document.addEventListener('click', function(event) {
    const scope = trackedScopeFor(event.target);
    if (scope) setTimeout(() => syncTrackedScope(scope), 0);
}, true);
document.addEventListener('htmx:afterSwap', event => initTrackedScopes(event.detail && event.detail.target));
document.addEventListener('htmx:historyRestore', () => initTrackedScopes(document));
document.addEventListener('htmx:beforeRequest', function(event) {
    const trigger = event.detail && event.detail.elt;
    if (!trigger || !trigger.closest('.nav-link') || !dirtyTrackedScopes().length) return;
    if (!window.confirm('You have unsaved settings changes. Leave this page without saving?')) event.preventDefault();
});
if (typeof window !== 'undefined' && typeof window.addEventListener === 'function') {
    window.addEventListener('beforeunload', function(event) {
        if (!dirtyTrackedScopes().length) return;
        event.preventDefault();
        event.returnValue = '';
    });
}

window.NeroFormUX = {
    markClean: function(scope) { markTrackedScopeSaved(scope, trackedScopeSnapshot(scope)); },
    setStatus: trackedScopeStatus,
};

function triggerElementShake(el) {
    if (!el || !el.classList) return;
    el.classList.remove('is-shake');
    void el.offsetWidth;
    el.classList.add('is-shake');
    setTimeout(() => el && el.classList && el.classList.remove('is-shake'), 350);
}

// Helper: AJAX save with standard loading, HTTP error, and toast feedback.
async function ajaxSave(url, payload, btn, successMsg = 'Saved!') {
    const scope = trackedScopeFor(btn);
    const submittedSnapshot = scope ? trackedScopeSnapshot(scope) : null;
    if (scope) trackedScopeStatus(scope, 'Saving…', 'saving');
    if (btn) setLoading(btn, true);
    try {
        const res = await fetch(url, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
        });
        let data = null;
        try { data = await res.json(); } catch (e) { /* non-JSON error pages are handled below */ }
        if (!res.ok) {
            if (res.status === 409 && data && data.code === 'GUILD_CONTEXT_MISMATCH') {
                if (scope) trackedScopeStatus(scope, 'Active server changed in another window — changes preserved', 'error');
                if (btn) triggerElementShake(btn);
                showToast('Active server changed in another window. Your unsaved changes are preserved.', 'error', 15000, {
                    label: 'Switch & Save',
                    onClick: async function() {
                        if (window.__GUILD_ID__) {
                            const f = new FormData();
                            f.append('csrf_token', window.__CSRF_TOKEN__ || '');
                            const sres = await fetch('/select-guild/' + window.__GUILD_ID__, { method: 'POST', body: f });
                            if (sres.ok && btn) btn.click();
                        }
                    }
                });
                return data;
            }
            const message = (data && data.error) || `Save failed (${res.status})`;
            if (scope) trackedScopeStatus(scope, 'Save failed — changes are still here', 'error');
            if (btn) triggerElementShake(btn);
            showToast(message, 'error', 12000, res.status >= 500 && btn ? {
                label: 'Retry', onClick: function() { btn.click(); },
            } : null);
            return data || { success: false, error: message, status: res.status };
        }
        if (!data) {
            if (scope) trackedScopeStatus(scope, 'Save failed — changes are still here', 'error');
            if (btn) triggerElementShake(btn);
            showToast('The server returned an unreadable response.', 'error', 12000, btn ? {
                label: 'Retry', onClick: function() { btn.click(); },
            } : null);
            return { success: false, error: 'Unreadable server response' };
        }
        if (data.success || data.ok) {
            if (scope) markTrackedScopeSaved(scope, submittedSnapshot);
            // `warnings` is advisory: the save happened and the feature works,
            // but something will behave differently than expected (a word two
            // systems both claim, say). Which one actually answers is decided
            // at runtime by utils/message_router.py, never by this dialog — so
            // these are shown as a warning toast, not as a failure.
            const warnings = Array.isArray(data.warnings) ? data.warnings : [];
            if (warnings.length) {
                // Pages that reload after a save need the note parked; the
                // replay picks it up on the next load. Pages that stay put
                // just see the toast — remember() is harmless either way,
                // because replay() only fires once and expires after 30s.
                window.NeroWarnings && window.NeroWarnings.remember(warnings, successMsg);
                window.NeroWarnings.showSaveWarnings(warnings, successMsg);
            } else {
                showToast(successMsg, 'success');
            }
        } else {
            if (scope) trackedScopeStatus(scope, 'Save failed — changes are still here', 'error');
            showToast(data.error || 'Save failed', 'error');
        }
        return data;
    } catch (err) {
        if (scope) trackedScopeStatus(scope, 'Save failed — changes are still here', 'error');
        showToast('Connection error — your changes are still here.', 'error', 12000, btn ? {
            label: 'Retry', onClick: function() { btn.click(); },
        } : null);
        return null;
    } finally {
        if (btn) setLoading(btn, false);
    }
}

// ── SAVE WARNINGS ─────────────────────────────────────────────
// Advisory notes from the API (an alias that overlaps a trigger word, say).
// Several manager pages reload after saving, which kills any toast shown
// first — so they are parked in sessionStorage and replayed on load. The API
// never blocks on these: runtime precedence in the bot decides who answers,
// the dashboard only explains it.
window.NeroWarnings = (function () {
    const KEY = 'nero:save-warnings';

    function remember(warnings, successMsg) {
        if (!Array.isArray(warnings) || !warnings.length) return false;
        try {
            sessionStorage.setItem(KEY, JSON.stringify({
                at: Date.now(), successMsg: successMsg || 'Saved',
                warnings: warnings.slice(0, 6),
            }));
        } catch (e) { /* private mode: the inline toast is enough */ }
        return true;
    }

    function replay() {
        let raw = null;
        try {
            raw = sessionStorage.getItem(KEY);
            if (raw) sessionStorage.removeItem(KEY);
        } catch (e) { return; }
        if (!raw) return;
        let payload;
        try { payload = JSON.parse(raw); } catch (e) { return; }
        // Only replay something fresh — a note from yesterday's save is noise.
        if (!payload || Date.now() - (payload.at || 0) > 30000) return;
        showSaveWarnings(payload.warnings, payload.successMsg);
    }

    function showSaveWarnings(warnings, successMsg) {
        if (!warnings || !warnings.length) return;
        const first = warnings[0].message || String(warnings[0]);
        const extra = warnings.length > 1 ? ` (+${warnings.length - 1} more)` : '';
        showToast(`${successMsg} — ${first}${extra}`, 'warning', 9000);
        document.dispatchEvent(new CustomEvent('nero:save-warnings', {
            detail: { warnings },
        }));
    }

    document.addEventListener('DOMContentLoaded', replay);
    return { remember, replay, showSaveWarnings };
})();

// ── CONFIRM MODAL ─────────────────────────────────────────────
function showConfirm(message, onConfirm) {
    let overlay = document.getElementById('confirm-overlay');
    if (!overlay) {
        overlay = document.createElement('div');
        overlay.id = 'confirm-overlay';
        overlay.className = 'modal-overlay';
        overlay.innerHTML = `
            <div class="modal">
                <div class="modal-title">Are you sure?</div>
                <div class="modal-body" id="confirm-message"></div>
                <div class="modal-actions">
                    <button class="btn btn-secondary" id="confirm-cancel">Cancel</button>
                    <button class="btn btn-danger" id="confirm-ok">Confirm</button>
                </div>
            </div>`;
        document.body.appendChild(overlay);
    }
    document.getElementById('confirm-message').textContent = message;
    overlay.style.display = 'flex';
    document.getElementById('confirm-cancel').onclick = () => { overlay.style.display = 'none'; };
    document.getElementById('confirm-ok').onclick = () => {
        overlay.style.display = 'none';
        onConfirm();
    };
}

// ── EDIT MEMBER MODAL ─────────────────────────────────────────
let _editUid = null;

function openEditModal(uid, xp, coins) {
    _editUid = uid;
    document.getElementById('edit-uid-label').textContent = uid;
    document.getElementById('edit-xp').value    = xp;
    document.getElementById('edit-coins').value  = coins;
    document.getElementById('edit-modal').style.display = 'flex';
}

function closeEditModal() {
    document.getElementById('edit-modal').style.display = 'none';
    _editUid = null;
}

async function saveEditModal() {
    const xp    = parseInt(document.getElementById('edit-xp').value);
    const coins = parseInt(document.getElementById('edit-coins').value);
    const btn   = document.querySelector('#edit-modal .btn-primary');
    const data  = await ajaxSave('/api/edit-member', { user_id: _editUid, xp, coins }, btn, 'Member updated!');
    if (data && data.success) {
        closeEditModal();
        setTimeout(() => location.reload(), 600);
    }
}

// ── COPY TO CLIPBOARD ─────────────────────────────────────────
function copyText(text) {
    navigator.clipboard.writeText(text).then(() => showToast('Copied!', 'info'));
}

// ── COLOR PICKER SYNC ─────────────────────────────────────────
function syncColorInput(pickerId, textId) {
    const picker = document.getElementById(pickerId);
    const text   = document.getElementById(textId);
    if (!picker || !text) return;
    picker.addEventListener('input', () => { text.value = picker.value; });
    text.addEventListener('input',  () => {
        if (/^#[0-9A-Fa-f]{6}$/.test(text.value)) picker.value = text.value;
    });
}

// ── USER IDENTITY CELL (client-rendered pages) ────────────────
// JS twin of dashboard/templates/macros/user_identity.html and
// dashboard/utils/user_identity.py — for pages that build their
// table rows entirely client-side via fetch() + template literals
// (Trade History, Missions) rather than server-rendered/htmx-partial
// HTML. Escapes name/id since a Discord nickname is attacker-
// controlled text being inserted via innerHTML.
function _escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str == null ? '' : String(str);
    return div.innerHTML;
}

function userIdentityHtml(userId, displayName, avatarUrl, compact = false) {
    const rawName = displayName || `User ${userId}`;
    const name    = _escapeHtml(rawName);
    const idStr   = _escapeHtml(userId);
    const initial = _escapeHtml((rawName[0] || '?').toUpperCase());

    let avatarHtml = '';
    if (!compact) {
        if (avatarUrl) {
            const avatarEsc = _escapeHtml(avatarUrl);
            avatarHtml = `<img src="${avatarEsc}" class="user-identity-avatar"
                onerror="this.style.display='none';this.nextElementSibling.style.display='flex';">
                <div class="user-identity-avatar-fallback" style="display:none;">${initial}</div>`;
        } else {
            avatarHtml = `<div class="user-identity-avatar-fallback">${initial}</div>`;
        }
    }
    const compactClass = compact ? ' user-identity-compact' : '';
    return `<div class="user-identity${compactClass}">${avatarHtml}<div class="user-identity-text"><div class="user-identity-name">${name}</div><div class="user-identity-id">${idStr}</div></div></div>`;
}

// ── CLOSE MODAL ON OVERLAY CLICK ─────────────────────────────
document.addEventListener('click', function(e) {
    const co = document.getElementById('confirm-overlay');
    if (e.target === co) co.style.display = 'none';
    const em = document.getElementById('edit-modal');
    if (e.target === em) closeEditModal();
});

// ── HTMX ERROR HANDLING ───────────────────────────────────────
function htmxGetRetryAction(event) {
    const detail = event && event.detail || {};
    const config = detail.requestConfig || {};
    const method = String(config.verb || config.method || '').toLowerCase();
    const trigger = detail.elt;
    return method === 'get' && trigger && trigger.isConnected
        ? { label: 'Retry', onClick: function() { trigger.click(); } }
        : null;
}
document.addEventListener('htmx:afterRequest', function(e) {
    const status = e.detail && e.detail.xhr && e.detail.xhr.status;
    if (status >= 500) {
        showToast('Server error — please try again', 'error', 12000, htmxGetRetryAction(e));
    } else if (status === 403) {
        showToast('Action blocked (permission or session expired) — refresh and try again', 'error');
    }
});
document.addEventListener('htmx:sendError', function(e) {
    showToast('Connection failed — check your network and try again', 'error', 12000, htmxGetRetryAction(e));
});
document.addEventListener('htmx:timeout', function(e) {
    showToast('The request timed out — please try again', 'error', 12000, htmxGetRetryAction(e));
});

// ── HTMX AFTER SWAP: re-init dashboard components on swapped content ──────
//
// WHY THIS LIVES HERE (and not on the page-level script alone)
// ---------------------------------------------------------
// dashboard.js is loaded once in base.html and therefore not re-evaluated
// when htmx swaps in a new page fragment. The page-level inline script in
// commands.html is re-executed by htmx, but the COMPONENTS it depends on
// (NeroAlias.initAll, NeroSelect.initAll) only walk the DOM and bind
// handlers on whatever is currently in the document — they do not run
// again on their own. Without re-running them here, after a sidebar click
// every `[data-alias-input]` host in the freshly swapped Commands page
// stays as an empty <div> with a hidden input and no chip UI, and the
// user cannot type, see existing chips, or click × on them.
//
// The same applies to back/forward navigation (htmx:historyRestore) and
// htmx:load (a fragment that finished loading outside the afterSwap flow,
// e.g. via a Boosted link), so we hook all three. Each component's
// initAll walks the document and short-circuits on elements that are
// already initialised, so calling it repeatedly is safe.
function reInitDashboardComponents() {
    if (window.NeroAlias && window.NeroAlias.initAll) {
        window.NeroAlias.initAll(document);
    }
    if (window.NeroSelect && window.NeroSelect.initAll) {
        // Single-select pickers on every page (overview, members, etc.).
        // The Commands page's per-command multi-select pickers are still
        // initialised lazily inside loadCommandSettings() — that hasn't
        // changed.
        window.NeroSelect.initAll(document);
    }
}

document.addEventListener('htmx:afterSwap',  reInitDashboardComponents);
document.addEventListener('htmx:load',        reInitDashboardComponents);
document.addEventListener('htmx:historyRestore', reInitDashboardComponents);

function syncDashboardPageTitle() {
    const content = document.getElementById('content-area');
    const pageTitle = content?.querySelector('[data-page-title]') || content;
    if (!pageTitle) return;
    const title = pageTitle.getAttribute('data-page-title');
    const titleEl = document.getElementById('page-title');
    if (title && titleEl) titleEl.textContent = title;
}
document.addEventListener('htmx:afterSwap', syncDashboardPageTitle);
document.addEventListener('htmx:historyRestore', syncDashboardPageTitle);

// ── GLOBAL COMMAND PALETTE (CTRL+K / CMD+K) ───────────────────
const NERO_PALETTE_ITEMS = [
    // Core Navigation
    { id: 'p-overview', title: 'Server Overview', desc: 'Summary of community, KPIs, and quick actions', url: '/', icon: 'nero-i-home', group: 'Navigation', keywords: 'home dashboard main' },
    { id: 'p-commands', title: 'Command Manager', desc: 'Browse, search, toggle and restrict server commands', url: '/commands', icon: 'nero-i-terminal', group: 'Navigation', keywords: 'commands toggles restrictions aliases cooldown' },
    { id: 'p-members', title: 'Member Manager', desc: 'Search and inspect members, levels, XP, and balances', url: '/members', icon: 'nero-i-users', group: 'Navigation', keywords: 'users members exp coins' },
    { id: 'p-moderation', title: 'Moderation Logs', desc: 'Review punishment history, warnings, and auto-escalation', url: '/moderation', icon: 'nero-i-shield', group: 'Navigation', keywords: 'mod ban kick mute warn' },
    { id: 'p-tickets', title: 'Support Tickets', desc: 'Manage support queues, categories, and transcripts', url: '/tickets', icon: 'nero-i-ticket', group: 'Navigation', keywords: 'tickets help support queue' },
    { id: 'p-embed-builder', title: 'Embed Builder', desc: 'Create rich embeds, announcements, and webhook cards', url: '/embed-builder', icon: 'nero-i-message', group: 'Navigation', keywords: 'embeds builder discord json' },
    { id: 'p-reactions', title: 'Reaction Roles', desc: 'Self-serve role menus and reaction button panels', url: '/reaction-roles', icon: 'nero-i-roles', group: 'Navigation', keywords: 'reaction roles buttons menus' },
    { id: 'p-triggers', title: 'Triggers & Sticky', desc: 'Auto-responders and sticky pinned channel messages', url: '/triggers', icon: 'nero-i-zap', group: 'Navigation', keywords: 'sticky auto-response keywords' },
    { id: 'p-custom-commands', title: 'Custom Commands', desc: 'Create and configure text/embed custom commands', url: '/custom-commands', icon: 'nero-i-terminal', group: 'Navigation', keywords: 'custom cc commands' },
    { id: 'p-mvp', title: 'MVP System', desc: 'Community scoreboards, daily standouts, and recognition', url: '/mvp', icon: 'nero-i-trophy', group: 'Navigation', keywords: 'mvp scoreboard leaders' },
    { id: 'p-leveling', title: 'Leveling & Rewards', desc: 'XP curves, voice/chat multipliers, and role rewards', url: '/leveling', icon: 'nero-i-star', group: 'Navigation', keywords: 'levels xp rank rewards' },
    { id: 'p-economy', title: 'Economy System', desc: 'Currency names, exchange rates, and bank settings', url: '/economy', icon: 'nero-i-coins', group: 'Navigation', keywords: 'coins diamonds bank cash' },
    { id: 'p-shop', title: 'Server Shop', desc: 'Manage items, colors, titles, and member purchases', url: '/shop', icon: 'nero-i-bag', group: 'Navigation', keywords: 'store shop buy items' },
    { id: 'p-shop-designer', title: 'Shop Designer', desc: 'Custom shop layouts and visual theme editor', url: '/shop-designer', icon: 'nero-i-bag', group: 'Navigation', keywords: 'shop designer theme' },
    { id: 'p-shop-publisher', title: 'Shop Publisher', desc: 'Publish and sync shop menus to Discord channels', url: '/shop-publisher', icon: 'nero-i-bag', group: 'Navigation', keywords: 'shop publish menu' },
    { id: 'p-events', title: 'Events Calendar', desc: 'Schedule and manage community events and dates', url: '/events', icon: 'nero-i-calendar', group: 'Navigation', keywords: 'events calendar' },
    { id: 'p-minigames', title: 'Minigames', desc: 'Configure trivia, math, wheel, and automated spawns', url: '/minigames', icon: 'nero-i-gamepad', group: 'Navigation', keywords: 'games trivia wheel math' },
    { id: 'p-missions', title: 'Missions & Tasks', desc: 'Community goals, daily missions, and reward payouts', url: '/missions', icon: 'nero-i-map', group: 'Navigation', keywords: 'missions quests goals' },
    { id: 'p-tag-missions', title: 'Tag Missions', desc: 'Server tag promotion missions and trackers', url: '/tag-missions', icon: 'nero-i-tag', group: 'Navigation', keywords: 'tag status mission' },
    { id: 'p-tag-partners', title: 'Tag Partners', desc: 'Partner server integrations and cross-promotions', url: '/tag-partners', icon: 'nero-i-handshake', group: 'Navigation', keywords: 'partners alliances' },
    { id: 'p-ledger', title: 'Transaction Ledger', desc: 'Read-only audit trail of currency and XP flows', url: '/ledger', icon: 'nero-i-book', group: 'Navigation', keywords: 'ledger transactions audit' },
    { id: 'p-inventory', title: 'Inventory Viewer', desc: 'Inspect member item bags and power-up holdings', url: '/inventory', icon: 'nero-i-backpack', group: 'Navigation', keywords: 'inventory items bag' },
    { id: 'p-trade', title: 'Trade History', desc: 'Log of member-to-member item and currency trades', url: '/trade', icon: 'nero-i-arrows', group: 'Navigation', keywords: 'trades swaps exchange' },
    { id: 'p-config-general', title: 'General Settings', desc: 'Prefix, bot nickname, and core server preferences', url: '/config/general', icon: 'nero-i-settings', group: 'Navigation', keywords: 'config settings prefix' },
    { id: 'p-config-welcome', title: 'Welcome & Gate', desc: 'Welcome cards, goodbye messages, and rules gate', url: '/config/welcome', icon: 'nero-i-hand', group: 'Navigation', keywords: 'welcome leave join gate' },
    { id: 'p-config-boost', title: 'Boost Roles', desc: 'Custom role rewards for server boosters', url: '/config/boost', icon: 'nero-i-star', group: 'Navigation', keywords: 'boost booster nitro roles' },
    { id: 'p-config-botprofile', title: 'Bot Profile', desc: 'Custom avatar, banner, and bio for Nero', url: '/config/botprofile', icon: 'nero-i-id', group: 'Navigation', keywords: 'profile avatar bio status' },
    { id: 'p-creator', title: 'Content Creator', desc: 'YouTube, Twitch, and Kick stream/video notifications', url: '/creator', icon: 'nero-i-video', group: 'Navigation', keywords: 'youtube twitch live stream' },
    { id: 'p-config-access', title: 'Dashboard Access', desc: 'Manage manager permissions and owner roles', url: '/config/access', icon: 'nero-i-lock', group: 'Navigation', keywords: 'access permissions admins' },
    { id: 'p-audit-log', title: 'Audit Log', desc: 'Complete history of all dashboard admin actions', url: '/audit-log', icon: 'nero-i-file', group: 'Navigation', keywords: 'audit logs admin history' },
    { id: 'p-reports', title: 'Report Queue', desc: 'In-app and Discord user message reports', url: '/reports', icon: 'nero-i-alert', group: 'Navigation', keywords: 'reports flag user message' },
    { id: 'p-backups', title: 'Database Backups', desc: 'Trigger manual snapshots and review backup archives', url: '/backups', icon: 'nero-i-database', group: 'Navigation', keywords: 'backups snapshot sqlite' },
    { id: 'p-health', title: 'System Health', desc: 'Cogs status, bot latency, error logs, and heartbeat', url: '/health', icon: 'nero-i-pulse', group: 'Navigation', keywords: 'health ping latency errors cogs' },

    // Quick Actions
    { id: 'a-theme', title: 'Toggle Color Theme', desc: 'Switch between Dark and Light mode', action: 'toggleTheme', icon: 'nero-i-sliders', group: 'Actions', keywords: 'theme dark light mode color' },
    { id: 'a-reload', title: 'Reload Page Data', desc: 'Refresh current dashboard view', action: 'reloadPage', icon: 'nero-i-refresh', group: 'Actions', keywords: 'refresh reload update' },

    // Core Commands Quick-Jumps
    { id: 'c-ban', title: '/ban, /kick, /mute', desc: 'Configure moderation commands in Command Manager', url: '/commands', icon: 'nero-i-shield', group: 'Commands', keywords: 'ban kick mute timeout punishment' },
    { id: 'c-level', title: '/rank, /leaderboard', desc: 'Configure leveling commands in Command Manager', url: '/commands', icon: 'nero-i-star', group: 'Commands', keywords: 'rank level leaderboard xp' },
    { id: 'c-shop', title: '/shop, /buy, /inventory', desc: 'Configure economy commands in Command Manager', url: '/commands', icon: 'nero-i-bag', group: 'Commands', keywords: 'shop buy inv balance daily' }
];

let _paletteActiveIndex = 0;
let _paletteFiltered = [];
let _palettePrevFocused = null;

function openCommandPalette() {
    const modal = document.getElementById('command-palette-modal');
    if (!modal) return;
    _palettePrevFocused = document.activeElement;
    modal.style.display = 'flex';
    const input = document.getElementById('cmd-palette-input');
    if (input) {
        input.value = '';
        input.focus();
    }
    filterPalette('');
}

function closeCommandPalette() {
    const modal = document.getElementById('command-palette-modal');
    if (!modal || modal.style.display === 'none') return;
    modal.style.display = 'none';
    if (_palettePrevFocused && typeof _palettePrevFocused.focus === 'function') {
        _palettePrevFocused.focus();
    }
}

function handlePaletteOverlayClick(event) {
    if (event.target && event.target.id === 'command-palette-modal') {
        closeCommandPalette();
    }
}

function filterPalette(rawQuery) {
    const q = (rawQuery || '').trim().toLowerCase();
    const resultsContainer = document.getElementById('cmd-palette-results');
    if (!resultsContainer) return;

    if (!q) {
        _paletteFiltered = NERO_PALETTE_ITEMS.slice(0, 12);
    } else {
        _paletteFiltered = NERO_PALETTE_ITEMS.filter(item => {
            const titleMatch = item.title.toLowerCase().includes(q);
            const descMatch = (item.desc || '').toLowerCase().includes(q);
            const keyMatch = (item.keywords || '').toLowerCase().includes(q);
            const groupMatch = (item.group || '').toLowerCase().includes(q);
            return titleMatch || descMatch || keyMatch || groupMatch;
        });
    }

    _paletteActiveIndex = 0;
    renderPaletteResults();
}

function renderPaletteResults() {
    const container = document.getElementById('cmd-palette-results');
    if (!container) return;

    if (!_paletteFiltered.length) {
        container.innerHTML = `<div class="cmd-palette-empty">No results matching your query.</div>`;
        return;
    }

    // Group by category
    const groups = {};
    _paletteFiltered.forEach((item, index) => {
        const g = item.group || 'Results';
        if (!groups[g]) groups[g] = [];
        groups[g].push({ ...item, globalIndex: index });
    });

    let html = '';
    Object.keys(groups).forEach(groupName => {
        html += `<div class="cmd-palette-group-title">${groupName}</div>`;
        groups[groupName].forEach(item => {
            const isActive = item.globalIndex === _paletteActiveIndex;
            html += `
                <div class="cmd-palette-item ${isActive ? 'active' : ''}" id="palette-item-${item.globalIndex}"
                     role="option" aria-selected="${isActive ? 'true' : 'false'}"
                     onclick="executePaletteItem(${item.globalIndex})">
                    <svg class="cmd-palette-item-icon" aria-hidden="true"><use href="#${item.icon || 'nero-i-home'}"></use></svg>
                    <div class="cmd-palette-item-text">
                        <div class="cmd-palette-item-title">${item.title}</div>
                        ${item.desc ? `<div class="cmd-palette-item-desc">${item.desc}</div>` : ''}
                    </div>
                    <span class="cmd-palette-item-badge">${item.group}</span>
                </div>
            `;
        });
    });

    container.innerHTML = html;
    const activeEl = document.getElementById(`palette-item-${_paletteActiveIndex}`);
    if (activeEl && typeof activeEl.scrollIntoView === 'function') {
        activeEl.scrollIntoView({ block: 'nearest' });
    }
}

function executePaletteItem(index) {
    const item = _paletteFiltered[index];
    if (!item) return;
    closeCommandPalette();

    if (item.action === 'toggleTheme') {
        if (typeof toggleTheme === 'function') toggleTheme();
    } else if (item.action === 'reloadPage') {
        window.location.reload();
    } else if (item.url) {
        window.location.href = item.url;
    }
}

function handlePaletteKeydown(event) {
    if (event.key === 'ArrowDown') {
        event.preventDefault();
        if (_paletteFiltered.length > 0) {
            _paletteActiveIndex = (_paletteActiveIndex + 1) % _paletteFiltered.length;
            renderPaletteResults();
        }
    } else if (event.key === 'ArrowUp') {
        event.preventDefault();
        if (_paletteFiltered.length > 0) {
            _paletteActiveIndex = (_paletteActiveIndex - 1 + _paletteFiltered.length) % _paletteFiltered.length;
            renderPaletteResults();
        }
    } else if (event.key === 'Enter') {
        event.preventDefault();
        if (_paletteFiltered.length > 0) {
            executePaletteItem(_paletteActiveIndex);
        }
    } else if (event.key === 'Escape') {
        event.preventDefault();
        closeCommandPalette();
    }
}

// Global keyboard listeners for Ctrl+K, Cmd+K, / and Escape
if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
    document.addEventListener('keydown', function(event) {
        const isCtrlK = (event.metaKey || event.ctrlKey) && (event.key === 'k' || event.key === 'K');
        const isSlash = event.key === '/' && !['INPUT', 'TEXTAREA', 'SELECT'].includes((document.activeElement?.tagName || '')) && !document.activeElement?.isContentEditable;

        if (isCtrlK || isSlash) {
            event.preventDefault();
            const modal = document.getElementById('command-palette-modal');
            if (modal && modal.style.display !== 'none') {
                closeCommandPalette();
            } else {
                openCommandPalette();
            }
        } else if (event.key === 'Escape') {
            const modal = document.getElementById('command-palette-modal');
            if (modal && modal.style.display !== 'none') {
                event.preventDefault();
                closeCommandPalette();
            }
        }
    });
}
