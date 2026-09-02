import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

import production_review_hardening as hardening


ADMIN = '''VALID_ADMIN_ROLES = {"noc_super_admin", "event_admin"}
def update_submission(conn, submission_id, status):
    existing = conn.execute("""
                SELECT
                    submission_id,
                    member_id,
                    rsn,
                    normalized_rsn,
                    discord_id,
                    status
                FROM regular_submissions
                WHERE submission_id = ?
            """, (submission_id,)).fetchone()
    member_id = existing["member_id"]

    conn.execute("""
        UPDATE regular_submissions
        SET status = ?
    """, (status,))
    values = (
                current_admin_name() or "member_management",
                submission_id,
    )
def search(rows, total_row):
    return ok({
            "submissions": [dict(row) for row in rows],
            "total": int(total_row["total"] or 0),
    })
'''

REVIEW = '''function buildCard(sub) {
            const isRuneLite = String(sub.source_type || "").toLowerCase() === "runelite";
            const imgSrc = screenshot || (isRuneLite ? itemIcon : "");
            const footerHTML = status === "pending"
                ? `<div class="action-buttons">
                     <button class="btn-approve" onclick="reviewRegularSubmission(${id}, 'approved', this)">✅ Approve</button>
                     <button class="btn-deny" onclick="reviewRegularSubmission(${id}, 'denied', this)">❌ Deny</button>
                   </div>`
                : buildReviewResult({
                        <div class="meta-item">
                            <span class="meta-label">Status</span>
}
'''


class Fixture:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.admin = self.root / "admin_app.py"
        self.review = self.root / "review.html"
        self.database = self.root / "RegularSubmissions.db"
        self.members = self.root / "Members.db"
        self.backups = self.root / "backups"
        self.admin.write_text(ADMIN)
        self.review.write_text(REVIEW)
        self.backups.mkdir()
        with sqlite3.connect(self.database) as db:
            db.executescript('''
                CREATE TABLE regular_submissions(
                    submission_id INTEGER PRIMARY KEY, external_id TEXT,
                    source_type TEXT, status TEXT, member_id INTEGER,
                    final_points INTEGER DEFAULT 0
                );
                CREATE TABLE rank_totals(member_id INTEGER PRIMARY KEY, total_points INTEGER);
                INSERT INTO rank_totals VALUES(2,77);
            ''')
        with sqlite3.connect(self.members) as db:
            db.execute("CREATE TABLE members(member_id INTEGER PRIMARY KEY, rank_points INTEGER)")
            db.execute("INSERT INTO members VALUES(2,77)")

    def close(self):
        self.temp.cleanup()

    def install(self, **kwargs):
        return hardening.install(self.admin, self.review, self.database, self.backups,
                                 admin_python=sys.executable, **kwargs)


