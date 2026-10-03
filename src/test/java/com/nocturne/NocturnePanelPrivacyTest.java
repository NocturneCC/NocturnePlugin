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
		assertTrue(visible.contains("RECENT DROPS"));
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
		assertTrue(visible.contains("No recent drops yet."));
		assertTrue(visible.contains("Load 50 older events"));
		assertTrue(visible.contains("Clear local history"));
		assertFalse(visible.contains("Defeat an NPC"));
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
