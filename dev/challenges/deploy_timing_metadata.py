#!/usr/bin/env python3
"""Guarded installer for Challenge timing metadata.

Default invocation is a read-only plan.  --apply is deliberately a separate,
root-only operation and is intended to be run from its matching immutable
release after that release has been prepared.
"""

from __future__ import annotations

import argparse
import copy
import contextlib
import errno
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

REPO = Path("/srv/projects/nocturne-plugin-intake")
RUNTIME = Path("/srv/nocturne-plugin")
DB = Path("/srv/projects/database/Challenges.db")
BACKUP_ROOT = Path("/var/backups/challenge-timing-metadata")
SOURCE_MANIFEST = Path("dev/challenges/source-manifest.json")
FILES = (
    ("dev/challenges/service/challenge_config.py", Path("/srv/projects/nocturne-services/challenge_config.py")),
    ("dev/challenges/website/challenge-admin.html", Path("/srv/projects/website/challenge-admin.html")),
    ("dev/challenges/website/challenge-admin-state.js", Path("/srv/projects/website/challenge-admin-state.js")),
)
# One explicitly verified predecessor for the label-only admin-page update.
# The content hash is the page shipped by 00b9768; metadata and ACL are checked
# against the established live-file profile before it can be accepted.
SAFE_LIVE_FILE_PREDECESSORS = {
    "/srv/projects/website/challenge-admin.html": {
        "commit": "00b976807ee137ffeaffc3e9b417d96c6bf7bff2",
        "source_relative": "dev/challenges/website/challenge-admin.html",
        "sha256": "3d0eb1a5a96455a94ad230c1bf1645f89de5639b9dee2e70d831e1a09031b06c",
        "uid": 1001,
        "gid": 33,
        "mode": "0664",
        "nlink": 1,
        "size": 45431,
    },
}
NEW_COLUMNS = (
    "timing_scope", "timing_segment_key", "timing_segment_label",
    "automatic_capture", "numeric_metric_key", "numeric_metric_label",
    "numeric_metric_unit",
)
LONG_SERVICES = (
    "osrs-drops-api.service",
    "nocturne-challenge-intake.service",
    "osrs-drops-admin.service",
)
# API and intake are required live import consumers.  The admin API is also a
# controlled database holder, but may legitimately be inactive before apply.
REQUIRED_ACTIVE_SERVICES = ("osrs-drops-api.service", "nocturne-challenge-intake.service")
WRITER_SERVICES = (
    "nocturne-challenge-shadow-sync.service",
    "nocturne-leaderboard-shadow-renderer.service",
)
TIMERS = (
    "nocturne-challenge-shadow-sync.timer",
    "nocturne-leaderboard-shadow-renderer.timer",
)
CONTROLLED = (*LONG_SERVICES, *WRITER_SERVICES, *TIMERS)
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
LOCK_PATH = Path("/run/nocturne-challenge-timing-deploy.lock")
MAX_ACL_OUTPUT_BYTES = 16 * 1024
SIDECAR_DRAIN_TIMEOUT_SECONDS = 10.0
SIDECAR_DRAIN_INTERVAL_SECONDS = 0.1
INTAKE_HEALTH_READY_TIMEOUT_SECONDS = 10.0
INTAKE_HEALTH_RETRY_INTERVAL_SECONDS = 0.25
INTAKE_HEALTH_REQUEST_TIMEOUT_SECONDS = 4.0
INTAKE_SERVICE = "nocturne-challenge-intake.service"


class DeployError(RuntimeError):
    pass


SIDECAR_FAILURE_CATEGORIES = frozenset({
    "known_holder_still_active", "unknown_holder_present", "holder_scan_failed",
    "sidecar_disappearance_timeout", "sidecar_identity_changed",
    "sidecar_metadata_changed", "sidecar_content_changed", "sidecar_reappeared",
    "database_identity_changed",
})


class CategorizedDeployError(DeployError):
    """A fixed, non-sensitive diagnostic category with a preserved cause."""

    def __init__(self, category: str, *, retryable: bool = False):
        if category not in SIDECAR_FAILURE_CATEGORIES:
            raise ValueError("unsupported diagnostic category")
        self.diagnostic_category = category
        self.retryable = retryable
        super().__init__(f"diagnostic_category={category}")


class LiveVerificationError(DeployError):
    """A bounded, non-sensitive classification for one post-install probe."""

    def __init__(self, category: str):
        allowed = {
            "intake_health_failed",
            "intake_service_unavailable",
            "configuration_read_failed",
            "configuration_version_mismatch",
            "schema_verification_failed",
            "leaderboard_api_failed",
            "admin_auth_gate_failed",
        }
        if category not in allowed:
            raise ValueError("unsupported live-verification category")
        self.diagnostic_category = category
        super().__init__(f"diagnostic_category={category}")


# Explicitly approved live Challenges.db ancestry and ACL principals.  This is
# intentionally narrower than a generic “writable parent is okay” rule.
_DB_ANCESTRY = {
    # Access and default ACLs are intentionally recorded independently.  The
    # database directory grants randal access to the existing tree without
    # inheriting that grant onto newly created children.
    Path("/srv"): (0, 0, 0o755, frozenset(), frozenset(), None),
    Path("/srv/projects"): (1000, 33, 0o2775, frozenset({1003}), frozenset({1003}), "r-x"),
    Path("/srv/projects/database"): (1000, 33, 0o2775, frozenset({1000, 1003}), frozenset({1003}), "rwx"),
}
_DB_FILE_ACL = {
    "user::": "rw-", "user:1003:": "rw-", "group::": "rw-",
    "mask::": "rw-", "other::": "r--",
}
_LEGACY_ACL_USER_LABELS = {1000: "randal", 1003: "glob"}


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha_file(path: Path) -> str:
    h = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise DeployError(f"unsafe file node: {path}")
        while block := os.read(fd, 1024 * 1024):
            h.update(block)
    finally:
        os.close(fd)
    return h.hexdigest()


def _read_regular(path: Path, maximum: int = 32 * 1024 * 1024) -> bytes:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode) or before.st_nlink != 1 or before.st_size > maximum:
        raise DeployError(f"unsafe or oversized regular file: {path}")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino, opened.st_uid, opened.st_gid, opened.st_mode, opened.st_nlink) != (
                before.st_dev, before.st_ino, before.st_uid, before.st_gid, before.st_mode, before.st_nlink):
            raise DeployError(f"file changed while opening: {path}")
        chunks = []
        total = 0
        while block := os.read(fd, min(1024 * 1024, maximum + 1 - total)):
            chunks.append(block)
            total += len(block)
            if total > maximum:
                raise DeployError(f"file exceeded read bound: {path}")
        after = os.fstat(fd)
        named = path.lstat()
        if (opened.st_dev, opened.st_ino, opened.st_size) != (after.st_dev, after.st_ino, after.st_size) or (
                after.st_dev, after.st_ino, after.st_uid, after.st_gid, after.st_mode, after.st_nlink) != (
                named.st_dev, named.st_ino, named.st_uid, named.st_gid, named.st_mode, named.st_nlink):
            raise DeployError(f"file changed while reading: {path}")
        return b"".join(chunks)
    finally:
        os.close(fd)


