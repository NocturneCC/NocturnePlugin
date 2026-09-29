package com.nocturne;

import com.google.gson.Gson;
import java.io.IOException;
import java.nio.file.Path;
import java.time.Clock;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.TimeUnit;
import java.util.function.Consumer;
import java.util.function.LongSupplier;
import okhttp3.Call;
import okhttp3.ConnectionPool;
import okhttp3.Dispatcher;
import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;

/** Isolated, fail-open synchronization of public emoji assets. */
final class EmojiSyncService implements AutoCloseable
{
	static final String ORIGIN = "https://nocturne.events";
	static final String ENDPOINT = ORIGIN + "/api/plugin/v1/emojis";
	static final long STARTUP_DELAY_MILLIS = 1_000;
	static final long POLL_INTERVAL_MILLIS = TimeUnit.MINUTES.toMillis(5);
	static final long JITTER_MILLIS = TimeUnit.SECONDS.toMillis(30);
	private static final int MAX_CONCURRENT_REQUESTS = 4;

	private final OkHttpClient http;
	private final Gson gson;
	private final EmojiCacheStore cache;
	private final Consumer<Map<String, EmojiAsset>> listener;
	private final ScheduledExecutorService worker;
	private final boolean ownsWorker;
	private final LongSupplier jitterSource;
	private final long startupDelayMillis;
	private final long pollIntervalMillis;
	private final long jitterMillis;
	private final Set<Call> assetCalls = Collections.newSetFromMap(new ConcurrentHashMap<>());
	private final Set<CompletableFuture<?>> assetFutures = Collections.newSetFromMap(new ConcurrentHashMap<>());
	private ScheduledFuture<?> scheduled;
	private Call manifestCall;
	private EmojiCacheStore.Loaded current;
	private boolean loaded;
	private boolean started;
	private boolean closed;

	EmojiSyncService(OkHttpClient base, Gson gson, Path cachePath,
		Consumer<Map<String, EmojiAsset>> listener)
	{
		this(base, gson, cachePath, listener, newWorker(), true, System::nanoTime,
			STARTUP_DELAY_MILLIS, POLL_INTERVAL_MILLIS, JITTER_MILLIS);
	}

	EmojiSyncService(OkHttpClient base, Gson gson, Path cachePath,
		Consumer<Map<String, EmojiAsset>> listener, ScheduledExecutorService worker,
		boolean ownsWorker, LongSupplier jitterSource, long startupDelayMillis,
		long pollIntervalMillis, long jitterMillis)
	{
		this.gson = gson;
		this.cache = new EmojiCacheStore(cachePath, gson);
		this.listener = listener;
		this.worker = worker;
		this.ownsWorker = ownsWorker;
		this.jitterSource = jitterSource;
		this.startupDelayMillis = startupDelayMillis;
		this.pollIntervalMillis = pollIntervalMillis;
		this.jitterMillis = jitterMillis;
		Dispatcher dispatcher = new Dispatcher(worker);
		dispatcher.setMaxRequests(MAX_CONCURRENT_REQUESTS);
		dispatcher.setMaxRequestsPerHost(MAX_CONCURRENT_REQUESTS);
		this.http = base.newBuilder()
			.dispatcher(dispatcher)
			.connectionPool(new ConnectionPool(MAX_CONCURRENT_REQUESTS, 1, TimeUnit.MINUTES))
			.followRedirects(false)
			.followSslRedirects(false)
			.retryOnConnectionFailure(false)
			.callTimeout(5, TimeUnit.SECONDS)
			.build();
	}

	private static ScheduledExecutorService newWorker()
	{
		ThreadFactory factory = runnable ->
		{
			Thread thread = new Thread(runnable, "nocturne-emojis");
			thread.setDaemon(true);
			return thread;
		};
		return Executors.newScheduledThreadPool(MAX_CONCURRENT_REQUESTS, factory);
	}

	synchronized void start()
	{
		if (started || closed) return;
		started = true;
		try { scheduled = worker.schedule(this::poll, startupDelayMillis, TimeUnit.MILLISECONDS); }
		catch (RuntimeException ignored) { }
	}

	private void poll()
	{
		synchronized (this)
		{
			if (closed || manifestCall != null) return;
			if (!loaded)
			{
				loaded = true;
				current = cache.load();
				if (current != null) notifyListener(current.assets);
			}
			Request.Builder request = new Request.Builder().url(ENDPOINT).get();
			if (current != null && current.etag != null) request.header("If-None-Match", current.etag);
			manifestCall = http.newCall(request.build());
		}
		try
		{
			Call call;
			synchronized (this) { call = manifestCall; }
			try (Response response = call.execute())
			{
				if (response.code() == 304) return;
				if (response.code() != 200 || response.body() == null
					|| !json(response.body().contentType())) return;
				byte[] raw = boundedBody(response, EmojiManifest.MAX_MANIFEST_BYTES);
				String etag = response.header("ETag");
				if (raw == null || etag == null || !etag.matches("\"[0-9a-f]{64}\"")) return;
				EmojiManifest manifest = EmojiManifest.parse(raw, gson);
				activate(manifest, etag);
			}
		}
		catch (IOException | RuntimeException ignored) { }
		finally
		{
			synchronized (this) { manifestCall = null; }
			scheduleNext();
		}
	}

