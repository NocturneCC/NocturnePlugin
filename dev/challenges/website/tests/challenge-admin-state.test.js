'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const {
  createBoss,
  createLeaderboardMode,
  diffLines,
  friendlyDiffLines,
  isDraftOnly,
  normalizeMetric,
  normalizeCaptureMetadata,
  captureDefinitionReady,
  setAutomaticCapture,
  normalizeSubmissionMode,
  normalizeTimeThreshold,
  reorderBosses,
  removeDraftBoss,
  selectExistingArtwork,
  uniqueBossKey,
  uniqueModeKey,
  validatePngFile,
  workflowState,
} = require('../challenge-admin-state');

function draft(state = 'draft', revision = 4, validatedRevision = null) {
  return { state, revision, validated_revision: validatedRevision };
}

function boss(metric = 'time') {
  return {
    metric_type: metric,
    tiers: [1, 2, 3, 4, 5].map((rank) => ({
      rank,
      threshold: rank,
      threshold_display: String(rank),
      metric_type: rank === 1 ? 'completion' : metric,
      operator: rank === 1 ? 'complete' : metric === 'numeric' ? 'gte' : 'lte',
      unit: rank === 1 ? 'boolean' : metric === 'numeric' ? 'waves' : 'milliseconds',
    })),
  };
}

test('edit, save, validate, and edit again gate publishing by revision', () => {
  let state = workflowState({ draft: draft(), dirty: true });
  assert.equal(state.label, 'Unsaved');
  assert.equal(state.canPublish, false);

  state = workflowState({ draft: draft(), dirty: false });
  assert.equal(state.saved, true);
  assert.equal(state.validated, false);
  assert.equal(state.canValidate, true);

  state = workflowState({ draft: draft('validated', 4, 4), dirty: false });
  assert.equal(state.validated, true);
  assert.equal(state.publishable, true);

  state = workflowState({ draft: draft('validated', 4, 4), dirty: true });
  assert.equal(state.unsaved, true);
  assert.equal(state.publishable, false);
});

test('validation is disabled while a save is pending', () => {
  const state = workflowState({ draft: draft(), dirty: true, saving: true });
  assert.equal(state.canValidate, false);
  assert.equal(state.canPublish, false);
});

test('time to numeric conversion resets hidden tier semantics', () => {
  const converted = normalizeMetric(boss('time'), 'numeric');
  assert.deepEqual(converted.tiers.slice(1).map((tier) => tier.threshold), [null, null, null, null]);
  assert.ok(converted.tiers.slice(1).every((tier) => tier.operator === 'gte' && tier.unit === 'waves'));
  assert.equal(converted.tiers[0].operator, 'complete');
});

test('numeric to completion conversion removes meaningless thresholds', () => {
  const converted = normalizeMetric(boss('numeric'), 'completion');
  assert.ok(converted.tiers.every((tier) => tier.threshold === 1));
  assert.ok(converted.tiers.every((tier) => tier.operator === 'complete' && tier.unit === 'boolean'));
});

test('submission modes materialize consistent group semantics', () => {
  const item = { supports_groups: true, min_party_size: 5 };
  normalizeSubmissionMode(item, 'solo');
  assert.deepEqual(item, { submission_mode: 'solo', supports_groups: false, min_party_size: 1 });
  normalizeSubmissionMode(item, 'group');
  assert.equal(item.supports_groups, true); assert.equal(item.min_party_size, 2);
  normalizeSubmissionMode(item, 'either');
  assert.equal(item.supports_groups, true); assert.equal(item.min_party_size, 1);
});

test('time format is materialized only for timed metrics', () => {
  const item = boss('numeric');
  normalizeMetric(item, 'time');
  assert.equal(item.time_input_format, 'HH:MM:SS.xx');
  normalizeMetric(item, 'completion');
  assert.equal(item.time_input_format, null);
});

test('legacy timing editor defaults to unconfigured and manual-only', () => {
  const item = normalizeCaptureMetadata({ metric_type: 'time' });
  assert.equal(item.timing_scope, 'unconfigured');
  assert.equal(item.automatic_capture, 'manual_only');
  assert.equal(captureDefinitionReady(item), false);
});

test('timing and numeric definitions gate automatic capture', () => {
  const timed = normalizeCaptureMetadata({ metric_type: 'time' });
  assert.throws(() => setAutomaticCapture(timed, 'enabled'), /Complete the timing/);
  timed.timing_scope = 'overall';
  assert.equal(captureDefinitionReady(timed), true);
  setAutomaticCapture(timed, 'enabled');
  assert.equal(timed.automatic_capture, 'enabled');

  const segment = { metric_type: 'time', timing_scope: 'segment',
    timing_segment_key: 'final_room', timing_segment_label: 'Final room' };
  assert.equal(captureDefinitionReady(segment), true);
  const numeric = normalizeCaptureMetadata({ metric_type: 'numeric' });
  assert.throws(() => setAutomaticCapture(numeric, 'enabled'), /Complete the timing or numeric/);
  Object.assign(numeric, { numeric_metric_key: 'depth_waves',
    numeric_metric_label: 'Deepest delve', numeric_metric_unit: 'waves' });
  assert.equal(captureDefinitionReady(numeric), true);
});

