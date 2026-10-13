package com.nocturne;

import java.time.Instant;
import java.time.LocalTime;
import java.time.ZoneId;
import java.util.UUID;
import java.util.List;

final class LootRecord
{
	final String id;
	final String occurredAt;
	// Updated only on the Swing event thread.
	SubmissionStatus submission = SubmissionStatus.LOCAL;
	// Updated only on the Swing event thread after authoritative tracking feedback.
	String activityTrackingNotice;
	// Completion capture is independent of the drop submission state above.
	Long activityDurationMillis;
	Integer activityNumericValue;
	String activityNumericLabel;
	String activityNumericUnit;
	ActivityTimingStatus activityTimingStatus;
	// In-memory bridge used only while merging the completion and reward rows.
	String completionLinkId;
	String activityEventId;
	final String rsn;
	final String source;
	final String activityKey;
	final String activityDisplayName;
	final String modeKey;
	final boolean ordinaryNpcLoot;
	final boolean completionOnly;
	final List<LootItem> items;
	final GroupSnapshot group;
	final LocalTime time;

	LootRecord(String rsn, String source, List<LootItem> items)
	{
		this(rsn, source, items, GroupSnapshot.unavailable("No group captured."));
	}

	LootRecord(String rsn, String source, List<LootItem> items, GroupSnapshot group)
	{
		this(UUID.randomUUID().toString(), Instant.now().toString(), rsn, source, items, group,
		SubmissionStatus.LOCAL, PluginActivityCatalog.matchSource(source));
	}

	LootRecord(String rsn, String source, List<LootItem> items, GroupSnapshot group,
		PluginActivityCatalog.Entry activity)
	{
		this(UUID.randomUUID().toString(), Instant.now().toString(), rsn, source, items, group,
			SubmissionStatus.LOCAL, activity, false);
	}

	LootRecord(String rsn, String source, List<LootItem> items, GroupSnapshot group,
		PluginActivityCatalog.Entry activity, boolean ordinaryNpcLoot)
	{
		this(UUID.randomUUID().toString(), Instant.now().toString(), rsn, source, items, group,
			SubmissionStatus.LOCAL, activity, ordinaryNpcLoot);
	}

	LootRecord(String id, String occurredAt, String rsn, String source, List<LootItem> items,
		GroupSnapshot group, SubmissionStatus submission, PluginActivityCatalog.Entry activity,
		boolean ordinaryNpcLoot)
	{
		this(id, occurredAt, rsn, source, items, group, submission,
			activity == null ? null : activity.activityKey,
			activity == null ? null : activity.displayName,
			activity == null ? null : activity.modeKey, ordinaryNpcLoot);
	}

	LootRecord(String id, String occurredAt, String rsn, String source, List<LootItem> items,
		GroupSnapshot group, SubmissionStatus submission)
	{
		this(id, occurredAt, rsn, source, items, group, submission, PluginActivityCatalog.matchSource(source));
	}

	LootRecord(String id, String occurredAt, String rsn, String source, List<LootItem> items,
		GroupSnapshot group, SubmissionStatus submission, PluginActivityCatalog.Entry activity)
	{
		this(id, occurredAt, rsn, source, items, group, submission,
			activity == null ? null : activity.activityKey,
			activity == null ? null : activity.displayName,
			activity == null ? null : activity.modeKey);
	}

	LootRecord(String id, String occurredAt, String rsn, String source, List<LootItem> items,
		GroupSnapshot group, SubmissionStatus submission, String activityKey,
		String activityDisplayName, String modeKey)
	{
		this(id, occurredAt, rsn, source, items, group, submission, activityKey,
			activityDisplayName, modeKey, false);
	}

	LootRecord(String id, String occurredAt, String rsn, String source, List<LootItem> items,
		GroupSnapshot group, SubmissionStatus submission, String activityKey,
		String activityDisplayName, String modeKey, boolean ordinaryNpcLoot)
	{
		this(id, occurredAt, rsn, source, items, group, submission, activityKey,
			activityDisplayName, modeKey, ordinaryNpcLoot, false);
	}

