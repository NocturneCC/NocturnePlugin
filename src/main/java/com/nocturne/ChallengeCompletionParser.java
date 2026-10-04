package com.nocturne;

import java.math.BigInteger;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import net.runelite.client.util.Text;

/** Parses only the bounded, anchored Jagex completion messages covered by fixtures. */
final class ChallengeCompletionParser
{
	enum Family { TOB, TOA, COX }
	enum Kind { ROOM_TIME, OVERALL_TIME, MODE_COUNT, COX_RESULT }
	enum Mode { REGULAR, HARD, NORMAL, EXPERT, CHALLENGE }

	static final long MAX_DURATION_MILLIS = 86_400_000L;
	static final int MAX_COMPLETION_COUNT = 1_000_000;
	static final int MAX_GROUP_SIZE = 10;
	private static final String TIME = "((?:[0-9]{1,4}:[0-5][0-9]|[0-9]{1,2}:[0-5][0-9]:[0-5][0-9])(?:\\.[0-9]{1,3})?)";
	private static final String PB = "(?:\\.?\\s+Personal best:\\s*" + TIME + ")?\\.?";
	private static final int FLAGS = Pattern.CASE_INSENSITIVE | Pattern.UNICODE_CASE;
	private static final Pattern TOB_ROOM = Pattern.compile("^Theatre of Blood completion time:\\s*" + TIME + PB + "\\s*$", FLAGS);
	private static final Pattern TOB_OVERALL = Pattern.compile("^Theatre of Blood total completion time:\\s*" + TIME + PB + "\\s*$", FLAGS);
	private static final Pattern TOB_REGULAR_COUNT = Pattern.compile("^Your completed Theatre of Blood count is:\\s*([0-9]{1,7})\\.?$", FLAGS);
	private static final Pattern TOB_HARD_COUNT = Pattern.compile("^Your completed Theatre of Blood: Hard Mode count is:\\s*([0-9]{1,7})\\.?$", FLAGS);
	private static final Pattern TOA_TIME = Pattern.compile("^Tombs of Amascut:\\s*(Expert Mode|Normal Mode) total completion time:\\s*" + TIME + PB + "\\s*$", FLAGS);
	private static final Pattern TOA_EXPERT_COUNT = Pattern.compile("^Your completed Tombs of Amascut: Expert Mode count is:\\s*([0-9]{1,7})\\.?$", FLAGS);
	private static final Pattern COX_RESULT = Pattern.compile("^Team size:\\s*([0-9]{1,2}) players\\s+Duration:\\s*" + TIME + PB + "(?:\\s+Olm duration:\\s*" + TIME + ")?\\s*$", FLAGS);
	private static final Pattern COX_CHALLENGE_COUNT = Pattern.compile("^Your completed Chambers of Xeric Challenge Mode count is:\\s*([0-9]{1,7})\\.?$", FLAGS);

	static final class Message
	{
		final Family family;
		final Kind kind;
		final Mode mode;
		final long durationMillis;
		final int count;
		final int teamSize;

		private Message(Family family, Kind kind, Mode mode, long durationMillis, int count, int teamSize)
		{
			this.family = family;
			this.kind = kind;
			this.mode = mode;
			this.durationMillis = durationMillis;
			this.count = count;
			this.teamSize = teamSize;
		}
	}

	Message parse(String raw)
	{
		if (raw == null || raw.length() > 512) return null;
		String text = Text.removeTags(raw).trim().replaceAll("\\s+", " ");
		Matcher matcher = TOB_ROOM.matcher(text);
		if (matcher.matches()) return timed(Family.TOB, Kind.ROOM_TIME, null, matcher.group(1));
		matcher = TOB_OVERALL.matcher(text);
		if (matcher.matches()) return timed(Family.TOB, Kind.OVERALL_TIME, null, matcher.group(1));
		matcher = TOB_REGULAR_COUNT.matcher(text);
		if (matcher.matches()) return counted(Family.TOB, Mode.REGULAR, matcher.group(1));
		matcher = TOB_HARD_COUNT.matcher(text);
		if (matcher.matches()) return counted(Family.TOB, Mode.HARD, matcher.group(1));
		matcher = TOA_TIME.matcher(text);
		if (matcher.matches())
		{
			return timed(Family.TOA, Kind.OVERALL_TIME,
				"Expert Mode".equalsIgnoreCase(matcher.group(1)) ? Mode.EXPERT : Mode.NORMAL, matcher.group(2));
		}
		matcher = TOA_EXPERT_COUNT.matcher(text);
		if (matcher.matches()) return counted(Family.TOA, Mode.EXPERT, matcher.group(1));
		matcher = COX_RESULT.matcher(text);
		if (matcher.matches())
		{
			Integer size = boundedInt(matcher.group(1), MAX_GROUP_SIZE);
			Long duration = parseDuration(matcher.group(2));
			if (size == null || size < 1 || duration == null) return null;
			return new Message(Family.COX, Kind.COX_RESULT, null, duration, 0, size);
		}
		matcher = COX_CHALLENGE_COUNT.matcher(text);
		if (matcher.matches()) return counted(Family.COX, Mode.CHALLENGE, matcher.group(1));
		return null;
	}

