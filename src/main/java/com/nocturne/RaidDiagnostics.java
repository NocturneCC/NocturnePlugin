package com.nocturne;

/** Local-only, name-free raid roster diagnostics. */
final class RaidDiagnostics
{
	static final RaidDiagnostics INACTIVE = new RaidDiagnostics("None", 0, -1, 0, 0,
		"No snapshot", "Roster widget not sampled");

	final String raidType;
	final int expectedPartySize;
	final int partyGroupHolder;
	final int candidateCount;
	final int acceptedNameCount;
	final String snapshotState;
	final String widgetStructure;

	RaidDiagnostics(String raidType, int expectedPartySize, int partyGroupHolder,
		int candidateCount, int acceptedNameCount, String snapshotState, String widgetStructure)
	{
		this.raidType = raidType;
		this.expectedPartySize = expectedPartySize;
		this.partyGroupHolder = partyGroupHolder;
		this.candidateCount = candidateCount;
		this.acceptedNameCount = acceptedNameCount;
		this.snapshotState = snapshotState;
		this.widgetStructure = widgetStructure;
	}

	static RaidDiagnostics awaiting(RaidType raid)
	{
		return new RaidDiagnostics(raid.title, 0, raid == RaidType.COX ? 0 : -1,
			0, 0, "AWAITING_SNAPSHOT", raid == RaidType.COX
				? "Raiding-party sidebar not sampled yet"
				: "RuneLite party-name slots not sampled yet");
	}

	static RaidDiagnostics partySlots(RaidType raid, int expectedSize, int acceptedNames,
		String snapshotState)
	{
		return new RaidDiagnostics(raid.title, expectedSize, -1, acceptedNames, acceptedNames,
			snapshotState, "RuneLite party-name slots; no roster widget traversal");
	}

	String displayText()
	{
		return "Raid diagnostics (local only)\nDetected: " + raidType
			+ "\nExpected party size: " + expectedPartySize
			+ "\nParty-group holder: " + partyGroupHolder
			+ "\nRoster candidates / accepted names: " + candidateCount + " / " + acceptedNameCount
			+ "\nSnapshot: " + snapshotState
			+ "\nWidget structure: " + widgetStructure;
	}

	RaidDiagnostics withSnapshotState(String state)
	{
		return new RaidDiagnostics(raidType, expectedPartySize, partyGroupHolder,
			candidateCount, acceptedNameCount, state, widgetStructure);
	}
}
