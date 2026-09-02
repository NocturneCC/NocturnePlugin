"""Guarded admin upgrade for transactional screenshot review-state synchronization."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
from uuid import uuid4

from derived_review_support import (_apply_metadata, _backup_status, _capture_safe_metadata,
    _database_check, _digest, _fsync, _stage_bytes, _verify_metadata)
from screenshot_lifecycle import require_compatible_schema

MARKER = "def sync_runelite_screenshot_review_state(conn, submission_ids):"
PURPOSE = "nocturne-screenshot-review-sync-v1"
MANIFEST = "SCREENSHOT_REVIEW_SYNC_MANIFEST.json"

HELPER = '''

def sync_runelite_screenshot_review_state(conn, submission_ids):
    ids = sorted({int(value) for value in submission_ids})
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    events = [row[0] for row in conn.execute(f"""
        SELECT DISTINCT event_uuid FROM runelite_screenshot_submissions
        WHERE submission_id IN ({placeholders})
    """, ids)]
    sync_runelite_screenshot_events(conn, events)

def sync_runelite_screenshot_events(conn, events):
    now = datetime.now(timezone.utc)
    for event_uuid in sorted(set(events)):
        statuses = [str(row[0] or "").lower() for row in conn.execute("""
            SELECT r.status FROM regular_submissions r
            JOIN runelite_screenshot_submissions l ON l.submission_id=r.submission_id
            WHERE l.event_uuid=?
        """, (event_uuid,))]
        if not statuses or any(status not in {"approved", "denied"} for status in statuses):
            desired_state, proposed_deadline = "pending", None
        elif "approved" in statuses:
            desired_state = "approved"
            proposed_deadline = (now + timedelta(days=30)).isoformat()
        else:
            desired_state = "denied"
            proposed_deadline = (now + timedelta(days=7)).isoformat()
        current = conn.execute("""
            SELECT review_state,purge_deadline FROM runelite_screenshot_evidence
            WHERE event_uuid=?
        """, (event_uuid,)).fetchone()
        if current is None:
            raise RuntimeError("screenshot evidence association is incomplete")
        deadline = (current[1] if current[0] == desired_state and current[1]
                    else proposed_deadline)
        if desired_state == "pending":
            deadline = None
        if (current[0], current[1]) != (desired_state, deadline):
            conn.execute("""
                UPDATE runelite_screenshot_evidence
                SET review_state=?,purge_deadline=? WHERE event_uuid=?
            """, (desired_state, deadline, event_uuid))
'''


def candidate_admin(text):
    if MARKER in text:
        raise ValueError("screenshot review synchronization already present")
    anchor = "def runelite_screenshot_evidence_state(conn, submission_id):\n"
    if text.count(anchor) != 1:
        raise ValueError("screenshot evidence helper marker missing or ambiguous")
    import_anchor = "from datetime import datetime, timedelta\n"
    if text.count(import_anchor) != 1:
        raise ValueError("datetime import marker missing or ambiguous")
    text = text.replace(import_anchor, "from datetime import datetime, timedelta, timezone\n", 1)
    text = text.replace(anchor, HELPER + "\n" + anchor, 1)

    after_update = '''                submission_id,
            ))

            totals = conn.execute("""
