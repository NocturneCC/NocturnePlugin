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
import java.nio.file.attribute.PosixFileAttributeView;
import java.nio.file.attribute.PosixFilePermission;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Base64;
import java.util.EnumSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;

/** Versioned, per-character local event storage. Never participates in capture or submission. */
final class LootHistoryStore
{
	static final int SCHEMA_VERSION = 2;
	private final Path root;
	private final Gson gson;
	private final Boolean posixOverride;
	private static final Set<PosixFilePermission> DIRECTORY_PERMISSIONS = EnumSet.of(
		PosixFilePermission.OWNER_READ, PosixFilePermission.OWNER_WRITE,
		PosixFilePermission.OWNER_EXECUTE);
	private static final Set<PosixFilePermission> FILE_PERMISSIONS = EnumSet.of(
		PosixFilePermission.OWNER_READ, PosixFilePermission.OWNER_WRITE);

	LootHistoryStore(Path root, Gson gson)
	{
		this(root, gson, null);
	}

	/** Test seam for filesystems whose POSIX capability differs from the host running tests. */
	LootHistoryStore(Path root, Gson gson, Boolean posixOverride)
	{
		this.root = root;
		this.gson = gson;
		this.posixOverride = posixOverride;
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

	synchronized boolean update(LootRecord record) throws IOException
	{
		Path path = pathFor(record.rsn);
		if (!Files.isRegularFile(path) || !contains(path, record.id)) return false;
		rewrite(path, line ->
		{
			LootRecord existing = parseRecord(line);
			if (existing == null || !existing.id.equals(record.id)) return line;
			LootRecord replacement = record;
			if (replacement.activityDurationMillis == null && existing.activityDurationMillis != null)
				replacement = replacement.withActivityTiming(existing.activityDurationMillis,
					existing.activityTimingStatus);
		if (replacement.submissionHttpStatus == null)
			replacement.submissionHttpStatus = existing.submissionHttpStatus;
		if (replacement.submissionRejectionReason == null)
			replacement.submissionRejectionReason = existing.submissionRejectionReason;
			if (replacement.activityNumericValue == null && existing.activityNumericValue != null)
				replacement = replacement.withActivityCompletion(existing.activityNumericValue,
					existing.activityNumericLabel, existing.activityNumericUnit,
					existing.activityTimingStatus);
			if (replacement.activityEventId == null) replacement.activityEventId = existing.activityEventId;
			return gson.toJson(toJson(replacement));
		}, null);
		return true;
	}

	/** Replace the appended loot event with its existing completion card in one rewrite. */
	synchronized boolean mergeCompletionLoot(String completionId, String lootId, LootRecord merged) throws IOException
	{
		Path path = pathFor(merged.rsn);
		if (!Files.isRegularFile(path) || Files.isSymbolicLink(path)) return false;
		boolean[] foundLoot = {false};
		boolean[] foundCompletion = {false};
		rewrite(path, line ->
		{
			LootRecord existing = parseRecord(line);
			if (existing == null) return line;
			if (existing.id.equals(completionId))
			{
				foundCompletion[0] = true;
				return gson.toJson(toJson(merged));
			}
			if (existing.id.equals(lootId)) { foundLoot[0] = true; return null; }
			return line;
		}, null);
		if (!foundLoot[0]) return false;
		if (!foundCompletion[0]) append(merged);
		return true;
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
		ensureDirectory();
		if (!Files.exists(path)) writeNew(path, header(rsn));
		if (!Files.isRegularFile(path) || Files.isSymbolicLink(path)) throw new IOException("unsafe history path");
		hardenFile(path);
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
		try
		{
			hardenFile(temp);
			try (BufferedReader reader = Files.newBufferedReader(path, StandardCharsets.UTF_8);
				 BufferedWriter writer = Files.newBufferedWriter(temp, StandardCharsets.UTF_8))
			{
				String line;
				while ((line = reader.readLine()) != null)
				{
					String transformed = transform.apply(line);
					if (transformed != null)
					{
						writer.write(transformed);
						writer.newLine();
					}
				}
				if (appendedLine != null)
				{
					writer.write(appendedLine);
					writer.newLine();
				}
			}
			force(temp);
			Files.move(temp, path, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
			hardenFile(path);
			forceDirectory();
		}
		finally
		{
			Files.deleteIfExists(temp);
		}
	}

	private void writeNew(Path path, String content) throws IOException
	{
		ensureDirectory();
		Path temp = Files.createTempFile(root, path.getFileName().toString(), ".tmp");
		try
		{
			hardenFile(temp);
			try (BufferedWriter writer = Files.newBufferedWriter(temp, StandardCharsets.UTF_8))
			{
				writer.write(content);
				writer.newLine();
			}
			force(temp);
			Files.move(temp, path, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
			hardenFile(path);
			forceDirectory();
		}
		finally
		{
			Files.deleteIfExists(temp);
		}
	}

	private void ensureDirectory() throws IOException
	{
		Files.createDirectories(root);
		if (!Files.isDirectory(root) || Files.isSymbolicLink(root)) throw new IOException("unsafe history directory");
		if (supportsPosix()) Files.setPosixFilePermissions(root, DIRECTORY_PERMISSIONS);
	}

	private void hardenFile(Path path) throws IOException
	{
		if (supportsPosix()) Files.setPosixFilePermissions(path, FILE_PERMISSIONS);
	}

	private boolean supportsPosix() throws IOException
	{
		return posixOverride != null ? posixOverride
			: Files.getFileStore(root).supportsFileAttributeView(PosixFileAttributeView.class);
	}

	private void force(Path path) throws IOException
	{
		try (FileChannel channel = FileChannel.open(path, StandardOpenOption.WRITE)) { channel.force(true); }
	}

	private void forceDirectory() throws IOException
	{
		// Windows/non-POSIX providers cannot open directories as FileChannels.
		// Their inherited per-user ACLs are retained; file contents are still
		// forced and replacement remains atomic on the underlying provider.
		if (!supportsPosix()) return;
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
		if (record.activityKey != null) object.addProperty("activityKey", record.activityKey);
		if (record.activityDisplayName != null) object.addProperty("activityDisplayName", record.activityDisplayName);
		if (record.modeKey != null) object.addProperty("modeKey", record.modeKey);
		object.add("items", gson.toJsonTree(record.items));
		object.add("group", gson.toJsonTree(record.group));
		object.addProperty("submission", record.submission.name());
		if (record.submissionHttpStatus != null)
			object.addProperty("submissionHttpStatus", record.submissionHttpStatus);
		if (SubmissionResult.isSafeReason(record.submissionRejectionReason))
			object.addProperty("submissionRejectionReason", record.submissionRejectionReason);
		if (record.ordinaryNpcLoot) object.addProperty("ordinaryNpcLoot", true);
		if (record.completionOnly) object.addProperty("completionOnly", true);
		if (record.activityEventId != null) object.addProperty("activityEventId", record.activityEventId);
		if ("Local only — account not linked".equals(record.activityTrackingNotice))
			object.addProperty("activityTrackingNotice", record.activityTrackingNotice);
		if (record.activityDurationMillis != null)
			object.addProperty("activityDurationMillis", record.activityDurationMillis);
		if (record.activityNumericValue != null)
		{
			object.addProperty("activityNumericValue", record.activityNumericValue);
			object.addProperty("activityNumericLabel", record.activityNumericLabel);
			object.addProperty("activityNumericUnit", record.activityNumericUnit);
		}
		if (record.activityTimingStatus != null)
			object.addProperty("activityTimingStatus", record.activityTimingStatus.name());
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
			boolean completionOnly = object.has("completionOnly") && object.get("completionOnly").isJsonPrimitive()
				&& object.getAsJsonPrimitive("completionOnly").isBoolean()
				&& object.get("completionOnly").getAsBoolean();
			SubmissionStatus status = object.has("submission")
				? SubmissionStatus.valueOf(object.get("submission").getAsString()) : SubmissionStatus.LOCAL;
			GroupSnapshot group = object.has("group")
				? gson.fromJson(object.get("group"), GroupSnapshot.class)
				: GroupSnapshot.unavailable("Not stored by history schema v1.");
			if (id.isEmpty() || rsn.isEmpty() || source.isEmpty() || items == null
				|| items.length == 0 && !completionOnly
				|| group == null || group.status == null) return null;
			for (LootItem item : items)
			{
				if (item == null || item.id <= 0 || item.quantity <= 0 || item.name == null
					|| item.name.isEmpty() || item.unitPriceGp < 0) return null;
			}
			String activityKey = object.has("activityKey") && !object.get("activityKey").isJsonNull()
				? object.get("activityKey").getAsString() : null;
			String activityDisplayName = object.has("activityDisplayName")
				&& !object.get("activityDisplayName").isJsonNull()
				? object.get("activityDisplayName").getAsString() : null;
			String modeKey = object.has("modeKey") && !object.get("modeKey").isJsonNull()
				? object.get("modeKey").getAsString() : null;
			LootRecord record = new LootRecord(id, occurred, rsn, source, List.of(items), group, status,
				activityKey, activityDisplayName, modeKey,
				object.has("ordinaryNpcLoot") && object.get("ordinaryNpcLoot").isJsonPrimitive()
					&& object.getAsJsonPrimitive("ordinaryNpcLoot").isBoolean()
					&& object.get("ordinaryNpcLoot").getAsBoolean());
			if (completionOnly)
				record = new LootRecord(id, occurred, rsn, source, List.of(items), group, status,
					activityKey, activityDisplayName, modeKey, false, true);
			if (object.has("submissionHttpStatus") && object.get("submissionHttpStatus").isJsonPrimitive())
			{
				int statusCode = object.get("submissionHttpStatus").getAsInt();
				if (statusCode >= 400 && statusCode <= 599) record.submissionHttpStatus = statusCode;
			}
			if (object.has("submissionRejectionReason")
				&& object.get("submissionRejectionReason").isJsonPrimitive())
			{
				String reason = object.get("submissionRejectionReason").getAsString();
				if (SubmissionResult.isSafeReason(reason)) record.submissionRejectionReason = reason;
			}
			if (object.has("activityEventId") && object.get("activityEventId").isJsonPrimitive()
				&& object.getAsJsonPrimitive("activityEventId").isString())
			{
				String activityEventId = object.get("activityEventId").getAsString();
				if (activityEventId.length() > 64) return null;
				record.activityEventId = activityEventId;
			}
			if (object.has("activityTrackingNotice") && object.get("activityTrackingNotice").isJsonPrimitive()
				&& object.getAsJsonPrimitive("activityTrackingNotice").isString()
				&& "Local only — account not linked".equals(object.get("activityTrackingNotice").getAsString()))
			{
				record.activityTrackingNotice = "Local only — account not linked";
			}
			if (object.has("activityDurationMillis") && object.has("activityTimingStatus"))
			{
				long duration = object.get("activityDurationMillis").getAsLong();
				ActivityTimingStatus timingStatus = ActivityTimingStatus.valueOf(
					object.get("activityTimingStatus").getAsString());
				if (duration <= 0 || duration > ChallengeObservation.MAX_DURATION_MILLIS) return null;
				record.activityDurationMillis = duration;
				record.activityTimingStatus = timingStatus;
			}
			if (object.has("activityNumericValue") && object.has("activityNumericLabel")
				&& object.has("activityNumericUnit") && object.has("activityTimingStatus"))
			{
				int value = object.get("activityNumericValue").getAsInt();
				String label = object.get("activityNumericLabel").getAsString();
				String unit = object.get("activityNumericUnit").getAsString();
				ActivityTimingStatus timingStatus = ActivityTimingStatus.valueOf(
					object.get("activityTimingStatus").getAsString());
				if (value < 1 || value > 10_000 || label.isEmpty() || label.length() > 64
					|| unit.isEmpty() || unit.length() > 32) return null;
				record.activityNumericValue = value;
				record.activityNumericLabel = label;
				record.activityNumericUnit = unit;
				record.activityTimingStatus = timingStatus;
			}
			return record;
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
