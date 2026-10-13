package com.nocturne;

import java.awt.Component;
import java.awt.Container;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import javax.swing.AbstractButton;
import javax.swing.JLabel;
import javax.swing.JTextArea;
import javax.swing.SwingUtilities;
import org.junit.BeforeClass;
import org.junit.Test;
import static org.junit.Assert.*;

public class NocturnePanelPrivacyTest
{
	@BeforeClass public static void headless() { System.setProperty("java.awt.headless", "true"); }

	@Test
	public void defaultPanelHidesInternalTelemetryAndKeepsMemberContent() throws Exception
	{
		NocturnePanel panel = panel();
		String visible = visibleText(panel);
		assertTrue(visible.contains("NOCTURNE"));
		assertFalse(visible.contains("COMPANION"));
		assertFalse(visible.contains("PREVIEW"));
		assertFalse(visible.contains(PluginMetadata.VERSION));
		assertTrue(visible.contains("Tester"));
		assertTrue(visible.contains("CLAN ANNOUNCEMENTS"));
		assertTrue(visible.contains("Clan news"));
		assertTrue(visible.contains("RECENT ACTIVITY"));
		assertFalse(visible.contains("Loot tracking"));
		assertFalse(visible.contains("Test submissions"));
		assertFalse(visible.contains("Group unavailable"));
		assertFalse(visible.contains("GAME_ROSTER"));
		assertFalse(visible.contains("INSTANCE_OBSERVED"));
		assertFalse(visible.contains("NOCTURNE_VERIFIED"));
		assertFalse(visible.contains("Proposed scoring"));
		assertFalse(visible.contains("roster preview"));
		assertFalse(visible.contains("loot events"));
		assertFalse(visible.contains("Stored locally per character"));
	}

	@Test
	public void emptyStateAndHistoryControlsRemainMemberFriendly() throws Exception
	{
		NocturnePanel panel = emptyPanel();
		String visible = visibleText(panel);
		assertTrue(visible.contains("No recent activity yet."));
		assertTrue(visible.contains("Load 50 older events"));
		assertTrue(visible.contains("Clear local history"));
		assertFalse(visible.contains("Defeat an NPC"));
	}

	@Test
	public void nonChallengeCatalogLootAppearsInRecentActivity() throws Exception
	{
		NocturnePanel panel = emptyPanel();
		PluginActivityCatalog.Entry nightmare = PluginActivityCatalog.matchSource("The Nightmare");
		LootRecord record = new LootRecord("nightmare", "2026-10-12T12:00:00Z", "Tester",
			"The Nightmare", List.of(),
			GroupSnapshot.unavailable("fixture"), SubmissionStatus.LOCAL, nightmare);
		onEdt(() -> panel.showHistory("Tester", new LootHistoryStore.Page(List.of(record), 1, 0, true, 0), false));
		String visible = visibleText(panel);
		assertTrue(visible.contains("The Nightmare"));
	}

	@Test
	public void ordinaryNpcLootStaysInHistoryButIsHiddenFromDefaultActivityFeed() throws Exception
	{
		NocturnePanel panel = emptyPanel();
		LootRecord ordinaryNpc = new LootRecord("npc", "2026-10-12T12:00:00Z", "Tester",
			"Hill Giant", List.of(), GroupSnapshot.unavailable("fixture"), SubmissionStatus.LOCAL,
		null, null, null, true);
		PluginActivityCatalog.Entry nightmare = PluginActivityCatalog.matchSource("The Nightmare");
		LootRecord activity = new LootRecord("activity", "2026-10-12T12:01:00Z", "Tester",
			"The Nightmare", List.of(), GroupSnapshot.unavailable("fixture"), SubmissionStatus.LOCAL, nightmare);
		onEdt(() -> panel.showHistory("Tester",
			new LootHistoryStore.Page(List.of(ordinaryNpc, activity), 2, 0, true, 0), false));
		String visible = visibleText(panel);
		assertFalse(visible.contains("Hill Giant"));
		assertTrue(visible.contains("The Nightmare"));
		assertTrue(panel.historySummary().startsWith("2 loot events"));
	}

	@Test
	public void activityTimeConsentTooltipIsShortAndWrapped() throws Exception
	{
		String description = NocturneConfig.class.getMethod("submitChallengeTimes")
			.getAnnotation(net.runelite.client.config.ConfigItem.class).description();
		assertTrue(description.startsWith("<html>"));
		assertTrue(description.contains("<br>"));
		assertTrue(description.contains("public party RSNs"));
		assertTrue(description.length() < 190);
	}

