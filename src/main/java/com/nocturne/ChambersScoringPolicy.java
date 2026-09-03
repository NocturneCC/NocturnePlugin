package com.nocturne;

/** Client-side proposal only. Any future award must independently validate this server-side. */
final class ChambersScoringPolicy
{
	enum Mode { SOLO_PERSONAL_ONLY, NORMAL_GROUP, MASS_PERSONAL_ONLY, INVALID }

	final Mode mode;
	final boolean eligible;
	final String explanation;

	private ChambersScoringPolicy(Mode mode, boolean eligible, String explanation)
	{
		this.mode = mode;
		this.eligible = eligible;
		this.explanation = explanation;
	}

	static ChambersScoringPolicy evaluate(RaidSession session)
	{
		int scale = session.maxReportedSize();
		Mode mode = scale == 1 ? Mode.SOLO_PERSONAL_ONLY
			: scale >= 2 && scale <= 20 ? Mode.NORMAL_GROUP
			: scale > 20 ? Mode.MASS_PERSONAL_ONLY : Mode.INVALID;
		if (!session.hasFinalPoints() || session.finalTeamPoints() <= 0 || session.finalPersonalPoints() <= 0)
		{
			return new ChambersScoringPolicy(mode, false,
				"Not eligible: valid final personal and team raid points were unavailable.");
		}
		if (mode == Mode.INVALID)
		{
			return new ChambersScoringPolicy(mode, false,
				"Not eligible: authoritative raid scale was unavailable.");
		}
		if (mode == Mode.MASS_PERSONAL_ONLY)
		{
			return new ChambersScoringPolicy(mode, true,
				"Mass raid: local recipient only, standard personal 1x; no roster split or 5% test.");
		}
		if (session.completionSnapshot().status != GroupSnapshot.Status.MATCHED)
		{
			return new ChambersScoringPolicy(mode, false,
				"Not eligible: the completion roster could not be verified.");
		}
		long personal = Math.max(0, session.finalPersonalPoints());
		boolean passes = personal * 10_000L >= (long) session.finalTeamPoints() * 500L;
		String policy = mode == Mode.SOLO_PERSONAL_ONLY ? " Solo raid: standard personal 1x."
			: " Normal group: any future multiplier/split proposal uses only the completion roster.";
		return new ChambersScoringPolicy(mode, passes, passes
			? "Local recipient contribution met the 5.00% minimum. Teammate percentages are unknown." + policy
			: "Not eligible: local recipient contribution was below the 5.00% minimum.");
	}
}
