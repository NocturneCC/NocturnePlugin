package com.nocturne;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import java.time.Instant;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import java.util.regex.Pattern;
import net.runelite.client.util.Text;

/** Validated, semantic v1 observation. Event UUID is added only by the HTTP service. */
final class ChallengeObservation
{
	static final int SCHEMA_VERSION = 1;
	static final int MAX_ROSTER = 10;
	static final long MAX_DURATION_MILLIS = 86_400_000L;
	private static final Pattern RSN = Pattern.compile("[A-Za-z0-9 _-]{1,12}");

	final String activityKey;
	final String modeKey;
	final String reporterRsn;
	final List<String> roster;
	final int groupSize;
	final Long roomTimeMillis;
	final Long overallTimeMillis;
	final Integer completionCount;
	final Instant occurredAt;
	final long displayDurationMillis;
	final String displayName;

	private ChallengeObservation(String activityKey, String modeKey, String reporterRsn,
		List<String> roster, Long roomTimeMillis, Long overallTimeMillis,
		Integer completionCount, Instant occurredAt, long displayDurationMillis, String displayName)
	{
		this.activityKey = activityKey;
		this.modeKey = modeKey;
		this.reporterRsn = reporterRsn;
		this.roster = List.copyOf(roster);
		this.groupSize = roster.size();
		this.roomTimeMillis = roomTimeMillis;
		this.overallTimeMillis = overallTimeMillis;
		this.completionCount = completionCount;
		this.occurredAt = occurredAt;
		this.displayDurationMillis = displayDurationMillis;
		this.displayName = displayName;
	}

	static ChallengeObservation create(String activityKey, String modeKey,
		Long roomTimeMillis, Long overallTimeMillis, Integer completionCount,
		GroupSnapshot snapshot, String localReporter, Instant occurredAt)
	{
		if (snapshot == null || snapshot.status != GroupSnapshot.Status.MATCHED
			|| snapshot.expectedSize < 1 || snapshot.expectedSize > MAX_ROSTER
			|| snapshot.names.size() != snapshot.expectedSize || occurredAt == null
			|| completionCount != null && (completionCount < 1
				|| completionCount > ChallengeCompletionParser.MAX_COMPLETION_COUNT))
		{
			return null;
		}
		if (!validDuration(roomTimeMillis) || !validDuration(overallTimeMillis)
			|| roomTimeMillis == null && overallTimeMillis == null) return null;

		List<String> normalizedRoster = new ArrayList<>();
		Set<String> identities = new HashSet<>();
		String reporter = safeRsn(localReporter);
		if (reporter == null) return null;
		String reporterKey = identityKey(reporter);
		int reporterOccurrences = 0;
		for (String raw : snapshot.names)
		{
			String rsn = safeRsn(raw);
			if (rsn == null) return null;
			String key = identityKey(rsn);
			if (key.isEmpty() || !identities.add(key)) return null;
			if (key.equals(reporterKey)) reporterOccurrences++;
			normalizedRoster.add(rsn);
		}
		if (reporterOccurrences != 1 || normalizedRoster.size() != snapshot.expectedSize) return null;
		normalizedRoster.sort(Comparator.comparing(ChallengeObservation::identityKey));

		int expectedSize = expectedSize(activityKey, modeKey);
		if (expectedSize < 1 || expectedSize != normalizedRoster.size()) return null;
		long displayDuration;
		String displayName;
		if ("theatre_of_blood".equals(activityKey))
		{
			if (roomTimeMillis == null) return null;
			displayDuration = roomTimeMillis;
			displayName = "Theatre of Blood";
		}
		else
		{
			if (overallTimeMillis == null) return null;
			displayDuration = overallTimeMillis;
			displayName = "theatre_of_blood_hard_mode".equals(activityKey) ? "HMT"
				: "tombs_of_amascut".equals(activityKey) ? "Tombs of Amascut" : "Chambers of Xeric";
		}
		return new ChallengeObservation(activityKey, modeKey, reporter, normalizedRoster,
			roomTimeMillis, overallTimeMillis, completionCount, occurredAt, displayDuration, displayName);
	}

	JsonObject payload(Gson gson, String eventId)
	{
		JsonObject body = new JsonObject();
		body.addProperty("schema_version", SCHEMA_VERSION);
		body.addProperty("event_id", eventId);
		body.addProperty("reporter_rsn", reporterRsn);
		body.addProperty("activity_key", activityKey);
		body.addProperty("mode_key", modeKey);
		body.addProperty("occurred_at", DateTimeFormatter.ISO_INSTANT.format(occurredAt));
		if (roomTimeMillis != null) body.addProperty("room_time_ms", roomTimeMillis);
		if (overallTimeMillis != null) body.addProperty("overall_time_ms", overallTimeMillis);
		JsonArray names = new JsonArray();
		for (String rsn : roster) names.add(rsn);
		body.add("roster", names);
		body.addProperty("group_size", groupSize);
		if (completionCount != null) body.addProperty("completion_count", completionCount);
		body.addProperty("plugin_version", PluginMetadata.VERSION);
		return body;
	}

	String fingerprint()
	{
		// completion_count is optional metadata (notably for ToA), so it must not
		// make a replay of the same timed run look like a new semantic event.
		return activityKey + '|' + modeKey + '|' + roomTimeMillis + '|' + overallTimeMillis + '|'
			+ String.join("|", roster.stream().map(ChallengeObservation::identityKey).toArray(String[]::new));
	}

	static String formatTime(long milliseconds)
	{
		long centiseconds = milliseconds / 10;
		long hours = centiseconds / 360_000;
		long minutes = (centiseconds / 6_000) % 60;
		long seconds = (centiseconds / 100) % 60;
		long fraction = centiseconds % 100;
		return hours > 0
			? String.format(Locale.ROOT, "%d:%02d:%02d.%02d", hours, minutes, seconds, fraction)
			: String.format(Locale.ROOT, "%d:%02d.%02d", centiseconds / 6_000, seconds, fraction);
	}

	private static boolean validDuration(Long value)
	{
		return value == null || value > 0 && value <= MAX_DURATION_MILLIS;
	}

	private static String safeRsn(String raw)
	{
		if (raw == null) return null;
		String value = Text.toJagexName(Text.removeTags(raw)).trim();
		return RSN.matcher(value).matches() ? value : null;
	}

	private static String identityKey(String rsn)
	{
		return rsn.toLowerCase(Locale.ROOT).replaceAll("[^a-z0-9]", "");
	}

	private static int expectedSize(String activity, String mode)
	{
		if ("theatre_of_blood".equals(activity))
		{
			switch (mode)
			{
				case "tob_duo": return 2;
				case "tob_trio": return 3;
				case "tob_5man": return 5;
				default: return -1; // Other seeded modes have no server Challenge boss mapping.
			}
		}
		if ("theatre_of_blood_hard_mode".equals(activity)) return "hmt_5man".equals(mode) ? 5 : -1;
		if ("tombs_of_amascut".equals(activity)) return "toa_expert_solo".equals(mode) ? 1 : -1;
		if ("chambers_of_xeric".equals(activity)) return "cox_solo".equals(mode) ? 1 : -1;
		if ("chambers_of_xeric_challenge_mode".equals(activity))
		{
			switch (mode)
			{
				case "cox_cm_solo": return 1;
				case "cox_cm_trio": return 3;
				case "cox_cm_5man": return 5;
				default: return -1;
			}
		}
		return -1;
	}
}
