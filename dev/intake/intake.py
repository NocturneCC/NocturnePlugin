"""Isolated WSGI test intake. Run behind gunicorn and the supplied nginx limits."""
import base64
import binascii
import hashlib
import json
import os
import re
import socket
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID
from derived_values import validate_item_valuation
from raid_presence import process as process_raid_presence

MAX_METADATA_BODY = 8192
MAX_SCREENSHOT_BYTES = 240 * 1024
MAX_BODY = 360 * 1024
MAX_ROWS = 10000
RSN = re.compile(r"[A-Za-z0-9 _-]{1,12}\Z")


def socket_handoff(socket_path, canonical, timeout=0.5):
    """Send one canonical payload to the local pending writer.

    The writer response is deliberately small and carries no member data.  A
    failure is returned to the client so the same event UUID can be submitted
    again; the intake row remains idempotently stored for inspection.
    """
    raw = canonical.encode("utf-8")
    if len(raw) > MAX_BODY:
        raise ValueError("handoff payload too large")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(socket_path)
        client.sendall(raw)
        client.shutdown(socket.SHUT_WR)
        chunks = []
        size = 0
        while True:
            chunk = client.recv(min(2049 - size, 2049))
            if not chunk:
                break
            size += len(chunk)
            if size > 2048:
                raise ValueError("handoff response too large")
            chunks.append(chunk)
        response = b"".join(chunks)
    if not response:
        raise ValueError("empty handoff response")
    if len(response) > 2048:
        raise ValueError("handoff response too large")
    receipt = json.loads(response)
    if not isinstance(receipt, dict) or receipt.get("status") not in {
        "pending_stored", "duplicate", "excluded", "presence_identity"
    }:
        raise ValueError("invalid handoff receipt")
    return receipt


def normalize_rsn(value):
    if not isinstance(value, str) or not RSN.fullmatch(value):
        raise ValueError("invalid RSN")
    name = " ".join(value.replace("_", " ").lower().split())
    if not name:
        raise ValueError("empty RSN")
    return name


def validate(data, now):
    if not isinstance(data, dict) or type(data.get("version")) is not int:
        raise ValueError("invalid fields")
    if data["version"] not in (1, 2, 3, 4):
        raise ValueError("invalid version")
    expected_fields = {"version", "event_id", "occurred_at", "rsn", "source", "items"}
    if data["version"] == 3 or (data["version"] == 4 and "screenshot" in data):
        expected_fields.add("screenshot")
    if set(data) != expected_fields:
        raise ValueError("invalid fields")
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
        expected = {"item_id", "quantity"} | ({"unit_price_gp"} if data["version"] in (2, 3) else set())
        if data["version"] == 4:
            expected = set(item) if isinstance(item, dict) and {"item_id", "quantity"}.issubset(item) else set()
        if not isinstance(item, dict) or set(item) != expected:
            raise ValueError("invalid item fields")
        if type(item["item_id"]) is not int or not 1 <= item["item_id"] <= 1000000:
            raise ValueError("invalid item ID")
        if type(item["quantity"]) is not int or not 1 <= item["quantity"] <= 2147483647:
            raise ValueError("invalid quantity")
        if data["version"] in (2, 3, 4) and (type(item.get("unit_price_gp")) is not int or not 0 <= item["unit_price_gp"] <= 2147483647):
            raise ValueError("invalid unit price")
        if data["version"] == 4:
            validate_item_valuation(item)
        if item["item_id"] in ids:
            raise ValueError("duplicate item stack")
        ids.add(item["item_id"])
    if data["version"] == 3 or (data["version"] == 4 and "screenshot" in data):
        validate_screenshot(data["screenshot"])
    canonical = dict(data, rsn=name, items=sorted(items, key=lambda x: x["item_id"]))
    return name, json.dumps(canonical, sort_keys=True, separators=(",", ":"))


