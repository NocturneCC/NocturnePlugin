#!/usr/bin/env python3
"""Dry-run-first installer for retiring the CSV-backed Challenge progress view.

All source bytes come from the exact prepared immutable release. The only
long-running service restarted is osrs-drops-api.service, whose api:app imports
challenge_config_api.py. No database migration, Nginx change, or data write is
performed. A consistent read-only SQLite backup is retained with the file
backups as transaction evidence.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import types
import uuid
from pathlib import Path
from typing import Any


REPO = Path("/srv/projects/nocturne-plugin-intake")
RUNTIME = Path("/srv/nocturne-plugin")
DATABASE = Path("/srv/projects/database/Challenges.db")
BACKUP_ROOT = Path("/var/backups/nocturne-challenge-retirement")
LOCK_PATH = Path("/run/nocturne-legacy-challenge-retirement.lock")
SERVICE = "osrs-drops-api.service"
SHEET_TIMER = "nocturne-challenge-sheet-sync.timer"
SHEET_SERVICE = "nocturne-challenge-sheet-sync.service"
API_REL = "dev/challenges/service/challenge_config_api.py"
REDIRECT_REL = "dev/challenges/website/nocturne-challenge-progress.html"
TRANSFORM_REL = "dev/challenges/website/legacy_challenge_retirement.py"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_DB_BACKUP_BYTES = 8 * 1024 * 1024 * 1024
OPERATOR_HTTP_MIN_INTERVAL = 0.65

LIVE_TARGETS = {
    "/srv/projects/nocturne-services/challenge_config_api.py": {
        "kind": "api", "before": "d4b4fab9c1e7eaa96e7915c926dabbc88468c6baa76bb865b0c364ef70b299cc",
        "uid": 1001, "gid": 33, "mode": "0664", "nlink": 1,
    },
    "/srv/projects/website/nocturne-challenge-progress.html": {
        "kind": "redirect", "before": "f508a11f047873ed3f3ff6e6e277c11d439c686cd5517a6e1a4cf4b8bc03e794",
        "uid": 1001, "gid": 33, "mode": "0664", "nlink": 1,
    },
    "/srv/projects/website/index.html": {
        "kind": "links", "before": "241965c28d2d09edb6a4bc94749f65a196af43c73ef05e7531159b5d303aca65",
        "uid": 1001, "gid": 33, "mode": "0664", "nlink": 1,
    },
    "/srv/projects/website/nocturne-challenges.html": {
        "kind": "links", "before": "b479666eabeccf94b5bd439d0e41755b65810108680be1e4605dbfd5dca566bd",
        "uid": 1001, "gid": 33, "mode": "0664", "nlink": 1,
    },
    "/srv/projects/website/nocturne-challenge-requirements.html": {
        "kind": "links", "before": "2ae1e38d4524810ab749e527d86980a9374bb3ea5b542e078a8d836a3b7a6553",
        "uid": 1001, "gid": 33, "mode": "0664", "nlink": 1,
    },
    "/srv/projects/website/nocturne-challenge-leaderboards.html": {
        "kind": "links", "before": "7dab75c55cf114572e0be8dcc5c92492d7d2341e623eced1ff0bfc7ea5579738",
        "uid": 1001, "gid": 33, "mode": "0664", "nlink": 1,
    },
    "/srv/projects/website/member-viewer.html": {
        "kind": "member", "before": "6c76b16bcefcff3746ab983cc1f4d1a5eaa96d854de3a8e66438534d38220646",
        "uid": 1001, "gid": 33, "mode": "0664", "nlink": 1,
    },
}

PARENT_PROFILES = {
    "/srv": (0, 0, "0755", {"user::": "rwx", "group::": "r-x", "other::": "r-x"}),
    "/srv/projects": (1000, 33, "2775", {
        "user::": "rwx", "user:1003:": "rwx", "group::": "r-x", "mask::": "rwx", "other::": "r-x",
        "default:user::": "rwx", "default:user:1003:": "rwx", "default:group::": "r-x",
        "default:mask::": "rwx", "default:other::": "r-x",
    }),
    "/srv/projects/nocturne-services": (1000, 33, "2775", {
        "user::": "rwx", "user:1003:": "rwx", "group::": "rwx", "mask::": "rwx", "other::": "r-x",
        "default:user::": "rwx", "default:user:1003:": "rwx", "default:group::": "rwx",
        "default:mask::": "rwx", "default:other::": "r-x",
    }),
    "/srv/projects/website": (1001, 33, "2775", {
        "user::": "rwx", "user:1003:": "rwx", "group::": "rwx", "mask::": "rwx", "other::": "r-x",
        "default:user::": "rwx", "default:user:1003:": "rwx", "default:group::": "rwx",
        "default:mask::": "rwx", "default:other::": "r-x",
    }),
}


class RetirementError(RuntimeError):
    def __init__(self, category: str, *, phase: str | None = None):
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,63}", category):
            raise ValueError("invalid diagnostic category")
        if phase is not None and not re.fullmatch(r"[a-z][a-z0-9_]{1,47}", phase):
            raise ValueError("invalid diagnostic phase")
        self.category = category
        self.phase = phase
        super().__init__(f"diagnostic_category={category}")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _reject_duplicate_json_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _load_module(path: Path, name: str, *, source: bytes | None = None):
    try:
        if source is None:
            source, _ = _read_nofollow(path, 4 * 1024 * 1024)
        if len(source) > 4 * 1024 * 1024:
            raise ValueError("module size exceeds bound")
        code = compile(source, str(path), "exec", dont_inherit=True)
    except Exception as exc:
        raise RetirementError("release_module_invalid") from exc
    module = types.ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = ""
    sys.modules[name] = module
    try:
        exec(code, module.__dict__)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def _read_nofollow(path: Path, maximum: int = MAX_FILE_BYTES) -> tuple[bytes, dict[str, Any]]:
    try:
        named = path.lstat()
        if not stat.S_ISREG(named.st_mode) or stat.S_ISLNK(named.st_mode) or named.st_nlink != 1:
            raise RetirementError("target_metadata_unsafe")
        if named.st_size < 0 or named.st_size > maximum:
            raise RetirementError("target_size_unsupported")
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        try:
            opened = os.fstat(fd)
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1 or
                    (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)):
                raise RetirementError("target_identity_changed")
            chunks, size = [], 0
            while True:
                part = os.read(fd, min(65536, maximum + 1 - size))
                if not part:
                    break
                chunks.append(part)
                size += len(part)
                if size > maximum:
                    raise RetirementError("target_size_unsupported")
            after = path.lstat()
            final = os.fstat(fd)
            identity = lambda st: (st.st_dev, st.st_ino, st.st_uid, st.st_gid,
                                   stat.S_IMODE(st.st_mode), st.st_nlink, st.st_size, st.st_mtime_ns)
            if identity(named) != identity(after) or identity(opened) != identity(final) or identity(final) != identity(after):
                raise RetirementError("target_identity_changed")
            data = b"".join(chunks)
            return data, {"dev": named.st_dev, "ino": named.st_ino, "uid": named.st_uid,
                          "gid": named.st_gid, "mode": f"{stat.S_IMODE(named.st_mode):04o}",
                          "nlink": named.st_nlink, "size": named.st_size}
        finally:
            os.close(fd)
    except RetirementError:
        raise
    except OSError as exc:
        raise RetirementError("target_read_failed") from exc


def _parent_guard(path: Path, support) -> None:
    current = Path(path.anchor)
    chain = [Path("/srv"), Path("/srv/projects"), path.parent]
    for node in chain:
        try:
            st = node.lstat()
            if not stat.S_ISDIR(st.st_mode) or stat.S_ISLNK(st.st_mode) or os.path.ismount(node):
                raise RetirementError("target_parent_unsafe")
            expected = PARENT_PROFILES.get(str(node))
            if expected is None:
                raise RetirementError("target_parent_unrecognized")
            uid, gid, mode, acl = expected
            if (st.st_uid, st.st_gid, f"{stat.S_IMODE(st.st_mode):04o}") != (uid, gid, mode):
                raise RetirementError("target_parent_metadata_mismatch")
            actual_acl = support._acl_entries(support._acl_text(node))
            if actual_acl != acl:
                raise RetirementError("target_parent_acl_mismatch")
            current = node
        except RetirementError:
            raise
        except Exception as exc:
            raise RetirementError("target_parent_inspection_failed") from exc


def _file_profile(path: Path, meta: dict[str, Any], source_record: dict | None, support) -> None:
    expected = LIVE_TARGETS[str(path)]
    if (meta.get("uid"), meta.get("gid"), meta.get("mode"), meta.get("nlink")) != (
            expected["uid"], expected["gid"], expected["mode"], expected["nlink"]):
        raise RetirementError("target_metadata_mismatch")
    try:
        entries = support._acl_entries(meta["acl_text"])
    except Exception as exc:
        raise RetirementError("target_acl_malformed") from exc
    if expected["kind"] == "api":
        # The API's exact ACL digest is pinned by the adopted source manifest.
        if not source_record or meta.get("acl_sha256") != source_record.get("acl_sha256"):
            raise RetirementError("api_acl_baseline_mismatch")
        expected_entries = {"user::": "rw-", "user:1003:": "rwx", "group::": "rwx",
                            "mask::": "rw-", "other::": "r--"}
    else:
        expected_entries = {"user::": "rw-", "user:1003:": "rw-", "group::": "r--",
                            "mask::": "rw-", "other::": "r--"}
    if entries != expected_entries:
        raise RetirementError("target_acl_profile_mismatch")


def _read_release_source(release: Path, relative: str, release_manifest: dict,
                        source_manifest: dict, *, extension: bool = True) -> bytes:
    bundle = [r for r in source_manifest.get("bundle_files", [])
              if isinstance(r, dict) and r.get("path") == relative]
    if len(bundle) != 1 or bundle[0].get("type") != "regular":
        raise RetirementError("source_manifest_binding_invalid")
    if extension and bundle[0].get("source_relationship") != "repository_owned_extension":
        raise RetirementError("source_extension_not_declared")
    expected = bundle[0].get("sha256")
    if (not isinstance(expected, str) or release_manifest.get("files", {}).get(relative) != expected):
        raise RetirementError("release_source_hash_mismatch")
    try:
        data, _ = _read_nofollow(release / relative, MAX_FILE_BYTES)
    except RetirementError:
        raise
    except Exception as exc:
        raise RetirementError("release_source_unavailable") from exc
    if _sha(data) != expected or len(data) != bundle[0].get("size"):
        raise RetirementError("release_source_hash_mismatch")
    return data


def build_file_outputs(live_data: dict[str, bytes], api_source: bytes, redirect_source: bytes,
                       transforms, *, predecessor_hashes: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Pure strict planner for target bytes; accepts only pinned or idempotent content."""
    if set(live_data) != set(LIVE_TARGETS):
        raise RetirementError("target_set_mismatch")
    predecessors = predecessor_hashes or {path: row["before"] for path, row in LIVE_TARGETS.items()}
    outputs = []
    for path, spec in LIVE_TARGETS.items():
        before = live_data[path]
        before_hash = _sha(before)
        if spec["kind"] == "api":
            after = api_source
        elif spec["kind"] == "redirect":
            after = redirect_source
        else:
            try:
                text = before.decode("utf-8", "strict")
                text = transforms.rewrite_legacy_progress_links(text)
                if spec["kind"] == "member":
                    text = transforms.remove_legacy_member_summary_consumer(text)
                after = text.encode("utf-8")
            except Exception as exc:
                raise RetirementError("website_source_shape_unsupported") from exc
            if "/nocturne-challenge-progress.html" in text:
                raise RetirementError("website_legacy_link_remains")
            if spec["kind"] == "member" and "/api/nocturne-challenges/summary" in text:
                raise RetirementError("website_legacy_api_consumer_remains")
        after_hash = _sha(after)
        if before_hash not in {predecessors[path], after_hash}:
            raise RetirementError("target_hash_drift")
        outputs.append({"target": path, "before_sha256": before_hash,
                        "after_sha256": after_hash, "before_data": before,
                        "after_data": after, "changed": before_hash != after_hash,
                        "kind": spec["kind"]})
    return outputs


