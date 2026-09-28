"""Versioned clan-announcement storage and bounded public/admin interfaces."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import re
import sqlite3
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
SEVERITIES = frozenset({"info", "notice", "warning", "urgent"})
STATES = frozenset({"draft", "published", "withdrawn", "expired"})
ALLOWED_LINKS = frozenset({
    ("nocturne.events", "/"),
    ("nocturne.events", "/event-board.html"),
})
_MARKUP = re.compile(r"<[^>]*>|\[[^\]]*\]\([^)]*\)|!\[[^\]]*\]|```|`[^`]*`")
_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")

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
    if any(unicodedata.category(character).startswith("C") for character in value):
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
    def __init__(self, database, clock=None):
        self.database = str(database)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self):
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("clock must return an aware datetime")
        return _timestamp(value)

    def _actor(self, actor):
        return _plain(actor, "actor", 128)

    def _change(self, announcement_id, action, actor, fields=None):
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
            return after

    def create_draft(self, fields, actor):
        return self._change(None, "create_draft", actor, fields)

    def edit_draft(self, announcement_id, fields, actor):
        return self._change(_plain(announcement_id, "announcement id", MAX_ID_CHARS),
                            "edit_draft", actor, fields)

    def transition(self, announcement_id, action, actor):
        if action not in {"publish", "withdraw", "expire"}:
            raise ValueError("invalid action")
        return self._change(_plain(announcement_id, "announcement id", MAX_ID_CHARS), action, actor)

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


def public_payload(database, now=None):
    """Read with a query-only connection; this function cannot mutate public state."""
    now = now or datetime.now(timezone.utc)
    current = _timestamp(now)
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        AnnouncementStore._require_schema(db)
        global_revision, generated = db.execute(
            "SELECT global_revision,generated_at FROM plugin_announcement_meta WHERE singleton=1").fetchone()
        rows = db.execute(_SELECT + " WHERE state='published' AND starts_at<=? AND expires_at>? "
                          "ORDER BY severity='urgent' DESC,starts_at DESC,announcement_id LIMIT ?",
                          (current, current, MAX_ANNOUNCEMENTS)).fetchall()
    if type(global_revision) is not int or global_revision < 0:
        raise ValueError("invalid global revision")
    _utc(generated)
    announcements = []
    for row in rows:
        value = _row(row)
        if not _ID.fullmatch(value["announcement_id"]):
            raise ValueError("invalid announcement id")
        if type(value["revision"]) is not int or value["revision"] < 1:
            raise ValueError("invalid announcement revision")
        validate_fields({key: value[key] for key in
                         ("title", "message", "severity", "starts_at", "expires_at", "link")})
        announcements.append({key: value[key] for key in
                              ("announcement_id", "revision", "title", "message", "severity",
                               "starts_at", "expires_at", "link")})
    payload = {"schema_version": SCHEMA_VERSION, "revision": global_revision,
               "generated_at": generated, "announcements": announcements}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("announcement response exceeds limit")
    etag = '"' + hashlib.sha256(raw).hexdigest() + '"'
    return raw, etag


def public_wsgi(database, environ, start_response, now=None):
    method = environ.get("REQUEST_METHOD", "")
    if method not in {"GET", "HEAD"}:
        start_response("405 Method Not Allowed", [("Allow", "GET, HEAD"), ("Content-Length", "0")])
        return [b""]
    if environ.get("CONTENT_LENGTH") not in (None, "", "0") or environ.get("HTTP_TRANSFER_ENCODING"):
        start_response("400 Bad Request", [("Content-Length", "0")])
        return [b""]
    raw, etag = public_payload(database, now)
    common = [("ETag", etag), ("Cache-Control", "public, max-age=300, must-revalidate")]
    if environ.get("HTTP_IF_NONE_MATCH") == etag:
        start_response("304 Not Modified", common)
        return [b""]
    headers = common + [("Content-Type", "application/json; charset=utf-8"),
                        ("Content-Length", str(len(raw)))]
    start_response("200 OK", headers)
    return [b"" if method == "HEAD" else raw]


def create_admin_blueprint(database, require_auth, authorized, actor_name):
    """Flask adapter for the active admin boundary; imported only by that service."""
    from flask import Blueprint, jsonify, request

    blueprint = Blueprint("nocturne_plugin_announcements", __name__)
    store = AnnouncementStore(database)

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
        value = request.get_json(silent=True)
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("invalid request fields")
        return value

    def failure(error):
        if isinstance(error, KeyError):
            return jsonify({"ok": False, "error": "Announcement not found."}), 404
        code = 403 if isinstance(error, PermissionError) else 400
        return jsonify({"ok": False, "error": str(error)}), code

    @blueprint.route("/admin/api/nocturne/plugin-announcements", methods=["GET"])
    @require_auth
    def list_announcements():
        rejection = denied()
        if rejection:
            return rejection
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
            value = body({"announcement"})
            edited = store.edit_draft(announcement_id, value["announcement"], actor())
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
            value = body({"action"})
            changed = store.transition(announcement_id, value["action"], actor())
            return jsonify({"ok": True, "announcement": changed})
        except (ValueError, KeyError, PermissionError, sqlite3.Error) as error:
            return failure(error)

    return blueprint
