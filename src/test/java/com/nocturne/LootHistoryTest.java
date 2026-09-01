package com.nocturne;

import java.util.List;
import org.junit.Test;
import static org.junit.Assert.*;

public class LootHistoryTest
{
	@Test
	public void retainsNewestFiftyWithoutMergingIdenticalDrops()
	{
		LootHistory history = new LootHistory();
		for (int i = 0; i < 60; i++)
		{
			history.add(drop("First", "NPC " + i));
		}
		assertEquals(60, history.getCount());
		assertEquals(50, history.getRecords().size());
		assertEquals("NPC 59", history.getRecords().get(0).source);
		assertEquals("NPC 10", history.getRecords().get(49).source);
		history.add(drop("First", "NPC 59"));
		assertEquals(61, history.getCount());
		assertEquals("NPC 59", history.getRecords().get(1).source);
	}

	@Test
	public void repeatedIdentityUpdatesPreserveLootButAccountSwitchClearsIt()
	{
		LootHistory history = new LootHistory();
		history.add(drop("First", "Goblin"));
		assertFalse(history.setPlayer("First"));
		assertEquals(1, history.getCount());
		history.add(drop("Second", "Cow"));
		assertEquals(1, history.getCount());
		assertEquals("Second", history.getRecords().get(0).rsn);
	}

	@Test
	public void logoutAndManualClearResetCountsAndRecords()
	{
		LootHistory history = new LootHistory();
		history.add(drop("First", "Goblin"));
		assertTrue(history.setPlayer(null));
		assertEquals(0, history.getCount());
		assertTrue(history.getRecords().isEmpty());
		history.add(drop("First", "Cow"));
		history.clear();
		assertEquals(0, history.getCount());
		assertTrue(history.getRecords().isEmpty());
	}

	private static LootRecord drop(String rsn, String source)
	{
		return new LootRecord(rsn, source, List.of("1 x Bones"));
	}
}
