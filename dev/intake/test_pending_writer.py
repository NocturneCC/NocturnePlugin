import json
import base64
import hashlib
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from intake import socket_handoff
from pending_writer import handle_connection, process_payload, store_screenshot


NOW = datetime(2026, 9, 1, 23, 0, tzinfo=timezone.utc)


class PendingWriterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.create(self.root / "Members.db", """
            CREATE TABLE members(member_id INTEGER, rsn TEXT, discord_id TEXT, status TEXT);
            CREATE TABLE member_accounts(member_id INTEGER, rsn TEXT, is_active INTEGER);
            INSERT INTO members VALUES(2,'Test Main','123456789012345678','active');
            INSERT INTO member_accounts VALUES(2,'Test Alt',1);
        """)
        self.create(self.root / "Items.db", """
            CREATE TABLE items(item_id INTEGER, osrs_item_id INTEGER, item_name TEXT,
                normalized_item_name TEXT, latest_price INTEGER,
                latest_price_checked_at TEXT, is_active INTEGER);
            CREATE TABLE pet_kit_point_values(name TEXT, normalized_name TEXT,
                points INTEGER, category TEXT, is_active INTEGER);
            INSERT INTO items VALUES(10,20997,'Twisted bow','twisted_bow',1,NULL,1);
            INSERT INTO items VALUES(11,526,'Bones','bones',1,NULL,1);
            INSERT INTO items VALUES(12,29790,'Noxious point','noxious_point',0,NULL,1);
        """)
        self.create(self.root / "RegularSubmissions.db", """
            CREATE TABLE regular_submissions (
                submission_id INTEGER PRIMARY KEY AUTOINCREMENT, member_id INTEGER,
                rsn TEXT NOT NULL, normalized_rsn TEXT NOT NULL, discord_id TEXT,
                item_id INTEGER, osrs_item_id INTEGER, item_name TEXT NOT NULL,
                normalized_item_name TEXT NOT NULL, item_price INTEGER DEFAULT 0,
                base_points INTEGER NOT NULL DEFAULT 0, multiplier REAL NOT NULL DEFAULT 1.0,
                final_points INTEGER NOT NULL DEFAULT 0, category TEXT NOT NULL DEFAULT 'drop',
                source_type TEXT NOT NULL DEFAULT 'regular', notes TEXT,
                status TEXT NOT NULL DEFAULT 'pending', submitted_at TEXT,
                identity_match_method TEXT, identity_match_notes TEXT,
                identity_review_status TEXT, external_id TEXT,
                screenshot_url TEXT
                ,price_source TEXT, valuation_rule_id TEXT, valuation_catalogue_version INTEGER,
                finished_output_item_id INTEGER, finished_output_item_name TEXT,
                finished_output_market_price_gp INTEGER, derived_unit_price_gp INTEGER
            );
            CREATE TABLE rank_totals(member_id INTEGER PRIMARY KEY, total_points INTEGER);
            INSERT INTO rank_totals VALUES(2,77);
        """)

    @staticmethod
    def create(path, sql):
        with closing(sqlite3.connect(path)) as db:
            db.executescript(sql)
            db.commit()

    @staticmethod
    def payload(item_id=20997, price=1_500_000, version=2):
        item = {"item_id": item_id, "quantity": 1}
        if version == 2:
            item["unit_price_gp"] = price
        return {
            "version": version,
            "event_id": str(uuid4()),
            "occurred_at": NOW.isoformat(),
            "rsn": "Test Alt",
            "source": "Test source",
            "items": [item],
        }

    @staticmethod
    def with_screenshot(payload):
        raw = b"\xff\xd8pending-writer-test\xff\xd9"
        payload["version"] = 3
        payload["screenshot"] = {
            "mime_type": "image/jpeg", "width": 640, "height": 480,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "data_base64": base64.b64encode(raw).decode(),
        }
        return payload, raw

    def rows(self, query):
        with closing(sqlite3.connect(self.root / "RegularSubmissions.db")) as db:
            return db.execute(query).fetchall()

    def test_eligible_report_creates_one_pending_row_idempotently(self):
        payload = self.payload()
        first = process_payload(payload, self.root, NOW.timestamp())
        second = process_payload(payload, self.root, NOW.timestamp())
        self.assertEqual(("pending_stored", 1), (first["status"], first["inserted"]))
        self.assertEqual("duplicate", second["status"])
        row = self.rows("SELECT rsn,status,source_type,final_points FROM regular_submissions")[0]
        self.assertEqual(("Test Alt", "pending", "runelite", 2), row)
        self.assertEqual([(77,)], self.rows("SELECT total_points FROM rank_totals"))

    def test_derived_metadata_is_preserved_on_pending_row(self):
        payload = self.payload(29790, 500_000, version=2)
        payload["version"] = 4
        payload["items"][0].update({
            "price_source": "runelite_derived_equal_share",
            "valuation_rule_id": "noxious_halberd_components",
            "valuation_catalogue_version": 1,
            "finished_output_item_id": 29796,
            "finished_output_item_name": "Noxious halberd",
            "finished_output_market_price_gp": 1_500_002,
            "derived_unit_price_gp": 500_000,
        })
        self.assertEqual("pending_stored", process_payload(payload, self.root, NOW.timestamp())["status"])
        row = self.rows("SELECT price_source,valuation_rule_id,finished_output_item_id,finished_output_market_price_gp,derived_unit_price_gp FROM regular_submissions")[0]
        self.assertEqual(("runelite_derived_equal_share", "noxious_halberd_components",
                          29796, 1_500_002, 500_000), row)

    def test_low_value_and_legacy_reports_are_excluded(self):
        low = process_payload(self.payload(526, 31), self.root, NOW.timestamp())
        legacy = process_payload(self.payload(526, version=1), self.root, NOW.timestamp())
        self.assertEqual("excluded", low["status"])
        self.assertEqual("excluded", legacy["status"])
        self.assertEqual([], self.rows("SELECT submission_id FROM regular_submissions"))

    def test_eligible_screenshot_is_private_and_attached_to_pending_row(self):
        payload, raw = self.with_screenshot(self.payload())
        result = process_payload(payload, self.root, NOW.timestamp())
        self.assertEqual("pending_stored", result["status"])
        expected_url = (
            "/admin/api/nocturne/runelite-submission-images/"
            + payload["event_id"] + ".jpg"
        )
        self.assertEqual([(expected_url,)], self.rows(
            "SELECT screenshot_url FROM regular_submissions"
        ))
        image = self.root / "runelite-submission-images" / (payload["event_id"] + ".jpg")
        self.assertEqual(raw, image.read_bytes())
        self.assertEqual(0o600, image.stat().st_mode & 0o777)

    def test_excluded_screenshot_is_not_persisted(self):
        payload, _raw = self.with_screenshot(self.payload(526, 31))
        self.assertEqual("excluded", process_payload(payload, self.root, NOW.timestamp())["status"])
        self.assertFalse((self.root / "runelite-submission-images").exists())

    def test_screenshot_cap_falls_back_to_pending_without_image(self):
        directory = self.root / "runelite-submission-images"
        directory.mkdir(mode=0o700)
        with patch("pending_writer.MAX_STORED_SCREENSHOTS", 0):
            payload, _raw = self.with_screenshot(self.payload())
            result = process_payload(payload, self.root, NOW.timestamp())
        self.assertEqual("pending_stored", result["status"])
        self.assertEqual([(None,)], self.rows(
            "SELECT screenshot_url FROM regular_submissions"
        ))
        self.assertEqual([], list(directory.glob("*.jpg")))

    def test_unsafe_screenshot_directory_is_rejected(self):
        directory = self.root / "runelite-submission-images"
        directory.mkdir(mode=0o770)
        payload, _raw = self.with_screenshot(self.payload())
        with self.assertRaisesRegex(ValueError, "unsafe screenshot directory"):
            store_screenshot(payload["screenshot"], payload["event_id"], self.root)

    def test_socket_protocol_returns_small_receipt(self):
        server, client = socket.socketpair()
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        payload = self.payload()
        payload["occurred_at"] = datetime.now(timezone.utc).isoformat()
        worker = threading.Thread(target=handle_connection, args=(server, self.root))
        worker.start()
        client.sendall(json.dumps(payload).encode())
        client.shutdown(socket.SHUT_WR)
        receipt = json.loads(client.recv(2048))
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual("pending_stored", receipt["status"])

    def test_socket_handoff_accepts_writer_receipt(self):
        class FakeSocket:
            def __init__(self):
                self.sent = b""
                self.responses = [b'{"status":"excluded"}', b""]

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def settimeout(self, _timeout):
                pass

            def connect(self, _path):
                pass

            def sendall(self, raw):
                self.sent += raw

            def shutdown(self, _direction):
                pass

            def recv(self, _size):
                return self.responses.pop(0)

        fake = FakeSocket()
        with patch("intake.socket.socket", return_value=fake):
            receipt = socket_handoff("/run/test.sock", json.dumps(self.payload()), timeout=2)
        self.assertEqual("excluded", receipt["status"])
        self.assertTrue(fake.sent)


if __name__ == "__main__":
    unittest.main()
