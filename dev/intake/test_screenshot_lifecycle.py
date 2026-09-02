from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest

from screenshot_lifecycle import (AUDIT, EVIDENCE, LINKS, VERSION_TABLE, cleanup,
                                  evidence_state, migrate, migration_sql,
                                  require_compatible_schema, schema_state)

NOW = datetime(2026, 9, 2, tzinfo=timezone.utc)


class ScreenshotLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.db = self.root / "RegularSubmissions.db"
        self.images = self.root / "runelite-submission-images"; self.images.mkdir()
        with closing(sqlite3.connect(self.db)) as db:
            db.executescript("CREATE TABLE regular_submissions(submission_id INTEGER PRIMARY KEY,status TEXT);" + migration_sql())

    def add(self, event, statuses, state="available", deadline=None):
        name = event + ".jpg"
        (self.images / name).write_bytes(b"jpeg")
        with closing(sqlite3.connect(self.db)) as db:
            db.execute(f"INSERT INTO {EVIDENCE} VALUES(?,?,?,4,?,'pending',?,NULL,?)",
                       (event, name, "digest-" + event, NOW.isoformat(), deadline, state))
            for index, status in enumerate(statuses, 1):
                sid = int(event[-2:], 16) * 10 + index
                db.execute("INSERT INTO regular_submissions VALUES(?,?)", (sid, status))
                db.execute(f"INSERT INTO {LINKS} VALUES(?,?)", (event, sid))
            db.commit()
        return name

    def test_mixed_pending_is_never_purged_and_resolved_boundaries(self):
        pending = "00000000-0000-4000-8000-000000000011"
        denied = "00000000-0000-4000-8000-000000000012"
        approved = "00000000-0000-4000-8000-000000000013"
        self.add(pending, ["denied", "pending"])
        self.add(denied, ["denied"], deadline=(NOW - timedelta(seconds=1)).isoformat())
        self.add(approved, ["approved"], deadline=(NOW - timedelta(seconds=1)).isoformat())
        report = cleanup(self.db, self.images, now=NOW, apply=False)
        self.assertEqual({denied, approved}, {a[1] for a in report["actions"]})
        self.assertTrue((self.images / (pending + ".jpg")).exists())

    def test_shared_image_waits_for_every_row(self):
        event = "00000000-0000-4000-8000-000000000021"
        self.add(event, ["denied", "pending"])
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(("pending", None), evidence_state(db, event, NOW))

    def test_retention_periods_are_seven_and_thirty_days(self):
        denied = "00000000-0000-4000-8000-000000000022"
        approved = "00000000-0000-4000-8000-000000000023"
        self.add(denied, ["denied"]); self.add(approved, ["approved"])
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual((NOW + timedelta(days=7)).isoformat(), evidence_state(db, denied, NOW)[1])
            self.assertEqual((NOW + timedelta(days=30)).isoformat(), evidence_state(db, approved, NOW)[1])

    def test_cleanup_is_dry_run_idempotent_and_preserves_digest_audit(self):
        event = "00000000-0000-4000-8000-000000000031"
        name = self.add(event, ["denied"], deadline=(NOW - timedelta(days=8)).isoformat())
        cleanup(self.db, self.images, now=NOW, apply=False)
        self.assertTrue((self.images / name).exists())
        cleanup(self.db, self.images, now=NOW, apply=True)
        self.assertFalse((self.images / name).exists())
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(("digest-" + event, "expired"), db.execute(
                f"SELECT image_sha256,storage_state FROM {EVIDENCE}").fetchone())
            self.assertEqual(("digest-" + event, "purge"), db.execute(
                f"SELECT image_sha256,action FROM {AUDIT}").fetchone())
        self.assertEqual([], cleanup(self.db, self.images, now=NOW, apply=False)["actions"])

    def test_orphan_regular_file_only_and_symlink_is_ignored(self):
        orphan = self.images / "00000000-0000-4000-8000-000000000041.jpg"; orphan.write_bytes(b"x")
        target = self.root / "unrelated"; target.write_bytes(b"safe")
        (self.images / "evil.jpg").symlink_to(target)
        actions = cleanup(self.db, self.images, now=NOW, apply=False)["actions"]
        self.assertEqual([orphan.name], [a[2] for a in actions])
        self.assertEqual(b"safe", target.read_bytes())

    def test_missing_file_is_distinguished_and_interruption_restores_file(self):
        missing = "00000000-0000-4000-8000-000000000051"
        name = self.add(missing, ["denied"], deadline=(NOW - timedelta(days=1)).isoformat())
        (self.images / name).unlink()
        cleanup(self.db, self.images, now=NOW, apply=True)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual("missing", db.execute(f"SELECT storage_state FROM {EVIDENCE}").fetchone()[0])
        interrupted = "00000000-0000-4000-8000-000000000052"
        kept = self.add(interrupted, ["denied"], deadline=(NOW - timedelta(days=1)).isoformat())
        def fail(phase):
            if phase.startswith("after_purge"): raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            cleanup(self.db, self.images, now=NOW, apply=True, fail=fail)
        self.assertTrue((self.images / kept).exists())

    def test_schema_dry_run_success_and_transactional_interruption(self):
        other = self.root / "other.db"
        with closing(sqlite3.connect(other)) as db: db.execute("CREATE TABLE regular_submissions(submission_id INTEGER,status TEXT)")
        self.assertEqual("not_applied", migrate(other)["state"])
        with self.assertRaises(KeyboardInterrupt):
            migrate(other, apply=True, fail=lambda phase: (_ for _ in ()).throw(KeyboardInterrupt()))
        self.assertEqual("not_applied", schema_state(other))
        self.assertEqual("already_applied", migrate(other, apply=True)["state"])

    def test_schema_version_rejects_missing_partial_and_future(self):
        missing = self.root / "missing.db"
        with closing(sqlite3.connect(missing)) as db: db.execute("CREATE TABLE harmless(value INTEGER)")
        with self.assertRaisesRegex(ValueError, "missing or partial"): require_compatible_schema(missing)
        self.assertEqual(1, require_compatible_schema(self.db))
        with closing(sqlite3.connect(self.db)) as db:
            db.execute(f"UPDATE {VERSION_TABLE} SET schema_version=2"); db.commit()
        with self.assertRaisesRegex(ValueError, "version: 2"): require_compatible_schema(self.db)
        with closing(sqlite3.connect(self.db)) as db:
            db.execute(f"UPDATE {VERSION_TABLE} SET schema_version=1")
            db.execute("DROP TABLE runelite_screenshot_submissions"); db.commit()
        with self.assertRaisesRegex(ValueError, "missing or partial"): require_compatible_schema(self.db)

    def test_append_only_audit(self):
        with closing(sqlite3.connect(self.db)) as db:
            db.execute(f"INSERT INTO {AUDIT}(action,detail) VALUES('test','safe')"); db.commit()
            with self.assertRaises(sqlite3.IntegrityError): db.execute(f"DELETE FROM {AUDIT}")


if __name__ == "__main__": unittest.main()
