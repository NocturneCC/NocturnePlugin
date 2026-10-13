package com.nocturne;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import java.io.IOException;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.ScheduledFuture;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.RejectedExecutionException;
import java.util.function.Consumer;
import okhttp3.Call;
import okhttp3.Callback;
import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;

/** Bounded asynchronous transport. One immutable UUID/body is reused across transient retries. */
final class ChallengeObservationService implements AutoCloseable
{
	static final String ENDPOINT = "https://nocturne.events/api/challenges/intake/observations";
	static final String PLUGIN_ENDPOINT = "https://nocturne.events/api/plugin/tracking/observations";
	static final int MAX_PENDING = 16;
	static final int MAX_RESPONSE_BYTES = 1024;
	static final int MAX_ATTEMPTS = 3;
	private static final long[] RETRY_SECONDS = {1, 3};
	private final OkHttpClient http;
	private final Gson gson;
	private final ScheduledExecutorService scheduler;
	private final Map<String, Pending> pending = new HashMap<>();
	private boolean closed;

	ChallengeObservationService(OkHttpClient http, Gson gson, ScheduledExecutorService scheduler)
	{
		this.http = http.newBuilder().followRedirects(false).followSslRedirects(false)
			.retryOnConnectionFailure(false).callTimeout(8, TimeUnit.SECONDS).build();
		this.gson = gson;
		this.scheduler = scheduler;
	}

	synchronized String submit(ChallengeObservation observation, Consumer<ChallengeObservationResponse> callback)
	{
		return submit(observation, observation != null && observation.challengeProjection
			? Route.CHALLENGE : Route.PLUGIN, callback);
	}

	synchronized String submitPlugin(ChallengeObservation observation,
		Consumer<ChallengeObservationResponse> callback)
	{
		return submit(observation, Route.PLUGIN, callback);
	}

	synchronized String submitChallengeProjection(ChallengeObservation observation,
		Consumer<ChallengeObservationResponse> callback)
	{
		return submit(observation, Route.CHALLENGE, callback);
	}

	private synchronized String submit(ChallengeObservation observation, Route route,
		Consumer<ChallengeObservationResponse> callback)
	{
		if (closed || observation == null) return null;
		String id = observation.eventId;
		String key = route.name() + ':' + id;
		Pending existing = pending.get(key);
		if (existing != null) return id;
		if (pending.size() >= MAX_PENDING) return null;
		String body = gson.toJson(observation.payload(gson, id));
		Pending item = new Pending(key, id, body, observation, route, callback);
		pending.put(key, item);
		send(item);
		return id;
	}

	private void send(Pending item)
	{
		Request request = new Request.Builder().url(item.route == Route.CHALLENGE ? ENDPOINT : PLUGIN_ENDPOINT).post(RequestBody.create(
			MediaType.parse("application/json; charset=utf-8"), item.body)).build();
		Call call = http.newCall(request);
		synchronized (this)
		{
			if (closed || item.cancelled || pending.get(item.key) != item) { call.cancel(); return; }
			item.attempts++;
			item.call = call;
			call.enqueue(new Callback()
		{
			@Override public void onFailure(Call failed, IOException error) { retryOrFinish(item, true, null); }
			@Override public void onResponse(Call completed, Response response)
			{
				boolean retry = false;
				boolean transportFailure = false;
				ChallengeObservationResponse result;
				try (Response ignored = response)
				{
					int code = response.code();
					if (code >= 500 && code <= 599) retry = true;
					JsonObject body = null;
					if (response.body() != null)
					{
						response.body().source().request(MAX_RESPONSE_BYTES + 1L);
						if (response.body().source().getBuffer().size() <= MAX_RESPONSE_BYTES)
							body = gson.fromJson(response.body().source().readUtf8(), JsonObject.class);
					}
					result = ChallengeObservationResponse.parse(code, body);
				}
				catch (IOException ignored)
				{
					transportFailure = true;
					result = new ChallengeObservationResponse(ChallengeObservationResponse.State.SERVER_FAILURE, null);
				}
				catch (RuntimeException ignored) { result = new ChallengeObservationResponse(ChallengeObservationResponse.State.PROTOCOL_ERROR, null); }
				retry |= transportFailure;
				if (retry) retryOrFinish(item, true, result);
				else finish(item, result);
			}
			});
		}
	}

	private void retryOrFinish(Pending item, boolean retryable, ChallengeObservationResponse last)
	{
		synchronized (this)
		{
			if (closed || item.cancelled || !pending.containsKey(item.key)) return;
			if (retryable && item.attempts < MAX_ATTEMPTS)
			{
				long delay = RETRY_SECONDS[Math.min(item.attempts - 1, RETRY_SECONDS.length - 1)];
				try
				{
					item.retry = scheduler.schedule(() -> send(item), delay, TimeUnit.SECONDS);
					return;
				}
				catch (RejectedExecutionException ignored) { /* bounded final failure below */ }
			}
		}
		finish(item, retryable
			? new ChallengeObservationResponse(ChallengeObservationResponse.State.SERVER_FAILURE, null)
			: last == null ? new ChallengeObservationResponse(ChallengeObservationResponse.State.PROTOCOL_ERROR, null) : last);
	}

	private void finish(Pending item, ChallengeObservationResponse result)
	{
		synchronized (this) { if (pending.remove(item.key) == null || item.cancelled) return; }
		item.callback.accept(result);
	}

	synchronized void cancelPending()
	{
		for (Pending item : pending.values())
		{
			item.cancelled = true;
			if (item.call != null) item.call.cancel();
			if (item.retry != null) item.retry.cancel(false);
		}
		pending.clear();
	}
	synchronized int pendingCount() { return pending.size(); }
	synchronized boolean hasScheduledRetry() { return pending.values().stream().anyMatch(item -> item.retry != null && !item.retry.isDone()); }
	@Override public synchronized void close() { closed = true; cancelPending(); }

	private static final class Pending
	{
		final String key, id, body; final ChallengeObservation observation; final Route route;
		final Consumer<ChallengeObservationResponse> callback;
		int attempts; boolean cancelled; Call call; ScheduledFuture<?> retry;
		Pending(String key, String id, String body, ChallengeObservation observation, Route route,
			Consumer<ChallengeObservationResponse> callback)
		{ this.key = key; this.id = id; this.body = body; this.observation = observation;
			this.route = route; this.callback = callback; }
	}

	private enum Route { PLUGIN, CHALLENGE }
}