	@Test
	public void unlinkedTimingNoticeDoesNotEraseAcceptedDropStatus() throws Exception
	{
		java.util.concurrent.atomic.AtomicReference<LootRecord> localOnlyUpdate = new java.util.concurrent.atomic.AtomicReference<>();
		NocturnePanel panel = new NocturnePanel(null, new NocturnePanel.HistoryActions()
		{
			@Override public void loadOlder(String rsn, int offset) { }
			@Override public void clear(String rsn) { }
			@Override public void localOnly(LootRecord record) { localOnlyUpdate.set(record); }
		});
		Instant occurred = Instant.parse("2026-10-12T12:00:00Z");
		PluginActivityCatalog.Entry activity = PluginActivityCatalog.matchSource("The Nightmare");
		LootRecord record = new LootRecord("nightmare-local", occurred.toString(), "De Lena",
			"The Nightmare", List.of(),
			GroupSnapshot.unavailable("fixture"), SubmissionStatus.ACCEPTED, activity);
		onEdt(() ->
		{
			panel.setPlayer("De Lena", 10);
			panel.showHistory("De Lena", new LootHistoryStore.Page(List.of(), 0, 0, true, 0), false, 10);
			// Exercise the race where the async intake result arrives before local history append.
			panel.markActivityAccountUnlinked("nightmare", "De Lena", occurred);
			panel.recordPersistedLoot(record, 1, 128, 10);
			assertEquals(SubmissionStatus.ACCEPTED, record.withSubmission(SubmissionStatus.ACCEPTED).submission);
			panel.setSubmission(record.id, SubmissionStatus.ACCEPTED);
		});
		String visible = visibleText(panel);
		assertTrue(visible.contains("The Nightmare"));
		assertTrue(visible.contains("Local only — account not linked"));
		assertTrue(visible.contains(SubmissionStatus.ACCEPTED.label));
		assertEquals(SubmissionStatus.ACCEPTED, record.submission);
		assertNotNull(localOnlyUpdate.get());
		assertEquals("Local only — account not linked", localOnlyUpdate.get().activityTrackingNotice);
		assertEquals(SubmissionStatus.ACCEPTED, localOnlyUpdate.get().submission);
	}

	@Test
	public void completionBeforeChestAttachesToMatchingRecentActivityAndKeepsDropStatusSeparate() throws Exception
	{
		java.util.concurrent.atomic.AtomicReference<LootRecord> persistedTiming = new java.util.concurrent.atomic.AtomicReference<>();
		NocturnePanel panel = new NocturnePanel(null, new NocturnePanel.HistoryActions()
		{
			@Override public void loadOlder(String rsn, int offset) { }
			@Override public void clear(String rsn) { }
			@Override public void activityTiming(LootRecord record) { persistedTiming.set(record); }
		});
		Instant completionAt = Instant.parse("2026-10-13T00:12:31Z");
		BossCompletionContext context = BossCompletionContext.fromActiveSoloActivity(
			"corrupted_gauntlet", "corrupted_gauntlet", "Tester", "fixture-session", 70,
			1_000_000_000L, completionAt);
		ChallengeObservation observation = ChallengeObservation.createTracked(context, 513_000L);
		assertNotNull(observation);
		PluginActivityCatalog.Entry activity = PluginActivityCatalog.matchSource("Corrupted Gauntlet");
		LootRecord chest = new LootRecord("cg-chest", "2026-10-13T00:12:36Z", "Tester",
			"Corrupted Gauntlet", List.of(), context.roster, SubmissionStatus.ACCEPTED, activity);
		onEdt(() ->
		{
			panel.setPlayer("Tester", 7);
			panel.showHistory("Tester", new LootHistoryStore.Page(List.of(), 0, 0, false, 0), false, 7);
			// Completion state arrives before the asynchronous loot-history append.
			panel.setActivityTiming(observation, ActivityTimingStatus.SENDING);
			panel.recordPersistedLoot(chest, 1, 100, 7);
			LootRecord attached = persistedTiming.get();
			assertNotNull(attached);
			assertEquals(Long.valueOf(513_000L), attached.activityDurationMillis);
			assertEquals(ActivityTimingStatus.SENDING, attached.activityTimingStatus);
			assertEquals(SubmissionStatus.ACCEPTED, attached.submission);
			panel.setActivityTiming(observation, ActivityTimingStatus.ACCEPTED);
			LootRecord updated = persistedTiming.get();
			assertEquals(ActivityTimingStatus.ACCEPTED, updated.activityTimingStatus);
			assertEquals(SubmissionStatus.ACCEPTED, updated.submission);
		});
		String visible = visibleText(panel);
		assertTrue(visible.contains("Completion time · 8:33.00"));
		assertTrue(visible.contains(ActivityTimingStatus.ACCEPTED.label));
		assertTrue(visible.contains(SubmissionStatus.ACCEPTED.label));
		assertNotNull(persistedTiming.get());
		assertEquals(SubmissionStatus.ACCEPTED, persistedTiming.get().submission);
	}

