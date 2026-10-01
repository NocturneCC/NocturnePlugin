package com.nocturne;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import java.io.IOException;
import java.io.StringReader;
import java.math.BigDecimal;
import java.math.BigInteger;
import java.net.URI;
import java.nio.ByteBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.time.Clock;
import java.time.Instant;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.TimeUnit;
import java.util.function.Consumer;
import java.util.function.LongSupplier;
import java.util.regex.Pattern;
import okhttp3.Call;
import okhttp3.Callback;
import okhttp3.ConnectionPool;
import okhttp3.Dispatcher;
import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;
import com.google.gson.stream.JsonReader;
import com.google.gson.stream.JsonToken;

/** Isolated, fail-open public announcement polling. */
final class AnnouncementService implements AutoCloseable
{
	static final String ENDPOINT = "https://nocturne.events/api/plugin/v1/announcements";
	static final int SCHEMA_VERSION = 1;
	static final int MAX_RESPONSE_BYTES = 16 * 1024;
	static final int MAX_ANNOUNCEMENTS = 3;
	static final int MAX_MESSAGE_CHARS = 500;
	static final int MAX_MESSAGE_LINES = 4;
	static final int MAX_TITLE_CHARS = 80;
	static final long STARTUP_DELAY_MILLIS = 1_000;
	static final long POLL_INTERVAL_MILLIS = TimeUnit.MINUTES.toMillis(15);
	static final long JITTER_MILLIS = TimeUnit.MINUTES.toMillis(1);
	private static final Pattern ID = Pattern.compile("[a-z0-9][a-z0-9_-]{0,63}");
	private static final Pattern ETAG = Pattern.compile("\"[0-9a-f]{64}\"");
	private static final Pattern MARKUP = Pattern.compile(
		"<[^>]*>|\\[[^\\]]*\\]\\([^)]*\\)|!\\[[^\\]]*\\]|```|`[^`]*`");
	private static final Set<String> SEVERITIES = Set.of("info", "notice", "warning", "urgent");
	private static final Set<String> ALLOWED_LINKS = Set.of(
		"https://nocturne.events/", "https://nocturne.events/event-board.html");

	private final OkHttpClient http;
	private final ScheduledExecutorService worker;
	private final boolean ownsWorker;
	private final AnnouncementStateStore stateStore;
	private final Consumer<Announcement> newlyActive;
	private final Consumer<List<Announcement>> sidebar;
	private final Clock clock;
	private final LongSupplier jitterSource;
	private final long startupDelayMillis;
	private final long pollIntervalMillis;
	private final long jitterMillis;
	private final Map<String, Integer> seen = new LinkedHashMap<>();
	private ScheduledFuture<?> scheduled;
	private Call inFlight;
	private String etag;
	private boolean loaded;
	private boolean started;
	private boolean closed;
	private List<Announcement> current = List.of();

	AnnouncementService(OkHttpClient base, Gson gson, Path statePath,
		Consumer<Announcement> newlyActive, Consumer<List<Announcement>> sidebar)
	{
		this(base, gson, statePath, newlyActive, sidebar, newWorker(), true, Clock.systemUTC(),
			System::nanoTime, STARTUP_DELAY_MILLIS, POLL_INTERVAL_MILLIS, JITTER_MILLIS);
	}

