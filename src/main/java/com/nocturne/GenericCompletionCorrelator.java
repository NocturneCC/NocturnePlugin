package com.nocturne;

import java.time.Instant;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/** Correlates generic duration tokens to encounter identities from existing loot/raid tracking. */
final class GenericCompletionCorrelator
{
	static final long BEFORE_WINDOW_NANOS = 8_000_000_000L;
	static final long AFTER_WINDOW_NANOS = 8_000_000_000L;
	static final long QUIET_WINDOW_NANOS = 1_500_000_000L;
	static final long RESOLVED_CONTEXT_TTL_NANOS = 30_000_000_000L;
	static final int MAX_CONTEXTS = 8;
	static final int MAX_RESOLVED_CONTEXTS = 32;
	private final CompletionDurationParser parser = new CompletionDurationParser();
	private final CompletionMessageBuffer buffer = new CompletionMessageBuffer();
	private final LinkedHashMap<String, Pending> pending = new LinkedHashMap<>();
	private final LinkedHashMap<String, Long> resolved = new LinkedHashMap<>();
	private final java.util.Map<Long, String> messageOwners = new LinkedHashMap<>();
	private final Set<Long> consumedMessages = new LinkedHashSet<>();
	private String lastDiagnosticReason;

	synchronized ChallengeObservation onMessage(String category, String text, long gameCycle,
		long nowNanos, Instant now)
	{
		pruneResolved(nowNanos);
		CompletionMessageBuffer.Entry entry = buffer.offer(category, text, gameCycle, nowNanos, parser);
		if (entry != null) assign(entry);
		return resolve(nowNanos, now);
	}

	synchronized ChallengeObservation onCompletion(BossCompletionContext context, long nowNanos, Instant now)
	{
		pruneResolved(nowNanos);
		if (context == null || resolved.containsKey(context.encounterIdentity)
			|| pending.containsKey(context.encounterIdentity)) return null;
		pending.put(context.encounterIdentity, new Pending(context));
		while (pending.size() > MAX_CONTEXTS)
		{
			String first = pending.keySet().iterator().next();
			remove(first);
			lastDiagnosticReason = "context_capacity";
		}
		for (CompletionMessageBuffer.Entry entry : buffer.snapshot(nowNanos)) assign(entry);
		return resolve(nowNanos, now);
	}

	synchronized ChallengeObservation poll(long nowNanos, Instant now)
	{
		return resolve(nowNanos, now);
	}

	synchronized void clearPartial()
	{
		pending.clear(); messageOwners.clear(); buffer.clear();
	}

	synchronized void reset() { clearPartial(); }
	synchronized void clear() { clearPartial(); resolved.clear(); lastDiagnosticReason = null; }
	synchronized int bufferedMessages() { return buffer.size(); }
	synchronized int pendingContexts() { return pending.size(); }
	synchronized String lastDiagnosticReason() { return lastDiagnosticReason; }

	private void assign(CompletionMessageBuffer.Entry entry)
	{
		if (messageOwners.containsKey(entry.sequence) || consumedMessages.contains(entry.sequence)) return;
		Pending best = null;
		long bestDistance = Long.MAX_VALUE;
		boolean tie = false;
		for (Pending candidate : pending.values())
		{
			long delta = entry.arrivalNanos - candidate.context.completionNanos;
			if (delta < -BEFORE_WINDOW_NANOS || delta > AFTER_WINDOW_NANOS) continue;
			long distance = Math.abs(delta);
			if (distance < bestDistance) { best = candidate; bestDistance = distance; tie = false; }
			else if (distance == bestDistance) tie = true;
		}
		if (best != null && !tie)
		{
			best.entries.add(entry);
			messageOwners.put(entry.sequence, best.context.encounterIdentity);
		}
		else if (tie)
		{
			consumedMessages.add(entry.sequence);
			while (consumedMessages.size() > 512) consumedMessages.remove(consumedMessages.iterator().next());
			lastDiagnosticReason = "completion_overlap";
		}
	}

