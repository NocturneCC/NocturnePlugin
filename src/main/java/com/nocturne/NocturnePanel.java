package com.nocturne;

import java.awt.BorderLayout;
import java.awt.Color;
import java.awt.Font;
import java.awt.Graphics2D;
import java.awt.RenderingHints;
import java.awt.image.BufferedImage;
import java.time.format.DateTimeFormatter;
import javax.swing.BorderFactory;
import javax.swing.BoxLayout;
import javax.swing.JButton;
import javax.swing.JLabel;
import javax.swing.JPanel;
import javax.swing.JTextArea;
import net.runelite.client.ui.PluginPanel;

final class NocturnePanel extends PluginPanel
{
	private static final Color BACKGROUND = new Color(25, 21, 34);
	private static final Color CARD = new Color(42, 33, 56);
	private static final Color PURPLE = new Color(184, 142, 238);
	private static final Color MUTED = new Color(186, 178, 198);
	private static final DateTimeFormatter TIME = DateTimeFormatter.ofPattern("HH:mm:ss");

	private final LootHistory history = new LootHistory();
	private final JLabel player = label("Log in to see your character", Color.WHITE);
	private final JLabel tracking = label("NPC loot tracking enabled", PURPLE);
	private final JLabel count = label("0 loot events", MUTED);
	private final JPanel feed = new JPanel();

	NocturnePanel()
	{
		setBackground(BACKGROUND);
		setLayout(new BorderLayout(0, 12));
		setBorder(BorderFactory.createEmptyBorder(12, 9, 12, 9));

		JPanel header = column(BACKGROUND);
		JLabel title = label("NOCTURNE", PURPLE);
		title.setFont(title.getFont().deriveFont(Font.BOLD, 21f));
		header.add(title);
		header.add(label("COMPANION  /  LOCAL PREVIEW", MUTED));
		header.add(spacer());
		header.add(label("CHARACTER", MUTED));
		header.add(player);
		header.add(spacer());
		header.add(tracking);
		header.add(note("Drops stay in this client. Backend connection comes later.", BACKGROUND));
		header.add(spacer());
		header.add(label("RECENT NPC LOOT", PURPLE));
		header.add(count);
		add(header, BorderLayout.NORTH);

		feed.setLayout(new BoxLayout(feed, BoxLayout.Y_AXIS));
		feed.setBackground(BACKGROUND);
		add(feed, BorderLayout.CENTER);

		JPanel footer = column(BACKGROUND);
		JButton clear = new JButton("Clear local history");
		clear.setBackground(CARD);
		clear.setForeground(Color.WHITE);
		clear.setFocusable(false);
		clear.addActionListener(event ->
		{
			history.clear();
			renderHistory();
		});
		footer.add(clear);
		footer.add(note("Latest 50 loot events. Clears on logout, character change or plugin restart.", BACKGROUND));
		add(footer, BorderLayout.SOUTH);
		renderHistory();
	}

	void setPlayer(String rsn)
	{
		if (history.setPlayer(rsn))
		{
			player.setText(rsn == null ? "Log in to see your character" : rsn);
			renderHistory();
		}
	}

	void setTracking(boolean enabled)
	{
		tracking.setText(enabled ? "NPC loot tracking enabled" : "NPC loot tracking paused");
		tracking.setForeground(enabled ? PURPLE : MUTED);
	}

	void recordLoot(LootRecord record)
	{
		setPlayer(record.rsn);
		history.add(record);
		renderHistory();
	}

	private void renderHistory()
	{
		feed.removeAll();
		count.setText(history.getCount() + " loot events");
		if (history.getRecords().isEmpty())
		{
			feed.add(note("No drops yet. Defeat an NPC that drops loot to test tracking.", CARD));
		}
		for (LootRecord record : history.getRecords())
		{
			JPanel card = column(CARD);
			card.setBorder(BorderFactory.createCompoundBorder(
				BorderFactory.createMatteBorder(0, 0, 6, 0, BACKGROUND),
				BorderFactory.createEmptyBorder(8, 8, 8, 8)));
			card.add(label(record.source, PURPLE));
			card.add(label(TIME.format(record.time) + "  |  " + record.rsn, MUTED));
			JTextArea items = note(String.join("\n", record.items), CARD);
			items.setForeground(Color.WHITE);
			card.add(items);
			feed.add(card);
		}
		feed.revalidate();
		feed.repaint();
	}

	private static JPanel column(Color background)
	{
		JPanel panel = new JPanel();
		panel.setLayout(new BoxLayout(panel, BoxLayout.Y_AXIS));
		panel.setBackground(background);
		panel.setAlignmentX(LEFT_ALIGNMENT);
		return panel;
	}

	private static JLabel label(String text, Color color)
	{
		JLabel label = new JLabel(text);
		label.putClientProperty("html.disable", Boolean.TRUE);
		label.setForeground(color);
		label.setAlignmentX(LEFT_ALIGNMENT);
		return label;
	}

	private static JLabel spacer()
	{
		return label(" ", MUTED);
	}

	private static JTextArea note(String text, Color background)
	{
		JTextArea area = new JTextArea(text);
		area.setEditable(false);
		area.setLineWrap(true);
		area.setWrapStyleWord(true);
		area.setColumns(17);
		area.setFont(new JLabel().getFont());
		area.setForeground(MUTED);
		area.setBackground(background);
		area.setBorder(BorderFactory.createEmptyBorder(6, 0, 6, 0));
		area.setAlignmentX(LEFT_ALIGNMENT);
		return area;
	}

	static BufferedImage createIcon()
	{
		BufferedImage image = new BufferedImage(24, 24, BufferedImage.TYPE_INT_ARGB);
		Graphics2D graphics = image.createGraphics();
		try
		{
			graphics.setRenderingHint(RenderingHints.KEY_ANTIALIASING, RenderingHints.VALUE_ANTIALIAS_ON);
			graphics.setColor(new Color(94, 52, 140));
			graphics.fillRoundRect(1, 1, 22, 22, 7, 7);
			graphics.setColor(Color.WHITE);
			graphics.setFont(new Font(Font.SANS_SERIF, Font.BOLD, 17));
			graphics.drawString("N", 6, 18);
		}
		finally
		{
			graphics.dispose();
		}
		return image;
	}
}
