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
