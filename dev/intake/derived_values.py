"""Strict loader and validator for the shared derived-value catalogue."""
import json
from pathlib import Path


CATALOGUE_PATH = Path(__file__).resolve().parents[2] / "shared" / "derived-value-catalogue.json"
DERIVED_SOURCES = {
    "full_output_value": "runelite_derived_full_output",
    "equal_share_output_value": "runelite_derived_equal_share",
}


def load_catalogue(path=CATALOGUE_PATH):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if set(data) != {"catalogue_version", "rules"} or type(data["catalogue_version"]) is not int or data["catalogue_version"] < 1:
        raise ValueError("invalid valuation catalogue")
    by_input, input_names, rule_ids = {}, {}, set()
    for rule in data["rules"]:
        expected = {"rule_id", "catalogue_version", "valuation_type", "input_item_ids", "input_item_names",
                    "output_item_id", "output_item_name", "required_component_count"}
        if not isinstance(rule, dict) or set(rule) != expected:
            raise ValueError("invalid valuation rule fields")
        inputs = rule["input_item_ids"]
        names = rule["input_item_names"]
        valuation = rule["valuation_type"]
        if (not isinstance(rule["rule_id"], str) or not rule["rule_id"] or rule["rule_id"] in rule_ids
                or rule["catalogue_version"] != data["catalogue_version"]
                or valuation not in DERIVED_SOURCES
                or not isinstance(inputs, list) or not inputs
                or not isinstance(names, list) or len(names) != len(inputs)
                or any(not isinstance(name, str) or not name.strip() for name in names)
                or type(rule["output_item_id"]) is not int or rule["output_item_id"] <= 0
                or not isinstance(rule["output_item_name"], str) or not rule["output_item_name"].strip()
                or type(rule["required_component_count"]) is not int
                or rule["required_component_count"] < 1
                or (valuation == "full_output_value" and rule["required_component_count"] != 1)
                or (valuation == "equal_share_output_value" and rule["required_component_count"] != len(inputs))):
            raise ValueError("invalid valuation rule")
        rule_ids.add(rule["rule_id"])
        for item_id, item_name in zip(inputs, names):
            if (type(item_id) is not int or item_id <= 0 or item_id == rule["output_item_id"]
                    or item_id in by_input):
                raise ValueError("duplicate or invalid valuation input")
            by_input[item_id] = rule
            input_names[item_id] = item_name
    return {"catalogue_version": data["catalogue_version"], "rules": data["rules"],
            "by_input": by_input, "input_names": input_names}


CATALOGUE = load_catalogue()


def validate_item_valuation(item):
    required = {"item_id", "quantity", "unit_price_gp", "price_source"}
    if not isinstance(item, dict) or not required.issubset(item) or not isinstance(item["price_source"], str):
        raise ValueError("invalid valuation metadata")
    source = item["price_source"]
    rule = CATALOGUE["by_input"].get(item["item_id"])
    direct_fields = {"item_id", "quantity", "unit_price_gp", "price_source"}
    derived_fields = direct_fields | {
        "valuation_rule_id", "valuation_catalogue_version", "finished_output_item_id",
        "finished_output_item_name", "finished_output_market_price_gp", "derived_unit_price_gp",
    }
    if source in {"runelite_market", "price_unavailable", "unpriced_untradeable"}:
        if set(item) != direct_fields or (source != "runelite_market" and item["unit_price_gp"] != 0):
            raise ValueError("invalid direct price metadata")
        if rule is not None and source != "unpriced_untradeable":
            raise ValueError("catalogued untradeable missing derived metadata")
        return
    if set(item) != derived_fields or rule is None:
        raise ValueError("invalid derived price metadata")
    expected_source = DERIVED_SOURCES[rule["valuation_type"]]
    output_price = item["finished_output_market_price_gp"]
    divisor = rule["required_component_count"] if rule["valuation_type"] == "equal_share_output_value" else 1
    if type(output_price) is not int or not 1 <= output_price <= 2147483647:
        raise ValueError("invalid finished output price")
    expected_price = output_price // divisor
    if (source != expected_source or item["valuation_rule_id"] != rule["rule_id"]
            or item["valuation_catalogue_version"] != CATALOGUE["catalogue_version"]
            or item["finished_output_item_id"] != rule["output_item_id"]
            or item["finished_output_item_name"] != rule["output_item_name"]
            or item["derived_unit_price_gp"] != expected_price
            or item["unit_price_gp"] != expected_price or expected_price <= 0):
        raise ValueError("derived price does not match catalogue")


def validated_derived_input(item):
    """Return the canonical dropped-item name only after exact derived validation."""
    validate_item_valuation(item)
    if item["price_source"] not in DERIVED_SOURCES.values():
        return None
    return CATALOGUE["input_names"].get(item["item_id"])
