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
import java.util.concurrent.atomic.AtomicBoolean;
import javax.inject.Inject;
import javax.swing.SwingUtilities;
import lombok.extern.slf4j.Slf4j;
import net.runelite.api.Client;
import net.runelite.api.GameState;
import net.runelite.api.Player;
import net.runelite.api.ChatMessageType;
import net.runelite.api.gameval.VarbitID;
import net.runelite.api.events.ChatMessage;
import net.runelite.api.events.GameStateChanged;
import net.runelite.api.events.GameTick;
import net.runelite.api.events.WidgetLoaded;
import net.runelite.api.events.MenuOptionClicked;
import net.runelite.api.gameval.InterfaceID;
import net.runelite.api.widgets.Widget;
import net.runelite.client.callback.ClientThread;
import net.runelite.client.RuneLite;
import net.runelite.client.config.ConfigManager;
import net.runelite.client.eventbus.Subscribe;
import net.runelite.client.events.ConfigChanged;
import net.runelite.client.events.NpcLootReceived;
import net.runelite.api.events.PlayerSpawned;
import net.runelite.client.game.ItemManager;
import net.runelite.client.game.ItemStack;
import net.runelite.client.game.ChatIconManager;
import net.runelite.client.plugins.Plugin;
import net.runelite.client.plugins.PluginDescriptor;
import net.runelite.client.plugins.loottracker.LootReceived;
import net.runelite.http.api.loottracker.LootRecordType;
import net.runelite.client.ui.ClientToolbar;
import net.runelite.client.ui.DrawManager;
import net.runelite.client.ui.NavigationButton;
import net.runelite.client.ui.overlay.OverlayManager;