	@Test
	public void completionRendersWithoutLootAndMergesOnlyUnambiguousLootAfterThirtySeconds() throws Exception
	{
		java.util.concurrent.atomic.AtomicReference<LootRecord> completionSaved = new java.util.concurrent.atomic.AtomicReference<>();
		java.util.concurrent.atomic.AtomicReference<LootRecord> mergedSaved = new java.util.concurrent.atomic.AtomicReference<>();
		NocturnePanel panel = new NocturnePanel(null, new NocturnePanel.HistoryActions()
		{
			@Override public void loadOlder(String rsn, int offset) { }
			@Override public void clear(String rsn) { }
			@Override public void completion(LootRecord record) { completionSaved.set(record); }
			@Override public void mergeCompletionLoot(LootRecord merged, String replacedLootId) { mergedSaved.set(merged); }
		});
		Instant completionAt = Instant.parse("2026-10-13T00:12:31Z");
		BossCompletionContext context = BossCompletionContext.fromActiveSoloActivity(
			"corrupted_gauntlet", "corrupted_gauntlet", "Tester", "delayed-fixture-session", 91,
			1_000_000_000L, completionAt);
		ChallengeObservation observation = ChallengeObservation.createTracked(context, 513_000L);
		assertNotNull(observation);
		PluginActivityCatalog.Entry activity = PluginActivityCatalog.matchSource("Corrupted Gauntlet");
		LootRecord chest = new LootRecord("late-cg-chest", "2026-10-13T00:13:12Z", "Tester",
			"Corrupted Gauntlet", List.of(), context.roster, SubmissionStatus.ACCEPTED, activity);
		onEdt(() ->
		{
			panel.setPlayer("Tester", 8);
			panel.showHistory("Tester", new LootHistoryStore.Page(List.of(), 0, 0, false, 0), false, 8);
			panel.setActivityTiming(observation, ActivityTimingStatus.SENDING);
		});
		assertNotNull("completion persists before any chest reward", completionSaved.get());
		assertTrue(completionSaved.get().completionOnly);
		assertEquals("completion event ID is the durable activity ID", observation.eventId, completionSaved.get().id);
		assertTrue(visibleText(panel).contains("Completion time · 8:33.00"));

		onEdt(() -> panel.recordPersistedLoot(chest, 2, 200, 8)); // 41 seconds later
		assertNotNull("late chest merges only to the unique pending completion", mergedSaved.get());
		assertEquals(chest.id, mergedSaved.get().id);
		assertEquals(observation.eventId, mergedSaved.get().activityEventId);
		assertFalse(mergedSaved.get().completionOnly);
		assertEquals(Long.valueOf(513_000L), mergedSaved.get().activityDurationMillis);
		assertEquals(SubmissionStatus.ACCEPTED, mergedSaved.get().submission);
		String mergedText = visibleText(panel);
		assertTrue(mergedText.contains("Completion time · 8:33.00"));
		assertTrue(mergedText.contains("Corrupted Gauntlet"));
	}

