"""Prepare the non-awardable RuneLite review interlock; dry-run by default."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import textwrap
from uuid import uuid4

from derived_review_support import (
    MIN_FREE_MARGIN, _apply_metadata, _backup_database, _backup_file,
    _backup_status, _capture_safe_metadata, _columns, _database_check, _digest, _fsync,
    _restore_database, _restore_file, _stage_bytes, _verify_metadata,
)

NON_AWARDABLE = ("runelite", "runelite_pilot")
INDEX = "uq_regular_submissions_external_id_nonnull"
AUDIT_TABLE = "runelite_review_transitions"
AUDIT_INDEX = "idx_runelite_review_transitions_submission"
TRIGGERS = {
    "runelite_no_approved_insert",
    "runelite_no_approved_update",
    "runelite_review_audit_no_update",
    "runelite_review_audit_no_delete",
}
ADMIN_MARKER = "RUNELITE_NON_AWARDABLE_SOURCES = {\"runelite\", \"runelite_pilot\"}"
UI_MARKER = "nonAwardableRuneLite"


def candidate_admin(original):
    if ADMIN_MARKER in original:
        raise ValueError("admin interlock is already present")
    role_anchor = 'VALID_ADMIN_ROLES = {"noc_super_admin", "event_admin"}\n'
    if original.count(role_anchor) != 1:
        raise ValueError("admin role anchor is missing or ambiguous")
    original = original.replace(role_anchor, role_anchor + "\n" + ADMIN_MARKER + "\n", 1)

    select_anchor = '''                    discord_id,
                    status
                FROM regular_submissions
                WHERE submission_id = ?
'''
    select_changed = '''                    discord_id,
                    status,
                    source_type
                FROM regular_submissions
                WHERE submission_id = ?
'''
    if original.count(select_anchor) != 1:
        raise ValueError("submission update select anchor is missing or ambiguous")
    original = original.replace(select_anchor, select_changed, 1)

    update_pattern = re.compile(r'(?m)^(?P<indent>\s*)member_id = existing\["member_id"\]\n\n'
                                r'(?P=indent)conn\.execute\("""\n'
                                r'(?P=indent)    UPDATE regular_submissions\n')
    update_changed = '''            member_id = existing["member_id"]
            source_type = str(existing["source_type"] or "regular").lower()
            previous_status = str(existing["status"] or "")
            reviewer = current_admin_name() or "member_management"

            if source_type in RUNELITE_NON_AWARDABLE_SOURCES and status == "approved":
                return error("RuneLite development and pilot submissions are non-awardable", 409)

            if source_type in RUNELITE_NON_AWARDABLE_SOURCES and status != previous_status:
                action = "deny" if status == "denied" else "status_transition"
                conn.execute("""
                    INSERT INTO runelite_review_transitions (
                        submission_id, previous_status, new_status, reviewer_identity, action
                    ) VALUES (?, ?, ?, ?, ?)
                """, (submission_id, previous_status, status, reviewer, action))

            conn.execute("""
                UPDATE regular_submissions
'''
    matches = list(update_pattern.finditer(original))
    if len(matches) != 1:
        raise ValueError("submission update mutation anchor is missing or ambiguous")
    indent = matches[0].group("indent")
    replacement = "\n".join(indent + line if line else "" for line in
                            textwrap.dedent(update_changed).strip().splitlines()) + "\n"
    original = update_pattern.sub(replacement, original, count=1)

    reviewer_anchor = '''                current_admin_name() or "member_management",
                submission_id,
'''
    if original.count(reviewer_anchor) != 1:
        raise ValueError("submission reviewer anchor is missing or ambiguous")
    original = original.replace(reviewer_anchor, '''                reviewer,
                submission_id,
''', 1)

    response_anchor = '''            "submissions": [dict(row) for row in rows],
            "total": int(total_row["total"] or 0),
'''
    response_changed = '''            "submissions": [dict(row) | {
                "non_awardable": str(row["source_type"] or "").lower()
                    in RUNELITE_NON_AWARDABLE_SOURCES,
                "source_label": (
                    "RuneLite development · non-awardable"
                    if str(row["source_type"] or "").lower() == "runelite"
                    else "RuneLite pilot · non-awardable"
                    if str(row["source_type"] or "").lower() == "runelite_pilot"
                    else str(row["source_type"] or "regular")
                ),
            } for row in rows],
            "total": int(total_row["total"] or 0),
'''
    if original.count(response_anchor) != 1:
        raise ValueError("regular-submission response anchor is missing or ambiguous")
    return original.replace(response_anchor, response_changed, 1)