	Family malformedCandidateFamily(String raw)
	{
		if (raw == null || raw.length() > 512) return null;
		String text = Text.removeTags(raw).trim().replaceAll("\\s+", " ").toLowerCase(java.util.Locale.ROOT);
		if (text.startsWith("theatre of blood completion time:")
			|| text.startsWith("theatre of blood total completion time:")
			|| text.startsWith("your completed theatre of blood count is:")
			|| text.startsWith("your completed theatre of blood: hard mode count is:")) return Family.TOB;
		if (text.startsWith("tombs of amascut: expert mode total completion time:")
			|| text.startsWith("tombs of amascut: normal mode total completion time:")
			|| text.startsWith("your completed tombs of amascut: expert mode count is:")) return Family.TOA;
		if (text.startsWith("team size:") || text.startsWith("your completed chambers of xeric challenge mode count is:")) return Family.COX;
		return null;
	}

	static Long parseDuration(String value)
	{
		if (value == null || value.length() > 32) return null;
		String[] parts = value.split(":", -1);
		if (parts.length < 2 || parts.length > 3) return null;
		String secondsPart = parts[parts.length - 1];
		String[] fractional = secondsPart.split("\\.", -1);
		if (fractional.length > 2 || !fractional[0].matches("[0-5][0-9]")) return null;
		String millisText = "000";
		if (fractional.length == 2)
		{
			if (!fractional[1].matches("[0-9]{1,3}")) return null;
			millisText = (fractional[1] + "000").substring(0, 3);
		}
		BigInteger totalSeconds;
		try
		{
			BigInteger first = new BigInteger(parts[0]);
			BigInteger seconds = new BigInteger(fractional[0]);
			if (parts.length == 2)
			{
				if (first.signum() < 0 || first.compareTo(BigInteger.valueOf(1440)) > 0) return null;
				totalSeconds = first.multiply(BigInteger.valueOf(60)).add(seconds);
			}
			else
			{
				BigInteger second = new BigInteger(parts[1]);
				if (second.compareTo(BigInteger.valueOf(59)) > 0) return null;
				totalSeconds = first.multiply(BigInteger.valueOf(3600))
					.add(second.multiply(BigInteger.valueOf(60))).add(seconds);
			}
			BigInteger millis = totalSeconds.multiply(BigInteger.valueOf(1000)).add(new BigInteger(millisText));
			if (millis.signum() <= 0 || millis.compareTo(BigInteger.valueOf(MAX_DURATION_MILLIS)) > 0) return null;
			return millis.longValueExact();
		}
		catch (ArithmeticException | NumberFormatException ignored)
		{
			return null;
		}
	}

	private static Message timed(Family family, Kind kind, Mode mode, String value)
	{
		Long duration = parseDuration(value);
		return duration == null ? null : new Message(family, kind, mode, duration, 0, 0);
	}

	private static Message counted(Family family, Mode mode, String value)
	{
		Integer count = boundedInt(value, MAX_COMPLETION_COUNT);
		return count == null || count < 1 ? null
			: new Message(family, Kind.MODE_COUNT, mode, 0, count, 0);
	}

	private static Integer boundedInt(String value, int max)
	{
		try
		{
			BigInteger number = new BigInteger(value);
			if (number.signum() < 0 || number.compareTo(BigInteger.valueOf(max)) > 0) return null;
			return number.intValueExact();
		}
		catch (ArithmeticException | NumberFormatException ignored) { return null; }
	}
}
