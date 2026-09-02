package com.nocturne;

import java.util.Collection;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

final class LootItem
{
	final int id;
	final int quantity;
	final String name;
	final int unitPriceGp;

	LootItem(int id, int quantity, String name)
	{
		this(id, quantity, name, 0);
	}

	LootItem(int id, int quantity, String name, int unitPriceGp)
	{
		this.unitPriceGp = Math.max(0, unitPriceGp);
		this.id = id;
		this.quantity = quantity;
		this.name = name;
	}

	static List<LootItem> consolidate(Collection<LootItem> items)
	{
		Map<Integer, LootItem> byId = new LinkedHashMap<>();
		for (LootItem item : items)
		{
			LootItem existing = byId.get(item.id);
			if (existing == null)
			{
				byId.put(item.id, item);
			}
			else
			{
				byId.put(item.id, new LootItem(item.id,
					Math.addExact(existing.quantity, item.quantity), existing.name, existing.unitPriceGp));
			}
		}
		return List.copyOf(byId.values());
	}

	String signature()
	{
		return id + ":" + quantity;
	}
}