def validate_screenshot(image):
    if not isinstance(image, dict) or set(image) != {
        "mime_type", "width", "height", "sha256", "data_base64"
    }:
        raise ValueError("invalid screenshot fields")
    if image["mime_type"] != "image/jpeg":
        raise ValueError("invalid screenshot type")
    if type(image["width"]) is not int or not 32 <= image["width"] <= 4096:
        raise ValueError("invalid screenshot width")
    if type(image["height"]) is not int or not 32 <= image["height"] <= 4096:
        raise ValueError("invalid screenshot height")
    if not isinstance(image["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", image["sha256"]):
        raise ValueError("invalid screenshot digest")
    if not isinstance(image["data_base64"], str):
        raise ValueError("invalid screenshot data")
    try:
        raw = base64.b64decode(image["data_base64"], validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("invalid screenshot encoding")
    if not 4 <= len(raw) <= MAX_SCREENSHOT_BYTES or not raw.startswith(b"\xff\xd8") or not raw.endswith(b"\xff\xd9"):
        raise ValueError("invalid screenshot bytes")
    if hashlib.sha256(raw).hexdigest() != image["sha256"]:
        raise ValueError("screenshot digest mismatch")
    return raw


def storage_payload(data, normalized_rsn):
    """Remove image bytes from persistent intake storage while retaining metadata."""
    stored = dict(data, rsn=normalized_rsn)
    stored["items"] = sorted(data["items"], key=lambda item: item["item_id"])
    stored.pop("screenshot", None)
    if stored["version"] == 3:
        stored["version"] = 2
    return json.dumps(stored, sort_keys=True, separators=(",", ":"))


def screenshot_digest(data):
    return data.get("screenshot", {}).get("sha256")


def create_app(state_dir=None, allowed_rsns=None, clock=None, handoff=None,
               presence_identity_resolver=None):
    state_dir = state_dir or os.environ["NOCTURNE_INTAKE_STATE"]
    if allowed_rsns is None:
        allowed_rsns = os.environ["NOCTURNE_TEST_RSNS"].split(",")
    allowed = {normalize_rsn(name) for name in allowed_rsns}
    if not allowed:
        raise ValueError("At least one test RSN is required")
    clock = clock or (lambda: datetime.now(timezone.utc).timestamp())
    socket_path = os.environ.get("NOCTURNE_PENDING_SOCKET")
    if handoff is None:
        handoff = ((lambda canonical: socket_handoff(socket_path, canonical))
                   if socket_path else (lambda canonical: None))
    directory = Path(state_dir)
    directory.mkdir(parents=True, exist_ok=True)
    database = directory / "test-drops.sqlite3"
    presence_database = directory / "raid-presence-v1.sqlite3"
    if presence_identity_resolver is None:
        if socket_path:
            def presence_identity_resolver(rsn):
                request = json.dumps({"operation": "raid_presence_identity", "rsn": rsn},
                                     sort_keys=True, separators=(",", ":"))
                receipt = socket_handoff(socket_path, request)
                return receipt.get("member_key") if receipt.get("status") == "presence_identity" else None
        else:
            # Production must resolve through the isolated writer's read-only
            # Members.db RPC. No socket means presence fails closed unless a
            # test or embedding explicitly provides an equivalent resolver.
            presence_identity_resolver = lambda _rsn: None

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
        columns = {row[1] for row in db.execute("PRAGMA table_info(test_drops)")}
        if "screenshot_sha256" not in columns:
            db.execute("ALTER TABLE test_drops ADD COLUMN screenshot_sha256 TEXT")

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
        path = environ.get("PATH_INFO")
        if path not in {"/api/plugin/dev/drops", "/api/plugin/dev/raid-presence"}:
            return error(404)
        if environ.get("REQUEST_METHOD") != "POST":
            return error(405)
        if environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower() != "application/json":
            return error(415)
        try:
            length = int(environ.get("CONTENT_LENGTH", ""))
        except (ValueError, TypeError):
            return error(411)
        limit = MAX_METADATA_BODY if path == "/api/plugin/dev/raid-presence" else MAX_BODY
        if length < 1 or length > limit:
            return error(413)
        try:
            raw = environ["wsgi.input"].read(length)
            if len(raw) != length:
                return error(400)
            data = json.loads(raw)
            now = clock()
            if path == "/api/plugin/dev/raid-presence":
                submitted_rsn = normalize_rsn(data.get("rsn"))
                if submitted_rsn not in allowed:
                    return error(403)
                result = process_raid_presence(
                    presence_database, data,
                    lambda rsn: presence_identity_resolver(rsn) if rsn in allowed else None,
                    now)
                return reply(start_response, 200 if result["status"] == "duplicate" else 201, result)
            name, canonical = validate(data, now)
            stored = storage_payload(data, name)
            digest = screenshot_digest(data)
        except PermissionError:
            return error(403)
        except sqlite3.Error:
            return error(503)
        except (ValueError, TypeError, OverflowError, RecursionError, AttributeError):
            return error(400)
        if name not in allowed:
            return error(403)
        try:
            with connect() as db:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute("SELECT payload, screenshot_sha256 FROM test_drops WHERE event_id=?", (data["event_id"],)).fetchone()
                if existing:
                    if existing != (stored, digest):
                        return error(409)
                    try:
                        handoff(canonical)
                    except (OSError, ValueError, TypeError, json.JSONDecodeError):
                        return error(503)
                    return reply(start_response, 200, {"event_id": data["event_id"], "status": "duplicate", "storage": "development"})
                db.execute("DELETE FROM test_drops WHERE received_at < ?", (now - 7 * 86400,))
                count = db.execute("SELECT count(*) FROM test_drops WHERE rsn=? AND received_at > ?", (name, now - 60)).fetchone()[0]
                if count >= 20:
                    return error(429)
                if db.execute("SELECT count(*) FROM test_drops").fetchone()[0] >= MAX_ROWS:
                    return error(507)
                db.execute("""INSERT INTO test_drops
                    (event_id, rsn, received_at, payload, screenshot_sha256)
                    VALUES (?, ?, ?, ?, ?)""", (data["event_id"], name, now, stored, digest))
            # Acknowledge only after the transaction commits.
            try:
                handoff(canonical)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                return error(503)
            return reply(start_response, 201, {"event_id": data["event_id"], "status": "stored", "storage": "development"})
        except sqlite3.Error:
            return error(503)

    return application
