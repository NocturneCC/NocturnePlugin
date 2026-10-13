package com.nocturne;

import java.time.Instant;
import java.util.ArrayDeque;
import java.util.HashMap;
import java.util.HashSet;
import java.util.Locale;
import java.util.Map;
import java.util.Set;

/** Bounded session link from a timed completion card to a later reward event. */
final class PendingActivityTiming
{
	static final int MAX_PENDING = 32;
	private final ArrayDeque<Entry> entries = new ArrayDeque<>();
	private final Map<String, String> latestStarted = new HashMap<>();
	private final Set<String> blockedIdentities = new HashSet<>();
	private final Set<String> seenContexts = new HashSet<>();
	private boolean linkingDisabled;

	synchronized void activityStarted(BossCompletionContext context)
	{
		if (context == null || linkingDisabled || !seenContexts.add(context.encounterIdentity)) return;
		if (seenContexts.size() > 128)
		{
			// Bounded identity tracking has saturated; fail closed for the session.
			linkingDisabled = true;
			entries.clear();
			return;
		}
		String key = identity(context.activityKey, context.modeKey, normalize(context.reporterRsn));
		String previous = latestStarted.put(key, context.encounterIdentity);
		if (previous != null && !previous.equals(context.encounterIdentity)
			&& entries.stream().anyMatch(entry -> key.equals(identity(entry.activityKey, entry.modeKey, entry.reporter))
				&& !context.encounterIdentity.equals(entry.encounterIdentity))) blockedIdentities.add(key);
		for (Entry entry : entries)
			if (key.equals(identity(entry.activityKey, entry.modeKey, entry.reporter))
				&& !context.encounterIdentity.equals(entry.encounterIdentity)) blockedIdentities.add(key);
	}

	synchronized void update(ChallengeObservation observation, ActivityTimingStatus status, long ignoredNowNanos)
	{
		if (observation == null || status == null) return;
		if (linkingDisabled) return;
		String key = identity(observation.activityKey, observation.modeKey, normalize(observation.reporterRsn));
		String latest = latestStarted.get(key);
		if (latest != null && !latest.equals(observation.encounterIdentity)) blockedIdentities.add(key);
		for (Entry entry : entries)
		{
			if (entry.eventId.equals(observation.eventId)) { entry.status = status; return; }
			if (key.equals(identity(entry.activityKey, entry.modeKey, entry.reporter))
				&& !entry.encounterIdentity.equals(observation.encounterIdentity)) blockedIdentities.add(key);
		}
		if (entries.size() >= MAX_PENDING)
		{
			// Preserve all completion cards; stop opportunistic linking rather than
			// evicting identity and risking a later reward being attached to the wrong run.
			entries.clear();
			linkingDisabled = true;
			return;
		}
		entries.addLast(new Entry(observation, status));
	}

	/**
	 * A reward can arrive well after its completion. Link only when exactly one
	 * unclaimed completion for this reporter/activity exists and the reward is
	 * later than that completion; ambiguity leaves both records separate.
	 */
	synchronized LootRecord merge(LootRecord loot)
	{
		Entry candidate = null;
		int matches = 0;
		Instant lootTime = parse(loot == null ? null : loot.occurredAt);
		if (linkingDisabled || lootTime == null) return null;
		for (Entry entry : entries)
		{
			boolean exactTrigger = loot.id.equals(entry.triggerLootRecordId);
			String key = identity(entry.activityKey, entry.modeKey, entry.reporter);
			if (blockedIdentities.contains(key) || !matches(loot, entry)
				|| !exactTrigger && lootTime.isBefore(entry.occurredAt)) continue;
			candidate = entry;
			matches++;
		}
		if (matches != 1) return null;
		entries.remove(candidate);
		LootRecord completion = LootRecord.completion(candidate.observation, candidate.status);
		return completion.withCompletionLoot(loot);
	}

	static boolean matches(LootRecord record, ChallengeObservation observation)
	{
		if (record == null || observation == null || record.activityKey == null || record.rsn == null
			|| !record.activityKey.equals(observation.activityKey)
			|| !java.util.Objects.equals(record.modeKey, observation.modeKey)
			|| !normalize(record.rsn).equals(normalize(observation.reporterRsn))) return false;
		if (observation.triggerLootRecordId != null)
			return record.id.equals(observation.triggerLootRecordId);
		Instant occurred = parse(record.occurredAt);
		return occurred != null && !occurred.isBefore(observation.occurredAt);
	}

	synchronized int size() { return entries.size(); }
	synchronized void clear()
	{
		entries.clear(); latestStarted.clear(); blockedIdentities.clear(); seenContexts.clear(); linkingDisabled = false;
	}

	private static boolean matches(LootRecord loot, Entry entry)
	{
		return loot != null && loot.activityKey != null && loot.rsn != null
			&& loot.activityKey.equals(entry.activityKey)
			&& java.util.Objects.equals(loot.modeKey, entry.modeKey)
			&& normalize(loot.rsn).equals(entry.reporter);
	}

	private static Instant parse(String value)
	{
		try { return Instant.parse(value); }
		catch (RuntimeException ignored) { return null; }
	}

	private static String normalize(String rsn)
	{
		return String.join("_", rsn.trim().toLowerCase(Locale.ROOT)
			.replace('-', ' ').replace('_', ' ').split("\\s+"));
	}

	private static String identity(String activityKey, String modeKey, String reporter)
	{
		return activityKey + '|' + modeKey + '|' + reporter;
	}

	private static final class Entry
	{
		final String eventId, activityKey, modeKey, reporter, triggerLootRecordId, encounterIdentity;
		final Instant occurredAt;
		final ChallengeObservation observation;
		ActivityTimingStatus status;

		Entry(ChallengeObservation observation, ActivityTimingStatus status)
		{
			this.eventId = observation.eventId;
			this.encounterIdentity = observation.encounterIdentity;
			this.activityKey = observation.activityKey;
			this.modeKey = observation.modeKey;
			this.reporter = normalize(observation.reporterRsn);
			this.triggerLootRecordId = observation.triggerLootRecordId;
			this.occurredAt = observation.occurredAt;
			this.observation = observation;
			this.status = status;
		}
	}
}
