"""Versioned clan-announcement storage and bounded public/admin interfaces."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import unicodedata
from urllib.parse import urlsplit
from uuid import uuid4


SCHEMA_VERSION = 1
PUBLIC_PATH = "/api/plugin/v1/announcements"
MAX_ANNOUNCEMENTS = 3
MAX_MESSAGE_CHARS = 500
MAX_MESSAGE_LINES = 4
MAX_TITLE_CHARS = 80
MAX_RESPONSE_BYTES = 16 * 1024
MAX_ID_CHARS = 64
MAX_SNAPSHOT_ANNOUNCEMENTS = 64
MAX_SNAPSHOT_BYTES = 128 * 1024
DEFAULT_PUBLIC_SNAPSHOT = "/srv/projects/nocturne-plugin-announcements-public/announcements-v1.json"
SEVERITIES = frozenset({"info", "notice", "warning", "urgent"})
STATES = frozenset({"draft", "published", "withdrawn", "expired"})
ALLOWED_LINKS = frozenset({
    ("nocturne.events", "/"),
    ("nocturne.events", "/event-board.html"),
})
_MARKUP = re.compile(r"<[^>]*>|\[[^\]]*\]\([^)]*\)|!\[[^\]]*\]|```|`[^`]*`")
_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")


class RevisionConflict(ValueError):
    """The administrator acted on a revision that is no longer current."""


def _strict_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON field")
        value[key] = item
    return value


def strict_json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_strict_object,
                          parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("invalid JSON number")))
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ValueError("invalid JSON") from error

SCHEMA_OBJECTS = frozenset({
    "plugin_announcement_meta",
    "plugin_announcements",
    "plugin_announcement_audit",
    "plugin_announcement_audit_no_update",
    "plugin_announcement_audit_no_delete",
})


def _utc(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError("invalid timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("invalid timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def _timestamp(value):
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _plain(value, name, maximum, *, required=True, lines=1):
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ValueError(f"invalid {name}")
    value = value.strip()
    if not value and not required:
        return None
    if (required and not value) or len(value) > maximum or len(value.splitlines()) > lines:
        raise ValueError(f"invalid {name}")
    if any(character != "\n" and unicodedata.category(character).startswith("C")
           for character in value):
        raise ValueError(f"unsafe {name}")
    if "<" in value or ">" in value or _MARKUP.search(value):
        raise ValueError(f"markup is not allowed in {name}")
    return value


def _link(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"label", "url"}:
        raise ValueError("invalid link fields")
    label = _plain(value["label"], "link label", 48)
    if not isinstance(value["url"], str) or len(value["url"]) > 256:
        raise ValueError("invalid link")
    parsed = urlsplit(value["url"])
    if (parsed.scheme != "https" or parsed.username is not None or parsed.password is not None
            or parsed.port is not None or parsed.query or parsed.fragment
            or (parsed.hostname, parsed.path or "/") not in ALLOWED_LINKS):
        raise ValueError("link is not allowlisted")
    canonical = f"https://{parsed.hostname}{parsed.path or '/'}"
    return {"label": label, "url": canonical}


def validate_fields(data):
    expected = {"title", "message", "severity", "starts_at", "expires_at", "link"}
    if not isinstance(data, dict) or set(data) != expected:
        raise ValueError("invalid announcement fields")
    title = _plain(data["title"], "title", MAX_TITLE_CHARS, required=False)
    message = _plain(data["message"], "message", MAX_MESSAGE_CHARS, lines=MAX_MESSAGE_LINES)
    severity = data["severity"]
    if severity not in SEVERITIES:
        raise ValueError("invalid severity")
    starts = _utc(data["starts_at"])
    expires = _utc(data["expires_at"])
    if starts >= expires:
        raise ValueError("expiration must follow start")
    return {
        "title": title,
        "message": message,
        "severity": severity,
        "starts_at": _timestamp(starts),
        "expires_at": _timestamp(expires),
        "link": _link(data["link"]),
    }


def install_schema(database):
    """Create only the dedicated announcement objects; caller controls backup/apply."""
    with closing(sqlite3.connect(database, timeout=10)) as db:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript("""
        BEGIN IMMEDIATE;
        CREATE TABLE IF NOT EXISTS plugin_announcement_meta(
          singleton INTEGER PRIMARY KEY CHECK(singleton=1),
          schema_version INTEGER NOT NULL,
          global_revision INTEGER NOT NULL CHECK(global_revision>=0),
          generated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS plugin_announcements(
          announcement_id TEXT PRIMARY KEY,
          revision INTEGER NOT NULL CHECK(revision>0),
          state TEXT NOT NULL CHECK(state IN ('draft','published','withdrawn','expired')),
          title TEXT,
          message TEXT NOT NULL,
          severity TEXT NOT NULL CHECK(severity IN ('info','notice','warning','urgent')),
          starts_at TEXT NOT NULL,
          expires_at TEXT NOT NULL,
          link_label TEXT,
          link_url TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          created_by TEXT NOT NULL,
          updated_by TEXT NOT NULL,
          CHECK((link_label IS NULL)=(link_url IS NULL))
        );
        CREATE TABLE IF NOT EXISTS plugin_announcement_audit(
          audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
          announcement_id TEXT NOT NULL,
          announcement_revision INTEGER NOT NULL,
          global_revision INTEGER NOT NULL,
          action TEXT NOT NULL CHECK(action IN
            ('create_draft','edit_draft','publish','withdraw','expire')),
          actor TEXT NOT NULL,
          occurred_at TEXT NOT NULL,
          before_json TEXT,
          after_json TEXT NOT NULL
        );
        CREATE TRIGGER IF NOT EXISTS plugin_announcement_audit_no_update
        BEFORE UPDATE ON plugin_announcement_audit BEGIN
          SELECT RAISE(ABORT,'announcement audit is append-only');
        END;
        CREATE TRIGGER IF NOT EXISTS plugin_announcement_audit_no_delete
        BEFORE DELETE ON plugin_announcement_audit BEGIN
          SELECT RAISE(ABORT,'announcement audit is append-only');
        END;
        """)
        now = _timestamp(datetime.now(timezone.utc))
        db.execute("INSERT OR IGNORE INTO plugin_announcement_meta VALUES(1,?,?,?)",
                   (SCHEMA_VERSION, 0, now))
        version = db.execute("SELECT schema_version FROM plugin_announcement_meta WHERE singleton=1").fetchone()
        if version != (SCHEMA_VERSION,):
            raise ValueError("unsupported announcement schema")
        db.commit()


def schema_state(database):
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        found = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE name LIKE 'plugin_announcement_%'")}
        if not found:
            return "not_applied"
        if found != SCHEMA_OBJECTS:
            raise ValueError("partial or unexpected announcement schema")
        row = db.execute("SELECT schema_version FROM plugin_announcement_meta WHERE singleton=1").fetchone()
        if row != (SCHEMA_VERSION,):
            raise ValueError("unsupported announcement schema")
        return "already_applied"


def _row(row):
    link = None if row[8] is None else {"label": row[8], "url": row[9]}
    return {"announcement_id": row[0], "revision": row[1], "state": row[2],
            "title": row[3], "message": row[4], "severity": row[5],
            "starts_at": row[6], "expires_at": row[7], "link": link,
            "created_at": row[10], "updated_at": row[11]}


_SELECT = ("SELECT announcement_id,revision,state,title,message,severity,starts_at,expires_at,"
           "link_label,link_url,created_at,updated_at FROM plugin_announcements")


class AnnouncementStore:
    def __init__(self, database, clock=None, public_snapshot=None):
        self.database = str(database)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.public_snapshot = public_snapshot

    def _now(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("clock must return an aware datetime")
        return _timestamp(value)

    def _actor(self, actor):
        return _plain(actor, "actor", 128)

    def _change(self, announcement_id, action, actor, fields=None, expected_revision=None):
        actor = self._actor(actor)
        now = self._now()
        with closing(sqlite3.connect(self.database, timeout=10)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            meta = db.execute("SELECT schema_version,global_revision FROM plugin_announcement_meta WHERE singleton=1").fetchone()
            if meta is None or meta[0] != SCHEMA_VERSION:
                raise ValueError("unsupported announcement schema")
            before_row = None
            if announcement_id is not None:
                before_row = db.execute(_SELECT + " WHERE announcement_id=?", (announcement_id,)).fetchone()
                if before_row is None:
                    raise KeyError("announcement not found")
            before = None if before_row is None else _row(before_row)
            if before is not None:
                if type(expected_revision) is not int or expected_revision < 1:
                    raise ValueError("expected_revision is required")
                if before["revision"] != expected_revision:
                    raise RevisionConflict("announcement revision changed")
            revision = 1 if before is None else before["revision"] + 1
            global_revision = meta[1] + 1
            if action == "create_draft":
                values = validate_fields(fields)
                announcement_id = uuid4().hex
                link = values["link"]
                db.execute("""INSERT INTO plugin_announcements VALUES(
                    ?,?,'draft',?,?,?,?,?,?,?,?,?,?,?)""",
                    (announcement_id, revision, values["title"], values["message"], values["severity"],
                     values["starts_at"], values["expires_at"],
                     None if link is None else link["label"], None if link is None else link["url"],
                     now, now, actor, actor))
            elif action == "edit_draft":
                if before["state"] != "draft":
                    raise ValueError("only drafts may be edited")
                values = validate_fields(fields)
                link = values["link"]
                db.execute("""UPDATE plugin_announcements SET revision=?,title=?,message=?,severity=?,
                    starts_at=?,expires_at=?,link_label=?,link_url=?,updated_at=?,updated_by=?
                    WHERE announcement_id=?""",
                    (revision, values["title"], values["message"], values["severity"],
                     values["starts_at"], values["expires_at"],
                     None if link is None else link["label"], None if link is None else link["url"],
                     now, actor, announcement_id))
            else:
                expected = "draft" if action == "publish" else "published"
                if before["state"] != expected:
                    raise ValueError(f"cannot {action} announcement in current state")
                if action == "publish":
                    scheduled = db.execute(
                        "SELECT COUNT(*) FROM plugin_announcements "
                        "WHERE state='published' AND expires_at>?", (now,)).fetchone()[0]
                    if type(scheduled) is not int or scheduled >= MAX_SNAPSHOT_ANNOUNCEMENTS:
                        raise ValueError("too many scheduled announcements")
                state = {"publish": "published", "withdraw": "withdrawn", "expire": "expired"}[action]
                expiry = now if action == "expire" else before["expires_at"]
                db.execute("UPDATE plugin_announcements SET revision=?,state=?,expires_at=?,updated_at=?,updated_by=? WHERE announcement_id=?",
                           (revision, state, expiry, now, actor, announcement_id))
            after = _row(db.execute(_SELECT + " WHERE announcement_id=?", (announcement_id,)).fetchone())
            db.execute("UPDATE plugin_announcement_meta SET global_revision=?,generated_at=? WHERE singleton=1",
                       (global_revision, now))
            db.execute("""INSERT INTO plugin_announcement_audit
                (announcement_id,announcement_revision,global_revision,action,actor,occurred_at,before_json,after_json)
                VALUES(?,?,?,?,?,?,?,?)""",
                (announcement_id, revision, global_revision, action, actor, now,
                 None if before is None else json.dumps(before, sort_keys=True, separators=(",", ":")),
                 json.dumps(after, sort_keys=True, separators=(",", ":"))))
            db.commit()
        if self.public_snapshot is not None:
            write_public_snapshot(self.database, self.public_snapshot, self.clock())
        return after

    def create_draft(self, fields, actor):
        return self._change(None, "create_draft", actor, fields)

    def edit_draft(self, announcement_id, fields, actor, expected_revision=None):
        return self._change(_plain(announcement_id, "announcement id", MAX_ID_CHARS),
                            "edit_draft", actor, fields, expected_revision)

    def transition(self, announcement_id, action, actor, expected_revision=None):
        if action not in {"publish", "withdraw", "expire"}:
            raise ValueError("invalid action")
        return self._change(_plain(announcement_id, "announcement id", MAX_ID_CHARS), action, actor,
                            expected_revision=expected_revision)

    def list_all(self):
        with closing(sqlite3.connect(f"file:{self.database}?mode=ro", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            self._require_schema(db)
            return [_row(row) for row in db.execute(_SELECT + " ORDER BY created_at DESC,announcement_id")]

    def list_audit(self):
        with closing(sqlite3.connect(f"file:{self.database}?mode=ro", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            self._require_schema(db)
            return [dict(zip(("audit_id", "announcement_id", "announcement_revision", "global_revision",
                              "action", "actor", "occurred_at", "before_json", "after_json"), row))
                    for row in db.execute("SELECT * FROM plugin_announcement_audit ORDER BY audit_id DESC")]

    @staticmethod
    def _require_schema(db):
        row = db.execute("SELECT schema_version FROM plugin_announcement_meta WHERE singleton=1").fetchone()
        if row != (SCHEMA_VERSION,):
            raise ValueError("unsupported announcement schema")


def _public_item(row):
    value = _row(row)
    if not _ID.fullmatch(value["announcement_id"]):
        raise ValueError("invalid announcement id")
    if type(value["revision"]) is not int or not 1 <= value["revision"] <= 2 ** 31 - 1:
        raise ValueError("invalid announcement revision")
    validate_fields({key: value[key] for key in
                     ("title", "message", "severity", "starts_at", "expires_at", "link")})
    return {key: value[key] for key in
            ("announcement_id", "revision", "title", "message", "severity",
             "starts_at", "expires_at", "link")}


def snapshot_payload(database, now=None):
    """Build the public-only publication snapshot from the trusted writable side."""
    now = now or datetime.now(timezone.utc)
    current = _timestamp(now)
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        AnnouncementStore._require_schema(db)
        global_revision, generated = db.execute(
            "SELECT global_revision,generated_at FROM plugin_announcement_meta WHERE singleton=1").fetchone()
        rows = db.execute(_SELECT + " WHERE state='published' AND expires_at>? "
                          "ORDER BY starts_at,announcement_id LIMIT ?",
                          (current, MAX_SNAPSHOT_ANNOUNCEMENTS + 1)).fetchall()
    if type(global_revision) is not int or not 0 <= global_revision <= 2 ** 63 - 1:
        raise ValueError("invalid global revision")
    _utc(generated)
    if len(rows) > MAX_SNAPSHOT_ANNOUNCEMENTS:
        raise ValueError("too many scheduled announcements")
    announcements = [_public_item(row) for row in rows]
    payload = {"schema_version": SCHEMA_VERSION, "revision": global_revision,
               "generated_at": generated, "announcements": announcements}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    if len(raw) > MAX_SNAPSHOT_BYTES:
        raise ValueError("announcement snapshot exceeds limit")
    return raw


def write_public_snapshot(database, snapshot=DEFAULT_PUBLIC_SNAPSHOT, now=None):
    """Atomically publish only bounded, validated, non-draft announcement fields."""
    target = Path(snapshot)
    parent = target.parent
    if not parent.is_dir() or parent.is_symlink() or target.is_symlink():
        raise ValueError("unsafe announcement snapshot path")
    raw = snapshot_payload(database, now)
    descriptor, name = tempfile.mkstemp(prefix=".announcements-v1-", dir=parent)
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(staged, 0o644)
        os.replace(staged, target)
        directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        staged.unlink(missing_ok=True)


def public_payload(snapshot, now=None):
    """Read a public-only immutable snapshot; this function has no database access."""
    now = now or datetime.now(timezone.utc)
    raw_snapshot = Path(snapshot).read_bytes()
    if len(raw_snapshot) > MAX_SNAPSHOT_BYTES:
        raise ValueError("announcement snapshot exceeds limit")
    source = strict_json(raw_snapshot.decode("utf-8", errors="strict"))
    if not isinstance(source, dict) or set(source) != {
            "schema_version", "revision", "generated_at", "announcements"}:
        raise ValueError("invalid announcement snapshot fields")
    if type(source["schema_version"]) is not int or source["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported announcement snapshot schema")
    if type(source["revision"]) is not int or not 0 <= source["revision"] <= 2 ** 63 - 1:
        raise ValueError("invalid global revision")
    _utc(source["generated_at"])
    values = source["announcements"]
    if not isinstance(values, list) or len(values) > MAX_SNAPSHOT_ANNOUNCEMENTS:
        raise ValueError("invalid announcement snapshot count")
    active = []
    ids = set()
    for value in values:
        if not isinstance(value, dict) or set(value) != {
                "announcement_id", "revision", "title", "message", "severity",
                "starts_at", "expires_at", "link"}:
            raise ValueError("invalid announcement snapshot item")
        announcement_id = value["announcement_id"]
        revision = value["revision"]
        if (not isinstance(announcement_id, str) or not _ID.fullmatch(announcement_id)
                or announcement_id in ids or type(revision) is not int
                or not 1 <= revision <= 2 ** 31 - 1):
            raise ValueError("invalid announcement identity")
        ids.add(announcement_id)
        validated = validate_fields({key: value[key] for key in
                                     ("title", "message", "severity", "starts_at", "expires_at", "link")})
        starts, expires = _utc(validated["starts_at"]), _utc(validated["expires_at"])
        if starts <= now.astimezone(timezone.utc) < expires:
            active.append(dict(value))
    active.sort(key=lambda value: (value["severity"] != "urgent", value["starts_at"],
                                   value["announcement_id"]), reverse=False)
    active = active[:MAX_ANNOUNCEMENTS]
    payload = {"schema_version": SCHEMA_VERSION, "revision": source["revision"],
               "generated_at": source["generated_at"], "announcements": active}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("announcement response exceeds limit")
    etag = '"' + hashlib.sha256(raw).hexdigest() + '"'
    boundaries = [_utc(value[key]) for value in values for key in ("starts_at", "expires_at")
                  if _utc(value[key]) > now.astimezone(timezone.utc)]
    max_age = 300 if not boundaries else max(0, min(300, int((min(boundaries) - now).total_seconds())))
    return raw, etag, max_age


def public_wsgi(snapshot, environ, start_response, now=None):
    method = environ.get("REQUEST_METHOD", "")
    if method not in {"GET", "HEAD"}:
        start_response("405 Method Not Allowed", [("Allow", "GET, HEAD"), ("Content-Length", "0")])
        return [b""]
    if environ.get("CONTENT_LENGTH") not in (None, "", "0") or environ.get("HTTP_TRANSFER_ENCODING"):
        start_response("400 Bad Request", [("Content-Length", "0")])
        return [b""]
    raw, etag, max_age = public_payload(snapshot, now)
    common = [("ETag", etag), ("Cache-Control", f"public, max-age={max_age}, must-revalidate")]
    if environ.get("HTTP_IF_NONE_MATCH") == etag:
        start_response("304 Not Modified", common)
        return [b""]
    headers = common + [("Content-Type", "application/json; charset=utf-8"),
                        ("Content-Length", str(len(raw)))]
    start_response("200 OK", headers)
    return [b"" if method == "HEAD" else raw]


def create_admin_blueprint(database, require_auth, authorized, actor_name,
                           public_snapshot=DEFAULT_PUBLIC_SNAPSHOT):
    """Flask adapter for the active admin boundary; imported only by that service."""
    from flask import Blueprint, jsonify, request

    blueprint = Blueprint("nocturne_plugin_announcements", __name__)
    store = AnnouncementStore(database, public_snapshot=public_snapshot)

    def publish_snapshot():
        write_public_snapshot(database, public_snapshot, store.clock())

    def denied():
        if authorized():
            return None
        return jsonify({"ok": False, "error": "Announcement administration requires event-admin access."}), 403

    def actor():
        value = actor_name()
        if not value:
            raise PermissionError("authenticated actor unavailable")
        return value

    def body(expected):
        if request.mimetype != "application/json" or request.content_length is None \
                or request.content_length < 1 or request.content_length > 4096:
            raise ValueError("invalid request body")
        value = strict_json(request.get_data(cache=False, as_text=True))
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("invalid request fields")
        return value

    def failure(error):
        if isinstance(error, KeyError):
            return jsonify({"ok": False, "error": "Announcement not found."}), 404
        code = 403 if isinstance(error, PermissionError) else (409 if isinstance(error, RevisionConflict) else 400)
        return jsonify({"ok": False, "error": str(error)}), code

    @blueprint.route("/admin/api/nocturne/plugin-announcements", methods=["GET"])
    @require_auth
    def list_announcements():
        rejection = denied()
        if rejection:
            return rejection
        publish_snapshot()
        return jsonify({"ok": True, "schema_version": SCHEMA_VERSION,
                        "announcements": store.list_all(), "audit": store.list_audit()})

    @blueprint.route("/admin/api/nocturne/plugin-announcements", methods=["POST"])
    @require_auth
    def create_announcement():
        rejection = denied()
        if rejection:
            return rejection
        try:
            value = body({"announcement"})
            created = store.create_draft(value["announcement"], actor())
            return jsonify({"ok": True, "announcement": created}), 201
        except (ValueError, KeyError, PermissionError, sqlite3.Error) as error:
            return failure(error)

    @blueprint.route("/admin/api/nocturne/plugin-announcements/<announcement_id>", methods=["PUT"])
    @require_auth
    def edit_announcement(announcement_id):
        rejection = denied()
        if rejection:
            return rejection
        try:
            value = body({"announcement", "expected_revision"})
            edited = store.edit_draft(announcement_id, value["announcement"], actor(),
                                      value["expected_revision"])
            return jsonify({"ok": True, "announcement": edited})
        except (ValueError, KeyError, PermissionError, sqlite3.Error) as error:
            return failure(error)

    @blueprint.route("/admin/api/nocturne/plugin-announcements/<announcement_id>/state", methods=["POST"])
    @require_auth
    def change_announcement(announcement_id):
        rejection = denied()
        if rejection:
            return rejection
        try:
            value = body({"action", "expected_revision"})
            changed = store.transition(announcement_id, value["action"], actor(),
                                       value["expected_revision"])
            return jsonify({"ok": True, "announcement": changed})
        except (ValueError, KeyError, PermissionError, sqlite3.Error) as error:
            return failure(error)

    return blueprint
