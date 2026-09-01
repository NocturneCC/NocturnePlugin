package com.nocturne;

import java.time.LocalTime;
import java.time.Instant;
import java.util.UUID;
import java.util.List;

final class LootRecord
{
	final String id = UUID.randomUUID().toString();
	final String occurredAt = Instant.now().toString();
	// Updated only on the Swing event thread.
	SubmissionStatus submission = SubmissionStatus.LOCAL;
	final String rsn;
	final String source;
	final List<LootItem> items;
	final GroupSnapshot group;
	final LocalTime time = LocalTime.now();

	LootRecord(String rsn, String source, List<LootItem> items)
	{
		this(rsn, source, items, GroupSnapshot.unavailable("No group captured."));
	}

	LootRecord(String rsn, String source, List<LootItem> items, GroupSnapshot group)
	{
		this.rsn = rsn;
		this.source = source;
		this.items = List.copyOf(items);
		this.group = group;
	}
}
