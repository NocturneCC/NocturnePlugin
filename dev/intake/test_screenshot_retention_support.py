from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import unittest

from production_review_hardening import candidate_admin as phase_admin, candidate_review as phase_review
import production_review_hardening as phase1
from test_production_review_hardening import ADMIN, REVIEW
import screenshot_retention_support as support


class ScreenshotRetentionSupportTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        root=Path(self.temp.name); self.admin=root/"admin.py"; self.review=root/"review.html"
        self.db=root/"db.sqlite"; self.backups=root/"backups"; self.backups.mkdir()
        self.admin.write_text(phase_admin(ADMIN)); self.review.write_text(phase_review(REVIEW))
        with sqlite3.connect(self.db) as db:
            db.execute("CREATE TABLE regular_submissions(submission_id INTEGER PRIMARY KEY,external_id TEXT,source_type TEXT,status TEXT)")
        phase1.migrate(self.db)

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

    def test_independent_verified_rollback_is_idempotent_and_keeps_phase1(self):
        result=self.install(apply=True)
        restored=support.rollback(result["backup"],self.admin,self.review,self.db)
        self.assertFalse(restored["already_restored"])
        self.assertEqual("already_applied",phase1.schema_state(self.db)[0])
        self.assertEqual("not_applied",support.schema_state(self.db))
        self.assertTrue(support.rollback(result["backup"],self.admin,self.review,self.db)["already_restored"])

    def test_rollback_refuses_unverified_wrong_purpose_host_checksum_and_lineage(self):
        result=self.install(apply=True); backup=Path(result["backup"])
        status=backup/"BACKUP_STATUS.json"; manifest=backup/support.BACKUP_MANIFEST
        original_status=status.read_text(); original_manifest=manifest.read_text()
        cases=((status,{"state":"incomplete"},"unverified"),
               (manifest,{**json.loads(original_manifest),"purpose":"other"},"purpose"),
               (manifest,{**json.loads(original_manifest),"host_fingerprint":"0"*64},"host"),
               (manifest,{**json.loads(original_manifest),"schema_lineage":"other"},"lineage"))
        for path,value,message in cases:
            with self.subTest(message=message):
                path.write_text(json.dumps(value)+"\n")
                with self.assertRaisesRegex(ValueError,message): support.rollback(backup,self.admin,self.review,self.db)
                status.write_text(original_status); manifest.write_text(original_manifest)
        saved=backup/json.loads(original_manifest)["files"]["review"]["backup_name"]
        saved.write_bytes(saved.read_bytes()+b"tamper")
        with self.assertRaisesRegex(ValueError,"checksum"): support.rollback(backup,self.admin,self.review,self.db)

    def test_rollback_interruptions_restore_applied_state(self):
        phases=("after_database_restore","after_admin_restore","after_review_restore")
        for phase in phases:
            fixture=ScreenshotRetentionSupportTest(); fixture.setUp()
            try:
                result=fixture.install(apply=True)
                def interrupt(current):
                    if current==phase: raise KeyboardInterrupt()
                with self.subTest(phase=phase),self.assertRaises(KeyboardInterrupt):
                    support.rollback(result["backup"],fixture.admin,fixture.review,fixture.db,fail=interrupt)
                self.assertEqual("already_applied",support.schema_state(fixture.db))
                self.assertEqual("already_applied",support.file_state(fixture.admin.read_text(),fixture.review.read_text()))
            finally:
                fixture.temp.cleanup()

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
