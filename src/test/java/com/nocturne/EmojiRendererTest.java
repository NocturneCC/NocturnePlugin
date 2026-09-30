package com.nocturne;

import java.awt.image.BufferedImage;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import net.runelite.api.ChatMessageType;
import net.runelite.api.Client;
import net.runelite.api.IndexedSprite;
import net.runelite.api.MessageNode;
import net.runelite.api.Node;
import net.runelite.api.events.ChatMessage;
import net.runelite.client.util.ImageUtil;
import org.junit.Test;
import static org.junit.Assert.*;

public class EmojiRendererTest
{
	@Test public void replacesOneThroughFiveAndLeavesSixthLiteral()
	{
		Map<String, Integer> registry = Map.of("wave", 12);
		assertEquals("hello <img=12> world", EmojiRenderer.format("hello :wave: world", registry));
		assertEquals("<img=12> <img=12> <img=12> <img=12> <img=12> :wave:",
			EmojiRenderer.format(":wave: :wave: :wave: :wave: :wave: :wave:", registry));
	}

	@Test public void unknownMalformedPartialMixedCaseAndUnicodeTokensRemainExact()
	{
		Map<String, Integer> registry = Map.of("wave", 12);
		for (String value : new String[]{":unknown:", ":wave_more:", ":Wave:",
			":wаve:", "::", ":::wave:", ":wave::", ":wave", ":wave-:"})
			assertNull(EmojiRenderer.format(value, registry));
	}

	@Test public void preservesFormattingSurroundingTextAndDoesNotRecurse()
	{
		Map<String, Integer> registry = Map.of("wave", 12, "img", 13);
		String input = "<col=ff0000>A :wave:</col> <img=4> :img: tail";
		assertEquals("<col=ff0000>A <img=12></col> <img=4> <img=13> tail",
			EmojiRenderer.format(input, registry));
		assertNull(EmojiRenderer.format("<img=:wave:>", registry));
	}

	@Test public void supportedPlayerChatTypesRenderAndSystemTypesRemainLiteral()
	{
		AtomicInteger refreshes = new AtomicInteger();
		FakeIcons icons = new FakeIcons();
		EmojiRenderer renderer = new EmojiRenderer(refreshes::incrementAndGet, icons);
		renderer.update(Map.of("wave", asset("wave", "a".repeat(64))));
		java.util.Set<ChatMessageType> supported = java.util.Set.of(
			ChatMessageType.PUBLICCHAT,
			ChatMessageType.CLAN_CHAT,
			ChatMessageType.CLAN_GUEST_CHAT,
			ChatMessageType.FRIENDSCHAT,
			ChatMessageType.PRIVATECHAT,
			ChatMessageType.PRIVATECHATOUT);
		for (ChatMessageType type : ChatMessageType.values())
		{
			TestNode node = new TestNode(":wave:");
			boolean replaced = renderer.onChatMessage(event(type, node));
			assertEquals(type.name(), supported.contains(type), replaced);
			assertEquals(type.name(), ":wave:", node.getValue());
			assertEquals(type.name(), supported.contains(type) ? "<img=100>" : null,
				node.getRuneLiteFormatMessage());
		}
	}

	@Test public void receivedAndLocalEchoMessagesUseOnlyTheirDisplayNodes()
	{
		EmojiRenderer renderer = new EmojiRenderer(() -> { }, new FakeIcons());
		renderer.update(Map.of("wave", asset("wave", "a".repeat(64))));
		TestNode incoming = new TestNode(":wave:");
		TestNode outgoingLocalEcho = new TestNode(":wave:");
		assertTrue(renderer.onChatMessage(event(ChatMessageType.PRIVATECHAT, incoming)));
		assertTrue(renderer.onChatMessage(event(ChatMessageType.PRIVATECHATOUT, outgoingLocalEcho)));
		assertEquals(":wave:", incoming.getValue());
		assertEquals("<img=100>", incoming.getRuneLiteFormatMessage());
		assertEquals("<img=100>", outgoingLocalEcho.getRuneLiteFormatMessage());
	}