def _run(argv: list[str], *, timeout: float = 15, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(argv, input=input_text, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=timeout, check=False,
                              env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DeployError(f"command failed safely: {Path(argv[0]).name}") from exc


def _git(repo: Path, *args: str) -> str:
    argv = ["/usr/bin/git", "-C", str(repo), "-c", f"safe.directory={repo}", *args]
    try:
        proc = _run(argv)
    except Exception as exc:
        raise DeployError("checkout gate failed diagnostic_category=git_launch_failed") from exc
    if proc.returncode:
        raise DeployError("checkout gate failed diagnostic_category=git_launch_failed")
    return proc.stdout


def _git_single_line(output: str) -> str:
    if not isinstance(output, str) or not output.endswith("\n") or output.count("\n") != 1:
        raise DeployError("checkout gate failed diagnostic_category=malformed_output")
    return output[:-1]


def verify_git(repo: Path, commit: str) -> None:
    if not FULL_SHA.fullmatch(commit):
        raise DeployError("checkout gate failed diagnostic_category=malformed_output")
    branch = _git_single_line(_git(repo, "branch", "--show-current"))
    if branch != "development":
        raise DeployError("checkout gate failed diagnostic_category=wrong_branch")
    status = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise DeployError("checkout gate failed diagnostic_category=dirty_worktree")
    head = _git_single_line(_git(repo, "rev-parse", "HEAD"))
    if not FULL_SHA.fullmatch(head):
        raise DeployError("checkout gate failed diagnostic_category=malformed_output")
    if head != commit:
        raise DeployError("checkout gate failed diagnostic_category=head_mismatch")
    remote = _git_single_line(_git(repo, "rev-parse", "origin/development"))
    if not FULL_SHA.fullmatch(remote):
        raise DeployError("checkout gate failed diagnostic_category=malformed_output")
    if remote != commit:
        raise DeployError("checkout gate failed diagnostic_category=origin_mismatch")


def _safe_parent(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise DeployError(f"unsafe absolute target path: {path}")
    current = Path(path.anchor)
    for part in path.parts[1:-1]:
        current /= part
        st = current.lstat()
        if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode):
            raise DeployError(f"unsafe target parent: {current}")


def _acl_entries(text: str) -> dict[str, str]:
    entries: dict[str, str] = {}
    effective_annotations: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "#" in line:
            line, comment = line.split("#", 1)
            match = re.fullmatch(r"\s*effective:([r-][w-][x-])\s*", comment)
            if not match:
                raise ValueError("malformed ACL effective-permission annotation")
            annotated_effective = match.group(1)
        else:
            annotated_effective = None
        line = line.strip()
        # getfacl -cpn emits numeric access entries as user::rwx or
        # user:<uid>:rwx, and default entries with a default: prefix.
        if line.startswith("default:"):
            line = line[len("default:"):]
            prefix = "default:"
        else:
            prefix = ""
        parts = line.split(":")
        if len(parts) == 3 and parts[0] in {"user", "group", "mask", "other"} and not parts[1]:
            key, perms = f"{prefix}{parts[0]}::", parts[2]
        elif len(parts) == 3 and parts[0] in {"user", "group"}:
            identity = parts[1]
            if not re.fullmatch(r"[0-9]+", identity):
                raise ValueError("unapproved ACL identity")
            key, perms = f"{prefix}{parts[0]}:{identity}:", parts[2]
        else:
            raise ValueError("malformed ACL entry")
        if key in entries or not re.fullmatch(r"[r-][w-][x-]", perms):
            raise ValueError("duplicate or malformed ACL entry")
        entries[key] = perms
        if annotated_effective is not None:
            if key.startswith("default:") or key in {"user::", "mask::", "other::"}:
                raise ValueError("effective annotation on unsupported ACL entry")
            effective_annotations[key] = annotated_effective
    mask = entries.get("mask::")
    for key, annotated in effective_annotations.items():
        permissions = entries[key]
        if mask is None:
            raise ValueError("effective ACL annotation missing mask")
        effective = "".join(bit if bit in mask else "-" for bit in permissions)
        if annotated != effective:
            raise ValueError("ACL effective-permission annotation mismatch")
    return entries


def _validate_acl_profile(text: str, expected: dict[str, str]) -> str:
    entries = _acl_entries(text)
    if entries != expected:
        raise ValueError("ACL profile mismatch")
    # Named entries must never gain effective permissions outside the mode's
    # group-class mask.  Exact-profile comparison above also rejects extra IDs.
    mask = entries.get("mask::", entries.get("group::"))
    if mask is None:
        raise ValueError("ACL mask missing")
    mask_set = {i for i, bit in enumerate(mask) if bit != "-"}
    for key, permissions in entries.items():
        if key.startswith(("user:", "group:")) and not key.endswith("::"):
            effective = {i for i, bit in enumerate(permissions) if bit != "-"}
            if not effective.issubset(mask_set):
                raise ValueError("ACL exceeds effective mode")
    return _acl_hash(text)


def _db_ancestry_snapshot(path: Path) -> tuple[tuple[Any, ...], ...]:
    if path != DB:
        return ()
    snapshot = []
    for node, (uid, gid, mode, access_users, default_users, group_acl) in _DB_ANCESTRY.items():
        try:
            before = _db_lstat(node)
            if (not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode) or
                    (before.st_uid, before.st_gid, stat.S_IMODE(before.st_mode)) != (uid, gid, mode)):
                raise ValueError("directory metadata mismatch")
            acl_text = _acl_text(node)
            if group_acl is not None:
                entries = _acl_entries(acl_text)
                prefix_sets = {"access": set(), "default": set()}
                for key in entries:
                    scope, entry = ("default", key[len("default:"):]) if key.startswith("default:") else ("access", key)
                    if entry.startswith("user:") and not entry.startswith("user::"):
                        prefix_sets[scope].add(int(entry.split(":")[1]))
                    elif entry.startswith("group:") and not entry.startswith("group::"):
                        raise ValueError("unapproved named group ACL")
                if (prefix_sets["access"] != set(access_users) or
                        prefix_sets["default"] != set(default_users)):
                    raise ValueError("ACL identity set mismatch")
                base = {"user::": "rwx", "group::": group_acl, "mask::": "rwx", "other::": "r-x"}
                expected = dict(base)
                expected.update({f"user:{identity}:": "rwx" for identity in access_users})
                default_base = {"user::": "rwx", "group::": group_acl,
                                "mask::": "rwx", "other::": "r-x"}
                default_base.update({f"user:{identity}:": "rwx" for identity in default_users})
                expected_default = {f"default:{key}": value for key, value in default_base.items()}
                if entries != {**expected, **expected_default}:
                    raise ValueError("ACL permission profile mismatch")
                acl_digest = _acl_hash(acl_text)
            else:
                acl_digest = _validate_acl_profile(acl_text, {
                    "user::": "rwx", "group::": "r-x", "other::": "r-x"})
            after = _db_lstat(node)
            if _stat_identity(before) != _stat_identity(after):
                raise RuntimeError("directory changed during capture")
            if _acl_hash(acl_text) != _acl_hash(_acl_text(node)):
                raise ValueError("ACL changed during capture")
            snapshot.append((str(node), *_stat_identity(before), acl_digest))
        except RuntimeError:
            raise DeployError("database inspection failed diagnostic_category=database_busy node=ancestry") from None
        except (OSError, ValueError, DeployError):
            raise DeployError("database inspection failed diagnostic_category=unsafe_metadata node=ancestry") from None
    return tuple(snapshot)


def _stat_identity(st: os.stat_result) -> tuple[int, ...]:
    return (st.st_dev, st.st_ino, st.st_uid, st.st_gid,
            stat.S_IMODE(st.st_mode), st.st_nlink, st.st_size, st.st_mtime_ns)


def _database_failure(category: str, node: str) -> DeployError:
    return DeployError(f"database inspection failed diagnostic_category={category} node={node}")


def _db_lstat(path: Path) -> os.stat_result:
    return path.lstat()


def _sidecar_present(path: Path) -> bool:
    try:
        _db_lstat(path)
        return True
    except FileNotFoundError:
        return False


def _safe_temp_snapshot_root(root: Path) -> None:
    try:
        parent = Path("/tmp")
        parent_st = parent.lstat()
        root_st = root.lstat()
        private_owner = (0, 0) if os.geteuid() == 0 else (os.geteuid(), os.getegid())
        parent_metadata_ok = (
            (parent_st.st_uid, parent_st.st_gid, stat.S_IMODE(parent_st.st_mode)) == (0, 0, 0o1777)
            if os.geteuid() == 0 else stat.S_IMODE(parent_st.st_mode) == 0o1777
        )
        if (not stat.S_ISDIR(parent_st.st_mode) or stat.S_ISLNK(parent_st.st_mode) or
                not parent_metadata_ok or not os.path.ismount(parent) or
                not stat.S_ISDIR(root_st.st_mode) or stat.S_ISLNK(root_st.st_mode) or
                (root_st.st_uid, root_st.st_gid) != private_owner or
                stat.S_IMODE(root_st.st_mode) != 0o700 or root_st.st_nlink < 2 or
                root_st.st_dev != parent_st.st_dev or os.path.ismount(root)):
            raise ValueError("private temporary profile mismatch")
        if os.geteuid() == 0:
            _validate_acl_profile(_acl_text(parent), {
                "user::": "rwx", "group::": "rwx", "other::" : "rwx"})
        _validate_acl_profile(_acl_text(root), {
            "user::": "rwx", "group::": "---", "other::": "---"})
    except (OSError, ValueError, DeployError):
        raise DeployError("database inspection failed diagnostic_category=unsafe_metadata node=private_temp") from None


def _acl_text(path: Path) -> str:
    proc = _run(["/usr/bin/getfacl", "-cpn", "--", str(path)], timeout=15)
    if (proc.returncode or len(proc.stdout.encode("utf-8")) > MAX_ACL_OUTPUT_BYTES or
            len(proc.stderr.encode("utf-8")) > MAX_ACL_OUTPUT_BYTES):
        raise DeployError("ACL inspection failed safely")
    return proc.stdout


def _acl_hash(text: str) -> str:
    lines = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line.strip() or line.startswith("#"):
            continue
        # The adopted source manifest predates numeric getfacl output and
        # fingerprints these fixed UIDs by their established account labels.
        # Normalize only for the digest; ACL validation remains numeric-only.
        for uid, label in _LEGACY_ACL_USER_LABELS.items():
            line = re.sub(rf"(^|default:)user:{uid}:", rf"\g<1>user:{label}:", line)
        lines.append(line)
    return _sha_bytes(("\n".join(lines) + "\n").encode())


def _raw_numeric_acl_hash(text: str) -> str:
    """Fingerprint the exact numeric ACL snapshot, including effective comments."""
    return _sha_bytes(text.encode("utf-8"))


_DB_SIDECAR_ACL_PROFILES = (
    {
        "user::": "rw-", "user:1003:": "rw-", "group::": "rw-",
        "mask::": "rw-", "other::": "r--",
    },
    {
        "user::": "rw-", "user:1003:": "rwx", "group::": "rwx",
        "mask::": "rw-", "other::": "r--",
    },
)


def _validate_db_sidecar_acl(text: str) -> str:
    """Accept only the explicit or safely masked inherited live WAL/SHM ACL."""
    entries = _acl_entries(text)
    if entries not in _DB_SIDECAR_ACL_PROFILES:
        raise ValueError("sidecar ACL profile mismatch")
    mask = entries["mask::"]
    if mask != "rw-":
        raise ValueError("sidecar ACL mask mismatch")
    for key, permissions in entries.items():
        if key in {"user::", "mask::", "other::"}:
            continue
        effective = "".join(bit if bit in mask else "-" for bit in permissions)
        if effective != "rw-":
            raise ValueError("sidecar ACL effective permissions mismatch")
    return _raw_numeric_acl_hash(text)


def capture_file(path: Path) -> dict[str, Any]:
    _safe_parent(path)
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode) or st.st_nlink != 1:
        raise DeployError(f"unsafe live file node: {path}")
    digest = _sha_file(path)
    after_hash = path.lstat()
    if (st.st_dev, st.st_ino, st.st_uid, st.st_gid, st.st_mode, st.st_nlink) != (
            after_hash.st_dev, after_hash.st_ino, after_hash.st_uid, after_hash.st_gid,
            after_hash.st_mode, after_hash.st_nlink):
        raise DeployError(f"file changed during metadata capture: {path}")
    acl = _acl_text(path)
    final = path.lstat()
    if (st.st_dev, st.st_ino, st.st_uid, st.st_gid, st.st_mode, st.st_nlink) != (
            final.st_dev, final.st_ino, final.st_uid, final.st_gid, final.st_mode, final.st_nlink):
        raise DeployError(f"file changed during ACL capture: {path}")
    return {"sha256": digest, "uid": st.st_uid, "gid": st.st_gid,
            "mode": f"{stat.S_IMODE(st.st_mode):04o}", "nlink": st.st_nlink,
            "size": st.st_size, "acl_sha256": _acl_hash(acl), "acl_text": acl}


def _same_file_metadata(left: dict[str, Any], right: dict[str, Any]) -> bool:
    keys = ("sha256", "uid", "gid", "mode", "nlink", "size", "acl_sha256")
    return all(left.get(key) == right.get(key) for key in keys)


def _load_manifest(release: Path, commit: str, runtime_root: Path = RUNTIME) -> tuple[dict, dict]:
    if release != runtime_root / "releases" / commit:
        raise DeployError("release path is not the exact target release")
    manifest_path = release / "RELEASE-MANIFEST.json"
    manifest_st = manifest_path.lstat()
    if not stat.S_ISREG(manifest_st.st_mode) or stat.S_ISLNK(manifest_st.st_mode):
        raise DeployError("release manifest is not an ordinary file")
    manifest = json.loads(_read_regular(manifest_path, 4 * 1024 * 1024))
    if (manifest.get("purpose") != "nocturne-immutable-runtime-v1" or manifest.get("commit") != commit
            or not isinstance(manifest.get("files"), dict)):
        raise DeployError("release manifest identity/schema mismatch")
    for rel, expected in manifest["files"].items():
        relpath = Path(rel)
        if relpath.is_absolute() or ".." in relpath.parts or not re.fullmatch(r"[0-9a-f]{64}", str(expected)):
            raise DeployError("unsafe release manifest entry")
        candidate = release / relpath
        if _sha_file(candidate) != expected:
            raise DeployError(f"release file digest mismatch: {rel}")
    sm_path = release / SOURCE_MANIFEST
    if _sha_file(sm_path) != manifest["files"].get(SOURCE_MANIFEST.as_posix()):
        raise DeployError("Challenge source manifest is not bound to the release")
    sm = json.loads(_read_regular(sm_path, 16 * 1024 * 1024))
    return manifest, sm


def _bundle_records(source_manifest: dict) -> tuple[dict[str, dict], dict[str, dict]]:
    bundles = source_manifest.get("bundle_files")
    live = source_manifest.get("live_sources")
    if not isinstance(bundles, list) or not isinstance(live, list):
        raise DeployError("Challenge source manifest schema is incomplete")
    by_path = {r.get("path"): r for r in bundles if isinstance(r, dict)}
    live_by_path = {r.get("path"): r for r in live if isinstance(r, dict)}
    if len(by_path) != len(bundles) or len(live_by_path) != len(live):
        raise DeployError("duplicate source manifest path")
    return by_path, live_by_path


def _source_targets(release: Path, source_manifest: dict, release_manifest: dict) -> list[dict]:
    bundle, live = _bundle_records(source_manifest)
    result = []
    for rel, target in FILES:
        b = bundle.get(rel)
        if not b or b.get("type") != "regular" or b.get("source_relationship") != "repository_owned_extension":
            raise DeployError(f"repository-owned extension is not declared: {rel}")
        expected_live = live.get(str(target))
        if not expected_live or expected_live.get("type") != "regular":
            raise DeployError(f"live baseline metadata is missing: {target}")
        digest = b.get("sha256")
        if release_manifest["files"].get(rel) != digest:
            raise DeployError(f"release/source manifest mismatch: {rel}")
        source = release / rel
        if _sha_file(source) != digest:
            raise DeployError(f"immutable source digest mismatch: {rel}")
        item = {"source_relative": rel, "source": source, "target": target,
                "after_sha256": digest, "after_size": int(b.get("size", -1)),
                "before": expected_live}
        predecessor = SAFE_LIVE_FILE_PREDECESSORS.get(str(target))
        if predecessor is not None:
            if (rel != predecessor["source_relative"] or
                    any(expected_live.get(key) != predecessor[key]
                        for key in ("uid", "gid", "mode", "nlink"))):
                raise DeployError("safe predecessor does not match the pinned live target profile")
            item["safe_predecessors"] = [{**predecessor,
                                           "acl_sha256": expected_live.get("acl_sha256")}]
        result.append(item)
    return result


