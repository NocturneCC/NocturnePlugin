package com.nocturne;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import java.io.IOException;
import java.util.concurrent.TimeUnit;
import okhttp3.Call;
import okhttp3.Callback;
import okhttp3.OkHttpClient;
import okhttp3.Request;
import okhttp3.Response;

/** Reads the public, cacheable Plugin Tracking catalog; no credentials are sent. */
final class PluginTrackingConfigService implements AutoCloseable
{
	private static final long MAX_BYTES = 128 * 1024;
	private final OkHttpClient http;
	private final Gson gson;
	private Call call;
	private boolean closed;

	PluginTrackingConfigService(OkHttpClient http, Gson gson)
	{
		this.http = http.newBuilder().followRedirects(false).followSslRedirects(false)
			.callTimeout(8, TimeUnit.SECONDS).build();
		this.gson = gson;
	}

	synchronized void refresh()
	{
		if (closed || call != null) return;
		Request request = new Request.Builder().url(PluginActivityCatalog.CONFIG_ENDPOINT).get().build();
		call = http.newCall(request);
		call.enqueue(new Callback()
		{
			@Override public void onFailure(Call failed, IOException error) { clear(failed); }
			@Override public void onResponse(Call completed, Response response)
			{
				try (Response ignored = response)
				{
					if (!response.isSuccessful() || response.body() == null) return;
					response.body().source().request(MAX_BYTES + 1);
					if (response.body().source().getBuffer().size() > MAX_BYTES) return;
					JsonObject root = gson.fromJson(response.body().source().readUtf8(), JsonObject.class);
					if (root != null && root.has("config"))
					{
						synchronized (PluginTrackingConfigService.this)
						{
							if (!closed) PluginActivityCatalog.applyPublicConfig(root.getAsJsonObject("config"));
						}
					}
				}
				catch (IOException | RuntimeException ignored) { }
				finally { clear(completed); }
			}
		});
	}

	private synchronized void clear(Call completed)
	{
		if (call == completed) call = null;
	}

	@Override public synchronized void close()
	{
		closed = true;
		if (call != null) call.cancel();
		call = null;
	}
}
