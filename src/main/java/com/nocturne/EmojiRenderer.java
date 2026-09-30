package com.nocturne;

import java.util.Collections;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.Consumer;
import net.runelite.api.ChatMessageType;
import net.runelite.api.Client;
import net.runelite.api.MessageNode;
import net.runelite.api.events.ChatMessage;
import net.runelite.client.callback.ClientThread;
import net.runelite.client.game.ChatIconManager;

/** Client-thread-only received Clan Chat formatter. No message text is retained. */
final class EmojiRenderer
{
	static final int MAX_REPLACEMENTS = 5;
	static final int MAX_MESSAGE_CHARS = 5_000;
	static final int MAX_SESSION_ICONS = EmojiManifest.MAX_EMOJIS;

	interface IconRegistrar
	{
		int reserve();
		void update(int icon, java.awt.image.BufferedImage image);
		int chatIndex(int icon);
	}

	interface ClientThreadDispatcher { void invoke(Runnable task); }

	private final Runnable refresh;
	private final IconRegistrar icons;
	private final Consumer<RenderDiagnostic> diagnostic;
	private final AtomicBoolean abyssalDiagnosticReported = new AtomicBoolean();
	private final Map<String, Integer> iconByDigest = new HashMap<>();
	private Map<String, Integer> active = Map.of();

	EmojiRenderer(Client client, ChatIconManager icons)
	{
		this(client, icons, ignored -> { });
	}

	EmojiRenderer(Client client, ChatIconManager icons, Consumer<RenderDiagnostic> diagnostic)
	{
		this(client::refreshChat, new IconRegistrar()
		{
			@Override public int reserve() { requireClientThread(client); return icons.reserveChatIcon(); }
			@Override public void update(int icon, java.awt.image.BufferedImage image)
			{
				requireClientThread(client);
				icons.updateChatIcon(icon, image);
			}
			@Override public int chatIndex(int icon) { requireClientThread(client); return icons.chatIconIndex(icon); }
		}, diagnostic);
	}

	private static void requireClientThread(Client client)
	{
		if (!client.isClientThread()) throw new IllegalStateException("emoji icon operation outside client thread");
	}

	EmojiRenderer(Runnable refresh, IconRegistrar icons)
	{
		this(refresh, icons, ignored -> { });
	}

	EmojiRenderer(Runnable refresh, IconRegistrar icons, Consumer<RenderDiagnostic> diagnostic)
	{
		this.refresh = refresh;
		this.icons = icons;
		this.diagnostic = diagnostic;
	}

	static ClientThreadDispatcher clientThreadDispatcher(ClientThread clientThread)
	{
		return task -> clientThread.invoke(task);
	}

	static EmojiSyncService.AssetListener clientThreadPublisher(ClientThreadDispatcher dispatcher,
		java.util.function.BooleanSupplier isCurrent, EmojiRenderer renderer,
		Consumer<RegistrationResult> registrationDiagnostic)
	{
		return (assets, complete) ->
		{
			try
			{
				dispatcher.invoke(() ->
				{
					RegistrationResult result;
					if (!isCurrent.getAsBoolean())
						result = RegistrationResult.failed(assets.size(), 0, 0, 0, "lifecycle_stale");
					else
					{
						try { result = renderer.update(assets); }
						catch (RuntimeException error)
						{
							result = RegistrationResult.failed(assets.size(), 0, 0, 0, "registration_exception");
						}
					}
					try { registrationDiagnostic.accept(result); }
					finally { complete.accept(result.success); }
				});
			}
			catch (RuntimeException error)
			{
				RegistrationResult result = RegistrationResult.notEntered(assets.size(),
					"client_thread_dispatch_failed");
				try { registrationDiagnostic.accept(result); }
				finally { complete.accept(false); }
			}
		};
	}