def _prepared_check(release: Path, commit: str, runtime_root: Path) -> str:
    if not FULL_SHA.fullmatch(commit):
        raise DeployError("canonical immutable prepared check failed exit_status=not_run diagnostic_category=invalid_commit")
    if runtime_root != RUNTIME:
        raise DeployError("canonical immutable prepared check failed exit_status=not_run diagnostic_category=wrong_runtime_root")
    if release != runtime_root / "releases" / commit:
        raise DeployError("canonical immutable prepared check failed exit_status=not_run diagnostic_category=release_identity_mismatch")

    # The supported operator interface is the immutable release's guarded
    # wrapper, not a direct invocation of its implementation module. The
    # wrapper binds the full commit, verifies the clean checkout, fixes the
    # runtime root, and emits the canonical read-only result.
    checker = release / "dev/intake/prepare_immutable_runtime.sh"
    try:
        proc = subprocess.run(
            ["/bin/bash", str(checker), "--check", commit],
            capture_output=True, text=True, timeout=90, check=False,
            env={
                "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
                "LC_ALL": "C",
                "GIT_OPTIONAL_LOCKS": "0",
                # The checker runs Git as root against Simon's checkout.
                # Supply the narrowly scoped trust exception to this process
                # tree only; never depend on or mutate global Git config.
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "safe.directory",
                "GIT_CONFIG_VALUE_0": str(REPO),
            },
        )
    except subprocess.TimeoutExpired as exc:
        raise DeployError(
            "canonical immutable prepared check failed exit_status=timeout diagnostic_category=checker_timeout"
        ) from exc
    except OSError as exc:
        raise DeployError(
            "canonical immutable prepared check failed exit_status=unavailable diagnostic_category=checker_launch_failed"
        ) from exc

    lines = proc.stdout.splitlines()
    status_lines = [line for line in lines if line.startswith("status=")]
    check_mode_lines = [line for line in lines if line.startswith("check_mode=")]
    if proc.returncode != 0:
        status = status_lines[0].partition("=")[2] if len(status_lines) == 1 else ""
        category = {
            "not_prepared": "not_prepared",
            "recoverable_incomplete": "recoverable_incomplete",
            "unsafe_blocking": "unsafe_blocking",
            "prepared": "checker_nonzero_with_prepared_status",
        }.get(status, "checker_nonzero_without_unique_status")
        raise DeployError(
            f"canonical immutable prepared check failed exit_status={proc.returncode} diagnostic_category={category}"
        )
    if len(status_lines) != 1 or status_lines[0] != "status=prepared":
        category = "missing_status" if not status_lines else "duplicate_status" if len(status_lines) > 1 else "unexpected_status"
        raise DeployError(
            f"canonical immutable prepared check failed exit_status=0 diagnostic_category={category}"
        )
    if len(check_mode_lines) != 1 or check_mode_lines[0] != "check_mode=read_only":
        category = "missing_check_mode" if not check_mode_lines else "duplicate_check_mode" if len(check_mode_lines) > 1 else "unexpected_check_mode"
        raise DeployError(
            f"canonical immutable prepared check failed exit_status=0 diagnostic_category={category}"
        )
    return proc.stdout[-12000:]


def verify_extension_sources(release: Path, release_manifest: dict, source_manifest: dict) -> list[dict]:
    return _source_targets(release, source_manifest, release_manifest)


def _systemd_show(unit: str, *, timeout: float = 15) -> dict[str, str]:
    fields = ("Id", "LoadState", "ActiveState", "SubState", "Result", "MainPID")
    proc = _run(["/usr/bin/systemctl", "show", unit, *(f"--property={x}" for x in fields)], timeout=timeout)
    if proc.returncode:
        raise DeployError(f"cannot inspect service state: {unit}")
    result: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        key, sep, value = line.partition("=")
        if not sep or key not in fields or key in result:
            raise DeployError(f"ambiguous systemd state: {unit}")
        result[key] = value
    is_timer = unit.endswith(".timer")
    required = set(fields) - {"Result"}
    if is_timer:
        required.discard("MainPID")
    if not required.issubset(result) or result.get("Id") != unit:
        raise DeployError(f"incomplete systemd state: {unit}")
    if "MainPID" in result and not result["MainPID"].isdigit():
        raise DeployError(f"malformed MainPID: {unit}")
    if is_timer and result.get("MainPID", "0") != "0":
        raise DeployError(f"timer unexpectedly has a process: {unit}")
    if not is_timer and "MainPID" not in result:
        raise DeployError(f"service MainPID is missing: {unit}")
    if result["LoadState"] == "not-found":
        if result["ActiveState"] != "inactive" or result["SubState"] != "dead":
            raise DeployError(f"absent unit has an ambiguous state: {unit}")
        return result
    if result["LoadState"] != "loaded":
        raise DeployError(f"unit not loaded: {unit}")
    if result["ActiveState"] not in {"active", "inactive", "failed"}:
        raise DeployError(f"unit state transitional: {unit}")
    if result["ActiveState"] == "inactive" and result["SubState"] != "dead":
        raise DeployError(f"inactive unit has an unexpected substate: {unit}")
    if is_timer and result["ActiveState"] == "active" and result["SubState"] != "waiting":
        raise DeployError(f"active timer has an unexpected substate: {unit}")
    if result["ActiveState"] == "failed":
        raise DeployError(f"unit is failed: {unit}")
    return result


class Systemd:
    def show(self, unit: str, *, timeout: float = 15) -> dict[str, str]:
        return _systemd_show(unit, timeout=timeout)

    def stop(self, unit: str) -> None:
        proc = _run(["/usr/bin/systemctl", "stop", unit], timeout=30)
        if proc.returncode:
            raise DeployError(f"systemctl stop failed: {unit}")

    def start(self, unit: str) -> None:
        proc = _run(["/usr/bin/systemctl", "start", unit], timeout=30)
        if proc.returncode:
            raise DeployError(f"systemctl start failed: {unit}")

    def wait_inactive(self, unit: str, timeout: float = 20) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.show(unit)
            # systemd does not define MainPID for timer units.  The parser
            # intentionally accepts that documented shape; keep this waiter
            # consistent while retaining the stricter service requirement.
            main_pid = state.get("MainPID")
            if not unit.endswith(".timer") and main_pid is None:
                raise DeployError(f"service MainPID is missing while waiting inactive: {unit}")
            pid_is_safe = main_pid in {None, "0"} if unit.endswith(".timer") else main_pid == "0"
            if (state["ActiveState"] == "inactive" and state["SubState"] == "dead" and
                    pid_is_safe):
                return
            time.sleep(.2)
        raise DeployError(f"unit did not become inactive: {unit}")

    def wait_active(self, unit: str, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.show(unit)
            if state["ActiveState"] == "active" and state["SubState"] == "running" and int(state["MainPID"]) > 0:
                return
            time.sleep(.25)
        raise DeployError(f"unit did not become active: {unit}")

    def wait_job_idle(self, unit: str, timeout: float = 40) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.show(unit)
            if state["ActiveState"] == "inactive" and state["SubState"] == "dead" and state["MainPID"] == "0":
                return
            if state["ActiveState"] == "active" and state["SubState"] == "exited" and state["MainPID"] == "0":
                return
            if state["ActiveState"] == "failed":
                raise DeployError(f"writer job failed before migration: {unit}")
            time.sleep(.2)
        raise DeployError(f"writer job did not finish before migration: {unit}")


def _table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    counts = {}
    for name in sorted(tables):
        quoted = '"' + name.replace('"', '""') + '"'
        try:
            counts[name] = int(conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0])
        except sqlite3.Error as exc:
            raise DeployError("could not snapshot Challenge table row counts") from exc
    required = {"challenge_config_versions", "challenge_config_bosses"}
    if not required.issubset(tables):
        raise DeployError("Challenge config schema is incomplete")
    return counts


def _sqlite_error_category(exc: sqlite3.Error, fallback: str) -> str:
    code = getattr(exc, "sqlite_errorcode", None)
    message = str(exc).lower()
    if ((code is not None and (code & 0xFF) in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}) or
            "database is locked" in message or "database is busy" in message):
        return "database_busy"
    return fallback


def _database_metadata(path: Path) -> dict[str, tuple[Any, ...]]:
    """Validate stable DB/sidecar nodes and capture private content fingerprints."""
    def fail(category: str, node: str) -> DeployError:
        return DeployError(f"database inspection failed diagnostic_category={category} node={node}")

    def acl_for(node: Path, node_class: str) -> str:
        try:
            return _acl_text(node)
        except (OSError, DeployError):
            try:
                _db_lstat(node)
            except OSError:
                raise fail("database_busy", node_class) from None
            raise fail("unsafe_metadata", node_class) from None

    try:
        _safe_parent(path)
    except (OSError, DeployError):
        raise fail("unsafe_metadata", "ancestry") from None
    ancestry_before = _db_ancestry_snapshot(path)
    nodes: dict[str, tuple[Any, ...]] = {}
    paths = [("main", path), *((suffix, Path(str(path) + suffix))
                               for suffix in ("-wal", "-shm", "-journal"))]
    main_identity = None
    initially_present: set[str] = set()
    for label, node in paths:
        node_class = {"main": "database", "-wal": "wal", "-shm": "shm", "-journal": "journal"}[label]
        try:
            before = _db_lstat(node)
        except FileNotFoundError:
            if label == "main":
                raise fail("database_missing", "database") from None
            continue
        except OSError as exc:
            category = "database_busy" if label != "main" and exc.errno in {errno.ENOENT, errno.EAGAIN, errno.ESTALE} else "unsafe_metadata"
            raise fail(category, node_class) from None
        initially_present.add(label)
        if (not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode) or before.st_nlink != 1):
            raise fail("unsafe_metadata", node_class)
        if before.st_size > 512 * 1024 * 1024:
            raise fail("unsafe_metadata", node_class)
        identity = _stat_identity(before)
        if label == "main":
            main_identity = identity
            if path == DB and (before.st_uid, before.st_gid, stat.S_IMODE(before.st_mode)) != (1001, 33, 0o664):
                raise fail("unsafe_metadata", "database")
        elif main_identity is not None and identity[2:5] != main_identity[2:5]:
            raise fail("unsafe_metadata", node_class)
        elif main_identity is not None and identity[0] != main_identity[0]:
            raise fail("unsafe_metadata", node_class)
        try:
            acl_text = acl_for(node, node_class)
            if path == DB and label == "main":
                acl_digest = _validate_acl_profile(acl_text, _DB_FILE_ACL)
            elif path == DB:
                acl_digest = _validate_db_sidecar_acl(acl_text)
            else:
                acl_digest = _acl_hash(acl_text)
        except DeployError as exc:
            match = re.search(r"diagnostic_category=(database_busy|unsafe_metadata)", str(exc))
            raise fail(match.group(1) if match else "unsafe_metadata", node_class) from None
        except (OSError, ValueError):
            raise fail("unsafe_metadata", node_class) from None
        try:
            after = _db_lstat(node)
        except OSError:
            raise fail("database_busy", node_class) from None
        after_identity = _stat_identity(after)
        if identity != after_identity:
            raise fail("database_busy", node_class)
        if not stat.S_ISREG(after.st_mode):
            raise fail("database_busy", node_class)
        try:
            file_digest = _safe_file_digest(node, identity)
            post_read = _db_lstat(node)
            if _stat_identity(post_read) != identity:
                raise fail("database_busy", node_class)
            acl_after = acl_for(node, node_class)
            acl_after_digest = (_acl_hash(acl_after) if label == "main" or path != DB
                                else _raw_numeric_acl_hash(acl_after))
            if acl_after_digest != acl_digest:
                raise fail("unsafe_metadata", node_class)
        except DeployError as exc:
            match = re.search(r"diagnostic_category=(database_busy|unsafe_metadata)", str(exc))
            raise fail(match.group(1) if match else "unsafe_metadata", node_class) from None
        except (OSError, ValueError) as exc:
            category = "database_busy" if isinstance(exc, OSError) and exc.errno in {
                errno.ENOENT, errno.EAGAIN, errno.ESTALE, errno.ELOOP
            } else "unsafe_metadata"
            raise fail(category, node_class) from None
        nodes[label] = (*identity, acl_digest, file_digest)
    if "main" not in nodes:
        raise fail("database_missing", "database")
    # WAL/SHM/journal nodes may appear or disappear as SQLite opens/closes. A
    # transition during capture is contention, not evidence of unsafe static
    # metadata; the next attempt will validate the new settled state.
    for label, node in paths[1:]:
        try:
            present = _sidecar_present(node)
        except OSError:
            raise fail("database_busy", {"-wal": "wal", "-shm": "shm", "-journal": "journal"}[label]) from None
        if present != (label in initially_present):
            raise fail("database_busy", {"-wal": "wal", "-shm": "shm", "-journal": "journal"}[label])
    ancestry_after = _db_ancestry_snapshot(path)
    if ancestry_before != ancestry_after:
        if tuple(item[-1] for item in ancestry_before) != tuple(item[-1] for item in ancestry_after):
            raise fail("unsafe_metadata", "ancestry")
        raise fail("database_busy", "ancestry")
    if ancestry_before:
        nodes["ancestry"] = (ancestry_before,)
    return nodes


