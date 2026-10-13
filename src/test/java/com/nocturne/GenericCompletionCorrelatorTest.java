package com.nocturne;

import java.time.Instant;
import java.util.List;
import org.junit.Test;
import static org.junit.Assert.*;

public class GenericCompletionCorrelatorTest
{
	private static final Instant NOW = Instant.parse("2026-10-12T12:00:00Z");
	private static BossCompletionContext context(long cycle, long nanos)
	{
		return BossCompletionContext.fromNpcLoot("Phosani's Nightmare", "Local Name", cycle, nanos, NOW);
	}

	@Test public void timingChatBeforeKillCorrelatesToLootEncounterAndSendsReporterOnly()
	{
		GenericCompletionCorrelator correlator = new GenericCompletionCorrelator();
		assertNull(correlator.onMessage("GAMEMESSAGE", "Fight duration: 5:58.20. Personal best: 4:48.60", 10, 900_000_000L, NOW));
		BossCompletionContext completion = context(11, 1_000_000_000L);
		assertEquals("phosanis", completion.bossKey);
		assertEquals("phosanis_nightmare", completion.activityKey);
		assertNull(correlator.onCompletion(completion, 1_000_000_000L, NOW));
		ChallengeObservation observation = correlator.poll(2_600_000_001L, NOW);
		assertNotNull(observation);
		assertEquals("phosani_nightmare", observation.modeKey);
		assertEquals(358_200L, observation.overallTimeMillis.longValue());
		assertEquals(List.of("Local Name"), observation.roster);
		assertEquals(1, observation.groupSize);
	}

	@Test public void timingChatAfterKillAndDuplicateDeliveryProduceOneDeterministicEvent()
	{
		GenericCompletionCorrelator first = new GenericCompletionCorrelator();
		BossCompletionContext completion = context(20, 1_000_000_000L);
		first.onCompletion(completion, 1_000_000_000L, NOW);
		first.onMessage("GAMEMESSAGE", "<col=fff>Team size: Solo Fight duration: 5:58.20. Personal best: 4:48.60</col>", 21, 1_100_000_000L, NOW);
		first.onMessage("GAMEMESSAGE", "<col=fff>Team size: Solo Fight duration: 5:58.20. Personal best: 4:48.60</col>", 22, 2_200_000_000L, NOW);
		ChallengeObservation result = first.poll(3_800_000_001L, NOW);
		assertNotNull(result);
		assertEquals(358_200L, result.overallTimeMillis.longValue());
		assertNull(first.onCompletion(completion, 2_800_000_000L, NOW));
		ChallengeObservation replayed = ChallengeObservation.createPhosani(358_200L,
			completion.roster, completion.reporterRsn, NOW, completion.encounterIdentity);
		assertEquals(result.eventId, replayed.eventId);
	}

	@Test public void staleMissingAndUnrelatedMessagesNeverCreateObservation()
	{
		GenericCompletionCorrelator stale = new GenericCompletionCorrelator();
		stale.onMessage("GAMEMESSAGE", "Fight duration: 5:58.20", 1, 100_000_000L, NOW);
		BossCompletionContext late = context(2, 9_000_000_000L);
		stale.onCompletion(late, 9_000_000_000L, NOW);
		assertNull(stale.poll(10_600_000_001L, NOW));
		GenericCompletionCorrelator lateAfter = new GenericCompletionCorrelator();
		lateAfter.onCompletion(context(22, 1_000_000_000L), 1_000_000_000L, NOW);
		lateAfter.onMessage("GAMEMESSAGE", "Fight duration: 5:58.20", 23, 9_500_000_000L, NOW);
		assertNull(lateAfter.poll(10_600_000_001L, NOW));

		GenericCompletionCorrelator missing = new GenericCompletionCorrelator();
		missing.onCompletion(context(3, 1_000_000_000L), 1_000_000_000L, NOW);
		missing.onMessage("CLAN_CHAT", "Fight duration: 5:58.20", 4, 1_100_000_000L, NOW);
		assertNull(missing.poll(10_000_000_001L, NOW));
	}

	@Test public void ambiguousDurationsFailClosedAndConsecutiveEncountersStayDistinct()
	{
		GenericCompletionCorrelator ambiguous = new GenericCompletionCorrelator();
		ambiguous.onCompletion(context(5, 1_000_000_000L), 1_000_000_000L, NOW);
		ambiguous.onMessage("GAMEMESSAGE", "Fight duration: 5:58.20. Overall duration: 2:11.00", 6, 1_100_000_000L, NOW);
		assertNull(ambiguous.poll(2_700_000_001L, NOW));
		assertEquals("duration_ambiguous", ambiguous.lastDiagnosticReason());

		GenericCompletionCorrelator sequential = new GenericCompletionCorrelator();
		BossCompletionContext one = context(10, 1_000_000_000L);
		BossCompletionContext two = context(20, 15_000_000_000L);
		sequential.onCompletion(one, one.completionNanos, NOW);
		sequential.onMessage("GAMEMESSAGE", "Fight duration: 5:58.20", 11, 1_100_000_000L, NOW);
		ChallengeObservation first = sequential.poll(2_700_000_001L, NOW);
		sequential.onCompletion(two, two.completionNanos, NOW);
		sequential.onMessage("GAMEMESSAGE", "Fight duration: 6:01.00", 21, 15_100_000_000L, NOW);
		ChallengeObservation second = sequential.poll(16_700_000_001L, NOW);
		assertNotNull(first); assertNotNull(second);
		assertNotEquals(first.eventId, second.eventId);
	}

