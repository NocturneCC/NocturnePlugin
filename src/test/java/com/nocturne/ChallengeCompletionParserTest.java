package com.nocturne;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.List;
import java.util.stream.Collectors;
import org.junit.Test;
import static org.junit.Assert.*;

public class ChallengeCompletionParserTest
{
	private final ChallengeCompletionParser parser = new ChallengeCompletionParser();
	private List<String> fixture(String name) throws Exception
	{
		try (BufferedReader reader = new BufferedReader(new InputStreamReader(getClass().getResourceAsStream("/" + name), StandardCharsets.UTF_8)))
		{ return reader.lines().collect(Collectors.toList()); }
	}
	@Test public void parsesTobMetricsAndIgnoresWaveDuration() throws Exception
	{
		List<String> lines = fixture("tob-completion-fixture.txt");
		assertEquals(901200L, parser.parse(lines.get(0)).durationMillis);
		assertEquals(ChallengeCompletionParser.Kind.OVERALL_TIME, parser.parse(lines.get(1)).kind);
		assertEquals(ChallengeCompletionParser.Mode.REGULAR, parser.parse(lines.get(2)).mode);
		assertNull(parser.parse(lines.get(3)));
	}
	@Test public void hmtNeedsSeparateConfirmation() throws Exception
	{
		List<String> lines = fixture("hmt-completion-fixture.txt");
		assertNull(parser.parse(lines.get(0)).mode);
		assertNull(parser.parse(lines.get(1)).mode);
		assertEquals(ChallengeCompletionParser.Mode.HARD, parser.parse(lines.get(2)).mode);
	}
	@Test public void parsesToaExpertAndCoxOverall() throws Exception
	{
		ChallengeCompletionParser.Message toa = parser.parse(fixture("toa-expert-completion-fixture.txt").get(0));
		assertEquals(ChallengeCompletionParser.Mode.EXPERT, toa.mode); assertEquals(2_101_200L, toa.durationMillis);
		ChallengeCompletionParser.Message cox = parser.parse(fixture("cox-cm-completion-fixture.txt").get(0));
		assertEquals(5, cox.teamSize); assertEquals(1_321_200L, cox.durationMillis);
	}
	@Test public void optionalPbAndFractionalUnitsAreExact()
	{
		assertEquals(Long.valueOf(901200), ChallengeCompletionParser.parseDuration("15:01.20"));
		assertEquals(Long.valueOf(901234), ChallengeCompletionParser.parseDuration("15:01.234"));
		assertEquals(Long.valueOf(901000), ChallengeCompletionParser.parseDuration("15:01"));
		assertEquals(901200L, parser.parse("Theatre of Blood completion time: 15:01.20.").durationMillis);
	}
	@Test public void rejectsMalformedOverflowAndLooseText()
	{
		assertNull(ChallengeCompletionParser.parseDuration("999999999999999999999:00"));
		assertNull(ChallengeCompletionParser.parseDuration("15:99"));
		assertNull(ChallengeCompletionParser.parseDuration("0:00"));
		assertNull(ChallengeCompletionParser.parseDuration("24:00:00.001"));
		assertNull(parser.parse("Someone says Theatre of Blood completion time: 15:01.20"));
		assertNull(parser.parse("Theatre of Blood total completion time: 15:xx"));
		assertNull(parser.parse("Duration: 3:52.40"));
	}
}
