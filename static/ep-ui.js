// Shared UI helpers for the panels. Loaded before each page's inline script;
// kept global on purpose (inline on* handlers call them too).

function escHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value || '-';
}

function formatBytes(bytes, decimals = 2) {
    if (!bytes || bytes === 0) return '0 B';
    const k = 1024;
    const dm = decimals < 0 ? 0 : decimals;
    const sizes = ['B', 'KB', 'MB', 'GB', 'TB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return parseFloat((bytes / Math.pow(k, i)).toFixed(dm)) + ' ' + sizes[i];
}

// The panel toasts (admin, recordings) style ".toast-<kind>"; the tunnel panels
// style ".toast.<kind>". Set both shapes so one implementation serves every page.
function toast(message, kind) {
    const el = document.getElementById('toast');
    if (!el) return;
    const suffix = kind ? ' ' + kind + ' toast-' + kind : '';
    el.textContent = message;
    el.className = 'toast show' + suffix;
    setTimeout(() => { el.className = 'toast' + suffix; }, 3000);
}

// fetch wrapper: adds api_password when the page knows it (window.EP_API_PASSWORD
// or ?api_password= in the URL) and bounces to the login on 401.
async function api(path, options) {
    const password = window.EP_API_PASSWORD
        || new URLSearchParams(window.location.search).get('api_password')
        || '';
    const url = path + (password ? (path.includes('?') ? '&' : '?') + 'api_password=' + encodeURIComponent(password) : '');
    const response = await fetch(url, options);
    if (response.status === 401) { window.location.href = '/admin/login'; throw new Error('unauthorized'); }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || ('HTTP ' + response.status));
    return data;
}

// "Last check" line of the tunnel panels; each panel sets its own storage key.
function epCheckStorageKey() {
    return window.EP_CHECK_STORAGE_KEY || 'easyproxy:last-check';
}

function persistCheckResult(value) {
    const target = document.getElementById('check-result');
    if (!target) return;
    target.textContent = value || '-';
    target.title = 'Last check: ' + new Date().toLocaleString();
    try { localStorage.setItem(epCheckStorageKey(), JSON.stringify({value, at: Date.now()})); } catch (_) {}
}

function restoreCheckResult() {
    try {
        const saved = JSON.parse(localStorage.getItem(epCheckStorageKey()) || 'null');
        if (saved && saved.value) {
            const target = document.getElementById('check-result');
            if (!target) return;
            target.textContent = saved.value;
            target.title = saved.at ? 'Last check: ' + new Date(saved.at).toLocaleString() : '';
        }
    } catch (_) {}
}