	@Test public void closestEligibleMessageWinsOverASecondDistantTimeCandidate()
	{
		GenericCompletionCorrelator correlator = new GenericCompletionCorrelator();
		correlator.onMessage("GAMEMESSAGE", "Previous fight: 5:00.00", 1, 500_000_000L, NOW);
		correlator.onCompletion(context(2, 1_000_000_000L), 1_000_000_000L, NOW);
		correlator.onMessage("GAMEMESSAGE", "Fight duration: 5:58.20. Personal best: 4:48.60", 3, 1_100_000_000L, NOW);
		ChallengeObservation result = correlator.poll(2_700_000_001L, NOW);
		assertNotNull(result);
		assertEquals(358_200L, result.overallTimeMillis.longValue());
	}

	@Test public void delayedPostKillTimingMessageUsesStillLiveContext()
	{
		GenericCompletionCorrelator correlator = new GenericCompletionCorrelator();
		correlator.onCompletion(context(50, 1_000_000_000L), 1_000_000_000L, NOW);
		assertNull(correlator.poll(3_000_000_000L, NOW));
		correlator.onMessage("GAMEMESSAGE", "Fight duration: 5:58.20", 54, 4_000_000_000L, NOW);
		ChallengeObservation observation = correlator.poll(5_600_000_001L, NOW);
		assertNotNull(observation);
		assertEquals(358_200L, observation.overallTimeMillis.longValue());
	}

	@Test public void trackedNightmareGroupUsesGenericOverallDurationWithoutChallengeProjection()
	{
		GroupSnapshot group = new GroupSnapshot("fixture group", List.of("Local Name", "Party Member"), 2,
			GroupSnapshot.Status.MATCHED, "stable fixture roster");
		BossCompletionContext completion = BossCompletionContext.fromTrackedActivity("The Nightmare",
			"Local Name", group, "session", 40, 1_000_000_000L, NOW);
		assertNotNull(completion);
		assertNull(completion.bossKey);
		GenericCompletionCorrelator correlator = new GenericCompletionCorrelator();
		assertNull(correlator.onCompletion(completion, completion.completionNanos, NOW));
		correlator.onMessage("GAMEMESSAGE", "The Nightmare duration: 6:12.34. Personal best: 5:50.00",
			41, 1_100_000_000L, NOW);
		ChallengeObservation observation = correlator.poll(2_700_000_001L, NOW);
		assertNotNull(observation);
		assertEquals("nightmare", observation.activityKey);
		assertEquals(372_340L, observation.overallTimeMillis.longValue());
		assertEquals(2, observation.groupSize);
		assertFalse(observation.challengeProjection);
	}

	@Test public void corruptedGauntletRewardLootUsesGenericCompletionTime()
	{
		BossCompletionContext completion = BossCompletionContext.fromNpcLoot("The Corrupted Gauntlet",
			"Local Name", "session", 48, 1_000_000_000L, NOW);
		assertNotNull(completion);
		assertEquals("corrupted_gauntlet", completion.activityKey);
		GenericCompletionCorrelator correlator = new GenericCompletionCorrelator();
		correlator.onCompletion(completion, completion.completionNanos, NOW);
		correlator.onMessage("GAMEMESSAGE", "Completion time: 5:41.27. Personal best: 5:12.00",
			49, 1_100_000_000L, NOW);
		ChallengeObservation observation = correlator.poll(2_700_000_001L, NOW);
		assertNotNull(observation);
		assertEquals(341_270L, observation.overallTimeMillis.longValue());
		assertEquals("corrupted_gauntlet", observation.activityKey);
	}

	@Test public void corruptedGauntletCompletionContextSelectsConfiguredOverallMetricWithoutLoot()
	{
		GenericCompletionCorrelator correlator = new GenericCompletionCorrelator();
		long completionNanos = 1_000_000_000L;
		// The overall completion message itself arrives before any reward chest event.
		correlator.onMessage("GAMEMESSAGE",
			"Corrupted challenge duration: 9:12. Personal best: 7:15.", 100,
			completionNanos, NOW);
		BossCompletionContext context = BossCompletionContext.fromActiveSoloActivity(
			"corrupted_gauntlet", "corrupted_gauntlet", "Local Name", "session", 100,
			completionNanos + 1, NOW);
		assertNotNull(context);
		assertEquals("gauntlet", context.bossKey);
		assertEquals("corrupted_gauntlet", context.activityKey);
		assertEquals("overall", context.timingScope);
		assertNull(correlator.onCompletion(context, context.completionNanos, NOW));

		// Other activity times may follow, but their arrival order cannot replace overall time.
		correlator.onMessage("GAMEMESSAGE",
			"Preparation time: 5:46. Hunllef kill time: 3:26.", 101,
			completionNanos + 200_000_000L, NOW);
		ChallengeObservation result = correlator.poll(completionNanos + 1_800_000_001L, NOW);
		assertNotNull(result);
		assertEquals(552_000L, result.overallTimeMillis.longValue());
		assertEquals("corrupted_gauntlet", result.activityKey);
		assertEquals(List.of("Local Name"), result.roster);
	}

