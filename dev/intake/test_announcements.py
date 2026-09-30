import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from contextlib import closing

from announcements import (
    MAX_ANNOUNCEMENTS,
    MAX_MESSAGE_CHARS,
    MAX_RESPONSE_BYTES,
    MAX_SNAPSHOT_ANNOUNCEMENTS,
    AnnouncementStore,
    RevisionConflict,
    create_admin_blueprint,
    install_schema,
    public_payload,
    public_wsgi,
    schema_state,
    validate_fields,
    write_public_snapshot,
    write_public_snapshot_from_connection,
)
from intake import create_app


UTC = timezone.utc


class AnnouncementsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "event_schedule.db"
        sqlite3.connect(self.database).close()
        install_schema(self.database)
        self.snapshot = Path(self.temp.name) / "public" / "announcements-v1.json"
        self.snapshot.parent.mkdir()
        self.now = datetime(2026, 9, 28, 20, 0, tzinfo=UTC)
        self.store = AnnouncementStore(self.database, lambda: self.now, self.snapshot)
        write_public_snapshot(self.database, self.snapshot, self.now)

    def fields(self, message="Clan event tonight", *, start=-60, end=3600, link=None):
        return {
            "title": "Nocturne news",
            "message": message,
            "severity": "notice",
            "starts_at": (self.now + timedelta(seconds=start)).isoformat(),
            "expires_at": (self.now + timedelta(seconds=end)).isoformat(),
            "link": link,
        }

    def publish(self, **changes):
        fields = self.fields()
        fields.update(changes)
        draft = self.store.create_draft(fields, "admin")
        return self.store.transition(draft["announcement_id"], "publish", "admin", draft["revision"])

    def test_schema_is_versioned_and_idempotent(self):
        self.assertEqual("already_applied", schema_state(self.database))
        install_schema(self.database)
        self.assertEqual("already_applied", schema_state(self.database))
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual(1, db.execute(
                "SELECT schema_version FROM plugin_announcement_meta").fetchone()[0])

    def test_public_filters_draft_future_expired_and_withdrawn(self):
        active = self.publish()
        self.store.create_draft(self.fields("draft"), "admin")
        self.publish(message="future", starts_at=(self.now + timedelta(hours=1)).isoformat(),
                     expires_at=(self.now + timedelta(hours=2)).isoformat())
        self.publish(message="expired", starts_at=(self.now - timedelta(hours=2)).isoformat(),
                     expires_at=(self.now - timedelta(hours=1)).isoformat())
        withdrawn = self.publish(message="withdrawn")
        self.store.transition(withdrawn["announcement_id"], "withdraw", "admin", withdrawn["revision"])
        payload = json.loads(public_payload(self.snapshot, self.now)[0])
        self.assertEqual([active["announcement_id"]],
                         [item["announcement_id"] for item in payload["announcements"]])
        self.assertFalse(any(item["message"] in {"draft", "future", "expired", "withdrawn"}
                             for item in payload["announcements"]))

    def test_precommit_withdraw_snapshot_is_fail_closed_if_database_rolls_back(self):
        active = self.publish(message="must not leak after withdrawal starts")
        old_revision = json.loads(self.snapshot.read_text())["revision"]
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE plugin_announcements SET state='withdrawn' WHERE announcement_id=?",
                       (active["announcement_id"],))
            write_public_snapshot_from_connection(db, self.snapshot, self.now)
            db.rollback()
        value = json.loads(public_payload(self.snapshot, self.now)[0])
        self.assertEqual(old_revision, value["revision"])
        self.assertEqual([], value["announcements"])
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual("published", db.execute(
                "SELECT state FROM plugin_announcements WHERE announcement_id=?",
                (active["announcement_id"],)).fetchone()[0])
        write_public_snapshot(self.database, self.snapshot, self.now)
        self.assertEqual(1, len(json.loads(public_payload(self.snapshot, self.now)[0])["announcements"]))

    def test_maximum_count_and_response_size_are_bounded(self):
        for number in range(MAX_ANNOUNCEMENTS + 2):
            self.publish(message=(str(number) + "x" * (MAX_MESSAGE_CHARS - 1)))
        raw, _, _ = public_payload(self.snapshot, self.now)
        self.assertEqual(MAX_ANNOUNCEMENTS, len(json.loads(raw)["announcements"]))
        self.assertLessEqual(len(raw), MAX_RESPONSE_BYTES)

    def test_snapshot_capacity_fails_before_state_or_audit_commit(self):
        for number in range(MAX_SNAPSHOT_ANNOUNCEMENTS):
            self.publish(message=f"scheduled {number}")
        draft = self.store.create_draft(self.fields("one too many"), "admin")
        with self.assertRaisesRegex(ValueError, "too many scheduled"):
            self.store.transition(draft["announcement_id"], "publish", "admin", 1)
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual("draft", db.execute(
                "SELECT state FROM plugin_announcements WHERE announcement_id=?",
                (draft["announcement_id"],)).fetchone()[0])
            self.assertEqual(MAX_SNAPSHOT_ANNOUNCEMENTS * 2 + 1, db.execute(
                "SELECT COUNT(*) FROM plugin_announcement_audit").fetchone()[0])

    def test_plain_text_and_unknown_fields_are_rejected(self):
        self.assertIn("one\ntwo\nthree\nfour",
                      validate_fields(self.fields("one\ntwo\nthree\nfour"))["message"])
        bad = ["x" * (MAX_MESSAGE_CHARS + 1), "<b>markup</b>", "<img=12>",
               "<script>alert(1)</script>",
               "[click](https://nocturne.events/)",
               "control\u0001text", "bidi\u202etext", "one\ntwo\nthree\nfour\nfive"]
        for message in bad:
            with self.subTest(message=repr(message)), self.assertRaises(ValueError):
                validate_fields(self.fields(message))
        extra = self.fields()
        extra["rsn"] = "must not be accepted"
        with self.assertRaises(ValueError):
            validate_fields(extra)

    def test_invalid_dates_and_field_types_fail_closed(self):
        invalid = [
            {"starts_at": "2026-09-30T12:00:00", "expires_at": "2026-10-01T12:00:00Z"},
            {"starts_at": "not-a-date"},
            {"starts_at": 123},
            {"starts_at": (self.now + timedelta(hours=2)).isoformat(),
             "expires_at": (self.now + timedelta(hours=1)).isoformat()},
            {"severity": 2},
            {"link": {"label": "open", "url": "https://nocturne.events/", "extra": True}},
        ]
        for changes in invalid:
            value = self.fields()
            value.update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_fields(value)

    def test_link_allowlist_is_exact_and_structured(self):
        allowed = self.fields(link={"label": "Event board", "url": "https://nocturne.events/event-board.html"})
        self.assertEqual(allowed["link"], validate_fields(allowed)["link"])
        for url in ("http://nocturne.events/", "https://evil.example/",
                    "https://nocturne.events/other", "https://nocturne.events/?next=evil",
                    "https://user@nocturne.events/"):
            value = self.fields(link={"label": "Open", "url": url})
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_fields(value)

    def test_etag_304_head_and_deterministic_unchanged_response(self):
        self.publish()
        first, etag, max_age = public_payload(self.snapshot, self.now)
        second, second_etag, second_max_age = public_payload(self.snapshot, self.now)
        self.assertEqual((first, etag, max_age), (second, second_etag, second_max_age))
        status, headers, body = self.wsgi("GET", HTTP_IF_NONE_MATCH=etag)
        self.assertEqual((304, b""), (status, body))
        self.assertEqual(etag, headers["ETag"])
        status, headers, body = self.wsgi("HEAD")
        self.assertEqual((200, b""), (status, body))
        self.assertEqual(str(len(first)), headers["Content-Length"])

    def test_public_route_rejects_mutation_and_request_bodies(self):
        before = hashlib.sha256(self.snapshot.read_bytes()).digest()
        self.assertEqual(405, self.wsgi("POST")[0])
        self.assertEqual(400, self.wsgi("GET", CONTENT_LENGTH="1")[0])
        self.assertEqual(200, self.wsgi("GET")[0])
        self.assertEqual(before, hashlib.sha256(self.snapshot.read_bytes()).digest())

    def test_isolated_intake_exposes_only_get_without_client_identity(self):
        state = Path(self.temp.name) / "intake"
        app = create_app(state, ["Test Account"], clock=lambda: self.now.timestamp(),
                         announcement_snapshot=self.snapshot)
        self.publish()
        captured = []
        environ = {"PATH_INFO": "/api/plugin/v1/announcements", "REQUEST_METHOD": "GET",
                   "wsgi.input": io.BytesIO(b"")}
        body = b"".join(app(environ, lambda status, headers: captured.append((status, headers))))
        self.assertEqual("200 OK", captured[0][0])
        value = json.loads(body)
        self.assertEqual(1, len(value["announcements"]))
        self.assertNotIn("Test Account", body.decode())

    def test_schema_version_mismatch_fails_closed(self):
        value = json.loads(self.snapshot.read_text())
        value["schema_version"] = 2
        self.snapshot.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError, "unsupported"):
            public_payload(self.snapshot, self.now)

    def test_change_and_audit_are_one_transaction_and_audit_is_append_only(self):
        draft = self.store.create_draft(self.fields(), "admin")
        with closing(sqlite3.connect(self.database)) as db:
            db.execute("""CREATE TRIGGER reject_next_audit BEFORE INSERT ON plugin_announcement_audit
                        BEGIN SELECT RAISE(ABORT,'simulated audit failure'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.edit_draft(draft["announcement_id"], self.fields("changed"), "admin", 1)
        with closing(sqlite3.connect(self.database)) as db:
            row = db.execute("SELECT revision,message FROM plugin_announcements WHERE announcement_id=?",
                             (draft["announcement_id"],)).fetchone()
            self.assertEqual((1, "Clan event tonight"), row)
            db.execute("DROP TRIGGER reject_next_audit")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                db.execute("UPDATE plugin_announcement_audit SET actor='other'")
            with self.assertRaisesRegex(sqlite3.IntegrityError, "append-only"):
                db.execute("DELETE FROM plugin_announcement_audit")

    def test_revisions_are_monotonic_and_revised_draft_is_audited(self):
        draft = self.store.create_draft(self.fields(), "admin")
        edited = self.store.edit_draft(draft["announcement_id"], self.fields("changed"), "admin", 1)
        published = self.store.transition(draft["announcement_id"], "publish", "admin", 2)
        self.assertEqual((1, 2, 3), (draft["revision"], edited["revision"], published["revision"]))
        with closing(sqlite3.connect(self.database)) as db:
            self.assertEqual([1, 2, 3], [row[0] for row in db.execute(
                "SELECT global_revision FROM plugin_announcement_audit ORDER BY audit_id")])

    def test_public_payload_has_no_identity_telemetry_or_receipt_fields(self):
        self.publish()
        text = public_payload(self.snapshot, self.now)[0].decode()
        for forbidden in ("rsn", "account", "profile", "chat", "raid", "telemetry",
                          "read_receipt", "actor", "created_by", "updated_by", "audit"):
            self.assertNotIn(forbidden, text.lower())

    def test_admin_blueprint_requires_auth_and_role_and_rejects_unknown_fields(self):
        try:
            from flask import Flask, jsonify, request
        except ImportError:
            self.skipTest("Flask is exercised with the active admin interpreter")

        app = Flask(__name__)

        def require_auth(function):
            @wraps(function)
            def wrapped(*args, **kwargs):
                if request.headers.get("X-Test-Auth") != "yes":
                    return jsonify({"ok": False}), 401
                return function(*args, **kwargs)
            return wrapped

        app.register_blueprint(create_admin_blueprint(
            self.database, require_auth,
            lambda: request.headers.get("X-Test-Role") == "eventadmin",
            lambda: "test-admin", self.snapshot))
        client = app.test_client()
        path = "/admin/api/nocturne/plugin-announcements"
        self.assertEqual(401, client.get(path).status_code)
        self.assertEqual(403, client.get(path, headers={"X-Test-Auth": "yes"}).status_code)
        headers = {"X-Test-Auth": "yes", "X-Test-Role": "eventadmin"}
        self.assertEqual(400, client.post(path, headers={**headers, "Origin": "https://nocturne.events",
                                                          "Content-Type": "application/json"},
                                         data=b"{" + b" " * 4096).status_code)
        self.assertEqual(400, client.post(path, headers={**headers, "Origin": "https://nocturne.events"},
                                         json={"announcement": self.fields(), "extra": True}).status_code)
        self.assertEqual(403, client.post(path, headers=headers,
                                          json={"announcement": self.fields()}).status_code)
        self.assertEqual(403, client.post(path, headers={**headers, "Origin": "https://nocturne.events.attacker.test"},
                                          json={"announcement": self.fields()}).status_code)
        headers["Origin"] = "https://nocturne.events"
        created = client.post(path, headers=headers, json={"announcement": self.fields()})
        self.assertEqual(201, created.status_code)
        self.assertEqual(1, len(client.get(path, headers=headers).get_json()["audit"]))

    def test_duplicate_json_keys_and_stale_concurrent_edits_fail_closed(self):
        try:
            from flask import Flask, request
        except ImportError:
            self.skipTest("Flask is exercised with the active admin interpreter")
        app = Flask(__name__)
        app.register_blueprint(create_admin_blueprint(
            self.database, lambda function: function, lambda: True, lambda: "admin", self.snapshot))
        client = app.test_client()
        path = "/admin/api/nocturne/plugin-announcements"
        duplicate = b'{"announcement":{},"announcement":{}}'
        self.assertEqual(400, client.post(path, data=duplicate,
                                         content_type="application/json",
                                         headers={"Origin": "https://nocturne.events"}).status_code)
        draft = self.store.create_draft(self.fields(), "admin")
        edited = self.store.edit_draft(draft["announcement_id"], self.fields("first"), "admin", 1)
        self.assertEqual(2, edited["revision"])
        with self.assertRaises(RevisionConflict):
            self.store.edit_draft(draft["announcement_id"], self.fields("stale"), "admin", 1)

    def test_authenticated_admin_create_edit_publish_withdraw_and_exact_snapshot(self):
        try:
            from flask import Flask, jsonify, request
        except ImportError:
            self.skipTest("Flask is exercised with the active admin interpreter")
        app = Flask(__name__)

        def require_auth(function):
            @wraps(function)
            def wrapped(*args, **kwargs):
                if request.headers.get("X-Test-Auth") != "yes":
                    return jsonify({"ok": False}), 401
                return function(*args, **kwargs)
            return wrapped

        app.register_blueprint(create_admin_blueprint(
            self.database, require_auth,
            lambda: request.headers.get("X-Test-Role") == "eventadmin",
            lambda: "verified-operator", self.snapshot))
        client = app.test_client()
        path = "/admin/api/nocturne/plugin-announcements"
        headers = {"X-Test-Auth": "yes", "X-Test-Role": "eventadmin",
                   "Origin": "https://nocturne.events"}
        current = datetime.now(UTC)
        fields = {"title": "Nocturne news", "message": "Clearly labeled temporary test announcement",
                  "severity": "notice", "starts_at": (current - timedelta(seconds=60)).isoformat(),
                  "expires_at": (current + timedelta(hours=1)).isoformat(), "link": None}
        created = client.post(path, headers=headers, json={"announcement": fields})
        self.assertEqual(201, created.status_code)
        item = created.get_json()["announcement"]
        edited = client.put(path + "/" + item["announcement_id"], headers=headers,
                            json={"announcement": {**fields, "message": "edited test text"},
                                  "expected_revision": item["revision"]})
        self.assertEqual(200, edited.status_code)
        item = edited.get_json()["announcement"]
        published = client.post(path + "/" + item["announcement_id"] + "/state", headers=headers,
                                json={"action": "publish", "expected_revision": item["revision"]})
        self.assertEqual(200, published.status_code)
        item = published.get_json()["announcement"]
        root = json.loads(self.snapshot.read_bytes())
        self.assertEqual({"schema_version", "revision", "generated_at", "announcements"}, set(root))
        self.assertEqual({"announcement_id", "revision", "title", "message", "severity",
                          "starts_at", "expires_at", "link"}, set(root["announcements"][0]))
        self.assertEqual("edited test text", root["announcements"][0]["message"])
        withdrawn = client.post(path + "/" + item["announcement_id"] + "/state", headers=headers,
                                json={"action": "withdraw", "expected_revision": item["revision"]})
        self.assertEqual(200, withdrawn.status_code)
        self.assertEqual([], json.loads(self.snapshot.read_bytes())["announcements"])
        audit = client.get(path, headers=headers).get_json()["audit"]
        self.assertEqual(["withdraw", "publish", "edit_draft", "create_draft"],
                         [entry["action"] for entry in audit])
        self.assertTrue(all(entry["actor"] == "verified-operator" for entry in audit))

    def test_announcement_blueprint_rejects_cross_site_reads_and_fetch_metadata(self):
        try:
            from flask import Flask, request
        except ImportError:
            self.skipTest("Flask is exercised with the active admin interpreter")
        app = Flask(__name__)
        app.register_blueprint(create_admin_blueprint(
            self.database, lambda function: function, lambda: True, lambda: "admin", self.snapshot))
        client = app.test_client()
        path = "/admin/api/nocturne/plugin-announcements"
        self.assertEqual(403, client.get(path, headers={"Origin": "https://nocturne.events.attacker.test"}).status_code)
        self.assertEqual(403, client.post(path, headers={"Origin": "https://nocturne.events",
                                                          "Sec-Fetch-Site": "cross-site"},
                                         json={"announcement": self.fields()}).status_code)

    def test_admin_blueprint_uses_isolated_publisher_with_server_generated_schema(self):
        try:
            from flask import Flask, request
        except ImportError:
            self.skipTest("Flask is exercised with the active admin interpreter")
        from announcements import validate_snapshot_bytes
        published = []
        app = Flask(__name__)
        app.register_blueprint(create_admin_blueprint(
            self.database, lambda function: function, lambda: True, lambda: "admin",
            snapshot_publisher=published.append))
        client = app.test_client()
        headers = {"Origin": "https://nocturne.events"}
        response = client.get("/admin/api/nocturne/plugin-announcements", headers=headers)
        self.assertEqual(200, response.status_code)
        self.assertTrue(published)
        for raw in published:
            snapshot = validate_snapshot_bytes(raw)
            self.assertEqual({"schema_version", "revision", "generated_at", "announcements"},
                             set(snapshot))
            self.assertNotIn("actor", snapshot)

    def test_atomic_public_snapshot_survives_database_replace_and_exposes_no_sqlite_files(self):
        old_inode = self.snapshot.stat().st_ino
        self.publish(message="replacement")
        self.assertNotEqual(old_inode, self.snapshot.stat().st_ino)
        self.assertEqual({"announcements-v1.json"}, {path.name for path in self.snapshot.parent.iterdir()})
        payload = json.loads(public_payload(self.snapshot, self.now)[0])
        self.assertEqual("replacement", payload["announcements"][0]["message"])

    def test_cache_lifetime_never_crosses_activation_or_expiry(self):
        self.publish(starts_at=(self.now - timedelta(seconds=60)).isoformat(),
                     expires_at=(self.now + timedelta(seconds=12)).isoformat())
        _raw, _etag, max_age = public_payload(self.snapshot, self.now)
        self.assertEqual(12, max_age)

    def wsgi(self, method, **extra):
        environ = {"REQUEST_METHOD": method, "wsgi.input": io.BytesIO(b"")}
        environ.update(extra)
        captured = []
        output = public_wsgi(self.snapshot, environ,
                             lambda status, headers: captured.append((status, dict(headers))), self.now)
        return int(captured[0][0].split()[0]), captured[0][1], b"".join(output)


if __name__ == "__main__":
    unittest.main()
