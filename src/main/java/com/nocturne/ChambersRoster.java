package com.nocturne;

import java.util.ArrayList;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.Comparator;
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
		if (root == null) return new Observation(List.of(), 0, "missing", "none");
		List<Field> fields = fields(root.getDynamicChildren());
		List<String> names = expectedSize > 0 ? extractRowsByGeometry(fields, expectedSize)
			: extract(new WidgetNode(root));
		int candidates = 0;
		for (Field field : fields) if (!"empty".equals(field.classification())) candidates++;
		return new Observation(names, candidates, structure(root), childDiagnostics(fields));
	}

	static List<String> extractRowsByGeometry(List<Field> fields, int expectedSize)
	{
		if (expectedSize <= 0 || fields == null) return List.of();
		List<Field> visible = new ArrayList<>();
		for (Field field : fields) if (!field.hidden) visible.add(field);
		visible.sort(Comparator.comparingInt((Field field) -> field.y).thenComparingInt(field -> field.x));
		List<Row> rows = new ArrayList<>();
		for (Field field : visible)
		{
			Row match = null;
			for (Row row : rows)
			{
				if (row.overlaps(field))
				{
					if (match != null) return List.of();
					match = row;
				}
			}
			if (match == null) rows.add(new Row(field)); else match.add(field);
		}
		if (rows.size() != expectedSize) return List.of();
		List<String> names = new ArrayList<>();
		for (Row row : rows)
		{
			List<String> rowNames = new ArrayList<>();
			for (Field field : row.fields)
			{
				String name = characterName(field.text);
				if (name != null) rowNames.add(name);
			}
			rowNames = GroupSnapshot.uniqueNames(rowNames);
			if (rowNames.size() != 1) return List.of();
			names.add(rowNames.get(0));
		}
		return GroupSnapshot.uniqueNames(names);
	}

	private static List<Field> fields(Widget[] children)
	{
		List<Field> fields = new ArrayList<>();
		if (children == null) return fields;
		for (int index = 0; index < children.length; index++)
		{
			Widget child = children[index];
			if (child != null) fields.add(new Field(index, child.getId(), child.getRelativeX(),
				child.getRelativeY(), child.getWidth(), child.getHeight(),
				child.isHidden() || child.isSelfHidden(), child.getText()));
		}
		return fields;
	}

	private static String childDiagnostics(List<Field> fields)
	{
		List<String> values = new ArrayList<>();
		for (Field field : fields) values.add(field.diagnostic());
		return String.join("; ", values);
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
		final String children;

		private Observation(List<String> names, int candidateCount, String structure, String children)
		{
			this.names = names;
			this.candidateCount = candidateCount;
			this.structure = structure;
			this.children = children;
		}
	}

	static final class Field
	{
		final int index, id, x, y, width, height;
		final boolean hidden;
		final String text;

		Field(int index, int id, int x, int y, int width, int height, boolean hidden, String text)
		{
			this.index = index; this.id = id; this.x = x; this.y = y;
			this.width = width; this.height = height; this.hidden = hidden; this.text = text;
		}

		String classification()
		{
			String value = text == null ? "" : Text.removeTags(text).trim();
			if (value.isEmpty()) return "empty";
			if (value.matches("[0-9,]+")) return "numeric";
			if (value.matches("[A-Za-z _-]+")) return "alphabetic";
			return "mixed";
		}

		String diagnostic()
		{
			return "index=" + index + ",id=" + id + ",x=" + x + ",y=" + y + ",w=" + width
				+ ",h=" + height + ",hidden=" + hidden + ",text=" + classification();
		}
	}

	private static final class Row
	{
		private final List<Field> fields = new ArrayList<>();
		private int commonTop;
		private int commonBottom;

		private Row(Field field)
		{
			commonTop = field.y;
			commonBottom = field.y + Math.max(1, field.height);
			fields.add(field);
		}

		private boolean overlaps(Field field)
		{
			int bottom = field.y + Math.max(1, field.height);
			return field.y < commonBottom && bottom > commonTop;
		}

		private void add(Field field)
		{
			commonTop = Math.max(commonTop, field.y);
			commonBottom = Math.min(commonBottom, field.y + Math.max(1, field.height));
			fields.add(field);
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
