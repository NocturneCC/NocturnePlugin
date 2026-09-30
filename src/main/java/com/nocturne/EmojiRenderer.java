package com.nocturne;

import java.util.Collections;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.Map;
import net.runelite.api.ChatMessageType;
import net.runelite.api.Client;
import net.runelite.api.MessageNode;
import net.runelite.api.events.ChatMessage;
import net.runelite.client.game.ChatIconManager;

/** Client-thread-only received Clan Chat formatter. No message text is retained. */
final class EmojiRenderer
{
	static final int MAX_REPLACEMENTS = 5;
	static final int MAX_MESSAGE_CHARS = 5_000;
	static final int MAX_SESSION_ICONS = EmojiManifest.MAX_EMOJIS;

	interface IconRegistrar
	{
		int register(java.awt.image.BufferedImage image);
		int chatIndex(int icon);
	}

	private final Runnable refresh;
	private final IconRegistrar icons;
	private final Map<String, Integer> iconByDigest = new HashMap<>();
	private Map<String, Integer> active = Map.of();

	EmojiRenderer(Client client, ChatIconManager icons)
	{
		this(client::refreshChat, new IconRegistrar()
		{
			@Override public int register(java.awt.image.BufferedImage image) { return icons.registerChatIcon(image); }
			@Override public int chatIndex(int icon) { return icons.chatIconIndex(icon); }
		});
	}

	EmojiRenderer(Runnable refresh, IconRegistrar icons)
	{
		this.refresh = refresh;
		this.icons = icons;
	}

	void update(Map<String, EmojiAsset> assets)
	{
		Map<String, Integer> next = new LinkedHashMap<>();
		for (EmojiAsset asset : assets.values())
		{
			Integer icon = iconByDigest.get(asset.digest);
			if (icon == null)
			{
				if (iconByDigest.size() >= MAX_SESSION_ICONS) continue;
				icon = icons.register(asset.image);
				iconByDigest.put(asset.digest, icon);
			}
			next.put(asset.name, icons.chatIndex(icon));
		}
		active = Collections.unmodifiableMap(next);
		refresh.run();
	}

	void clear()
	{
		// RuneLite has no ownership-aware unregistration API. Keep the bounded icon
		// slots reserved for this client session, but immediately disable triggers.
		active = Map.of();
		refresh.run();
	}

	boolean onChatMessage(ChatMessage event)
	{
		if (event == null || !supports(event.getType())) return false;
		MessageNode node = event.getMessageNode();
		if (node == null) return false;
		String value = node.getValue();
		String formatted = format(value, active);
		if (formatted == null) return false;
		// Read the node's current value and update only this event. We never retain a
		// historical copy that could overwrite a later formatter's changes.
		node.setValue(formatted);
		refresh.run();
		return true;
	}

	static boolean supports(ChatMessageType type)
	{
		return type == ChatMessageType.PUBLICCHAT
			|| type == ChatMessageType.CLAN_CHAT
			|| type == ChatMessageType.CLAN_GUEST_CHAT
			|| type == ChatMessageType.FRIENDSCHAT
			|| type == ChatMessageType.PRIVATECHAT
			|| type == ChatMessageType.PRIVATECHATOUT;
	}

	static String format(String input, Map<String, Integer> registry)
	{
		if (input == null || input.length() > MAX_MESSAGE_CHARS || registry.isEmpty()) return null;
		StringBuilder output = null;
		int replacements = 0;
		int cursor = 0;
		while (cursor < input.length())
		{
			char character = input.charAt(cursor);
			if (character == '<')
			{
				int end = input.indexOf('>', cursor + 1);
				int next = end < 0 ? input.length() : end + 1;
				if (output != null) output.append(input, cursor, next);
				cursor = next;
				continue;
			}
			if (character != ':' || replacements >= MAX_REPLACEMENTS)
			{
				if (output != null) output.append(character);
				cursor++;
				continue;
			}
			int end = tokenEnd(input, cursor);
			if (end < 0)
			{
				if (output != null) output.append(character);
				cursor++;
				continue;
			}
			String name = input.substring(cursor + 1, end);
			Integer icon = registry.get(name);
			if (icon == null)
			{
				if (output != null) output.append(input, cursor, end + 1);
				cursor = end + 1;
				continue;
			}
			if (output == null)
			{
				output = new StringBuilder(input.length() + 16);
				output.append(input, 0, cursor);
			}
			output.append("<img=").append(icon).append('>');
			replacements++;
			cursor = end + 1;
		}
		return output == null ? null : output.toString();
	}

	private static int tokenEnd(String input, int start)
	{
		if (start > 0 && input.charAt(start - 1) == ':') return -1;
		int maximum = Math.min(input.length() - 1, start + 33);
		for (int index = start + 1; index <= maximum; index++)
		{
			char value = input.charAt(index);
			if (value == ':')
				return index == start + 1 || (index + 1 < input.length() && input.charAt(index + 1) == ':')
					? -1 : index;
			if (!((value >= 'a' && value <= 'z') || (value >= '0' && value <= '9') || value == '_'))
				return -1;
		}
		return -1;
	}

	int sessionIconCountForTest() { return iconByDigest.size(); }
	int activeTriggerCountForTest() { return active.size(); }
}
