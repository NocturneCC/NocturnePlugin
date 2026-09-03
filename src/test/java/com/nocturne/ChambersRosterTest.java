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
		List<ChambersRoster.Field> fields = List.of(
			field(0, "Bifuor", 10, 0), field(1, "117", 90, 0), field(2, "123", 130, 0),
			field(3, "De Lena", 10, 20), field(4, "1409", 90, 20), field(5, "1932", 130, 20),
			field(6, "Not ZB", 10, 40), field(7, "2044", 90, 40), field(8, "86", 130, 40));

		assertEquals(List.of("Bifuor", "De Lena", "Not ZB"),
			ChambersRoster.extractRowsByGeometry(fields, 3));
		assertNull(ChambersRoster.characterName("117"));
		assertNull(ChambersRoster.characterName("2044"));
	}

	@Test
	public void columnMajorAndInterleavedArraysUseGeometryNotArrayOrder()
	{
		List<ChambersRoster.Field> columnMajor = List.of(
			field(0, "Bifuor", 10, 0), field(1, "De Lena", 10, 20), field(2, "Not ZB", 10, 40),
			field(3, "117", 90, 0), field(4, "1409", 90, 20), field(5, "2044", 90, 40),
			field(6, "123", 130, 0), field(7, "1932", 130, 20), field(8, "86", 130, 40));
		List<ChambersRoster.Field> interleaved = List.of(columnMajor.get(5), columnMajor.get(0),
			columnMajor.get(7), columnMajor.get(2), columnMajor.get(3), columnMajor.get(8),
			columnMajor.get(1), columnMajor.get(6), columnMajor.get(4));
		List<String> expected = List.of("Bifuor", "De Lena", "Not ZB");
		assertEquals(expected, ChambersRoster.extractRowsByGeometry(columnMajor, 3));
		assertEquals(expected, ChambersRoster.extractRowsByGeometry(interleaved, 3));
	}

	@Test
	public void hiddenAndMalformedGeometryFailClosedWithoutTruncation()
	{
		List<ChambersRoster.Field> hidden = List.of(field(0, "Bifuor", 10, 0),
			field(1, "De Lena", 10, 20, true), field(2, "Not ZB", 10, 40));
		List<ChambersRoster.Field> ambiguous = List.of(field(0, "Bifuor", 10, 0),
			field(1, "Other Name", 50, 0), field(2, "De Lena", 10, 20));
		List<ChambersRoster.Field> malformed = List.of(field(0, "Bifuor", 10, 0),
			field(1, "De Lena", 10, 0), field(2, "Not ZB", 10, 40));
		assertTrue(ChambersRoster.extractRowsByGeometry(hidden, 3).isEmpty());
		assertTrue(ChambersRoster.extractRowsByGeometry(ambiguous, 2).isEmpty());
		assertTrue(ChambersRoster.extractRowsByGeometry(malformed, 3).isEmpty());
	}

	@Test
	public void childDiagnosticsContainGeometryAndClassificationButNeverText()
	{
		ChambersRoster.Field alpha = field(4, "De Lena", 12, 34);
		ChambersRoster.Field numeric = field(5, "1,409", 70, 34);
		assertEquals("alphabetic", alpha.classification());
		assertEquals("numeric", numeric.classification());
		assertTrue(alpha.diagnostic().contains("index=4,id=1004,x=12,y=34,w=50,h=15"));
		assertFalse(alpha.diagnostic().contains("De Lena"));
		assertEquals("mixed", field(6, "Player2", 0, 0).classification());
		assertEquals("empty", field(7, "", 0, 0).classification());
	}

	private static ChambersRoster.Field field(int index, String text, int x, int y)
	{
		return field(index, text, x, y, false);
	}

	private static ChambersRoster.Field field(int index, String text, int x, int y, boolean hidden)
	{
		return new ChambersRoster.Field(index, 1000 + index, x, y, 50, 15, hidden, text);
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
