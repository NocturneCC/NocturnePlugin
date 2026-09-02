package com.nocturne;

import java.util.List;
import org.junit.Test;
import static org.junit.Assert.*;

public class LootItemTest
{
	@Test
	public void duplicateItemIdsAreSummedOnce()
	{
		LootRecord record = new LootRecord("First", "Gargoyle", List.of(
			item(2361, 2, "Adamantite bar"),
			item(2361, 4, "Adamantite bar")));

		assertEquals(1, record.items.size());
		assertEquals(2361, record.items.get(0).id);
		assertEquals(6, record.items.get(0).quantity);
	}

	@Test
	public void distinctVariantIdsAreNeverMerged()
	{
		List<LootItem> items = LootItem.consolidate(List.of(
			item(385, 2, "Shark"),
			item(386, 3, "Shark")));

		assertEquals(2, items.size());
		assertEquals(385, items.get(0).id);
		assertEquals(386, items.get(1).id);
	}

	@Test
	public void consolidationPreservesFirstOccurrenceOrdering()
	{
		List<LootItem> items = LootItem.consolidate(List.of(
			item(10, 1, "First"),
			item(20, 2, "Second"),
			item(10, 3, "First"),
			item(30, 4, "Third")));

		assertEquals(List.of(10, 20, 30), List.of(items.get(0).id, items.get(1).id, items.get(2).id));
		assertEquals(4, items.get(0).quantity);
	}

	private static LootItem item(int id, int quantity, String name)
	{
		return new LootItem(id, quantity, name, 100);
	}
}
