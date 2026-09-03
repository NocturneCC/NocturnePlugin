package com.nocturne;

import java.util.Collection;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.time.Instant;
import java.util.UUID;

/** Client-thread state for one run. Old loot records hold immutable snapshots. */
final class RaidSession
{
	static final long RETENTION_TICKS = 1000; // About ten minutes after the last active tick.
	final RaidType type;
	final long runEpoch;
	final String presenceEpoch;
	final long startedAt;
	final boolean challengeMode;
	private int partyGroup;
	private final String localName;
	private final boolean entryObserved;
	private final Set<String> initialNames = new LinkedHashSet<>();
	private final Set<String> currentNames = new LinkedHashSet<>();
	private final Set<String> completionNames = new LinkedHashSet<>();
	private final Set<String> observedNames = new LinkedHashSet<>();
	private final Set<String> rewardSignatures = new HashSet<>();
	private int maxReportedSize;
	private int initialReportedSize;
	private int currentReportedSize;
	private int completionReportedSize;
	private int lastReportedSize;
	private boolean completed;
	private boolean lastRosterObservationVerified;
	private boolean completionRosterRetained;
	private boolean overflow;
	private boolean finalPointsCaptured;
	private int finalTeamPoints;
	private int finalPersonalPoints;
	private long completedAt;
	private long lastActiveTick;

	RaidSession(RaidType type, String localName, boolean entryObserved, long tick)
	{
		this(type, localName, entryObserved, tick, 0, -1);
	}

	RaidSession(RaidType type, String localName, boolean entryObserved, long tick,
		long runEpoch, int partyGroup)
	{
		this(type, localName, entryObserved, tick, runEpoch, partyGroup, false);
	}

	RaidSession(RaidType type, String localName, boolean entryObserved, long tick,
		long runEpoch, int partyGroup, boolean challengeMode)
	{
		this.type = type;
		this.runEpoch = runEpoch;
		this.partyGroup = partyGroup;
		this.challengeMode = challengeMode;
		this.presenceEpoch = UUID.randomUUID().toString();
		this.startedAt = Instant.now().getEpochSecond();
		this.localName = localName;
		this.entryObserved = entryObserved;
		lastActiveTick = tick;
	}

	boolean isRun(long epoch, int group)
	{
		return runEpoch == epoch && (type != RaidType.COX || partyGroup == group);
	}

	int partyGroup()
	{
		return partyGroup;
	}

	void updatePartyGroup(int group)
	{
		if (!completed && group >= 0) partyGroup = group;
	}

	void observe(Collection<String> observedNames, int reportedSize, long tick)
	{
		if (completed)
		{
			return;
		}
		lastActiveTick = tick;
		List<String> unique = GroupSnapshot.uniqueNames(observedNames);
		if (type == RaidType.COX || type == RaidType.TOA || type == RaidType.TOB)
		{
			lastReportedSize = reportedSize;
			lastRosterObservationVerified = reportedSize > 0 && unique.size() == reportedSize
				&& unique.stream().anyMatch(name -> name.equalsIgnoreCase(localName));
		}
		if (!unique.isEmpty())
		{
			if (type == RaidType.COX || type == RaidType.TOA || type == RaidType.TOB) currentNames.clear();
			for (String name : unique)
			{
				if (currentNames.size() >= 100) { overflow = true; break; }
				currentNames.add(name);
				this.observedNames.add(name);
			}
			if (initialNames.isEmpty())
			{
				initialNames.addAll(currentNames);
				initialReportedSize = reportedSize;
			}
		}
		if (reportedSize > 0)
		{
			maxReportedSize = Math.max(maxReportedSize, reportedSize);
			currentReportedSize = type == RaidType.COX || type == RaidType.TOA || type == RaidType.TOB
				? reportedSize : maxReportedSize;
		}
	}

	void finish()
	{
		completed = true;
	}

	void finishTombs(boolean freshCompletionRead)
	{
		if (type != RaidType.TOA) return;
		finishPartySlots(freshCompletionRead);
	}

	void finishTheatre(boolean freshCompletionRead)
	{
		if (type != RaidType.TOB) return;
		finishPartySlots(freshCompletionRead);
	}

	private void finishPartySlots(boolean freshCompletionRead)
	{
		if (completed) return;
		completionNames.clear();
		completionNames.addAll(currentNames);
		completionReportedSize = lastReportedSize > 0 ? lastReportedSize : currentReportedSize;
		completionRosterRetained = !freshCompletionRead || !lastRosterObservationVerified;
		completedAt = Instant.now().getEpochSecond();
		completed = true;
	}

	boolean hasVerifiedCurrentRoster()
	{
		return lastRosterObservationVerified;
	}

