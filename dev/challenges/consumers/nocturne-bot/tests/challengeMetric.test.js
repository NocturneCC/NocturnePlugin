'use strict';

const assert = require('node:assert/strict');
const test = require('node:test');
const {
  calculateChallengeTier,
  parseTimeMetric,
  submissionPartyPolicy,
} = require('../utils/challengeMetric');

function gauntlet() {
  return {
    inputType: 'time', timeInputFormat: 'MM:SS.xx',
    tiers: [
      { key: 'bronze', name: 'Bronze', rank: 1, threshold: 1, operator: 'complete', target: 'Completion', points: 10 },
      { key: 'silver', name: 'Silver', rank: 2, threshold: 510000, operator: 'lte', target: '0:08:30', points: 20 },
      { key: 'gold', name: 'Gold', rank: 3, threshold: 450000, operator: 'lte', target: '0:07:30', points: 30 },
      { key: 'platinum', name: 'Platinum', rank: 4, threshold: 390000, operator: 'lte', target: '0:06:30', points: 40 },
      { key: 'ascendant', name: 'Ascendant', rank: 5, threshold: 360000, operator: 'lte', target: '0:06:00', points: 50 },
    ],
  };
}

test('configured MM:SS.xx accepts Gauntlet values and calculates authoritative tier', () => {
  assert.equal(parseTimeMetric('07:45.00', 'MM:SS.xx'), 465000);
  assert.equal(parseTimeMetric('09:30.00', 'MM:SS.xx'), 570000);
  assert.equal(calculateChallengeTier(gauntlet(), '07:24.00').tier.key, 'gold');
  assert.equal(calculateChallengeTier(gauntlet(), '07:45.00').tier.key, 'silver');
  assert.equal(calculateChallengeTier(gauntlet(), '09:30.00').tier.key, 'bronze');
});

test('configured time formats reject malformed or wrong-shape values', () => {
  for (const value of ['7:45.00', '07:60.00', '00:07:45.00', '07:45', 'text']) {
    assert.throws(() => parseTimeMetric(value, 'MM:SS.xx'));
  }
  assert.equal(parseTimeMetric('00:36:21.00', 'HH:MM:SS.xx'), 2181000);
  assert.throws(() => parseTimeMetric('36:21.00', 'HH:MM:SS.xx'));
});

test('solo skips party prompt; group requires it; either makes it optional', () => {
  const boss = gauntlet();
  boss.submissionMode = 'solo';
  assert.deepEqual(submissionPartyPolicy(boss), {
    mode: 'solo', promptForParty: false, allowAdditionalParty: false, minPartySize: 1,
  });
  boss.submissionMode = 'group'; boss.minPartySize = 3;
  assert.deepEqual(submissionPartyPolicy(boss), {
    mode: 'group', promptForParty: true, allowAdditionalParty: true, minPartySize: 3,
  });
  boss.submissionMode = 'either'; boss.minPartySize = 1;
  assert.deepEqual(submissionPartyPolicy(boss), {
    mode: 'either', promptForParty: true, allowAdditionalParty: true, minPartySize: 1,
  });
});
