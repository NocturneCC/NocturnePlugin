"""Isolated WSGI test intake. Run behind gunicorn and the supplied nginx limits."""
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

MAX_BODY = 8192
MAX_ROWS = 10000
RSN = re.compile(r"[A-Za-z0-9 _-]{1,12}\Z")


def normalize_rsn(value):
    if not isinstance(value, str) or not RSN.fullmatch(value):
        raise ValueError("invalid RSN")
    name = " ".join(value.replace("_", " ").lower().split())
    if not name:
        raise ValueError("empty RSN")
    return name


def validate(data, now):
    if not isinstance(data, dict) or set(data) != {
        "version", "event_id", "occurred_at", "rsn", "source", "items"
    }:
        raise ValueError("invalid fields")
    if type(data["version"]) is not int or data["version"] not in (1, 2):
        raise ValueError("invalid version")
    if not isinstance(data["event_id"], str) or str(UUID(data["event_id"])) != data["event_id"]:
        raise ValueError("invalid event ID")
    name = normalize_rsn(data["rsn"])
    source = data["source"]
    if not isinstance(source, str) or not 1 <= len(source) <= 100 or not source.strip() or not source.isprintable():
        raise ValueError("invalid source")
    stamp = data["occurred_at"]
    if not isinstance(stamp, str) or len(stamp) > 40:
        raise ValueError("invalid timestamp")
    occurred = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    if occurred.tzinfo is None or not -120 <= now - occurred.timestamp() <= 600:
        raise ValueError("timestamp out of range")
    items = data["items"]
    if not isinstance(items, list) or not 1 <= len(items) <= 64:
        raise ValueError("invalid items")
    ids = set()
    for item in items:
        expected = {"item_id", "quantity"} | ({"unit_price_gp"} if data["version"] == 2 else set())
        if not isinstance(item, dict) or set(item) != expected:
            raise ValueError("invalid item fields")
        if type(item["item_id"]) is not int or not 1 <= item["item_id"] <= 1000000:
            raise ValueError("invalid item ID")
        if type(item["quantity"]) is not int or not 1 <= item["quantity"] <= 2147483647:
            raise ValueError("invalid quantity")
        if data["version"] == 2 and (type(item["unit_price_gp"]) is not int or not 0 <= item["unit_price_gp"] <= 2147483647):
            raise ValueError("invalid unit price")
        if item["item_id"] in ids:
            raise ValueError("duplicate item stack")
        ids.add(item["item_id"])
    canonical = dict(data, rsn=name, items=sorted(items, key=lambda x: x["item_id"]))
    return name, json.dumps(canonical, sort_keys=True, separators=(",", ":"))


def create_app(state_dir=None, allowed_rsns=None, clock=None):
    state_dir = state_dir or os.environ["NOCTURNE_INTAKE_STATE"]
    if allowed_rsns is None:
        allowed_rsns = os.environ["NOCTURNE_TEST_RSNS"].split(",")
    allowed = {normalize_rsn(name) for name in allowed_rsns}
    if not allowed:
        raise ValueError("At least one test RSN is required")
    clock = clock or (lambda: datetime.now(timezone.utc).timestamp())
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    database = directory / "test-drops.sqlite3"

    @contextmanager
    def connect():
        db = sqlite3.connect(database, timeout=2)
        try:
            # Fixed storage budget, including SQLite indexes; no unbounded WAL.
            db.execute("PRAGMA max_page_count=4096")
            with db:
                yield db
        finally:
            db.close()

    with connect() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS test_drops (
            event_id TEXT PRIMARY KEY, rsn TEXT NOT NULL,
            received_at REAL NOT NULL, payload TEXT NOT NULL)""")
        db.execute("CREATE INDEX IF NOT EXISTS intake_rsn_time ON test_drops(rsn, received_at)")

    def reply(start_response, code, body):
        reasons = {200: "OK", 201: "Created", 400: "Bad Request", 403: "Forbidden",
                   404: "Not Found", 405: "Method Not Allowed", 409: "Conflict",
                   411: "Length Required", 413: "Content Too Large", 415: "Unsupported Media Type",
                   429: "Too Many Requests", 503: "Service Unavailable", 507: "Insufficient Storage"}
        raw = json.dumps(body, separators=(",", ":")).encode()
        headers = [("Content-Type", "application/json"), ("Content-Length", str(len(raw))),
                   ("Cache-Control", "no-store")]
        if code == 405:
            headers.append(("Allow", "POST"))
        if code in (429, 503):
            headers.append(("Retry-After", "60"))
        start_response(f"{code} {reasons[code]}", headers)
        return [raw]

    def application(environ, start_response):
        def error(code):
            return reply(start_response, code, {"status": "not_accepted", "storage": "development"})
        if environ.get("PATH_INFO") != "/api/plugin/dev/drops":
            return error(404)
        if environ.get("REQUEST_METHOD") != "POST":
            return error(405)
        if environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower() != "application/json":
            return error(415)
        try:
            length = int(environ.get("CONTENT_LENGTH", ""))
        except (ValueError, TypeError):
            return error(411)
        if length < 1 or length > MAX_BODY:
            return error(413)
        try:
            raw = environ["wsgi.input"].read(length)
            if len(raw) != length:
                return error(400)
            data = json.loads(raw)
            now = clock()
            name, canonical = validate(data, now)
        except (ValueError, TypeError, OverflowError, RecursionError, AttributeError):
            return error(400)
        if name not in allowed:
            return error(403)
        try:
            with connect() as db:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute("SELECT payload FROM test_drops WHERE event_id=?", (data["event_id"],)).fetchone()
                if existing:
                    if existing[0] != canonical:
                        return error(409)
                    return reply(start_response, 200, {"event_id": data["event_id"], "status": "duplicate", "storage": "development"})
                db.execute("DELETE FROM test_drops WHERE received_at < ?", (now - 7 * 86400,))
                count = db.execute("SELECT count(*) FROM test_drops WHERE rsn=? AND received_at > ?", (name, now - 60)).fetchone()[0]
                if count >= 20:
                    return error(429)
                if db.execute("SELECT count(*) FROM test_drops").fetchone()[0] >= MAX_ROWS:
                    return error(507)
                db.execute("INSERT INTO test_drops VALUES (?, ?, ?, ?)", (data["event_id"], name, now, canonical))
            # Acknowledge only after the transaction commits.
            return reply(start_response, 201, {"event_id": data["event_id"], "status": "stored", "storage": "development"})
        except sqlite3.Error:
            return error(503)

    return application
