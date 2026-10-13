package com.nocturne;

import static org.junit.Assert.*;
import java.time.Instant;
import org.junit.Test;

public class DoomRunTrackerTest
{
	@Test public void screenshotCompletionHeaderIsExactSuccessSignal()
	{
		assertEquals(Integer.valueOf(7), DoomRunTracker.completedLevel("Level 7 Complete!"));
		assertEquals(Integer.valueOf(7), DoomRunTracker.completedLevel("<col=ff0000> Level&nbsp; 7 Complete! </col>"));
		assertNull(DoomRunTracker.completedLevel("Level 8"));
		assertNull(DoomRunTracker.completedLevel("Level 10001 Complete!"));
	}

	@Test public void successiveLevelsAndDeathDuringNextLevelRetainHighestCompleted()
	{
		DoomRunTracker tracker = new DoomRunTracker();
		Instant first = Instant.parse("2026-10-13T10:00:00Z");
		DoomRunTracker.Progress levelThree = tracker.completeLevel(3, "Local Name", "session", 10, first);
		DoomRunTracker.Progress levelSeven = tracker.completeLevel(7, "Local Name", "session", 20, first.plusSeconds(60));
		assertEquals(3, levelThree.highestLevel);
		assertEquals(7, levelSeven.highestLevel);
		assertNull("currently entered floor is not a success event",
			DoomRunTracker.completedLevel("You descend to level 8."));
		assertTrue(DoomRunTracker.isDeathMessage("Oh dear, you are dead!"));
		DoomRunTracker.Result result = tracker.finishRun();
		assertNotNull(result);
		assertEquals(7, result.highestLevel);
		assertEquals(first.plusSeconds(60), result.occurredAt);
		assertNull(tracker.finishRun());
	}

	@Test public void duplicateSignalsAndRewardClaimFinishOnceAndLinkOnlyClaimedLoot()
	{
		DoomRunTracker tracker = new DoomRunTracker();
		Instant now = Instant.parse("2026-10-13T10:00:00Z");
		DoomRunTracker.Progress first = tracker.completeLevel(6, "Local Name", "session", 50, now);
		DoomRunTracker.Progress latest = tracker.completeLevel(7, "Local Name", "session", 80, now.plusSeconds(31));
		assertEquals(first.eventId, latest.eventId);
		assertNull(tracker.completeLevel(7, "Local Name", "session", 81, now.plusSeconds(32)));
		assertNull("unclaimed or in-run loot cannot be assigned by guess", tracker.consumeClaimedLootLink());
		DoomRunTracker.Result claimed = tracker.finishByAction(DoomRunTracker.EndAction.CLAIM_REWARDS);
		assertEquals(7, claimed.highestLevel);
		assertNull("replayed loaded completion after finalization remains a replay",
			tracker.completeLevel(7, "Local Name", "session", 80, now.plusSeconds(32)));
		assertNull("a later replay of a non-first floor cannot start a phantom run",
			tracker.completeLevel(7, "Local Name", "session", 150, now.plusSeconds(40)));
		assertNull("duplicate claim does not create a second completion", tracker.finishByAction(
			DoomRunTracker.EndAction.CLAIM_REWARDS));
		assertEquals("the explicit claim association has no arbitrary 30-second expiry",
			first.eventId, tracker.consumeClaimedLootLink());
		assertNull("a claimed reward is linked at most once", tracker.consumeClaimedLootLink());
	}

	@Test public void leavingWithoutLootKeepsCompletionButNeverClaimsLootAssociation()
	{
		DoomRunTracker tracker = new DoomRunTracker();
		Instant now = Instant.parse("2026-10-13T10:00:00Z");
		tracker.completeLevel(7, "Local Name", "session", 50, now);
		DoomRunTracker.Result left = tracker.finishByAction(DoomRunTracker.EndAction.LEAVE_DELVING);
		assertNotNull(left);
		assertEquals(7, left.highestLevel);
		assertNull(tracker.consumeClaimedLootLink());
		assertNull(tracker.finishByAction(DoomRunTracker.EndAction.LEAVE_DELVING));
	}

	@Test public void deathPreservesHighestAndLaterExplicitClaimCanLinkItsReward()
	{
		DoomRunTracker tracker = new DoomRunTracker();
		Instant now = Instant.parse("2026-10-13T10:00:00Z");
		tracker.completeLevel(4, "Local Name", "session", 50, now);
		tracker.completeLevel(7, "Local Name", "session", 80, now.plusSeconds(60));
		DoomRunTracker.Result death = tracker.finishOnDeathMessage("Oh dear, you are dead!");
		assertNotNull(death);
		assertEquals(7, death.highestLevel);
		assertNull(tracker.consumeClaimedLootLink());
		assertNull("claim after death must not submit a second completion",
			tracker.finishByAction(DoomRunTracker.EndAction.CLAIM_REWARDS));
		assertEquals(death.eventId, tracker.consumeClaimedLootLink());
		assertNull("a repeated claim cannot relink later unrelated loot",
			tracker.finishByAction(DoomRunTracker.EndAction.CLAIM_REWARDS));
	}

	@Test public void nextRunGetsFreshIdentityAndResetsDeepestLevel()
	{
		DoomRunTracker tracker = new DoomRunTracker();
		Instant now = Instant.parse("2026-10-13T10:00:00Z");
		DoomRunTracker.Progress first = tracker.completeLevel(7, "Local Name", "session", 1, now);
		tracker.finishByAction(DoomRunTracker.EndAction.CLAIM_REWARDS);
		assertEquals(first.eventId, tracker.consumeClaimedLootLink());
		DoomRunTracker.Progress secondRun = tracker.completeLevel(1, "Local Name", "session", 2, now.plusSeconds(600));
		assertEquals(1, secondRun.highestLevel);
		assertNotEquals(first.eventId, secondRun.eventId);
		assertNull("a new run invalidates the prior claim-to-loot association",
			tracker.consumeClaimedLootLink());
	}

	@Test public void noSuccessfulLevelCreatesNoCompletion()
	{
		DoomRunTracker tracker = new DoomRunTracker();
		assertNull(tracker.finishRun());
		assertFalse(tracker.hasActiveRun());
	}
}
