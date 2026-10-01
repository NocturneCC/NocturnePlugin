import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

os.environ["CHALLENGE_INTAKE_SKIP_DEFAULT_APP"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from challenge_direct_intake import migrate_phase3b_schema, reconcile_direct_observations
from challenge_award_delivery import boss_tier_candidates, migrate_schema as migrate_award_schema
from challenge_intake_api import create_app
from challenge_shadow_common import migrate_schema, rw_connection
from challenge_shadow_sync import rebuild_derived
from challenge_config import (
    create_draft, migrate_schema as migrate_config_schema, publish_draft,
    save_draft, validate_draft,
)


LEGACY_SCHEMA = """
CREATE TABLE challenge_bosses (
  boss_key TEXT PRIMARY KEY,display_name TEXT NOT NULL,metric_type TEXT DEFAULT 'time',
  sort_order INTEGER DEFAULT 999,image_url TEXT,is_active INTEGER DEFAULT 1
);
CREATE TABLE challenge_submissions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,normalized_rsn TEXT,rsn TEXT,discord_id TEXT,
  raw_boss TEXT,boss_key TEXT,boss_name TEXT,tier_rank INTEGER DEFAULT 0,tier_name TEXT,
  metric_display TEXT,party_key TEXT,approved_by TEXT,submitted_at TEXT,raw_note TEXT
);
"""

MEMBERS_SCHEMA = """
CREATE TABLE members (
 member_id INTEGER PRIMARY KEY,rsn TEXT UNIQUE,normalized_rsn TEXT UNIQUE,
 discord_id TEXT UNIQUE,rank_points INTEGER DEFAULT 0
);
CREATE TABLE member_accounts (
 account_id INTEGER PRIMARY KEY,member_id INTEGER,rsn TEXT,normalized_rsn TEXT UNIQUE,
 is_primary INTEGER DEFAULT 0,is_active INTEGER DEFAULT 1
);
CREATE TABLE member_aliases (
 alias_id INTEGER PRIMARY KEY,member_id INTEGER,alias_rsn TEXT,normalized_alias_rsn TEXT UNIQUE
);
"""


class DirectIntakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.challenge_path = root / "Challenges.db"
        self.members_path = root / "Members.db"
        with sqlite3.connect(self.challenge_path) as conn:
            conn.executescript(LEGACY_SCHEMA)
        conn = rw_connection(self.challenge_path)
        migrate_schema(conn)
        migrate_phase3b_schema(conn)
        migrate_config_schema(conn)
        migrate_award_schema(conn)
        conn.close()
        with sqlite3.connect(self.members_path) as conn:
            conn.executescript(MEMBERS_SCHEMA)
            conn.execute("INSERT INTO members VALUES(1,'Primary One','primaryone','111111111111111111',1000)")
            conn.execute("INSERT INTO members VALUES(2,'Party Two','partytwo','222222222222222222',2000)")
            conn.execute("INSERT INTO member_aliases VALUES(1,1,'Old Alias','oldalias')")
        self.token = "t" * 64
        self.app = create_app({
            "TESTING": True,
            "CHALLENGES_DB": self.challenge_path,
            "MEMBERS_DB": self.members_path,
            "INTAKE_TOKEN": self.token,
            "RATE_LIMIT_ENABLED": False,
        })
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def payload(self, **changes):
        base = {
            "provider": "discord",
            "event_id": "approval:100000000000000001",
            "discord_guild_id": "100000000000000001",
            "discord_channel_id": "100000000000000002",
            "discord_message_id": "100000000000000003",
            "submitter_discord_id": "111111111111111111",
            "submitted_rsn": "Primary One",
            "boss": "Gauntlet",
            "earned_tier": "Platinum",
            "raw_metric": "00:06:25.20",
            "normalized_metric": {"value": 385200, "unit": "milliseconds"},
            "party_members": [],
            "approver_discord_id": "999999999999999999",
            "evidence_url": "https://example.invalid/evidence/1",
            "approval_timestamp": "2026-09-28T21:00:00Z",
            "raw_notes": "approved challenge",
        }
        base.update(changes)
        return base

    def post(self, payload, authenticated=True):
        headers = {"Authorization": f"Bearer {self.token}"} if authenticated else {}
        return self.client.post("/api/challenges/intake/approved", json=payload, headers=headers)

    def db(self):
        conn = sqlite3.connect(self.challenge_path)
        conn.row_factory = sqlite3.Row
        return conn

    def set_modes(self, *, award_mode="shadow", delivery_mode="dry_run", direct_mode="shadow"):
        with self.db() as conn:
            conn.execute(
                "UPDATE challenge_settings SET setting_value=? WHERE setting_key='award_mode'",
                (award_mode,),
            )
            conn.execute(
                "UPDATE challenge_settings SET setting_value=? WHERE setting_key='award_delivery_mode'",
                (delivery_mode,),
            )
            conn.execute(
                "UPDATE challenge_settings SET setting_value=? WHERE setting_key='direct_intake_mode'",
                (direct_mode,),
            )

    def member_rank_map(self):
        with sqlite3.connect(self.members_path) as conn:
            return list(conn.execute("SELECT member_id,rank_points FROM members ORDER BY member_id"))

    def publish_gauntlet_submission_config(self, *, mode, time_format="MM:SS.xx", min_party=1):
        with self.db() as conn:
            draft=create_draft(conn,"test")
            document=draft["config"]
            boss=next(item for item in document["bosses"] if item["boss_key"]=="gauntlet")
            boss.update({
                "submission_mode":mode,
                "supports_groups":mode != "solo",
                "min_party_size":min_party,
                "time_input_format":time_format,
            })
            saved=save_draft(conn,draft["draft_id"],document,"test",draft["revision"])
            checked=validate_draft(conn,draft["draft_id"],"test",saved["revision"])
            self.assertTrue(checked["valid"],checked["errors"])
            return publish_draft(
                conn,draft["draft_id"],"test",confirmed=True,
                expected_revision=saved["revision"],
            )["version_id"]

    def test_authenticated_intake_and_unauthenticated_rejection(self):
        denied = self.post(self.payload(), authenticated=False)
        self.assertEqual(denied.status_code, 401)
        accepted = self.post(self.payload())
        self.assertEqual(accepted.status_code, 201)
        self.assertTrue(accepted.json["ok"])
        self.assertEqual(accepted.json["reconciliation_state"], "DIRECT_WAITING_FOR_GOOGLE")
        with self.db() as conn:
            outcomes = dict(conn.execute("SELECT outcome,COUNT(*) FROM challenge_intake_request_audit GROUP BY outcome"))
            self.assertEqual(outcomes, {"accepted": 1, "auth_failure": 1})

    def test_submitter_cannot_be_the_approval_actor(self):
        before = self.member_rank_map()
        response = self.post(self.payload(
            approver_discord_id="111111111111111111",
        ))
        self.assertEqual(response.status_code, 422, response.json)
        self.assertEqual(
            response.json["error"],
            "submitter_cannot_approve_own_submission",
        )
        self.assertEqual(before, self.member_rank_map())
        with self.db() as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM challenge_submissions "
                    "WHERE source_system='discord_direct'"
                ).fetchone()[0],
                0,
            )

    def test_published_solo_and_time_format_are_enforced(self):
        version=self.publish_gauntlet_submission_config(mode="solo")
        base=self.payload(
            event_id="approval:solo-format",
            config_version_id=version,
            boss_key="gauntlet",
            raw_metric="07:45.00",
            earned_tier="Silver",
            normalized_metric={"value":465000,"unit":"milliseconds"},
        )
        accepted=self.post(base)
        self.assertEqual(accepted.status_code,201,accepted.json)
        invalid_format=self.post({
            **base,"event_id":"approval:wrong-format",
            "discord_message_id":"100000000000000099",
            "raw_metric":"00:07:45.00",
        })
        self.assertEqual(invalid_format.status_code,422,invalid_format.json)
        self.assertEqual(invalid_format.json["error"],"invalid_time_metric")
        party=self.post({
            **base,"event_id":"approval:solo-party",
            "discord_message_id":"100000000000000098",
            "party_members":[{"discord_id":"222222222222222222"}],
        })
        self.assertEqual(party.status_code,422,party.json)
        self.assertEqual(party.json["error"],"party_not_allowed")

    def test_published_group_minimum_is_enforced(self):
        version=self.publish_gauntlet_submission_config(
            mode="group",time_format="HH:MM:SS.xx",min_party=3,
        )
        response=self.post(self.payload(
            event_id="approval:group-too-small",
            config_version_id=version,boss_key="gauntlet",
            raw_metric="00:07:45.00",earned_tier="Silver",
            party_members=[{"discord_id":"222222222222222222"}],
        ))
        self.assertEqual(response.status_code,422,response.json)
        self.assertEqual(response.json["error"],"party_too_small")

    def test_shadow_intake_is_independent_of_award_mode(self):
        for index, (award_mode, delivery_mode) in enumerate((
            ("shadow", "dry_run"),
            ("live", "dry_run"),
            ("live", "live"),
        ), start=1):
            with self.subTest(award_mode=award_mode, delivery_mode=delivery_mode):
                self.set_modes(award_mode=award_mode, delivery_mode=delivery_mode)
                before = self.member_rank_map()
                response = self.post(self.payload(
                    event_id=f"approval:mode-{index}",
                    discord_message_id=f"10000000000000010{index}",
                    approval_timestamp=f"2026-09-28T21:0{index}:00Z",
                ))
                self.assertEqual(response.status_code, 201, response.json)
                self.assertEqual(before, self.member_rank_map())
                with self.db() as conn:
                    self.assertEqual(
                        conn.execute("SELECT COUNT(*) FROM challenge_rank_write_receipts").fetchone()[0],
                        0,
                    )
                health = self.client.get("/health")
                self.assertEqual(health.status_code, 200)
                self.assertTrue(health.json["ok"])

    def test_disabled_direct_intake_is_rejected_even_when_awards_are_live(self):
        self.set_modes(award_mode="live", delivery_mode="live", direct_mode="disabled")
        before = self.member_rank_map()
        response = self.post(self.payload(event_id="approval:direct-disabled"))
        self.assertEqual(response.status_code, 503, response.json)
        self.assertEqual(response.json["error"], "direct_intake_disabled")
        self.assertEqual(before, self.member_rank_map())
        with self.db() as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM challenge_submissions WHERE source_system='discord_direct'").fetchone()[0],
                0,
            )
        health = self.client.get("/health")
        self.assertFalse(health.json["ok"])

    def test_submitter_discord_without_rsn_resolves_canonically(self):
        payload = self.payload(event_id="approval:100000000000000010")
        payload.pop("submitted_rsn")
        response = self.post(payload)
        self.assertEqual(response.status_code, 201, response.json)
        with self.db() as conn:
            participant = conn.execute(
                """SELECT member_id,discord_id,rsn_snapshot,normalized_rsn_snapshot,
                          identity_resolution_method
                     FROM challenge_submission_participants
                    WHERE participant_role='submitter'"""
            ).fetchone()
            self.assertEqual(participant["member_id"], 1)
            self.assertEqual(participant["discord_id"], "111111111111111111")
            self.assertIsNone(participant["rsn_snapshot"])
            self.assertIsNone(participant["normalized_rsn_snapshot"])
            self.assertEqual(participant["identity_resolution_method"], "members.discord_id")

    def test_submitter_discord_with_source_rsn_is_accepted(self):
        response = self.post(self.payload(event_id="approval:100000000000000011"))
        self.assertEqual(response.status_code, 201, response.json)
        with self.db() as conn:
            participant = conn.execute(
                "SELECT member_id,rsn_snapshot FROM challenge_submission_participants WHERE participant_role='submitter'"
            ).fetchone()
            self.assertEqual((participant["member_id"], participant["rsn_snapshot"]), (1, "Primary One"))

    def test_known_discord_with_stale_alias_preserves_source_and_canonical_member(self):
        response = self.post(self.payload(
            event_id="approval:100000000000000012",
            submitted_rsn="Old Alias",
        ))
        self.assertEqual(response.status_code, 201, response.json)
        with self.db() as conn:
            participant = conn.execute(
                """SELECT member_id,rsn_snapshot,normalized_rsn_snapshot,identity_resolution_method
                     FROM challenge_submission_participants WHERE participant_role='submitter'"""
            ).fetchone()
            self.assertEqual(participant["member_id"], 1)
            self.assertEqual(participant["rsn_snapshot"], "Old Alias")
            self.assertEqual(participant["normalized_rsn_snapshot"], "oldalias")
            self.assertEqual(participant["identity_resolution_method"], "members.discord_id")

    def test_unknown_discord_without_rsn_is_rejected_without_submission(self):
        payload = self.payload(
            event_id="approval:100000000000000013",
            submitter_discord_id="333333333333333333",
        )
        payload.pop("submitted_rsn")
        response = self.post(payload)
        self.assertEqual(response.status_code, 422, response.json)
        self.assertEqual(response.json["error"], "submitter_identity_unresolved")
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM challenge_direct_intake_metadata").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM challenge_submissions WHERE source_system='discord_direct'").fetchone()[0], 0)

    def test_malformed_optional_rsn_and_missing_discord_are_rejected(self):
        malformed = self.post(self.payload(
            event_id="approval:100000000000000014",
            submitted_rsn="invalid!rsn",
        ))
        self.assertEqual(malformed.status_code, 422, malformed.json)
        self.assertEqual(malformed.json["error"], "invalid_submitted_rsn")
        missing_discord = self.post(self.payload(
            event_id="approval:100000000000000015",
            submitter_discord_id=None,
        ))
        self.assertEqual(missing_discord.status_code, 422, missing_discord.json)
        self.assertEqual(missing_discord.json["error"], "missing_submitter_discord_id")

    def test_unknown_fields_remain_rejected(self):
        response = self.post(self.payload(
            event_id="approval:100000000000000016",
            unexpected_field="not allowed",
        ))
        self.assertEqual(response.status_code, 422, response.json)
        self.assertEqual(response.json["error"], "unknown_fields")

    def test_stable_boss_key_and_config_version_are_accepted_and_recorded(self):
        payload=self.payload(event_id="approval:100000000000000099",boss_key="gauntlet",config_version_id=1)
        response=self.post(payload)
        self.assertEqual(response.status_code,201,response.json)
        with self.db() as conn:
            row=conn.execute("SELECT boss_key,config_version_id FROM challenge_submissions WHERE source_system='discord_direct'").fetchone()
            self.assertEqual((row["boss_key"],row["config_version_id"]),("gauntlet",1))

    def test_idempotent_replay_and_secondary_duplicate(self):
        first = self.post(self.payload())
        replay = self.post(self.payload())
        self.assertEqual(first.status_code, 201)
        self.assertEqual(replay.status_code, 200)
        self.assertTrue(replay.json["idempotent"])
        changed_event = self.payload(event_id="approval:100000000000000099")
        duplicate = self.post(changed_event)
        self.assertEqual(duplicate.status_code, 200)
        self.assertEqual(duplicate.json["duplicate_class"], "duplicate_direct")
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM challenge_direct_intake_metadata").fetchone()[0], 1)

    def test_same_member_boss_tier_distinct_attempt_is_preserved(self):
        self.assertEqual(self.post(self.payload()).status_code, 201)
        second = self.payload(
            event_id="approval:100000000000000004",
            discord_message_id="100000000000000004",
            approval_timestamp="2026-09-28T21:01:00Z",
        )
        self.assertEqual(self.post(second).status_code, 201)
        with self.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM challenge_direct_intake_metadata").fetchone()[0], 2)

    def test_alias_resolution_and_party_parsing(self):
        payload = self.payload(
            event_id="approval:100000000000000005",
            submitter_discord_id="333333333333333333",
            submitted_rsn="Old Alias",
            party_members=[{"discord_id":"222222222222222222","rsn":"Party Two"}],
        )
        response = self.post(payload)
        self.assertEqual(response.status_code, 201, response.json)
        with self.db() as conn:
            rows = conn.execute(
                "SELECT member_id,participant_role,identity_resolution_method FROM challenge_submission_participants ORDER BY participant_id"
            ).fetchall()
            self.assertEqual([(r[0],r[1]) for r in rows], [(1,"submitter"),(2,"party_member")])
            self.assertEqual(rows[0][2], "member_aliases.normalized_alias_rsn")

    def test_time_delve_completion_and_tier_evaluation(self):
        time_response = self.post(self.payload(
            event_id="approval:100000000000000006",
            discord_message_id="100000000000000006",
            approval_timestamp="2026-09-28T21:02:00Z",
            raw_metric="00:05:59.40",
        ))
        self.assertEqual(time_response.status_code, 201)
        self.assertEqual(time_response.json["earned_tier"], "ascendant")
        delve = self.post(self.payload(
            event_id="approval:100000000000000007",
            discord_message_id="100000000000000007",
            approval_timestamp="2026-09-28T21:03:00Z",
            boss="Delve",earned_tier="Gold",raw_metric="30 waves",
        ))
        self.assertEqual(delve.status_code, 201, delve.json)
        self.assertEqual(delve.json["earned_tier"], "ascendant")
        bronze = self.post(self.payload(
            event_id="approval:100000000000000008",
            discord_message_id="100000000000000008",
            approval_timestamp="2026-09-28T21:04:00Z",
            boss="Jad",earned_tier="Bronze",raw_metric=None,
        ))
        self.assertEqual(bronze.status_code, 201, bronze.json)
        self.assertEqual(bronze.json["metric"]["type"], "completion")

    def test_config_and_metric_validation(self):
        unknown = self.post(self.payload(boss="Not A Boss"))
        self.assertEqual(unknown.status_code, 422)
        bad_tier = self.post(self.payload(earned_tier="Diamond"))
        self.assertEqual(bad_tier.status_code, 422)
        too_slow = self.post(self.payload(earned_tier="Ascendant",raw_metric="00:06:10"))
        self.assertEqual(too_slow.status_code, 422)

    def _insert_google(
        self, direct_id: int, metric_value=385200, tier_rank=4, tier_key="platinum",
        *, source_id=101, member_id=1, discord_id="111111111111111111",
        subject_key="member:1", metric_type="time", metric_unit="milliseconds",
        metric_display="00:06:25.20", evidence_url="https://example.invalid/evidence/1",
        boss_key="gauntlet", boss_name="Gauntlet", supersedes=None,
        first_observed="2026-09-28T21:05:00+00:00",
        points=40,
    ):
        with self.db() as conn:
            config_id = conn.execute("SELECT config_version_id FROM challenge_config_versions WHERE status='active'").fetchone()[0]
            cur = conn.execute(
                """INSERT INTO challenge_submissions
                   (source_system,source_record_id,legacy_regular_submission_id,source_external_id,
                    source_snapshot_hash,ingest_fingerprint,config_version_id,boss_key,boss_display_name,
                    earned_tier_key,earned_tier_rank,source_submission_points,metric_type,metric_value,
                    metric_unit,metric_display,submitter_discord_id,evidence_url,source_submitted_at,
                    source_approved_at,first_observed_at,raw_payload_json,raw_payload_sha256,
                    record_state,parse_status,supersedes_submission_id)
                   VALUES('regular_submissions_sheet_sync',?,?,?, ?,?,?,?, ?,?,?,?, ?,?,?, ?,?,?,
                          '2026-09-28','2026-09-28',?,'{}',?,'approved','parsed',?)""",
                (
                    f"regular_submissions:{source_id}",source_id,f"google_sheet:test:{source_id}",
                    f"gsnap:{source_id}:{supersedes}",f"gfinger:{source_id}:{supersedes}",config_id,
                    boss_key,boss_name,tier_key,tier_rank,points,metric_type,metric_value,metric_unit,
                    metric_display,discord_id,evidence_url,first_observed,
                    f"graw:{source_id}:{supersedes}",supersedes,
                ),
            )
            google_id = cur.lastrowid
            conn.execute(
                """INSERT INTO challenge_submission_participants
                   (submission_id,subject_key,member_id,discord_id,rsn_snapshot,normalized_rsn_snapshot,
                    participant_role,identity_resolution_method)
                   VALUES(?,?,?,?,NULL,NULL,'submitter','test')""",
                (google_id,subject_key,member_id,discord_id),
            )
            conn.commit()
        return google_id

    def test_direct_google_match_and_disagreement(self):
        direct = self.post(self.payload()).json["submission_id"]
        self._insert_google(direct)
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            result = reconcile_direct_observations(conn)
            conn.commit()
            state = conn.execute("SELECT reconciliation_state FROM challenge_observation_reconciliation WHERE direct_submission_id=?",(direct,)).fetchone()[0]
        self.assertEqual(state,"MATCH")
        self.assertEqual(result["matched"],1)

        # A separate database/test event can be classified by tier disagreement.
        second_payload = self.payload(
            event_id="approval:100000000000000009",
            discord_message_id="100000000000000009",
            approval_timestamp="2026-09-29T21:00:00Z",
            raw_metric="00:05:59",
        )
        second = self.post(second_payload).json["submission_id"]
        self._insert_google(
            second,source_id=102,metric_value=359000,tier_rank=4,tier_key="platinum",
            metric_display="00:05:59",first_observed="2026-09-29T21:05:00+00:00",
        )
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            reconcile_direct_observations(conn)
            conn.commit()
            state=conn.execute("SELECT reconciliation_state FROM challenge_observation_reconciliation WHERE direct_submission_id=?",(second,)).fetchone()[0]
        self.assertEqual(state,"TIER_DIFFERENCE")

    def test_identical_metric_type_and_value_match(self):
        direct = self.post(self.payload(event_id="approval:metric-exact")).json["submission_id"]
        google = self._insert_google(direct,source_id=201)
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            reconcile_direct_observations(conn)
            conn.commit()
            summary = conn.execute(
                "SELECT reconciliation_state FROM challenge_observation_reconciliation WHERE direct_submission_id=?",
                (direct,),
            ).fetchone()[0]
            link = conn.execute(
                "SELECT match_class FROM challenge_observation_reconciliation_links WHERE direct_submission_id=? AND google_submission_id=?",
                (direct,google),
            ).fetchone()[0]
        self.assertEqual((summary,link),("MATCH","MATCH"))

    def test_bronze_time_and_completion_same_evidence_display_are_semantically_equal(self):
        direct = self.post(self.payload(
            event_id="approval:bronze-semantic",
            boss="Phosani's",earned_tier="Bronze",raw_metric="00:10:00.00",
        )).json["submission_id"]
        google = self._insert_google(
            direct,source_id=202,metric_value=1,tier_rank=1,tier_key="bronze",
            metric_type="completion",metric_unit="boolean",metric_display="00:10:00.00",
            boss_key="phosanis",boss_name="Phosani's",
        )
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            reconcile_direct_observations(conn)
            conn.commit()
            summary = conn.execute(
                "SELECT reconciliation_state,details_json FROM challenge_observation_reconciliation WHERE direct_submission_id=?",
                (direct,),
            ).fetchone()
            link = conn.execute(
                "SELECT match_class,details_json FROM challenge_observation_reconciliation_links WHERE google_submission_id=?",
                (google,),
            ).fetchone()
        self.assertEqual(summary[0],"MATCH")
        self.assertEqual(link[0],"MATCH")
        details=json.loads(link[1])
        self.assertEqual(details["metric_comparison"],"semantic_equivalent")
        self.assertEqual(details["metric_normalization_reason"],"bronze_time_vs_completion_same_evidence_and_display_time")
        self.assertIn("bronze_time_vs_completion_same_evidence_and_display_time",summary[1])

    def test_differing_real_times_are_value_difference(self):
        direct = self.post(self.payload(event_id="approval:metric-difference")).json["submission_id"]
        self._insert_google(
            direct,source_id=203,metric_value=390000,metric_display="00:06:30.00",
        )
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            reconcile_direct_observations(conn)
            conn.commit()
            state = conn.execute(
                "SELECT reconciliation_state FROM challenge_observation_reconciliation WHERE direct_submission_id=?",
                (direct,),
            ).fetchone()[0]
        self.assertEqual(state,"GOOGLE_VALUE_DIFFERENCE")

    def test_completion_and_time_without_corroborating_display_are_not_equal(self):
        direct = self.post(self.payload(
            event_id="approval:bronze-incompatible",
            boss="Phosani's",earned_tier="Bronze",raw_metric="00:10:00.00",
        )).json["submission_id"]
        self._insert_google(
            direct,source_id=204,metric_value=1,tier_rank=1,tier_key="bronze",
            metric_type="completion",metric_unit="boolean",metric_display="Completion",
            boss_key="phosanis",boss_name="Phosani's",
        )
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            reconcile_direct_observations(conn)
            conn.commit()
            row = conn.execute(
                "SELECT reconciliation_state,details_json FROM challenge_observation_reconciliation WHERE direct_submission_id=?",
                (direct,),
            ).fetchone()
        self.assertEqual(row[0],"GOOGLE_VALUE_DIFFERENCE")
        self.assertIn('"metric_comparison":"incompatible"',row[1])

    def test_superseded_google_only_is_excluded(self):
        old = self._insert_google(0,source_id=205)
        self._insert_google(0,source_id=205,supersedes=old)
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            result=reconcile_direct_observations(conn)
            conn.commit()
        self.assertEqual(result["google_only_new"],1)

    def test_corrected_google_observation_can_reconcile(self):
        direct = self.post(self.payload(event_id="approval:corrected-google")).json["submission_id"]
        old = self._insert_google(direct,source_id=206)
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            reconcile_direct_observations(conn)
            conn.commit()
        corrected = self._insert_google(direct,source_id=206,supersedes=old)
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            result=reconcile_direct_observations(conn)
            conn.commit()
            active_links=conn.execute(
                """SELECT l.google_submission_id FROM challenge_observation_reconciliation_links l
                   JOIN challenge_submissions s ON s.submission_id=l.google_submission_id
                  WHERE l.direct_submission_id=?
                    AND NOT EXISTS (SELECT 1 FROM challenge_submissions n WHERE n.supersedes_submission_id=s.submission_id)""",
                (direct,),
            ).fetchall()
            state=conn.execute(
                "SELECT reconciliation_state FROM challenge_observation_reconciliation WHERE direct_submission_id=?",
                (direct,),
            ).fetchone()[0]
        self.assertEqual([row[0] for row in active_links],[corrected])
        self.assertEqual(state,"MATCH")
        self.assertEqual(result["google_only_new"],0)

    def test_one_direct_links_multiple_google_participants_without_duplicate_achievements(self):
        direct = self.post(self.payload(
            event_id="approval:group-fanout",
            party_members=[{"discord_id":"222222222222222222","rsn":"Party Two"}],
        )).json["submission_id"]
        self._insert_google(direct,source_id=207)
        self._insert_google(
            direct,source_id=208,member_id=2,discord_id="222222222222222222",
            subject_key="member:2",
        )
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            reconcile_direct_observations(conn)
            config_id=conn.execute("SELECT config_version_id FROM challenge_config_versions WHERE status='active'").fetchone()[0]
            rebuild_derived(conn,config_id)
            conn.commit()
            link_count=conn.execute(
                "SELECT COUNT(*) FROM challenge_observation_reconciliation_links WHERE direct_submission_id=?",
                (direct,),
            ).fetchone()[0]
            summary=conn.execute(
                "SELECT reconciliation_state,details_json FROM challenge_observation_reconciliation WHERE direct_submission_id=?",
                (direct,),
            ).fetchone()
            duplicates=conn.execute(
                """SELECT COUNT(*) FROM (
                       SELECT subject_key,boss_key,tier_key,COUNT(*) n
                         FROM challenge_member_tier_achievements
                        GROUP BY subject_key,boss_key,tier_key HAVING n>1
                   )"""
            ).fetchone()[0]
        self.assertEqual(link_count,2)
        self.assertEqual(summary[0],"MATCH")
        self.assertIn('"participant_coverage_complete":true',summary[1])
        self.assertEqual(duplicates,0)

    def test_google_observation_does_not_duplicate_direct_award_candidates(self):
        direct = self.post(self.payload(event_id="approval:candidate-stability")).json["submission_id"]
        with self.db() as conn:
            before = [
                row.idempotency_key for row in boss_tier_candidates(conn)
                if row.subject_key == "member:1" and row.boss_key == "gauntlet"
            ]
        self._insert_google(direct, source_id=210)
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            reconcile_direct_observations(conn)
            config_id = conn.execute(
                "SELECT config_version_id FROM challenge_config_versions WHERE status='active'"
            ).fetchone()[0]
            rebuild_derived(conn, config_id)
            conn.commit()
            after = [
                row.idempotency_key for row in boss_tier_candidates(conn)
                if row.subject_key == "member:1" and row.boss_key == "gauntlet"
            ]
        self.assertEqual(before, after)
        self.assertEqual(len(after), len(set(after)))

    def test_post_19254_direct_then_google_is_one_candidate_and_no_rank_write(self):
        before_rank = self.member_rank_map()
        response = self.post(self.payload(
            event_id="approval:post-cutover-bronze",
            discord_message_id="100000000000000255",
            earned_tier="Bronze",
            raw_metric="00:10:00.00",
            normalized_metric={"value": 600000, "unit": "milliseconds"},
            approval_timestamp="2026-09-29T18:30:00Z",
        ))
        self.assertEqual(201, response.status_code, response.json)
        direct = response.json["submission_id"]
        with self.db() as conn:
            before = [row for row in boss_tier_candidates(conn)
                      if row.subject_key == "member:1" and row.boss_key == "gauntlet"
                      and row.classification == "eligible"]
        self.assertEqual(["bronze"], [row.tier_key for row in before])
        google = self._insert_google(
            direct, source_id=19255, tier_rank=1, tier_key="bronze", points=10,
            metric_type="completion", metric_value=1, metric_unit="boolean",
            metric_display="00:10:00.00", first_observed="2026-09-29T18:35:00+00:00",
        )
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            reconcile_direct_observations(conn)
            config_id = conn.execute(
                "SELECT config_version_id FROM challenge_config_versions WHERE status='active'"
            ).fetchone()[0]
            rebuild_derived(conn, config_id)
            conn.commit()
            link = conn.execute(
                """SELECT match_class FROM challenge_observation_reconciliation_links
                    WHERE direct_submission_id=? AND google_submission_id=?""", (direct, google)
            ).fetchone()
            after = [row for row in boss_tier_candidates(conn)
                     if row.subject_key == "member:1" and row.boss_key == "gauntlet"
                     and row.classification == "eligible"]
            receipts = conn.execute("SELECT COUNT(*) FROM challenge_rank_write_receipts").fetchone()[0]
        self.assertEqual("MATCH", link[0])
        self.assertEqual(["bronze"], [row.tier_key for row in after])
        self.assertEqual(before_rank, self.member_rank_map())
        self.assertEqual(0, receipts)

    def test_one_google_observation_cannot_link_multiple_direct_events(self):
        first = self.post(self.payload(event_id="approval:exclusive-google-1")).json["submission_id"]
        second = self.post(self.payload(
            event_id="approval:exclusive-google-2",
            discord_message_id="100000000000000004",
            approval_timestamp="2026-09-28T21:01:00Z",
        )).json["submission_id"]
        google = self._insert_google(first,source_id=209)
        with self.db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            reconcile_direct_observations(conn)
            conn.commit()
            links=conn.execute(
                "SELECT direct_submission_id FROM challenge_observation_reconciliation_links WHERE google_submission_id=?",
                (google,),
            ).fetchall()
            second_state=conn.execute(
                "SELECT reconciliation_state FROM challenge_observation_reconciliation WHERE direct_submission_id=?",
                (second,),
            ).fetchone()[0]
        self.assertEqual([row[0] for row in links],[first])
        self.assertEqual(second_state,"DIRECT_WAITING_FOR_GOOGLE")

    def test_zero_rank_writes_and_manual_award_guard(self):
        with sqlite3.connect(self.members_path) as members:
            before=list(members.execute("SELECT member_id,rank_points FROM members ORDER BY member_id"))
        response=self.post(self.payload(raw_metric="00:05:59"))
        self.assertEqual(response.status_code,201,response.json)
        with sqlite3.connect(self.members_path) as members:
            after=list(members.execute("SELECT member_id,rank_points FROM members ORDER BY member_id"))
        self.assertEqual(before,after)
        with self.db() as conn:
            award=conn.execute("SELECT award_id FROM challenge_tier_awards WHERE award_state='shadow_eligible' LIMIT 1").fetchone()
            self.assertIsNotNone(award)
            review=conn.execute("SELECT review_state FROM challenge_award_reviews WHERE award_id=?",(award[0],)).fetchone()
            self.assertEqual(review[0],"manual_review_required")
            conn.execute("UPDATE challenge_settings SET setting_value='live' WHERE setting_key='award_mode'")
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE challenge_tier_awards SET award_state='queued' WHERE award_id=?",(award[0],))


if __name__ == "__main__":
    unittest.main()