def candidate_review(original):
    if UI_MARKER in original:
        raise ValueError("review interlock is already present")
    source_anchor = '''            const isRuneLite = String(sub.source_type || "").toLowerCase() === "runelite";
            const imgSrc = screenshot || (isRuneLite ? itemIcon : "");
'''
    source_changed = '''            const sourceType = String(sub.source_type || "").toLowerCase();
            const isRuneLite = sourceType === "runelite" || sourceType === "runelite_pilot";
            const nonAwardableRuneLite = Boolean(sub.non_awardable) || isRuneLite;
            const sourceLabel = String(sub.source_label || sub.source_type || "regular");
            const imgSrc = screenshot || (isRuneLite ? itemIcon : "");
'''
    if original.count(source_anchor) != 1:
        raise ValueError("review source anchor is missing or ambiguous")
    original = original.replace(source_anchor, source_changed, 1)

    footer_anchor = '''            const footerHTML = status === "pending"
                ? `<div class="action-buttons">
                     <button class="btn-approve" onclick="reviewRegularSubmission(${id}, 'approved', this)">✅ Approve</button>
                     <button class="btn-deny" onclick="reviewRegularSubmission(${id}, 'denied', this)">❌ Deny</button>
                   </div>`
                : buildReviewResult({
'''
    footer_changed = '''            const footerHTML = status === "pending"
                ? `<div class="action-buttons">
                     ${nonAwardableRuneLite
                        ? `<span class="review-result denied">Non-awardable · pending review</span>`
                        : `<button class="btn-approve" onclick="reviewRegularSubmission(${id}, 'approved', this)">✅ Approve</button>`}
                     <button class="btn-deny" onclick="reviewRegularSubmission(${id}, 'denied', this)">❌ Deny</button>
                   </div>`
                : buildReviewResult({
'''
    if original.count(footer_anchor) != 1:
        raise ValueError("review action anchor is missing or ambiguous")
    original = original.replace(footer_anchor, footer_changed, 1)

    status_anchor = '''                        <div class="meta-item">
                            <span class="meta-label">Status</span>
'''
    source_display = '''                        <div class="meta-item">
                            <span class="meta-label">Source</span>
                            <span class="meta-value">${escHtml(sourceLabel)}</span>
                        </div>
'''
    if original.count(status_anchor) != 1:
        raise ValueError("review status anchor is missing or ambiguous")
    return original.replace(status_anchor, source_display + status_anchor, 1)


def schema_state(database):
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        objects = {row[0]: row[1] for row in db.execute(
            "SELECT name,type FROM sqlite_master WHERE name IN (?,?,?,?,?,?,?)",
            (INDEX, AUDIT_TABLE, AUDIT_INDEX, *sorted(TRIGGERS)))}
        duplicates = db.execute("""
            SELECT COUNT(*) FROM (
                SELECT external_id FROM regular_submissions
                WHERE external_id IS NOT NULL
                GROUP BY external_id HAVING COUNT(*) > 1
            )
        """).fetchone()[0]
    expected = {INDEX: "index", AUDIT_TABLE: "table", AUDIT_INDEX: "index",
                **{name: "trigger" for name in TRIGGERS}}
    if not objects:
        return "not_applied", duplicates
    if objects == expected:
        return "already_applied", duplicates
    raise ValueError("ambiguous partially applied production-hardening schema")


def migration_sql():
    return f'''
        CREATE UNIQUE INDEX {INDEX}
        ON regular_submissions(external_id) WHERE external_id IS NOT NULL;
        CREATE TABLE {AUDIT_TABLE} (
            transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
            submission_id INTEGER NOT NULL,
            previous_status TEXT NOT NULL,
            new_status TEXT NOT NULL,
            reviewer_identity TEXT NOT NULL,
            transitioned_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            action TEXT NOT NULL
        );
        CREATE INDEX idx_runelite_review_transitions_submission
        ON {AUDIT_TABLE}(submission_id, transition_id);
        CREATE TRIGGER runelite_no_approved_insert
        BEFORE INSERT ON regular_submissions
        WHEN LOWER(COALESCE(NEW.source_type, '')) IN ('runelite','runelite_pilot')
         AND LOWER(COALESCE(NEW.status, '')) = 'approved'
        BEGIN SELECT RAISE(ABORT, 'RuneLite submissions are non-awardable'); END;
        CREATE TRIGGER runelite_no_approved_update
        BEFORE UPDATE OF status, source_type ON regular_submissions
        WHEN LOWER(COALESCE(NEW.source_type, '')) IN ('runelite','runelite_pilot')
         AND LOWER(COALESCE(NEW.status, '')) = 'approved'
        BEGIN SELECT RAISE(ABORT, 'RuneLite submissions are non-awardable'); END;
        CREATE TRIGGER runelite_review_audit_no_update
        BEFORE UPDATE ON {AUDIT_TABLE}
        BEGIN SELECT RAISE(ABORT, 'RuneLite review audit is append-only'); END;
        CREATE TRIGGER runelite_review_audit_no_delete
        BEFORE DELETE ON {AUDIT_TABLE}
        BEGIN SELECT RAISE(ABORT, 'RuneLite review audit is append-only'); END;
    '''