	@Test public void rendererRegistrationFromWorkerIsDeferredToClientThread()
		throws InterruptedException
	{
		AtomicInteger refreshes = new AtomicInteger();
		AtomicReference<Runnable> clientQueue = new AtomicReference<>();
		AtomicReference<Boolean> completion = new AtomicReference<>();
		AtomicReference<EmojiRenderer.RegistrationResult> registration = new AtomicReference<>();
		AtomicReference<Thread> clientThread = new AtomicReference<>();
		FakeIcons icons = new FakeIcons();
		EmojiRenderer renderer = new EmojiRenderer(refreshes::incrementAndGet, icons);
		EmojiSyncService.AssetListener publish = EmojiRenderer.clientThreadPublisher(
			task -> clientQueue.set(task), () -> true, renderer, registration::set);
		Thread worker = new Thread(() -> publish.publish(Map.of("abyssaldagger",
			asset("abyssaldagger", "b".repeat(64))), completion::set), "nocturne-emojis-test");
		worker.start();
		worker.join();
		assertEquals(0, icons.registrations.get());
		clientThread.set(Thread.currentThread());
		clientQueue.get().run();
		assertEquals(Boolean.TRUE, completion.get());
		assertEquals(1, registration.get().assetCount);
		assertTrue(registration.get().clientThreadTaskEntered);
		assertEquals(1, registration.get().slotsReserved);
		assertEquals(1, registration.get().iconsUpdated);
		assertEquals(1, registration.get().usableMappings);
		assertEquals("none", registration.get().failureCategory);
		assertEquals(1, icons.registrations.get());
		assertSame(clientThread.get(), icons.registrationThread.get());
		assertEquals(1, renderer.activeTriggerCountForTest());
	}

	@Test public void zeroUsableMappingPublicationClearsTriggersWithoutReportingSuccess()
	{
		EmojiRenderer renderer = new EmojiRenderer(() -> { }, new FakeIcons());
		renderer.update(Map.of("wave", asset("wave", "e".repeat(64))));
		EmojiRenderer.RegistrationResult result = renderer.update(Map.of());
		assertFalse(result.success);
		assertEquals(0, result.usableMappings);
		assertEquals("no_usable_mappings", result.failureCategory);
		assertEquals(0, renderer.activeTriggerCountForTest());
	}

	@Test public void runelitePaletteOverflowIsReproducedAndAllManifestAssetsRegisterAfterNormalization()
	{
		BufferedImage highColor = highColorImage();
		try
		{
			ImageUtil.getImageIndexedSprite(highColor, spriteClient());
			fail("RuneLite must reject 256 opaque colors plus its transparent palette entry");
		}
		catch (RuntimeException error)
		{
			assertEquals(RuntimeException.class, error.getClass());
			assertEquals("Passed in image had 256 different colors, exceeding the max of 255.",
				error.getMessage());
		}

		AtomicInteger registered = new AtomicInteger();
		AtomicInteger nextIcon = new AtomicInteger();
		EmojiRenderer.IconRegistrar runeliteSpriteConversion = new EmojiRenderer.IconRegistrar()
		{
			@Override public int reserve() { return nextIcon.getAndIncrement(); }
			@Override public void update(int icon, BufferedImage image)
			{
				assertEquals(BufferedImage.TYPE_INT_ARGB, image.getType());
				ImageUtil.getImageIndexedSprite(image, spriteClient());
				registered.incrementAndGet();
			}
			@Override public int chatIndex(int icon) { return icon + 100; }
		};
		EmojiRenderer renderer = new EmojiRenderer(() -> { }, runeliteSpriteConversion);
		Map<String, EmojiAsset> assets = new LinkedHashMap<>();
		for (int index = 0; index < 251; index++)
		{
			String name = index == 5 ? "abyssaldagger" : "emoji_" + index;
			BufferedImage image = index == 5 ? highColor : asset(name, String.format("%064x", index + 1)).image;
			assets.put(name, new EmojiAsset(name, String.format("%064x", index + 1), image));
		}
		EmojiRenderer.RegistrationResult result = renderer.update(assets);
		assertTrue(result.success);
		assertEquals(251, result.assetCount);
		assertEquals(251, result.slotsReserved);
		assertEquals(251, result.iconsUpdated);
		assertEquals(251, result.usableMappings);
		assertEquals(251, registered.get());
		assertEquals("<img=105>", EmojiRenderer.format(":abyssaldagger:",
			Map.of("abyssaldagger", 105)));
		assertEquals(251, renderer.activeTriggerCountForTest());
	}

