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
import net.runelite.api.widgets.Widget;
import net.runelite.client.util.Text;

/**
 * Local-only group capture. Raid source mapping is adapted from DropTracker's
 * NearbyPlayerTracker (BSD-2-Clause); see THIRD_PARTY_NOTICES.md.
 * All game reads and state mutations happen on the client thread.
 */
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
	private GroupSnapshot current = GroupSnapshot.unavailable("Enter a raid to preview its roster.");

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
				session.finish();
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
		if (raid != active)
		{
			boolean spectator = raid == RaidType.TOB && client.getVarbitValue(VarbitID.TOB_CLIENT_PARTYSTATUS) == 3;
			session = new RaidSession(raid, name, outsideObserved && !spectator, tick);
			active = raid;
			outsideObserved = false;
			scanTicks = 5;
		}
		if (++scanTicks >= 5)
		{
			scanTicks = 0;
			capture();
		}
		current = session.snapshot();
	}

	GroupSnapshot current()
	{
		return current;
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
				if (list != null && list.getChildren() != null)
				{
					for (Widget child : list.getChildren())
					{
						if (child != null) addName(names, child.getName());
					}
				}
				size = client.getVarbitValue(VarbitID.RAIDS_CLIENT_PARTYSIZE);
				break;
		}
		session.observe(names, size, tick);
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

	void onGameMessage(String text)
	{
		syncIdentity();
		if (text == null) return;
		// Freeze the roster before reward-room state/party widgets are cleared.
		String message = Text.removeTags(text).toLowerCase(Locale.ROOT);
		if (session != null && message.startsWith("your ") && message.contains("count is")
			&& RaidType.fromSource(message) == session.type)
		{
			capture();
			session.finish();
			current = session.snapshot();
		}
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
