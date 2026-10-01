from __future__ import annotations

import copy
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

sys.path.append("/srv/projects/nocturne-services")

import challenge_config_api
from challenge_config import config_document
from challenge_member_view import build_boss_projection, build_overall


SOURCE = Path(
    "/srv/projects/backups/database/Challenges/"
    "Challenges.db.before-phase4b-20260929T044217Z"
)
MEMBER_ID = 990001
SUBJECT = f"member:{MEMBER_ID}"


MEMBERS_SCHEMA = """
CREATE TABLE members (
 member_id INTEGER PRIMARY KEY,rsn TEXT UNIQUE,normalized_rsn TEXT UNIQUE,
 discord_id TEXT UNIQUE,display_name TEXT,status TEXT,rank_points INTEGER DEFAULT 0
);
CREATE TABLE member_accounts (
 account_id INTEGER PRIMARY KEY,member_id INTEGER,rsn TEXT,normalized_rsn TEXT UNIQUE,
 is_primary INTEGER DEFAULT 0,is_active INTEGER DEFAULT 1
);
CREATE TABLE member_aliases (
 alias_id INTEGER PRIMARY KEY,member_id INTEGER,alias_rsn TEXT,
 normalized_alias_rsn TEXT UNIQUE
);
"""


class ChallengeMemberApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.challenge_path = root / "Challenges.db"
        self.members_path = root / "Members.db"
        shutil.copy2(SOURCE, self.challenge_path)
        with sqlite3.connect(self.members_path) as conn:
            conn.executescript(MEMBERS_SCHEMA)
            conn.execute(
                "INSERT INTO members VALUES(?,?,?,?,?,?,?)",
                (MEMBER_ID, "Fixture Main", "fixturemain", "111111111111111111",
                 "Fixture Display", "active", 1234),
            )
            conn.execute(
                "INSERT INTO member_accounts VALUES(1,?,?,?,?,?)",
                (MEMBER_ID, "Fixture Alt", "fixturealt", 0, 1),
            )
            conn.execute(
                "INSERT INTO member_aliases VALUES(1,?,?,?)",
                (MEMBER_ID, "Fixture Old", "fixtureold"),
            )
        with sqlite3.connect(self.challenge_path) as conn:
            conn.row_factory = sqlite3.Row
            config_id = int(conn.execute(
                "SELECT config_version_id FROM challenge_config_versions WHERE status='active'"
            ).fetchone()[0])
            submission_id = int(conn.execute(
                "SELECT MIN(submission_id) FROM challenge_submissions"
            ).fetchone()[0])
            for boss_key, maximum in (("gauntlet", 2), ("delve", 3)):
                tiers = conn.execute(
                    """SELECT tier_key,tier_rank FROM challenge_tiers
                        WHERE config_version_id=? AND boss_key=? AND tier_rank<=?
                        ORDER BY tier_rank""",
                    (config_id, boss_key, maximum),
                ).fetchall()
                for tier in tiers:
                    conn.execute(
                        """INSERT INTO challenge_member_tier_achievements
                           (subject_key,member_id,boss_key,tier_key,tier_rank,
                            first_qualifying_submission_id,config_version_id,earned_at,
                            achievement_basis)
                           VALUES(?,?,?,?,?,?,?,?,?)""",
                        (SUBJECT, MEMBER_ID, boss_key, tier["tier_key"], tier["tier_rank"],
                         submission_id, config_id, f"2026-01-0{tier['tier_rank']}",
                         "explicit_submission" if tier["tier_rank"] == maximum else "inferred_from_higher_tier"),
                    )
            conn.execute(
                """INSERT INTO challenge_member_bests
                   (subject_key,member_id,boss_key,metric_type,metric_value,metric_unit,
                    metric_display,submission_id,evaluated_config_version_id,calculated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (SUBJECT, MEMBER_ID, "gauntlet", "time", 600123, "milliseconds",
                 "uncanonical source", submission_id, config_id, "2026-01-10T00:00:00Z"),
            )
            conn.execute(
                """INSERT INTO challenge_member_bests
                   (subject_key,member_id,boss_key,metric_type,metric_value,metric_unit,
                    metric_display,submission_id,evaluated_config_version_id,calculated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (SUBJECT, MEMBER_ID, "delve", "numeric", 42, "waves", "42",
                 submission_id, config_id, "2026-01-11T00:00:00Z"),
            )
            conn.execute(
                """INSERT INTO challenge_member_progress
                   (subject_key,member_id,config_version_id,challenge_progression_points,
                    active_challenges_completed,active_challenge_count,current_system_tier_key,
                    current_system_tier_rank,bronze_complete,silver_complete,gold_complete,
                    platinum_complete,ascendant_eligible,source_watermark,calculation_hash,calculated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (SUBJECT, MEMBER_ID, config_id, 90, 2, 15, None, 0, 0, 0, 0, 0, 0,
                 999, "fixture", "2026-01-12T00:00:00Z"),
            )
        challenge_config_api.CHALLENGES_DB = self.challenge_path
        challenge_config_api.MEMBERS_DB = self.members_path
        self.app = Flask(__name__)
        self.app.register_blueprint(challenge_config_api.bp)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def response(self, path="/api/challenges/member/Fixture%20Main"):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def test_primary_alias_and_member_id_lookup(self):
        primary = self.response()
        alias = self.response("/api/challenges/member?rsn=Fixture%20Old")
        linked = self.response("/api/challenges/member?rsn=Fixture%20Alt")
        by_id = self.response(f"/api/challenges/member/{MEMBER_ID}")
        for payload in (primary, alias, linked, by_id):
            self.assertEqual(payload["member"]["member_id"], MEMBER_ID)
            self.assertEqual(payload["member"]["primary_rsn"], "Fixture Main")
            self.assertNotIn("discord_id", payload["member"])

    def test_unknown_member_is_404(self):
        response = self.client.get("/api/challenges/member/No%20Such%20Fixture")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json["error"], "member_not_found")

    def test_time_numeric_and_missing_pb_display(self):
        payload = self.response()
        bosses = {boss["boss_key"]: boss for boss in payload["bosses"]}
        self.assertEqual(bosses["gauntlet"]["pb"]["display"], "0:10:00.12")
        self.assertEqual(bosses["gauntlet"]["pb"]["normalized"], 600123)
        self.assertEqual(bosses["delve"]["pb"]["display"], "42")
        self.assertIsNone(bosses["colosseum"]["pb"])

    def test_five_tier_statuses_points_and_active_order(self):
        payload = self.response()
        self.assertEqual(len(payload["bosses"]), 15)
        orders = [boss["display_order"] for boss in payload["bosses"]]
        self.assertEqual(orders, sorted(orders))
        gauntlet = next(boss for boss in payload["bosses"] if boss["boss_key"] == "gauntlet")
        self.assertEqual(len(gauntlet["tiers"]), 5)
        self.assertEqual([tier["earned"] for tier in gauntlet["tiers"]], [True, True, False, False, False])
        self.assertTrue(gauntlet["tiers"][1]["is_highest"])
        self.assertEqual(gauntlet["progression_points"], 30)
        self.assertEqual(payload["overall"]["progression_points"], 90)
        self.assertEqual(payload["overall"]["next_tier"]["tier_key"], "bronze")
        self.assertEqual(payload["overall"]["next_tier"]["points_remaining"], 60)

    def test_completion_display_and_inactive_boss_exclusion(self):
        with sqlite3.connect(self.challenge_path) as conn:
            conn.row_factory = sqlite3.Row
            config = config_document(conn)
        synthetic = copy.deepcopy(config)
        gauntlet = next(boss for boss in synthetic["bosses"] if boss["boss_key"] == "gauntlet")
        gauntlet["metric_type"] = "completion"
        gauntlet["comparison_direction"] = "complete"
        achievements = {("gauntlet", "bronze"): {"tier_rank": 1, "earned_at": "2026-01-01"}}
        projected = build_boss_projection(synthetic, achievements, {})
        completion = next(boss for boss in projected if boss["boss_key"] == "gauntlet")
        self.assertEqual(completion["pb"]["display"], "Completed")
        gauntlet["active"] = False
        projected = build_boss_projection(synthetic, achievements, {})
        self.assertNotIn("gauntlet", {boss["boss_key"] for boss in projected})
        self.assertEqual(build_overall(synthetic, projected)["progression_points"], 0)

    def test_legacy_baseline_is_visible_but_not_evidence_backed(self):
        with sqlite3.connect(self.challenge_path) as conn:
            conn.execute(
                """INSERT INTO challenge_legacy_baselines
                   (snapshot_key,subject_key,member_id,boss_key,highest_tier_key,
                    highest_tier_rank,progression_points,source_system,
                    source_provenance_json,source_provenance_sha256,
                    config_version_id,snapshot_timestamp)
                   VALUES('fixture',?,?, 'colosseum','bronze',1,10,
                          'google_legacy_baseline','{}','fixture',2,'2026-09-29T00:00:00Z')""",
                (SUBJECT, MEMBER_ID),
            )
        payload = self.response()
        boss = next(item for item in payload["bosses"] if item["boss_key"] == "colosseum")
        self.assertEqual(boss["highest_tier_key"], "bronze")
        self.assertEqual(boss["highest_tier_source"], "legacy_baseline")
        self.assertTrue(boss["tiers"][0]["earned"])
        self.assertFalse(boss["tiers"][0]["evidence_backed"])
        self.assertEqual(payload["overall"]["progression_points"], 100)

    def test_ascendant_missing_boss_list(self):
        bosses = [{
            "boss_key": f"boss_{index}", "display_name": f"Boss {index}",
            "complete": index < 14, "progression_points": 150 if index < 14 else 0,
            "highest_tier_key": "ascendant" if index < 14 else None,
        } for index in range(15)]
        with sqlite3.connect(self.challenge_path) as conn:
            conn.row_factory = sqlite3.Row
            config = config_document(conn)
        overall = build_overall(config, bosses)
        self.assertEqual(overall["current_tier"]["tier_key"], "platinum")
        self.assertEqual(overall["next_tier"]["tier_key"], "ascendant")
        self.assertIsNone(overall["next_tier"]["percentage"])
        self.assertEqual(overall["ascendant_missing_bosses"], [
            {"boss_key": "boss_14", "display_name": "Boss 14"}
        ])

    def test_existing_member_ascendant_recalculates_for_added_or_deactivated_boss(self):
        with sqlite3.connect(self.challenge_path) as conn:
            conn.row_factory = sqlite3.Row
            config = config_document(conn)
        achievements = {
            (boss["boss_key"], "bronze"): {
                "tier_rank": 1,
                "earned_at": "2026-01-01T00:00:00Z",
            }
            for boss in config["bosses"] if boss["active"]
        }
        projected = build_boss_projection(config, achievements, {})
        self.assertTrue(build_overall(config, projected)["ascendant_requirements_met"])

        expanded = copy.deepcopy(config)
        future = copy.deepcopy(expanded["bosses"][0])
        future.update({
            "boss_key": "future_boss",
            "display_name": "Future Boss",
            "display_order": 9999,
            "aliases": [],
            "icon_url": None,
            "active": True,
        })
        expanded["bosses"].append(future)
        projected = build_boss_projection(expanded, achievements, {})
        overall = build_overall(expanded, projected)
        self.assertFalse(overall["ascendant_requirements_met"])
        self.assertIn(
            {"boss_key": "future_boss", "display_name": "Future Boss"},
            overall["ascendant_missing_bosses"],
        )

        future["active"] = False
        projected = build_boss_projection(expanded, achievements, {})
        overall = build_overall(expanded, projected)
        self.assertTrue(overall["ascendant_requirements_met"])
        self.assertNotIn("future_boss", {boss["boss_key"] for boss in projected})

    def test_bronze_through_platinum_next_tier_calculations(self):
        with sqlite3.connect(self.challenge_path) as conn:
            conn.row_factory = sqlite3.Row
            config = config_document(conn)
        cases = (
            (0, "bronze", 150),
            (160, "silver", 290),
            (500, "gold", 400),
            (1000, "platinum", 500),
        )
        for points, expected_tier, remaining in cases:
            with self.subTest(points=points):
                bosses = [{
                    "boss_key": "fixture", "display_name": "Fixture", "complete": points > 0,
                    "progression_points": points, "highest_tier_key": "bronze" if points else None,
                }]
                overall = build_overall(config, bosses)
                self.assertEqual(overall["next_tier"]["tier_key"], expected_tier)
                self.assertEqual(overall["next_tier"]["points_remaining"], remaining)

    def test_safe_member_search_and_leaderboard_foundation(self):
        search = self.client.get("/api/challenges/members/search?q=Fixture%20Old")
        self.assertEqual(search.status_code, 200)
        self.assertEqual(search.json["members"][0]["member_id"], MEMBER_ID)
        self.assertNotIn("discord_id", search.json["members"][0])
        board = self.client.get("/api/challenges/leaderboard")
        self.assertEqual(board.status_code, 200)
        entry = next(item for item in board.json["entries"] if item["member"]["member_id"] == MEMBER_ID)
        self.assertEqual(entry["progression_points"], 90)
        self.assertNotIn("discord_id", entry["member"])

    def test_requests_do_not_write_rank_points(self):
        with sqlite3.connect(self.members_path) as conn:
            before = conn.execute("SELECT rank_points FROM members WHERE member_id=?", (MEMBER_ID,)).fetchone()[0]
        with patch("socket.create_connection", side_effect=AssertionError("network access attempted")):
            self.response()
            self.client.get("/api/challenges/leaderboard")
        with sqlite3.connect(self.members_path) as conn:
            after = conn.execute("SELECT rank_points FROM members WHERE member_id=?", (MEMBER_ID,)).fetchone()[0]
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
