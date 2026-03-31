// ═══════════════════════════════════════════════════════════════
// NERO DASHBOARD — dashboard.js  (Phase 2 upgrade)
// ═══════════════════════════════════════════════════════════════

// ── THEME TOGGLE ──────────────────────────────────────────────
function toggleTheme() {
    const html = document.documentElement;
    const next = html.getAttribute('data-theme') === 'dark' ? 'light' : 'dark';
    html.setAttribute('data-theme', next);
    localStorage.setItem('nero-theme', next);
}
(function initTheme() {
    const saved = localStorage.getItem('nero-theme');
    if (saved) document.documentElement.setAttribute('data-theme', saved);
})();

// ── MOBILE SIDEBAR ────────────────────────────────────────────
function toggleSidebar() {
    const sidebar  = document.getElementById('sidebar');
    const overlay  = document.querySelector('.sidebar-overlay');
    const isOpen   = sidebar && sidebar.classList.contains('open');
    if (sidebar) sidebar.classList.toggle('open');
    if (overlay) overlay.classList.toggle('active');
    document.body.classList.toggle('sidebar-open', !isOpen);
}

// ── TOAST SYSTEM (4 types) ────────────────────────────────────
const TOAST_CONFIG = {
    success: { icon: '✅', bg: 'var(--success)',  fg: '#000', dur: 3000 },
    error:   { icon: '❌', bg: 'var(--danger)',   fg: '#fff', dur: 5000 },
    warning: { icon: '⚠️', bg: 'var(--warning)',  fg: '#000', dur: 4000 },
    info:    { icon: 'ℹ️', bg: 'var(--accent)',   fg: '#fff', dur: 3000 },
    // Legacy aliases
    danger:  { icon: '❌', bg: 'var(--danger)',   fg: '#fff', dur: 5000 },
};

function showToast(message, type = 'success', duration) {
    const cfg = TOAST_CONFIG[type] || TOAST_CONFIG.success;
    const dur = duration || cfg.dur;

    let container = document.getElementById('toast-container');
    if (!container) {
        container = document.createElement('div');
        container.id = 'toast-container';
        document.body.appendChild(container);
    }

    const toast = document.createElement('div');
    toast.className = 'toast-item';
    toast.innerHTML = `<span class="toast-icon">${cfg.icon}</span><span class="toast-msg">${message}</span><button class="toast-close" onclick="this.parentElement.remove()">✕</button>`;
    toast.style.setProperty('--toast-bg', cfg.bg);
    toast.style.setProperty('--toast-fg', cfg.fg);
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
        btn.disabled   = true;
        btn.innerHTML  = `<span class="btn-spinner"></span> ${loadText}`;
        btn.classList.add('loading');
    } else {
        btn.disabled   = btn._origDis || false;
        btn.innerHTML  = btn._origText || btn.innerHTML;
        btn.classList.remove('loading');
    }
}

// Helper: AJAX save with standard loading + toast feedback
async function ajaxSave(url, payload, btn, successMsg = 'Saved!') {
    if (btn) setLoading(btn, true);
    try {
        const res  = await fetch(url, {
            method:  'POST',
            headers: { 'Content-Type': 'application/json' },
            body:    JSON.stringify(payload),
        });
        const data = await res.json();
        if (data.success || data.ok) {
            showToast(successMsg, 'success');
        } else {
            showToast(data.error || 'Save failed', 'error');
        }
        return data;
    } catch (err) {
        showToast('Connection error', 'error');
        return null;
    } finally {
        if (btn) setLoading(btn, false);
    }
}

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

// ── CLOSE MODAL ON OVERLAY CLICK ─────────────────────────────
document.addEventListener('click', function(e) {
    const co = document.getElementById('confirm-overlay');
    if (e.target === co) co.style.display = 'none';
    const em = document.getElementById('edit-modal');
    if (e.target === em) closeEditModal();
});

// ── HTMX ERROR HANDLING ───────────────────────────────────────
document.addEventListener('htmx:afterRequest', function(e) {
    if (e.detail.xhr && e.detail.xhr.status >= 500) {
        showToast('Server error — please try again', 'error');
    }
});

// ── HTMX AFTER SWAP: Re-init Select2 and update page title ──────
document.addEventListener('htmx:afterSwap', function(e) {
    // Re-initialize Select2 pickers if NeroSelect is available
    if (window.NeroSelect && window.NeroSelect.initAll) {
        window.NeroSelect.initAll(document);
    }
    
    // Update page title from data-page-title attribute
    const pageTitle = document.querySelector('[data-page-title]');
    if (pageTitle) {
        const title = pageTitle.getAttribute('data-page-title');
        if (title) {
            const titleEl = document.getElementById('page-title');
            if (titleEl) titleEl.textContent = title;
        }
    }
});
