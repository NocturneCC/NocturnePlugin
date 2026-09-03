package com.nocturne;

final class RaidVerificationStatus
{
	static final RaidVerificationStatus INACTIVE = new RaidVerificationStatus(0, 0, false, false,
		"Waiting for an active Chambers raid.");
	static final RaidVerificationStatus UNAVAILABLE = new RaidVerificationStatus(0, 0, false, false,
		"Presence backend unavailable; loot capture continues locally.");
	final int verified, expected;
	final boolean consistent, groupQualified;
	final String reason;

	RaidVerificationStatus(int verified, int expected, boolean consistent, boolean groupQualified, String reason)
	{
		this.verified = verified; this.expected = expected; this.consistent = consistent;
		this.groupQualified = groupQualified; this.reason = reason;
	}
}