def _safe_file_digest(path: Path, identity: tuple[int, ...]) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        category = "database_busy" if exc.errno in {errno.ENOENT, errno.EAGAIN, errno.ESTALE, errno.ELOOP} else "unsafe_metadata"
        raise DeployError(f"database inspection failed diagnostic_category={category}") from None
    digest = hashlib.sha256()
    try:
        before = os.fstat(fd)
        current = (before.st_dev, before.st_ino, before.st_uid, before.st_gid,
                   stat.S_IMODE(before.st_mode), before.st_nlink, before.st_size, before.st_mtime_ns)
        if current[:6] != identity[:6]:
            raise DeployError("database inspection failed diagnostic_category=database_busy")
        if current[6:] != identity[6:]:
            raise DeployError("database inspection failed diagnostic_category=database_busy")
        total = 0
        while block := os.read(fd, 1024 * 1024):
            total += len(block)
            if total > 512 * 1024 * 1024:
                raise _database_failure("unsafe_metadata", "private_snapshot")
            digest.update(block)
        after = os.fstat(fd)
        final = (after.st_dev, after.st_ino, after.st_uid, after.st_gid,
                 stat.S_IMODE(after.st_mode), after.st_nlink, after.st_size, after.st_mtime_ns)
        try:
            named = path.lstat()
        except OSError:
            raise DeployError("database inspection failed diagnostic_category=database_busy") from None
        named_identity = (named.st_dev, named.st_ino, named.st_uid, named.st_gid,
                         stat.S_IMODE(named.st_mode), named.st_nlink, named.st_size,
                         named.st_mtime_ns)
        if current[:6] != final[:6] or current[:6] != named_identity[:6]:
            raise DeployError("database inspection failed diagnostic_category=database_busy")
        if current[6:] != final[6:] or current[6:] != named_identity[6:]:
            raise DeployError("database inspection failed diagnostic_category=database_busy")
        return digest.hexdigest()
    finally:
        os.close(fd)


def _raise_if_sidecar_changed(path: Path, baseline: dict[str, tuple[Any, ...]]) -> None:
    """Classify sidecar transitions during a snapshot as contention.

    An ACL mutation is deliberately different: it remains unsafe metadata.
    """
    for label in ("-wal", "-shm", "-journal"):
        node = Path(str(path) + label)
        node_class = {"-wal": "wal", "-shm": "shm", "-journal": "journal"}[label]
        try:
            current = _db_lstat(node)
        except FileNotFoundError:
            if label in baseline:
                raise _database_failure("database_busy", node_class) from None
            continue
        except OSError:
            raise _database_failure("database_busy", node_class) from None
        if label not in baseline:
            raise _database_failure("database_busy", node_class)
        if not stat.S_ISREG(current.st_mode) or stat.S_ISLNK(current.st_mode):
            raise _database_failure("database_busy", node_class)
        try:
            acl = _acl_text(node)
        except (OSError, DeployError):
            raise _database_failure("database_busy", node_class) from None
        if _acl_hash(acl) != baseline[label][8]:
            raise _database_failure("unsafe_metadata", node_class)
        if _stat_identity(current) != baseline[label][:8]:
            raise _database_failure("database_busy", node_class)
        if _safe_file_digest(node, baseline[label][:8]) != baseline[label][9]:
            raise _database_failure("database_busy", node_class)


@contextlib.contextmanager
def _readonly_database_snapshot(path: Path):
    """Yield a WAL-aware mode=ro connection over a stable private byte snapshot.

    SQLite's direct mode=ro WAL reader updates transient lock bytes in the live
    -shm sidecar. A private main/WAL copy keeps inspection byte-for-byte
    non-mutating while SQLite still applies committed WAL data (no immutable=1).
    """
    before = _database_metadata(path)
    if "-journal" in before:
        raise DeployError("database inspection failed diagnostic_category=database_busy")
    with tempfile.TemporaryDirectory(prefix="nocturne-challenge-db-inspect-", dir="/tmp") as directory:
        root = Path(directory)
        _safe_temp_snapshot_root(root)
        snapshot = root / path.name
        for label, suffix in (("main", ""), ("-wal", "-wal")):
            if label not in before:
                continue
            source = path if not suffix else Path(str(path) + suffix)
            destination = Path(str(snapshot) + suffix)
            src_fd = None
            dst_fd = None
            copied = hashlib.sha256()
            try:
                src_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                dst_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                                 getattr(os, "O_NOFOLLOW", 0), 0o600)
                os.fchmod(dst_fd, 0o600)
                opened = os.fstat(src_fd)
                expected = before[label][:8]
                actual = (opened.st_dev, opened.st_ino, opened.st_uid, opened.st_gid,
                          stat.S_IMODE(opened.st_mode), opened.st_nlink, opened.st_size, opened.st_mtime_ns)
                if actual != expected:
                    raise DeployError("database inspection failed diagnostic_category=database_busy")
                total = 0
                while block := os.read(src_fd, 1024 * 1024):
                    total += len(block)
                    if total > 512 * 1024 * 1024:
                            raise _database_failure("unsafe_metadata", "private_snapshot")
                    copied.update(block)
                    view = memoryview(block)
                    while view:
                        view = view[os.write(dst_fd, view):]
                after = os.fstat(src_fd)
                after_identity = (after.st_dev, after.st_ino, after.st_uid, after.st_gid,
                                  stat.S_IMODE(after.st_mode), after.st_nlink, after.st_size, after.st_mtime_ns)
                if after_identity != expected or copied.hexdigest() != before[label][9]:
                    raise DeployError("database inspection failed diagnostic_category=database_busy")
                os.fsync(dst_fd)
                copied_st = os.fstat(dst_fd)
                if (not stat.S_ISREG(copied_st.st_mode) or copied_st.st_nlink != 1 or
                        copied_st.st_uid != os.geteuid() or stat.S_IMODE(copied_st.st_mode) != 0o600):
                    raise _database_failure("unsafe_metadata", "private_snapshot")
            except OSError as exc:
                category = "database_busy" if exc.errno in {errno.ENOENT, errno.EAGAIN, errno.ESTALE, errno.ELOOP} else "unsafe_metadata"
                node_class = "database" if label == "main" else "wal"
                raise _database_failure(category, node_class) from None
            finally:
                if src_fd is not None:
                    os.close(src_fd)
                if dst_fd is not None:
                    os.close(dst_fd)
        try:
            after_metadata = _database_metadata(path)
        except DeployError:
            _raise_if_sidecar_changed(path, before)
            raise
        if after_metadata != before:
            raise DeployError("database inspection failed diagnostic_category=database_busy")
        uri = snapshot.as_uri() + "?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=10)
        except sqlite3.Error as exc:
            category = _sqlite_error_category(exc, "malformed_result")
            raise DeployError(f"database inspection failed diagnostic_category={category}") from None
        try:
            yield conn
        finally:
            conn.close()


def _inspect_sqlite_connection(conn: sqlite3.Connection,
                               expected_journal_mode: str | None = None) -> dict[str, str]:
    def one_text(sql: str) -> str:
        try:
            rows = conn.execute(sql).fetchall()
        except sqlite3.Error as exc:
            category = _sqlite_error_category(exc, "malformed_result")
            raise DeployError(f"database inspection failed diagnostic_category={category}") from None
        if len(rows) != 1 or len(rows[0]) != 1 or not isinstance(rows[0][0], str):
            raise DeployError("database inspection failed diagnostic_category=malformed_result")
        return rows[0][0].lower()

    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.Error as exc:
        category = _sqlite_error_category(exc, "integrity_failed")
        raise DeployError(f"database inspection failed diagnostic_category={category}") from None
    if len(rows) != 1 or len(rows[0]) != 1:
        raise DeployError("database inspection failed diagnostic_category=malformed_result")
    if rows[0][0] != "ok":
        raise DeployError("database inspection failed diagnostic_category=integrity_failed")
    journal = one_text("PRAGMA journal_mode")
    if expected_journal_mode is not None and journal != expected_journal_mode:
        raise DeployError("database inspection failed diagnostic_category=unsupported_journal_mode")
    locking = one_text("PRAGMA locking_mode")
    if locking != "normal":
        raise DeployError("database inspection failed diagnostic_category=unsupported_locking_mode")
    return {"integrity": "ok", "journal_mode": journal, "locking_mode": locking}


def _inspect_sqlite_profile(path: Path, *, expected_journal_mode: str | None = None) -> dict[str, str]:
    with _readonly_database_snapshot(path) as conn:
        return _inspect_sqlite_connection(conn, expected_journal_mode)


def _db_facts(path: Path, config_module, *, expected_journal_mode: str | None = None) -> dict[str, Any]:
    with _readonly_database_snapshot(path) as conn:
        profile = _inspect_sqlite_connection(conn, expected_journal_mode)
        conn.row_factory = sqlite3.Row
        try:
            active = conn.execute("SELECT config_version_id FROM challenge_config_versions WHERE status='active'").fetchall()
            if len(active) != 1 or int(active[0][0]) != 10:
                raise DeployError("active Challenge version identity is not version 10")
            document = config_module.config_document(conn)
            if int(document.get("version_id", -1)) != 10:
                raise DeployError("active config document is not version 10")
            _validate_legacy_defaults(document)
            return {"integrity": profile["integrity"], "journal_mode": profile["journal_mode"],
                    "locking_mode": profile["locking_mode"], "active_version_id": 10,
                    "document": document, "counts": _table_counts(conn)}
        except sqlite3.Error as exc:
            category = _sqlite_error_category(exc, "malformed_result")
            raise DeployError(f"database inspection failed diagnostic_category={category}") from None


def _validate_production_journal_profile(facts: dict[str, Any],
                                         metadata: dict[str, tuple[Any, ...]]) -> None:
    """Accept the observed production modes without weakening sidecar checks."""
    profile = facts
    if profile.get("integrity") != "ok":
        raise DeployError("database inspection failed diagnostic_category=integrity_failed")
    if profile.get("locking_mode") != "normal":
        raise DeployError("database inspection failed diagnostic_category=unsupported_locking_mode")
    sidecars = {"-wal", "-shm", "-journal"}.intersection(metadata)
    journal = profile.get("journal_mode")
    if journal == "wal":
        return
    if journal == "delete" and not sidecars:
        return
    raise DeployError("database inspection failed diagnostic_category=unsupported_journal_mode")


