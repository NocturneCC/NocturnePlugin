package com.nocturne;

import java.util.List;
import org.junit.Test;
import static org.junit.Assert.*;

public class RaidSessionTest
{
	@Test
	public void fullRosterIncludesRecipientAndSurvivesSizeDroppingAfterDeaths()
	{
		RaidSession session = session(true);
		session.observe(List.of("Simon", "Alice", "Bob"), 3, 5);
		session.observe(List.of("Simon", "Alice"), 2, 10);
		GroupSnapshot snapshot = session.snapshot();
		assertEquals(GroupSnapshot.Status.MATCHED, snapshot.status);
		assertEquals(3, snapshot.expectedSize);
		assertEquals(List.of("Alice", "Bob", "Simon"), snapshot.names);
	}

	@Test
	public void smallerRosterDoesNotBecomeCompleteJustBecauseAllObservedNamesExist()
	{
		RaidSession session = session(true);
		session.observe(List.of("Simon", "Alice"), 3, 5);
		assertEquals(GroupSnapshot.Status.INCOMPLETE, session.snapshot().status);
		assertEquals(3, session.snapshot().expectedSize);
	}

	@Test
	public void midRaidEnableRemainsIncompleteEvenWithMatchingNames()
	{
		RaidSession session = session(false);
		session.observe(List.of("Simon", "Alice"), 2, 5);
		assertEquals(GroupSnapshot.Status.INCOMPLETE, session.snapshot().status);
	}

	@Test
	public void missingRecipientOrMissingTeamSizeCannotMatch()
	{
		RaidSession session = session(true);
		session.observe(List.of("Alice", "Bob"), 2, 5);
		assertEquals(GroupSnapshot.Status.INCOMPLETE, session.snapshot().status);
		session = session(true);
		session.observe(List.of("Simon"), 0, 5);
		assertEquals(GroupSnapshot.Status.INCOMPLETE, session.snapshot().status);
	}

	@Test
	public void knownSoloRemainsOnePerson()
	{
		RaidSession session = session(true);
		session.observe(List.of("Simon"), 1, 5);
		assertEquals(GroupSnapshot.Status.MATCHED, session.snapshot().status);
		assertEquals(1, session.snapshot().names.size());
	}

	@Test
	public void completedRosterDoesNotAbsorbLaterLobbyNames()
	{
		RaidSession session = session(true);
		session.observe(List.of("Simon", "Alice"), 2, 5);
		session.finish();
		session.observe(List.of("Simon", "Stranger"), 2, 6);
		assertEquals(List.of("Alice", "Simon"), session.snapshot().names);
	}

	@Test
	public void changedTeamAndOldSnapshotsStayDistinct()
	{
		RaidSession session = session(true);
		session.observe(List.of("Simon", "Alice"), 2, 5);
		GroupSnapshot earlier = session.snapshot();
		session.observe(List.of("Simon", "Bob"), 2, 10);
		assertEquals(List.of("Alice", "Simon"), earlier.names);
		assertEquals(GroupSnapshot.Status.INCOMPLETE, session.snapshot().status);
		RaidSession next = session(true);
		next.observe(List.of("Simon", "Bob"), 2, 20);
		assertEquals(List.of("Bob", "Simon"), next.snapshot().names);
		assertEquals(GroupSnapshot.Status.MATCHED, next.snapshot().status);
	}

	@Test
	public void repeatedNamesAreCaseInsensitiveAndBlankSlotsIgnored()
	{
		RaidSession session = session(true);
		session.observe(List.of("Simon", "simon", " Alice ", "-", ""), 2, 5);
		assertEquals(2, session.snapshot().names.size());
		assertEquals(GroupSnapshot.Status.MATCHED, session.snapshot().status);
	}

	@Test
	public void retainedRosterExpires()
	{
		RaidSession session = session(true);
		session.observe(List.of("Simon"), 1, 5);
		session.finish();
		assertFalse(session.expired(1005));
		assertTrue(session.expired(1006));
	}

	@Test
	public void reopeningChestIsSuppressedButIdenticalLootInNextRunIsKept()
	{
		RaidSession session = session(true);
		assertTrue(session.acceptReward("526:1|995:100"));
		assertFalse(session.acceptReward("526:1|995:100"));
		assertTrue(session(true).acceptReward("526:1|995:100"));
	}

	@Test
	public void modeNamesResolveToCorrectRaidAndOtherBossesDoNot()
	{
		assertEquals(RaidType.TOB, RaidType.fromSource("Theatre of Blood: Hard Mode"));
		assertEquals(RaidType.COX, RaidType.fromSource("Chambers of Xeric Challenge Mode"));
		assertEquals(RaidType.TOA, RaidType.fromSource("Tombs of Amascut: Expert Mode"));
		assertNull(RaidType.fromSource("Nex"));
		assertNull(RaidType.fromSource(null));
	}

	@Test
	public void chambersSoloAndThreePlayerRosterRequireExactAuthoritativeSize()
	{
		RaidSession solo = chambers(true, 1, 44);
		solo.observe(List.of("De Lena"), 1, 2);
		assertEquals(GroupSnapshot.Status.MATCHED, solo.snapshot().status);

		RaidSession team = chambers(true, 2, 45);
		team.observe(List.of("De Lena", "Teammate 1", "Third"), 3, 3);
		assertEquals(GroupSnapshot.Status.MATCHED, team.snapshot().status);
		assertEquals(3, team.snapshot().names.size());
		RaidSession partial = chambers(true, 3, 46);
		partial.observe(List.of("De Lena", "Teammate 1"), 3, 3);
		assertEquals(GroupSnapshot.Status.INCOMPLETE, partial.snapshot().status);
	}

