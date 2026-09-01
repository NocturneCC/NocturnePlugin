package com.nocturne;

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

	String signature()
	{
		return id + ":" + quantity;
	}
}
