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
            conn.execute("INSERT INTO members(member_id,rsn,normalized_rsn,status) VALUES(7,?,?, 'active')",
                         ("Doctor Arden", "doctor_arden"))
            conn.execute("INSERT INTO members(member_id,rsn,normalized_rsn,status) VALUES(8,?,?, 'active')",
                         ("Linked Parent", "linked_parent"))
            conn.execute("INSERT INTO member_accounts(account_id,member_id,rsn,normalized_rsn,is_primary,is_active) VALUES(3,8,?,?,0,1)",
                         ("Linked Hero", "linked_hero"))
            conn.execute("INSERT INTO member_accounts(account_id,member_id,rsn,normalized_rsn,is_primary,is_active) VALUES(4,8,?,?,0,0)",
                         ("Inactive Alt", "inactive_alt"))
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
            "phosanis": "overall",
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

    def test_public_plugin_tracking_catalog_and_non_challenge_observation_route(self):
        config = self.client.get("/api/plugin/tracking/config")
        self.assertEqual(200, config.status_code)
        self.assertIn("ETag", config.headers)
        document = config.get_json()["config"]
        nightmare = next(row for row in document["activities"]
                         if row["activity_key"] == "nightmare")
        self.assertTrue(nightmare["modes"][0]["ttk_tracking_enabled"])
        self.assertNotIn("test-token-never-returned", config.get_data(as_text=True))
        self.assertNotIn("draft", config.get_data(as_text=True).lower())

        payload = {
            "schema_version": 1, "event_id": str(uuid.uuid4()),
            "reporter_rsn": "Doctor Arden", "activity_key": "nightmare",
            "mode_key": "nightmare", "occurred_at": datetime.now(timezone.utc).isoformat(),
            "overall_time_ms": 358200, "roster": ["Doctor Arden"], "group_size": 1,
            "plugin_version": "0.3.2",
        }
        response = self.client.post("/api/plugin/tracking/observations", json=payload)
        self.assertEqual(200, response.status_code, response.get_json())
        self.assertEqual({"state": "accepted"}, response.get_json())
        retry = self.client.post("/api/plugin/tracking/observations", json=payload)
        self.assertEqual(200, retry.status_code)
        self.assertEqual({"state": "duplicate"}, retry.get_json())
        with self.db(self.challenge_path) as conn:
            self.assertEqual(1, conn.execute(
                "SELECT COUNT(*) FROM plugin_tracking_observations").fetchone()[0])
            self.assertEqual(1, conn.execute(
                "SELECT COUNT(*) FROM plugin_tracking_observation_participants").fetchone()[0])
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM challenge_submissions").fetchone()[0])
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM leaderboard_observations").fetchone()[0])

    def test_public_plugin_tracking_accepts_typed_numeric_doom_completion(self):
        config = self.client.get("/api/plugin/tracking/config").get_json()["config"]
        doom = next(row for row in config["activities"]
                    if row["activity_key"] == "doom_of_mokhaiotl")
        mode = doom["modes"][0]
        self.assertTrue(mode["completion_tracking_enabled"])
        self.assertEqual("deepest_level", mode["completion_metric_key"])
        self.assertFalse(mode["ttk_tracking_enabled"])

        payload = {
            "schema_version": 1, "event_id": str(uuid.uuid4()),
            "reporter_rsn": "Doctor Arden", "activity_key": "doom_of_mokhaiotl",
            "mode_key": "doom_of_mokhaiotl", "occurred_at": datetime.now(timezone.utc).isoformat(),
            "completion_metric_key": "deepest_level", "completion_value": 7,
            "roster": ["Doctor Arden"], "group_size": 1, "plugin_version": "0.3.2",
        }
        response = self.client.post("/api/plugin/tracking/observations", json=payload)
        self.assertEqual(200, response.status_code, response.get_json())
        self.assertEqual({"state": "accepted"}, response.get_json())
        retry = self.client.post("/api/plugin/tracking/observations", json=payload)
        self.assertEqual({"state": "duplicate"}, retry.get_json())
        with self.db(self.challenge_path) as conn:
            row = conn.execute("SELECT completion_metric_key,completion_value,observed_group_size "
                               "FROM plugin_tracking_completion_observations").fetchone()
            self.assertEqual(("deepest_level", 7, 1), row)
            self.assertEqual(1, conn.execute(
                "SELECT COUNT(*) FROM plugin_tracking_completion_participants").fetchone()[0])
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM plugin_tracking_observations").fetchone()[0])
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM challenge_submissions").fetchone()[0])
            self.assertEqual(0, conn.execute(
                "SELECT COUNT(*) FROM leaderboard_observations").fetchone()[0])

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

    def pnm_payload(self, **updates):
        roster = updates.pop("roster", ["Primary One"])
        values = {"room_time_ms": None, "overall_time_ms": 358200,
                  "completion_count": 1394}
        values.update(updates)
        return self.payload(
            "phosanis_nightmare", "phosani_nightmare", roster,
            **values,
        )

    def test_phosani_nightmare_accepts_only_solo_overall_observation(self):
        response = self.post(self.pnm_payload())
        self.assertEqual((response.status_code, response.get_json()), (201, {"state": "accepted"}))
        with self.db(self.challenge_path) as conn:
            observation = conn.execute(
                """SELECT activity_key,mode_key,overall_time_ms,selected_timing_scope,
                          selected_duration_ms,observed_group_size,disposition
                     FROM challenge_automatic_observations"""
            ).fetchone()
            participant = conn.execute(
                "SELECT normalized_rsn FROM challenge_automatic_observation_participants"
            ).fetchall()
            submission = conn.execute(
                "SELECT boss_key,metric_value,source_system FROM challenge_submissions"
            ).fetchone()
        self.assertEqual(observation, (
            "phosanis_nightmare", "phosani_nightmare", 358200,
            "overall", 358200, 1, "accepted",
        ))
        self.assertEqual(participant, [("primaryone",)])
        self.assertEqual(submission, ("phosanis", 358200, "runelite_automatic"))

    def test_java_serialized_pnm_request_is_accepted_and_retry_is_idempotent(self):
        fixture = Path(__file__).resolve().parents[4] / "src/test/resources/pnm-automatic-observation.json"
        request_body = fixture.read_bytes()
        java_payload = json.loads(request_body)
        self.assertRegex(java_payload["event_id"],
                         r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
        self.assertEqual(java_payload["reporter_rsn"], "Doctor Arden")
        self.assertEqual(java_payload["roster"], ["Doctor Arden"])
        self.assertEqual(java_payload["overall_time_ms"], 308400)
        self.assertNotIn("room_time_ms", java_payload)
        self.assertNotIn("pb", java_payload)
        self.assertNotIn("config_version_id", java_payload)
        self.assertEqual(java_payload["plugin_version"], "0.3.2")
        response = self.client.post(
            "/api/challenges/intake/observations", data=request_body,
            content_type="application/json; charset=utf-8",
        )
        self.assertEqual((response.status_code, response.get_json()), (201, {"state": "accepted"}))
        with self.db(self.challenge_path) as conn:
            observation = conn.execute(
                """SELECT activity_key,mode_key,reporter_normalized_rsn,overall_time_ms,
                          room_time_ms,selected_timing_scope,selected_duration_ms,observed_group_size,
                          occurred_at,plugin_version,config_version_id
                     FROM challenge_automatic_observations"""
            ).fetchone()
            active_version = conn.execute(
                "SELECT config_version_id FROM challenge_config_versions WHERE status='active'"
            ).fetchone()[0]
            participants = conn.execute(
                "SELECT normalized_rsn FROM challenge_automatic_observation_participants"
            ).fetchall()
            result_value = conn.execute("SELECT boss_key,metric_value FROM challenge_submissions").fetchone()
        self.assertEqual(observation, (
            "phosanis_nightmare", "phosani_nightmare", "doctorarden", 308400,
            None, "overall", 308400, 1, "2026-10-12T19:45:17.000Z", "0.3.2", active_version,
        ))
        self.assertEqual(participants, [("doctorarden",)])
        self.assertEqual(result_value, ("phosanis", 308400))

        retry = self.client.post(
            "/api/challenges/intake/observations", data=request_body,
            content_type="application/json; charset=utf-8",
        )
        self.assertEqual((retry.status_code, retry.get_json()), (200, {"state": "duplicate"}))
        self.assertEqual(self.counts(), (1, 1, 1))

    def test_challenge_and_authoritative_member_databases_are_separate(self):
        with self.db(self.challenge_path) as challenges:
            challenge_tables = {row[0] for row in challenges.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        with self.db(self.members_path) as members:
            member_tables = {row[0] for row in members.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("members", challenge_tables)
        self.assertIn("members", member_tables)
        self.assertIn("member_accounts", member_tables)

    def test_active_primary_rsn_accepts_space_or_underscore_normalization(self):
        for completion_count, reporter in enumerate(("Doctor Arden", "Doctor_Arden"), 1394):
            with self.subTest(reporter=reporter):
                response = self.post(self.pnm_payload(
                    reporter_rsn="Doctor Arden", roster=[reporter], completion_count=completion_count))
                self.assertEqual((response.status_code, response.get_json()),
                                 (201, {"state": "accepted"}))
        with self.db(self.challenge_path) as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM challenge_automatic_observations WHERE reporter_normalized_rsn='doctorarden'"
            ).fetchone()[0], 2)

    def test_active_linked_rsn_resolves_to_parent_member(self):
        response = self.post(self.pnm_payload(
            reporter_rsn="Linked Hero", roster=["Linked Hero"]))
        self.assertEqual((response.status_code, response.get_json()),
                         (201, {"state": "accepted"}))
        with self.db(self.challenge_path) as conn:
            participant = conn.execute(
                """SELECT member_id,normalized_rsn_snapshot,identity_resolution_method
                     FROM challenge_submission_participants"""
            ).fetchone()
            observation = conn.execute(
                "SELECT reporter_normalized_rsn,observed_group_size FROM challenge_automatic_observations"
            ).fetchone()
        self.assertEqual(participant, (8, "linkedhero", "member_accounts.normalized_rsn"))
        self.assertEqual(observation, ("linkedhero", 1))

    def test_inactive_member_linked_account_unknown_and_ambiguous_rsn_are_rejected(self):
        with self.db(self.members_path) as conn:
            conn.execute("UPDATE members SET status='inactive' WHERE member_id=7")
        inactive_member = self.post(self.pnm_payload(roster=["Doctor Arden"]))
        self.assertEqual((inactive_member.status_code, inactive_member.get_json()),
                         (422, {"state": "invalid", "reason": "reporter_not_eligible"}))

        inactive_account = self.post(self.pnm_payload(
            reporter_rsn="Inactive Alt", roster=["Inactive Alt"]))
        self.assertEqual((inactive_account.status_code, inactive_account.get_json()),
                         (422, {"state": "invalid", "reason": "reporter_not_eligible"}))

        with self.db(self.members_path) as conn:
            conn.execute("UPDATE members SET status='inactive' WHERE member_id=8")
        inactive_parent = self.post(self.pnm_payload(
            reporter_rsn="Linked Hero", roster=["Linked Hero"]))
        self.assertEqual((inactive_parent.status_code, inactive_parent.get_json()),
                         (422, {"state": "invalid", "reason": "reporter_not_eligible"}))

        unknown = self.post(self.pnm_payload(
            reporter_rsn="Unknown RSN", roster=["Unknown RSN"]))
        self.assertEqual((unknown.status_code, unknown.get_json()),
                         (422, {"state": "invalid", "reason": "reporter_not_eligible"}))

        with self.db(self.members_path) as conn:
            conn.execute("UPDATE members SET status='active' WHERE member_id=7")
            conn.execute("INSERT INTO members(member_id,rsn,normalized_rsn,status) VALUES(9,?,?, 'active')",
                         ("Doctor Arden Alt", "doctorarden"))
        ambiguous = self.post(self.pnm_payload(roster=["Doctor Arden"]))
        self.assertEqual((ambiguous.status_code, ambiguous.get_json()),
                         (422, {"state": "invalid", "reason": "reporter_not_eligible"}))
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_missing_schema_and_unsafe_member_database_paths_fail_closed(self):
        empty_members = Path(self.temp.name) / "empty-members.db"
        with self.db(empty_members):
            pass
        self.app.config["MEMBERS_DB"] = empty_members
        missing_schema = self.post(self.pnm_payload())
        self.assertEqual((missing_schema.status_code, missing_schema.get_json()),
                         (503, {"state": "server_failure"}))

        self.app.config["MEMBERS_DB"] = Path(self.temp.name) / "missing-members.db"
        missing_file = self.post(self.pnm_payload())
        self.assertEqual((missing_file.status_code, missing_file.get_json()),
                         (503, {"state": "server_failure"}))
        self.assertFalse(self.app.config["MEMBERS_DB"].exists())

        symlink = Path(self.temp.name) / "members-link.db"
        symlink.symlink_to(self.members_path)
        self.app.config["MEMBERS_DB"] = symlink
        unsafe_path = self.post(self.pnm_payload())
        self.assertEqual((unsafe_path.status_code, unsafe_path.get_json()),
                         (503, {"state": "server_failure"}))
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_phosani_nightmare_manual_unconfigured_and_mismatched_scope_are_not_accepted(self):
        self.publish_policy("phosanis", "overall", "manual_only")
        manual = self.post(self.pnm_payload())
        self.assertEqual(manual.get_json(), {"state": "ignored", "reason": "manual_only"})
        self.assertEqual(self.counts()[1], 0)

        self.publish_policy("phosanis", "unconfigured", "manual_only")
        unconfigured = self.post(self.pnm_payload(completion_count=1395))
        self.assertEqual(unconfigured.get_json(), {"state": "ignored", "reason": "unconfigured"})
        self.assertEqual(self.counts()[1], 0)

        self.publish_policy("phosanis", "segment", "enabled")
        mismatched = self.post(self.pnm_payload(completion_count=1396))
        self.assertEqual(mismatched.get_json(), {
            "state": "invalid", "reason": "unsupported_timing_policy",
        })
        self.assertEqual(self.counts()[1], 0)

    def test_phosani_nightmare_rejects_wrong_activity_or_mode_identity(self):
        wrong_activity = self.post(self.pnm_payload(activity_key="phosanis"))
        self.assertEqual(wrong_activity.get_json()["reason"], "unsupported_activity")
        wrong_mode = self.post(self.pnm_payload(mode_key="tob_solo"))
        self.assertEqual(wrong_mode.get_json()["reason"], "unsupported_mode")

    def test_phosani_nightmare_rejects_published_mode_redirect_to_another_boss(self):
        from challenge_automatic_intake import config_document as load_config_document

        def redirected_config(conn, version_id):
            document = load_config_document(conn, version_id)
            mode = next(row for row in document["leaderboard_modes"]
                        if row["mode_key"] == "phosani_nightmare")
            mode["boss_key"] = "jad"
            return document

        with patch("challenge_automatic_intake.config_document", side_effect=redirected_config):
            response = self.post(self.pnm_payload())
        self.assertEqual(response.get_json(), {"state": "invalid", "reason": "unsupported_mode"})
        self.assertEqual(self.counts()[1], 0)

    def test_phosani_nightmare_enforces_solo_roster(self):
        payload = self.pnm_payload(roster=["Primary One", "Party Two"], group_size=2)
        self.assertEqual(self.post(payload).get_json()["reason"], "invalid_group_size")
        self.assertEqual(self.counts()[1], 0)

    def test_phosani_nightmare_event_idempotency_and_payload_conflict(self):
        payload = self.pnm_payload()
        self.assertEqual(self.post(payload).status_code, 201)
        self.assertEqual(self.post(payload).get_json(), {"state": "duplicate"})
        changed = {**payload, "overall_time_ms": 358201}
        response = self.post(changed)
        self.assertEqual((response.status_code, response.get_json()),
                         (409, {"state": "idempotency_conflict"}))
        self.assertEqual(self.counts()[1], 1)
        self.assertEqual(self.counts()[2], 1)

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
