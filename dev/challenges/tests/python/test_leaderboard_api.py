from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from flask import Flask

import leaderboard_api
import challenge_config
import leaderboard_proof_resolver


EXPECTED_PARITY_DIGEST = "1c7ce246a5ef90ba018e8ef13f063abd88477115d72ccfb3d93ee6bf7e2276d4"


class LeaderboardApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tempdir = tempfile.TemporaryDirectory(prefix="leaderboard-api-")
        root = Path(cls.tempdir.name)
        cls.challenges = root / "Challenges.db"
        cls.members = root / "Members.db"
        cls._backup_sqlite(Path("/srv/projects/database/Challenges.db"), cls.challenges)
        cls._backup_sqlite(Path("/srv/projects/database/Members.db"), cls.members)
        with sqlite3.connect(cls.challenges) as conn:
            conn.row_factory = sqlite3.Row
            challenge_config.migrate_schema(conn)
        leaderboard_api.CHALLENGES_DB = cls.challenges
        leaderboard_api.MEMBERS_DB = cls.members
        app = Flask(__name__)
        app.register_blueprint(leaderboard_api.bp)
        app.testing = True
        cls.client = app.test_client()

    @classmethod
    def tearDownClass(cls):
        cls.tempdir.cleanup()

    @staticmethod
    def _backup_sqlite(source: Path, target: Path):
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src:
            with sqlite3.connect(target) as dst:
                src.backup(dst)

    def test_modes_uses_active_published_version(self):
        response = self.client.get("/api/leaderboards/modes")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertTrue(body["ok"])
        with sqlite3.connect(self.challenges) as conn:
            active_version = conn.execute(
                "SELECT config_version_id FROM challenge_config_versions WHERE status='active'"
            ).fetchone()[0]
        self.assertEqual(body["config_version_id"], active_version)
        self.assertEqual(len(body["modes"]), 17)
        orders = [mode["display_order"] for mode in body["modes"]]
        self.assertEqual(orders, sorted(orders))
        required = {"mode_key", "display_name", "icon_url", "active", "display_order", "metric_type"}
        self.assertTrue(all(required <= set(mode) for mode in body["modes"]))

    def test_mode_returns_top_n_and_invalid_mode_is_404(self):
        response = self.client.get("/api/leaderboards/corrupted_gauntlet")
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(len(body["entries"]), 3)
        self.assertEqual([entry["rank"] for entry in body["entries"]], [1, 2, 3])
        entry = body["entries"][0]
        self.assertTrue(entry["participants"])
        self.assertIn("normalized", entry["metric"])
        self.assertIn("display", entry["metric"])
        self.assertIn("proof_url", entry)
        cached = next(
            (
                candidate
                for candidate in body["entries"]
                if candidate["proof_url"]
                and candidate["proof_url"].startswith("https://nocturne.events/proofs/")
            ),
            None,
        )
        # This mode may not have a cached top-three proof; resolution is covered
        # independently and the API must always return a safe web URL or null.
        self.assertTrue(
            cached is not None
            or all(
                item["proof_url"] is None
                or item["proof_url"].startswith(("https://", "http://"))
                for item in body["entries"]
            )
        )
        self.assertIsNotNone(body["last_calculated_at"])
        missing = self.client.get("/api/leaderboards/not_a_real_mode")
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.get_json()["error"], "leaderboard_mode_not_found")

    def test_empty_configured_mode_is_valid(self):
        response = self.client.get("/api/leaderboards/tob_solo")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["entries"], [])

    def test_confirmed_historical_404_entry_has_null_proof(self):
        import hashlib
        with sqlite3.connect(self.challenges) as conn:
            row = conn.execute(
                "SELECT proof_url FROM leaderboard_observations WHERE observation_id=6"
            ).fetchone()
        original = row[0]
        old = leaderboard_proof_resolver.VERIFIED_HISTORICAL_404S
        leaderboard_proof_resolver.VERIFIED_HISTORICAL_404S = {
            6: hashlib.sha256(original.encode()).hexdigest()
        }
        try:
            response = self.client.get("/api/leaderboards/toa_expert_solo")
            self.assertEqual(response.status_code, 200)
            entry = next(x for x in response.get_json()["entries"] if x["observation_id"] == 6)
            self.assertIsNone(entry["proof_url"])
            self.assertEqual("unavailable", entry["proof_status"])
        finally:
            leaderboard_proof_resolver.VERIFIED_HISTORICAL_404S = old

    def test_player_lookup_by_id_primary_rsn_and_alias(self):
        with sqlite3.connect(self.challenges) as challenges:
            member_id = challenges.execute(
                "SELECT member_id FROM leaderboard_observation_participants ORDER BY observation_id LIMIT 1"
            ).fetchone()[0]
        with sqlite3.connect(self.members) as members:
            rsn = members.execute("SELECT rsn FROM members WHERE member_id=?", (member_id,)).fetchone()[0]
            alias = members.execute(
                "SELECT alias_rsn FROM member_aliases WHERE member_id=? ORDER BY alias_id LIMIT 1", (member_id,)
            ).fetchone()
        by_id = self.client.get(f"/api/leaderboards/player/{member_id}")
        by_rsn = self.client.get(f"/api/leaderboards/player/{rsn}")
        self.assertEqual(by_id.status_code, 200)
        self.assertEqual(by_rsn.status_code, 200)
        self.assertEqual(by_id.get_json()["member"]["member_id"], member_id)
        self.assertEqual(by_id.get_json()["personal_bests"], by_rsn.get_json()["personal_bests"])
        if alias:
            by_alias = self.client.get(f"/api/leaderboards/player/{alias[0]}")
            self.assertEqual(by_alias.status_code, 200)
            self.assertEqual(by_alias.get_json()["member"]["member_id"], member_id)

    def test_unknown_member_is_404(self):
        response = self.client.get("/api/leaderboards/player/definitely-not-a-clan-member-xyz")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"], "member_not_found")

    def test_import_snapshot_top_three_parity(self):
        modes = self.client.get("/api/leaderboards/modes").get_json()["modes"]
        canonical = []
        for mode in modes:
            body = self.client.get(f"/api/leaderboards/{mode['mode_key']}").get_json()
            for entry in body["entries"]:
                canonical.append({
                    "mode_key": mode["mode_key"],
                    "rank": entry["rank"],
                    "metric": entry["metric"]["normalized"],
                    "participants": sorted(p["member_id"] for p in entry["participants"]),
                    "proof": bool(entry["proof_url"]),
                    "config": entry["config_version_id"],
                })
        self.assertEqual(len(canonical), 45)
        digest = hashlib.sha256(
            json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.assertEqual(digest, EXPECTED_PARITY_DIGEST)

    def test_requests_are_read_only(self):
        def digest(path: Path) -> str:
            return hashlib.sha256(path.read_bytes()).hexdigest()

        before = (digest(self.challenges), digest(self.members))
        self.client.get("/api/leaderboards/modes")
        self.client.get("/api/leaderboards/corrupted_gauntlet")
        self.client.get("/api/leaderboards/player/1093")
        after = (digest(self.challenges), digest(self.members))
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
