package com.nocturne;

import java.time.LocalTime;
import java.util.List;

final class LootRecord
{
	final String rsn;
	final String source;
	final List<String> items;
	final GroupSnapshot group;
	final LocalTime time = LocalTime.now();

	LootRecord(String rsn, String source, List<String> items)
	{
		this(rsn, source, items, GroupSnapshot.unavailable("No group captured."));
	}

	LootRecord(String rsn, String source, List<String> items, GroupSnapshot group)
	{
		this.rsn = rsn;
		this.source = source;
		this.items = List.copyOf(items);
		this.group = group;
	}
}
