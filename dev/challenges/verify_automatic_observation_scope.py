#!/usr/bin/env python3
"""Offline integrity gate for the automatic-observation deployment boundary.

Unlike prepare.py --verify-bundle, this gate intentionally does not inspect
external live-source references. It validates only repository files consumed
by the automatic-observation installer/runtime and that installer's exact
target/predecessor contract.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = Path("dev/challenges/source-manifest.json")
DEPLOYER = Path("dev/challenges/deploy_automatic_observations.py")
AUTO_TARGET = "/srv/projects/nocturne-services/challenge_automatic_intake.py"
AUTHORITATIVE_MEMBERS_DB = "/srv/projects/database/Members.db"
EXPECTED_TARGETS = (
    ("dev/challenges/service/challenge_automatic_intake.py", AUTO_TARGET, "absent_or_previous"),
    ("dev/challenges/service/challenge_intake_api.py",
     "/srv/projects/nocturne-services/challenge_intake_api.py", "manifest"),
    ("dev/challenges/service/leaderboard_challenge_ingest.py",
     "/srv/projects/nocturne-services/leaderboard_challenge_ingest.py", "manifest"),
    ("dev/challenges/integration/routes/nocturne-challenge-intake.location.conf",
     "/srv/projects/nocturne-services/nginx/nocturne-challenge-intake.location.conf", "manifest"),
)

# This is the exact repository-local closure imported by the runtime checks and
# observation path, plus installer support, its canonical read-only readiness
# checker, the single route fragment, controlled service units, and regressions.
SERVICE_RUNTIME = (
    "challenge_automatic_intake.py", "challenge_award_delivery.py",
    "challenge_config.py", "challenge_direct_diagnostics.py",
    "challenge_direct_intake.py", "challenge_intake_api.py",
    "challenge_legacy_baseline.py", "challenge_shadow_common.py",
    "challenge_shadow_sync.py", "leaderboard_challenge_ingest.py",
)
UNIT_REFERENCES = (
    "dev/challenges/integration/units/nocturne-challenge-intake.active.service",
    "dev/challenges/integration/units/nocturne-challenge-intake.service",
    "dev/challenges/integration/units/osrs-drops-admin.active.service",
    "dev/challenges/integration/units/osrs-drops-admin.service",
    "dev/challenges/integration/units/osrs-drops-api.service",
)
EXPECTED_CONTROLLED = frozenset({
    "osrs-drops-api.service", "nocturne-challenge-intake.service",
    "osrs-drops-admin.service", "nocturne-challenge-shadow-sync.service",
    "nocturne-leaderboard-shadow-renderer.service",
    "nocturne-challenge-shadow-sync.timer", "nocturne-leaderboard-shadow-renderer.timer",
})
EXPECTED_RESTARTS = ("nocturne-challenge-intake.service",)
SCOPE_BUNDLE_PATHS = (
    "dev/challenges/AUTOMATIC_OBSERVATIONS.md",
    "dev/challenges/AUTOMATIC_OBSERVATIONS_DEPLOYMENT.md",
    "dev/challenges/deploy_automatic_observations.py",
    "dev/challenges/deploy_timing_metadata.py",
    "dev/challenges/verify_automatic_observation_scope.py",
    "dev/challenges/integration/routes/nocturne-challenge-intake.location.conf",
    *(f"dev/challenges/service/{name}" for name in SERVICE_RUNTIME),
    *UNIT_REFERENCES,
    "dev/challenges/tests/python/test_challenge_automatic_intake.py",
    "dev/challenges/tests/python/test_deploy_automatic_observations.py",
    "dev/challenges/tests/python/test_automatic_observation_scope.py",
)
SCOPE_SET = frozenset(SCOPE_BUNDLE_PATHS)
# These repository-level runtime gates are outside the adopted Challenges
# bundle. Pin their exact repository bytes here rather than pulling unrelated
# product/external-reference records into this feature's publication gate.
REPOSITORY_RUNTIME_PINS = {
    "dev/intake/prepare_immutable_runtime.sh": ("37fc800734c9da335deb4fcc51f2cf8e9cc15353246c74d5b7fa55c1c9c6166b", 12371),
    "dev/intake/immutable_runtime_check.py": ("903a751249c4a54dbbfddb4ab35c457f2ef81132fe71d9838f2c9ab214c09f58", 19568),
    "dev/intake/nginx_reload_convergence.py": ("f104765dacb203feb35b7d588818d05a05f282ed9f72bcab4c0b5ed48f501547", 7378),
}


class ScopeError(RuntimeError):
    pass


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest(repo: Path) -> dict:
    path = repo / MANIFEST
    if path.is_symlink() or not path.is_file():
        raise ScopeError("scoped source manifest is not a regular file")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ScopeError("scoped source manifest is unreadable") from exc
    if not isinstance(value, dict):
        raise ScopeError("scoped source manifest schema rejected")
    return value


def _records(records: object, paths: frozenset[str], label: str) -> dict[str, dict]:
    if not isinstance(records, list):
        raise ScopeError(f"{label} records are missing")
    result: dict[str, dict] = {}
    for record in records:
        if not isinstance(record, dict) or record.get("path") not in paths:
            continue
        path = record["path"]
        if path in result:
            raise ScopeError(f"duplicate scoped {label} record")
        result[path] = record
    return result


def _load_deployer(repo: Path):
    path = repo / DEPLOYER
    if path.is_symlink() or not path.is_file():
        raise ScopeError("automatic-observation installer is missing")
    name = f"_automatic_observation_scope_deployer_{_sha(path)[:16]}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ScopeError("automatic-observation installer cannot be inspected")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _verify_import_closure(repo: Path) -> None:
    service_root = repo / "dev/challenges/service"
    discovered: set[str] = set()
    pending = ["challenge_automatic_intake", "challenge_intake_api", "leaderboard_challenge_ingest"]
    while pending:
        module = pending.pop()
        if module in discovered:
            continue
        discovered.add(module)
        source = service_root / f"{module}.py"
        if source.is_symlink() or not source.is_file():
            raise ScopeError("automatic-observation runtime import is missing")
        import ast
        tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
        for node in ast.walk(tree):
            imported = ([item.name for item in node.names] if isinstance(node, ast.Import)
                        else [node.module] if isinstance(node, ast.ImportFrom) and node.module
                        else [])
            for name in imported:
                if name.startswith(("challenge_", "leaderboard_")) and (service_root / f"{name}.py").is_file():
                    pending.append(name)
    expected = set(SERVICE_RUNTIME)
    if discovered != {Path(item).stem for item in expected}:
        raise ScopeError("automatic-observation runtime import closure changed")


def verify_scope(repo: Path = ROOT) -> dict[str, object]:
    """Validate scoped repository bytes and deployment assumptions only."""
    repo = Path(repo)
    manifest = _manifest(repo)
    bundles = _records(manifest.get("bundle_files"), SCOPE_SET, "bundle")
    if set(bundles) != SCOPE_SET:
        raise ScopeError("automatic-observation scoped bundle is incomplete")
    for relative in sorted(SCOPE_SET):
        record = bundles[relative]
        source = repo / relative
        if (source.is_symlink() or not source.is_file() or not stat.S_ISREG(source.lstat().st_mode)
                or record.get("type") != "regular"
                or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256", "")))
                or isinstance(record.get("size"), bool)
                or not isinstance(record.get("size"), int)):
            raise ScopeError("automatic-observation scoped file metadata rejected")
        if source.stat().st_size != record["size"] or _sha(source) != record["sha256"]:
            raise ScopeError("automatic-observation scoped file digest mismatch")
    for relative, (digest, size) in REPOSITORY_RUNTIME_PINS.items():
        source = repo / relative
        if (source.is_symlink() or not source.is_file() or source.stat().st_size != size or
                _sha(source) != digest):
            raise ScopeError("automatic-observation runtime reference drift")

    deployer = _load_deployer(repo)
    actual_targets = tuple((str(source), str(target), kind)
                           for source, target, kind in deployer.TARGETS)
    if actual_targets != EXPECTED_TARGETS:
        raise ScopeError("automatic-observation install target contract changed")
    actual_controlled = frozenset(str(value) for value in deployer.CONTROLLED)
    if actual_controlled != EXPECTED_CONTROLLED or tuple(deployer.RESTART_FOR_IMPORTS) != EXPECTED_RESTARTS:
        raise ScopeError("automatic-observation service control contract changed")
    unit_text = "\n".join((repo / path).read_text(encoding="utf-8") for path in UNIT_REFERENCES)
    required_runtime_wiring = (
        "challenge_intake_api:app",
        "127.0.0.1:5011", "127.0.0.1:5003", "127.0.0.1:5002",
    )
    if any(value not in unit_text for value in required_runtime_wiring):
        raise ScopeError("automatic-observation service unit reference changed")
    intake_api = (repo / "dev/challenges/service/challenge_intake_api.py").read_text(encoding="utf-8")
    if (f'DEFAULT_MEMBERS_DB = Path("{AUTHORITATIVE_MEMBERS_DB}")' not in intake_api or
            'MEMBERS_DB=DEFAULT_MEMBERS_DB' not in intake_api or
            'conn = ro_connection(path)' not in intake_api or
            f'ReadOnlyPaths={AUTHORITATIVE_MEMBERS_DB}' not in unit_text):
        raise ScopeError("automatic-observation authoritative membership database boundary changed")
    live_paths = frozenset(target for _source, target, kind in EXPECTED_TARGETS if kind == "manifest")
    live_records = _records(manifest.get("live_sources"), live_paths, "live predecessor")
    if set(live_records) != live_paths:
        raise ScopeError("automatic-observation predecessor records are incomplete")
    for target, record in live_records.items():
        if (record.get("type") != "regular" or
                not re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256", ""))) or
                not isinstance(record.get("size"), int)):
            raise ScopeError("automatic-observation predecessor profile is malformed")
    if any(item.get("path") == AUTO_TARGET for item in manifest.get("live_sources", [])
           if isinstance(item, dict)):
        raise ScopeError("new automatic-observation target acquired an unreviewed live baseline")
    if (getattr(deployer, "PREVIOUS_AUTOMATIC_INTAKE_SHA256", None) !=
            "52c6d06f732f64759e148119e3e93c53f4139a97a2d27242dcd8352cd643ede3" or
            getattr(deployer, "PREVIOUS_AUTOMATIC_INTAKE_SIZE", None) != 28178 or
            getattr(deployer, "LEGACY_AUTOMATIC_INTAKE_PREDECESSORS", None) != (
                ("9008d57932b8addea6d17421aa93c7265b9d41603b1d7154cc5e8efd1c53db03", 27618),)):
        raise ScopeError("automatic-observation installed/predecessor contract changed")
    _verify_import_closure(repo)
    return {
        "status": "scoped_bundle_verified",
        "bundle_files": len(SCOPE_SET),
        "runtime_modules": len(SERVICE_RUNTIME),
        "install_targets": len(EXPECTED_TARGETS),
        "controlled_units": len(EXPECTED_CONTROLLED),
        "live_predecessors": len(live_paths),
        "pinned_runtime_references": len(REPOSITORY_RUNTIME_PINS),
        "external_references_checked": 0,
    }


if __name__ == "__main__":
    try:
        print(json.dumps(verify_scope(), sort_keys=True))
    except Exception as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}))
        raise SystemExit(1)
