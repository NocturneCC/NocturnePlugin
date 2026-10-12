(() => {
  'use strict';
  const status = document.getElementById('status');
  const editor = document.getElementById('catalog');
  let revision = 0;
  let busy = false;

  async function request(path, method = 'GET', body) {
    const options = { method, credentials: 'same-origin', headers: { Accept: 'application/json' } };
    if (body !== undefined) {
      options.headers['Content-Type'] = 'application/json';
      options.body = JSON.stringify(body);
    }
    const response = await fetch(path, options);
    const result = await response.json();
    if (!response.ok || result.ok !== true) throw new Error('request_rejected');
    return result;
  }

  async function load() {
    try {
      const result = await request('/admin/api/plugin-tracking/catalog/draft');
      revision = result.draft.revision;
      editor.value = JSON.stringify(result.draft.catalog, null, 2);
      status.textContent = `Draft revision ${revision} loaded.`;
    } catch (_error) {
      status.textContent = 'Plugin Tracking catalog is unavailable.';
    }
  }

  async function save() {
    if (busy) return false;
    busy = true;
    try {
      const catalog = JSON.parse(editor.value);
      const result = await request('/admin/api/plugin-tracking/catalog/draft', 'PUT', { catalog, revision });
      revision = result.draft.revision;
      status.textContent = `Draft revision ${revision} saved.`;
      return true;
    } catch (_error) {
      status.textContent = 'Draft could not be saved. Check the JSON and reload if another editor published.';
      return false;
    } finally { busy = false; }
  }

  document.getElementById('save').addEventListener('click', save);
  document.getElementById('publish').addEventListener('click', async () => {
    if (!window.confirm('Publish this Plugin Tracking catalog?')) return;
    if (!await save()) return;
    try {
      await request('/admin/api/plugin-tracking/catalog/draft/publish', 'POST', { revision, confirm: true });
      status.textContent = 'Plugin Tracking catalog published.';
      await load();
    } catch (_error) {
      status.textContent = 'Catalog was not published. Reload the draft and review its state.';
    }
  });
  load();
})();
