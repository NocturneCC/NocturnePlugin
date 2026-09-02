"""Install bounded RuneLite screenshot support on the inspected Midgard stack."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from uuid import uuid4


IMAGE_CONFIG = 'RUNELITE_SUBMISSION_IMAGE_DIR = "/srv/projects/database/runelite-submission-images"\n'
CONFIG_ANCHOR = 'IMG_DIR      = "/srv/projects/database/images"\n'
ROUTE_ANCHOR = '@app.route("/admin/api/nocturne/regular-submissions", methods=["GET"])\n'
ROUTE_MARKER = '@app.route("/admin/api/nocturne/runelite-submission-images/<filename>", methods=["GET"])'
ROUTE = '''@app.route("/admin/api/nocturne/runelite-submission-images/<filename>", methods=["GET"])
@require_noc_super_admin
def nocturne_runelite_submission_image(filename):
    if not re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\\.jpg",
        str(filename or ""),
    ):
        return error("Image not found.", 404)

    response = send_from_directory(
        RUNELITE_SUBMISSION_IMAGE_DIR,
        filename,
        mimetype="image/jpeg",
        as_attachment=False,
        max_age=0,
    )
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


'''


def candidate_admin(original):
    if ROUTE_MARKER in original or "RUNELITE_SUBMISSION_IMAGE_DIR" in original:
        raise ValueError("RuneLite screenshot route already exists; inspect it before rerunning")
    if original.count(CONFIG_ANCHOR) != 1:
        raise ValueError("Admin image configuration anchor differs from inspection")
    if original.count(ROUTE_ANCHOR) != 1:
        raise ValueError("Admin regular-submission route anchor differs from inspection")
    changed = original.replace(CONFIG_ANCHOR, CONFIG_ANCHOR + IMAGE_CONFIG, 1)
    return changed.replace(ROUTE_ANCHOR, ROUTE + ROUTE_ANCHOR, 1)


def indent_location(text):
    return "\n".join("    " + line if line else "" for line in text.strip().splitlines())


def candidate_nginx(original, repository_location):
    new = indent_location(repository_location)
    old = new.replace("client_max_body_size 360k;", "client_max_body_size 8k;")
    if new == old:
        raise ValueError("Repository screenshot body limit is missing")
    if original.count(old) != 1:
        raise ValueError("Installed intake location differs from the expected 8k route")
    return original.replace(old, new, 1)


def command(args):
    result = subprocess.run(args, timeout=45)
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


def install(project=Path("/srv/projects/nocturne-plugin-intake"),
            admin_app=Path("/srv/projects/api/admin_app.py"),
            nginx_site=Path("/etc/nginx/sites-enabled/nocturne"),
            backup_root=Path("/etc/nocturne-plugin-backups"),
            admin_python=Path("/srv/projects/api/venv-simon/bin/python"),
            run=command):
    project, admin_app, nginx_site = Path(project), Path(admin_app), Path(nginx_site)
    backup_root, admin_python = Path(backup_root), Path(admin_python)
    location = project / "dev/intake/nginx-location.conf"
    required = (admin_app, nginx_site, location, admin_python)
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise ValueError("Missing inspected path(s): " + ", ".join(sorted(missing)))

    original_admin = admin_app.read_text()
    original_nginx = nginx_site.read_text()
    changed_admin = candidate_admin(original_admin)
    changed_nginx = candidate_nginx(original_nginx, location.read_text())

    units = (
        "nocturne-plugin-writer.service",
        "nocturne-plugin-dev.service",
        "osrs-drops-admin.service",
        "nginx.service",
    )
    for unit in units:
        run(["systemctl", "is-active", "--quiet", unit])
    run([str(admin_python), "-B", "-m", "unittest", "discover",
         "-s", str(project / "dev/intake"), "-q"])
    run(["nginx", "-t"])

    backup_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    backup = Path(tempfile.mkdtemp(
        prefix=f"screenshot-support-{stamp}-{uuid4().hex[:8]}-",
        dir=backup_root,
    ))
    saved_admin = backup / "admin_app.py"
    saved_nginx = backup / "nocturne"
    admin_stat = admin_app.stat()
    nginx_stat = nginx_site.stat()
    shutil.copy2(admin_app, saved_admin)
    shutil.copy2(nginx_site, saved_nginx)
    staged_admin = backup / "admin_app.staged.py"
    staged_nginx = backup / "nocturne.staged"
    staged_admin.write_text(changed_admin)
    staged_nginx.write_text(changed_nginx)
    os.chmod(staged_admin, admin_stat.st_mode & 0o7777)
    os.chown(staged_admin, admin_stat.st_uid, admin_stat.st_gid)
    os.chmod(staged_nginx, nginx_stat.st_mode & 0o7777)
    os.chown(staged_nginx, nginx_stat.st_uid, nginx_stat.st_gid)
    run([str(admin_python), "-B", "-m", "py_compile", str(staged_admin)])

    admin_applied = False
    nginx_applied = False
    try:
        if admin_app.read_text() != original_admin or nginx_site.read_text() != original_nginx:
            raise ValueError("A target changed during preparation; no deployment attempted")
        os.replace(staged_admin, admin_app)
        admin_applied = True
        os.replace(staged_nginx, nginx_site)
        nginx_applied = True
        run(["nginx", "-t"])
        for unit in units[:3]:
            run(["systemctl", "restart", unit])
            wait_active(unit, run=run)
        run(["systemctl", "reload", "nginx.service"])
        wait_active("nginx.service", run=run)
    except BaseException:
        if admin_applied or nginx_applied:
            if admin_applied:
                shutil.copy2(saved_admin, admin_app)
            if nginx_applied:
                shutil.copy2(saved_nginx, nginx_site)
            try:
                run(["nginx", "-t"])
                for unit in units[:3]:
                    run(["systemctl", "restart", unit])
                run(["systemctl", "reload", "nginx.service"])
            except Exception as restore_error:
                print(f"Files restored, but service recovery needs attention: {restore_error}", flush=True)
        raise

    print("RuneLite screenshot support installed.")
    print(f"Previous admin and nginx files: {backup}")
    print("Images require noc_super_admin and are never placed under the public website root.")
    return backup


if __name__ == "__main__":
    if os.geteuid() != 0:
        raise SystemExit("Run this installer with sudo on Midgard")
    try:
        install()
    except Exception as error:
        raise SystemExit(f"Screenshot installation stopped: {error}")
