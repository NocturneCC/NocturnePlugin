import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from raid_presence_route_support import candidate_site, install


class RaidPresenceRouteSupportTest(unittest.TestCase):
    def setUp(self):
        self.drop = "location = /api/plugin/dev/drops {\n    proxy_pass http://127.0.0.1:5072;\n}\n"
        self.presence = "location = /api/plugin/dev/raid-presence {\n    client_max_body_size 8k;\n}\n"
        indented = "\n".join("    " + line for line in self.drop.strip().splitlines()) + "\n"
        self.active = "server {\n    # Nocturne plugin development intake\n" + indented + "}\n"

    def test_adds_exact_bounded_route_once_without_changing_drop_route(self):
        result = candidate_site(self.active, self.drop, self.presence)
        self.assertEqual(1, result.count("/api/plugin/dev/drops"))
        self.assertEqual(1, result.count("/api/plugin/dev/raid-presence"))
        self.assertIn("client_max_body_size 8k", result)

    def test_refuses_absent_changed_or_already_applied_state(self):
        for active in (self.active.replace("127.0.0.1", "localhost"),
                       self.active + "/api/plugin/dev/raid-presence"):
            with self.assertRaises(ValueError):
                candidate_site(active, self.drop, self.presence)

    def test_dry_run_and_apply_are_separate_and_apply_validates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "nocturne"
            source = root / "source"; source.mkdir()
            backups = root / "backups"
            target.write_text(self.active)
            (source / "nginx-location.conf").write_text(self.drop)
            (source / "nginx-raid-presence-location.conf").write_text(self.presence)
            metadata = {"uid": 0, "gid": 0, "mode": 0o640,
                        "acl": "user::rw-\ngroup::r--\nother::---\n"}
            calls = []
            with patch("raid_presence_route_support._capture_safe_metadata", return_value=metadata), \
                 patch("raid_presence_route_support._apply_metadata"), \
                 patch("raid_presence_route_support._verify_metadata"):
                self.assertEqual("not_applied", install(target, source, backups)["state"])
                self.assertNotIn("raid-presence", target.read_text())
                result = install(target, source, backups, True, lambda: calls.append("nginx-t"))
                self.assertEqual("applied", result["state"])
                self.assertEqual(["nginx-t"], calls)
                self.assertTrue((Path(result["backup"]) / "MANIFEST.json").is_file())
                self.assertEqual("already_applied", install(target, source, backups)["state"])

    def test_post_activation_validation_failure_restores_verified_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); target = root / "nocturne"
            source = root / "source"; source.mkdir(); backups = root / "backups"
            target.write_text(self.active)
            (source / "nginx-location.conf").write_text(self.drop)
            (source / "nginx-raid-presence-location.conf").write_text(self.presence)
            metadata = {"uid": 0, "gid": 0, "mode": 0o640,
                        "acl": "user::rw-\ngroup::r--\nother::---\n"}
            with patch("raid_presence_route_support._capture_safe_metadata", return_value=metadata), \
                 patch("raid_presence_route_support._apply_metadata"), \
                 patch("raid_presence_route_support._verify_metadata"):
                with self.assertRaises(RuntimeError):
                    install(target, source, backups, True,
                            lambda: (_ for _ in ()).throw(RuntimeError("nginx check failed")))
            self.assertEqual(self.active, target.read_text())
            backup_dirs = list(backups.iterdir())
            self.assertEqual(1, len(backup_dirs))
            self.assertEqual(self.active, (backup_dirs[0] / "nocturne.before").read_text())


if __name__ == "__main__": unittest.main()