def _pre_migration_db_facts(path: Path, config_module) -> dict[str, Any]:
    """Inspect the production DB under its approved WAL or quiescent DELETE profile.

    The before/after metadata captures bind journal-mode policy to a stable
    ordinary-file/ACL snapshot. A DELETE-mode database is supported only when
    no WAL, SHM, or rollback-journal sidecar exists for that same snapshot.
    Fixture databases retain the generic integrity/schema checks.
    """
    if path != DB:
        return _db_facts(path, config_module)
    metadata_before = _database_metadata(path)
    facts = _db_facts(path, config_module)
    metadata_after = _database_metadata(path)
    if metadata_after != metadata_before:
        raise DeployError("database inspection failed diagnostic_category=database_busy")
    _validate_production_journal_profile(facts, metadata_before)
    return facts


def _legacy_projection(document: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(document)
    for boss in value.get("bosses", []):
        for key in NEW_COLUMNS:
            boss.pop(key, None)
    return value


def _validate_legacy_defaults(document: dict[str, Any]) -> None:
    for activity in document.get("bosses", []):
        metric = str(activity.get("metric_type", "time")).lower()
        if metric in {"time", "duration", "completion_time"}:
            if activity.get("timing_scope") != "unconfigured" or activity.get("automatic_capture") != "manual_only":
                raise DeployError("legacy time activity did not normalize to unconfigured/manual_only")
        elif metric == "numeric" and activity.get("automatic_capture") != "manual_only":
            raise DeployError("legacy numeric activity did not remain manual_only")


def _schema_columns(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    conn.row_factory = sqlite3.Row
    return {row["name"]: row for row in conn.execute("PRAGMA table_info(challenge_config_bosses)")}


def _validate_new_columns(conn: sqlite3.Connection) -> None:
    columns = _schema_columns(conn)
    for name in NEW_COLUMNS:
        row = columns.get(name)
        if row is None or str(row["type"]).upper() != "TEXT" or int(row["notnull"]) != 0 or row["dflt_value"] is not None:
            raise DeployError(f"metadata column is not additive nullable TEXT: {name}")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _verify_private_file(path: Path, owner_uid: int = 0) -> None:
    owner_gid = 0 if owner_uid == 0 else os.getegid()
    st = path.lstat()
    if (not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode) or st.st_nlink != 1 or
            st.st_uid != owner_uid or st.st_gid != owner_gid or stat.S_IMODE(st.st_mode) != 0o600):
        raise DeployError(f"unsafe private transaction file: {path.name}")


def _ensure_private_dir(path: Path, owner_uid: int = 0) -> None:
    owner_gid = 0 if owner_uid == 0 else os.getegid()
    if path.exists():
        st = path.lstat()
        if (not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode) or st.st_uid != owner_uid or
                st.st_gid != owner_gid or stat.S_IMODE(st.st_mode) != 0o700 or st.st_nlink < 2 or os.path.ismount(path)):
            raise DeployError(f"unsafe backup directory: {path}")
        _require_basic_acl(path)
        return
    path.mkdir(mode=0o700)
    os.chown(path, owner_uid, os.getegid() if owner_uid != 0 else 0)
    os.chmod(path, 0o700)
    _require_basic_acl(path)
    _fsync_dir(path.parent)


def _require_basic_acl(path: Path) -> None:
    text = _acl_text(path)
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        if line.startswith("default:"):
            raise DeployError(f"default ACL forbidden on private backup node: {path}")
        if line.startswith("user:") and not line.startswith("user::"):
            raise DeployError(f"named ACL forbidden on private backup node: {path}")
        if line.startswith("group:") and not line.startswith("group::"):
            raise DeployError(f"named ACL forbidden on private backup node: {path}")
        if line.startswith("mask::"):
            raise DeployError(f"extended ACL mask forbidden on private backup node: {path}")


def _atomic_replace(path: Path, data: bytes, meta: dict[str, Any]) -> None:
    parent = path.parent
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.timing-", dir=parent)
    temp = Path(temporary)
    try:
        os.fchown(fd, int(meta["uid"]), int(meta["gid"]))
        os.fchmod(fd, int(str(meta["mode"]), 8))
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(fd)
        proc = _run(["/usr/bin/setfacl", "--set-file=-", str(temp)], input_text=meta["acl_text"])
        if proc.returncode:
            raise DeployError("cannot reproduce captured ACL on temporary file")
        os.fsync(fd)
        os.replace(temp, path)
        _fsync_dir(parent)
        actual = capture_file(path)
        if (actual["sha256"] != _sha_bytes(data) or actual["uid"] != meta["uid"] or
                actual["gid"] != meta["gid"] or actual["mode"] != meta["mode"] or
                actual["nlink"] != 1 or actual["acl_sha256"] != meta["acl_sha256"]):
            raise DeployError(f"installed file metadata/content mismatch: {path}")
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
        if temp.exists():
            temp.unlink()


def _backup_database(source: Path, destination: Path, owner_uid: int = 0) -> None:
    if os.path.lexists(destination):
        raise DeployError("SQLite backup destination already exists")
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=15)
    dst = sqlite3.connect(destination, timeout=15)
    try:
        src.backup(dst, pages=256, sleep=.05)
        dst.commit()
        if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise DeployError("SQLite backup integrity verification failed")
    finally:
        dst.close()
        src.close()
    os.chmod(destination, 0o600)
    os.chown(destination, owner_uid, os.getegid() if owner_uid != 0 else 0)
    st = destination.lstat()
    if (not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode) or st.st_nlink != 1 or
            st.st_uid != owner_uid or stat.S_IMODE(st.st_mode) != 0o600):
        raise DeployError("SQLite backup file metadata is unsafe")
    _require_basic_acl(destination)
    fd = os.open(destination, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir(destination.parent)


def _copy_restore_database(backup: Path, target: Path, original: dict[str, Any]) -> None:
    for suffix in ("-journal", "-wal", "-shm"):
        if os.path.lexists(Path(str(target) + suffix)):
            raise DeployError("database sidecar prevents safe rollback")
    backup_stat = backup.lstat()
    if (not stat.S_ISREG(backup_stat.st_mode) or stat.S_ISLNK(backup_stat.st_mode) or
            backup_stat.st_nlink != 1 or stat.S_IMODE(backup_stat.st_mode) != 0o600 or
            backup_stat.st_uid not in {0, int(original["uid"])}):
        raise DeployError("database rollback backup metadata is unsafe")
    _require_basic_acl(backup)
    backup_fd = os.open(backup, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    if os.fstat(backup_fd).st_ino != backup_stat.st_ino or os.fstat(backup_fd).st_dev != backup_stat.st_dev:
        os.close(backup_fd)
        raise DeployError("database rollback backup changed before read")
    fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.restore-", dir=target.parent)
    temp = Path(temporary)
    try:
        os.fchown(fd, int(original["uid"]), int(original["gid"]))
        os.fchmod(fd, int(original["mode"], 8))
        with os.fdopen(fd, "wb", closefd=False) as stream, os.fdopen(backup_fd, "rb", closefd=False) as source:
            shutil.copyfileobj(source, stream, 1024 * 1024)
            stream.flush()
            os.fsync(fd)
        acl = _run(["/usr/bin/setfacl", "--set-file=-", str(temp)], input_text=original["acl_text"])
        if acl.returncode:
            raise DeployError("cannot restore database ACL")
        os.fsync(fd)
        os.replace(temp, target)
        _fsync_dir(target.parent)
        if _sha_file(target) != _sha_file(backup):
            raise DeployError("restored database digest verification failed")
    finally:
        try:
            os.close(backup_fd)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
        if temp.exists():
            temp.unlink()


def _load_config_module(release: Path):
    source = release / "dev/challenges/service/challenge_config.py"
    sys.path.insert(0, str(source.parent))
    spec = importlib.util.spec_from_file_location("challenge_config_deploy_target", source)
    if spec is None or spec.loader is None:
        raise DeployError("cannot load target Challenge config module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _http_json(url: str, *, expected: set[int] = {200}, maximum: int = 2 * 1024 * 1024,
               timeout: float = 4.0) -> tuple[int, dict[str, Any] | None]:
    import urllib.error
    import urllib.request
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, response, code, message, headers, new_url):
            return None
    request = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        response = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect).open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        exc.close()
        if status not in expected:
            raise DeployError(f"local endpoint returned unexpected HTTP {status}")
        return status, None
    except (OSError, TimeoutError) as exc:
        raise DeployError("local service health request failed") from exc
    with response:
        status = int(response.status)
        if status not in expected:
            raise DeployError(f"local endpoint returned unexpected HTTP {status}")
        body = response.read(maximum + 1)
        if len(body) > maximum:
            raise DeployError("local health response exceeded bound")
        if status != 200:
            return status, None
        try:
            payload = json.loads(body)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise DeployError("local health response was not valid JSON") from exc
        if not isinstance(payload, dict):
            raise DeployError("local health response shape is invalid")
        return status, payload


def _live_endpoint_json(url: str, category: str, *, expected: set[int] = {200}) -> tuple[int, dict[str, Any] | None]:
    """Probe one local endpoint without allowing its URL/error into diagnostics."""
    try:
        return _http_json(url, expected=expected)
    except Exception as exc:
        raise LiveVerificationError(category) from exc


def _require_intake_service_ready(systemd, *, timeout: float) -> None:
    try:
        state = systemd.show(INTAKE_SERVICE, timeout=timeout)
        ready = (
            isinstance(state, dict)
            and state.get("Id") == INTAKE_SERVICE
            and state.get("LoadState") == "loaded"
            and state.get("ActiveState") == "active"
            and state.get("SubState") == "running"
            and isinstance(state.get("MainPID"), str)
            and state["MainPID"].isascii()
            and state["MainPID"].isdecimal()
            and int(state["MainPID"]) > 0
        )
    except Exception as exc:
        raise LiveVerificationError("intake_service_unavailable") from exc
    if not ready:
        raise LiveVerificationError("intake_service_unavailable")


def _verify_intake_health(systemd) -> None:
    """Poll only intake readiness, bounded by monotonic time and service state."""
    url = "http://127.0.0.1:5011/health"
    deadline = time.monotonic() + INTAKE_HEALTH_READY_TIMEOUT_SECONDS
    last_failure: Exception | None = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LiveVerificationError("intake_health_failed") from last_failure
        try:
            status, payload = _http_json(
                url, timeout=min(INTAKE_HEALTH_REQUEST_TIMEOUT_SECONDS, remaining))
            if (time.monotonic() <= deadline and status == 200
                    and payload and payload.get("ok") is True):
                return
            last_failure = DeployError("intake readiness response was not ready")
        except DeployError as exc:
            # _http_json uses DeployError for bounded transport, status, and
            # JSON-shape failures. Keep its detail chained, never in output.
            last_failure = exc

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LiveVerificationError("intake_health_failed") from last_failure
        time.sleep(min(INTAKE_HEALTH_RETRY_INTERVAL_SECONDS, remaining))
        if time.monotonic() >= deadline:
            raise LiveVerificationError("intake_health_failed") from last_failure
        # Check immediately before the next request so an exited worker is not
        # misreported as a transient endpoint-readiness delay.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LiveVerificationError("intake_health_failed") from last_failure
        _require_intake_service_ready(systemd, timeout=min(2.0, remaining))


