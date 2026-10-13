package com.nocturne;

import java.time.Instant;
import java.util.List;

/** Immutable encounter identity supplied by existing loot/raid tracking, never by timing text. */
final class BossCompletionContext
{
	final String bossKey;
	final String activityKey;
	final String modeKey;
	final String timingScope;
	final String reporterRsn;
	final GroupSnapshot roster;
	final String encounterIdentity;
	final long completionNanos;
	final long gameCycle;
	final Instant occurredAt;
	final PluginActivityCatalog.Entry activity;
	final String triggerLootRecordId;

	private BossCompletionContext(String bossKey, String activityKey, String modeKey, String timingScope, String reporterRsn,
		GroupSnapshot roster, String encounterIdentity, long completionNanos, long gameCycle, Instant occurredAt,
		PluginActivityCatalog.Entry activity, String triggerLootRecordId)
	{
		this.bossKey = bossKey; this.activityKey = activityKey; this.modeKey = modeKey;
		this.timingScope = timingScope;
		this.reporterRsn = reporterRsn; this.roster = roster; this.encounterIdentity = encounterIdentity;
		this.completionNanos = completionNanos; this.gameCycle = gameCycle; this.occurredAt = occurredAt;
		this.activity = activity;
		this.triggerLootRecordId = triggerLootRecordId;
	}

	static BossCompletionContext fromNpcLoot(String source, String reporter, long cycle, long nowNanos, Instant now)
	{
		return fromNpcLoot(source, reporter, "fixture-session", cycle, nowNanos, now);
	}

	static BossCompletionContext fromNpcLoot(String source, String reporter, String clientSessionId,
		long cycle, long nowNanos, Instant now)
	{
		if (source == null || reporter == null || clientSessionId == null || now == null) return null;
		PluginActivityCatalog.Entry activity = PluginActivityCatalog.matchSource(source);
		if (activity == null || activity.challengeBossKey == null) return null;
		return fromActiveSoloActivity(activity, reporter, clientSessionId, cycle, nowNanos, now);
	}

	static BossCompletionContext fromNpcLoot(String source, String reporter, String clientSessionId,
		long cycle, long nowNanos, Instant now, String lootRecordId)
	{
		BossCompletionContext base = fromNpcLoot(source, reporter, clientSessionId, cycle, nowNanos, now);
		if (base == null || lootRecordId == null) return base;
		return new BossCompletionContext(base.bossKey, base.activityKey, base.modeKey,
			base.timingScope, base.reporterRsn, base.roster, base.encounterIdentity,
			base.completionNanos, base.gameCycle, base.occurredAt, base.activity, lootRecordId);
	}

	static BossCompletionContext fromActiveSoloActivity(String activityKey, String modeKey,
		String reporter, String clientSessionId, long cycle, long nowNanos, Instant now)
	{
		return fromActiveSoloActivity(PluginActivityCatalog.find(activityKey, modeKey), reporter,
			clientSessionId, cycle, nowNanos, now);
	}

	private static BossCompletionContext fromActiveSoloActivity(PluginActivityCatalog.Entry activity,
		String reporter, String clientSessionId, long cycle, long nowNanos, Instant now)
	{
		if (activity == null || reporter == null || clientSessionId == null || now == null
			|| !activity.ttkTrackingEnabled
			|| !"overall".equals(activity.timingScope) || !activity.supportsSolo) return null;
		String encounter = clientSessionId + "|" + activity.activityKey + "|"
			+ reporter.trim().toLowerCase(java.util.Locale.ROOT) + "|" + cycle;
		GroupSnapshot solo = new GroupSnapshot("local reporter", List.of(reporter), 1,
			GroupSnapshot.Status.MATCHED, "solo encounter");
		return new BossCompletionContext(activity.challengeBossKey, activity.activityKey,
			activity.modeKey, activity.timingScope,
			reporter, solo, encounter, nowNanos, cycle, now, activity, null);
	}

	static BossCompletionContext fromTrackedActivity(String source, String reporter, GroupSnapshot roster,
		String clientSessionId, long cycle, long nowNanos, Instant now)
	{
		PluginActivityCatalog.Entry activity = PluginActivityCatalog.matchSource(source);
		if (activity == null || !activity.lootTrackingEnabled || !activity.ttkTrackingEnabled
			|| !"overall".equals(activity.timingScope) || reporter == null || now == null
			|| clientSessionId == null || roster == null || roster.status != GroupSnapshot.Status.MATCHED
			|| roster.names.size() != roster.expectedSize || roster.expectedSize < 1 || roster.expectedSize > 10
			|| roster.expectedSize == 1 && !activity.supportsSolo
			|| roster.expectedSize > 1 && !activity.supportsGroup) return null;
		String encounter = clientSessionId + "|" + activity.activityKey + "|"
			+ reporter.trim().toLowerCase(java.util.Locale.ROOT) + "|" + cycle;
		return new BossCompletionContext(activity.challengeBossKey, activity.activityKey, activity.modeKey,
			activity.timingScope, reporter, roster, encounter, nowNanos, cycle, now, activity, null);
	}

	static BossCompletionContext fromTrackedActivity(String source, String reporter, GroupSnapshot roster,
		String clientSessionId, long cycle, long nowNanos, Instant now, String lootRecordId)
	{
		BossCompletionContext base = fromTrackedActivity(source, reporter, roster, clientSessionId,
			cycle, nowNanos, now);
		if (base == null || lootRecordId == null) return base;
		return new BossCompletionContext(base.bossKey, base.activityKey, base.modeKey,
			base.timingScope, base.reporterRsn, base.roster, base.encounterIdentity,
			base.completionNanos, base.gameCycle, base.occurredAt, base.activity, lootRecordId);
	}
}