test('capture metadata survives editor-style JSON draft round trip and appears in review', () => {
  const loaded = normalizeCaptureMetadata({ boss_key: 'fixture', metric_type: 'time' });
  assert.equal(loaded.timing_scope, 'unconfigured');
  loaded.timing_scope = 'segment';
  loaded.timing_segment_key = 'final_room';
  loaded.timing_segment_label = 'Final room';
  setAutomaticCapture(loaded, 'enabled');
  const savedDraft = JSON.parse(JSON.stringify({ bosses: [loaded] }));
  assert.deepEqual(savedDraft.bosses[0], loaded);
  assert.equal(savedDraft.bosses[0].timing_segment_key, 'final_room');
  assert.equal(savedDraft.bosses[0].automatic_capture, 'enabled');
  const lines = friendlyDiffLines({ has_changes: true, bosses: [{
    change_type: 'modified', display_name: 'Fixture', fields: [
      { field: 'timing_scope', before: 'unconfigured', after: 'segment' },
      { field: 'automatic_capture', before: 'manual_only', after: 'enabled' },
    ], tiers: [],
  }] });
  assert.match(lines.join('\n'), /Timing scope/);
  assert.match(lines.join('\n'), /Automatic capture/);
});

test('admin form exposes timing, numeric meaning, capture, and legacy status controls', () => {
  const html = fs.readFileSync(path.join(__dirname, '..', 'challenge-admin.html'), 'utf8');
  for (const id of ['timingScope', 'timingSegmentKey', 'timingSegmentLabel',
    'numericMetricKey', 'numericMetricLabel', 'numericMetricUnit', 'automaticCapture']) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
  assert.match(html, /Unconfigured \/ manual submissions only/);
});

test('configured time input formats normalize to milliseconds and canonical storage', () => {
  assert.deepEqual(normalizeTimeThreshold('17:00.00', 'MM:SS.xx'), {
    milliseconds: 1020000,
    display: '17:00.00',
    internal: '0:17:00.000',
  });
  assert.deepEqual(normalizeTimeThreshold('00:36:21.00', 'HH:MM:SS.xx'), {
    milliseconds: 2181000,
    display: '00:36:21.00',
    internal: '0:36:21.000',
  });
  assert.equal(normalizeTimeThreshold('0:17:00.000', 'MM:SS.xx').display, '17:00.00');
  assert.throws(() => normalizeTimeThreshold('17 minutes', 'MM:SS.xx'), /MM:SS\.xx/);
});

test('drag ordering changes only draft display_order values', () => {
  const config = { bosses: [
    { boss_key: 'one', display_order: 10 },
    { boss_key: 'two', display_order: 20 },
    { boss_key: 'three', display_order: 30 },
  ] };
  assert.equal(reorderBosses(config, 'three', 'one'), true);
  assert.deepEqual(config.bosses.map((item) => item.boss_key), ['three', 'one', 'two']);
  assert.deepEqual(config.bosses.map((item) => item.display_order), [10, 20, 30]);
  assert.equal(reorderBosses(config, 'missing', 'one'), false);
});

test('only unpublished keys are presented as draft-only removable bosses', () => {
  assert.equal(isDraftOnly('temporary', ['gauntlet']), true);
  assert.equal(isDraftOnly('gauntlet', ['gauntlet']), false);
});

test('temporary draft boss can be removed but a published boss cannot', () => {
  const config = { bosses: [{ boss_key: 'gauntlet' }, { boss_key: 'temporary' }] };
  removeDraftBoss(config, 'temporary', ['gauntlet']);
  assert.deepEqual(config.bosses.map((boss) => boss.boss_key), ['gauntlet']);
  assert.throws(
    () => removeDraftBoss(config, 'gauntlet', ['gauntlet']),
    /must be deactivated/,
  );
});

test('boss configurator creates canonical draft-only boss models', () => {
  const created = createBoss({
    bossKey: 'new_encounter', displayName: 'New Encounter',
    metricType: 'numeric', submissionMode: 'group', displayOrder: 160,
  });
  assert.equal(created.boss_key, 'new_encounter');
  assert.equal(created.display_name, 'New Encounter');
  assert.equal(created.comparison_direction, 'higher');
  assert.equal(created.time_input_format, null);
  assert.equal(created.submission_mode, 'group');
  assert.equal(created.supports_groups, true);
  assert.equal(created.min_party_size, 2);
  assert.equal(created.icon_url, null);
  assert.deepEqual(created.tiers.map((tier) => tier.rank), [1, 2, 3, 4, 5]);
  assert.ok(created.tiers.slice(1).every((tier) => tier.operator === 'gte'));
  assert.throws(() => createBoss({ bossKey: 'Not Valid', displayName: 'Bad' }), /Invalid boss key/);
});

