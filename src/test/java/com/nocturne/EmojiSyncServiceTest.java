package com.nocturne;

import com.google.gson.Gson;
import java.io.IOException;
import java.net.InetAddress;
import java.net.UnknownHostException;
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
import okio.Buffer;
import okio.BufferedSource;
import okio.Okio;
import okio.Source;
import okio.Timeout;
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

	@Test public void startImmediatelySynchronizesAndPublishesVerifiedAssets() throws Exception
	{
		byte[] image = EmojiTestFixtures.png(0xff224466);
		byte[] manifest = EmojiTestFixtures.manifest(gson,
			List.of(new EmojiTestFixtures.FixtureEntry("wave", image)));
		Path parent = Files.createTempDirectory("emoji-startup");
		Path cachePath = parent.resolve("nocturne").resolve("emoji-cache-v1");
		CountDownLatch published = new CountDownLatch(1);
		CountDownLatch completed = new CountDownLatch(1);
		List<Boolean> outcomes = new CopyOnWriteArrayList<>();
		AtomicInteger icon = new AtomicInteger(10);
		AtomicInteger registered = new AtomicInteger();
		AtomicReference<Thread> registrationThread = new AtomicReference<>();
		AtomicReference<Runnable> clientTask = new AtomicReference<>();
		EmojiRenderer renderer = new EmojiRenderer(() -> { }, new EmojiRenderer.IconRegistrar()
		{
			@Override public int reserve() { return icon.getAndIncrement(); }
			@Override public void update(int value, java.awt.image.BufferedImage image)
			{
				registered.incrementAndGet();
				registrationThread.set(Thread.currentThread());
			}
			@Override public int chatIndex(int value) { return value; }
		});
		EmojiSyncService.AssetListener publish = EmojiRenderer.clientThreadPublisher(
			task -> clientTask.set(task), () -> true, renderer, ignored -> { });
		AtomicInteger manifestRequests = new AtomicInteger();
		OkHttpClient base = new OkHttpClient.Builder().addInterceptor(chain ->
		{
			if (chain.request().url().encodedPath().equals("/api/plugin/v1/emojis"))
			{
				manifestRequests.incrementAndGet();
				return response(chain.request(), 200, "application/json", manifest, etag(manifest));
			}
			return response(chain.request(), 200, "image/png", image, null);
		}).build();
		EmojiSyncService service = new EmojiSyncService(base, gson, cachePath,
			(assets, completion) ->
			{
				publish.publish(assets, completion);
				published.countDown();
			}, succeeded ->
			{
				outcomes.add(succeeded);
				completed.countDown();
			});
		try
		{
			service.start();
			assertTrue("initial synchronization did not publish before timeout",
				published.await(3, TimeUnit.SECONDS));
			assertEquals(1, manifestRequests.get());
			assertEquals("worker must not register chat icons", 0, registered.get());
			assertTrue("sync success must wait for client-thread publication", outcomes.isEmpty());
			clientTask.get().run();
			assertTrue("success not reported after publication", completed.await(3, TimeUnit.SECONDS));
			assertEquals(List.of(true), outcomes);
			assertEquals(1, registered.get());
			assertSame(Thread.currentThread(), registrationThread.get());
			assertEquals(1, renderer.activeTriggerCountForTest());
			EmojiRendererTest.TestNode node = new EmojiRendererTest.TestNode(":wave:");
			assertTrue(renderer.onChatMessage(new net.runelite.api.events.ChatMessage(node,
				net.runelite.api.ChatMessageType.PUBLICCHAT, "name", node.getValue(), "sender", 1)));
			assertEquals("<img=10>", node.getRuneLiteFormatMessage());
			EmojiCacheStore.Loaded cached = new EmojiCacheStore(cachePath, gson).load();
			assertNotNull(cached);
			assertTrue(cached.assets.containsKey("wave"));
		}
		finally
		{
			service.close();
			base.dispatcher().executorService().shutdownNow();
			base.connectionPool().evictAll();
			Harness.delete(parent);
		}
	}

	@Test public void emptyRendererPublicationDoesNotReportSynchronizationSuccess() throws Exception
	{
		byte[] manifest = EmojiTestFixtures.manifest(gson, List.of());
		Path cachePath = Files.createTempDirectory("emoji-empty-publication").resolve("cache");
		CountDownLatch finished = new CountDownLatch(1);
		List<Boolean> outcomes = new CopyOnWriteArrayList<>();
		EmojiRenderer renderer = new EmojiRenderer(() -> { }, new EmojiRenderer.IconRegistrar()
		{
			@Override public int reserve() { return 1; }
			@Override public void update(int value, java.awt.image.BufferedImage image) { }
			@Override public int chatIndex(int value) { return value; }
		});
		OkHttpClient base = new OkHttpClient.Builder().addInterceptor(chain ->
			response(chain.request(), 200, "application/json", manifest, etag(manifest))).build();
		EmojiSyncService.AssetListener publisher = EmojiRenderer.clientThreadPublisher(
			task -> task.run(), () -> true, renderer, ignored -> { });
		EmojiSyncService service = new EmojiSyncService(base, gson, cachePath, publisher, outcome ->
		{
			outcomes.add(outcome);
			finished.countDown();
		});
		try
		{
			service.start();
			assertTrue(finished.await(3, TimeUnit.SECONDS));
			assertEquals(List.of(false), outcomes);
			assertEquals(0, renderer.activeTriggerCountForTest());
		}
		finally
		{
			service.close();
			base.dispatcher().executorService().shutdownNow();
		}
	}

	@Test public void manifestAndAssetRequestsArePacedBelowThePerIpLimit() throws Exception
	{
		byte[] first = EmojiTestFixtures.png(0xff224466);
		byte[] second = EmojiTestFixtures.png(0xff661122);
		byte[] third = EmojiTestFixtures.png(0xff116622);
		List<EmojiTestFixtures.FixtureEntry> entries = List.of(
			new EmojiTestFixtures.FixtureEntry("one", first),
			new EmojiTestFixtures.FixtureEntry("three", third),
			new EmojiTestFixtures.FixtureEntry("two", second));
		byte[] manifest = EmojiTestFixtures.manifest(gson, entries);
		Map<String, byte[]> assets = Map.of(
			EmojiManifest.sha256(first), first,
			EmojiManifest.sha256(second), second,
			EmojiManifest.sha256(third), third);
		long spacingMillis = 30;
		long requiredNanos = TimeUnit.MILLISECONDS.toNanos(spacingMillis - 3);
		java.util.concurrent.atomic.AtomicLong lastRequest = new java.util.concurrent.atomic.AtomicLong();
		java.util.concurrent.atomic.AtomicInteger requests = new java.util.concurrent.atomic.AtomicInteger();
		java.util.concurrent.atomic.AtomicInteger throttled = new java.util.concurrent.atomic.AtomicInteger();
		List<Boolean> outcomes = new CopyOnWriteArrayList<>();
		Path cache = Files.createTempDirectory("emoji-paced");
		OkHttpClient base = new OkHttpClient.Builder().addInterceptor(chain ->
		{
			long now = System.nanoTime();
			long previous = lastRequest.getAndSet(now);
			requests.incrementAndGet();
			if (previous != 0 && now - previous < requiredNanos)
			{
				throttled.incrementAndGet();
				return response(chain.request(), 429, "image/png", new byte[0], null);
			}
			String path = chain.request().url().encodedPath();
			if (path.equals("/api/plugin/v1/emojis"))
				return response(chain.request(), 200, "application/json", manifest, etag(manifest));
			String digest = path.substring(path.lastIndexOf('/') + 1, path.length() - 4);
			byte[] body = assets.get(digest);
			return body == null ? response(chain.request(), 404, "image/png", new byte[0], null)
				: response(chain.request(), 200, "image/png", body, null);
		}).build();
		ScheduledExecutorService worker = Executors.newScheduledThreadPool(4);
		EmojiSyncService service = new EmojiSyncService(base, gson, cache, ignored -> { }, outcomes::add,
			worker, false, () -> 0, 0, TimeUnit.DAYS.toMillis(1), 0, spacingMillis);
		try
		{
			service.pollForTest();
			assertEquals(4, requests.get());
			assertEquals(0, throttled.get());
			assertEquals(List.of(true), outcomes);
			assertEquals(3, new EmojiCacheStore(cache, gson).load().assets.size());
		}
		finally
		{
			service.close();
			worker.shutdownNow();
			base.dispatcher().executorService().shutdownNow();
			base.connectionPool().evictAll();
			Harness.delete(cache);
		}
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

	@Test public void synchronizationOutcomesAreReportedOnceWithoutResponseDetails() throws Exception
	{
		byte[] image = EmojiTestFixtures.png(0xff224466);
		byte[] manifest = EmojiTestFixtures.manifest(gson,
			List.of(new EmojiTestFixtures.FixtureEntry("wave", image)));
		AtomicInteger status = new AtomicInteger(503);
		List<Boolean> outcomes = new CopyOnWriteArrayList<>();
		Harness harness = harness(Files.createTempDirectory("emoji-outcome"), chain ->
		{
			if (!chain.request().url().encodedPath().equals("/api/plugin/v1/emojis"))
				return response(chain.request(), 200, "image/png", image, null);
			int code = status.get();
			return response(chain.request(), code, "application/json",
				code == 200 ? manifest : new byte[0], code == 200 ? etag(manifest) : null);
		}, outcomes::add);
		try
		{
			harness.service.pollForTest();
			harness.service.pollForTest();
			assertEquals(List.of(false), outcomes);

			status.set(200);
			harness.service.pollForTest();
			assertEquals(List.of(false, true), outcomes);

			status.set(304);
			harness.service.pollForTest();
			assertEquals(List.of(false, true), outcomes);

			status.set(503);
			harness.service.pollForTest();
			assertEquals(List.of(false, true), outcomes);
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

	@Test public void shutdownCancelsQueuedAndActiveAssetWorkWithoutCallback() throws Exception
	{
		byte[] image = EmojiTestFixtures.png(0xff224466);
		List<EmojiTestFixtures.FixtureEntry> entries = new ArrayList<>();
		for (int index = 0; index < 12; index++)
			entries.add(new EmojiTestFixtures.FixtureEntry("emoji_" + index, image));
		entries.sort(java.util.Comparator.comparing(entry -> entry.name));
		byte[] manifest = EmojiTestFixtures.manifest(gson, entries);
		CountDownLatch assetEntered = new CountDownLatch(1);
		CountDownLatch release = new CountDownLatch(1);
		Harness harness = harness(chain ->
		{
			if (!chain.request().url().encodedPath().endsWith(".png"))
				return response(chain.request(), 200, "application/json", manifest, etag(manifest));
			assetEntered.countDown();
			try { release.await(3, TimeUnit.SECONDS); }
			catch (InterruptedException error) { Thread.currentThread().interrupt(); throw new IOException(error); }
			return response(chain.request(), 200, "image/png", image, null);
		});
		Thread poll = new Thread(harness.service::pollForTest);
		poll.start();
		assertTrue(assetEntered.await(1, TimeUnit.SECONDS));
		assertTrue(harness.service.assetFutureCountForTest() > 0);
		harness.service.close();
		poll.join(2_000);
		assertFalse(poll.isAlive());
		release.countDown();
		assertTrue(harness.updates.isEmpty());
		harness.close();
	}

	@Test public void shutdownDuringDnsCannotActivateARegistry() throws Exception
	{
		CountDownLatch entered = new CountDownLatch(1);
		CountDownLatch release = new CountDownLatch(1);
		OkHttpClient base = new OkHttpClient.Builder().dns(hostname ->
		{
			entered.countDown();
			try { release.await(3, TimeUnit.SECONDS); }
			catch (InterruptedException error)
			{
				Thread.currentThread().interrupt();
				throw new UnknownHostException("cancelled");
			}
			return List.of(InetAddress.getLoopbackAddress());
		}).build();
		Path cache = Files.createTempDirectory("emoji-service-dns");
		ScheduledExecutorService worker = Executors.newScheduledThreadPool(4);
		List<Map<String, EmojiAsset>> updates = new CopyOnWriteArrayList<>();
		EmojiSyncService service = new EmojiSyncService(base, gson, cache, updates::add,
			worker, false, () -> 0, 0, TimeUnit.DAYS.toMillis(1), 0);
		Thread poll = new Thread(service::pollForTest);
		poll.start();
		assertTrue(entered.await(1, TimeUnit.SECONDS));
		service.close();
		release.countDown();
		poll.join(2_000);
		assertFalse(poll.isAlive());
		assertTrue(updates.isEmpty());
		worker.shutdownNow();
		base.dispatcher().executorService().shutdownNow();
		base.connectionPool().evictAll();
		Harness.delete(cache);
	}

	@Test public void shutdownDuringManifestBodyCannotActivateARegistry() throws Exception
	{
		byte[] image = EmojiTestFixtures.png(0xff224466);
		byte[] manifest = EmojiTestFixtures.manifest(gson,
			List.of(new EmojiTestFixtures.FixtureEntry("wave", image)));
		CountDownLatch entered = new CountDownLatch(1);
		CountDownLatch release = new CountDownLatch(1);
		Harness harness = harness(chain -> blockingResponse(chain.request(), "application/json",
			manifest, etag(manifest), entered, release));
		Thread poll = new Thread(harness.service::pollForTest);
		poll.start();
		assertTrue(entered.await(1, TimeUnit.SECONDS));
		harness.service.close();
		release.countDown();
		poll.join(2_000);
		assertFalse(poll.isAlive());
		assertTrue(harness.updates.isEmpty());
		harness.close();
	}

	@Test public void shutdownDuringAssetBodyCannotActivateARegistry() throws Exception
	{
		byte[] image = EmojiTestFixtures.png(0xff224466);
		byte[] manifest = EmojiTestFixtures.manifest(gson,
			List.of(new EmojiTestFixtures.FixtureEntry("wave", image)));
		CountDownLatch entered = new CountDownLatch(1);
		CountDownLatch release = new CountDownLatch(1);
		Harness harness = harness(chain -> chain.request().url().encodedPath().endsWith(".png")
			? blockingResponse(chain.request(), "image/png", image, null, entered, release)
			: response(chain.request(), 200, "application/json", manifest, etag(manifest)));
		Thread poll = new Thread(harness.service::pollForTest);
		poll.start();
		assertTrue(entered.await(1, TimeUnit.SECONDS));
		harness.service.close();
		release.countDown();
		poll.join(2_000);
		assertFalse(poll.isAlive());
		assertTrue(harness.updates.isEmpty());
		harness.close();
	}

	private Harness harness(Interceptor interceptor) throws Exception
	{
		return harness(Files.createTempDirectory("emoji-service"), interceptor);
	}

	private Harness harness(Path cache, Interceptor interceptor)
	{
		return harness(cache, interceptor, ignored -> { });
	}

	private Harness harness(Path cache, Interceptor interceptor, java.util.function.Consumer<Boolean> outcomes)
	{
		OkHttpClient base = new OkHttpClient.Builder().addInterceptor(interceptor).build();
		ScheduledExecutorService worker = Executors.newScheduledThreadPool(4);
		List<Map<String, EmojiAsset>> updates = new CopyOnWriteArrayList<>();
		EmojiSyncService service = new EmojiSyncService(base, gson, cache, updates::add, outcomes,
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

	private static Response blockingResponse(Request request, String type, byte[] body, String etag,
		CountDownLatch entered, CountDownLatch release)
	{
		ResponseBody responseBody = new ResponseBody()
		{
			private final BufferedSource source = Okio.buffer(new Source()
			{
				private boolean sent;
				@Override public long read(Buffer sink, long count) throws IOException
				{
					if (sent) return -1;
					entered.countDown();
					try { release.await(3, TimeUnit.SECONDS); }
					catch (InterruptedException error)
					{
						Thread.currentThread().interrupt();
						throw new IOException("cancelled", error);
					}
					sink.write(body);
					sent = true;
					return body.length;
				}
				@Override public Timeout timeout() { return Timeout.NONE; }
				@Override public void close() { }
			});
			@Override public MediaType contentType() { return MediaType.parse(type); }
			@Override public long contentLength() { return -1; }
			@Override public BufferedSource source() { return source; }
		};
		Response.Builder builder = new Response.Builder().request(request).protocol(Protocol.HTTP_1_1)
			.code(200).message("fixture").body(responseBody);
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
