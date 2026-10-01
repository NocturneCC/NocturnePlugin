(function attachChallengeAdminState(root, factory) {
  const api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.ChallengeAdminState = api;
}(typeof globalThis !== 'undefined' ? globalThis : this, function buildStateApi() {
  'use strict';

  const TIME_DEFAULTS = {
    2: [null, ''], 3: [null, ''], 4: [null, ''], 5: [null, ''],
  };
  const NUMERIC_DEFAULTS = { 2: null, 3: null, 4: null, 5: null };

  function uniqueBossKey(displayName, existingKeys = []) {
    const base = String(displayName || '').trim().toLowerCase()
      .replace(/['’]/g, '')
      .replace(/[^a-z0-9]+/g, '_')
      .replace(/^_+|_+$/g, '');
    if (!base) throw new Error('Enter a Challenge name first.');
    const used = new Set(existingKeys || []);
    if (!used.has(base)) return base;
    let suffix = 2;
    while (used.has(`${base}_${suffix}`)) suffix += 1;
    return `${base}_${suffix}`;
  }

  async function validatePngFile(file) {
    if (!file) return 'Choose a PNG image.';
    if (!String(file.name || '').toLowerCase().endsWith('.png')) {
      return 'Boss artwork must have a .png filename.';
    }
    if (String(file.type || '').toLowerCase() !== 'image/png') {
      return 'Boss artwork must be a PNG image, not a renamed JPG or WebP file.';
    }
    if (!Number(file.size) || Number(file.size) > 5 * 1024 * 1024) {
      return 'Boss artwork must be a non-empty PNG no larger than 5 MB.';
    }
    const bytes = new Uint8Array(await file.slice(0, 24).arrayBuffer());
    const signature = [137, 80, 78, 71, 13, 10, 26, 10];
    const valid = bytes.length >= 24
      && signature.every((value, index) => bytes[index] === value)
      && String.fromCharCode(...bytes.slice(12, 16)) === 'IHDR';
    return valid ? null : 'The selected file is not a valid PNG. Export it as PNG and try again.';
  }

  function selectExistingArtwork(boss, iconUrl) {
    const url = String(iconUrl || '').trim();
    if (!/^\/media\/boss_icons\/[^/?#]+\.png$/i.test(url)) {
      throw new Error('Choose artwork from the available icon catalog.');
    }
    boss.icon_url = url;
    return boss;
  }

  function normalizeMetric(boss, nextMetric) {
    if (!['time', 'numeric', 'completion'].includes(nextMetric)) {
      throw new Error('Unsupported metric type');
    }
    boss.metric_type = nextMetric;
    boss.automatic_capture = 'manual_only';
    if (nextMetric === 'time') {
      delete boss.numeric_metric_key; delete boss.numeric_metric_label; delete boss.numeric_metric_unit;
      boss.timing_scope = 'unconfigured'; boss.timing_segment_key = null; boss.timing_segment_label = null;
    } else if (nextMetric === 'numeric') {
      delete boss.timing_scope; delete boss.timing_segment_key; delete boss.timing_segment_label;
      boss.numeric_metric_key = null; boss.numeric_metric_label = null; boss.numeric_metric_unit = null;
    } else {
      for (const field of ['timing_scope', 'timing_segment_key', 'timing_segment_label',
        'numeric_metric_key', 'numeric_metric_label', 'numeric_metric_unit', 'automatic_capture']) {
        delete boss[field];
      }
    }
    boss.time_input_format = nextMetric === 'time'
      ? (boss.time_input_format || 'HH:MM:SS.xx') : null;
    boss.comparison_direction = nextMetric === 'numeric'
      ? 'higher'
      : nextMetric === 'completion' ? 'complete' : 'lower';
    for (const tier of boss.tiers || []) {
      const rank = Number(tier.rank);
      if (rank === 1 || nextMetric === 'completion') {
        Object.assign(tier, {
          threshold: 1,
          threshold_display: 'Completion',
          metric_type: 'completion',
          operator: 'complete',
          unit: 'boolean',
        });
      } else if (nextMetric === 'numeric') {
        const value = Object.hasOwn(NUMERIC_DEFAULTS, rank)
          ? NUMERIC_DEFAULTS[rank] : null;
        Object.assign(tier, {
          threshold: value,
          threshold_display: '',
          metric_type: 'numeric',
          operator: 'gte',
          unit: 'waves',
        });
      } else {
        const [value, display] = TIME_DEFAULTS[rank] || [0, '0:00:00'];
        Object.assign(tier, {
          threshold: value,
          threshold_display: display,
          metric_type: 'time',
          operator: 'lte',
          unit: 'milliseconds',
        });
      }
    }
    return boss;
  }

  function normalizeCaptureMetadata(boss) {
    if (boss.metric_type === 'time') {
      boss.timing_scope ??= 'unconfigured';
      boss.timing_segment_key ??= null;
      boss.timing_segment_label ??= null;
      boss.automatic_capture ??= 'manual_only';
    } else if (boss.metric_type === 'numeric') {
      boss.numeric_metric_key ??= null;
      boss.numeric_metric_label ??= null;
      boss.numeric_metric_unit ??= null;
      boss.automatic_capture ??= 'manual_only';
    }
    return boss;
  }

  function captureDefinitionReady(boss) {
    const key = value => typeof value === 'string' && /^[a-z][a-z0-9_]{0,63}$/.test(value);
    const text = (value, max) => typeof value === 'string'
      && value.trim().length > 0 && value.trim().length <= max
      && !/[\u0000-\u001f\u007f-\u009f]/.test(value);
    if (boss.metric_type === 'time') {
      if (boss.timing_scope === 'overall') return true;
      return boss.timing_scope === 'segment'
        && key(boss.timing_segment_key) && text(boss.timing_segment_label, 120);
    }
    if (boss.metric_type === 'numeric') {
      return key(boss.numeric_metric_key) && text(boss.numeric_metric_label, 120)
        && text(boss.numeric_metric_unit, 32);
    }
    return false;
  }

  function setAutomaticCapture(boss, value) {
    if (!['manual_only', 'enabled'].includes(value)) throw new Error('Unsupported automatic capture mode');
    if (value === 'enabled' && !captureDefinitionReady(boss)) {
      throw new Error('Complete the timing or numeric definition before enabling automatic capture.');
    }
    boss.automatic_capture = value;
    return boss;
  }

  function normalizeSubmissionMode(boss, mode) {
    if (!['solo', 'group', 'either'].includes(mode)) {
      throw new Error('Unsupported submission mode');
    }
    boss.submission_mode = mode;
    if (mode === 'solo') {
      boss.supports_groups = false;
      boss.min_party_size = 1;
    } else if (mode === 'group') {
      boss.supports_groups = true;
      boss.min_party_size = Math.max(2, Number(boss.min_party_size) || 2);
    } else {
      boss.supports_groups = true;
      boss.min_party_size = 1;
    }
    return boss;
  }

  function normalizeTimeThreshold(value, inputFormat = 'HH:MM:SS.xx') {
    const text = String(value || '').trim();
    let hours = 0; let minutes; let seconds; let fraction; let fractionMilliseconds;
    if (inputFormat === 'MM:SS.xx') {
      const match = /^(\d+):([0-5]\d)(?:\.(\d{2}))?$/.exec(text);
      const canonical = /^(\d+):([0-5]\d):([0-5]\d)(?:\.(\d{1,3}))?$/.exec(text);
      if (!match && !canonical) throw new Error('Enter time as MM:SS.xx, for example 17:00.00.');
      if (match) {
        minutes = Number(match[1]); seconds = Number(match[2]); fraction = match[3] || '00';
      } else {
        hours = Number(canonical[1]); minutes = Number(canonical[2]); seconds = Number(canonical[3]);
        const canonicalFraction = String(canonical[4] || '0').padEnd(3, '0').slice(0, 3);
        fractionMilliseconds = Number(canonicalFraction);
        if (fractionMilliseconds % 10) throw new Error('This stored time has millisecond precision that MM:SS.xx cannot represent. Choose HH:MM:SS.xx or enter a centisecond value.');
        fraction = canonicalFraction.slice(0, 2);
      }
    } else if (inputFormat === 'HH:MM:SS.xx') {
      const match = /^(\d+):([0-5]\d):([0-5]\d)(?:\.(\d{2}))?$/.exec(text);
      if (!match) throw new Error('Enter time as HH:MM:SS.xx, for example 00:17:00.00.');
      hours = Number(match[1]); minutes = Number(match[2]); seconds = Number(match[3]); fraction = match[4] || '00';
    } else {
      throw new Error('Unsupported time input format.');
    }
    const milliseconds = ((hours * 3600 + minutes * 60 + seconds) * 1000)
      + (fractionMilliseconds === undefined ? Number(fraction) * 10 : fractionMilliseconds);
    const internalMinutes = Math.floor((milliseconds % 3600000) / 60000);
    const internalSeconds = Math.floor((milliseconds % 60000) / 1000);
    const internalMillis = milliseconds % 1000;
    const internalHours = Math.floor(milliseconds / 3600000);
    const display = inputFormat === 'MM:SS.xx'
      ? `${String(Math.floor(milliseconds / 60000)).padStart(2, '0')}:${String(internalSeconds).padStart(2, '0')}.${fraction}`
      : `${String(internalHours).padStart(2, '0')}:${String(internalMinutes).padStart(2, '0')}:${String(internalSeconds).padStart(2, '0')}.${fraction}`;
    return { milliseconds, display, internal: `${internalHours}:${String(internalMinutes).padStart(2, '0')}:${String(internalSeconds).padStart(2, '0')}.${String(internalMillis).padStart(3, '0')}` };
  }

  function reorderBosses(config, draggedKey, targetKey) {
    const bosses = [...(config?.bosses || [])].sort((a, b) => Number(a.display_order) - Number(b.display_order) || String(a.boss_key).localeCompare(String(b.boss_key)));
    const from = bosses.findIndex(boss => boss.boss_key === draggedKey);
    const to = bosses.findIndex(boss => boss.boss_key === targetKey);
    if (from < 0 || to < 0 || from === to) return false;
    const [moved] = bosses.splice(from, 1);
    bosses.splice(to, 0, moved);
    bosses.forEach((boss, index) => { boss.display_order = (index + 1) * 10; });
    config.bosses = bosses;
    return true;
  }

  function createBoss({ bossKey, displayName, metricType = 'time', submissionMode = 'either', displayOrder = 0 }) {
    const key = String(bossKey || '').trim();
    const name = String(displayName || '').trim();
    if (!/^[a-z0-9]+(?:_[a-z0-9]+)*$/.test(key)) throw new Error('Invalid boss key');
    if (!name) throw new Error('Display name is required');
    const definitions = [
      ['bronze', 'Bronze', 1], ['silver', 'Silver', 2], ['gold', 'Gold', 3],
      ['platinum', 'Platinum', 4], ['ascendant', 'Ascendant', 5],
    ];
    const boss = {
      boss_key: key, display_name: name, active: true,
      display_order: Number(displayOrder), metric_type: metricType,
      comparison_direction: 'lower', aliases: [name, key],
      description: null, help_text: null, icon_url: null, discord_label: name,
      discord_emoji: null, discord_group: null, submission_enabled: true,
      supports_groups: true, min_party_size: 1,
      submission_mode: submissionMode, time_input_format: 'HH:MM:SS.xx',
      tiers: definitions.map(([tierKey, tier, rank]) => ({
        tier_key: tierKey, tier, rank, threshold: rank === 1 ? 1 : null,
        threshold_display: rank === 1 ? 'Completion' : '',
        metric_type: rank === 1 ? 'completion' : 'time',
        operator: rank === 1 ? 'complete' : 'lte',
        unit: rank === 1 ? 'boolean' : 'milliseconds',
        points: rank * 10, progression_points: 5 * rank * (rank + 1),
      })),
    };
    normalizeMetric(boss, metricType);
    normalizeCaptureMetadata(boss);
    normalizeSubmissionMode(boss, submissionMode);
    return boss;
  }

  function uniqueModeKey(displayName, existingKeys = []) {
    return uniqueBossKey(displayName, existingKeys);
  }

  function createLeaderboardMode({
    modeKey, boss = null, displayName, contentKey = null,
    partySizeMin = 1, partySizeMax = partySizeMin, displayOrder = 0,
  }) {
    const key = String(modeKey || '').trim();
    const name = String(displayName || '').trim();
    if (!/^[a-z0-9]+(?:_[a-z0-9]+)*$/.test(key)) throw new Error('Invalid leaderboard mode key');
    if (!name) throw new Error('Leaderboard display name is required');
    const metricType = boss?.metric_type || 'time';
    return {
      mode_key: key,
      boss_key: boss?.boss_key || null,
      content_key: String(contentKey || boss?.boss_key || key),
      display_name: name,
      active: true,
      display_order: Number(displayOrder),
      metric_type: metricType,
      comparison_direction: boss?.comparison_direction
        || (metricType === 'numeric' ? 'higher' : metricType === 'completion' ? 'complete' : 'lower'),
      metric_unit: metricType === 'time' ? 'milliseconds'
        : metricType === 'completion' ? 'boolean' : 'waves',
      party_size_min: Number(partySizeMin),
      party_size_max: Number(partySizeMax),
      top_n: 3,
      inherit_boss_icon: Boolean(boss),
      icon_url: null,
      publication_group_key: key,
      publication_group_name: name,
      publication_group_order: Number(displayOrder),
      group_icon_url: null,
      aliases: [name, key],
    };
  }

  function diffLines(diff) {
    if (!diff || !diff.has_changes) return ['No semantic changes.'];
    const lines = [];
    for (const boss of diff.bosses || []) {
      lines.push(`${boss.change_type}: ${boss.display_name || boss.boss_key} (${boss.boss_key})`);
      for (const change of boss.fields || []) {
        lines.push(`  ${change.field}: ${JSON.stringify(change.before)} → ${JSON.stringify(change.after)}`);
      }
      for (const tier of boss.tiers || []) {
        lines.push(`  ${tier.tier_key}: ${tier.change_type}`);
        for (const change of tier.fields || []) {
          lines.push(`    ${change.field}: ${JSON.stringify(change.before)} → ${JSON.stringify(change.after)}`);
        }
      }
    }
    for (const tier of diff.system_tiers || []) {
      lines.push(`system tier ${tier.system_tier_key}: ${tier.change_type}`);
    }
    for (const mode of diff.leaderboard_modes || []) {
      lines.push(`leaderboard ${mode.change_type}: ${mode.display_name || mode.mode_key} (${mode.mode_key})`);
      for (const change of mode.fields || []) {
        lines.push(`  ${change.field}: ${JSON.stringify(change.before)} → ${JSON.stringify(change.after)}`);
      }
    }
    return lines;
  }

  function friendlyDiffLines(diff) {
    if (!diff || !diff.has_changes) return ['No changes to publish.'];
    const labels = {
      display_name: 'Name', active: 'Active status', aliases: 'Accepted names',
      metric_type: 'Submission type', submission_mode: 'Who can submit',
      min_party_size: 'Minimum party size', time_input_format: 'Time format',
      timing_scope: 'Timing scope', timing_segment_key: 'Timing segment key',
      timing_segment_label: 'Timing segment label', automatic_capture: 'Automatic capture',
      numeric_metric_key: 'Numeric meaning', numeric_metric_label: 'Numeric label',
      numeric_metric_unit: 'Numeric unit',
      submission_enabled: 'Available to members', icon_url: 'Boss artwork',
      points: 'Points', threshold: 'Requirement', threshold_display: 'Requirement',
      description: 'Description', help_text: 'Help text', display_order: 'Display order',
    };
    const lines = [];
    for (const boss of diff.bosses || []) {
      const action = boss.change_type === 'added' ? 'Add'
        : boss.change_type === 'removed' ? 'Remove'
          : boss.change_type === 'deactivated' ? 'Deactivate'
            : boss.change_type === 'reactivated' ? 'Reactivate' : 'Update';
      lines.push(`${action}: ${boss.display_name || 'Challenge'}`);
      for (const change of boss.fields || []) {
        const label = labels[change.field] || change.field.replaceAll('_', ' ');
        lines.push(`  ${label}: ${JSON.stringify(change.before)} → ${JSON.stringify(change.after)}`);
      }
      for (const tier of boss.tiers || []) {
        for (const change of tier.fields || []) {
          const label = labels[change.field] || change.field.replaceAll('_', ' ');
          lines.push(`  ${tier.tier_key[0].toUpperCase()+tier.tier_key.slice(1)} ${label}: ${JSON.stringify(change.before)} → ${JSON.stringify(change.after)}`);
        }
      }
    }
    for (const mode of diff.leaderboard_modes || []) {
      const action = mode.change_type === 'added' ? 'Add leaderboard'
        : mode.change_type === 'removed' ? 'Remove leaderboard' : 'Update leaderboard';
      lines.push(`${action}: ${mode.display_name || mode.mode_key}`);
      for (const change of mode.fields || []) {
        const label = labels[change.field] || change.field.replaceAll('_', ' ');
        lines.push(`  ${label}: ${JSON.stringify(change.before)} → ${JSON.stringify(change.after)}`);
      }
    }
    return lines;
  }

  function workflowState({ draft, dirty = false, saving = false }) {
    const hasDraft = Boolean(draft);
    const revision = hasDraft ? Number(draft.revision) : null;
    const validatedRevision = hasDraft && draft.validated_revision !== null
      && draft.validated_revision !== undefined
      ? Number(draft.validated_revision)
      : null;
    const validated = hasDraft
      && !dirty
      && !saving
      && draft.state === 'validated'
      && validatedRevision === revision;
    return {
      hasDraft,
      revision,
      validatedRevision,
      unsaved: hasDraft && dirty,
      saved: hasDraft && !dirty && !saving,
      validated,
      publishable: validated,
      canSave: hasDraft && dirty && !saving,
      canValidate: hasDraft && !dirty && !saving,
      canPublish: validated,
      label: !hasDraft
        ? 'No draft'
        : saving ? 'Saving…'
          : dirty ? 'Unsaved'
            : validated ? 'Publishable'
              : 'Saved · validation required',
    };
  }

  function isDraftOnly(bossKey, publishedBossKeys) {
    return !new Set(publishedBossKeys || []).has(bossKey);
  }

  function removeDraftBoss(config, bossKey, publishedBossKeys) {
    if (!isDraftOnly(bossKey, publishedBossKeys)) {
      throw new Error('Published bosses must be deactivated rather than removed');
    }
    const bosses = Array.isArray(config && config.bosses) ? config.bosses : [];
    const remaining = bosses.filter((boss) => boss.boss_key !== bossKey);
    if (remaining.length === bosses.length) throw new Error('Draft boss not found');
    config.bosses = remaining;
    return config;
  }

  return {
    uniqueBossKey,
    validatePngFile,
    selectExistingArtwork,
    normalizeMetric,
    normalizeCaptureMetadata,
    captureDefinitionReady,
    setAutomaticCapture,
    normalizeSubmissionMode,
    normalizeTimeThreshold,
    reorderBosses,
    createBoss,
    uniqueModeKey,
    createLeaderboardMode,
    diffLines,
    friendlyDiffLines,
    workflowState,
    isDraftOnly,
    removeDraftBoss,
  };
}));
