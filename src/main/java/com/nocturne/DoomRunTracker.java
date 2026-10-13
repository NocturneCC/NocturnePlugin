package com.nocturne;

import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.HashSet;
import java.util.Set;
import java.util.UUID;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import net.runelite.client.util.Text;

/** Tracks confirmed Doom floor completions, never the currently entered floor. */
final class DoomRunTracker
{
	enum EndAction { CLAIM_REWARDS, LEAVE_DELVING }
	static final int MAX_LEVEL = 10_000;
	private static final Pattern COMPLETE = Pattern.compile(
		"(?i)^level\\s+([1-9][0-9]{0,3})\\s+complete[.!]?$" );
	private static final Pattern DEATH = Pattern.compile(
		"(?i)^(?:oh dear, you are dead!?|you have died\\.?|you are dead\\.?)$");
	private long runSequence;
	private Run active;
	private Result finalizedForLoot;
	private String claimedLootEventId;
	private boolean claimProcessed;
	private String lastSignalSession;
	private int lastSignalLevel;
	private Instant lastSignalAt;

	static Integer completedLevel(String raw)
	{
		if (raw == null || raw.length() > 256) return null;
		String text = Text.removeTags(raw).replaceAll("(?i)&nbsp;", " ")
			.replace('\u00a0', ' ').replaceAll("\\s+", " ").trim();
		Matcher match = COMPLETE.matcher(text);
		if (!match.matches()) return null;
		try
		{
			int level = Integer.parseInt(match.group(1));
			return level <= MAX_LEVEL ? level : null;
		}
		catch (NumberFormatException ignored) { return null; }
	}

	static boolean isDeathMessage(String raw)
	{
		if (raw == null || raw.length() > 256) return false;
		return DEATH.matcher(Text.removeTags(raw).replace('\u00a0', ' ')
			.replaceAll("\\s+", " ").trim()).matches();
	}

	synchronized Result finishOnDeathMessage(String raw)
	{
		return hasActiveRun() && isDeathMessage(raw) ? finishRun() : null;
	}

	synchronized Result finishByAction(EndAction action)
	{
		if (action == EndAction.CLAIM_REWARDS)
		{
			if (claimProcessed) return null;
			Result result = finishRun();
			if (result != null) claimedLootEventId = result.eventId;
			else if (finalizedForLoot != null) claimedLootEventId = finalizedForLoot.eventId;
			if (claimedLootEventId != null) claimProcessed = true;
			return result;
		}
		return action == EndAction.LEAVE_DELVING ? finishRun() : null;
	}

	synchronized Progress completeLevel(int level, String reporter, String sessionId,
		long gameCycle, Instant now)
	{
		if (level < 1 || level > MAX_LEVEL || reporter == null || reporter.trim().isEmpty()
			|| sessionId == null || now == null) return null;
		if (level == lastSignalLevel && sessionId.equals(lastSignalSession) && lastSignalAt != null
			&& !now.isBefore(lastSignalAt) && now.toEpochMilli() - lastSignalAt.toEpochMilli() <= 2_000L)
			return null;
		if (active == null)
		{
			// Once a run ended, only a newly completed first level may open another
			// run. A replayed later-floor header must not mint a second event ID.
			if (finalizedForLoot != null && level != 1) return null;
			finalizedForLoot = null;
			claimedLootEventId = null;
			claimProcessed = false;
			String identity = sessionId + "|doom-of-mokhaiotl|run|" + (++runSequence);
			String eventId = UUID.nameUUIDFromBytes(("numeric-run|" + identity)
				.getBytes(StandardCharsets.UTF_8)).toString();
			active = new Run(identity, eventId, reporter.trim(), now, gameCycle);
		}
		if (!active.completedLevels.add(level)) return null;
		lastSignalSession = sessionId;
		lastSignalLevel = level;
		lastSignalAt = now;
		if (level > active.highestLevel)
		{
			active.highestLevel = level;
			active.highestCompletedAt = now;
			active.highestGameCycle = gameCycle;
		}
		return active.snapshot();
	}

	/** Finish on death, explicit leave/claim, or session exit. Repeated finishes are inert. */
	synchronized Result finishRun()
	{
		if (active == null || active.highestLevel < 1) return null;
		Progress progress = active.snapshot();
		finalizedForLoot = new Result(progress.runIdentity, progress.eventId, progress.reporterRsn,
			progress.highestLevel, progress.occurredAt, progress.gameCycle);
		active = null;
		return finalizedForLoot;
	}

	synchronized Progress activeProgress() { return active == null ? null : active.snapshot(); }

	/** Consume the one explicit claim-to-reward association; never guess across runs. */
	synchronized String consumeClaimedLootLink()
	{
		String result = claimedLootEventId;
		claimedLootEventId = null;
		return result;
	}

	synchronized boolean hasActiveRun() { return active != null; }

	synchronized void reset()
	{
		active = null;
		finalizedForLoot = null;
		claimedLootEventId = null;
		lastSignalSession = null;
		lastSignalLevel = 0;
		lastSignalAt = null;
	}

	static class Progress
	{
		final String runIdentity;
		final String eventId;
		final String reporterRsn;
		final int highestLevel;
		final Instant occurredAt;
		final long gameCycle;
		final boolean finalized;

		Progress(String runIdentity, String eventId, String reporterRsn, int highestLevel,
			Instant occurredAt, long gameCycle, boolean finalized)
		{
			this.runIdentity = runIdentity; this.eventId = eventId; this.reporterRsn = reporterRsn;
			this.highestLevel = highestLevel; this.occurredAt = occurredAt;
			this.gameCycle = gameCycle; this.finalized = finalized;
		}
	}

	static final class Result extends Progress
	{
		Result(String runIdentity, String eventId, String reporterRsn, int highestLevel,
			Instant occurredAt, long gameCycle)
		{
			super(runIdentity, eventId, reporterRsn, highestLevel, occurredAt, gameCycle, true);
		}
	}

	private static final class Run
	{
		final String identity;
		final String eventId;
		final String reporter;
		final Instant startedAt;
		final long startedCycle;
		final Set<Integer> completedLevels = new HashSet<>();
		int highestLevel;
		Instant highestCompletedAt;
		long highestGameCycle;

		Run(String identity, String eventId, String reporter, Instant startedAt, long startedCycle)
		{
			this.identity = identity; this.eventId = eventId; this.reporter = reporter;
			this.startedAt = startedAt; this.startedCycle = startedCycle;
		}

		Progress snapshot()
		{
			return new Progress(identity, eventId, reporter, highestLevel,
				highestCompletedAt == null ? startedAt : highestCompletedAt,
				highestCompletedAt == null ? startedCycle : highestGameCycle, false);
		}
	}
}
