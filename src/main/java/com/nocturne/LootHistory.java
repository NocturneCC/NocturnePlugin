package com.nocturne;

import java.util.ArrayList;
import java.util.List;
import java.util.Objects;

/** Bounded display window, owned by the Swing event dispatch thread. */
final class LootHistory
{
	static final int PAGE_SIZE = 50;
	private final List<LootRecord> records = new ArrayList<>();
	private String player;
	private int count;
	private long storageBytes;
	private boolean hasOlder;
	private boolean storageFailure;
	private int displayLimit = PAGE_SIZE;

	boolean setPlayer(String rsn)
	{
		// Logout is not an account selection change; keep the last selected feed.
		if (rsn == null) return false;
		if (Objects.equals(player, rsn))
		{
			return false;
		}
		player = rsn;
		records.clear();
		count = 0;
		storageBytes = 0;
		storageFailure = false;
		hasOlder = false;
		displayLimit = PAGE_SIZE;
		return true;
	}

	void add(LootRecord record)
	{
		setPlayer(record.rsn);
		records.add(0, record);
		while (records.size() > displayLimit) records.remove(records.size() - 1);
		count++;
	}

	void replace(LootHistoryStore.Page page)
	{
		records.clear();
		records.addAll(page.records);
		count = page.totalCount;
		storageBytes = page.storageBytes;
		storageFailure = false;
		hasOlder = page.hasOlder;
		displayLimit = Math.max(PAGE_SIZE, records.size());
	}

	void appendOlder(LootHistoryStore.Page page)
	{
		records.addAll(page.records);
		displayLimit = records.size();
		count = page.totalCount;
		storageBytes = page.storageBytes;
		hasOlder = page.hasOlder;
	}

	void updateStats(int totalCount, long bytes)
	{
		count = totalCount;
		storageBytes = bytes;
		storageFailure = false;
	}

	void markStorageFailure() { storageFailure = true; }

	void clear()
	{
		records.clear();
		count = 0;
		storageBytes = 0;
		storageFailure = false;
		hasOlder = false;
		displayLimit = PAGE_SIZE;
	}

	int getCount()
	{
		return count;
	}

	List<LootRecord> getRecords()
	{
		return List.copyOf(records);
	}

	String getPlayer() { return player; }
	long getStorageBytes() { return storageBytes; }
	boolean hasOlder() { return hasOlder; }
	boolean hasStorageFailure() { return storageFailure; }
}
