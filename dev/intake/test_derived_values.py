import json
from pathlib import Path
import tempfile
import unittest

from derived_values import CATALOGUE, load_catalogue, validate_item_valuation


class DerivedValuesTest(unittest.TestCase):
    def test_catalogue_has_every_required_exact_input(self):
        self.assertEqual({
            28285, 28281, 28279, 28283, 29799, 31109,
            29790, 29792, 29794, 13274, 13275, 13276,
            22969, 22971, 22973, 28319, 28321, 28323, 28325,
        }, set(CATALOGUE["by_input"]))

    def test_full_and_equal_share_metadata_validate_with_floor_rounding(self):
        validate_item_valuation(self.derived(28285, 28307, "Ultor ring", 1_234_567,
                                              1_234_567, "ultor_vestige_to_ultor_ring",
                                              "runelite_derived_full_output"))
        validate_item_valuation(self.derived(29790, 29796, "Noxious halberd", 1_500_002,
                                              500_000, "noxious_halberd_components",
                                              "runelite_derived_equal_share"))

    def test_version_input_output_and_arithmetic_mismatches_are_rejected(self):
        original = self.derived(29790, 29796, "Noxious halberd", 1_500_002, 500_000,
                                "noxious_halberd_components", "runelite_derived_equal_share")
        for field, value in [("valuation_catalogue_version", 2), ("finished_output_item_id", 1),
                             ("finished_output_item_name", "Wrong"), ("derived_unit_price_gp", 500_001),
                             ("unit_price_gp", 500_001)]:
            item = dict(original, **{field: value})
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_item_valuation(item)

    def test_missing_zero_and_invalid_finished_prices_are_rejected(self):
        for output_price in (None, 0, -1, True):
            item = self.derived(28285, 28307, "Ultor ring", output_price, 0,
                                "ultor_vestige_to_ultor_ring", "runelite_derived_full_output")
            with self.subTest(output_price=output_price), self.assertRaises(ValueError):
                validate_item_valuation(item)

    def test_unknown_untradeable_is_unpriced_and_direct_market_stays_compatible(self):
        validate_item_valuation({"item_id": 999999, "quantity": 1, "unit_price_gp": 0,
                                 "price_source": "unpriced_untradeable"})
        validate_item_valuation({"item_id": 526, "quantity": 2, "unit_price_gp": 32,
                                 "price_source": "runelite_market"})

    def test_catalogue_integrity_rejects_duplicate_ids_inputs_and_invalid_outputs(self):
        base = {"catalogue_version": 1, "rules": [
            {"rule_id": "one", "catalogue_version": 1, "valuation_type": "full_output_value",
             "input_item_ids": [1], "output_item_id": 2, "output_item_name": "Output",
             "required_component_count": 1},
        ]}
        variants = []
        duplicate_id = json.loads(json.dumps(base)); duplicate_id["rules"].append(dict(duplicate_id["rules"][0], input_item_ids=[3]))
        duplicate_input = json.loads(json.dumps(base)); duplicate_input["rules"].append(dict(duplicate_input["rules"][0], rule_id="two", output_item_id=4))
        invalid_output = json.loads(json.dumps(base)); invalid_output["rules"][0]["output_item_id"] = 0
        variants.extend([duplicate_id, duplicate_input, invalid_output])
        for data in variants:
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "catalogue.json"
                path.write_text(json.dumps(data))
                with self.assertRaises(ValueError):
                    load_catalogue(path)

    @staticmethod
    def derived(input_id, output_id, output_name, output_price, derived, rule_id, source):
        return {"item_id": input_id, "quantity": 1, "unit_price_gp": derived,
                "price_source": source, "valuation_rule_id": rule_id,
                "valuation_catalogue_version": 1, "finished_output_item_id": output_id,
                "finished_output_item_name": output_name,
                "finished_output_market_price_gp": output_price,
                "derived_unit_price_gp": derived}


if __name__ == "__main__":
    unittest.main()
