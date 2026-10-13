package com.nocturne;

import java.math.BigInteger;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import net.runelite.client.util.Text;

/** Generic token parser for OSRS-style elapsed durations; it never identifies the activity. */
final class CompletionDurationParser
{
	static final long MAX_DURATION_MILLIS = 86_400_000L;
	private static final Pattern TOKEN = Pattern.compile(
		"(?<![0-9:])((?:[0-9]{1,4}\\s*:\\s*[0-5][0-9]|[0-9]{1,2}\\s*:\\s*[0-5][0-9]\\s*:\\s*[0-5][0-9])(?:\\s*\\.[0-9]{1,3})?)(?![0-9:])");
	private static final Pattern REFERENCE = Pattern.compile(
		"(?i)(?:\\bpersonal\\s+best\\b|\\bprevious\\s+best\\b|\\bprior\\s+best\\b|\\bold\\s+best\\b|\\brecord\\b|\\bpb\\b|\\breference\\b)");
	private static final Pattern OVERALL_LABEL = Pattern.compile(
		"(?i)(?:\\bchallenge\\s+duration\\b|\\b(?:overall|total)(?:\\s+completion)?\\s+(?:time|duration)\\b|\\bcompletion\\s+(?:time|duration)\\b|\\bfight\\s+duration\\b|\\braid\\s+duration\\b)");
	private static final Pattern SEGMENT_LABEL = Pattern.compile(
		"(?i)\\b(?:room|segment)\\s+(?:time|duration)\\b");

	enum Metric { OVERALL, SEGMENT, UNSPECIFIED }

	static final class Token
	{
		final long durationMillis;
		final int start;
		final int end;
		final boolean referenceOnly;
		final Metric metric;

		Token(long durationMillis, int start, int end, boolean referenceOnly, Metric metric)
		{
			this.durationMillis = durationMillis;
			this.start = start;
			this.end = end;
			this.referenceOnly = referenceOnly;
			this.metric = metric;
		}
	}

	String normalize(String raw)
	{
		if (raw == null || raw.length() > 1024) return null;
		String text = Text.removeTags(raw).replace('\u2018', '\'').replace('\u2019', '\'')
			.replace('\u00a0', ' ').replaceAll("\\s+", " ").trim();
		return text.isEmpty() ? null : text;
	}

	List<Token> parse(String raw)
	{
		String text = normalize(raw);
		if (text == null) return List.of();
		Matcher matcher = TOKEN.matcher(text);
		List<Token> result = new ArrayList<>();
		int previousEnd = 0;
		while (matcher.find())
		{
			String value = matcher.group(1).replaceAll("\\s*:\\s*", ":").replaceAll("\\s*\\.\\s*", ".");
			Long duration = parseDuration(value);
			if (duration != null)
			{
				int prefixStart = previousEnd;
				if (prefixStart == 0)
				{
					prefixStart = Math.max(0, matcher.start() - 96);
					for (int i = prefixStart; i < matcher.start(); i++)
						if (".!?;|".indexOf(text.charAt(i)) >= 0) prefixStart = i + 1;
				}
				String prefix = text.substring(prefixStart, matcher.start()).toLowerCase(Locale.ROOT);
				boolean reference = REFERENCE.matcher(prefix).find();
				Metric metric = OVERALL_LABEL.matcher(prefix).find() ? Metric.OVERALL
					: SEGMENT_LABEL.matcher(prefix).find() ? Metric.SEGMENT : Metric.UNSPECIFIED;
				result.add(new Token(duration, matcher.start(), matcher.end(), reference, metric));
			}
			previousEnd = matcher.end();
		}
		return result;
	}

	static Long parseDuration(String value)
	{
		if (value == null || value.length() > 32) return null;
		value = value.replaceAll("\\s*:\\s*", ":").replaceAll("\\s*\\.\\s*", ".").trim();
		String[] parts = value.split(":", -1);
		if (parts.length != 2 && parts.length != 3) return null;
		String secondsText = parts[parts.length - 1];
		String[] fraction = secondsText.split("\\.", -1);
		if (fraction.length > 2 || !fraction[0].matches("[0-5][0-9]")) return null;
		String millis = "000";
		if (fraction.length == 2)
		{
			if (!fraction[1].matches("[0-9]{1,3}")) return null;
			millis = (fraction[1] + "000").substring(0, 3);
		}
		try
		{
			BigInteger first = new BigInteger(parts[0]);
			BigInteger seconds = new BigInteger(fraction[0]);
			BigInteger totalSeconds;
			if (parts.length == 2)
			{
				if (first.signum() < 0 || first.compareTo(BigInteger.valueOf(1440)) > 0) return null;
				totalSeconds = first.multiply(BigInteger.valueOf(60)).add(seconds);
			}
			else
			{
				BigInteger minutes = new BigInteger(parts[1]);
				if (minutes.compareTo(BigInteger.valueOf(59)) > 0) return null;
				totalSeconds = first.multiply(BigInteger.valueOf(3600))
					.add(minutes.multiply(BigInteger.valueOf(60))).add(seconds);
			}
			BigInteger totalMillis = totalSeconds.multiply(BigInteger.valueOf(1000)).add(new BigInteger(millis));
			if (totalMillis.signum() <= 0 || totalMillis.compareTo(BigInteger.valueOf(MAX_DURATION_MILLIS)) > 0) return null;
			return totalMillis.longValueExact();
		}
		catch (ArithmeticException | NumberFormatException ignored) { return null; }
	}
}
