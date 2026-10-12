package com.nocturne;

import com.google.gson.JsonObject;
import java.util.Set;

/** Strict mapping of the documented, bounded automatic-observation response contract. */
final class ChallengeObservationResponse
{
	enum State { ACCEPTED, DUPLICATE, IGNORED, INVALID, CONFLICT, RATE_LIMITED, SERVER_FAILURE, PROTOCOL_ERROR }
	final State state;
	final String reason;
	final int httpStatus;
	private static final Set<String> SAFE_REASONS = Set.of(
		"invalid_schema", "unsupported_schema", "invalid_event_id", "invalid_reporter",
		"unsupported_activity", "unsupported_mode", "invalid_group_size", "incomplete_roster",
		"invalid_roster", "duplicate_participant", "reporter_not_in_roster", "invalid_duration",
		"missing_metric", "invalid_completion_count", "invalid_plugin_version", "invalid_timestamp",
		"timestamp_out_of_range", "unsupported_timing_policy", "selected_metric_missing",
		"unsupported_tier_policy", "duplicate_member_identity", "reporter_not_eligible",
		"request_too_large", "content_type_required", "invalid_json");

	ChallengeObservationResponse(State state, String reason) { this(state, reason, 0); }
	ChallengeObservationResponse(State state, String reason, int httpStatus)
	{ this.state = state; this.reason = reason; this.httpStatus = httpStatus; }

	static ChallengeObservationResponse parse(int status, JsonObject json)
	{
		if (json == null || json.size() < 1 || json.size() > 2 || !json.has("state") || !json.get("state").isJsonPrimitive()
			|| !json.getAsJsonPrimitive("state").isString()) return new ChallengeObservationResponse(State.PROTOCOL_ERROR, null, status);
		String state = json.get("state").getAsString();
		if ("accepted".equals(state) && status == 201 && json.size() == 1) return new ChallengeObservationResponse(State.ACCEPTED, null, status);
		if ("duplicate".equals(state) && status == 200 && json.size() == 1) return new ChallengeObservationResponse(State.DUPLICATE, null, status);
		if ("ignored".equals(state) && status == 200 && json.has("reason") && json.size() == 2)
		{
			String reason = string(json, "reason");
			if ("disabled".equals(reason) || "manual_only".equals(reason) || "unconfigured".equals(reason)) return new ChallengeObservationResponse(State.IGNORED, reason, status);
		}
		if ("invalid".equals(state) && (status == 400 || status == 413 || status == 415 || status == 422)
			&& json.size() == 2 && json.has("reason") && string(json, "reason") != null)
			return new ChallengeObservationResponse(State.INVALID, safeReason(string(json, "reason")), status);
		if ("idempotency_conflict".equals(state) && status == 409 && json.size() == 1) return new ChallengeObservationResponse(State.CONFLICT, null, status);
		if ("rate_limited".equals(state) && status == 429 && json.size() == 1) return new ChallengeObservationResponse(State.RATE_LIMITED, null, status);
		if ("server_failure".equals(state) && status == 503 && json.size() == 1) return new ChallengeObservationResponse(State.SERVER_FAILURE, null, status);
		return new ChallengeObservationResponse(State.PROTOCOL_ERROR, null, status);
	}
	private static String safeReason(String reason) { return SAFE_REASONS.contains(reason) ? reason : "unrecognized"; }
	private static String string(JsonObject json, String key)
	{
		return json.get(key) != null && json.get(key).isJsonPrimitive() && json.getAsJsonPrimitive(key).isString()
			? json.get(key).getAsString() : null;
	}
}
