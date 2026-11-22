// Configuration reset for every inactive plugin, including cards without toggles.
(function () {
  let timer;
  async function decorate() {
    const root = document.getElementById('pluginListDisabled');
    if (!root) return;
    const responses = await Promise.all([fetch('/api/plugins'), fetch('/api/plugins/clearable')]);
    if (responses.some(r => !r.ok)) return;
    const plugins = await responses[0].json();
    const allowed = new Set(await responses[1].json());
    root.querySelectorAll('.plugin-row').forEach(row => {
      const checkbox = row.querySelector('.plugin-toggle');
      const link = row.querySelector('.qa-config');
      const plugin = plugins.find(p => checkbox ? p.id === checkbox.dataset.id :
        link && p.config_url === link.getAttribute('href'));
      if (!plugin || plugin.enabled || !allowed.has(plugin.id) || row.querySelector('.qa-clear-config')) return;
      const actions = row.querySelector('.plugin-quick-actions');
      if (!actions) return;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'quick-action-btn qa-clear-config';
      button.textContent = 'Wyczyść config';
      button.style.cssText = 'width:auto;padding:0 8px;color:var(--danger)';
      button.onclick = async event => {
        event.stopPropagation();
        if (Object.keys(pendingChanges).length) {
          showMsg(false, 'Najpierw zapisz oczekujące zmiany.');
          return;
        }
        if (!confirm('Wyczyścić konfigurację: ' + plugin.name + '? Operacji nie można cofnąć. Historia pozostanie.')) return;
        button.disabled = true;
        try {
          const response = await authorizedFetch('/api/plugins/' + encodeURIComponent(plugin.id) + '/clear_config', {method: 'POST'});
          const data = await response.json();
          showMsg(response.ok, data.message || data.error);
          await loadPlugins();
        } catch (error) {
          showMsg(false, 'Błąd czyszczenia: ' + error);
        } finally {
          button.disabled = false;
        }
      };
      if (link) link.after(button); else actions.appendChild(button);
    });
  }
  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(() => decorate().catch(error => showMsg(false, String(error))), 150);
  }
  document.addEventListener('DOMContentLoaded', () => {
    const root = document.getElementById('pluginListDisabled');
    if (!root) return;
    new MutationObserver(schedule).observe(root, {childList: true, subtree: true});
    schedule();
  });
})();