	AnnouncementService(OkHttpClient base, Gson gson, Path statePath,
		Consumer<Announcement> newlyActive, Consumer<List<Announcement>> sidebar,
		ScheduledExecutorService worker, boolean ownsWorker, Clock clock,
		LongSupplier jitterSource, long startupDelayMillis, long pollIntervalMillis,
		long jitterMillis)
	{
		this.worker = worker;
		this.ownsWorker = ownsWorker;
		this.stateStore = new AnnouncementStateStore(statePath, gson);
		this.newlyActive = newlyActive;
		this.sidebar = sidebar;
		this.clock = clock;
		this.jitterSource = jitterSource;
		this.startupDelayMillis = startupDelayMillis;
		this.pollIntervalMillis = pollIntervalMillis;
		this.jitterMillis = jitterMillis;
		Dispatcher dispatcher = new Dispatcher(worker);
		dispatcher.setMaxRequests(1);
		dispatcher.setMaxRequestsPerHost(1);
		this.http = base.newBuilder()
			.dispatcher(dispatcher)
			.connectionPool(new ConnectionPool(1, 1, TimeUnit.MINUTES))
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
			Thread thread = new Thread(runnable, "nocturne-announcements");
			thread.setDaemon(true);
			return thread;
		};
		return Executors.newSingleThreadScheduledExecutor(factory);
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
			if (closed || inFlight != null) return;
			List<Announcement> active = new ArrayList<>();
			Instant now = clock.instant();
			for (Announcement announcement : current)
				if (now.isBefore(announcement.expiresAt)) active.add(announcement);
			if (active.size() != current.size())
			{
				current = List.copyOf(active);
				try { sidebar.accept(current); }
				catch (RuntimeException ignored) { }
			}
			if (!loaded)
			{
				seen.putAll(stateStore.load());
				loaded = true;
			}
			Request.Builder builder = new Request.Builder().url(ENDPOINT).get();
			if (etag != null) builder.header("If-None-Match", etag);
			inFlight = http.newCall(builder.build());
			Call call = inFlight;
			try
			{
				call.enqueue(new Callback()
				{
					@Override public void onFailure(Call failed, IOException error)
					{
						finish(failed, null, null);
					}

					@Override public void onResponse(Call completed, Response response)
					{
						List<Announcement> announcements = null;
						String responseEtag = null;
						try (Response ignored = response)
						{
							if (response.code() == 304)
							{
								finish(completed, null, null);
								return;
							}
							MediaType type = response.body() == null ? null : response.body().contentType();
							if (response.code() == 200 && response.body() != null && type != null
								&& "application".equals(type.type()) && "json".equals(type.subtype()))
							{
								response.body().source().request(MAX_RESPONSE_BYTES + 1L);
								if (response.body().source().getBuffer().size() <= MAX_RESPONSE_BYTES)
								{
									announcements = parse(decodeUtf8(
										response.body().source().readByteArray()), clock.instant());
									String candidate = response.header("ETag");
									if (candidate != null && ETAG.matcher(candidate).matches()) responseEtag = candidate;
								}
							}
						}
						catch (IOException | RuntimeException ignored) { }
						finish(completed, announcements, responseEtag);
					}
				});
			}
			catch (RuntimeException error)
			{
				finish(call, null, null);
			}
		}
	}

	private void finish(Call call, List<Announcement> announcements, String responseEtag)
	{
		List<Announcement> display = announcements == null ? null : List.copyOf(announcements);
		List<Announcement> newlyActiveAnnouncements = new ArrayList<>();
		synchronized (this)
		{
			if (inFlight == call) inFlight = null;
			if (closed) return;
			if (display != null)
			{
				current = display;
				if (responseEtag != null) etag = responseEtag;
				for (Announcement announcement : display)
				{
					Integer previous = seen.get(announcement.id);
					if (previous == null || announcement.revision > previous)
					{
						seen.remove(announcement.id);
						seen.put(announcement.id, announcement.revision);
						newlyActiveAnnouncements.add(announcement);
					}
				}
				trimSeen();
				try { stateStore.save(seen); }
				catch (IOException | RuntimeException ignored) { }
			}
			try
			{
				if (display != null) sidebar.accept(display);
				for (Announcement announcement : newlyActiveAnnouncements) newlyActive.accept(announcement);
			}
			catch (RuntimeException ignored) { }
			finally { scheduleNext(); }
		}
	}

	private synchronized void trimSeen()
	{
		while (seen.size() > AnnouncementStateStore.MAX_ENTRIES)
		{
			java.util.Iterator<String> oldest = seen.keySet().iterator();
			oldest.next();
			oldest.remove();
		}
	}

	private synchronized void scheduleNext()
	{
		if (closed) return;
		long delay = nextDelay(pollIntervalMillis, jitterMillis, jitterSource.getAsLong(),
			clock.instant(), current);
		try { scheduled = worker.schedule(this::poll, delay, TimeUnit.MILLISECONDS); }
		catch (RuntimeException ignored) { }
	}

	static long nextDelay(long interval, long jitter, long random)
	{
		if (interval < 1 || jitter < 0 || jitter >= interval) throw new IllegalArgumentException("invalid polling bounds");
		if (jitter == 0) return interval;
		long offset = Math.floorMod(random, jitter * 2 + 1) - jitter;
		return interval + offset;
	}

	static long nextDelay(long interval, long jitter, long random, Instant now,
		List<Announcement> announcements)
	{
		long delay = nextDelay(interval, jitter, random);
		for (Announcement announcement : announcements)
		{
			long expiry = Duration.between(now, announcement.expiresAt).toMillis();
			if (expiry >= 0) delay = Math.min(delay, Math.max(1, expiry + 1));
		}
		return delay;
	}

	static List<Announcement> parse(String raw, Instant now)
	{
		JsonObject root = parseStrictJson(raw).getAsJsonObject();
		if (!exact(root, "schema_version", "revision", "generated_at", "announcements")
			|| integer(root, "schema_version", SCHEMA_VERSION, SCHEMA_VERSION) != SCHEMA_VERSION)
			throw new IllegalArgumentException("invalid response");
		integer(root, "revision", 0, Long.MAX_VALUE);
		Instant.parse(string(root, "generated_at", false));
		JsonArray values = root.getAsJsonArray("announcements");
		if (values.size() > MAX_ANNOUNCEMENTS) throw new IllegalArgumentException("too many announcements");
		List<Announcement> result = new ArrayList<>();
		Set<String> ids = new HashSet<>();
		for (JsonElement element : values)
		{
			JsonObject value = element.getAsJsonObject();
			if (!exact(value, "announcement_id", "revision", "title", "message", "severity",
				"starts_at", "expires_at", "link")) throw new IllegalArgumentException("invalid fields");
			String id = string(value, "announcement_id", false);
			int revision = (int) integer(value, "revision", 1, Integer.MAX_VALUE);
			String titleValue = string(value, "title", true);
			String title = titleValue == null ? null : plain(titleValue, MAX_TITLE_CHARS, 1, false);
			String message = plain(string(value, "message", false), MAX_MESSAGE_CHARS,
				MAX_MESSAGE_LINES, true);
			String severity = string(value, "severity", false);
			Instant starts = Instant.parse(string(value, "starts_at", false));
			Instant expires = Instant.parse(string(value, "expires_at", false));
			if (!ID.matcher(id).matches() || !ids.add(id) || revision < 1 || !SEVERITIES.contains(severity)
				|| !starts.isBefore(expires) || now.isBefore(starts) || !now.isBefore(expires))
				throw new IllegalArgumentException("invalid announcement");
			String linkLabel = null;
			String linkUrl = null;
			if (!value.get("link").isJsonNull())
			{
				JsonObject link = value.getAsJsonObject("link");
				if (!exact(link, "label", "url")) throw new IllegalArgumentException("invalid link fields");
				linkLabel = plain(string(link, "label", false), 48, 1, true);
				linkUrl = allowedLink(string(link, "url", false));
			}
			result.add(new Announcement(id, revision, title, message, severity,
				starts, expires, linkLabel, linkUrl));
		}
		return Collections.unmodifiableList(result);
	}

	static JsonElement parseStrictJson(String raw)
	{
		try
		{
			JsonReader reader = new JsonReader(new StringReader(raw));
			reader.setLenient(false);
			JsonElement value = readStrict(reader, 0);
			if (reader.peek() != JsonToken.END_DOCUMENT) throw new IllegalArgumentException("trailing JSON");
			return value;
		}
		catch (IOException | IllegalStateException error)
		{
			throw new IllegalArgumentException("invalid JSON", error);
		}
	}

	static String decodeUtf8(byte[] raw) throws CharacterCodingException
	{
		return StandardCharsets.UTF_8.newDecoder()
			.onMalformedInput(CodingErrorAction.REPORT)
			.onUnmappableCharacter(CodingErrorAction.REPORT)
			.decode(ByteBuffer.wrap(raw)).toString();
	}

	private static JsonElement readStrict(JsonReader reader, int depth) throws IOException
	{
		if (depth > 16) throw new IllegalArgumentException("JSON nesting exceeds limit");
		switch (reader.peek())
		{
			case BEGIN_OBJECT:
				reader.beginObject();
				JsonObject object = new JsonObject();
				Set<String> names = new HashSet<>();
				while (reader.hasNext())
				{
					String name = reader.nextName();
					if (names.size() >= 256 || !names.add(name))
						throw new IllegalArgumentException("duplicate or excessive JSON fields");
					object.add(name, readStrict(reader, depth + 1));
				}
				reader.endObject();
				return object;
			case BEGIN_ARRAY:
				reader.beginArray();
				JsonArray array = new JsonArray();
				while (reader.hasNext())
				{
					if (array.size() >= 256) throw new IllegalArgumentException("JSON array exceeds limit");
					array.add(readStrict(reader, depth + 1));
				}
				reader.endArray();
				return array;
			case STRING: return new com.google.gson.JsonPrimitive(reader.nextString());
			case NUMBER: return new com.google.gson.JsonPrimitive(new BigDecimal(reader.nextString()));
			case BOOLEAN: return new com.google.gson.JsonPrimitive(reader.nextBoolean());
			case NULL:
				reader.nextNull();
				return com.google.gson.JsonNull.INSTANCE;
			default: throw new IllegalArgumentException("invalid JSON token");
		}
	}

	private static String plain(String value, int maximum, int lines, boolean required)
	{
		if (value == null) throw new IllegalArgumentException("invalid text");
		value = value.trim();
		if ((!required && value.isEmpty())) return null;
		if (value.isEmpty() || value.codePointCount(0, value.length()) > maximum
			|| value.split("\\n", -1).length > lines || value.indexOf('<') >= 0
			|| value.indexOf('>') >= 0 || MARKUP.matcher(value).find())
			throw new IllegalArgumentException("unsafe text");
		for (int index = 0; index < value.length(); )
		{
			int point = value.codePointAt(index);
			int type = Character.getType(point);
			if (point != '\n' && (type == Character.CONTROL || type == Character.FORMAT || type == Character.SURROGATE
				|| type == Character.PRIVATE_USE || type == Character.UNASSIGNED
				|| type == Character.LINE_SEPARATOR || type == Character.PARAGRAPH_SEPARATOR)
				) throw new IllegalArgumentException("unsafe text");
			index += Character.charCount(point);
		}
		return value;
	}

	private static String allowedLink(String value)
	{
		if (value == null || value.length() > 256 || !ALLOWED_LINKS.contains(value))
			throw new IllegalArgumentException("link is not allowlisted");
		try
		{
			URI link = URI.create(value);
			if (!"https".equals(link.getScheme()) || link.getRawUserInfo() != null || link.getPort() != -1
				|| link.getRawQuery() != null || link.getRawFragment() != null)
				throw new IllegalArgumentException("invalid link");
		}
		catch (RuntimeException error)
		{
			throw new IllegalArgumentException("invalid link", error);
		}
		return value;
	}

	private static boolean exact(JsonObject object, String... names)
	{
		if (object == null || object.entrySet().size() != names.length) return false;
		for (String name : names) if (!object.has(name)) return false;
		return true;
	}

	private static String string(JsonObject object, String name, boolean nullable)
	{
		JsonElement element = object.get(name);
		if (nullable && element != null && element.isJsonNull()) return null;
		if (element == null || !element.isJsonPrimitive()
			|| !element.getAsJsonPrimitive().isString())
			throw new IllegalArgumentException("invalid string");
		return element.getAsString();
	}

	private static long integer(JsonObject object, String name, long minimum, long maximum)
	{
		JsonElement element = object.get(name);
		if (element == null || !element.isJsonPrimitive()
			|| !element.getAsJsonPrimitive().isNumber()
			|| !element.toString().matches("-?(0|[1-9][0-9]*)"))
			throw new IllegalArgumentException("invalid integer");
		BigInteger value = new BigInteger(element.getAsString());
		if (value.compareTo(BigInteger.valueOf(minimum)) < 0
			|| value.compareTo(BigInteger.valueOf(maximum)) > 0)
			throw new IllegalArgumentException("invalid integer");
		return value.longValue();
	}

	@Override public synchronized void close()
	{
		if (closed) return;
		closed = true;
		if (scheduled != null) scheduled.cancel(true);
		if (inFlight != null) inFlight.cancel();
		http.dispatcher().cancelAll();
		http.connectionPool().evictAll();
		if (ownsWorker) worker.shutdownNow();
		inFlight = null;
	}

	void pollNowForTest()
	{
		worker.execute(this::poll);
	}

	void pollForTest()
	{
		poll();
	}

	synchronized boolean inFlightForTest()
	{
		return inFlight != null;
	}

	OkHttpClient httpForTest()
	{
		return http;
	}
}
