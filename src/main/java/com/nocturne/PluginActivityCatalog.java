package com.nocturne;

import java.util.Arrays;
import java.util.Collections;
import java.util.List;
import java.util.Locale;
import java.util.ArrayList;
import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;

/** Client-side source-name projection of the public Plugin Tracking catalog. */
final class PluginActivityCatalog
{
	static final String CONFIG_ENDPOINT = "https://nocturne.events/api/plugin/tracking/config";
	static final Entry PHOSANIS = new Entry("phosanis_nightmare", "Phosani's Nightmare",
		"phosani_nightmare", "overall", true, false, true, true, "phosanis",
		Arrays.asList("Phosani's Nightmare", "Phosani’s Nightmare", "Phosani", "Phosanis"));
	static final Entry GAUNTLET = new Entry("corrupted_gauntlet", "Corrupted Gauntlet",
		"corrupted_gauntlet", "overall", true, false, true, true, "gauntlet",
		Arrays.asList("Corrupted Gauntlet", "The Corrupted Gauntlet", "Corrupted Hunllef",
			"The Corrupted Hunllef", "CG", "Gauntlet"));
	static final Entry NIGHTMARE = new Entry("nightmare", "The Nightmare", "nightmare",
		"overall", true, true, true, true, null,
		Arrays.asList("The Nightmare", "Nightmare"));
	private static final List<Entry> DEFAULTS = Collections.unmodifiableList(
		Arrays.asList(PHOSANIS, GAUNTLET, NIGHTMARE));
	private static volatile List<Entry> entries = DEFAULTS;

	private PluginActivityCatalog() { }

	static Entry matchSource(String source)
	{
		if (source == null) return null;
		String key = normalize(source);
		Entry found = null;
		for (Entry entry : entries)
		{
			for (String alias : entry.sourceNames)
			{
				if (key.equals(normalize(alias)))
				{
					if (found != null && found != entry) return null;
					found = entry;
				}
			}
		}
		return found;
	}

	static Entry find(String activityKey, String modeKey)
	{
		if (activityKey == null || modeKey == null) return null;
		for (Entry entry : entries)
			if (activityKey.equals(entry.activityKey) && modeKey.equals(entry.modeKey)) return entry;
		return null;
	}

	static boolean completionComesFromLoot(Entry activity)
	{
		return activity != null && !"corrupted_gauntlet".equals(activity.activityKey);
	}

	static synchronized boolean applyPublicConfig(JsonObject document)
	{
		try
		{
			if (document == null || document.get("schema_version").getAsInt() != 1) return false;
			JsonArray activities = document.getAsJsonArray("activities");
			if (activities == null || activities.size() > 300) return false;
			List<Entry> parsed = new ArrayList<>();
			for (JsonElement activityElement : activities)
			{
				JsonObject activity = activityElement.getAsJsonObject();
				String activityKey = activity.get("activity_key").getAsString();
				String displayName = activity.get("display_name").getAsString();
				List<String> activityNames = strings(activity.getAsJsonArray("aliases"));
				JsonArray modes = activity.getAsJsonArray("modes");
				if (modes == null || modes.size() > 20) return false;
				for (JsonElement modeElement : modes)
				{
					JsonObject mode = modeElement.getAsJsonObject();
					List<String> names = new ArrayList<>(activityNames);
					names.add(displayName);
					names.addAll(strings(mode.getAsJsonArray("source_names")));
					// LootTracker/NPC completion sources can name the final corrupted boss
					// rather than the reward container. Keep this source alias local to the
					// canonical activity mapping; policy still comes from the public catalog.
					if ("corrupted_gauntlet".equals(activityKey))
					{
						names.add("Corrupted Hunllef");
						names.add("The Corrupted Hunllef");
					}
					String scope = mode.get("timing_scope").getAsString();
					boolean enabled = mode.get("ttk_tracking_enabled").getAsBoolean();
					if (enabled && !"overall".equals(scope) && !"segment".equals(scope)) return false;
					parsed.add(new Entry(activityKey, displayName, mode.get("mode_key").getAsString(),
						scope, mode.get("supports_solo").getAsBoolean(),
						mode.get("supports_group").getAsBoolean(),
						mode.get("loot_tracking_enabled").getAsBoolean(), enabled,
						mode.has("challenge_boss_key") && !mode.get("challenge_boss_key").isJsonNull()
							? mode.get("challenge_boss_key").getAsString() : null, names));
				}
			}
			if (parsed.isEmpty()) return false;
			entries = Collections.unmodifiableList(parsed);
			return true;
		}
		catch (RuntimeException ignored) { return false; }
	}

	static synchronized void resetToBundledDefaults() { entries = DEFAULTS; }
	static synchronized void resetForTests() { resetToBundledDefaults(); }

	private static List<String> strings(JsonArray array)
	{
		List<String> result = new ArrayList<>();
		if (array == null) return result;
		for (JsonElement element : array)
		{
			if (!element.isJsonPrimitive() || !element.getAsJsonPrimitive().isString())
				throw new IllegalArgumentException("catalog_name_invalid");
			result.add(element.getAsString());
		}
		return result;
	}

	static String normalize(String value)
	{
		return value.replace('\u2018', '\'').replace('\u2019', '\'')
			.toLowerCase(Locale.ROOT).replaceAll("[^a-z0-9]", "");
	}

	static final class Entry
	{
		final String activityKey;
		final String displayName;
		final String modeKey;
		final String timingScope;
		final boolean supportsSolo;
		final boolean supportsGroup;
		final boolean lootTrackingEnabled;
		final boolean ttkTrackingEnabled;
		final String challengeBossKey;
		final List<String> sourceNames;

		Entry(String activityKey, String displayName, String modeKey, String timingScope,
			boolean supportsSolo, boolean supportsGroup, boolean lootTrackingEnabled,
			boolean ttkTrackingEnabled, String challengeBossKey, List<String> sourceNames)
		{
			this.activityKey = activityKey;
			this.displayName = displayName;
			this.modeKey = modeKey;
			this.timingScope = timingScope;
			this.supportsSolo = supportsSolo;
			this.supportsGroup = supportsGroup;
			this.lootTrackingEnabled = lootTrackingEnabled;
			this.ttkTrackingEnabled = ttkTrackingEnabled;
			this.challengeBossKey = challengeBossKey;
			this.sourceNames = Collections.unmodifiableList(sourceNames);
		}
	}
}
