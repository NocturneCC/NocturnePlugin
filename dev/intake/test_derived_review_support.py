from contextlib import closing
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

import derived_review_support as support


ADMIN = '''@app.route("/admin/api/nocturne/regular-submissions", methods=["GET"])
def route():
    query = """SELECT
                    item_price,
                    base_points,
                FROM regular_submissions"""
@app.route("/next")
def next_route():
    pass
'''
REVIEW = '''        function buildCard(sub) {
                        <div class="meta-item">
                            <span class="meta-label">Item Value</span>
                            <span class="meta-value">${formatGp(sub.item_price)}</span>
                        </div>
'''


class Space:
    def __init__(self, free):
        self.free = free


class Fixture:
    def __init__(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.admin, self.review = root / "admin_app.py", root / "review.html"
        self.database, self.backups, self.source = root / "submissions.sqlite3", root / "backups", root / "source.py"
        self.admin.write_text(ADMIN)
        self.review.write_text(REVIEW)
        self.source.write_text("# source\n")
        self.backups.mkdir()
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("CREATE TABLE regular_submissions(submission_id INTEGER PRIMARY KEY, item_name TEXT)")
            db.execute("INSERT INTO regular_submissions VALUES(1,'Existing')")
            db.commit()
        for path, mode in ((self.admin, 0o640), (self.review, 0o644),
                           (self.database, 0o660), (self.backups, 0o750)):
            os.chmod(path, mode)
        self.expected = {name: support._metadata(path) for name, path in (
            ("admin_app", self.admin), ("review_page", self.review),
            ("database", self.database), ("backup_dir", self.backups))}
        self.python = Path(sys.executable).resolve()

    def close(self):
        self.temp.cleanup()

    def install(self, **kwargs):
        return support.install(self.admin, self.review, self.database, self.backups,
                               admin_python=self.python, expected=self.expected,
                               source_files=[self.source], disk_usage=lambda _: Space(10 * 1024 ** 3), **kwargs)

    def state(self):
        return self.admin.read_text(), self.review.read_text(), support._columns(self.database)


class DerivedReviewSupportTest(unittest.TestCase):
    def test_patch_candidates_expose_and_label_derived_values(self):
        admin, review = support.candidate_admin(ADMIN), support.candidate_review_page(REVIEW)
        self.assertIn("finished_output_market_price_gp", admin)
        for text in ("Derived valuation", "finished_output_item_name", "valuation_rule_id", "derived_unit_price_gp"):
            self.assertIn(text, review)

    def test_dry_run_is_default_and_changes_nothing(self):
        fixture = Fixture(); self.addCleanup(fixture.close)
        before = fixture.state()
        result = fixture.install()
        self.assertEqual("not_applied", result["state"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(before, fixture.state())
        self.assertEqual([], list(fixture.backups.iterdir()))

    def test_clean_success_preserves_rows_metadata_and_is_idempotent(self):
        fixture = Fixture(); self.addCleanup(fixture.close)
        result = fixture.install(apply=True)
        self.assertEqual("already_applied", result["state"])
        self.assertTrue(Path(result["backup"]).is_dir())
        with closing(sqlite3.connect(fixture.database)) as db:
            self.assertEqual((1, "Existing"), db.execute("SELECT submission_id,item_name FROM regular_submissions").fetchone())
        self.assertEqual(support.COLUMNS, {name: support._columns(fixture.database)[name] for name in support.COLUMNS})
        for name, path in (("admin_app", fixture.admin), ("review_page", fixture.review), ("database", fixture.database)):
            self.assertEqual(fixture.expected[name], support._metadata(path))
        rerun = fixture.install(apply=True)
        self.assertEqual("already_applied", rerun["state"])
        self.assertIsNone(rerun["backup"])
        self.assertEqual(1, len(list(fixture.backups.iterdir())))

    def test_insufficient_space_refuses_before_backup(self):
        fixture = Fixture(); self.addCleanup(fixture.close)
        before = fixture.state()
        with self.assertRaisesRegex(ValueError, "insufficient backup space"):
            support.install(fixture.admin, fixture.review, fixture.database, fixture.backups, apply=True,
                            admin_python=fixture.python, expected=fixture.expected, source_files=[fixture.source],
                            disk_usage=lambda _: Space(1))
        self.assertEqual(before, fixture.state())
        self.assertEqual([], list(fixture.backups.iterdir()))

    def test_ambiguous_partial_state_refuses_before_backup(self):
        fixture = Fixture(); self.addCleanup(fixture.close)
        fixture.admin.write_text(support.candidate_admin(ADMIN))
        os.chmod(fixture.admin, fixture.expected["admin_app"]["mode"])
        with self.assertRaisesRegex(ValueError, "ambiguous partially applied"):
            fixture.install(apply=True)
        self.assertEqual([], list(fixture.backups.iterdir()))

    def test_all_failure_injection_points_leave_consistent_old_state(self):
        phases = ("after_backup", "after_admin_stage", "after_review_stage", "schema_application",
                  "first_replacement", "second_replacement")
        for phase in phases:
            with self.subTest(phase=phase):
                fixture = Fixture()
                try:
                    before = fixture.state()
                    def fail(value):
                        if value == phase:
                            raise OSError("injected " + phase)
                    with self.assertRaisesRegex(RuntimeError, "installation failed") as caught:
                        fixture.install(apply=True, fail=fail)
                    self.assertEqual(before, fixture.state())
                    expected = "restoration=not_required" if phase.startswith("after_") else "restoration=succeeded"
                    self.assertIn(expected, str(caught.exception))
                finally:
                    fixture.close()

    def test_automatic_rollback_allows_safe_rerun(self):
        fixture = Fixture(); self.addCleanup(fixture.close)
        def fail(phase):
            if phase == "first_replacement":
                raise OSError("injected")
        with self.assertRaisesRegex(RuntimeError, "restoration=succeeded"):
            fixture.install(apply=True, fail=fail)
        self.assertEqual("already_applied", fixture.install(apply=True)["state"])

    def test_staged_python_validation_failure_does_not_mutate(self):
        fixture = Fixture(); self.addCleanup(fixture.close)
        before = fixture.state()
        def run(command, **kwargs):
            if "-c" in command:
                raise subprocess.CalledProcessError(1, command)
            return support._run(command, **kwargs)
        with self.assertRaisesRegex(RuntimeError, "restoration=not_required"):
            support.install(fixture.admin, fixture.review, fixture.database, fixture.backups, apply=True,
                            admin_python=fixture.python, expected=fixture.expected, source_files=[fixture.source],
                            disk_usage=lambda _: Space(10 * 1024 ** 3), run=run)
        self.assertEqual(before, fixture.state())

    def test_wal_database_gets_consistent_verified_backup(self):
        fixture = Fixture(); self.addCleanup(fixture.close)
        with closing(sqlite3.connect(fixture.database)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("INSERT INTO regular_submissions VALUES(2,'WAL row')")
            db.commit()
        fixture.expected["database"] = support._metadata(fixture.database)
        result = fixture.install(apply=True)
        saved = Path(result["backup"]) / fixture.database.name
        self.assertEqual("wal", result["database_journal_mode"])
        with closing(sqlite3.connect(saved)) as db:
            self.assertEqual("ok", db.execute("PRAGMA quick_check").fetchone()[0])
            self.assertEqual(2, db.execute("SELECT COUNT(*) FROM regular_submissions").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
