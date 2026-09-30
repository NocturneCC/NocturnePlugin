"""Guarded installer for the source-owned announcement admin page/navigation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from uuid import uuid4

from derived_review_support import (_apply_metadata, _capture_safe_metadata,
                                    _verify_metadata)


CARD = '''            <a class="card" href="/plugin-announcements-admin.html">
              <div class="icon">📣</div>
              <div class="card-title">Clan Announcements</div>
              <div class="card-desc">Draft, preview, publish, schedule, and withdraw RuneLite clan announcements.</div>
              <span class="tag">Event Admin</span>
            </a>

'''
ANCHOR = '            <a class="card" href="/event-manager.html">'
PURPOSE = "nocturne_announcement_admin_ui_v1"
DEFAULT_SITE_ROOT = Path("/srv/projects/website")
DEFAULT_BACKUP_ROOT = Path("/etc/nocturne-plugin-backups")
PAGE_NAME = "plugin-announcements-admin.html"


def digest_bytes(value):
    return hashlib.sha256(value).hexdigest()


def candidate_admin(original):
    if not isinstance(original, str) or original.count(ANCHOR) != 1:
        raise ValueError("admin navigation anchor is missing or ambiguous")
    if original.count('href="/plugin-announcements-admin.html"'):
        raise ValueError("announcement admin navigation is already partially installed")
    return original.replace(ANCHOR, CARD + ANCHOR, 1)


def _regular(path):
    value = Path(path).lstat()
    if not stat.S_ISREG(value.st_mode) or stat.S_ISLNK(value.st_mode) or value.st_nlink != 1:
        raise ValueError("website target is not a single-link regular file")
    return value


def _no_symlink_path(path):
    path = Path(path).absolute()
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current = current / component
        try:
            value = current.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(value.st_mode):
            raise ValueError("website path contains a symlink")


def inspect(site_root=DEFAULT_SITE_ROOT, *, source_page=None):
    root = Path(site_root)
    admin = root / "admin.html"
    page = root / PAGE_NAME
    _no_symlink_path(root)
    if root.is_symlink() or not root.is_dir():
        raise ValueError("website root is unsafe")
    admin_stat = _regular(admin)
    original = admin.read_bytes()
    try:
        text = original.decode("utf-8", errors="strict")
    except UnicodeError as error:
        raise ValueError("admin page is not valid UTF-8") from error
    if page.exists() or page.is_symlink():
        page_stat = _regular(page)
        if source_page is None or page.read_bytes() != Path(source_page).read_bytes():
            raise ValueError("announcement admin page already exists with unexpected content")
    else:
        page_stat = None
    card_present = 'href="/plugin-announcements-admin.html"' in text
    if card_present and (text.count(CARD) != 1 or text.count('href="/plugin-announcements-admin.html"') != 1):
        raise ValueError("announcement navigation card differs from its exact source-owned form")
    if card_present != (page_stat is not None):
        raise ValueError("announcement page and navigation are only partially installed")
    updated = text.encode() if card_present else candidate_admin(text).encode("utf-8")
    page_bytes = None if source_page is None else Path(source_page).read_bytes()
    return {"state": "already_applied" if card_present else "not_applied",
            "admin": admin, "page": page, "admin_before_sha256": digest_bytes(original),
            "admin_after_sha256": digest_bytes(updated),
            "page_sha256": None if page_bytes is None else digest_bytes(page_bytes),
            "admin_metadata": _capture_safe_metadata(admin),
            "page_metadata": None if page_stat is None else _capture_safe_metadata(page)}


def _atomic_file(target, content, metadata):
    target = Path(target)
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.nocturne-", dir=target.parent)
    staged = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        _apply_metadata(staged, metadata)
        if _regular(staged).st_nlink != 1:
            raise ValueError("staged website file link count is unsafe")
        os.replace(staged, target)
        _verify_metadata(target, metadata)
        if target.read_bytes() != content:
            raise ValueError("installed website file differs from staged content")
        directory = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        staged.unlink(missing_ok=True)


def _verify_checkout(repo, commit):
    repo = Path(repo).resolve(strict=True)
    if not isinstance(commit, str) or len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        raise ValueError("exact lowercase commit SHA required")
    git = ["git", "-c", f"safe.directory={repo}", "-C", str(repo)]
    def output(args):
        result = subprocess.run(git + args, check=True, timeout=15,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return result.stdout.strip()
    if Path(output(["rev-parse", "--show-toplevel"])).resolve() != repo:
        raise ValueError("unexpected source repository")
    if output(["rev-parse", "HEAD"]) != commit:
        raise ValueError("source HEAD differs from requested commit")
    if output(["status", "--porcelain=v1", "--untracked-files=all"]):
        raise ValueError("source checkout is dirty")
    if output(["rev-parse", "--verify", commit + "^{commit}"]) != commit:
        raise ValueError("requested source commit is unavailable")


def install(site_root=DEFAULT_SITE_ROOT, source_page=None, backup_root=DEFAULT_BACKUP_ROOT, *,
            apply=False, repo=Path(__file__).resolve().parents[2], commit=None):
    if source_page is None:
        source_page = Path(__file__).with_name(PAGE_NAME)
    source_page = Path(source_page)
    if not source_page.is_file() or source_page.is_symlink():
        raise ValueError("source announcement page is missing or unsafe")
    if commit is not None:
        _verify_checkout(repo, commit)
    planned = inspect(site_root, source_page=source_page)
    report = {key: value for key, value in planned.items()
              if key not in {"admin", "page", "admin_metadata", "page_metadata"}}
    report.update({"dry_run": not apply, "backup": None})
    if not apply or planned["state"] == "already_applied":
        return report
    if os.geteuid() != 0:
        raise PermissionError("--apply requires root")
    admin, page = planned["admin"], planned["page"]
    admin_metadata = planned["admin_metadata"]
    page_metadata = dict(admin_metadata)
    if page.exists() or page.is_symlink():
        raise ValueError("announcement admin page appeared after preflight")
    if digest_bytes(admin.read_bytes()) != planned["admin_before_sha256"]:
        raise ValueError("admin navigation changed after preflight")
    backup_root = Path(backup_root)
    if backup_root.is_symlink() or not backup_root.is_dir():
        raise ValueError("announcement UI backup root is unsafe")
    if backup_root == DEFAULT_BACKUP_ROOT:
        metadata = backup_root.lstat()
        if metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise ValueError("announcement UI backup root ownership/mode is unsafe")
    backup = backup_root / ("ui-" + uuid4().hex)
    backup.mkdir(mode=0o700)
    saved_admin = backup / "admin.html.before"
    shutil.copyfile(admin, saved_admin)
    _apply_metadata(saved_admin, admin_metadata)
    if digest_bytes(saved_admin.read_bytes()) != planned["admin_before_sha256"]:
        raise ValueError("admin navigation backup verification failed")
    manifest = {"purpose": PURPOSE, "status": "verified", "admin": str(admin),
                "page": str(page), "admin_before_sha256": planned["admin_before_sha256"],
                "admin_after_sha256": planned["admin_after_sha256"],
                "page_sha256": planned["page_sha256"],
                "admin_metadata": admin_metadata, "page_metadata": page_metadata}
    manifest_path = backup / "MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    with manifest_path.open("rb") as value:
        os.fsync(value.fileno())
    changed_admin = False
    created_page = False
    try:
        updated = candidate_admin(admin.read_text(encoding="utf-8")).encode("utf-8")
        changed_admin = True
        _regular(admin)
        if (_capture_safe_metadata(admin) != admin_metadata
                or digest_bytes(admin.read_bytes()) != planned["admin_before_sha256"]):
            raise ValueError("admin navigation changed immediately before replacement")
        _atomic_file(admin, updated, admin_metadata)
        fd, name = tempfile.mkstemp(prefix=f".{page.name}.nocturne-", dir=page.parent)
        staged = Path(name)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(source_page.read_bytes())
                output.flush()
                os.fsync(output.fileno())
            _apply_metadata(staged, page_metadata)
            os.link(staged, page, follow_symlinks=False)
            created_page = True
            staged.unlink()
            _verify_metadata(page, page_metadata)
            if digest_bytes(page.read_bytes()) != planned["page_sha256"]:
                raise ValueError("installed announcement page verification failed")
            directory = os.open(page.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            staged.unlink(missing_ok=True)
    except BaseException:
        if created_page and page.is_file() and not page.is_symlink() \
                and page.stat().st_nlink == 1 and digest_bytes(page.read_bytes()) == planned["page_sha256"]:
            page.unlink()
        if changed_admin:
            _atomic_file(admin, saved_admin.read_bytes(), admin_metadata)
        raise
    report.update(state="already_applied", dry_run=False, backup=str(backup))
    return report


def rollback(backup, site_root=DEFAULT_SITE_ROOT):
    backup = Path(backup)
    if backup.is_symlink() or not backup.is_dir():
        raise ValueError("UI rollback backup is unsafe")
    manifest_path = backup / "MANIFEST.json"
    _regular(manifest_path)
    if manifest_path.stat().st_size > 16 * 1024:
        raise ValueError("UI rollback manifest exceeds limit")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("purpose") != PURPOSE or manifest.get("status") != "verified":
        raise ValueError("UI rollback manifest is invalid")
    admin, page = Path(manifest["admin"]), Path(manifest["page"])
    expected_root = Path(site_root).absolute()
    if admin != expected_root / "admin.html" or page != expected_root / PAGE_NAME:
        raise ValueError("UI rollback targets differ from the exact website root")
    _no_symlink_path(expected_root)
    _regular(admin)
    if digest_bytes(admin.read_bytes()) != manifest["admin_after_sha256"]:
        raise ValueError("admin page changed since UI install")
    if _capture_safe_metadata(admin) != manifest["admin_metadata"]:
        raise ValueError("admin page metadata changed since UI install")
    if not page.is_file() or page.is_symlink() or page.stat().st_nlink != 1 \
            or digest_bytes(page.read_bytes()) != manifest["page_sha256"]:
        raise ValueError("announcement page changed since UI install")
    if _capture_safe_metadata(page) != manifest["page_metadata"]:
        raise ValueError("announcement page metadata changed since UI install")
    before = backup / "admin.html.before"
    _regular(before)
    if _capture_safe_metadata(before) != manifest["admin_metadata"]:
        raise ValueError("admin navigation backup metadata mismatch")
    if digest_bytes(before.read_bytes()) != manifest["admin_before_sha256"]:
        raise ValueError("UI rollback backup checksum mismatch")
    if os.geteuid() != 0:
        raise PermissionError("rollback requires root")
    _atomic_file(admin, before.read_bytes(), manifest["admin_metadata"])
    page.unlink()
    directory = os.open(page.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return {"state": "rolled_back", "admin": str(admin), "page_removed": str(page)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-root", default=str(DEFAULT_SITE_ROOT))
    parser.add_argument("--backup-root", default=str(DEFAULT_BACKUP_ROOT))
    parser.add_argument("--commit", required=True)
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback-backup")
    args = parser.parse_args()
    if args.rollback_backup:
        result = rollback(args.rollback_backup, args.site_root)
    else:
        result = install(args.site_root, backup_root=args.backup_root, apply=args.apply,
                         repo=args.repo, commit=args.commit)
    print(json.dumps(result, sort_keys=True))
    if not args.apply and not args.rollback_backup:
        print("Dry run only; no website files were changed.")


if __name__ == "__main__":
    main()
