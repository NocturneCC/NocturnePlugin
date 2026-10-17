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

	@Test public void persistsLocalOnlyUnlinkedActivityWithoutSuccessState() throws Exception
	{
		LootRecord record = record("local-only", "De Lena", 12);
		record.activityTrackingNotice = "Local only — account not linked";
		record.submission = SubmissionStatus.ACCEPTED;
		store().append(record);
		LootRecord loaded = store().load("De Lena", 0, 1).records.get(0);
		assertEquals("Local only — account not linked", loaded.activityTrackingNotice);
		assertEquals(SubmissionStatus.ACCEPTED, loaded.submission);
	}

	@Test public void persistsCompletionTimingAndKeepsItSeparateFromDropStatus() throws Exception
	{
		LootRecord source = record("timed", "Tester", 12);
		source.activityDurationMillis = 513_000L;
		source.activityTimingStatus = ActivityTimingStatus.ACCEPTED;
		source.submission = SubmissionStatus.ACCEPTED;
		store().append(source);
		LootRecord loaded = store().load("Tester", 0, 1).records.get(0);
		assertEquals(Long.valueOf(513_000L), loaded.activityDurationMillis);
		assertEquals(ActivityTimingStatus.ACCEPTED, loaded.activityTimingStatus);
		assertEquals(SubmissionStatus.ACCEPTED, loaded.submission);
		LootRecord ineligibleDrop = loaded.withSubmission(SubmissionStatus.INELIGIBLE);
		assertEquals(ActivityTimingStatus.ACCEPTED, ineligibleDrop.activityTimingStatus);
		assertEquals(Long.valueOf(513_000L), ineligibleDrop.activityDurationMillis);
	}

	@Test public void persistsOnlyBoundedDropRejectionDetailsSeparatelyFromTiming() throws Exception
	{
		LootRecord record = record("rejected", "Tester", 15)
			.withSubmission(SubmissionResult.rejected(403, "rsn_not_allowlisted"))
			.withActivityTiming(308_400L, ActivityTimingStatus.ACCEPTED);
		store().append(record);
		LootRecord loaded = store().load("Tester", 0, 1).records.get(0);
		assertEquals(Integer.valueOf(403), loaded.submissionHttpStatus);
		assertEquals("rsn_not_allowlisted", loaded.submissionRejectionReason);
		assertTrue(loaded.submissionLabel().contains("HTTP 403"));
		assertEquals(Long.valueOf(308_400L), loaded.activityDurationMillis);
		assertEquals(ActivityTimingStatus.ACCEPTED, loaded.activityTimingStatus);

		LootRecord unsafe = record("unsafe-reason", "Tester", 16)
			.withSubmission(SubmissionResult.rejected(422, "member id 42 private"));
		assertNull(unsafe.submissionRejectionReason);
		assertFalse(unsafe.submissionLabel().contains("member id"));
	}

	@Test public void standaloneCompletionCardCanBeMergedWithLaterLootWithoutChangingItsIdentity() throws Exception
	{
		BossCompletionContext context = BossCompletionContext.fromActiveSoloActivity(
			"corrupted_gauntlet", "corrupted_gauntlet", "Tester", "store-merge-session", 17,
			1L, Instant.parse("2026-10-13T00:12:31Z"));
		ChallengeObservation observation = ChallengeObservation.createTracked(context, 513_000L);
		LootRecord completion = LootRecord.completion(observation, ActivityTimingStatus.ACCEPTED);
		LootRecord chest = record("late-chest", "Tester", 90);
		chest = new LootRecord(chest.id, "2026-10-13T00:14:01Z", "Tester", "Corrupted Gauntlet",
			chest.items, context.roster, SubmissionStatus.ACCEPTED, PluginActivityCatalog.GAUNTLET);
		LootHistoryStore store = store();
		store.append(completion);
		store.append(chest);
	assertTrue(store.mergeCompletionLoot(completion.id, chest.id, completion.withCompletionLoot(chest)));
		LootHistoryStore.Page page = store.load("Tester", 0, 10);
		assertEquals(1, page.totalCount);
		LootRecord merged = page.records.get(0);
		assertEquals(chest.id, merged.id);
		assertEquals(observation.eventId, merged.activityEventId);
		assertFalse(merged.completionOnly);
		assertEquals("Corrupted Gauntlet", merged.source);
		assertEquals(1, merged.items.size());
		assertEquals(Long.valueOf(513_000L), merged.activityDurationMillis);
		assertEquals(ActivityTimingStatus.ACCEPTED, merged.activityTimingStatus);
		assertEquals(SubmissionStatus.ACCEPTED, merged.submission);
		assertTrue(store.update(chest.withSubmission(SubmissionStatus.INELIGIBLE)));
		LootRecord afterDropUpdate = store.load("Tester", 0, 1).records.get(0);
		assertEquals(SubmissionStatus.INELIGIBLE, afterDropUpdate.submission);
		assertEquals(Long.valueOf(513_000L), afterDropUpdate.activityDurationMillis);
		assertEquals(observation.eventId, afterDropUpdate.activityEventId);
	}

	@Test public void persistsTimedCompletionWithNoLootAsItsOwnHistoryRecord() throws Exception
	{
		BossCompletionContext context = BossCompletionContext.fromActiveSoloActivity(
			"corrupted_gauntlet", "corrupted_gauntlet", "Tester", "store-only-session", 18,
			1L, Instant.parse("2026-10-13T00:12:31Z"));
		ChallengeObservation observation = ChallengeObservation.createTracked(context, 513_000L);
		LootHistoryStore store = store();
		store.append(LootRecord.completion(observation, ActivityTimingStatus.SENDING));
		LootRecord loaded = store.load("Tester", 0, 1).records.get(0);
		assertTrue(loaded.completionOnly);
		assertTrue(loaded.items.isEmpty());
		assertEquals(observation.eventId, loaded.id);
		assertEquals(Long.valueOf(513_000L), loaded.activityDurationMillis);
	}

	@Test public void persistsNumericDoomCompletionAndMergesRewardWithoutTimeCoercion() throws Exception
	{
		Instant completedAt = Instant.parse("2026-10-13T12:00:00Z");
		BossCompletionContext context = BossCompletionContext.fromNumericRun(
			PluginActivityCatalog.DOOM_OF_MOKHAIOTL, "Tester", "doom-store-run", 88,
			1L, completedAt);
		ChallengeObservation observation = ChallengeObservation.createTrackedNumeric(context, 7);
		LootRecord completion = LootRecord.completion(observation, ActivityTimingStatus.ACCEPTED);
		LootHistoryStore store = store();
		store.append(completion);
		LootRecord loaded = store.load("Tester", 0, 1).records.get(0);
		assertTrue(loaded.completionOnly);
		assertEquals(Integer.valueOf(7), loaded.activityNumericValue);
		assertEquals("Deepest level", loaded.activityNumericLabel);
		assertNull(loaded.activityDurationMillis);

		LootRecord loot = new LootRecord("doom-loot", "2026-10-13T12:01:00Z", "Tester",
			"Doom of Mokhaiotl", List.of(new LootItem(100, 1, "Fixture reward")),
			context.roster, SubmissionStatus.ACCEPTED, PluginActivityCatalog.DOOM_OF_MOKHAIOTL);
		loot.completionLinkId = observation.eventId;
		LootRecord merged = loaded.withCompletionLoot(loot);
		store.append(loot);
		assertTrue(store.mergeCompletionLoot(completion.id, loot.id, merged));
		LootRecord after = store.load("Tester", 0, 1).records.get(0);
		assertFalse(after.completionOnly);
		assertEquals(Integer.valueOf(7), after.activityNumericValue);
		assertNull(after.activityDurationMillis);
		assertEquals(observation.eventId, after.activityEventId);
		assertEquals("Fixture reward", after.items.get(0).name);
	}

	@Test public void persistsOrdinaryNpcFeedClassification() throws Exception
	{
		LootRecord source = record("npc", "Tester", 12);
		LootRecord ordinaryNpc = new LootRecord(source.id, source.occurredAt, source.rsn, source.source,
			source.items, source.group, source.submission, source.activityKey,
			source.activityDisplayName, source.modeKey, true);
		store().append(ordinaryNpc);
		assertTrue(store().load("Tester", 0, 1).records.get(0).ordinaryNpcLoot);
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
