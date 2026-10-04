package com.nocturne;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import java.time.Instant;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import okhttp3.Interceptor;
import okhttp3.OkHttpClient;
import okhttp3.Protocol;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.ResponseBody;
import org.junit.Test;
import static org.junit.Assert.*;

public class ChallengeObservationServiceTest
{
	private static ChallengeObservation observation()
	{
		GroupSnapshot roster = new GroupSnapshot("fixture", List.of("Local Name", "Party Name"), 2,
			GroupSnapshot.Status.MATCHED, "fixture");
		return ChallengeObservation.create("theatre_of_blood", "tob_duo", 901200L, 1714070L, 42,
			roster, "Local Name", Instant.parse("2026-10-03T12:34:56Z"));
	}
	private static Response response(Request request, int code, String body)
	{
		return new Response.Builder().request(request).protocol(Protocol.HTTP_1_1).code(code).message("fixture")
			.body(ResponseBody.create(okhttp3.MediaType.parse("application/json"), body)).build();
	}

	@Test public void retryReusesExactUuidAndPayloadAndDoesNotContactNetwork() throws Exception
	{
		List<String> bodies = new CopyOnWriteArrayList<>(); List<String> ids = new CopyOnWriteArrayList<>();
		List<String> methods = new CopyOnWriteArrayList<>();
		java.util.concurrent.atomic.AtomicInteger count = new java.util.concurrent.atomic.AtomicInteger();
		Interceptor interceptor = chain ->
		{
			Request request = chain.request();
			assertEquals(ChallengeObservationService.ENDPOINT, request.url().toString());
			methods.add(request.method());
			assertNull(request.header("Authorization"));
			assertEquals("application/json; charset=utf-8", request.body().contentType().toString());
			okio.Buffer buffer = new okio.Buffer(); request.body().writeTo(buffer); bodies.add(buffer.readUtf8());
			JsonObject body = new Gson().fromJson(bodies.get(bodies.size() - 1), JsonObject.class);
			ids.add(body.get("event_id").getAsString());
			assertEquals(1, body.get("schema_version").getAsInt());
			assertEquals("theatre_of_blood", body.get("activity_key").getAsString());
			assertTrue(body.has("room_time_ms")); assertTrue(body.has("overall_time_ms"));
			assertFalse(body.has("pb")); assertFalse(body.has("discord_id")); assertFalse(body.has("chat"));
			return response(request, count.getAndIncrement() == 0 ? 503 : 201,
				count.get() == 1 ? "{\"state\":\"server_failure\"}" : "{\"state\":\"accepted\"}");
		};
		ScheduledExecutorService scheduler = Executors.newSingleThreadScheduledExecutor();
		CountDownLatch finished = new CountDownLatch(1);
		List<ChallengeObservationResponse> results = new CopyOnWriteArrayList<>();
		ChallengeObservationService service = new ChallengeObservationService(new OkHttpClient.Builder().addInterceptor(interceptor).build(), new Gson(), scheduler);
		try
		{
			String id = service.submit(observation(), result -> { results.add(result); finished.countDown(); });
			assertNotNull(id); assertTrue(finished.await(6, TimeUnit.SECONDS));
			java.util.UUID.fromString(id);
			assertEquals(List.of(id, id), ids); assertEquals(bodies.get(0), bodies.get(1));
			assertEquals(ChallengeObservationResponse.State.ACCEPTED, results.get(0).state);
			assertEquals(List.of("POST", "POST"), methods);
		}
		finally { service.close(); scheduler.shutdownNow(); }
	}

	@Test public void shutdownCancelsPendingCall()
	{
		java.util.concurrent.CountDownLatch entered = new java.util.concurrent.CountDownLatch(1);
		java.util.concurrent.CountDownLatch release = new java.util.concurrent.CountDownLatch(1);
		Interceptor slow = chain -> { entered.countDown(); try { release.await(); } catch (InterruptedException ignored) { Thread.currentThread().interrupt(); }
			return response(chain.request(), 201, "{\"state\":\"accepted\"}"); };
		ScheduledExecutorService scheduler = Executors.newSingleThreadScheduledExecutor();
		ChallengeObservationService service = new ChallengeObservationService(new OkHttpClient.Builder().addInterceptor(slow).build(), new Gson(), scheduler);
		try
		{
			service.submit(observation(), result -> fail("cancelled submission must not complete"));
			assertTrue(entered.await(2, TimeUnit.SECONDS)); service.close(); release.countDown(); assertEquals(0, service.pendingCount());
		}
		catch (InterruptedException e) { Thread.currentThread().interrupt(); fail(e.getClass().getSimpleName()); }
		finally { service.close(); scheduler.shutdownNow(); }
	}

