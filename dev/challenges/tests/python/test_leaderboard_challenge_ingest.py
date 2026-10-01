from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import leaderboard_api
from flask import Flask

from leaderboard_challenge_ingest import digest, ingest, parse_time_ms
from leaderboard_projection_refresh import refresh_projection
from leaderboard_shadow_renderer import ShadowRenderer


BASE_DATABASE = Path("/srv/projects/database/Challenges.db")
MEMBERS_DATABASE = Path("/srv/projects/database/Members.db")


class FlaskApi:
    def __init__(self, client):
        self.client = client

    def get(self, path):
        response = self.client.get(path)
        if response.status_code != 200:
            raise RuntimeError(f"HTTP {response.status_code}: {path}")
        return response.get_json()


class LeaderboardChallengeIngestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="leaderboard-live-ingest-")
        self.root = Path(self.temp.name)
        self.database = self.root / "Challenges.db"
        shutil.copy2(BASE_DATABASE, self.database)
        with sqlite3.connect(self.database) as conn:
            conn.execute("PRAGMA journal_mode=DELETE")
        ingest(self.database, apply=False, allow_migrate=True)

    def tearDown(self):
        self.temp.cleanup()

    def _app(self):
        leaderboard_api.CHALLENGES_DB = self.database
        leaderboard_api.MEMBERS_DB = MEMBERS_DATABASE
        app = Flask(__name__)
        app.register_blueprint(leaderboard_api.bp)
        return app

    def _seed_direct_and_mirror(self):
        conn = sqlite3.connect(self.database)
        conn.row_factory = sqlite3.Row
        pb = conn.execute(
            """SELECT o.metric_value,p.member_id,p.subject_key,p.discord_id,p.submitted_rsn
                 FROM leaderboard_personal_bests pb
                 JOIN leaderboard_observations o USING(observation_id)
                 JOIN leaderboard_observation_participants p USING(observation_id)
                WHERE pb.mode_key='corrupted_gauntlet' ORDER BY pb.position LIMIT 1"""
        ).fetchone()
        value = int(pb["metric_value"]) + 999999
        payload = json.dumps({"fixture": "approved challenge"}, sort_keys=True)
        payload_hash = digest(payload)
        common = (
            0, "fixture_external", payload_hash, digest("fixture-ingest"), None, 10,
            "gauntlet", "Corrupted Gauntlet", "bronze", 1, 10,
            "time", value, "milliseconds", "99:59.99", "fixture-party", None,
            pb["discord_id"], "staff-fixture", None, "https://example.invalid/non-pb.png",
            "fixture", "2026-10-01T16:00:00+00:00", "2026-10-01T16:00:00+00:00",
            "2026-10-01T16:00:01+00:00", payload, payload_hash, "approved", "parsed",
        )
        columns = """legacy_regular_submission_id,source_external_id,source_snapshot_hash,
          ingest_fingerprint,supersedes_submission_id,config_version_id,boss_key,boss_display_name,
          earned_tier_key,earned_tier_rank,source_submission_points,metric_type,metric_value,
          metric_unit,metric_display,party_key,party_display,submitter_discord_id,
          approver_discord_id,approver_display,evidence_url,raw_notes,source_submitted_at,
          source_approved_at,first_observed_at,raw_payload_json,raw_payload_sha256,record_state,parse_status"""
        direct = int(conn.execute(
            f"INSERT INTO challenge_submissions(source_system,source_record_id,{columns}) VALUES('discord_direct','fixture:direct'," + ",".join("?" * len(common)) + ")",
            common,
        ).lastrowid)
        mirror_values = list(common)
        mirror_values[3] = digest("fixture-mirror-ingest")
        mirror = int(conn.execute(
            f"INSERT INTO challenge_submissions(source_system,source_record_id,{columns}) VALUES('regular_submissions_sheet_sync','fixture:mirror'," + ",".join("?" * len(common)) + ")",
            tuple(mirror_values),
        ).lastrowid)
        for submission_id in (direct, mirror):
            conn.execute(
                """INSERT INTO challenge_submission_participants(
                   submission_id,subject_key,member_id,discord_id,rsn_snapshot,
                   normalized_rsn_snapshot,participant_role,identity_resolution_method)
                   VALUES(?,?,?,?,?,?,'submitter','fixture')""",
                (submission_id, pb["subject_key"], pb["member_id"], pb["discord_id"], pb["submitted_rsn"], "fixture"),
            )
        details = "{}"
        conn.execute(
            """INSERT INTO challenge_observation_reconciliation_links(
               direct_submission_id,google_submission_id,participant_subject_key,match_class,
               match_method,details_json,details_sha256,first_seen_at,last_checked_at,reconciled_at)
               VALUES(?,?,?,'MATCH','fixture',?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)""",
            (direct, mirror, pb["subject_key"], details, digest(details)),
        )
        conn.commit()
        conn.close()
        return direct, mirror

    def test_direct_and_google_mirror_create_one_non_pb_observation(self):
        app = self._app()
        before_report = ShadowRenderer(FlaskApi(app.test_client()), self.root / "before").run()
        before_group_hashes = [item["payload_sha256"] for item in before_report["rendered_groups"]]
        with sqlite3.connect(self.database) as conn:
            before_observations = conn.execute("SELECT count(*) FROM leaderboard_observations").fetchone()[0]
            before_pbs = conn.execute("SELECT count(*) FROM leaderboard_personal_bests").fetchone()[0]

        direct, mirror = self._seed_direct_and_mirror()
        result = ingest(self.database, apply=True)
        self.assertEqual(result["created"], 1)
        self.assertEqual(result["mirror_linked"], 1)
        self.assertEqual(result["would_create"], 1)
        projection = refresh_projection(self.database)
        self.assertTrue(projection["changed"] is False)

        with sqlite3.connect(self.database) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM leaderboard_observations").fetchone()[0], before_observations + 1)
            self.assertEqual(conn.execute("SELECT count(*) FROM leaderboard_personal_bests").fetchone()[0], before_pbs)
            links = conn.execute(
                "SELECT source_submission_id,observation_id FROM leaderboard_challenge_submission_links WHERE source_submission_id IN (?,?) ORDER BY source_submission_id",
                (direct, mirror),
            ).fetchall()
            self.assertEqual(len(links), 2)
            self.assertEqual(links[0][1], links[1][1])
            self.assertEqual(conn.execute("SELECT count(*) FROM leaderboard_observation_participants WHERE observation_id=?", (links[0][1],)).fetchone()[0], 1)

        replay = ingest(self.database, apply=True)
        self.assertEqual(replay["created"], 0)
        self.assertEqual(replay["scanned"], 0)
        after_report = ShadowRenderer(FlaskApi(app.test_client()), self.root / "after").run()
        self.assertEqual([item["payload_sha256"] for item in after_report["rendered_groups"]], before_group_hashes)

    def test_current_production_snapshot_has_no_post_cutoff_gap(self):
        result = ingest(self.database, apply=False)
        self.assertEqual(result["scanned"], 0)
        self.assertEqual(result["would_create"], 0)
        self.assertEqual(result["leaderboard_only_modes"], ["tob_solo", "tob_4man"])

    def test_time_parser_is_strict_and_normalizes_supported_displays(self):
        self.assertEqual(parse_time_ms("07:45.00"), 465000)
        self.assertEqual(parse_time_ms("00:36:21.00"), 2181000)
        self.assertIsNone(parse_time_ms("Completion"))
        self.assertIsNone(parse_time_ms("7 minutes"))


if __name__ == "__main__":
    unittest.main()
