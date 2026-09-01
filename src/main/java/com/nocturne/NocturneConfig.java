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
		name = "Track loot",
		description = "Show NPC drops and raid rewards in the Nocturne sidebar. Stored in memory only.",
		position = 0
	)
	default boolean trackNpcLoot()
	{
		return true;
	}

	@ConfigItem(
		keyName = "captureGroups",
		name = "Capture groups locally",
		description = "Show raid rosters and nearby names on drops. No names are sent to a server.",
		position = 1
	)
	default boolean captureGroups()
	{
		return true;
	}
}
