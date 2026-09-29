"""Inspect immutable-runtime ownership and plan exact, non-recursive migration."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess

from immutable_runtime_release import verify_release, verify_release_ownership
from deployment_trust import verify_checkout


PURPOSE = "nocturne-runtime-ownership-migration-v1"
CONTAINERS = {"releases": 0o755, "venvs": 0o755, "wheelhouse": 0o755,
              "staged-units": 0o755, "staged-nginx": 0o755,
              "activation-records": 0o700, "quarantine": 0o700}


def _acl_state(path):
    acl = subprocess.run(["getfacl", "-cp", str(path)], check=True,
                         stdout=subprocess.PIPE, text=True, timeout=10).stdout
    return "extended" if any(
        line.startswith("default:")
        or (line.startswith("user:") and not line.startswith("user::"))
        or (line.startswith("group:") and not line.startswith("group::"))
        for line in acl.splitlines()) else "basic"


def _metadata(path):
    path = Path(path)
    try: value = path.lstat()
    except FileNotFoundError: return {"path": str(path), "state": "absent"}
    kind = ("symlink" if stat.S_ISLNK(value.st_mode) else
            "directory" if stat.S_ISDIR(value.st_mode) else
            "file" if stat.S_ISREG(value.st_mode) else "unsupported")
    result = {"path": str(path), "state": "present", "type": kind,
              "uid": value.st_uid, "gid": value.st_gid,
              "mode": format(stat.S_IMODE(value.st_mode), "04o")}
    if kind == "symlink":
        result["link_target"] = os.readlink(path)
        try:
            target = path.resolve(strict=True).stat()
            result["resolved_target"] = {"path": str(path.resolve(strict=True)),
                                         "uid": target.st_uid, "gid": target.st_gid,
                                         "mode": format(stat.S_IMODE(target.st_mode), "04o")}
        except (OSError, RuntimeError):
            result["resolved_target"] = None
    else:
        result["is_mount"] = os.path.ismount(path)
        result["acl"] = _acl_state(path)
    return result


def inspect(runtime_root, commit=None):
    root = Path(runtime_root)
    paths = [root, *(root / name for name in CONTAINERS),
             root / "current", root / "venv",
             root / "venvs/python3.14-gunicorn-26.2.0",
             root / "venvs/emoji-python3.14-pillow-12.3.0"]
    if commit:
        paths += [root / "releases" / commit, root / "staged-units" / commit,
                  root / "staged-nginx" / commit]
    return {"runtime_root": str(root), "commit": commit,
            "nodes": [_metadata(path) for path in paths]}


def _snapshot(path):
    path = Path(path)
    value = path.lstat()
    is_link = stat.S_ISLNK(value.st_mode)
    return {"path": str(path), "dev": value.st_dev, "ino": value.st_ino,
            "nlink": value.st_nlink, "uid": value.st_uid, "gid": value.st_gid,
            "mode": stat.S_IMODE(value.st_mode), "type": stat.S_IFMT(value.st_mode),
            "mount": False if is_link else os.path.ismount(path),
            "acl": None if is_link else _acl_state(path),
            "link_target": os.readlink(path) if is_link else None}


def _migration_nodes(runtime_root, commit, from_uid, from_gid):
    root = Path(runtime_root)
    nodes = []
    candidates = [(root, 0o755), *((root / name, mode) for name, mode in
                                    CONTAINERS.items() if (root / name).exists())]
    for path, expected_mode in candidates:
        value = path.lstat()
        if (path.is_symlink() or not stat.S_ISDIR(value.st_mode) or os.path.ismount(path)
                or stat.S_IMODE(value.st_mode) != expected_mode
                or _acl_state(path) != "basic"
                or (value.st_uid, value.st_gid) not in
                    ((from_uid, from_gid), (0, 0))):
            raise ValueError(f"runtime ownership container is unsafe: {path}")
        if (value.st_uid, value.st_gid) == (from_uid, from_gid):
            nodes.append(path)
    if len(set(nodes)) != len(nodes):
        raise ValueError("duplicate runtime ownership migration path")
    release = root / "releases" / commit
    verify_release(release, commit)
    for path in [release, *sorted(release.rglob("*"))]:
        value = path.lstat()
        expected_mode = 0o555 if stat.S_ISDIR(value.st_mode) else 0o444
        if (path.is_symlink() or os.path.ismount(path)
                or not (stat.S_ISDIR(value.st_mode) or stat.S_ISREG(value.st_mode))
                or (stat.S_ISREG(value.st_mode) and value.st_nlink != 1)
                or _acl_state(path) != "basic"
                or (value.st_uid, value.st_gid, stat.S_IMODE(value.st_mode))
                    != (0, 0, expected_mode)):
            raise ValueError(f"immutable release ownership is unsafe: {path}")
    selector = root / "current"
    if (not selector.is_symlink() or selector.resolve(strict=True) != release.resolve(strict=True)):
        raise ValueError("active release selector does not match the inspected commit")
    selector_metadata = selector.lstat()
    if (selector_metadata.st_uid, selector_metadata.st_gid) == (from_uid, from_gid):
        nodes.append(selector)
    elif (selector_metadata.st_uid, selector_metadata.st_gid) != (0, 0):
        raise ValueError("active release selector has unexpected ownership")
    if not nodes:
        raise ValueError("no exact runtime ownership nodes require migration")
    if len(set(nodes)) != len(nodes):
        raise ValueError("duplicate runtime ownership migration path")
    # The exact selector may be lchowned, but its ownership is independently
    # validated from the already root-owned target and it is never followed.
    return nodes


def migrate(runtime_root, commit, *, from_uid, from_gid, apply=False,
            _geteuid=os.geteuid, _chown=os.chown):
    if (from_uid, from_gid) == (0, 0):
        raise ValueError("runtime containers are already root-owned")
    nodes = _migration_nodes(runtime_root, commit, from_uid, from_gid)
    snapshots = [_snapshot(path) for path in nodes]
    listing = "\n".join(str(path) for path in nodes).encode()
    report = {"purpose": PURPOSE, "dry_run": not apply, "commit": commit,
              "from_uid": from_uid, "from_gid": from_gid,
              "to_uid": 0, "to_gid": 0,
              "node_count": len(nodes), "nodes": [str(path) for path in nodes],
              "node_list_sha256": hashlib.sha256(listing).hexdigest()}
    if not apply: return report
    if _geteuid() != 0:
        raise PermissionError("runtime ownership migration requires root")
    release = Path(runtime_root) / "releases" / commit
    verify_release(release, commit)
    verify_release_ownership(release)
    changed = []
    try:
        for path, before in zip(nodes, snapshots, strict=True):
            if _snapshot(path) != before or before["uid"] != from_uid \
                    or before["gid"] != from_gid:
                raise ValueError("runtime ownership changed after preflight")
            _chown(path, 0, 0, follow_symlinks=False)
            changed.append(path)
        for path, before in zip(nodes, snapshots, strict=True):
            after = _snapshot(path)
            if ((after["uid"], after["gid"]) != (0, 0)
                    or {key: after[key] for key in
                        ("dev", "ino", "mode", "type", "nlink", "mount", "acl")} !=
                       {key: before[key] for key in
                        ("dev", "ino", "mode", "type", "nlink", "mount", "acl")}
                    or after["link_target"] != before["link_target"]):
                raise RuntimeError("runtime ownership migration verification failed")
        verify_release(release, commit)
        verify_release_ownership(release)
    except BaseException:
        rollback_errors = []
        for path in reversed(changed):
            try:
                _chown(path, from_uid, from_gid, follow_symlinks=False)
            except BaseException as error:
                rollback_errors.append(error)
        if rollback_errors:
            raise RuntimeError("runtime ownership migration rollback failed") from rollback_errors[0]
        raise
    return {**report, "dry_run": False, "state": "migrated"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="/srv/projects/nocturne-plugin-intake")
    parser.add_argument("--runtime-root", default="/srv/nocturne-plugin")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--from-uid", type=int)
    parser.add_argument("--from-gid", type=int)
    args = parser.parse_args(argv)
    verify_checkout(args.repo, args.commit)
    if args.apply and not args.migrate: parser.error("--apply requires --migrate")
    if args.migrate:
        if args.commit is None or args.from_uid is None or args.from_gid is None:
            parser.error("migration requires --commit, --from-uid, and --from-gid")
        result = migrate(args.runtime_root, args.commit, from_uid=args.from_uid,
                         from_gid=args.from_gid, apply=args.apply)
    else:
        result = inspect(args.runtime_root, args.commit)
    print(json.dumps(result, sort_keys=True))
    if not args.apply:
        print("Dry run only; no ownership, symlink, release, venv, or staging state was changed.")


if __name__ == "__main__":
    main()
