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

	private static RaidSession session(boolean entryObserved)
	{
		return new RaidSession(RaidType.TOB, "Simon", entryObserved, 0);
	}
}
