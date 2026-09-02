package com.nocturne;

import java.util.List;
import javax.swing.SwingUtilities;
import org.junit.BeforeClass;
import org.junit.Test;
import static org.junit.Assert.*;

public class LootHistoryStatusTest
{
	@BeforeClass public static void headless() { System.setProperty("java.awt.headless", "true"); }

	@Test
	public void persistedRecordCountAndBytesAppearAtomically() throws Exception
	{
		NocturnePanel panel = panel("First", 10);
		onEdt(() ->
		{
			panel.recordPersistedLoot(record("one", "First"), 1, 347, 10);
			assertEquals("1 loot events · 347 B local", panel.historySummary());
		});
	}

	@Test
	public void rapidWritesApplyExactLatestPostWriteStatistics() throws Exception
	{
		NocturnePanel panel = panel("First", 10);
		onEdt(() ->
		{
			panel.recordPersistedLoot(record("one", "First"), 1, 347, 10);
			panel.recordPersistedLoot(record("two", "First"), 2, 612, 10);
			assertEquals("2 loot events · 612 B local", panel.historySummary());
		});
	}

	@Test
	public void failedWriteStaysVisibleWithoutClaimingItWasSaved() throws Exception
	{
		NocturnePanel panel = panel("First", 10);
		onEdt(() ->
		{
			panel.recordUnsavedLoot(record("one", "First"), 10);
			assertEquals("1 loot events · history not saved", panel.historySummary());
			assertFalse(panel.historySummary().contains("0 B"));
		});
	}

	@Test
	public void delayedCallbacksCannotUpdateAnotherAccountOrReusedName() throws Exception
	{
		NocturnePanel panel = panel("First", 10);
		onEdt(() ->
		{
			panel.setPlayer("Second", 11);
			panel.showHistory("Second", page(0, 0), false, 11);
			panel.recordPersistedLoot(record("late", "First"), 1, 400, 10);
			assertEquals("0 loot events · 0 B local", panel.historySummary());
			panel.setPlayer("First", 12);
			panel.showHistory("First", page(0, 0), false, 12);
			panel.recordPersistedLoot(record("stale", "First"), 1, 400, 10);
			assertEquals("0 loot events · 0 B local", panel.historySummary());
		});
	}

	private static NocturnePanel panel(String rsn, long generation) throws Exception
	{
		final NocturnePanel[] result = new NocturnePanel[1];
		onEdt(() ->
		{
			result[0] = new NocturnePanel(null);
			result[0].setPlayer(rsn, generation);
			result[0].showHistory(rsn, page(0, 0), false, generation);
		});
		return result[0];
	}

	private static LootHistoryStore.Page page(int count, long bytes)
	{
		return new LootHistoryStore.Page(List.of(), count, bytes, false, 0);
	}

	private static LootRecord record(String id, String rsn)
	{
		return new LootRecord(id, "2026-09-02T12:00:00Z", rsn, "Test", List.of(),
			GroupSnapshot.unavailable("test"), SubmissionStatus.LOCAL);
	}

	private static void onEdt(Runnable action) throws Exception
	{
		if (SwingUtilities.isEventDispatchThread()) action.run(); else SwingUtilities.invokeAndWait(action);
	}
}
