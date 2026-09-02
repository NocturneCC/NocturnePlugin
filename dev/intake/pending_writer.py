"""Local Unix-socket writer for unverified RuneLite pending submissions."""
import argparse
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import socket
import sqlite3

from import_pending import REQUIRED_COLUMNS, apply_candidates, plan, schema_columns
from intake import MAX_BODY, validate, validate_screenshot
from preview import identity, inspect_item, readonly
from screenshot_lifecycle import AUDIT, EVIDENCE, LINKS


MAX_RESPONSE = 2048
MAX_STORED_SCREENSHOTS = 2000
SCREENSHOT_URL_PREFIX = "/admin/api/nocturne/runelite-submission-images/"


def private_screenshot_directory(database_dir):
    directory = Path(database_dir) / "runelite-submission-images"
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        pass
    metadata = directory.lstat()
    if (not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077):
        raise ValueError("unsafe screenshot directory")
    return directory


def store_screenshot(image, event_id, database_dir):
    if image is None:
        return None
    raw = validate_screenshot(image)
    directory = private_screenshot_directory(database_dir)
    final = directory / f"{event_id}.jpg"
    if final.exists():
        metadata = final.lstat()
        if not stat.S_ISREG(metadata.st_mode) or final.is_symlink():
            raise ValueError("unsafe existing screenshot")
        if final.read_bytes() != raw:
            raise ValueError("screenshot event collision")
        return {"url": SCREENSHOT_URL_PREFIX + final.name, "created": False,
                "state": "available", "filename": final.name, "digest": image["sha256"],
                "bytes": len(raw)}
    if sum(1 for path in directory.iterdir()
           if path.is_file() and path.suffix == ".jpg") >= MAX_STORED_SCREENSHOTS:
        return {"url": None, "created": False, "state": "storage_failed",
                "filename": None, "digest": image["sha256"], "bytes": len(raw),
                "failure": "capacity_exhausted"}
    temporary = directory / f".{event_id}.{os.getpid()}.tmp"
    try:
        with temporary.open("xb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, final)
    finally:
        temporary.unlink(missing_ok=True)
    return {"url": SCREENSHOT_URL_PREFIX + final.name, "created": True,
            "state": "available", "filename": final.name, "digest": image["sha256"],
            "bytes": len(raw)}


def evidence_inserter(data, stored, created_at):
    event_id = data["event_id"]
    state = stored["state"] if stored else "capture_failed"
    def insert(db, _candidate, submission_id):
        cursor = db.execute(f"""INSERT OR IGNORE INTO {EVIDENCE}
              (event_uuid,image_filename,image_sha256,image_bytes,created_at,review_state,storage_state)
              VALUES(?,?,?,?,?,'pending',?)""",
                            (event_id, stored and stored["filename"], stored and stored["digest"],
                             stored and stored["bytes"], created_at, state))
        db.execute(f"INSERT OR IGNORE INTO {LINKS}(event_uuid,submission_id) VALUES(?,?)",
                   (event_id, submission_id))
        if cursor.rowcount:
            detail = stored.get("failure", "stored") if stored else "screenshot_not_provided"
            db.execute(f"INSERT INTO {AUDIT}(event_uuid,image_filename,image_sha256,action,detail) VALUES(?,?,?,?,?)",
                       (event_id, stored and stored["filename"], stored and stored["digest"], state, detail))
    return insert


def check_environment(database_dir):
    root = Path(database_dir)
    missing = REQUIRED_COLUMNS - schema_columns(root / "RegularSubmissions.db")
    if missing:
        raise ValueError("regular_submissions schema missing: " + ", ".join(sorted(missing)))
    with closing(sqlite3.connect(root / "RegularSubmissions.db")) as db:
        present = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required = {EVIDENCE, LINKS, AUDIT}
        if not required <= present:
            raise ValueError("screenshot lifecycle schema missing")
    with ExitStack() as stack:
        members = stack.enter_context(readonly(root / "Members.db"))
        items = stack.enter_context(readonly(root / "Items.db"))
        if members.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='members'").fetchone() is None:
            raise ValueError("Members.db is missing members")
        if items.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='items'").fetchone() is None:
            raise ValueError("Items.db is missing items")


def process_payload(data, database_dir, now=None):
    """Revalidate one report and insert eligible proposals as pending only."""
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    validate(data, now)
    if data["version"] not in (2, 3, 4):
        return {"status": "excluded", "reason": "legacy_payload", "inserted": 0}
    if data["source"].startswith("Synthetic "):
        return {"status": "excluded", "reason": "synthetic_test", "inserted": 0}

    root = Path(database_dir)
    with ExitStack() as stack:
        members_db = stack.enter_context(readonly(root / "Members.db"))
        items_db = stack.enter_context(readonly(root / "Items.db"))
        submissions_db = stack.enter_context(readonly(root / "RegularSubmissions.db"))
        member = identity(members_db, data["rsn"])
        event = {
            "event_id": data["event_id"],
            "payload_version": data["version"],
            "rsn": data["rsn"],
            "source": data["source"],
            "occurred_at": data["occurred_at"],
            "status": "inspected",
            "member": member,
            "items": [
                inspect_item(items_db, submissions_db, data["event_id"], item,
                             member, now, 24)
                for item in data["items"]
            ],
        }
    candidates, excluded = plan({"events": [event]})
    stored = None
    if candidates and data.get("screenshot"):
        stored = store_screenshot(
            data["screenshot"], data["event_id"], root
        )
        for candidate in candidates:
            candidate["screenshot_url"] = stored["url"]
    result = apply_candidates(root / "RegularSubmissions.db", candidates, backup=False,
                              after_insert=evidence_inserter(data, stored,
                                  datetime.now(timezone.utc).isoformat()))
    if stored and stored["created"] and not result["inserted"]:
        (root / "runelite-submission-images" / f"{data['event_id']}.jpg").unlink(missing_ok=True)
    if result["inserted"]:
        status = "pending_stored"
    elif result["duplicates"] or excluded.get("already_imported"):
        status = "duplicate"
    else:
        status = "excluded"
    return {
        "status": status,
        "inserted": result["inserted"],
        "duplicates": result["duplicates"],
        "excluded": excluded,
        "automatic_awards_enabled": False,
        "rank_total_writes": 0,
        "screenshot_state": stored["state"] if stored else ("capture_failed" if result["inserted"] else None),
        "screenshot_failure": stored.get("failure") if stored else None,
    }


def read_request(connection):
    chunks = []
    size = 0
    while True:
        chunk = connection.recv(min(4096, MAX_BODY + 1 - size))
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_BODY:
            raise ValueError("request too large")
        chunks.append(chunk)
    if not chunks:
        raise ValueError("empty request")
    return json.loads(b"".join(chunks))


def response_bytes(body):
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_RESPONSE:
        raise ValueError("response too large")
    return raw


def handle_connection(connection, database_dir):
    try:
        body = process_payload(read_request(connection), database_dir)
    except (ValueError, TypeError, AttributeError, OverflowError,
            RecursionError, json.JSONDecodeError):
        body = {"status": "rejected"}
    except (OSError, sqlite3.Error):
        body = {"status": "unavailable"}
    connection.sendall(response_bytes(body))


def serve(socket_path, database_dir):
    path = Path(socket_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_socket():
        path.unlink()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        os.chmod(path, 0o666)
        server.listen(32)
        while True:
            connection, _ = server.accept()
            with connection:
                handle_connection(connection, database_dir)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", default="/run/nocturne-plugin-writer/pending.sock")
    parser.add_argument("--database-dir", default="/srv/projects/database")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        check_environment(args.database_dir)
        print("Pending writer database checks passed without writes.")
    else:
        serve(args.socket, args.database_dir)


if __name__ == "__main__":
    main()
