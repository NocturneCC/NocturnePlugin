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

	private BossCompletionContext(String bossKey, String activityKey, String modeKey, String timingScope, String reporterRsn,
		GroupSnapshot roster, String encounterIdentity, long completionNanos, long gameCycle, Instant occurredAt,
		PluginActivityCatalog.Entry activity)
	{
		this.bossKey = bossKey; this.activityKey = activityKey; this.modeKey = modeKey;
		this.timingScope = timingScope;
		this.reporterRsn = reporterRsn; this.roster = roster; this.encounterIdentity = encounterIdentity;
		this.completionNanos = completionNanos; this.gameCycle = gameCycle; this.occurredAt = occurredAt;
		this.activity = activity;
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
		if (activity == null || !activity.ttkTrackingEnabled || activity.challengeBossKey == null
			|| !"overall".equals(activity.timingScope) || !activity.supportsSolo) return null;
		String encounter = clientSessionId + "|" + activity.challengeBossKey + "|"
			+ reporter.trim().toLowerCase(java.util.Locale.ROOT) + "|" + cycle;
		GroupSnapshot solo = new GroupSnapshot("local reporter", List.of(reporter), 1,
			GroupSnapshot.Status.MATCHED, "solo encounter");
		return new BossCompletionContext(activity.challengeBossKey, activity.activityKey,
			activity.modeKey, activity.timingScope,
			reporter, solo, encounter, nowNanos, cycle, now, activity);
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
			activity.timingScope, reporter, roster, encounter, nowNanos, cycle, now, activity);
	}
}
