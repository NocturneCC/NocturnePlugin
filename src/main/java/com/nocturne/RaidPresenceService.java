package com.nocturne;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import java.io.IOException;
import java.util.HashSet;
import java.util.Set;
import java.util.concurrent.TimeUnit;
import java.util.function.Consumer;
import okhttp3.Call;
import okhttp3.Callback;
import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;

/** Automatic, bounded own-client presence delivery. Failure is deliberately non-fatal. */
final class RaidPresenceService
{
	static final String ENDPOINT = "https://nocturne.events/api/plugin/dev/raid-presence";
	static final long HEARTBEAT_SECONDS = 60;
	private final OkHttpClient http;
	private final Gson gson;
	private final Set<Call> pending = new HashSet<>();
	private String epoch;
	private long lastHeartbeat;
	private long lastEntryAttempt;
	private boolean entryAcknowledged;
	private boolean closed;

	RaidPresenceService(OkHttpClient http, Gson gson)
	{
		this.http = http.newBuilder().followRedirects(false).followSslRedirects(false)
			.retryOnConnectionFailure(false).callTimeout(12, TimeUnit.SECONDS).build();
		this.gson = gson;
	}

	synchronized void heartbeat(RaidPresenceReport report, Consumer<RaidVerificationStatus> update)
	{
		if (report == null || closed) return;
		if (!report.raidEpoch.equals(epoch))
		{
			epoch = report.raidEpoch; lastHeartbeat = 0; lastEntryAttempt = 0;
			entryAcknowledged = false;
		}
		if (!entryAcknowledged)
		{
			if (report.observedAt - lastEntryAttempt < HEARTBEAT_SECONDS) return;
			lastEntryAttempt = report.observedAt; lastHeartbeat = report.observedAt;
			submit(report.withState("entry"), status ->
			{
				synchronized (RaidPresenceService.this)
				{
					if (status != RaidVerificationStatus.UNAVAILABLE) entryAcknowledged = true;
				}
				update.accept(status);
			});
			return;
		}
		if (report.observedAt - lastHeartbeat < HEARTBEAT_SECONDS) return;
		lastHeartbeat = report.observedAt;
		submit(report, update);
	}

	synchronized void submit(RaidPresenceReport report, Consumer<RaidVerificationStatus> update)
	{
		if (report == null || closed || pending.size() >= 4) return;
		Request request = new Request.Builder().url(ENDPOINT).post(RequestBody.create(
			MediaType.parse("application/json; charset=utf-8"), gson.toJson(report.json()))).build();
		Call call = http.newCall(request); pending.add(call);
		call.enqueue(new Callback()
		{
			@Override public void onFailure(Call failed, IOException error)
			{
				finish(failed, RaidVerificationStatus.UNAVAILABLE, update);
			}
			@Override public void onResponse(Call completed, Response response)
			{
				RaidVerificationStatus status = null;
				try (Response ignored = response)
				{
					if ((response.code() == 200 || response.code() == 201) && response.body() != null)
					{
						response.body().source().request(2049);
						if (response.body().source().getBuffer().size() <= 2048)
							status = parse(gson.fromJson(response.body().source().readUtf8(), JsonObject.class));
					}
				}
				catch (IOException | RuntimeException ignored) { }
				finish(completed, status == null ? RaidVerificationStatus.UNAVAILABLE : status, update);
			}
		});
	}

	static RaidVerificationStatus parse(JsonObject reply)
	{
		if (reply == null || reply.get("presence_version").getAsInt() != 1
			|| reply.get("automatic_awards_enabled").getAsBoolean()
			|| reply.get("point_writes").getAsInt() != 0) return null;
		return new RaidVerificationStatus(reply.get("verified").getAsInt(), reply.get("expected").getAsInt(),
			reply.get("consistent").getAsBoolean(), reply.get("group_qualified").getAsBoolean(),
			reply.get("reason").getAsString());
	}

	private synchronized void finish(Call call, RaidVerificationStatus status,
		Consumer<RaidVerificationStatus> update)
	{
		pending.remove(call); if (!closed && status != null) update.accept(status);
	}

	synchronized void close()
	{
		closed = true; pending.forEach(Call::cancel); pending.clear();
	}
}
