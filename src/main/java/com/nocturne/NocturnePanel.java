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
import javax.swing.JScrollBar;
import javax.swing.JScrollPane;
import javax.swing.JTextArea;
import javax.swing.SwingUtilities;
import javax.swing.JOptionPane;
import net.runelite.client.ui.PluginPanel;
import net.runelite.client.game.ItemManager;
import java.awt.Dimension;
import java.awt.Component;

final class NocturnePanel extends PluginPanel
{
	private static final Color BACKGROUND = new Color(25, 21, 34);
	private static final Color CARD = new Color(42, 33, 56);
	private static final Color PURPLE = new Color(184, 142, 238);
	private static final Color MUTED = new Color(186, 178, 198);
	private static final DateTimeFormatter TIME = DateTimeFormatter.ofPattern("HH:mm:ss");

	private final ItemManager itemManager;
	private final HistoryActions historyActions;
	private boolean diagnostics;
	private GroupSnapshot liveGroup = GroupSnapshot.unavailable("Enter a raid to preview its roster.");
	private final JTextArea connection = note("Local capture · submissions off", BACKGROUND);
	private final LootHistory history = new LootHistory();
	private final JLabel player = label("Log in to see your character", Color.WHITE);
	private final JLabel tracking = label("Loot tracking enabled", PURPLE);
	private final JLabel count = label("0 loot events", MUTED);
	private final JButton loadOlder = new JButton("Load 50 older events");
	private final JPanel feed = new JPanel();
	private final JTextArea groupPreview = note("Enter a raid to preview its roster.", BACKGROUND);
	private final JTextArea raidDiagnostics = note("Raid diagnostics inactive.", BACKGROUND);
	private ViewportAnchor pendingPrependAnchor;
	private int viewportGeneration;
	private boolean viewportRestoreScheduled;
	private long selectedHistoryGeneration;

	NocturnePanel(ItemManager itemManager)
	{
		this(itemManager, HistoryActions.NONE);
	}