	LootRecord(String id, String occurredAt, String rsn, String source, List<LootItem> items,
		GroupSnapshot group, SubmissionStatus submission, String activityKey,
		String activityDisplayName, String modeKey, boolean ordinaryNpcLoot, boolean completionOnly)
	{
		this.id = id;
		this.occurredAt = occurredAt;
		this.rsn = rsn;
		this.source = source;
		this.activityKey = activityKey;
		this.activityDisplayName = activityDisplayName;
		this.modeKey = modeKey;
		this.ordinaryNpcLoot = ordinaryNpcLoot;
		this.completionOnly = completionOnly;
		this.items = LootItem.consolidate(items);
		this.group = group;
		this.submission = submission;
		this.time = Instant.parse(occurredAt).atZone(ZoneId.systemDefault()).toLocalTime();
	}

	LootRecord withSubmission(SubmissionStatus status)
	{
		LootRecord updated = new LootRecord(id, occurredAt, rsn, source, items, group, status,
			activityKey, activityDisplayName, modeKey, ordinaryNpcLoot, completionOnly);
		updated.activityTrackingNotice = activityTrackingNotice;
		updated.activityDurationMillis = activityDurationMillis;
		updated.activityNumericValue = activityNumericValue;
		updated.activityNumericLabel = activityNumericLabel;
		updated.activityNumericUnit = activityNumericUnit;
		updated.activityTimingStatus = activityTimingStatus;
		updated.completionLinkId = completionLinkId;
		updated.activityEventId = activityEventId;
		return updated;
	}

	LootRecord withActivityTiming(long durationMillis, ActivityTimingStatus status)
	{
		LootRecord updated = new LootRecord(id, occurredAt, rsn, source, items, group, submission,
			activityKey, activityDisplayName, modeKey, ordinaryNpcLoot, completionOnly);
		updated.activityTrackingNotice = activityTrackingNotice;
		updated.activityDurationMillis = durationMillis;
		updated.activityNumericValue = activityNumericValue;
		updated.activityNumericLabel = activityNumericLabel;
		updated.activityNumericUnit = activityNumericUnit;
		updated.activityTimingStatus = status;
		updated.completionLinkId = completionLinkId;
		updated.activityEventId = activityEventId;
		return updated;
	}

	LootRecord withActivityCompletion(int value, String label, String unit, ActivityTimingStatus status)
	{
		LootRecord updated = new LootRecord(id, occurredAt, rsn, source, items, group, submission,
			activityKey, activityDisplayName, modeKey, ordinaryNpcLoot, completionOnly);
		updated.activityTrackingNotice = activityTrackingNotice;
		updated.activityDurationMillis = activityDurationMillis;
		updated.activityTimingStatus = status;
		updated.activityNumericValue = value;
		updated.activityNumericLabel = label;
		updated.activityNumericUnit = unit;
		updated.completionLinkId = completionLinkId;
		updated.activityEventId = activityEventId;
		return updated;
	}

	static LootRecord completion(ChallengeObservation observation, ActivityTimingStatus status)
	{
		LootRecord record = new LootRecord(observation.eventId, observation.occurredAt.toString(),
			observation.reporterRsn, observation.displayName, List.of(),
			GroupSnapshot.unavailable("Completion-only activity."), SubmissionStatus.LOCAL,
			observation.activityKey, observation.displayName, observation.modeKey, false, true)
			;
		if (observation.completionMetricValue != null)
			record = record.withActivityCompletion(observation.completionMetricValue,
				observation.completionMetricLabel, observation.completionMetricUnit, status);
		else record = record.withActivityTiming(observation.displayDurationMillis, status);
		record.activityEventId = observation.eventId;
		return record;
	}

	LootRecord withCompletionLoot(LootRecord loot)
	{
		LootRecord merged = new LootRecord(loot.id, occurredAt, loot.rsn, loot.source, loot.items,
			loot.group, loot.submission, activityKey, activityDisplayName, modeKey,
			loot.ordinaryNpcLoot, false);
		merged.activityTrackingNotice = loot.activityTrackingNotice;
		merged.activityDurationMillis = activityDurationMillis;
		merged.activityNumericValue = activityNumericValue;
		merged.activityNumericLabel = activityNumericLabel;
		merged.activityNumericUnit = activityNumericUnit;
		merged.activityTimingStatus = activityTimingStatus;
		merged.completionLinkId = id;
		merged.activityEventId = activityEventId == null ? id : activityEventId;
		return merged;
	}
}