def _database_summary(path: Path) -> dict[str, Any]:
    try:
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=8)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchall()
            if integrity != [("ok",)]:
                raise RetirementError("database_integrity_failed")
            journal = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            locking = str(conn.execute("PRAGMA locking_mode").fetchone()[0]).lower()
            version = conn.execute(
                "SELECT config_version_id FROM challenge_config_versions WHERE status='active'"
            ).fetchall()
            if len(version) != 1:
                raise RetirementError("active_config_identity_invalid")
            return {"integrity": "ok", "journal_mode": journal,
                    "locking_mode": locking, "active_config_version_id": int(version[0][0])}
        finally:
            conn.close()
    except RetirementError:
        raise
    except sqlite3.OperationalError as exc:
        text = str(exc).lower()
        category = "database_busy" if "locked" in text or "busy" in text else "database_read_failed"
        raise RetirementError(category) from exc
    except Exception as exc:
        raise RetirementError("database_read_failed") from exc


def _read_systemd_unit_file_state(support, unit: str) -> str:
    proc = support._run(["/usr/bin/systemctl", "show", unit, "--property=UnitFileState", "--value"], timeout=10)
    if proc.returncode or proc.stdout.count("\n") != 1:
        raise RetirementError("unit_file_state_unavailable")
    state = proc.stdout[:-1]
    if state not in {"enabled", "enabled-runtime", "linked", "linked-runtime", "static", "indirect",
                     "disabled", "masked", "masked-runtime", "generated", "transient", "alias"}:
        raise RetirementError("unit_file_state_ambiguous")
    return state


def _capture_unit_state(systemd, support, unit: str, *, timer: bool = False) -> dict[str, Any]:
    state = systemd.show(unit)
    unit_file_state = _read_systemd_unit_file_state(support, unit)
    if state.get("LoadState") != "loaded" or state.get("Id") != unit:
        raise RetirementError("required_unit_not_loaded")
    active, sub, pid = state.get("ActiveState"), state.get("SubState"), state.get("MainPID")
    if timer:
        if (active, sub) not in {("active", "waiting"), ("inactive", "dead")}:
            raise RetirementError("timer_state_ambiguous")
        if pid not in {None, "0"}:
            raise RetirementError("timer_process_unexpected")
        if unit_file_state not in {"enabled", "enabled-runtime", "disabled"}:
            raise RetirementError("timer_enablement_unsupported")
    else:
        valid = ((active == "inactive" and sub == "dead" and pid == "0") or
                 (active == "active" and sub in {"running", "exited"} and pid is not None and pid.isdigit()))
        if not valid:
            raise RetirementError("service_state_ambiguous")
        if active == "active" and sub == "running" and int(pid) <= 0:
            raise RetirementError("service_pid_missing")
    return {"Id": unit, "LoadState": state["LoadState"], "ActiveState": active,
            "SubState": sub, "MainPID": pid, "UnitFileState": unit_file_state}