	@Test
	public void outsideEntrySurvivesPartyAssemblyHolderChangesInNormalAndChallengeMode()
	{
		assertEquals(RaidType.COX, RaidType.fromSource("Chambers of Xeric"));
		assertEquals(RaidType.COX, RaidType.fromSource("Chambers of Xeric Challenge Mode"));
		for (String ignored : List.of("normal", "challenge"))
		{
			RaidSession session = chambers(true, 11, -1);
			session.observe(List.of("De Lena"), 1, 1);
			assertEquals(List.of("De Lena"), session.initialSnapshot().names);
			session.updatePartyGroup(41);
			session.updatePartyGroup(42);
			session.observe(List.of("Bifuor", "De Lena", "Not ZB"), 3, 2);
			assertEquals(42, session.partyGroup());
			assertEquals(3, session.maxReportedSize());
			assertEquals(List.of("De Lena"), session.initialSnapshot().names);
			assertEquals(GroupSnapshot.Status.MATCHED, session.snapshot().status);
			assertFalse(GroupTracker.startsNewRaidSession(RaidType.COX, RaidType.COX));
		}
	}

	@Test
	public void lateEnableReconnectAndDifferentRaidStillStartIncompleteOrFresh()
	{
		RaidSession late = chambers(false, 11, 42);
		late.observe(List.of("Bifuor", "De Lena", "Not ZB"), 3, 1);
		assertEquals(GroupSnapshot.Status.INCOMPLETE, late.snapshot().status);
		assertTrue(GroupTracker.startsNewRaidSession(null, RaidType.COX));
		assertTrue(GroupTracker.startsNewRaidSession(RaidType.TOA, RaidType.COX));
	}

	@Test
	public void rewardDiagnosticsStayFrozenUntilTheNextRaid()
	{
		assertTrue(GroupTracker.shouldSampleDiagnostics(true, false));
		assertFalse(GroupTracker.shouldSampleDiagnostics(true, true));
		assertFalse(GroupTracker.shouldSampleDiagnostics(false, false));
	}

	@Test
	public void chambersCompletionUsesCurrentRosterAndSurvivesWidgetDisappearance()
	{
		RaidSession session = chambers(true, 1, 44);
		session.observe(List.of("De Lena", "One"), 3, 2);
		session.observe(List.of("De Lena", "Two"), 3, 7);
		session.observe(List.of(), 0, 12);
		assertEquals(List.of("De Lena", "One"), session.initialSnapshot().names);
		assertEquals(List.of("De Lena", "Two"), session.currentSnapshot().names);
		session.finishChambers(10_000, 1_000);
		assertEquals(List.of("De Lena", "Two"), session.completionSnapshot().names);
		assertTrue(session.snapshot().detail.contains("departed_before_completion=1"));
	}

	@Test
	public void reconnectAndLateEnableRemainIncomplete()
	{
		RaidSession reconnected = chambers(false, 1, 44);
		reconnected.observe(List.of("De Lena", "One", "Two"), 3, 2);
		assertEquals(GroupSnapshot.Status.INCOMPLETE, reconnected.snapshot().status);
	}

	@Test
	public void completionMessageAndRunIdentityRejectStaleRewards()
	{
		assertTrue(GroupTracker.isChambersCompletionMessage(
			"<col=ef1020>Congratulations - your raid is complete!</col>"));
		RaidSession old = chambers(true, 7, 100);
		old.observe(List.of("De Lena", "One", "Two"), 3, 2);
		old.finish();
		assertTrue(GroupTracker.isCurrentChambersReward(old, null, 7, 100, 3));
		assertFalse(GroupTracker.isCurrentChambersReward(old, null, 8, 101, 3));
		assertFalse(GroupTracker.isCurrentChambersReward(old, null, 7, 101, 3));
		assertTrue(GroupTracker.isCurrentChambersReward(old, null, 7, -1, 3));
	}

	@Test
	public void consumedRosterCannotLeakIntoAnotherReward()
	{
		RaidSession session = chambers(true, 7, 100);
		session.observe(List.of("De Lena", "One", "Two"), 3, 2);
		assertTrue(session.acceptReward("loot"));
		GroupSnapshot frozen = session.snapshot();
		session.clearRoster();
		assertEquals(3, frozen.names.size());
		assertTrue(session.snapshot().names.isEmpty());
		assertFalse(session.acceptReward("loot"));
	}

	@Test public void presenceReportSeparatesSessionFactsFromRosterNames()
	{
		RaidSession session = new RaidSession(RaidType.COX, "De Lena", true, 0, 9, 44, true);
		session.observe(List.of("Bifuor", "De Lena", "Not ZB"), 3, 1);
		session.finishChambers(10_000, 500);
		JsonAssertions.assertOwnPresenceOnly(session.presenceReport("completion", 420, 1_000).json());
	}

	private static final class JsonAssertions
	{
		static void assertOwnPresenceOnly(com.google.gson.JsonObject json)
		{
			assertEquals("COX_CM", json.get("raid_type").getAsString());
			assertEquals(500, json.get("contribution_basis_points").getAsInt());
			assertFalse(json.toString().contains("Bifuor"));
			assertFalse(json.toString().contains("Not ZB"));
		}
	}

	private static RaidSession chambers(boolean entryObserved, long epoch, int partyGroup)
	{
		return new RaidSession(RaidType.COX, "De Lena", entryObserved, 0, epoch, partyGroup);
	}

	private static RaidSession session(boolean entryObserved)
	{
		return new RaidSession(RaidType.TOB, "Simon", entryObserved, 0);
	}
}
