'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const { ChallengeConfigManager, parseApprovalMetadata, selectMenuEmoji } = require('../utils/challengeConfig');

function tier(key, rank, display, points = rank * 10) {
  return { tier_key: key, tier: key[0].toUpperCase() + key.slice(1), rank,
    threshold: rank === 1 ? 1 : rank * 1000, threshold_display: display,
    metric_type: rank === 1 ? 'completion' : 'time', operator: rank === 1 ? 'complete' : 'lte',
    unit: rank === 1 ? 'boolean' : 'milliseconds', points };
}
function document(version = 7) {
  const tiers = [tier('bronze', 1, 'Completion'), tier('silver', 2, '0:10:00'),
    tier('gold', 3, '0:09:00'), tier('platinum', 4, '0:08:00'), tier('ascendant', 5, '0:07:00')];
  return { ok: true, version_id: version, version_key: `v${version}`, bosses: [
    { boss_key: 'active_boss', display_name: 'Renamed Boss', active: true, submission_enabled: true,
      display_order: 20, metric_type: 'time', supports_groups: true,
      submission_mode: 'group', min_party_size: 3, time_input_format: 'MM:SS.xx',
      aliases: ['Old Boss'], tiers },
    { boss_key: 'inactive_boss', display_name: 'Inactive', active: false, submission_enabled: false,
      display_order: 10, metric_type: 'time', tiers },
  ] };
}
function response(payload, ok = true) { return { ok, status: ok ? 200 : 503, async json() { return payload; } }; }

test('startup fetch builds dynamic menu from active submission-enabled bosses', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'challenge-config-'));
  const manager = new ChallengeConfigManager({ fetchImpl: async () => response(document()), cachePath: path.join(dir, 'cache.json'), refreshMs: 0, logger: {} });
  await manager.initialize();
  const bosses = manager.getSubmissionBosses();
  assert.equal(bosses.length, 1); assert.equal(bosses[0].name, 'Renamed Boss');
  assert.equal(bosses[0].bossKey, 'active_boss'); assert.equal(bosses[0].minPartySize, 3);
  assert.equal(bosses[0].submissionMode, 'group'); assert.equal(bosses[0].supportsGroups, true);
  assert.equal(bosses[0].timeInputFormat, 'MM:SS.xx');
  assert.equal(bosses[0].tiers[1].target, '0:10:00'); assert.equal(bosses[0].tiers[1].points, 20);
});

test('refresh failure retains last-known-good in memory', async () => {
  let fail = false; const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'challenge-config-'));
  const manager = new ChallengeConfigManager({ fetchImpl: async () => { if (fail) throw new Error('offline'); return response(document(8)); }, cachePath: path.join(dir, 'cache.json'), refreshMs: 0, logger: {} });
  await manager.initialize(); fail = true;
  await assert.rejects(manager.refresh()); assert.equal(manager.versionId, 8); assert.equal(manager.getSubmissionBosses()[0].name, 'Renamed Boss');
});

test('startup uses persisted last-known-good when endpoint fails', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'challenge-config-')); const cache = path.join(dir, 'cache.json');
  fs.writeFileSync(cache, JSON.stringify(document(9)), { mode: 0o600 });
  const manager = new ChallengeConfigManager({ fetchImpl: async () => { throw new Error('offline'); }, cachePath: cache, refreshMs: 0, logger: {} });
  await manager.initialize(); assert.equal(manager.versionId, 9);
});

test('startup refuses hard-coded fallback when endpoint and cache are invalid', async () => {
  const manager = new ChallengeConfigManager({ fetchImpl: async () => { throw new Error('offline'); }, cachePath: '/tmp/no-such-challenge-cache.json', refreshMs: 0, logger: {} });
  await assert.rejects(manager.initialize(), /No valid published/);
});

test('stable approval metadata survives display rename and legacy remains parseable', () => {
  assert.deepEqual(parseApprovalMetadata({ footer: { text: 'challenge-config:7:active_boss' } }), { configVersionId: 7, bossKey: 'active_boss' });
  assert.equal(parseApprovalMetadata({ fields: [{ name: 'Boss', value: 'Old Boss' }] }), null);
});

test('same boss tier can change only through fetched published config', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'challenge-config-')); let payload = document(10);
  const manager = new ChallengeConfigManager({ fetchImpl: async () => response(payload), cachePath: path.join(dir, 'cache.json'), refreshMs: 0, logger: {} });
  await manager.initialize(); assert.equal(manager.getSubmissionBosses()[0].tiers[1].points, 20);
  payload = document(11); payload.bosses[0].tiers[1].points = 27; await manager.refresh();
  assert.equal(manager.versionId, 11); assert.equal(manager.getSubmissionBosses()[0].tiers[1].points, 27);
});

test('completion metric remains completion in the Discord model', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'challenge-config-'));
  const payload = document(12);
  payload.bosses[0].metric_type = 'completion';
  payload.bosses[0].tiers = payload.bosses[0].tiers.map((item) => ({
    ...item, threshold: 1, threshold_display: 'Completion',
    metric_type: 'completion', operator: 'complete', unit: 'boolean',
  }));
  const manager = new ChallengeConfigManager({
    fetchImpl: async () => response(payload),
    cachePath: path.join(dir, 'cache.json'), refreshMs: 0, logger: {},
  });
  await manager.initialize();
  const boss = manager.getSubmissionBosses()[0];
  assert.equal(boss.inputType, 'completion');
  assert.equal(boss.tiers[4].target, 'Completion');
});

test('select menu omits a custom emoji unavailable to the bot', () => {
  const client = { guilds: { cache: { some: (predicate) => [
    { emojis: { cache: new Map([['111111111111111111', {}]]) } },
  ].some(predicate) } } };
  assert.equal(selectMenuEmoji('<:known:111111111111111111>', client), '<:known:111111111111111111>');
  assert.equal(selectMenuEmoji('<:missing:222222222222222222>', client), undefined);
});

test('select menu keeps Unicode emoji and rejects custom emoji without a cache', () => {
  assert.equal(selectMenuEmoji('🛡️', {}), '🛡️');
  assert.equal(selectMenuEmoji('<:missing:222222222222222222>', {}), undefined);
  assert.equal(selectMenuEmoji(null, {}), undefined);
});
