// ═══════════════════════════════════════════════
// NERO DASHBOARD — dashboard.js
// Global JS: theme toggle, modals, toasts, helpers
// ═══════════════════════════════════════════════

// ── THEME TOGGLE ──
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

// ── MOBILE SIDEBAR ──
function toggleSidebar() {
    document.querySelector('.sidebar')?.classList.toggle('open');
}

// ── TOAST ──
function showToast(message, type = 'success', duration = 3000) {
    let toast = document.getElementById('global-toast');
    if (!toast) {
        toast = document.createElement('div');
        toast.id = 'global-toast';
        toast.className = 'toast';
        document.body.appendChild(toast);
    }
    toast.textContent = message;
    toast.className = `toast show${type === 'danger' ? ' danger' : ''}`;
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => {
        toast.classList.remove('show');
    }, duration);
}

// ── CONFIRM MODAL ──
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
    document.getElementById('confirm-cancel').onclick = () => {
        overlay.style.display = 'none';
    };
    document.getElementById('confirm-ok').onclick = () => {
        overlay.style.display = 'none';
        onConfirm();
    };
}

// ── EDIT MEMBER MODAL ──
let _editUid = null;

function openEditModal(uid, xp, coins) {
    _editUid = uid;
    document.getElementById('edit-uid-label').textContent = uid;
    document.getElementById('edit-xp').value   = xp;
    document.getElementById('edit-coins').value = coins;
    document.getElementById('edit-modal').style.display = 'flex';
}

function closeEditModal() {
    document.getElementById('edit-modal').style.display = 'none';
    _editUid = null;
}

async function saveEditModal() {
    const xp    = parseInt(document.getElementById('edit-xp').value);
    const coins = parseInt(document.getElementById('edit-coins').value);
    const res   = await fetch('/api/edit-member', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ user_id: _editUid, xp, coins }),
    });
    const data = await res.json();
    if (data.success) {
        showToast('Member updated!');
        closeEditModal();
        setTimeout(() => location.reload(), 800);
    } else {
        showToast('Error saving', 'danger');
    }
}

// ── HTMX EVENTS ──
document.addEventListener('htmx:afterRequest', function(e) {
    if (e.detail.xhr.status >= 400) {
        showToast('Something went wrong', 'danger');
    }
});

document.addEventListener('htmx:afterSwap', function(e) {
    if (e.detail.target.id &&
        e.detail.target.id.includes('tbody') &&
        e.detail.xhr.status === 200) {
        // Rows swapped successfully — no toast needed
    }
});

// ── COPY TO CLIPBOARD ──
function copyText(text) {
    navigator.clipboard.writeText(text).then(() => {
        showToast('Copied!');
    });
}

// ── COLOR PICKER SYNC ──
function syncColorInput(pickerId, textId) {
    const picker = document.getElementById(pickerId);
    const text   = document.getElementById(textId);
    if (!picker || !text) return;
    picker.addEventListener('input', () => { text.value = picker.value; });
    text.addEventListener('input', () => {
        if (/^#[0-9A-Fa-f]{6}$/.test(text.value)) {
            picker.value = text.value;
        }
    });
}

// ── SAVE BANNER AUTO-HIDE ──
(function hideSaveBanner() {
    const banner = document.querySelector('.save-banner');
    if (banner) setTimeout(() => banner.remove(), 4000);
})();

// ── CLOSE MODAL ON OVERLAY CLICK ──
document.addEventListener('click', function(e) {
    const overlay = document.getElementById('confirm-overlay');
    if (e.target === overlay) overlay.style.display = 'none';
    const editModal = document.getElementById('edit-modal');
    if (e.target === editModal) closeEditModal();
});
