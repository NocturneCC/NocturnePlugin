"""Install the local pending writer without granting access to intake state."""
import os
from pathlib import Path
import shutil
import subprocess
import time


OBSOLETE_UNITS = ("nocturne-plugin-import.service", "nocturne-plugin-import.timer")


def command(args):
    result = subprocess.run(args, timeout=30)
    if result.returncode:
        raise RuntimeError("Command failed: " + " ".join(args))


def wait_active(unit, run=command, attempts=20, delay=0.25):
    last_error = None
    for _ in range(attempts):
        try:
            run(["systemctl", "is-active", "--quiet", unit])
            return
        except RuntimeError as error:
            last_error = error
            time.sleep(delay)
    raise RuntimeError(f"{unit} did not become active") from last_error


def wait_socket(path, attempts=20, delay=0.25):
    for _ in range(attempts):
        if path.is_socket():
            return
        time.sleep(delay)
    raise RuntimeError(f"Writer socket did not appear: {path}")


def baseline_intake(hardened):
    for line in (
        "After=nocturne-plugin-writer.service\n",
        "Requires=nocturne-plugin-writer.service\n",
        "Environment=NOCTURNE_PENDING_SOCKET=/run/nocturne-plugin-writer/pending.sock\n",
        "InaccessiblePaths=/srv/projects/database\n",
    ):
        if hardened.count(line) != 1:
            raise ValueError(f"Repository intake unit is missing: {line.strip()}")
        hardened = hardened.replace(line, "", 1)
    return hardened


def install(project=Path("/srv/projects/nocturne-plugin-intake"),
            systemd=Path("/etc/systemd/system"),
            database=Path("/srv/projects/database"),
            backup=Path("/etc/nocturne-plugin-backups/unix-writer"),
            socket_path=Path("/run/nocturne-plugin-writer/pending.sock"),
            run=command, wait_for_socket=wait_socket):
    project, systemd, database = Path(project), Path(systemd), Path(database)
    backup, socket_path = Path(backup), Path(socket_path)
    sources = project / "dev/intake"
    intake_unit = systemd / "nocturne-plugin-dev.service"
    writer_unit = systemd / "nocturne-plugin-writer.service"
    required = (
        project / ".venv/bin/python",
        sources / "pending_writer.py",
        sources / "nocturne-plugin-dev.service",
        sources / "nocturne-plugin-writer.service",
        intake_unit,
        database / "Members.db",
        database / "Items.db",
        database / "RegularSubmissions.db",
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise ValueError("Missing inspected path(s): " + ", ".join(sorted(missing)))
    if writer_unit.exists():
        raise ValueError("Writer unit already exists; inspect it instead of overwriting it")
    if backup.exists():
        raise ValueError(f"Backup path already exists: {backup}")

    hardened = (sources / intake_unit.name).read_text()
    if intake_unit.read_text() != baseline_intake(hardened):
        raise ValueError("Installed intake unit differs from the expected pre-writer version")

    run(["runuser", "-u", "randal", "--", str(project / ".venv/bin/python"),
         "-B", str(sources / "pending_writer.py"), "--database-dir", str(database), "--check"])

    backup.mkdir(parents=True, mode=0o700)
    shutil.copy2(intake_unit, backup / intake_unit.name)
    for name in OBSOLETE_UNITS:
        old = systemd / name
        if old.exists():
            shutil.copy2(old, backup / name)

    staged_intake = systemd / ".nocturne-plugin-dev.service.writer-staged"
    created_writer = replaced_intake = False
    try:
        run(["systemctl", "disable", "--now", "nocturne-plugin-import.timer"])
        for name in OBSOLETE_UNITS:
            (systemd / name).unlink(missing_ok=True)
        with writer_unit.open("x") as stream:
            stream.write((sources / writer_unit.name).read_text())
        writer_unit.chmod(0o644)
        created_writer = True
        staged_intake.write_text(hardened)
        staged_intake.chmod(0o644)
        run(["systemd-analyze", "verify", str(staged_intake), str(writer_unit)])
        os.replace(staged_intake, intake_unit)
        replaced_intake = True
        run(["systemctl", "daemon-reload"])
        run(["systemctl", "enable", "--now", writer_unit.name])
        wait_active(writer_unit.name, run=run)
        wait_for_socket(socket_path)
        run(["systemctl", "restart", intake_unit.name])
        wait_active(intake_unit.name, run=run)
    except BaseException:
        try:
            run(["systemctl", "disable", "--now", writer_unit.name])
        except Exception:
            pass
        if created_writer:
            writer_unit.unlink(missing_ok=True)
        staged_intake.unlink(missing_ok=True)
        if replaced_intake:
            shutil.copy2(backup / intake_unit.name, intake_unit)
        for name in OBSOLETE_UNITS:
            saved = backup / name
            if saved.exists():
                shutil.copy2(saved, systemd / name)
        try:
            run(["systemctl", "daemon-reload"])
            run(["systemctl", "restart", intake_unit.name])
        except Exception:
            pass
        raise
    print("Pending writer installed and active.")
    print("The public intake is blocked from /srv/projects/database.")
    print("Eligible reports create pending rows only; rank totals remain unchanged.")


if __name__ == "__main__":
    if os.geteuid() != 0:
        raise SystemExit("Run this installer with sudo on Midgard")
    try:
        install()
    except Exception as error:
        raise SystemExit(f"Writer installation stopped: {error}")
