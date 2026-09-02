"""Private RuneLite screenshot metadata migration and retention cleanup.

Dry-run is the default. Image bytes remain in the private filesystem, never SQLite.
"""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat
import uuid

EVIDENCE = "runelite_screenshot_evidence"
LINKS = "runelite_screenshot_submissions"
AUDIT = "runelite_screenshot_lifecycle_audit"
VERSION_TABLE = "runelite_screenshot_schema"
SCHEMA_VERSION = 1
OBJECTS = {VERSION_TABLE, EVIDENCE, LINKS, AUDIT, "runelite_screenshot_audit_no_update",
           "runelite_screenshot_audit_no_delete"}
DENIED_DAYS = 7
APPROVED_DAYS = 30


def migration_sql():
    return f"""
    CREATE TABLE {VERSION_TABLE}(
      singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema_version INTEGER NOT NULL
    );
    INSERT INTO {VERSION_TABLE}(singleton,schema_version) VALUES(1,{SCHEMA_VERSION});
    CREATE TABLE {EVIDENCE}(
      event_uuid TEXT PRIMARY KEY, image_filename TEXT UNIQUE, image_sha256 TEXT,
      image_bytes INTEGER, created_at TEXT NOT NULL, review_state TEXT NOT NULL,
      purge_deadline TEXT, deleted_at TEXT, storage_state TEXT NOT NULL,
      CHECK(storage_state IN ('available','expired','missing','capture_failed','storage_failed'))
    );
    CREATE TABLE {LINKS}(
      event_uuid TEXT NOT NULL, submission_id INTEGER NOT NULL UNIQUE,
      PRIMARY KEY(event_uuid,submission_id),
      FOREIGN KEY(event_uuid) REFERENCES {EVIDENCE}(event_uuid)
    );
    CREATE TABLE {AUDIT}(
      audit_id INTEGER PRIMARY KEY AUTOINCREMENT, event_uuid TEXT,
      image_filename TEXT, image_sha256 TEXT, action TEXT NOT NULL,
      occurred_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, detail TEXT NOT NULL
    );
    CREATE TRIGGER runelite_screenshot_audit_no_update BEFORE UPDATE ON {AUDIT}
      BEGIN SELECT RAISE(ABORT,'screenshot lifecycle audit is append-only'); END;
    CREATE TRIGGER runelite_screenshot_audit_no_delete BEFORE DELETE ON {AUDIT}
      BEGIN SELECT RAISE(ABORT,'screenshot lifecycle audit is append-only'); END;
    """


def schema_state(database):
    with closing(sqlite3.connect(f"file:{Path(database)}?mode=ro", uri=True)) as db:
        found = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE name IN (%s)" %
                 ",".join("?" * len(OBJECTS)), tuple(OBJECTS))}
    if not found:
        return "not_applied"
    if found == OBJECTS:
        return "already_applied"
    raise ValueError("ambiguous partially applied screenshot lifecycle schema")