test('publish preview renders normalized boss and tier differences', () => {
  const lines = diffLines({
    has_changes: true,
    bosses: [{
      boss_key: 'gauntlet', display_name: 'Corrupted Gauntlet', change_type: 'modified',
      fields: [{ field: 'active', before: true, after: false }],
      tiers: [{
        tier_key: 'gold', change_type: 'modified',
        fields: [{ field: 'points', before: 30, after: 35 }],
      }],
    }],
    system_tiers: [],
  });
  assert.match(lines.join('\n'), /modified: Corrupted Gauntlet/);
  assert.match(lines.join('\n'), /active: true → false/);
  assert.match(lines.join('\n'), /points: 30 → 35/);
  assert.deepEqual(diffLines({ has_changes: false }), ['No semantic changes.']);
});

test('add-boss wizard derives stable unique keys without showing them to admins', () => {
  assert.equal(uniqueBossKey("Phosani's Nightmare", []), 'phosanis_nightmare');
  assert.equal(uniqueBossKey('New Boss', ['new_boss', 'new_boss_2']), 'new_boss_3');
  assert.throws(() => uniqueBossKey('---', []), /Challenge name/);
});

test('browser PNG validation checks filename, MIME, size, signature, and IHDR', async () => {
  const bytes = Uint8Array.from([
    137,80,78,71,13,10,26,10, 0,0,0,13, 73,72,68,82,
    0,0,0,64, 0,0,0,64,
  ]);
  const file = (overrides = {}) => ({
    name: 'boss.png', type: 'image/png', size: bytes.length,
    slice: () => ({ arrayBuffer: async () => bytes.buffer }),
    ...overrides,
  });
  assert.equal(await validatePngFile(file()), null);
  assert.match(await validatePngFile(file({ name: 'boss.webp' })), /\.png filename/);
  assert.match(await validatePngFile(file({ type: 'image/webp' })), /renamed JPG or WebP/);
  const bad = Uint8Array.from(bytes); bad[1] = 0;
  assert.match(await validatePngFile(file({ slice: () => ({ arrayBuffer: async () => bad.buffer }) })), /not a valid PNG/);
});

test('selecting existing artwork stores its canonical URL without an upload', () => {
  const item = { boss_key: 'new_boss', icon_url: null };
  selectExistingArtwork(item, '/media/boss_icons/Theatre_of_blood.png');
  assert.equal(item.icon_url, '/media/boss_icons/Theatre_of_blood.png');
  assert.throws(
    () => selectExistingArtwork(item, '/media/challenge-bosses/copied.png'),
    /available icon catalog/,
  );
});

test('review output uses ordinary admin language', () => {
  const lines = friendlyDiffLines({has_changes:true,bosses:[{
    change_type:'modified',display_name:'Gauntlet',fields:[
      {field:'submission_mode',before:'either',after:'solo'},
      {field:'icon_url',before:null,after:'/media/example.png'},
    ],tiers:[{tier_key:'gold',fields:[{field:'points',before:30,after:35}]}],
  }]});
  assert.match(lines.join('\n'), /Who can submit/);
  assert.match(lines.join('\n'), /Boss artwork/);
  assert.match(lines.join('\n'), /Gold Points/);
  assert.doesNotMatch(lines.join('\n'), /submission_mode|icon_url/);
});

test('leaderboard modes support challenge-backed and leaderboard-only configuration', () => {
  const challenge = {
    boss_key: 'cm_3', metric_type: 'time', comparison_direction: 'lower',
  };
  const backed = createLeaderboardMode({
    modeKey: 'cox_cm_trio', boss: challenge,
    displayName: 'Chambers of Xeric CM: Trio', partySizeMin: 3, partySizeMax: 3,
  });
  assert.equal(backed.boss_key, 'cm_3');
  assert.equal(backed.inherit_boss_icon, true);
  assert.equal(backed.metric_unit, 'milliseconds');
  const only = createLeaderboardMode({
    modeKey: 'tob_solo', displayName: 'Theatre of Blood: Solo',
    contentKey: 'theatre_of_blood', partySizeMin: 1,
  });
  assert.equal(only.boss_key, null);
  assert.equal(only.inherit_boss_icon, false);
  assert.equal(uniqueModeKey('Tob Solo', ['tob_solo']), 'tob_solo_2');
});

test('review output includes leaderboard mode changes', () => {
  const lines = friendlyDiffLines({
    has_changes: true, bosses: [], system_tiers: [],
    leaderboard_modes: [{
      change_type: 'modified', display_name: 'Theatre of Blood: Trio',
      fields: [{field: 'party_size_min', before: 2, after: 3}],
    }],
  });
  assert.match(lines.join('\n'), /Update leaderboard: Theatre of Blood: Trio/);
  assert.match(lines.join('\n'), /party size min/);
});
