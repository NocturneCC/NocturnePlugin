package com.nocturne;

import com.google.gson.JsonObject;
import java.util.Set;

/** Bounded receipt/rejection information safe to persist and show on a local card. */
final class SubmissionResult
{
	private static final Set<String> SAFE_REASONS = Set.of(
		"invalid_request", "rsn_not_allowlisted", "event_id_conflict",
		"content_length_required", "request_too_large", "json_required",
		"rate_limited", "intake_unavailable", "storage_capacity",
		"method_not_allowed", "route_not_found");

	final SubmissionStatus status;
	final Integer httpStatus;
	final String reason;

	SubmissionResult(SubmissionStatus status, Integer httpStatus, String reason)
	{
		this.status = status;
		this.httpStatus = httpStatus;
		this.reason = reason != null && SAFE_REASONS.contains(reason) ? reason : null;
	}

	static SubmissionResult of(SubmissionStatus status)
	{
		return new SubmissionResult(status, null, null);
	}

	static SubmissionResult rejected(int httpStatus, String reason)
	{
		return new SubmissionResult(SubmissionStatus.REJECTED, httpStatus, reason);
	}

	static String safeReason(JsonObject body)
	{
		if (body == null || !body.has("reason") || !body.get("reason").isJsonPrimitive()
			|| !body.getAsJsonPrimitive("reason").isString()) return null;
		String candidate = body.get("reason").getAsString();
		return SAFE_REASONS.contains(candidate) ? candidate : null;
	}

	static boolean isSafeReason(String reason) { return reason != null && SAFE_REASONS.contains(reason); }

	String displayLabel()
	{
		if (status == SubmissionStatus.ACCEPTED)
			return "Stored by test intake · pending review";
		if (status != SubmissionStatus.REJECTED || httpStatus == null)
			return status.label;
		String detail = reasonLabel(reason);
		return "Not accepted by test intake · HTTP " + httpStatus
			+ (detail == null ? "" : " · " + detail);
	}

	private static String reasonLabel(String reason)
	{
		if (reason == null) return null;
		switch (reason)
		{
			case "invalid_request": return "invalid request";
			case "rsn_not_allowlisted": return "RSN not on test allowlist";
			case "event_id_conflict": return "event ID conflict";
			case "content_length_required": return "missing content length";
			case "request_too_large": return "request too large";
			case "json_required": return "JSON required";
			case "rate_limited": return "rate limited";
			case "intake_unavailable": return "intake unavailable";
			case "storage_capacity": return "intake storage full";
			case "method_not_allowed": return "method not allowed";
			case "route_not_found": return "route not found";
			default: return null;
		}
	}
}