	private void activate(EmojiManifest manifest, String etag) throws IOException
	{
		Map<String, byte[]> rawAssets = new ConcurrentHashMap<>();
		List<CompletableFuture<Void>> downloads = new ArrayList<>();
		for (EmojiManifest.Entry entry : manifest.entries)
		{
			byte[] cached = current == null ? null : current.rawAssets.get(entry.digest);
			if (cached != null)
			{
				try
				{
					EmojiCacheStore.validateAsset(cached, entry);
					rawAssets.put(entry.digest, cached);
					continue;
				}
				catch (IOException ignored) { }
			}
			CompletableFuture<Void> download;
			synchronized (this)
			{
				if (closed) throw new IOException("emoji synchronization stopped");
				download = CompletableFuture.runAsync(() ->
				{
					try { rawAssets.put(entry.digest, download(entry)); }
					catch (IOException error) { throw new java.util.concurrent.CompletionException(error); }
				}, worker);
				assetFutures.add(download);
			}
			download.whenComplete((ignored, error) -> assetFutures.remove(download));
			downloads.add(download);
		}
		try { CompletableFuture.allOf(downloads.toArray(new CompletableFuture[0])).join(); }
		catch (RuntimeException error) { throw new IOException("emoji generation unavailable", error); }
		if (isClosed()) return;
		cache.save(manifest, rawAssets, etag);
		EmojiCacheStore.Loaded loadedGeneration = cache.load();
		if (loadedGeneration == null || !manifest.revision.equals(loadedGeneration.manifest.revision))
			throw new IOException("emoji cache verification failed");
		synchronized (this)
		{
			if (closed) return;
			current = loadedGeneration;
		}
		notifyListener(loadedGeneration.assets);
	}

	private byte[] download(EmojiManifest.Entry entry) throws IOException
	{
		String url = ORIGIN + entry.assetPath;
		if (!url.equals(ORIGIN + "/api/plugin/v1/emojis/assets/" + entry.digest + ".png"))
			throw new IOException("unsafe emoji asset path");
		Call call = http.newCall(new Request.Builder().url(url).get().build());
		synchronized (this)
		{
			// Serialize registration with close(): either shutdown sees and cancels
			// this call, or this call observes shutdown and never starts.
			if (closed) throw new IOException("emoji synchronization stopped");
			assetCalls.add(call);
		}
		try (Response response = call.execute())
		{
			MediaType type = response.body() == null ? null : response.body().contentType();
			if (response.code() != 200 || response.body() == null || type == null
				|| !"image".equals(type.type()) || !"png".equals(type.subtype()))
				throw new IOException("emoji asset unavailable");
			byte[] raw = boundedBody(response, EmojiManifest.MAX_ASSET_BYTES);
			if (raw == null) throw new IOException("emoji asset too large");
			EmojiCacheStore.validateAsset(raw, entry);
			return raw;
		}
		finally { assetCalls.remove(call); }
	}

	private static byte[] boundedBody(Response response, int maximum) throws IOException
	{
		response.body().source().request(maximum + 1L);
		if (response.body().source().getBuffer().size() > maximum) return null;
		return response.body().source().readByteArray();
	}

	private static boolean json(MediaType type)
	{
		return type != null && "application".equals(type.type()) && "json".equals(type.subtype());
	}

	private void notifyListener(Map<String, EmojiAsset> assets)
	{
		if (isClosed()) return;
		try { listener.accept(Collections.unmodifiableMap(new LinkedHashMap<>(assets))); }
		catch (RuntimeException ignored) { }
	}

	private synchronized boolean isClosed() { return closed; }

	private synchronized void scheduleNext()
	{
		if (closed) return;
		long delay = nextDelay(pollIntervalMillis, jitterMillis, jitterSource.getAsLong());
		try { scheduled = worker.schedule(this::poll, delay, TimeUnit.MILLISECONDS); }
		catch (RuntimeException ignored) { }
	}

	static long nextDelay(long interval, long jitter, long random)
	{
		if (interval < 1 || jitter < 0 || jitter >= interval)
			throw new IllegalArgumentException("invalid polling bounds");
		if (jitter == 0) return interval;
		return interval + Math.floorMod(random, jitter * 2 + 1) - jitter;
	}

	@Override public synchronized void close()
	{
		if (closed) return;
		closed = true;
		if (scheduled != null) scheduled.cancel(true);
		if (manifestCall != null) manifestCall.cancel();
		for (Call call : assetCalls) call.cancel();
		for (CompletableFuture<?> future : assetFutures) future.cancel(true);
		assetCalls.clear();
		assetFutures.clear();
		http.dispatcher().cancelAll();
		http.connectionPool().evictAll();
		if (ownsWorker) worker.shutdownNow();
		manifestCall = null;
	}

	void pollForTest() { poll(); }
	synchronized boolean inFlightForTest() { return manifestCall != null; }
	int assetFutureCountForTest() { return assetFutures.size(); }
	OkHttpClient httpForTest() { return http; }
}
