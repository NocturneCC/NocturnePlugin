package com.nocturne;

import com.google.inject.Provides;
import com.google.gson.Gson;
import okhttp3.OkHttpClient;
import java.awt.Rectangle;
import java.util.ArrayList;
import java.util.Collection;
import java.util.List;
import java.util.function.Consumer;
import java.util.concurrent.ScheduledExecutorService;
import javax.inject.Inject;
import javax.swing.SwingUtilities;
import lombok.extern.slf4j.Slf4j;
import net.runelite.api.Client;
import net.runelite.api.GameState;
import net.runelite.api.Player;
import net.runelite.api.ChatMessageType;
import net.runelite.api.events.ChatMessage;
import net.runelite.api.events.GameStateChanged;
import net.runelite.api.events.GameTick;
import net.runelite.client.callback.ClientThread;
import net.runelite.client.config.ConfigManager;
import net.runelite.client.eventbus.Subscribe;
import net.runelite.client.events.ConfigChanged;
import net.runelite.client.events.NpcLootReceived;
import net.runelite.client.game.ItemManager;
import net.runelite.client.game.ItemStack;
import net.runelite.client.plugins.Plugin;
import net.runelite.client.plugins.PluginDescriptor;
import net.runelite.client.plugins.loottracker.LootReceived;
import net.runelite.http.api.loottracker.LootRecordType;
import net.runelite.client.ui.ClientToolbar;
import net.runelite.client.ui.DrawManager;
import net.runelite.client.ui.NavigationButton;

@Slf4j
@PluginDescriptor(
	name = "Nocturne",
	description = "View loot and locally captured raid groups",
	tags = {"nocturne", "clan", "loot"}
)
public class NocturnePlugin extends Plugin
{
	@Inject
	private Client client;

	@Inject
	private ClientThread clientThread;

	@Inject
	private ClientToolbar clientToolbar;

	@Inject
	private ItemManager itemManager;

	@Inject
	private NocturneConfig config;

	@Inject
	private OkHttpClient http;

	@Inject
	private Gson gson;

	@Inject
	private DrawManager drawManager;

	@Inject
	private ScheduledExecutorService executor;

	private volatile SubmissionService submissions;

	// The lifecycle token prevents queued UI work from reviving a disabled plugin.
	private volatile Object lifecycle;
	private volatile GroupTracker groups;
	// Panel and navigation are accessed only on Swing's event dispatch thread.
	private NocturnePanel panel;
	private NavigationButton navigation;

	@Override
	protected void startUp()
	{
		Object token = new Object();
		lifecycle = token;
		groups = new GroupTracker(client);
		submissions = new SubmissionService(http, gson);
		SwingUtilities.invokeLater(() ->
		{
			if (lifecycle != token)
			{
				return;
			}
			panel = new NocturnePanel(itemManager);
			panel.setTracking(config.trackNpcLoot());
			panel.setDiagnostics(config.showDiagnostics());
			panel.setSubmissionEnabled(config.submitTestDrops());
			navigation = NavigationButton.builder()
				.tooltip("Nocturne")
				.icon(NocturnePanel.createIcon())
				.priority(6)
				.panel(panel)
				.build();
			clientToolbar.addNavigation(navigation);
			clientThread.invoke(() ->
			{
				if (lifecycle == token)
				{
					updatePlayer();
				}
			});
		});
		log.debug("Nocturne local loot tracker started");
	}

	@Override
	protected void shutDown()
	{
		lifecycle = null;
		SubmissionService sender = submissions;
		submissions = null;
		if (sender != null) sender.close();
		groups = null;
		SwingUtilities.invokeLater(() ->
		{
			if (navigation != null)
			{
				clientToolbar.removeNavigation(navigation);
				navigation = null;
			}
			panel = null;
		});
		log.debug("Nocturne stopped");
	}

	@Subscribe
	public void onGameTick(GameTick event)
	{
		updatePlayer();
		GroupTracker tracker = groups;
		if (tracker != null && config.captureGroups())
		{
			tracker.onTick();
			GroupSnapshot snapshot = tracker.current();
			withPanel(view -> view.setGroup(snapshot));
		}
	}

	@Subscribe
	public void onGameStateChanged(GameStateChanged event)
	{
		if (event.getGameState() == GameState.LOGIN_SCREEN)
		{
			if (groups != null) groups.reset();
			if (submissions != null) submissions.cancelPending();
			withPanel(view -> view.setPlayer(null));
		}
		else if (event.getGameState() == GameState.HOPPING)
		{
			if (groups != null) groups.reset();
			withPanel(view -> view.setGroup(GroupSnapshot.unavailable("World changed; group capture reset.")));
		}
		else if (event.getGameState() == GameState.LOGGED_IN)
		{
			updatePlayer();
		}
	}

