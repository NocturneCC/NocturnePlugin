"""Install only the inspected Nocturne test-intake nginx route on Midgard.

Run with sudo after the loopback intake test passes. No live DB changes.
Stops on unexpected config, snapshots originals outside includes, and restores
files on validation/reload failure. Tests inject temporary paths and fake commands.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ANCHOR = "    # SSL managed by Certbot\n"
HTTPS_HEADER = (
    "server {\n    listen 443 ssl;\n    listen [::]:443 ssl;\n\n"
    "    server_name nocturne.events www.nocturne.events;\n\n" + ANCHOR
)
MARKER = "    # Nocturne plugin development intake\n"


def candidate_site(original, location):
    if "/api/plugin/dev/drops" in original or MARKER in original:
        raise ValueError("Intake route already exists; inspect it before rerunning.")
    if original.count(HTTPS_HEADER) != 1 or original.count(ANCHOR) != 1:
        raise ValueError("HTTPS header differs from the inspected configuration; no changes made.")
    block = MARKER + "\n".join("    " + line if line else "" for line in location.strip().splitlines()) + "\n\n"
    return original.replace(ANCHOR, block + ANCHOR, 1)


def command(args):
    result = subprocess.run(args, timeout=30)
    if result.returncode:
        raise RuntimeError("Command failed: " + " ".join(args))


def install(root=Path("/etc/nginx"), snippets=None, run=command):
    root = Path(root)
    snippets = Path(snippets) if snippets else Path(__file__).resolve().parent
    site = root / "sites-enabled/nocturne"
    duplicate = root / "sites-enabled/nocturne.backup"
    limits = root / "conf.d/nocturne-plugin-dev-limits.conf"
    for path in (site, duplicate):
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Expected the inspected regular file: {path}")
    if limits.exists() or limits.is_symlink():
        raise ValueError(f"Limits file already exists: {limits}; no changes made.")
    original = site.read_text()
    old = duplicate.read_text()
    # The user inspected exactly this one-line difference. Refuse other changes.
    extra_line = "        client_max_body_size 15M;\n"
    if old != original and not (original.count(extra_line) == 1 and old == original.replace(extra_line, "", 1)):
        raise ValueError("Backup differs beyond the inspected body-size line; no changes made.")
    location_text = (snippets / "nginx-location.conf").read_text()
    limits_text = (snippets / "nginx-limits.conf").read_text()
    candidate = candidate_site(original, location_text)
    run(["systemctl", "is-active", "--quiet", "nocturne-plugin-dev.service"])
    run(["nginx", "-t"])

    archive_root = root / "nocturne-plugin-backups"
    archive_root.mkdir(mode=0o700, exist_ok=True)
    archive = Path(tempfile.mkdtemp(prefix="install-", dir=archive_root))
    snapshot = archive / "nocturne.before"
    moved_backup = archive / "nocturne.backup"
    shutil.copy2(site, snapshot)
    staged = archive / "nocturne.staged"
    shutil.copy2(site, staged)
    staged.write_text(candidate)
    limits_created = False
    backup_moved = False
    site_changed = False
    reload_attempted = False
    try:
        # Recheck before replacing files to avoid applying to changed input.
        if site.read_text() != original or duplicate.read_text() != old:
            raise ValueError("Configuration changed during preparation; stopping.")
        with limits.open("x") as stream:
            limits_created = True
            stream.write(limits_text)
        limits.chmod(0o644)
        duplicate.rename(moved_backup)
        backup_moved = True
        os.replace(staged, site)
        site_changed = True
        run(["nginx", "-t"])
        reload_attempted = True
        run(["systemctl", "reload", "nginx"])
    except BaseException:
        if site_changed:
            restored = archive / "nocturne.restore"
            shutil.copy2(snapshot, restored)
            os.replace(restored, site)
        if backup_moved:
            moved_backup.rename(duplicate)
        if limits_created:
            limits.unlink()
        print(f"Previous config files restored. Snapshot: {archive}", flush=True)
        if reload_attempted:
            try:
                run(["nginx", "-t"])
                run(["systemctl", "reload", "nginx"])
            except Exception as error:
                print(f"Restored files, but could not confirm nginx reload: {error}", flush=True)
        raise
    print(f"HTTPS intake route installed; nginx reload requested. Backups: {archive}", flush=True)
    print("Next: test https://nocturne.events/api/plugin/dev/drops before enabling RuneLite submissions.", flush=True)
    return archive


if __name__ == "__main__":
    if os.geteuid() != 0:
        raise SystemExit("Run this installer with sudo on Midgard.")
    try:
        install()
    except Exception as error:
        raise SystemExit(f"Installation stopped: {error}")
