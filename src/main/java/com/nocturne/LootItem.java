package com.nocturne;

final class LootItem
{
	final int id;
	final int quantity;
	final String name;

	LootItem(int id, int quantity, String name)
	{
		this.id = id;
		this.quantity = quantity;
		this.name = name;
	}

	String signature()
	{
		return id + ":" + quantity;
	}
}
