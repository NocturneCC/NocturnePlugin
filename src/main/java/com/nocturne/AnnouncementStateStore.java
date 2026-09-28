package com.nocturne;

import com.google.gson.Gson;
import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.channels.FileChannel;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.attribute.PosixFileAttributeView;
import java.nio.file.attribute.PosixFilePermission;
import java.util.EnumSet;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;
import java.math.BigInteger;

/** Bounded ID/revision-only local state. Announcement text is never persisted. */
final class AnnouncementStateStore
{
	static final int SCHEMA_VERSION = 1;
	static final int MAX_ENTRIES = 64;
	private static final Pattern ID = Pattern.compile("[a-z0-9][a-z0-9_-]{0,63}");
	private static final Set<PosixFilePermission> DIRECTORY_PERMISSIONS = EnumSet.of(
		PosixFilePermission.OWNER_READ, PosixFilePermission.OWNER_WRITE,
		PosixFilePermission.OWNER_EXECUTE);
	private static final Set<PosixFilePermission> FILE_PERMISSIONS = EnumSet.of(
		PosixFilePermission.OWNER_READ, PosixFilePermission.OWNER_WRITE);

	private final Path path;
	private final Gson gson;

	AnnouncementStateStore(Path path, Gson gson)
	{
		this.path = path;
		this.gson = gson;
	}

	Map<String, Integer> load()
	{
		LinkedHashMap<String, Integer> result = new LinkedHashMap<>();
		if (!Files.isRegularFile(path) || Files.isSymbolicLink(path)) return result;
		try
		{
			if (Files.size(path) > 16 * 1024) return result;
			JsonObject root = AnnouncementService.parseStrictJson(
				Files.readString(path, StandardCharsets.UTF_8)).getAsJsonObject();
			if (!exact(root, "schema_version", "seen")
				|| root.get("schema_version").getAsInt() != SCHEMA_VERSION) return result;
			JsonArray entries = root.getAsJsonArray("seen");
			if (entries.size() > MAX_ENTRIES) return new LinkedHashMap<>();
			for (JsonElement element : entries)
			{
				JsonObject entry = element.getAsJsonObject();
				if (!exact(entry, "announcement_id", "revision")) return new LinkedHashMap<>();
				String id = entry.get("announcement_id").getAsString();
				JsonElement revisionValue = entry.get("revision");
				if (revisionValue == null || !revisionValue.isJsonPrimitive()
					|| !revisionValue.getAsJsonPrimitive().isNumber()
					|| !revisionValue.toString().matches("[1-9][0-9]*")) return new LinkedHashMap<>();
				BigInteger parsed = new BigInteger(revisionValue.getAsString());
				if (parsed.compareTo(BigInteger.valueOf(Integer.MAX_VALUE)) > 0) return new LinkedHashMap<>();
				int revision = parsed.intValue();
				if (!ID.matcher(id).matches() || revision < 1 || result.put(id, revision) != null)
					return new LinkedHashMap<>();
			}
		}
		catch (IOException | RuntimeException ignored)
		{
			return new LinkedHashMap<>();
		}
		return result;
	}

	void save(Map<String, Integer> seen) throws IOException
	{
		LinkedHashMap<String, Integer> bounded = new LinkedHashMap<>();
		for (Map.Entry<String, Integer> entry : seen.entrySet())
		{
			if (!ID.matcher(entry.getKey()).matches() || entry.getValue() == null || entry.getValue() < 1)
				continue;
			bounded.put(entry.getKey(), entry.getValue());
			while (bounded.size() > MAX_ENTRIES)
			{
				Iterator<String> oldest = bounded.keySet().iterator();
				oldest.next();
				oldest.remove();
			}
		}
		Path parent = path.getParent();
		Files.createDirectories(parent);
		if (!Files.isDirectory(parent) || Files.isSymbolicLink(parent)) throw new IOException("unsafe state directory");
		if (supportsPosix(parent)) Files.setPosixFilePermissions(parent, DIRECTORY_PERMISSIONS);
		JsonObject root = new JsonObject();
		root.addProperty("schema_version", SCHEMA_VERSION);
		JsonArray entries = new JsonArray();
		for (Map.Entry<String, Integer> entry : bounded.entrySet())
		{
			JsonObject value = new JsonObject();
			value.addProperty("announcement_id", entry.getKey());
			value.addProperty("revision", entry.getValue());
			entries.add(value);
		}
		root.add("seen", entries);
		Path temporary = Files.createTempFile(parent, ".announcement-state-", ".tmp");
		try
		{
			Files.writeString(temporary, gson.toJson(root), StandardCharsets.UTF_8);
			if (supportsPosix(parent)) Files.setPosixFilePermissions(temporary, FILE_PERMISSIONS);
			try (FileChannel channel = FileChannel.open(temporary, java.nio.file.StandardOpenOption.WRITE))
			{
				channel.force(true);
			}
			Files.move(temporary, path, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
			if (supportsPosix(parent))
			{
				try (FileChannel directory = FileChannel.open(parent, java.nio.file.StandardOpenOption.READ))
				{
					directory.force(true);
				}
			}
		}
		finally
		{
			Files.deleteIfExists(temporary);
		}
	}

	private static boolean supportsPosix(Path path) throws IOException
	{
		return Files.getFileStore(path).supportsFileAttributeView(PosixFileAttributeView.class);
	}

	private static boolean exact(JsonObject object, String... names)
	{
		if (object == null || object.entrySet().size() != names.length) return false;
		for (String name : names) if (!object.has(name)) return false;
		return true;
	}
}
