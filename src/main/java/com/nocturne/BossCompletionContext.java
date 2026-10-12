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

	private BossCompletionContext(String bossKey, String activityKey, String modeKey, String timingScope, String reporterRsn,
		GroupSnapshot roster, String encounterIdentity, long completionNanos, long gameCycle, Instant occurredAt)
	{
		this.bossKey = bossKey; this.activityKey = activityKey; this.modeKey = modeKey;
		this.timingScope = timingScope;
		this.reporterRsn = reporterRsn; this.roster = roster; this.encounterIdentity = encounterIdentity;
		this.completionNanos = completionNanos; this.gameCycle = gameCycle; this.occurredAt = occurredAt;
	}

	static BossCompletionContext fromNpcLoot(String source, String reporter, long cycle, long nowNanos, Instant now)
	{
		return fromNpcLoot(source, reporter, "fixture-session", cycle, nowNanos, now);
	}

	static BossCompletionContext fromNpcLoot(String source, String reporter, String clientSessionId,
		long cycle, long nowNanos, Instant now)
	{
		if (source == null || reporter == null || clientSessionId == null || now == null) return null;
		String normalized = source.replace('\u2019', '\'').replace('\u2018', '\'').trim();
		if (!"phosani's nightmare".equalsIgnoreCase(normalized)) return null;
		String encounter = clientSessionId + "|phosanis|"
			+ reporter.trim().toLowerCase(java.util.Locale.ROOT) + "|" + cycle;
		GroupSnapshot solo = new GroupSnapshot("local reporter", List.of(reporter), 1,
			GroupSnapshot.Status.MATCHED, "solo encounter");
		return new BossCompletionContext("phosanis", "phosanis_nightmare", "phosani_nightmare", "overall",
			reporter, solo, encounter, nowNanos, cycle, now);
	}
}
