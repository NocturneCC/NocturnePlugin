package com.nocturne;

import java.awt.Dimension;
import java.awt.Graphics2D;
import java.awt.Point;
import java.awt.Rectangle;
import java.awt.image.BufferedImage;
import java.lang.reflect.InvocationHandler;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.time.Instant;
import java.util.concurrent.atomic.AtomicLong;
import net.runelite.api.Client;
import net.runelite.api.widgets.Widget;
import net.runelite.api.widgets.WidgetInfo;
import org.junit.Test;
import static org.junit.Assert.*;

public class AnnouncementToastOverlayTest
{
	private static final Instant NOW = Instant.parse("2026-10-01T12:00:00Z");

	@Test public void firstDisplayUsesExpectedYellowLineAndRevisionIdentity()
	{
		AtomicLong time = new AtomicLong(7_000_000_000L);
		AnnouncementToastQueue queue = new AnnouncementToastQueue(time::get);
		Announcement first = announcement("event", 1, "Tonight", "Clan event begins soon");

		assertTrue(queue.enqueue(first));
		assertEquals("Clan announcement (1:00): Tonight — Clan event begins soon", queue.current().text());
		assertFalse(queue.enqueue(announcement("event", 1, "Tonight", "Clan event begins soon")));
		assertTrue(queue.enqueue(announcement("event", 2, "Tonight", "Updated event time")));
		assertEquals(1, queue.pendingCount());
	}

	@Test public void countdownExpiresAtSixtySecondsAndRevisedAnnouncementDisplays()
	{
		AtomicLong time = new AtomicLong(0);
		AnnouncementToastQueue queue = new AnnouncementToastQueue(time::get);
		queue.enqueue(announcement("event", 1, "Notice", "First revision"));
		queue.enqueue(announcement("event", 2, "Notice", "Second revision"));
		assertEquals("Clan announcement (1:00): Notice — First revision", queue.current().text());

		time.set(59_000_000_000L);
		assertEquals("Clan announcement (0:01): Notice — First revision", queue.current().text());
		time.set(59_999_999_999L);
		assertEquals("Clan announcement (0:01): Notice — First revision", queue.current().text());
		time.set(60_000_000_000L);
		assertEquals("Clan announcement (0:00): Notice — First revision", queue.current().text());
		time.incrementAndGet();
		assertEquals("Clan announcement (1:00): Notice — Second revision", queue.current().text());
		time.addAndGet(60_000_000_001L);
		assertNull(queue.current());
	}

	@Test public void missingChatboxDoesNotConsumeDisplayLifetime()
	{
		AtomicLong time = new AtomicLong(0);
		WidgetFixture widget = new WidgetFixture();
		widget.present = false;
		Client client = clientFor(widget);
		AnnouncementToastOverlay overlay = new AnnouncementToastOverlay(client, time::get);
		overlay.enqueue(announcement("event", 1, "Clan", "Announcement text"));
		BufferedImage canvas = new BufferedImage(1000, 800, BufferedImage.TYPE_INT_ARGB);
		BufferedImage controlCanvas = new BufferedImage(1000, 800, BufferedImage.TYPE_INT_ARGB);
		Graphics2D graphics = canvas.createGraphics();
		Graphics2D controlGraphics = controlCanvas.createGraphics();
		AnnouncementToastOverlay control = new AnnouncementToastOverlay(client, time::get);
		control.enqueue(announcement("event", 1, "Clan", "Announcement text"));
		try
		{
			assertNull(overlay.render(graphics));
			time.set(120_000_000_000L);
			widget.present = true;
			assertNull(overlay.render(graphics)); // overlay manager applies the new bounds next frame
			Point position = overlay.getPreferredLocation();
			Dimension desired = overlay.getPreferredSize();
			overlay.setBounds(new Rectangle(position, desired));
			assertNotNull(overlay.render(graphics));

			assertNull(control.render(controlGraphics));
			position = control.getPreferredLocation();
			desired = control.getPreferredSize();
			control.setBounds(new Rectangle(position, desired));
			assertNotNull(control.render(controlGraphics));
			assertImagesEqual(canvas, controlCanvas);
		}
		finally
		{
			graphics.dispose();
			controlGraphics.dispose();
			 overlay.close();
			control.close();
		}
	}

	@Test public void queuedAnnouncementsDisplayInReceiveOrderWithoutOverlap()
	{
		AtomicLong time = new AtomicLong(100);
		AnnouncementToastQueue queue = new AnnouncementToastQueue(time::get);
		queue.enqueue(announcement("first", 1, "First", "one"));
		queue.enqueue(announcement("second", 1, "Second", "two"));
		queue.enqueue(announcement("third", 1, "Third", "three"));
		assertEquals("first", queue.current().announcement.id);
		assertEquals(2, queue.pendingCount());
		time.addAndGet(60_000_000_001L);
		assertEquals("second", queue.current().announcement.id);
		time.addAndGet(60_000_000_001L);
		assertEquals("third", queue.current().announcement.id);
	}

