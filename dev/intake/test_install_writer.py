from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest

from install_writer import baseline_intake, install


class WriterInstallTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.systemd = self.root / "systemd"
        self.database = self.root / "database"
        self.backup = self.root / "backup"
        sources = self.project / "dev/intake"
        for path in (self.project / ".venv/bin", sources, self.systemd, self.database):
            path.mkdir(parents=True, exist_ok=True)
        (self.project / ".venv/bin/python").touch()
        (sources / "pending_writer.py").touch()
        hardened = """[Unit]
After=nocturne-plugin-writer.service
Requires=nocturne-plugin-writer.service
[Service]
Environment=NOCTURNE_PENDING_SOCKET=/run/nocturne-plugin-writer/pending.sock
ProtectSystem=strict
InaccessiblePaths=/srv/projects/database
"""
        (sources / "nocturne-plugin-dev.service").write_text(hardened)
        (sources / "nocturne-plugin-writer.service").write_text("writer fixture\n")
        (self.systemd / "nocturne-plugin-dev.service").write_text(baseline_intake(hardened))
        (self.systemd / "nocturne-plugin-import.service").write_text("old service\n")
        (self.systemd / "nocturne-plugin-import.timer").write_text("old timer\n")
        for name in ("Members.db", "Items.db", "RegularSubmissions.db"):
            (self.database / name).touch()
        self.commands = []

    def command(self, args):
        self.commands.append(args)

    def invoke(self, run=None):
        return install(self.project, self.systemd, self.database, self.backup,
                       self.root / "pending.sock", run=run or self.command,
                       wait_for_socket=lambda path: None)

    def test_success_replaces_timer_with_writer_and_hardens_intake(self):
        with redirect_stdout(io.StringIO()):
            self.invoke()
        self.assertEqual("writer fixture\n",
                         (self.systemd / "nocturne-plugin-writer.service").read_text())
        self.assertFalse((self.systemd / "nocturne-plugin-import.timer").exists())
        self.assertFalse((self.systemd / "nocturne-plugin-import.service").exists())
        intake = (self.systemd / "nocturne-plugin-dev.service").read_text()
        self.assertIn("InaccessiblePaths=/srv/projects/database", intake)
        self.assertTrue((self.backup / "nocturne-plugin-import.timer").exists())
        flat = [" ".join(command) for command in self.commands]
        self.assertTrue(any(command.startswith("runuser -u randal") for command in flat))
        self.assertIn("systemctl enable --now nocturne-plugin-writer.service", flat)
        self.assertIn("systemctl restart nocturne-plugin-dev.service", flat)
        verify = next(command for command in self.commands if command[0] == "systemd-analyze")
        self.assertEqual("nocturne-plugin-dev.service", Path(verify[2]).name)
        self.assertEqual("staging", Path(verify[2]).parent.name)

    def test_start_failure_restores_original_units(self):
        def failing(args):
            self.command(args)
            if args == ["systemctl", "enable", "--now", "nocturne-plugin-writer.service"]:
                raise RuntimeError("writer failed")

        with self.assertRaises(RuntimeError):
            self.invoke(run=failing)
        self.assertFalse((self.systemd / "nocturne-plugin-writer.service").exists())
        self.assertEqual(baseline_intake(
            (self.project / "dev/intake/nocturne-plugin-dev.service").read_text()),
            (self.systemd / "nocturne-plugin-dev.service").read_text())
        self.assertEqual("old service\n",
                         (self.systemd / "nocturne-plugin-import.service").read_text())
        self.assertEqual("old timer\n",
                         (self.systemd / "nocturne-plugin-import.timer").read_text())

    def test_unexpected_intake_unit_stops_before_changes(self):
        (self.systemd / "nocturne-plugin-dev.service").write_text("unexpected\n")
        with self.assertRaises(ValueError):
            self.invoke()
        self.assertEqual([], self.commands)
        self.assertFalse(self.backup.exists())


if __name__ == "__main__":
    unittest.main()
