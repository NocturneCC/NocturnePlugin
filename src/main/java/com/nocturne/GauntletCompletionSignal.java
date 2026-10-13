package com.nocturne;

import java.util.regex.Pattern;

/** Recognizes the Gauntlet completion event only when an active game context is supplied. */
final class GauntletCompletionSignal
{
	private static final Pattern COMPLETION_PREFIX = Pattern.compile(
		"(?i)^corrupted\\s+challenge\\s+duration\\s*:");
	private static final CompletionDurationParser NORMALIZER = new CompletionDurationParser();

	private GauntletCompletionSignal() { }

	static boolean isCompletion(String category, String raw, boolean activeGauntlet)
	{
		if (!activeGauntlet || !CompletionMessageBuffer.eligibleCategory(category)) return false;
		String normalized = NORMALIZER.normalize(raw);
		return normalized != null && COMPLETION_PREFIX.matcher(normalized).find();
	}
}
