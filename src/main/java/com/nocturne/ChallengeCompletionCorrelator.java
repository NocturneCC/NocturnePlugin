package com.nocturne;

import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.function.LongSupplier;

/** Bounded, single-run correlation of sanitized completion lines and a frozen raid roster. */
final class ChallengeCompletionCorrelator
{
	static final long WINDOW_NANOS = 20_000_000_000L;
	static final long OPTIONAL_COUNT_GRACE_NANOS = 2_000_000_000L;
	private static final int MAX_FINGERPRINTS = 256;
	private static final long FINGERPRINT_TTL_NANOS = 86_400_000_000_000L;
	private final ChallengeCompletionParser parser;
	private final LongSupplier nanoTime;
	private Partial partial;
	private long blockedRunEpoch = Long.MIN_VALUE;
	private ChallengeCompletionParser.Family blockedFamily;
	private final LinkedHashMap<String, Long> sent = new LinkedHashMap<>();

	ChallengeCompletionCorrelator() { this(new ChallengeCompletionParser(), System::nanoTime); }
	ChallengeCompletionCorrelator(ChallengeCompletionParser parser, LongSupplier nanoTime)
	{
		this.parser = parser;
		this.nanoTime = nanoTime;
	}

	ChallengeObservation accept(String raw, RaidCompletionEvidence evidence, String reporter, Instant now)
	{
		long current = nanoTime.getAsLong();
		prune(current);
		if (partial != null && (current - partial.started > WINDOW_NANOS
			|| evidence == null || partial.runEpoch != evidence.runEpoch || !compatible(partial.family, evidence.raid))) partial = null;
		ChallengeCompletionParser.Message message = parser.parse(raw);
		if (message == null)
		{
			ChallengeCompletionParser.Family malformed = parser.malformedCandidateFamily(raw);
			if (malformed != null && evidence != null && compatible(malformed, evidence.raid))
			{
				blockedRunEpoch = evidence.runEpoch;
				blockedFamily = malformed;
				partial = null;
			}
			return null;
		}
		if (evidence == null || !compatible(message.family, evidence.raid)) return null;
		if (blockedRunEpoch == evidence.runEpoch && blockedFamily == message.family) return null;
		if (partial != null && conflicting(partial, message))
		{
			blockedRunEpoch = partial.runEpoch;
			blockedFamily = partial.family;
			partial = null;
			return null;
		}
		if (partial == null)
		{
			partial = new Partial(message.family, evidence.runEpoch, current);
		}
		Partial p = partial;
		if (message.family != p.family) { partial = null; return null; }
		switch (message.kind)
		{
			case ROOM_TIME: if (!putMetric(p, true, message.durationMillis)) return null; break;
			case OVERALL_TIME:
				if (!putMetric(p, false, message.durationMillis)) return null;
				if (message.mode != null)
				{
					if (p.mode != null && p.mode != message.mode) { partial = null; return null; }
					p.mode = message.mode;
				}
				break;
			case MODE_COUNT:
				if (p.mode != null && p.mode != message.mode) { partial = null; return null; }
				if (p.count != null && !p.count.equals(message.count)) { partial = null; return null; }
				p.mode = message.mode; p.count = message.count; break;
			case COX_RESULT:
				if (!putMetric(p, false, message.durationMillis)) return null;
				if (p.teamSize != null && p.teamSize != message.teamSize) { partial = null; return null; }
				p.teamSize = message.teamSize; break;
			default: return null;
		}
		return assemble(p, evidence, reporter, now);
	}

	ChallengeObservation onEvidence(RaidCompletionEvidence evidence, String reporter, Instant now)
	{
		long current = nanoTime.getAsLong();
		prune(current);
		if (partial == null || evidence == null || partial.runEpoch != evidence.runEpoch
			|| !compatible(partial.family, evidence.raid) || current - partial.started > WINDOW_NANOS)
		{
			partial = null;
			return null;
		}
		return assemble(partial, evidence, reporter, now);
	}

	void reset() { partial = null; blockedRunEpoch = Long.MIN_VALUE; blockedFamily = null; }
	void clear() { reset(); sent.clear(); }
	void clearPartial() { partial = null; }
	int rememberedCount() { return sent.size(); }

