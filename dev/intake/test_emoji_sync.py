import hashlib
from io import BytesIO
import json
import logging
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image

from emoji_sync import (CANVAS_SIZE, EmojiSynchronizer, MAX_GENERATIONS,
                        SyncFailure, eligible_metadata, normalize_image,
                        parse_denylist, read_current)


def png(size=(12, 8), color=(120, 40, 200, 180)):
    output = BytesIO()
    Image.new("RGBA", size, color).save(output, "PNG")
    return output.getvalue()


def gif():
    output = BytesIO()
    first = Image.new("RGBA", (8, 12), (255, 0, 0, 255))
    second = Image.new("RGBA", (8, 12), (0, 0, 255, 255))
    first.save(output, "GIF", save_all=True, append_images=[second], duration=100, loop=0)
    return output.getvalue()


class FakeTransport:
    def __init__(self, values=None):
        self.values = [] if values is None else values
        self.assets = {}
        self.list_status = 200
        self.list_headers = {"Content-Type": "application/json"}
        self.failure = None
        self.requests = []

    def list_emojis(self, guild_id, token):
        self.requests.append(("list", guild_id, token))
        if self.failure:
            raise self.failure
        return self.list_status, self.list_headers, json.dumps(self.values).encode()

    def fetch_asset(self, emoji_id, animated):
        self.requests.append(("asset", emoji_id, animated))
        value = self.assets[emoji_id]
        if isinstance(value, Exception):
            raise value
        return value


def item(identifier, name, **changes):
    value = {"id": str(identifier), "name": name, "animated": False,
             "available": True, "managed": False, "roles": []}
    value.update(changes)
    return value


class EmojiSynchronizerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.transport = FakeTransport()
        self.sync = EmojiSynchronizer(self.root, self.transport,
                                      clock=lambda: __import__("datetime").datetime(
                                          2026, 9, 28, 20, 0,
                                          tzinfo=__import__("datetime").timezone.utc))

    def configure(self, values):
        self.transport.values = values
        for value in values:
            if value.get("id") and value["id"] not in self.transport.assets:
                animated = value.get("animated", False)
                self.transport.assets[value["id"]] = (
                    200, {"Content-Type": "image/gif" if animated else "image/png"},
                    gif() if animated else png())

    def test_initial_add_rename_delete_and_deterministic_unchanged(self):
        self.configure([item(10, "wave")])
        first = self.sync.synchronize("123", "fixture-token")
        self.assertEqual("published", first["status"])
        initial = read_current(self.root)
        self.assertEqual(["wave"], [value["name"] for value in initial["emojis"]])
        self.assertNotIn("fixture-token", json.dumps(initial))

        unchanged = self.sync.synchronize("123", "fixture-token")
        self.assertEqual(("unchanged", first["revision"]),
                         (unchanged["status"], unchanged["revision"]))
        self.configure([item(10, "renamed"), item(11, "new_one")])
        renamed = self.sync.synchronize("123", "fixture-token")
        self.assertEqual(["new_one", "renamed"],
                         [value["name"] for value in read_current(self.root)["emojis"]])
        self.configure([item(11, "new_one")])
        deleted = self.sync.synchronize("123", "fixture-token")
        self.assertNotEqual(renamed["revision"], deleted["revision"])
        self.assertEqual(["new_one"], [value["name"] for value in read_current(self.root)["emojis"]])

    def test_valid_empty_is_published_but_failure_preserves_last_generation(self):
        self.configure([item(10, "wave")])
        original = self.sync.synchronize("123", "fixture-token")["revision"]
        self.transport.values = []
        empty = self.sync.synchronize("123", "fixture-token")
        self.assertEqual("ok_empty", read_current(self.root)["source_status"])
        self.assertNotEqual(original, empty["revision"])
        empty_revision = empty["revision"]
        self.transport.failure = SyncFailure("transport_error")
        with self.assertRaises(SyncFailure):
            self.sync.synchronize("123", "fixture-token")
        self.assertEqual(empty_revision, read_current(self.root)["revision"])

    def test_rate_limit_is_bounded_and_does_not_retry(self):
        self.transport.list_status = 429
        self.transport.list_headers = {"Content-Type": "application/json", "Retry-After": "30"}
        with self.assertRaises(SyncFailure) as raised:
            self.sync.synchronize("123", "fixture-token")
        self.assertEqual(("rate_limited", 30),
                         (raised.exception.category, raised.exception.retry_after))
        self.assertEqual(1, len(self.transport.requests))

    def test_malformed_json_duplicate_keys_and_invalid_root_fail_closed(self):
        for raw in (b'{"id":1,"id":2}', b'\xff', b'{}'):
            class Raw(FakeTransport):
                def list_emojis(self, guild_id, token):
                    return 200, {"Content-Type": "application/json"}, raw
            with self.subTest(raw=raw), self.assertRaises(SyncFailure):
                EmojiSynchronizer(self.root / hashlib.sha256(raw).hexdigest(), Raw()).synchronize(
                    "123", "fixture-token")

    def test_duplicate_ids_names_normalization_and_eligibility_filters(self):
        values = [item(1, "One"), item(2, "one"), item(3, "safe"), item(3, "other"),
                  item(4, "unavailable", available=False), item(5, "managed", managed=True),
                  item(6, "roles", roles=["not-published"]), item(7, "denied"),
                  item(8, "bad-name"), {"id": None, "name": "missing"}]
        accepted = eligible_metadata(json.dumps(values).encode(), parse_denylist(["denied"]))
        self.assertEqual([], accepted)

    def test_static_and_animated_first_frame_are_deterministic_twenty_pixel_pngs(self):
        static = normalize_image(png((7, 14)), "image/png", False)
        animated = normalize_image(gif(), "image/gif", True)
        self.assertEqual(static, normalize_image(png((7, 14)), "image/png", False))
        for raw in (static, animated):
            with Image.open(BytesIO(raw)) as image:
                self.assertEqual((CANVAS_SIZE, CANVAS_SIZE), image.size)
                self.assertEqual("RGBA", image.mode)
                self.assertEqual(1, getattr(image, "n_frames", 1))

    def test_corrupt_truncated_polyglot_oversized_and_wrong_mime_are_rejected(self):
        valid = png()
        cases = [(b"not-image", "image/png", False), (valid[:-5], "image/png", False),
                 (valid + b"payload", "image/png", False),
                 (b"x" * (256 * 1024 + 1), "image/png", False),
                 (valid, "image/gif", False)]
        for raw, mime, animated in cases:
            with self.subTest(size=len(raw)), self.assertRaises(SyncFailure):
                normalize_image(raw, mime, animated)
        huge = png((513, 1))
        with self.assertRaises(SyncFailure):
            normalize_image(huge, "image/png", False)

    def test_bad_assets_are_omitted_and_manifest_contains_only_public_fields(self):
        self.configure([item(1, "good"), item(2, "bad")])
        self.transport.assets["2"] = (200, {"Content-Type": "text/html"}, b"no")
        self.sync.synchronize("123", "fixture-token")
        manifest = read_current(self.root)
        self.assertEqual(["good"], [value["name"] for value in manifest["emojis"]])
        serialized = json.dumps(manifest).lower()
        for forbidden in ("token", "guild", "creator", "roles", "user", "cdn", "header"):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual({"name", "sha256", "width", "height", "byte_length",
                          "animated_source", "asset_path"}, set(manifest["emojis"][0]))

    def test_atomic_publication_interruption_keeps_last_current(self):
        self.configure([item(1, "first")])
        original = self.sync.synchronize("123", "fixture-token")["revision"]
        self.configure([item(1, "second")])
        real_replace = __import__("os").replace
        def interrupted(source, target):
            if Path(target).name == "current":
                raise OSError("fixture interruption")
            return real_replace(source, target)
        with patch("emoji_sync.os.replace", side_effect=interrupted), self.assertRaises(OSError):
            self.sync.synchronize("123", "fixture-token")
        self.assertEqual(original, read_current(self.root)["revision"])

    def test_concurrent_sync_is_excluded(self):
        self.configure([item(1, "one")])
        entered = threading.Event()
        release = threading.Event()
        parent = self.transport
        class Blocking(FakeTransport):
            def list_emojis(self, guild_id, token):
                entered.set()
                release.wait(2)
                return parent.list_emojis(guild_id, token)
            def fetch_asset(self, emoji_id, animated):
                return parent.fetch_asset(emoji_id, animated)
        blocking = EmojiSynchronizer(self.root, Blocking())
        thread = threading.Thread(target=lambda: blocking.synchronize("123", "fixture-token"))
        thread.start()
        self.assertTrue(entered.wait(1))
        with self.assertRaisesRegex(SyncFailure, "sync_in_progress"):
            self.sync.synchronize("123", "fixture-token")
        release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_generation_cleanup_is_bounded(self):
        for number in range(MAX_GENERATIONS + 3):
            self.configure([item(number + 1, f"emoji_{number}")])
            self.sync.synchronize("123", "fixture-token")
        generations = [path for path in (self.root / "generations").iterdir() if path.is_dir()]
        self.assertLessEqual(len(generations), MAX_GENERATIONS)

    def test_manifest_asset_digest_size_and_permissions(self):
        self.configure([item(1, "one")])
        self.sync.synchronize("123", "fixture-token")
        manifest = read_current(self.root)
        entry = manifest["emojis"][0]
        asset = self.root / "current" / "assets" / f'{entry["sha256"]}.png'
        raw = asset.read_bytes()
        self.assertEqual(entry["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(entry["byte_length"], len(raw))
        self.assertEqual(0o644, asset.stat().st_mode & 0o777)

    def test_exceptions_and_logs_do_not_include_token_or_headers(self):
        secret = "fixture-secret-must-not-leak"
        self.transport.failure = SyncFailure("transport_error")
        with self.assertLogs("nocturne-emoji-sync", logging.ERROR) as captured:
            try:
                self.sync.synchronize("123", secret)
            except SyncFailure as error:
                logging.getLogger("nocturne-emoji-sync").error("category=%s", error.category)
        self.assertNotIn(secret, "\n".join(captured.output))


if __name__ == "__main__":
    unittest.main()
