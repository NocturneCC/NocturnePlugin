(() => {
  'use strict';

  const DOOM_ACTIVITY = Object.freeze({
    activity_key: 'doom_of_mokhaiotl',
    display_name: 'Doom of Mokhaiotl',
    aliases: ['Doom'],
    modes: [{
      mode_key: 'doom_of_mokhaiotl',
      display_name: 'Solo delve',
      source_names: ['Doom of Mokhaiotl', 'Mokhaiotl'],
      supports_solo: true,
      supports_group: false,
      loot_tracking_enabled: true,
      ttk_tracking_enabled: false,
      timing_scope: 'unconfigured',
      challenge_boss_key: null,
      challenge_mode_key: null,
      challenge_content_key: null,
      completion_tracking_enabled: true,
      completion_metric_key: 'deepest_level',
      completion_metric_label: 'Deepest level',
      completion_metric_unit: 'level'
    }]
  });

  function withDoomCompletion(catalog) {
    const next = JSON.parse(JSON.stringify(catalog));
    let activity = next.activities.find(item => item.activity_key === DOOM_ACTIVITY.activity_key);
    if (!activity) {
      activity = JSON.parse(JSON.stringify(DOOM_ACTIVITY));
      next.activities.push(activity);
      return next;
    }
    let mode = activity.modes.find(item => item.mode_key === 'doom_of_mokhaiotl');
    if (!mode) {
      mode = JSON.parse(JSON.stringify(DOOM_ACTIVITY.modes[0]));
      activity.modes.push(mode);
      return next;
    }
    Object.assign(mode, {
      completion_tracking_enabled: true,
      completion_metric_key: 'deepest_level',
      completion_metric_label: 'Deepest level',
      completion_metric_unit: 'level'
    });
    return next;
  }

  function applyCompletionControls(catalog, values) {
    const next = JSON.parse(JSON.stringify(catalog));
    for (const value of values) {
      const activity = next.activities.find(item => item.activity_key === value.activityKey);
      const mode = activity && activity.modes.find(item => item.mode_key === value.modeKey);
      if (!mode) throw new Error('completion_control_target_missing');
      mode.completion_tracking_enabled = value.enabled === true;
      if (value.enabled) {
        mode.completion_metric_key = String(value.metricKey || '').trim();
        mode.completion_metric_label = String(value.metricLabel || '').trim();
        mode.completion_metric_unit = String(value.metricUnit || '').trim();
      } else {
        mode.completion_metric_key = null;
        mode.completion_metric_label = null;
        mode.completion_metric_unit = null;
      }
    }
    return next;
  }

  function completionControlValues(container) {
    return Array.from(container.querySelectorAll('[data-completion-mode]')).map(row => ({
      activityKey: row.dataset.activityKey,
      modeKey: row.dataset.modeKey,
      enabled: row.querySelector('[data-field="enabled"]').checked,
      metricKey: row.querySelector('[data-field="metric-key"]').value,
      metricLabel: row.querySelector('[data-field="metric-label"]').value,
      metricUnit: row.querySelector('[data-field="metric-unit"]').value
    }));
  }

  function mount(document, fetchImpl, confirmImpl) {
    const status = document.getElementById('status');
    const editor = document.getElementById('catalog');
    const controls = document.getElementById('completion-controls');
    let revision = 0;
    let busy = false;

    function render(catalog) {
      controls.replaceChildren();
      for (const activity of catalog.activities) {
        for (const mode of activity.modes) {
          const fieldset = document.createElement('fieldset');
          fieldset.dataset.completionMode = 'true';
          fieldset.dataset.activityKey = activity.activity_key;
          fieldset.dataset.modeKey = mode.mode_key;
          const legend = document.createElement('legend');
          legend.textContent = `${activity.display_name} — ${mode.display_name}`;
          fieldset.append(legend);

          const enabledLabel = document.createElement('label');
          const enabled = document.createElement('input');
          enabled.type = 'checkbox';
          enabled.dataset.field = 'enabled';
          enabled.checked = mode.completion_tracking_enabled === true;
          enabledLabel.append(enabled, document.createTextNode(' Numeric completion tracking enabled'));
          fieldset.append(enabledLabel);

          const fields = [
            ['metric-key', 'Metric key', mode.completion_metric_key || '', 64],
            ['metric-label', 'Metric label', mode.completion_metric_label || '', 80],
            ['metric-unit', 'Unit', mode.completion_metric_unit || '', 32]
          ];
          for (const [key, labelText, initial, maxLength] of fields) {
            const label = document.createElement('label');
            label.textContent = labelText;
            const input = document.createElement('input');
            input.type = 'text';
            input.dataset.field = key;
            input.value = initial;
            input.maxLength = maxLength;
            input.disabled = !enabled.checked;
            label.append(input);
            fieldset.append(label);
          }
          enabled.addEventListener('change', () => {
            const inputs = fieldset.querySelectorAll('input[type="text"]');
            for (const input of inputs) input.disabled = !enabled.checked;
            if (enabled.checked && mode.mode_key === 'doom_of_mokhaiotl') {
              fieldset.querySelector('[data-field="metric-key"]').value ||= 'deepest_level';
              fieldset.querySelector('[data-field="metric-label"]').value ||= 'Deepest level';
              fieldset.querySelector('[data-field="metric-unit"]').value ||= 'level';
            }
          });
          controls.append(fieldset);
        }
      }
    }

    async function request(path, method = 'GET', body) {
      const options = { method, credentials: 'same-origin', headers: { Accept: 'application/json' } };
      if (body !== undefined) {
        options.headers['Content-Type'] = 'application/json';
        options.body = JSON.stringify(body);
      }
      const response = await fetchImpl(path, options);
      const result = await response.json();
      if (!response.ok || result.ok !== true) throw new Error('request_rejected');
      return result;
    }

    function parseEditor() {
      const catalog = JSON.parse(editor.value);
      return applyCompletionControls(catalog, completionControlValues(controls));
    }

    async function load() {
      try {
        const result = await request('/admin/api/plugin-tracking/catalog/draft');
        revision = result.draft.revision;
        editor.value = JSON.stringify(result.draft.catalog, null, 2);
        render(result.draft.catalog);
        status.textContent = `Draft revision ${revision} loaded.`;
      } catch (_error) {
        status.textContent = 'Plugin Tracking catalog is unavailable.';
      }
    }

    async function save() {
      if (busy) return false;
      busy = true;
      try {
        const catalog = parseEditor();
        const result = await request('/admin/api/plugin-tracking/catalog/draft', 'PUT', { catalog, revision });
        revision = result.draft.revision;
        editor.value = JSON.stringify(catalog, null, 2);
        render(catalog);
        status.textContent = `Draft revision ${revision} saved.`;
        return true;
      } catch (_error) {
        status.textContent = 'Draft could not be saved. Check the catalog fields and reload if another editor published.';
        return false;
      } finally { busy = false; }
    }

    document.getElementById('enable-doom').addEventListener('click', () => {
      try {
        const catalog = withDoomCompletion(JSON.parse(editor.value));
        editor.value = JSON.stringify(catalog, null, 2);
        render(catalog);
        status.textContent = 'Doom numeric completion is enabled in this draft. Save and publish to activate it.';
      } catch (_error) {
        status.textContent = 'Reload the catalog draft before enabling Doom completion.';
      }
    });
    document.getElementById('refresh-controls').addEventListener('click', () => {
      try {
        render(JSON.parse(editor.value));
        status.textContent = 'Numeric completion controls refreshed from the JSON.';
      } catch (_error) {
        status.textContent = 'Catalog JSON is invalid; controls were not changed.';
      }
    });
    document.getElementById('save').addEventListener('click', save);
    document.getElementById('publish').addEventListener('click', async () => {
      if (!confirmImpl('Publish this Plugin Tracking catalog?')) return;
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
  }

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { withDoomCompletion, applyCompletionControls };
  }
  if (typeof document !== 'undefined') mount(document, fetch.bind(window), window.confirm.bind(window));
})();
