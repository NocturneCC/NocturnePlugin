package com.nocturne;

import java.util.ArrayList;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Set;
import net.runelite.api.widgets.Widget;
import net.runelite.client.util.Text;

/** Reads only visible character-name text from the Chambers party interface. */
final class ChambersRoster
{
	private ChambersRoster() { }

	static List<String> extract(Widget root)
	{
		return extract(root == null ? null : new WidgetNode(root));
	}

	static List<String> extract(Node root)
	{
		List<String> names = new ArrayList<>();
		Set<Node> visited = Collections.newSetFromMap(new IdentityHashMap<>());
		walk(root, names, visited);
		return GroupSnapshot.uniqueNames(names);
	}

	private static void walk(Node node, List<String> names, Set<Node> visited)
	{
		if (node == null || !visited.add(node) || !node.visible()) return;
		List<? extends Node> children = node.children();
		if (children == null || children.isEmpty())
		{
			String name = characterName(node.text());
			if (name != null) names.add(name);
			return;
		}
		for (Node child : children) walk(child, names, visited);
	}

	static String characterName(String raw)
	{
		if (raw == null) return null;
		String name = Text.toJagexName(Text.removeTags(raw)).trim();
		if (name.length() < 1 || name.length() > 12 || !name.matches("[A-Za-z0-9 _-]+")
			|| !name.matches(".*[A-Za-z0-9].*")) return null;
		return name;
	}

	interface Node
	{
		String text();
		boolean visible();
		List<? extends Node> children();
	}

	private static final class WidgetNode implements Node
	{
		private final Widget widget;
		private WidgetNode(Widget widget) { this.widget = widget; }
		@Override public String text() { return widget.getText(); }
		@Override public boolean visible() { return !widget.isHidden() && !widget.isSelfHidden(); }
		@Override public List<WidgetNode> children()
		{
			Widget[] children = widget.getChildren();
			if (children == null || children.length == 0) return List.of();
			List<WidgetNode> result = new ArrayList<>();
			for (Widget child : children) if (child != null) result.add(new WidgetNode(child));
			return result;
		}
	}
}
