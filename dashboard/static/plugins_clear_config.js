// Przycisk "Wyczyść config" w sekcji "Wtyczki do włączenia" (strona /plugins).
(function () {
  const LIST_ID = 'pluginListDisabled';
  let clearable = new Set();
  let timer = null;

  function showMsg(ok, text) {
    const el = document.getElementById('pluginsMsg');
    if (!el) return;
    el.className = 'msg-box ' + (ok ? 'ok' : 'error');
    el.style.display = '';
    el.textContent = (ok ? '✅ ' : '❌ ') + text;
  }

  async function refreshClearable() {
    try {
      const resp = await fetch('/api/plugins/clearable');
      if (resp.ok) clearable = new Set(await resp.json());
    } catch (e) { /* pozostaw poprzedni stan */ }
  }

  async function clearConfig(id, name) {
    if (!confirm('Usunąć zapisaną konfigurację wtyczki "' + name + '"? Tej operacji nie można cofnąć.')) return;
    try {
      const resp = await fetch('/api/plugins/' + encodeURIComponent(id) + '/clear_config', { method: 'POST' });
      if (resp.status === 401) { window.location.href = '/login?next=' + encodeURIComponent(location.pathname); return; }
      const data = await resp.json();
      showMsg(resp.ok, data.message || data.error || 'Gotowe');
      if (typeof loadPlugins === 'function') await loadPlugins();
    } catch (e) {
      showMsg(false, 'Błąd komunikacji z serwerem: ' + e);
    }
  }

  function decorate() {
    const root = document.getElementById(LIST_ID);
    if (!root) return;
    root.querySelectorAll('.plugin-row').forEach((row) => {
      const checkbox = row.querySelector('.plugin-toggle');
      if (!checkbox) return;
      const id = checkbox.dataset.id;
      const existing = row.querySelector('.clear-config-row');
      if (!clearable.has(id)) { if (existing) existing.remove(); return; }
      if (existing) return;
      const actions = row.querySelector('.plugin-quick-actions');
      if (!actions) return;
      const name = (row.querySelector('.plugin-name') || {}).textContent || id;
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'quick-action-btn clear-config-row';
      btn.textContent = 'Wyczyść config';
      btn.title = 'Usuń zapisane ustawienia nieaktywnej wtyczki';
      btn.style.width = 'auto';
      btn.style.padding = '0 8px';
      btn.style.color = 'var(--danger)';
      btn.addEventListener('click', (event) => {
        event.stopPropagation();
        if (Object.keys(pendingChanges).length) {
          showMsg(false, 'Najpierw zapisz oczekujące zmiany.');
          return;
        }
        clearConfig(id, name);
      });
      const settings = actions.querySelector('.qa-config');
      if (settings) settings.after(btn);
      else actions.appendChild(btn);
    });
  }

  async function update() {
    await refreshClearable();
    decorate();
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(update, 150);
  }

  document.addEventListener('DOMContentLoaded', () => {
    const root = document.getElementById(LIST_ID);
    if (!root) return;
    new MutationObserver(schedule).observe(root, { childList: true, subtree: true });
    schedule();
  });
})();
