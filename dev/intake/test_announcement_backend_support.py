import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import announcement_backend_support as support
from announcements import SCHEMA_OBJECTS, schema_state


ADMIN = '''EVENT_SCHEDULE_DB = "event_schedule.db"
def current_admin_name():
    return "admin"
def _event_scheduler_allowed():
    return True
def require_auth(function):
    return function
class App:
    def register_blueprint(self, blueprint):
        pass
app = App()
'''


class AnnouncementBackendSupportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.admin = root / "admin_app.py"
        self.module = root / "nocturne_announcements.py"
        self.database = root / "event_schedule.db"
        self.snapshot = root / "announcement-public" / "announcements-v1.json"
        self.backups = root / "backups"
        self.backups.mkdir()
        self.admin.write_text(ADMIN)
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("CREATE TABLE events(event_id INTEGER PRIMARY KEY,title TEXT)")
            db.execute("INSERT INTO events(title) VALUES('preserved')")
            db.commit()
        self.metadata = {"uid": os.getuid(), "gid": os.getgid(), "mode": 0o640,
                         "acl": "user::rw-\ngroup::r--\nother::---\n"}
        self.directory_metadata = dict(self.metadata, mode=0o750,
                                       acl="user::rwx\ngroup::r-x\nother::---\n")

    def mocked(self):
        def capture(path, run=None, directory=False):
            return self.directory_metadata if directory else self.metadata
        return (patch.object(support, "_capture_safe_metadata", side_effect=capture),
                patch.object(support, "_apply_metadata"),
                patch.object(support, "_verify_metadata"))

    def install(self, apply=False, fail=None):
        first, second, third = self.mocked()
        with first, second as applied, third as verified:
            result = support.install(self.admin, self.module, self.database, self.backups,
                                     public_snapshot=self.snapshot,
                                     apply=apply, maintenance_confirmed=apply,
                                     stopped_services=support.REQUIRED_STOPPED_SERVICES if apply else (),
                                     service_active=lambda _service: False,
                                     run=lambda *args, **kwargs: None, fail=fail)
            return result, applied.call_args_list, verified.call_args_list

    def test_dry_run_reports_exact_plan_without_mutation(self):
        before_admin = self.admin.read_bytes()
        before_database = self.database.read_bytes()
        result, applied, verified = self.install()
        self.assertEqual(("not_applied", True, None),
                         (result["state"], result["dry_run"], result["backup"]))
        self.assertEqual(sorted(SCHEMA_OBJECTS), result["schema_objects"])
        self.assertEqual(before_admin, self.admin.read_bytes())
        self.assertEqual(before_database, self.database.read_bytes())
        self.assertFalse(self.module.exists())
        self.assertFalse(self.snapshot.exists())
        self.assertEqual([], applied)
        self.assertEqual([], verified)

    def test_apply_preserves_unrelated_data_is_idempotent_and_rollback_is_verified(self):
        result, applied, verified = self.install(True)
        self.assertEqual(("already_applied", False), (result["state"], result["dry_run"]))
        self.assertTrue(self.module.is_file())
        self.assertTrue(self.snapshot.is_file())
        self.assertEqual(0o644, self.snapshot.stat().st_mode & 0o777)
        self.assertIn(support.REGISTRATION, self.admin.read_text())
        self.assertEqual("already_applied", schema_state(self.database))
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual("preserved", db.execute("SELECT title FROM events").fetchone()[0])
        self.assertTrue(applied)
        self.assertGreaterEqual(len(verified), 3)
        manifest = json.loads((Path(result["backup"]) / "MANIFEST.json").read_text())
        self.assertEqual((support.PURPOSE, "verified"),
                         (manifest["purpose"], manifest["status"]))

        second, _, _ = self.install(True)
        self.assertEqual("already_applied", second["state"])
        with self.mocked()[0], self.mocked()[1], self.mocked()[2]:
            rolled = support.rollback(result["backup"], self.admin, self.module, self.database,
                                      self.snapshot,
                                      maintenance_confirmed=True,
                                      stopped_services=support.REQUIRED_STOPPED_SERVICES,
                                      service_active=lambda _service: False,
                                      run=lambda *args, **kwargs: None)
        self.assertFalse(rolled["already_restored"])
        self.assertEqual(ADMIN, self.admin.read_text())
        self.assertFalse(self.module.exists())
        self.assertFalse(self.snapshot.exists())
        self.assertEqual("not_applied", schema_state(self.database))
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual("preserved", db.execute("SELECT title FROM events").fetchone()[0])

    def test_failure_after_each_mutation_restores_files_database_and_metadata(self):
        for phase in ("after_schema", "after_module", "after_admin"):
            with self.subTest(phase=phase):
                fixture = AnnouncementBackendSupportTest(methodName="runTest")
                fixture.setUp()
                self.addCleanup(fixture.temp.cleanup)
                def fail(current):
                    if current == phase:
                        raise RuntimeError("simulated failure")
                with fixture.mocked()[0], fixture.mocked()[1], fixture.mocked()[2]:
                    with self.assertRaisesRegex(RuntimeError, "simulated failure"):
                        support.install(fixture.admin, fixture.module, fixture.database,
                                        fixture.backups, public_snapshot=fixture.snapshot,
                                        apply=True, maintenance_confirmed=True,
                                        stopped_services=support.REQUIRED_STOPPED_SERVICES,
                                        service_active=lambda _service: False,
                                        run=lambda *args, **kwargs: None, fail=fail)
                self.assertEqual(ADMIN, fixture.admin.read_text())
                self.assertFalse(fixture.module.exists())
                self.assertFalse(fixture.snapshot.exists())
                self.assertEqual("not_applied", schema_state(fixture.database))

    def test_refuses_partial_state_changed_source_and_wrong_backup(self):
        self.module.write_text("changed")
        with self.assertRaisesRegex(ValueError, "partially applied"):
            self.install()
        self.module.unlink()
        bad = self.backups / "bad"
        bad.mkdir()
        (bad / "MANIFEST.json").write_text('{"purpose":"wrong","status":"verified"}')
        with self.assertRaisesRegex(ValueError, "wrong or unverified"):
            support.rollback(bad, self.admin, self.module, self.database,
                             self.snapshot, maintenance_confirmed=True,
                             stopped_services=support.REQUIRED_STOPPED_SERVICES,
                             service_active=lambda _service: False)

    def test_apply_and_rollback_require_explicit_maintenance_confirmation(self):
        first, second, third = self.mocked()
        with first, second, third, self.assertRaisesRegex(ValueError, "maintenance-confirmed"):
            support.install(self.admin, self.module, self.database, self.backups, apply=True,
                            run=lambda *args, **kwargs: None)

    def test_active_event_writer_is_a_hard_interlock(self):
        first, second, third = self.mocked()
        with first, second, third, self.assertRaisesRegex(ValueError, "while active"):
            support.install(self.admin, self.module, self.database, self.backups,
                            public_snapshot=self.snapshot, apply=True,
                            maintenance_confirmed=True,
                            stopped_services=support.REQUIRED_STOPPED_SERVICES,
                            service_active=lambda service: service == "osrs-drops-admin.service",
                            run=lambda *args, **kwargs: None)


if __name__ == "__main__":
    unittest.main()