def _validate_rollback_units(prior: dict[str, Any]) -> None:
    allowed_file_states = {"enabled", "enabled-runtime", "linked", "linked-runtime", "static",
                           "indirect", "disabled", "masked", "masked-runtime", "generated",
                           "transient", "alias"}
    if not isinstance(prior, dict) or set(prior) != {SERVICE, SHEET_TIMER, SHEET_SERVICE}:
        raise RetirementError("rollback_unit_record_invalid")
    api = prior[SERVICE]
    if (not isinstance(api, dict) or api.get("Id") != SERVICE or api.get("LoadState") != "loaded" or
            api.get("ActiveState") != "active" or api.get("SubState") != "running" or
            not str(api.get("MainPID", "")).isdigit() or int(api["MainPID"]) <= 0 or
            api.get("UnitFileState") not in allowed_file_states):
        raise RetirementError("rollback_api_prestate_invalid")
    timer = prior[SHEET_TIMER]
    if (not isinstance(timer, dict) or timer.get("Id") != SHEET_TIMER or timer.get("LoadState") != "loaded" or
            (timer.get("ActiveState"), timer.get("SubState")) not in {("active", "waiting"), ("inactive", "dead")} or
            timer.get("MainPID") not in {None, "0"} or timer.get("UnitFileState") not in allowed_file_states):
        raise RetirementError("rollback_timer_prestate_invalid")
    sheet = prior[SHEET_SERVICE]
    if (not isinstance(sheet, dict) or sheet.get("Id") != SHEET_SERVICE or sheet.get("LoadState") != "loaded" or
            (sheet.get("ActiveState"), sheet.get("SubState"), sheet.get("MainPID")) !=
            ("inactive", "dead", "0") or sheet.get("UnitFileState") not in allowed_file_states):
        raise RetirementError("rollback_sheet_service_prestate_invalid")


def make_plan(*, commit: str, support, systemd, repo: Path = REPO,
              runtime: Path = RUNTIME) -> dict[str, Any]:
    if not FULL_SHA.fullmatch(commit):
        raise RetirementError("invalid_commit")
    release = runtime / "releases" / commit
    if runtime != RUNTIME or release != RUNTIME / "releases" / commit:
        raise RetirementError("runtime_binding_mismatch")
    support.verify_git(repo, commit)
    release_manifest, source_manifest = support._load_manifest(release, commit, runtime)
    if release_manifest.get("commit") != commit:
        raise RetirementError("release_commit_mismatch")
    api_source = _read_release_source(release, API_REL, release_manifest, source_manifest)
    redirect_source = _read_release_source(release, REDIRECT_REL, release_manifest, source_manifest)
    transform_source = _read_release_source(release, TRANSFORM_REL, release_manifest, source_manifest)
    transform_path = release / TRANSFORM_REL
    transforms = _load_module(transform_path, f"legacy_retirement_{commit[:12]}", source=transform_source)
    if not hasattr(transforms, "rewrite_legacy_progress_links") or not hasattr(
            transforms, "remove_legacy_member_summary_consumer"):
        raise RetirementError("transform_interface_mismatch")

    live_data, live_meta = {}, {}
    api_manifest_records = [r for r in source_manifest.get("live_sources", [])
                            if isinstance(r, dict) and r.get("path") == "/srv/projects/nocturne-services/challenge_config_api.py"]
    if len(api_manifest_records) != 1:
        raise RetirementError("api_predecessor_manifest_ambiguous")
    api_predecessor = api_manifest_records[0]
    if api_predecessor.get("sha256") != LIVE_TARGETS["/srv/projects/nocturne-services/challenge_config_api.py"]["before"]:
        raise RetirementError("api_predecessor_manifest_mismatch")

    for target, spec in LIVE_TARGETS.items():
        path = Path(target)
        _parent_guard(path, support)
        try:
            metadata = support.capture_file(path)
            data, _ = _read_nofollow(path)
        except RetirementError:
            raise
        except Exception as exc:
            raise RetirementError("target_capture_failed") from exc
        _file_profile(path, metadata, api_predecessor if spec["kind"] == "api" else None, support)
        if metadata.get("sha256") != _sha(data):
            raise RetirementError("target_changed_during_capture")
        live_data[target] = data
        live_meta[target] = metadata

    outputs = build_file_outputs(live_data, api_source, redirect_source, transforms)
    by_target = {row["target"]: row for row in outputs}
    for target, meta in live_meta.items():
        by_target[target]["before_metadata"] = meta
        by_target[target]["after_size"] = len(by_target[target]["after_data"])
    database = _database_summary(DATABASE)
    try:
        database_metadata = support._database_metadata(DATABASE)
    except Exception as exc:
        raise RetirementError("database_metadata_unsafe") from exc
    api_state = _capture_unit_state(systemd, support, SERVICE)
    timer_state = _capture_unit_state(systemd, support, SHEET_TIMER, timer=True)
    sheet_state = _capture_unit_state(systemd, support, SHEET_SERVICE)
    if api_state["ActiveState"] != "active" or api_state["SubState"] != "running":
        raise RetirementError("api_service_not_ready")
    # Verify service import wiring from the exact manifest-pinned unit and
    # registration source; only osrs-drops-api imports api:app.
    refs = source_manifest.get("external_references", [])
    by_label = {r.get("label"): r for r in refs if isinstance(r, dict)}
    for label in ("public_api_systemd_unit", "public_blueprint_registration"):
        if label not in by_label:
            raise RetirementError("api_consumer_reference_missing")
        ref = by_label[label].get("file", {})
        ref_path = Path(str(ref.get("path", "")))
        try:
            source_data, _ = _read_nofollow(ref_path, 4 * 1024 * 1024)
        except Exception as exc:
            raise RetirementError("api_consumer_reference_unavailable") from exc
        if _sha(source_data) != ref.get("sha256"):
            raise RetirementError("api_consumer_reference_drift")
        if label == "public_blueprint_registration" and (
                b"from challenge_config_api import bp as challenge_config_public_bp" not in source_data or
                b"app.register_blueprint(challenge_config_public_bp)" not in source_data):
            raise RetirementError("api_consumer_registration_mismatch")
    unit_properties = support._run(
        ["/usr/bin/systemctl", "show", SERVICE, "--property=ExecStart", "--property=WorkingDirectory"], timeout=10)
    if unit_properties.returncode or "api:app" not in unit_properties.stdout or "/srv/projects/nocturne-services" not in unit_properties.stdout:
        raise RetirementError("api_consumer_unit_mismatch")

    safe_files = [{"target": row["target"], "before_sha256": row["before_sha256"],
                   "after_sha256": row["after_sha256"], "changed": row["changed"]}
                  for row in outputs]
    return {"status": "already_current" if not any(row["changed"] for row in outputs) else "upgrade_required",
            "target_commit": commit, "release_manifest_sha256": support._sha_file(release / "RELEASE-MANIFEST.json"),
            "current_file_set": "pinned_predecessor_or_exact_target", "files": safe_files,
            "database": database, "services": {SERVICE: api_state, SHEET_TIMER: timer_state,
                                                  SHEET_SERVICE: sheet_state},
            "services_restarted": [SERVICE] if api_state["ActiveState"] == "active" else [],
            "nginx_changed": False, "database_migrated": False,
            "backup_capability": "root_private_file_backups_plus_consistent_read_only_sqlite_backup",
            "_items": outputs, "_api_predecessor": api_predecessor,
            "_transforms": transforms, "_database_metadata": database_metadata}


