package com.nocturne;

import java.util.Collection;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/** Client-thread state for one run. Old loot records hold immutable snapshots. */
final class RaidSession
{
	static final long RETENTION_TICKS = 1000; // About ten minutes after the last active tick.
	final RaidType type;
	final long runEpoch;
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
	private boolean completed;
	private boolean overflow;
	private boolean finalPointsCaptured;
	private int finalTeamPoints;
	private int finalPersonalPoints;
	private long lastActiveTick;

	RaidSession(RaidType type, String localName, boolean entryObserved, long tick)
	{
		this(type, localName, entryObserved, tick, 0, -1);
	}

	RaidSession(RaidType type, String localName, boolean entryObserved, long tick,
		long runEpoch, int partyGroup)
	{
		this.type = type;
		this.runEpoch = runEpoch;
		this.partyGroup = partyGroup;
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
		if (!unique.isEmpty())
		{
			if (type == RaidType.COX) currentNames.clear();
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
			currentReportedSize = type == RaidType.COX ? reportedSize : maxReportedSize;
		}
	}

	void finish()
	{
		completed = true;
	}

	void finishChambers(int teamPoints, int personalPoints)
	{
		if (completed) return;
		completionNames.clear();
		completionNames.addAll(currentNames);
		completionReportedSize = currentReportedSize;
		finalTeamPoints = teamPoints;
		finalPersonalPoints = Math.max(0, personalPoints);
		finalPointsCaptured = teamPoints > 0 && personalPoints >= 0;
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
		maxReportedSize = 0;
	}

	int maxReportedSize() { return maxReportedSize; }
	boolean hasFinalPoints() { return completed && finalPointsCaptured; }
	int finalTeamPoints() { return finalTeamPoints; }
	int finalPersonalPoints() { return finalPersonalPoints; }

	GroupSnapshot initialSnapshot() { return snapshot(initialNames, initialReportedSize); }
	GroupSnapshot currentSnapshot() { return snapshot(currentNames, currentReportedSize); }
	GroupSnapshot completionSnapshot() { return snapshot(completionNames, completionReportedSize); }

	GroupSnapshot snapshot()
	{
		GroupSnapshot base = completed && type == RaidType.COX ? completionSnapshot() : currentSnapshot();
		if (!completed || type != RaidType.COX) return base;
		return chambersRewardSnapshot();
	}

	GroupSnapshot chambersRewardSnapshot()
	{
		GroupSnapshot base = completed ? completionSnapshot() : currentSnapshot();
		ChambersScoringPolicy policy = ChambersScoringPolicy.evaluate(this);
		return new GroupSnapshot(base.source, base.names, base.expectedSize, base.status,
			base.detail, policy.eligible, policy.explanation);
	}

	private GroupSnapshot snapshot(Collection<String> selectedNames, int reportedSize)
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
		return new GroupSnapshot(type.title + " / " + type.rosterSource, unique, reportedSize, status, detail);
	}
}