	@Test
	public void delayedCompletionResultAttachesToOneLaterLootCardButNotSeveralRuns() throws Exception
	{
		java.util.concurrent.atomic.AtomicReference<LootRecord> persisted = new java.util.concurrent.atomic.AtomicReference<>();
		NocturnePanel panel = new NocturnePanel(null, new NocturnePanel.HistoryActions()
		{
			@Override public void loadOlder(String rsn, int offset) { }
			@Override public void clear(String rsn) { }
			@Override public void activityTiming(LootRecord record) { persisted.set(record); }
		});
		Instant completionAt = Instant.parse("2026-10-13T00:12:31Z");
		BossCompletionContext context = BossCompletionContext.fromActiveSoloActivity(
			"corrupted_gauntlet", "corrupted_gauntlet", "Tester", "response-late-session", 93,
			1_000_000_000L, completionAt);
		ChallengeObservation observation = ChallengeObservation.createTracked(context, 513_000L);
		PluginActivityCatalog.Entry activity = PluginActivityCatalog.GAUNTLET;
		LootRecord onlyLaterLoot = new LootRecord("later-one", "2026-10-13T00:13:20Z", "Tester",
			"Corrupted Gauntlet", List.of(), context.roster, SubmissionStatus.LOCAL, activity);
		onEdt(() ->
		{
			panel.setPlayer("Tester", 9);
			panel.showHistory("Tester", new LootHistoryStore.Page(List.of(onlyLaterLoot), 1, 100, false, 0), false, 9);
			panel.setActivityTiming(observation, ActivityTimingStatus.ACCEPTED);
		});
		assertEquals("completion-after-loot delivery preserves the loot identity", onlyLaterLoot.id, persisted.get().id);
		assertEquals(Long.valueOf(513_000L), persisted.get().activityDurationMillis);
		assertTrue(visibleText(panel).contains("Completion time · 8:33.00"));

		LootRecord secondLaterLoot = new LootRecord("later-two", "2026-10-13T00:13:40Z", "Tester",
			"Corrupted Gauntlet", List.of(), context.roster, SubmissionStatus.LOCAL, activity);
		NocturnePanel ambiguousPanel = new NocturnePanel(null, NocturnePanel.HistoryActions.NONE);
		onEdt(() ->
		{
			ambiguousPanel.setPlayer("Tester", 10);
			ambiguousPanel.showHistory("Tester",
				new LootHistoryStore.Page(List.of(onlyLaterLoot, secondLaterLoot), 2, 200, false, 0), false, 10);
			ambiguousPanel.setActivityTiming(observation, ActivityTimingStatus.ACCEPTED);
		});
		assertEquals(1, visibleText(ambiguousPanel).split("Completion time · 8:33.00", -1).length - 1);
	}

	@Test
	public void diagnosticsSettingRestoresHiddenTelemetryAndRaidDetails() throws Exception
	{
		NocturnePanel panel = panel();
		onEdt(() -> panel.setDiagnostics(true));
		String visible = visibleText(panel);
		assertTrue(visible.contains("Loot tracking paused"));
		assertTrue(visible.contains("Test submissions on"));
		assertTrue(visible.contains("GAME_ROSTER: COMPLETION"));
		assertTrue(visible.contains("INSTANCE_OBSERVED: 2 observed"));
		assertTrue(visible.contains("NOCTURNE_VERIFIED: 2/3"));
		assertTrue(visible.contains("NORMAL_GROUP"));
		assertTrue(visible.contains("Capture incomplete"));
		assertTrue(visible.contains("PRIVATE-TEST-ROSTER"));
		assertTrue(visible.contains("loot events"));
		assertTrue(visible.contains("Stored locally per character"));
	}

	@Test
	public void ordinaryCardsHideRoutineStatesAndGroupInternals() throws Exception
	{
		NocturnePanel panel = emptyPanel();
		GroupSnapshot group = new GroupSnapshot("PRIVATE-TEST-SOURCE", List.of("PRIVATE-TEST-ROSTER"), 2,
			GroupSnapshot.Status.MATCHED, "private test detail", true, "PRIVATE-TEST-ELIGIBILITY",
			"PRIVATE-TEST-ROSTER-STATE", "PRIVATE-TEST-SCORING");
		List<LootRecord> records = List.of(
			record("local", group, SubmissionStatus.LOCAL),
			record("unpriced", group, SubmissionStatus.UNPRICED),
			record("ineligible", group, SubmissionStatus.INELIGIBLE));
		onEdt(() -> panel.showHistory("Tester", new LootHistoryStore.Page(records, 3, 456, false, 0), false));
		String visible = visibleText(panel);
		for (String internal : List.of("Captured locally", "price unavailable", "below 500,000",
			"PRIVATE-TEST-ELIGIBILITY", "PRIVATE-TEST-ROSTER-STATE", "PRIVATE-TEST-SCORING",
			"PRIVATE-TEST-ROSTER", "Group unavailable", "private test detail"))
		{
			assertFalse("unexpected ordinary-panel text: " + internal, visible.contains(internal));
		}
	}