def migration_statements():
    script = migration_sql()
    # SQLite trigger bodies contain semicolons, so keep each trigger as one
    # explicit statement rather than splitting the script mechanically.
    prefix, triggers = script.split("        CREATE TRIGGER runelite_no_approved_insert", 1)
    statements = [value.strip() for value in prefix.split(";") if value.strip()]
    trigger_text = "CREATE TRIGGER runelite_no_approved_insert" + triggers
    for name in ("runelite_no_approved_update", "runelite_review_audit_no_update",
                 "runelite_review_audit_no_delete"):
        marker = "        CREATE TRIGGER " + name
        before, trigger_text = trigger_text.split(marker, 1)
        statements.append(before.strip())
        trigger_text = "CREATE TRIGGER " + name + trigger_text
    statements.append(trigger_text.strip())
    return statements


def migrate(database, fail=None):
    state, duplicates = schema_state(database)
    if state != "not_applied" or duplicates:
        raise ValueError("migration preflight state or duplicate external IDs is unsafe")
    with closing(sqlite3.connect(database)) as db:
        try:
            db.execute("BEGIN EXCLUSIVE")
            for position, statement in enumerate(migration_statements()):
                db.execute(statement)
                if fail: fail(f"schema_statement_{position}")
            db.commit()
        except BaseException:
            db.rollback()
            raise


def file_state(admin, review):
    admin_applied = ADMIN_MARKER in admin
    review_applied = UI_MARKER in review
    if not admin_applied and not review_applied:
        candidate_admin(admin); candidate_review(review)
        return "not_applied"
    if admin_applied and review_applied:
        return "already_applied"
    raise ValueError("ambiguous partially applied production-hardening files")


def _manifest(backup, files):
    value = {path.name: _digest(path) for path in files}
    target = backup / "SHA256SUMS.json"
    target.write_text(json.dumps(value, sort_keys=True) + "\n")
    _fsync(target)
    return value


