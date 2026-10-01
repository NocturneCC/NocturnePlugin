"""Guarded installation/rollback of the announcement snapshot writer boundary."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from uuid import uuid4

from derived_review_support import (_apply_metadata, _capture_safe_metadata,
                                    _verify_metadata)
from announcement_snapshot_writer import validate_live_output


PURPOSE = "nocturne_announcement_publication_boundary_v1"
STOPPED_SERVICES = frozenset({"osrs-drops-admin.service",
                              "nocturne-announcement-snapshot-writer.service",
                              "nocturne-announcement-snapshot-writer.socket"})
UNIT_FILES = {"nocturne-announcement-snapshot-writer.service",
              "nocturne-announcement-snapshot-writer.socket"}
ADMIN_DROPIN_NAME = "osrs-drops-admin.service.d/20-nocturne-announcement-snapshot-writer.conf"
ADMIN_DROPIN_SOURCE = "osrs-drops-admin-announcement-writer.conf"
ARTIFACT_SOURCE_PAIRS = (
    ("nocturne_announcements.py", "announcements.py"),
    ("announcement_snapshot_writer.py", "announcement_snapshot_writer.py"),
    ("announcements.py", "announcements.py"),
    ("nocturne-announcement-snapshot-writer.service",
     "nocturne-announcement-snapshot-writer.service"),
    ("nocturne-announcement-snapshot-writer.socket",
     "nocturne-announcement-snapshot-writer.socket"),
    (ADMIN_DROPIN_NAME, ADMIN_DROPIN_SOURCE),
)
EXPECTED_ARTIFACT_SOURCE_MAP = {
    "nocturne_announcements.py": "announcements.py",
    "announcement_snapshot_writer.py": "announcement_snapshot_writer.py",
    "announcements.py": "announcements.py",
    "nocturne-announcement-snapshot-writer.service":
        "nocturne-announcement-snapshot-writer.service",
    "nocturne-announcement-snapshot-writer.socket":
        "nocturne-announcement-snapshot-writer.socket",
    ADMIN_DROPIN_NAME: ADMIN_DROPIN_SOURCE,
}
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


def _validated_artifact_source_map(pairs=ARTIFACT_SOURCE_PAIRS):
    mapping = {}
    for pair in pairs:
        if (not isinstance(pair, tuple) or len(pair) != 2
                or any(not isinstance(value, str) or not value for value in pair)):
            raise ValueError("announcement artifact mapping is malformed")
        target, source = pair
        if target in mapping:
            raise ValueError("announcement artifact mapping has a duplicate target")
        mapping[target] = source
    if mapping != EXPECTED_ARTIFACT_SOURCE_MAP:
        raise ValueError("announcement artifact mapping is incomplete or unexpected")
    return mapping


def _source_files(source_dir):
    source_dir = Path(source_dir)
    mapping = _validated_artifact_source_map()
    source_names = set(mapping.values())
    for name in source_names:
        path = source_dir / name
        if not path.is_file() or path.is_symlink() or path.stat().st_nlink != 1:
            raise ValueError("publication source artifact is missing or unsafe")
    return {target: (source_dir / source).read_bytes()
            for target, source in mapping.items()}


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


def _validate_service_directory(path, run=_run, *, allow_mount=False):
    path = Path(path)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or info.st_uid != 0 or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o755 or info.st_nlink < 2
            or (not allow_mount and os.path.ismount(path))):
        raise ValueError("publication service directory must be a safe root-owned mode-0755 directory")
    metadata = _capture_safe_metadata(path, run, directory=True)
    entries = [line.split("#", 1)[0].strip() for line in metadata["acl"].splitlines()
               if line.split("#", 1)[0].strip()]
    if entries != ["user::rwx", "group::r-x", "other::r-x"]:
        raise ValueError("publication service directory has unexpected ACLs")


def _require_basic_acl(path, expected, run=_run):
    metadata = _capture_safe_metadata(path, run, directory=Path(path).is_dir())
    entries = [line.split("#", 1)[0].strip() for line in metadata["acl"].splitlines()
               if line.split("#", 1)[0].strip()]
    if entries != expected:
        raise ValueError("announcement backup ACL profile is unsafe")


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
            admin_dropin_dir=Path("/etc/systemd/system/osrs-drops-admin.service.d"),
            backup_root=Path("/etc/nocturne-plugin-backups"), apply=False,
            maintenance_confirmed=False, stopped_services=(), service_active=None,
            expected_module_sha256=None, run=_run):
    _source_trust(repo, commit)
    source_dir = Path(source_dir or Path(__file__).parent)
    api_dir, library_dir, systemd_dir, admin_dropin_dir, backup_root = map(
        Path, (api_dir, library_dir, systemd_dir, admin_dropin_dir, backup_root))
    sources = _source_files(source_dir)
    validate_live_output()
    targets = {
        "nocturne_announcements.py": api_dir / "nocturne_announcements.py",
        "announcement_snapshot_writer.py": library_dir / "announcement_snapshot_writer.py",
        "announcements.py": library_dir / "announcements.py",
        **{name: systemd_dir / name for name in UNIT_FILES},
        ADMIN_DROPIN_NAME: admin_dropin_dir / "20-nocturne-announcement-snapshot-writer.conf",
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
    if admin_dropin_dir.is_symlink() or (admin_dropin_dir.exists() and not admin_dropin_dir.is_dir()):
        raise ValueError("admin service drop-in directory is unsafe")
    for directory in (library_dir, admin_dropin_dir):
        if directory.exists():
            _validate_service_directory(directory, run)
    existing = {}
    for name, target in targets.items():
        info = _safe_target(target, optional=True)
        existing[name] = None if info is None else {
            "metadata": _capture_safe_metadata(target, run),
            "sha256": _hash(target.read_bytes()),
        }
    if existing["nocturne_announcements.py"] is None:
        raise ValueError("installed announcement API module is missing")
    for name in ("announcement_snapshot_writer.py", "announcements.py", *UNIT_FILES,
                 ADMIN_DROPIN_NAME):
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
    planned_directories = [directory for directory in (library_dir, admin_dropin_dir)
                           if not directory.exists()]
    if not all(existing[name] is None or existing[name]["sha256"] == _hash(sources[name])
               for name in targets):
        raise ValueError("publication targets changed after preflight")
    backup = backup_root / ("announcement-publication-" + uuid4().hex)
    backup.mkdir(mode=0o700)
    os.chown(backup, 0, 0)
    os.chmod(backup, 0o700)
    _require_basic_acl(backup, ["user::rwx", "group::---", "other::---"], run)
    before = {}
    for name, target in targets.items():
        if existing[name] is not None:
            saved = backup / (name.replace("/", "__") + ".before")
            shutil.copyfile(target, saved)
            _apply_metadata(saved, existing[name]["metadata"], run)
            if _hash(saved.read_bytes()) != existing[name]["sha256"]:
                raise ValueError("publication backup verification failed")
            before[name] = {"existed": True, **existing[name], "backup": saved.name}
        else:
            before[name] = {"existed": False, "metadata": None, "sha256": None, "backup": None}
    manifest = {"purpose": PURPOSE, "commit": commit, "status": "verified",
                "targets": {name: str(path) for name, path in targets.items()},
                "before": before, "after": {name: _hash(sources[name]) for name in targets},
                "created_directories": [str(path) for path in planned_directories]}
    manifest_path = backup / "MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    os.chown(manifest_path, 0, 0)
    os.chmod(manifest_path, 0o600)
    _require_basic_acl(manifest_path, ["user::rw-", "group::---", "other::---"], run)
    with manifest_path.open("rb") as value:
        os.fsync(value.fileno())
    backup_fd = os.open(backup, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(backup_fd)
    finally:
        os.close(backup_fd)
    changed = []
    staged = []
    created_directories = []
    try:
        for directory in planned_directories:
            directory.mkdir(mode=0o755)
            created_directories.append(directory)
            os.chown(directory, 0, 0)
            os.chmod(directory, 0o755)
        for directory in (library_dir, admin_dropin_dir):
            _validate_service_directory(directory, run)
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
        _remove_created_directories(created_directories,
                                    {str(library_dir), str(admin_dropin_dir)})
        raise
    finally:
        for stage in staged:
            stage.unlink(missing_ok=True)
    plan.update(dry_run=False, state="installed", backup=str(backup),
                created_directories=[str(path) for path in created_directories])
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
                if target.is_symlink():
                    raise ValueError("refusing to remove changed publication artifact")
                if not target.exists():
                    continue
                if not target.is_file() or _hash(target.read_bytes()) != manifest["after"][name]:
                    raise ValueError("refusing to remove changed publication artifact")
                target.unlink()
        except BaseException as error:
            errors.append(type(error).__name__)
    if errors:
        raise RuntimeError("publication rollback was incomplete: " + ",".join(errors))


def _validate_created_directories(directories, allowed):
    if (not isinstance(directories, list)
            or any(not isinstance(value, str) for value in directories)
            or len(directories) != len(set(directories))
            or any(value not in allowed for value in directories)):
        raise ValueError("created publication directory record is invalid")


def _remove_created_directories(directories, allowed):
    _validate_created_directories(directories, allowed)
    for directory in reversed(directories):
        path = Path(directory)
        if path.is_symlink():
            raise ValueError("created publication directory became a symlink")
        if path.exists():
            info = path.lstat()
            if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_gid != 0
                    or stat.S_IMODE(info.st_mode) != 0o755):
                raise ValueError("created publication directory changed")
            path.rmdir()


def rollback(backup, *, maintenance_confirmed=False, stopped_services=(),
             service_active=None, run=_run):
    backup = Path(backup)
    backup_info = backup.lstat()
    if (not stat.S_ISDIR(backup_info.st_mode) or stat.S_ISLNK(backup_info.st_mode)
            or backup_info.st_uid != 0 or backup_info.st_gid != 0
            or stat.S_IMODE(backup_info.st_mode) != 0o700 or os.path.ismount(backup)):
        raise ValueError("publication backup directory is unsafe")
    _require_basic_acl(backup, ["user::rwx", "group::---", "other::---"], run)
    manifest_path = backup / "MANIFEST.json"
    manifest_stat = _safe_target(manifest_path)
    if (manifest_stat.st_uid != 0 or manifest_stat.st_gid != 0
            or stat.S_IMODE(manifest_stat.st_mode) != 0o600):
        raise ValueError("publication rollback manifest permissions are unsafe")
    _require_basic_acl(manifest_path, ["user::rw-", "group::---", "other::---"], run)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("purpose") != PURPOSE or manifest.get("status") != "verified":
        raise ValueError("publication backup manifest is invalid")
    targets = {name: Path(value) for name, value in manifest["targets"].items()}
    if set(targets) != {"nocturne_announcements.py", "announcement_snapshot_writer.py",
                        "announcements.py", *UNIT_FILES, ADMIN_DROPIN_NAME}:
        raise ValueError("publication backup targets are unexpected")
    expected = {
        "nocturne_announcements.py": Path("/srv/projects/api/nocturne_announcements.py"),
        "announcement_snapshot_writer.py": Path("/usr/local/lib/nocturne-plugin/announcement_snapshot_writer.py"),
        "announcements.py": Path("/usr/local/lib/nocturne-plugin/announcements.py"),
        **{name: Path("/etc/systemd/system") / name for name in UNIT_FILES},
        ADMIN_DROPIN_NAME: Path("/etc/systemd/system/osrs-drops-admin.service.d")
        / "20-nocturne-announcement-snapshot-writer.conf",
    }
    if targets != expected:
        raise ValueError("publication backup paths differ from the fixed target set")
    allowed_created_directories = {
        "/usr/local/lib/nocturne-plugin",
        "/etc/systemd/system/osrs-drops-admin.service.d",
    }
    created_directories = manifest.get("created_directories", [])
    _validate_created_directories(created_directories, allowed_created_directories)
    if set(manifest.get("before", {})) != set(targets) or set(manifest.get("after", {})) != set(targets):
        raise ValueError("publication backup manifest is incomplete")
    for name, item in manifest.get("before", {}).items():
        if item.get("existed"):
            backup_name = item.get("backup", "")
            if (not isinstance(backup_name, str) or Path(backup_name).name != backup_name
                    or not backup_name.endswith(".before")):
                raise ValueError("publication rollback backup name is unsafe")
            saved = backup / backup_name
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
    _remove_created_directories(created_directories, allowed_created_directories)
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
