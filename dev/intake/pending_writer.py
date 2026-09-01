"""Local Unix-socket writer for unverified RuneLite pending submissions."""
import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import sqlite3

from import_pending import REQUIRED_COLUMNS, apply_candidates, plan, schema_columns
from intake import MAX_BODY, validate
from preview import identity, inspect_item, readonly


MAX_RESPONSE = 2048


def check_environment(database_dir):
    root = Path(database_dir)
    missing = REQUIRED_COLUMNS - schema_columns(root / "RegularSubmissions.db")
    if missing:
        raise ValueError("regular_submissions schema missing: " + ", ".join(sorted(missing)))
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
    if data["version"] != 2:
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
    result = apply_candidates(root / "RegularSubmissions.db", candidates, backup=False)
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
