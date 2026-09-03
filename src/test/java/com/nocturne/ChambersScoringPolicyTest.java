package com.nocturne;

import java.util.List;
import org.junit.Test;
import static org.junit.Assert.*;

public class ChambersScoringPolicyTest
{
	@Test public void exactlyFivePercentPassesAndJustBelowFailsWithIntegerArithmetic()
	{
		assertTrue(policy(2_000_000, 100_000, 3, 3).eligible);
		assertFalse(policy(2_000_000, 99_999, 3, 3).eligible);
	}

	@Test public void negativeZeroAndMissingFinalPointsFailClosed()
	{
		RaidSession negative = session(true);
		negative.observe(List.of("Local", "One", "Two"), 3, 1);
		negative.finishChambers(10_000, -1);
		assertEquals(0, negative.finalPersonalPoints());
		assertFalse(ChambersScoringPolicy.evaluate(negative).eligible);
		assertFalse(policy(10_000, 0, 3, 3).eligible);
		assertFalse(policy(0, 1_000, 3, 3).eligible);
		assertFalse(policy(-1, 1_000, 3, 3).eligible);
		RaidSession unfinished = session(true);
		unfinished.observe(List.of("Local", "One", "Two"), 3, 1);
		assertFalse(ChambersScoringPolicy.evaluate(unfinished).eligible);
	}

	@Test public void departedScalerIsExcludedButMaximumScaleClassifiesNormalGroup()
	{
		RaidSession session = session(true);
		session.observe(List.of("Local", "Scaler", "Stayer"), 3, 1);
		session.observe(List.of("Local", "Stayer"), 2, 2);
		session.finishChambers(10_000, 1_000);
		assertEquals(List.of("Local", "Stayer"), session.completionSnapshot().names);
		assertEquals(3, session.maxReportedSize());
		assertEquals(ChambersScoringPolicy.Mode.NORMAL_GROUP,
			ChambersScoringPolicy.evaluate(session).mode);
		assertTrue(session.snapshot().detail.contains("departed_before_completion=1"));
	}

	@Test public void scalerRemainingIsInCompletionRoster()
	{
		RaidSession session = session(true);
		session.observe(List.of("Local", "Scaler", "Stayer"), 3, 1);
		session.finishChambers(10_000, 1_000);
		assertEquals(List.of("Local", "Scaler", "Stayer"), session.completionSnapshot().names);
		assertFalse(session.snapshot().detail.contains("departed_before_completion"));
	}

	@Test public void massUsesMaximumScaleAndPositivePersonalPointsWithoutFivePercent()
	{
		RaidSession session = session(true);
		session.observe(names(21), 21, 1);
		session.observe(List.of("Local", "One"), 2, 2);
		session.finishChambers(1_000_000, 1);
		ChambersScoringPolicy policy = ChambersScoringPolicy.evaluate(session);
		assertEquals(ChambersScoringPolicy.Mode.MASS_PERSONAL_ONLY, policy.mode);
		assertTrue(policy.eligible);
		assertTrue(policy.explanation.contains("standard personal 1x"));
	}

	@Test public void soloIsPersonalOnlyAndNormalAndChallengeShareThePolicy()
	{
		assertEquals(ChambersScoringPolicy.Mode.SOLO_PERSONAL_ONLY, policy(10_000, 10_000, 1, 1).mode);
		assertEquals(RaidType.COX, RaidType.fromSource("Chambers of Xeric"));
		assertEquals(RaidType.COX, RaidType.fromSource("Chambers of Xeric Challenge Mode"));
	}

	@Test public void reconnectAndIncompleteCompletionRosterFailClosed()
	{
		RaidSession incomplete = session(true);
		incomplete.observe(List.of("Local", "One"), 3, 1);
		incomplete.finishChambers(10_000, 1_000);
		assertFalse(ChambersScoringPolicy.evaluate(incomplete).eligible);
		RaidSession reconnect = session(false);
		reconnect.observe(List.of("Local", "One", "Two"), 3, 1);
		reconnect.finishChambers(10_000, 1_000);
		assertFalse(ChambersScoringPolicy.evaluate(reconnect).eligible);
	}

	@Test public void emptyCompletionReadRetainsLastCurrentRoster()
	{
		RaidSession session = session(true);
		session.observe(List.of("Local", "One", "Two"), 3, 1);
		session.observe(List.of(), 0, 2);
		session.finishChambers(10_000, 1_000);
		assertEquals(List.of("Local", "One", "Two"), session.completionSnapshot().names);
	}

	@Test public void rewardBeforeOfficialCompletionAndStaleSessionFailClosed()
	{
		RaidSession session = session(true);
		session.observe(List.of("Local", "One"), 2, 1);
		assertFalse(session.chambersRewardSnapshot().allowsSubmission());
		assertFalse(GroupTracker.isCurrentChambersReward(session, RaidType.COX, 1, 7,
			1 + RaidSession.RETENTION_TICKS + 1));
	}

	private static ChambersScoringPolicy policy(int team, int personal, int maximum, int completion)
	{
		RaidSession session = session(true);
		session.observe(names(maximum), maximum, 1);
		session.observe(names(completion), completion, 2);
		session.finishChambers(team, personal);
		return ChambersScoringPolicy.evaluate(session);
	}

	private static RaidSession session(boolean entryObserved)
	{
		return new RaidSession(RaidType.COX, "Local", entryObserved, 0, 1, 7);
	}

	private static List<String> names(int size)
	{
		java.util.ArrayList<String> names = new java.util.ArrayList<>();
		names.add("Local");
		for (int i = 1; i < size; i++) names.add("P" + i);
		return names;
	}
}
