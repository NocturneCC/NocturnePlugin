package com.nocturne;

import com.google.gson.Gson;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.function.IntFunction;

final class DerivedValueCatalogue
{
	static final String RESOURCE = "/derived-value-catalogue.json";
	private final int catalogueVersion;
	private final Map<Integer, Rule> byInput;

	private DerivedValueCatalogue(int catalogueVersion, Map<Integer, Rule> byInput)
	{
		this.catalogueVersion = catalogueVersion;
		this.byInput = Map.copyOf(byInput);
	}

	static DerivedValueCatalogue load(Gson gson)
	{
		InputStream stream = DerivedValueCatalogue.class.getResourceAsStream(RESOURCE);
		if (stream == null) throw new IllegalStateException("Missing derived-value catalogue");
		try (InputStreamReader reader = new InputStreamReader(stream, StandardCharsets.UTF_8))
		{
			Document document = gson.fromJson(reader, Document.class);
			return validate(document);
		}
		catch (java.io.IOException error)
		{
			throw new IllegalStateException("Unable to read derived-value catalogue", error);
		}
	}

	static DerivedValueCatalogue validate(Document document)
	{
		if (document == null || document.catalogue_version < 1 || document.rules == null)
			throw new IllegalArgumentException("Invalid derived-value catalogue");
		Map<Integer, Rule> inputs = new HashMap<>();
		Set<String> ids = new HashSet<>();
		for (Rule rule : document.rules)
		{
			if (rule == null || rule.rule_id == null || !ids.add(rule.rule_id)
				|| rule.catalogue_version != document.catalogue_version
				|| !("full_output_value".equals(rule.valuation_type)
					|| "equal_share_output_value".equals(rule.valuation_type))
				|| rule.output_item_id <= 0 || rule.output_item_name == null || rule.output_item_name.isBlank()
				|| rule.input_item_ids == null || rule.input_item_ids.isEmpty()
				|| rule.required_component_count < 1
				|| ("full_output_value".equals(rule.valuation_type) && rule.required_component_count != 1)
				|| ("equal_share_output_value".equals(rule.valuation_type)
					&& rule.required_component_count != rule.input_item_ids.size()))
				throw new IllegalArgumentException("Invalid derived-value rule");
			for (int input : rule.input_item_ids)
			{
				if (input <= 0 || input == rule.output_item_id || inputs.put(input, rule) != null)
					throw new IllegalArgumentException("Duplicate or invalid derived-value input");
			}
		}
		return new DerivedValueCatalogue(document.catalogue_version, inputs);
	}

	LootItem value(int id, int quantity, String name, int marketPrice, boolean tradeable,
		IntFunction<Integer> outputPrice, IntFunction<String> outputName)
	{
		Rule rule = byInput.get(id);
		if (rule == null)
		{
			return tradeable
				? LootItem.market(id, quantity, name, marketPrice)
				: LootItem.unpricedUntradeable(id, quantity, name);
		}
		Integer finishedPrice = outputPrice.apply(rule.output_item_id);
		String finishedName = outputName.apply(rule.output_item_id);
		if (finishedPrice == null || finishedPrice <= 0 || finishedName == null || finishedName.isBlank())
			return LootItem.unpricedUntradeable(id, quantity, name);
		int derived = "full_output_value".equals(rule.valuation_type)
			? finishedPrice : finishedPrice / rule.required_component_count;
		if (derived <= 0) return LootItem.unpricedUntradeable(id, quantity, name);
		String source = "full_output_value".equals(rule.valuation_type)
			? "runelite_derived_full_output" : "runelite_derived_equal_share";
		return LootItem.derived(id, quantity, name, derived, source, rule.rule_id,
			catalogueVersion, rule.output_item_id, finishedName, finishedPrice);
	}

	static final class Document
	{
		int catalogue_version;
		List<Rule> rules;
	}

	static final class Rule
	{
		String rule_id;
		int catalogue_version;
		String valuation_type;
		List<Integer> input_item_ids;
		int output_item_id;
		String output_item_name;
		int required_component_count;
	}
}