def verify_live_behavior(expected_document: dict[str, Any], *, systemd) -> dict[str, Any]:
    """Read-only bounded probes; response contents are never emitted."""
    if not isinstance(expected_document, dict) or expected_document.get("version_id") != 10:
        raise LiveVerificationError("configuration_version_mismatch")
    _verify_intake_health(systemd)
    _status, public_config = _live_endpoint_json(
        "http://127.0.0.1:5002/api/challenges/config/active", "configuration_read_failed")
    if not public_config:
        raise LiveVerificationError("configuration_read_failed")
    if public_config.get("version_id") != 10:
        raise LiveVerificationError("configuration_version_mismatch")
    if not isinstance(public_config.get("bosses"), list):
        raise LiveVerificationError("schema_verification_failed")
    normalized_public = {key: value for key, value in public_config.items() if key != "ok"}
    if normalized_public.get("version_id") != 10:
        raise LiveVerificationError("configuration_version_mismatch")
    if not isinstance(normalized_public.get("bosses"), list):
        raise LiveVerificationError("schema_verification_failed")
    if normalized_public != expected_document:
        raise LiveVerificationError("schema_verification_failed")
    try:
        _validate_legacy_defaults(normalized_public)
    except Exception as exc:
        raise LiveVerificationError("schema_verification_failed") from exc
    _status, leaderboard = _live_endpoint_json(
        "http://127.0.0.1:5002/api/leaderboards/modes", "leaderboard_api_failed")
    if not leaderboard or leaderboard.get("ok") is not True:
        raise LiveVerificationError("leaderboard_api_failed")
    # Deliberately test the existing auth gate without presenting or revealing
    # a cookie: the focused API/admin suite exercises the authenticated path.
    admin_status, _ = _live_endpoint_json(
        "http://127.0.0.1:5003/admin/api/challenges/config/published",
        "admin_auth_gate_failed",
        expected={200, 302, 401, 403},
    )
    if admin_status == 200:
        raise LiveVerificationError("admin_auth_gate_failed")
    return {"intake_health": "ok", "public_active_config": "ok",
            "active_version_id": 10, "leaderboard_read": "ok",
            "admin_auth_gate": "protected", "manual_submission": "unchanged; no submission sent"}


def _migrate(path: Path, config_module, before: dict[str, Any], tx: "Transaction") -> None:
    conn = sqlite3.connect(path, timeout=20, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=20000")
        # The production database is kept in WAL mode while services run.  Only
        # after the caller has stopped every known holder, verified the bounded
        # sidecar drain, and taken the transaction backup may SQLite switch it
        # to DELETE mode for the schema transaction.
        journal_mode = conn.execute("PRAGMA journal_mode=DELETE").fetchone()
        if not journal_mode or str(journal_mode[0]).lower() != "delete":
            raise DeployError("Challenges.db could not enter migration journal mode")
        conn.execute("BEGIN EXCLUSIVE")
        if str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower() != "delete":
            raise DeployError("Challenges.db journal mode changed during migration")
        columns = _schema_columns(conn)
        for name in NEW_COLUMNS:
            if name not in columns:
                conn.execute(f'ALTER TABLE challenge_config_bosses ADD COLUMN "{name}" TEXT')
        _validate_new_columns(conn)
        after_doc = config_module.config_document(conn)
        if int(after_doc.get("version_id", -1)) != 10:
            raise DeployError("active version changed during migration")
        _validate_legacy_defaults(after_doc)
        if _legacy_projection(before["document"]) != _legacy_projection(after_doc):
            raise DeployError("active version 10 changed semantically")
        if _table_counts(conn) != before["counts"]:
            raise DeployError("Challenge row counts changed during metadata migration")
        conn.commit()
        tx.database_after_meta = capture_file(path)
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def _unit_snapshot(systemd, units: tuple[str, ...]) -> dict[str, dict[str, str]]:
    snapshot = {}
    for unit in units:
        state = systemd.show(unit)
        if state["LoadState"] == "not-found":
            # An absent optional writer/timer is an explicit safe predecessor state.
            if unit in LONG_SERVICES:
                raise DeployError(f"required Challenge consumer unit is absent: {unit}")
        elif state["ActiveState"] not in {"active", "inactive"}:
            raise DeployError(f"unit is failed or transitional: {unit}")
        if unit in REQUIRED_ACTIVE_SERVICES and (state["ActiveState"] != "active" or state["SubState"] != "running" or int(state["MainPID"]) <= 0):
            raise DeployError(f"required import consumer is not active/running: {unit}")
        snapshot[unit] = state
    return snapshot


def _stop_for_migration(systemd, snapshot: dict[str, dict[str, str]]) -> None:
    # Suspend timers first so no Challenge writer can launch during the DDL.
    for unit in TIMERS:
        state = snapshot[unit]
        if state["LoadState"] == "loaded" and state["ActiveState"] == "active":
            systemd.stop(unit)
            systemd.wait_inactive(unit)
    # Drain one-shot jobs before stopping the long-lived import holders.
    for unit in WRITER_SERVICES:
        state = snapshot[unit]
        if state["LoadState"] == "loaded" and state["ActiveState"] == "active" and state["SubState"] != "exited":
            systemd.wait_job_idle(unit)
    # Long-lived SQLite import holders are the final known processes to stop.
    for unit in LONG_SERVICES:
        state = snapshot[unit]
        if state["LoadState"] == "loaded" and state["ActiveState"] == "active":
            systemd.stop(unit)
            systemd.wait_inactive(unit)


def _verify_migration_maintenance(systemd, snapshot: dict[str, dict[str, str]]) -> None:
    """Require every captured Challenge unit to be safely paused or drained."""
    for unit, before in snapshot.items():
        try:
            current = systemd.show(unit)
        except Exception as exc:
            raise CategorizedDeployError("holder_scan_failed") from exc
        if current.get("LoadState") != before.get("LoadState"):
            raise CategorizedDeployError("known_holder_still_active")
        if before.get("LoadState") == "not-found":
            if current.get("ActiveState") != "inactive" or current.get("SubState") != "dead":
                raise CategorizedDeployError("known_holder_still_active")
            continue
        pid = current.get("MainPID")
        if unit.endswith(".timer"):
            if (current.get("ActiveState") != "inactive" or current.get("SubState") != "dead" or
                    pid not in {None, "0"}):
                raise CategorizedDeployError("known_holder_still_active")
        elif unit in LONG_SERVICES:
            if (current.get("ActiveState") != "inactive" or current.get("SubState") != "dead" or
                    pid != "0"):
                raise CategorizedDeployError("known_holder_still_active")
        elif unit in WRITER_SERVICES:
            safe_idle = (current.get("ActiveState"), current.get("SubState"), pid) in {
                ("inactive", "dead", "0"), ("active", "exited", "0")}
            if not safe_idle:
                raise CategorizedDeployError("known_holder_still_active")
        else:
            raise CategorizedDeployError("holder_scan_failed")


def _sidecar_state_snapshot(path: Path) -> dict[str, tuple[Any, ...]]:
    """Capture only validated sidecar identity/metadata while waiting for close."""
    try:
        main = _db_lstat(path)
    except OSError as exc:
        raise CategorizedDeployError("database_identity_changed") from exc
    result: dict[str, tuple[Any, ...]] = {}
    for label, suffix in (("-journal", "-journal"), ("-wal", "-wal"), ("-shm", "-shm")):
        node = Path(str(path) + suffix)
        try:
            st = _db_lstat(node)
        except FileNotFoundError:
            continue
        except OSError as exc:
            retryable = exc.errno in {errno.ENOENT, errno.EAGAIN, errno.ESTALE}
            raise CategorizedDeployError("sidecar_identity_changed", retryable=retryable) from exc
        if (not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode) or st.st_nlink != 1 or
                st.st_size > 512 * 1024 * 1024 or
                (st.st_dev, st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) !=
                (main.st_dev, main.st_uid, main.st_gid, stat.S_IMODE(main.st_mode))):
            raise CategorizedDeployError("sidecar_metadata_changed")
        try:
            acl_text = _acl_text(node)
            acl_digest = (_validate_db_sidecar_acl(acl_text) if path == DB else _acl_hash(acl_text))
        except (OSError, ValueError, DeployError) as exc:
            raise CategorizedDeployError("sidecar_metadata_changed") from exc
        identity = _stat_identity(st)
        try:
            content_digest = _safe_file_digest(node, identity)
        except (OSError, DeployError) as exc:
            try:
                node.lstat()
            except FileNotFoundError:
                raise CategorizedDeployError("sidecar_identity_changed", retryable=True) from exc
            except OSError as stat_error:
                retryable = stat_error.errno in {errno.ENOENT, errno.EAGAIN, errno.ESTALE}
                raise CategorizedDeployError("sidecar_identity_changed", retryable=retryable) from exc
            raise CategorizedDeployError("sidecar_content_changed") from exc
        try:
            after = _db_lstat(node)
            acl_after = _acl_text(node)
        except OSError as exc:
            retryable = exc.errno in {errno.ENOENT, errno.EAGAIN, errno.ESTALE}
            raise CategorizedDeployError("sidecar_identity_changed", retryable=retryable) from exc
        except DeployError as exc:
            raise CategorizedDeployError("sidecar_metadata_changed") from exc
        if _stat_identity(after) != identity:
            raise CategorizedDeployError("sidecar_identity_changed")
        acl_after_digest = _raw_numeric_acl_hash(acl_after) if path == DB else _acl_hash(acl_after)
        if acl_after_digest != acl_digest:
            raise CategorizedDeployError("sidecar_metadata_changed")
        result[label] = (*identity, acl_digest, content_digest)
    return result


def _database_open_holders(path: Path, identities: set[tuple[int, int]]) -> set[int]:
    """Find processes holding any captured DB/sidecar inode through /proc/fd."""
    holders: set[int] = set()
    try:
        processes = list(os.scandir("/proc"))
    except OSError as exc:
        raise CategorizedDeployError("holder_scan_failed") from exc
    for process in processes:
        if not process.name.isdigit():
            continue
        pid = int(process.name)
        fd_directory = f"/proc/{process.name}/fd"
        try:
            descriptors = list(os.scandir(fd_directory))
        except (FileNotFoundError, ProcessLookupError):
            continue
        except PermissionError:
            raise CategorizedDeployError("holder_scan_failed") from None
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ESRCH}:
                continue
            raise CategorizedDeployError("holder_scan_failed") from exc
        for descriptor in descriptors:
            try:
                opened = os.stat(descriptor.path)
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError:
                raise CategorizedDeployError("holder_scan_failed") from None
            except OSError as exc:
                if exc.errno in {errno.ENOENT, errno.ESRCH}:
                    continue
                raise CategorizedDeployError("holder_scan_failed") from exc
            if (opened.st_dev, opened.st_ino) in identities:
                holders.add(pid)
                break
    return holders


