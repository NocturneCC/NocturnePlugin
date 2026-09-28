import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from announcement_route_support import candidate_site, install, rollback


class AnnouncementRouteSupportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.backups = self.root / "backups"
        self.backups.mkdir()
        self.target = self.root / "nocturne"
        self.drop = "location = /api/plugin/dev/drops {\n    proxy_pass http://127.0.0.1:5072;\n}\n"
        self.announcement = """# Public announcements
location = /api/plugin/v1/announcements {
    limit_except GET { deny all; }
    if ($content_length !~ "^$|^0$") { return 413; }
    if ($http_transfer_encoding != "") { return 413; }
    client_max_body_size 1;
    proxy_request_buffering on;
    proxy_pass_header ETag;
    proxy_pass_header Cache-Control;
    proxy_connect_timeout 2s;
    proxy_read_timeout 7s;
    proxy_pass http://127.0.0.1:5072;
}
"""
        indented = "\n".join("    " + line for line in self.drop.strip().splitlines()) + "\n"
        self.active = "server {\n    # Nocturne plugin development intake\n" + indented + "}\n"
        self.target.write_text(self.active)
        (self.source / "nginx-location.conf").write_text(self.drop)
        (self.source / "nginx-announcements-location.conf").write_text(self.announcement)
        self.metadata = {"uid": 0, "gid": 0, "mode": 0o640,
                         "acl": "user::rw-\ngroup::r--\nother::---\n"}

    def mocks(self):
        return (patch("announcement_route_support._capture_safe_metadata",
                      return_value=self.metadata),
                patch("announcement_route_support._apply_metadata"),
                patch("announcement_route_support._verify_metadata"))

    def test_exact_route_allows_only_reads_and_preserves_cache_headers(self):
        candidate = candidate_site(self.active, self.drop, self.announcement)
        self.assertEqual(1, candidate.count("location = /api/plugin/v1/announcements"))
        self.assertIn("limit_except GET", candidate)
        self.assertIn('if ($content_length !~ "^$|^0$")', candidate)
        self.assertIn("client_max_body_size 1", candidate)
        self.assertIn("proxy_request_buffering on", candidate)
        self.assertIn("proxy_pass_header ETag", candidate)
        self.assertIn("proxy_pass_header Cache-Control", candidate)
        self.assertIn("proxy_connect_timeout 2s", candidate)
        self.assertIn("proxy_read_timeout 7s", candidate)
        self.assertEqual(1, candidate.count("location = /api/plugin/dev/drops"))

    def test_refuses_absent_changed_or_partial_route_state(self):
        for active in (self.active.replace("127.0.0.1", "localhost"),
                       self.active + "/api/plugin/v1/announcements"):
            with self.subTest(), self.assertRaises(ValueError):
                candidate_site(active, self.drop, self.announcement)

    def test_dry_run_apply_idempotency_metadata_and_independent_rollback(self):
        validate = []
        first, second, third = self.mocks()
        with first, second as applied, third as verified:
            dry = install(self.target, self.source, self.backups)
            self.assertEqual(("not_applied", True), (dry["state"], dry["dry_run"]))
            self.assertEqual(self.active, self.target.read_text())
            result = install(self.target, self.source, self.backups, apply=True,
                             validate=lambda: validate.append("nginx-t"))
            self.assertEqual(("applied", False), (result["state"], result["dry_run"]))
            self.assertEqual(["nginx-t"], validate)
            self.assertTrue(applied.called)
            self.assertTrue(verified.called)
            self.assertEqual("already_applied",
                             install(self.target, self.source, self.backups)["state"])
            restored = rollback(result["backup"], self.target,
                                validate=lambda: validate.append("rollback-nginx-t"))
            self.assertFalse(restored["already_restored"])
        self.assertEqual(self.active, self.target.read_text())
        self.assertEqual(["nginx-t", "rollback-nginx-t"], validate)

    def test_validation_failure_restores_verified_original(self):
        first, second, third = self.mocks()
        with first, second, third:
            with self.assertRaisesRegex(RuntimeError, "syntax failure"):
                install(self.target, self.source, self.backups, apply=True,
                        validate=lambda: (_ for _ in ()).throw(RuntimeError("syntax failure")))
        self.assertEqual(self.active, self.target.read_text())
        backup = next(self.backups.iterdir())
        self.assertEqual(self.active, (backup / "nocturne.before").read_text())


if __name__ == "__main__":
    unittest.main()
