package com.nocturne;

import com.google.gson.Gson;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;
import okhttp3.Interceptor;
import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Protocol;
import okhttp3.Request;
import okhttp3.Response;
import okhttp3.ResponseBody;
import org.junit.Test;
import static org.junit.Assert.*;

public class EmojiSyncServiceTest
{
	private final Gson gson = new Gson();

	@Test public void requestIsIdentityFreeSameOriginBoundedAndIsolated() throws Exception
	{
		byte[] image = EmojiTestFixtures.png(0xff224466);
		EmojiTestFixtures.FixtureEntry entry = new EmojiTestFixtures.FixtureEntry("wave", image);
		byte[] manifest = EmojiTestFixtures.manifest(gson, List.of(entry));
		List<Request> requests = new CopyOnWriteArrayList<>();
		Harness harness = harness(chain ->
		{
			requests.add(chain.request());
			return chain.request().url().encodedPath().equals("/api/plugin/v1/emojis")
				? response(chain.request(), 200, "application/json", manifest, etag(manifest))
				: response(chain.request(), 200, "image/png", image, null);
		});
		try
		{
			harness.service.pollForTest();
			assertEquals(1, harness.updates.size());
			assertEquals(2, requests.size());
			for (Request request : requests)
			{
				assertEquals("https", request.url().scheme());
				assertEquals("nocturne.events", request.url().host());
				assertEquals("GET", request.method());
				assertNull(request.body());
				assertNull(request.url().query());
				String headers = request.headers().toString().toLowerCase();
				for (String forbidden : List.of("rsn", "account", "profile", "chat", "raid",
					"telemetry", "receipt", "discord", "authorization")) assertFalse(headers.contains(forbidden));
			}
			assertFalse(harness.service.httpForTest().followRedirects());
			assertFalse(harness.service.httpForTest().followSslRedirects());
			assertFalse(harness.service.httpForTest().retryOnConnectionFailure());
			assertEquals(5_000, harness.service.httpForTest().callTimeoutMillis());
			assertNotSame(harness.base.dispatcher(), harness.service.httpForTest().dispatcher());
		}
		finally { harness.close(); }
	}

	@Test public void manifest304404429AndServerErrorsRetainLastVerifiedRegistry() throws Exception
	{
		byte[] image = EmojiTestFixtures.png(0xff224466);
		byte[] manifest = EmojiTestFixtures.manifest(gson,
			List.of(new EmojiTestFixtures.FixtureEntry("wave", image)));
		AtomicInteger code = new AtomicInteger(200);
		Harness harness = harness(chain ->
		{
			if (!chain.request().url().encodedPath().equals("/api/plugin/v1/emojis"))
				return response(chain.request(), 200, "image/png", image, null);
			int value = code.get();
			return response(chain.request(), value, "application/json",
				value == 200 ? manifest : new byte[0], value == 200 ? etag(manifest) : null);
		});
		try
		{
			harness.service.pollForTest();
			assertEquals(1, harness.updates.size());
			for (int status : new int[]{304, 404, 429, 500, 503})
			{
				code.set(status);
				harness.service.pollForTest();
				assertEquals(1, harness.updates.size());
			}
		}
		finally { harness.close(); }
	}

	@Test public void cacheProvidesLastVerifiedGenerationWhenServerIsUnavailable() throws Exception
	{
		byte[] image = EmojiTestFixtures.png(0xff224466);
		byte[] manifest = EmojiTestFixtures.manifest(gson,
			List.of(new EmojiTestFixtures.FixtureEntry("wave", image)));
		Path cache = Files.createTempDirectory("emoji-service-cache");
		Harness first = harness(cache, chain -> chain.request().url().encodedPath().endsWith(".png")
			? response(chain.request(), 200, "image/png", image, null)
			: response(chain.request(), 200, "application/json", manifest, etag(manifest)));
		first.service.pollForTest();
		first.close(false);
		Harness second = harness(cache, chain -> response(chain.request(), 503,
			"application/json", new byte[0], null));
		try
		{
			second.service.pollForTest();
			assertEquals(1, second.updates.size());
			assertTrue(second.updates.get(0).containsKey("wave"));
		}
		finally { second.close(true); }
	}

	@Test public void invalidMimeDigestDimensionsAndOversizedBodiesNeverActivate() throws Exception
	{
		byte[] image = EmojiTestFixtures.png(0xff224466);
		EmojiTestFixtures.FixtureEntry entry = new EmojiTestFixtures.FixtureEntry("wave", image);
		byte[] manifest = EmojiTestFixtures.manifest(gson, List.of(entry));
		for (byte[] asset : List.of("not-png".getBytes(), new byte[EmojiManifest.MAX_ASSET_BYTES + 1]))
		{
			Harness harness = harness(chain -> chain.request().url().encodedPath().endsWith(".png")
				? response(chain.request(), 200, "image/png", asset, null)
				: response(chain.request(), 200, "application/json", manifest, etag(manifest)));
			try
			{
				harness.service.pollForTest();
				assertTrue(harness.updates.isEmpty());
			}
			finally { harness.close(); }
		}
	}

