'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const { withDoomCompletion, applyCompletionControls } = require('../website/plugin-tracking.js');

function catalogWithoutDoom() {
  return {
    schema_version: 1,
    activities: [{
      activity_key: 'phosanis_nightmare', display_name: "Phosani's Nightmare", aliases: [],
      modes: [{
        mode_key: 'phosani_nightmare', display_name: 'Solo', source_names: ["Phosani's Nightmare"],
        supports_solo: true, supports_group: false, loot_tracking_enabled: true,
        ttk_tracking_enabled: true, timing_scope: 'overall', challenge_boss_key: 'phosanis',
        challenge_mode_key: 'phosani_nightmare', challenge_content_key: 'phosani_nightmare'
      }]
    }]
  };
}

function doomMode(document) {
  return document.activities.find(activity => activity.activity_key === 'doom_of_mokhaiotl')
    .modes.find(mode => mode.mode_key === 'doom_of_mokhaiotl');
}

test('staff can add Doom with its numeric completion metric without editing catalog JSON', () => {
  const result = withDoomCompletion(catalogWithoutDoom());
  const mode = doomMode(result);
  assert.equal(mode.completion_tracking_enabled, true);
  assert.equal(mode.completion_metric_key, 'deepest_level');
  assert.equal(mode.completion_metric_label, 'Deepest level');
  assert.equal(mode.completion_metric_unit, 'level');
  assert.equal(mode.ttk_tracking_enabled, false);
  assert.equal(mode.challenge_boss_key, null);
});

test('enable action updates an existing Doom mode without duplicating its activity', () => {
  const first = withDoomCompletion(catalogWithoutDoom());
  const mode = doomMode(first);
  mode.completion_tracking_enabled = false;
  mode.completion_metric_key = null;
  mode.completion_metric_label = null;
  mode.completion_metric_unit = null;
  const enabled = withDoomCompletion(first);
  assert.equal(enabled.activities.filter(activity => activity.activity_key === 'doom_of_mokhaiotl').length, 1);
  assert.equal(doomMode(enabled).completion_tracking_enabled, true);
});

test('numeric controls write enabled metric fields and clear them when disabled', () => {
  const catalog = withDoomCompletion(catalogWithoutDoom());
  const updated = applyCompletionControls(catalog, [{
    activityKey: 'doom_of_mokhaiotl', modeKey: 'doom_of_mokhaiotl', enabled: true,
    metricKey: 'highest_floor', metricLabel: 'Highest floor', metricUnit: 'floor'
  }]);
  assert.deepEqual([
    doomMode(updated).completion_tracking_enabled,
    doomMode(updated).completion_metric_key,
    doomMode(updated).completion_metric_label,
    doomMode(updated).completion_metric_unit
  ], [true, 'highest_floor', 'Highest floor', 'floor']);

  const disabled = applyCompletionControls(updated, [{
    activityKey: 'doom_of_mokhaiotl', modeKey: 'doom_of_mokhaiotl', enabled: false,
    metricKey: 'ignored', metricLabel: 'ignored', metricUnit: 'ignored'
  }]);
  assert.deepEqual([
    doomMode(disabled).completion_tracking_enabled,
    doomMode(disabled).completion_metric_key,
    doomMode(disabled).completion_metric_label,
    doomMode(disabled).completion_metric_unit
  ], [false, null, null, null]);
});

test('HTML exposes completion controls and Add Doom outside the advanced JSON editor', () => {
  const fs = require('node:fs');
  const path = require('node:path');
  const html = fs.readFileSync(path.join(__dirname, '../website/plugin-tracking.html'), 'utf8');
  assert.match(html, /id="enable-doom"/);
  assert.match(html, /id="completion-controls"/);
  assert.match(html, /<details>/);
});
