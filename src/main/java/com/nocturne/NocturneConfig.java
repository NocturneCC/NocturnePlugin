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
		description = "Show and store loot locally. During CoX/CM, automatically send only your own RSN and raid-presence facts for verification.",
		warning = "This feature submits your IP address to a 3rd-party server not controlled or verified by RuneLite developers",
		position = 0
	)
	default boolean trackNpcLoot()
	{
		return false;
	}

	@ConfigItem(
		keyName = "showDiagnostics",
		name = "Show capture diagnostics",
		description = "Show item IDs and name-free raid widget structure diagnostics locally. Disabled by default.",
		position = 1
	)
	default boolean showDiagnostics() { return false; }

	@ConfigItem(
		keyName = "submitTestDrops",
		name = "Send drops to test intake",
		description = "Send your RSN, loot source, item IDs, quantities, RuneLite prices and timestamps to nocturne.events. No group names. Eligible reports enter pending review; no points are awarded automatically.",
		warning = "This feature submits your IP address to a 3rd-party server not controlled or verified by RuneLite developers",
		position = 2
	)
	default boolean submitTestDrops() { return false; }

	@ConfigItem(
		keyName = "attachScreenshots",
		name = "Attach drop screenshots",
		description = "Attach a compressed RuneLite canvas image to likely point-eligible submissions. Images are held in memory only until sent.",
		warning = "This feature submits your IP address to a 3rd-party server not controlled or verified by RuneLite developers",
		position = 3
	)
	default boolean attachScreenshots() { return false; }

	@ConfigItem(
		keyName = "includeChatInScreenshots",
		name = "Include chat in screenshots",
		description = "Include the full RuneLite canvas, including chat. Off crops submissions to the game viewport to protect private messages.",
		warning = "This feature submits your IP address to a 3rd-party server not controlled or verified by RuneLite developers",
		position = 4
	)
	default boolean includeChatInScreenshots() { return false; }
}
