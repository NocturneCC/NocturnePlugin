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
	final String rsn;
	final String source;
	final String activityKey;
	final String activityDisplayName;
	final String modeKey;
	final boolean ordinaryNpcLoot;
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

	private LootRecord(String id, String occurredAt, String rsn, String source, List<LootItem> items,
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
		this.id = id;
		this.occurredAt = occurredAt;
		this.rsn = rsn;
		this.source = source;
		this.activityKey = activityKey;
		this.activityDisplayName = activityDisplayName;
		this.modeKey = modeKey;
		this.ordinaryNpcLoot = ordinaryNpcLoot;
		this.items = LootItem.consolidate(items);
		this.group = group;
		this.submission = submission;
		this.time = Instant.parse(occurredAt).atZone(ZoneId.systemDefault()).toLocalTime();
	}

	LootRecord withSubmission(SubmissionStatus status)
	{
		LootRecord updated = new LootRecord(id, occurredAt, rsn, source, items, group, status,
			activityKey, activityDisplayName, modeKey, ordinaryNpcLoot);
		updated.activityTrackingNotice = activityTrackingNotice;
		return updated;
	}
}