def install(admin_app, review_page, database, backup_root, *, apply=False,
            admin_python="/srv/projects/api/venv-simon/bin/python", fail=None):
    admin, review, database, backup_root = map(Path, (admin_app, review_page, database, backup_root))
    for path in (admin, review, database):
        if not path.is_file() or path.is_symlink(): raise ValueError(f"unsafe target: {path}")
    if not backup_root.is_dir() or backup_root.is_symlink(): raise ValueError("unsafe backup root")
    metadata = {"admin": _capture_safe_metadata(admin), "review": _capture_safe_metadata(review),
                "database": _capture_safe_metadata(database),
                "backup_root": _capture_safe_metadata(backup_root, directory=True)}
    admin_text, review_text = admin.read_text(), review.read_text()
    files = file_state(admin_text, review_text)
    schema, duplicates = schema_state(database)
    if duplicates: raise ValueError(f"duplicate non-null external IDs exist: {duplicates}")
    if files != schema: raise ValueError("ambiguous partially applied production-hardening state")
    _database_check(database)
    report = {"state": files, "dry_run": not apply, "duplicate_external_ids": duplicates,
              "database_size": database.stat().st_size, "backup": None}
    if files == "already_applied" or not apply: return report
    required = report["database_size"] + max(MIN_FREE_MARGIN, report["database_size"] // 4)
    if shutil.disk_usage(backup_root).free < required: raise ValueError("insufficient backup space")
    backup = backup_root / ("runelite-review-hardening-" + uuid4().hex[:8])
    staged = []
    mutation = False
    backup.mkdir(mode=0o700)
    saved_admin, saved_review, saved_db = backup / admin.name, backup / review.name, backup / database.name
    try:
        _backup_status(backup, "incomplete", "Backup creation or verification has not completed")
        _backup_file(admin, saved_admin, metadata["admin"])
        _backup_file(review, saved_review, metadata["review"])
        _backup_database(database, saved_db, metadata["database"])
        _database_check(saved_db)
        manifest = _manifest(backup, (saved_admin, saved_review, saved_db))
        if any(_digest(backup / name) != digest for name, digest in manifest.items()):
            raise ValueError("backup checksum verification failed")
        _backup_status(backup, "verified", "All backups and checks completed")
        if fail: fail("after_backup")
        staged_admin = _stage_bytes(admin, candidate_admin(admin_text).encode(), metadata["admin"])
        staged_review = _stage_bytes(review, candidate_review(review_text).encode(), metadata["review"])
        staged.extend((staged_admin, staged_review))
        compile_script = "from pathlib import Path;import sys;compile(Path(sys.argv[1]).read_bytes(),sys.argv[1],'exec')"
        import subprocess
        subprocess.run([admin_python, "-B", "-c", compile_script, str(staged_admin)], check=True, timeout=30)
        if file_state(staged_admin.read_text(), staged_review.read_text()) != "already_applied":
            raise ValueError("staged file validation failed")
        if fail: fail("after_staging")
        for path, expected in ((admin, metadata["admin"]), (review, metadata["review"]),
                               (database, metadata["database"])):
            _verify_metadata(path, expected)
        mutation = True
        migrate(database, fail)
        if fail: fail("after_schema")
        os.replace(staged_admin, admin); staged.remove(staged_admin)
        if fail: fail("after_admin")
        os.replace(staged_review, review); staged.remove(staged_review)
        if fail: fail("after_review")
        _database_check(database)
        if schema_state(database)[0] != "already_applied" or file_state(admin.read_text(), review.read_text()) != "already_applied":
            raise ValueError("final verification failed")
        report.update(state="already_applied", dry_run=False, backup=str(backup))
        return report
    except BaseException as error:
        if not mutation:
            try: _backup_status(backup, "incomplete", f"Pre-mutation failure: {type(error).__name__}")
            except BaseException: pass
        if mutation:
            errors = []
            for restore in (lambda: _restore_file(saved_admin, admin, metadata["admin"]),
                            lambda: _restore_file(saved_review, review, metadata["review"]),
                            lambda: _restore_database(saved_db, database, metadata["database"])):
                try: restore()
                except BaseException as caught: errors.append(str(caught))
            if errors: raise RuntimeError("rollback failed: " + " | ".join(errors)) from error
            if file_state(admin.read_text(), review.read_text()) != "not_applied" or schema_state(database)[0] != "not_applied":
                raise RuntimeError("rollback verification failed") from error
        raise
    finally:
        for path in staged: path.unlink(missing_ok=True)


def rollback(backup, admin_app, review_page, database):
    backup, admin, review, database = map(Path, (backup, admin_app, review_page, database))
    status = json.loads((backup / "BACKUP_STATUS.json").read_text())
    manifest = json.loads((backup / "SHA256SUMS.json").read_text())
    if status.get("state") != "verified": raise ValueError("backup is not verified")
    saved_admin, saved_review, saved_db = backup / admin.name, backup / review.name, backup / database.name
    for saved in (saved_admin, saved_review, saved_db):
        if manifest.get(saved.name) != _digest(saved): raise ValueError("backup checksum mismatch")
    _database_check(saved_db)
    if file_state(admin.read_text(), review.read_text()) != "already_applied" or schema_state(database)[0] != "already_applied":
        raise ValueError("active state is not the expected applied state")
    metadata = {"admin": _capture_safe_metadata(saved_admin),
                "review": _capture_safe_metadata(saved_review),
                "database": _capture_safe_metadata(saved_db)}
    _restore_file(saved_admin, admin, metadata["admin"])
    _restore_file(saved_review, review, metadata["review"])
    _restore_database(saved_db, database, metadata["database"])
    if file_state(admin.read_text(), review.read_text()) != "not_applied" or schema_state(database)[0] != "not_applied":
        raise RuntimeError("rollback verification failed")
    return {"state": "not_applied", "restored_from": str(backup)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback-backup")
    parser.add_argument("--admin-app", default="/srv/projects/api/admin_app.py")
    parser.add_argument("--review-page", default="/srv/projects/website/regular-submissions-review.html")
    parser.add_argument("--database", default="/srv/projects/database/RegularSubmissions.db")
    parser.add_argument("--backup-dir", default="/etc/nocturne-plugin-backups")
    args = parser.parse_args()
    if args.apply and args.rollback_backup: raise SystemExit("choose --apply or --rollback-backup")
    result = (rollback(args.rollback_backup, args.admin_app, args.review_page, args.database)
              if args.rollback_backup else
              install(args.admin_app, args.review_page, args.database, args.backup_dir, apply=args.apply))
    print(json.dumps(result, sort_keys=True))
    if not args.apply: print("Dry run only; no files or database were changed.")


if __name__ == "__main__": main()
