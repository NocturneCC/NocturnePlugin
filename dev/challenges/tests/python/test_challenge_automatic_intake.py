from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

os.environ["CHALLENGE_INTAKE_SKIP_DEFAULT_APP"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "service"))

from challenge_automatic_intake import migrate_schema, rollback_schema
from challenge_award_delivery import migrate_schema as migrate_award_schema
from challenge_config import (
    create_draft, migrate_schema as migrate_config_schema, publish_draft,
    save_draft, validate_draft,
)
from challenge_direct_intake import migrate_phase3b_schema
from challenge_intake_api import create_app
from challenge_shadow_common import migrate_schema as migrate_core_schema, rw_connection
from leaderboard_challenge_ingest import migrate as migrate_leaderboard_ingest


LEADERBOARD_FIXTURE = """
CREATE TABLE challenge_bosses (
 boss_key TEXT PRIMARY KEY, display_name TEXT NOT NULL, metric_type TEXT DEFAULT 'time',
 sort_order INTEGER DEFAULT 999, image_url TEXT, is_active INTEGER DEFAULT 1
);
CREATE TABLE challenge_submissions (
 id INTEGER PRIMARY KEY AUTOINCREMENT, normalized_rsn TEXT, rsn TEXT, discord_id TEXT,
 raw_boss TEXT, boss_key TEXT, boss_name TEXT, tier_rank INTEGER DEFAULT 0,
 tier_name TEXT, metric_display TEXT, party_key TEXT, approved_by TEXT,
 submitted_at TEXT, raw_note TEXT
);
CREATE TABLE leaderboard_import_runs (
 import_run_id INTEGER PRIMARY KEY AUTOINCREMENT, source_system TEXT,
 snapshot_captured_at TEXT, leaderboard_sha256 TEXT, proof_sha256 TEXT,
 leaderboard_row_count INTEGER, proof_row_count INTEGER,
 leaderboard_headers_json TEXT, proof_headers_json TEXT,
 leaderboard_max_index INTEGER, proof_max_index INTEGER, imported_at TEXT
);
CREATE TABLE leaderboard_observations (
 observation_id INTEGER PRIMARY KEY AUTOINCREMENT, import_run_id INTEGER,
 config_version_id INTEGER, mode_key TEXT, metric_type TEXT, metric_value INTEGER,
 metric_unit TEXT, metric_display TEXT, comparison_direction TEXT, proof_url TEXT,
 proof_identity_type TEXT, proof_identity TEXT, occurred_at TEXT, last_occurred_at TEXT,
 party_key TEXT, party_display TEXT, competitor_key TEXT, source_system TEXT,
 source_indexes_json TEXT, source_rows_json TEXT, source_payload_hashes_json TEXT,
 source_row_count INTEGER, ingest_fingerprint TEXT, identity_state TEXT, created_at TEXT
);
CREATE TABLE leaderboard_observation_participants (
 observation_id INTEGER, participant_order INTEGER, discord_id TEXT, submitted_rsn TEXT,
 member_id INTEGER, subject_key TEXT, resolution_method TEXT, source_indexes_json TEXT,
 PRIMARY KEY(observation_id,participant_order)
);
"""

MEMBERS_SCHEMA = """
CREATE TABLE members(
 member_id INTEGER PRIMARY KEY, rsn TEXT NOT NULL, normalized_rsn TEXT NOT NULL UNIQUE,
 discord_id TEXT, display_name TEXT, status TEXT DEFAULT 'active'
);
CREATE TABLE member_accounts(
 account_id INTEGER PRIMARY KEY, member_id INTEGER NOT NULL, rsn TEXT,
 normalized_rsn TEXT UNIQUE, is_primary INTEGER DEFAULT 0, is_active INTEGER DEFAULT 1
);
CREATE TABLE member_aliases(
 alias_id INTEGER PRIMARY KEY, member_id INTEGER NOT NULL, alias_rsn TEXT,
 normalized_alias_rsn TEXT UNIQUE
);
"""


