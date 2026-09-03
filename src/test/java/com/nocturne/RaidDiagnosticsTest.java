package com.nocturne;

import org.junit.Test;
import static org.junit.Assert.*;

public class RaidDiagnosticsTest
{
	@Test
	public void reportsNormalAndChallengeModeSignalsWithoutNames()
	{
		RaidDiagnostics normal = diagnostic("Chambers of Xeric: Normal");
		RaidDiagnostics challenge = diagnostic("Chambers of Xeric: Challenge Mode");
		for (RaidDiagnostics value : new RaidDiagnostics[] {normal, challenge})
		{
			String text = value.displayText();
			assertTrue(text.contains(value.raidType));
			assertTrue(text.contains("Expected party size: 4"));
			assertTrue(text.contains("Roster candidates / accepted names: 7 / 0"));
			assertTrue(text.contains("Snapshot: INCOMPLETE"));
			assertFalse(text.contains("Teammate"));
		}
	}

	@Test
	public void snapshotStateCanAdvanceWithoutChangingStructuralCounts()
	{
		RaidDiagnostics before = diagnostic("Chambers of Xeric: Challenge Mode");
		RaidDiagnostics after = before.withSnapshotState("MATCHED");
		assertEquals(7, after.candidateCount);
		assertEquals(0, after.acceptedNameCount);
		assertEquals("MATCHED", after.snapshotState);
	}

	@Test
	public void raidSessionChangeReplacesStaleChambersDiagnostics()
	{
		RaidDiagnostics chambers = diagnostic("Chambers of Xeric: Normal");
		RaidDiagnostics tombs = RaidDiagnostics.awaiting(RaidType.TOA);
		assertTrue(chambers.displayText().contains("Chambers of Xeric"));
		assertTrue(tombs.displayText().contains("Tombs of Amascut"));
		assertFalse(tombs.displayText().contains("Chambers of Xeric"));
		assertTrue(tombs.displayText().contains("AWAITING_SNAPSHOT"));
	}

	@Test
	public void tombsDiagnosticsDescribeOnlyCurrentPartySlotSnapshot()
	{
		RaidDiagnostics tombs = RaidDiagnostics.partySlots(RaidType.TOA, 3, 3, "MATCHED");
		String text = tombs.displayText();
		assertTrue(text.contains("Tombs of Amascut"));
		assertTrue(text.contains("Expected party size: 3"));
		assertTrue(text.contains("Roster candidates / accepted names: 3 / 3"));
		assertTrue(text.contains("RuneLite party-name slots"));
		assertFalse(text.contains("Bifuor"));
		assertFalse(text.contains("De Lena"));
		assertFalse(text.contains("Smooth"));
	}

	private static RaidDiagnostics diagnostic(String type)
	{
		return new RaidDiagnostics(type, 4, 81, 7, 0, "INCOMPLETE",
			"id=32768010, hidden=false, dynamic=0, static=4, nested=12, text-present=4, name-present=0");
	}
}
