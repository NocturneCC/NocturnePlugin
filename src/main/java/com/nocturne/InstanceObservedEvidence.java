package com.nocturne;

import java.util.ArrayList;
import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;

/** Local-only corroboration. This is never an authoritative roster or scoring input. */
final class InstanceObservedEvidence
{
	static final long NEAR_COMPLETION_TICKS = 50;
	private long epoch;
	private boolean accepting;
	private long completionTick = -1;
	private final Map<String, Seen> players = new LinkedHashMap<>();

	void begin(long raidEpoch, Collection<String> initiallyVisible, long tick)
	{
		epoch = raidEpoch;
		accepting = true;
		completionTick = -1;
		players.clear();
		observe(raidEpoch, initiallyVisible, tick);
	}

	void clear()
	{
		epoch = 0;
		accepting = false;
		completionTick = -1;
		players.clear();
	}

	void observe(long raidEpoch, Collection<String> names, long tick)
	{
		if (!accepting || raidEpoch != epoch) return;
		for (String raw : names)
		{
			String name = normalize(raw);
			if (name == null) continue;
			String key = name.toLowerCase(Locale.ROOT);
			Seen seen = players.get(key);
			if (seen == null) players.put(key, new Seen(name, tick, tick, false));
			else seen.lastSeenTick = tick;
		}
	}

	void complete(long raidEpoch, long tick)
	{
		if (!accepting || raidEpoch != epoch) return;
		completionTick = tick;
		for (Seen seen : players.values())
			seen.nearCompletion = tick - seen.lastSeenTick <= NEAR_COMPLETION_TICKS;
		accepting = false;
	}

	void stop(long raidEpoch)
	{
		if (raidEpoch == epoch) accepting = false;
	}

	Snapshot snapshot()
	{
		return new Snapshot(epoch, accepting, completionTick, new ArrayList<>(players.values()));
	}

	private static String normalize(String raw)
	{
		if (raw == null) return null;
		String value = raw.trim().replace('_', ' ');
		return value.isEmpty() || value.length() > 12 ? null : value;
	}

	static final class Seen
	{
		final String name;
		final long firstSeenTick;
		long lastSeenTick;
		boolean nearCompletion;
		private Seen(String name, long firstSeenTick, long lastSeenTick, boolean nearCompletion)
		{
			this.name = name; this.firstSeenTick = firstSeenTick;
			this.lastSeenTick = lastSeenTick; this.nearCompletion = nearCompletion;
		}
	}

	static final class Snapshot
	{
		final long epoch;
		final boolean accepting;
		final long completionTick;
		final List<Seen> players;
		private Snapshot(long epoch, boolean accepting, long completionTick, List<Seen> players)
		{
			this.epoch = epoch; this.accepting = accepting;
			this.completionTick = completionTick; this.players = List.copyOf(players);
		}
		int nearCompletionCount()
		{
			return (int) players.stream().filter(seen -> seen.nearCompletion).count();
		}
	}
}
