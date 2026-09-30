"""Guarded installation/rollback of the announcement snapshot writer boundary."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import tempfile
from uuid import uuid4

from derived_review_support import (_apply_metadata, _capture_safe_metadata,
                                    _verify_metadata)
from announcement_snapshot_writer import _safe_directory
from announcements import DEFAULT_PUBLIC_SNAPSHOT


PURPOSE = "nocturne_announcement_publication_boundary_v1"
STOPPED_SERVICES = frozenset({"osrs-drops-admin.service",
                              "nocturne-announcement-snapshot-writer.service",
                              "nocturne-announcement-snapshot-writer.socket"})
UNIT_FILES = {"nocturne-announcement-snapshot-writer.service",
              "nocturne-announcement-snapshot-writer.socket"}
BASIC_ROOT_FILE = {"uid": 0, "gid": 0, "mode": 0o644,
                   "acl": "user::rw-\ngroup::r--\nother::r--\n\n"}


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _run(args, **kwargs):
    return subprocess.run(args, check=True, timeout=20, **kwargs)


def _require_services_stopped(confirmed, stopped_services, service_active):
    if not confirmed or set(stopped_services) != STOPPED_SERVICES:
        raise ValueError("operation requires exact admin and snapshot-writer stopped-service confirmation")
    active = service_active or (lambda unit: subprocess.run(
        ["systemctl", "is-active", "--quiet", unit], check=False, timeout=10).returncode == 0)
    running = sorted(unit for unit in STOPPED_SERVICES if active(unit))
    if running:
        raise ValueError("announcement publication services are active: " + ", ".join(running))


def _source_files(source_dir):
    source_dir = Path(source_dir)
    for name in ("announcements.py", "announcement_snapshot_writer.py", *UNIT_FILES):
        path = source_dir / name
        if not path.is_file() or path.is_symlink() or path.stat().st_nlink != 1:
            raise ValueError("publication source artifact is missing or unsafe")
    result = {"nocturne_announcements.py": (source_dir / "announcements.py").read_bytes(),
              "announcement_snapshot_writer.py": (source_dir / "announcement_snapshot_writer.py").read_bytes()}
    for name in UNIT_FILES:
        result[name] = (source_dir / name).read_bytes()
    for name in ("nocturne_announcements.py", "announcement_snapshot_writer.py", *UNIT_FILES):
        if not (source_dir / ("announcements.py" if name == "nocturne_announcements.py" else name)).is_file():
            raise ValueError("publication source artifact is missing")
    return result


def _safe_target(path, *, optional=False):
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        if optional:
            return None
        raise ValueError("required publication target is missing")
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
        raise ValueError("publication target is not a single-link regular file")
    return info


def _source_trust(repo, commit):
    repo = Path(repo).resolve(strict=True)
    if not isinstance(commit, str) or len(commit) != 40 or any(c not in "0123456789abcdef" for c in commit):
        raise ValueError("exact full commit SHA required")
    command = ["git", "-c", f"safe.directory={repo}", "-C", str(repo)]
    def output(args):
        value = subprocess.run(command + args, check=True, timeout=15, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return value.stdout.strip()
    if Path(output(["rev-parse", "--show-toplevel"])).resolve() != repo:
        raise ValueError("unexpected source checkout")
    if output(["rev-parse", "HEAD"]) != commit or output(["status", "--porcelain=v1", "--untracked-files=all"]):
        raise ValueError("source checkout is not clean at requested commit")
    if output(["rev-parse", "--verify", commit + "^{commit}"]) != commit:
        raise ValueError("requested commit is unavailable")


def _stage(target, raw, metadata):
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.announcement-", dir=target.parent)
    staged = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        _apply_metadata(staged, metadata)
        if staged.read_bytes() != raw:
            raise ValueError("staged publication artifact mismatch")
        return staged
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def install(*, repo, commit, source_dir=None, api_dir=Path("/srv/projects/api"),
            library_dir=Path("/usr/local/lib/nocturne-plugin"),
            systemd_dir=Path("/etc/systemd/system"),
            backup_root=Path("/etc/nocturne-plugin-backups"), apply=False,
            maintenance_confirmed=False, stopped_services=(), service_active=None,
            expected_module_sha256=None, run=_run):
    _source_trust(repo, commit)
    source_dir = Path(source_dir or Path(__file__).parent)
    api_dir, library_dir, systemd_dir, backup_root = map(
        Path, (api_dir, library_dir, systemd_dir, backup_root))
    sources = _source_files(source_dir)
    nobody = pwd.getpwnam("nobody")
    _safe_directory(DEFAULT_PUBLIC_SNAPSHOT, nobody.pw_uid, nobody.pw_gid)
    targets = {
        "nocturne_announcements.py": api_dir / "nocturne_announcements.py",
        "announcement_snapshot_writer.py": library_dir / "announcement_snapshot_writer.py",
        "announcements.py": library_dir / "announcements.py",
        **{name: systemd_dir / name for name in UNIT_FILES},
    }
    for directory in (api_dir, systemd_dir, backup_root):
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError("publication target directory is unsafe")
    for directory in (systemd_dir, backup_root):
        info = directory.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError("systemd or backup directory ownership/mode is unsafe")
    if library_dir.parent.is_symlink() or not library_dir.parent.is_dir():
        raise ValueError("writer library parent directory is unsafe")
    parent_info = library_dir.parent.lstat()
    if parent_info.st_uid != 0 or parent_info.st_mode & 0o022:
        raise ValueError("writer library parent ownership/mode is unsafe")
    if library_dir.is_symlink() or (library_dir.exists() and not library_dir.is_dir()):
        raise ValueError("publication library directory is unsafe")
    existing = {}
    for name, target in targets.items():
        info = _safe_target(target, optional=True)
        existing[name] = None if info is None else {
            "metadata": _capture_safe_metadata(target, run),
            "sha256": _hash(target.read_bytes()),
        }
    if existing["nocturne_announcements.py"] is None:
        raise ValueError("installed announcement API module is missing")
    for name in ("announcement_snapshot_writer.py", "announcements.py", *UNIT_FILES):
        if existing[name] is not None and existing[name]["sha256"] != _hash(sources[name]):
            raise ValueError(f"unexpected existing writer artifact: {name}")
    if existing["nocturne_announcements.py"]["sha256"] == _hash(sources["nocturne_announcements.py"]):
        api_state = "already_current"
    else:
        api_state = "upgrade_required"
    plan = {"purpose": PURPOSE, "commit": commit, "api_state": api_state,
            "artifacts": {name: {"target": str(target),
                                "before_sha256": None if existing[name] is None else existing[name]["sha256"],
                                "after_sha256": _hash(sources[name]),
                                "state": "already_current" if existing[name] is not None
                                and existing[name]["sha256"] == _hash(sources[name]) else "install"}
                          for name, target in targets.items()},
            "required_stopped_services": sorted(STOPPED_SERVICES), "dry_run": not apply}
    if not apply or all(item["state"] == "already_current" for item in plan["artifacts"].values()):
        return plan
    if os.geteuid() != 0:
        raise PermissionError("--apply requires UID 0")
    if (not isinstance(expected_module_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_module_sha256) is None
            or expected_module_sha256 != existing["nocturne_announcements.py"]["sha256"]):
        raise ValueError("apply requires the exact reviewed installed announcement-module SHA-256")
    _require_services_stopped(maintenance_confirmed, stopped_services, service_active)
    if not library_dir.exists():
        library_dir.mkdir(mode=0o755)
        os.chown(library_dir, 0, 0)
        os.chmod(library_dir, 0o755)
    if library_dir.stat().st_uid != 0 or stat.S_IMODE(library_dir.stat().st_mode) != 0o755:
        raise ValueError("writer library directory must be root-owned mode 0755")
    if not all(existing[name] is None or existing[name]["sha256"] == _hash(sources[name])
               for name in targets):
        raise ValueError("publication targets changed after preflight")
    backup = backup_root / ("announcement-publication-" + uuid4().hex)
    backup.mkdir(mode=0o700)
    before = {}
    for name, target in targets.items():
        if existing[name] is not None:
            saved = backup / (name + ".before")
            shutil.copyfile(target, saved)
            _apply_metadata(saved, existing[name]["metadata"], run)
            if _hash(saved.read_bytes()) != existing[name]["sha256"]:
                raise ValueError("publication backup verification failed")
            before[name] = {"existed": True, **existing[name], "backup": saved.name}
        else:
            before[name] = {"existed": False, "metadata": None, "sha256": None, "backup": None}
    manifest = {"purpose": PURPOSE, "commit": commit, "status": "verified",
                "targets": {name: str(path) for name, path in targets.items()},
                "before": before, "after": {name: _hash(sources[name]) for name in targets}}
    manifest_path = backup / "MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    with manifest_path.open("rb") as value:
        os.fsync(value.fileno())
    changed = []
    staged = []
    try:
        for name, target in targets.items():
            if existing[name] is not None and existing[name]["sha256"] == _hash(sources[name]):
                continue
            metadata = existing[name]["metadata"] if existing[name] is not None else BASIC_ROOT_FILE
            stage = _stage(target, sources[name], metadata)
            staged.append(stage)
            changed.append(name)
            os.replace(stage, target)
            staged.remove(stage)
            _verify_metadata(target, metadata, run)
            if _hash(target.read_bytes()) != manifest["after"][name]:
                raise ValueError("installed publication artifact verification failed")
    except BaseException:
        _restore_files(manifest, backup, targets, changed, run)
        raise
    finally:
        for stage in staged:
            stage.unlink(missing_ok=True)
    plan.update(dry_run=False, state="installed", backup=str(backup))
    return plan


def _restore_files(manifest, backup, targets, changed, run):
    errors = []
    for name in reversed(changed):
        target = targets[name]
        item = manifest["before"][name]
        try:
            if item["existed"]:
                saved = Path(backup) / item["backup"]
                stage = _stage(target, saved.read_bytes(), item["metadata"])
                os.replace(stage, target)
                _verify_metadata(target, item["metadata"], run)
                if _hash(target.read_bytes()) != item["sha256"]:
                    raise ValueError("restored publication artifact mismatch")
            else:
                if target.is_symlink() or not target.is_file() or _hash(target.read_bytes()) != manifest["after"][name]:
                    raise ValueError("refusing to remove changed publication artifact")
                target.unlink()
        except BaseException as error:
            errors.append(type(error).__name__)
    if errors:
        raise RuntimeError("publication rollback was incomplete: " + ",".join(errors))


def rollback(backup, *, maintenance_confirmed=False, stopped_services=(),
             service_active=None, run=_run):
    backup = Path(backup)
    if backup.is_symlink() or not backup.is_dir():
        raise ValueError("publication backup directory is unsafe")
    manifest = json.loads((backup / "MANIFEST.json").read_text(encoding="utf-8"))
    if manifest.get("purpose") != PURPOSE or manifest.get("status") != "verified":
        raise ValueError("publication backup manifest is invalid")
    targets = {name: Path(value) for name, value in manifest["targets"].items()}
    if set(targets) != {"nocturne_announcements.py", "announcement_snapshot_writer.py",
                        "announcements.py", *UNIT_FILES}:
        raise ValueError("publication backup targets are unexpected")
    expected = {
        "nocturne_announcements.py": Path("/srv/projects/api/nocturne_announcements.py"),
        "announcement_snapshot_writer.py": Path("/usr/local/lib/nocturne-plugin/announcement_snapshot_writer.py"),
        "announcements.py": Path("/usr/local/lib/nocturne-plugin/announcements.py"),
        **{name: Path("/etc/systemd/system") / name for name in UNIT_FILES},
    }
    if targets != expected:
        raise ValueError("publication backup paths differ from the fixed target set")
    if set(manifest.get("before", {})) != set(targets) or set(manifest.get("after", {})) != set(targets):
        raise ValueError("publication backup manifest is incomplete")
    for name, item in manifest.get("before", {}).items():
        if item.get("existed"):
            saved = backup / item.get("backup", "")
            saved_stat = _safe_target(saved)
            if _hash(saved.read_bytes()) != item.get("sha256"):
                raise ValueError("publication rollback backup checksum mismatch")
            _verify_metadata(saved, item.get("metadata"), run)
    for name, target in targets.items():
        after = manifest["after"].get(name)
        if after is None:
            raise ValueError("publication backup is incomplete")
        if _safe_target(target, optional=True) is None:
            if manifest["before"][name]["existed"]:
                raise ValueError("installed publication artifact is missing")
        elif _hash(target.read_bytes()) != after:
            raise ValueError("publication artifact changed since install")
    if os.geteuid() != 0:
        raise PermissionError("rollback requires UID 0")
    _require_services_stopped(maintenance_confirmed, stopped_services, service_active)
    changed = list(targets)
    _restore_files(manifest, backup, targets, changed, run)
    return {"state": "rolled_back", "backup": str(backup)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--commit", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--expected-module-sha256")
    parser.add_argument("--maintenance-confirmed", action="store_true")
    parser.add_argument("--stopped-service", action="append", default=[])
    parser.add_argument("--rollback-backup")
    args = parser.parse_args()
    if args.rollback_backup:
        result = rollback(args.rollback_backup, maintenance_confirmed=args.maintenance_confirmed,
                          stopped_services=args.stopped_service)
    else:
        result = install(repo=args.repo, commit=args.commit, apply=args.apply,
                         maintenance_confirmed=args.maintenance_confirmed,
                         stopped_services=args.stopped_service,
                         expected_module_sha256=args.expected_module_sha256)
    print(json.dumps(result, sort_keys=True))
    if not args.apply and not args.rollback_backup:
        print("Dry run only; no service control or active runtime change was performed.")


if __name__ == "__main__":
    main()
