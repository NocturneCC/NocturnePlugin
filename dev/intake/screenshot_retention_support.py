"""Install private screenshot lifecycle schema/review support; dry-run by default."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
from uuid import uuid4

from derived_review_support import (MIN_FREE_MARGIN, _backup_database, _backup_file,
    _backup_status, _capture_safe_metadata, _database_check, _digest, _restore_database,
    _restore_file, _stage_bytes, _verify_metadata)
from screenshot_lifecycle import migrate, schema_state

ADMIN_MARKER = "def runelite_screenshot_evidence_state(conn, submission_id):"
UI_MARKER = "screenshotEvidenceLabel"


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
    files, schema = file_state(*texts), schema_state(database)
    if files != schema: raise ValueError("ambiguous partially applied screenshot lifecycle state")
    _database_check(database)
    report = {"state": files, "dry_run": not apply, "database_size": database.stat().st_size}
    if files == "already_applied" or not apply: return report
    required = report["database_size"] + max(MIN_FREE_MARGIN, report["database_size"] // 4)
    if shutil.disk_usage(backup_root).free < required: raise ValueError("insufficient backup space")
    backup = backup_root / ("screenshot-retention-" + uuid4().hex[:8]); backup.mkdir(mode=0o700)
    saved = (backup / admin.name, backup / review.name, backup / database.name)
    staged = []; mutation = False
    try:
        _backup_status(backup, "incomplete", "Backup creation or verification incomplete")
        _backup_file(admin, saved[0], metadata["admin"]); _backup_file(review, saved[1], metadata["review"])
        _backup_database(database, saved[2], metadata["database"]); _database_check(saved[2])
        manifest = {p.name: _digest(p) for p in saved}
        (backup / "SHA256SUMS.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
        if any(_digest(backup / n) != d for n, d in manifest.items()): raise ValueError("backup checksum failure")
        _backup_status(backup, "verified", "Backups verified")
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
            for fn in (lambda:_restore_file(saved[0],admin,metadata["admin"]), lambda:_restore_file(saved[1],review,metadata["review"]), lambda:_restore_database(saved[2],database,metadata["database"])):
                try: fn()
                except BaseException as caught: errors.append(str(caught))
            if errors: raise RuntimeError("rollback failed: " + " | ".join(errors)) from error
            if file_state(admin.read_text(), review.read_text()) != "not_applied" or schema_state(database) != "not_applied": raise RuntimeError("rollback verification failed") from error
        raise
    finally:
        for path in staged: path.unlink(missing_ok=True)


def main():
    p=argparse.ArgumentParser(description=__doc__); p.add_argument("--apply",action="store_true")
    p.add_argument("--admin-app",default="/srv/projects/api/admin_app.py"); p.add_argument("--review-page",default="/srv/projects/website/regular-submissions-review.html")
    p.add_argument("--database",default="/srv/projects/database/RegularSubmissions.db"); p.add_argument("--backup-dir",default="/etc/nocturne-plugin-backups")
    a=p.parse_args(); print(json.dumps(install(a.admin_app,a.review_page,a.database,a.backup_dir,apply=a.apply),sort_keys=True))
    if not a.apply: print("Dry run only; no files or database were changed.")


if __name__ == "__main__": main()
