package com.nocturne;

import java.time.Instant;
import java.util.List;
import org.junit.Test;
import static org.junit.Assert.*;

public class PendingActivityTimingTest
{
	private static final Instant BASE = Instant.parse("2026-10-13T00:12:31Z");

	private static ChallengeObservation observation(int cycle)
	{
		BossCompletionContext context = BossCompletionContext.fromActiveSoloActivity(
			"corrupted_gauntlet", "corrupted_gauntlet", "Tester", "link-session", cycle,
			cycle * 1_000_000_000L, BASE.plusSeconds(cycle - 1));
		return ChallengeObservation.createTracked(context, 513_000L);
	}

	private static LootRecord loot(String id, Instant at)
	{
		LootItem reward = LootItem.derived(23962, 1, "Crystal shard", 1000,
			"runelite_derived_full_output", "fixture", 1, 23962, "Crystal shard", 1000);
		return new LootRecord(id, at.toString(), "Tester", "Corrupted Gauntlet", List.of(reward),
			GroupSnapshot.unavailable("fixture"), SubmissionStatus.ACCEPTED,
			PluginActivityCatalog.GAUNTLET);
	}

	@Test public void lateLootBeyondThirtySecondsMergesByUniqueUnclaimedCompletion()
	{
		PendingActivityTiming pending = new PendingActivityTiming();
		ChallengeObservation completion = observation(1);
		pending.update(completion, ActivityTimingStatus.ACCEPTED, 1L);
		LootRecord merged = pending.merge(loot("chest", BASE.plusSeconds(90)));
		assertNotNull(merged);
		assertEquals("chest", merged.id);
		assertEquals(completion.eventId, merged.activityEventId);
		assertEquals(Long.valueOf(513_000L), merged.activityDurationMillis);
		assertEquals(SubmissionStatus.ACCEPTED, merged.submission);
		assertFalse(merged.completionOnly);
		assertEquals(0, pending.size());
	}

	@Test public void overlappingUnclaimedRunsLeaveLootUnmergedRatherThanGuess()
	{
		PendingActivityTiming pending = new PendingActivityTiming();
		ChallengeObservation first = observation(1);
		ChallengeObservation second = observation(2);
		assertNotEquals(first.eventId, second.eventId);
		pending.update(first, ActivityTimingStatus.ACCEPTED, 1L);
		pending.update(second, ActivityTimingStatus.ACCEPTED, 2L);
		assertNull(pending.merge(loot("ambiguous-chest", BASE.plusSeconds(90))));
		assertEquals(2, pending.size());
	}

	@Test public void aNewEncounterBlocksLateLootFromBeingGuessedOntoAnotherRun()
	{
		PendingActivityTiming pending = new PendingActivityTiming();
		BossCompletionContext firstContext = BossCompletionContext.fromActiveSoloActivity(
			"corrupted_gauntlet", "corrupted_gauntlet", "Tester", "session-boundary", 1,
			1L, BASE);
		ChallengeObservation first = ChallengeObservation.createTracked(firstContext, 513_000L);
		pending.activityStarted(firstContext);
		pending.update(first, ActivityTimingStatus.ACCEPTED, 1L);
		pending.activityStarted(firstContext); // a replay is not a new run
		assertNotNull(pending.merge(loot("first-chest", BASE.plusSeconds(45))));

		PendingActivityTiming overlapping = new PendingActivityTiming();
		BossCompletionContext previous = BossCompletionContext.fromActiveSoloActivity(
			"corrupted_gauntlet", "corrupted_gauntlet", "Tester", "session-boundary", 2,
			2L, BASE);
		BossCompletionContext current = BossCompletionContext.fromActiveSoloActivity(
			"corrupted_gauntlet", "corrupted_gauntlet", "Tester", "session-boundary", 3,
			3L, BASE.plusSeconds(60));
		ChallengeObservation previousObservation = ChallengeObservation.createTracked(previous, 513_000L);
		overlapping.activityStarted(previous);
		overlapping.update(previousObservation, ActivityTimingStatus.ACCEPTED, 1L);
		overlapping.activityStarted(current);
		ChallengeObservation currentObservation = ChallengeObservation.createTracked(current, 520_000L);
		overlapping.update(currentObservation, ActivityTimingStatus.ACCEPTED, 2L);
		assertNull(overlapping.merge(loot("old-or-new-chest", BASE.plusSeconds(90))));
		assertEquals(2, overlapping.size());
	}

	@Test public void completionCardRemainsStoredWhenNoLootArrives() throws Exception
	{
		org.junit.rules.TemporaryFolder folder = new org.junit.rules.TemporaryFolder();
		folder.create();
		LootHistoryStore store = new LootHistoryStore(folder.getRoot().toPath(), new com.google.gson.Gson());
		LootRecord completion = LootRecord.completion(observation(1), ActivityTimingStatus.SENDING);
		store.append(completion);
		LootHistoryStore.Page page = new LootHistoryStore(folder.getRoot().toPath(),
			new com.google.gson.Gson()).load("Tester", 0, 5);
		assertEquals(1, page.records.size());
		assertTrue(page.records.get(0).completionOnly);
		assertEquals(Long.valueOf(513_000L), page.records.get(0).activityDurationMillis);
	}
}