	void finishChambers(int teamPoints, int personalPoints)
	{
		if (completed) return;
		completionNames.clear();
		completionNames.addAll(currentNames);
		completionRosterRetained = !lastRosterObservationVerified;
		completionReportedSize = completionRosterRetained && lastReportedSize > 0
			? lastReportedSize : currentReportedSize;
		finalTeamPoints = teamPoints;
		finalPersonalPoints = Math.max(0, personalPoints);
		finalPointsCaptured = teamPoints > 0 && personalPoints >= 0;
		completedAt = Instant.now().getEpochSecond();
		completed = true;
	}

	boolean expired(long tick)
	{
		return tick - lastActiveTick > RETENTION_TICKS;
	}

	boolean acceptReward(String signature)
	{
		// Reopening a reward chest must not create another record for this run.
		// The next run gets a fresh RaidSession, including for identical loot.
		if (rewardSignatures.size() >= 16)
		{
			return false;
		}
		return rewardSignatures.add(signature);
	}

	void clearRoster()
	{
		initialNames.clear();
		currentNames.clear();
		completionNames.clear();
		observedNames.clear();
	}

	RaidPresenceReport presenceReport(String state, int world, long now)
	{
		boolean finalState = "completion".equals(state) || "reward_observed".equals(state);
		ChambersScoringPolicy policy = ChambersScoringPolicy.evaluate(this);
		return new RaidPresenceReport(localName, challengeMode ? "COX_CM" : "COX", state,
			world, partyGroup, presenceEpoch, startedAt, now, maxReportedSize,
			finalState ? completionReportedSize : null,
			finalState ? finalPersonalPoints : null, finalState ? finalTeamPoints : null,
			policy.mode.name(), finalState ? completedAt : null,
			"reward_observed".equals(state) ? now : null);
	}

	int maxReportedSize() { return maxReportedSize; }
	boolean hasFinalPoints() { return completed && finalPointsCaptured; }
	int finalTeamPoints() { return finalTeamPoints; }
	int finalPersonalPoints() { return finalPersonalPoints; }
	boolean completionRosterRetained() { return completionRosterRetained; }

	GroupSnapshot initialSnapshot() { return snapshot(initialNames, initialReportedSize, "INITIAL_OBSERVED"); }
	GroupSnapshot currentSnapshot()
	{
		return snapshot(currentNames, currentReportedSize,
			lastRosterObservationVerified ? "CURRENT_OBSERVED" : "RETAINED_CURRENT");
	}
	GroupSnapshot completionSnapshot()
	{
		return snapshot(completionNames, completionReportedSize,
			completionRosterRetained ? "RETAINED_PRE_COMPLETION" : "COMPLETION");
	}

	GroupSnapshot snapshot()
	{
		GroupSnapshot base = completed
			&& (type == RaidType.COX || type == RaidType.TOA || type == RaidType.TOB)
			? completionSnapshot() : currentSnapshot();
		if (!completed || type != RaidType.COX) return base;
		return chambersRewardSnapshot();
	}

	GroupSnapshot chambersRewardSnapshot()
	{
		GroupSnapshot base = completed ? completionSnapshot() : currentSnapshot();
		ChambersScoringPolicy policy = ChambersScoringPolicy.evaluate(this);
		return new GroupSnapshot(base.source, base.names, base.expectedSize, base.status,
			base.detail, policy.eligible, policy.explanation, base.rosterState, policy.mode.name());
	}

	private GroupSnapshot snapshot(Collection<String> selectedNames, int reportedSize, String rosterState)
	{
		List<String> unique = GroupSnapshot.uniqueNames(selectedNames);
		boolean includesLocal = unique.stream().anyMatch(name -> name.equalsIgnoreCase(localName));
		String detail;
		GroupSnapshot.Status status = GroupSnapshot.Status.INCOMPLETE;
		if (!entryObserved)
		{
			detail = "Capture started after entry, or in spectator mode.";
		}
		else if (overflow)
		{
			detail = "Roster exceeded the local capture limit.";
		}
		else if (!includesLocal)
		{
			detail = "Your RSN was not found in the game roster.";
		}
		else if (reportedSize <= 0)
		{
			detail = "The game team-size signal was unavailable.";
		}
		else if (unique.size() != reportedSize)
		{
			detail = "Name count and team-size signal disagree.";
		}
		else
		{
			status = GroupSnapshot.Status.MATCHED;
			detail = "Includes you. Membership and bonuses not checked.";
		}
		if (completed && type == RaidType.COX)
		{
			List<String> departed = new java.util.ArrayList<>();
			for (String name : observedNames)
				if (unique.stream().noneMatch(current -> current.equalsIgnoreCase(name))) departed.add(name);
			if (!departed.isEmpty()) detail += " departed_before_completion=" + departed.size() + ".";
		}
		return new GroupSnapshot(type.title + " / " + type.rosterSource, unique, reportedSize,
			status, detail, null, null, rosterState, null);
	}
}
