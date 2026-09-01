package com.nocturne;

import java.time.LocalTime;
import java.util.List;

final class LootRecord
{
	final String rsn;
	final String source;
	final List<String> items;
	final LocalTime time = LocalTime.now();

	LootRecord(String rsn, String source, List<String> items)
	{
		this.rsn = rsn;
		this.source = source;
		this.items = List.copyOf(items);
	}
}
