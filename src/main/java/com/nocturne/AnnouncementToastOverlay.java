package com.nocturne;

import java.awt.Color;
import java.awt.Dimension;
import java.awt.Font;
import java.awt.FontMetrics;
import java.awt.Graphics2D;
import java.awt.Point;
import java.awt.Rectangle;
import java.util.Collections;
import java.util.IdentityHashMap;
import java.util.Set;
import java.util.function.LongSupplier;
import net.runelite.api.Client;
import net.runelite.api.widgets.Widget;
import net.runelite.api.widgets.WidgetInfo;
import net.runelite.client.ui.FontManager;
import net.runelite.client.ui.overlay.Overlay;
import net.runelite.client.ui.overlay.OverlayLayer;
import net.runelite.client.ui.overlay.OverlayPosition;

/** Unboxed, session-only announcement text drawn immediately above the chatbox. */
final class AnnouncementToastOverlay extends Overlay implements AutoCloseable
{
	private static final int HORIZONTAL_INSET = 4;
	private static final int CHATBOX_GAP = 2;
	private static final Color SHADOW = new Color(0, 0, 0, 190);
	private static final Color YELLOW = new Color(255, 255, 0);
	private final Client client;
	private final AnnouncementToastQueue queue;

	AnnouncementToastOverlay(Client client)
	{
		this(client, System::nanoTime);
	}

	AnnouncementToastOverlay(Client client, LongSupplier nanoTime)
	{
		this.client = client;
		this.queue = new AnnouncementToastQueue(nanoTime);
		setPosition(OverlayPosition.DYNAMIC);
		setLayer(OverlayLayer.ABOVE_WIDGETS);
		setPriority(PRIORITY_HIGH);
		setResizable(false);
		setMovable(true);
		setDragTargetable(false);
		setSnappable(false);
	}

	synchronized boolean enqueue(Announcement announcement)
	{
		return queue.enqueue(announcement);
	}

	@Override public Dimension render(Graphics2D graphics)
	{
		Widget chatbox = visibleChatbox();
		if (chatbox == null) return null;
		Rectangle bounds = chatbox.getBounds();
		if (bounds == null || bounds.width <= HORIZONTAL_INSET * 2 || bounds.y <= 0) return null;
		AnnouncementToastQueue.Display display = queue.current();
		if (display == null) return null;

		Font previousFont = graphics.getFont();
		Color previousColor = graphics.getColor();
		try
		{
			Font font = FontManager.getRunescapeSmallFont();
			graphics.setFont(font);
			FontMetrics metrics = graphics.getFontMetrics(font);
			int width = bounds.width - HORIZONTAL_INSET * 2;
			String text = fit(display.text(), metrics, width);
			int height = metrics.getHeight();
			Point location = new Point(bounds.x + HORIZONTAL_INSET,
				bounds.y - height - CHATBOX_GAP);
			setPreferredLocation(location);
			setPreferredSize(new Dimension(width, height));
			Rectangle overlayBounds = getBounds();
			if (overlayBounds.x != location.x || overlayBounds.y != location.y
				|| overlayBounds.width != width || overlayBounds.height != height)
			{
				return null;
			}
			graphics.setColor(SHADOW);
			graphics.drawString(text, 1, metrics.getAscent() + 1);
			graphics.setColor(YELLOW);
			graphics.drawString(text, 0, metrics.getAscent());
			return new Dimension(width, height);
		}
		finally
		{
			graphics.setFont(previousFont);
			graphics.setColor(previousColor);
		}
	}

	private Widget visibleChatbox()
	{
		Widget chatbox = client.getWidget(WidgetInfo.CHATBOX);
		if (chatbox == null) return null;
		Set<Widget> visited = Collections.newSetFromMap(new IdentityHashMap<>());
		Widget widget = chatbox;
		for (int depth = 0; widget != null && depth < 32; depth++)
		{
			if (!visited.add(widget) || widget.isHidden()) return null;
			widget = widget.getParent();
		}
		return widget == null ? chatbox : null;
	}

	static String fit(String text, FontMetrics metrics, int maxWidth)
	{
		if (maxWidth <= 0) return "";
		if (metrics.stringWidth(text) <= maxWidth) return text;
		String ellipsis = "…";
		int end = 0;
		for (int offset = 0; offset < text.length();)
		{
			int codePoint = text.codePointAt(offset);
			int next = offset + Character.charCount(codePoint);
			if (metrics.stringWidth(text.substring(0, next) + ellipsis) > maxWidth) break;
			end = next;
			offset = next;
		}
		return text.substring(0, end) + ellipsis;
	}

	@Override public void close()
	{
		queue.close();
	}
}
