package com.nocturne;

import java.util.ArrayDeque;
import java.util.Deque;
import java.util.List;
import java.util.Objects;

/** In-memory history, owned by the Swing event dispatch thread. */
final class LootHistory
{
	static final int LIMIT = 50;
	private final Deque<LootRecord> records = new ArrayDeque<>();
	private String player;
	private int count;

	boolean setPlayer(String rsn)
	{
		if (Objects.equals(player, rsn))
		{
			return false;
		}
		player = rsn;
		clear();
		return true;
	}

	void add(LootRecord record)
	{
		setPlayer(record.rsn);
		records.addFirst(record);
		while (records.size() > LIMIT)
		{
			records.removeLast();
		}
		count++;
	}

	void clear()
	{
		records.clear();
		count = 0;
	}

	int getCount()
	{
		return count;
	}

	List<LootRecord> getRecords()
	{
		return List.copyOf(records);
	}
}
