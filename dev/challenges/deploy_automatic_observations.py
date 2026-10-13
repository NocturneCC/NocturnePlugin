#!/usr/bin/env python3
"""Guarded deployment for the RuneLite automatic Challenge observation path.

The default invocation is a read-only plan.  Apply and rollback are root-only,
require the exact clean published commit and its already prepared immutable
release, and reuse the Challenge timing deployment's audited filesystem,
SQLite/WAL, backup, and systemd primitives.  This tool never sends a valid
observation and never changes the active Challenge configuration.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

REPO = Path("/srv/projects/nocturne-plugin-intake")
RUNTIME = Path("/srv/nocturne-plugin")
DATABASE = Path("/srv/projects/database/Challenges.db")
SERVICE_ROOT = Path("/srv/projects/nocturne-services")
NGINX_INCLUDE = SERVICE_ROOT / "nginx/nocturne-challenge-intake.location.conf"
BACKUP_ROOT = Path("/var/backups/challenge-automatic-observations")
LOCK = Path("/run/nocturne-challenge-timing-deploy.lock")
MANIFEST_REL = "dev/challenges/source-manifest.json"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
MAX_OBSERVATION_BODY = 8192
SCHEMA_TABLES = ("challenge_automatic_observations", "challenge_automatic_observation_participants")
TRACKING_TABLES = ("plugin_tracking_observations", "plugin_tracking_observation_participants",
                   "plugin_tracking_completion_observations",
                   "plugin_tracking_completion_participants")
LEGACY_AUTOMATIC_INTAKE_PREDECESSORS = (
    ("9008d57932b8addea6d17421aa93c7265b9d41603b1d7154cc5e8efd1c53db03", 27618),
    # Installed by the committed 3cf99e7 automatic-observation deployment.
    ("fe1d1b567eba44996bd2f6e75c9479961430052d3a8270271b5a408626577580", 29232),
)
# Exact source installed by the immediately preceding published automatic-
# observation release. Keep the older original predecessor explicitly
# supported for hosts that have not yet installed that release.
PREVIOUS_AUTOMATIC_INTAKE_SHA256 = "52c6d06f732f64759e148119e3e93c53f4139a97a2d27242dcd8352cd643ede3"
PREVIOUS_AUTOMATIC_INTAKE_SIZE = 28178
# Exact target bytes installed by the failed-forward 384f7a2 transaction. For
# targets unchanged by this release, this is the stable baseline; changed
# targets use it as an explicitly authenticated predecessor. Metadata remains
# sourced from the existing target profile (or the API profile for new files).
PREVIOUS_DEPLOYMENT_TARGETS = {
    "dev/challenges/service/challenge_automatic_intake.py":
        ("e88a8bce7e9e120cb6ebfbe6a0ca8f520b1dad58d46b5041462534d318e80db4", 29969),
    "dev/challenges/service/challenge_intake_api.py":
        ("62af22ce71c37b9479a5962dcc1b6293d01563fbdab0de8aa33732a25d1156fd", 31864),
    "dev/challenges/service/plugin_tracking_catalog.py":
        ("cc617af85ad0537cb368464b81716b68c19cb7087b134fad011abc0fc549d13b", 26459),
    "dev/challenges/service/leaderboard_challenge_ingest.py":
        ("a827b62a7d4df316246a5a4ccbd4ba3d2cdada63002c480c8834d02ed78ac53c", 24209),
    "dev/challenges/integration/routes/nocturne-challenge-intake.location.conf":
        ("0c90be8dcbee08964f1eaf2cbcfcb90a4fea08e55c68974f7b64f4de8b669154", 2583),
}

# This is the complete install set for the feature.  The first file is a
# repository-owned source may be absent on the original predecessor or match
# the exact preceding implementation. Other predecessor hashes are bound by
# source-manifest.json and checked against the captured host-root metadata.
TARGETS = (
    ("dev/challenges/service/challenge_automatic_intake.py",
     SERVICE_ROOT / "challenge_automatic_intake.py", "absent_or_previous"),
    ("dev/challenges/service/challenge_intake_api.py",
     SERVICE_ROOT / "challenge_intake_api.py", "manifest"),
    ("dev/challenges/service/plugin_tracking_catalog.py",
     SERVICE_ROOT / "plugin_tracking_catalog.py", "absent"),
    ("dev/challenges/service/leaderboard_challenge_ingest.py",
     SERVICE_ROOT / "leaderboard_challenge_ingest.py", "manifest"),
    ("dev/challenges/integration/routes/nocturne-challenge-intake.location.conf",
     NGINX_INCLUDE, "manifest"),
)

# Maintenance set copied from the established guarded Challenge timing
# installer.  Only the intake imports changed Python.  Other listed services
# are stopped solely to obtain SQLite quiescence and are restored to their
# captured pre-state; the leaderboard renderer consumes the new ingest module
# on its next normal timer invocation and is never force-run here.
LONG_SERVICES = (
    "osrs-drops-api.service",
    "nocturne-challenge-intake.service",
    "osrs-drops-admin.service",
)
WRITER_SERVICES = (
    "nocturne-challenge-shadow-sync.service",
    "nocturne-leaderboard-shadow-renderer.service",
)
TIMERS = (
    "nocturne-challenge-shadow-sync.timer",
    "nocturne-leaderboard-shadow-renderer.timer",
)
CONTROLLED = (*LONG_SERVICES, *WRITER_SERVICES, *TIMERS)
REQUIRED_ACTIVE = ("osrs-drops-api.service", "nocturne-challenge-intake.service")
RESTART_FOR_IMPORTS = ("nocturne-challenge-intake.service",)
PUBLIC_PROBES = (
    ("https://nocturne.events/api/challenges/config/active", "config"),
    ("https://nocturne.events/api/plugin/tracking/config", "plugin_tracking_config"),
    ("https://nocturne.events/api/challenges/leaderboard", "leaderboard"),
)
_NEW_FILE_META_KEYS = ("uid", "gid", "mode", "acl_text", "acl_sha256", "nlink")
_COMPLETE_CAPTURE_META_KEYS = ("sha256", "uid", "gid", "mode", "nlink", "size", "acl_sha256", "acl_text")
_ACL_TEXT_MAX_BYTES = 64 * 1024
_PHASE_DIAGNOSTIC_CATEGORIES = {
    "maintenance_stop": "maintenance_stop_failure",
    "sidecar_quiescence": "sidecar_quiescence_failure",
    "database_revalidation": "database_revalidation_failure",
    "backup_creation": "backup_creation_failure",
    "file_installation": "file_installation_failure",
    "schema_migration": "schema_migration_failure",
    "runtime_import_check": "runtime_import_check_failure",
    "service_restoration": "service_restoration_failure",
    "nginx_validation": "nginx_validation_failure",
    "live_verification": "live_verification_failure",
}


class DeploymentError(RuntimeError):
    """Bounded deployment failure; caller-facing messages contain no payloads."""


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_support(release: Path, runtime_root: Path = RUNTIME):
    """Load the already-published guarded deployment primitives from the release."""
    helper = release / "dev/challenges/deploy_timing_metadata.py"
    if release != runtime_root / "releases" / release.name or not FULL_SHA.fullmatch(release.name):
        raise DeploymentError("release identity rejected")
    spec = importlib.util.spec_from_file_location("nocturne_timing_deploy_support", helper)
    if spec is None or spec.loader is None:
        raise DeploymentError("deployment support unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _read_manifest(release: Path, commit: str, support, *, require_prepared: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
    try:
        release_manifest, source_manifest = support._load_manifest(release, commit, RUNTIME)
        if require_prepared:
            support._prepared_check(release, commit, RUNTIME)
    except Exception as exc:
        raise DeploymentError("prepared release rejected") from exc
    if release_manifest.get("commit") != commit:
        raise DeploymentError("release commit mismatch")
    return release_manifest, source_manifest


def _manifest_target_items(release: Path, commit: str, release_manifest: dict[str, Any],
                           source_manifest: dict[str, Any], support) -> list[dict[str, Any]]:
    bundles, live = support._bundle_records(source_manifest)
    result: list[dict[str, Any]] = []
    for relative, target, predecessor_kind in TARGETS:
        bundle = bundles.get(relative)
        if (not isinstance(bundle, dict) or bundle.get("type") != "regular" or
                bundle.get("sha256") != release_manifest["files"].get(relative)):
            raise DeploymentError("target source is not bound by both immutable manifests")
        if bundle.get("source_relationship") != "repository_owned_extension":
            raise DeploymentError("target source is not declared as a reviewed repository extension")
        if target == SERVICE_ROOT / "challenge_automatic_intake.py":
            # Its prior version is pinned here; target metadata is constrained
            # by the authenticated intake API predecessor below.
            if relative in live or any(item.get("path") == str(target) for item in source_manifest.get("live_sources", [])):
                raise DeploymentError("automatic intake target acquired an unreviewed live baseline")
            if predecessor_kind != "absent_or_previous":
                raise DeploymentError("automatic intake predecessor contract rejected")
            baseline = None
        elif target == SERVICE_ROOT / "plugin_tracking_catalog.py":
            if relative in live or any(item.get("path") == str(target)
                                       for item in source_manifest.get("live_sources", [])):
                raise DeploymentError("Plugin Tracking module acquired an unreviewed live baseline")
            if predecessor_kind != "absent":
                raise DeploymentError("Plugin Tracking module predecessor contract rejected")
            baseline = None
        else:
            baseline = live.get(str(target))
            if (not isinstance(baseline, dict) or baseline.get("type") != "regular" or
                    predecessor_kind != "manifest"):
                raise DeploymentError("supported live predecessor is missing from source manifest")
        source = release / relative
        after_digest = str(bundle["sha256"])
        if support._sha_file(source) != after_digest:
            raise DeploymentError("immutable target source digest mismatch")
        if target == NGINX_INCLUDE:
            _validate_nginx_fragment(source, support)
        result.append({"source_relative": relative, "source": source, "target": target,
                       "after_sha256": after_digest, "after_size": int(bundle["size"]),
                       "baseline": baseline, "predecessor_kind": predecessor_kind,
                       "allow_absent": target == SERVICE_ROOT / "plugin_tracking_catalog.py"})
    api_item = next((item for item in result
                     if item["target"] == SERVICE_ROOT / "challenge_intake_api.py"), None)
    api_baseline = api_item.get("baseline") if api_item else None
    if not isinstance(api_baseline, dict):
        raise DeploymentError("API metadata profile is unavailable for automatic intake")
    for item in result:
        previous = PREVIOUS_DEPLOYMENT_TARGETS.get(item["source_relative"])
        if previous is None:
            raise DeploymentError("previous deployment target record is incomplete")
        original = item["baseline"]
        profile = original if isinstance(original, dict) else api_baseline
        metadata_profile = {key: profile[key]
                            for key in ("uid", "gid", "mode", "nlink", "acl_sha256")}
        previous_record = {**metadata_profile, "sha256": previous[0], "size": previous[1],
                           "type": "regular"}
        supported = [previous_record]
        if isinstance(original, dict) and any(
                original.get(key) != previous_record.get(key)
                for key in ("sha256", "size", "uid", "gid", "mode", "nlink", "acl_sha256", "type")):
            supported.append(dict(original))
        if item["source_relative"] == "dev/challenges/service/challenge_automatic_intake.py":
            supported.extend(
                {**metadata_profile, "sha256": digest, "size": size, "type": "regular"}
                for digest, size in (*LEGACY_AUTOMATIC_INTAKE_PREDECESSORS,
                                     (PREVIOUS_AUTOMATIC_INTAKE_SHA256,
                                      PREVIOUS_AUTOMATIC_INTAKE_SIZE)))
            item["allow_absent"] = True
        item["baseline"] = previous_record
        item["supported_predecessors"] = supported
    return result


def _validate_nginx_fragment(path: Path, support) -> None:
    """Prove the immutable fragment preserves approved intake and adds one exact route."""
    raw = support._read_regular(path, 128 * 1024)
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise DeploymentError("nginx include encoding rejected") from exc
    observation_locations = re.findall(r"(?m)^\s*location\s*=\s*/api/challenges/intake/observations\s*\{", text)
    tracking_locations = re.findall(r"(?m)^\s*location\s*=\s*/api/plugin/tracking/config\s*\{", text)
    tracking_observation_locations = re.findall(r"(?m)^\s*location\s*=\s*/api/plugin/tracking/observations\s*\{", text)
    approved_locations = re.findall(r"(?m)^\s*location\s*=\s*/api/challenges/intake/approved\s*\{", text)
    proxy_targets = re.findall(r"(?m)^\s*proxy_pass\s+http://127\.0\.0\.1:5011/api/challenges/intake/observations;\s*$", text)
    tracking_proxy = re.findall(r"(?m)^\s*proxy_pass\s+http://127\.0\.0\.1:5011/api/plugin/tracking/config;\s*$", text)
    tracking_observation_proxy = re.findall(r"(?m)^\s*proxy_pass\s+http://127\.0\.0\.1:5011/api/plugin/tracking/observations;\s*$", text)
    if (len(observation_locations) != 1 or len(approved_locations) != 1 or len(proxy_targets) != 1
            or len(tracking_locations) != 1 or len(tracking_proxy) != 1
            or len(tracking_observation_locations) != 1 or len(tracking_observation_proxy) != 1):
        raise DeploymentError("nginx route fragment contract rejected")


def _capture_target(path: Path, support) -> dict[str, Any] | None:
    try:
        return support.capture_file(path)
    except FileNotFoundError:
        return None


def _expected_live_matches(actual: dict[str, Any], record: dict[str, Any]) -> bool:
    return all(actual.get(key) == record.get(key)
               for key in ("sha256", "uid", "gid", "mode", "nlink", "size", "acl_sha256"))


def _supported_automatic_predecessor(actual: dict[str, Any], item: dict[str, Any]) -> dict[str, Any] | None:
    """Return the exact pinned installed predecessor matching this target."""
    records = item.get("supported_predecessors")
    if not isinstance(records, list):
        # Compatibility for direct fixture callers using the former single
        # predecessor representation; production plans always use the list.
        records = [item.get("supported_previous") or item.get("baseline")]
    return next((record for record in records
                 if isinstance(record, dict) and _expected_live_matches(actual, record)), None)


def _supported_target_predecessor(actual: dict[str, Any], item: dict[str, Any]) -> dict[str, Any] | None:
    records = item.get("supported_predecessors")
    if not isinstance(records, list):
        return None
    return next((record for record in records
                 if isinstance(record, dict) and _expected_live_matches(actual, record)), None)


def _target_predecessor_records(item: dict[str, Any]) -> list[dict[str, Any]]:
    records = item.get("supported_predecessors")
    if isinstance(records, list):
        return [record for record in records if isinstance(record, dict)]
    baseline = item.get("baseline")
    return [baseline] if isinstance(baseline, dict) else []


def _classify_files(items: list[dict[str, Any]], support, *, strict_owner: bool = True) -> tuple[str, list[dict[str, Any]]]:
    states: list[str] = []
    observed: list[dict[str, Any]] = []
    checked = (("uid", "gid", "mode", "nlink", "acl_sha256") if strict_owner else
               ("mode", "nlink", "acl_sha256"))
    api_profile = next((item["baseline"] for item in items
                        if item["target"] == SERVICE_ROOT / "challenge_intake_api.py"), None)
    for item in items:
        target = item["target"]
        support._safe_parent(target)
        actual = _capture_target(target, support)
        baseline = item["baseline"]
        if actual is None:
            absent_is_predecessor = (item.get("predecessor_kind") in {"absent", "absent_or_previous"}
                                     and item.get("allow_absent"))
            if not absent_is_predecessor:
                raise DeploymentError("unsupported live file content or metadata drift")
            states.append("new")
            observed.append({"source": item["source_relative"], "target": str(target),
                             "before_sha256": None, "after_sha256": item["after_sha256"],
                             "current_sha256": None})
            continue
        # Files created by this installer inherit the established API target's
        # owner/mode/ACL profile. Existing targets use their exact pinned
        # predecessor metadata. Type and link-count are also enforced by the
        # no-follow regular-file capture before these comparisons.
        profile = baseline if isinstance(baseline, dict) else api_profile
        if (not isinstance(profile, dict) or
                any(actual.get(key) != profile.get(key) for key in checked) or
                actual.get("nlink") != 1):
            raise DeploymentError("unsupported live file content or metadata drift")

        after = {"sha256": item["after_sha256"], "size": item["after_size"]}
        matches_after = all(actual.get(key) == value for key, value in after.items())
        predecessor_records = _target_predecessor_records(item)
        matches_predecessor = any(
            isinstance(record, dict) and
            all(actual.get(key) == record.get(key)
                for key in ("sha256", "size", *checked))
            for record in predecessor_records
        )
        before_equals_after = (isinstance(baseline, dict) and
                               baseline.get("sha256") == item["after_sha256"] and
                               baseline.get("size") == item["after_size"])
        if before_equals_after and matches_after and matches_predecessor:
            state = "stable"
        elif matches_after:
            state = "installed"
        elif matches_predecessor:
            state = "predecessor"
        else:
            raise DeploymentError("unsupported live file content or metadata drift")
        states.append(state)
        observed.append({"source": item["source_relative"], "target": str(target),
                         "before_sha256": (baseline.get("sha256") if isinstance(baseline, dict) else None),
                         "after_sha256": item["after_sha256"],
                         "current_sha256": actual["sha256"]})
    if not states:
        raise DeploymentError("deployment target set is empty")
    changed = [state for state in states if state != "stable"]
    if not changed:
        return "already_installed", observed
    if all(state in {"predecessor", "new"} for state in changed):
        return "upgrade_predecessor", observed
    if all(state == "installed" for state in changed):
        return "already_installed", observed
    raise DeploymentError("mixed target file set requires operator recovery")


def _expected_schema_signature(auto_module) -> tuple[tuple[str, str, str | None], ...]:
    conn = sqlite3.connect(":memory:")
    try:
        auto_module.migrate_schema(conn)
        rows = conn.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE name LIKE 'challenge_automatic_%' "
            "OR name LIKE 'plugin_tracking_%' "
            "OR name IN ('idx_plugin_tracking_observations_created',"
            "'idx_plugin_tracking_completion_created') ORDER BY type,name"
        ).fetchall()
        return tuple((str(kind), str(name), re.sub(r"\s+", " ", str(sql or "")).strip())
                     for kind, name, sql in rows)
    finally:
        conn.close()


def _schema_state(conn: sqlite3.Connection, auto_module) -> str:
    found = {str(row[0]) for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?)", SCHEMA_TABLES)}
    if not found:
        tracking_names = ("plugin_tracking_catalog_versions", "plugin_tracking_catalog_drafts", *TRACKING_TABLES)
        placeholders = ",".join("?" for _ in tracking_names)
        tracking_found = {str(row[0]) for row in conn.execute(
            f"SELECT name FROM sqlite_master WHERE type='table' AND name IN ({placeholders})",
            tracking_names)}
        return "unsupported" if tracking_found else "absent"
    if found != set(SCHEMA_TABLES):
        return "unsupported"
    rows = tuple((str(kind), str(name), re.sub(r"\s+", " ", str(sql or "")).strip())
                 for kind, name, sql in conn.execute(
                     "SELECT type,name,sql FROM sqlite_master WHERE name LIKE 'challenge_automatic_%' "
                     "OR name LIKE 'plugin_tracking_%' "
                     "OR name IN ('idx_plugin_tracking_observations_created',"
                     "'idx_plugin_tracking_completion_created') ORDER BY type,name"))
    expected = _expected_schema_signature(auto_module)
    if rows == expected:
        return "present"
    legacy_tracking = tuple(row for row in expected
                            if not row[1].startswith("plugin_tracking_completion_")
                            and row[1] != "idx_plugin_tracking_completion_created")
    if rows == legacy_tracking:
        return "legacy_tracking"
    tracking_names = ("plugin_tracking_catalog_versions", "plugin_tracking_catalog_drafts", *TRACKING_TABLES)
    placeholders = ",".join("?" for _ in tracking_names)
    catalog_names = {str(row[0]) for row in conn.execute(
        f"SELECT name FROM sqlite_master WHERE type='table' AND name IN ({placeholders})",
        tracking_names)}
    previous_automatic = tuple(row for row in expected
                               if not row[1].startswith("plugin_tracking_")
                               and row[1] not in {"idx_plugin_tracking_observations_created",
                                                  "idx_plugin_tracking_completion_created"})
    if not catalog_names and rows == previous_automatic:
        return "catalog_absent"
    return "unsupported"


def _database_snapshot(database: Path, config_module, auto_module, support, *, strict_metadata: bool = True) -> dict[str, Any]:
    metadata = support._database_metadata(database) if strict_metadata else None
    if strict_metadata:
        connection_context = support._readonly_database_snapshot(database)
    else:
        class DirectReadOnly:
            def __enter__(self):
                self.conn = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=8)
                return self.conn
            def __exit__(self, _kind, _value, _traceback):
                self.conn.close()
        connection_context = DirectReadOnly()
    with connection_context as conn:
        profile = support._inspect_sqlite_connection(conn)
        facts = {"integrity": profile["integrity"], "journal_mode": profile["journal_mode"],
                 "locking_mode": profile["locking_mode"]}
        if strict_metadata:
            support._validate_production_journal_profile(facts, metadata)
        elif facts["journal_mode"] == "delete":
            if any(Path(str(database) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
                raise DeploymentError("quiescent DELETE journal profile has sidecars")
        elif facts["journal_mode"] != "wal":
            raise DeploymentError("database journal mode is unsupported")
        if facts["integrity"] != "ok" or facts["locking_mode"] != "normal":
            raise DeploymentError("database integrity or locking profile is unsupported")
        conn.row_factory = sqlite3.Row
        active = conn.execute("SELECT config_version_id FROM challenge_config_versions WHERE status='active'").fetchall()
        if len(active) != 1:
            raise DeploymentError("active configuration identity is ambiguous")
        version_id = int(active[0][0])
        document = config_module.config_document(conn)
        if int(document.get("version_id", -1)) != version_id:
            raise DeploymentError("active configuration read is inconsistent")
        counts = support._table_counts(conn)
        schema_state = _schema_state(conn, auto_module)
        automatic_counts = {name: (int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
                                   if name in {str(row[0]) for row in conn.execute(
                                       "SELECT name FROM sqlite_master WHERE type='table'")} else 0)
                            for name in (*SCHEMA_TABLES, *TRACKING_TABLES)}
        return {**facts, "active_version_id": version_id,
                "active_config_sha256": _sha_bytes(json.dumps(document, sort_keys=True, separators=(",", ":")).encode()),
                "table_counts": counts, "schema_state": schema_state,
                "metadata_verified": strict_metadata,
                "observation_counts": automatic_counts, "database_metadata": metadata}


def _load_release_modules(release: Path):
    service = release / "dev/challenges/service"
    if not service.is_dir() or service.is_symlink():
        raise DeploymentError("immutable Challenge service tree missing")
    sys.path.insert(0, str(service))
    os.environ["CHALLENGE_INTAKE_SKIP_DEFAULT_APP"] = "1"
    try:
        import challenge_automatic_intake as automatic
        import challenge_config as config
        import plugin_tracking_catalog as tracking
        assert callable(tracking.migrate_schema)
    except Exception as exc:
        raise DeploymentError("immutable Challenge migration modules failed to load") from exc
    return config, automatic


def _unit_plan(systemd, support) -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    snapshot = support._unit_snapshot(systemd, CONTROLLED)
    nginx = systemd.show("nginx.service")
    if (nginx.get("LoadState") != "loaded" or nginx.get("ActiveState") != "active" or
            nginx.get("SubState") != "running" or not str(nginx.get("MainPID", "")).isdigit() or
            int(nginx.get("MainPID", "0")) <= 0):
        raise DeploymentError("nginx is not in the required active/running state")
    return snapshot, nginx


def _preflight_step(category: str, function, *args, **kwargs):
    """Attach a fixed, bounded category while retaining the exception cause."""
    try:
        return function(*args, **kwargs)
    except Exception as exc:
        raise DeploymentError(f"preflight failed diagnostic_category={category}") from exc


def make_plan(*, commit: str, repo: Path = REPO, runtime_root: Path = RUNTIME,
              database: Path = DATABASE, release: Path | None = None, support=None,
              systemd=None) -> dict[str, Any]:
    if not FULL_SHA.fullmatch(commit):
        raise DeploymentError("preflight failed diagnostic_category=commit_preflight_failed")
    if repo != REPO or runtime_root != RUNTIME or database != DATABASE:
        raise DeploymentError("preflight failed diagnostic_category=deployment_path_preflight_failed")
    release = release or runtime_root / "releases" / commit
    support = support or _load_support(release, runtime_root)
    _preflight_step("checkout_preflight_failed", support.verify_git, repo, commit)
    root_authoritative = os.geteuid() == 0
    release_manifest, source_manifest = _preflight_step(
        "immutable_release_preflight_failed", _read_manifest, release, commit, support,
        require_prepared=root_authoritative)
    items = _preflight_step("target_contract_preflight_failed", _manifest_target_items,
                            release, commit, release_manifest, source_manifest, support)
    file_state, source_files = _preflight_step(
        "file_set_classification_failed", _classify_files, items, support,
        strict_owner=root_authoritative)
    config_module, automatic_module = _preflight_step(
        "runtime_import_preflight_failed", _load_release_modules, release)
    database_state = _preflight_step(
        "database_preflight_failed", _database_snapshot, database, config_module,
        automatic_module, support, strict_metadata=root_authoritative)
    systemd = systemd or support.Systemd()
    units, nginx = _preflight_step("service_state_preflight_failed", _unit_plan,
                                   systemd, support)
    nginx_change = any(item["target"] == str(NGINX_INCLUDE) and
                       item.get("current_sha256") != item.get("after_sha256")
                       for item in source_files)
    release_manifest_sha256 = _preflight_step(
        "immutable_release_preflight_failed", support._sha_file,
        release / "RELEASE-MANIFEST.json")
    inconsistent = (file_state == "already_installed" and database_state["schema_state"] != "present") or \
                   database_state["schema_state"] == "unsupported"
    complete_plan = root_authoritative and database_state["metadata_verified"]
    return {
        "status": ("blocked_inconsistent_state" if inconsistent else
                   "read_only_plan_only" if not complete_plan else
                   "already_installed" if file_state == "already_installed" else "dry_run"),
        "target_commit": commit,
        "release_manifest_sha256": release_manifest_sha256,
        "current_file_set": "unsupported" if inconsistent else file_state,
        "canonical_prepared_check": "status=prepared" if root_authoritative else "deferred_root_required",
        "live_metadata_authoritative": complete_plan,
        "source_targets": source_files,
        "schema_state": database_state["schema_state"],
        "observation_rows": database_state["observation_counts"][SCHEMA_TABLES[0]],
        "participant_rows": database_state["observation_counts"][SCHEMA_TABLES[1]],
        "active_configuration_version": database_state["active_version_id"],
        "integrity": database_state["integrity"],
        "journal_mode": database_state["journal_mode"],
        "services_and_timers": {name: {key: value for key, value in state.items() if key != "Result"}
                                 for name, state in units.items()},
        "nginx": {key: value for key, value in nginx.items() if key != "Result"},
        "nginx_would_change": nginx_change,
        "services_restarted_for_imports": list(RESTART_FOR_IMPORTS),
        "maintenance_services_restarted_if_previously_active": list(LONG_SERVICES),
        "timers_paused_and_restored_if_active": list(TIMERS),
        "one_shot_services_drained_not_force_run": list(WRITER_SERVICES),
        "backup_capability": "transactional-sqlite-backup-and-exact-file-acl-backups",
        "rollback_capability": "automatic-before-observation-data; schema rollback refuses nonzero observation rows",
        "active_configuration_changed": False,
        "valid_observation_sent": False,
    }


class Transaction:
    def __init__(self, *, directory: Path, commit: str, items: list[dict[str, Any]],
                 prior_units: dict[str, dict[str, str]], prior_nginx: dict[str, str],
                 database_before: dict[str, Any], database_backup: Path):
        self.directory = directory
        self.commit = commit
        self.items = items
        self.prior_units = prior_units
        self.prior_nginx = prior_nginx
        self.database_before = database_before
        self.database_backup = database_backup
        self.file_backups: list[dict[str, Any]] = []
        self.schema_migrated = False
        self.nginx_reloaded = False
        self.restoration: dict[str, Any] = {"services": "pending", "timers": "pending", "nginx": "pending"}
        self.record = directory / "transaction.json"

    def write_record(self, state: str, support) -> None:
        data = {
            "schema": 1, "target_commit": self.commit, "state": state,
            "database_backup": str(self.database_backup),
            "database_backup_sha256": support._sha_file(self.database_backup) if self.database_backup.exists() else None,
            "database_before": {key: value for key, value in self.database_before.items()
                                if key in {"integrity", "journal_mode", "locking_mode", "active_version_id",
                                           "active_config_sha256", "table_counts", "schema_state",
                                           "observation_counts", "database_file_meta"}},
            "prior_units": self.prior_units, "prior_nginx": self.prior_nginx,
            "files": [{**{key: item.get(key) for key in ("source_relative", "before_sha256",
                       "after_sha256", "after_size", "backup_sha256", "before_metadata",
                       "was_absent", "installed")},
                       "target": str(item["target"]),
                       "backup_path": str(item["backup_path"]) if item.get("backup_path") else None}
                      for item in self.file_backups],
            "schema_migrated": self.schema_migrated,
            "nginx_reloaded": self.nginx_reloaded,
            "restoration": self.restoration,
        }
        temp = self.record.with_name(".transaction.tmp")
        support._write_private(temp, json.dumps(data, sort_keys=True, separators=(",", ":")).encode())
        support._verify_private_file(temp, 0)
        os.replace(temp, self.record)
        support._fsync_dir(self.directory)


def _create_file_backups(tx: Transaction, support) -> None:
    records = []
    for index, item in enumerate(tx.items):
        before = _capture_target(item["target"], support)
        baseline = item["baseline"]
        if item.get("allow_absent") and before is None:
            record = {**item, "before_sha256": None, "before_metadata": None,
                      "backup_path": None, "backup_sha256": None, "was_absent": True}
        elif before is None:
            if baseline is not None or not item.get("allow_absent"):
                raise DeploymentError("required source target is absent before backup")
            record = {**item, "before_sha256": None, "before_metadata": None,
                      "backup_path": None, "backup_sha256": None, "was_absent": True}
        else:
            predecessor_match = bool(_supported_target_predecessor(before, item))
            baseline_match = bool(before and isinstance(baseline, dict) and
                                  _expected_live_matches(before, baseline))
            if before is None or not (baseline_match or predecessor_match):
                raise DeploymentError("target changed immediately before backup")
            data = support._read_regular(item["target"], max(int(before["size"]) + 1, 1))
            if _sha_bytes(data) != before["sha256"]:
                raise DeploymentError("target changed while backing up")
            path = tx.directory / f"file-{index}.backup"
            support._write_private(path, data)
            support._verify_private_file(path, 0)
            record = {**item, "before_sha256": before["sha256"],
                      "before_metadata": before, "backup_path": str(path),
                      "backup_sha256": _sha_bytes(data), "was_absent": False}
        records.append(record)
    tx.file_backups = records
    support._fsync_dir(tx.directory)


def _expected_new_file_meta(file_backups: list[dict[str, Any]], support) -> dict[str, Any]:
    """Derive the new module's install profile from its captured API predecessor.

    The immutable manifest authenticates the predecessor's content and ACL
    digest.  The transaction capture additionally contains the exact ACL text
    needed by atomic replacement; it is deliberately not stored in the source
    manifest.
    """
    api_target = SERVICE_ROOT / "challenge_intake_api.py"
    matches = [entry for entry in file_backups
               if isinstance(entry, dict) and entry.get("target") == api_target]
    if len(matches) != 1:
        raise DeploymentError("captured API predecessor record is absent or ambiguous")
    entry = matches[0]
    baseline = entry.get("baseline")
    before = entry.get("before_metadata")
    captured_predecessor = (_supported_target_predecessor(before, entry)
                            if isinstance(before, dict) else None)
    if (captured_predecessor is None and isinstance(before, dict) and
            isinstance(baseline, dict) and _expected_live_matches(before, baseline)):
        captured_predecessor = baseline
    if (entry.get("was_absent") is not False or entry.get("predecessor_kind") != "manifest" or
            entry.get("source_relative") != "dev/challenges/service/challenge_intake_api.py" or
            not isinstance(baseline, dict) or baseline.get("type") != "regular" or
            not isinstance(before, dict) or not set(_COMPLETE_CAPTURE_META_KEYS).issubset(before) or
            entry.get("before_sha256") != before.get("sha256") or
            captured_predecessor is None):
        raise DeploymentError("captured API predecessor metadata is incomplete or inconsistent")
    uid, gid, mode, acl_text, acl_sha256, nlink = (before.get(key) for key in _NEW_FILE_META_KEYS)
    if (type(uid) is not int or uid < 0 or type(gid) is not int or gid < 0 or
            not isinstance(mode, str) or re.fullmatch(r"[0-7]{4}", mode) is None or
            type(nlink) is not int or nlink != 1 or
            not isinstance(acl_text, str) or not acl_text or "\x00" in acl_text or
            len(acl_text.encode("utf-8")) > _ACL_TEXT_MAX_BYTES or
            not isinstance(acl_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", acl_sha256) is None or
            acl_sha256 != baseline.get("acl_sha256")):
        raise DeploymentError("captured API predecessor metadata is unsafe")
    try:
        if support._acl_hash(acl_text) != acl_sha256:
            raise DeploymentError("captured API predecessor ACL fingerprint is inconsistent")
    except DeploymentError:
        raise
    except Exception as exc:
        raise DeploymentError("captured API predecessor ACL fingerprint is invalid") from exc
    return {key: before[key] for key in _NEW_FILE_META_KEYS}


def _install_files(tx: Transaction, items: list[dict[str, Any]], support) -> None:
    new_meta = _expected_new_file_meta(tx.file_backups, support)
    for item in tx.file_backups:
        source_data = support._read_regular(item["source"])
        if _sha_bytes(source_data) != item["after_sha256"]:
            raise DeploymentError("release source changed during apply")
        target = item["target"]
        fresh = _capture_target(target, support)
        if item["was_absent"]:
            if fresh is not None:
                raise DeploymentError("new target appeared during apply")
            meta = new_meta
        else:
            if fresh is None or not _expected_live_matches(fresh, item["before_metadata"]):
                raise DeploymentError("target changed immediately before install")
            meta = item["before_metadata"]
        item["installed"] = True
        support._atomic_replace(target, source_data, meta)
        installed = support.capture_file(target)
        if (installed["sha256"] != item["after_sha256"] or
                any(installed.get(key) != meta.get(key) for key in ("uid", "gid", "mode", "acl_sha256")) or
                installed["nlink"] != 1):
            raise DeploymentError("installed target verification failed")


def _atomic_database_migration(database: Path, automatic_module) -> None:
    conn = sqlite3.connect(database, timeout=20, isolation_level=None)
    try:
        conn.execute("PRAGMA busy_timeout=20000")
        automatic_module.migrate_schema(conn)
    except Exception as exc:
        if conn.in_transaction:
            conn.rollback()
        raise DeploymentError("automatic observation schema migration failed") from exc
    finally:
        conn.close()


def _assert_schema_and_zero(database: Path, auto_module, support) -> None:
    with support._readonly_database_snapshot(database) as conn:
        state = _schema_state(conn, auto_module)
        if state != "present":
            raise DeploymentError("automatic observation schema verification failed")
        for name in (*SCHEMA_TABLES, *TRACKING_TABLES):
            if conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] != 0:
                raise DeploymentError("observation data exists; destructive rollback is forbidden")
        from plugin_tracking_catalog import active_document
        active_document(conn)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def _http(url: str, *, method: str = "GET", body: bytes | None = None,
          content_type: str | None = None, timeout: float = 4.0) -> tuple[int, bytes]:
    headers = {"Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        try:
            return int(exc.code), exc.read(1024 * 1024 + 1)
        finally:
            exc.close()
    with response:
        data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise DeploymentError("verification response exceeded bounds")
        return int(response.status), data


def _json_response(status: int, data: bytes) -> dict[str, Any]:
    try:
        value = json.loads(data)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DeploymentError("verification response was malformed") from exc
    if not isinstance(value, dict):
        raise DeploymentError("verification response shape was malformed")
    return value


def _observation_counts(database: Path, support) -> dict[str, int]:
    with support._readonly_database_snapshot(database) as conn:
        names = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return {name: int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]) if name in names else 0
                for name in (*SCHEMA_TABLES, *TRACKING_TABLES)}


def verify_live(database: Path, systemd, nginx_master_pid: str, prior_db: dict[str, Any],
                support, *, request=_http) -> dict[str, Any]:
    # All public request bodies are invalid or authorization probes. No valid
    # observation or manual submission is ever sent during deployment checks.
    health_status, health_body = request("http://127.0.0.1:5011/health")
    health = _json_response(health_status, health_body)
    if health_status != 200 or health.get("ok") is not True:
        raise DeploymentError("Challenge intake health verification failed")
    approved_status, _ = request("http://127.0.0.1:5011/api/challenges/intake/approved",
                                 method="POST", body=b"{}", content_type="application/json")
    if approved_status != 401:
        raise DeploymentError("privileged approved-intake authorization gate failed")
    obs_url = "https://nocturne.events/api/challenges/intake/observations"
    malformed_status, malformed_body = request(obs_url, method="POST", body=b"{}", content_type="application/json")
    malformed = _json_response(malformed_status, malformed_body)
    if malformed_status != 422 or malformed.get("state") != "invalid":
        raise DeploymentError("malformed observation smoke response failed")
    if any(_observation_counts(database, support).values()):
        raise DeploymentError("malformed observation smoke created rows")
    type_status, _ = request(obs_url, method="POST", body=b"{}", content_type="text/plain")
    if type_status != 415:
        raise DeploymentError("wrong-content-type smoke response failed")
    if any(_observation_counts(database, support).values()):
        raise DeploymentError("content-type smoke created rows")
    oversized_status, _ = request(obs_url, method="POST", body=b" " * (MAX_OBSERVATION_BODY + 1),
                                  content_type="application/json")
    if oversized_status != 413:
        raise DeploymentError("oversized observation smoke response failed")
    if any(_observation_counts(database, support).values()):
        raise DeploymentError("oversized observation smoke created rows")
    tracking_url = "https://nocturne.events/api/plugin/tracking/observations"
    tracking_status, tracking_body = request(tracking_url, method="POST", body=b"{}",
                                              content_type="application/json")
    tracking_smoke = _json_response(tracking_status, tracking_body)
    if tracking_status != 422 or tracking_smoke.get("state") != "invalid":
        raise DeploymentError("malformed Plugin Tracking smoke response failed")
    if any(_observation_counts(database, support).values()):
        raise DeploymentError("malformed Plugin Tracking smoke created rows")
    public_results = {}
    for url, category in PUBLIC_PROBES:
        status, body = request(url)
        payload = _json_response(status, body)
        if status != 200 or payload.get("ok") is not True:
            raise DeploymentError(f"public {category} read verification failed")
        public_results[category] = "ok"
    nginx = systemd.show("nginx.service")
    if (nginx.get("LoadState") != "loaded" or nginx.get("ActiveState") != "active" or
            nginx.get("SubState") != "running" or nginx.get("MainPID") != nginx_master_pid):
        raise DeploymentError("nginx service identity changed during verification")
    after = _database_snapshot(database, prior_db["config_module"], prior_db["automatic_module"], support)
    if after["integrity"] != "ok" or after["active_version_id"] != prior_db["active_version_id"] or \
            after["active_config_sha256"] != prior_db["active_config_sha256"]:
        raise DeploymentError("database or active configuration changed during verification")
    before_counts = prior_db["table_counts"]
    allowed_auth_audit = "challenge_intake_request_audit"
    if any(after["table_counts"].get(name) != count + (1 if name == allowed_auth_audit else 0)
           for name, count in before_counts.items() if name not in SCHEMA_TABLES):
        raise DeploymentError("manual Challenge or leaderboard rows changed during verification")
    if (allowed_auth_audit not in before_counts or
            after["table_counts"].get(allowed_auth_audit) != before_counts[allowed_auth_audit] + 1):
        raise DeploymentError("authorization smoke audit was not recorded exactly once")
    if any(after["observation_counts"].values()):
        raise DeploymentError("observation rows appeared during verification")
    return {"intake_health": "ok", "approved_intake_auth": "protected",
            "observation_smokes": "invalid/no rows", "public_reads": public_results,
            "database_integrity": "ok", "observations": 0, "participants": 0,
            "plugin_tracking_observations": 0, "plugin_tracking_participants": 0,
            "plugin_tracking_completion_observations": 0,
            "plugin_tracking_completion_participants": 0,
            "valid_observation_sent": False}


def _restore_target_files(tx: Transaction, support) -> None:
    for item in reversed(tx.file_backups):
        current = _capture_target(item["target"], support)
        if item["was_absent"]:
            if current is None:
                continue
            profile = _expected_new_file_meta(tx.file_backups, support)
            if (current["sha256"] != item["after_sha256"] or current["nlink"] != 1 or
                    any(current.get(key) != profile.get(key)
                        for key in ("uid", "gid", "mode", "acl_sha256"))):
                raise DeploymentError("new target drift prevents rollback")
            support._safe_parent(item["target"])
            item["target"].unlink()
            support._fsync_dir(item["target"].parent)
            continue
        before = item["before_metadata"]
        if current and _expected_live_matches(current, before):
            continue
        if (current is None or current["sha256"] != item["after_sha256"] or current["nlink"] != 1 or
                current["uid"] != before["uid"] or current["gid"] != before["gid"] or
                current["mode"] != before["mode"] or current["acl_sha256"] != before["acl_sha256"]):
            raise DeploymentError("target drift prevents safe rollback")
        backup = Path(item["backup_path"])
        if support._sha_file(backup) != item["backup_sha256"]:
            raise DeploymentError("file backup digest mismatch")
        data = support._read_regular(backup, max(int(before["size"]) + 1, 1))
        support._atomic_replace(item["target"], data, before)


def _refuse_rollback_with_observations(tx: Transaction, systemd, support, release: Path) -> None:
    """Retain accepted data but always attempt captured service/timer restoration."""
    errors: list[str] = []
    if tx.nginx_reloaded:
        try:
            _validate_nginx(support)
            _reload_nginx(systemd, tx.prior_nginx, support, release)
        except Exception:
            errors.append("nginx_restore_failed")
    try:
        support._restore_units(systemd, tx.prior_units)
        _verify_restored_units(systemd, tx.prior_units, support)
    except Exception:
        errors.append("unit_restore_failed")
    restored = "restored-to-prestate" if "unit_restore_failed" not in errors else "restore_failed"
    tx.restoration = {
        "services": restored,
        "timers": restored,
        "nginx": "prestate-retained" if not tx.nginx_reloaded else
                 ("reloaded-to-prestate" if "nginx_restore_failed" not in errors else "restore_failed"),
    }
    tx.write_record("rollback_failed" if errors else "rollback_refused_observation_data", support)
    if errors:
        raise DeploymentError("rollback refused: observation data retained; restoration failed")
    raise DeploymentError("rollback refused: automatic observation data exists")


def _rollback(tx: Transaction, systemd, support, release: Path, auto_module,
              *, database: Path = DATABASE) -> None:
    # Never destroy automatically accepted data. If a concurrent valid client
    # request arrived after route exposure, leave the installed schema/code in
    # place and report that operator recovery is required.
    counts = _observation_counts(database, support)
    if any(counts.values()):
        _refuse_rollback_with_observations(tx, systemd, support, release)
    fresh_units = {}
    for unit in CONTROLLED:
        fresh = support._systemd_show(unit)
        if fresh["LoadState"] not in {"loaded", "not-found"} or fresh["ActiveState"] not in {"active", "inactive"}:
            raise DeploymentError("rollback found ambiguous unit state")
        fresh_units[unit] = fresh
    initial_sidecars = support._sidecar_state_snapshot(database)
    db_identity = support._stat_identity(support._db_lstat(database))
    support._stop_for_migration(systemd, fresh_units)
    support._verify_migration_maintenance(systemd, fresh_units)
    support._wait_for_no_sqlite_sidecars(database, systemd, fresh_units, initial_sidecars,
                                         main_identity=db_identity)
    # Close the race with an observation accepted between the first check and
    # the intake service stopping. Once the DB is quiescent, retain the new
    # schema and code rather than restoring a backup that could erase it.
    if any(_observation_counts(database, support).values()):
        _refuse_rollback_with_observations(tx, systemd, support, release)
    # Restore only if the database is still the guarded transaction image.
    if tx.schema_migrated:
        backup_hash = support._sha_file(tx.database_backup)
        if support._sha_file(database) != backup_hash:
            backup_meta = tx.database_before["database_file_meta"]
            support._copy_restore_database(tx.database_backup, database, backup_meta)
        restored = _database_snapshot(database, tx.database_before["config_module"], auto_module, support)
        if (restored["active_version_id"] != tx.database_before["active_version_id"] or
                restored["active_config_sha256"] != tx.database_before["active_config_sha256"] or
                restored["table_counts"] != tx.database_before["table_counts"] or
                restored["schema_state"] != tx.database_before["schema_state"]):
            raise DeploymentError("database rollback verification failed")
    _restore_target_files(tx, support)
    _validate_nginx(support)
    if tx.nginx_reloaded:
        _reload_nginx(systemd, tx.prior_nginx, support)
    support._restore_units(systemd, tx.prior_units)
    _verify_restored_units(systemd, tx.prior_units, support)
    tx.restoration = {"services": "restored-to-prestate", "timers": "restored-to-prestate",
                      "nginx": "validated-and-restored"}
    tx.write_record("rolled_back", support)


def _verify_restored_units(systemd, snapshot: dict[str, dict[str, str]], support) -> None:
    for unit, before in snapshot.items():
        after = systemd.show(unit)
        if after.get("LoadState") != before.get("LoadState"):
            raise DeploymentError("captured unit load state was not restored")
        if unit.endswith(".timer"):
            if before.get("ActiveState") == "active":
                valid = (after.get("ActiveState"), after.get("SubState"), after.get("MainPID", "0")) == ("active", "waiting", "0")
            else:
                valid = after.get("ActiveState") == "inactive" and after.get("SubState") == "dead" and after.get("MainPID", "0") == "0"
        elif unit in LONG_SERVICES:
            if before.get("ActiveState") == "active":
                valid = (after.get("ActiveState") == "active" and after.get("SubState") == "running" and
                         str(after.get("MainPID", "")).isdigit() and int(after.get("MainPID", "0")) > 0)
            else:
                valid = after.get("ActiveState") == "inactive" and after.get("SubState") == "dead" and after.get("MainPID") == "0"
        else:
            # A one-shot may complete while it is being drained. Never rerun a
            # leaderboard writer merely to recreate systemd's historical
            # active/exited presentation; require only a safe non-running job.
            valid = ((after.get("ActiveState"), after.get("SubState"), after.get("MainPID")) in {
                ("inactive", "dead", "0"), ("active", "exited", "0")})
        if not valid:
            raise DeploymentError("captured unit state was not safely restored")


def _validate_nginx(support) -> None:
    proc = support._run(["/usr/sbin/nginx", "-t"], timeout=30)
    if proc.returncode:
        raise DeploymentError("nginx configuration validation failed")


def _load_nginx_convergence(release: Path):
    helper = release / "dev/intake/nginx_reload_convergence.py"
    if release != RUNTIME / "releases" / release.name or not helper.is_file() or helper.is_symlink():
        raise DeploymentError("committed Nginx convergence helper unavailable")
    spec = importlib.util.spec_from_file_location("nocturne_nginx_reload_convergence", helper)
    if spec is None or spec.loader is None:
        raise DeploymentError("committed Nginx convergence helper unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _reload_nginx(systemd, prior_nginx: dict[str, str], support, release: Path) -> None:
    before = systemd.show("nginx.service")
    if before.get("MainPID") != prior_nginx.get("MainPID") or before.get("ActiveState") != "active":
        raise DeploymentError("nginx master identity changed before reload")
    convergence = _load_nginx_convergence(release)
    generation = convergence.capture_generation()
    if generation.get("master_pid") != int(prior_nginx.get("MainPID", "0")):
        raise DeploymentError("nginx worker generation belongs to a different master")
    proc = support._run(["/usr/bin/systemctl", "reload", "nginx.service"], timeout=30)
    if proc.returncode:
        raise DeploymentError("nginx reload failed")
    try:
        convergence.wait_for_reload(generation, attempts=40, delay=0.25)
    except Exception as exc:
        raise DeploymentError("nginx worker generation did not converge") from exc
    state = systemd.show("nginx.service")
    if (state.get("LoadState") != "loaded" or state.get("ActiveState") != "active" or
            state.get("SubState") != "running" or state.get("MainPID") != prior_nginx.get("MainPID")):
        raise DeploymentError("nginx master state changed after reload")


def apply(*, commit: str, plan: dict[str, Any], support, systemd,
          repo: Path = REPO, runtime_root: Path = RUNTIME, database: Path = DATABASE,
          backup_root: Path = BACKUP_ROOT, release: Path | None = None,
          failpoint=None) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise DeploymentError("apply requires root")
    if plan.get("target_commit") != commit:
        raise DeploymentError("apply plan target mismatch")
    if plan.get("status") == "already_installed":
        return {"status": "already_installed", "target_commit": commit,
                "active_configuration_changed": False, "valid_observation_sent": False}
    if plan.get("status") != "dry_run":
        raise DeploymentError("apply refused for inconsistent target/schema state")
    # Recheck the exact checkout/prepared artifact state under the shared lock.
    support.verify_git(repo, commit)
    release = release or runtime_root / "releases" / commit
    if release != runtime_root / "releases" / commit:
        raise DeploymentError("apply release path is not the exact target")
    release_manifest, source_manifest = _read_manifest(release, commit, support)
    items = _manifest_target_items(release, commit, release_manifest, source_manifest, support)
    current_plan = make_plan(commit=commit, repo=repo, runtime_root=runtime_root, database=database,
                             release=release, support=support, systemd=systemd)
    if current_plan != plan:
        raise DeploymentError("live state changed after dry-run plan")
    units, nginx_before = _unit_plan(systemd, support)
    config_module, automatic_module = _load_release_modules(release)
    db_before = _database_snapshot(database, config_module, automatic_module, support)
    db_before["config_module"] = config_module
    db_before["automatic_module"] = automatic_module
    db_before["database_file_meta"] = support.capture_file(database)
    db_before["sidecars"] = support._sidecar_state_snapshot(database)
    backup_dir = support._create_backup_dir(backup_root, commit, 0)
    db_backup = backup_dir / "Challenges.db.sqlite-backup"
    tx = Transaction(directory=backup_dir, commit=commit, items=items, prior_units=units,
                     prior_nginx=nginx_before, database_before=db_before, database_backup=db_backup)
    phase = "maintenance_stop"
    try:
        tx.write_record("prestate_captured", support)
        support._stop_for_migration(systemd, units)
        phase = "sidecar_quiescence"
        support._verify_migration_maintenance(systemd, units)
        support._wait_for_no_sqlite_sidecars(database, systemd, units, db_before["sidecars"],
                                              main_identity=support._stat_identity(support._db_lstat(database)))
        phase = "database_revalidation"
        quiesced = _database_snapshot(database, config_module, automatic_module, support)
        if any(quiesced[key] != db_before[key] for key in
               ("active_version_id", "active_config_sha256", "table_counts", "schema_state", "observation_counts")):
            raise DeploymentError("Challenge database changed during maintenance stop")
        if failpoint: failpoint("sidecars_quiesced")
        phase = "backup_creation"
        _create_file_backups(tx, support)
        support._backup_database(database, db_backup, 0)
        backup_facts = _database_snapshot(db_backup, config_module, automatic_module, support)
        if any(backup_facts[key] != db_before[key] for key in
               ("active_version_id", "active_config_sha256", "table_counts", "schema_state", "observation_counts")):
            raise DeploymentError("SQLite backup differs from pre-deployment state")
        tx.write_record("backed_up", support)
        if failpoint: failpoint("backup_complete")
        phase = "file_installation"
        _install_files(tx, items, support)
        tx.write_record("files_installed", support)
        if failpoint: failpoint("files_installed")
        phase = "schema_migration"
        tx.schema_migrated = True  # Pending is treated as possibly committed after a process crash.
        tx.write_record("schema_migration_pending", support)
        _atomic_database_migration(database, automatic_module)
        _assert_schema_and_zero(database, automatic_module, support)
        tx.write_record("schema_installed", support)
        if failpoint: failpoint("schema_installed")
        phase = "runtime_import_check"
        _verify_imports(database.parent if False else SERVICE_ROOT, support)
        phase = "service_restoration"
        support._restore_units(systemd, units)
        _verify_restored_units(systemd, units, support)
        tx.restoration["services"] = "restored-to-prestate"
        tx.restoration["timers"] = "restored-to-prestate"
        if failpoint: failpoint("services_restored")
        phase = "nginx_validation"
        nginx_changed = plan["nginx_would_change"]
        if nginx_changed:
            _validate_nginx(support)
            # Even a failing reload command can race with systemd's reload job.
            tx.nginx_reloaded = True
            tx.write_record("nginx_reload_pending", support)
            _reload_nginx(systemd, nginx_before, support, release)
            tx.restoration["nginx"] = "reloaded-after-config-change"
            tx.write_record("nginx_reloaded", support)
        phase = "live_verification"
        live = verify_live(database, systemd, nginx_before["MainPID"], db_before,
                           support, request=_http)
        _verify_restored_units(systemd, units, support)
        tx.restoration["services"] = "verified"
        tx.restoration["timers"] = "verified"
        tx.write_record("committed", support)
        return {"status": "installed", "target_commit": commit,
                "transaction_record": str(tx.record), "nginx_changed": nginx_changed,
                "services_restarted_for_imports": list(RESTART_FOR_IMPORTS),
                "post_install": live, "active_configuration_changed": False}
    except BaseException as exc:
        try:
            if tx.database_backup.exists() and tx.file_backups:
                _rollback(tx, systemd, support, release, automatic_module, database=database)
            else:
                support._restore_units(systemd, units)
                tx.restoration = {"services": "restored-to-prestate", "timers": "restored-to-prestate",
                                  "nginx": "unchanged"}
                if tx.directory.exists():
                    tx.write_record("failed_before_mutation", support)
        except DeploymentError as rollback_exc:
            if ("rollback refused" in str(rollback_exc) and
                    "restoration failed" not in str(rollback_exc)):
                raise rollback_exc from exc
            raise DeploymentError(f"deployment failed phase={phase} rollback=failed") from exc
        except Exception as rollback_exc:
            raise DeploymentError(f"deployment failed phase={phase} rollback=failed") from rollback_exc
        raise DeploymentError(f"deployment failed phase={phase} rollback=complete") from exc


def _verify_imports(service_root: Path, support) -> None:
    command = ["/srv/projects/api/venv-simon/bin/python", "-B", "-c",
               "import challenge_automatic_intake, challenge_intake_api, leaderboard_challenge_ingest, plugin_tracking_catalog; "
               "assert callable(challenge_automatic_intake.migrate_schema); "
               "assert callable(plugin_tracking_catalog.active_document); "
               "assert hasattr(challenge_intake_api, 'app') and challenge_intake_api.app is None; "
               "assert callable(leaderboard_challenge_ingest.ingest_connection)"]
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C",
           "PYTHONDONTWRITEBYTECODE": "1", "CHALLENGE_INTAKE_SKIP_DEFAULT_APP": "1"}
    proc = subprocess.run(command, cwd=service_root, env=env, capture_output=True, timeout=30, check=False)
    if proc.returncode:
        raise DeploymentError("installed runtime import check failed")


def rollback_record(*, record_path: Path, support, systemd, commit: str | None = None) -> dict[str, Any]:
    """Recover an interrupted/committed transaction through the same guarded path."""
    if os.geteuid() != 0:
        raise DeploymentError("rollback requires root")
    try:
        resolved_root = BACKUP_ROOT.resolve(strict=True)
        if record_path.is_symlink() or not record_path.is_absolute() or record_path.resolve(strict=True).parent != record_path.parent.resolve(strict=True):
            raise ValueError("record path shape")
        record_path.relative_to(resolved_root)
        support._verify_private_file(record_path, 0)
        data = json.loads(support._read_regular(record_path, 1024 * 1024))
    except Exception as exc:
        raise DeploymentError("rollback record metadata/schema rejected") from exc
    required = {"schema", "target_commit", "state", "database_backup", "database_backup_sha256",
                "database_before", "prior_units", "prior_nginx", "files", "schema_migrated",
                "nginx_reloaded", "restoration"}
    if not isinstance(data, dict) or set(data) != required or data.get("schema") != 1:
        raise DeploymentError("rollback record schema rejected")
    target_commit = data.get("target_commit")
    if not isinstance(target_commit, str) or not FULL_SHA.fullmatch(target_commit) or (commit and commit != target_commit):
        raise DeploymentError("rollback target identity rejected")
    allowed_states = {"backed_up", "files_installed", "schema_migration_pending", "schema_installed",
                      "nginx_reload_pending", "nginx_reloaded", "committed", "rollback_failed"}
    if data.get("state") not in allowed_states:
        raise DeploymentError("rollback transaction state rejected")
    release = RUNTIME / "releases" / target_commit
    support.verify_git(REPO, target_commit)
    release_manifest, source_manifest = _read_manifest(release, target_commit, support)
    target_items = _manifest_target_items(release, target_commit, release_manifest, source_manifest, support)
    target_by_path = {str(item["target"]): item for item in target_items}
    file_records = data.get("files")
    if not isinstance(file_records, list) or len(file_records) != len(TARGETS):
        raise DeploymentError("rollback file manifest incomplete")
    tx_files = []
    seen = set()
    for entry in file_records:
        if not isinstance(entry, dict) or entry.get("target") not in target_by_path or entry["target"] in seen:
            raise DeploymentError("rollback file manifest ambiguous")
        seen.add(entry["target"])
        expected = target_by_path[entry["target"]]
        expected_absent = expected["baseline"] is None or bool(expected.get("allow_absent") and entry.get("was_absent"))
        if (entry.get("source_relative") != expected["source_relative"] or
                entry.get("after_sha256") != expected["after_sha256"] or
                entry.get("after_size") != expected["after_size"] or
                bool(entry.get("was_absent")) != expected_absent):
            raise DeploymentError("rollback file manifest does not match immutable release")
        if not entry["was_absent"]:
            backup = Path(str(entry.get("backup_path") or ""))
            if backup.parent != record_path.parent or support._sha_file(backup) != entry.get("backup_sha256"):
                raise DeploymentError("rollback file backup rejected")
            support._verify_private_file(backup, 0)
            if not isinstance(entry.get("before_metadata"), dict) or (
                    expected["baseline"] is not None and
                    entry.get("before_sha256") != expected["baseline"]["sha256"]):
                raise DeploymentError("rollback file predecessor metadata rejected")
        else:
            if entry.get("backup_path") is not None or entry.get("before_sha256") is not None:
                raise DeploymentError("rollback absent-file record malformed")
        tx_files.append({**expected, **entry, "target": expected["target"], "source": expected["source"]})
    backup = Path(str(data.get("database_backup") or ""))
    if backup.parent != record_path.parent or not backup.exists() or support._sha_file(backup) != data.get("database_backup_sha256"):
        raise DeploymentError("rollback database backup rejected")
    support._verify_private_file(backup, 0)
    db_before = data.get("database_before")
    if (not isinstance(db_before, dict) or "database_file_meta" not in db_before or
            not isinstance(data.get("prior_units"), dict) or set(data["prior_units"]) != set(CONTROLLED)):
        raise DeploymentError("rollback prestate record incomplete")
    config_module, auto_module = _load_release_modules(release)
    db_before = {**db_before, "config_module": config_module, "automatic_module": auto_module}
    tx = Transaction(directory=record_path.parent, commit=target_commit, items=target_items,
                     prior_units=data["prior_units"], prior_nginx=data["prior_nginx"],
                     database_before=db_before, database_backup=backup)
    tx.file_backups = tx_files
    tx.schema_migrated = bool(data["schema_migrated"])
    tx.nginx_reloaded = bool(data["nginx_reloaded"]) or data["state"] == "nginx_reload_pending"
    _rollback(tx, systemd, support, release, auto_module)
    return {"status": "rolled_back", "target_commit": target_commit,
            "transaction_record": str(record_path), "observation_rows": 0,
            "valid_observation_sent": False}


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="read-only plan (default)")
    mode.add_argument("--apply", action="store_true", help="apply guarded installation; root required")
    mode.add_argument("--rollback-record", type=Path, help="root-only guarded rollback from a transaction record")
    parser.add_argument("--commit", help="exact full SHA with a prepared immutable release")
    args = parser.parse_args(argv)
    try:
        if args.rollback_record is not None:
            if args.commit is None or not FULL_SHA.fullmatch(args.commit):
                raise DeploymentError("rollback requires the exact transaction commit SHA")
            support = _load_support(RUNTIME / "releases" / args.commit)
            if os.geteuid() != 0:
                raise DeploymentError("rollback requires root")
            lock_fd = support._deployment_lock(LOCK)
            try:
                result = rollback_record(record_path=args.rollback_record, support=support,
                                         systemd=support.Systemd(), commit=args.commit)
            finally:
                import fcntl
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
            return 0
        if args.commit is None or not FULL_SHA.fullmatch(args.commit):
            raise DeploymentError("preflight failed diagnostic_category=commit_preflight_failed")
        release = RUNTIME / "releases" / args.commit
        support = _preflight_step("runtime_support_preflight_failed", _load_support, release)
        systemd = _preflight_step("service_manager_preflight_failed", support.Systemd)
        lock_fd = None
        if args.apply:
            if os.geteuid() != 0:
                raise DeploymentError("apply requires root")
            lock_fd = support._deployment_lock(LOCK)
        try:
            plan = make_plan(commit=args.commit, support=support, systemd=systemd)
            result = (apply(commit=args.commit, plan=plan, support=support, systemd=systemd)
                      if args.apply else plan)
        finally:
            if lock_fd is not None:
                import fcntl
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception as exc:
        category = _diagnostic_category(exc)
        # Only fixed categories are emitted. No exception text, source path,
        # request response, database contents, or identity reaches stdout.
        print(json.dumps({"status": "blocked", "diagnostic_category": category}, separators=(",", ":")))
        return 2


def _diagnostic_category(exc: BaseException) -> str:
    """Map only fixed deployment phases/categories to bounded CLI output."""
    message = str(exc)
    explicit = re.search(r"(?:^|\s)diagnostic_category=([a-z_]+)(?:\s|$)", message)
    if explicit:
        return explicit.group(1)
    phase = re.search(r"(?:^|\s)phase=([a-z_]+)(?:\s|$)", message)
    if phase:
        return _PHASE_DIAGNOSTIC_CATEGORIES.get(phase.group(1), "preflight_blocked")
    return "preflight_blocked"


if __name__ == "__main__":
    raise SystemExit(_cli())
