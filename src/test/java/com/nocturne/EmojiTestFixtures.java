package com.nocturne;

import com.google.gson.Gson;
import java.awt.Color;
import java.awt.Graphics2D;
import java.awt.image.BufferedImage;
import java.io.ByteArrayOutputStream;
import java.util.List;
import javax.imageio.ImageIO;

final class EmojiTestFixtures
{
	private EmojiTestFixtures() { }

	static byte[] png(int color) throws Exception
	{
		BufferedImage image = new BufferedImage(20, 20, BufferedImage.TYPE_INT_ARGB);
		Graphics2D graphics = image.createGraphics();
		graphics.setColor(new Color(color, true));
		graphics.fillRect(2, 2, 16, 16);
		graphics.dispose();
		ByteArrayOutputStream output = new ByteArrayOutputStream();
		ImageIO.write(image, "PNG", output);
		return output.toByteArray();
	}

	static byte[] manifest(Gson gson, List<FixtureEntry> entries)
	{
		StringBuilder canonical = new StringBuilder("[");
		for (int index = 0; index < entries.size(); index++)
		{
			if (index > 0) canonical.append(',');
			FixtureEntry entry = entries.get(index);
			canonical.append("{\"animated_source\":").append(entry.animated)
				.append(",\"asset_path\":\"").append(entry.path()).append("\"")
				.append(",\"byte_length\":").append(entry.raw.length)
				.append(",\"height\":20,\"name\":\"").append(entry.name).append("\"")
				.append(",\"sha256\":\"").append(entry.digest()).append("\"")
				.append(",\"width\":20}");
		}
		canonical.append(']');
		String revision = EmojiManifest.sha256(canonical.toString().getBytes(java.nio.charset.StandardCharsets.UTF_8));
		String json = "{\"emojis\":" + canonical + ",\"generated_at\":\"2026-09-28T20:00:00Z\","
			+ "\"revision\":\"" + revision + "\",\"schema_version\":1,\"source_status\":\""
			+ (entries.isEmpty() ? "ok_empty" : "ok") + "\"}";
		return json.getBytes(java.nio.charset.StandardCharsets.UTF_8);
	}

	static final class FixtureEntry
	{
		final String name;
		final byte[] raw;
		final boolean animated;

		FixtureEntry(String name, byte[] raw) { this(name, raw, false); }
		FixtureEntry(String name, byte[] raw, boolean animated)
		{
			this.name = name;
			this.raw = raw;
			this.animated = animated;
		}
		String digest() { return EmojiManifest.sha256(raw); }
		String path() { return "/api/plugin/v1/emojis/assets/" + digest() + ".png"; }
	}
}
