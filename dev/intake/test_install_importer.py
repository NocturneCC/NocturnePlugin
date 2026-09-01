from pathlib import Path
from contextlib import redirect_stdout
import io
import tempfile
import unittest

from install_importer import IMPORT_USER, install, wait_active


class ImporterInstallTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.systemd = self.root / "systemd"
        self.private = self.root / "private"
        self.state = self.private / "nocturne-plugin-dev"
        self.link = self.root / "state-link"
        self.database = self.root / "database"
        for path in (self.project / ".venv/bin", self.project / "dev/intake",
                     self.systemd, self.state, self.database):
            path.mkdir(parents=True, exist_ok=True)
        (self.project / ".venv/bin/python").touch()
        (self.project / "dev/intake/import_pending.py").touch()
        (self.project / "dev/intake/nocturne-plugin-import.service").write_text("service fixture\n")
        (self.project / "dev/intake/nocturne-plugin-import.timer").write_text("timer fixture\n")
        baseline = "[Service]\nProtectSystem=strict\n"
        (self.project / "dev/intake/nocturne-plugin-dev.service").write_text(
            baseline + "InaccessiblePaths=/srv/projects/database\n")
        (self.systemd / "nocturne-plugin-dev.service").write_text(baseline)
        (self.database / "RegularSubmissions.db").touch()
        (self.state / "test-drops.sqlite3").touch()
        self.link.symlink_to(self.state)
        self.commands = []

    def fake_command(self, args):
        self.commands.append(args)

    def invoke(self, run=None):
        return install(self.project, self.systemd, self.link, self.private,
                       self.database, run=run or self.fake_command,
                       which=lambda name: "/usr/bin/setfacl")

    def test_success_installs_units_acl_and_timer(self):
        with redirect_stdout(io.StringIO()):
            self.invoke()
        self.assertEqual("service fixture\n", (self.systemd / "nocturne-plugin-import.service").read_text())
        self.assertEqual("timer fixture\n", (self.systemd / "nocturne-plugin-import.timer").read_text())
        self.assertIn("InaccessiblePaths=/srv/projects/database",
                      (self.systemd / "nocturne-plugin-dev.service").read_text())
        flat = [" ".join(command) for command in self.commands]
        self.assertTrue(any(command.startswith("setfacl -m u:randal:r--") for command in flat))
        self.assertIn("systemctl enable --now nocturne-plugin-import.timer", flat)
        self.assertTrue(any(command.startswith("runuser -u randal") for command in flat))

    def test_failed_preview_rolls_back_units_acl_and_intake_hardening(self):
        def fail_start(args):
            self.fake_command(args)
            if args and args[0] == "runuser":
                raise RuntimeError("start failed")

        with self.assertRaises(RuntimeError):
            self.invoke(run=fail_start)
        self.assertFalse((self.systemd / "nocturne-plugin-import.service").exists())
        self.assertFalse((self.systemd / "nocturne-plugin-import.timer").exists())
        self.assertEqual("[Service]\nProtectSystem=strict\n",
                         (self.systemd / "nocturne-plugin-dev.service").read_text())
        flat = [" ".join(command) for command in self.commands]
        self.assertTrue(any(command.startswith("setfacl -x u:randal") for command in flat))

    def test_preexisting_unit_stops_without_changes(self):
        (self.systemd / "nocturne-plugin-import.service").touch()
        with self.assertRaises(ValueError):
            self.invoke()
        self.assertEqual([], self.commands)

    def test_active_check_retries_transient_systemd_state(self):
        attempts = []

        def transient(args):
            attempts.append(args)
            if len(attempts) < 3:
                raise RuntimeError("activating")

        wait_active("example.service", run=transient, attempts=3, delay=0)
        self.assertEqual(3, len(attempts))


if __name__ == "__main__":
    unittest.main()
