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

	private static RaidDiagnostics diagnostic(String type)
	{
		return new RaidDiagnostics(type, 4, 81, 7, 0, "INCOMPLETE",
			"id=32768010, hidden=false, dynamic=0, static=4, nested=12, text-present=4, name-present=0");
	}
}
