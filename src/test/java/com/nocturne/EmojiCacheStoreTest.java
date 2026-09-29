package com.nocturne;

import com.google.gson.Gson;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.PosixFilePermission;
import java.util.List;
import java.util.Map;
import org.junit.Test;
import static org.junit.Assert.*;

public class EmojiCacheStoreTest
{
	@Test public void cacheIsAtomicOwnerOnlyBoundedAndRecoversFromCorruption() throws Exception
	{
		Path root = Files.createTempDirectory("emoji-cache-test");
		try
		{
			Gson gson = new Gson();
			byte[] image = EmojiTestFixtures.png(0xff113355);
			EmojiTestFixtures.FixtureEntry fixture = new EmojiTestFixtures.FixtureEntry("wave", image);
			EmojiManifest manifest = EmojiManifest.parse(
				EmojiTestFixtures.manifest(gson, List.of(fixture)), gson);
			EmojiCacheStore store = new EmojiCacheStore(root, gson);
			store.save(manifest, Map.of(fixture.digest(), image), "\"" + "a".repeat(64) + "\"");
			EmojiCacheStore.Loaded loaded = store.load();
			assertNotNull(loaded);
			assertEquals(1, loaded.assets.size());
			assertEquals(1, loaded.rawAssets.size());
			if (Files.getFileStore(root).supportsFileAttributeView("posix"))
			{
				assertEquals(java.util.Set.of(PosixFilePermission.OWNER_READ,
					PosixFilePermission.OWNER_WRITE),
					Files.getPosixFilePermissions(root.resolve("current")));
			}
			Files.writeString(root.resolve("current"), "../../unsafe");
			assertNull(store.load());
		}
		finally { delete(root); }
	}

	@Test public void digestDimensionAlphaAndByteLengthAreRevalidated() throws Exception
	{
		Gson gson = new Gson();
		byte[] image = EmojiTestFixtures.png(0xff113355);
		EmojiTestFixtures.FixtureEntry fixture = new EmojiTestFixtures.FixtureEntry("wave", image);
		EmojiManifest.Entry entry = EmojiManifest.parse(
			EmojiTestFixtures.manifest(gson, List.of(fixture)), gson).entries.get(0);
		assertEquals(20, EmojiCacheStore.validateAsset(image, entry).getWidth());
		byte[] corrupted = image.clone();
		corrupted[corrupted.length / 2] ^= 1;
		try { EmojiCacheStore.validateAsset(corrupted, entry); fail(); }
		catch (java.io.IOException expected) { }
	}

	@Test public void distinctTriggersMayShareOneDigestAddressedAsset() throws Exception
	{
		Path root = Files.createTempDirectory("emoji-cache-shared-digest");
		try
		{
			Gson gson = new Gson();
			byte[] image = EmojiTestFixtures.png(0xff113355);
			EmojiTestFixtures.FixtureEntry first = new EmojiTestFixtures.FixtureEntry("first", image);
			EmojiTestFixtures.FixtureEntry second = new EmojiTestFixtures.FixtureEntry("second", image);
			EmojiManifest manifest = EmojiManifest.parse(
				EmojiTestFixtures.manifest(gson, List.of(first, second)), gson);
			EmojiCacheStore store = new EmojiCacheStore(root, gson);
			store.save(manifest, Map.of(first.digest(), image), "\"" + "a".repeat(64) + "\"");
			EmojiCacheStore.Loaded loaded = store.load();
			assertNotNull(loaded);
			assertEquals(2, loaded.assets.size());
			assertEquals(loaded.assets.get("first").digest, loaded.assets.get("second").digest);
			assertEquals(1, loaded.rawAssets.size());
			try (java.nio.file.DirectoryStream<Path> assets = Files.newDirectoryStream(
				root.resolve("generations").resolve(manifest.revision).resolve("assets")))
			{
				int count = 0;
				for (Path ignored : assets) count++;
				assertEquals(1, count);
			}
		}
		finally { delete(root); }
	}

	@Test public void hardLinksAndStaleTemporaryEntriesFailClosedOrAreRemoved() throws Exception
	{
		Path root = Files.createTempDirectory("emoji-cache-hardlink");
		try
		{
			Gson gson = new Gson();
			byte[] image = EmojiTestFixtures.png(0xff113355);
			EmojiTestFixtures.FixtureEntry fixture = new EmojiTestFixtures.FixtureEntry("wave", image);
			EmojiManifest manifest = EmojiManifest.parse(
				EmojiTestFixtures.manifest(gson, List.of(fixture)), gson);
			EmojiCacheStore store = new EmojiCacheStore(root, gson);
			store.save(manifest, Map.of(fixture.digest(), image), "\"" + "a".repeat(64) + "\"");

			Path pointer = root.resolve("current");
			Path linked = root.resolve("linked-current");
			Files.createLink(linked, pointer);
			assertNull(store.load());
			Files.delete(linked);

			Path staleDirectory = root.resolve(".generation-stale");
			Files.createDirectory(staleDirectory);
			Files.writeString(staleDirectory.resolve("partial"), "partial");
			Path stalePointer = root.resolve(".current-stale");
			Files.writeString(stalePointer, "partial");
			EmojiTestFixtures.FixtureEntry next = new EmojiTestFixtures.FixtureEntry("next", image);
			EmojiManifest nextManifest = EmojiManifest.parse(
				EmojiTestFixtures.manifest(gson, List.of(next)), gson);
			store.save(nextManifest, Map.of(next.digest(), image), "\"" + "b".repeat(64) + "\"");
			assertFalse(Files.exists(staleDirectory));
			assertFalse(Files.exists(stalePointer));
		}
		finally { delete(root); }
	}

	private static void delete(Path path) throws Exception
	{
		if (!Files.exists(path)) return;
		if (Files.isDirectory(path))
			try (java.nio.file.DirectoryStream<Path> stream = Files.newDirectoryStream(path))
			{ for (Path child : stream) delete(child); }
		Files.deleteIfExists(path);
	}
}
