package com.nocturne;

import java.util.List;
import org.junit.Test;
import static org.junit.Assert.*;

public class ChambersRosterTest
{
	@Test
	public void nestedTextOnlyRosterStripsMarkupAndDeduplicatesNames()
	{
		Node root = node("ignored container",
			node(null, node("<col=ffffff>De Lena</col>")),
			node(null, node("<img=1>Teammate 1")),
			node(null, node("teammate 1")),
			node(null, node("Third-Player")));

		assertEquals(List.of("De Lena", "Teammate 1", "Third Player"),
			ChambersRoster.extract(root));
	}

	@Test
	public void onlyVisibleLeafCharacterNamesAreAccepted()
	{
		Node root = node(null,
			node("This parent label is ignored", node("Valid_Name")),
			hidden("HiddenUser"), node(""), node("thirteen chars"), node("<br>"));

		assertEquals(List.of("Valid Name"), ChambersRoster.extract(root));
		assertNull(ChambersRoster.characterName("This name is much too long"));
	}

	@Test
	public void currentWidgetChildViewsAreReportedWithoutRosterText()
	{
		// RuneLite's current Widget API exposes dynamic, static, and all nested
		// descendants separately. Counts show where runtime roster rows landed.
		String structure = ChambersRoster.structure(0x01f4000a, false, 0, 4, 12, 4, 0);
		assertEquals("id=32768010, hidden=false, dynamic=0, static=4, nested=12, "
			+ "text-present=4, name-present=0", structure);
		assertFalse(structure.contains("De Lena"));
	}

	@Test
	public void missingCurrentListIsExplicitRatherThanBorrowingAnotherPlayerSource()
	{
		assertEquals("missing", ChambersRoster.inspect(null, 3).structure);
		assertTrue(ChambersRoster.extract((net.runelite.api.widgets.Widget) null).isEmpty());
	}

	@Test
	public void liveThreePlayerRowsSelectNamesAndRejectSixNumericStats()
	{
		List<Node> fields = List.of(
			node(null), node("Bifuor"), node("117"), node("123"), node(null), node(null), node(null),
			node(null), node("De Lena"), node("1409"), node("1932"), node(null), node(null), node(null),
			node(null), node("Not ZB"), node("2044"), node("86"), node(null), node(null), node(null));

		assertEquals(List.of("Bifuor", "De Lena", "Not ZB"), ChambersRoster.extractRows(fields, 3));
		assertNull(ChambersRoster.characterName("117"));
		assertNull(ChambersRoster.characterName("2044"));
	}

	@Test
	public void rowParsingRefusesAmbiguityAndDoesNotTruncateToPartySize()
	{
		List<Node> ambiguous = List.of(node("Bifuor"), node("Other Name"), node("117"),
			node("De Lena"), node("1409"), node("1932"));
		assertTrue(ChambersRoster.extractRows(ambiguous, 2).isEmpty());
		assertTrue(ChambersRoster.extractRows(List.of(node("Bifuor"), node("117")), 3).isEmpty());
	}

	private static Node node(String text, Node... children)
	{
		return new Node(text, true, List.of(children));
	}

	private static Node hidden(String text)
	{
		return new Node(text, false, List.of());
	}

	private static final class Node implements ChambersRoster.Node
	{
		private final String text;
		private final boolean visible;
		private final List<Node> children;
		private Node(String text, boolean visible, List<Node> children)
		{
			this.text = text; this.visible = visible; this.children = children;
		}
		@Override public String text() { return text; }
		@Override public boolean visible() { return visible; }
		@Override public List<Node> children() { return children; }
	}
}
