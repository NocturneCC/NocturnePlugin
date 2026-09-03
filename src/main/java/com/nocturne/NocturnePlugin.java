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
import java.util.concurrent.CompletableFuture;
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
import net.runelite.client.RuneLite;
import net.runelite.client.config.ConfigManager;
import net.runelite.client.eventbus.Subscribe;
import net.runelite.client.events.ConfigChanged;
import net.runelite.client.events.NpcLootReceived;
import net.runelite.api.events.PlayerSpawned;
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
	enum LootOrigin
	{
		NPC, GENERIC_EVENT, RAID_EVENT, REJECTED
	}

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
	private volatile RaidPresenceService raidPresence;
	private volatile RaidVerificationStatus raidVerification = RaidVerificationStatus.INACTIVE;
	private DerivedValueCatalogue derivedValues;
	private LootHistoryStore historyStore;
	private CompletableFuture<Void> historyWork = CompletableFuture.completedFuture(null);
	private String activeRsn;
	private volatile String historyRsn;
	private volatile long historyGeneration;

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
		groups.setDiagnosticsEnabled(config.showDiagnostics());
		derivedValues = DerivedValueCatalogue.load(gson);
		historyStore = new LootHistoryStore(RuneLite.RUNELITE_DIR.toPath()
			.resolve("nocturne").resolve("loot-history"), gson);
		submissions = new SubmissionService(http, gson);
		raidPresence = new RaidPresenceService(http, gson);
		SwingUtilities.invokeLater(() ->
		{
			if (lifecycle != token)
			{
				return;
			}
			panel = new NocturnePanel(itemManager, new NocturnePanel.HistoryActions()
			{
				@Override public void loadOlder(String rsn, int offset) { loadHistory(rsn, offset, true, historyGeneration); }
				@Override public void clear(String rsn) { clearHistory(rsn); }
			});
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
		RaidPresenceService presence = raidPresence;
		raidPresence = null;
		if (presence != null) presence.close();
		raidVerification = RaidVerificationStatus.INACTIVE;
		groups = null;
		activeRsn = null;
		historyRsn = null;
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
		if (tracker != null && config.trackNpcLoot())
		{
			tracker.onTick();
			RaidPresenceService presence = raidPresence;
			if (presence != null && tracker.isActiveChambers())
			{
				presence.heartbeat(tracker.presenceReport("heartbeat"), this::updateRaidVerification);
			}
			GroupSnapshot snapshot = tracker.current();
			RaidDiagnostics diagnostics = tracker.diagnostics();
			InstanceObservedEvidence.Snapshot observed = tracker.instanceObserved();
			RaidVerificationStatus verification = raidVerification;
			withPanel(view ->
			{
				view.setGroup(snapshot);
				view.setRaidDiagnostics(diagnostics);
				view.setRaidEvidence(snapshot, observed, verification);
			});
		}
	}

	@Subscribe
	public void onGameStateChanged(GameStateChanged event)
	{
		if (event.getGameState() == GameState.LOGIN_SCREEN)
		{
			activeRsn = null;
			if (groups != null) groups.reset();
			raidVerification = RaidVerificationStatus.INACTIVE;
			if (submissions != null) submissions.cancelPending();
			withPanel(NocturnePanel::setLoggedOut);
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
			GroupTracker diagnosticsTracker = groups;
			if (diagnosticsTracker != null) diagnosticsTracker.setDiagnosticsEnabled(config.showDiagnostics());
			boolean enabled = config.trackNpcLoot();
			withPanel(view ->
			{
				view.setTracking(enabled);
				view.setDiagnostics(config.showDiagnostics());
				view.setSubmissionEnabled(config.submitTestDrops());
			});
		}
	}

	@Subscribe
	public void onNpcLootReceived(NpcLootReceived event)
	{
		String source = event.getNpc() == null ? null : event.getNpc().getName();
		recordLoot(source == null || source.isEmpty() ? "Unknown NPC" : source, event.getItems(), LootOrigin.NPC);
	}

	@Subscribe
	public void onPlayerSpawned(PlayerSpawned event)
	{
		GroupTracker tracker = groups;
		if (tracker != null && config.trackNpcLoot()) tracker.onPlayerObserved(event.getPlayer());
	}

	@Subscribe
	public void onLootReceived(LootReceived event)
	{
		LootOrigin origin = classifyLoot(event.getType(), event.getName());
		if (origin != LootOrigin.REJECTED)
		{
			String source = event.getName();
			recordLoot(source == null || source.isEmpty() ? "Unknown reward" : source, event.getItems(), origin);
		}
	}

	static LootOrigin classifyLoot(LootRecordType type, String source)
	{
		// NPC records are handled exclusively by NpcLootReceived. PLAYER and PICKPOCKET
		// retain their explicit rejection policy here.
		if (type != LootRecordType.EVENT)
		{
			return LootOrigin.REJECTED;
		}
		return RaidType.fromSource(source) == null ? LootOrigin.GENERIC_EVENT : LootOrigin.RAID_EVENT;
	}

	static boolean usesGroupContext(LootOrigin origin)
	{
		return origin != LootOrigin.GENERIC_EVENT;
	}

	static boolean isSubmissionEligible(List<LootItem> items)
	{
		return ScreenshotCapture.isLikelyEligible(items);
	}

	static boolean isSubmissionEligible(List<LootItem> items, GroupSnapshot group)
	{
		return isSubmissionEligible(items) && group.allowsSubmission();
	}

	@Subscribe
	public void onChatMessage(ChatMessage event)
	{
		GroupTracker tracker = groups;
		if (tracker != null && config.trackNpcLoot()
			&& (event.getType() == ChatMessageType.GAMEMESSAGE || event.getType() == ChatMessageType.FRIENDSCHATNOTIFICATION))
		{
			if (tracker.onGameMessage(event.getMessage()))
			{
				RaidPresenceService presence = raidPresence;
				if (presence != null) presence.submit(tracker.presenceReport("completion"), this::updateRaidVerification);
			}
		}
	}

	private void recordLoot(String source, Collection<ItemStack> stacks, LootOrigin origin)
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
				boolean tradeable = itemManager.getItemComposition(item.getId()).isTradeable();
				int unitPriceGp = itemManager.getItemPrice(item.getId());
				items.add(derivedValues.value(item.getId(), item.getQuantity(), name, unitPriceGp, tradeable,
					itemManager::getItemPrice, outputId -> itemManager.getItemComposition(outputId).getName()));
			}
		}
		if (items.isEmpty())
		{
			return;
		}
		items = LootItem.consolidate(items);
		GroupTracker tracker = groups;
		GroupSnapshot group = GroupSnapshot.unavailable(origin == LootOrigin.GENERIC_EVENT
			? "Generic reward; no raid group context." : "Group capture is off.");
		if (tracker != null && usesGroupContext(origin))
		{
			List<String> signature = items.stream().map(LootItem::signature)
				.collect(java.util.stream.Collectors.toList());
			if (origin == LootOrigin.RAID_EVENT && RaidType.fromSource(source) == RaidType.COX)
			{
				group = tracker.takeChambersReward(source, signature);
				if (group == null) return;
				RaidPresenceService presence = raidPresence;
				if (presence != null) presence.submit(tracker.presenceReport("reward_observed"),
					this::updateRaidVerification);
			}
			else
			{
				if (origin == LootOrigin.RAID_EVENT && !tracker.acceptRaidReward(source, signature)) return;
				group = tracker.forLoot(source);
			}
		}
		LootRecord record = new LootRecord(rsn, source, items, group);
		boolean eligible = isSubmissionEligible(items, group);
		if (!eligible)
		{
			record.submission = SubmissionStatus.INELIGIBLE;
		}
		persist(record, false);
		SubmissionService sender = submissions;
		Object token = lifecycle;
		if (sender != null && config.submitTestDrops() && eligible)
		{
			Consumer<SubmissionStatus> update = status ->
			{
				persist(record.withSubmission(status), true);
				if (lifecycle == token) withPanel(view ->
				{
					view.setSubmission(record.id, status);
				});
			};
			if (config.attachScreenshots())
			{
				boolean includeChat = config.includeChatInScreenshots();
				Rectangle viewport = new Rectangle(client.getViewportXOffset(), client.getViewportYOffset(),
					client.getViewportWidth(), client.getViewportHeight());
				drawManager.requestNextFrameListener(frame -> executor.submit(() ->
				{
					SubmissionScreenshot screenshot = ScreenshotCapture.encode(
						frame, viewport, includeChat, record, PluginMetadata.VERSION);
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

	private void updateRaidVerification(RaidVerificationStatus status)
	{
		raidVerification = status;
		clientThread.invoke(() ->
		{
			GroupTracker tracker = groups;
			if (tracker != null)
			{
				GroupSnapshot snapshot = tracker.current();
				InstanceObservedEvidence.Snapshot observed = tracker.instanceObserved();
				withPanel(view -> view.setRaidEvidence(snapshot, observed, status));
			}
		});
	}

	private void updatePlayer()
	{
		if (client.getGameState() == GameState.LOGGED_IN)
		{
			Player player = client.getLocalPlayer();
			if (player != null && player.getName() != null)
			{
				String rsn = player.getName();
				if (!rsn.equals(activeRsn))
				{
					activeRsn = rsn;
					historyRsn = rsn;
					long generation = ++historyGeneration;
					withPanel(view -> view.setPlayer(rsn, generation));
					loadHistory(rsn, 0, false, generation);
				}
			}
		}
	}

	private synchronized void persist(LootRecord record, boolean update)
	{
		LootHistoryStore store = historyStore;
		if (store == null) return;
		long generation = historyGeneration;
		historyWork = historyWork.handle((ignored, error) -> null).thenRunAsync(() ->
		{
			try
			{
				if (update && !store.update(record)) return;
				if (!update) store.append(record);
				LootHistoryStore.Page stats = store.load(record.rsn, 0, 1);
				if (!update) withPanel(view -> view.recordPersistedLoot(
					record, stats.totalCount, stats.storageBytes, generation));
				else withPanel(view ->
				{
					if (matchesHistorySelection(record.rsn, generation))
						view.updateHistoryStats(record.rsn, stats.totalCount, stats.storageBytes);
				});
			}
			catch (java.io.IOException e)
			{
				log.debug("Unable to persist local loot history", e);
				if (!update) withPanel(view -> view.recordUnsavedLoot(record, generation));
			}
		}, executor);
	}

	private synchronized void loadHistory(String rsn, int offset, boolean append, long generation)
	{
		if (rsn == null || historyStore == null) return;
		LootHistoryStore store = historyStore;
		historyWork = historyWork.handle((ignored, error) -> null).thenRunAsync(() ->
		{
			try
			{
				LootHistoryStore.Page page = store.load(rsn, offset, LootHistory.PAGE_SIZE);
				withPanel(view -> view.showHistory(rsn, page, append, generation));
				if (page.malformedRecords > 0) log.debug("Recovered local history with {} malformed records skipped", page.malformedRecords);
			}
			catch (java.io.IOException e)
			{
				log.debug("Unable to load local loot history", e);
				withPanel(view -> view.historyLoadFailed(rsn, generation));
			}
		}, executor);
	}

	private boolean matchesHistorySelection(String rsn, long generation)
	{
		return generation == historyGeneration && java.util.Objects.equals(rsn, historyRsn);
	}

	private synchronized void clearHistory(String rsn)
	{
		if (rsn == null || historyStore == null) return;
		LootHistoryStore store = historyStore;
		long generation = historyGeneration;
		historyWork = historyWork.handle((ignored, error) -> null).thenRunAsync(() ->
		{
			try
			{
				store.clear(rsn);
				withPanel(view -> view.historyCleared(rsn, generation));
			}
			catch (java.io.IOException e) { log.debug("Unable to clear local loot history", e); }
		}, executor);
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
