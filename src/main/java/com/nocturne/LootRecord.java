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
	final String rsn;
	final String source;
	final String activityKey;
	final String activityDisplayName;
	final String modeKey;
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
			SubmissionStatus.LOCAL, activity);
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
		this.id = id;
		this.occurredAt = occurredAt;
		this.rsn = rsn;
		this.source = source;
		this.activityKey = activityKey;
		this.activityDisplayName = activityDisplayName;
		this.modeKey = modeKey;
		this.items = LootItem.consolidate(items);
		this.group = group;
		this.submission = submission;
		this.time = Instant.parse(occurredAt).atZone(ZoneId.systemDefault()).toLocalTime();
	}

	LootRecord withSubmission(SubmissionStatus status)
	{
		return new LootRecord(id, occurredAt, rsn, source, items, group, status,
			activityKey, activityDisplayName, modeKey);
	}
}
