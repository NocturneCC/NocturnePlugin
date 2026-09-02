from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from install_screenshot_support import (
    CONFIG_ANCHOR,
    ROUTE_ANCHOR,
    ROUTE_MARKER,
    candidate_admin,
    candidate_nginx,
    install,
)


class ScreenshotSupportInstallTest(unittest.TestCase):
    def test_admin_patch_adds_private_route_once(self):
        original = "imports\n" + CONFIG_ANCHOR + "helpers\n" + ROUTE_ANCHOR + "def route(): pass\n"
        changed = candidate_admin(original)
        self.assertIn(ROUTE_MARKER, changed)
        self.assertIn("@require_noc_super_admin", changed)
        self.assertIn('Cache-Control"] = "private, no-store"', changed)
        with self.assertRaises(ValueError):
            candidate_admin(changed)

    def test_nginx_patch_changes_only_intake_body_limit(self):
        repository = """location = /api/plugin/dev/drops {
    client_max_body_size 360k;
    proxy_pass http://127.0.0.1:5072;
}
"""
        installed = "server {\n" + "\n".join(
            "    " + line for line in repository.replace("360k", "8k").strip().splitlines()
        ) + "\n    client_max_body_size 8M;\n}\n"
        changed = candidate_nginx(installed, repository)
        self.assertEqual(1, changed.count("client_max_body_size 360k;"))
        self.assertIn("client_max_body_size 8M;", changed)
        self.assertNotIn("client_max_body_size 8k;", changed)

    def test_changed_targets_are_rejected(self):
        with self.assertRaises(ValueError):
            candidate_admin(CONFIG_ANCHOR + ROUTE_ANCHOR + ROUTE_ANCHOR)
        with self.assertRaises(ValueError):
            candidate_nginx("unrelated", "client_max_body_size 360k;")

    def test_partial_file_replacement_is_rolled_back(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            intake = project / "dev/intake"
            intake.mkdir(parents=True)
            admin = root / "admin_app.py"
            nginx = root / "nocturne"
            python = root / "python"
            backups = root / "backups"
            original_admin = "imports\n" + CONFIG_ANCHOR + "helpers\n" + ROUTE_ANCHOR + "def route(): pass\n"
            repository = """location = /api/plugin/dev/drops {
    client_max_body_size 360k;
    proxy_pass http://127.0.0.1:5072;
}
"""
            original_nginx = "server {\n" + "\n".join(
                "    " + line for line in repository.replace("360k", "8k").strip().splitlines()
            ) + "\n}\n"
            admin.write_text(original_admin)
            nginx.write_text(original_nginx)
            python.write_text("")
            (intake / "nginx-location.conf").write_text(repository)

            real_replace = os.replace
            replacements = 0

            def fail_second_replace(source, destination):
                nonlocal replacements
                replacements += 1
                if replacements == 2:
                    raise OSError("simulated nginx replacement failure")
                real_replace(source, destination)

            with patch("install_screenshot_support.os.replace", side_effect=fail_second_replace):
                with self.assertRaisesRegex(OSError, "simulated nginx replacement failure"):
                    install(project=project, admin_app=admin, nginx_site=nginx,
                            backup_root=backups, admin_python=python,
                            run=lambda _args: None)

            self.assertEqual(original_admin, admin.read_text())
            self.assertEqual(original_nginx, nginx.read_text())


if __name__ == "__main__":
    unittest.main()
