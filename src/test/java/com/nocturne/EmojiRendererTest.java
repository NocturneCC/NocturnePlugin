package com.nocturne;

import java.awt.image.BufferedImage;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import net.runelite.api.ChatMessageType;
import net.runelite.api.MessageNode;
import net.runelite.api.Node;
import net.runelite.api.events.ChatMessage;
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
