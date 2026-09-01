package com.nocturne;

import net.runelite.client.config.Config;
import net.runelite.client.config.ConfigGroup;
import net.runelite.client.config.ConfigItem;

@ConfigGroup(NocturneConfig.GROUP)
public interface NocturneConfig extends Config
{
	String GROUP = "nocturne-companion";

	@ConfigItem(
		keyName = "trackNpcLoot",
		name = "Track NPC loot",
		description = "Show your detected NPC drops in the Nocturne sidebar. Stored in memory only.",
		position = 0
	)
	default boolean trackNpcLoot()
	{
		return true;
	}
}
