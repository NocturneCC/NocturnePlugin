import hashlib
from io import BytesIO
import json
import logging
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler

from PIL import Image

from emoji_sync import (CANVAS_SIZE, DISCORD_USER_AGENT, DiscordTransport,
                        EmojiSynchronizer, MAX_GENERATIONS, NoRedirect,
                        SyncFailure, _read_config, _read_credential,
                        eligible_metadata, initialize_private_root, initialize_public_root,
                        initialize_roots, main, normalize_image, parse_denylist,
                        read_current)


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


def changed_stat(value, **changes):
    return os.stat_result((changes.get("st_mode", value.st_mode),
                           changes.get("st_ino", value.st_ino),
                           changes.get("st_dev", value.st_dev),
                           changes.get("st_nlink", value.st_nlink),
                           changes.get("st_uid", value.st_uid),
                           changes.get("st_gid", value.st_gid),
                           changes.get("st_size", value.st_size),
                           value.st_atime, value.st_mtime, value.st_ctime))


class EmojiSynchronizerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "private"
        self.public = self.root / "public"
        self.transport = FakeTransport()
        self.sync = EmojiSynchronizer(self.state, self.public, self.transport,
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
        initial = read_current(self.public)
        self.assertEqual(["wave"], [value["name"] for value in initial["emojis"]])
        self.assertNotIn("fixture-token", json.dumps(initial))

        unchanged = self.sync.synchronize("123", "fixture-token")
        self.assertEqual(("unchanged", first["revision"]),
                         (unchanged["status"], unchanged["revision"]))
        self.configure([item(10, "renamed"), item(11, "new_one")])
        renamed = self.sync.synchronize("123", "fixture-token")
        self.assertEqual(["new_one", "renamed"],
                         [value["name"] for value in read_current(self.public)["emojis"]])
        self.configure([item(11, "new_one")])
        deleted = self.sync.synchronize("123", "fixture-token")
        self.assertNotEqual(renamed["revision"], deleted["revision"])
        self.assertEqual(["new_one"], [value["name"] for value in read_current(self.public)["emojis"]])

    def test_valid_empty_is_published_but_failure_preserves_last_generation(self):
        self.configure([item(10, "wave")])
        original = self.sync.synchronize("123", "fixture-token")["revision"]
        self.transport.values = []
        empty = self.sync.synchronize("123", "fixture-token")
        self.assertEqual("ok_empty", read_current(self.public)["source_status"])
        self.assertNotEqual(original, empty["revision"])
        empty_revision = empty["revision"]
        self.transport.failure = SyncFailure("transport_error")
        with self.assertRaises(SyncFailure):
            self.sync.synchronize("123", "fixture-token")
        self.assertEqual(empty_revision, read_current(self.public)["revision"])

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
                digest = hashlib.sha256(raw).hexdigest()
                EmojiSynchronizer(self.root / ("private-" + digest),
                                  self.root / ("public-" + digest), Raw()).synchronize(
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
                 (gif() + b"payload;", "image/gif", True),
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
        manifest = read_current(self.public)
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
        self.assertEqual(original, read_current(self.public)["revision"])

    def test_every_publication_boundary_preserves_last_complete_generation(self):
        self.configure([item(1, "first")])
        original = self.sync.synchronize("123", "fixture-token")["revision"]
        real_write = __import__("emoji_sync")._write
        real_replace = os.replace

        def fail_asset(path, raw, mode=0o644):
            if Path(path).suffix == ".png":
                raise OSError("asset fixture interruption")
            return real_write(path, raw, mode)

        def fail_manifest(path, raw, mode=0o644):
            if Path(path).name == "manifest.json":
                raise OSError("manifest fixture interruption")
            return real_write(path, raw, mode)

        def fail_generation(source, target):
            if Path(target).parent.name == "generations":
                raise OSError("generation fixture interruption")
            return real_replace(source, target)

        def fail_current(source, target):
            if Path(target).name == "current":
                raise OSError("current fixture interruption")
            return real_replace(source, target)

        for name, target, replacement in (
                ("asset", "emoji_sync._write", fail_asset),
                ("manifest", "emoji_sync._write", fail_manifest),
                ("generation", "emoji_sync.os.replace", fail_generation),
                ("current", "emoji_sync.os.replace", fail_current)):
            with self.subTest(boundary=name):
                self.configure([item(1, f"second_{name}")])
                with patch(target, side_effect=replacement), self.assertRaises(OSError):
                    self.sync.synchronize("123", "fixture-token")
                self.assertEqual(original, read_current(self.public)["revision"])

    def test_preexisting_generation_hardlinks_mounts_and_lock_links_fail_closed(self):
        self.configure([item(1, "one")])
        revision = self.sync.synchronize("123", "fixture-token")["revision"]
        generation = self.public / "generations" / revision
        manifest = generation / "manifest.json"
        saved = self.public / "saved-manifest"
        saved.write_bytes(manifest.read_bytes())
        saved.chmod(0o644)
        manifest.unlink()
        os.link(saved, manifest)
        with self.assertRaisesRegex(SyncFailure, "unsafe_generation_target"):
            self.sync.synchronize("123", "fixture-token")

        manifest.unlink()
        manifest.write_bytes(saved.read_bytes())
        manifest.chmod(0o644)
        with patch("emoji_sync.os.path.ismount", side_effect=lambda path: Path(path) == generation), \
                self.assertRaisesRegex(SyncFailure, "unsafe_generation_target"):
            self.sync.synchronize("123", "fixture-token")

        other = Path(self.temp.name) / "other-private"
        other.mkdir(mode=0o700)
        other_public = Path(self.temp.name) / "other-public"
        victim = other / "victim"
        victim.write_text("unchanged")
        (other / ".sync.lock").symlink_to(victim)
        sync = EmojiSynchronizer(other, other_public, self.transport)
        with self.assertRaisesRegex(SyncFailure, "unsafe_sync_lock"):
            sync.synchronize("123", "fixture-token")
        self.assertEqual("unchanged", victim.read_text())

    def test_generation_directory_is_verified_before_chmod(self):
        self.configure([item(1, "one")])
        initialize_private_root(self.state)
        initialize_public_root(self.public)
        victim = self.public / "victim"
        victim.mkdir(mode=0o700)
        (self.public / "generations").symlink_to(victim, target_is_directory=True)
        with self.assertRaisesRegex(SyncFailure, "unsafe_generation_directory"):
            self.sync.synchronize("123", "fixture-token")
        self.assertEqual(0o700, victim.stat().st_mode & 0o777)

        (self.public / "generations").unlink()
        generations = self.public / "generations"
        generations.mkdir(mode=0o700)
        with patch("emoji_sync.os.path.ismount",
                   side_effect=lambda path: Path(path) == generations), \
                self.assertRaisesRegex(SyncFailure, "unsafe_generation_directory"):
            self.sync.synchronize("123", "fixture-token")
        self.assertEqual(0o700, generations.stat().st_mode & 0o777)

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
        blocking = EmojiSynchronizer(self.state, self.public, Blocking())
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
        generations = [path for path in (self.public / "generations").iterdir() if path.is_dir()]
        self.assertLessEqual(len(generations), MAX_GENERATIONS)

    def test_manifest_asset_digest_size_and_permissions(self):
        self.configure([item(1, "one")])
        previous = os.umask(0o077)
        try:
            self.sync.synchronize("123", "fixture-token")
        finally:
            os.umask(previous)
        manifest = read_current(self.public)
        entry = manifest["emojis"][0]
        generation = (self.public / "current").resolve()
        manifest_path = generation / "manifest.json"
        asset = self.public / "current" / "assets" / f'{entry["sha256"]}.png'
        raw = asset.read_bytes()
        self.assertEqual(entry["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(entry["byte_length"], len(raw))
        self.assertEqual(0o644, asset.stat().st_mode & 0o777)
        self.assertEqual(0o700, self.state.stat().st_mode & 0o777)
        self.assertEqual(0o755, self.public.stat().st_mode & 0o777)
        self.assertEqual(0o755, (self.public / "generations").stat().st_mode & 0o777)
        self.assertEqual(0o755, generation.stat().st_mode & 0o777)
        self.assertEqual(0o755, (generation / "assets").stat().st_mode & 0o777)
        self.assertEqual(0o644, manifest_path.stat().st_mode & 0o777)
        self.assertEqual(0o644, asset.stat().st_mode & 0o777)
        for directory in (self.public, self.public / "generations", generation,
                          generation / "assets"):
            self.assertTrue(directory.stat().st_mode & stat.S_IXOTH,
                            f"distinct UID cannot traverse {directory}")
        for value in (manifest_path, asset):
            self.assertTrue(value.stat().st_mode & stat.S_IROTH,
                            f"distinct UID cannot read {value}")
        for value in (self.public, self.public / "generations", generation,
                      generation / "assets", manifest_path, asset):
            self.assertFalse({"system.posix_acl_access", "system.posix_acl_default"}
                             .intersection(os.listxattr(value, follow_symlinks=False)))

    def test_first_run_initializes_separate_private_and_public_roots(self):
        state = self.root / "first-private"
        public = self.root / "first-public"
        self.configure([item(1, "one")])
        result = EmojiSynchronizer(state, public, self.transport).synchronize(
            "123", "fixture-token")
        self.assertEqual("published", result["status"])
        self.assertEqual(0o700, stat.S_IMODE(state.lstat().st_mode))
        metadata = public.lstat()
        self.assertEqual((os.geteuid(), os.getegid(), 0o755),
                         (metadata.st_uid, metadata.st_gid,
                          stat.S_IMODE(metadata.st_mode)))
        self.assertIsNotNone(read_current(public))

    def test_existing_private_root_and_absent_public_root_are_supported(self):
        state = self.root / "existing-private"
        initialize_private_root(state)
        public = self.root / "new-public"
        self.configure([item(1, "one")])
        result = EmojiSynchronizer(state, public, self.transport).synchronize(
            "123", "fixture-token")
        self.assertEqual("published", result["status"])
        self.assertEqual(0o700, stat.S_IMODE(state.stat().st_mode))
        self.assertEqual(0o755, stat.S_IMODE(public.stat().st_mode))
        self.assertIsNotNone(read_current(public))

    def test_private_and_public_roots_must_be_independent(self):
        private = self.root / "layout-private"
        for public in (private, private / "public"):
            with self.subTest(public=public), \
                    self.assertRaisesRegex(SyncFailure, "unsafe_root_layout"):
                initialize_roots(private, public)
            with self.assertRaisesRegex(ValueError, "must be separate"):
                EmojiSynchronizer(private, public, self.transport)

    def test_exact_systemd_dynamic_user_state_links_are_verified(self):
        container = self.root / "state-container"
        container.mkdir(mode=0o700)
        backing = container / "private"
        backing.mkdir(mode=0o700)
        private_backing = backing / "nocturne-plugin-emojis"
        public_backing = backing / "nocturne-plugin-emoji-public"
        private_backing.mkdir(mode=0o755)
        public_backing.mkdir(mode=0o755)
        private_alias = container / "nocturne-plugin-emojis"
        public_alias = container / "nocturne-plugin-emoji-public"
        private_alias.symlink_to("private/nocturne-plugin-emojis")
        public_alias.symlink_to("private/nocturne-plugin-emoji-public")
        self.assertEqual(private_backing, initialize_private_root(private_alias))
        self.assertEqual(public_backing, initialize_public_root(public_alias))
        self.assertEqual(0o700, stat.S_IMODE(private_backing.stat().st_mode))
        self.assertEqual(0o755, stat.S_IMODE(public_backing.stat().st_mode))

    def test_exact_systemd_dynamic_user_state_mounts_are_verified(self):
        container = self.root / "mounted-state-container"
        container.mkdir(mode=0o700)
        backing = container / "private"
        backing.mkdir(mode=0o755)
        private_backing = backing / "nocturne-plugin-emojis"
        public_backing = backing / "nocturne-plugin-emoji-public"
        private_backing.mkdir(mode=0o755)
        public_backing.mkdir(mode=0o755)
        private_alias = container / "nocturne-plugin-emojis"
        public_alias = container / "nocturne-plugin-emoji-public"
        private_alias.symlink_to("private/nocturne-plugin-emojis")
        public_alias.symlink_to("private/nocturne-plugin-emoji-public")
        mounts = {backing, private_backing, public_backing}
        state_directories = f"{private_alias}:{public_alias}"
        with patch.dict(os.environ, {"STATE_DIRECTORY": state_directories}), \
                patch("emoji_sync.os.path.ismount",
                      side_effect=lambda path: Path(path) in mounts):
            self.assertEqual(private_backing, initialize_private_root(private_alias))
            self.assertEqual(public_backing, initialize_public_root(public_alias))
        self.assertEqual(0o700, stat.S_IMODE(private_backing.stat().st_mode))
        self.assertEqual(0o755, stat.S_IMODE(public_backing.stat().st_mode))

    def test_verified_systemd_state_mounts_may_cross_parent_device_boundary(self):
        container = self.root / "device-state-container"
        container.mkdir(mode=0o700)
        backing = container / "private"
        backing.mkdir(mode=0o755)
        private_backing = backing / "nocturne-plugin-emojis"
        public_backing = backing / "nocturne-plugin-emoji-public"
        private_backing.mkdir(mode=0o755)
        public_backing.mkdir(mode=0o755)
        private_alias = container / "nocturne-plugin-emojis"
        public_alias = container / "nocturne-plugin-emoji-public"
        private_alias.symlink_to("private/nocturne-plugin-emojis")
        public_alias.symlink_to("private/nocturne-plugin-emoji-public")
        mounts = {backing, private_backing, public_backing}
        parent_device = backing.lstat().st_dev + 1000
        root_device = parent_device + 1000
        real_lstat = Path.lstat
        real_fstat = os.fstat

        def with_device(value, device):
            return os.stat_result((value.st_mode, value.st_ino, device,
                                   value.st_nlink, value.st_uid, value.st_gid,
                                   value.st_size, value.st_atime, value.st_mtime,
                                   value.st_ctime))

        def lstat_with_devices(path):
            value = real_lstat(path)
            if path == backing:
                return with_device(value, parent_device)
            if path in {private_backing, public_backing}:
                return with_device(value, root_device)
            return value

        def fstat_with_root_device(descriptor):
            return with_device(real_fstat(descriptor), root_device)

        state_directories = f"{private_alias}:{public_alias}"
        with patch.dict(os.environ, {"STATE_DIRECTORY": state_directories}), \
                patch.object(Path, "lstat", autospec=True,
                             side_effect=lstat_with_devices), \
                patch("emoji_sync.os.fstat", side_effect=fstat_with_root_device), \
                patch("emoji_sync.os.path.ismount",
                      side_effect=lambda path: Path(path) in mounts):
            self.assertEqual(private_backing, initialize_private_root(private_alias))
            self.assertEqual(public_backing, initialize_public_root(public_alias))
        self.assertEqual(0o700, stat.S_IMODE(private_backing.stat().st_mode))
        self.assertEqual(0o755, stat.S_IMODE(public_backing.stat().st_mode))

    def test_ordinary_root_must_share_its_parent_device(self):
        ordinary = self.root / "ordinary-device-root"
        ordinary.mkdir(mode=0o755)
        real_fstat = os.fstat

        def fstat_on_other_device(descriptor):
            value = real_fstat(descriptor)
            return os.stat_result((value.st_mode, value.st_ino,
                                   value.st_dev + 1000, value.st_nlink,
                                   value.st_uid, value.st_gid, value.st_size,
                                   value.st_atime, value.st_mtime, value.st_ctime))

        with patch("emoji_sync.os.fstat", side_effect=fstat_on_other_device), \
                self.assertRaisesRegex(SyncFailure, "unsafe_output_directory"):
            initialize_public_root(ordinary)

    def test_mounted_systemd_state_parent_requires_exact_environment_evidence(self):
        container = self.root / "evidence-state-container"
        container.mkdir(mode=0o700)
        backing = container / "private"
        backing.mkdir(mode=0o755)
        target = backing / "nocturne-plugin-emojis"
        target.mkdir(mode=0o755)
        alias = container / "nocturne-plugin-emojis"
        alias.symlink_to("private/nocturne-plugin-emojis")
        mounts = {backing, target}
        for evidence in (None, "", str(container / "other"),
                         f"{alias}::{container / 'other'}"):
            environment = {} if evidence is None else {"STATE_DIRECTORY": evidence}
            with self.subTest(evidence=evidence), \
                    patch.dict(os.environ, environment, clear=True), \
                    patch("emoji_sync.os.path.ismount",
                          side_effect=lambda path: Path(path) in mounts), \
                    self.assertRaisesRegex(SyncFailure, "unsafe_private_directory"):
                initialize_private_root(alias)

    def test_mounted_systemd_state_parent_metadata_remains_fail_closed(self):
        for case in ("mode", "ownership", "acl"):
            with self.subTest(case=case):
                container = self.root / ("mounted-parent-" + case)
                container.mkdir(mode=0o700)
                backing = container / "private"
                backing.mkdir(mode=0o755)
                target = backing / "nocturne-plugin-emojis"
                target.mkdir(mode=0o755)
                alias = container / "nocturne-plugin-emojis"
                alias.symlink_to("private/nocturne-plugin-emojis")
                mounts = {backing, target}
                if case == "mode":
                    backing.chmod(0o750)
                    context = patch("emoji_sync.os.geteuid", wraps=os.geteuid)
                elif case == "ownership":
                    real_lstat = Path.lstat
                    observed = backing.lstat()
                    foreign = __import__("types").SimpleNamespace(
                        st_mode=observed.st_mode, st_uid=os.geteuid() + 1000,
                        st_gid=observed.st_gid, st_dev=observed.st_dev,
                        st_ino=observed.st_ino, st_nlink=observed.st_nlink)
                    context = patch.object(
                        Path, "lstat", autospec=True,
                        side_effect=lambda path: foreign if path == backing
                        else real_lstat(path))
                else:
                    context = patch("emoji_sync._acl_free",
                                    side_effect=lambda path: Path(path) != backing)
                with patch.dict(os.environ, {"STATE_DIRECTORY": str(alias)}), \
                        patch("emoji_sync.os.path.ismount",
                              side_effect=lambda path: Path(path) in mounts), \
                        context, self.assertRaisesRegex(
                            SyncFailure, "unsafe_private_directory"):
                    initialize_private_root(alias)

    def test_systemd_state_link_shape_remains_fail_closed(self):
        container = self.root / "unsafe-state-container"
        container.mkdir(mode=0o700)
        backing = container / "private"
        backing.mkdir(mode=0o700)

        wrong = backing / "wrong"
        wrong.mkdir(mode=0o755)
        wrong_alias = container / "nocturne-plugin-emojis"
        wrong_alias.symlink_to("private/wrong")
        with self.assertRaisesRegex(SyncFailure, "unsafe_private_directory"):
            initialize_private_root(wrong_alias)

        for case in ("mode", "ownership", "acl"):
            with self.subTest(case=case):
                name = f"nocturne-plugin-{case}"
                target = backing / name
                target.mkdir(mode=0o755)
                alias = container / name
                alias.symlink_to(f"private/{name}")
                if case == "mode":
                    target.chmod(0o750)
                    context = patch("emoji_sync.os.path.ismount", return_value=False)
                elif case == "ownership":
                    context = patch("emoji_sync.os.geteuid", return_value=os.geteuid() + 1)
                else:
                    context = patch("emoji_sync._acl_free",
                                    side_effect=lambda path: Path(path) != target)
                with context, self.assertRaisesRegex(
                        SyncFailure, "unsafe_(private|output)_directory"):
                    initialize_private_root(alias)

    def test_output_initialization_is_idempotent_and_precedes_credentials(self):
        state = self.root / "initialize-private"
        public = self.root / "initialize-public"
        self.assertEqual(state, initialize_private_root(state))
        self.assertEqual(public, initialize_public_root(public))
        state_before = state.lstat().st_ino
        before = public.lstat().st_ino
        self.assertEqual(state, initialize_private_root(state))
        self.assertEqual(public, initialize_public_root(public))
        self.assertEqual(state_before, state.lstat().st_ino)
        self.assertEqual(before, public.lstat().st_ino)
        with patch("emoji_sync.DiscordTransport") as transport:
            self.assertEqual(0, main(["--initialize-output", "--state", str(state),
                                      "--output", str(public)]))
        transport.assert_not_called()

        bad_config = self.root / "bad-config.json"
        bad_config.write_text("{}")
        bad_config.chmod(0o600)
        token = self.root / "token"
        token.write_text("fixture-token")
        token.chmod(0o600)
        second_state = self.root / "second-private"
        second_public = self.root / "second-public"
        with self.assertLogs("nocturne-emoji-sync", logging.ERROR) as captured:
            self.assertEqual(78, main(["--state", str(second_state),
                                      "--output", str(second_public),
                                      "--config-file", str(bad_config),
                                      "--token-file", str(token)]))
        self.assertIn("category=invalid_config_file", "\n".join(captured.output))
        self.assertTrue(second_public.is_dir())
        self.assertEqual(0o755, stat.S_IMODE(second_public.stat().st_mode))

        good_config = self.root / "good-config.json"
        good_config.write_text('{"guild_id":"123","denylist":[]}')
        good_config.chmod(0o600)
        unsafe_token = self.root / "unsafe-token"
        unsafe_token.write_text("fixture-token")
        unsafe_token.chmod(0o644)
        with self.assertLogs("nocturne-emoji-sync", logging.ERROR) as captured:
            self.assertEqual(78, main(["--state", str(second_state),
                                      "--output", str(second_public),
                                      "--config-file", str(good_config),
                                      "--token-file", str(unsafe_token)]))
        self.assertIn("category=invalid_credential_file", "\n".join(captured.output))
        self.assertNotIn("fixture-token", "\n".join(captured.output))

    def test_output_initialization_rejects_unsafe_existing_nodes(self):
        victim = self.root / "victim"
        victim.mkdir(mode=0o700)
        linked = self.root / "linked"
        linked.symlink_to(victim, target_is_directory=True)
        with self.assertRaisesRegex(SyncFailure, "unsafe_output_directory"):
            initialize_public_root(linked)
        valid_state = self.root / "valid-private"
        with self.assertLogs("nocturne-emoji-sync", logging.ERROR) as captured:
            self.assertEqual(75, main(["--initialize-output", "--state", str(valid_state),
                                      "--output", str(linked)]))
        self.assertIn("category=unsafe_output_directory", "\n".join(captured.output))

        wrong_mode = self.root / "wrong-mode"
        wrong_mode.mkdir(mode=0o750)
        wrong_mode.chmod(0o750)
        with self.assertRaisesRegex(SyncFailure, "unsafe_output_directory"):
            initialize_public_root(wrong_mode)

        unsafe_parent = self.root / "unsafe-parent"
        unsafe_parent.mkdir(mode=0o777)
        unsafe_parent.chmod(0o777)
        nested = unsafe_parent / "public"
        with self.assertRaisesRegex(SyncFailure, "unsafe_output_parent"):
            initialize_public_root(nested)

        real_parent = self.root / "real-parent"
        real_parent.mkdir(mode=0o700)
        linked_parent = self.root / "linked-parent"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        with self.assertRaisesRegex(SyncFailure, "unsafe_output_parent"):
            initialize_public_root(linked_parent / "public")

        foreign = self.root / "foreign"
        foreign.mkdir(mode=0o700)
        with patch("emoji_sync.os.geteuid", return_value=os.geteuid() + 1), \
                self.assertRaisesRegex(SyncFailure, "unsafe_output_(parent|directory)"):
            initialize_public_root(foreign)

        mounted = self.root / "mounted"
        mounted.mkdir(mode=0o700)
        with patch("emoji_sync.os.path.ismount",
                   side_effect=lambda path: Path(path) == mounted), \
                self.assertRaisesRegex(SyncFailure, "unsafe_output_directory"):
            initialize_public_root(mounted)

        mounted_parent = self.root / "mounted-parent"
        mounted_parent.mkdir(mode=0o700)
        with patch("emoji_sync.os.path.ismount",
                   side_effect=lambda path: Path(path) == mounted_parent), \
                self.assertRaisesRegex(SyncFailure, "unsafe_output_parent"):
            initialize_public_root(mounted_parent / "public")

        private_link = self.root / "private-link"
        private_link.symlink_to(victim, target_is_directory=True)
        with self.assertRaisesRegex(SyncFailure, "unsafe_private_directory"):
            initialize_private_root(private_link)

    def test_publication_io_failure_is_not_mislabeled_as_configuration(self):
        config = self.root / "config.json"
        config.write_text('{"guild_id":"123","denylist":[]}')
        config.chmod(0o600)
        token = self.root / "token"
        token.write_text("fixture-token")
        token.chmod(0o600)
        state = self.root / "publication-private"
        public = self.root / "publication-error"
        self.transport.values = []
        with patch("emoji_sync.DiscordTransport", return_value=self.transport), \
                patch.object(EmojiSynchronizer, "_publish",
                             side_effect=OSError("fixture publication failure")), \
                self.assertLogs("nocturne-emoji-sync", logging.ERROR) as captured:
            result = main(["--state", str(state), "--output", str(public),
                           "--config-file", str(config),
                           "--token-file", str(token)])
        self.assertEqual(75, result)
        rendered = "\n".join(captured.output)
        self.assertIn("category=local_publication_error", rendered)
        self.assertNotIn("fixture publication failure", rendered)
        self.assertNotIn("fixture-token", rendered)

    def test_output_initialization_rejects_acl_when_supported(self):
        if not __import__("shutil").which("setfacl"):
            self.skipTest("setfacl unavailable")
        public = self.root / "public-acl"
        public.mkdir(mode=0o700)
        result = __import__("subprocess").run(
            ["setfacl", "-m", "u:65534:---", str(public)],
            capture_output=True, text=True)
        if result.returncode:
            self.skipTest("fixture filesystem has no POSIX ACL support")
        with self.assertRaisesRegex(SyncFailure, "unsafe_output_directory"):
            initialize_public_root(public)

    def test_unexpected_public_acl_fails_closed_when_supported(self):
        if not __import__("shutil").which("setfacl"):
            self.skipTest("setfacl unavailable")
        initialize_private_root(self.state)
        self.public.mkdir(mode=0o700)
        result = __import__("subprocess").run(
            ["setfacl", "-m", "u:65534:---", str(self.public)],
            capture_output=True, text=True)
        if result.returncode:
            self.skipTest("fixture filesystem has no POSIX ACL support")
        self.configure([item(1, "one")])
        with self.assertRaisesRegex(SyncFailure, "unsafe_output_directory"):
            self.sync.synchronize("123", "fixture-token")

    def test_private_workspace_and_lock_are_never_in_public_mirror(self):
        self.configure([item(1, "one")])
        self.sync.synchronize("123", "fixture-token")
        self.assertEqual({".sync.lock"}, {path.name for path in self.state.iterdir()})
        self.assertEqual({"current", "generations"},
                         {path.name for path in self.public.iterdir()})
        lock = self.state / ".sync.lock"
        self.assertEqual(0o600, stat.S_IMODE(lock.stat().st_mode))
        self.assertEqual(1, lock.stat().st_nlink)
        self.assertFalse((self.public / ".sync.lock").exists())

    def test_transport_hosts_headers_redirects_and_bounds_are_fixed(self):
        class Response:
            status = 200
            headers = {"Content-Type": "application/json", "X-Secret": "ignored"}
            def __enter__(self): return self
            def __exit__(self, *_args): return None
            def read(self, count):
                self.count = count
                return b"[]"

        class Opener:
            def __init__(self): self.calls = []
            def open(self, request, timeout):
                response = Response()
                self.calls.append((request, timeout, response))
                return response

        opener = Opener()
        transport = DiscordTransport(timeout=10, opener=opener)
        transport.list_emojis("123", "fixture-secret")
        transport.fetch_asset("456", False)
        listing, asset = opener.calls
        self.assertEqual(
            "DiscordBot (https://github.com/NocturneCC/NocturnePlugin, 0.3.2)",
            DISCORD_USER_AGENT)
        self.assertEqual("https://discord.com/api/v10/guilds/123/emojis",
                         listing[0].full_url)
        self.assertEqual("GET", listing[0].get_method())
        self.assertEqual("Bot fixture-secret", listing[0].get_header("Authorization"))
        self.assertEqual(DISCORD_USER_AGENT, listing[0].get_header("User-agent"))
        self.assertEqual("application/json", listing[0].get_header("Accept"))
        self.assertEqual("application/json", listing[0].get_header("Content-type"))
        self.assertEqual("https://cdn.discordapp.com/emojis/456.png", asset[0].full_url)
        self.assertEqual("GET", asset[0].get_method())
        self.assertIsNone(asset[0].get_header("Authorization"))
        self.assertEqual(DISCORD_USER_AGENT, asset[0].get_header("User-agent"))
        self.assertEqual("image/gif,image/png", asset[0].get_header("Accept"))
        self.assertIsNone(asset[0].get_header("Content-type"))
        self.assertEqual((10, 10), (listing[1], asset[1]))
        self.assertEqual((256 * 1024 + 1, 256 * 1024 + 1),
                         (listing[2].count, asset[2].count))
        self.assertNotIn("fixture-secret", listing[0].full_url)
        self.assertIsNone(NoRedirect().redirect_request(
            listing[0], None, 302, "redirect", {}, "https://example.invalid/"))
        for invalid in ("../1", "https://example.invalid/x", "1.png", ""):
            with self.assertRaisesRegex(SyncFailure, "invalid_emoji_id"):
                transport.fetch_asset(invalid, False)
        with self.assertRaises(ValueError):
            DiscordTransport(timeout=0, opener=opener)

    def test_default_transport_disables_environment_proxies(self):
        with patch("emoji_sync.build_opener") as build:
            DiscordTransport()
        handlers = build.call_args.args
        self.assertEqual(2, len(handlers))
        self.assertIsInstance(handlers[0], ProxyHandler)
        self.assertEqual({}, handlers[0].proxies)
        self.assertIsInstance(handlers[1], NoRedirect)

    def test_structured_discord_error_is_reduced_to_safe_category(self):
        body_secret = "structured-body-must-not-leak"
        header_secret = "structured-header-must-not-leak"

        class Opener:
            def open(self, request, timeout):
                raise HTTPError(request.full_url, 403, "Forbidden",
                                {"Content-Type": "application/json",
                                 "X-Secret": header_secret},
                                BytesIO(json.dumps({"code": 50013,
                                                   "message": "Missing Permissions",
                                                   "secret": body_secret}).encode()))

        transport = DiscordTransport(opener=Opener())
        with self.assertRaisesRegex(SyncFailure, "api_error") as caught:
            transport.list_emojis("123", "structured-token-must-not-leak")
        rendered = repr(caught.exception)
        self.assertNotIn(body_secret, rendered)
        self.assertNotIn(header_secret, rendered)
        self.assertNotIn("structured-token-must-not-leak", rendered)

    def test_unstructured_cloudflare_error_is_reduced_to_safe_category(self):
        body_secret = "cloudflare-body-must-not-leak"
        header_secret = "cloudflare-header-must-not-leak"

        class Opener:
            def open(self, request, timeout):
                raise HTTPError(request.full_url, 403, "Forbidden",
                                {"Content-Type": "text/html", "CF-Ray": header_secret},
                                BytesIO(f"<html>{body_secret}</html>".encode()))

        transport = DiscordTransport(opener=Opener())
        with self.assertRaisesRegex(SyncFailure, "api_error") as caught:
            transport.list_emojis("123", "cloudflare-token-must-not-leak")
        rendered = repr(caught.exception)
        self.assertNotIn(body_secret, rendered)
        self.assertNotIn(header_secret, rendered)
        self.assertNotIn("cloudflare-token-must-not-leak", rendered)

    def test_private_credential_and_strict_config_loading(self):
        token = self.root / "token"
        token.write_text("fixture-token\n")
        token.chmod(0o600)
        config = self.root / "config.json"
        config.write_text('{"guild_id":"123","denylist":["blocked","456"]}')
        config.chmod(0o600)
        self.assertEqual("fixture-token", _read_credential(token))
        self.assertEqual(("123", frozenset({"blocked", "456"})), _read_config(config))
        token.chmod(0o640)
        with self.assertRaisesRegex(ValueError, "unsafe private file"):
            _read_credential(token)
        token.chmod(0o600)
        token.write_text("bad\nheader\n")
        with self.assertRaisesRegex(ValueError, "invalid credential"):
            _read_credential(token)
        config.write_text('{"guild_id":"123","guild_id":"456"}')
        with self.assertRaisesRegex(ValueError, "invalid synchronization config"):
            _read_config(config)

    def test_exact_systemd_load_credentials_are_accepted(self):
        directory = self.root / "credentials"
        directory.mkdir(mode=0o700)
        config = directory / "emoji-sync-config"
        config.write_text('{"guild_id":"123","denylist":["blocked"]}')
        config.chmod(0o440)
        token = directory / "discord-token"
        token.write_text("fixture-token\n")
        token.chmod(0o440)
        directory.chmod(0o550)
        files = {config, token}
        real_lstat = Path.lstat
        real_fstat = os.fstat

        def credential_lstat(path):
            value = real_lstat(path)
            if path == directory:
                return changed_stat(value, st_mode=stat.S_IFDIR | 0o550,
                                    st_uid=0, st_gid=0)
            if path in files:
                return changed_stat(value, st_mode=stat.S_IFREG | 0o440,
                                    st_uid=0, st_gid=0)
            return value

        def credential_fstat(descriptor):
            return changed_stat(real_fstat(descriptor),
                                st_mode=stat.S_IFREG | 0o440,
                                st_uid=0, st_gid=0)

        with patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": str(directory)},
                        clear=True), \
                patch.object(Path, "lstat", autospec=True,
                             side_effect=credential_lstat), \
                patch("emoji_sync.os.fstat", side_effect=credential_fstat), \
                patch("emoji_sync.os.path.ismount",
                      side_effect=lambda path: Path(path) == directory):
            self.assertEqual(("123", frozenset({"blocked"})), _read_config(config))
            self.assertEqual("fixture-token", _read_credential(token))

    def test_systemd_load_credential_metadata_remains_fail_closed(self):
        directory = self.root / "credential-cases"
        directory.mkdir(mode=0o700)
        token = directory / "discord-token"
        token.write_text("fixture-token\n")
        token.chmod(0o440)
        directory.chmod(0o550)
        real_lstat = Path.lstat
        real_fstat = os.fstat

        def run_case(*, environment=None, mounted=True, parent_changes=None,
                     file_changes=None, unsafe_acl_path=None, path=token):
            parent_changes = {} if parent_changes is None else parent_changes
            file_changes = {} if file_changes is None else file_changes

            def credential_lstat(candidate):
                value = real_lstat(candidate)
                if candidate == directory:
                    return changed_stat(value, **{"st_mode": stat.S_IFDIR | 0o550,
                                                  "st_uid": 0, "st_gid": 0,
                                                  **parent_changes})
                if candidate == token:
                    return changed_stat(value, **{"st_mode": stat.S_IFREG | 0o440,
                                                  "st_uid": 0, "st_gid": 0,
                                                  **file_changes})
                return value

            def credential_fstat(descriptor):
                return changed_stat(real_fstat(descriptor),
                                    **{"st_mode": stat.S_IFREG | 0o440,
                                       "st_uid": 0, "st_gid": 0,
                                       **file_changes})

            credentials = (str(directory) if environment is None else environment)
            with patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": credentials},
                            clear=True), \
                    patch.object(Path, "lstat", autospec=True,
                                 side_effect=credential_lstat), \
                    patch("emoji_sync.os.fstat", side_effect=credential_fstat), \
                    patch("emoji_sync.os.path.ismount",
                          side_effect=lambda candidate: mounted
                          and Path(candidate) == directory), \
                    patch("emoji_sync._acl_free",
                          side_effect=lambda candidate: Path(candidate)
                          != unsafe_acl_path):
                with self.assertRaisesRegex(ValueError, "unsafe private file"):
                    _read_credential(path)

        outside = self.root / "outside-token"
        outside.write_text("fixture-token\n")
        outside.chmod(0o440)
        cases = {
            "wrong_path": {"path": outside},
            "unmounted_parent": {"mounted": False},
            "parent_mode": {"parent_changes": {"st_mode": stat.S_IFDIR | 0o750}},
            "parent_owner": {"parent_changes": {"st_uid": 1}},
            "parent_acl": {"unsafe_acl_path": directory},
            "file_acl": {"unsafe_acl_path": token},
            "file_mode": {"file_changes": {"st_mode": stat.S_IFREG | 0o444}},
            "file_owner": {"file_changes": {"st_uid": 1}},
            "file_link": {"file_changes": {"st_nlink": 2}},
            "relative_environment": {"environment": "relative/credentials"},
            "spoofed_environment": {
                "environment": str(directory.parent / "other" / ".." / directory.name)},
        }
        for name, arguments in cases.items():
            with self.subTest(name=name):
                run_case(**arguments)

        target = self.root / "credential-target"
        target.write_text("fixture-token\n")
        target.chmod(0o440)
        linked = directory / "linked-token"
        directory.chmod(0o700)
        linked.symlink_to(target)
        directory.chmod(0o550)
        with patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": str(directory)}, clear=True), \
                self.assertRaisesRegex(ValueError, "unsafe private file"):
            _read_credential(linked)

    def test_credential_sync_failure_is_reduced_to_invalid_file(self):
        with patch("emoji_sync._safe_private_file",
                   side_effect=[b'{"guild_id":"123","denylist":[]}',
                                SyncFailure("unsafe_private_directory")]), \
                self.assertLogs("nocturne-emoji-sync", logging.ERROR) as captured:
            result = main(["--state", str(self.root / "credential-private"),
                           "--output", str(self.root / "credential-public"),
                           "--config-file", str(self.root / "emoji-sync-config"),
                           "--token-file", str(self.root / "discord-token")])
        self.assertEqual(78, result)
        self.assertIn("category=invalid_credential_file", "\n".join(captured.output))

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
