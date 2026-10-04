package com.nocturne;

import com.google.gson.JsonObject;

/** Strict mapping of the documented, bounded automatic-observation response contract. */
final class ChallengeObservationResponse
{
	enum State { ACCEPTED, DUPLICATE, IGNORED, INVALID, CONFLICT, RATE_LIMITED, SERVER_FAILURE, PROTOCOL_ERROR }
	final State state;
	final String reason;
	ChallengeObservationResponse(State state, String reason) { this.state = state; this.reason = reason; }

	static ChallengeObservationResponse parse(int status, JsonObject json)
	{
		if (json == null || json.size() < 1 || json.size() > 2 || !json.has("state") || !json.get("state").isJsonPrimitive()
			|| !json.getAsJsonPrimitive("state").isString()) return new ChallengeObservationResponse(State.PROTOCOL_ERROR, null);
		String state = json.get("state").getAsString();
		if ("accepted".equals(state) && status == 201 && json.size() == 1) return new ChallengeObservationResponse(State.ACCEPTED, null);
		if ("duplicate".equals(state) && status == 200 && json.size() == 1) return new ChallengeObservationResponse(State.DUPLICATE, null);
		if ("ignored".equals(state) && status == 200 && json.has("reason") && json.size() == 2)
		{
			String reason = string(json, "reason");
			if ("disabled".equals(reason) || "manual_only".equals(reason) || "unconfigured".equals(reason)) return new ChallengeObservationResponse(State.IGNORED, reason);
		}
		if ("invalid".equals(state) && (status == 400 || status == 413 || status == 415 || status == 422)
			&& json.size() == 2 && json.has("reason") && string(json, "reason") != null)
			return new ChallengeObservationResponse(State.INVALID, null);
		if ("idempotency_conflict".equals(state) && status == 409 && json.size() == 1) return new ChallengeObservationResponse(State.CONFLICT, null);
		if ("rate_limited".equals(state) && status == 429 && json.size() == 1) return new ChallengeObservationResponse(State.RATE_LIMITED, null);
		if ("server_failure".equals(state) && status == 503 && json.size() == 1) return new ChallengeObservationResponse(State.SERVER_FAILURE, null);
		return new ChallengeObservationResponse(State.PROTOCOL_ERROR, null);
	}
	private static String string(JsonObject json, String key)
	{
		return json.get(key) != null && json.get(key).isJsonPrimitive() && json.getAsJsonPrimitive(key).isString()
			? json.get(key).getAsString() : null;
	}
}
