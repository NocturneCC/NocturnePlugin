package com.nocturne;

import java.awt.Component;
import java.awt.Container;
import java.util.ArrayList;
import java.util.List;
import javax.swing.JLabel;
import javax.swing.JPanel;
import javax.swing.text.JTextComponent;
import net.runelite.http.api.loottracker.LootRecordType;
import org.junit.Test;
import static org.junit.Assert.*;

public class LootEventCoverageTest
{
	@Test
	public void barrowsEventIsGenericAndRendersInPanel()
	{
		assertEquals(NocturnePlugin.LootOrigin.GENERIC_EVENT,
			NocturnePlugin.classifyLoot(LootRecordType.EVENT, "Barrows Chests"));
		LootRecord record = new LootRecord("First", "Barrows Chests", List.of());
		record.submission = SubmissionStatus.INELIGIBLE;
		JPanel card = new NocturnePanel(null).renderRecord(record);
		List<String> labels = labels(card);
		assertTrue(labels.contains("Barrows Chests"));
		assertTrue(labels.contains(SubmissionStatus.INELIGIBLE.label));
	}

	@Test
	public void genericEventUsesPerUnitSubmissionAndScreenshotThreshold()
	{
		LootItem below = new LootItem(1, 2_000_000, "Cheap stack", 499_999);
		LootItem eligible = new LootItem(2, 1, "Valuable unit", 500_000);
		assertFalse(NocturnePlugin.isSubmissionEligible(List.of(below)));
		assertTrue(NocturnePlugin.isSubmissionEligible(List.of(eligible)));
	}

	@Test
	public void recognizedRaidsRetainRaidClassification()
	{
		assertEquals(NocturnePlugin.LootOrigin.RAID_EVENT,
			NocturnePlugin.classifyLoot(LootRecordType.EVENT, "Tombs of Amascut"));
		assertEquals(NocturnePlugin.LootOrigin.RAID_EVENT,
			NocturnePlugin.classifyLoot(LootRecordType.EVENT, "Chambers of Xeric"));
		assertTrue(NocturnePlugin.usesGroupContext(NocturnePlugin.LootOrigin.RAID_EVENT));
		assertFalse(NocturnePlugin.usesGroupContext(NocturnePlugin.LootOrigin.GENERIC_EVENT));
	}

	@Test
	public void playerPickpocketAndNpcLootReceivedTypesStayRejected()
	{
		assertEquals(NocturnePlugin.LootOrigin.REJECTED,
			NocturnePlugin.classifyLoot(LootRecordType.PLAYER, "Player"));
		assertEquals(NocturnePlugin.LootOrigin.REJECTED,
			NocturnePlugin.classifyLoot(LootRecordType.PICKPOCKET, "Knight"));
		assertEquals(NocturnePlugin.LootOrigin.REJECTED,
			NocturnePlugin.classifyLoot(LootRecordType.NPC, "Goblin"));
	}

	@Test
	public void npcAndEventPathsAreMutuallyExclusiveToPreventDuplicates()
	{
		assertEquals(NocturnePlugin.LootOrigin.REJECTED,
			NocturnePlugin.classifyLoot(LootRecordType.NPC, "Barrows Chests"));
		assertEquals(NocturnePlugin.LootOrigin.GENERIC_EVENT,
			NocturnePlugin.classifyLoot(LootRecordType.EVENT, "Barrows Chests"));
		assertTrue(NocturnePlugin.usesGroupContext(NocturnePlugin.LootOrigin.NPC));
	}

	@Test
	public void npcLootInsideRaidIsLabeledAsContextNotAsItsOwnRoster()
	{
		GroupSnapshot group = new GroupSnapshot("Chambers of Xeric / Raiding-party sidebar",
			List.of(), 4, GroupSnapshot.Status.INCOMPLETE, "No names accepted");
		LootRecord record = new LootRecord("First", "Scavenger beast", List.of(), group);
		List<String> labels = labels(new NocturnePanel(null).renderRecord(record));
		assertTrue(labels.stream().anyMatch(text -> text.startsWith("Active raid context (incomplete)")));
		assertFalse(labels.stream().anyMatch(text -> text.startsWith("Raid roster")));
	}

	private static List<String> labels(Container root)
	{
		List<String> result = new ArrayList<>();
		for (Component component : root.getComponents())
		{
			if (component instanceof JLabel) result.add(((JLabel) component).getText());
			if (component instanceof JTextComponent) result.add(((JTextComponent) component).getText());
			if (component instanceof Container) result.addAll(labels((Container) component));
		}
		return result;
	}
}
