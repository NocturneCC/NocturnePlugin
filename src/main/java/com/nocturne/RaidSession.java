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
	private final Set<String> names = new LinkedHashSet<>();
	private final Set<String> rewardSignatures = new HashSet<>();
	private int maxReportedSize;
	private boolean completed;
	private boolean overflow;
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
		for (String name : GroupSnapshot.uniqueNames(observedNames))
		{
			if (names.stream().anyMatch(existing -> existing.equalsIgnoreCase(name))) continue;
			if (names.size() < 100)
			{
				names.add(name);
			}
			else
			{
				overflow = true;
			}
		}
		maxReportedSize = Math.max(maxReportedSize, reportedSize);
	}

	void finish()
	{
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
		names.clear();
		maxReportedSize = 0;
	}

	GroupSnapshot snapshot()
	{
		List<String> unique = GroupSnapshot.uniqueNames(names);
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
		else if (maxReportedSize <= 0)
		{
			detail = "The game team-size signal was unavailable.";
		}
		else if (unique.size() != maxReportedSize)
		{
			detail = "Name count and team-size signal disagree.";
		}
		else
		{
			status = GroupSnapshot.Status.MATCHED;
			detail = "Includes you. Membership and bonuses not checked.";
		}
		return new GroupSnapshot(type.title + " / " + type.rosterSource, unique, maxReportedSize, status, detail);
	}
}
