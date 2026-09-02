package com.nocturne;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.io.BufferedReader;
import java.io.BufferedWriter;
import java.io.IOException;
import java.nio.channels.FileChannel;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.StandardOpenOption;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Base64;
import java.util.List;
import java.util.Locale;

/** Versioned, per-character local event storage. Never participates in capture or submission. */
final class LootHistoryStore
{
	static final int SCHEMA_VERSION = 2;
	private final Path root;
	private final Gson gson;

	LootHistoryStore(Path root, Gson gson)
	{
		this.root = root;
		this.gson = gson;
	}

	static String normalizeRsn(String rsn)
	{
		return String.join("_", rsn.trim().toLowerCase(Locale.ROOT)
			.replace('-', ' ').replace('_', ' ').split("\\s+"));
	}

	Path pathFor(String rsn)
	{
		String normalized = normalizeRsn(rsn);
		String safe = normalized.replaceAll("[^a-z0-9_]", "_");
		if (safe.isEmpty()) safe = "account";
		return root.resolve(safe + "-" + digest(normalized).substring(0, 16) + ".jsonl");
	}

	synchronized void append(LootRecord record) throws IOException
	{
		Path path = pathFor(record.rsn);
		ensureFile(path, record.rsn);
		if (contains(path, record.id)) return;
		rewrite(path, line -> line, gson.toJson(toJson(record)));
	}

	synchronized void update(LootRecord record) throws IOException
	{
		Path path = pathFor(record.rsn);
		if (!Files.isRegularFile(path)) return;
		rewrite(path, line ->
		{
			LootRecord existing = parseRecord(line);
			return existing != null && existing.id.equals(record.id) ? gson.toJson(toJson(record)) : line;
		}, null);
	}

	synchronized Page load(String rsn, int offset, int limit) throws IOException
	{
		if (offset < 0 || limit < 1) throw new IllegalArgumentException("invalid page");
		Path path = pathFor(rsn);
		if (!Files.isRegularFile(path)) return new Page(List.of(), 0, 0, false, 0);
		if (Files.isSymbolicLink(path)) throw new IOException("unsafe history path");
		int capacity = Math.addExact(offset, limit);
		ArrayDeque<LootRecord> newest = new ArrayDeque<>(capacity);
		int valid = 0;
		int malformed = 0;
		try (BufferedReader reader = Files.newBufferedReader(path, StandardCharsets.UTF_8))
		{
			String line;
			while ((line = reader.readLine()) != null)
			{
				LootRecord record = parseRecord(line);
				if (record == null)
				{
					if (!isHeader(line)) malformed++;
					continue;
				}
				if (!normalizeRsn(rsn).equals(normalizeRsn(record.rsn))) continue;
				valid++;
				newest.addLast(record);
				if (newest.size() > capacity) newest.removeFirst();
			}
		}
		List<LootRecord> chronological = new ArrayList<>(newest);
		List<LootRecord> page = new ArrayList<>();
		for (int i = chronological.size() - 1 - offset; i >= 0 && page.size() < limit; i--)
		{
			page.add(chronological.get(i));
		}
		return new Page(List.copyOf(page), valid, Files.size(path), offset + page.size() < valid, malformed);
	}

	synchronized void clear(String rsn) throws IOException
	{
		Path path = pathFor(rsn);
		if (Files.isSymbolicLink(path)) throw new IOException("unsafe history path");
		if (Files.exists(path)) writeNew(path, header(rsn));
	}

	private void ensureFile(Path path, String rsn) throws IOException
	{
		Files.createDirectories(root);
		if (!Files.exists(path)) writeNew(path, header(rsn));
		if (!Files.isRegularFile(path) || Files.isSymbolicLink(path)) throw new IOException("unsafe history path");
	}

	private boolean contains(Path path, String id) throws IOException
	{
		try (BufferedReader reader = Files.newBufferedReader(path, StandardCharsets.UTF_8))
		{
			String line;
			while ((line = reader.readLine()) != null)
			{
				LootRecord record = parseRecord(line);
				if (record != null && record.id.equals(id)) return true;
			}
		}
		return false;
	}

	private interface LineTransform { String apply(String line); }

