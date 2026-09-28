package com.nocturne;

import com.google.gson.Gson;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;
import java.nio.file.attribute.PosixFilePermissions;
import org.junit.Test;
import static org.junit.Assert.*;

public class AnnouncementStateStoreTest
{
	@Test public void stateIsBoundedAndContainsOnlyIdsAndRevisions() throws Exception
	{
		Path root = Files.createTempDirectory("nocturne-announcement-state");
		Path path = root.resolve("nested").resolve("state.json");
		AnnouncementStateStore store = new AnnouncementStateStore(path, new Gson());
		Map<String, Integer> seen = new LinkedHashMap<>();
		for (int index = 0; index < 80; index++) seen.put("announcement-" + index, index + 1);
		store.save(seen);
		Map<String, Integer> loaded = store.load();
		assertEquals(AnnouncementStateStore.MAX_ENTRIES, loaded.size());
		assertFalse(loaded.containsKey("announcement-0"));
		assertEquals(Integer.valueOf(80), loaded.get("announcement-79"));
		String stored = Files.readString(path, StandardCharsets.UTF_8);
		assertFalse(stored.contains("message"));
		assertFalse(stored.contains("rsn"));
		assertFalse(stored.contains("telemetry"));
		if (Files.getFileStore(path).supportsFileAttributeView("posix"))
		{
			assertEquals(PosixFilePermissions.fromString("rw-------"), Files.getPosixFilePermissions(path));
			assertEquals(PosixFilePermissions.fromString("rwx------"),
				Files.getPosixFilePermissions(path.getParent()));
		}
	}

	@Test public void corruptOversizedAndUnexpectedStateFailOpenToEmpty() throws Exception
	{
		Path root = Files.createTempDirectory("nocturne-announcement-corrupt");
		Path path = root.resolve("state.json");
		AnnouncementStateStore store = new AnnouncementStateStore(path, new Gson());
		for (String content : new String[] {
			"not-json",
			"{\"schema_version\":2,\"seen\":[]}",
			"{\"schema_version\":1,\"seen\":[],\"extra\":true}",
			"{\"schema_version\":1,\"schema_version\":1,\"seen\":[]}",
			"{\"schema_version\":1,\"seen\":[{\"announcement_id\":\"id\",\"revision\":1.5}]}",
			"{\"schema_version\":1,\"seen\":[{\"announcement_id\":\"bad id\",\"revision\":1}]}"
		})
		{
			Files.writeString(path, content, StandardCharsets.UTF_8);
			assertTrue(store.load().isEmpty());
		}
		Map<String, Integer> tooMany = new LinkedHashMap<>();
		for (int index = 0; index <= AnnouncementStateStore.MAX_ENTRIES; index++)
			tooMany.put("id-" + index, 1);
		StringBuilder raw = new StringBuilder("{\"schema_version\":1,\"seen\":[");
		for (String id : tooMany.keySet())
		{
			if (raw.charAt(raw.length() - 1) != '[') raw.append(',');
			raw.append("{\"announcement_id\":\"").append(id).append("\",\"revision\":1}");
		}
		raw.append("]}");
		Files.writeString(path, raw, StandardCharsets.UTF_8);
		assertTrue(store.load().isEmpty());
	}
}
