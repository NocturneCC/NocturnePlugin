package com.nocturne;

import java.util.ArrayList;
import java.util.Collection;
import java.util.List;
import java.util.Locale;
import java.util.Objects;
import net.runelite.api.Client;
import net.runelite.api.GameState;
import net.runelite.api.Player;
import net.runelite.api.WorldType;
import net.runelite.api.WorldView;
import net.runelite.api.coords.WorldPoint;
import net.runelite.api.gameval.InterfaceID;
import net.runelite.api.gameval.VarbitID;
import net.runelite.api.gameval.VarClientID;
import net.runelite.api.gameval.VarPlayerID;
import net.runelite.api.widgets.Widget;
import net.runelite.client.util.Text;
import lombok.extern.slf4j.Slf4j;

/**
 * Local-only group capture. Raid source mapping is adapted from DropTracker's
 * NearbyPlayerTracker (BSD-2-Clause); see THIRD_PARTY_NOTICES.md.
 * All game reads and state mutations happen on the client thread.
 */
@Slf4j
final class GroupTracker
{
	private final Client client;
	private RaidSession session;
	private RaidType active;
	private boolean outsideObserved;
	private String character;
	private int world;
	private long tick;
	private int scanTicks;
	private long nextRunEpoch;
	private GroupSnapshot current = GroupSnapshot.unavailable("Enter a raid to preview its roster.");
	private RaidDiagnostics diagnostics = RaidDiagnostics.INACTIVE;
	private boolean diagnosticsEnabled;
	private boolean diagnosticsFrozen;
	private String lastStructure;

	GroupTracker(Client client)
	{
		this.client = client;
	}

	void reset()
	{
		session = null;
		active = null;
		outsideObserved = false;
		character = null;
		world = 0;
		scanTicks = 0;
		current = GroupSnapshot.unavailable("Enter a raid to preview its roster.");
		diagnostics = RaidDiagnostics.INACTIVE;
		diagnosticsFrozen = false;
		lastStructure = null;
	}

	void setDiagnosticsEnabled(boolean enabled)
	{
		diagnosticsEnabled = enabled;
		if (!enabled) diagnostics = RaidDiagnostics.INACTIVE;
	}

	void onTick()
	{
		tick++;
		if (client.getGameState() != GameState.LOGGED_IN || client.getLocalPlayer() == null)
		{
			return;
		}
		syncIdentity();
		String name = character;
		RaidType raid = activeRaid();
		if (raid == null)
		{
			if (active != null && session != null)
			{
				if (session.type != RaidType.COX) session.finish();
			}
			active = null;
			outsideObserved = true;
			if (session != null && session.expired(tick))
			{
				session = null;
			}
			if (session == null)
			{
				current = GroupSnapshot.unavailable("Enter a raid to preview its roster.");
			}
			else
			{
				GroupSnapshot saved = session.snapshot();
				current = new GroupSnapshot(saved.source, saved.names, saved.expectedSize, saved.status,
					"Retained for the reward chest. " + saved.detail);
			}
			return;
		}
		int partyGroup = raid == RaidType.COX
			? client.getVarpValue(VarPlayerID.RAIDS_PARTY_GROUPHOLDER) : -1;
		if (startsNewRaidSession(active, raid))
		{
			boolean spectator = raid == RaidType.TOB && client.getVarbitValue(VarbitID.TOB_CLIENT_PARTYSTATUS) == 3;
			session = new RaidSession(raid, name, outsideObserved && !spectator, tick,
				++nextRunEpoch, partyGroup);
			active = raid;
			outsideObserved = false;
			scanTicks = 5;
			diagnosticsFrozen = false;
		}
		else if (raid == RaidType.COX && session != null)
		{
			// The holder can change while a scouted party is assembled. It is a
			// session signal, not proof that the client missed entry into a new raid.
			session.updatePartyGroup(partyGroup);
		}
		if (++scanTicks >= 5)
		{
			scanTicks = 0;
			capture();
		}
		current = session.snapshot();
	}

	static boolean startsNewRaidSession(RaidType activeRaid, RaidType detectedRaid)
	{
		return activeRaid != detectedRaid;
	}

	GroupSnapshot current()
	{
		return current;
	}

