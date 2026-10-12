package com.nocturne;

import java.util.LinkedHashMap;
import java.util.List;

/** Short-lived cross-event-source duplicate guard for RuneLite's two loot APIs. */
final class LootEventDeduplicator
{
	private static final int MAX_ENTRIES = 256;
	private static final long MAX_CYCLE_DISTANCE = 1;
	private final LinkedHashMap<String, Long> seen = new LinkedHashMap<>();

	synchronized boolean isDuplicate(String source, List<LootItem> items, long cycle)
	{
		String key = PluginActivityCatalog.normalize(source == null ? "" : source) + '|'
			+ String.join("|", items.stream().map(item -> item.id + ":" + item.quantity).sorted()
				.toArray(String[]::new));
		Long previous = seen.get(key);
		seen.put(key, cycle);
		seen.entrySet().removeIf(entry -> cycle - entry.getValue() > MAX_CYCLE_DISTANCE);
		while (seen.size() > MAX_ENTRIES) seen.remove(seen.keySet().iterator().next());
		return previous != null && cycle >= previous && cycle - previous <= MAX_CYCLE_DISTANCE;
	}

	synchronized void clear() { seen.clear(); }
}
