package com.nocturne;

import com.google.gson.Gson;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.net.InetAddress;
import java.net.UnknownHostException;
import java.time.Clock;
import java.time.Instant;
import java.time.ZoneOffset;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import okhttp3.Call;
import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Protocol;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.ResponseBody;
import okio.Buffer;
import okio.BufferedSource;
import okio.Okio;
import okio.Source;
import okio.Timeout;
import org.junit.Test;
import static org.junit.Assert.*;

public class AnnouncementServiceTest
{
	private static final Instant NOW = Instant.parse("2026-09-28T20:00:00Z");

	@Test public void startupIsNonBlockingAndRequestHasNoIdentityOrBody() throws Exception
	{
		CompletableFuture<Request> requested = new CompletableFuture<>();
		Harness harness = harness(chain ->
		{
			requested.complete(chain.request());
			return response(chain.request(), 200, valid(1, "Public clan news"), "\"" + "a".repeat(64) + "\"");
		});
		try
		{
			long before = System.nanoTime();
			harness.service.start();
			assertTrue(TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - before) < 250);
			Request request = requested.get(3, TimeUnit.SECONDS);
			await(() -> harness.messages.size() == 1);
			assertEquals("GET", request.method());
			assertNull(request.body());
			assertEquals(AnnouncementService.ENDPOINT, request.url().toString());
			assertNull(request.url().query());
			String headers = request.headers().toString().toLowerCase();
			for (String forbidden : List.of("rsn", "account", "profile", "chat", "raid",
				"telemetry", "receipt")) assertFalse(headers.contains(forbidden));
			assertEquals("Clan notice", harness.messages.get(0).title);
			assertEquals("Public clan news", harness.messages.get(0).message);
			String persisted = Files.readString(harness.state);
			assertFalse(persisted.contains("Public clan news"));
			assertFalse(persisted.toLowerCase().contains("rsn"));
		}
		finally { harness.close(); }
	}

	@Test public void pollingIsBoundedJitteredAndUsesAnIsolatedFiveSecondClient()
		throws Exception
	{
		Harness harness = harness(chain -> response(chain.request(), 404, "{}", null));
		try
		{
			assertEquals(TimeUnit.SECONDS.toMillis(5), harness.service.httpForTest().callTimeoutMillis());
			assertFalse(harness.service.httpForTest().followRedirects());
			assertFalse(harness.service.httpForTest().followSslRedirects());
			assertFalse(harness.service.httpForTest().retryOnConnectionFailure());
			assertNotSame(harness.base.dispatcher(), harness.service.httpForTest().dispatcher());
			long interval = AnnouncementService.POLL_INTERVAL_MILLIS;
			long jitter = AnnouncementService.JITTER_MILLIS;
			for (long random : new long[] {Long.MIN_VALUE, -1, 0, 1, Long.MAX_VALUE})
			{
				long delay = AnnouncementService.nextDelay(interval, jitter, random);
				assertTrue(delay >= interval - jitter && delay <= interval + jitter);
			}
		}
		finally { harness.close(); }
	}

	@Test public void statusesFailOpenWithoutRetryStormOrFeatureSideEffects() throws Exception
	{
		for (int code : new int[] {304, 404, 429, 500, 503})
		{
			AtomicInteger calls = new AtomicInteger();
			Harness harness = harness(chain ->
			{
				calls.incrementAndGet();
				return response(chain.request(), code, code == 304 ? "" : "{}", null);
			});
			try
			{
				harness.service.pollNowForTest();
				await(() -> calls.get() == 1 && !harness.service.inFlightForTest());
				harness.worker.submit(() -> { }).get(3, TimeUnit.SECONDS);
				assertEquals(1, calls.get());
				assertTrue(harness.messages.isEmpty());
				assertTrue(harness.sidebars.isEmpty());
				LootRecord loot = new LootRecord("Tester", "Man",
					List.of(new LootItem(526, 1, "Bones", 32)));
				assertEquals(4, SubmissionService.payload(loot).get("version").getAsInt());
			}
			finally { harness.close(); }
		}
	}

	@Test public void connectionFailureDoesNotAutomaticallyRetry() throws Exception
	{
		AtomicInteger calls = new AtomicInteger();
		Harness harness = harness(chain ->
		{
			calls.incrementAndGet();
			throw new IOException("offline");
		});
		try
		{
			harness.service.pollNowForTest();
			await(() -> calls.get() == 1 && !harness.service.inFlightForTest());
			harness.worker.submit(() -> { }).get(3, TimeUnit.SECONDS);
			assertEquals(1, calls.get());
			assertTrue(harness.messages.isEmpty());
		}
		finally { harness.close(); }
	}

	@Test public void malformedOversizedUnsupportedAndUnsafeResponsesAreIgnored() throws Exception
	{
		List<String> invalid = new ArrayList<>();
		invalid.add("not-json");
		invalid.add(valid(1, "<img=4>"));
		invalid.add(valid(1, "[click](https://evil.example/)"));
		invalid.add(valid(1, "safe").replace("\"schema_version\":1", "\"schema_version\":2"));
		invalid.add(valid(1, "safe").replace("\"severity\":\"notice\"", "\"severity\":\"other\""));
		invalid.add(valid(1, "safe").replace("\"schema_version\":1,",
			"\"schema_version\":1,\"schema_version\":1,"));
		invalid.add(valid(1, "safe").replace("\"revision\":1,", "\"revision\":1.5,"));
		invalid.add(valid(1, "safe").replace("\"revision\":1,",
			"\"revision\":9223372036854775808,"));
		invalid.add(valid(1, "safe").replace("\"message\":\"safe\"", "\"message\":\"safe\\u202e\""));
		invalid.add(valid(1, "safe").replace("\"message\":\"safe\"", "\"message\":\"safe\\u2028line\""));
		invalid.add(valid(1, "safe").replace("\"message\":\"safe\"", "\"message\":\"safe\\ud800\""));
		invalid.add(valid(1, "safe").replace("\"message\":\"safe\"", "\"message\":123"));
		invalid.add(valid(1, "safe").replace("\"severity\":\"notice\"", "\"severity\":true"));
		invalid.add(valid(1, "safe").replace("\"link\":null",
			"\"link\":{\"label\":\"Bad\",\"url\":\"https://evil.example/\"}"));
		invalid.add(valid(1, "safe").replace("\"expires_at\":\"2026-09-28T21:00:00Z\"",
			"\"expires_at\":\"2026-09-28T19:00:00Z\""));
		for (String raw : invalid)
		{
			withInvalid(raw);
		}
		withInvalid("x".repeat(AnnouncementService.MAX_RESPONSE_BYTES + 1));
		try
		{
			AnnouncementService.decodeUtf8(new byte[] {(byte) 0xc3, 0x28});
			fail("invalid UTF-8 must be rejected");
		}
		catch (java.nio.charset.CharacterCodingException expected) { }
	}

	@Test public void idRevisionDedupeDisplaysNewAndRevisedAnnouncementsExactlyOnce() throws Exception
	{
		AtomicInteger calls = new AtomicInteger();
		Harness harness = harness(chain ->
		{
			int call = calls.getAndIncrement();
			int revision = call < 2 ? 1 : 2;
			return response(chain.request(), 200, valid(revision, "Revision " + revision),
				"\"" + String.valueOf(revision).repeat(64) + "\"");
		});
		try
		{
			for (int expected = 1; expected <= 4; expected++)
			{
				harness.service.pollNowForTest();
				final int count = expected;
				await(() -> calls.get() == count && !harness.service.inFlightForTest());
			}
			assertEquals(2, harness.messages.size());
			assertEquals("Revision 1", harness.messages.get(0).message);
			assertEquals("Revision 2", harness.messages.get(1).message);
			assertEquals(4, harness.sidebars.size());
		}
		finally { harness.close(); }
	}

	@Test public void onlyOneCallRunsAndShutdownCancelsOutstandingWork() throws Exception
	{
		AtomicInteger calls = new AtomicInteger();
		AtomicReference<Call> captured = new AtomicReference<>();
		CompletableFuture<Void> entered = new CompletableFuture<>();
		CompletableFuture<Void> release = new CompletableFuture<>();
		Harness harness = harness(chain ->
		{
			calls.incrementAndGet();
			captured.set(chain.call());
			entered.complete(null);
			try { release.get(3, TimeUnit.SECONDS); }
			catch (Exception error) { throw new IOException("cancelled", error); }
			throw new IOException("offline");
		});
		harness.service.pollForTest();
		harness.service.pollForTest();
		entered.get(3, TimeUnit.SECONDS);
		assertEquals(1, calls.get());
		harness.service.close();
		assertTrue(captured.get().isCanceled());
		release.complete(null);
		Thread.sleep(50);
		assertTrue(harness.messages.isEmpty());
		assertTrue(harness.sidebars.isEmpty());
		harness.closeBase();
	}

	@Test public void shutdownDuringDnsCannotPublishCallbacks() throws Exception
	{
		CompletableFuture<Void> entered = new CompletableFuture<>();
		CompletableFuture<Void> release = new CompletableFuture<>();
		OkHttpClient base = new OkHttpClient.Builder().dns(hostname ->
		{
			entered.complete(null);
			try { release.get(3, TimeUnit.SECONDS); }
			catch (Exception error) { throw new UnknownHostException("cancelled"); }
			return List.of(InetAddress.getLoopbackAddress());
		}).build();
		ScheduledExecutorService worker = Executors.newSingleThreadScheduledExecutor();
		List<Announcement> messages = new CopyOnWriteArrayList<>();
		List<List<Announcement>> sidebars = new CopyOnWriteArrayList<>();
		Path state = Files.createTempDirectory("nocturne-announcement-dns").resolve("state.json");
		AnnouncementService service = new AnnouncementService(base, new Gson(), state,
			messages::add, sidebars::add, worker, true, Clock.fixed(NOW, ZoneOffset.UTC),
			() -> 0, 0, 1000, 0);
		service.pollNowForTest();
		entered.get(3, TimeUnit.SECONDS);
		service.close();
		release.complete(null);
		Thread.sleep(50);
		assertTrue(messages.isEmpty());
		assertTrue(sidebars.isEmpty());
		base.dispatcher().executorService().shutdownNow();
		base.connectionPool().evictAll();
	}

	@Test public void shutdownDuringStreamingBodyCannotPublishCallbacks() throws Exception
	{
		CompletableFuture<Void> entered = new CompletableFuture<>();
		CompletableFuture<Void> release = new CompletableFuture<>();
		Harness harness = harness(chain -> new Response.Builder().request(chain.request())
			.protocol(Protocol.HTTP_1_1).code(200).message("test").body(new ResponseBody()
			{
				private final BufferedSource source = Okio.buffer(new Source()
				{
					private boolean sent;
					@Override public long read(Buffer sink, long count) throws IOException
					{
						if (sent) return -1;
						entered.complete(null);
						try { release.get(3, TimeUnit.SECONDS); }
						catch (Exception error) { throw new IOException("cancelled", error); }
						byte[] raw = valid(1, "body").getBytes(java.nio.charset.StandardCharsets.UTF_8);
						sink.write(raw);
						sent = true;
						return raw.length;
					}
					@Override public Timeout timeout() { return Timeout.NONE; }
					@Override public void close() { }
				});
				@Override public MediaType contentType() { return MediaType.parse("application/json"); }
				@Override public long contentLength() { return -1; }
				@Override public BufferedSource source() { return source; }
			}).build());
		harness.service.pollNowForTest();
		entered.get(3, TimeUnit.SECONDS);
		harness.service.close();
		release.complete(null);
		Thread.sleep(50);
		assertTrue(harness.messages.isEmpty());
		assertTrue(harness.sidebars.isEmpty());
		harness.closeBase();
	}

	@Test public void repeatedStartStopAndRejectedSchedulingFailOpen() throws Exception
	{
		ScheduledExecutorService rejected = Executors.newSingleThreadScheduledExecutor();
		rejected.shutdownNow();
		OkHttpClient base = new OkHttpClient();
		Path state = Files.createTempDirectory("nocturne-announcement-rejected").resolve("state.json");
		AnnouncementService service = new AnnouncementService(base, new Gson(), state,
			announcement -> fail("unexpected announcement"), values -> fail("unexpected sidebar"), rejected,
			false, Clock.fixed(NOW, ZoneOffset.UTC), () -> 0, 0, 1000, 0);
		service.start();
		service.start();
		service.close();
		service.close();
		base.dispatcher().executorService().shutdownNow();
		base.connectionPool().evictAll();
	}

	@Test public void parserAcceptsOnlyBoundedActivePlaintextAndAllowlistedLinks()
	{
		List<Announcement> parsed = AnnouncementService.parse(valid(1, "one\ntwo\nthree\nfour").replace(
			"\"link\":null", "\"link\":{\"label\":\"Event board\","
				+ "\"url\":\"https://nocturne.events/event-board.html\"}"), NOW);
		assertEquals(1, parsed.size());
		assertEquals("https://nocturne.events/event-board.html", parsed.get(0).linkUrl);
		assertEquals(10_001, AnnouncementService.nextDelay(TimeUnit.MINUTES.toMillis(15), 0, 0,
			NOW, List.of(new Announcement("notice", 1, null, "Message", "notice", NOW.minusSeconds(1),
				NOW.plusSeconds(10), null, null))));
	}

	private static void withInvalid(String raw) throws Exception
	{
		Harness harness = harness(chain -> response(chain.request(), 200, raw, null));
		try
		{
			harness.service.pollNowForTest();
			await(() -> !harness.service.inFlightForTest());
			assertTrue(harness.messages.isEmpty());
			assertTrue(harness.sidebars.isEmpty());
		}
		finally { harness.close(); }
	}

	private static Harness harness(okhttp3.Interceptor interceptor) throws Exception
	{
		return new Harness(interceptor);
	}

	private static Response response(Request request, int code, String body, String etag)
	{
		Response.Builder builder = new Response.Builder().request(request).protocol(Protocol.HTTP_1_1)
			.code(code).message("test").body(ResponseBody.create(
				MediaType.parse("application/json; charset=utf-8"), body));
		if (etag != null) builder.header("ETag", etag);
		return builder.build();
	}

	private static String valid(int revision, String message)
	{
		return "{\"schema_version\":1,\"revision\":" + revision
			+ ",\"generated_at\":\"2026-09-28T20:00:00Z\",\"announcements\":[{"
			+ "\"announcement_id\":\"clan-news\",\"revision\":" + revision
			+ ",\"title\":\"Clan notice\",\"message\":" + new Gson().toJson(message)
			+ ",\"severity\":\"notice\",\"starts_at\":\"2026-09-28T19:00:00Z\","
			+ "\"expires_at\":\"2026-09-28T21:00:00Z\",\"link\":null}]}";
	}

	private static void await(Check check) throws Exception
	{
		long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(3);
		while (!check.value() && System.nanoTime() < deadline) Thread.yield();
		assertTrue(check.value());
	}

	private interface Check { boolean value(); }

	private static final class Harness
	{
		private final OkHttpClient base;
		private final ScheduledExecutorService worker = Executors.newSingleThreadScheduledExecutor();
		private final List<Announcement> messages = new CopyOnWriteArrayList<>();
		private final List<List<Announcement>> sidebars = new CopyOnWriteArrayList<>();
		private final Path state;
		private final AnnouncementService service;

		private Harness(okhttp3.Interceptor interceptor) throws Exception
		{
			base = new OkHttpClient.Builder().addInterceptor(interceptor).build();
			state = Files.createTempDirectory("nocturne-announcement-service").resolve("state.json");
			service = new AnnouncementService(base, new Gson(), state,
				messages::add, sidebars::add,
				worker, true, Clock.fixed(NOW, ZoneOffset.UTC), () -> 0L,
				0, TimeUnit.HOURS.toMillis(1), 0);
		}

		private void close()
		{
			service.close();
			closeBase();
		}

		private void closeBase()
		{
			base.dispatcher().executorService().shutdownNow();
			base.connectionPool().evictAll();
		}
	}
}
