package com.nocturne;

import com.google.gson.JsonObject;
import org.junit.Test;
import static org.junit.Assert.*;

public class ChallengeObservationResponseTest
{
	private ChallengeObservationResponse parse(int status, String json)
	{ return ChallengeObservationResponse.parse(status, json == null ? null : new com.google.gson.Gson().fromJson(json, JsonObject.class)); }
	@Test public void mapsAllDocumentedStatesStrictly()
	{
		assertEquals(ChallengeObservationResponse.State.ACCEPTED, parse(201, "{\"state\":\"accepted\"}").state);
		assertEquals(ChallengeObservationResponse.State.DUPLICATE, parse(200, "{\"state\":\"duplicate\"}").state);
		for (String reason : new String[]{"manual_only", "unconfigured", "disabled"})
			assertEquals(ChallengeObservationResponse.State.IGNORED, parse(200, "{\"state\":\"ignored\",\"reason\":\"" + reason + "\"}").state);
		assertEquals(ChallengeObservationResponse.State.INVALID, parse(422, "{\"state\":\"invalid\",\"reason\":\"policy\"}").state);
		assertEquals(ChallengeObservationResponse.State.CONFLICT, parse(409, "{\"state\":\"idempotency_conflict\"}").state);
		assertEquals(ChallengeObservationResponse.State.RATE_LIMITED, parse(429, "{\"state\":\"rate_limited\"}").state);
		assertEquals(ChallengeObservationResponse.State.SERVER_FAILURE, parse(503, "{\"state\":\"server_failure\"}").state);
		assertEquals(ChallengeObservationResponse.State.PROTOCOL_ERROR, parse(200, "{\"state\":\"accepted\"}").state);
		assertEquals(ChallengeObservationResponse.State.PROTOCOL_ERROR, parse(201, "{\"state\":\"accepted\",\"token\":\"x\"}").state);
	}
}