	@Test public void onlyOneManifestRequestCanBeInFlightAndShutdownCancelsIt() throws Exception
	{
		CountDownLatch entered = new CountDownLatch(1);
		CountDownLatch release = new CountDownLatch(1);
		AtomicInteger requests = new AtomicInteger();
		AtomicReference<okhttp3.Call> call = new AtomicReference<>();
		Harness harness = harness(chain ->
		{
			requests.incrementAndGet();
			call.set(chain.call());
			entered.countDown();
			try { release.await(3, TimeUnit.SECONDS); }
			catch (InterruptedException error) { Thread.currentThread().interrupt(); throw new IOException(error); }
			return response(chain.request(), 503, "application/json", new byte[0], null);
		});
		Thread poll = new Thread(harness.service::pollForTest);
		poll.start();
		assertTrue(entered.await(1, TimeUnit.SECONDS));
		assertTrue(harness.service.inFlightForTest());
		harness.service.pollForTest();
		assertEquals(1, requests.get());
		harness.service.close();
		assertTrue(call.get().isCanceled());
		release.countDown();
		poll.join(2_000);
		assertFalse(poll.isAlive());
		harness.close();
	}

	@Test public void pollingJitterDuplicateStartAndRejectedSchedulerAreBounded() throws Exception
	{
		long interval = EmojiSyncService.POLL_INTERVAL_MILLIS;
		long jitter = EmojiSyncService.JITTER_MILLIS;
		assertTrue(EmojiSyncService.nextDelay(interval, jitter, Long.MIN_VALUE) >= interval - jitter);
		assertTrue(EmojiSyncService.nextDelay(interval, jitter, Long.MAX_VALUE) <= interval + jitter);
		ScheduledExecutorService rejected = Executors.newSingleThreadScheduledExecutor();
		rejected.shutdownNow();
		EmojiSyncService service = new EmojiSyncService(new OkHttpClient(), gson,
			Files.createTempDirectory("emoji-rejected"), ignored -> { }, rejected, false,
			() -> 0, 0, interval, jitter);
		service.start();
		service.start();
		service.close();
		service.close();
	}

	private Harness harness(Interceptor interceptor) throws Exception
	{
		return harness(Files.createTempDirectory("emoji-service"), interceptor);
	}

	private Harness harness(Path cache, Interceptor interceptor)
	{
		OkHttpClient base = new OkHttpClient.Builder().addInterceptor(interceptor).build();
		ScheduledExecutorService worker = Executors.newScheduledThreadPool(4);
		List<Map<String, EmojiAsset>> updates = new CopyOnWriteArrayList<>();
		EmojiSyncService service = new EmojiSyncService(base, gson, cache, updates::add,
			worker, false, () -> 0, 0, TimeUnit.DAYS.toMillis(1), 0);
		return new Harness(base, service, worker, updates, cache);
	}

	private static String etag(byte[] raw) { return "\"" + EmojiManifest.sha256(raw) + "\""; }

	private static Response response(Request request, int code, String type, byte[] body, String etag)
	{
		Response.Builder builder = new Response.Builder().request(request).protocol(Protocol.HTTP_1_1)
			.code(code).message("fixture")
			.body(ResponseBody.create(MediaType.parse(type), body));
		if (etag != null) builder.header("ETag", etag);
		return builder.build();
	}

	private static final class Harness implements AutoCloseable
	{
		final OkHttpClient base;
		final EmojiSyncService service;
		final ScheduledExecutorService worker;
		final List<Map<String, EmojiAsset>> updates;
		final Path cache;
		Harness(OkHttpClient base, EmojiSyncService service, ScheduledExecutorService worker,
			List<Map<String, EmojiAsset>> updates, Path cache)
		{
			this.base = base;
			this.service = service;
			this.worker = worker;
			this.updates = updates;
			this.cache = cache;
		}
		@Override public void close() throws Exception { close(true); }
		void close(boolean delete) throws Exception
		{
			service.close();
			worker.shutdownNow();
			if (delete) delete(cache);
		}
		private static void delete(Path path) throws Exception
		{
			if (!Files.exists(path)) return;
			if (Files.isDirectory(path))
				try (java.nio.file.DirectoryStream<Path> stream = Files.newDirectoryStream(path))
				{ for (Path child : stream) delete(child); }
			Files.deleteIfExists(path);
		}
	}
}