	RaidDiagnostics diagnostics()
	{
		return diagnostics;
	}

	private RaidType activeRaid()
	{
		if (client.getVarbitValue(VarbitID.TOB_CLIENT_PARTYSTATUS) >= 2) return RaidType.TOB;
		if (client.getVarbitValue(VarbitID.TOA_CLIENT_PARTYSTATUS) >= 2) return RaidType.TOA;
		if (client.getVarbitValue(VarbitID.RAIDS_CLIENT_INDUNGEON) == 1) return RaidType.COX;
		return null;
	}

	private void capture()
	{
		if (session == null || active == null) return;
		List<String> names = new ArrayList<>();
		int size = 0;
		switch (active)
		{
			case TOB:
				readNames(VarClientID.TOB_CLIENT_NAME0, VarClientID.TOB_CLIENT_NAME4, names);
				size = countOccupied(VarbitID.TOB_CLIENT_P0, VarbitID.TOB_CLIENT_P4);
				break;
			case TOA:
				readNames(VarClientID.TOA_CLIENT_NAME0, VarClientID.TOA_CLIENT_NAME7, names);
				size = countOccupied(VarbitID.TOA_CLIENT_P0, VarbitID.TOA_CLIENT_P7);
				break;
			case COX:
				Widget list = client.getWidget(InterfaceID.RaidsSidepanel.LIST);
				size = client.getVarbitValue(VarbitID.RAIDS_CLIENT_PARTYSIZE);
				ChambersRoster.Observation observation = ChambersRoster.inspect(list, size);
				names.addAll(observation.names);
				if (shouldSampleDiagnostics(diagnosticsEnabled, diagnosticsFrozen))
				{
					String structure = ChambersRoster.structuralSummary(list,
						client.getWidget(InterfaceID.RaidsSidepanel.LISTLAYER),
						client.getWidget(InterfaceID.RaidsSidepanel.UNIVERSE));
					String mode = client.getVarbitValue(VarbitID.RAIDS_CHALLENGE_MODE) == 1
						? "Chambers of Xeric: Challenge Mode" : "Chambers of Xeric: Normal";
					GroupSnapshot snapshot = session.snapshot();
					diagnostics = new RaidDiagnostics(mode, size, session.partyGroup(),
						observation.candidateCount, observation.names.size(), snapshot.status.name(),
						structure + "\nChildren: " + observation.children);
					String diagnosticShape = structure + "|" + observation.children;
					if (!diagnosticShape.equals(lastStructure))
					{
						log.debug("Chambers roster widget structure: {}; children: {}",
							structure, observation.children);
						lastStructure = diagnosticShape;
					}
				}
				break;
		}
		session.observe(names, size, tick);
		if (shouldSampleDiagnostics(diagnosticsEnabled, diagnosticsFrozen) && active == RaidType.COX)
		{
			diagnostics = diagnostics.withSnapshotState(session.snapshot().status.name());
		}
	}

	static boolean shouldSampleDiagnostics(boolean enabled, boolean frozen)
	{
		return enabled && !frozen;
	}

	private void readNames(int first, int last, List<String> names)
	{
		for (int id = first; id <= last; id++) addName(names, client.getVarcStrValue(id));
	}

	private int countOccupied(int first, int last)
	{
		int size = 0;
		for (int id = first; id <= last; id++) if (client.getVarbitValue(id) > 0) size++;
		return size;
	}

	GroupSnapshot forLoot(String source)
	{
		syncIdentity();
		RaidType raid = RaidType.fromSource(source);
		if (raid != null)
		{
			if (session == null || session.type != raid || session.expired(tick))
			{
				return GroupSnapshot.unavailable("No retained roster for this raid reward.");
			}
			return session.snapshot();
		}
		if (active != null && session != null) return session.snapshot();
		return nearby();
	}

	boolean acceptRaidReward(String source, Collection<String> itemSignature)
	{
		syncIdentity();
		RaidType raid = RaidType.fromSource(source);
		if (raid == null || session == null || session.type != raid || session.expired(tick)) return true;
		List<String> sorted = new ArrayList<>(itemSignature);
		sorted.sort(String::compareTo);
		return session.acceptReward(String.join("|", sorted));
	}

