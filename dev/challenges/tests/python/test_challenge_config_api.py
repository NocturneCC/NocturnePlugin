from __future__ import annotations

import copy
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

from flask import Flask

os.environ["CHALLENGE_INTAKE_SKIP_DEFAULT_APP"] = "1"
sys.path.append("/srv/projects/nocturne-services")

import challenge_config_api
from challenge_config import create_draft, migrate_schema, save_draft
from challenge_intake_api import create_app


SOURCE = Path(
    "/srv/projects/backups/database/Challenges/"
    "Challenges.db.before-phase4a1-20260929T025335Z"
)


class PublicConfigApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "Challenges.db"
        shutil.copy2(SOURCE, self.path)
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            migrate_schema(conn)
        challenge_config_api.CHALLENGES_DB = self.path
        self.app = Flask(__name__)
        self.app.register_blueprint(challenge_config_api.bp)
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def test_active_endpoint_exposes_only_published_snapshot(self):
        before = self.client.get("/api/challenges/config/active")
        self.assertEqual(before.status_code, 200)
        active_id = before.json["version_id"]
        self.assertEqual(len(before.json["bosses"]), 15)
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            draft = create_draft(conn, "test")
            doc = copy.deepcopy(draft["config"])
            doc["bosses"][0]["display_name"] = "Unpublished Name"
            save_draft(conn, draft["draft_id"], doc, "test", draft["revision"])
        after = self.client.get("/api/challenges/config/active")
        self.assertEqual(after.json["version_id"], active_id)
        self.assertNotIn(
            "Unpublished Name", [boss["display_name"] for boss in after.json["bosses"]]
        )
        self.assertIn("ETag", after.headers)


class InternalAdminApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.path = root / "Challenges.db"
        self.members_path = root / "Members.db"
        shutil.copy2(SOURCE, self.path)
        sqlite3.connect(self.members_path).close()
        with sqlite3.connect(self.path) as conn:
            conn.row_factory = sqlite3.Row
            migrate_schema(conn)
        self.token = "t" * 64
        self.app = create_app({
            "TESTING": True,
            "CHALLENGES_DB": self.path,
            "MEMBERS_DB": self.members_path,
            "INTAKE_TOKEN": self.token,
            "RATE_LIMIT_ENABLED": False,
        })
        self.client = self.app.test_client()
        self.headers = {"Authorization": f"Bearer {self.token}"}
        with sqlite3.connect(self.path) as conn:
            self.active_id = int(conn.execute(
                "SELECT config_version_id FROM challenge_config_versions WHERE status='active'"
            ).fetchone()[0])

    def tearDown(self):
        self.tmp.cleanup()

    def post(self, path, body=None, authenticated=True):
        return self.client.post(
            path, json=body or {}, headers=self.headers if authenticated else {}
        )

    def create_draft(self):
        response = self.post("/internal/challenges/config/draft")
        self.assertEqual(response.status_code, 200)
        return response.json["draft"]

    def save(self, draft, document):
        response = self.client.put(
            "/internal/challenges/config/draft",
            json={
                "draft_id": draft["draft_id"],
                "revision": draft["revision"],
                "config": document,
            },
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200, response.json)
        return response.json["draft"]

    def test_unauthenticated_admin_write_rejected(self):
        response = self.post(
            "/internal/challenges/config/draft", authenticated=False
        )
        self.assertEqual(response.status_code, 401)

    def test_unvalidated_and_stale_revision_publish_rejected(self):
        draft = self.create_draft()
        unvalidated = self.post(
            "/internal/challenges/config/draft/publish",
            {"draft_id": draft["draft_id"], "revision": draft["revision"], "confirm": True},
        )
        self.assertEqual(unvalidated.status_code, 409)
        self.assertEqual(unvalidated.json["error"], "draft_revision_not_validated")
        document = copy.deepcopy(draft["config"])
        document["bosses"][0]["description"] = "revision guard"
        saved = self.save(draft, document)
        validated = self.post(
            "/internal/challenges/config/draft/validate",
            {"draft_id": saved["draft_id"], "revision": saved["revision"]},
        )
        self.assertEqual(validated.status_code, 200)
        stale = self.post(
            "/internal/challenges/config/draft/publish",
            {"draft_id": saved["draft_id"], "revision": draft["revision"], "confirm": True},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json["error"], "draft_revision_conflict")

    def test_noop_publish_rejected_without_active_switch(self):
        draft = self.create_draft()
        validated = self.post(
            "/internal/challenges/config/draft/validate",
            {"draft_id": draft["draft_id"], "revision": draft["revision"]},
        )
        self.assertTrue(validated.json["valid"])
        first_publish = self.post(
            "/internal/challenges/config/draft/publish",
            {"draft_id": draft["draft_id"], "revision": draft["revision"], "confirm": True},
        )
        self.assertEqual(first_publish.status_code, 200)
        first_active_id = first_publish.json["config"]["version_id"]

        # The legacy active snapshot predates leaderboard configuration.  Its
        # seeded modes are publishable once; an actual no-op after that must
        # still be rejected without switching the active version.
        second_draft = self.create_draft()
        second_validated = self.post(
            "/internal/challenges/config/draft/validate",
            {"draft_id": second_draft["draft_id"], "revision": second_draft["revision"]},
        )
        self.assertTrue(second_validated.json["valid"])
        response = self.post(
            "/internal/challenges/config/draft/publish",
            {
                "draft_id": second_draft["draft_id"],
                "revision": second_draft["revision"],
                "confirm": True,
            },
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["error"], "no_changes_to_publish")
        with sqlite3.connect(self.path) as conn:
            active = conn.execute(
                "SELECT config_version_id FROM challenge_config_versions WHERE status='active'"
            ).fetchone()[0]
        self.assertEqual(active, first_active_id)

    def add_temporary_boss(self, draft, key="api_draft_only"):
        boss = copy.deepcopy(draft["config"]["bosses"][0])
        boss.update({
            "boss_key": key,
            "display_name": "API Draft Only",
            "aliases": ["API Draft Only"],
            "display_order": 9990,
            "active": False,
            "submission_enabled": False,
        })
        response = self.post(
            "/internal/challenges/config/draft/bosses",
            {"draft_id": draft["draft_id"], "revision": draft["revision"], "boss": boss},
        )
        self.assertEqual(response.status_code, 200, response.json)
        return response.json["draft"]

    def delete_boss(self, draft, key):
        return self.client.delete(
            f"/internal/challenges/config/draft/bosses/{key}",
            json={"draft_id": draft["draft_id"], "revision": draft["revision"]},
            headers=self.headers,
        )

    def test_unpublished_removal_allowed_and_published_removal_rejected(self):
        draft = self.add_temporary_boss(self.create_draft())
        removed = self.delete_boss(draft, "api_draft_only")
        self.assertEqual(removed.status_code, 200, removed.json)
        self.assertNotIn(
            "api_draft_only",
            {boss["boss_key"] for boss in removed.json["draft"]["config"]["bosses"]},
        )
        rejected = self.delete_boss(removed.json["draft"], "gauntlet")
        self.assertEqual(rejected.status_code, 409)
        self.assertEqual(rejected.json["error"], "published_boss_cannot_be_removed")

    def test_historical_unpublished_removal_rejected(self):
        draft = self.add_temporary_boss(self.create_draft(), "historical_api_draft")
        with sqlite3.connect(self.path) as conn:
            submission_id = conn.execute(
                "SELECT MIN(submission_id) FROM challenge_submissions"
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO challenge_member_bests
                   (subject_key,member_id,boss_key,metric_type,metric_value,metric_unit,
                    metric_display,submission_id,evaluated_config_version_id,calculated_at)
                   VALUES('test:api-history',NULL,'historical_api_draft','completion',1,
                          'boolean','Completion',?,?,CURRENT_TIMESTAMP)""",
                (submission_id, self.active_id),
            )
        response = self.delete_boss(draft, "historical_api_draft")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json["error"], "historical_boss_cannot_be_removed")

    def test_metric_conversions_are_normalized_by_api(self):
        draft = self.create_draft()
        document = copy.deepcopy(draft["config"])
        boss = next(item for item in document["bosses"] if item["boss_key"] == "gauntlet")
        boss["metric_type"] = "numeric"
        numeric = self.save(draft, document)
        boss = next(item for item in numeric["config"]["bosses"] if item["boss_key"] == "gauntlet")
        self.assertTrue(all(
            tier["operator"] == "gte" and tier["unit"] == "waves"
            for tier in boss["tiers"][1:]
        ))
        document = copy.deepcopy(numeric["config"])
        boss = next(item for item in document["bosses"] if item["boss_key"] == "gauntlet")
        boss["metric_type"] = "completion"
        completion = self.save(numeric, document)
        boss = next(item for item in completion["config"]["bosses"] if item["boss_key"] == "gauntlet")
        self.assertTrue(all(
            tier["metric_type"] == "completion"
            and tier["operator"] == "complete"
            and tier["unit"] == "boolean"
            and tier["threshold"] == 1
            for tier in boss["tiers"]
        ))

    def test_invalid_tier_semantics_fail_validation(self):
        draft = self.create_draft()
        document = copy.deepcopy(draft["config"])
        boss = next(item for item in document["bosses"] if item["boss_key"] == "gauntlet")
        boss["tiers"][1]["unit"] = "waves"
        # Simulate an older/externally malformed saved draft. The regular save
        # path repairs this automatically; validation must still reject stored
        # invalid semantics authoritatively.
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "UPDATE challenge_config_drafts SET draft_json=? WHERE draft_id=?",
                (json.dumps(document), draft["draft_id"]),
            )
        response = self.post(
            "/internal/challenges/config/draft/validate",
            {"draft_id": draft["draft_id"], "revision": draft["revision"]},
        )
        self.assertFalse(response.json["valid"])
        self.assertIn("invalid_metric_semantics", {
            error["code"] for error in response.json["errors"]
        })

    def test_protected_diff_reports_current_saved_revision(self):
        draft = self.create_draft()
        document = copy.deepcopy(draft["config"])
        boss = next(item for item in document["bosses"] if item["boss_key"] == "gauntlet")
        boss["display_name"] = "Diff Preview Boss"
        boss["tiers"][1]["tier"] = "Argent"
        saved = self.save(draft, document)
        response = self.post(
            "/internal/challenges/config/draft/diff",
            {"draft_id": saved["draft_id"], "revision": saved["revision"]},
        )
        self.assertEqual(200, response.status_code, response.json)
        self.assertTrue(response.json["diff"]["has_changes"])
        self.assertEqual(saved["revision"], response.json["diff"]["revision"])
        change = next(
            item for item in response.json["diff"]["bosses"]
            if item["boss_key"] == "gauntlet"
        )
        self.assertEqual("modified", change["change_type"])
        self.assertEqual("silver", change["tiers"][0]["tier_key"])

        stale = self.post(
            "/internal/challenges/config/draft/diff",
            {"draft_id": saved["draft_id"], "revision": saved["revision"] - 1},
        )
        self.assertEqual(409, stale.status_code)
        self.assertEqual("draft_revision_conflict", stale.json["error"])
        unauthorized = self.post(
            "/internal/challenges/config/draft/diff",
            {"draft_id": saved["draft_id"], "revision": saved["revision"]},
            authenticated=False,
        )
        self.assertEqual(401, unauthorized.status_code)


if __name__ == "__main__":
    unittest.main()
