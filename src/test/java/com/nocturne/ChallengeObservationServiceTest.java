package com.nocturne;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.time.Instant;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;
import java.util.concurrent.atomic.AtomicReference;
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
	@Test public void pluginTrackingResponseStatesMatchThePublishedRouteStatusContract()
	{
		assertEquals(ChallengeObservationResponse.State.ACCEPTED,
			ChallengeObservationResponse.parse(200, new Gson().fromJson("{\"state\":\"accepted\"}", JsonObject.class)).state);
		assertEquals(ChallengeObservationResponse.State.IGNORED,
			ChallengeObservationResponse.parse(200, new Gson().fromJson(
				"{\"state\":\"ignored\",\"reason\":\"unconfigured\"}", JsonObject.class)).state);
		assertEquals(ChallengeObservationResponse.State.SERVER_FAILURE,
			ChallengeObservationResponse.parse(500, new Gson().fromJson("{\"state\":\"server_failure\"}", JsonObject.class)).state);
	}

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
			assertEquals(observation().eventId, id);
			assertEquals(List.of(id, id), ids); assertEquals(bodies.get(0), bodies.get(1));
			assertEquals(ChallengeObservationResponse.State.ACCEPTED, results.get(0).state);
			assertEquals(List.of("POST", "POST"), methods);
		}
		finally { service.close(); scheduler.shutdownNow(); }
	}

	@Test public void phosaniPayloadUsesCanonicalSoloIdentityAndDeterministicEventId() throws Exception
	{
		GroupSnapshot solo = new GroupSnapshot("local reporter", List.of("Local Name"), 1,
			GroupSnapshot.Status.MATCHED, "solo encounter");
		ChallengeObservation observation = ChallengeObservation.createPhosani(358_200L, solo,
			"Local Name", Instant.parse("2026-10-12T12:00:00Z"), "pnm-encounter-fixture-1");
		assertNotNull(observation);
		java.util.concurrent.atomic.AtomicInteger calls = new java.util.concurrent.atomic.AtomicInteger();
		java.util.concurrent.atomic.AtomicReference<String> eventId = new java.util.concurrent.atomic.AtomicReference<>();
		ScheduledExecutorService scheduler = Executors.newSingleThreadScheduledExecutor();
		CountDownLatch finished = new CountDownLatch(1);
		CountDownLatch requestEntered = new CountDownLatch(1);
		CountDownLatch releaseRequest = new CountDownLatch(1);
		ChallengeObservationService service = new ChallengeObservationService(new OkHttpClient.Builder().addInterceptor(chain ->
		{
			assertEquals(ChallengeObservationService.ENDPOINT, chain.request().url().toString());
			assertNull(chain.request().header("Authorization"));
			okio.Buffer body = new okio.Buffer(); chain.request().body().writeTo(body);
			JsonObject payload = new Gson().fromJson(body.readUtf8(), JsonObject.class);
			assertEquals("phosanis_nightmare", payload.get("activity_key").getAsString());
			assertEquals("phosani_nightmare", payload.get("mode_key").getAsString());
			assertEquals(358_200L, payload.get("overall_time_ms").getAsLong());
			assertFalse(payload.has("room_time_ms"));
			assertEquals(1, payload.get("group_size").getAsInt());
			assertEquals(1, payload.getAsJsonArray("roster").size());
			eventId.set(payload.get("event_id").getAsString());
			calls.incrementAndGet();
			requestEntered.countDown();
			try { releaseRequest.await(); }
			catch (InterruptedException e) { Thread.currentThread().interrupt(); throw new java.io.IOException("interrupted"); }
			return response(chain.request(), 201, "{\"state\":\"accepted\"}");
		}).build(), new Gson(), scheduler);
		try
		{
			AtomicInteger callbacks = new AtomicInteger();
			assertEquals(observation.eventId, service.submit(observation, result -> { callbacks.incrementAndGet(); finished.countDown(); }));
			assertTrue(requestEntered.await(2, TimeUnit.SECONDS));
			assertEquals(observation.eventId, service.submit(observation, result -> callbacks.incrementAndGet()));
			releaseRequest.countDown();
			assertTrue(finished.await(3, TimeUnit.SECONDS));
			assertEquals(observation.eventId, eventId.get());
			assertEquals(1, calls.get());
			assertEquals(1, callbacks.get());
		}
		finally { releaseRequest.countDown(); service.close(); scheduler.shutdownNow(); }
	}

	@Test public void javaRequestBuilderMatchesSharedCrossLanguagePnmFixture() throws Exception
	{
		GroupSnapshot solo = new GroupSnapshot("local reporter", List.of("Doctor Arden"), 1,
			GroupSnapshot.Status.MATCHED, "solo encounter");
		ChallengeObservation observation = ChallengeObservation.createPhosani(308_400L, solo,
			"Doctor Arden", Instant.parse("2026-10-12T19:45:17Z"),
			"fixture-session|phosanis|doctor arden|fixture-encounter-1");
		assertNotNull(observation);
		String expected;
		try (InputStream fixture = getClass().getResourceAsStream("/pnm-automatic-observation.json"))
		{
			assertNotNull(fixture);
			expected = new String(fixture.readAllBytes(), StandardCharsets.UTF_8).trim();
		}
		AtomicReference<String> capturedBody = new AtomicReference<>();
		AtomicReference<String> contentType = new AtomicReference<>();
		CountDownLatch complete = new CountDownLatch(1);
		ScheduledExecutorService scheduler = Executors.newSingleThreadScheduledExecutor();
		ChallengeObservationService service = new ChallengeObservationService(new OkHttpClient.Builder().addInterceptor(chain ->
		{
			contentType.set(chain.request().body().contentType().toString());
			okio.Buffer requestBody = new okio.Buffer();
			chain.request().body().writeTo(requestBody);
			capturedBody.set(requestBody.readUtf8());
			return response(chain.request(), 201, "{\"state\":\"accepted\"}");
		}).build(), new Gson(), scheduler);
		try
		{
			assertNotNull(service.submit(observation, result -> complete.countDown()));
			assertTrue(complete.await(3, TimeUnit.SECONDS));
			assertEquals("application/json; charset=utf-8", contentType.get());
			assertEquals(expected, capturedBody.get());
		}
		finally { service.close(); scheduler.shutdownNow(); }
	}

	@Test public void nonChallengeTrackedActivityUsesPublicPluginTrackingRoute()
	{
		GroupSnapshot group = new GroupSnapshot("fixture group", List.of("Local Name", "Party Name"), 2,
			GroupSnapshot.Status.MATCHED, "fixture stable group");
		BossCompletionContext context = BossCompletionContext.fromTrackedActivity("The Nightmare",
			"Local Name", group, "session", 1, 1L, Instant.parse("2026-10-12T12:00:00Z"));
		ChallengeObservation observation = ChallengeObservation.createTracked(context, 372_340L);
		assertNotNull(observation);
		assertFalse(observation.challengeProjection);
		ScheduledExecutorService scheduler = Executors.newSingleThreadScheduledExecutor();
		CountDownLatch complete = new CountDownLatch(1);
		ChallengeObservationService service = new ChallengeObservationService(new OkHttpClient.Builder().addInterceptor(chain ->
		{
			assertEquals(ChallengeObservationService.PLUGIN_ENDPOINT, chain.request().url().toString());
			assertNull(chain.request().header("Authorization"));
			okio.Buffer body = new okio.Buffer(); chain.request().body().writeTo(body);
			JsonObject payload = new Gson().fromJson(body.readUtf8(), JsonObject.class);
			assertEquals("nightmare", payload.get("activity_key").getAsString());
			assertEquals(372_340L, payload.get("overall_time_ms").getAsLong());
			assertEquals(2, payload.get("group_size").getAsInt());
			return response(chain.request(), 200, "{\"state\":\"accepted\"}");
		}).build(), new Gson(), scheduler);
		try
		{
			assertNotNull(service.submit(observation, result -> complete.countDown()));
			assertTrue(complete.await(3, TimeUnit.SECONDS));
		}
		catch (InterruptedException exception)
		{
			Thread.currentThread().interrupt();
			fail("mock observation request interrupted");
		}
		finally { service.close(); scheduler.shutdownNow(); }
	}

	@Test public void validationResponseStatusAndAllowlistedReasonAreLoggedWithoutEnablingDiagnostics() throws Exception
	{
		AtomicLong clock = new AtomicLong(1_000_000_000L);
		CountDownLatch logged = new CountDownLatch(1);
		AtomicReference<String> diagnostic = new AtomicReference<>();
		ScheduledExecutorService scheduler = Executors.newSingleThreadScheduledExecutor();
		ChallengeObservationService service = new ChallengeObservationService(new OkHttpClient.Builder().addInterceptor(chain ->
			response(chain.request(), 422, "{\"state\":\"invalid\",\"reason\":\"reporter_not_eligible\"}")).build(),
			new Gson(), scheduler);
		ChallengeCompletionCapture capture = new ChallengeCompletionCapture(new ChallengeCompletionCorrelator(), service,
			message -> fail("validation response does not add user-facing clutter"), message ->
			{
				diagnostic.set(message);
				logged.countDown();
			}, clock::get);
		try
		{
			Instant now = Instant.parse("2026-10-12T19:45:17Z");
			capture.onBossCompletion("Phosani's Nightmare", "Doctor Arden", "session", 10, true, false, now);
			clock.set(1_100_000_000L);
			capture.onMessage("GAMEMESSAGE", "Fight duration: 5:08.40. Personal best: 4:48.60",
				null, "Doctor Arden", true, false, now, 11);
			clock.set(2_700_000_001L);
			capture.onTick(true, false, now);
			assertTrue(logged.await(3, TimeUnit.SECONDS));
			assertEquals("automatic Challenge observation response http_status=422 state=invalid reason=reporter_not_eligible",
				diagnostic.get());
		}
		finally { capture.close(); service.close(); scheduler.shutdownNow(); }
	}

	@Test public void encounterScopedDedupAllowsIdenticalSecondPnmKillAndStaleChatDoesNotResubmit() throws Exception
	{
		AtomicLong clock = new AtomicLong(1_000_000_000L);
		AtomicInteger calls = new AtomicInteger();
		List<String> ids = new CopyOnWriteArrayList<>();
		CountDownLatch completed = new CountDownLatch(2);
		CountDownLatch firstRequest = new CountDownLatch(1);
		ScheduledExecutorService scheduler = Executors.newSingleThreadScheduledExecutor();
		ChallengeObservationService service = new ChallengeObservationService(new OkHttpClient.Builder().addInterceptor(chain ->
		{
			assertEquals(ChallengeObservationService.ENDPOINT, chain.request().url().toString());
			okio.Buffer requestBody = new okio.Buffer();
			chain.request().body().writeTo(requestBody);
			JsonObject payload = new Gson().fromJson(requestBody.readUtf8(), JsonObject.class);
			assertEquals(358_200L, payload.get("overall_time_ms").getAsLong());
			assertEquals(1, payload.get("group_size").getAsInt());
			ids.add(payload.get("event_id").getAsString());
			calls.incrementAndGet();
			firstRequest.countDown();
			completed.countDown();
			return response(chain.request(), 201, "{\"state\":\"accepted\"}");
		}).build(), new Gson(), scheduler);
		ChallengeCompletionCapture capture = new ChallengeCompletionCapture(
			new ChallengeCompletionCorrelator(), service, message -> { }, message -> fail("diagnostics disabled"), clock::get);
		try
		{
			Instant now = Instant.parse("2026-10-12T12:00:00Z");
			clock.set(900_000_000L);
			capture.onMessage("GAMEMESSAGE", "Team size: Solo Fight duration: 5:58.20. Personal best: 4:48.60",
				null, "Local Name", true, false, now, 9);
			clock.set(1_000_000_000L);
			capture.onBossCompletion("Phosani's Nightmare", "Local Name", "session", 10, true, false, now);
			capture.onBossCompletion("Phosani's Nightmare", "Local Name", "session", 10, true, false, now);
			clock.set(1_400_000_000L);
			capture.onMessage("GAMEMESSAGE", "Team size: Solo Fight duration: 5:58.20. Personal best: 4:48.60",
				null, "Local Name", true, false, now, 11);
			clock.set(2_600_000_001L);
			capture.onTick(true, false, now);
			assertTrue(firstRequest.await(3, TimeUnit.SECONDS));

			// A replay without a new encounter, followed by the duplicate loot trigger,
			// must not resurrect the completed context.
			clock.set(3_000_000_000L);
			capture.onMessage("GAMEMESSAGE", "Team size: Solo Fight duration: 5:58.20. Personal best: 4:48.60",
				null, "Local Name", true, false, now, 12);
			capture.onBossCompletion("Phosani's Nightmare", "Local Name", "session", 10, true, false, now);
			clock.set(4_600_000_001L);
			capture.onTick(true, false, now);
			assertEquals(1, calls.get());

			// Same reporter, roster, boss, and duration, but a distinct encounter identity.
			clock.set(8_000_000_000L);
			capture.onBossCompletion("Phosani's Nightmare", "Local Name", "session", 20, true, false, now);
			clock.set(8_100_000_000L);
			capture.onMessage("GAMEMESSAGE", "Team size: Solo Fight duration: 5:58.20. Personal best: 4:48.60",
				null, "Local Name", true, false, now, 21);
			clock.set(9_700_000_001L);
			capture.onTick(true, false, now);
			assertTrue(completed.await(3, TimeUnit.SECONDS));
			assertEquals(2, calls.get());
			assertEquals(2, ids.size());
			assertNotEquals(ids.get(0), ids.get(1));
		}
		finally { capture.close(); service.close(); scheduler.shutdownNow(); }
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

	@Test public void failedCorrelationNeverContactsObservationEndpoint()
	{
		java.util.concurrent.atomic.AtomicInteger calls = new java.util.concurrent.atomic.AtomicInteger();
		ScheduledExecutorService scheduler = Executors.newSingleThreadScheduledExecutor();
		ChallengeObservationService service = new ChallengeObservationService(new OkHttpClient.Builder().addInterceptor(chain ->
		{
			calls.incrementAndGet();
			return response(chain.request(), 201, "{\"state\":\"accepted\"}");
		}).build(), new Gson(), scheduler);
		ChallengeCompletionCapture capture = new ChallengeCompletionCapture(new ChallengeCompletionCorrelator(), service,
			message -> fail("failed correlation has no user feedback"), message -> fail("diagnostics are disabled"));
		try
		{
			capture.onMessage("CLAN_CHAT", "Fight duration: 5:58.20", null, "Local Name", true,
				false, Instant.now(), 10);
			capture.onMessage("GAMEMESSAGE", "Fight duration: 5:58.20", null, "Local Name", true,
				false, Instant.now(), 11);
			assertEquals(0, calls.get());
			assertEquals(0, service.pendingCount());
		}
		finally { capture.close(); service.close(); scheduler.shutdownNow(); }
	}

	@Test public void disabledChallengeConsentDoesNotBufferOrSubmitPnm()
	{
		java.util.concurrent.atomic.AtomicInteger calls = new java.util.concurrent.atomic.AtomicInteger();
		ScheduledExecutorService scheduler = Executors.newSingleThreadScheduledExecutor();
		ChallengeObservationService service = new ChallengeObservationService(new OkHttpClient.Builder().addInterceptor(chain ->
		{
			calls.incrementAndGet();
			return response(chain.request(), 201, "{\"state\":\"accepted\"}");
		}).build(), new Gson(), scheduler);
		ChallengeCompletionCapture capture = new ChallengeCompletionCapture(new ChallengeCompletionCorrelator(), service,
			message -> fail("disabled consent is quiet"), message -> fail("diagnostics are disabled"));
		try
		{
			capture.onBossCompletion("Phosani's Nightmare", "Local Name", "session", 10,
				false, false, Instant.now());
			capture.onMessage("GAMEMESSAGE", "Fight duration: 5:58.20", null, "Local Name", false,
				false, Instant.now(), 11);
			assertEquals(0, calls.get());
			assertEquals(0, service.pendingCount());
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
			ChallengeObservation first = observation();
			ChallengeObservation second = ChallengeObservation.create("theatre_of_blood", "tob_duo", 901200L,
				1714070L, 42, new GroupSnapshot("fixture", List.of("Local Name", "Party Name"), 2,
					GroupSnapshot.Status.MATCHED, "fixture"), "Local Name", Instant.parse("2026-10-03T12:34:57Z"));
			service.submit(first, result -> { states.add(result.state); finished.countDown(); });
			service.submit(second, result -> { states.add(result.state); finished.countDown(); });
			assertTrue(finished.await(3, TimeUnit.SECONDS));
			assertEquals(2, calls.get());
			assertTrue(states.contains(ChallengeObservationResponse.State.INVALID));
			assertTrue(states.contains(ChallengeObservationResponse.State.RATE_LIMITED));
		}
		finally { service.close(); scheduler.shutdownNow(); }
	}

	@Test public void disabledManualOnlyAndUnconfiguredServerPoliciesAreTerminal() throws Exception
	{
		String[] reasons = {"disabled", "manual_only", "unconfigured"};
		java.util.concurrent.atomic.AtomicInteger calls = new java.util.concurrent.atomic.AtomicInteger();
		CountDownLatch finished = new CountDownLatch(reasons.length);
		List<ChallengeObservationResponse.State> states = new CopyOnWriteArrayList<>();
		ScheduledExecutorService scheduler = Executors.newSingleThreadScheduledExecutor();
		ChallengeObservationService service = new ChallengeObservationService(new OkHttpClient.Builder().addInterceptor(chain ->
		{
			int index = calls.getAndIncrement();
			return response(chain.request(), 200, "{\"state\":\"ignored\",\"reason\":\"" + reasons[index] + "\"}");
		}).build(), new Gson(), scheduler);
		try
		{
			for (int i = 0; i < reasons.length; i++)
			{
				ChallengeObservation item = ChallengeObservation.create("theatre_of_blood", "tob_duo", 901200L,
					1714070L, null, new GroupSnapshot("fixture", List.of("Local Name", "Party Name"), 2,
						GroupSnapshot.Status.MATCHED, "fixture"), "Local Name", Instant.parse("2026-10-03T12:40:0" + i + "Z"));
				service.submit(item, result -> { states.add(result.state); finished.countDown(); });
			}
			assertTrue(finished.await(3, TimeUnit.SECONDS));
			assertEquals(3, calls.get());
			assertEquals(3, states.stream().filter(s -> s == ChallengeObservationResponse.State.IGNORED).count());
		}
		finally { service.close(); scheduler.shutdownNow(); }
	}
}
