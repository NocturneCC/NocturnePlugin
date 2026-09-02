from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

import production_review_hardening as phase1
from screenshot_lifecycle import EVIDENCE, LINKS, migrate
import screenshot_review_sync_support as support

NOW = datetime(2026, 9, 2, tzinfo=timezone.utc)
ADMIN_FIXTURE = '''from datetime import datetime, timedelta
import sqlite3
RUNELITE_NON_AWARDABLE_SOURCES = {"runelite", "runelite_pilot"}
REGULAR_SUBMISSIONS_DB = "db"
class App:
    def route(self,value): return lambda fn: fn
app=App()
def runelite_screenshot_evidence_state(conn, submission_id):
    return "available"
def update(conn, submission_id, status, previous_status, reviewer, member_id):
    try:
        with conn:
            if "runelite" in RUNELITE_NON_AWARDABLE_SOURCES and status == "approved":
                return "RuneLite development and pilot submissions are non-awardable"
            conn.execute("INSERT INTO runelite_review_transitions VALUES (?,?,?,?,?)",
                         (submission_id, previous_status, status, reviewer, "deny"))
            conn.execute("""
                UPDATE regular_submissions
                SET
                    status = ?
                WHERE submission_id = ?
            """, (
                status,
                submission_id,
            ))

            totals = conn.execute("""
                SELECT 1
            """)
            conn.commit()
    except Exception:
        raise

@app.route("/delete")
def nocturne_submission_delete(submission_id):
    try:
        with sqlite3.connect(REGULAR_SUBMISSIONS_DB) as conn:
            existing = {"member_id": 1}
            member_id = existing["member_id"]

            conn.execute("""
                DELETE FROM regular_submissions
                WHERE submission_id = ?
            """, (submission_id,))
            conn.commit()

        if member_id:
            return member_id
    except Exception:
        raise

@app.route("/next")
def next_route():
    pass
'''


class ScreenshotReviewSyncSupportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name); self.admin = root / "admin_app.py"
        self.admin.write_text(ADMIN_FIXTURE)
        self.db = root / "RegularSubmissions.db"; self.backups = root / "backups"; self.backups.mkdir()
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("CREATE TABLE regular_submissions(submission_id INTEGER PRIMARY KEY,external_id TEXT,source_type TEXT,status TEXT)")
        phase1.migrate(self.db); migrate(self.db, apply=True)

    def install(self, **kwargs):
        return support.install(self.admin,self.db,self.backups,admin_python=sys.executable,**kwargs)

    def test_candidate_covers_status_and_association_paths(self):
        changed=support.candidate_admin(self.admin.read_text())
        self.assertEqual(1,changed.count("if status != previous_status:"))
        self.assertEqual(1,changed.count("sync_runelite_screenshot_review_state(conn, [submission_id])"))
        self.assertEqual(1,changed.count("sync_runelite_screenshot_events(conn, screenshot_events)"))
        self.assertIn("RuneLite development and pilot submissions are non-awardable",changed)

    def test_upgrade_dry_run_apply_rollback_and_rerun(self):
        original=self.admin.read_bytes(); self.assertEqual("not_applied",self.install()["state"])
        result=self.install(apply=True); self.assertEqual("already_applied",result["state"])
        self.assertEqual("already_applied",self.install(apply=True)["state"])
        self.assertEqual("not_applied",support.rollback(result["backup"],self.admin)["state"])
        self.assertEqual(original,self.admin.read_bytes())
        self.assertTrue(support.rollback(result["backup"],self.admin)["already_restored"])

    def test_activation_failure_restores_admin(self):
        original=self.admin.read_bytes()
        with self.assertRaises(KeyboardInterrupt):
            self.install(apply=True,fail=lambda phase: (_ for _ in ()).throw(KeyboardInterrupt()) if phase=="after_activation" else None)
        self.assertEqual(original,self.admin.read_bytes())

    def test_lifecycle_failure_rolls_status_and_audit_back(self):
        namespace={"datetime":datetime,"timedelta":timedelta,"timezone":timezone}; exec(support.HELPER,namespace)
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("INSERT INTO regular_submissions VALUES(1,'event:1','runelite','pending')")
            db.execute(f"INSERT INTO {LINKS} VALUES('00000000-0000-4000-8000-000000000001',1)"); db.commit()
            with self.assertRaisesRegex(RuntimeError,"association is incomplete"):
                with db:
                    db.execute("INSERT INTO runelite_review_transitions(submission_id,previous_status,new_status,reviewer_identity,action) VALUES(1,'pending','denied','reviewer','deny')")
                    db.execute("UPDATE regular_submissions SET status='denied' WHERE submission_id=1")
                    namespace["sync_runelite_screenshot_review_state"](db,[1])
            self.assertEqual("pending",db.execute("SELECT status FROM regular_submissions WHERE submission_id=1").fetchone()[0])
            self.assertEqual(0,db.execute("SELECT COUNT(*) FROM runelite_review_transitions").fetchone()[0])

    def test_same_state_does_not_extend_deadline(self):
        namespace={"datetime":datetime,"timedelta":timedelta,"timezone":timezone}; exec(support.HELPER,namespace)
        event="00000000-0000-4000-8000-000000000002"; deadline=(NOW+timedelta(days=7)).isoformat()
        with closing(sqlite3.connect(self.db)) as db:
            db.execute("INSERT INTO regular_submissions VALUES(2,'event:2','runelite','denied')")
            db.execute(f"INSERT INTO {EVIDENCE}(event_uuid,created_at,review_state,purge_deadline,storage_state) VALUES(?,?,'denied',?,'available')",(event,NOW.isoformat(),deadline))
            db.execute(f"INSERT INTO {LINKS} VALUES(?,2)",(event,)); db.commit()
            namespace["sync_runelite_screenshot_review_state"](db,[2])
            self.assertEqual(deadline,db.execute(f"SELECT purge_deadline FROM {EVIDENCE}").fetchone()[0])


if __name__ == "__main__": unittest.main()
