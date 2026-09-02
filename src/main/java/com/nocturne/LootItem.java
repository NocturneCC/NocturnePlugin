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
	final String priceSource;
	final String valuationRuleId;
	final Integer valuationCatalogueVersion;
	final Integer finishedOutputItemId;
	final String finishedOutputItemName;
	final Integer finishedOutputMarketPriceGp;

	LootItem(int id, int quantity, String name)
	{
		this(id, quantity, name, 0);
	}

	LootItem(int id, int quantity, String name, int unitPriceGp)
	{
		this(id, quantity, name, unitPriceGp, unitPriceGp > 0 ? "runelite_market" : "price_unavailable",
			null, null, null, null, null);
	}

	private LootItem(int id, int quantity, String name, int unitPriceGp, String priceSource,
		String valuationRuleId, Integer valuationCatalogueVersion, Integer finishedOutputItemId,
		String finishedOutputItemName, Integer finishedOutputMarketPriceGp)
	{
		this.unitPriceGp = Math.max(0, unitPriceGp);
		this.id = id;
		this.quantity = quantity;
		this.name = name;
		this.priceSource = priceSource;
		this.valuationRuleId = valuationRuleId;
		this.valuationCatalogueVersion = valuationCatalogueVersion;
		this.finishedOutputItemId = finishedOutputItemId;
		this.finishedOutputItemName = finishedOutputItemName;
		this.finishedOutputMarketPriceGp = finishedOutputMarketPriceGp;
	}

	static LootItem market(int id, int quantity, String name, int price)
	{
		return new LootItem(id, quantity, name, price, price > 0 ? "runelite_market" : "price_unavailable",
			null, null, null, null, null);
	}

	static LootItem unpricedUntradeable(int id, int quantity, String name)
	{
		return new LootItem(id, quantity, name, 0, "unpriced_untradeable", null, null, null, null, null);
	}

	static LootItem derived(int id, int quantity, String name, int price, String source, String ruleId,
		int version, int outputId, String outputName, int outputPrice)
	{
		return new LootItem(id, quantity, name, price, source, ruleId, version,
			outputId, outputName, outputPrice);
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
					Math.addExact(existing.quantity, item.quantity), existing.name, existing.unitPriceGp,
					existing.priceSource, existing.valuationRuleId, existing.valuationCatalogueVersion,
					existing.finishedOutputItemId, existing.finishedOutputItemName,
					existing.finishedOutputMarketPriceGp));
			}
		}
		return List.copyOf(byId.values());
	}

	String signature()
	{
		return id + ":" + quantity;
	}
}
