package com.nocturne;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.List;

/** Bounded, short-lived ring of eligible system messages; player and clan chat are excluded. */
final class CompletionMessageBuffer
{
	static final int CAPACITY = 128;
	static final long TTL_NANOS = 30_000_000_000L;
	private static final long REPLAY_NANOS = 750_000_000L;
	private final Deque<Entry> entries = new ArrayDeque<>();
	private long sequence;

	static boolean eligibleCategory(String category)
	{
		return "GAMEMESSAGE".equals(category) || "SPAM".equals(category) || "ENGINE".equals(category);
	}

	synchronized Entry offer(String category, String raw, long gameCycle, long arrivalNanos,
		CompletionDurationParser parser)
	{
		prune(arrivalNanos);
		if (!eligibleCategory(category)) return null;
		String normalized = parser.normalize(raw);
		if (normalized == null) return null;
		for (Entry old : entries)
			if (old.category.equals(category) && old.normalizedText.equals(normalized)
				&& arrivalNanos >= old.arrivalNanos && arrivalNanos - old.arrivalNanos <= REPLAY_NANOS) return old;
		Entry entry = new Entry(++sequence, category, normalized, arrivalNanos, gameCycle);
		entries.addLast(entry);
		while (entries.size() > CAPACITY) entries.removeFirst();
		return entry;
	}

	synchronized List<Entry> snapshot(long nowNanos)
	{
		prune(nowNanos);
		return new ArrayList<>(entries);
	}

	synchronized void clear() { entries.clear(); }
	synchronized int size() { return entries.size(); }

	private void prune(long nowNanos)
	{
		while (!entries.isEmpty() && nowNanos - entries.peekFirst().arrivalNanos > TTL_NANOS) entries.removeFirst();
	}

	static final class Entry
	{
		final long sequence;
		final String category;
		final String normalizedText;
		final long arrivalNanos;
		final long gameCycle;
		Entry(long sequence, String category, String normalizedText, long arrivalNanos, long gameCycle)
		{
			this.sequence = sequence; this.category = category; this.normalizedText = normalizedText;
			this.arrivalNanos = arrivalNanos; this.gameCycle = gameCycle;
		}
	}
}