	GroupSnapshot takeChambersReward(String source, Collection<String> itemSignature)
	{
		syncIdentity();
		int partyGroup = client.getVarpValue(VarPlayerID.RAIDS_PARTY_GROUPHOLDER);
		if (RaidType.fromSource(source) != RaidType.COX
			|| !isCurrentChambersReward(session, active, nextRunEpoch, partyGroup, tick)) return null;
		List<String> sorted = new ArrayList<>(itemSignature);
		sorted.sort(String::compareTo);
		if (!session.acceptReward(String.join("|", sorted))) return null;
		GroupSnapshot snapshot = session.chambersRewardSnapshot();
		diagnosticsFrozen = true;
		session.clearRoster();
		current = GroupSnapshot.unavailable("Chambers roster consumed by the reward event.");
		return snapshot;
	}

	void onGameMessage(String text)
	{
		syncIdentity();
		if (text == null) return;
		// Freeze the roster before reward-room state/party widgets are cleared.
		String message = Text.removeTags(text).toLowerCase(Locale.ROOT);
		if (session != null && session.type == RaidType.COX && isChambersCompletionMessage(message))
		{
			capture();
			session.finishChambers(client.getVarbitValue(VarbitID.RAIDS_CLIENT_PARTYSCORE),
				client.getVarpValue(VarPlayerID.RAIDS_PLAYERSCORE));
			current = session.snapshot();
			return;
		}
		if (session != null && message.startsWith("your ") && message.contains("count is")
			&& RaidType.fromSource(message) == session.type)
		{
			capture();
			session.finish();
			current = session.snapshot();
		}
	}

	static boolean isChambersCompletionMessage(String message)
	{
		return message != null && Text.removeTags(message).trim().toLowerCase(Locale.ROOT)
			.startsWith("congratulations - your raid is complete!");
	}

	static boolean isCurrentChambersReward(RaidSession candidate, RaidType active, long epoch,
		int currentPartyGroup, long tick)
	{
		return candidate != null && candidate.type == RaidType.COX && !candidate.expired(tick)
			&& candidate.isRun(epoch, candidate.partyGroup())
			&& (active == RaidType.COX || currentPartyGroup < 0
				|| currentPartyGroup == candidate.partyGroup());
	}

	private void syncIdentity()
	{
		Player local = client.getLocalPlayer();
		String name = local == null ? null : normalize(local.getName());
		if (!Objects.equals(character, name) || world != client.getWorld())
		{
			reset();
			character = name;
			world = client.getWorld();
		}
	}

	private GroupSnapshot nearby()
	{
		if (client.getVarbitValue(VarbitID.INSIDE_WILDERNESS) != 0
			|| client.getWorldType().contains(WorldType.PVP)
			|| client.getWorldType().contains(WorldType.DEADMAN)
			|| client.getWorldType().contains(WorldType.PVP_ARENA))
		{
			return GroupSnapshot.unavailable("Nearby capture is disabled in PvP areas/worlds.");
		}
		Player local = client.getLocalPlayer();
		if (local == null || local.getWorldLocation() == null || local.getWorldView() == null)
		{
			return GroupSnapshot.unavailable("Nearby players could not be read.");
		}
		WorldView view = local.getWorldView();
		WorldPoint center = local.getWorldLocation();
		List<String> names = new ArrayList<>();
		addName(names, local.getName());
		for (Player player : view.players())
		{
			if (player == null || player.getWorldView() != view) continue;
			WorldPoint point = player.getWorldLocation();
			if (point != null && point.getPlane() == center.getPlane() && center.distanceTo2D(point) <= 20)
			{
				addName(names, player.getName());
			}
			if (names.size() >= 100) break;
		}
		return new GroupSnapshot("Within 20 tiles at drop time", names, 0, GroupSnapshot.Status.OBSERVED,
			"Includes you. Presence does not prove participation or a complete team.");
	}

	private static void addName(List<String> names, String raw)
	{
		String name = normalize(raw);
		if (name != null) names.add(name);
	}

	private static String normalize(String raw)
	{
		if (raw == null) return null;
		String name = Text.toJagexName(Text.removeTags(raw)).trim();
		return name.isEmpty() || "-".equals(name) ? null : name;
	}
}
