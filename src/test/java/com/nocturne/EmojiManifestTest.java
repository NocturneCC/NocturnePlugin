package com.nocturne;

import com.google.gson.Gson;
import java.util.List;
import org.junit.Test;
import static org.junit.Assert.*;

public class EmojiManifestTest
{
	private final Gson gson = new Gson();

	@Test public void strictManifestAcceptsOnlySortedBoundedPublicEntries() throws Exception
	{
		byte[] image = EmojiTestFixtures.png(0xff224466);
		byte[] raw = EmojiTestFixtures.manifest(gson,
			List.of(new EmojiTestFixtures.FixtureEntry("wave", image, true)));
		EmojiManifest manifest = EmojiManifest.parse(raw, gson);
		assertEquals(1, manifest.entries.size());
		assertEquals("wave", manifest.entries.get(0).name);
		assertTrue(manifest.entries.get(0).animatedSource);
		String value = new String(raw, java.nio.charset.StandardCharsets.UTF_8).toLowerCase();
		for (String forbidden : List.of("token", "guild", "creator", "roles", "user", "cdn",
			"rsn", "profile", "chat", "telemetry", "receipt")) assertFalse(value.contains(forbidden));
	}

	@Test public void malformedUnsupportedOversizedAndTypeConfusedManifestsFail() throws Exception
	{
		byte[] image = EmojiTestFixtures.png(0xff224466);
		String valid = new String(EmojiTestFixtures.manifest(gson,
			List.of(new EmojiTestFixtures.FixtureEntry("wave", image))),
			java.nio.charset.StandardCharsets.UTF_8);
		for (String bad : List.of(valid.replace("\"schema_version\":1", "\"schema_version\":2"),
			valid.replace("\"width\":20", "\"width\":20.0"),
			valid.replace("\"name\":\"wave\"", "\"name\":\"Wave\""),
			valid.replaceFirst("\\{", "{\"schema_version\":1,"),
			valid.replace("\"asset_path\":\"/api", "\"asset_path\":\"https://evil.invalid/api")))
		{
			try { EmojiManifest.parse(bad.getBytes(java.nio.charset.StandardCharsets.UTF_8), gson); fail(); }
			catch (IllegalArgumentException expected) { }
		}
		try { EmojiManifest.parse(new byte[EmojiManifest.MAX_MANIFEST_BYTES + 1], gson); fail(); }
		catch (IllegalArgumentException expected) { }
	}
}
