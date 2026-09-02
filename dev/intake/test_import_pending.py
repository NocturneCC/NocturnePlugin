import hashlib
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from uuid import uuid4

from import_pending import run


NOW = datetime(2026, 9, 1, 22, 0, tzinfo=timezone.utc)


class PendingImportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.intake = self.root / "intake.sqlite3"
        self.create(self.intake, "CREATE TABLE test_drops(event_id TEXT PRIMARY KEY, received_at REAL, payload TEXT)")
        self.create(self.root / "Members.db", """
            CREATE TABLE members(member_id INTEGER, rsn TEXT, discord_id TEXT, status TEXT);
            CREATE TABLE member_accounts(member_id INTEGER, rsn TEXT, is_active INTEGER);
            INSERT INTO members VALUES(2,'Test Main','123456789012345678','active');
            INSERT INTO member_accounts VALUES(2,'Test Alt',1);
        """)
        self.create(self.root / "Items.db", """
            CREATE TABLE items(item_id INTEGER, osrs_item_id INTEGER, item_name TEXT,
                normalized_item_name TEXT, latest_price INTEGER, latest_price_checked_at TEXT, is_active INTEGER);
            CREATE TABLE pet_kit_point_values(name TEXT, normalized_name TEXT, points INTEGER, category TEXT, is_active INTEGER);
            INSERT INTO items VALUES(10,20997,'Twisted bow','twisted_bow',1,NULL,1);
            INSERT INTO items VALUES(11,526,'Bones','bones',1,NULL,1);
            INSERT INTO items VALUES(12,999,'Test pet','test_pet',1,NULL,1);
            INSERT INTO pet_kit_point_values VALUES('Test pet','test_pet',250,'pets/kits',1);
        """)
        self.create(self.root / "RegularSubmissions.db", self.submission_schema())

    @staticmethod
    def submission_schema():
        return """
            CREATE TABLE regular_submissions (
                submission_id INTEGER PRIMARY KEY AUTOINCREMENT, member_id INTEGER,
                rsn TEXT NOT NULL, normalized_rsn TEXT NOT NULL, discord_id TEXT,
                item_id INTEGER, osrs_item_id INTEGER, item_name TEXT NOT NULL,
                normalized_item_name TEXT NOT NULL, item_price INTEGER DEFAULT 0,
                base_points INTEGER NOT NULL DEFAULT 0, multiplier REAL NOT NULL DEFAULT 1.0,
                final_points INTEGER NOT NULL DEFAULT 0, category TEXT NOT NULL DEFAULT 'drop',
                source_type TEXT NOT NULL DEFAULT 'regular', source_event_type TEXT,
                source_event_db TEXT, source_event_id INTEGER, source_event_submission_id INTEGER,
                screenshot_url TEXT, notes TEXT, status TEXT NOT NULL DEFAULT 'pending',
                submitted_at TEXT DEFAULT CURRENT_TIMESTAMP, reviewed_at TEXT, reviewed_by TEXT,
                review_notes TEXT, identity_match_method TEXT, identity_match_notes TEXT,
                identity_review_status TEXT, identity_reviewed_by TEXT, identity_reviewed_at TEXT,
                identity_review_notes TEXT, external_id TEXT
                ,price_source TEXT, valuation_rule_id TEXT, valuation_catalogue_version INTEGER,
                finished_output_item_id INTEGER, finished_output_item_name TEXT,
                finished_output_market_price_gp INTEGER, derived_unit_price_gp INTEGER
            );
            CREATE TABLE rank_totals(member_id INTEGER PRIMARY KEY, total_points INTEGER NOT NULL DEFAULT 0);
            INSERT INTO rank_totals VALUES(2,77);
        """

    @staticmethod
    def create(path, sql):
        with closing(sqlite3.connect(path)) as db:
            db.executescript(sql)
            db.commit()

    def event(self, item_id, price, quantity=1):
        event_id = str(uuid4())
        payload = {
            "version": 2, "event_id": event_id, "occurred_at": NOW.isoformat(),
            "rsn": "Test Alt", "source": "Test source",
            "items": [{"item_id": item_id, "quantity": quantity, "unit_price_gp": price}],
        }
        with closing(sqlite3.connect(self.intake)) as db:
            db.execute("INSERT INTO test_drops VALUES(?,?,?)",
                       (event_id, NOW.timestamp(), json.dumps(payload)))
            db.commit()
        return event_id

    def execute(self, apply=False):
        return run(self.intake, self.root, 50, apply=apply)

    def rows(self, table):
        with closing(sqlite3.connect(self.root / "RegularSubmissions.db")) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]

    def test_preview_is_read_only_and_applies_item_cap(self):
        self.event(20997, 1_500_000_000)
        before = hashlib.sha256((self.root / "RegularSubmissions.db").read_bytes()).digest()
        result = self.execute()
        self.assertEqual("preview_pending", result["mode"])
        self.assertEqual(1, result["candidate_count"])
        self.assertEqual(1500, result["candidates"][0]["base_points"])
        self.assertEqual(200, result["candidates"][0]["final_points"])
        self.assertEqual(before, hashlib.sha256((self.root / "RegularSubmissions.db").read_bytes()).digest())

    def test_apply_inserts_pending_once_and_never_changes_totals(self):
        event_id = self.event(20997, 1_500_000)
        result = self.execute(apply=True)
        self.assertEqual((1, 0), (result["inserted"], result["duplicates"]))
        self.assertTrue(Path(result["backup"]).is_file())
        row = self.rows("regular_submissions")[0]
        self.assertEqual(f"runelite:{event_id}:20997", row["external_id"])
        self.assertEqual((2, "test_alt", "123456789012345678"),
                         (row["member_id"], row["normalized_rsn"], row["discord_id"]))
        self.assertEqual("Test Alt", row["rsn"])
        self.assertEqual((2, 1.0, 2, "pending", "runelite"),
                         (row["base_points"], row["multiplier"], row["final_points"],
                          row["status"], row["source_type"]))
        self.assertEqual(77, self.rows("rank_totals")[0]["total_points"])
        again = self.execute(apply=True)
        self.assertEqual((0, 0), (again["inserted"], again["duplicates"]))
        self.assertEqual(1, again["excluded"]["already_imported"])
        self.assertEqual(1, len(self.rows("regular_submissions")))

    def test_low_value_report_stays_intake_only_without_backup(self):
        self.event(526, 31)
        result = self.execute(apply=True)
        self.assertEqual(0, result["candidate_count"])
        self.assertEqual(0, result["inserted"])
        self.assertIsNone(result["backup"])
        self.assertEqual([], self.rows("regular_submissions"))

    def test_restricted_service_mode_skips_large_backup(self):
        self.event(20997, 1_500_000)
        result = run(self.intake, self.root, 50, apply=True, backup=False)
        self.assertEqual(1, result["inserted"])
        self.assertIsNone(result["backup"])
        self.assertEqual("disabled_for_pending_service", result["backup_policy"])
        self.assertFalse((self.root / "backups").exists())

    def test_fixed_reward_is_personal_and_capped(self):
        self.event(999, 1)
        result = self.execute()
        row = result["candidates"][0]
        self.assertEqual((250, 200, "pets/kits"),
                         (row["base_points"], row["final_points"], row["category"]))
        self.assertIn("never share", row["notes"])

    def test_schema_mismatch_stops_before_backup_or_write(self):
        self.event(20997, 1_500_000)
        with closing(sqlite3.connect(self.root / "RegularSubmissions.db")) as db:
            db.execute("ALTER TABLE regular_submissions RENAME TO unexpected")
            db.commit()
        with self.assertRaises(ValueError):
            self.execute(apply=True)
        self.assertFalse((self.root / "backups").exists())


if __name__ == "__main__":
    unittest.main()