	private void rewrite(Path path, LineTransform transform, String appendedLine) throws IOException
	{
		Path temp = Files.createTempFile(root, path.getFileName().toString(), ".tmp");
		try (BufferedReader reader = Files.newBufferedReader(path, StandardCharsets.UTF_8);
			 BufferedWriter writer = Files.newBufferedWriter(temp, StandardCharsets.UTF_8))
		{
			String line;
			while ((line = reader.readLine()) != null)
			{
				writer.write(transform.apply(line));
				writer.newLine();
			}
			if (appendedLine != null)
			{
				writer.write(appendedLine);
				writer.newLine();
			}
		}
		force(temp);
		Files.move(temp, path, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
		forceDirectory();
	}

	private void writeNew(Path path, String content) throws IOException
	{
		Files.createDirectories(root);
		Path temp = Files.createTempFile(root, path.getFileName().toString(), ".tmp");
		Files.writeString(temp, content + "\n", StandardCharsets.UTF_8);
		force(temp);
		Files.move(temp, path, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
		forceDirectory();
	}

	private void force(Path path) throws IOException
	{
		try (FileChannel channel = FileChannel.open(path, StandardOpenOption.WRITE)) { channel.force(true); }
	}

	private void forceDirectory() throws IOException
	{
		try (FileChannel channel = FileChannel.open(root, StandardOpenOption.READ)) { channel.force(true); }
	}

	private String header(String rsn)
	{
		JsonObject object = new JsonObject();
		object.addProperty("kind", "header");
		object.addProperty("schema_version", SCHEMA_VERSION);
		object.addProperty("normalized_rsn", normalizeRsn(rsn));
		return gson.toJson(object);
	}

	private boolean isHeader(String line)
	{
		try { return "header".equals(new JsonParser().parse(line).getAsJsonObject().get("kind").getAsString()); }
		catch (RuntimeException e) { return false; }
	}

	private JsonObject toJson(LootRecord record)
	{
		JsonObject object = new JsonObject();
		object.addProperty("kind", "record");
		object.addProperty("schema_version", SCHEMA_VERSION);
		object.addProperty("id", record.id);
		object.addProperty("occurredAt", record.occurredAt);
		object.addProperty("rsn", record.rsn);
		object.addProperty("source", record.source);
		object.add("items", gson.toJsonTree(record.items));
		object.add("group", gson.toJsonTree(record.group));
		object.addProperty("submission", record.submission.name());
		return object;
	}

	private LootRecord parseRecord(String line)
	{
		try
		{
			JsonObject object = new JsonParser().parse(line).getAsJsonObject();
			if (!"record".equals(object.get("kind").getAsString())) return null;
			int version = object.has("schema_version") ? object.get("schema_version").getAsInt() : 1;
			if (version < 1 || version > SCHEMA_VERSION) return null;
			String id = object.get("id").getAsString();
			String occurred = object.get("occurredAt").getAsString();
			String rsn = object.get("rsn").getAsString();
			String source = object.get("source").getAsString();
			LootItem[] items = gson.fromJson(object.get("items"), LootItem[].class);
			SubmissionStatus status = object.has("submission")
				? SubmissionStatus.valueOf(object.get("submission").getAsString()) : SubmissionStatus.LOCAL;
			GroupSnapshot group = object.has("group")
				? gson.fromJson(object.get("group"), GroupSnapshot.class)
				: GroupSnapshot.unavailable("Not stored by history schema v1.");
			if (id.isEmpty() || rsn.isEmpty() || source.isEmpty() || items == null || items.length == 0
				|| group == null || group.status == null) return null;
			for (LootItem item : items)
			{
				if (item == null || item.id <= 0 || item.quantity <= 0 || item.name == null
					|| item.name.isEmpty() || item.unitPriceGp < 0) return null;
			}
			return new LootRecord(id, occurred, rsn, source, List.of(items), group, status);
		}
		catch (RuntimeException e) { return null; }
	}

	private static String digest(String value)
	{
		try
		{
			return Base64.getUrlEncoder().withoutPadding().encodeToString(
				MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8)));
		}
		catch (NoSuchAlgorithmException e) { throw new IllegalStateException(e); }
	}

	static final class Page
	{
		final List<LootRecord> records;
		final int totalCount;
		final long storageBytes;
		final boolean hasOlder;
		final int malformedRecords;
		Page(List<LootRecord> records, int totalCount, long storageBytes, boolean hasOlder, int malformedRecords)
		{
			this.records = records; this.totalCount = totalCount; this.storageBytes = storageBytes;
			this.hasOlder = hasOlder; this.malformedRecords = malformedRecords;
		}
	}
}
