package com.nocturne;

import java.awt.Component;
import java.awt.Container;
import java.util.ArrayList;
import java.util.List;
import javax.swing.JComponent;
import javax.swing.JScrollPane;
import javax.swing.SwingUtilities;
import org.junit.BeforeClass;
import org.junit.Test;
import static org.junit.Assert.*;

public class NocturnePanelScrollTest
{
	@BeforeClass public static void headless() { System.setProperty("java.awt.headless", "true"); }

	@Test
	public void livePrependAtTopStaysAtZeroAndNewestIsFirst() throws Exception
	{
		Harness h = harness(12);
		onEdt(() -> { h.bar().setValue(0); h.record("live"); h.layout(); });
		flushEdt();
		assertEquals(0, value(h));
		assertEquals("live", cardIds(h).get(0));
	}

	@Test
	public void livePrependPreservesMiddleEventAndPixelOffset() throws Exception
	{
		Harness h = harness(16);
		Anchor before = positionAt(h, "old-7", 11);
		onEdt(() -> { h.record("live"); h.layout(); });
		flushEdt();
		assertAnchor(h, before);
	}

	@Test
	public void livePrependAtBottomPreservesEventInsteadOfBottomDistance() throws Exception
	{
		Harness h = harness(18);
		onEdt(() -> h.bar().setValue(h.bar().getMaximum() - h.bar().getVisibleAmount()));
		Anchor before = visibleAnchor(h);
		onEdt(() -> { h.record("live"); h.layout(); });
		flushEdt();
		assertAnchor(h, before);
	}

	@Test
	public void rapidPrependsUseOneStableAnchor() throws Exception
	{
		Harness h = harness(18);
		Anchor before = positionAt(h, "old-4", 9);
		onEdt(() ->
		{
			for (int i = 0; i < 5; i++) h.record("live-" + i);
			h.layout();
		});
		flushEdt();
		assertAnchor(h, before);
		assertEquals(List.of("live-4", "live-3", "live-2", "live-1", "live-0"),
			cardIds(h).subList(0, 5));
	}

	@Test
	public void paginationAppendsBelowWithoutMovingViewport() throws Exception
	{
		Harness h = harness(12);
		Anchor before = positionAt(h, "old-5", 7);
		List<LootRecord> older = List.of(record("older-1"), record("older-2"));
		onEdt(() ->
		{
			h.panel.showHistory("Tester", new LootHistoryStore.Page(older, 14, 100, false, 0), true);
			h.layout();
		});
		flushEdt();
		assertAnchor(h, before);
		List<String> ids = cardIds(h);
		assertEquals(List.of("older-1", "older-2"), ids.subList(ids.size() - 2, ids.size()));
	}

	@Test
	public void startupAndAccountHistoryRestorationBeginAtNewest() throws Exception
	{
		Harness h = harness(10);
		positionAt(h, "old-6", 4);
		List<LootRecord> second = List.of(recordFor("second-new", "Second"), recordFor("second-old", "Second"));
		onEdt(() ->
		{
			h.panel.setPlayer("Second");
			h.panel.showHistory("Second", new LootHistoryStore.Page(second, 2, 20, false, 0), false);
			h.layout();
		});
		flushEdt();
		assertEquals(0, value(h));
		assertEquals("second-new", cardIds(h).get(0));

		List<LootRecord> restored = List.of(record("restored-new"), record("restored-old"));
		onEdt(() ->
		{
			h.panel.setPlayer("Tester");
			h.panel.showHistory("Tester", new LootHistoryStore.Page(restored, 2, 20, false, 0), false);
			h.layout();
		});
		flushEdt();
		assertEquals(0, value(h));
		assertEquals("restored-new", cardIds(h).get(0));
	}

	private static Harness harness(int records) throws Exception
	{
		final Harness[] result = new Harness[1];
		onEdt(() ->
		{
			NocturnePanel panel = new NocturnePanel(null);
			panel.setPlayer("Tester");
			List<LootRecord> page = new ArrayList<>();
			for (int i = 0; i < records; i++) page.add(record("old-" + i));
			panel.showHistory("Tester", new LootHistoryStore.Page(page, records, 100, false, 0), false);
			result[0] = new Harness(panel);
			result[0].layout();
		});
		flushEdt();
		return result[0];
	}

