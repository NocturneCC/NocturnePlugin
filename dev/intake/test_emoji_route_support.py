import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from emoji_route_support import candidate_site, install, rollback


class EmojiRouteSupportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        self.commit = "a" * 40
        self.source = self.runtime / "staged-nginx" / self.commit
        self.source.mkdir(parents=True)
        self.release_source = self.runtime / "releases" / self.commit / "dev/intake"
        self.release_source.mkdir(parents=True)
        (self.runtime / "current").symlink_to(Path("releases") / self.commit)
        self.backups = self.root / "backups"
        self.backups.mkdir()
        self.target = self.root / "nocturne"
        self.announcement = """location = /api/plugin/v1/announcements {
    limit_except GET { deny all; }
    proxy_pass http://127.0.0.1:5072;
}
"""
        self.emoji = """location = /api/plugin/v1/emojis {
    limit_except GET { deny all; }
    if ($content_length !~ "^$|^0$") { return 413; }
    proxy_pass_header ETag;
    proxy_pass_header Cache-Control;
    proxy_pass http://127.0.0.1:5072;
}
location ~ "^/api/plugin/v1/emojis/assets/[0-9a-f]{64}\\.png$" {
    limit_except GET { deny all; }
    proxy_pass http://127.0.0.1:5072;
}
"""
        indented = "\n".join("    " + line for line in self.announcement.strip().splitlines()) + "\n"
        self.active = "server {\n    # Nocturne plugin development intake\n" + indented + "}\n"
        self.target.write_text(self.active)
        (self.release_source / "nginx-announcements-location.conf").write_text(self.announcement)
        (self.source / "nginx-emojis-location.conf").write_text(self.emoji)
        (self.source / "STAGED-NGINX-MANIFEST.json").write_text("{}\n")
        self.metadata = {"uid": 0, "gid": 0, "mode": 0o640,
                         "acl": "user::rw-\ngroup::r--\nother::---\n"}
        self.activation_record=self.root/"activation-record"
        self.systemd=self.root/"systemd"; self.systemd.mkdir()

    def install(self,*args,**kwargs):
        kwargs.setdefault("activation_record",self.activation_record)
        kwargs.setdefault("systemd_dir",self.systemd)
        return install(*args,**kwargs)

    def rollback(self,*args,**kwargs):
        kwargs.setdefault("activation_record",self.activation_record)
        kwargs.setdefault("systemd_dir",self.systemd)
        return rollback(*args,**kwargs)

    def mocks(self):
        return (patch("emoji_route_support._capture_safe_metadata", return_value=self.metadata),
                patch("emoji_route_support._apply_metadata"),
                patch("emoji_route_support._verify_metadata"))

    def test_routes_are_narrow_bodyless_bounded_and_preserve_cache_headers(self):
        candidate = candidate_site(self.active, self.announcement, self.emoji)
        self.assertEqual(1, candidate.count("location = /api/plugin/v1/emojis"))
        self.assertEqual(1, candidate.count("/api/plugin/v1/emojis/assets/"))
        self.assertIn('"^/api/plugin/v1/emojis/assets/[0-9a-f]{64}\\.png$"', candidate)
        self.assertEqual(3, candidate.count("limit_except GET"))
        self.assertIn('if ($content_length !~ "^$|^0$")', candidate)
        self.assertIn("proxy_pass_header ETag", candidate)
        self.assertIn("proxy_pass_header Cache-Control", candidate)

    def test_refuses_missing_changed_or_partial_route_state(self):
        for active in (self.active.replace("127.0.0.1", "localhost"),
                       self.active + "/api/plugin/v1/emojis"):
            with self.subTest(), self.assertRaises(ValueError):
                candidate_site(active, self.announcement, self.emoji)

    def test_dry_run_apply_idempotency_failure_restore_and_exact_rollback(self):
        first, second, third = self.mocks()
        validations = []
        with first, second, third:
            with patch("immutable_runtime_release.verify_staged_deployment", return_value={}), \
                    patch("immutable_runtime_release.verify_applied_activation",return_value={}):
                dry = self.install(self.target, self.runtime, self.commit, self.backups)
            self.assertEqual(("not_applied", True), (dry["state"], dry["dry_run"]))
            with patch("immutable_runtime_release.verify_staged_deployment", return_value={}), \
                    patch("immutable_runtime_release.verify_applied_activation",return_value={}):
                applied = self.install(self.target, self.runtime, self.commit, self.backups, apply=True,
                                  validate=lambda: validations.append("nginx-t"))
                self.assertEqual("already_applied",
                                 self.install(self.target, self.runtime, self.commit, self.backups)["state"])
                restored = self.rollback(applied["backup"], self.target, runtime_root=self.runtime,
                                    commit=self.commit,
                                validate=lambda: validations.append("rollback-t"))
            self.assertFalse(restored["already_restored"])
        self.assertEqual(self.active, self.target.read_text())
        self.assertEqual(["nginx-t", "rollback-t"], validations)

        first, second, third = self.mocks()
        with first, second, third:
            with self.assertRaisesRegex(RuntimeError, "syntax"):
                with patch("immutable_runtime_release.verify_staged_deployment", return_value={}), \
                        patch("immutable_runtime_release.verify_applied_activation",return_value={}):
                    self.install(self.target, self.runtime, self.commit, self.backups, apply=True,
                            validate=lambda: (_ for _ in ()).throw(RuntimeError("syntax")))
        self.assertEqual(self.active, self.target.read_text())

    def test_failed_rollback_restores_applied_route(self):
        first, second, third = self.mocks()
        with first, second, third:
            with patch("immutable_runtime_release.verify_staged_deployment", return_value={}), \
                    patch("immutable_runtime_release.verify_applied_activation",return_value={}):
                applied = self.install(self.target, self.runtime, self.commit, self.backups, apply=True,
                                  validate=lambda: None)
            applied_text = self.target.read_text()
            with self.assertRaisesRegex(RuntimeError, "rollback syntax"):
                with patch("immutable_runtime_release.verify_staged_deployment", return_value={}), \
                        patch("immutable_runtime_release.verify_applied_activation",return_value={}):
                    self.rollback(applied["backup"], self.target, runtime_root=self.runtime,
                             commit=self.commit,
                             validate=lambda: (_ for _ in ()).throw(RuntimeError("rollback syntax")))
        self.assertEqual(applied_text, self.target.read_text())

    def test_hardlinked_target_is_rejected(self):
        linked = self.root / "linked-site"
        __import__("os").link(self.target, linked)
        with self.assertRaisesRegex(ValueError, "hard-linked"):
            with patch("immutable_runtime_release.verify_staged_deployment", return_value={}), \
                    patch("immutable_runtime_release.verify_applied_activation",return_value={}):
                self.install(self.target, self.runtime, self.commit, self.backups)


if __name__ == "__main__":
    unittest.main()