	private ChallengeObservation assemble(Partial p, RaidCompletionEvidence evidence, String reporter, Instant now)
	{
		if (evidence == null || evidence.runEpoch != p.runEpoch || !compatible(p.family, evidence.raid) || !evidence.completed) return null;
		if (blockedRunEpoch == p.runEpoch && blockedFamily == p.family) return null;
		if (p.family == ChallengeCompletionParser.Family.TOA && p.count == null)
		{
			long current = nanoTime.getAsLong();
			if (!p.optionalCountWaitStarted) { p.optionalCountWaitStarted = true; p.optionalCountWaitStartedAt = current; return null; }
			if (current - p.optionalCountWaitStartedAt < OPTIONAL_COUNT_GRACE_NANOS) return null;
		}
		String activity;
		String mode;
		if (p.family == ChallengeCompletionParser.Family.TOB)
		{
			if (p.room == null || p.overall == null || p.mode == null) return null;
			if (p.mode == ChallengeCompletionParser.Mode.REGULAR) { activity = "theatre_of_blood"; mode = modeForTob(evidence.roster.expectedSize); }
			else if (p.mode == ChallengeCompletionParser.Mode.HARD) { activity = "theatre_of_blood_hard_mode"; mode = "hmt_5man"; }
			else return null;
		}
		else if (p.family == ChallengeCompletionParser.Family.TOA)
		{
			if (p.overall == null || p.mode == null) return null;
			activity = "tombs_of_amascut";
			mode = p.mode == ChallengeCompletionParser.Mode.EXPERT ? "toa_expert_solo" : null;
		}
		else
		{
			if (p.overall == null || p.teamSize == null || p.teamSize != evidence.roster.expectedSize) return null;
			boolean cm = evidence.challengeMode;
			if (p.mode != null && (p.mode == ChallengeCompletionParser.Mode.CHALLENGE) != cm) return null;
			activity = cm ? "chambers_of_xeric_challenge_mode" : "chambers_of_xeric";
			mode = coxMode(cm, p.teamSize);
		}
		if (mode == null) return null;
		ChallengeObservation result = ChallengeObservation.create(activity, mode, p.room, p.overall,
			p.count, evidence.roster, reporter, now);
		if (result == null) return null;
		String fingerprint = result.fingerprint();
		if (sent.containsKey(fingerprint)) { partial = null; return null; }
		sent.put(fingerprint, nanoTime.getAsLong());
		while (sent.size() > MAX_FINGERPRINTS) sent.remove(sent.keySet().iterator().next());
		partial = null;
		return result;
	}

	private static boolean putMetric(Partial p, boolean room, long value)
	{
		Long old = room ? p.room : p.overall;
		if (old != null && old != value) { return false; }
		if (room) p.room = value; else p.overall = value;
		return true;
	}

	private static boolean conflicting(Partial p, ChallengeCompletionParser.Message m)
	{
		if (m.family != p.family) return true;
		if (m.kind == ChallengeCompletionParser.Kind.MODE_COUNT
			&& (p.mode != null && p.mode != m.mode || p.count != null && !p.count.equals(m.count))) return true;
		if (m.kind == ChallengeCompletionParser.Kind.OVERALL_TIME)
			return p.overall != null && p.overall != m.durationMillis
				|| m.mode != null && p.mode != null && p.mode != m.mode;
		if (m.kind == ChallengeCompletionParser.Kind.ROOM_TIME) return p.room != null && p.room != m.durationMillis;
		return m.kind == ChallengeCompletionParser.Kind.COX_RESULT
			&& (p.overall != null && p.overall != m.durationMillis || p.teamSize != null && p.teamSize != m.teamSize);
	}

	private void prune(long now)
	{
		if (partial != null && now - partial.started > WINDOW_NANOS) partial = null;
		sent.entrySet().removeIf(e -> now - e.getValue() > FINGERPRINT_TTL_NANOS);
	}
	private static boolean compatible(ChallengeCompletionParser.Family family, RaidType type)
	{
		return family == ChallengeCompletionParser.Family.TOB && type == RaidType.TOB
			|| family == ChallengeCompletionParser.Family.TOA && type == RaidType.TOA
			|| family == ChallengeCompletionParser.Family.COX && type == RaidType.COX;
	}
	private static String modeForTob(int size)
	{
		switch (size) { case 2: return "tob_duo"; case 3: return "tob_trio"; case 5: return "tob_5man"; default: return null; }
	}
	private static String coxMode(boolean cm, int size)
	{
		if (cm)
		{
			switch (size) { case 1: return "cox_cm_solo"; case 3: return "cox_cm_trio"; case 5: return "cox_cm_5man"; default: return null; }
		}
		return size == 1 ? "cox_solo" : null;
	}
	private static final class Partial
	{
		final ChallengeCompletionParser.Family family; final long runEpoch; final long started;
		Long room, overall; Integer count, teamSize; ChallengeCompletionParser.Mode mode;
		boolean optionalCountWaitStarted; long optionalCountWaitStartedAt;
		Partial(ChallengeCompletionParser.Family family, long runEpoch, long started)
		{ this.family = family; this.runEpoch = runEpoch; this.started = started; }
	}
}