def _write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchown(fd, 0, 0)
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_record(path: Path, value: dict[str, Any]) -> None:
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    _write_private(temporary, data)
    os.replace(temporary, path)
    _fsync_dir(path.parent)


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _backup_files(items: list[dict[str, Any]], directory: Path, support) -> list[dict[str, Any]]:
    backups = []
    for index, item in enumerate(items):
        target = Path(item["target"])
        current_meta = support.capture_file(target)
        if current_meta != item["before_metadata"]:
            raise RetirementError("target_metadata_changed_before_backup")
        data, _ = _read_nofollow(target)
        if _sha(data) != item["before_sha256"]:
            raise RetirementError("target_changed_before_backup")
        name = f"file-{index:02d}.backup"
        backup = directory / name
        _write_private(backup, data)
        support._verify_private_file(backup, 0)
        support._require_basic_acl(backup)
        backups.append({"target": str(target), "backup_name": name,
                        "backup_sha256": _sha(data), "before_sha256": item["before_sha256"],
                        "after_sha256": item["after_sha256"],
                        "before_metadata": item["before_metadata"]})
    _fsync_dir(directory)
    return backups


def _systemctl(support, action: str, unit: str) -> None:
    proc = support._run(["/usr/bin/systemctl", action, unit], timeout=30)
    if proc.returncode:
        raise RetirementError(f"systemd_{action}_failed")


def _set_sheet_timer_enablement(target: str, support) -> None:
    """Set only the supported enabled/disabled timer states, failing closed."""
    if target not in {"enabled", "enabled-runtime", "disabled"}:
        raise RetirementError("sheet_timer_enablement_unsupported")
    current = _read_systemd_unit_file_state(support, SHEET_TIMER)
    if current == target:
        return
    if current not in {"enabled", "enabled-runtime", "disabled"}:
        raise RetirementError("sheet_timer_enablement_unsupported")
    if current != "disabled":
        argv = ["/usr/bin/systemctl", "disable"]
        if current == "enabled-runtime":
            argv.append("--runtime")
        argv.append(SHEET_TIMER)
        proc = support._run(argv, timeout=30)
        if proc.returncode:
            raise RetirementError("systemd_disable_failed")
        current = "disabled"
    if target != "disabled":
        argv = ["/usr/bin/systemctl", "enable"]
        if target == "enabled-runtime":
            argv.append("--runtime")
        argv.append(SHEET_TIMER)
        proc = support._run(argv, timeout=30)
        if proc.returncode:
            raise RetirementError("systemd_enable_failed")
    if _read_systemd_unit_file_state(support, SHEET_TIMER) != target:
        raise RetirementError("sheet_timer_enablement_restore_mismatch")


def _verify_sheet_timer_state(*, active: str, sub: str, enablement: str,
                              systemd, support) -> None:
    state = systemd.show(SHEET_TIMER)
    if (state.get("Id") != SHEET_TIMER or state.get("LoadState") != "loaded" or
            state.get("ActiveState") != active or state.get("SubState") != sub or
            state.get("MainPID") not in {None, "0"}):
        raise RetirementError("sheet_timer_state_restore_mismatch")
    if _read_systemd_unit_file_state(support, SHEET_TIMER) != enablement:
        raise RetirementError("sheet_timer_enablement_changed")


def _retire_sheet_timer(systemd, support) -> None:
    """Leave the obsolete importer timer disabled and inactive after success."""
    state = systemd.show(SHEET_TIMER)
    if state.get("ActiveState") == "active":
        _systemctl(support, "stop", SHEET_TIMER)
        systemd.wait_inactive(SHEET_TIMER, timeout=20)
    state = systemd.show(SHEET_TIMER)
    if (state.get("Id") != SHEET_TIMER or state.get("LoadState") != "loaded" or
            state.get("ActiveState") != "inactive" or state.get("SubState") != "dead" or
            state.get("MainPID") not in {None, "0"}):
        raise RetirementError("sheet_timer_stop_verification_failed")
    current_enablement = _read_systemd_unit_file_state(support, SHEET_TIMER)
    if current_enablement != "disabled":
        _set_sheet_timer_enablement("disabled", support)
    _verify_sheet_timer_state(active="inactive", sub="dead", enablement="disabled",
                              systemd=systemd, support=support)


def _restore_timer(prior: dict[str, Any], systemd, support) -> None:
    if prior.get("UnitFileState") not in {"enabled", "enabled-runtime", "disabled"}:
        raise RetirementError("sheet_timer_enablement_unsupported")
    _set_sheet_timer_enablement(prior["UnitFileState"], support)
    current = systemd.show(SHEET_TIMER)
    if prior["ActiveState"] == "active":
        if current.get("ActiveState") != "active":
            _systemctl(support, "start", SHEET_TIMER)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            state = systemd.show(SHEET_TIMER)
            if state.get("ActiveState") == "active" and state.get("SubState") == "waiting":
                break
            time.sleep(.2)
        else:
            raise RetirementError("sheet_timer_restore_failed")
    elif current.get("ActiveState") != "inactive" or current.get("SubState") != "dead":
        _systemctl(support, "stop", SHEET_TIMER)
        systemd.wait_inactive(SHEET_TIMER, timeout=20)
    _verify_sheet_timer_state(active=prior["ActiveState"], sub=prior["SubState"],
                              enablement=prior["UnitFileState"], systemd=systemd,
                              support=support)


def _restore_api(prior: dict[str, Any], systemd, support) -> None:
    current = systemd.show(SERVICE)
    if prior["ActiveState"] == "active":
        if current.get("ActiveState") == "failed":
            _systemctl(support, "reset-failed", SERVICE)
            current = systemd.show(SERVICE)
        if current.get("ActiveState") != "active":
            _systemctl(support, "start", SERVICE)
        systemd.wait_active(SERVICE, timeout=45)
    else:
        if current.get("ActiveState") != "inactive":
            if current.get("ActiveState") == "failed":
                _systemctl(support, "reset-failed", SERVICE)
            _systemctl(support, "stop", SERVICE)
            systemd.wait_inactive(SERVICE, timeout=20)
    final = systemd.show(SERVICE)
    if final.get("Id") != SERVICE or final.get("LoadState") != "loaded":
        raise RetirementError("api_service_restore_identity_mismatch")
    if prior["ActiveState"] == "active":
        if final.get("ActiveState") != "active" or final.get("SubState") != prior["SubState"]:
            raise RetirementError("api_service_restore_state_mismatch")
        if prior["SubState"] == "running" and (not str(final.get("MainPID", "")).isdigit()
                                                or int(final["MainPID"]) <= 0):
            raise RetirementError("api_service_restore_pid_missing")
    elif (final.get("ActiveState"), final.get("SubState"), final.get("MainPID")) != (
            "inactive", "dead", "0"):
        raise RetirementError("api_service_restore_state_mismatch")
    if _read_systemd_unit_file_state(support, SERVICE) != prior["UnitFileState"]:
        raise RetirementError("api_service_enablement_changed")


