"""Install the isolated pending-import service after inspected Midgard checks.

The public DynamicUser remains unable to read live databases. The importer uses
the existing trusted `randal` database owner, with ACL read access to intake.
"""
import os
from pathlib import Path
import shutil
import subprocess
import time


IMPORT_USER = "randal"


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


def require_inspected_paths(project, systemd, state_link, database):
    expected = {
        project / ".venv/bin/python",
        project / "dev/intake/import_pending.py",
        systemd / "nocturne-plugin-dev.service",
        database / "RegularSubmissions.db",
    }
    missing = [str(path) for path in expected if not path.exists()]
    if missing:
        raise ValueError("Missing inspected path(s): " + ", ".join(sorted(missing)))
    if not state_link.is_symlink():
        raise ValueError(f"Expected DynamicUser state symlink: {state_link}")
    state = state_link.resolve(strict=True)
    intake = state / "test-drops.sqlite3"
    if state.name != "nocturne-plugin-dev" or not intake.is_file():
        raise ValueError("Unexpected intake state target")
    return state, intake


def install(project=Path("/srv/projects/nocturne-plugin-intake"),
            systemd=Path("/etc/systemd/system"),
            state_link=Path("/var/lib/nocturne-plugin-dev"),
            private_root=Path("/var/lib/private"),
            database=Path("/srv/projects/database"), run=command,
            which=shutil.which):
    project, systemd = Path(project), Path(systemd)
    state_link, private_root, database = Path(state_link), Path(private_root), Path(database)
    state, intake = require_inspected_paths(project, systemd, state_link, database)
    intake_service = systemd / "nocturne-plugin-dev.service"
    service = systemd / "nocturne-plugin-import.service"
    timer = systemd / "nocturne-plugin-import.timer"
    if service.exists() or timer.exists():
        raise ValueError("Importer unit already exists; inspect it instead of overwriting it")
    if which("setfacl") is None:
        raise ValueError("setfacl is unavailable; install the Debian acl package first")
    sources = project / "dev/intake"
    hardened_intake = (sources / intake_service.name).read_text()
    hardening_line = "InaccessiblePaths=/srv/projects/database\n"
    if hardened_intake.count(hardening_line) != 1:
        raise ValueError("Repository intake unit is missing the expected database isolation")
    original_intake = hardened_intake.replace(hardening_line, "", 1)
    if intake_service.read_text() != original_intake:
        raise ValueError("Installed intake unit differs from the inspected version")
    created_service = created_timer = False
    intake_hardened = False
    staged_intake = systemd / ".nocturne-plugin-dev.service.staged"
    acl_paths = []
    try:
        for path, entry in (
            (private_root, f"u:{IMPORT_USER}:--x"),
            (state, f"u:{IMPORT_USER}:r-x"),
            (intake, f"u:{IMPORT_USER}:r--"),
        ):
            run(["setfacl", "-m", entry, str(path)])
            acl_paths.append(path)
        run(["setfacl", "-m", f"d:u:{IMPORT_USER}:r--", str(state)])

        with service.open("x") as stream:
            stream.write((sources / service.name).read_text())
        created_service = True
        service.chmod(0o644)
        with timer.open("x") as stream:
            stream.write((sources / timer.name).read_text())
        created_timer = True
        timer.chmod(0o644)
        run(["systemd-analyze", "verify", str(sources / intake_service.name),
             str(service), str(timer)])
        with staged_intake.open("x") as stream:
            stream.write(hardened_intake)
        staged_intake.chmod(0o644)
        os.replace(staged_intake, intake_service)
        intake_hardened = True
        run(["systemctl", "daemon-reload"])
        run(["systemctl", "restart", intake_service.name])
        wait_active(intake_service.name, run=run)
        run(["runuser", "-u", IMPORT_USER, "--",
             str(project / ".venv/bin/python"), "-B",
             str(project / "dev/intake/import_pending.py"), "--limit", "50"])
        run(["systemctl", "enable", "--now", timer.name])
        wait_active(timer.name, run=run)
    except BaseException:
        if created_timer:
            try:
                run(["systemctl", "disable", "--now", timer.name])
            except Exception:
                pass
            timer.unlink(missing_ok=True)
        if created_service:
            service.unlink(missing_ok=True)
        staged_intake.unlink(missing_ok=True)
        if intake_hardened:
            restore = systemd / ".nocturne-plugin-dev.service.restore"
            try:
                restore.write_text(original_intake)
                restore.chmod(0o644)
                os.replace(restore, intake_service)
            except Exception:
                pass
        try:
            run(["systemctl", "daemon-reload"])
            if intake_hardened:
                run(["systemctl", "restart", intake_service.name])
        except Exception:
            pass
        try:
            run(["setfacl", "-x", f"d:u:{IMPORT_USER}", str(state)])
        except Exception:
            pass
        for path in reversed(acl_paths):
            try:
                run(["setfacl", "-x", f"u:{IMPORT_USER}", str(path)])
            except Exception:
                pass
        raise
    print("Importer timer installed and active.")
    print("The public intake cannot access /srv/projects/database; imported rows remain pending.")


if __name__ == "__main__":
    if os.geteuid() != 0:
        raise SystemExit("Run this installer with sudo on Midgard")
    try:
        install()
    except Exception as error:
        raise SystemExit(f"Importer installation stopped: {error}")
