"""Install the isolated pending-import service after inspected Midgard checks.

The public DynamicUser remains unable to read live databases. The importer uses
the existing trusted `randal` database owner, with ACL read access to intake.
"""
import os
from pathlib import Path
import shutil
import subprocess


IMPORT_USER = "randal"


def command(args):
    result = subprocess.run(args, timeout=30)
    if result.returncode:
        raise RuntimeError("Command failed: " + " ".join(args))


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
    service = systemd / "nocturne-plugin-import.service"
    timer = systemd / "nocturne-plugin-import.timer"
    if service.exists() or timer.exists():
        raise ValueError("Importer unit already exists; inspect it instead of overwriting it")
    if which("setfacl") is None:
        raise ValueError("setfacl is unavailable; install the Debian acl package first")
    sources = project / "dev/intake"
    created_service = created_timer = False
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
        run(["systemd-analyze", "verify", str(service), str(timer)])
        run(["systemctl", "daemon-reload"])
        run(["runuser", "-u", IMPORT_USER, "--",
             str(project / ".venv/bin/python"), "-B",
             str(project / "dev/intake/import_pending.py"), "--limit", "50"])
        run(["systemctl", "enable", "--now", timer.name])
        run(["systemctl", "is-active", "--quiet", timer.name])
    except BaseException:
        if created_timer:
            try:
                run(["systemctl", "disable", "--now", timer.name])
            except Exception:
                pass
            timer.unlink(missing_ok=True)
        if created_service:
            service.unlink(missing_ok=True)
        if created_service or created_timer:
            try:
                run(["systemctl", "daemon-reload"])
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
    print("The public intake still has no live-database access; imported rows remain pending.")


if __name__ == "__main__":
    if os.geteuid() != 0:
        raise SystemExit("Run this installer with sudo on Midgard")
    try:
        install()
    except Exception as error:
        raise SystemExit(f"Importer installation stopped: {error}")