	RegistrationResult update(Map<String, EmojiAsset> assets)
	{
		Map<String, Integer> next = new LinkedHashMap<>();
		int supplied = assets.size();
		int reserved = 0;
		int updated = 0;
		if (assets.isEmpty())
		{
			active = Map.of();
			refresh.run();
			return RegistrationResult.failed(0, 0, 0, 0, "no_usable_mappings");
		}
		for (EmojiAsset asset : assets.values())
		{
			Integer icon = iconByDigest.get(asset.digest);
			if (icon == null)
			{
				if (iconByDigest.size() >= MAX_SESSION_ICONS)
					return RegistrationResult.failed(supplied, reserved, updated, next.size(), "session_icon_capacity");
				// ChatIconManager.registerChatIcon defers slot installation via
				// invokeLater, so chatIconIndex() would still be -1 here. The built-in
				// EmojiPlugin reserves the slot on the client thread and then updates it.
				try { icon = icons.reserve(); }
				catch (RuntimeException error)
				{
					return RegistrationResult.failed(supplied, reserved, updated, next.size(), "slot_reservation_failed");
				}
				reserved++;
				if (icon < 0)
					return RegistrationResult.failed(supplied, reserved, updated, next.size(), "invalid_reserved_slot");
				iconByDigest.put(asset.digest, icon);
			}
			try
			{
				icons.update(icon, asset.image);
				updated++;
			}
			catch (RuntimeException error)
			{
				return RegistrationResult.failed(supplied, reserved, updated, next.size(), "icon_update_failed");
			}
			int chatIndex;
			try { chatIndex = icons.chatIndex(icon); }
			catch (RuntimeException error)
			{
				return RegistrationResult.failed(supplied, reserved, updated, next.size(), "chat_index_lookup_failed");
			}
			if (chatIndex < 0)
				return RegistrationResult.failed(supplied, reserved, updated, next.size(), "invalid_chat_index");
			next.put(asset.name, chatIndex);
		}
		active = Collections.unmodifiableMap(next);
		refresh.run();
		return RegistrationResult.succeeded(supplied, reserved, updated, next.size());
	}

	static final class RegistrationResult
	{
		final int assetCount;
		final boolean clientThreadTaskEntered;
		final int slotsReserved;
		final int iconsUpdated;
		final int usableMappings;
		final String failureCategory;
		final boolean success;

		private RegistrationResult(int assetCount, boolean clientThreadTaskEntered, int slotsReserved,
			int iconsUpdated, int usableMappings, String failureCategory, boolean success)
		{
			this.assetCount = assetCount;
			this.clientThreadTaskEntered = clientThreadTaskEntered;
			this.slotsReserved = slotsReserved;
			this.iconsUpdated = iconsUpdated;
			this.usableMappings = usableMappings;
			this.failureCategory = failureCategory;
			this.success = success;
		}

		static RegistrationResult succeeded(int assets, int slots, int updated, int mappings)
		{
			return new RegistrationResult(assets, true, slots, updated, mappings, "none", true);
		}

		static RegistrationResult failed(int assets, int slots, int updated, int mappings, String category)
		{
			return new RegistrationResult(assets, true, slots, updated, mappings, category, false);
		}

		static RegistrationResult notEntered(int assets, String category)
		{
			return new RegistrationResult(assets, false, 0, 0, 0, category, false);
		}
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
		boolean supported = event != null && supports(event.getType());
		MessageNode node = event == null ? null : event.getMessageNode();
		String value = node == null ? null : node.getRuneLiteFormatMessage();
		if (value == null && node != null) value = node.getValue();
		boolean targetToken = value != null && value.length() <= MAX_MESSAGE_CHARS
			&& value.contains(":abyssaldagger:");
		boolean registered = active.containsKey("abyssaldagger")
			&& active.get("abyssaldagger") >= 0;
		boolean rewritten = false;
		boolean refreshed = false;
		if (supported && node != null)
		{
			String formatted = format(value, active);
			if (formatted != null)
			{
				// RuneLite processes this override after chat-message subscribers and
				// keeps the original node value intact. Later plugins can still build
				// on the current override rather than a stale copy.
				node.setRuneLiteFormatMessage(formatted);
				rewritten = true;
				refresh.run();
				refreshed = true;
			}
		}
		if (targetToken && abyssalDiagnosticReported.compareAndSet(false, true))
			diagnostic.accept(new RenderDiagnostic(supported, true, registered, rewritten, refreshed));
		return rewritten;
	}

	static final class RenderDiagnostic
	{
		final boolean supportedMessageType;
		final boolean tokenMatched;
		final boolean iconRegistered;
		final boolean nodeRewritten;
		final boolean refreshRequested;

		RenderDiagnostic(boolean supportedMessageType, boolean tokenMatched, boolean iconRegistered,
			boolean nodeRewritten, boolean refreshRequested)
		{
			this.supportedMessageType = supportedMessageType;
			this.tokenMatched = tokenMatched;
			this.iconRegistered = iconRegistered;
			this.nodeRewritten = nodeRewritten;
			this.refreshRequested = refreshRequested;
		}
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
