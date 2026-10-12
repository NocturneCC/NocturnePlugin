package com.nocturne;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.stream.Collectors;
import org.junit.Test;
import static org.junit.Assert.*;

public class CompletionDurationParserTest
{
	private final CompletionDurationParser parser = new CompletionDurationParser();

	private List<String> fixture() throws Exception
	{
		try (BufferedReader reader = new BufferedReader(new InputStreamReader(
			getClass().getResourceAsStream("/pnm-generic-duration-fixture.txt"), StandardCharsets.UTF_8)))
		{
			return reader.lines().collect(Collectors.toList());
		}
	}

	@Test public void suppliedCombinedDurationSelectsFightDurationNotPersonalBest() throws Exception
	{
		List<CompletionDurationParser.Token> tokens = parser.parse(fixture().get(1));
		assertEquals(2, tokens.size());
		assertEquals(358_200L, tokens.get(0).durationMillis);
		assertFalse(tokens.get(0).referenceOnly);
		assertEquals(288_600L, tokens.get(1).durationMillis);
		assertTrue(tokens.get(1).referenceOnly);
	}

	@Test public void selectsCurrentCompletionWhetherOrNotItIsBetterThanPersonalBest()
	{
		assertCurrent("Fight duration: 5:58.20. Personal best: 4:48.60", 358_200L);
		assertCurrent("Fight duration: 4:48.60. Personal best: 5:58.20", 288_600L);
		assertCurrent("Personal best: 4:48.60. Fight duration: 5:58.20", 358_200L);
		assertCurrent("Fight duration: 5:58.20. Personal best: 4:48.60", 358_200L);
		assertCurrent("Fight duration: 5:58.20", 358_200L);
	}

	@Test public void normalizesTagsWhitespaceCurlyPunctuationAndFractions()
	{
		List<CompletionDurationParser.Token> tokens = parser.parse("<col=ff00ff>Fight   duration:</col> 5 : 58.20! Personal best: 4:48.60");
		assertEquals(2, tokens.size());
		assertEquals(358_200L, tokens.get(0).durationMillis);
		assertEquals(Long.valueOf(62_000L), CompletionDurationParser.parseDuration("1:02"));
		assertEquals(Long.valueOf(62_340L), CompletionDurationParser.parseDuration("1:02.34"));
		assertEquals(Long.valueOf(3_723_450L), CompletionDurationParser.parseDuration("1:02:03.45"));
		assertEquals(Long.valueOf(3_723_000L), CompletionDurationParser.parseDuration("1 : 02 : 03"));
		assertEquals("Phosani's Nightmare", parser.normalize("<col=fff>Phosani’s   Nightmare</col>"));
	}

	@Test public void malformedAndOverflowingTokensAreRejected()
	{
		assertNull(CompletionDurationParser.parseDuration("1:60"));
		assertNull(CompletionDurationParser.parseDuration("1:02:60"));
		assertNull(CompletionDurationParser.parseDuration("24:00:00.01"));
		assertNull(CompletionDurationParser.parseDuration("999999999999999999999:00"));
		assertNull(CompletionDurationParser.parseDuration("0:00"));
		assertTrue(parser.parse("Duration: 3:72.40").isEmpty());
	}

	@Test public void unrelatedTimeShapedChatIsNotMessageEligible()
	{
		assertFalse(CompletionMessageBuffer.eligibleCategory("CLAN_CHAT"));
		assertFalse(CompletionMessageBuffer.eligibleCategory("FRIENDSCHATNOTIFICATION"));
		assertTrue(CompletionMessageBuffer.eligibleCategory("GAMEMESSAGE"));
		assertTrue(CompletionMessageBuffer.eligibleCategory("ENGINE"));
	}

	@Test public void messageBufferIsBoundedExpiresAndSuppressesImmediateReplay()
	{
		CompletionMessageBuffer buffer = new CompletionMessageBuffer();
		CompletionMessageBuffer.Entry first = buffer.offer("GAMEMESSAGE", "Fight duration: 5:58.20",
			1, 1_000_000_000L, parser);
		CompletionMessageBuffer.Entry replay = buffer.offer("GAMEMESSAGE", "Fight duration: 5:58.20",
			2, 1_200_000_000L, parser);
		assertEquals(first.sequence, replay.sequence);
		for (int i = 0; i < CompletionMessageBuffer.CAPACITY + 10; i++)
			buffer.offer("ENGINE", "System event " + i, i + 3, 2_000_000_000L + i, parser);
		assertEquals(CompletionMessageBuffer.CAPACITY, buffer.size());
		assertTrue(buffer.snapshot(CompletionMessageBuffer.TTL_NANOS + 10_000_000_000L).isEmpty());
	}

	private void assertCurrent(String text, long expected)
	{
		List<CompletionDurationParser.Token> tokens = parser.parse(text);
		List<CompletionDurationParser.Token> current = tokens.stream().filter(t -> !t.referenceOnly).collect(Collectors.toList());
		assertEquals(1, current.size());
		assertEquals(expected, current.get(0).durationMillis);
	}
}