	@Test public void chatSpriteResizeImprovesColorErrorAndFitsRuneLiteChatLine()
	{
		BufferedImage detailed = opaqueGradient(20, 20);
		BufferedImage oldReduced = legacy332(detailed);
		BufferedImage oldChatSprite = EmojiRenderer.fitToChatCanvas(oldReduced);
		BufferedImage newChatSprite = EmojiRenderer.runeLiteIconImage(detailed);
		assertEquals(20, oldReduced.getWidth());
		assertEquals(EmojiRenderer.CHAT_ICON_CANVAS_SIZE, newChatSprite.getWidth());
		assertEquals(EmojiRenderer.CHAT_ICON_CANVAS_SIZE, newChatSprite.getHeight());
		assertEquals(13, oldChatSprite.getWidth());
		assertTrue("new MSE=" + rgbMeanSquaredError(newChatSprite, referenceResize(detailed))
			+ " old MSE=" + rgbMeanSquaredError(oldChatSprite, referenceResize(detailed)),
			rgbMeanSquaredError(newChatSprite, referenceResize(detailed))
				< rgbMeanSquaredError(oldChatSprite, referenceResize(detailed)));
		assertTrue(opaqueColorCount(newChatSprite) <= 255);
		try
		{
			ImageUtil.getImageIndexedSprite(newChatSprite, spriteClient());
		}
		catch (RuntimeException failure)
		{
			fail("13x13 chat sprite must fit RuneLite's indexed palette");
		}
	}

	@Test public void chatSpriteCanvasPreservesAspectCentersNarrowImagesAndNeverUpscales()
	{
		assertFit(20, 10, 13, 7, 0, 3);
		assertFit(10, 20, 7, 13, 3, 0);
		assertFit(20, 20, 13, 13, 0, 0);
		assertFit(7, 5, 7, 5, 3, 4);
	}

	@Test public void lanczosKeepsTransparentPaddingAndLowColorPixels()
	{
		BufferedImage transparent = new BufferedImage(20, 20, BufferedImage.TYPE_INT_ARGB);
		BufferedImage transparentFit = EmojiRenderer.runeLiteIconImage(transparent);
		for (int y = 0; y < transparentFit.getHeight(); y++)
			for (int x = 0; x < transparentFit.getWidth(); x++)
				assertEquals(0, transparentFit.getRGB(x, y) >>> 24);

		BufferedImage small = new BufferedImage(13, 13, BufferedImage.TYPE_INT_ARGB);
		for (int y = 2; y < 11; y++)
			for (int x = 3; x < 10; x++) small.setRGB(x, y, 0xff3377aa);
		assertArrayEquals(small.getRGB(0, 0, 13, 13, null, 0, 13),
			EmojiRenderer.fitToChatCanvas(small).getRGB(0, 0, 13, 13, null, 0, 13));

		BufferedImage alphaGradient = new BufferedImage(20, 20, BufferedImage.TYPE_INT_ARGB);
		for (int y = 0; y < 20; y++)
			for (int x = 0; x < 20; x++)
				alphaGradient.setRGB(x, y, (x * 255 / 19) << 24 | (x * 10 << 16) | (y * 9 << 8) | 0x40);
		BufferedImage gradientFit = EmojiRenderer.runeLiteIconImage(alphaGradient);
		assertEquals(0, gradientFit.getRGB(0, 6) >>> 24);
		assertEquals(255, gradientFit.getRGB(12, 6) >>> 24);
		assertTrue(opaqueColorCount(gradientFit) <= 255);

		BufferedImage curved = new BufferedImage(20, 20, BufferedImage.TYPE_INT_ARGB);
		for (int y = 0; y < 20; y++)
		{
			for (int x = 0; x < 20; x++)
			{
				double distance = Math.hypot(x - 9.5, y - 9.5);
				int alpha = (int) Math.max(0, Math.min(255, (10.0 - distance) * 255));
				curved.setRGB(x, y, alpha << 24 | 0x00e05090);
			}
		}
		BufferedImage curvedFit = EmojiRenderer.runeLiteIconImage(curved);
		assertEquals(0, curvedFit.getRGB(0, 0) >>> 24);
		assertEquals(255, curvedFit.getRGB(6, 6) >>> 24);
		assertTrue(opaqueColorCount(curvedFit) <= 255);
	}

	@Test public void detailedAssetConversionIsDeterministic()
	{
		BufferedImage source = highColorImage();
		BufferedImage first = EmojiRenderer.runeLiteIconImage(source);
		BufferedImage second = EmojiRenderer.runeLiteIconImage(source);
		assertArrayEquals(first.getRGB(0, 0, 13, 13, null, 0, 13),
			second.getRGB(0, 0, 13, 13, null, 0, 13));
	}

