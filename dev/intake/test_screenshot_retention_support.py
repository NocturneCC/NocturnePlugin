from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest

from production_review_hardening import candidate_admin as phase_admin, candidate_review as phase_review
from test_production_review_hardening import ADMIN, REVIEW
import screenshot_retention_support as support


class ScreenshotRetentionSupportTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name); self.admin=root/"admin.py"; self.review=root/"review.html"
        self.db=root/"db.sqlite"; self.backups=root/"backups"; self.backups.mkdir()
        self.admin.write_text(phase_admin(ADMIN)); self.review.write_text(phase_review(REVIEW))
        with sqlite3.connect(self.db) as db:
            db.execute("CREATE TABLE regular_submissions(submission_id INTEGER PRIMARY KEY,status TEXT)")

    def install(self, **kwargs):
        return support.install(self.admin,self.review,self.db,self.backups,
                               admin_python=sys.executable,**kwargs)

    def test_candidates_label_all_storage_states(self):
        admin=support.candidate_admin(self.admin.read_text()); review=support.candidate_review(self.review.read_text())
        self.assertIn("runelite_screenshot_evidence_state",admin)
        for label in ("Evidence available","Evidence expired","Evidence missing",
                      "Evidence capture failed","Evidence storage failed"):
            self.assertIn(label,review)

    def test_dry_run_success_and_safe_rerun(self):
        self.assertEqual("not_applied",self.install()["state"])
        result=self.install(apply=True); self.assertEqual("already_applied",result["state"])
        self.assertTrue(Path(result["backup"]).is_dir())
        self.assertEqual("already_applied",self.install(apply=True)["state"])

    def test_post_mutation_interrupt_restores_every_target(self):
        original=(self.admin.read_bytes(),self.review.read_bytes())
        def fail(phase):
            if phase == "after_admin": raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt): self.install(apply=True,fail=fail)
        self.assertEqual(original,(self.admin.read_bytes(),self.review.read_bytes()))
        self.assertEqual("not_applied",support.schema_state(self.db))
        with sqlite3.connect(self.db) as db:
            self.assertEqual("ok",db.execute("PRAGMA quick_check").fetchone()[0])


if __name__ == "__main__": unittest.main()