	@Test public void shutdownCancelsScheduledRetry() throws Exception
	{
		java.util.concurrent.atomic.AtomicInteger calls = new java.util.concurrent.atomic.AtomicInteger();
		CountDownLatch firstResponse = new CountDownLatch(1);
		ScheduledExecutorService scheduler = Executors.newSingleThreadScheduledExecutor();
		ChallengeObservationService service = new ChallengeObservationService(new OkHttpClient.Builder().addInterceptor(chain ->
		{
			calls.incrementAndGet(); firstResponse.countDown();
			return response(chain.request(), 503, "{\"state\":\"server_failure\"}");
		}).build(), new Gson(), scheduler);
		try
		{
			service.submit(observation(), result -> fail("closed retry must not complete"));
			assertTrue(firstResponse.await(2, TimeUnit.SECONDS));
			long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(2);
			while (!service.hasScheduledRetry() && System.nanoTime() < deadline) Thread.sleep(5);
			assertTrue(service.hasScheduledRetry());
			service.close(); Thread.sleep(1100);
			assertEquals(1, calls.get()); assertEquals(0, service.pendingCount());
		}
		finally { service.close(); scheduler.shutdownNow(); }
	}

	@Test public void disabledConsentDoesNotSubmit()
	{
		java.util.concurrent.atomic.AtomicInteger calls = new java.util.concurrent.atomic.AtomicInteger();
		ScheduledExecutorService scheduler = Executors.newSingleThreadScheduledExecutor();
		ChallengeObservationService service = new ChallengeObservationService(new OkHttpClient.Builder().addInterceptor(chain ->
		{ calls.incrementAndGet(); return response(chain.request(), 201, "{\"state\":\"accepted\"}"); }).build(), new Gson(), scheduler);
		ChallengeCompletionCapture capture = new ChallengeCompletionCapture(new ChallengeCompletionCorrelator(), service,
			message -> fail("no feedback expected"), message -> fail("no diagnostics expected"));
		GroupSnapshot group = new GroupSnapshot("fixture", List.of("Local Name", "Party Name"), 2,
			GroupSnapshot.Status.MATCHED, "fixture");
		RaidCompletionEvidence evidence = new RaidCompletionEvidence(RaidType.TOB, 11, false, true, group);
		try
		{
			capture.onMessage("Theatre of Blood completion time: 15:01.20", evidence, "Local Name", false, false, Instant.now());
			capture.onMessage("Theatre of Blood total completion time: 28:34.07", evidence, "Local Name", false, false, Instant.now());
			capture.onMessage("Your completed Theatre of Blood count is: 42", evidence, "Local Name", false, false, Instant.now());
			assertEquals(0, calls.get()); assertEquals(0, service.pendingCount());
		}
		finally { capture.close(); service.close(); scheduler.shutdownNow(); }
	}

	@Test public void invalidAndRateLimitedResponsesAreNotRetried() throws Exception
	{
		java.util.concurrent.atomic.AtomicInteger calls = new java.util.concurrent.atomic.AtomicInteger();
		ScheduledExecutorService scheduler = Executors.newSingleThreadScheduledExecutor();
		CountDownLatch finished = new CountDownLatch(2);
		List<ChallengeObservationResponse.State> states = new CopyOnWriteArrayList<>();
		ChallengeObservationService service = new ChallengeObservationService(new OkHttpClient.Builder().addInterceptor(chain ->
		{
			int n = calls.getAndIncrement();
			return n == 0 ? response(chain.request(), 422, "{\"state\":\"invalid\",\"reason\":\"unsupported\"}")
				: response(chain.request(), 429, "{\"state\":\"rate_limited\"}");
		}).build(), new Gson(), scheduler);
		try
		{
			service.submit(observation(), result -> { states.add(result.state); finished.countDown(); });
			service.submit(observation(), result -> { states.add(result.state); finished.countDown(); });
			assertTrue(finished.await(3, TimeUnit.SECONDS));
			assertEquals(2, calls.get());
			assertTrue(states.contains(ChallengeObservationResponse.State.INVALID));
			assertTrue(states.contains(ChallengeObservationResponse.State.RATE_LIMITED));
		}
		finally { service.close(); scheduler.shutdownNow(); }
	}
}