class AutomaticObservationTests(unittest.TestCase):
    @staticmethod
    @contextmanager
    def db(path):
        conn = sqlite3.connect(path)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="challenge-auto-observation-")
        root = Path(self.temp.name)
        self.challenge_path = root / "Challenges.db"
        self.members_path = root / "Members.db"
        with self.db(self.challenge_path) as conn:
            conn.executescript(LEADERBOARD_FIXTURE)
        conn = rw_connection(self.challenge_path)
        migrate_core_schema(conn)
        migrate_phase3b_schema(conn)
        migrate_config_schema(conn)
        migrate_award_schema(conn)
        migrate_leaderboard_ingest(conn)
        migrate_schema(conn)
        self.enable_policies(conn)
        conn.execute(
            "INSERT INTO leaderboard_import_runs(source_system,snapshot_captured_at,imported_at) VALUES('google_legacy_csv:test','2020-01-01T00:00:00Z','2020-01-01T00:00:00Z')"
        )
        conn.commit()
        conn.close()
        with self.db(self.members_path) as conn:
            conn.executescript(MEMBERS_SCHEMA)
            names = ["Primary One", "Party Two", "Third Clan", "Fourth Clan", "Fifth Clan", "Sixth Clan"]
            for member_id, name in enumerate(names, 1):
                normalized = "".join(ch.lower() for ch in name if ch.isalnum())
                conn.execute("INSERT INTO members(member_id,rsn,normalized_rsn,status) VALUES(?,?,?,'active')",
                             (member_id, name, normalized))
        self.app = create_app({
            "TESTING": True, "CHALLENGES_DB": self.challenge_path,
            "MEMBERS_DB": self.members_path, "INTAKE_TOKEN": "test-token-never-returned",
            "RATE_LIMIT_ENABLED": False,
        })
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def enable_policies(conn):
        draft = create_draft(conn, "fixture")
        document = draft["config"]
        scope_by_boss = {
            "tob_2": "segment", "tob_3": "segment", "tob_5": "segment",
            "hmt_5": "overall", "toa_1_300": "overall", "cox_1": "overall",
            "cm_1": "overall", "cm_3": "overall", "cm_5": "overall",
        }
        for boss in document["bosses"]:
            scope = scope_by_boss.get(boss["boss_key"])
            if scope:
                boss["automatic_capture"] = "enabled"
                boss["timing_scope"] = scope
                boss["timing_segment_key"] = "timed_rooms" if scope == "segment" else None
                boss["timing_segment_label"] = "Combined timed rooms" if scope == "segment" else None
        saved = save_draft(conn, draft["draft_id"], document, "fixture", expected_revision=draft["revision"])
        validation = validate_draft(conn, draft["draft_id"], "fixture", expected_revision=saved["revision"])
        if not validation["valid"]:
            raise AssertionError(validation["errors"])
        publish_draft(conn, draft["draft_id"], "fixture", confirmed=True,
                      expected_revision=validation["draft"]["revision"])

    def publish_policy(self, boss_key, scope, capture):
        conn = rw_connection(self.challenge_path)
        draft = create_draft(conn, "fixture-policy")
        document = draft["config"]
        boss = next(row for row in document["bosses"] if row["boss_key"] == boss_key)
        boss["timing_scope"] = scope
        boss["automatic_capture"] = capture
        boss["timing_segment_key"] = "timed_rooms" if scope == "segment" else None
        boss["timing_segment_label"] = "Combined timed rooms" if scope == "segment" else None
        saved = save_draft(conn, draft["draft_id"], document, "fixture-policy", expected_revision=draft["revision"])
        validation = validate_draft(conn, draft["draft_id"], "fixture-policy", expected_revision=saved["revision"])
        if not validation["valid"]:
            raise AssertionError(validation["errors"])
        publish_draft(conn, draft["draft_id"], "fixture-policy", confirmed=True,
                      expected_revision=validation["draft"]["revision"])
        conn.close()

    def payload(self, activity="theatre_of_blood", mode="tob_duo", roster=None, **updates):
        roster = roster or ["Primary One", "Party Two"]
        value = {
            "schema_version": 1,
            "event_id": str(uuid.uuid4()),
            "reporter_rsn": roster[0],
            "activity_key": activity,
            "mode_key": mode,
            "occurred_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "room_time_ms": 901230,
            "overall_time_ms": 1802460,
            "roster": roster,
            "group_size": len(roster),
            "completion_count": None,
            "plugin_version": "0.3.2",
        }
        value.update(updates)
        return value

    def post(self, payload):
        return self.client.post("/api/challenges/intake/observations", json=payload)

    def counts(self):
        with self.db(self.challenge_path) as conn:
            return tuple(conn.execute(
                "SELECT (SELECT COUNT(*) FROM challenge_automatic_observations),(SELECT COUNT(*) FROM challenge_submissions),(SELECT COUNT(*) FROM leaderboard_observations)"
            ).fetchone())

    def test_regular_tob_selects_room_time(self):
        response = self.post(self.payload())
        self.assertEqual(response.status_code, 201, response.get_json())
        with self.db(self.challenge_path) as conn:
            row = conn.execute("SELECT selected_timing_scope,selected_duration_ms FROM challenge_automatic_observations").fetchone()
            submission = conn.execute("SELECT metric_value,metric_display,source_system,approver_discord_id,source_approved_at FROM challenge_submissions").fetchone()
            mode = conn.execute("SELECT mode_key,metric_value FROM leaderboard_observations").fetchone()
        self.assertEqual(row, ("segment", 901230))
        self.assertEqual(submission[0], 901230)
        self.assertEqual(submission[2], "runelite_automatic")
        self.assertIsNone(submission[3])
        self.assertIsNone(submission[4])
        self.assertEqual(mode, ("tob_duo", 901230))

    def test_hmt_selects_overall_time(self):
        payload = self.payload("theatre_of_blood_hard_mode", "hmt_5man",
                               ["Primary One", "Party Two", "Third Clan", "Fourth Clan", "Fifth Clan"])
        response = self.post(payload)
        self.assertEqual(response.status_code, 201, response.get_json())
        with self.db(self.challenge_path) as conn:
            self.assertEqual(conn.execute("SELECT selected_duration_ms FROM challenge_automatic_observations").fetchone()[0], 1802460)

    def test_toa_and_cox_choose_overall_time(self):
        cases = [
            ("tombs_of_amascut", "toa_expert_solo", ["Primary One"]),
            ("chambers_of_xeric", "cox_solo", ["Primary One"]),
            ("chambers_of_xeric_challenge_mode", "cox_cm_solo", ["Primary One"]),
        ]
        for activity, mode, roster in cases:
            with self.subTest(activity=activity):
                response = self.post(self.payload(activity, mode, roster))
                self.assertEqual(response.status_code, 201, response.get_json())
                with self.db(self.challenge_path) as conn:
                    self.assertEqual(conn.execute(
                        "SELECT selected_timing_scope,selected_duration_ms FROM challenge_automatic_observations ORDER BY observation_id DESC LIMIT 1"
                    ).fetchone(), ("overall", 1802460))

    def test_solo_complete_all_clan_and_mixed_roster_projection(self):
        solo = self.post(self.payload("tombs_of_amascut", "toa_expert_solo", ["Primary One"]))
        self.assertEqual(solo.status_code, 201)
        all_clan = self.post(self.payload("theatre_of_blood", "tob_5man",
                                          ["Primary One", "Party Two", "Third Clan", "Fourth Clan", "Fifth Clan"]))
        self.assertEqual(all_clan.status_code, 201)
        mixed = self.post(self.payload("theatre_of_blood", "tob_duo", ["Primary One", "Guest Name"]))
        self.assertEqual(mixed.status_code, 201)
        with self.db(self.challenge_path) as conn:
            obs = conn.execute("SELECT observed_group_size,submission_id FROM challenge_automatic_observations ORDER BY observation_id DESC LIMIT 1").fetchone()
            roster_count = conn.execute("SELECT COUNT(*) FROM challenge_automatic_observation_participants WHERE observation_id=(SELECT observation_id FROM challenge_automatic_observations ORDER BY observation_id DESC LIMIT 1)").fetchone()[0]
            credited = conn.execute("SELECT COUNT(*) FROM challenge_submission_participants WHERE submission_id=?", (obs[1],)).fetchone()[0]
            projected = conn.execute("SELECT mode_key,party_display FROM leaderboard_observations ORDER BY observation_id DESC LIMIT 1").fetchone()
        self.assertEqual((obs[0], roster_count, credited), (2, 2, 1))
        self.assertEqual(projected[0], "tob_duo")
        self.assertIn("1 unlinked", projected[1])

    def test_reporter_and_duplicate_roster_validation(self):
        missing = self.payload(reporter_rsn="Third Clan")
        self.assertEqual(self.post(missing).get_json()["state"], "invalid")
        duplicated = self.payload(roster=["Primary One", "primary-one"])
        self.assertEqual(self.post(duplicated).get_json()["reason"], "duplicate_participant")
        normalized_duplicate = self.payload(roster=["Primary One", "primaryone"])
        self.assertEqual(self.post(normalized_duplicate).get_json()["reason"], "duplicate_participant")
        mismatch = self.payload(group_size=3)
        self.assertEqual(self.post(mismatch).get_json()["reason"], "incomplete_roster")
        absent = self.payload(roster=["Party Two", "Third Clan"], reporter_rsn="Primary One")
        self.assertEqual(self.post(absent).get_json()["reason"], "reporter_not_in_roster")
        unknown = self.payload("tombs_of_amascut", "toa_expert_solo", ["Unknown"], reporter_rsn="Unknown")
        self.assertEqual(self.post(unknown).get_json()["reason"], "reporter_not_eligible")

    def test_invalid_durations_groups_and_unknown_private_fields_fail_closed(self):
        for duration in (0, -1, 86_400_001, 2**63, True, "901230"):
            with self.subTest(duration=duration):
                self.assertEqual(self.post(self.payload(room_time_ms=duration)).get_json()["state"], "invalid")
        self.assertEqual(self.post(self.payload(room_time_ms=None, overall_time_ms=None)).get_json()["reason"], "missing_metric")
        self.assertEqual(self.post(self.payload("theatre_of_blood", "tob_duo", ["Primary One"])).get_json()["reason"], "invalid_group_size")
        self.assertEqual(self.post(self.payload(mode="not_a_published_mode")).get_json()["reason"], "unsupported_mode")
        self.assertEqual(self.post(self.payload(room_time_ms=None)).get_json()["reason"], "selected_metric_missing")
        for size in (0, -1, 11, True, "2"):
            with self.subTest(size=size):
                self.assertEqual(self.post(self.payload(group_size=size)).get_json()["state"], "invalid")
        for addition in ({"discord_id": "123456789012345678"}, {"member_id": 42}, {"pb": True}, {"chat_text": "private"}, {"config_version_id": 1}, {"points": 999}):
            self.assertEqual(self.post({**self.payload(), **addition}).get_json()["state"], "invalid")

    def test_manual_unconfigured_and_disabled_policy_are_ignored_without_submission(self):
        payload = self.payload()
        self.publish_policy("tob_2", "segment", "manual_only")
        manual = self.post(payload)
        self.assertEqual(manual.get_json(), {"state": "ignored", "reason": "manual_only"})
        self.assertEqual(self.counts()[1], 0)

        self.publish_policy("tob_2", "unconfigured", "manual_only")
        unconfigured = self.post(self.payload())
        self.assertEqual(unconfigured.get_json(), {"state": "ignored", "reason": "unconfigured"})

    def test_direct_intake_disabled_policy_is_ignored(self):
        with self.db(self.challenge_path) as conn:
            conn.execute("UPDATE challenge_settings SET setting_value='disabled' WHERE setting_key='direct_intake_mode'")
        disabled = self.post(self.payload())
        self.assertEqual(disabled.get_json(), {"state": "ignored", "reason": "disabled"})

    def test_event_idempotency_conflict_and_semantic_replay(self):
        payload = self.payload(completion_count=55)
        first = self.post(payload)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(self.post(payload).get_json(), {"state": "duplicate"})
        changed = {**payload, "overall_time_ms": payload["overall_time_ms"] + 10}
        self.assertEqual(self.post(changed).status_code, 409)
        semantic_retry = {**payload, "event_id": str(uuid.uuid4())}
        self.assertEqual(self.post(semantic_retry).get_json(), {"state": "duplicate"})
        semantic_conflict = {**changed, "event_id": str(uuid.uuid4())}
        self.assertEqual(self.post(semantic_conflict).status_code, 409)
        self.assertEqual(self.counts()[1], 1)
        self.assertEqual(self.counts()[2], 1)

    def test_concurrent_duplicate_requests_create_one_result(self):
        payload = self.payload()
        barrier = threading.Barrier(2)
        results = []
        def send():
            client = self.app.test_client()
            barrier.wait()
            response = client.post("/api/challenges/intake/observations", json=payload)
            results.append((response.status_code, response.get_json()))
        threads = [threading.Thread(target=send) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=20)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertCountEqual([item[1]["state"] for item in results], ["accepted", "duplicate"])
        self.assertEqual(self.counts()[1:], (1, 1))

    def test_pb_improves_and_does_not_regress_for_worse_result(self):
        first = self.payload(room_time_ms=300_000)
        self.assertEqual(self.post(first).status_code, 201)
        better = self.payload(room_time_ms=250_000)
        self.assertEqual(self.post(better).status_code, 201)
        worse = self.payload(room_time_ms=400_000)
        self.assertEqual(self.post(worse).status_code, 201)
        with self.db(self.challenge_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT metric_value FROM challenge_member_bests WHERE subject_key='member:1' AND boss_key='tob_2'"
            ).fetchone()[0], 250_000)

    def test_projection_uses_stored_full_group_size_not_resolved_participants(self):
        response = self.post(self.payload(roster=["Primary One", "Guest Name"], group_size=2))
        self.assertEqual(response.status_code, 201, response.get_json())
        with self.db(self.challenge_path) as conn:
            row = conn.execute("SELECT mode_key,party_display FROM leaderboard_observations").fetchone()
        self.assertEqual(row[0], "tob_duo")
        self.assertTrue(row[1].endswith("+ 1 unlinked"))

    def test_transaction_failure_rolls_back_observation_submission_and_projection(self):
        before = self.counts()
        import leaderboard_challenge_ingest
        with patch.object(leaderboard_challenge_ingest, "ingest_connection", side_effect=RuntimeError("fixture")):
            with self.assertLogs("challenge_intake_api", level="ERROR") as captured:
                response = self.post(self.payload())
        self.assertEqual(response.get_json(), {"state": "server_failure"})
        log_text = "\n".join(captured.output)
        self.assertNotIn("test-token-never-returned", log_text)
        self.assertNotIn("Primary One", log_text)
        self.assertNotIn("fixture", log_text)
        self.assertEqual(self.counts(), before)

    def test_rate_limit_and_body_bound_have_stable_responses(self):
        limited_app = create_app({
            "TESTING": True, "CHALLENGES_DB": self.challenge_path,
            "MEMBERS_DB": self.members_path, "INTAKE_TOKEN": "test-token-never-returned",
            "RATE_LIMIT_ENABLED": True,
        })
        client = limited_app.test_client()
        for _ in range(10):
            self.assertEqual(client.post("/api/challenges/intake/observations", json={}).status_code, 422)
        response = client.post("/api/challenges/intake/observations", json={})
        self.assertEqual((response.status_code, response.get_json()), (429, {"state": "rate_limited"}))
        oversized = self.client.post(
            "/api/challenges/intake/observations", data=b" " * 8193, content_type="application/json"
        )
        self.assertEqual((oversized.status_code, oversized.get_json()),
                         (413, {"state": "invalid", "reason": "request_too_large"}))

    def test_timestamp_and_strict_json_contract(self):
        for stamp in ("not-a-date", "2020-01-01T00:00:00Z", "2099-01-01T00:00:00Z", "2026-10-03T12:00:00"):
            self.assertEqual(self.post(self.payload(occurred_at=stamp)).get_json()["state"], "invalid")
        response = self.client.post("/api/challenges/intake/observations", data=b'{"a":1,"a":2}', content_type="application/json")
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/challenges/intake/observations", data=b"{}", content_type="text/plain")
        self.assertEqual(response.status_code, 415)

    def test_response_and_logs_do_not_expose_privileged_or_private_values(self):
        response = self.post(self.payload())
        text = response.get_data(as_text=True)
        self.assertNotIn("test-token-never-returned", text)
        self.assertNotIn("discord", text.lower())
        self.assertNotIn("member_id", text)

    def test_migration_is_reversible_only_when_empty(self):
        with self.db(self.challenge_path) as conn:
            rollback_schema(conn)
            self.assertIsNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='challenge_automatic_observations'").fetchone())
            migrate_schema(conn)
            self.assertIsNotNone(conn.execute("SELECT 1 FROM sqlite_master WHERE name='challenge_automatic_observations'").fetchone())

    def test_migration_rollback_refuses_to_erase_observation_data(self):
        self.assertEqual(self.post(self.payload()).status_code, 201)
        with self.db(self.challenge_path) as conn:
            with self.assertRaisesRegex(RuntimeError, "prevents migration rollback"):
                rollback_schema(conn)


if __name__ == "__main__":
    unittest.main()
