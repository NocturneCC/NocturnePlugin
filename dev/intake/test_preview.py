import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from uuid import uuid4
from preview import build_report, readonly

NOW = datetime(2026, 9, 1, 21, 0, tzinfo=timezone.utc).timestamp()
STAMP = datetime.fromtimestamp(NOW, timezone.utc).isoformat()


class PreviewTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.intake = self.root / "test-drops.sqlite3"
        self.create(self.intake, "CREATE TABLE test_drops(event_id TEXT PRIMARY KEY, received_at REAL, payload TEXT)")
        self.create(self.root / "Members.db", """
            CREATE TABLE members(member_id INTEGER, rsn TEXT, status TEXT);
            CREATE TABLE member_accounts(member_id INTEGER, rsn TEXT, is_active INTEGER);
            CREATE TABLE member_aliases(member_id INTEGER, alias_rsn TEXT);
            INSERT INTO members VALUES(1,'Main Account','active');
            INSERT INTO member_accounts VALUES(1,'Simons Alt',1);
        """)
        self.create(self.root / "Items.db", """
            CREATE TABLE items(item_id INTEGER, osrs_item_id INTEGER, item_name TEXT,
                normalized_item_name TEXT, latest_price INTEGER, latest_price_checked_at TEXT, is_active INTEGER);
            CREATE TABLE pet_kit_point_values(name TEXT, normalized_name TEXT, points INTEGER, category TEXT, is_active INTEGER);
        """)
        self.create(self.root / "RegularSubmissions.db", "CREATE TABLE regular_submissions(submission_id INTEGER, external_id TEXT, status TEXT)")
        self.edit("Items.db", "INSERT INTO items VALUES(1,526,'Bones','bones',100,?,1)", (STAMP,))
        self.event_id = self.add_event()

    def create(self, path, sql):
        db = sqlite3.connect(path)
        try:
            db.executescript(sql)
        finally:
            db.close()

    def edit(self, filename, sql, params=()):
        db = sqlite3.connect(self.root / filename)
        try:
            db.execute(sql, params)
            db.commit()
        finally:
            db.close()

    def add_event(self, source="Goblin", quantity=1, rsn="Simons Alt", unit_price_gp=None):
        event_id = str(uuid4())
        item = dict(item_id=526, quantity=quantity)
        version = 1
        if unit_price_gp is not None:
            version = 2
            item["unit_price_gp"] = unit_price_gp
        payload = dict(version=version, event_id=event_id, occurred_at=STAMP, rsn=rsn,
                       source=source, items=[item])
        self.edit("test-drops.sqlite3", "INSERT INTO test_drops VALUES(?,?,?)", (event_id, NOW, json.dumps(payload)))
        return event_id

    def report(self):
        return build_report(self.intake, self.root, now=NOW)

    def item(self):
        return next(event for event in self.report()["events"] if event["event_id"] == self.event_id)["items"][0]

    def test_existing_databases_remain_byte_for_byte_unchanged(self):
        files = list(self.root.iterdir())
        before = {file: hashlib.sha256(file.read_bytes()).digest() for file in files}
        report = self.report()
        self.assertEqual("matched", report["events"][0]["member"]["status"])
        self.assertEqual("linked_account", report["events"][0]["member"]["method"])
        self.assertEqual("excluded_below_unit_threshold", self.item()["status"])
        self.assertEqual(0, self.item()["final_points"])
        self.assertEqual(before, {file: hashlib.sha256(file.read_bytes()).digest() for file in files})
        with readonly(self.intake) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM test_drops")

    def test_large_stack_cannot_cross_unit_threshold(self):
        self.edit("Items.db", "UPDATE items SET latest_price=100000")
        self.event_id = self.add_event(quantity=100)
        self.assertEqual(0, self.item()["base_points"])

    def test_missing_stale_or_unknown_timestamp_is_not_zero_points(self):
        for price, stamp, expected in [(None, STAMP, "price_unavailable"),
                                       (100, "2026-06-04T22:39:13+00:00", "price_stale"),
                                       (100, None, "price_timestamp_unavailable")]:
            self.edit("Items.db", "UPDATE items SET latest_price=?, latest_price_checked_at=?", (price, stamp))
            item = self.item()
            self.assertEqual(expected, item["status"])
            self.assertIsNone(item["final_points"])
            self.assertNotIn("base_points", item)

    def test_sqlite_current_timestamp_is_utc_and_does_not_bypass_freshness(self):
        for stamp, expected in [("2026-09-01 21:00:00", "current"),
                                ("2026-06-04 22:39:13", "price_stale"),
                                ("2026-09-01 21:05:00", "price_stale"),
                                ("2026-09-01T21:00:00", "price_timestamp_unavailable")]:
            with self.subTest(stamp=stamp):
                self.edit("Items.db", "UPDATE items SET latest_price_checked_at=?", (stamp,))
                price = self.item()["price"]
                self.assertEqual(expected, price["status"])
                if expected == "current":
                    self.assertEqual(0, price["age_hours"])

    def test_eligible_units_get_base_points_but_no_assumed_group_or_event(self):
        self.edit("Items.db", "UPDATE items SET latest_price=600000")
        self.event_id = self.add_event(quantity=3)
        item = self.item()
        self.assertEqual(3, item["base_points"])
        self.assertEqual("needs_context", item["status"])
        self.assertIsNone(item["final_points"])
        self.assertFalse(item["would_insert"])

    def test_v2_uses_captured_runelite_unit_price(self):
        self.edit("Items.db", "UPDATE items SET latest_price=100, latest_price_checked_at=NULL")
        self.event_id = self.add_event(quantity=3, unit_price_gp=600000)
        item = self.item()
        self.assertEqual("runelite_client", item["price"]["source"])
        self.assertEqual("reported_at_capture", item["price"]["status"])
        self.assertEqual(600000, item["price"]["unit_price_gp"])
        self.assertEqual(3, item["base_points"])
        self.assertEqual("needs_context", item["status"])

    def test_v2_zero_price_remains_unavailable(self):
        self.event_id = self.add_event(unit_price_gp=0)
        item = self.item()
        self.assertEqual("price_unavailable", item["status"])
        self.assertIsNone(item["final_points"])

    def test_ambiguous_member_is_not_arbitrarily_selected(self):
        self.edit("Members.db", "INSERT INTO members VALUES(2,'Simons Alt','active')")
        self.edit("Items.db", "UPDATE items SET latest_price=600000")
        self.assertEqual("ambiguous", self.report()["events"][0]["member"]["status"])
        self.assertEqual("identity_review", self.item()["status"])

    def test_inactive_and_historical_accounts_do_not_authorize_awards(self):
        self.edit("Members.db", "UPDATE member_accounts SET is_active=0")
        self.assertEqual("inactive", self.report()["events"][0]["member"]["status"])
        self.edit("Members.db", "DELETE FROM member_accounts")
        self.edit("Members.db", "INSERT INTO member_aliases VALUES(1,'Simons Alt')")
        self.assertEqual("unmatched", self.report()["events"][0]["member"]["status"])

    def test_existing_external_id_blocks_reimport_regardless_of_status(self):
        self.edit("RegularSubmissions.db", "INSERT INTO regular_submissions VALUES(10,?,'pending')", (f"runelite:{self.event_id}:526",))
        self.assertEqual("already_imported", self.item()["status"])

    def test_fixed_catalogue_rewards_do_not_use_gp_price(self):
        self.edit("Items.db", "INSERT INTO pet_kit_point_values VALUES('Bones','bones',75,'pets/kits',1)")
        item = self.item()
        self.assertEqual("fixed_reward_review", item["status"])
        self.assertTrue(item["personal"])
        self.assertNotIn("price", item)

    def test_synthetic_tests_are_excluded(self):
        event_id = self.add_event(source="Synthetic HTTPS test")
        event = next(event for event in self.report()["events"] if event["event_id"] == event_id)
        self.assertEqual("synthetic_test_excluded", event["status"])
        self.assertNotIn("items", event)

    def test_missing_database_is_not_created(self):
        missing = self.root / "missing.sqlite3"
        with self.assertRaises(sqlite3.OperationalError):
            build_report(missing, self.root, now=NOW)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
