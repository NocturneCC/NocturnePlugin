import tempfile
from contextlib import redirect_stdout
from io import StringIO
import unittest
from pathlib import Path
from install_nginx import HTTPS_HEADER, candidate_site, install


class NginxInstallTest(unittest.TestCase):
    def setUp(self):
        quiet = redirect_stdout(StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "sites-enabled").mkdir()
        (self.root / "conf.d").mkdir()
        self.site = self.root / "sites-enabled/nocturne"
        self.old = self.root / "sites-enabled/nocturne.backup"
        self.content = "server { listen 80; return 301 https://$host$request_uri; }\n" + HTTPS_HEADER + "    location / { return 200; }\n}\n"
        self.site.write_text(self.content)
        self.old.write_text(self.content)
        self.snippets = Path(__file__).parent
        self.calls = []

    def run_ok(self, args):
        self.calls.append(args)

    def test_success_keeps_other_routes_and_archives_duplicate(self):
        archive = install(self.root, self.snippets, self.run_ok)
        changed = self.site.read_text()
        self.assertEqual(1, changed.count("location = /api/plugin/dev/drops"))
        self.assertIn("    location / { return 200; }", changed)
        self.assertIn("limit_req zone=nocturne_plugin_dev_ip", changed)
        self.assertEqual(self.content, (archive / "nocturne.before").read_text())
        self.assertEqual(self.content, (archive / "nocturne.backup").read_text())
        self.assertFalse(self.old.exists())
        self.assertEqual(["nginx", "-t"], self.calls[-2])
        self.assertEqual(["systemctl", "reload", "nginx"], self.calls[-1])

    def test_failed_validation_restores_every_file_without_reload(self):
        def fail_second_check(args):
            self.calls.append(args)
            if self.calls.count(["nginx", "-t"]) == 2:
                raise RuntimeError("test syntax error")
        with self.assertRaises(RuntimeError):
            install(self.root, self.snippets, fail_second_check)
        self.assertEqual(self.content, self.site.read_text())
        self.assertEqual(self.content, self.old.read_text())
        self.assertFalse((self.root / "conf.d/nocturne-plugin-dev-limits.conf").exists())
        self.assertNotIn(["systemctl", "reload", "nginx"], self.calls)

    def test_failed_reload_restores_and_reloads_old_configuration(self):
        def fail_first_reload(args):
            self.calls.append(args)
            if args == ["systemctl", "reload", "nginx"] and self.calls.count(args) == 1:
                raise RuntimeError("test reload failure")
        with self.assertRaises(RuntimeError):
            install(self.root, self.snippets, fail_first_reload)
        self.assertEqual(self.content, self.site.read_text())
        self.assertTrue(self.old.exists())
        self.assertEqual(2, self.calls.count(["systemctl", "reload", "nginx"]))

    def test_unexpected_backup_aborts_without_changes(self):
        self.old.write_text("unrelated server configuration")
        with self.assertRaises(ValueError):
            install(self.root, self.snippets, self.run_ok)
        self.assertEqual(self.content, self.site.read_text())
        self.assertEqual([], self.calls)

    def test_inspected_body_size_difference_is_allowed(self):
        self.site.write_text(self.content.replace("    location /", "        client_max_body_size 15M;\n    location /"))
        install(self.root, self.snippets, self.run_ok)
        self.assertIn("client_max_body_size 15M;", self.site.read_text())

    def test_changed_header_and_preexisting_route_are_rejected(self):
        for value in [self.content.replace("listen 443 ssl;", "listen 444 ssl;"), self.content + "/api/plugin/dev/drops"]:
            with self.assertRaises(ValueError):
                candidate_site(value, "location = /api/plugin/dev/drops {}")


if __name__ == "__main__":
    unittest.main()
