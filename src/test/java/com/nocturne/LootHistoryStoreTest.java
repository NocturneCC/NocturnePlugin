package com.nocturne;

import com.google.gson.Gson;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.nio.file.attribute.PosixFilePermission;
import java.time.Instant;
import java.util.EnumSet;
import java.util.List;
import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;
import static org.junit.Assert.*;

public class LootHistoryStoreTest
{
	@Rule public TemporaryFolder temporary = new TemporaryFolder();

	private LootHistoryStore store() throws Exception
	{
		return new LootHistoryStore(temporary.getRoot().toPath().resolve("history"), new Gson());
	}

	private static LootRecord record(String id, String rsn, int second)
	{
		LootItem item = LootItem.derived(28281, 2, "Magus vestige", 500000,
			"runelite_derived_full_output", "dt2_magus_vestige", 1, 28313,
			"Magus ring", 500000);
		return new LootRecord(id, Instant.ofEpochSecond(second).toString(), rsn, "Source " + second,
			List.of(item), GroupSnapshot.unavailable("local"), SubmissionStatus.INELIGIBLE);
	}

	@Test public void persistsAcrossStoreRestartAndKeepsStructuredMetadata() throws Exception
	{
		LootHistoryStore first = store();
		first.append(record("one", "First", 1));
		LootHistoryStore second = new LootHistoryStore(temporary.getRoot().toPath().resolve("history"), new Gson());
		LootRecord loaded = second.load("First", 0, 50).records.get(0);
		assertEquals("one", loaded.id);
		assertEquals(500000, loaded.items.get(0).unitPriceGp);
		assertEquals("dt2_magus_vestige", loaded.items.get(0).valuationRuleId);
		assertEquals(Integer.valueOf(28313), loaded.items.get(0).finishedOutputItemId);
		assertEquals(SubmissionStatus.INELIGIBLE, loaded.submission);
		assertFalse(Files.readString(second.pathFor("First")).toLowerCase().contains("screenshot"));
	}

	@Test public void reportsExactPostWriteFileSize() throws Exception
	{
		LootHistoryStore store = store();
		store.append(record("one", "First", 1));
		LootHistoryStore.Page page = store.load("First", 0, 1);
		assertEquals(Files.size(store.pathFor("First")), page.storageBytes);
		assertTrue(page.storageBytes > 0);
	}

	@Test public void writeFailureDoesNotCreateAFalseStoredRecord() throws Exception
	{
		Path blockedRoot = temporary.getRoot().toPath().resolve("not-a-directory");
		Files.writeString(blockedRoot, "blocked");
		LootHistoryStore store = new LootHistoryStore(blockedRoot, new Gson());
		try
		{
			store.append(record("one", "First", 1));
			fail("expected write failure");
		}
		catch (java.io.IOException expected)
		{
			assertFalse(Files.exists(store.pathFor("First")));
		}
	}

	@Test public void persistedLoadPathCannotInvokeCaptureOrSubmission() throws Exception
	{
		String source = Files.readString(Path.of("src/main/java/com/nocturne/NocturnePlugin.java"));
		String loadPath = source.substring(source.indexOf("private synchronized void loadHistory"),
			source.indexOf("private synchronized void clearHistory"));
		assertTrue(loadPath.contains("view.showHistory"));
		assertFalse(loadPath.contains("recordLoot("));
		assertFalse(loadPath.contains("submissions"));
		assertFalse(loadPath.contains("drawManager"));
		assertFalse(loadPath.contains("ScreenshotCapture"));
	}

	@Test public void isolatesNormalizedAccountsAndSwitchesBack() throws Exception
	{
		LootHistoryStore store = store();
		store.append(record("a", "First Name", 1));
		store.append(record("b", "Second", 2));
		assertEquals("a", store.load("first_name", 0, 50).records.get(0).id);
		assertEquals("b", store.load("Second", 0, 50).records.get(0).id);
		assertNotEquals(store.pathFor("First Name"), store.pathFor("Second"));
	}

	@Test public void accountKeyCannotEscapeHistoryDirectory() throws Exception
	{
		LootHistoryStore store = store();
		Path path = store.pathFor("../../First\\Second");
		assertEquals(path.getParent(), temporary.getRoot().toPath().resolve("history"));
		assertFalse(path.getFileName().toString().contains(".."));
	}

	@Test public void pagesNewestFirstWithoutDeletingHistory() throws Exception
	{
		LootHistoryStore store = store();
		for (int i = 0; i < 120; i++) store.append(record("id" + i, "First", i + 1));
		LootHistoryStore.Page first = store.load("First", 0, 50);
		LootHistoryStore.Page second = store.load("First", 50, 50);
		assertEquals(120, first.totalCount);
		assertEquals("id119", first.records.get(0).id);
		assertEquals("id70", first.records.get(49).id);
		assertEquals("id69", second.records.get(0).id);
		assertTrue(first.hasOlder);
		assertTrue(second.hasOlder);
	}

