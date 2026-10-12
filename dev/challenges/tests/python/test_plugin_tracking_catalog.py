import json
import sqlite3
import unittest
import uuid
from datetime import datetime, timezone

from plugin_tracking_catalog import (
    active_document,
    client_document,
    default_document,
    match_source,
    migrate_schema,
    publish_draft,
    save_draft,
    process_tracking_observation,
    TrackingObservationError,
)


class PluginTrackingCatalogTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        migrate_schema(self.conn)
        self.members = sqlite3.connect(":memory:")
        self.members.row_factory = sqlite3.Row
        self.members.executescript("""
            CREATE TABLE members(member_id INTEGER PRIMARY KEY, rsn TEXT, normalized_rsn TEXT, status TEXT);
            CREATE TABLE member_accounts(account_id INTEGER PRIMARY KEY, member_id INTEGER,
                rsn TEXT, normalized_rsn TEXT, is_active INTEGER);
            INSERT INTO members VALUES(1,'Reporter One','reporterone','active');
        """)

    def tearDown(self):
        self.conn.close()
        self.members.close()

    def test_catalog_is_independent_and_maps_ordinary_reward_sources(self):
        tables = {row[0] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("plugin_tracking_catalog_versions", tables)
        self.assertNotIn("challenge_bosses", tables)
        self.assertEqual("phosanis_nightmare", match_source(
            self.conn, "Phosani’s Nightmare")[0]["activity_key"])
        self.assertEqual("corrupted_gauntlet", match_source(
            self.conn, "The Corrupted Gauntlet")[0]["activity_key"])
        self.assertEqual("nightmare", match_source(
            self.conn, "The Nightmare")[0]["activity_key"])

    def test_client_projection_contains_tracking_policy_not_admin_state(self):
        document = client_document(self.conn)
        self.assertEqual(1, document["schema_version"])
        self.assertNotIn("draft", json.dumps(document).lower())
        pnm = document["activities"][0]["modes"][0]
        self.assertTrue(pnm["loot_tracking_enabled"])
        self.assertTrue(pnm["ttk_tracking_enabled"])
        self.assertEqual("overall", pnm["timing_scope"])

    def test_non_challenge_activity_has_no_challenge_overlay(self):
        activity, mode = match_source(self.conn, "The Nightmare")
        self.assertEqual("nightmare", activity["activity_key"])
        self.assertIsNone(mode["challenge_boss_key"])
        self.assertTrue(mode["loot_tracking_enabled"])
        self.assertTrue(mode["ttk_tracking_enabled"])

    def test_conflicting_source_alias_fails_closed(self):
        document = default_document()
        document["activities"][1]["modes"][0]["source_names"].append("Nightmare")
        with self.assertRaisesRegex(ValueError, "catalog_source_name_duplicate"):
            save_draft(self.conn, document, "staff", 1)

    def test_publish_is_versioned_and_draft_can_be_reopened(self):
        draft = json.loads(json.dumps(default_document()))
        draft["activities"][0]["display_name"] = "Phosani’s Nightmare"
        saved = save_draft(self.conn, draft, "staff", 1)
        published = publish_draft(self.conn, "staff", saved["revision"], True)
        self.assertEqual("Phosani’s Nightmare", published["activities"][0]["display_name"])
        self.assertEqual("draft", __import__("plugin_tracking_catalog").draft_document(
            self.conn)["state"])

    def tracking_payload(self, **updates):
        payload = {
            "schema_version": 1, "event_id": str(uuid.uuid4()),
            "reporter_rsn": "Reporter One", "activity_key": "nightmare",
            "mode_key": "nightmare", "occurred_at": "2026-10-12T12:00:00Z",
            "overall_time_ms": 358200, "roster": ["Reporter One", "PublicMate"],
            "group_size": 2, "plugin_version": "0.3.2",
        }
        payload.update(updates)
        return payload

    def test_non_challenge_group_timing_is_stored_without_challenge_projection(self):
        payload = self.tracking_payload()
        self.conn.execute("BEGIN IMMEDIATE")
        result = process_tracking_observation(
            self.conn, self.members, payload,
            now=datetime(2026, 10, 12, 12, 1, tzinfo=timezone.utc))
        self.conn.commit()
        self.assertEqual({"state": "accepted"}, result)
        row = self.conn.execute(
            "SELECT activity_key,selected_timing_scope,selected_duration_ms,observed_group_size "
            "FROM plugin_tracking_observations").fetchone()
        self.assertEqual(("nightmare", "overall", 358200, 2), tuple(row))
        self.assertEqual(2, self.conn.execute(
            "SELECT COUNT(*) FROM plugin_tracking_observation_participants").fetchone()[0])

    def test_tracking_event_id_duplicate_and_changed_payload_conflict(self):
        payload = self.tracking_payload()
        self.conn.execute("BEGIN IMMEDIATE")
        self.assertEqual("accepted", process_tracking_observation(self.conn, self.members, payload)["state"])
        self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")
        self.assertEqual("duplicate", process_tracking_observation(self.conn, self.members, payload)["state"])
        self.conn.commit()
        altered = {**payload, "overall_time_ms": 358201}
        self.conn.execute("BEGIN IMMEDIATE")
        with self.assertRaises(TrackingObservationError) as raised:
            process_tracking_observation(self.conn, self.members, altered)
        self.assertEqual("idempotency_conflict", raised.exception.state)
        self.conn.rollback()
        self.assertEqual(1, self.conn.execute("SELECT COUNT(*) FROM plugin_tracking_observations").fetchone()[0])

    def test_tracking_observations_and_participants_are_append_only(self):
        payload = self.tracking_payload()
        self.conn.execute("BEGIN IMMEDIATE")
        result = process_tracking_observation(self.conn, self.members, payload)
        self.conn.commit()
        observation_id = self.conn.execute(
            "SELECT observation_id FROM plugin_tracking_observations").fetchone()[0]
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.conn.execute("UPDATE plugin_tracking_observations SET selected_duration_ms=1 WHERE observation_id=?",
                              (observation_id,))
        self.conn.rollback()
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            self.conn.execute("DELETE FROM plugin_tracking_observation_participants WHERE observation_id=?",
                              (observation_id,))
        self.conn.rollback()

    def test_tracking_requires_catalog_enabled_mode_and_complete_reporter_roster(self):
        payload = self.tracking_payload(roster=["PublicMate"], group_size=1)
        self.conn.execute("BEGIN IMMEDIATE")
        with self.assertRaises(TrackingObservationError) as raised:
            process_tracking_observation(self.conn, self.members, payload)
        self.assertEqual("reporter_not_in_roster", raised.exception.reason)
        self.conn.rollback()

        unknown = self.tracking_payload(activity_key="unlisted", mode_key="unlisted")
        self.conn.execute("BEGIN IMMEDIATE")
        with self.assertRaises(TrackingObservationError) as raised:
            process_tracking_observation(self.conn, self.members, unknown)
        self.assertEqual("unsupported_activity", raised.exception.reason)
        self.conn.rollback()

        document = default_document()
        document["activities"][2]["modes"][0]["ttk_tracking_enabled"] = False
        draft = save_draft(self.conn, document, "staff", 1)
        publish_draft(self.conn, "staff", draft["revision"], True)
        self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")
        ignored = process_tracking_observation(self.conn, self.members, self.tracking_payload())
        self.conn.commit()
        self.assertEqual({"state": "ignored", "reason": "unconfigured"}, ignored)
        self.assertEqual(0, self.conn.execute("SELECT COUNT(*) FROM plugin_tracking_observations").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