	@Test public void shutdownClearsActiveAndQueuedRevisions()
	{
		AnnouncementToastQueue queue = new AnnouncementToastQueue(() -> 10L);
		queue.enqueue(announcement("first", 1, "First", "one"));
		queue.enqueue(announcement("second", 1, "Second", "two"));
		queue.close();
		assertNull(queue.current());
		assertEquals(0, queue.pendingCount());
		assertFalse(queue.enqueue(announcement("third", 1, "Third", "three")));
	}

	@Test public void overlayUsesCurrentChatboxBoundsAndDoesNotDrawWhenMissingOrHidden()
	{
		AtomicLong time = new AtomicLong(0);
		WidgetFixture widget = new WidgetFixture();
		Client client = proxy(Client.class, (proxy, method, args) ->
			method.getName().equals("getWidget") && args != null && args.length == 1
				&& args[0] == WidgetInfo.CHATBOX && widget.present ? widget.widget : null);
		AnnouncementToastOverlay overlay = new AnnouncementToastOverlay(client, time::get);
		overlay.enqueue(announcement("event", 1, "Clan", "Announcement text"));
		BufferedImage canvas = new BufferedImage(1000, 800, BufferedImage.TYPE_INT_ARGB);
		Graphics2D graphics = canvas.createGraphics();
		try
		{
			assertNull(overlay.render(graphics));
			Dimension desired = overlay.getPreferredSize();
			Point position = overlay.getPreferredLocation();
			overlay.setBounds(new Rectangle(position, desired));
			Dimension rendered = overlay.render(graphics);
			assertNotNull(rendered);
			assertEquals(508, rendered.width);
			assertEquals(widget.bounds.x + 4, overlay.getPreferredLocation().x);
			assertEquals(widget.bounds.y - rendered.height - 2, overlay.getPreferredLocation().y);

			widget.bounds = new Rectangle(310, 430, 700, 480);
			assertNull(overlay.render(graphics));
			desired = overlay.getPreferredSize();
			position = overlay.getPreferredLocation();
			overlay.setBounds(new Rectangle(position, desired));
			rendered = overlay.render(graphics);
			assertEquals(692, rendered.width);
			assertEquals(new Point(314, 430 - rendered.height - 2), overlay.getPreferredLocation());

			widget.hidden = true;
			assertNull(overlay.render(graphics));
			widget.hidden = false;
			widget.present = false;
			assertNull(overlay.render(graphics));
		}
		finally
		{
			graphics.dispose();
			overlay.close();
		}
	}

	private static Announcement announcement(String id, int revision, String title, String message)
	{
		return new Announcement(id, revision, title, message, "notice", NOW.minusSeconds(1),
			NOW.plusSeconds(3600), null, null);
	}

	private static Client clientFor(WidgetFixture widget)
	{
		return proxy(Client.class, (proxy, method, args) ->
			method.getName().equals("getWidget") && args != null && args.length == 1
				&& args[0] == WidgetInfo.CHATBOX && widget.present ? widget.widget : null);
	}

	private static void assertImagesEqual(BufferedImage actual, BufferedImage expected)
	{
		for (int y = 0; y < actual.getHeight(); y++)
		{
			for (int x = 0; x < actual.getWidth(); x++)
			{
				assertEquals("pixel at " + x + "," + y, expected.getRGB(x, y), actual.getRGB(x, y));
			}
		}
	}

	@SuppressWarnings("unchecked")
	private static <T> T proxy(Class<T> type, InvocationHandler handler)
	{
		return (T) Proxy.newProxyInstance(type.getClassLoader(), new Class<?>[] {type}, handler);
	}

	private static final class WidgetFixture
	{
		private Rectangle bounds = new Rectangle(100, 600, 516, 165);
		private boolean hidden;
		private boolean present = true;
		private final Widget widget = proxy(Widget.class, this::invoke);

		private Object invoke(Object proxy, Method method, Object[] args)
		{
			switch (method.getName())
			{
				case "isHidden": return hidden;
				case "getParent": return null;
				case "getBounds": return new Rectangle(bounds);
				case "toString": return "widget-fixture";
				case "hashCode": return System.identityHashCode(proxy);
				case "equals": return proxy == args[0];
				default:
					if (method.getReturnType() == boolean.class) return false;
					if (method.getReturnType() == int.class) return 0;
					return null;
			}
		}
	}
}