def _verify_sheet_service_drained(prior: dict[str, Any], systemd, support,
                                  *, timeout: float = 90) -> None:
    """Wait boundedly for a timer-triggered one-shot, without stopping it."""
    deadline = time.monotonic() + timeout
    while True:
        current = systemd.show(SHEET_SERVICE)
        if current.get("Id") != SHEET_SERVICE or current.get("LoadState") != "loaded":
            raise RetirementError("sheet_service_drain_failed")
        active, sub, pid = current.get("ActiveState"), current.get("SubState"), current.get("MainPID")
        if (active, sub, pid) == ("inactive", "dead", "0"):
            break
        active_oneshot = ((sub == "running" and isinstance(pid, str) and pid.isdigit() and int(pid) > 0) or
                          (sub == "exited" and pid in {None, "0"}))
        if active == "active" and active_oneshot:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RetirementError("sheet_service_drain_timeout")
            try:
                systemd.wait_job_idle(SHEET_SERVICE, timeout=min(remaining, 15))
            except Exception as exc:
                if time.monotonic() >= deadline:
                    raise RetirementError("sheet_service_drain_timeout") from exc
            after_wait = systemd.show(SHEET_SERVICE)
            if (after_wait.get("ActiveState"), after_wait.get("SubState"), after_wait.get("MainPID")) == (active, sub, pid):
                time.sleep(min(.2, max(0, deadline - time.monotonic())))
            continue
        if active in {"activating", "deactivating"} and time.monotonic() < deadline:
            time.sleep(min(.2, max(0, deadline - time.monotonic())))
            continue
        if time.monotonic() >= deadline:
            raise RetirementError("sheet_service_drain_timeout")
        raise RetirementError("sheet_service_drain_failed")
    if _read_systemd_unit_file_state(support, SHEET_SERVICE) != prior["UnitFileState"]:
        raise RetirementError("sheet_service_enablement_changed")


def _atomic_restore_files(backups: list[dict[str, Any]], directory: Path, support) -> None:
    for entry in reversed(backups):
        target = Path(entry["target"])
        current = support.capture_file(target)
        if current.get("sha256") not in {entry["before_sha256"], entry["after_sha256"]}:
            raise RetirementError("rollback_target_drift")
        expected = entry.get("before_metadata")
        if not isinstance(expected, dict) or any(
                current.get(key) != expected.get(key)
                for key in ("uid", "gid", "mode", "nlink", "acl_sha256")):
            raise RetirementError("rollback_target_metadata_drift")
        if current.get("sha256") == entry["before_sha256"]:
            continue
        backup = directory / entry["backup_name"]
        _verify_backup(backup, entry, support)
        data, _ = _read_nofollow(backup, MAX_FILE_BYTES)
        support._atomic_replace(target, data, entry["before_metadata"])


def _verify_backup(path: Path, entry: dict[str, Any], support) -> None:
    try:
        data, metadata = _read_nofollow(path, MAX_FILE_BYTES)
        support._verify_private_file(path, 0)
        support._require_basic_acl(path)
    except Exception as exc:
        raise RetirementError("file_backup_unsafe") from exc
    if _sha(data) != entry["backup_sha256"] or _sha(data) != entry["before_sha256"]:
        raise RetirementError("file_backup_digest_mismatch")
    if metadata["uid"] != 0 or metadata["mode"] != "0600" or metadata["nlink"] != 1:
        raise RetirementError("file_backup_metadata_unsafe")


def _verify_backup_tree(directory: Path, commit: str, support) -> None:
    expected = BACKUP_ROOT / commit
    if (not FULL_SHA.fullmatch(commit) or directory.parent != expected or
            directory.name in {"", ".", ".."}):
        raise RetirementError("rollback_record_path_invalid")
    for node in (Path("/var"), Path("/var/backups"), BACKUP_ROOT, expected, directory):
        try:
            named = node.lstat()
            private_node = node not in {Path("/var"), Path("/var/backups")}
            if (not stat.S_ISDIR(named.st_mode) or stat.S_ISLNK(named.st_mode) or
                    named.st_uid != 0 or named.st_gid != 0 or
                    (stat.S_IMODE(named.st_mode) != 0o700 if private_node else
                     bool(stat.S_IMODE(named.st_mode) & 0o022)) or named.st_nlink < 2 or
                    (private_node and os.path.ismount(node)) or node.resolve(strict=True) != node):
                raise RetirementError("rollback_backup_directory_unsafe")
            fd = os.open(node, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) |
                         getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
            try:
                opened = os.fstat(fd)
                current = node.lstat()
                if ((opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino) or
                        (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)):
                    raise RetirementError("rollback_backup_directory_changed")
            finally:
                os.close(fd)
            if private_node:
                support._require_basic_acl(node)
        except RetirementError:
            raise
        except Exception as exc:
            raise RetirementError("rollback_backup_directory_unsafe") from exc


def _public_get(url: str, expected: int, temp: Path, probe, state_path: Path) -> tuple[int, bytes, bytes]:
    body = temp / (uuid.uuid4().hex + ".body")
    headers = temp / (uuid.uuid4().hex + ".headers")
    command = ["/usr/bin/curl", "--disable", "--silent", "--show-error", "--noproxy", "*",
               "--max-time", "10", "--max-redirs", "0", "--output", str(body),
               "--dump-header", str(headers), "--write-out", "%{http_code}", url]
    try:
        status = probe.request(expected, command, state_path, interval=OPERATOR_HTTP_MIN_INTERVAL,
                              timeout=14, max_429_retries=3)
        body_data, _ = _read_nofollow(body, 2 * 1024 * 1024)
        header_data, _ = _read_nofollow(headers, 64 * 1024)
        return status, body_data, header_data
    except Exception as exc:
        raise RetirementError("public_route_probe_failed") from exc
    finally:
        for path in (body, headers):
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def _verify_public_routes(temp: Path, release: Path) -> dict[str, Any]:
    probe = _load_module(release / "dev/intake/operator_http_probe.py", "retirement_http_probe")
    state_path = temp / "http-probe-state.json"
    _write_private(state_path, b"")
    outcomes = {}
    page_url = "https://nocturne.events/nocturne-challenge-progress.html"
    for key, url, expected in (
        ("legacy_page", page_url, 200),
        ("legacy_page_rsn", page_url + "?rsn=Safe%20Fixture", 200),
        ("legacy_bosses", "https://nocturne.events/api/nocturne-challenges/bosses", 200),
        ("legacy_summary", "https://nocturne.events/api/nocturne-challenges/summary", 410),
        ("legacy_progress", "https://nocturne.events/api/nocturne-challenges/progress", 410),
        ("legacy_leaderboards", "https://nocturne.events/api/nocturne-challenges/leaderboards", 410),
        ("active_config", "https://nocturne.events/api/challenges/config/active", 200),
        ("leaderboard", "https://nocturne.events/api/challenges/leaderboard", 200),
    ):
        status, body, headers = _public_get(url, expected, temp, probe, state_path)
        if key in {"legacy_page", "legacy_page_rsn"}:
            text = body.decode("utf-8", "strict")
            if "new URLSearchParams(location.search)" not in text or "encodeURIComponent(rsn)" not in text or "/challenge-member.html?rsn=" not in text:
                raise RetirementError("legacy_page_redirect_invalid")
            if key == "legacy_page" and "'/challenges.html'" not in text:
                raise RetirementError("legacy_page_default_target_invalid")
        elif key in {"legacy_summary", "legacy_progress", "legacy_leaderboards"}:
            try:
                payload = json.loads(body)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise RetirementError("legacy_route_response_invalid") from exc
            header_text = headers.decode("latin-1", "replace").lower()
            if (type(payload) is not dict or payload.get("error") != "legacy_endpoint_retired"
                    or "no-store" not in header_text):
                raise RetirementError("legacy_route_not_retired")
        elif key == "legacy_bosses":
            try:
                payload = json.loads(body)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise RetirementError("legacy_boss_route_invalid") from exc
            if type(payload) is not dict or payload.get("ok") is not True or not isinstance(payload.get("bosses"), list):
                raise RetirementError("legacy_boss_route_invalid")
        else:
            try:
                payload = json.loads(body)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise RetirementError("current_challenge_route_invalid") from exc
            if type(payload) is not dict or payload.get("ok") is not True:
                raise RetirementError("current_challenge_route_invalid")
        outcomes[key] = status
    return outcomes