	@Test
	public void actionableDeliveryStatesRemainVisibleAndRoutineStatesReturnInDiagnostics() throws Exception
	{
		NocturnePanel panel = emptyPanel();
		List<LootRecord> records = new ArrayList<>();
		for (SubmissionStatus status : SubmissionStatus.values())
		{
			records.add(record(status.name(), GroupSnapshot.unavailable("diagnostic group detail"), status));
		}
		onEdt(() -> panel.showHistory("Tester", new LootHistoryStore.Page(records, records.size(), 99, false, 0), false));
		String ordinary = visibleText(panel);
		for (SubmissionStatus status : List.of(SubmissionStatus.SENDING, SubmissionStatus.ACCEPTED,
			SubmissionStatus.UNCERTAIN, SubmissionStatus.REJECTED, SubmissionStatus.BUSY,
			SubmissionStatus.CANCELLED))
		{
			assertTrue("missing actionable state " + status, ordinary.contains(status.label));
		}
		for (SubmissionStatus status : List.of(SubmissionStatus.LOCAL, SubmissionStatus.UNPRICED,
			SubmissionStatus.INELIGIBLE))
		{
			assertFalse("routine state should be hidden " + status, ordinary.contains(status.label));
		}
		onEdt(() -> panel.setDiagnostics(true));
		String diagnostic = visibleText(panel);
		for (SubmissionStatus status : List.of(SubmissionStatus.LOCAL, SubmissionStatus.UNPRICED,
			SubmissionStatus.INELIGIBLE))
		{
			assertTrue("diagnostic state should be available " + status, diagnostic.contains(status.label));
		}
	}

	@Test
	public void pricedDropDetailsRemainVisible() throws Exception
	{
		assertEquals("\n12,345 gp each", NocturnePanel.priceText(LootItem.market(1, 2, "Rune", 12345)));
		assertEquals("\nPrice unavailable", NocturnePanel.priceText(LootItem.market(2, 1, "Unpriced", 0)));
	}

	private static NocturnePanel panel() throws Exception
	{
		NocturnePanel panel = emptyPanel();
		Announcement announcement = new Announcement("notice", 1, "Clan news", "A useful update", "notice",
			Instant.parse("2026-10-02T12:00:00Z"), Instant.parse("2026-10-02T13:00:00Z"), null, null);
		GroupSnapshot group = new GroupSnapshot("Chambers", List.of("PRIVATE-TEST-ROSTER"), 3,
			GroupSnapshot.Status.INCOMPLETE, "private test detail", true, "PRIVATE-TEST-ELIGIBILITY",
			"COMPLETION", "NORMAL_GROUP");
		InstanceObservedEvidence observed = new InstanceObservedEvidence();
		observed.begin(1, List.of("Tester", "Other"), 1);
		onEdt(() ->
		{
			panel.setAnnouncements(List.of(announcement));
			panel.setTracking(false);
			panel.setSubmissionEnabled(true);
			panel.setGroup(group);
			panel.setRaidEvidence(group, observed.snapshot(),
				new RaidVerificationStatus(2, 3, true, false, "private verification reason"));
			panel.setRaidDiagnostics(new RaidDiagnostics("Chambers", 3, 1, 2, 2,
				"COMPLETION", "private raid diagnostic"));
			panel.updateHistoryStats("Tester", 4, 456);
		});
		return panel;
	}

	private static NocturnePanel emptyPanel() throws Exception
	{
		final NocturnePanel[] result = new NocturnePanel[1];
		onEdt(() ->
		{
			result[0] = new NocturnePanel(null);
			result[0].setPlayer("Tester");
			result[0].showHistory("Tester", new LootHistoryStore.Page(List.of(), 0, 0, true, 0), false);
		});
		return result[0];
	}

	private static LootRecord record(String id, GroupSnapshot group, SubmissionStatus status)
	{
		return new LootRecord(id, "2026-10-02T12:00:00Z", "Tester", "Challenge", List.of(), group, status);
	}

	private static String visibleText(Component component)
	{
		if (!component.isVisible()) return "";
		StringBuilder text = new StringBuilder();
		if (component instanceof JLabel) text.append(((JLabel) component).getText()).append('\n');
		if (component instanceof JTextArea) text.append(((JTextArea) component).getText()).append('\n');
		if (component instanceof AbstractButton) text.append(((AbstractButton) component).getText()).append('\n');
		if (component instanceof Container)
		{
			for (Component child : ((Container) component).getComponents()) text.append(visibleText(child));
		}
		return text.toString();
	}

	private static void onEdt(Runnable action) throws Exception
	{
		if (SwingUtilities.isEventDispatchThread()) action.run(); else SwingUtilities.invokeAndWait(action);
	}
}
