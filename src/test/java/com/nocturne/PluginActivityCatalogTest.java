package com.nocturne;

import static org.junit.Assert.*;

import com.google.gson.JsonParser;
import java.time.Instant;
import java.util.List;
import net.runelite.http.api.loottracker.LootRecordType;
import org.junit.After;
import org.junit.Test;

public class PluginActivityCatalogTest
{
	@After public void resetCatalog() { PluginActivityCatalog.resetForTests(); }

	@Test public void pnmNpcLootKeepsCanonicalActivityAndSoloChallengeOverlay()
	{
		PluginActivityCatalog.Entry activity = PluginActivityCatalog.matchSource("Phosani’s Nightmare");
		assertNotNull(activity);
		assertEquals("phosanis_nightmare", activity.activityKey);
		assertEquals("overall", activity.timingScope);
		assertEquals("phosanis", activity.challengeBossKey);
		assertEquals("NPC", NocturnePlugin.classifyLoot(LootRecordType.NPC, "Phosani's Nightmare").name());
		BossCompletionContext context = BossCompletionContext.fromNpcLoot("Phosani's Nightmare",
			"Local Reporter", "session", 9, 100L, Instant.parse("2026-10-12T00:00:00Z"));
		assertNotNull(context);
		assertEquals(List.of("Local Reporter"), context.roster.names);
	}

	@Test public void corruptedGauntletRewardContainerIsRecognizedAndTimedFromCompletionContext()
	{
		PluginActivityCatalog.Entry activity = PluginActivityCatalog.matchSource("The Corrupted Hunllef");
		assertNotNull(activity);
		assertEquals("corrupted_gauntlet", activity.activityKey);
		assertEquals("gauntlet", activity.challengeBossKey);
		assertEquals("NPC", NocturnePlugin.classifyLoot(LootRecordType.NPC, "Corrupted Hunllef").name());
		BossCompletionContext context = BossCompletionContext.fromNpcLoot("The Corrupted Hunllef",
			"Tester", "session", 10, 200L, Instant.parse("2026-10-12T00:00:01Z"));
		assertNotNull(context);
		assertEquals("overall", context.timingScope);
		GenericCompletionCorrelator correlator = new GenericCompletionCorrelator();
		long completionNanos = 2_000_000_000L;
		context = BossCompletionContext.fromNpcLoot("The Corrupted Hunllef", "Tester",
			"session", 10, completionNanos, Instant.parse("2026-10-12T00:00:01Z"));
		assertNull(correlator.onCompletion(context, completionNanos, Instant.parse("2026-10-12T00:00:01Z")));
		assertNull(correlator.onMessage("GAMEMESSAGE", "Fight duration: 7:12.34. Personal best: 6:10.00",
			11, 2_100_000_000L, Instant.parse("2026-10-12T00:00:02Z")));
		ChallengeObservation observation = correlator.poll(3_700_000_000L,
			Instant.parse("2026-10-12T00:00:04Z"));
		assertNotNull("reason=" + correlator.lastDiagnosticReason()
			+ " buffered=" + correlator.bufferedMessages() + " contexts=" + correlator.pendingContexts(), observation);
		assertEquals("corrupted_gauntlet", observation.activityKey);
		assertEquals(432_340L, observation.overallTimeMillis.longValue());
	}

	@Test public void publishedGauntletCatalogRetainsHunllefLootAlias()
	{
		String document = "{\"schema_version\":1,\"activities\":[{\"activity_key\":\"corrupted_gauntlet\","
			+ "\"display_name\":\"Corrupted Gauntlet\",\"aliases\":[\"CG\",\"Gauntlet\"],\"modes\":[{"
			+ "\"mode_key\":\"corrupted_gauntlet\",\"display_name\":\"Corrupted\","
			+ "\"source_names\":[\"Corrupted Gauntlet\",\"The Corrupted Gauntlet\"],"
			+ "\"supports_solo\":true,\"supports_group\":false,\"loot_tracking_enabled\":true,"
			+ "\"ttk_tracking_enabled\":true,\"timing_scope\":\"overall\","
			+ "\"challenge_boss_key\":\"gauntlet\",\"challenge_mode_key\":null,"
			+ "\"challenge_content_key\":null}]}]}";
		assertTrue(PluginActivityCatalog.applyPublicConfig(new JsonParser().parse(document).getAsJsonObject()));
		assertEquals("corrupted_gauntlet", PluginActivityCatalog.matchSource("The Corrupted Hunllef").activityKey);
	}

	@Test public void regularNightmareIsLocallyTrackedWithoutChallengeProjection()
	{
		PluginActivityCatalog.Entry activity = PluginActivityCatalog.matchSource("The Nightmare");
		assertNotNull(activity);
		assertEquals("nightmare", activity.activityKey);
		assertTrue(activity.supportsGroup);
		assertNull(activity.challengeBossKey);
		LootRecord record = new LootRecord("Local Reporter", "The Nightmare",
			List.of(new LootItem(100, 1, "Fixture item")),
			GroupSnapshot.unavailable("fixture"), activity);
		assertEquals("nightmare", record.activityKey);
		assertEquals("The Nightmare", record.activityDisplayName);
		assertNull(BossCompletionContext.fromNpcLoot("The Nightmare", "Local Reporter",
			"session", 1, 1L, Instant.now()));
	}

