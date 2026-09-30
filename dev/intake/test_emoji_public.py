from io import BytesIO
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from emoji_public import MANIFEST_PATH, load_manifest, public_wsgi
from emoji_sync import EmojiSynchronizer, SyncFailure
from intake import create_app


def png():
    output = BytesIO()
    Image.new("RGBA", (10, 12), (20, 80, 160, 200)).save(output, "PNG")
    return output.getvalue()


def changed_stat(value, **changes):
    return os.stat_result((changes.get("st_mode", value.st_mode),
                           changes.get("st_ino", value.st_ino),
                           changes.get("st_dev", value.st_dev),
                           changes.get("st_nlink", value.st_nlink),
                           changes.get("st_uid", value.st_uid),
                           changes.get("st_gid", value.st_gid),
                           changes.get("st_size", value.st_size),
                           value.st_atime, value.st_mtime, value.st_ctime))


class FixtureTransport:
    def list_emojis(self, guild_id, token):
        value = [{"id": "1", "name": "wave", "animated": False,
                  "available": True, "managed": False, "roles": []}]
        return 200, {"Content-Type": "application/json"}, json.dumps(value).encode()

    def fetch_asset(self, emoji_id, animated):
        return 200, {"Content-Type": "image/png"}, png()


def request(app, path=MANIFEST_PATH, method="GET", headers=None, length=None):
    captured = {}
    environ = {"PATH_INFO": path, "REQUEST_METHOD": method,
               "CONTENT_LENGTH": "" if length is None else str(length),
               "wsgi.input": BytesIO(b"x" * (length or 0))}
    for key, value in (headers or {}).items():
        environ["HTTP_" + key.upper().replace("-", "_")] = value
    def start(status, response_headers):
        captured["status"] = status
        captured["headers"] = dict(response_headers)
    captured["body"] = b"".join(app(environ, start))
    return captured


class EmojiPublicTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.state = self.base / "private"
        self.root = self.base / "public"
        EmojiSynchronizer(self.state, self.root, FixtureTransport()).synchronize(
            "123", "fixture-token")
        self.app = lambda env, start: public_wsgi(self.root, env, start)

    def test_exact_systemd_public_alias_uses_verified_backing(self):
        parent = self.base / "systemd-var-lib"
        backing = parent / "private" / "nocturne-plugin-emoji-public"
        backing.parent.mkdir(parents=True, mode=0o700)
        EmojiSynchronizer(self.base / "alias-private", backing,
                          FixtureTransport()).synchronize("123", "fixture-token")
        alias = parent / "nocturne-plugin-emoji-public"
        alias.symlink_to("private/nocturne-plugin-emoji-public")
        real_lstat = Path.lstat

        def systemd_lstat(path):
            value = real_lstat(path)
            return changed_stat(value, st_uid=0, st_gid=0, st_nlink=1) \
                if path == alias else value

        with patch("emoji_public.SYSTEMD_PUBLIC_ALIAS", alias), \
                patch("emoji_public.SYSTEMD_PUBLIC_BACKING", backing), \
                patch.object(Path, "lstat", autospec=True,
                             side_effect=systemd_lstat):
            generation, value, _raw, _digests, _root_stat = load_manifest(alias)
        self.assertEqual(backing, generation.parents[1])
        self.assertEqual(1, len(value["emojis"]))

    def test_systemd_public_alias_shape_remains_fail_closed(self):
        for case in ("wrong_target", "not_symlink", "wrong_owner", "wrong_group",
                     "extra_link", "unexpected_resolved", "ambiguous_request"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as name:
                parent = Path(name) / "var" / "lib"
                private = parent / "private"
                private.mkdir(parents=True)
                alias = parent / "nocturne-plugin-emoji-public"
                backing = private / "nocturne-plugin-emoji-public"
                if case == "unexpected_resolved":
                    other = Path(name) / "other"
                    other.mkdir()
                    backing.symlink_to(other)
                    alias.symlink_to("private/nocturne-plugin-emoji-public")
                elif case == "wrong_target":
                    wrong = private / "wrong"
                    wrong.mkdir()
                    backing.mkdir()
                    alias.symlink_to("private/wrong")
                elif case == "not_symlink":
                    backing.mkdir()
                    alias.mkdir()
                else:
                    backing.mkdir()
                    alias.symlink_to("private/nocturne-plugin-emoji-public")
                real_lstat = Path.lstat

                def systemd_lstat(path):
                    value = real_lstat(path)
                    if path != alias:
                        return value
                    if case == "wrong_owner":
                        return changed_stat(value, st_uid=1, st_gid=0, st_nlink=1)
                    if case == "wrong_group":
                        return changed_stat(value, st_uid=0, st_gid=1, st_nlink=1)
                    if case == "extra_link":
                        return changed_stat(value, st_uid=0, st_gid=0, st_nlink=2)
                    return changed_stat(value, st_uid=0, st_gid=0, st_nlink=1)

                requested = (f"{alias.parent}/./{alias.name}"
                             if case == "ambiguous_request" else alias)
                with patch("emoji_public.SYSTEMD_PUBLIC_ALIAS", alias), \
                        patch("emoji_public.SYSTEMD_PUBLIC_BACKING", backing), \
                        patch.object(Path, "lstat", autospec=True,
                                     side_effect=systemd_lstat), \
                        self.assertRaisesRegex(ValueError, "emoji public root"):
                    load_manifest(requested)

    def test_direct_public_root_behavior_is_unchanged(self):
        generation, value, _raw, _digests, _root_stat = load_manifest(self.root)
        self.assertEqual(self.root, generation.parents[1])
        self.assertEqual(1, len(value["emojis"]))

    def test_manifest_get_head_etag_304_and_deterministic_body(self):
        first = request(self.app)
        second = request(self.app)
        self.assertEqual("200 OK", first["status"])
        self.assertEqual(first["body"], second["body"])
        self.assertEqual("application/json; charset=utf-8", first["headers"]["Content-Type"])
        self.assertIn("must-revalidate", first["headers"]["Cache-Control"])
        head = request(self.app, method="HEAD")
        self.assertEqual(("200 OK", b""), (head["status"], head["body"]))
        cached = request(self.app, headers={"If-None-Match": first["headers"]["ETag"]})
        self.assertEqual(("304 Not Modified", b""), (cached["status"], cached["body"]))
        self.assertEqual(first["headers"]["ETag"], cached["headers"]["ETag"])

    def test_asset_is_digest_addressed_verified_and_immutable(self):
        manifest = json.loads(request(self.app)["body"])
        entry = manifest["emojis"][0]
        result = request(self.app, entry["asset_path"])
        self.assertEqual("200 OK", result["status"])
        self.assertEqual("image/png", result["headers"]["Content-Type"])
        self.assertIn("immutable", result["headers"]["Cache-Control"])
        self.assertEqual(entry["sha256"], hashlib.sha256(result["body"]).hexdigest())
        cached = request(self.app, entry["asset_path"],
                         headers={"If-None-Match": result["headers"]["ETag"]})
        self.assertEqual(("304 Not Modified", b""), (cached["status"], cached["body"]))

    def test_body_and_mutating_methods_are_rejected(self):
        self.assertEqual("400 Bad Request", request(self.app, length=1)["status"])
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            self.assertEqual("405 Method Not Allowed", request(self.app, method=method)["status"])

    def test_traversal_unknown_digest_and_arbitrary_paths_are_not_served(self):
        paths = [MANIFEST_PATH + "/assets/../manifest.json",
                 MANIFEST_PATH + "/assets/" + "f" * 64 + ".png",
                 MANIFEST_PATH + "/assets/not-a-digest.png", MANIFEST_PATH + "/anything"]
        for path in paths:
            with self.subTest(path=path):
                self.assertEqual("404 Not Found", request(self.app, path)["status"])

    def test_missing_malformed_and_digest_mismatched_generation_fail_closed(self):
        current = self.root / "current"
        target = current.readlink()
        manifest = self.root / target / "manifest.json"
        original = manifest.read_bytes()
        for raw in (b"{}", original + b"x"):
            manifest.write_bytes(raw)
            self.assertEqual("503 Service Unavailable", request(self.app)["status"])
        manifest.write_bytes(original)
        current.unlink()
        self.assertEqual("503 Service Unavailable", request(self.app)["status"])

    def test_hardlinked_manifest_and_asset_are_never_served(self):
        current = self.root / "current"
        generation = (self.root / current.readlink())
        manifest = generation / "manifest.json"
        manifest_copy = self.root / "manifest-copy"
        manifest_copy.write_bytes(manifest.read_bytes())
        manifest_copy.chmod(0o644)
        manifest.unlink()
        __import__("os").link(manifest_copy, manifest)
        self.assertEqual("503 Service Unavailable", request(self.app)["status"])

        manifest.unlink()
        manifest.write_bytes(manifest_copy.read_bytes())
        manifest.chmod(0o644)
        entry = json.loads(request(self.app)["body"])["emojis"][0]
        asset = generation / "assets" / f'{entry["sha256"]}.png'
        asset_copy = self.root / "asset-copy"
        asset_copy.write_bytes(asset.read_bytes())
        asset_copy.chmod(0o644)
        asset.unlink()
        __import__("os").link(asset_copy, asset)
        self.assertEqual("404 Not Found", request(self.app, entry["asset_path"])["status"])

    def test_public_fields_contain_no_identity_or_discord_transport_data(self):
        value = json.loads(request(self.app)["body"])
        serialized = json.dumps(value).lower()
        for field in ("token", "guild", "creator", "user", "role", "cdn", "rsn",
                      "profile", "chat", "telemetry", "receipt"):
            self.assertNotIn(field, serialized)

    def test_intake_integration_routes_only_manifest_and_digest_assets(self):
        state = self.root / "intake"
        app = create_app(state, ["Tester"], handoff=lambda _value: None,
                         presence_identity_resolver=lambda _value: None,
                         emoji_public_root=self.root)
        manifest = request(app)
        self.assertEqual("200 OK", manifest["status"])
        entry = json.loads(manifest["body"])["emojis"][0]
        self.assertEqual("200 OK", request(app, entry["asset_path"])["status"])

    def test_missing_emoji_mirror_does_not_disable_intake(self):
        missing = self.root / "not-yet-synchronized"
        state = self.root / "intake-without-emojis"
        app = create_app(state, ["Tester"], handoff=lambda _value: None,
                         presence_identity_resolver=lambda _value: None,
                         emoji_public_root=missing)
        self.assertEqual("503 Service Unavailable", request(app)["status"])
        self.assertEqual("405 Method Not Allowed",
                         request(app, "/api/plugin/dev/drops", method="GET")["status"])

    def test_sync_failure_initializes_public_root_before_intake_start(self):
        class FailureTransport:
            def list_emojis(self, _guild_id, _token):
                raise SyncFailure("transport_error")

        state = self.base / "failure-private"
        public = self.base / "failure-public"
        with self.assertRaisesRegex(SyncFailure, "transport_error"):
            EmojiSynchronizer(state, public, FailureTransport()).synchronize(
                "123", "fixture-token")
        self.assertTrue(public.is_dir())
        app = create_app(self.base / "failure-intake", ["Tester"],
                         handoff=lambda _value: None,
                         presence_identity_resolver=lambda _value: None,
                         emoji_public_root=public)
        self.assertEqual("503 Service Unavailable", request(app)["status"])
        self.assertEqual("405 Method Not Allowed",
                         request(app, "/api/plugin/dev/drops", method="GET")["status"])


if __name__ == "__main__":
    unittest.main()
