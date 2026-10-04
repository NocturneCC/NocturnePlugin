package com.nocturne;

/** Immutable completed or in-progress GroupTracker identity for correlation only. */
final class RaidCompletionEvidence
{
	final RaidType raid;
	final long runEpoch;
	final boolean challengeMode;
	final boolean completed;
	final GroupSnapshot roster;

	RaidCompletionEvidence(RaidType raid, long runEpoch, boolean challengeMode,
		boolean completed, GroupSnapshot roster)
	{
		this.raid = raid;
		this.runEpoch = runEpoch;
		this.challengeMode = challengeMode;
		this.completed = completed;
		this.roster = roster;
	}
}