	@Test public void updateFailureMetadataIsBoundedAndPartialMapIsNeverPublished()
	{
		AtomicInteger updates = new AtomicInteger();
		EmojiRenderer.IconRegistrar failing = new EmojiRenderer.IconRegistrar()
		{
			@Override public int reserve() { return updates.get(); }
			@Override public void update(int icon, BufferedImage image)
			{
				if (updates.incrementAndGet() == 6) throw new IllegalStateException("do not retain this message");
			}
			@Override public int chatIndex(int icon) { return icon + 200; }
		};
		EmojiRenderer renderer = new EmojiRenderer(() -> { }, failing);
		Map<String, EmojiAsset> assets = new LinkedHashMap<>();
		for (int index = 0; index < 8; index++)
		{
			String name = "item_" + index;
			BufferedImage image = index == 5 ? highColorImage() : asset(name,
				String.format("%064x", index + 1)).image;
			assets.put(name, new EmojiAsset(name, String.format("%064x", index + 1), image));
		}
		EmojiRenderer.RegistrationResult result = renderer.update(assets);
		assertFalse(result.success);
		assertEquals(8, result.assetCount);
		assertEquals(6, result.slotsReserved);
		assertEquals(5, result.iconsUpdated);
		assertEquals(5, result.usableMappings);
		assertEquals("icon_update_failed", result.failureCategory);
		assertEquals(6, result.failedAssetOrdinal);
		assertEquals(5, result.reservedSlot);
		assertEquals(20, result.imageWidth);
		assertEquals(20, result.imageHeight);
		assertEquals(BufferedImage.TYPE_3BYTE_BGR, result.imageType);
		assertTrue(result.colorModelClass.endsWith("ComponentColorModel"));
		assertEquals(IllegalStateException.class.getName(), result.exceptionClass);
		assertEquals(0, renderer.activeTriggerCountForTest());
	}

	@Test public void abyssalDiagnosticIsOneTimeAndContainsOnlyClassifications()
	{
		AtomicInteger refreshes = new AtomicInteger();
		AtomicInteger diagnostics = new AtomicInteger();
		AtomicReference<EmojiRenderer.RenderDiagnostic> result = new AtomicReference<>();
		EmojiRenderer renderer = new EmojiRenderer(refreshes::incrementAndGet, new FakeIcons(), value ->
		{
			diagnostics.incrementAndGet();
			result.set(value);
		});
		renderer.update(Map.of("abyssaldagger", asset("abyssaldagger", "c".repeat(64))));
		TestNode node = new TestNode(":abyssaldagger:");
		assertTrue(renderer.onChatMessage(event(ChatMessageType.PUBLICCHAT, node)));
		assertEquals("<img=100>", node.getRuneLiteFormatMessage());
		assertTrue(renderer.onChatMessage(event(ChatMessageType.PUBLICCHAT, new TestNode(":abyssaldagger:"))));
		assertEquals(1, diagnostics.get());
		assertTrue(result.get().supportedMessageType);
		assertTrue(result.get().tokenMatched);
		assertTrue(result.get().iconRegistered);
		assertTrue(result.get().nodeRewritten);
		assertTrue(result.get().refreshRequested);
	}

	@Test public void runeLiteFormatOverrideSurvivesLaterCoreEmojiValueUpdate()
	{
		EmojiRenderer renderer = new EmojiRenderer(() -> { }, new FakeIcons());
		renderer.update(Map.of("wave", asset("wave", "d".repeat(64))));
		TestNode node = new TestNode(":wave:");
		assertTrue(renderer.onChatMessage(event(ChatMessageType.PUBLICCHAT, node)));
		// RuneLite's built-in EmojiPlugin uses MessageNode.setValue(). The
		// display override consumed by ChatMessageManager remains authoritative.
		node.setValue("<img=55>");
		assertEquals("<img=55>", node.getValue());
		assertEquals("<img=100>", node.getRuneLiteFormatMessage());
	}