	private static LootRecord record(String id) { return recordFor(id, "Tester"); }
	private static LootRecord recordFor(String id, String rsn)
	{
		return new LootRecord(id, "2026-09-02T12:00:00Z", rsn, id, List.of(),
			GroupSnapshot.unavailable("test"), SubmissionStatus.LOCAL);
	}

	private static Anchor positionAt(Harness h, String id, int offset) throws Exception
	{
		Anchor anchor = new Anchor(id, offset);
		onEdt(() -> h.bar().setValue(cardY(h, id) + offset));
		assertAnchor(h, anchor);
		return anchor;
	}

	private static Anchor visibleAnchor(Harness h) throws Exception
	{
		final Anchor[] result = new Anchor[1];
		onEdt(() ->
		{
			int y = value(h);
			for (Component card : cards(h))
			{
				int cardY = SwingUtilities.convertPoint(card, 0, 0, h.scroll().getViewport().getView()).y;
				if (cardY + card.getHeight() > y)
				{
					result[0] = new Anchor(id(card), y - cardY);
					return;
				}
			}
		});
		return result[0];
	}

	private static void assertAnchor(Harness h, Anchor expected) throws Exception
	{
		Anchor actual = visibleAnchor(h);
		assertNotNull(actual);
		assertEquals("scroll=" + value(h) + " max=" + h.bar().getMaximum()
			+ " expectedY=" + cardY(h, expected.id), expected.id, actual.id);
		assertEquals(expected.offset, actual.offset);
	}

	private static int cardY(Harness h, String expected)
	{
		for (Component card : cards(h))
		{
			if (expected.equals(id(card)))
				return SwingUtilities.convertPoint(card, 0, 0, h.scroll().getViewport().getView()).y;
		}
		throw new AssertionError("missing card " + expected);
	}

	private static List<String> cardIds(Harness h) throws Exception
	{
		final List<String>[] result = new List[1];
		onEdt(() ->
		{
			List<String> ids = new ArrayList<>();
			for (Component card : cards(h)) ids.add(id(card));
			result[0] = ids;
		});
		return result[0];
	}

	private static List<Component> cards(Harness h)
	{
		List<Component> result = new ArrayList<>();
		collectCards(h.panel, result);
		return result;
	}

	private static void collectCards(Component component, List<Component> result)
	{
		if (component instanceof JComponent
			&& ((JComponent) component).getClientProperty("lootRecordId") != null) result.add(component);
		if (component instanceof Container)
			for (Component child : ((Container) component).getComponents()) collectCards(child, result);
	}

	private static String id(Component card)
	{
		return String.valueOf(((JComponent) card).getClientProperty("lootRecordId"));
	}

	private static int value(Harness h) { return h.bar().getValue(); }
	private static void flushEdt() throws Exception { onEdt(() -> { }); onEdt(() -> { }); }
	private static void onEdt(Runnable action) throws Exception
	{
		if (SwingUtilities.isEventDispatchThread()) action.run(); else SwingUtilities.invokeAndWait(action);
	}

	private static final class Harness
	{
		private final NocturnePanel panel;
		private int total;
		private Harness(NocturnePanel panel) { this.panel = panel; this.total = cards(this).size(); }
		private void record(String id)
		{
			panel.recordPersistedLoot(NocturnePanelScrollTest.record(id), ++total, 100 + total, 1);
		}
		private JScrollPane scroll() { return panel.scrollPane(); }
		private javax.swing.JScrollBar bar() { return scroll().getVerticalScrollBar(); }
		private void layout()
		{
			scroll().setSize(260, 320);
			scroll().doLayout();
			java.awt.Dimension preferred = panel.getPreferredSize();
			preferred.width = scroll().getViewport().getExtentSize().width;
			panel.setSize(preferred);
			scroll().getViewport().setViewSize(preferred);
			layoutTree(panel);
			scroll().doLayout();
		}
		private static void layoutTree(Container parent)
		{
			parent.doLayout();
			for (Component child : parent.getComponents())
				if (child instanceof Container) layoutTree((Container) child);
		}
	}

	private static final class Anchor
	{
		private final String id;
		private final int offset;
		private Anchor(String id, int offset) { this.id = id; this.offset = offset; }
	}
}
