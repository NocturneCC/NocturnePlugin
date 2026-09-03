package com.nocturne;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import java.io.IOException;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.CopyOnWriteArrayList;
import okio.Buffer;
import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Protocol;
import okhttp3.Response;
import okhttp3.ResponseBody;
import org.junit.Test;
import static org.junit.Assert.*;

public class RaidPresenceServiceTest
{
	private static RaidPresenceReport report(String state, long time)
	{
		boolean complete = !"heartbeat".equals(state);
		return new RaidPresenceReport("De Lena", "COX", state, 420, 7,
			"11111111-1111-1111-1111-111111111111", time - 10, time, 3,
			complete ? 3 : null, complete ? 500 : null, complete ? 10_000 : null,
			"NORMAL_GROUP", complete ? time : null, null);
	}

	@Test public void payloadContainsOnlyOwnIdentityAndExactIntegerContribution()
	{
		String json = report("completion", 1_000).json().toString();
		assertTrue(json.contains("\"rsn\":\"De Lena\""));
		assertTrue(json.contains("\"contribution_basis_points\":500"));
		assertFalse(json.contains("Bifuor"));
		assertFalse(json.contains("Not ZB"));
		assertFalse(json.contains("game_roster"));
		assertFalse(json.contains("instance_observed"));
	}

	@Test public void firstHeartbeatBecomesEntryAndLaterHeartbeatsAreBounded() throws Exception
	{
		AtomicInteger calls = new AtomicInteger();
		CopyOnWriteArrayList<String> states = new CopyOnWriteArrayList<>();
		OkHttpClient http = client(calls, false, states);
		RaidPresenceService service = new RaidPresenceService(http, new Gson());
		try
		{
			CompletableFuture<RaidVerificationStatus> entry = new CompletableFuture<>();
			service.heartbeat(report("heartbeat", 1_000), entry::complete);
			entry.get(3, TimeUnit.SECONDS);
			service.heartbeat(report("heartbeat", 1_030), ignored -> { });
			service.heartbeat(report("heartbeat", 1_061), ignored -> { });
			awaitStates(states, 2);
			assertEquals(2, calls.get());
			assertEquals("entry", states.get(0));
			assertEquals("heartbeat", states.get(1));
		}
		finally { service.close(); close(http); }
	}

	@Test public void backendFailureIsReportedWithoutThrowingIntoLootFlow() throws Exception
	{
		AtomicInteger calls = new AtomicInteger();
		OkHttpClient http = client(calls, true, new CopyOnWriteArrayList<>());
		RaidPresenceService service = new RaidPresenceService(http, new Gson());
		CompletableFuture<RaidVerificationStatus> result = new CompletableFuture<>();
		try
		{
			service.submit(report("completion", 1_000), result::complete);
			assertSame(RaidVerificationStatus.UNAVAILABLE, result.get(3, TimeUnit.SECONDS));
		}
		finally { service.close(); close(http); }
	}

	@Test public void responseCanNeverEnableAwards()
	{
		JsonObject valid = new JsonObject();
		valid.addProperty("presence_version", 1); valid.addProperty("verified", 3);
		valid.addProperty("expected", 3); valid.addProperty("consistent", true);
		valid.addProperty("group_qualified", true); valid.addProperty("reason", "verified");
		valid.addProperty("automatic_awards_enabled", false); valid.addProperty("point_writes", 0);
		assertTrue(RaidPresenceService.parse(valid).groupQualified);
		valid.addProperty("automatic_awards_enabled", true);
		assertNull(RaidPresenceService.parse(valid));
	}

	private static OkHttpClient client(AtomicInteger calls, boolean fail,
		CopyOnWriteArrayList<String> states)
	{
		return new OkHttpClient.Builder().addInterceptor(chain ->
		{
			calls.incrementAndGet();
			Buffer requestBody = new Buffer();
			chain.request().body().writeTo(requestBody);
			states.add(new Gson().fromJson(requestBody.readUtf8(), JsonObject.class)
				.get("state").getAsString());
			if (fail) throw new IOException("offline");
			String body = "{\"presence_version\":1,\"verified\":1,\"expected\":3,"
				+ "\"consistent\":true,\"group_qualified\":false,"
				+ "\"automatic_awards_enabled\":false,\"point_writes\":0,"
				+ "\"reason\":\"partial\"}";
			return new Response.Builder().request(chain.request()).protocol(Protocol.HTTP_1_1)
				.code(201).message("Created").body(ResponseBody.create(
					MediaType.parse("application/json"), body)).build();
		}).build();
	}

	private static void awaitStates(CopyOnWriteArrayList<String> states, int expected) throws Exception
	{
		long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(3);
		while (states.size() < expected && System.nanoTime() < deadline) Thread.yield();
	}

	private static void close(OkHttpClient http)
	{
		http.dispatcher().executorService().shutdownNow(); http.connectionPool().evictAll();
	}
}