def _public_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in plan.items() if not key.startswith("_")}


def apply_plan(*, commit: str, plan: dict[str, Any], support, systemd,
               database: Path = DATABASE, backup_root: Path = BACKUP_ROOT,
               verify_live=_verify_public_routes) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise RetirementError("apply_requires_root")
    if plan.get("target_commit") != commit or not FULL_SHA.fullmatch(commit):
        raise RetirementError("apply_target_mismatch")
    lock_fd = support._deployment_lock(LOCK_PATH)
    directory = None
    prior = None
    backups: list[dict[str, Any]] = []
    record = None
    phase = "initialization"
    try:
        support._prepared_check(RUNTIME / "releases" / commit, commit, RUNTIME)
        fresh = make_plan(commit=commit, support=support, systemd=systemd)
        if fresh["status"] == "already_current":
            return {"status": "already_current", "target_commit": commit, "rewritten_files": 0}
        if [(x["target"], x["before_sha256"], x["after_sha256"]) for x in fresh["_items"]] != [
                (x["target"], x["before_sha256"], x["after_sha256"]) for x in plan["_items"]]:
            raise RetirementError("prestate_changed_since_plan")
        phase = "backup_creation"
        directory = support._create_backup_dir(backup_root, commit)
        backups = _backup_files(fresh["_items"], directory, support)
        prior = {name: _capture_unit_state(systemd, support, name, timer=name.endswith(".timer"))
                 for name in (SERVICE, SHEET_TIMER, SHEET_SERVICE)}
        if any(prior[unit] != fresh["services"][unit]
               for unit in (SERVICE, SHEET_TIMER, SHEET_SERVICE)):
            raise RetirementError("service_state_changed_since_plan")
        if prior[SERVICE]["ActiveState"] != "active" or prior[SERVICE]["SubState"] != "running":
            raise RetirementError("api_service_not_ready")
        record_path = directory / "transaction.json"
        record = {"schema_version": 1, "target_commit": commit, "state": "backups_verified",
                  "created_at_epoch": int(time.time()), "files": backups,
                  "prior_units": prior, "database_backup": None, "database_mutated": False,
                  "schema_migrated": False, "nginx_changed": False, "nginx_reloaded": False,
                  "services_restarted": [SERVICE]}
        _write_record(record_path, record)

        phase = "maintenance_stop"
        if prior[SHEET_TIMER]["ActiveState"] == "active":
            _systemctl(support, "stop", SHEET_TIMER)
            systemd.wait_inactive(SHEET_TIMER, timeout=20)
        # The one-shot is allowed to finish naturally; it is never killed or
        # re-run, avoiding a second CSV import. Its stable post-drain state is
        # inactive/dead. On successful retirement the timer remains stopped
        # and is disabled; rollback alone restores its captured prestate.
        sheet_state = systemd.show(SHEET_SERVICE)
        if sheet_state.get("ActiveState") == "active":
            systemd.wait_job_idle(SHEET_SERVICE, timeout=90)
        _verify_sheet_service_drained(prior[SHEET_SERVICE], systemd, support)
        # A running oneshot has no stable state to restore without executing
        # the retired importer a second time (which could rewrite CSV tables).
        # Drain it safely and require a later retry once the captured service
        # prestate is stable/inactive. A successful retirement never restores
        # or re-enables the obsolete timer.
        if prior[SHEET_SERVICE]["ActiveState"] != "inactive":
            raise RetirementError("sheet_service_transient_prestate")
        if prior[SERVICE]["ActiveState"] == "active":
            _systemctl(support, "stop", SERVICE)
            systemd.wait_inactive(SERVICE, timeout=30)

        phase = "database_backup"
        database_before = _database_summary(database)
        if database == DATABASE:
            try:
                support._database_metadata(database)
            except Exception as exc:
                raise RetirementError("database_metadata_unsafe") from exc
        db_record = support._backup_database(database, directory / "Challenges.db.snapshot", 0)
        del db_record
        backup_path = directory / "Challenges.db.snapshot"
        st = backup_path.lstat()
        if (not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode) or st.st_nlink != 1 or
                st.st_uid != 0 or st.st_gid != 0 or stat.S_IMODE(st.st_mode) != 0o600 or st.st_size > MAX_DB_BACKUP_BYTES):
            raise RetirementError("database_backup_metadata_unsafe")
        support._require_basic_acl(backup_path)
        backup_summary = _database_summary(backup_path)
        if (backup_summary["integrity"] != "ok" or
                backup_summary["locking_mode"] != "normal" or
                backup_summary["active_config_version_id"] != database_before["active_config_version_id"]):
            raise RetirementError("database_backup_consistency_failed")
        record["database_backup"] = {"name": backup_path.name, "sha256": support._sha_file(backup_path),
                                     "size": st.st_size, "source_profile": database_before,
                                     "profile": backup_summary}
        record["state"] = "backups_verified"
        _write_record(record_path, record)

        phase = "file_installation"
        for item in fresh["_items"]:
            target = Path(item["target"])
            current = support.capture_file(target)
            if current != item["before_metadata"]:
                raise RetirementError("target_changed_before_install")
            current_data, _ = _read_nofollow(target)
            if _sha(current_data) != item["before_sha256"]:
                raise RetirementError("target_changed_before_install")
            if item["changed"]:
                # A replacement can succeed before a later fsync raises.
                support._atomic_replace(target, item["after_data"], current)
                after = support.capture_file(target)
                if after["sha256"] != item["after_sha256"] or any(
                        after.get(key) != current.get(key) for key in ("uid", "gid", "mode", "nlink", "acl_sha256")):
                    raise RetirementError("installed_file_verification_failed")
        record["state"] = "files_installed"
        _write_record(record_path, record)

        phase = "service_restore"
        if prior[SERVICE]["ActiveState"] == "active":
            _systemctl(support, "start", SERVICE)
            systemd.wait_active(SERVICE, timeout=45)
        _restore_api(prior[SERVICE], systemd, support)
        phase = "live_verification"
        with tempfile.TemporaryDirectory(prefix="retirement-probe-", dir=directory) as temp_text:
            temp = Path(temp_text)
            os.chmod(temp, 0o700)
            verify_live(temp, RUNTIME / "releases" / commit)
        after_db = _database_summary(database)
        if after_db["integrity"] != "ok" or after_db["active_config_version_id"] != database_before["active_config_version_id"]:
            raise RetirementError("database_poststate_changed")
        phase = "timer_retirement"
        _retire_sheet_timer(systemd, support)
        _verify_sheet_service_drained(prior[SHEET_SERVICE], systemd, support)
        record["state"] = "applied"
        record["completed_at_epoch"] = int(time.time())
        record["database_after"] = after_db
        record["verified_routes"] = ["legacy_page", "legacy_page_rsn", "legacy_bosses",
                                     "legacy_summary", "legacy_progress", "legacy_leaderboards",
                                     "active_config", "leaderboard"]
        record["restoration_result"] = {
            "api": "active_running" if prior[SERVICE]["ActiveState"] == "active" else "inactive_dead",
            "sheet_timer": "disabled_inactive",
            "sheet_service": "inactive_dead",
        }
        _write_record(record_path, record)
        return {"status": "applied", "target_commit": commit, "rewritten_files": sum(bool(x["changed"]) for x in fresh["_items"]),
                "transaction_record": str(record_path), "sheet_timer_retired": True,
                "database_migrated": False, "nginx_changed": False}
    except Exception as exc:
        failure_phase = phase
        failure_category = exc.category if isinstance(exc, RetirementError) else "unexpected_failure"
        rollback_category = "none"
        rollback_ok = True
        files_outcome = "not_needed"
        api_outcome = "not_needed"
        timer_outcome = "not_needed"
        sheet_service_outcome = "not_needed"

        def rollback_step(category: str, operation) -> bool:
            nonlocal rollback_ok, rollback_category
            try:
                operation()
                return True
            except Exception as rollback_exc:
                rollback_ok = False
                if rollback_category == "none":
                    rollback_category = (rollback_exc.category if isinstance(rollback_exc, RetirementError)
                                         else category)
                return False

        can_restore_files = True
        if backups and directory is not None:
            def stop_api_for_restore():
                state = systemd.show(SERVICE)
                if state.get("ActiveState") == "active":
                    _systemctl(support, "stop", SERVICE)
                    systemd.wait_inactive(SERVICE, timeout=30)
                    final = systemd.show(SERVICE)
                    if (final.get("ActiveState"), final.get("SubState")) != ("inactive", "dead"):
                        raise RetirementError("api_stop_for_rollback_failed")

            can_restore_files = rollback_step("api_stop_for_rollback_failed", stop_api_for_restore)
            if can_restore_files:
                files_outcome = ("restored" if rollback_step(
                    "file_restore_failed", lambda: _atomic_restore_files(backups, directory, support))
                    else "failed")
            else:
                files_outcome = "not_attempted"

        if prior is not None:
            if files_outcome not in {"failed", "not_attempted"}:
                api_outcome = ("restored" if rollback_step(
                    "api_restore_failed", lambda: _restore_api(prior[SERVICE], systemd, support))
                    else "failed")
            else:
                api_outcome = "not_attempted"
            timer_restored = rollback_step("sheet_timer_restore_failed",
                                           lambda: _restore_timer(prior[SHEET_TIMER], systemd, support))
            timer_outcome = "restored" if timer_restored else "failed"
            if timer_restored:
                sheet_service_outcome = ("restored" if rollback_step(
                    "sheet_service_restore_failed",
                    lambda: _verify_sheet_service_drained(prior[SHEET_SERVICE], systemd, support))
                    else "failed")
            else:
                sheet_service_outcome = "not_attempted"

        if directory is not None:
            failed_record = {"schema_version": 1, "target_commit": commit,
                             "state": "rolled_back" if rollback_ok else "rollback_failed",
                             "failure_phase": failure_phase, "failure_category": failure_category,
                             "rollback_category": rollback_category,
                             "created_at_epoch": record["created_at_epoch"] if record else int(time.time()),
                             "completed_at_epoch": int(time.time()), "files": backups,
                             "prior_units": prior or {},
                             "database_backup": record.get("database_backup") if record else None,
                             "database_mutated": False, "schema_migrated": False,
                             "nginx_changed": False, "nginx_reloaded": False,
                             "services_restarted": [SERVICE] if prior and prior[SERVICE]["ActiveState"] == "active" else [],
                             "restoration_result": {
                                 "outcome": "complete" if rollback_ok else "failed",
                                 "files": files_outcome, "api": api_outcome,
                                 "sheet_timer": timer_outcome,
                                 "sheet_service": sheet_service_outcome}}
            try:
                _write_record(directory / "transaction.json", failed_record)
            except Exception:
                rollback_ok = False
        # The original fixed category remains internal as __cause__; the
        # operator receives bounded phase plus rollback outcome only.
        raise RetirementError("rollback_complete" if rollback_ok else "rollback_failed",
                              phase=phase) from exc
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def rollback_record(*, record_path: Path, commit: str, support, systemd) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise RetirementError("rollback_requires_root")
    if not FULL_SHA.fullmatch(commit):
        raise RetirementError("invalid_commit")
    directory = record_path.parent
    expected_parent = BACKUP_ROOT / commit
    if directory.parent != expected_parent or record_path.name != "transaction.json":
        raise RetirementError("rollback_record_path_invalid")
    lock_fd = support._deployment_lock(LOCK_PATH)
    try:
        _verify_backup_tree(directory, commit, support)
        support._verify_private_file(record_path, 0)
        support._require_basic_acl(record_path)
        raw, _ = _read_nofollow(record_path, 1024 * 1024)
        try:
            record = json.loads(raw, object_pairs_hook=_reject_duplicate_json_pairs)
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise RetirementError("rollback_record_malformed") from exc
        expected_record_keys = {"schema_version", "target_commit", "state", "created_at_epoch", "files",
                                "prior_units", "database_backup", "database_mutated", "nginx_changed",
                                "schema_migrated", "nginx_reloaded", "services_restarted", "completed_at_epoch",
                                "database_after", "verified_routes", "restoration_result"}
        if (type(record) is not dict or record.get("schema_version") != 1 or record.get("target_commit") != commit or
                record.get("state") != "applied" or record.get("database_mutated") is not False or
                record.get("schema_migrated") is not False or record.get("nginx_changed") is not False or
                record.get("nginx_reloaded") is not False or not isinstance(record.get("files"), list) or
                len(record["files"]) != len(LIVE_TARGETS) or set(record) != expected_record_keys):
            raise RetirementError("rollback_record_schema_invalid")
        if (type(record.get("created_at_epoch")) is not int or record["created_at_epoch"] <= 0 or
                type(record.get("completed_at_epoch")) is not int or
                record["completed_at_epoch"] < record["created_at_epoch"] or
                record.get("services_restarted") != [SERVICE] or
                record.get("verified_routes") != ["legacy_page", "legacy_page_rsn", "legacy_bosses",
                                                   "legacy_summary", "legacy_progress", "legacy_leaderboards",
                                                   "active_config", "leaderboard"]):
            raise RetirementError("rollback_record_schema_invalid")
        prior = record.get("prior_units")
        _validate_rollback_units(prior)
        expected_restoration = {
            "api": "active_running",
            "sheet_timer": "disabled_inactive",
            "sheet_service": "inactive_dead",
        }
        # Accept pre-correction applied records for recoverability, but every
        # newly written transaction must record the retired disabled state.
        legacy_restoration = {
            "api": "active_running",
            "sheet_timer": "active_waiting" if prior[SHEET_TIMER]["ActiveState"] == "active" else "inactive_dead",
            "sheet_service": "inactive_dead",
        }
        if record.get("restoration_result") not in (expected_restoration, legacy_restoration):
            raise RetirementError("rollback_restoration_record_invalid")
        expected_backup_names = {target: f"file-{index:02d}.backup"
                                 for index, target in enumerate(LIVE_TARGETS)}
        seen_targets = set()
        for entry in record["files"]:
            if not isinstance(entry, dict) or entry.get("target") not in LIVE_TARGETS:
                raise RetirementError("rollback_target_set_invalid")
            target = entry["target"]
            if target in seen_targets or entry.get("backup_name") != expected_backup_names[target]:
                raise RetirementError("rollback_target_set_invalid")
            seen_targets.add(target)
            if (not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("before_sha256"))) or
                    not re.fullmatch(r"[0-9a-f]{64}", str(entry.get("after_sha256"))) or
                    entry["before_sha256"] not in {LIVE_TARGETS[target]["before"], entry["after_sha256"]}):
                raise RetirementError("rollback_file_record_invalid")
        for entry in record["files"]:
            current = support.capture_file(Path(entry["target"]))
            if current.get("sha256") != entry.get("after_sha256"):
                raise RetirementError("rollback_target_drift")
            backup = directory / entry.get("backup_name", "")
            _verify_backup(backup, entry, support)
        db_record = record.get("database_backup")
        profile_keys = {"integrity", "journal_mode", "locking_mode", "active_config_version_id"}
        if (not isinstance(db_record, dict) or set(db_record) != {"name", "sha256", "size", "profile", "source_profile"}
                or db_record.get("name") != "Challenges.db.snapshot"
                or not isinstance(db_record.get("profile"), dict) or set(db_record["profile"]) != profile_keys
                or not isinstance(db_record.get("source_profile"), dict)
                or set(db_record["source_profile"]) != profile_keys):
            raise RetirementError("rollback_database_record_invalid")
        db_backup = directory / db_record["name"]
        support._verify_private_file(db_backup, 0)
        support._require_basic_acl(db_backup)
        db_stat = db_backup.lstat()
        if (not stat.S_ISREG(db_stat.st_mode) or stat.S_ISLNK(db_stat.st_mode) or
                db_stat.st_nlink != 1 or db_stat.st_uid != 0 or db_stat.st_gid != 0 or
                stat.S_IMODE(db_stat.st_mode) != 0o600 or db_stat.st_size != db_record.get("size") or
                support._sha_file(db_backup) != db_record.get("sha256")):
            raise RetirementError("rollback_database_backup_unsafe")
        backup_profile = _database_summary(db_backup)
        if backup_profile.get("integrity") != "ok" or backup_profile != db_record.get("profile"):
            raise RetirementError("rollback_database_backup_invalid")
        current_db = _database_summary(DATABASE)
        source_profile = db_record.get("source_profile")
        if (not isinstance(source_profile, dict) or current_db.get("integrity") != "ok" or
                current_db.get("active_config_version_id") != source_profile.get("active_config_version_id")):
            raise RetirementError("rollback_database_state_changed")
        # Recreate the source plan solely to validate the pinned prepared target
        # and committed transformation contract before touching live files.
        support._prepared_check(RUNTIME / "releases" / commit, commit, RUNTIME)
        current_plan = make_plan(commit=commit, support=support, systemd=systemd)
        expected_after = {item["target"]: item["after_sha256"] for item in current_plan["_items"]}
        if any(entry["after_sha256"] != expected_after.get(entry["target"]) for entry in record["files"]):
            raise RetirementError("rollback_release_hash_mismatch")
        current_timer = systemd.show(SHEET_TIMER)
        if current_timer.get("ActiveState") == "active":
            _systemctl(support, "stop", SHEET_TIMER)
            systemd.wait_inactive(SHEET_TIMER, timeout=20)
        current_sheet = systemd.show(SHEET_SERVICE)
        if current_sheet.get("ActiveState") == "active":
            systemd.wait_job_idle(SHEET_SERVICE, timeout=90)
        _verify_sheet_service_drained(prior[SHEET_SERVICE], systemd, support)
        api_state = systemd.show(SERVICE)
        if api_state.get("ActiveState") == "active":
            _systemctl(support, "stop", SERVICE)
            systemd.wait_inactive(SERVICE, timeout=30)
        _atomic_restore_files(record["files"], directory, support)
        _restore_api(prior[SERVICE], systemd, support)
        _restore_timer(prior[SHEET_TIMER], systemd, support)
        _verify_sheet_service_drained(prior[SHEET_SERVICE], systemd, support)
        record["state"] = "rolled_back"
        record["rollback_at_epoch"] = int(time.time())
        _write_record(record_path, record)
        return {"status": "rolled_back", "target_commit": commit, "transaction_record": str(record_path),
                "database_restored": False, "nginx_changed": False}
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _canonical_check_nonroot(release: Path, commit: str) -> None:
    checker = release / "dev/intake/prepare_immutable_runtime.sh"
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "GIT_OPTIONAL_LOCKS": "0",
           "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory", "GIT_CONFIG_VALUE_0": str(REPO)}
    command = (["/bin/bash", str(checker), "--check", commit] if os.geteuid() == 0 else
               ["/usr/bin/sudo", "/bin/bash", str(checker), "--check", commit])
    try:
        proc = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              timeout=90, check=False, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RetirementError("prepared_check_unavailable") from exc
    if proc.returncode != 0:
        raise RetirementError("prepared_check_failed")
    lines = proc.stdout.splitlines()
    if lines.count("status=prepared") != 1 or lines.count("check_mode=read_only") != 1:
        raise RetirementError("prepared_check_output_invalid")


