package com.nocturne;

import java.time.Instant;
import java.util.List;
import java.util.concurrent.atomic.AtomicLong;
import org.junit.Test;
import static org.junit.Assert.*;

public class ChallengeCompletionCorrelatorTest
{
	private static final Instant NOW = Instant.parse("2026-10-03T12:34:56Z");
	private static GroupSnapshot roster(String... names) { return new GroupSnapshot("fixture", List.of(names), names.length, GroupSnapshot.Status.MATCHED, "fixture"); }
	private static RaidCompletionEvidence evidence(RaidType type, long epoch, boolean cm, boolean done, String... names)
	{ return new RaidCompletionEvidence(type, epoch, cm, done, roster(names)); }
	private static ChallengeCompletionCorrelator correlator(AtomicLong clock)
	{ return new ChallengeCompletionCorrelator(new ChallengeCompletionParser(), clock::get); }

	@Test public void regularTobCarriesBothAndDeduplicates()
	{
		ChallengeCompletionCorrelator c = new ChallengeCompletionCorrelator();
		RaidCompletionEvidence e = evidence(RaidType.TOB, 1, false, true, "Local Name", "Partner Name");
		assertNull(c.accept("Theatre of Blood total completion time: 28:34.07", e, "Local Name", NOW));
		assertNull(c.accept("Duration: 3:52.40", e, "Local Name", NOW));
		assertNull(c.accept("Theatre of Blood completion time: 15:01.20", e, "Local Name", NOW));
		ChallengeObservation o = c.accept("Your completed Theatre of Blood count is: 42", e, "Local Name", NOW);
		assertNotNull(o); assertEquals("theatre_of_blood", o.activityKey); assertEquals("tob_duo", o.modeKey);
		assertEquals(Long.valueOf(901200), o.roomTimeMillis); assertEquals(Long.valueOf(1714070), o.overallTimeMillis);
		assertEquals(Integer.valueOf(42), o.completionCount);
		assertNull(c.accept("Theatre of Blood completion time: 15:01.20", e, "Local Name", NOW));
	}
	@Test public void hmtNeedsHardConfirmationAndSelectsOverall()
	{
		ChallengeCompletionCorrelator c = new ChallengeCompletionCorrelator();
		RaidCompletionEvidence e = evidence(RaidType.TOB, 2, true, true, "Local Name", "A", "B", "C", "D");
		c.accept("Theatre of Blood completion time: 15:01.20", e, "Local Name", NOW);
		c.accept("Theatre of Blood total completion time: 21:03.00", e, "Local Name", NOW);
		assertNull(c.onEvidence(e, "Local Name", NOW));
		ChallengeObservation o = c.accept("Your completed Theatre of Blood: Hard Mode count is: 8", e, "Local Name", NOW);
		assertNotNull(o); assertEquals("theatre_of_blood_hard_mode", o.activityKey); assertEquals("hmt_5man", o.modeKey);
		assertEquals(Long.valueOf(1_263_000), o.overallTimeMillis);
	}
	@Test public void tobModeConfirmationMayArriveBeforeMetrics()
	{
		ChallengeCompletionCorrelator c = new ChallengeCompletionCorrelator();
		RaidCompletionEvidence e = evidence(RaidType.TOB, 13, false, true, "Local Name", "Partner Name");
		assertNull(c.accept("Your completed Theatre of Blood count is: 42", e, "Local Name", NOW));
		assertNull(c.accept("Theatre of Blood total completion time: 28:34.07", e, "Local Name", NOW));
		ChallengeObservation result = c.accept("Theatre of Blood completion time: 15:01.20", e, "Local Name", NOW);
		assertNotNull(result); assertEquals("theatre_of_blood", result.activityKey);
	}
	@Test public void toaAndCoxSelectOverall()
	{
		ChallengeCompletionCorrelator toa = new ChallengeCompletionCorrelator();
		RaidCompletionEvidence te = evidence(RaidType.TOA, 3, false, true, "Local Name");
		assertNotNull(ChallengeObservation.create("tombs_of_amascut", "toa_expert_solo", null,
			2_101_200L, 42, te.roster, "Local Name", NOW));
		assertEquals(ChallengeCompletionParser.Mode.EXPERT, new ChallengeCompletionParser().parse("Tombs of Amascut: Expert Mode total completion time: 35:01.20").mode);
		assertEquals(ChallengeCompletionParser.Mode.EXPERT, new ChallengeCompletionParser().parse("Your completed Tombs of Amascut: Expert Mode count is: 42").mode);
		toa.accept("Tombs of Amascut: Expert Mode total completion time: 35:01.20", te, "Local Name", NOW);
		ChallengeObservation t = toa.accept("Your completed Tombs of Amascut: Expert Mode count is: 42", te, "Local Name", NOW);
		assertNotNull(t); assertEquals("toa_expert_solo", t.modeKey); assertEquals(Long.valueOf(2_101_200), t.overallTimeMillis);
		ChallengeCompletionCorrelator cox = new ChallengeCompletionCorrelator();
		RaidCompletionEvidence ce = evidence(RaidType.COX, 4, true, true, "Local Name");
		ChallengeObservation x = cox.accept("Team size: 1 players Duration: 22:01.20 Olm duration: 5:03.40", ce, "Local Name", NOW);
		assertNotNull(x); assertEquals("chambers_of_xeric_challenge_mode", x.activityKey);
		assertEquals(Long.valueOf(1_321_200), x.overallTimeMillis);
		ChallengeCompletionCorrelator normal = new ChallengeCompletionCorrelator();
		RaidCompletionEvidence ne = evidence(RaidType.COX, 12, false, true, "Local Name");
		ChallengeObservation n = normal.accept("Team size: 1 players Duration: 22:01.20", ne, "Local Name", NOW);
		assertNotNull(n); assertEquals("chambers_of_xeric", n.activityKey); assertEquals("cox_solo", n.modeKey);
	}
	@Test public void timeoutRunChangeAndIncompleteRosterFailClosed()
	{
		AtomicLong clock = new AtomicLong(); ChallengeCompletionCorrelator c = correlator(clock);
		RaidCompletionEvidence incomplete = new RaidCompletionEvidence(RaidType.TOB, 5, false, true,
			new GroupSnapshot("fixture", List.of("Local Name"), 2, GroupSnapshot.Status.INCOMPLETE, "incomplete"));
		c.accept("Theatre of Blood completion time: 15:01", incomplete, "Local Name", NOW);
		c.accept("Theatre of Blood total completion time: 28:34", incomplete, "Local Name", NOW);
		assertNull(c.accept("Your completed Theatre of Blood count is: 3", incomplete, "Local Name", NOW));
		ChallengeCompletionCorrelator stale = correlator(clock);
		stale.accept("Theatre of Blood completion time: 15:01", evidence(RaidType.TOB, 6, false, true, "Local Name", "Partner"), "Local Name", NOW);
		clock.addAndGet(ChallengeCompletionCorrelator.WINDOW_NANOS + 1);
		assertNull(stale.accept("Theatre of Blood total completion time: 28:34", evidence(RaidType.TOB, 6, false, true, "Local Name", "Partner"), "Local Name", NOW));
		assertNull(stale.accept("Theatre of Blood completion time: 15:01", evidence(RaidType.TOB, 7, false, true, "Local Name", "Partner"), "Local Name", NOW));
	}
	@Test public void toaWithoutOptionalCountWaitsBrieflyAndCanBeDiscardedOnTimeout()
	{
		AtomicLong clock = new AtomicLong(); ChallengeCompletionCorrelator c = correlator(clock);
		RaidCompletionEvidence e = evidence(RaidType.TOA, 9, false, true, "Local Name");
		assertNull(c.accept("Tombs of Amascut: Expert Mode total completion time: 35:01.20", e, "Local Name", NOW));
		assertNull(c.onEvidence(e, "Local Name", NOW));
		clock.addAndGet(ChallengeCompletionCorrelator.OPTIONAL_COUNT_GRACE_NANOS + 1);
		ChallengeObservation result = c.onEvidence(e, "Local Name", NOW);
		assertNotNull(result); assertNull(result.completionCount);
	}
	@Test public void conflictingDuplicateBlocksTheRun()
	{
		ChallengeCompletionCorrelator c = new ChallengeCompletionCorrelator();
		RaidCompletionEvidence e = evidence(RaidType.TOB, 10, false, true, "Local Name", "Partner");
		c.accept("Theatre of Blood completion time: 15:01.20", e, "Local Name", NOW);
		assertNull(c.accept("Theatre of Blood completion time: 15:02.20", e, "Local Name", NOW));
		assertNull(c.accept("Theatre of Blood total completion time: 28:34.07", e, "Local Name", NOW));
		assertNull(c.accept("Your completed Theatre of Blood count is: 42", e, "Local Name", NOW));
	}
	@Test public void reporterDuplicatesAndSizesFailClosed()
	{
		assertNull(ChallengeObservation.create("theatre_of_blood", "tob_duo", 1L, 2L, null, roster("Local Name", "localname"), "Local Name", NOW));
		assertNull(ChallengeObservation.create("theatre_of_blood", "tob_duo", 1L, 2L, null, roster("Other One", "Other Two"), "Local Name", NOW));
		assertNull(ChallengeObservation.create("theatre_of_blood", "tob_duo", 1L, 2L, null,
			new GroupSnapshot("fixture", List.of("Local Name"), 2, GroupSnapshot.Status.MATCHED, "bad"), "Local Name", NOW));
		assertNull(ChallengeObservation.create("unknown", "bad", 1L, 2L, null, roster("Local Name"), "Local Name", NOW));
	}
}