def _wait_for_no_sqlite_sidecars(path: Path, systemd, snapshot: dict[str, dict[str, str]],
                                 initial_sidecars: dict[str, tuple[Any, ...]], *,
                                 main_identity: tuple[int, ...] | None = None,
                                 timeout: float | None = None,
                                 interval: float | None = None) -> None:
    """Wait boundedly for normal SQLite close; never unlink sidecars."""
    timeout = SIDECAR_DRAIN_TIMEOUT_SECONDS if timeout is None else timeout
    interval = SIDECAR_DRAIN_INTERVAL_SECONDS if interval is None else interval
    deadline = time.monotonic() + timeout
    previous: dict[str, tuple[Any, ...]] | None = None
    disappeared: set[str] = set()
    empty_observations = 0
    try:
        main = _db_lstat(path)
    except OSError as exc:
        raise CategorizedDeployError("database_identity_changed") from exc
    expected_main = main_identity[:6] if main_identity is not None else _stat_identity(main)[:6]
    if _stat_identity(main)[:6] != expected_main:
        raise CategorizedDeployError("database_identity_changed")
    tracked = {(main.st_dev, main.st_ino)}
    tracked.update((value[0], value[1]) for value in initial_sidecars.values())
    while True:
        _verify_migration_maintenance(systemd, snapshot)
        try:
            current_main = _db_lstat(path)
        except OSError as exc:
            raise CategorizedDeployError("database_identity_changed") from exc
        if _stat_identity(current_main)[:6] != expected_main:
            raise CategorizedDeployError("database_identity_changed")
        try:
            current = _sidecar_state_snapshot(path)
        except CategorizedDeployError as exc:
            # A sidecar disappearing while its metadata is sampled can race
            # the last SQLite close. Retry only that bounded ENOENT/ESTALE
            # class; every other category fails closed immediately.
            if not exc.retryable:
                raise
            current = None
        except DeployError as exc:
            raise CategorizedDeployError("sidecar_metadata_changed") from exc
        if current is not None:
            if set(current) - set(initial_sidecars):
                raise CategorizedDeployError("sidecar_identity_changed")
            if any(label in disappeared for label in current):
                raise CategorizedDeployError("sidecar_reappeared")
            for label, value in current.items():
                original = initial_sidecars.get(label)
                if original is None or value[:2] != original[:2]:
                    raise CategorizedDeployError("sidecar_identity_changed")
                if value[2:6] != original[2:6] or value[8] != original[8]:
                    raise CategorizedDeployError("sidecar_metadata_changed")
                if value[6:8] != original[6:8] or value[9] != original[9]:
                    raise CategorizedDeployError("sidecar_content_changed")
                if previous is not None and label in previous:
                    prior = previous[label]
                    if value[:2] != prior[:2]:
                        raise CategorizedDeployError("sidecar_identity_changed")
                    if value[2:6] != prior[2:6] or value[8] != prior[8]:
                        raise CategorizedDeployError("sidecar_metadata_changed")
                    if value[6:8] != prior[6:8] or value[9] != prior[9]:
                        raise CategorizedDeployError("sidecar_content_changed")
            if previous is not None:
                disappeared.update(set(previous) - set(current))
            if previous is None:
                disappeared.update(set(initial_sidecars) - set(current))
            if not current:
                try:
                    holders = _database_open_holders(path, tracked)
                except CategorizedDeployError:
                    raise
                except DeployError as exc:
                    raise CategorizedDeployError("holder_scan_failed") from exc
                if holders:
                    raise CategorizedDeployError("unknown_holder_present")
                empty_observations += 1
                if empty_observations >= 2:
                    return
            else:
                empty_observations = 0
            previous = current
        if time.monotonic() >= deadline:
            raise CategorizedDeployError("sidecar_disappearance_timeout")
        time.sleep(min(interval, max(0.0, deadline - time.monotonic())))


def _restore_units(systemd, snapshot: dict[str, dict[str, str]]) -> None:
    failures = []
    # Services first, then timers, preserving only originally active units.
    for unit in LONG_SERVICES:
        state = snapshot.get(unit, {})
        if state.get("LoadState") == "loaded" and state.get("ActiveState") == "active":
            try:
                current = systemd.show(unit)
                if not (current.get("LoadState") == "loaded" and current.get("ActiveState") == "active" and
                        current.get("SubState") == "running" and current.get("MainPID", "0").isdigit() and
                        int(current.get("MainPID", "0")) > 0):
                    systemd.start(unit)
                    systemd.wait_active(unit)
            except Exception:
                failures.append(unit)
    for unit in TIMERS:
        state = snapshot.get(unit, {})
        if state.get("LoadState") == "loaded" and state.get("ActiveState") == "active":
            try:
                current = systemd.show(unit)
                if not (current.get("LoadState") == "loaded" and current.get("ActiveState") == "active" and
                        current.get("SubState") == "waiting" and current.get("MainPID") in {None, "0"}):
                    systemd.start(unit)
            except Exception:
                failures.append(unit)
    if failures:
        raise DeployError("could not restore original Challenge unit state")


def _verify_installed(path: Path, item: dict[str, Any]) -> None:
    state = capture_file(path)
    baseline = item["before"]
    if (state["sha256"] != item["after_sha256"] or state["uid"] != baseline["uid"] or
            state["gid"] != baseline["gid"] or state["mode"] != baseline["mode"] or
            state["size"] != item["after_size"] or state["nlink"] != 1 or
            state["acl_sha256"] != baseline["acl_sha256"]):
        raise DeployError(f"installed target verification failed: {path}")


def _validate_file_prestate(items: list[dict[str, Any]]) -> str:
    states: dict[str, str] = {}
    for item in items:
        actual = capture_file(item["target"])
        expected = item["before"]
        if (actual["uid"] != expected.get("uid") or actual["gid"] != expected.get("gid") or
                actual["mode"] != expected.get("mode") or actual["nlink"] != expected.get("nlink") or
                actual["acl_sha256"] != expected.get("acl_sha256")):
            raise DeployError(f"live target metadata drift: {item['target']}")
        if actual["sha256"] == item["after_sha256"]:
            state, expected_size = "after", item["after_size"]
        elif actual["sha256"] == expected.get("sha256"):
            state, expected_size = "before", expected.get("size")
        else:
            predecessor = next((profile for profile in item.get("safe_predecessors", [])
                                if actual["sha256"] == profile.get("sha256")), None)
            if predecessor is None:
                raise DeployError(f"live target content drift: {item['target']}")
            if any(actual.get(key) != predecessor.get(key)
                   for key in ("uid", "gid", "mode", "nlink", "size", "acl_sha256")):
                raise DeployError(f"live target predecessor metadata drift: {item['target']}")
            state, expected_size = "safe_predecessor", predecessor["size"]
        if actual["size"] != expected_size:
            raise DeployError(f"live target metadata drift: {item['target']}")
        states[str(item["target"])] = state
    if states and all(value == "before" for value in states.values()):
        return "before"
    if states and all(value == "after" for value in states.values()):
        return "after"
    # The only mixed state admitted is the exact prior release for this
    # label-only upgrade: its admin page plus already-current unchanged files.
    safe_predecessor_targets = {
        str(item["target"]) for item in items if item.get("safe_predecessors")
    }
    for target in safe_predecessor_targets:
        if (states.get(target) == "safe_predecessor" and
                all(value == "after" for path, value in states.items() if path != target) and
                len(states) == len(FILES)):
            return "predecessor"
    raise DeployError("mixed old/new live file set; recover before retrying")


def make_plan(*, commit: str, repo: Path = REPO, runtime_root: Path = RUNTIME,
              database: Path = DB, release: Path | None = None, systemd=None) -> dict[str, Any]:
    verify_git(repo, commit)
    release = release or runtime_root / "releases" / commit
    rel_manifest, source_manifest = _load_manifest(release, commit, runtime_root)
    items = verify_extension_sources(release, rel_manifest, source_manifest)
    prepared_output = _prepared_check(release, commit, runtime_root)
    file_state = _validate_file_prestate(items)
    systemd = systemd or Systemd()
    units = _unit_snapshot(systemd, CONTROLLED)
    config_module = _load_config_module(release)
    facts = _pre_migration_db_facts(database, config_module)
    return {
        "status": "already_current" if file_state == "after" and _has_columns(database) else "dry_run",
        "target": commit,
        "release_manifest_sha256": _sha_file(release / "RELEASE-MANIFEST.json"),
        "prepared": True,
        "source_files": [{"release_path": x["source_relative"], "target": str(x["target"]),
                          "before_sha256": x["before"]["sha256"], "after_sha256": x["after_sha256"]} for x in items],
        "file_set": file_state,
        "active_version_id": facts["active_version_id"],
        "database_integrity": facts["integrity"],
        "schema_columns_present": _has_columns(database),
        "service_units": {name: {k: v for k, v in state.items() if k != "Result"}
                          for name, state in units.items()},
        "pause_if_active": list(TIMERS),
        "drain_if_running": list(WRITER_SERVICES),
        "stop_and_restart_if_active": list(LONG_SERVICES),
        "services_restarted_for_imports": list(REQUIRED_ACTIVE_SERVICES),
        "nginx_changed": False,
        "announcement_version_published": False,
        "prepared_check_summary_sha256": _sha_bytes(prepared_output.encode()),
    }


def _has_columns(path: Path) -> bool:
    with _readonly_database_snapshot(path) as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(challenge_config_bosses)")}
        return set(NEW_COLUMNS).issubset(columns)


@dataclass
class Transaction:
    backup_dir: Path
    database_backup: Path
    database_before: dict[str, Any]
    original_db_meta: dict[str, Any]
    item_backups: list[tuple[dict[str, Any], bytes, dict[str, Any]]]
    units: dict[str, dict[str, str]]
    installed: list[dict[str, Any]]
    config_module: Any
    database_path: Path
    db_changed: bool = False
    database_after_meta: dict[str, Any] | None = None


def _create_backup_dir(root: Path, commit: str, owner_uid: int = 0) -> Path:
    _ensure_private_dir(root, owner_uid)
    commit_dir = root / commit
    if not commit_dir.exists():
        commit_dir.mkdir(mode=0o700)
        os.chown(commit_dir, owner_uid, os.getegid() if owner_uid != 0 else 0)
        os.chmod(commit_dir, 0o700)
        _fsync_dir(root)
    _ensure_private_dir(commit_dir, owner_uid)
    txn = commit_dir / (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:12])
    txn.mkdir(mode=0o700)
    os.chown(txn, owner_uid, os.getegid() if owner_uid != 0 else 0)
    os.chmod(txn, 0o700)
    _ensure_private_dir(txn, owner_uid)
    _fsync_dir(commit_dir)
    return txn


def _deployment_lock(path: Path = LOCK_PATH) -> int:
    st_parent = path.parent.lstat()
    if (not stat.S_ISDIR(st_parent.st_mode) or stat.S_ISLNK(st_parent.st_mode) or
            st_parent.st_uid != 0 or stat.S_IMODE(st_parent.st_mode) & 0o022):
        raise DeployError("deployment lock parent is unsafe")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    st = os.fstat(fd)
    if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != 0 or
            stat.S_IMODE(st.st_mode) != 0o600):
        os.close(fd)
        raise DeployError("deployment lock file is unsafe")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        raise DeployError("another Challenge timing deployment is active") from exc
    return fd


def _backup_files(items: list[dict[str, Any]], backup_dir: Path) -> list[tuple[dict[str, Any], bytes, dict[str, Any]]]:
    backups = []
    records = []
    for index, item in enumerate(items):
        state = capture_file(item["target"])
        data = _read_regular(item["target"], max(state["size"] + 1, 1))
        if _sha_bytes(data) != state["sha256"]:
            raise DeployError("target changed while creating backup")
        backup_path = backup_dir / f"file-{index}.backup"
        _write_private(backup_path, data)
        _verify_private_file(backup_path, os.geteuid())
        backups.append((item, data, state))
        records.append({"target": str(item["target"]), "backup": backup_path.name,
                        "sha256": state["sha256"], "uid": state["uid"], "gid": state["gid"],
                        "mode": state["mode"], "nlink": state["nlink"], "acl_sha256": state["acl_sha256"],
                        "acl_text": state["acl_text"]})
    _write_private(backup_dir / "file-backups.json", json.dumps(records, sort_keys=True, separators=(",", ":")).encode())
    _verify_private_file(backup_dir / "file-backups.json", os.geteuid())
    _fsync_dir(backup_dir)
    return backups


def _write_tx_record(tx: Transaction, commit: str, state: str) -> None:
    record = {"schema": 1, "target": commit, "state": state,
              "database_backup_sha256": _sha_file(tx.database_backup),
              "database_before": {"sha256": tx.original_db_meta["sha256"],
                                  "uid": tx.original_db_meta["uid"], "gid": tx.original_db_meta["gid"],
                                  "mode": tx.original_db_meta["mode"], "nlink": tx.original_db_meta["nlink"],
                                  "acl_sha256": tx.original_db_meta["acl_sha256"],
                                  "acl_text": tx.original_db_meta["acl_text"],
                                  "integrity": tx.database_before["integrity"],
                                  "active_version_id": tx.database_before["active_version_id"],
                                  "table_counts": tx.database_before["counts"]},
              "prior_units": tx.units,
              "files": [{"target": str(item["target"]), "before_sha256": meta["sha256"],
                         "after_sha256": item["after_sha256"], "uid": meta["uid"],
                         "gid": meta["gid"], "mode": meta["mode"], "nlink": meta["nlink"],
                         "acl_sha256": meta["acl_sha256"], "acl_text": meta["acl_text"]}
                        for item, _data, meta in tx.item_backups]}
    path = tx.backup_dir / "transaction.json"
    temp = tx.backup_dir / ".transaction.tmp"
    if temp.exists():
        raise DeployError("unexpected transaction record temporary exists")
    _write_private(temp, json.dumps(record, sort_keys=True, separators=(",", ":")).encode())
    _verify_private_file(temp, os.geteuid())
    os.replace(temp, path)
    _fsync_dir(tx.backup_dir)


