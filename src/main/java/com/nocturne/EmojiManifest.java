package com.nocturne;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import java.math.BigInteger;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Collections;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import java.util.regex.Pattern;

/** Strict public emoji manifest. It contains no Discord credential or user data. */
final class EmojiManifest
{
	static final int SCHEMA_VERSION = 1;
	static final int MAX_EMOJIS = 256;
	static final int MAX_MANIFEST_BYTES = 256 * 1024;
	static final int MAX_ASSET_BYTES = 8 * 1024;
	static final int DIMENSION = 20;
	static final Pattern DIGEST = Pattern.compile("[0-9a-f]{64}");
	static final Pattern NAME = Pattern.compile("[a-z0-9_]{1,32}");

	final String revision;
	final String generatedAt;
	final String sourceStatus;
	final List<Entry> entries;
	final byte[] raw;

	private EmojiManifest(String revision, String generatedAt, String sourceStatus,
		List<Entry> entries, byte[] raw)
	{
		this.revision = revision;
		this.generatedAt = generatedAt;
		this.sourceStatus = sourceStatus;
		this.entries = Collections.unmodifiableList(entries);
		this.raw = raw.clone();
	}

	static EmojiManifest parse(byte[] bytes, Gson gson)
	{
		if (bytes == null || bytes.length < 1 || bytes.length > MAX_MANIFEST_BYTES)
			throw new IllegalArgumentException("invalid emoji manifest size");
		final String decoded;
		try { decoded = AnnouncementService.decodeUtf8(bytes); }
		catch (Exception error) { throw new IllegalArgumentException("invalid emoji UTF-8", error); }
		JsonObject root = AnnouncementService.parseStrictJson(decoded).getAsJsonObject();
		if (!exact(root, "schema_version", "revision", "generated_at", "source_status", "emojis")
			|| integer(root, "schema_version", SCHEMA_VERSION, SCHEMA_VERSION) != SCHEMA_VERSION)
			throw new IllegalArgumentException("invalid emoji manifest fields");
		String revision = string(root, "revision");
		String generatedAt = string(root, "generated_at");
		String sourceStatus = string(root, "source_status");
		if (!DIGEST.matcher(revision).matches()
			|| !(sourceStatus.equals("ok") || sourceStatus.equals("ok_empty")))
			throw new IllegalArgumentException("invalid emoji manifest identity");
		Instant.parse(generatedAt);
		JsonArray values = root.getAsJsonArray("emojis");
		if (values.size() > MAX_EMOJIS) throw new IllegalArgumentException("too many emojis");
		List<Entry> entries = new ArrayList<>();
		Set<String> names = new HashSet<>();
		long total = 0;
		String previous = null;
		for (JsonElement element : values)
		{
			JsonObject value = element.getAsJsonObject();
			if (!exact(value, "name", "sha256", "width", "height", "byte_length",
				"animated_source", "asset_path")) throw new IllegalArgumentException("invalid emoji fields");
			String name = string(value, "name");
			String digest = string(value, "sha256");
			int width = (int) integer(value, "width", DIMENSION, DIMENSION);
			int height = (int) integer(value, "height", DIMENSION, DIMENSION);
			int length = (int) integer(value, "byte_length", 1, MAX_ASSET_BYTES);
			JsonElement animatedValue = value.get("animated_source");
			if (animatedValue == null || !animatedValue.isJsonPrimitive()
				|| !animatedValue.getAsJsonPrimitive().isBoolean())
				throw new IllegalArgumentException("invalid animated flag");
			boolean animated = animatedValue.getAsBoolean();
			String path = string(value, "asset_path");
			if (!NAME.matcher(name).matches() || !names.add(name)
				|| !DIGEST.matcher(digest).matches()
				|| !path.equals("/api/plugin/v1/emojis/assets/" + digest + ".png")
				|| (previous != null && previous.compareTo(name) >= 0))
				throw new IllegalArgumentException("invalid emoji entry");
			previous = name;
			total += length;
			if (total > (long) MAX_EMOJIS * MAX_ASSET_BYTES)
				throw new IllegalArgumentException("emoji assets exceed total limit");
			entries.add(new Entry(name, digest, width, height, length, animated, path));
		}
		if (!revision.equals(sha256(canonicalEntries(entries, gson))))
			throw new IllegalArgumentException("emoji revision mismatch");
		return new EmojiManifest(revision, generatedAt, sourceStatus, entries, bytes);
	}

	private static byte[] canonicalEntries(List<Entry> entries, Gson gson)
	{
		JsonArray array = new JsonArray();
		for (Entry entry : entries)
		{
			// Python publication uses sort_keys=True; preserve that lexical order.
			JsonObject value = new JsonObject();
			value.addProperty("animated_source", entry.animatedSource);
			value.addProperty("asset_path", entry.assetPath);
			value.addProperty("byte_length", entry.byteLength);
			value.addProperty("height", entry.height);
			value.addProperty("name", entry.name);
			value.addProperty("sha256", entry.digest);
			value.addProperty("width", entry.width);
			array.add(value);
		}
		return gson.toJson(array).getBytes(StandardCharsets.UTF_8);
	}

	static String sha256(byte[] bytes)
	{
		try
		{
			byte[] digest = MessageDigest.getInstance("SHA-256").digest(bytes);
			StringBuilder value = new StringBuilder(64);
			for (byte item : digest) value.append(String.format("%02x", item & 0xff));
			return value.toString();
		}
		catch (java.security.NoSuchAlgorithmException impossible)
		{
			throw new IllegalStateException(impossible);
		}
	}

	private static boolean exact(JsonObject object, String... names)
	{
		if (object == null || object.entrySet().size() != names.length) return false;
		for (String name : names) if (!object.has(name)) return false;
		return true;
	}

	private static String string(JsonObject object, String name)
	{
		JsonElement value = object.get(name);
		if (value == null || !value.isJsonPrimitive() || !value.getAsJsonPrimitive().isString())
			throw new IllegalArgumentException("invalid string field");
		return value.getAsString();
	}

	private static long integer(JsonObject object, String name, long minimum, long maximum)
	{
		JsonElement value = object.get(name);
		if (value == null || !value.isJsonPrimitive() || !value.getAsJsonPrimitive().isNumber()
			|| !value.toString().matches("-?(0|[1-9][0-9]*)"))
			throw new IllegalArgumentException("invalid integer field");
		BigInteger parsed = new BigInteger(value.getAsString());
		if (parsed.compareTo(BigInteger.valueOf(minimum)) < 0
			|| parsed.compareTo(BigInteger.valueOf(maximum)) > 0)
			throw new IllegalArgumentException("integer field out of range");
		return parsed.longValue();
	}

	static final class Entry
	{
		final String name;
		final String digest;
		final int width;
		final int height;
		final int byteLength;
		final boolean animatedSource;
		final String assetPath;

		Entry(String name, String digest, int width, int height, int byteLength,
			boolean animatedSource, String assetPath)
		{
			this.name = name;
			this.digest = digest;
			this.width = width;
			this.height = height;
			this.byteLength = byteLength;
			this.animatedSource = animatedSource;
			this.assetPath = assetPath;
		}
	}
}
