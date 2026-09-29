import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emoji_service_support import STOP_CONFIRMATION, UNITS, install, rollback


class EmojiServiceSupportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.targets = self.root / "systemd"
        self.targets.mkdir()
        self.backups = self.root / "backups"
        self.backups.mkdir()
        for name in UNITS:
            (self.source / name).write_text(f"[Unit]\nDescription={name}\n[Service]\nExecStart=/bin/true\n")
        self.old = "[Unit]\nDescription=old intake\n[Service]\nExecStart=/bin/false\n"
        (self.targets / "nocturne-plugin-dev.service").write_text(self.old)
        self.metadata = {"uid": 0, "gid": 0, "mode": 0o640,
                         "acl": "user::rw-\ngroup::r--\nother::---\n"}

    def mocks(self):
        return (patch("emoji_service_support._capture_safe_metadata", return_value=self.metadata),
                patch("emoji_service_support._apply_metadata"),
                patch("emoji_service_support._verify_metadata"))

    def test_dry_run_requires_explicit_stopped_process_confirmation_for_apply(self):
        validations = []
        dry = install(self.targets, self.source, self.backups,
                      validate=lambda paths: validations.append(tuple(paths)))
        self.assertTrue(dry["dry_run"])
        self.assertEqual(STOP_CONFIRMATION, dry["required_stop_confirmation"])
        with self.assertRaisesRegex(ValueError, "must be stopped"):
            install(self.targets, self.source, self.backups, apply=True,
                    validate=lambda paths: None)

    def test_apply_idempotency_metadata_validation_and_exact_rollback(self):
        first, second, third = self.mocks()
        validations = []
        with first, second as applied, third as verified:
            result = install(self.targets, self.source, self.backups, apply=True,
                             confirmed_services_stopped=True,
                             validate=lambda paths: validations.append([Path(p).name for p in paths]))
            self.assertEqual("applied", result["state"])
            self.assertTrue(applied.called)
            self.assertTrue(verified.called)
            self.assertEqual("already_applied", install(
                self.targets, self.source, self.backups,
                validate=lambda paths: None)["state"])
            restored = rollback(result["backup"], confirmed_services_stopped=True,
                                validate=lambda paths: validations.append([Path(p).name for p in paths]))
        self.assertEqual((1, 2), (restored["restored"], restored["removed"]))
        self.assertEqual(self.old, (self.targets / "nocturne-plugin-dev.service").read_text())
        self.assertFalse((self.targets / "nocturne-plugin-emoji-sync.service").exists())
        self.assertFalse((self.targets / "nocturne-plugin-emoji-sync.timer").exists())

    def test_validation_failure_restores_old_and_removes_new_units(self):
        first, second, third = self.mocks()
        calls = []
        def validate(paths):
            calls.append(paths)
            if len(calls) == 2:
                raise RuntimeError("unit validation failed")
        with first, second, third:
            with self.assertRaisesRegex(RuntimeError, "unit validation"):
                install(self.targets, self.source, self.backups, apply=True,
                        confirmed_services_stopped=True, validate=validate)
        self.assertEqual(self.old, (self.targets / "nocturne-plugin-dev.service").read_text())
        self.assertFalse((self.targets / "nocturne-plugin-emoji-sync.service").exists())

    def test_post_replace_metadata_failure_restores_every_target(self):
        first, second, _third = self.mocks()
        def verify(path, _metadata):
            path = Path(path)
            if path.parent == self.targets and path.name == "nocturne-plugin-emoji-sync.service":
                raise RuntimeError("fixture metadata failure")
        with first, second, patch("emoji_service_support._verify_metadata", side_effect=verify):
            with self.assertRaisesRegex(RuntimeError, "fixture metadata failure"):
                install(self.targets, self.source, self.backups, apply=True,
                        confirmed_services_stopped=True, validate=lambda paths: None)
        self.assertEqual(self.old, (self.targets / "nocturne-plugin-dev.service").read_text())
        self.assertFalse((self.targets / "nocturne-plugin-emoji-sync.service").exists())
        self.assertFalse((self.targets / "nocturne-plugin-emoji-sync.timer").exists())

    def test_real_unit_uses_private_credentials_and_pinned_runtime(self):
        source = Path(__file__).resolve().parent
        unit = (source / "nocturne-plugin-emoji-sync.service").read_text()
        self.assertNotIn("EnvironmentFile=", unit)
        self.assertNotIn("NOCTURNE_DISCORD_GUILD_ID", unit)
        self.assertNotIn("NOCTURNE_EMOJI_DENYLIST", unit)
        self.assertIn("LoadCredential=emoji-sync-config:", unit)
        self.assertIn("LoadCredential=discord-token:", unit)
        self.assertIn("--config-file=%d/emoji-sync-config", unit)
        self.assertIn("--token-file=%d/discord-token", unit)
        for required in ("DynamicUser=yes", "ProtectSystem=strict", "ProtectHome=yes",
                         "PrivateDevices=yes", "NoNewPrivileges=yes", "MemoryMax=128M"):
            self.assertIn(required, unit)
        requirements = (source / "emoji-sync-requirements.txt").read_text()
        self.assertIn("--only-binary=:all:", requirements)
        self.assertIn("Pillow==12.3.0", requirements)
        self.assertIn("251bf95b67017e27b13d82f5b326234ca62d70f9cf4c2b9032de2358a3b12c7b",
                      requirements)

    def test_failed_rollback_restores_applied_units(self):
        first, second, third = self.mocks()
        with first, second, third:
            applied = install(self.targets, self.source, self.backups, apply=True,
                              confirmed_services_stopped=True, validate=lambda paths: None)
            expected = {name: (self.targets / name).read_bytes() for name in UNITS}
            with self.assertRaisesRegex(RuntimeError, "rollback unit validation"):
                rollback(applied["backup"], confirmed_services_stopped=True,
                         validate=lambda paths: (_ for _ in ()).throw(
                             RuntimeError("rollback unit validation")))
        self.assertEqual(expected, {name: (self.targets / name).read_bytes() for name in UNITS})

    def test_hardlinked_unit_target_is_rejected(self):
        linked = self.root / "linked-unit"
        __import__("os").link(self.targets / "nocturne-plugin-dev.service", linked)
        with self.assertRaisesRegex(ValueError, "unsafe unit target"):
            install(self.targets, self.source, self.backups, validate=lambda paths: None)


if __name__ == "__main__":
    unittest.main()