	@Subscribe
	public void onConfigChanged(ConfigChanged event)
	{
		if (NocturneConfig.GROUP.equals(event.getGroup()))
		{
			if (!config.submitTestDrops() && submissions != null) submissions.cancelPending();
			boolean enabled = config.trackNpcLoot();
			withPanel(view ->
			{
				view.setTracking(enabled);
				view.setDiagnostics(config.showDiagnostics());
				view.setSubmissionEnabled(config.submitTestDrops());
			});
			if ("captureGroups".equals(event.getKey()))
			{
				GroupTracker tracker = groups;
				clientThread.invoke(() ->
				{
					if (tracker != null && groups == tracker) tracker.reset();
				});
				withPanel(view -> view.setGroup(GroupSnapshot.unavailable(
					config.captureGroups() ? "Waiting for group capture." : "Group capture is off.")));
			}
		}
	}

	@Subscribe
	public void onNpcLootReceived(NpcLootReceived event)
	{
		String source = event.getNpc() == null ? null : event.getNpc().getName();
		recordLoot(source == null || source.isEmpty() ? "Unknown NPC" : source, event.getItems(), false);
	}

	@Subscribe
	public void onLootReceived(LootReceived event)
	{
		// Raid reward interfaces use LootReceived rather than an NPC death.
		// Do not also accept generic NPC events here: that would duplicate the listener above.
		if (event.getType() == LootRecordType.EVENT && RaidType.fromSource(event.getName()) != null)
		{
			recordLoot(event.getName(), event.getItems(), true);
		}
	}

	@Subscribe
	public void onChatMessage(ChatMessage event)
	{
		GroupTracker tracker = groups;
		if (tracker != null && config.captureGroups()
			&& (event.getType() == ChatMessageType.GAMEMESSAGE || event.getType() == ChatMessageType.FRIENDSCHATNOTIFICATION))
		{
			tracker.onGameMessage(event.getMessage());
		}
	}

	private void recordLoot(String source, Collection<ItemStack> stacks, boolean raidReward)
	{
		if (!config.trackNpcLoot() || client.getGameState() != GameState.LOGGED_IN)
		{
			return;
		}
		Player player = client.getLocalPlayer();
		if (player == null || player.getName() == null || stacks.isEmpty())
		{
			return;
		}

		// Copy game data on the client thread; never pass NPC or Player objects to Swing.
		String rsn = player.getName();
		List<LootItem> items = new ArrayList<>();
		for (ItemStack item : stacks)
		{
			if (item.getQuantity() > 0)
			{
				String name = itemManager.getItemComposition(item.getId()).getName();
				int unitPriceGp = itemManager.getItemPrice(item.getId());
				items.add(new LootItem(item.getId(), item.getQuantity(), name, unitPriceGp));
			}
		}
		if (items.isEmpty())
		{
			return;
		}
		GroupTracker tracker = groups;
		GroupSnapshot group = GroupSnapshot.unavailable("Group capture is off.");
		if (tracker != null && config.captureGroups())
		{
			if (raidReward && !tracker.acceptRaidReward(source, items.stream().map(LootItem::signature).collect(java.util.stream.Collectors.toList()))) return;
			group = tracker.forLoot(source);
		}
		LootRecord record = new LootRecord(rsn, source, items, group);
		withPanel(view -> view.recordLoot(record));
		SubmissionService sender = submissions;
		Object token = lifecycle;
		if (sender != null && config.submitTestDrops())
		{
			Consumer<SubmissionStatus> update = status ->
			{
				if (lifecycle == token) withPanel(view -> view.setSubmission(record.id, status));
			};
			if (config.attachScreenshots() && ScreenshotCapture.isLikelyEligible(items))
			{
				boolean includeChat = config.includeChatInScreenshots();
				Rectangle viewport = new Rectangle(client.getViewportXOffset(), client.getViewportYOffset(),
					client.getViewportWidth(), client.getViewportHeight());
				drawManager.requestNextFrameListener(frame -> executor.submit(() ->
				{
					SubmissionScreenshot screenshot = ScreenshotCapture.encode(frame, viewport, includeChat);
					SubmissionService active = submissions;
					if (lifecycle == token && active == sender)
					{
						active.submit(record, screenshot, update);
					}
				}));
			}
			else
			{
				sender.submit(record, update);
			}
		}
		log.debug("Nocturne detected loot: {} from {}, {} item stacks", rsn, source, items.size());
	}

	private void updatePlayer()
	{
		if (client.getGameState() == GameState.LOGGED_IN)
		{
			Player player = client.getLocalPlayer();
			if (player != null && player.getName() != null)
			{
				String rsn = player.getName();
				withPanel(view -> view.setPlayer(rsn));
			}
		}
	}

	private void withPanel(Consumer<NocturnePanel> action)
	{
		Object token = lifecycle;
		if (token == null)
		{
			return;
		}
		SwingUtilities.invokeLater(() ->
		{
			if (lifecycle == token && panel != null)
			{
				action.accept(panel);
			}
		});
	}

	@Provides
	NocturneConfig provideConfig(ConfigManager manager)
	{
		return manager.getConfig(NocturneConfig.class);
	}
}
