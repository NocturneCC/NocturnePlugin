package com.nocturne;

import com.google.gson.JsonNull;
import com.google.gson.JsonObject;
import java.util.UUID;

/** Own-client facts only. No teammate identity is represented by this type. */
final class RaidPresenceReport
{
	static final int VERSION = 1;
	final String checkinId = UUID.randomUUID().toString();
	final String rsn, raidType, state, raidEpoch, scoringMode;
	final int world, partyGroupHolder, maxScale;
	final long startedAt, observedAt;
	final Integer finalPartySize, personalPoints, teamPoints, contributionBasisPoints;
	final Long completionAt, rewardObservedAt;

	RaidPresenceReport(String rsn, String raidType, String state, int world, int partyGroupHolder,
		String raidEpoch, long startedAt, long observedAt, int maxScale, Integer finalPartySize,
		Integer personalPoints, Integer teamPoints, String scoringMode, Long completionAt,
		Long rewardObservedAt)
	{
		this.rsn = rsn; this.raidType = raidType; this.state = state; this.world = world;
		this.partyGroupHolder = partyGroupHolder; this.raidEpoch = raidEpoch;
		this.startedAt = startedAt; this.observedAt = observedAt; this.maxScale = maxScale;
		this.finalPartySize = finalPartySize; this.personalPoints = personalPoints;
		this.teamPoints = teamPoints; this.scoringMode = scoringMode;
		this.completionAt = completionAt; this.rewardObservedAt = rewardObservedAt;
		this.contributionBasisPoints = personalPoints == null || teamPoints == null || teamPoints <= 0
			? null : (int) Math.min(10_000L, Math.max(0L, personalPoints) * 10_000L / teamPoints);
	}

	JsonObject json()
	{
		JsonObject body = new JsonObject();
		body.addProperty("presence_version", VERSION); body.addProperty("checkin_id", checkinId);
		body.addProperty("rsn", rsn); body.addProperty("raid_type", raidType);
		body.addProperty("state", state); body.addProperty("world", world);
		body.addProperty("party_group_holder", partyGroupHolder); body.addProperty("raid_epoch", raidEpoch);
		body.addProperty("raid_started_at", startedAt); body.addProperty("observed_at", observedAt);
		body.addProperty("max_scale", maxScale); add(body, "final_party_size", finalPartySize);
		add(body, "final_personal_points", personalPoints); add(body, "final_team_points", teamPoints);
		add(body, "contribution_basis_points", contributionBasisPoints);
		body.addProperty("proposed_scoring_mode", scoringMode);
		add(body, "completion_at", completionAt); add(body, "reward_observed_at", rewardObservedAt);
		return body;
	}

	RaidPresenceReport withState(String nextState)
	{
		return new RaidPresenceReport(rsn, raidType, nextState, world, partyGroupHolder,
			raidEpoch, startedAt, observedAt, maxScale, null, null, null, scoringMode, null, null);
	}

	private static void add(JsonObject body, String key, Number value)
	{
		if (value == null) body.add(key, JsonNull.INSTANCE); else body.addProperty(key, value);
	}
}
