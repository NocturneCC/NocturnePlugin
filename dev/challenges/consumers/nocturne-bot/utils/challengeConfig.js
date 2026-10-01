'use strict';

const fs = require('fs');
const path = require('path');

const DEFAULT_URL = 'https://nocturne.events/api/challenges/config/active';
const DEFAULT_CACHE = path.join(__dirname, '..', 'db', 'challenge_config_lkg.json');
const CUSTOM_EMOJI = /^<a?:[A-Za-z0-9_]{2,32}:(\d{17,20})>$/;

function selectMenuEmoji(rawEmoji, client) {
  const value = typeof rawEmoji === 'string' ? rawEmoji.trim() : '';
  if (!value) return undefined;
  const custom = value.match(CUSTOM_EMOJI);
  if (!custom) return value;
  const guilds = client?.guilds?.cache;
  if (!guilds || typeof guilds.some !== 'function') return undefined;
  const available = guilds.some((guild) => guild?.emojis?.cache?.has?.(custom[1]));
  return available ? value : undefined;
}

class ChallengeConfigManager {
  constructor({ fetchImpl = global.fetch, endpoint = process.env.MIDGARD_CHALLENGE_CONFIG_URL || DEFAULT_URL,
    cachePath = process.env.MIDGARD_CHALLENGE_CONFIG_CACHE || DEFAULT_CACHE,
    refreshMs = Number(process.env.MIDGARD_CHALLENGE_CONFIG_REFRESH_MS || 300000), logger = console } = {}) {
    this.fetchImpl = fetchImpl;
    this.endpoint = endpoint;
    this.cachePath = cachePath;
    this.refreshMs = refreshMs;
    this.logger = logger;
    this.current = null;
    this.timer = null;
  }

  validate(document) {
    if (!document || document.ok === false || !Number.isInteger(Number(document.version_id)) || !Array.isArray(document.bosses)) {
      throw new Error('invalid challenge config document');
    }
    const keys = new Set();
    for (const boss of document.bosses) {
      if (!/^[a-z0-9]+(?:_[a-z0-9]+)*$/.test(boss.boss_key || '') || keys.has(boss.boss_key)) {
        throw new Error('invalid or duplicate challenge boss key');
      }
      keys.add(boss.boss_key);
      if (!['time', 'numeric', 'completion'].includes(boss.metric_type) || !Array.isArray(boss.tiers) || boss.tiers.length !== 5) {
        throw new Error(`invalid challenge definition: ${boss.boss_key}`);
      }
      for (const tier of boss.tiers) {
        if (!Number.isInteger(Number(tier.rank)) || Number(tier.rank) < 1 || Number(tier.rank) > 5
          || !Number.isInteger(Number(tier.points)) || Number(tier.points) < 0) {
          throw new Error(`invalid challenge tier: ${boss.boss_key}`);
        }
      }
    }
    return document;
  }

  async fetchActive() {
    const destination = new URL(this.endpoint);
    if (!['https:', 'http:'].includes(destination.protocol)) throw new Error('invalid challenge config endpoint');
    const response = await this.fetchImpl(destination, {
      method: 'GET', redirect: 'error', signal: AbortSignal.timeout(5000),
      headers: { Accept: 'application/json' },
    });
    if (!response.ok) throw new Error(`challenge config HTTP ${response.status}`);
    return this.validate(await response.json());
  }

  async loadCache() {
    const parsed = JSON.parse(await fs.promises.readFile(this.cachePath, 'utf8'));
    return this.validate(parsed);
  }

  async persist(document) {
    await fs.promises.mkdir(path.dirname(this.cachePath), { recursive: true, mode: 0o700 });
    const temporary = `${this.cachePath}.tmp-${process.pid}`;
    await fs.promises.writeFile(temporary, `${JSON.stringify(document)}\n`, { mode: 0o600 });
    await fs.promises.rename(temporary, this.cachePath);
  }

  async refresh() {
    const next = await this.fetchActive();
    await this.persist(next);
    this.current = next;
    this.logger.info?.(`[challenge-config] active version ${next.version_id}`);
    return next;
  }

  async initialize() {
    try {
      await this.refresh();
    } catch (networkError) {
      try {
        this.current = await this.loadCache();
        this.logger.warn?.(`[challenge-config] endpoint unavailable; using last-known-good version ${this.current.version_id}`);
      } catch (cacheError) {
        throw new Error('No valid published Challenge configuration or last-known-good cache is available');
      }
    }
    if (this.refreshMs > 0) {
      this.timer = setInterval(() => {
        this.refresh().catch(() => this.logger.warn?.('[challenge-config] refresh failed; retaining last-known-good configuration'));
      }, this.refreshMs);
      this.timer.unref?.();
    }
    return this.current;
  }

  stop() { if (this.timer) clearInterval(this.timer); this.timer = null; }

  get versionId() { return Number(this.requireCurrent().version_id); }
  requireCurrent() { if (!this.current) throw new Error('Challenge configuration is not initialized'); return this.current; }

  getSubmissionBosses() {
    return this.requireCurrent().bosses
      .filter((boss) => boss.active && boss.submission_enabled)
      .sort((a, b) => Number(a.display_order) - Number(b.display_order))
      .map((boss) => this.toBotChallenge(boss));
  }

  getBossByKey(key) {
    const boss = this.requireCurrent().bosses.find((item) => item.boss_key === key && item.active && item.submission_enabled);
    return boss ? this.toBotChallenge(boss) : null;
  }

  toBotChallenge(boss) {
    return {
      bossKey: boss.boss_key, configVersionId: this.versionId,
      name: boss.display_name, emoji: boss.discord_emoji || undefined,
      aliases: Array.isArray(boss.aliases) ? boss.aliases.slice() : [],
      comparisonDirection: boss.comparison_direction,
      inputType: boss.metric_type === 'numeric' ? 'wave' : boss.metric_type,
      supportsGroups: Boolean(boss.supports_groups),
      submissionMode: boss.submission_mode || (
        !boss.supports_groups ? 'solo' : Number(boss.min_party_size || 1) >= 2 ? 'group' : 'either'
      ),
      minPartySize: Number(boss.min_party_size || 1),
      timeInputFormat: boss.metric_type === 'time'
        ? (boss.time_input_format || 'HH:MM:SS.xx') : null,
      tiers: boss.tiers.slice().sort((a, b) => Number(a.rank) - Number(b.rank)).map((tier) => ({
        key: tier.tier_key, name: tier.tier, emoji: tier.discord_emoji || undefined,
        target: tier.threshold_display, threshold: tier.threshold, operator: tier.operator,
        points: Number(tier.points), rank: Number(tier.rank),
      })),
    };
  }
}

const manager = new ChallengeConfigManager();

function parseApprovalMetadata(embed) {
  const match = String(embed?.footer?.text || '').match(/^challenge-config:(\d+):([a-z0-9_]+)$/);
  return match ? { configVersionId: Number(match[1]), bossKey: match[2] } : null;
}

module.exports = { ChallengeConfigManager, manager, parseApprovalMetadata, selectMenuEmoji };