	@Test public void gauntletWithoutConfiguredOverallLabelFailsClosedRatherThanChoosingLatestTime()
	{
		GenericCompletionCorrelator correlator = new GenericCompletionCorrelator();
		BossCompletionContext context = BossCompletionContext.fromActiveSoloActivity(
			"corrupted_gauntlet", "corrupted_gauntlet", "Local Name", "session", 110,
			1_000_000_000L, NOW);
		correlator.onCompletion(context, context.completionNanos, NOW);
		correlator.onMessage("GAMEMESSAGE", "Preparation time: 5:46", 111, 1_100_000_000L, NOW);
		correlator.onMessage("GAMEMESSAGE", "Hunllef kill time: 3:26", 112, 1_200_000_000L, NOW);
		assertNull(correlator.poll(10_000_000_001L, NOW));
		assertEquals("configured_metric_missing", correlator.lastDiagnosticReason());
	}

	@Test public void onlyCanonicalPhosaniNpcSourceCreatesBossIdentity()
	{
		assertNotNull(context(1, 1L));
		assertNull(BossCompletionContext.fromNpcLoot("Nightmare", "Local Name", 1, 1L, NOW));
		assertNull(BossCompletionContext.fromNpcLoot("Phosani's Nightmare pet", "Local Name", 1, 1L, NOW));
	}

	@Test public void eventIdsAreStableWithinEncounterAndScopedAcrossReconnectSessions()
	{
		GenericCompletionCorrelator correlator = new GenericCompletionCorrelator();
		BossCompletionContext first = BossCompletionContext.fromNpcLoot(
			"Phosani's Nightmare", "Local Name", "session-a", 30, 1_000_000_000L, NOW);
		correlator.onCompletion(first, first.completionNanos, NOW);
		correlator.onMessage("GAMEMESSAGE", "Fight duration: 5:58.20", 31, 1_100_000_000L, NOW);
		ChallengeObservation accepted = correlator.poll(2_700_000_001L, NOW);
		assertNotNull(accepted);

		BossCompletionContext reconnectReplay = BossCompletionContext.fromNpcLoot(
			"Phosani's Nightmare", "Local Name", "session-b", 40, 5_000_000_000L, NOW);
		correlator.onCompletion(reconnectReplay, reconnectReplay.completionNanos, NOW);
		correlator.onMessage("GAMEMESSAGE", "Fight duration: 5:58.20", 41, 5_100_000_000L, NOW);
		ChallengeObservation replay = correlator.poll(6_700_000_001L, NOW);
		assertNotNull(replay);
		assertNotEquals(accepted.eventId, replay.eventId);
		assertEquals(accepted.fingerprint(), replay.fingerprint());
	}

	@Test public void identicalPnmTimesInDistinctEncounterContextsRemainDistinct()
	{
		GenericCompletionCorrelator correlator = new GenericCompletionCorrelator();
		BossCompletionContext firstContext = BossCompletionContext.fromNpcLoot(
			"Phosani's Nightmare", "Local Name", "same-session", 101, 1_000_000_000L, NOW);
		BossCompletionContext secondContext = BossCompletionContext.fromNpcLoot(
			"Phosani's Nightmare", "Local Name", "same-session", 202, 5_000_000_000L, NOW);
		ChallengeObservation first = complete(correlator, firstContext, "5:58.20");
		ChallengeObservation second = complete(correlator, secondContext, "5:58.20");

		assertEquals(first.fingerprint(), second.fingerprint());
		assertEquals(358_200L, first.overallTimeMillis.longValue());
		assertEquals(358_200L, second.overallTimeMillis.longValue());
		assertNotEquals(first.eventId, second.eventId);
	}

	@Test public void staleReplayedChatWithoutANewEncounterContextCannotCreateObservation()
	{
		GenericCompletionCorrelator correlator = new GenericCompletionCorrelator();
		correlator.onMessage("GAMEMESSAGE", "Fight duration: 5:58.20", 1, 1_000_000_000L, NOW);
		assertNull(correlator.poll(2_600_000_001L, NOW));
		assertNull(correlator.onMessage("GAMEMESSAGE", "Fight duration: 5:58.20", 2,
			3_000_000_000L, NOW));
		assertEquals(0, correlator.pendingContexts());
	}

	private static ChallengeObservation complete(GenericCompletionCorrelator correlator,
		BossCompletionContext context, String duration)
	{
		correlator.onCompletion(context, context.completionNanos, NOW);
		correlator.onMessage("GAMEMESSAGE", "Fight duration: " + duration, context.gameCycle + 1,
			context.completionNanos + 100_000_000L, NOW);
		ChallengeObservation result = correlator.poll(context.completionNanos + 1_700_000_001L, NOW);
		assertNotNull(result);
		return result;
	}
}