@Slf4j
@PluginDescriptor(
	name = "Nocturne",
 description = "Automatically checks nocturne.events for announcements and Clan Chat emojis; optional settings submit drops, raid presence, or completion times including public party RSNs",
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
	private OverlayManager overlayManager;

	@Inject
	private ChatIconManager chatIconManager;

	@Inject
	private ScheduledExecutorService executor;

	private volatile SubmissionService submissions;
	private volatile ChallengeObservationService challengeObservations;
	private volatile ChallengeCompletionCapture challengeCapture;
	private volatile PluginTrackingConfigService trackingConfig;
	private volatile RaidPresenceService raidPresence;
	private volatile AnnouncementService announcementService;
	private volatile AnnouncementToastOverlay announcementToastOverlay;
	private volatile EmojiSyncService emojiSyncService;
	private volatile EmojiRenderer emojiRenderer;
	private volatile RaidVerificationStatus raidVerification = RaidVerificationStatus.INACTIVE;
	private DerivedValueCatalogue derivedValues;
	private LootHistoryStore historyStore;
	private CompletableFuture<Void> historyWork = CompletableFuture.completedFuture(null);
	private String activeRsn;
	private volatile String historyRsn;
	private volatile long historyGeneration;
	private long gameTick;
	private String clientSessionId;
	private final AtomicBoolean emojiRegistrationDiagnosticLogged = new AtomicBoolean();
	private final LootEventDeduplicator lootEventDeduplicator = new LootEventDeduplicator();
	private final DoomRunTracker doomRunTracker = new DoomRunTracker();
	private final DoomCompletionHeaderCapture doomHeaderCapture = new DoomCompletionHeaderCapture();

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
		clientSessionId = java.util.UUID.randomUUID().toString();
		groups = new GroupTracker(client);
		gameTick = 0;
		groups.setDiagnosticsEnabled(config.showDiagnostics());
		derivedValues = DerivedValueCatalogue.load(gson);
		historyStore = new LootHistoryStore(RuneLite.RUNELITE_DIR.toPath()
			.resolve("nocturne").resolve("loot-history"), gson);
		submissions = new SubmissionService(http, gson);
		refreshTrackingCatalog(config.submitChallengeTimes());
		ChallengeObservationService observations = new ChallengeObservationService(http, gson, executor);
		challengeObservations = observations;
		challengeCapture = new ChallengeCompletionCapture(new ChallengeCompletionCorrelator(), observations,
			message -> clientThread.invoke(() -> { if (lifecycle == token) client.addChatMessage(ChatMessageType.GAMEMESSAGE, "Nocturne", message, null); }),
			message -> log.debug("{}", message), System::nanoTime, observation -> clientThread.invoke(() ->
			{
				if (lifecycle == token) withPanel(view -> view.markActivityAccountUnlinked(
					observation.activityKey, observation.reporterRsn, observation.occurredAt));
			}), (observation, status) -> clientThread.invoke(() ->
			{
				if (lifecycle == token) withPanel(view -> view.setActivityTiming(observation, status));
			}), context -> clientThread.invoke(() ->
			{
				if (lifecycle == token) withPanel(view -> view.activityContextStarted(context));
			}));
		raidPresence = new RaidPresenceService(http, gson);
		EmojiRenderer createdEmojiRenderer = new EmojiRenderer(client, chatIconManager, result ->
			log.debug("Nocturne emoji render diagnostic supported_message_type={} token_matched={} icon_registered={} node_rewritten={} refresh_requested={}",
				result.supportedMessageType, result.tokenMatched, result.iconRegistered,
				result.nodeRewritten, result.refreshRequested));
		emojiRenderer = createdEmojiRenderer;
		try
		{
			EmojiSyncService emojis = new EmojiSyncService(http, gson,
				RuneLite.RUNELITE_DIR.toPath().resolve("nocturne").resolve("emoji-cache-v1"),
				EmojiRenderer.clientThreadPublisher(EmojiRenderer.clientThreadDispatcher(clientThread),
					() -> lifecycle == token && emojiRenderer == createdEmojiRenderer, createdEmojiRenderer,
					result ->
					{
						if (emojiRegistrationDiagnosticLogged.compareAndSet(false, true))
							log.debug("Nocturne emoji asset activation asset_count={} client_thread_registration_task_entered={} slots_reserved={} icons_updated={} usable_mappings={} registration_failure_category={} failed_asset_ordinal={} reserved_slot={} image_width={} image_height={} image_type={} color_model_class={} exception_class={}",
								result.assetCount, result.clientThreadTaskEntered, result.slotsReserved,
								result.iconsUpdated, result.usableMappings, result.failureCategory,
								result.failedAssetOrdinal, result.reservedSlot, result.imageWidth,
								result.imageHeight, result.imageType, result.colorModelClass,
								result.exceptionClass);
					}),
				succeeded -> log.debug(succeeded
					? "Public Nocturne emoji manifest synchronized"
					: "Unable to synchronize public Nocturne emoji manifest"));
			emojiSyncService = emojis;
			emojis.start();
		}
		catch (RuntimeException error)
		{
			log.debug("Unable to initialize public clan emojis", error);
		}
		AnnouncementService createdAnnouncements = null;
		AnnouncementToastOverlay createdAnnouncementOverlay = new AnnouncementToastOverlay(client);
		announcementToastOverlay = createdAnnouncementOverlay;
		overlayManager.add(createdAnnouncementOverlay);
		try
		{
			createdAnnouncements = new AnnouncementService(http, gson,
				RuneLite.RUNELITE_DIR.toPath().resolve("nocturne").resolve("announcement-state-v1.json"),
				announcement -> clientThread.invoke(() ->
				{
					if (lifecycle == token && announcementToastOverlay == createdAnnouncementOverlay)
					{
						createdAnnouncementOverlay.enqueue(announcement);
					}
				}), current -> withPanel(view -> view.setAnnouncements(current)));
		}
		catch (RuntimeException error)
		{
			log.debug("Unable to initialize public clan announcements", error);
		}
		AnnouncementService announcements = createdAnnouncements;
		announcementService = announcements;
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
				@Override public void localOnly(LootRecord record) { persist(record, true); }
				@Override public void activityTiming(LootRecord record) { persist(record, true); }
				@Override public void completion(LootRecord record) { persist(record, false); }
				@Override public void mergeCompletionLoot(LootRecord merged, String replacedLootId)
				{ persistCompletionLootMerge(merged, replacedLootId); }
			});
			panel.setTracking(true);
			panel.setDiagnostics(config.showDiagnostics());
			panel.setSubmissionEnabled(config.submitTestDrops());
			navigation = NavigationButton.builder()
				.tooltip("Nocturne")
				.icon(NocturnePanel.createIcon())
				.priority(6)
				.panel(panel)
				.build();
			clientToolbar.addNavigation(navigation);
			if (announcements != null) announcements.start();
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
		clientSessionId = null;
		lootEventDeduplicator.clear();
		doomHeaderCapture.clear();
		doomRunTracker.reset();
		PluginTrackingConfigService catalog = trackingConfig;
		trackingConfig = null;
		if (catalog != null) catalog.close();
		SubmissionService sender = submissions;
		submissions = null;
		if (sender != null) sender.close();
		ChallengeCompletionCapture capture = challengeCapture;
		challengeCapture = null;
		if (capture != null) capture.close();
		ChallengeObservationService observations = challengeObservations;
		if (observations != null) observations.close();
		challengeObservations = null;
		RaidPresenceService presence = raidPresence;
		raidPresence = null;
		if (presence != null) presence.close();
		AnnouncementService announcements = announcementService;
		announcementService = null;
		if (announcements != null) announcements.close();
		AnnouncementToastOverlay announcementOverlay = announcementToastOverlay;
		announcementToastOverlay = null;
		if (announcementOverlay != null)
		{
			overlayManager.remove(announcementOverlay);
			announcementOverlay.close();
		}
		EmojiSyncService emojis = emojiSyncService;
		emojiSyncService = null;
		if (emojis != null) emojis.close();
		EmojiRenderer renderer = emojiRenderer;
		emojiRenderer = null;
		if (renderer != null) clientThread.invoke(renderer::clear);
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
		gameTick++;
		pollDoomCompletionHeader();
		updatePlayer();
		GroupTracker tracker = groups;
		if (tracker != null)
		{
			tracker.onTick();
			observeCompletion(null, tracker);
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
		ChallengeCompletionCapture capture = challengeCapture;
		if (capture != null)
			capture.onTick(config.submitChallengeTimes(), config.showDiagnostics(), java.time.Instant.now());
	}

	@Subscribe
	public void onGameStateChanged(GameStateChanged event)
	{
		if (event.getGameState() == GameState.LOGIN_SCREEN)
		{
			doomHeaderCapture.clear();
			finishDoomRun();
			doomRunTracker.reset();
			activeRsn = null;
			if (groups != null) groups.reset();
			if (challengeCapture != null) challengeCapture.reset();
			if (challengeObservations != null) challengeObservations.cancelPending();
			withPanel(NocturnePanel::clearPendingActivityLinks);
			raidVerification = RaidVerificationStatus.INACTIVE;
			if (submissions != null) submissions.cancelPending();
			withPanel(NocturnePanel::setLoggedOut);
		}
		else if (event.getGameState() == GameState.HOPPING)
		{
			doomHeaderCapture.clear();
			finishDoomRun();
			doomRunTracker.reset();
			withPanel(NocturnePanel::clearPendingActivityLinks);
			if (groups != null) groups.reset();
			if (challengeCapture != null) challengeCapture.reset();
			if (challengeObservations != null) challengeObservations.cancelPending();
			withPanel(view -> view.setGroup(GroupSnapshot.unavailable("World changed; group capture reset.")));
		}
		else if (event.getGameState() == GameState.LOGGED_IN)
		{
			updatePlayer();
		}
	}

	@Subscribe
	public void onWidgetLoaded(WidgetLoaded event)
	{
		if (event.getGroupId() != (InterfaceID.DomEndLevelUi.UNIVERSE >>> 16)
			|| client.getGameState() != GameState.LOGGED_IN) return;
		String text = doomHeaderText();
		Integer completed = DoomRunTracker.completedLevel(text);
		if (completed != null)
		{
			captureDoomLevel(completed);
		}
		// The header can still be populated or revised by the UI script after the
		// widget-loaded event. Keep a short baseline window even when an initial
		// value was readable, so a subsequent floor completion is not missed.
		doomHeaderCapture.arm(text);
	}

	private String doomHeaderText()
	{
		Widget header = client.getWidget(InterfaceID.DomEndLevelUi.DELVE_HEADER);
		return header == null ? null : header.getText();
	}

	private void pollDoomCompletionHeader()
	{
		if (!doomHeaderCapture.isPending()) return;
		Integer completed = doomHeaderCapture.poll(doomHeaderText());
		if (completed != null) captureDoomLevel(completed);
	}

	private void captureDoomLevel(int completed)
	{
		Player local = client.getLocalPlayer();
		if (client.getGameState() != GameState.LOGGED_IN || local == null || local.getName() == null) return;
		DoomRunTracker.Progress progress = doomRunTracker.completeLevel(completed, local.getName(),
			clientSessionId, gameTick, java.time.Instant.now());
		ChallengeCompletionCapture capture = challengeCapture;
		if (capture != null) capture.onDoomLevelProgress(progress,
			config.submitChallengeTimes(), config.showDiagnostics());
	}

	@Subscribe
	public void onMenuOptionClicked(MenuOptionClicked event)
	{
		Widget widget = event.getWidget();
		if (widget == null) return;
		int id = widget.getId();
		if (id == InterfaceID.DomEndLevelUi.BTN_CLAIM)
		{
			doomHeaderCapture.clear();
			finishDoomRun(DoomRunTracker.EndAction.CLAIM_REWARDS);
		}
		else if (id == InterfaceID.DomEndLevelUi.BTN_LEAVE)
		{
			doomHeaderCapture.clear();
			finishDoomRun(DoomRunTracker.EndAction.LEAVE_DELVING);
		}
	}

	@Subscribe
	public void onConfigChanged(ConfigChanged event)
	{
		if (NocturneConfig.GROUP.equals(event.getGroup()))
		{
			if (!config.submitTestDrops() && submissions != null) submissions.cancelPending();
			if (!config.submitChallengeTimes())
			{
				if (challengeCapture != null) challengeCapture.clearPartial();
				if (challengeObservations != null) challengeObservations.cancelPending();
			}
			refreshTrackingCatalog(config.submitChallengeTimes());
			GroupTracker diagnosticsTracker = groups;
			if (diagnosticsTracker != null) diagnosticsTracker.setDiagnosticsEnabled(config.showDiagnostics());
			withPanel(view ->
			{
				view.setDiagnostics(config.showDiagnostics());
				view.setSubmissionEnabled(config.submitTestDrops());
			});
		}
	}

	private synchronized void refreshTrackingCatalog(boolean consent)
	{
		if (!consent)
		{
			PluginTrackingConfigService previous = trackingConfig;
			trackingConfig = null;
			if (previous != null) previous.close();
			PluginActivityCatalog.resetToBundledDefaults();
			return;
		}
		if (trackingConfig != null) return;
		PluginTrackingConfigService catalog = new PluginTrackingConfigService(http, gson);
		trackingConfig = catalog;
		catalog.refresh();
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
		if (tracker != null) tracker.onPlayerObserved(event.getPlayer());
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
		// RuneLite's loot tracker emits activity/reward-container records (including
		// Gauntlet chests) as NPC. Keep those; player and pickpocket records remain out.
		if (type == LootRecordType.PLAYER || type == LootRecordType.PICKPOCKET)
		{
			return LootOrigin.REJECTED;
		}
		if (type == LootRecordType.NPC) return LootOrigin.NPC;
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

	static SubmissionStatus ineligibleStatus(List<LootItem> items)
	{
		return items.stream().anyMatch(item -> item.unitPriceGp > 0)
			? SubmissionStatus.INELIGIBLE : SubmissionStatus.UNPRICED;
	}

	static int compatibleItemPrice(long price)
	{
		// The submission/history protocol is intentionally int-bounded. Treat values
		// outside that protocol as unavailable instead of wrapping or aborting capture.
		return price > 0 && price <= Integer.MAX_VALUE ? (int) price : 0;
	}

	@Subscribe
	public void onChatMessage(ChatMessage event)
	{
		DoomRunTracker.Result death = doomRunTracker.finishOnDeathMessage(event.getMessage());
		publishDoomRun(death);
		GroupTracker tracker = groups;
		String category = event.getType().name();
		if (tracker != null && (event.getType() == ChatMessageType.GAMEMESSAGE
			|| event.getType() == ChatMessageType.FRIENDSCHATNOTIFICATION))
		{
			if (tracker.onGameMessage(event.getMessage()))
			{
				RaidPresenceService presence = raidPresence;
				if (presence != null) presence.submit(tracker.presenceReport("completion"), this::updateRaidVerification);
			}
			observeCompletion(category, event.getMessage(), tracker);
		}
		else if (CompletionMessageBuffer.eligibleCategory(category))
		{
			ChallengeCompletionCapture capture = challengeCapture;
			if (capture != null) capture.onSystemMessage(category, event.getMessage(), gameTick,
				config.submitChallengeTimes(), config.showDiagnostics(), java.time.Instant.now());
		}
		if (CompletionMessageBuffer.eligibleCategory(category))
		{
			Player local = client.getLocalPlayer();
			ChallengeCompletionCapture capture = challengeCapture;
			if (capture != null && local != null && local.getName() != null)
			{
				capture.onGauntletCompletion(category, event.getMessage(),
					client.getVarbitValue(VarbitID.PLAYER_IN_GAUNTLET) != 0
						&& client.getVarbitValue(VarbitID.GAUNTLET_CORRUPTED) != 0,
					local.getName(), clientSessionId, gameTick, config.submitChallengeTimes(),
					config.showDiagnostics(), java.time.Instant.now());
			}
		}
		EmojiRenderer renderer = emojiRenderer;
		if (renderer != null) renderer.onChatMessage(event);
	}

	private void observeCompletion(String message, GroupTracker tracker)
	{
		observeCompletion(message == null ? null : "GAMEMESSAGE", message, tracker);
	}

	private void observeCompletion(String category, String message, GroupTracker tracker)
	{
		ChallengeCompletionCapture capture = challengeCapture;
		if (capture == null || !config.submitChallengeTimes() || client.getGameState() != GameState.LOGGED_IN) return;
		Player local = client.getLocalPlayer();
		if (local == null || local.getName() == null) return;
		if (message == null)
		{
			capture.onEvidence(tracker.completionEvidence(), local.getName(), true,
				config.showDiagnostics(), java.time.Instant.now());
		}
		else
		{
			capture.onMessage(category, message, tracker.completionEvidence(), local.getName(), true,
				config.showDiagnostics(), java.time.Instant.now(), gameTick);
		}
	}

	private void recordLoot(String source, Collection<ItemStack> stacks, LootOrigin origin)
	{
		if (client.getGameState() != GameState.LOGGED_IN)
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
				int unitPriceGp = compatibleItemPrice(itemManager.getItemPrice(item.getId()));
				items.add(derivedValues.value(item.getId(), item.getQuantity(), name, unitPriceGp, tradeable,
					outputId -> compatibleItemPrice(itemManager.getItemPrice(outputId)),
					outputId -> itemManager.getItemComposition(outputId).getName()));
			}
		}
		if (items.isEmpty())
		{
			return;
		}
		items = LootItem.consolidate(items);
		if (lootEventDeduplicator.isDuplicate(source, items, gameTick)) return;
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
		PluginActivityCatalog.Entry activity = PluginActivityCatalog.matchSource(source);
		LootRecord record = new LootRecord(rsn, source, items, group, activity,
			origin == LootOrigin.NPC && activity == null);
		if (activity != null && "doom_of_mokhaiotl".equals(activity.activityKey))
			record.completionLinkId = doomRunTracker.consumeClaimedLootLink();
		boolean eligible = isSubmissionEligible(items, group);
		if (!eligible)
		{
			record.submission = ineligibleStatus(items);
		}
		persist(record, false);
		ChallengeCompletionCapture capture = challengeCapture;
		if (capture != null && activity != null && PluginActivityCatalog.completionComesFromLoot(activity)
			&& activity.lootTrackingEnabled
			&& config.submitChallengeTimes())
		{
			// Local history is queued before the asynchronous policy result can arrive.
			// Reuse the exact group snapshot used by the loot record; nearby-player data
			// is never promoted to a submission roster.
			capture.onActivityCompletion(source, rsn, group, clientSessionId, gameTick,
					true, config.showDiagnostics(), java.time.Instant.now(), record.id);
		}
		SubmissionService sender = submissions;
		Object token = lifecycle;
		if (sender != null && config.submitTestDrops() && eligible)
		{
			Consumer<SubmissionResult> update = result ->
			{
				persist(record.withSubmission(result), true);
				if (lifecycle == token) withPanel(view ->
				{
					view.setSubmission(record.id, result);
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

	private void finishDoomRun()
	{
		publishDoomRun(doomRunTracker.finishRun());
	}

	private void finishDoomRun(DoomRunTracker.EndAction action)
	{
		publishDoomRun(doomRunTracker.finishByAction(action));
	}

	private void publishDoomRun(DoomRunTracker.Result result)
	{
		if (result == null) return;
		ChallengeCompletionCapture capture = challengeCapture;
		if (capture != null) capture.finishDoomRun(result, config.submitChallengeTimes(), config.showDiagnostics());
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

	private synchronized void persistCompletionLootMerge(LootRecord merged, String replacedLootId)
	{
		LootHistoryStore store = historyStore;
		if (store == null) return;
		long generation = historyGeneration;
		historyWork = historyWork.handle((ignored, error) -> null).thenRunAsync(() ->
		{
			try
			{
				if (!store.mergeCompletionLoot(merged.completionLinkId, replacedLootId, merged)) store.append(merged);
				LootHistoryStore.Page stats = store.load(merged.rsn, 0, 1);
				withPanel(view ->
				{
					if (matchesHistorySelection(merged.rsn, generation))
						view.updateHistoryStats(merged.rsn, stats.totalCount, stats.storageBytes);
				});
			}
			catch (java.io.IOException e)
			{
				log.debug("Unable to persist completion-linked activity history", e);
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