	private ChallengeObservation resolve(long nowNanos, Instant now)
	{
		for (Pending state : new ArrayList<>(pending.values()))
		{
			List<CompletionDurationParser.Token> nearestCandidates = new ArrayList<>();
			Set<String> uniqueTokens = new LinkedHashSet<>();
			long nearestDistance = Long.MAX_VALUE;
			long latestCandidate = state.context.completionNanos;
			for (CompletionMessageBuffer.Entry entry : state.entries)
			{
				long delta = entry.arrivalNanos - state.context.completionNanos;
				if (delta < -BEFORE_WINDOW_NANOS || delta > AFTER_WINDOW_NANOS) continue;
				for (CompletionDurationParser.Token token : parser.parse(entry.normalizedText))
					if (!token.referenceOnly)
					{
						String tokenKey = entry.normalizedText + '|' + token.start + '|' + token.end;
						long distance = Math.abs(delta);
						if (distance < nearestDistance)
						{
							nearestDistance = distance;
							nearestCandidates.clear();
							uniqueTokens.clear();
						}
						if (distance == nearestDistance && uniqueTokens.add(tokenKey)) nearestCandidates.add(token);
						latestCandidate = Math.max(latestCandidate, entry.arrivalNanos);
					}
			}
			boolean quiet = nowNanos - latestCandidate >= QUIET_WINDOW_NANOS;
			boolean expired = nowNanos - state.context.completionNanos > AFTER_WINDOW_NANOS;
			if (nearestCandidates.isEmpty())
			{
				if (expired)
				{
					lastDiagnosticReason = "duration_missing";
					remove(state.context.encounterIdentity);
				}
				continue;
			}
			if (!quiet && !expired) continue;
			if (nearestCandidates.size() != 1)
			{
				lastDiagnosticReason = "duration_ambiguous";
				if (expired || quiet) remove(state.context.encounterIdentity);
				continue;
			}
			CompletionDurationParser.Token selected = nearestCandidates.get(0);
			ChallengeObservation observation;
			if ("overall".equals(state.context.timingScope) && state.context.bossKey == null)
				observation = ChallengeObservation.createTracked(state.context, selected.durationMillis);
			else if ("overall".equals(state.context.timingScope))
				observation = ChallengeObservation.create(state.context.activityKey, state.context.modeKey,
					null, selected.durationMillis, null, state.context.roster, state.context.reporterRsn,
					state.context.occurredAt == null ? now : state.context.occurredAt,
					state.context.encounterIdentity);
			else
			{
				lastDiagnosticReason = "unsupported_timing_scope";
				remove(state.context.encounterIdentity);
				continue;
			}
			remove(state.context.encounterIdentity);
			if (observation == null)
			{
				lastDiagnosticReason = "observation_invalid";
				continue;
			}
			resolved.put(state.context.encounterIdentity, nowNanos);
			while (resolved.size() > MAX_RESOLVED_CONTEXTS) resolved.remove(resolved.keySet().iterator().next());
			lastDiagnosticReason = null;
			return observation;
		}
		prune(nowNanos);
		return null;
	}

	private void remove(String identity)
	{
		Pending removed = pending.remove(identity);
		if (removed != null)
		{
			for (CompletionMessageBuffer.Entry entry : removed.entries)
			{
				messageOwners.remove(entry.sequence);
				consumedMessages.add(entry.sequence);
			}
			while (consumedMessages.size() > 512) consumedMessages.remove(consumedMessages.iterator().next());
		}
	}

	private void prune(long nowNanos)
	{
		for (Pending state : new ArrayList<>(pending.values()))
			if (nowNanos - state.context.completionNanos > AFTER_WINDOW_NANOS) remove(state.context.encounterIdentity);
	}

	private void pruneResolved(long nowNanos)
	{
		resolved.entrySet().removeIf(entry -> nowNanos - entry.getValue() > RESOLVED_CONTEXT_TTL_NANOS);
	}

	private static final class Pending
	{
		final BossCompletionContext context;
		final List<CompletionMessageBuffer.Entry> entries = new ArrayList<>();
		Pending(BossCompletionContext context) { this.context = context; }
	}
}
