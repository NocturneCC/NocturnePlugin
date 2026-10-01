'use strict';

const TIME_FORMATS = new Set(['MM:SS.xx', 'HH:MM:SS.xx']);

function parseTimeMetric(text, format) {
  const value = String(text || '').trim();
  let match;
  if (format === 'MM:SS.xx') {
    match = value.match(/^(\d{2}):([0-5]\d)\.(\d{2})$/);
    if (!match) throw new Error('time must use MM:SS.xx');
    return ((Number(match[1]) * 60 + Number(match[2])) * 1000)
      + Number(match[3]) * 10;
  }
  if (format === 'HH:MM:SS.xx') {
    match = value.match(/^(\d{2}):([0-5]\d):([0-5]\d)\.(\d{2})$/);
    if (!match) throw new Error('time must use HH:MM:SS.xx');
    return ((Number(match[1]) * 3600 + Number(match[2]) * 60
      + Number(match[3])) * 1000) + Number(match[4]) * 10;
  }
  throw new Error('unsupported time input format');
}

function formatTimeMetric(milliseconds, format) {
  const value = Number(milliseconds);
  if (!Number.isInteger(value) || value < 0 || !TIME_FORMATS.has(format)) {
    throw new Error('invalid normalized time');
  }
  const centiseconds = Math.floor((value % 1000) / 10);
  const totalSeconds = Math.floor(value / 1000);
  const seconds = totalSeconds % 60;
  if (format === 'MM:SS.xx') {
    const minutes = Math.floor(totalSeconds / 60);
    return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}.${String(centiseconds).padStart(2, '0')}`;
  }
  const minutes = Math.floor(totalSeconds / 60) % 60;
  const hours = Math.floor(totalSeconds / 3600);
  return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}.${String(centiseconds).padStart(2, '0')}`;
}

function calculateChallengeTier(challenge, submittedMetric) {
  const tiers = [...(challenge.tiers || [])].sort((a, b) => Number(a.rank) - Number(b.rank));
  if (!tiers.length) throw new Error('challenge has no configured tiers');
  let metricValue;
  let metricDisplay;
  if (challenge.inputType === 'completion') {
    metricValue = 1;
    metricDisplay = 'Completion';
  } else if (challenge.inputType === 'wave') {
    if (!/^\d+$/.test(String(submittedMetric || '').trim())) {
      throw new Error('wave count must be a non-negative integer');
    }
    metricValue = Number(submittedMetric);
    metricDisplay = String(metricValue);
  } else {
    metricDisplay = String(submittedMetric || '').trim();
    metricValue = parseTimeMetric(metricDisplay, challenge.timeInputFormat);
  }

  const qualifying = tiers.filter((tier) => {
    if (Number(tier.rank) === 1) return true;
    if (challenge.inputType === 'completion') return false;
    if (tier.operator === 'lte') return metricValue <= Number(tier.threshold);
    if (tier.operator === 'gte') return metricValue >= Number(tier.threshold);
    return false;
  });
  const tier = qualifying.at(-1) || tiers[0];
  const requirement = Number(tier.rank) === 1
    ? 'Completion'
    : `${tier.operator === 'lte' ? '<=' : '>='} ${challenge.inputType === 'time'
      ? formatTimeMetric(Number(tier.threshold), challenge.timeInputFormat)
      : tier.target}`;
  return {
    tier,
    metricType: challenge.inputType === 'wave' ? 'numeric' : challenge.inputType,
    metricValue,
    metricDisplay,
    requirement,
  };
}

function submissionPartyPolicy(challenge) {
  const mode = challenge.submissionMode;
  if (!['solo', 'group', 'either'].includes(mode)) {
    throw new Error('unsupported submission mode');
  }
  return {
    mode,
    promptForParty: mode !== 'solo',
    allowAdditionalParty: mode !== 'solo',
    minPartySize: mode === 'group' ? Math.max(2, Number(challenge.minPartySize) || 2) : 1,
  };
}

module.exports = {
  TIME_FORMATS,
  calculateChallengeTier,
  formatTimeMetric,
  parseTimeMetric,
  submissionPartyPolicy,
};