	@Test public void doomCompletionIsNumericAndSeparateFromTtkAndChallengeScoring()
	{
		PluginActivityCatalog.Entry activity = PluginActivityCatalog.matchSource("Doom of Mokhaiotl");
		assertNotNull(activity);
		assertEquals("doom_of_mokhaiotl", activity.activityKey);
		assertEquals("doom_of_mokhaiotl", activity.modeKey);
		assertFalse(activity.ttkTrackingEnabled);
		assertEquals("unconfigured", activity.timingScope);
		assertTrue(activity.completionTrackingEnabled);
		assertEquals("deepest_level", activity.completionMetricKey);
		assertNull("no Challenge projection is configured by default", activity.challengeBossKey);

		BossCompletionContext context = BossCompletionContext.fromNumericRun(activity, "Local Name",
			"session|doom-run|1", 70, 1L, Instant.parse("2026-10-13T12:00:00Z"));
		ChallengeObservation observation = ChallengeObservation.createTrackedNumeric(context, 7);
		assertNotNull(observation);
		assertTrue(observation.pluginTrackingCandidate);
		assertFalse(observation.challengeCandidate);
		com.google.gson.JsonObject payload = observation.payload(new com.google.gson.Gson(), observation.eventId);
		assertEquals("deepest_level", payload.get("completion_metric_key").getAsString());
		assertEquals(7, payload.get("completion_value").getAsInt());
		assertFalse(payload.has("overall_time_ms"));
		assertFalse(payload.has("room_time_ms"));
		assertEquals(1, payload.get("group_size").getAsInt());
		assertEquals(1, payload.getAsJsonArray("roster").size());
		assertEquals(observation.eventId,
			ChallengeObservation.createTrackedNumeric(context, 9).eventId);
	}

	@Test public void unknownAndDisabledActivitiesDoNotCreateTimingContextButRemainLocal()
	{
		assertNull(PluginActivityCatalog.matchSource("Unknown Encounter"));
		assertNull(BossCompletionContext.fromNpcLoot("Unknown Encounter", "Local Reporter",
			"session", 1, 1L, Instant.now()));
		assertNull(new LootRecord("Local Reporter", "Unknown Encounter",
			List.of(new LootItem(100, 1, "Fixture item")),
			GroupSnapshot.unavailable("fixture")).activityKey);
		String disabled = "{\"schema_version\":1,\"activities\":[{\"activity_key\":\"x\","
			+ "\"display_name\":\"X\",\"aliases\":[],\"modes\":[{\"mode_key\":\"x\","
			+ "\"display_name\":\"X\",\"source_names\":[\"Phosani's Nightmare\"],"
			+ "\"supports_solo\":true,\"supports_group\":false,\"loot_tracking_enabled\":true,"
			+ "\"ttk_tracking_enabled\":false,\"timing_scope\":\"unconfigured\","
			+ "\"challenge_boss_key\":null,\"challenge_mode_key\":null,\"challenge_content_key\":null}]}]}";
		assertTrue(PluginActivityCatalog.applyPublicConfig(new JsonParser().parse(disabled).getAsJsonObject()));
		assertNotNull(PluginActivityCatalog.matchSource("Phosani's Nightmare"));
		assertNull(BossCompletionContext.fromNpcLoot("Phosani's Nightmare", "Local Reporter",
			"session", 1, 1L, Instant.now()));
	}

	@Test public void publicCatalogConfigReplacesAliasesAndAmbiguityFailsClosed()
	{
		String ambiguous = "{\"schema_version\":1,\"activities\":["
			+ "{\"activity_key\":\"a\",\"display_name\":\"A\",\"aliases\":[],\"modes\":[{"
			+ "\"mode_key\":\"a\",\"display_name\":\"A\",\"source_names\":[\"Same\"],"
			+ "\"supports_solo\":true,\"supports_group\":false,\"loot_tracking_enabled\":true,"
			+ "\"ttk_tracking_enabled\":true,\"timing_scope\":\"overall\",\"challenge_boss_key\":null,"
			+ "\"challenge_mode_key\":null,\"challenge_content_key\":null}]},"
			+ "{\"activity_key\":\"b\",\"display_name\":\"B\",\"aliases\":[],\"modes\":[{"
			+ "\"mode_key\":\"b\",\"display_name\":\"B\",\"source_names\":[\"Same\"],"
			+ "\"supports_solo\":true,\"supports_group\":false,\"loot_tracking_enabled\":true,"
			+ "\"ttk_tracking_enabled\":true,\"timing_scope\":\"overall\",\"challenge_boss_key\":null,"
			+ "\"challenge_mode_key\":null,\"challenge_content_key\":null}]}]}";
		assertTrue(PluginActivityCatalog.applyPublicConfig(new JsonParser().parse(ambiguous).getAsJsonObject()));
		assertNull(PluginActivityCatalog.matchSource("Same"));
	}

	@Test public void duplicateRewardContainerDeliveryIsBoundedToAdjacentGameCycle()
	{
		LootEventDeduplicator deduplicator = new LootEventDeduplicator();
		List<LootItem> drop = List.of(new LootItem(100, 1, "Fixture item"));
		assertFalse(deduplicator.isDuplicate("Corrupted Gauntlet", drop, 10));
		assertTrue(deduplicator.isDuplicate("Corrupted Gauntlet", drop, 11));
		assertFalse(deduplicator.isDuplicate("Corrupted Gauntlet", drop, 13));
	}
}