	@Test public void addRenameDeleteDuplicateUpdatesAndSessionCapAreBounded()
	{
		FakeIcons icons = new FakeIcons();
		EmojiRenderer renderer = new EmojiRenderer(() -> { }, icons);
		renderer.update(Map.of("old", asset("old", "a".repeat(64))));
		assertEquals(1, renderer.activeTriggerCountForTest());
		renderer.update(Map.of("renamed", asset("renamed", "a".repeat(64))));
		assertEquals(1, renderer.sessionIconCountForTest());
		TestNode old = new TestNode(":old:");
		TestNode renamed = new TestNode(":renamed:");
		assertFalse(renderer.onChatMessage(event(ChatMessageType.CLAN_CHAT, old)));
		assertTrue(renderer.onChatMessage(event(ChatMessageType.CLAN_CHAT, renamed)));
		renderer.update(Map.of());
		assertEquals(0, renderer.activeTriggerCountForTest());
		assertEquals(1, renderer.sessionIconCountForTest());
		assertFalse(renderer.onChatMessage(event(ChatMessageType.CLAN_CHAT, new TestNode(":renamed:"))));

		Map<String, EmojiAsset> many = new LinkedHashMap<>();
		for (int index = 0; index < EmojiRenderer.MAX_SESSION_ICONS + 20; index++)
		{
			String name = "e_" + index;
			many.put(name, asset(name, String.format("%064x", index + 2)));
		}
		renderer.update(many);
		assertEquals(EmojiRenderer.MAX_SESSION_ICONS, renderer.sessionIconCountForTest());
		assertTrue(renderer.activeTriggerCountForTest() <= EmojiRenderer.MAX_SESSION_ICONS);
		renderer.clear();
		assertEquals(0, renderer.activeTriggerCountForTest());
	}

	@Test public void nullAndVeryLongMessagesAreIgnoredWithoutRetention()
	{
		assertNull(EmojiRenderer.format(null, Map.of("wave", 1)));
		assertNull(EmojiRenderer.format(":wave:" + "x".repeat(EmojiRenderer.MAX_MESSAGE_CHARS),
			Map.of("wave", 1)));
	}

	private static EmojiAsset asset(String name, String digest)
	{
		return new EmojiAsset(name, digest, new BufferedImage(20, 20, BufferedImage.TYPE_INT_ARGB));
	}

	private static BufferedImage highColorImage()
	{
		BufferedImage image = new BufferedImage(20, 20, BufferedImage.TYPE_3BYTE_BGR);
		for (int index = 0; index < 256; index++)
			image.setRGB(index % 20, index / 20, 0xff010000 | (index + 1));
		return image;
	}

	private static void assertFit(int sourceWidth, int sourceHeight, int contentWidth, int contentHeight,
		int left, int top)
	{
		BufferedImage source = new BufferedImage(sourceWidth, sourceHeight, BufferedImage.TYPE_INT_ARGB);
		java.awt.Graphics2D graphics = source.createGraphics();
		try { graphics.setColor(new java.awt.Color(0x4488cc)); graphics.fillRect(0, 0, sourceWidth, sourceHeight); }
		finally { graphics.dispose(); }
		BufferedImage result = EmojiRenderer.fitToChatCanvas(source);
		assertEquals(EmojiRenderer.CHAT_ICON_CANVAS_SIZE, result.getWidth());
		assertEquals(EmojiRenderer.CHAT_ICON_CANVAS_SIZE, result.getHeight());
		int minX = result.getWidth(), minY = result.getHeight(), maxX = -1, maxY = -1;
		for (int y = 0; y < result.getHeight(); y++)
			for (int x = 0; x < result.getWidth(); x++)
				if ((result.getRGB(x, y) >>> 24) == 255)
				{
					minX = Math.min(minX, x); maxX = Math.max(maxX, x);
					minY = Math.min(minY, y); maxY = Math.max(maxY, y);
				}
		assertEquals(left, minX);
		assertEquals(top, minY);
		assertEquals(contentWidth - 1, maxX - minX);
		assertEquals(contentHeight - 1, maxY - minY);
		assertTrue(maxX < result.getWidth() && maxY < result.getHeight());
	}

	private static BufferedImage opaqueGradient(int width, int height)
	{
		BufferedImage image = new BufferedImage(width, height, BufferedImage.TYPE_INT_ARGB);
		for (int y = 0; y < height; y++)
			for (int x = 0; x < width; x++)
			{
				int red = 24 + x * 10;
				int green = 32 + y * 9;
				int blue = 28 + (x * 7 + y * 5) % 200;
				image.setRGB(x, y, 0xff000000 | red << 16 | green << 8 | blue);
			}
		return image;
	}