	@Test public void clearAffectsOnlySelectedAccount() throws Exception
	{
		LootHistoryStore store = store();
		store.append(record("a", "First", 1));
		store.append(record("b", "Second", 2));
		store.clear("First");
		assertEquals(0, store.load("First", 0, 50).totalCount);
		assertEquals(1, store.load("Second", 0, 50).totalCount);
	}

	@Test public void malformedTailAndInterruptedTempPreserveValidRecords() throws Exception
	{
		LootHistoryStore store = store();
		store.append(record("a", "First", 1));
		Files.writeString(store.pathFor("First"), "{broken\n", StandardCharsets.UTF_8,
			java.nio.file.StandardOpenOption.APPEND);
		Files.writeString(store.pathFor("First").resolveSibling("interrupted.tmp"), "partial");
		LootHistoryStore.Page page = store.load("First", 0, 50);
		assertEquals(1, page.totalCount);
		assertEquals(1, page.malformedRecords);
	}

	@Test public void interruptedUnterminatedAppendDoesNotConsumeNextRecord() throws Exception
	{
		LootHistoryStore store = store();
		store.append(record("a", "First", 1));
		Files.writeString(store.pathFor("First"), "{partial", StandardCharsets.UTF_8,
			java.nio.file.StandardOpenOption.APPEND);
		store.append(record("b", "First", 2));
		LootHistoryStore.Page page = store.load("First", 0, 50);
		assertEquals(2, page.totalCount);
		assertEquals("b", page.records.get(0).id);
	}

	@Test public void schemaOneMigratesDefaultsOnRead() throws Exception
	{
		LootHistoryStore store = store();
		Path path = store.pathFor("First");
		Files.createDirectories(path.getParent());
		String old = "{\"kind\":\"record\",\"schema_version\":1,\"id\":\"old\",\"occurredAt\":\"1970-01-01T00:00:01Z\",\"rsn\":\"First\",\"source\":\"Old\",\"items\":[{\"id\":526,\"quantity\":1,\"name\":\"Bones\",\"unitPriceGp\":31}]}\n";
		Files.writeString(path, old);
		LootRecord loaded = store.load("First", 0, 50).records.get(0);
		assertEquals(SubmissionStatus.LOCAL, loaded.submission);
		assertEquals(GroupSnapshot.Status.UNAVAILABLE, loaded.group.status);
	}

	@Test public void duplicateEventIsStoredOnlyOnceAndStatusCanBeUpdated() throws Exception
	{
		LootHistoryStore store = store();
		LootRecord record = record("same", "First", 1);
		store.append(record);
		store.append(record);
		record.submission = SubmissionStatus.REJECTED;
		store.update(record);
		LootHistoryStore.Page page = store.load("First", 0, 50);
		assertEquals(1, page.totalCount);
		assertEquals(SubmissionStatus.REJECTED, page.records.get(0).submission);
	}

	@Test public void nonPosixStorageSkipsUnsupportedDirectoryFsyncAndRemainsReplaceable() throws Exception
	{
		Path root = temporary.getRoot().toPath().resolve("windows-history");
		LootHistoryStore store = new LootHistoryStore(root, new Gson(), false);
		store.append(record("one", "First", 1));
		store.append(record("two", "First", 2));
		Path history = store.pathFor("First");
		Path moved = history.resolveSibling("moved.jsonl");
		Files.move(history, moved, StandardCopyOption.ATOMIC_MOVE);
		Files.delete(moved);
		Files.delete(root);
	}

	@Test public void posixStorageHardensDirectoryAndFilesToOwnerOnly() throws Exception
	{
		Path root = temporary.getRoot().toPath().resolve("posix-history");
		LootHistoryStore store = new LootHistoryStore(root, new Gson());
		store.append(record("one", "First", 1));
		if (Files.getFileStore(root).supportsFileAttributeView("posix"))
		{
			assertEquals(EnumSet.of(PosixFilePermission.OWNER_READ, PosixFilePermission.OWNER_WRITE,
				PosixFilePermission.OWNER_EXECUTE), Files.getPosixFilePermissions(root));
			assertEquals(EnumSet.of(PosixFilePermission.OWNER_READ, PosixFilePermission.OWNER_WRITE),
				Files.getPosixFilePermissions(store.pathFor("First")));
		}
		else
		{
			assertEquals(1, store.load("First", 0, 1).totalCount);
		}
	}
}