def load_runtime(commit: str):
    release = RUNTIME / "releases" / commit
    # Readiness authenticates the exact published checkout/release before any
    # helper code from that release is imported into this process.
    _canonical_check_nonroot(release, commit)
    support_path = release / "dev/challenges/deploy_timing_metadata.py"
    support = _load_module(support_path, f"timing_support_{commit[:12]}")
    support.verify_git(REPO, commit)
    manifest, source_manifest = support._load_manifest(release, commit, RUNTIME)
    if manifest.get("commit") != commit:
        raise RetirementError("release_commit_mismatch")
    return support, manifest, source_manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true", help="read-only plan (default)")
    modes.add_argument("--apply", action="store_true", help="install pinned retirement; root required")
    modes.add_argument("--rollback-record", type=Path, help="root-only rollback of one recorded install")
    parser.add_argument("--commit", required=True, help="exact full immutable commit SHA")
    args = parser.parse_args(argv)
    phase = "initialization"
    try:
        if not FULL_SHA.fullmatch(args.commit):
            raise RetirementError("invalid_commit")
        if args.apply and os.geteuid() != 0:
            raise RetirementError("apply_requires_root")
        if args.rollback_record is not None and os.geteuid() != 0:
            raise RetirementError("rollback_requires_root")
        support, _, _ = load_runtime(args.commit)
        systemd = support.Systemd()
        if args.rollback_record is not None:
            result = rollback_record(record_path=args.rollback_record, commit=args.commit,
                                     support=support, systemd=systemd)
        else:
            phase = "read_only_preflight"
            plan = make_plan(commit=args.commit, support=support, systemd=systemd)
            result = apply_plan(commit=args.commit, plan=plan, support=support, systemd=systemd) if args.apply else _public_plan(plan)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except RetirementError as exc:
        category = exc.category
        if exc.phase is not None:
            phase = exc.phase
    except Exception:
        category = "unexpected_failure"
    print(json.dumps({"status": "blocked", "phase": phase, "diagnostic_category": category},
                     sort_keys=True, separators=(",", ":")))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