class ProductionReviewHardeningTest(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.addCleanup(self.fixture.close)

    def test_candidate_labels_both_sources_and_removes_runelite_approve(self):
        admin = hardening.candidate_admin(ADMIN)
        review = hardening.candidate_review(REVIEW)
        self.assertIn('RUNELITE_NON_AWARDABLE_SOURCES = {"runelite", "runelite_pilot"}', admin)
        self.assertIn('"non_awardable"', admin)
        self.assertIn("RuneLite pilot · non-awardable", admin)
        self.assertIn("nonAwardableRuneLite", review)
        self.assertIn("Non-awardable · pending review", review)
        self.assertIn("btn-deny", review)

    def test_development_and_pilot_cannot_be_approved_at_database_layer(self):
        hardening.migrate(self.fixture.database)
        with sqlite3.connect(self.fixture.database) as db:
            for submission_id, source in ((1, "runelite"), (2, "runelite_pilot")):
                db.execute("INSERT INTO regular_submissions VALUES(?,?,?,'pending',2,5)",
                           (submission_id, f"event:{submission_id}", source))
            db.commit()
            before_rows = db.execute("SELECT * FROM regular_submissions ORDER BY submission_id").fetchall()
            before_totals = db.execute("SELECT * FROM rank_totals").fetchall()
            for submission_id in (1, 2):
                with self.assertRaisesRegex(sqlite3.IntegrityError, "non-awardable"):
                    db.execute("UPDATE regular_submissions SET status='approved' WHERE submission_id=?",
                               (submission_id,))
            self.assertEqual(before_rows, db.execute(
                "SELECT * FROM regular_submissions ORDER BY submission_id").fetchall())
            self.assertEqual(before_totals, db.execute("SELECT * FROM rank_totals").fetchall())
        with sqlite3.connect(self.fixture.members) as members:
            self.assertEqual(77, members.execute(
                "SELECT rank_points FROM members WHERE member_id=2").fetchone()[0])

    def test_denial_is_audited_and_append_only_without_points(self):
        hardening.migrate(self.fixture.database)
        with sqlite3.connect(self.fixture.database) as db:
            db.execute("INSERT INTO regular_submissions VALUES(1,'event:1','runelite','pending',2,5)")
            db.execute('''INSERT INTO runelite_review_transitions
                (submission_id,previous_status,new_status,reviewer_identity,action)
                VALUES(1,'pending','denied','reviewer','deny')''')
            db.execute("UPDATE regular_submissions SET status='denied' WHERE submission_id=1")
            db.commit()
            self.assertEqual("denied", db.execute(
                "SELECT status FROM regular_submissions WHERE submission_id=1").fetchone()[0])
            self.assertEqual((1, "pending", "denied", "reviewer", "deny"), db.execute('''
                SELECT submission_id,previous_status,new_status,reviewer_identity,action
                FROM runelite_review_transitions''').fetchone())
            self.assertEqual(77, db.execute("SELECT total_points FROM rank_totals").fetchone()[0])
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                db.execute("DELETE FROM runelite_review_transitions")

    def test_ordinary_approval_and_null_external_ids_remain_allowed(self):
        hardening.migrate(self.fixture.database)
        with sqlite3.connect(self.fixture.database) as db:
            db.execute("INSERT INTO regular_submissions VALUES(1,NULL,'regular','pending',2,5)")
            db.execute("INSERT INTO regular_submissions VALUES(2,NULL,'regular','pending',2,6)")
            db.execute("UPDATE regular_submissions SET status='approved' WHERE submission_id=1")
            db.commit()
            self.assertEqual("approved", db.execute(
                "SELECT status FROM regular_submissions WHERE submission_id=1").fetchone()[0])

    def test_duplicate_external_id_is_rejected_by_unique_partial_index(self):
        hardening.migrate(self.fixture.database)
        with sqlite3.connect(self.fixture.database) as db:
            db.execute("INSERT INTO regular_submissions VALUES(1,'same','regular','pending',2,0)")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO regular_submissions VALUES(2,'same','regular','pending',2,0)")

    def test_dry_run_changes_nothing(self):
        before = (self.fixture.admin.read_bytes(), self.fixture.review.read_bytes(),
                  self.fixture.database.read_bytes())
        result = self.fixture.install()
        self.assertTrue(result["dry_run"])
        self.assertEqual(before, (self.fixture.admin.read_bytes(), self.fixture.review.read_bytes(),
                                  self.fixture.database.read_bytes()))
        self.assertEqual([], list(self.fixture.backups.iterdir()))

    def test_success_and_independent_verified_rollback(self):
        result = self.fixture.install(apply=True)
        self.assertEqual("already_applied", result["state"])
        self.assertEqual("already_applied", hardening.schema_state(self.fixture.database)[0])
        restored = hardening.rollback(result["backup"], self.fixture.admin,
                                      self.fixture.review, self.fixture.database)
        self.assertEqual("not_applied", restored["state"])
        self.assertEqual(ADMIN, self.fixture.admin.read_text())
        self.assertEqual(REVIEW, self.fixture.review.read_text())

    def test_keyboard_interrupt_after_schema_rolls_back_every_target(self):
        def interrupt(phase):
            if phase == "after_schema":
                raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.fixture.install(apply=True, fail=interrupt)
        self.assertEqual("not_applied", hardening.schema_state(self.fixture.database)[0])
        self.assertEqual(ADMIN, self.fixture.admin.read_text())
        self.assertEqual(REVIEW, self.fixture.review.read_text())

    def test_interruptions_at_each_mutating_phase_restore_old_state(self):
        phases = [*(f"schema_statement_{value}" for value in range(7)),
                  "after_schema", "after_admin", "after_review"]
        for phase in phases:
            fixture = Fixture()
            try:
                def interrupt(current):
                    if current == phase:
                        raise KeyboardInterrupt()
                with self.subTest(phase=phase), self.assertRaises(KeyboardInterrupt):
                    fixture.install(apply=True, fail=interrupt)
                self.assertEqual("not_applied", hardening.schema_state(fixture.database)[0])
                self.assertEqual(ADMIN, fixture.admin.read_text())
                self.assertEqual(REVIEW, fixture.review.read_text())
            finally:
                fixture.close()

    def test_duplicate_preflight_refuses_before_backup(self):
        with sqlite3.connect(self.fixture.database) as db:
            db.execute("INSERT INTO regular_submissions VALUES(1,'same','regular','pending',2,0)")
            db.execute("INSERT INTO regular_submissions VALUES(2,'same','regular','pending',2,0)")
            db.commit()
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.fixture.install(apply=True)
        self.assertEqual([], list(self.fixture.backups.iterdir()))


if __name__ == "__main__":
    unittest.main()