'''
    replacement = '''                submission_id,
            ))

            if status != previous_status:
                sync_runelite_screenshot_review_state(conn, [submission_id])

            totals = conn.execute("""
'''
    if text.count(after_update) != 1:
        raise ValueError("regular submission post-update marker missing or ambiguous")
    text = text.replace(after_update, replacement, 1)

    delete_select = '''            member_id = existing["member_id"]

            conn.execute("""
                DELETE FROM regular_submissions
'''
    delete_changed = '''            member_id = existing["member_id"]
            screenshot_events = [row[0] for row in conn.execute("""
                SELECT event_uuid FROM runelite_screenshot_submissions
                WHERE submission_id = ?
            """, (submission_id,))]
            conn.execute("DELETE FROM runelite_screenshot_submissions WHERE submission_id=?", (submission_id,))

            conn.execute("""
                DELETE FROM regular_submissions
'''
    if text.count(delete_select) != 1:
        raise ValueError("regular submission delete association path missing or ambiguous")
    text = text.replace(delete_select, delete_changed, 1)
    commit_anchor = '''            conn.commit()

        if member_id:
'''
    # The first matching block after the delete route is selected by route bounds.
    route_start = text.index('def nocturne_submission_delete(submission_id):')
    route_end = text.index('\n@app.route', route_start)
    route = text[route_start:route_end]
    if route.count(commit_anchor) != 1:
        raise ValueError("regular deletion commit path missing or ambiguous")
    route = route.replace(commit_anchor, '''            sync_runelite_screenshot_events(conn, screenshot_events)
            conn.commit()

        if member_id:
''', 1)
    return text[:route_start] + route + text[route_end:]


def file_state(text):
    if MARKER in text:
        return "already_applied"
    candidate_admin(text)
    return "not_applied"


def _host():
    path = Path("/etc/machine-id")
    return hashlib.sha256(path.read_bytes() if path.is_file() else platform.node().encode()).hexdigest()


def install(admin, database, backup_root, *, apply=False,
            admin_python="/srv/projects/api/venv-simon/bin/python", fail=None):
    admin, database, backup_root = map(Path, (admin, database, backup_root))
    if not admin.is_file() or admin.is_symlink() or not database.is_file() or database.is_symlink():
        raise ValueError("unsafe active target")
    require_compatible_schema(database); _database_check(database)
    metadata = _capture_safe_metadata(admin); original = admin.read_text(); state = file_state(original)
    report = {"state": state, "dry_run": not apply}
    if state == "already_applied" or not apply:
        return report
    if not backup_root.is_dir() or backup_root.is_symlink():
        raise ValueError("unsafe backup root")
    backup = backup_root / ("screenshot-review-sync-" + uuid4().hex[:8]); backup.mkdir(mode=0o700)
    saved = backup / admin.name; staged = None; mutation = False
    try:
        _backup_status(backup, "incomplete", "Admin backup incomplete")
        shutil.copyfile(admin, saved); _fsync(saved); _apply_metadata(saved, metadata)
        manifest = {"purpose": PURPOSE, "host": _host(), "target": str(admin),
                    "sha256": _digest(saved), "metadata": metadata}
        (backup / MANIFEST).write_text(json.dumps(manifest, sort_keys=True) + "\n"); _fsync(backup / MANIFEST)
        if _digest(saved) != _digest(admin): raise ValueError("backup checksum mismatch")
        _backup_status(backup, "verified", "Admin backup verified")
        if fail: fail("after_backup")
        staged = _stage_bytes(admin, candidate_admin(original).encode(), metadata)
        subprocess.run([admin_python, "-B", "-m", "py_compile", str(staged)], check=True, timeout=30)
        if file_state(staged.read_text()) != "already_applied": raise ValueError("staged validation failed")
        _verify_metadata(admin, metadata); mutation = True
        os.replace(staged, admin); staged = None
        if fail: fail("after_activation")
        _verify_metadata(admin, metadata)
        return {"state": "already_applied", "dry_run": False, "backup": str(backup)}
    except BaseException:
        if mutation:
            restored = _stage_bytes(admin, saved.read_bytes(), metadata)
            os.replace(restored, admin); _verify_metadata(admin, metadata)
            if file_state(admin.read_text()) != "not_applied": raise RuntimeError("admin rollback failed")
        raise
    finally:
        if staged: staged.unlink(missing_ok=True)


def rollback(backup, admin):
    backup, admin = map(Path, (backup, admin))
    status = json.loads((backup / "BACKUP_STATUS.json").read_text())
    manifest = json.loads((backup / MANIFEST).read_text())
    if status.get("state") != "verified" or manifest.get("purpose") != PURPOSE or manifest.get("host") != _host():
        raise ValueError("backup is not verified for this host and purpose")
    if manifest.get("target") != str(admin): raise ValueError("backup target mismatch")
    saved = backup / admin.name
    if _digest(saved) != manifest.get("sha256"): raise ValueError("backup checksum mismatch")
    if file_state(admin.read_text()) == "not_applied":
        if _digest(admin) != _digest(saved): raise ValueError("ambiguous already-restored state")
        return {"state": "not_applied", "already_restored": True}
    metadata = manifest["metadata"]; _verify_metadata(admin, metadata)
    staged = _stage_bytes(admin, saved.read_bytes(), metadata)
    os.replace(staged, admin); _verify_metadata(admin, metadata)
    if file_state(admin.read_text()) != "not_applied": raise RuntimeError("rollback failed")
    return {"state": "not_applied", "already_restored": False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply",action="store_true"); parser.add_argument("--rollback-backup")
    parser.add_argument("--admin-app",default="/srv/projects/api/admin_app.py")
    parser.add_argument("--database",default="/srv/projects/database/RegularSubmissions.db")
    parser.add_argument("--backup-dir",default="/etc/nocturne-plugin-backups")
    args=parser.parse_args()
    if args.apply and args.rollback_backup: raise SystemExit("choose apply or rollback")
    result=rollback(args.rollback_backup,args.admin_app) if args.rollback_backup else install(args.admin_app,args.database,args.backup_dir,apply=args.apply)
    print(json.dumps(result,sort_keys=True))
    if not (args.apply or args.rollback_backup): print("Dry run only; no active file was changed.")


if __name__ == "__main__": main()
