"""Install private screenshot lifecycle schema/review support; dry-run by default."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
from uuid import uuid4

from derived_review_support import (MIN_FREE_MARGIN, _backup_database, _backup_file,
    _backup_status, _capture_safe_metadata, _database_check, _digest, _fsync, _restore_database,
    _restore_file, _stage_bytes, _verify_metadata)
import production_review_hardening as phase1
from screenshot_lifecycle import migrate, schema_state

ADMIN_MARKER = "def runelite_screenshot_evidence_state(conn, submission_id):"
UI_MARKER = "screenshotEvidenceLabel"
BACKUP_PURPOSE = "nocturne-screenshot-retention-v1"
BACKUP_MANIFEST = "RETENTION_BACKUP_MANIFEST.json"


def _host_fingerprint():
    identity = Path("/etc/machine-id")
    value = identity.read_bytes() if identity.is_file() else platform.node().encode()
    return hashlib.sha256(value).hexdigest()


def _phase1_state(admin, review, database):
    files = phase1.file_state(admin, review)
    schema, duplicates = phase1.schema_state(database)
    if duplicates:
        raise ValueError("Phase 1 lineage has duplicate external IDs")
    if files != "already_applied" or schema != "already_applied":
        raise ValueError("expected fully applied Phase 1 lineage")


def _write_manifest(backup, saved, targets, metadata):
    value = {"purpose": BACKUP_PURPOSE, "host_fingerprint": _host_fingerprint(),
             "schema_lineage": "phase1_applied_retention_not_applied",
             "files": {key: {"backup_name": source.name, "target_name": targets[key].name,
                               "sha256": _digest(source), "metadata": metadata[key]}
                       for key, source in saved.items()}}
    target = backup / BACKUP_MANIFEST
    target.write_text(json.dumps(value, sort_keys=True) + "\n")
    _fsync(target)
    return value


def _verified_backup(backup, targets, *, require_status=True):
    backup = Path(backup)
    if not backup.is_dir() or backup.is_symlink():
        raise ValueError("rollback backup must be an exact regular directory")
    status_path, manifest_path = backup / "BACKUP_STATUS.json", backup / BACKUP_MANIFEST
    if not status_path.is_file() or status_path.is_symlink() or not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("backup is incomplete or unverified")
    status, manifest = json.loads(status_path.read_text()), json.loads(manifest_path.read_text())
    if require_status and status.get("state") != "verified": raise ValueError("backup is incomplete or unverified")
    if manifest.get("purpose") != BACKUP_PURPOSE: raise ValueError("backup has the wrong purpose")
    if manifest.get("host_fingerprint") != _host_fingerprint(): raise ValueError("backup belongs to a different host")
    if manifest.get("schema_lineage") != "phase1_applied_retention_not_applied": raise ValueError("backup has mismatched schema lineage")
    if set(manifest.get("files", {})) != {"admin", "review", "database"}: raise ValueError("backup manifest target set is invalid")
    saved = {}
    for key in ("admin", "review", "database"):
        entry = manifest["files"][key]
        if entry.get("target_name") != targets[key].name: raise ValueError("backup target names do not match")
        source = backup / str(entry.get("backup_name", ""))
        if source.parent != backup or not source.is_file() or source.is_symlink(): raise ValueError("backup file is missing or unsafe")
        if _digest(source) != entry.get("sha256"): raise ValueError("backup checksum mismatch")
        if _capture_safe_metadata(source) != entry.get("metadata"): raise ValueError("backup metadata mismatch")
        saved[key] = source
    _database_check(saved["database"])
    _phase1_state(saved["admin"].read_text(), saved["review"].read_text(), saved["database"])
    if file_state(saved["admin"].read_text(), saved["review"].read_text()) != "not_applied" or schema_state(saved["database"]) != "not_applied":
        raise ValueError("backup is not from the expected pre-retention state")
    return manifest, saved


def candidate_admin(text):
    if ADMIN_MARKER in text: raise ValueError("screenshot lifecycle admin support already present")
    anchor = 'RUNELITE_NON_AWARDABLE_SOURCES = {"runelite", "runelite_pilot"}\n'
    if text.count(anchor) != 1: raise ValueError("Phase 1 admin marker missing or ambiguous")
    helper = '''

def runelite_screenshot_evidence_state(conn, submission_id):
    row = conn.execute("""
        SELECT e.storage_state FROM runelite_screenshot_evidence e
        JOIN runelite_screenshot_submissions l ON l.event_uuid=e.event_uuid
        WHERE l.submission_id=?
    """, (submission_id,)).fetchone()
    return str(row["storage_state"] if row else "capture_failed")
'''
    text = text.replace(anchor, anchor + helper, 1)
    response = '''            "submissions": [dict(row) | {
                "non_awardable":'''
    changed = '''            "submissions": [dict(row) | {
                "screenshot_evidence_state": runelite_screenshot_evidence_state(conn, row["submission_id"]),
                "non_awardable":'''
    if text.count(response) != 1: raise ValueError("Phase 1 response marker missing or ambiguous")
    return text.replace(response, changed, 1)


def candidate_review(text):
    if UI_MARKER in text: raise ValueError("screenshot lifecycle review support already present")
    anchor = '            const imgSrc = screenshot || (isRuneLite ? itemIcon : "");\n'
    if text.count(anchor) != 1: raise ValueError("Phase 1 review marker missing or ambiguous")
    changed = anchor + '''            const screenshotEvidenceLabel = ({
                available: "Evidence available", expired: "Evidence expired",
                missing: "Evidence missing", capture_failed: "Evidence capture failed",
                storage_failed: "Evidence storage failed"
            })[String(sub.screenshot_evidence_state || "")] || "Evidence unavailable";
'''
    status_anchor = '''                        <div class="meta-item">
                            <span class="meta-label">Source</span>
'''
    display = '''                        ${isRuneLite ? `<div class="meta-item">
                            <span class="meta-label">Screenshot evidence</span>
                            <span class="meta-value">${escHtml(screenshotEvidenceLabel)}</span>
                        </div>` : ""}
'''
    text = text.replace(anchor, changed, 1)
    if text.count(status_anchor) != 1: raise ValueError("review source display marker missing or ambiguous")
    return text.replace(status_anchor, display + status_anchor, 1)


def file_state(admin, review):
    values = (ADMIN_MARKER in admin, UI_MARKER in review)
    if values == (False, False): candidate_admin(admin); candidate_review(review); return "not_applied"
    if values == (True, True): return "already_applied"
    raise ValueError("ambiguous partially applied screenshot lifecycle files")


def install(admin_app, review_page, database, backup_root, *, apply=False,
            admin_python="/srv/projects/api/venv-simon/bin/python", fail=None):
    admin, review, database, backup_root = map(Path, (admin_app, review_page, database, backup_root))
    for path in (admin, review, database):
        if not path.is_file() or path.is_symlink(): raise ValueError(f"unsafe target: {path}")
    if not backup_root.is_dir() or backup_root.is_symlink(): raise ValueError("unsafe backup root")
    metadata = {"admin": _capture_safe_metadata(admin), "review": _capture_safe_metadata(review),
                "database": _capture_safe_metadata(database)}
    texts = (admin.read_text(), review.read_text())
    _phase1_state(texts[0], texts[1], database)
    files, schema = file_state(*texts), schema_state(database)
    if files != schema: raise ValueError("ambiguous partially applied screenshot lifecycle state")
    _database_check(database)
    report = {"state": files, "dry_run": not apply, "database_size": database.stat().st_size}
    if files == "already_applied" or not apply: return report
    required = report["database_size"] + max(MIN_FREE_MARGIN, report["database_size"] // 4)
    if shutil.disk_usage(backup_root).free < required: raise ValueError("insufficient backup space")
    backup = backup_root / ("screenshot-retention-" + uuid4().hex[:8]); backup.mkdir(mode=0o700)
    targets = {"admin": admin, "review": review, "database": database}
    saved = {key: backup / path.name for key, path in targets.items()}
    staged = []; mutation = False
    try:
        _backup_status(backup, "incomplete", "Backup creation or verification incomplete")
        _backup_file(admin, saved["admin"], metadata["admin"]); _backup_file(review, saved["review"], metadata["review"])
        _backup_database(database, saved["database"], metadata["database"]); _database_check(saved["database"])
        _write_manifest(backup, saved, targets, metadata)
        _verified_backup(backup, targets, require_status=False)
        _backup_status(backup, "verified", "Backups verified")
        _verified_backup(backup, targets)
        if fail: fail("after_backup")
        staged_admin = _stage_bytes(admin, candidate_admin(texts[0]).encode(), metadata["admin"])
        staged_review = _stage_bytes(review, candidate_review(texts[1]).encode(), metadata["review"])
        staged += [staged_admin, staged_review]
        subprocess.run([admin_python, "-B", "-m", "py_compile", str(staged_admin)], check=True, timeout=30)
        if file_state(staged_admin.read_text(), staged_review.read_text()) != "already_applied": raise ValueError("staged validation failed")
        for path, key in ((admin,"admin"),(review,"review"),(database,"database")): _verify_metadata(path, metadata[key])
        mutation = True; migrate(database, apply=True, fail=fail)
        if fail: fail("after_schema")
        os.replace(staged_admin, admin); staged.remove(staged_admin)
        if fail: fail("after_admin")
        os.replace(staged_review, review); staged.remove(staged_review)
        if fail: fail("after_review")
        _database_check(database)
        if file_state(admin.read_text(), review.read_text()) != "already_applied" or schema_state(database) != "already_applied": raise ValueError("final verification failed")
        return {**report, "state": "already_applied", "dry_run": False, "backup": str(backup)}
    except BaseException as error:
        if mutation:
            errors=[]
            for fn in (lambda:_restore_file(saved["admin"],admin,metadata["admin"]), lambda:_restore_file(saved["review"],review,metadata["review"]), lambda:_restore_database(saved["database"],database,metadata["database"])):
                try: fn()
                except BaseException as caught: errors.append(str(caught))
            if errors: raise RuntimeError("rollback failed: " + " | ".join(errors)) from error
            if file_state(admin.read_text(), review.read_text()) != "not_applied" or schema_state(database) != "not_applied": raise RuntimeError("rollback verification failed") from error
        raise
    finally:
        for path in staged: path.unlink(missing_ok=True)


def rollback(backup, admin_app, review_page, database, *, fail=None):
    targets = {"admin": Path(admin_app), "review": Path(review_page), "database": Path(database)}
    for path in targets.values():
        if not path.is_file() or path.is_symlink(): raise ValueError(f"unsafe active target: {path}")
    manifest, saved = _verified_backup(backup, targets)
    metadata = {key: manifest["files"][key]["metadata"] for key in targets}
    active_files = file_state(targets["admin"].read_text(), targets["review"].read_text())
    active_schema = schema_state(targets["database"])
    if active_files == "not_applied" and active_schema == "not_applied":
        _phase1_state(targets["admin"].read_text(), targets["review"].read_text(), targets["database"])
        if any(_digest(targets[key]) != manifest["files"][key]["sha256"] for key in targets):
            raise ValueError("ambiguous pre-retention state does not match this backup")
        return {"state": "not_applied", "already_restored": True, "restored_from": str(backup)}
    if active_files != "already_applied" or active_schema != "already_applied":
        raise ValueError("ambiguous partial active retention state")
    _phase1_state(targets["admin"].read_text(), targets["review"].read_text(), targets["database"])
    current_metadata = {key: _capture_safe_metadata(path) for key, path in targets.items()}
    guard_root = Path(backup) / ("rollback-guard-" + uuid4().hex[:8])
    guard_root.mkdir(mode=0o700)
    guard = {key: guard_root / path.name for key, path in targets.items()}
    mutation = False
    try:
        _backup_file(targets["admin"], guard["admin"], current_metadata["admin"])
        _backup_file(targets["review"], guard["review"], current_metadata["review"])
        _backup_database(targets["database"], guard["database"], current_metadata["database"])
        _database_check(guard["database"])
        if fail: fail("after_rollback_guard")
        for key, path in targets.items(): _verify_metadata(path, current_metadata[key])
        mutation = True
        _restore_file(saved["database"], targets["database"], metadata["database"])
        _database_check(targets["database"])
        if fail: fail("after_database_restore")
        _restore_file(saved["admin"], targets["admin"], metadata["admin"])
        if fail: fail("after_admin_restore")
        _restore_file(saved["review"], targets["review"], metadata["review"])
        if fail: fail("after_review_restore")
        _database_check(targets["database"])
        _phase1_state(targets["admin"].read_text(), targets["review"].read_text(), targets["database"])
        if file_state(targets["admin"].read_text(), targets["review"].read_text()) != "not_applied" or schema_state(targets["database"]) != "not_applied":
            raise RuntimeError("rollback final state verification failed")
        for key, path in targets.items():
            if _digest(path) != manifest["files"][key]["sha256"]: raise RuntimeError("rollback hash verification failed")
            _verify_metadata(path, metadata[key])
        return {"state": "not_applied", "already_restored": False, "restored_from": str(backup)}
    except BaseException as error:
        if mutation:
            errors = []
            for key, restore in (("database", _restore_file), ("admin", _restore_file), ("review", _restore_file)):
                try: restore(guard[key], targets[key], current_metadata[key])
                except BaseException as caught: errors.append(f"{key}: {caught}")
            if errors: raise RuntimeError("retention rollback failed and applied-state restoration failed: " + " | ".join(errors)) from error
            if file_state(targets["admin"].read_text(), targets["review"].read_text()) != "already_applied" or schema_state(targets["database"]) != "already_applied":
                raise RuntimeError("retention rollback failed; applied-state restoration verification failed") from error
        raise


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument("--apply",action="store_true"); p.add_argument("--rollback-backup")
    p.add_argument("--admin-app",default="/srv/projects/api/admin_app.py"); p.add_argument("--review-page",default="/srv/projects/website/regular-submissions-review.html")
    p.add_argument("--database",default="/srv/projects/database/RegularSubmissions.db"); p.add_argument("--backup-dir",default="/etc/nocturne-plugin-backups")
    a=p.parse_args()
    if a.apply and a.rollback_backup: raise SystemExit("choose --apply or --rollback-backup")
    result = (rollback(a.rollback_backup,a.admin_app,a.review_page,a.database) if a.rollback_backup else
              install(a.admin_app,a.review_page,a.database,a.backup_dir,apply=a.apply))
    print(json.dumps(result,sort_keys=True))
    if not (a.apply or a.rollback_backup): print("Dry run only; no files or database were changed.")


if __name__ == "__main__": main()
