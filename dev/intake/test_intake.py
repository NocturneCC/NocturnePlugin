import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch
from intake import create_app


class IntakeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = datetime.now(timezone.utc).timestamp()
        self.app = create_app(self.temp.name, ["Simons Alt"], clock=lambda: self.now)
        self.body = {"version": 2, "event_id": str(uuid4()),
                     "occurred_at": datetime.fromtimestamp(self.now, timezone.utc).isoformat(),
                     "rsn": "Simons Alt", "source": "Man",
                     "items": [{"item_id": 526, "quantity": 1, "unit_price_gp": 32}]}

    def send(self, body=None, raw=None, **overrides):
        raw = raw if raw is not None else json.dumps(body or self.body).encode()
        environ = {"PATH_INFO": "/api/plugin/dev/drops", "REQUEST_METHOD": "POST",
                   "CONTENT_TYPE": "application/json", "CONTENT_LENGTH": str(len(raw)),
                   "wsgi.input": io.BytesIO(raw)}
        environ.update(overrides)
        result = []
        output = self.app(environ, lambda status, headers: result.append(int(status.split()[0])))
        return result[0], json.loads(b"".join(output))

    def test_receipt_matches_committed_record_and_duplicate_does_not_add_row(self):
        code, receipt = self.send()
        self.assertEqual(201, code)
        self.assertEqual({"event_id": self.body["event_id"], "status": "stored", "storage": "development"}, receipt)
        code, receipt = self.send()
        self.assertEqual((200, "duplicate"), (code, receipt["status"]))
        with closing(sqlite3.connect(Path(self.temp.name) / "test-drops.sqlite3")) as db:
            rows = db.execute("SELECT rsn, payload FROM test_drops").fetchall()
        self.assertEqual(1, len(rows))
        self.assertEqual("simons alt", rows[0][0])
        self.assertEqual(self.body["items"], json.loads(rows[0][1])["items"])

    def test_reused_id_with_changed_payload_conflicts(self):
        self.send()
        self.body["items"][0]["quantity"] = 2
        self.assertEqual(409, self.send()[0])

    def test_distinct_ids_preserve_identical_kills(self):
        self.send()
        self.body["event_id"] = str(uuid4())
        self.assertEqual(201, self.send()[0])

    def test_unlisted_rsn_rejected_and_alias_normalization(self):
        self.body["rsn"] = "Other Player"
        self.assertEqual(403, self.send()[0])
        self.body["rsn"] = "SIMONS_ALT"
        self.assertEqual(201, self.send()[0])

    def test_extra_group_data_rejected(self):
        self.body["group"] = {"names": ["Other Player"]}
        self.assertEqual(400, self.send()[0])

    def test_request_limits_and_bad_json(self):
        self.assertEqual(413, self.send(raw=b" " * 8193)[0])
        self.assertEqual(400, self.send(raw=b"{")[0])
        self.assertEqual(411, self.send(CONTENT_LENGTH="")[0])
        self.assertEqual(415, self.send(CONTENT_TYPE="text/plain")[0])
        self.assertEqual(405, self.send(REQUEST_METHOD="GET")[0])
        self.assertEqual(404, self.send(PATH_INFO="/api/plugin/dev/drops/other")[0])

    def test_item_types_and_duplicates(self):
        for items in [[], [{"item_id": True, "quantity": 1, "unit_price_gp": 1}],
                      [{"item_id": 526, "quantity": 0, "unit_price_gp": 1}],
                      [{"item_id": 526, "quantity": 1, "unit_price_gp": True}],
                      [{"item_id": 526, "quantity": 1, "unit_price_gp": -1}],
                      [{"item_id": 526, "quantity": 1, "unit_price_gp": 1}] * 2]:
            self.body["items"] = items
            self.assertEqual(400, self.send()[0])

    def test_v1_remains_accepted_during_rollout(self):
        self.body["version"] = 1
        self.body["items"] = [{"item_id": 526, "quantity": 1}]
        self.assertEqual(201, self.send()[0])

    def test_price_field_must_match_payload_version(self):
        self.body["items"] = [{"item_id": 526, "quantity": 1}]
        self.assertEqual(400, self.send()[0])
        self.body["version"] = 1
        self.body["items"] = [{"item_id": 526, "quantity": 1, "unit_price_gp": 32}]
        self.assertEqual(400, self.send()[0])

    def test_stale_and_future_timestamps_rejected(self):
        for delta in [-601, 121]:
            self.body["occurred_at"] = datetime.fromtimestamp(self.now + delta, timezone.utc).isoformat()
            self.assertEqual(400, self.send()[0])

    def test_rate_limit_does_not_block_idempotent_receipt(self):
        for _ in range(20):
            self.body["event_id"] = str(uuid4())
            self.assertEqual(201, self.send()[0])
        self.assertEqual(200, self.send()[0])
        self.body["event_id"] = str(uuid4())
        self.assertEqual(429, self.send()[0])

    def test_storage_cap_and_retention(self):
        with patch("intake.MAX_ROWS", 1):
            self.assertEqual(201, self.send()[0])
            self.body["event_id"] = str(uuid4())
            self.assertEqual(507, self.send()[0])
            self.now += 8 * 86400
            self.body["occurred_at"] = datetime.fromtimestamp(self.now, timezone.utc).isoformat()
            self.assertEqual(201, self.send()[0])

    def test_storage_failure_never_reports_success(self):
        with patch("intake.sqlite3.connect", side_effect=sqlite3.OperationalError("locked")):
            self.assertEqual(503, self.send()[0])


if __name__ == "__main__":
    unittest.main()
