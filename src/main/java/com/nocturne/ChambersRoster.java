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
		return inspect(root, 0).names;
	}

	static Observation inspect(Widget root, int expectedSize)
	{
		if (root == null) return new Observation(List.of(), 0, "missing");
		CandidateCounter counter = new CandidateCounter();
		WidgetNode node = new WidgetNode(root, counter);
		List<WidgetNode> fields = node.children();
		boolean rowStructureAvailable = expectedSize > 0 && fields.size() >= expectedSize
			&& fields.size() % expectedSize == 0;
		List<String> names = extractRows(fields, expectedSize);
		if (!rowStructureAvailable)
		{
			counter.count = 0;
			names = extract(node);
		}
		return new Observation(names, counter.count, structure(root));
	}

	static List<String> extractRows(List<? extends Node> fields, int expectedSize)
	{
		if (expectedSize <= 0 || fields == null || fields.size() < expectedSize
			|| fields.size() % expectedSize != 0) return List.of();
		int fieldsPerRow = fields.size() / expectedSize;
		List<String> names = new ArrayList<>();
		boolean unambiguous = true;
		for (int row = 0; row < expectedSize; row++)
		{
			List<String> rowNames = new ArrayList<>();
			for (int field = 0; field < fieldsPerRow; field++)
			{
				rowNames.addAll(extract(fields.get(row * fieldsPerRow + field)));
			}
			rowNames = GroupSnapshot.uniqueNames(rowNames);
			// Live Chambers rows have one display-name field plus numeric stat fields.
			// Refuse ambiguous rows instead of selecting by candidate order or truncating.
			if (rowNames.size() != 1)
			{
				unambiguous = false;
			}
			else names.add(rowNames.get(0));
		}
		return unambiguous ? GroupSnapshot.uniqueNames(names) : List.of();
	}

	static String structuralSummary(Widget list, Widget layer, Widget universe)
	{
		return "list{" + structureOrMissing(list) + "}; layer{" + structureOrMissing(layer)
			+ "}; universe{" + structureOrMissing(universe) + "}";
	}

	private static String structureOrMissing(Widget widget)
	{
		return widget == null ? "missing" : structure(widget);
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
		// RuneScape display names used here must contain a letter. This deliberately
		// rejects all-numeric raid statistics exposed beside the name in each row.
		if (name.length() < 1 || name.length() > 12 || !name.matches("[A-Za-z0-9 _-]+")
			|| !name.matches(".*[A-Za-z].*")) return null;
		return name;
	}

	interface Node
	{
		String text();
		boolean visible();
		List<? extends Node> children();
	}

	static final class Observation
	{
		final List<String> names;
		final int candidateCount;
		final String structure;

		private Observation(List<String> names, int candidateCount, String structure)
		{
			this.names = names;
			this.candidateCount = candidateCount;
			this.structure = structure;
		}
	}

	private static String structure(Widget widget)
	{
		Widget[] dynamic = widget.getDynamicChildren();
		Widget[] statics = widget.getStaticChildren();
		Widget[] nested = widget.getNestedChildren();
		return structure(widget.getId(), widget.isHidden() || widget.isSelfHidden(),
			populated(dynamic), populated(statics), populated(nested), presentText(nested, false),
			presentText(nested, true));
	}

	static String structure(int id, boolean hidden, int dynamic, int statics, int nested,
		int textPresent, int namePresent)
	{
		return "id=" + id + ", hidden=" + hidden + ", dynamic=" + dynamic + ", static=" + statics
			+ ", nested=" + nested + ", text-present=" + textPresent + ", name-present=" + namePresent;
	}

	private static int populated(Widget[] widgets)
	{
		if (widgets == null) return 0;
		int count = 0;
		for (Widget widget : widgets) if (widget != null) count++;
		return count;
	}

	private static int presentText(Widget[] widgets, boolean name)
	{
		if (widgets == null) return 0;
		int count = 0;
		for (Widget widget : widgets)
		{
			if (widget == null) continue;
			String value = name ? widget.getName() : widget.getText();
			if (value != null && !value.isEmpty()) count++;
		}
		return count;
	}

	private static final class CandidateCounter { private int count; }

	private static final class WidgetNode implements Node
	{
		private final Widget widget;
		private final CandidateCounter counter;
		private WidgetNode(Widget widget, CandidateCounter counter)
		{
			this.widget = widget;
			this.counter = counter;
		}
		@Override public String text()
		{
			String text = widget.getText();
			if (text != null && !text.isEmpty()) counter.count++;
			return text;
		}
		@Override public boolean visible() { return !widget.isHidden() && !widget.isSelfHidden(); }
		@Override public List<WidgetNode> children()
		{
			Widget[] children = widget.getChildren();
			if (children == null || children.length == 0) return List.of();
			List<WidgetNode> result = new ArrayList<>();
			for (Widget child : children) if (child != null) result.add(new WidgetNode(child, counter));
			return result;
		}
	}
}