	NocturnePanel(ItemManager itemManager, HistoryActions historyActions)
	{
		this.itemManager = itemManager;
		this.historyActions = historyActions;
		setBackground(BACKGROUND);
		setLayout(new BorderLayout(0, 12));
		setBorder(BorderFactory.createEmptyBorder(12, 9, 12, 9));

		JPanel header = column(BACKGROUND);
		JLabel title = label("NOCTURNE", PURPLE);
		title.setFont(title.getFont().deriveFont(Font.BOLD, 21f));
		header.add(title);
		header.add(label("COMPANION  /  PREVIEW " + PluginMetadata.VERSION, MUTED));
		header.add(spacer());
		header.add(label("CHARACTER", MUTED));
		header.add(player);
		header.add(spacer());
		header.add(tracking);
		header.add(connection);
		header.add(spacer());
		header.add(groupPreview);
		header.add(raidDiagnostics);
		header.add(spacer());
		header.add(label("RECENT LOOT", PURPLE));
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
			if (history.getPlayer() != null && JOptionPane.showConfirmDialog(this,
				"Clear local loot history for " + history.getPlayer() + " only?",
				"Clear local history", JOptionPane.OK_CANCEL_OPTION, JOptionPane.WARNING_MESSAGE)
				== JOptionPane.OK_OPTION)
			{
				historyActions.clear(history.getPlayer());
			}
		});
		loadOlder.setBackground(CARD);
		loadOlder.setForeground(Color.WHITE);
		loadOlder.setFocusable(false);
		loadOlder.addActionListener(event ->
		{
			loadOlder.setEnabled(false);
			historyActions.loadOlder(history.getPlayer(), history.getRecords().size());
		});
		footer.add(loadOlder);
		footer.add(clear);
		footer.add(note("Stored locally per character. Persisted records are display-only and are never resubmitted.", BACKGROUND));
		add(footer, BorderLayout.SOUTH);
		renderHistory();
	}

	void setPlayer(String rsn)
	{
		setPlayer(rsn, selectedHistoryGeneration + 1);
	}

	void setPlayer(String rsn, long generation)
	{
		requireEdt();
		selectedHistoryGeneration = generation;
		if (history.setPlayer(rsn))
		{
			viewportGeneration++;
			pendingPrependAnchor = null;
			player.setText(rsn == null ? "Log in to see your character" : rsn);
			liveGroup = GroupSnapshot.unavailable("Enter a raid to preview its roster.");
			setGroup(liveGroup);
			renderHistory();
		}
	}

	void setLoggedOut()
	{
		player.setText(history.getPlayer() == null ? "Log in to see your character"
			: "Logged out · showing " + history.getPlayer());
	}

	void showHistory(String rsn, LootHistoryStore.Page page, boolean append)
	{
		showHistory(rsn, page, append, selectedHistoryGeneration);
	}

	void showHistory(String rsn, LootHistoryStore.Page page, boolean append, long generation)
	{
		requireEdt();
		if (!matchesHistorySelection(rsn, generation)) return;
		ViewportAnchor anchor = append ? captureViewportAnchor() : ViewportAnchor.TOP;
		if (append) history.appendOlder(page); else history.replace(page);
		renderHistory();
		loadOlder.setEnabled(true);
		restoreAfterLayout(anchor, ++viewportGeneration);
	}

	void historyLoadFailed(String rsn, long generation)
	{
		if (matchesHistorySelection(rsn, generation)) loadOlder.setEnabled(true);
	}

	void historyCleared(String rsn, long generation)
	{
		if (matchesHistorySelection(rsn, generation))
		{
			history.clear();
			renderHistory();
		}
	}

	void updateHistoryStats(String rsn, int totalCount, long bytes)
	{
		if (java.util.Objects.equals(history.getPlayer(), rsn))
		{
			history.updateStats(totalCount, bytes);
			count.setText(historySummary());
		}
	}

	void setTracking(boolean enabled)
	{
		tracking.setText(enabled ? "Loot tracking enabled" : "Loot tracking paused");
		tracking.setForeground(enabled ? PURPLE : MUTED);
	}

	void setGroup(GroupSnapshot group)
	{
		liveGroup = group;
		groupPreview.setVisible(diagnostics);
		String text = group.displayText();
		if (!text.equals(groupPreview.getText()))
		{
			groupPreview.setText(text);
			revalidate();
		}
	}

	void setRaidDiagnostics(RaidDiagnostics value)
	{
		raidDiagnostics.setText(value.displayText());
		raidDiagnostics.setVisible(diagnostics);
	}

	void recordPersistedLoot(LootRecord record, int totalCount, long bytes, long generation)
	{
		requireEdt();
		if (!matchesHistorySelection(record.rsn, generation)) return;
		prepend(record, () -> history.updateStats(totalCount, bytes));
	}

	void recordUnsavedLoot(LootRecord record, long generation)
	{
		requireEdt();
		if (!matchesHistorySelection(record.rsn, generation)) return;
		history.markStorageFailure();
		prepend(record, null);
	}

	private void prepend(LootRecord record, Runnable afterAdd)
	{
		if (pendingPrependAnchor == null)
		{
			pendingPrependAnchor = captureViewportAnchor();
		}
		history.add(record);
		if (afterAdd != null) afterAdd.run();
		renderHistory();
		schedulePrependRestore();
	}

	private boolean matchesHistorySelection(String rsn, long generation)
	{
		return selectedHistoryGeneration == generation
			&& java.util.Objects.equals(history.getPlayer(), rsn);
	}

	private void schedulePrependRestore()
	{
		if (viewportRestoreScheduled)
		{
			return;
		}
		viewportRestoreScheduled = true;
		int generation = viewportGeneration;
		SwingUtilities.invokeLater(() ->
		{
			ViewportAnchor anchor = pendingPrependAnchor;
			if (generation == viewportGeneration) restoreViewport(anchor);
			SwingUtilities.invokeLater(() ->
			{
				viewportRestoreScheduled = false;
				if (generation == viewportGeneration) restoreViewport(anchor);
				if (pendingPrependAnchor == anchor) pendingPrependAnchor = null;
			});
		});
	}

	private void restoreAfterLayout(ViewportAnchor anchor, int generation)
	{
		SwingUtilities.invokeLater(() ->
		{
			if (generation != viewportGeneration) return;
			restoreViewport(anchor);
			SwingUtilities.invokeLater(() ->
			{
				if (generation == viewportGeneration) restoreViewport(anchor);
			});
		});
	}

	private ViewportAnchor captureViewportAnchor()
	{
		JScrollBar scrollBar = getScrollPane().getVerticalScrollBar();
		if (scrollBar.getValue() == scrollBar.getMinimum()) return ViewportAnchor.TOP;
		Component view = getScrollPane().getViewport().getView();
		int viewportY = getScrollPane().getViewport().getViewPosition().y;
		for (Component component : feed.getComponents())
		{
			int componentY = SwingUtilities.convertPoint(component, 0, 0, view).y;
			if (componentY + component.getHeight() > viewportY
				&& component instanceof JPanel)
			{
				Object id = ((JPanel) component).getClientProperty("lootRecordId");
				if (id != null) return new ViewportAnchor(id.toString(), viewportY - componentY);
			}
		}
		return new ViewportAnchor(null, viewportY);
	}

	private void restoreViewport(ViewportAnchor anchor)
	{
		if (anchor == null) return;
		if (anchor == ViewportAnchor.TOP)
		{
			getScrollPane().getVerticalScrollBar().setValue(0);
			return;
		}
		if (anchor.recordId != null)
		{
			Component view = getScrollPane().getViewport().getView();
			for (Component component : feed.getComponents())
			{
				if (component instanceof JPanel && anchor.recordId.equals(
					((JPanel) component).getClientProperty("lootRecordId")))
				{
					int componentY = SwingUtilities.convertPoint(component, 0, 0, view).y;
					getScrollPane().getVerticalScrollBar().setValue(componentY + anchor.pixelOffset);
					return;
				}
			}
		}
		getScrollPane().getVerticalScrollBar().setValue(anchor.pixelOffset);
	}

	private static void requireEdt()
	{
		if (!SwingUtilities.isEventDispatchThread())
		{
			throw new IllegalStateException("Nocturne panel mutations must run on the EDT");
		}
	}

	JScrollPane scrollPane()
	{
		return getScrollPane();
	}

	void setDiagnostics(boolean enabled)
	{
		if (diagnostics != enabled)
		{
			diagnostics = enabled;
			setGroup(liveGroup);
			renderHistory();
		}
		groupPreview.setVisible(enabled);
		raidDiagnostics.setVisible(enabled);
	}

	void setSubmissionEnabled(boolean enabled)
	{
		connection.setText(enabled ? "Test submissions on · no points awarded" : "Local capture · submissions off");
	}

	void setSubmission(String id, SubmissionStatus status)
	{
		for (LootRecord record : history.getRecords())
		{
			if (record.id.equals(id))
			{
				record.submission = status;
				renderHistory();
				break;
			}
		}
	}

	private void renderHistory()
	{
		feed.removeAll();
		count.setText(historySummary());
		loadOlder.setVisible(history.hasOlder());
		if (history.getRecords().isEmpty())
		{
			feed.add(note("No drops yet. Defeat an NPC that drops loot to test tracking.", CARD));
		}
		for (LootRecord record : history.getRecords())
		{
			feed.add(renderRecord(record));
		}
		feed.revalidate();
		feed.repaint();
	}

	String historySummary()
	{
		return history.getCount() + " loot events · " + (history.hasStorageFailure()
			? "history not saved" : storageText(history.getStorageBytes()) + " local");
	}

	static String storageText(long bytes)
	{
		if (bytes < 1024) return bytes + " B";
		if (bytes < 1024 * 1024) return String.format(java.util.Locale.US, "%.1f KiB", bytes / 1024d);
		return String.format(java.util.Locale.US, "%.1f MiB", bytes / (1024d * 1024d));
	}

	interface HistoryActions
	{
		HistoryActions NONE = new HistoryActions() { public void loadOlder(String rsn, int offset) { }
			public void clear(String rsn) { } };
		void loadOlder(String rsn, int offset);
		void clear(String rsn);
	}

	JPanel renderRecord(LootRecord record)
	{
			JPanel card = column(CARD);
			card.putClientProperty("lootRecordId", record.id);
			card.setBorder(BorderFactory.createCompoundBorder(
				BorderFactory.createMatteBorder(0, 0, 6, 0, BACKGROUND),
				BorderFactory.createEmptyBorder(8, 8, 8, 8)));
			card.add(label(record.source, PURPLE));
			card.add(label(TIME.format(record.time) + "  |  " + record.rsn, MUTED));
			for (LootItem item : record.items)
			{
				JPanel row = new JPanel(new BorderLayout(7, 0));
				row.setBackground(CARD);
				row.setAlignmentX(LEFT_ALIGNMENT);
				JLabel icon = label("", Color.WHITE);
				icon.setPreferredSize(new Dimension(36, 32));
				itemManager.getImage(item.id).addTo(icon);
				row.add(icon, BorderLayout.WEST);
				String price = priceText(item);
				JTextArea text = note(item.quantity + " × " + item.name
					+ (diagnostics ? " [" + item.id + "]" : "") + price, CARD);
				text.setForeground(Color.WHITE);
				row.add(text, BorderLayout.CENTER);
				card.add(row);
			}
			card.add(note(record.submission.label, CARD));
			if (record.group.eligibilityNote != null)
			{
				card.add(note(record.group.eligibilityNote, CARD));
			}
			if (record.group.rosterState != null)
			{
				card.add(note("Roster snapshot: " + record.group.rosterState.replace('_', ' '), CARD));
			}
			if (record.group.scoringMode != null)
			{
				card.add(note("Proposed scoring mode: " + record.group.scoringMode, CARD));
			}
			if (record.group.status == GroupSnapshot.Status.MATCHED
				|| record.group.status == GroupSnapshot.Status.INCOMPLETE)
			{
				boolean rewardRoster = RaidType.fromSource(record.source) != null;
				card.add(note((rewardRoster ? "Raid roster" : "Active raid context")
					+ (record.group.status == GroupSnapshot.Status.INCOMPLETE
					? " (incomplete)" : "") + " · local only\n"
					+ (record.group.names.isEmpty() ? "Unavailable" : String.join(", ", record.group.names)), CARD));
			}
			if (diagnostics) card.add(note(record.group.displayText(), CARD));
			return card;
	}

	private static final class ViewportAnchor
	{
		private static final ViewportAnchor TOP = new ViewportAnchor(null, 0);
		private final String recordId;
		private final int pixelOffset;

		private ViewportAnchor(String recordId, int pixelOffset)
		{
			this.recordId = recordId;
			this.pixelOffset = pixelOffset;
		}
	}

	static String priceText(LootItem item)
	{
		String price = item.unitPriceGp > 0
			? "\n" + String.format(java.util.Locale.US, "%,d gp each", item.unitPriceGp)
			: "\nPrice unavailable";
		if (item.valuationRuleId != null)
		{
			price += "\nDerived from " + item.finishedOutputItemName + " ("
				+ String.format(java.util.Locale.US, "%,d gp", item.finishedOutputMarketPriceGp) + ")";
		}
		return price;
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
