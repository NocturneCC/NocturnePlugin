import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from challenge_shadow_common import migrate_schema, parse_challenge, rw_connection, time_ms
from challenge_shadow_sync import calculate_current_progress


LEGACY_SCHEMA="""
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


class ShadowSchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/"Challenges.db"
        with sqlite3.connect(self.path) as conn:
            conn.executescript(LEGACY_SCHEMA)

    def tearDown(self):
        self.tmp.cleanup()

    def test_migration_config_and_guards(self):
        conn=rw_connection(self.path)
        config_id=migrate_schema(conn)
        self.assertEqual(conn.execute("SELECT setting_value FROM challenge_settings WHERE setting_key='award_mode'").fetchone()[0],"shadow")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM challenge_bosses WHERE is_active=1").fetchone()[0],15)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM challenge_tiers WHERE config_version_id=?",(config_id,)).fetchone()[0],75)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM challenge_system_tiers WHERE config_version_id=?",(config_id,)).fetchone()[0],5)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("""INSERT INTO challenge_tier_awards
              (subject_key,member_id,system_tier_key,eligibility_config_version_id,award_state,would_award_points,awarded_points,idempotency_key)
              VALUES('member:1',1,'bronze',?,'queued',75,0,'guard-test')""",(config_id,))
        conn.execute("""INSERT INTO challenge_submissions
          (source_system,source_record_id,legacy_regular_submission_id,source_snapshot_hash,ingest_fingerprint,
           config_version_id,first_observed_at,raw_payload_json,raw_payload_sha256,record_state,parse_status)
          VALUES('test','1',1,'a','b',?,'now','{}','c','approved','partial')""",(config_id,))
        conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE challenge_submissions SET record_state='retracted' WHERE submission_id=1")
        conn.close()

    def test_parser_time_and_party(self):
        parsed=parse_challenge("Gauntlet - Platinum","TIME SUBMISSION | 00:06:25.20 | target 0:06:30 | party_key 123 | approved by 456")
        self.assertEqual(parsed.boss_key,"gauntlet")
        self.assertEqual(parsed.tier_rank,4)
        self.assertEqual(parsed.metric_value,385200)
        self.assertEqual(parsed.party_key,"123")
        self.assertEqual(parsed.approver_discord_id,"456")

    def test_parser_legacy_four_component_centiseconds(self):
        cases = {
            "00:14:52:20": 892200,
            "0:5:14:40": 314400,
            "1:25:26:00": 5126000,
            "00:07:30:60": 450600,
            "00:29:43:80": 1783800,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(time_ms(raw), expected)
                parsed = parse_challenge("Gauntlet - Platinum", f"TIME SUBMISSION | {raw} | approved by 456")
                self.assertEqual(parsed.metric_value, expected)
                self.assertEqual(parsed.parse_status, "parsed")

    def test_parser_ambiguous_legacy_formats_remain_quarantined(self):
        for raw in ("", "00:07:50.ms"):
            with self.subTest(raw=raw):
                parsed = parse_challenge("Gauntlet - Platinum", f"TIME SUBMISSION | {raw} | approved by 456")
                self.assertIsNone(parsed.metric_value)
                self.assertIn(parsed.parse_status, ("partial",))

    def test_parser_legacy_completion(self):
        parsed=parse_challenge("Delve - Bronze","")
        self.assertEqual(parsed.metric_type,"completion")
        self.assertEqual(parsed.metric_value,1)
        self.assertEqual(parsed.parse_status,"parsed")

    def test_current_progress_excludes_inactive_bosses(self):
        catalog={"bosses":{
            "active": {"active":True,"tiers_by_rank":{2:{"progression_points":30}}},
            "archived": {"active":False,"tiers_by_rank":{5:{"progression_points":150}}},
        }}
        points,completed,ascendant=calculate_current_progress(
            catalog,{"active":2,"archived":5}
        )
        self.assertEqual(points,30)
        self.assertEqual(completed,1)
        self.assertEqual(ascendant,1)

    def test_ascendant_requirement_tracks_active_boss_catalog(self):
        catalog={"bosses":{
            f"boss_{index}": {"active":True,"tiers_by_rank":{1:{"progression_points":10}}}
            for index in range(1, 16)
        }}
        ranks={f"boss_{index}":1 for index in range(1, 16)}
        self.assertEqual(calculate_current_progress(catalog,ranks),(150,15,1))

        catalog["bosses"]["boss_16"]={
            "active":True,"tiers_by_rank":{1:{"progression_points":10}},
        }
        self.assertEqual(calculate_current_progress(catalog,ranks),(150,15,0))

        catalog["bosses"]["boss_16"]["active"]=False
        self.assertEqual(calculate_current_progress(catalog,ranks),(150,15,1))


if __name__=="__main__":
    unittest.main()