	private static BufferedImage legacy332(BufferedImage source)
	{
		BufferedImage reduced = new BufferedImage(source.getWidth(), source.getHeight(), BufferedImage.TYPE_INT_ARGB);
		for (int y = 0; y < source.getHeight(); y++)
			for (int x = 0; x < source.getWidth(); x++)
			{
				int pixel = source.getRGB(x, y);
				if ((pixel >>> 24) != 255) continue;
				int rgb = (((pixel >>> 16) & 0xe0) << 16) | (((pixel >>> 8) & 0xe0) << 8) | (pixel & 0xc0);
				if (rgb == 0) rgb = 1;
				reduced.setRGB(x, y, 0xff000000 | rgb);
			}
		return reduced;
	}

	private static BufferedImage referenceResize(BufferedImage source)
	{
		// The new renderer retains every filtered color: this opaque reference is
		// therefore the ideal target for measuring error introduced by old 3-3-2.
		return EmojiRenderer.fitToChatCanvas(source);
	}

	private static double rgbMeanSquaredError(BufferedImage actual, BufferedImage reference)
	{
		long error = 0;
		int count = 0;
		for (int y = 0; y < reference.getHeight(); y++)
			for (int x = 0; x < reference.getWidth(); x++)
			{
				int expected = reference.getRGB(x, y);
				if ((expected >>> 24) != 255) continue;
				int observed = actual.getRGB(x, y);
				for (int shift : new int[]{16, 8, 0})
				{
					int delta = ((expected >>> shift) & 0xff) - ((observed >>> shift) & 0xff);
					error += delta * delta;
					count++;
				}
			}
		return count == 0 ? 0 : (double) error / count;
	}

	private static int opaqueColorCount(BufferedImage image)
	{
		java.util.Set<Integer> colors = new java.util.HashSet<>();
		for (int y = 0; y < image.getHeight(); y++)
			for (int x = 0; x < image.getWidth(); x++)
			{
				int pixel = image.getRGB(x, y);
				if ((pixel >>> 24) == 255) colors.add(pixel & 0x00ffffff);
			}
		return colors.size();
	}

	private static Client spriteClient()
	{
		return (Client) java.lang.reflect.Proxy.newProxyInstance(Client.class.getClassLoader(),
			new Class<?>[]{Client.class}, (proxy, method, args) ->
			{
				if (method.getName().equals("createIndexedSprite"))
					return java.lang.reflect.Proxy.newProxyInstance(IndexedSprite.class.getClassLoader(),
						new Class<?>[]{IndexedSprite.class}, (sprite, setter, values) -> null);
				Class<?> type = method.getReturnType();
				if (type == boolean.class) return false;
				if (type == int.class) return 0;
				if (type == long.class) return 0L;
				if (type == float.class) return 0f;
				if (type == double.class) return 0d;
				return null;
			});
	}

	private static ChatMessage event(ChatMessageType type, MessageNode node)
	{
		return new ChatMessage(node, type, "name", node.getValue(), "sender", 1);
	}

	private static final class FakeIcons implements EmojiRenderer.IconRegistrar
	{
		int next;
		final AtomicInteger registrations = new AtomicInteger();
		final AtomicReference<Thread> registrationThread = new AtomicReference<>();
		@Override public int reserve() { return next++; }
		@Override public void update(int icon, BufferedImage image)
		{
			registrations.incrementAndGet();
			registrationThread.set(Thread.currentThread());
		}
		@Override public int chatIndex(int icon) { return icon + 100; }
	}

	static final class TestNode implements MessageNode
	{
		private String value;
		private String runeLiteFormatMessage;
		TestNode(String value) { this.value = value; }
		@Override public int getId() { return 1; }
		@Override public ChatMessageType getType() { return ChatMessageType.CLAN_CHAT; }
		@Override public String getName() { return "name"; }
		@Override public void setName(String name) { }
		@Override public String getSender() { return "sender"; }
		@Override public void setSender(String sender) { }
		@Override public String getValue() { return value; }
		@Override public void setValue(String value) { this.value = value; }
		@Override public String getRuneLiteFormatMessage() { return runeLiteFormatMessage; }
		@Override public void setRuneLiteFormatMessage(String message) { runeLiteFormatMessage = message; }
		@Override public int getTimestamp() { return 1; }
		@Override public void setTimestamp(int timestamp) { }
		@Override public Node getNext() { return null; }
		@Override public Node getPrevious() { return null; }
		@Override public long getHash() { return 1; }
	}
}
