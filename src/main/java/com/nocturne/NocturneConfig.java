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
		description = "Capture local raid rosters and nearby observations. No names are sent to a server.",
		position = 1
	)
	default boolean captureGroups()
	{
		return true;
	}

	@ConfigItem(
		keyName = "showDiagnostics",
		name = "Show capture diagnostics",
		description = "Show item IDs, nearby observations and detailed roster checks locally.",
		position = 2
	)
	default boolean showDiagnostics() { return false; }

	@ConfigItem(
		keyName = "submitTestDrops",
		name = "Send drops to test intake",
		description = "Send your RSN, loot source, item IDs, quantities, RuneLite prices and timestamps to nocturne.events. No group names. Test storage only; no points.",
		warning = "This feature submits your IP address to a 3rd-party server not controlled or verified by RuneLite developers",
		position = 3
	)
	default boolean submitTestDrops() { return false; }
}
