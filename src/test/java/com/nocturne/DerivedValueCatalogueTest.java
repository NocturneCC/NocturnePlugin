package com.nocturne;

import com.google.gson.Gson;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import org.junit.Test;
import static org.junit.Assert.*;

public class DerivedValueCatalogueTest
{
	private final DerivedValueCatalogue catalogue = DerivedValueCatalogue.load(new Gson());

	@Test
	public void requiredInputsUseExactIdsAndOutputs()
	{
		Map<Integer, Integer> expected = new LinkedHashMap<>();
		expected.put(28285, 28307); expected.put(28281, 28313); expected.put(28279, 28316);
		expected.put(28283, 28310); expected.put(29799, 29801); expected.put(31109, 31106);
		expected.put(29790, 29796); expected.put(29792, 29796); expected.put(29794, 29796);
		expected.put(13274, 13263); expected.put(13275, 13263); expected.put(13276, 13263);
		expected.put(22969, 22975); expected.put(22971, 22975); expected.put(22973, 22975);
		expected.put(28319, 28338); expected.put(28321, 28338);
		expected.put(28323, 28338); expected.put(28325, 28338);
		for (Map.Entry<Integer, Integer> entry : expected.entrySet())
		{
			LootItem item = value(entry.getKey(), 1_200_001);
			assertEquals(entry.getValue().intValue(), item.finishedOutputItemId.intValue());
			assertEquals(Integer.valueOf(1), item.valuationCatalogueVersion);
		}
	}

	@Test
	public void fullOutputDoesNotSubtractCraftingInputs()
	{
		LootItem vestige = value(28285, 1_234_567);
		assertEquals(1_234_567, vestige.unitPriceGp);
		assertEquals("runelite_derived_full_output", vestige.priceSource);
	}

	@Test
	public void equalSharesUseFloorDivision()
	{
		assertEquals(400_000, value(29790, 1_200_001).unitPriceGp);
		assertEquals(300_000, value(28319, 1_200_003).unitPriceGp);
		assertEquals("runelite_derived_equal_share", value(13274, 1_200_001).priceSource);
	}

	@Test
	public void missingZeroAndInvalidOutputPricesFailClosed()
	{
		for (Integer price : new Integer[]{null, 0, -1})
		{
			LootItem item = catalogue.value(28285, 1, "Ultor vestige", 99, false,
				ignored -> price, ignored -> "Ultor ring");
			assertEquals(0, item.unitPriceGp);
			assertEquals("unpriced_untradeable", item.priceSource);
			assertNull(item.valuationRuleId);
		}
	}

	@Test
	public void unknownUntradeablesFailClosedAndTradeablesRemainUnchanged()
	{
		LootItem unknown = catalogue.value(999_999, 1, "Unknown", 900_000, false,
			ignored -> 1, ignored -> "Output");
		LootItem tradeable = catalogue.value(526, 2, "Bones", 32, true,
			ignored -> 1, ignored -> "Output");
		assertEquals("unpriced_untradeable", unknown.priceSource);
		assertEquals(0, unknown.unitPriceGp);
		assertEquals("runelite_market", tradeable.priceSource);
		assertEquals(32, tradeable.unitPriceGp);
	}

	@Test
	public void eligibilityUsesDerivedUnitBoundaryAndPanelLabelsOutput()
	{
		LootItem below = value(29790, 1_499_999);
		LootItem at = value(29790, 1_500_000);
		assertEquals(499_999, below.unitPriceGp);
		assertEquals(500_000, at.unitPriceGp);
		assertFalse(ScreenshotCapture.isLikelyEligible(List.of(below)));
		assertTrue(ScreenshotCapture.isLikelyEligible(List.of(at)));
		assertTrue(NocturnePanel.priceText(at).contains("Derived from Noxious halberd"));
		assertTrue(NocturnePanel.priceText(at).contains("1,500,000 gp"));
	}

	@Test
	public void integrityRejectsDuplicateRulesInputsAndInvalidOutputs()
	{
		for (DerivedValueCatalogue.Document document : List.of(
			document(rule("same", 1, 2), rule("same", 3, 4)),
			document(rule("one", 1, 3), rule("two", 1, 4)),
			document(rule("bad", 1, 0))))
		{
			try
			{
				DerivedValueCatalogue.validate(document);
				fail("Expected invalid catalogue to be rejected");
			}
			catch (IllegalArgumentException expected)
			{
				// Expected.
			}
		}
	}

	private LootItem value(int input, Integer outputPrice)
	{
		return catalogue.value(input, 1, "Input", 0, false, ignored -> outputPrice,
			output -> output == 29796 ? "Noxious halberd" : "Output");
	}

	private static DerivedValueCatalogue.Document document(DerivedValueCatalogue.Rule... rules)
	{
		DerivedValueCatalogue.Document document = new DerivedValueCatalogue.Document();
		document.catalogue_version = 1;
		document.rules = List.of(rules);
		return document;
	}

	private static DerivedValueCatalogue.Rule rule(String id, int input, int output)
	{
		DerivedValueCatalogue.Rule rule = new DerivedValueCatalogue.Rule();
		rule.rule_id = id; rule.catalogue_version = 1; rule.valuation_type = "full_output_value";
		rule.input_item_ids = List.of(input); rule.output_item_id = output;
		rule.output_item_name = "Output"; rule.required_component_count = 1;
		return rule;
	}
}
