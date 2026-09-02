package com.nocturne;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonObject;
import java.io.IOException;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.TimeUnit;
import java.util.function.Consumer;
import okhttp3.Call;
import okhttp3.Callback;
import okhttp3.MediaType;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.RequestBody;
import okhttp3.Response;

/** Bounded, opt-in test delivery. No game objects, group names or credentials. */
final class SubmissionService
{
	private static final String ENDPOINT = "https://nocturne.events/api/plugin/dev/drops";
	private final OkHttpClient http;
	private final Gson gson;
	private final Map<Call, Consumer<SubmissionStatus>> pending = new HashMap<>();
	private boolean closed;

	SubmissionService(OkHttpClient http, Gson gson)
	{
		this.http = http.newBuilder().followRedirects(false).followSslRedirects(false)
			.retryOnConnectionFailure(false).callTimeout(12, TimeUnit.SECONDS).build();
		this.gson = gson;
	}

	static JsonObject payload(LootRecord record)
	{
		return payload(record, null);
	}

	static JsonObject payload(LootRecord record, SubmissionScreenshot screenshot)
	{
		JsonObject body = new JsonObject();
		body.addProperty("version", screenshot == null ? 2 : 3);
		body.addProperty("event_id", record.id);
		body.addProperty("occurred_at", record.occurredAt);
		body.addProperty("rsn", record.rsn);
		body.addProperty("source", record.source);
		JsonArray items = new JsonArray();
		for (LootItem item : record.items)
		{
			JsonObject entry = new JsonObject();
			entry.addProperty("item_id", item.id);
			entry.addProperty("quantity", item.quantity);
			entry.addProperty("unit_price_gp", item.unitPriceGp);
			items.add(entry);
		}
		body.add("items", items);
		if (screenshot != null)
		{
			JsonObject image = new JsonObject();
			image.addProperty("mime_type", screenshot.mimeType);
			image.addProperty("width", screenshot.width);
			image.addProperty("height", screenshot.height);
			image.addProperty("sha256", screenshot.sha256);
			image.addProperty("data_base64", screenshot.base64());
			body.add("screenshot", image);
		}
		return body;
	}

	synchronized void submit(LootRecord record, Consumer<SubmissionStatus> update)
	{
		submit(record, null, update);
	}

	synchronized void submit(LootRecord record, SubmissionScreenshot screenshot,
		Consumer<SubmissionStatus> update)
	{
		if (closed) { update.accept(SubmissionStatus.CANCELLED); return; }
		if (pending.size() >= 8) { update.accept(SubmissionStatus.BUSY); return; }
		Request request = new Request.Builder().url(ENDPOINT)
			.post(RequestBody.create(MediaType.parse("application/json; charset=utf-8"),
				gson.toJson(payload(record, screenshot))))
			.build();
		Call call = http.newCall(request);
		pending.put(call, update);
		update.accept(SubmissionStatus.SENDING);
		call.enqueue(new Callback()
		{
			@Override
			public void onFailure(Call failed, IOException error)
			{
				finish(failed, SubmissionStatus.UNCERTAIN);
			}

			@Override
			public void onResponse(Call completed, Response response)
			{
				SubmissionStatus status = SubmissionStatus.UNCERTAIN;
				try (Response ignored = response)
				{
					if (response.code() >= 400 && response.code() < 500)
					{
						status = SubmissionStatus.REJECTED;
					}
					else if (response.body() != null && (response.code() == 200 || response.code() == 201))
					{
						// Do not read an unbounded error page or trust HTTP 200 by itself.
						response.body().source().request(1025);
						if (response.body().source().getBuffer().size() <= 1024)
						{
							JsonObject reply = gson.fromJson(response.body().source().readUtf8(), JsonObject.class);
							if (acknowledges(reply, record.id)) status = SubmissionStatus.ACCEPTED;
						}
					}
				}
				catch (IOException | RuntimeException ignored)
				{
					// A timeout or malformed response cannot establish whether storage happened.
				}
				finish(completed, status);
			}
		});
	}

	static boolean acknowledges(JsonObject reply, String id)
	{
		if (reply == null) return false;
		try
		{
			String status = reply.get("status").getAsString();
			return id.equals(reply.get("event_id").getAsString())
				&& "development".equals(reply.get("storage").getAsString())
				&& ("stored".equals(status) || "duplicate".equals(status));
		}
		catch (RuntimeException ignored) { return false; }
	}

	private synchronized void finish(Call call, SubmissionStatus status)
	{
		Consumer<SubmissionStatus> update = pending.remove(call);
		if (update != null) update.accept(status);
	}

	synchronized void cancelPending()
	{
		pending.forEach((call, update) ->
		{
			call.cancel();
			update.accept(SubmissionStatus.CANCELLED);
		});
		pending.clear();
	}

	synchronized void close()
	{
		closed = true;
		cancelPending();
	}
}