def require_compatible_schema(database):
    try:
        state = schema_state(database)
    except ValueError as error:
        raise ValueError("screenshot lifecycle schema is missing or partial") from error
    if state != "already_applied":
        raise ValueError("screenshot lifecycle schema is missing or partial")
    expected_columns = {
        VERSION_TABLE: ("singleton", "schema_version"),
        EVIDENCE: ("event_uuid", "image_filename", "image_sha256", "image_bytes", "created_at",
                   "review_state", "purge_deadline", "deleted_at", "storage_state"),
        LINKS: ("event_uuid", "submission_id"),
        AUDIT: ("audit_id", "event_uuid", "image_filename", "image_sha256", "action",
                "occurred_at", "detail"),
    }
    with closing(sqlite3.connect(f"file:{Path(database)}?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        versions = db.execute(f"SELECT singleton,schema_version FROM {VERSION_TABLE}").fetchall()
        if versions != [(1, SCHEMA_VERSION)]:
            reported = versions[0][1] if len(versions) == 1 else "invalid"
            raise ValueError(f"incompatible screenshot lifecycle schema version: {reported}")
        for table, expected in expected_columns.items():
            actual = tuple(row[1] for row in db.execute(f'PRAGMA table_info("{table}")'))
            if actual != expected:
                raise ValueError(f"incompatible screenshot lifecycle table: {table}")
    return SCHEMA_VERSION


def migrate(database, apply=False, fail=None):
    state = schema_state(database)
    if state == "already_applied" or not apply:
        return {"state": state, "dry_run": not apply}
    with closing(sqlite3.connect(database)) as db:
        try:
            db.execute("BEGIN EXCLUSIVE")
            statements = [s.strip() for s in migration_sql().split(";") if s.strip()]
            # Trigger bodies contain one internal semicolon; join those fragments back.
            fixed = []
            for statement in statements:
                if statement.startswith("END") and fixed:
                    fixed[-1] += "; " + statement
                else:
                    fixed.append(statement)
            for position, statement in enumerate(fixed):
                db.execute(statement)
                if fail: fail(f"schema_{position}")
            db.commit()
        except BaseException:
            db.rollback()
            raise
    require_compatible_schema(database)
    return {"state": schema_state(database), "dry_run": False}


def evidence_state(db, event_uuid, now):
    statuses = [str(r[0]).lower() for r in db.execute(f"""
      SELECT r.status FROM regular_submissions r JOIN {LINKS} l
      ON l.submission_id=r.submission_id WHERE l.event_uuid=?""", (event_uuid,))]
    if not statuses or "pending" in statuses:
        return "pending", None
    days = APPROVED_DAYS if "approved" in statuses else DENIED_DAYS
    label = "approved" if "approved" in statuses else "denied"
    return label, (now + timedelta(days=days)).isoformat()


def refresh_review_state(db, event_uuid, now):
    state, deadline = evidence_state(db, event_uuid, now)
    db.execute(f"UPDATE {EVIDENCE} SET review_state=?,purge_deadline=? WHERE event_uuid=?",
               (state, deadline, event_uuid))
    return state, deadline


def _safe_image(directory, filename):
    if filename != Path(filename).name or not filename.endswith(".jpg"):
        raise ValueError("unsafe image filename")
    path = directory / filename
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise ValueError("unsafe image file")
    return path


def cleanup(database, image_dir, *, now=None, apply=False, fail=None):
    now = now or datetime.now(timezone.utc)
    directory = Path(image_dir)
    directory_metadata = directory.lstat()
    if not stat.S_ISDIR(directory_metadata.st_mode) or directory.is_symlink():
        raise ValueError("unsafe screenshot directory")
    lock_path = directory / ".retention.lock"
    actions = []
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with closing(sqlite3.connect(database)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            rows = db.execute(f"SELECT event_uuid,image_filename,image_sha256,purge_deadline,storage_state FROM {EVIDENCE}").fetchall()
            known = {r[1] for r in rows if r[1]}
            for event_id, filename, digest, deadline, storage in rows:
                state, _computed = evidence_state(db, event_id, now)
                if state != "pending" and not deadline:
                    days = APPROVED_DAYS if state == "approved" else DENIED_DAYS
                    actions.append(("schedule", event_id, filename, digest,
                                    (now + timedelta(days=days)).isoformat()))
                    continue
                due = (state != "pending" and deadline
                       and datetime.fromisoformat(deadline) <= now)
                if filename and storage == "available" and due:
                    try:
                        _safe_image(directory, filename)
                    except FileNotFoundError:
                        actions.append(("missing", event_id, filename, digest, state))
                    else:
                        actions.append(("purge", event_id, filename, digest, state))
            for entry in os.scandir(directory):
                if entry.name.startswith(".") or entry.name in known or not entry.name.endswith(".jpg"):
                    continue
                if entry.is_file(follow_symlinks=False):
                    actions.append(("orphan", None, entry.name, None, "untracked"))
            if not apply:
                return {"dry_run": True, "actions": actions}
            db.execute("BEGIN IMMEDIATE")
            moved = []
            try:
                for position, (action, event_id, filename, digest, detail) in enumerate(actions):
                    if action == "schedule":
                        db.execute(f"UPDATE {EVIDENCE} SET review_state=?,purge_deadline=? WHERE event_uuid=?",
                                   (evidence_state(db, event_id, now)[0], detail, event_id))
                        db.execute(f"INSERT INTO {AUDIT}(event_uuid,image_filename,image_sha256,action,detail) VALUES(?,?,?,?,?)",
                                   (event_id, filename, digest, action, detail))
                        continue
                    if action == "missing":
                        db.execute(f"UPDATE {EVIDENCE} SET storage_state='missing',review_state=? WHERE event_uuid=?",
                                   (detail, event_id))
                        db.execute(f"INSERT INTO {AUDIT}(event_uuid,image_filename,image_sha256,action,detail) VALUES(?,?,?,?,?)",
                                   (event_id, filename, digest, action, detail))
                        continue
                    path = _safe_image(directory, filename)
                    if fail: fail(f"before_{action}_{position}")
                    quarantine = directory / (".retention-" + uuid.uuid4().hex + ".tmp")
                    os.replace(path, quarantine)
                    moved.append((quarantine, path))
                    if fail: fail(f"after_{action}_{position}")
                    if event_id:
                        db.execute(f"UPDATE {EVIDENCE} SET storage_state='expired',deleted_at=?,review_state=?,purge_deadline=? WHERE event_uuid=?",
                                   (now.isoformat(), detail, now.isoformat(), event_id))
                    db.execute(f"INSERT INTO {AUDIT}(event_uuid,image_filename,image_sha256,action,detail) VALUES(?,?,?,?,?)",
                               (event_id, filename, digest, action, detail))
                db.commit()
            except BaseException:
                db.rollback()
                for quarantine, original in reversed(moved):
                    if quarantine.exists(): os.replace(quarantine, original)
                raise
            for quarantine, _original in moved:
                quarantine.unlink(missing_ok=True)
    return {"dry_run": False, "actions": actions}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="/srv/projects/database/RegularSubmissions.db")
    parser.add_argument("--image-dir", default="/srv/projects/database/runelite-submission-images")
    parser.add_argument("--apply-migration", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--apply-cleanup", action="store_true")
    args = parser.parse_args()
    result = migrate(args.database, apply=args.apply_migration)
    if args.cleanup or args.apply_cleanup:
        result["cleanup"] = cleanup(args.database, args.image_dir, apply=args.apply_cleanup)
    print(json.dumps(result, sort_keys=True))
    if not (args.apply_migration or args.apply_cleanup):
        print("Dry run only; no database or image was changed.")


if __name__ == "__main__":
    main()