def _restore_transaction(tx: Transaction, systemd, commit: str) -> None:
    # Do not replace a database/file while a process may still have it open.
    for unit in (*TIMERS, *LONG_SERVICES, *WRITER_SERVICES):
        state = systemd.show(unit)
        if state["LoadState"] == "loaded" and state["ActiveState"] == "active":
            systemd.stop(unit)
            systemd.wait_inactive(unit)
    database_state = None
    if tx.db_changed:
        database_state = capture_file(tx.database_path)
        if (not _same_file_metadata(database_state, tx.original_db_meta) and
                not (tx.database_after_meta and _same_file_metadata(database_state, tx.database_after_meta))):
            raise DeployError("database changed outside the guarded migration; refusing rollback overwrite")
    restore_files: list[tuple[dict[str, Any], bytes, dict[str, Any]]] = []
    for item, data, meta in reversed(tx.item_backups):
        current = capture_file(item["target"])
        if _same_file_metadata(current, meta):
            continue
        if (current["sha256"] != item["after_sha256"] or current["uid"] != meta["uid"] or
                current["gid"] != meta["gid"] or current["mode"] != meta["mode"] or
                current["nlink"] != 1 or current["acl_sha256"] != meta["acl_sha256"] or
                current["size"] != item["after_size"]):
            raise DeployError("live file changed outside the guarded install; refusing rollback overwrite")
        restore_files.append((item, data, meta))
    if tx.db_changed:
        if database_state and not _same_file_metadata(database_state, tx.original_db_meta):
            _copy_restore_database(tx.database_backup, tx.database_path, tx.original_db_meta)
            restored_meta = capture_file(tx.database_path)
            if restored_meta["sha256"] != _sha_file(tx.database_backup):
                raise DeployError("restored database differs from verified SQLite backup")
        restored = _db_facts(tx.database_path, tx.config_module,
                             expected_journal_mode=tx.database_before.get("journal_mode"))
        if (restored["active_version_id"] != tx.database_before["active_version_id"] or
                restored["counts"] != tx.database_before["counts"] or
                _legacy_projection(restored["document"]) != _legacy_projection(tx.database_before["document"])):
            raise DeployError("restored database does not match transaction pre-state")
    for item, data, meta in restore_files:
        _atomic_replace(item["target"], data, meta)
    _restore_units(systemd, tx.units)
    _write_tx_record(tx, commit, "rolled_back")


def apply_install(plan: dict[str, Any], *, commit: str, repo: Path = REPO,
                  runtime_root: Path = RUNTIME, database: Path = DB,
                  backup_root: Path = BACKUP_ROOT, release: Path | None = None,
                  systemd=None, failpoint: Callable[[str], None] | None = None,
                  _testing_owner_uid: int = 0) -> dict[str, Any]:
    if os.geteuid() != 0 and _testing_owner_uid == 0:
        raise DeployError("--apply requires root")
    if database != DB and _testing_owner_uid == 0:
        raise DeployError("production database path is fixed")
    systemd = systemd or Systemd()
    release = release or runtime_root / "releases" / commit
    rel_manifest, source_manifest = _load_manifest(release, commit, runtime_root)
    items = verify_extension_sources(release, rel_manifest, source_manifest)
    verify_git(repo, commit)
    if plan.get("target") != commit or plan.get("release_manifest_sha256") != _sha_file(release / "RELEASE-MANIFEST.json"):
        raise DeployError("plan/release identity changed before apply")
    state = _validate_file_prestate(items)
    config_module = _load_config_module(release)
    before = _pre_migration_db_facts(database, config_module)
    if before["active_version_id"] != 10:
        raise DeployError("active version changed before apply")
    schema_present = _has_columns(database)
    if state == "after" and schema_present:
        check_conn = sqlite3.connect(database)
        try:
            _validate_new_columns(check_conn)
        finally:
            check_conn.close()
        return {"status": "already_current", "target": commit, "active_version_id": 10,
                "database_integrity": before["integrity"], "nginx_changed": False,
                "configuration_version_published": False}
    if state == "after" or (schema_present and state != "predecessor"):
        raise DeployError("partial file/schema state requires operator recovery")
    main_identity_before_stop = _stat_identity(_db_lstat(database))
    sidecars_before_stop = _sidecar_state_snapshot(database)
    units = _unit_snapshot(systemd, CONTROLLED)
    backup_dir = _create_backup_dir(backup_root, commit, _testing_owner_uid)
    db_backup = backup_dir / "Challenges.db.sqlite-backup"
    item_backups: list[tuple[dict[str, Any], bytes, dict[str, Any]]] = []
    tx: Transaction | None = None
    phase = "maintenance_stop"
    try:
        _stop_for_migration(systemd, units)
        phase = "sidecar_quiescence"
        # Re-read every controlled unit after the stop/wait calls.  In
        # particular, a successful stop request alone is not evidence that
        # the shared-UID admin workers have released SQLite.
        _verify_migration_maintenance(systemd, units)
        _wait_for_no_sqlite_sidecars(database, systemd, units, sidecars_before_stop,
                                     main_identity=main_identity_before_stop)
        # SQLite may checkpoint committed WAL pages into the main file while
        # the final known connection closes.  Revalidate semantic state after
        # that normal close, then capture the exact rollback image metadata.
        phase = "database_revalidation"
        quiesced = _db_facts(database, config_module, expected_journal_mode=before["journal_mode"])
        if (quiesced["active_version_id"] != before["active_version_id"] or
                quiesced["counts"] != before["counts"] or
                _legacy_projection(quiesced["document"]) != _legacy_projection(before["document"])):
            raise DeployError("Challenges.db semantic state changed while services were quiescing")
        phase = "backup_initialization"
        original_db_meta = capture_file(database)
        tx = Transaction(backup_dir, db_backup, before, original_db_meta, item_backups,
                         units, [], config_module, database)
        phase = "backup_creation"
        item_backups = _backup_files(items, backup_dir)
        tx.item_backups = item_backups
        _backup_database(database, db_backup, _testing_owner_uid)
        if not _same_file_metadata(capture_file(database), original_db_meta):
            raise DeployError("Challenges.db changed during transactional backup")
        backup_facts = _db_facts(db_backup, config_module,
                                 expected_journal_mode=before.get("journal_mode"))
        if (backup_facts["active_version_id"] != before["active_version_id"] or
                backup_facts["counts"] != before["counts"] or
                _legacy_projection(backup_facts["document"]) != _legacy_projection(before["document"])):
            raise DeployError("transactional SQLite backup does not match pre-migration state")
        if failpoint: failpoint("backup_complete")
        _write_tx_record(tx, commit, "backed_up")
        # Recapture all targets immediately before any mutation.
        phase = "backup_verification"
        for item, _data, metadata in item_backups:
            fresh = capture_file(item["target"])
            if any(fresh[key] != metadata[key] for key in ("sha256", "uid", "gid", "mode", "size", "nlink", "acl_sha256")):
                raise DeployError("live target changed after backup")
        phase = "file_installation"
        backup_by_target = {str(item["target"]): (data, meta) for item, data, meta in item_backups}
        for item in items:
            data = _read_regular(item["source"])
            if _sha_bytes(data) != item["after_sha256"]:
                raise DeployError("immutable source changed during apply")
            old_data, old_meta = backup_by_target[str(item["target"])]
            fresh = capture_file(item["target"])
            if any(fresh[key] != old_meta[key] for key in ("sha256", "uid", "gid", "mode", "size", "nlink", "acl_sha256")):
                raise DeployError("target changed immediately before replacement")
            _atomic_replace(item["target"], data, {**item["before"], "acl_text": old_meta["acl_text"]})
            tx.installed.append(item)
            _verify_installed(item["target"], item)
            if failpoint: failpoint(f"file:{item['target'].name}")
        # If interrupted immediately after SQLite commits, rollback must still
        # restore the already verified snapshot.
        phase = "schema_migration"
        tx.db_changed = True
        _migrate(database, config_module, before, tx)
        if failpoint: failpoint("migration_complete")
        phase = "post_migration_verification"
        after = _db_facts(database, config_module, expected_journal_mode="delete")
        if after["counts"] != before["counts"] or _legacy_projection(before["document"]) != _legacy_projection(after["document"]):
            raise DeployError("post-migration Challenge data preservation check failed")
        check_conn = sqlite3.connect(database)
        try:
            _validate_new_columns(check_conn)
        finally:
            check_conn.close()
        for item in items: _verify_installed(item["target"], item)
        if failpoint: failpoint("post_install_verify")
        phase = "service_restoration"
        _restore_units(systemd, units)
        if failpoint: failpoint("services_restored")
        phase = "live_behavior_verification"
        live = verify_live_behavior(after["document"], systemd=systemd)
        phase = "transaction_commit_record"
        _write_tx_record(tx, commit, "committed")
        return {"status": "installed", "target": commit, "backup_record": str(backup_dir / "transaction.json"),
                "active_version_id": 10, "database_integrity": after["integrity"],
                "schema_columns_added_or_verified": list(NEW_COLUMNS),
                "services_restarted": [unit for unit in LONG_SERVICES
                                        if units[unit].get("LoadState") == "loaded" and
                                        units[unit].get("ActiveState") == "active"],
                "nginx_changed": False,
                "configuration_version_published": False, "post_install": live}
    except BaseException as error:
        diagnostic_category = (error.diagnostic_category
            if phase == "live_behavior_verification" and isinstance(error, LiveVerificationError) else
            error.diagnostic_category if phase == "sidecar_quiescence" and isinstance(error, CategorizedDeployError) else
            "unexpected_failure" if not isinstance(error, DeployError) else
            "maintenance_failure" if phase == "maintenance_stop" else
            "sidecar_quiescence_failure" if phase == "sidecar_quiescence" else
            "database_validation_failure" if phase in {"database_revalidation", "post_migration_verification"} else
            "backup_failure" if phase.startswith("backup_") or phase == "backup_creation" else
            "file_installation_failure" if phase == "file_installation" else
            "migration_failure" if phase == "schema_migration" else
            "service_restoration_failure" if phase == "service_restoration" else
            "live_verification_failure" if phase == "live_behavior_verification" else
            "transaction_record_failure")
        try:
            if tx is not None and tx.database_backup.exists() and tx.item_backups:
                _restore_transaction(tx, systemd, commit)
            else:
                _restore_units(systemd, units)
        except Exception as rollback_error:
            raise DeployError(
                f"installation failed; rollback incomplete phase={phase} diagnostic_category=rollback_failure"
            ) from error
        raise DeployError(
            f"installation failed and was rolled back phase={phase} diagnostic_category={diagnostic_category}"
        ) from error


def _cli() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="read-only plan (default)")
    mode.add_argument("--apply", action="store_true", help="perform guarded installation; root required")
    parser.add_argument("--commit", required=True, help="exact full SHA of the clean prepared release")
    args = parser.parse_args()
    try:
        plan = make_plan(commit=args.commit)
        if args.apply:
            if os.geteuid() != 0:
                raise DeployError("--apply requires root")
            lock_fd = _deployment_lock()
            try:
                # Re-plan under the lock so no stale preflight snapshot is used.
                plan = make_plan(commit=args.commit)
                result = apply_install(plan, commit=args.commit)
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
        else:
            result = plan
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except DeployError as exc:
        print(json.dumps({"status": "blocked", "error": str(exc)}, sort_keys=True, separators=(",", ":")))
        return 2
    except Exception as exc:
        print(json.dumps({"status": "blocked", "error": f"unexpected_{type(exc).__name__}"},
                         sort_keys=True, separators=(",", ":")))
        return 2


if __name__ == "__main__":
    raise SystemExit(_cli())
