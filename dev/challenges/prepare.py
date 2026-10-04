#!/usr/bin/env python3
"""Verify the adopted Challenges source snapshot; no production apply mode."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TREE = ROOT / "dev/challenges"
MANIFEST = TREE / "source-manifest.json"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
NORMALIZATIONS = {
    "dev/challenges/website/assets/styles.css": "strip_trailing_horizontal_whitespace",
    "dev/challenges/website/assets/nocturne-global.css": "normalize_known_global_css_whitespace",
}
REPOSITORY_EXTENSIONS = {
    "dev/challenges/service/challenge_config.py": "Adds repository-owned timing/capture fields and validation; not byte-equivalent to the live baseline.",
    "dev/challenges/tests/python/test_challenge_config.py": "Adds regression coverage for repository-owned timing/capture behavior; not byte-equivalent to the live baseline.",
    "dev/challenges/website/challenge-admin-state.js": "Adds repository-owned timing/capture editor state; not byte-equivalent to the live baseline.",
    "dev/challenges/website/challenge-admin.html": "Adds repository-owned timing/capture controls; not byte-equivalent to the live baseline.",
    "dev/challenges/website/tests/challenge-admin-state.test.js": "Adds regression coverage for repository-owned timing/capture controls; not byte-equivalent to the live baseline.",
    "dev/challenges/service/challenge_automatic_intake.py": "Adds the repository-owned, tokenless server-authoritative RuneLite observation boundary and reversible additive schema.",
    "dev/challenges/service/challenge_intake_api.py": "Adds a repository-owned public bounded observation route; privileged/manual routes remain unchanged.",
    "dev/challenges/service/leaderboard_challenge_ingest.py": "Adds repository-owned automatic-observation provenance and full observed group-size projection while preserving the legacy participant-count fallback.",
    "dev/challenges/integration/routes/nocturne-challenge-intake.location.conf": "Adds the repository-owned bounded Nginx route for automatic observations; not byte-equivalent to the live baseline.",
    "dev/challenges/tests/python/test_challenge_automatic_intake.py": "Adds repository-owned fixtures for automatic observation validation, persistence, idempotency, and projection.",
    "dev/challenges/AUTOMATIC_OBSERVATIONS.md": "Documents the repository-owned public observation contract and trust boundary.",
    "dev/challenges/AUTOMATIC_OBSERVATIONS_DEPLOYMENT.md": "Documents the repository-owned guarded automatic-observation deployment boundary and operator procedure.",
    "dev/challenges/deploy_automatic_observations.py": "Adds a repository-owned dry-run-first, immutable-release-bound installer with transactional database backup, WAL quiescence, rollback, and non-mutating verification.",
    "dev/challenges/tests/python/test_deploy_automatic_observations.py": "Adds repository-owned disposable state-machine and migration regression fixtures for the automatic-observation deployment helper.",
}


def bindings() -> dict[str, str]:
    """Repository-relative file -> read-only live source path."""
    result: dict[str, str] = {}
    service_files = (
        "challenge_award_delivery.py", "challenge_config.py",
        "challenge_config_api.py", "challenge_direct_intake.py", "challenge_direct_diagnostics.py",
        "challenge_intake_api.py", "challenge_legacy_baseline.py",
        "challenge_member_view.py", "challenge_shadow_common.py",
        "challenge_shadow_sync.py", "leaderboard_api.py",
        "leaderboard_challenge_ingest.py", "leaderboard_projection_refresh.py",
        "leaderboard_proof_resolver.py", "leaderboard_shadow_renderer.py",
    )
    for name in service_files:
        result[f"dev/challenges/service/{name}"] = f"/srv/projects/nocturne-services/{name}"
    result["dev/challenges/service/leaderboard_proof_404.json"] = "/srv/projects/nocturne-services/leaderboard_proof_404.json"
    result.update({
        "dev/challenges/website/challenge-admin.html": "/srv/projects/website/challenge-admin.html",
        "dev/challenges/website/challenge-admin-state.js": "/srv/projects/website/challenge-admin-state.js",
        "dev/challenges/website/navbar.html": "/srv/projects/website/navbar.html",
        "dev/challenges/website/assets/styles.css": "/srv/projects/website/styles.css",
        "dev/challenges/website/assets/nocturne-theme.css": "/srv/projects/website/nocturne-theme.css",
        "dev/challenges/website/assets/nocturne-global.css": "/srv/projects/website/nocturne-global.css",
        "dev/challenges/website/media/nocturne_logo.gif": "/srv/projects/website/media/nocturne_logo.gif",
        "dev/challenges/consumers/nocturne-bot/utils/challengeConfig.js": "/srv/projects/nocturne-bot/utils/challengeConfig.js",
        "dev/challenges/consumers/nocturne-bot/utils/challengeMetric.js": "/srv/projects/nocturne-bot/utils/challengeMetric.js",
        "dev/challenges/consumers/nocturne-bot/utils/challengeSubmissionIdentity.js": "/srv/projects/nocturne-bot/utils/challengeSubmissionIdentity.js",
        "dev/challenges/consumers/nocturne-bot/utils/midgardChallengeIntake.js": "/srv/projects/nocturne-bot/utils/midgardChallengeIntake.js",
        "dev/challenges/consumers/nocturne-bot/utils/dropSubmissionUtils.js": "/srv/projects/nocturne-bot/utils/dropSubmissionUtils.js",
        "dev/challenges/website/tests/challenge-admin-state.test.js": "/srv/projects/website/tests/challenge-admin-state.test.js",
        "dev/challenges/consumers/nocturne-bot/tests/challengeConfig.test.js": "/srv/projects/nocturne-bot/tests/challengeConfig.test.js",
        "dev/challenges/consumers/nocturne-bot/tests/challengeMetric.test.js": "/srv/projects/nocturne-bot/tests/challengeMetric.test.js",
        "dev/challenges/integration/units/nocturne-challenge-intake.service": "/srv/projects/nocturne-services/systemd/nocturne-challenge-intake.service",
        "dev/challenges/integration/units/osrs-drops-admin.service": "/srv/projects/api/osrs-drops-admin.service",
        "dev/challenges/integration/units/osrs-drops-admin.active.service": "/etc/systemd/system/osrs-drops-admin.service",
        "dev/challenges/integration/units/nocturne-challenge-intake.active.service": "/etc/systemd/system/nocturne-challenge-intake.service",
        "dev/challenges/integration/units/osrs-drops-api.service": "/etc/systemd/system/osrs-drops-api.service",
        "dev/challenges/integration/routes/nocturne-challenge-intake.location.conf": "/srv/projects/nocturne-services/nginx/nocturne-challenge-intake.location.conf",
    })
    for name in (
        "test_challenge_config.py", "test_challenge_config_api.py",
        "test_challenge_direct_intake.py", "test_leaderboard_api.py",
        "test_leaderboard_challenge_ingest.py", "test_leaderboard_projection_refresh.py",
        "test_challenge_member_api.py", "test_challenge_shadow.py",
    ):
        result[f"dev/challenges/tests/python/{name}"] = f"/srv/projects/nocturne-services/tests/{name}"
    for path in sorted(Path("/srv/projects/website/media/boss_icons").glob("*.png")):
        result[f"dev/challenges/website/media/boss_icons/{path.name}"] = str(path)
    return result


EXTERNAL_REFERENCES = {
    "admin_api_auth_proxy": ("/srv/projects/api/admin_app.py", [[39, 48], [241, 260], [293, 423], [426, 551]]),
    "admin_navigation_source": ("/srv/projects/website/admin.html", [[1015, 1019]]),
    "public_blueprint_registration": ("/srv/projects/nocturne-services/api.py", [[72, 73]]),
    "manual_challenge_submission_entry": ("/srv/projects/nocturne-bot/commands/utility/submit.js", [[1, 80]]),
    "manual_challenge_approval_handler": ("/srv/projects/nocturne-bot/handlers/buttonHandler.js", [[885, 990]]),
    "admin_nginx_auth_and_routes": ("/etc/nginx/sites-enabled/nocturne", [[275, 297], [379, 405]]),
    "public_api_systemd_unit": ("/etc/systemd/system/osrs-drops-api.service", None),
    "admin_active_systemd_unit": ("/etc/systemd/system/osrs-drops-admin.service", None),
    "intake_active_systemd_unit": ("/etc/systemd/system/nocturne-challenge-intake.service", None),
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def strip_trailing_horizontal_whitespace(data: bytes) -> bytes:
    output = []
    for line in data.splitlines(keepends=True):
        if line.endswith(b"\r\n"):
            body, ending = line[:-2], b"\r\n"
        elif line.endswith((b"\n", b"\r")):
            body, ending = line[:-1], line[-1:]
        else:
            body, ending = line, b""
        output.append(body.rstrip(b" \t") + ending)
    return b"".join(output)


def normalize_known_global_css_whitespace(data: bytes) -> bytes:
    """Remove two known redundant blank lines from the imported stylesheet."""
    marker = b"}\n\n\n/* ----------------------------------------------------------\n   MOBILE"
    if data.count(marker) != 1:
        raise RuntimeError("global stylesheet whitespace normalization anchor changed")
    return data.replace(marker, marker.replace(b"}\n\n\n", b"}\n\n"), 1).rstrip(b"\r\n") + b"\n"


def acl_fingerprint(path: Path) -> str:
    proc = subprocess.run(
        ["/usr/bin/getfacl", "--absolute-names", "--omit-header", str(path)],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if proc.returncode:
        raise RuntimeError(f"cannot inspect ACL metadata for {path}")
    normalized = "\n".join(line.rstrip() for line in proc.stdout.splitlines() if line.strip()) + "\n"
    return hashlib.sha256(normalized.encode()).hexdigest()


def file_record(path: Path, source: str | None = None) -> dict:
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode) or st.st_nlink != 1:
        raise RuntimeError(f"unsafe non-ordinary file in source bundle: {path.relative_to(ROOT)}")
    relative = path.relative_to(ROOT).as_posix()
    record = {
        "path": relative,
        "type": "regular",
        "sha256": sha256(path),
        "size": st.st_size,
        "executable": bool(stat.S_IMODE(st.st_mode) & 0o111),
        "source": source,
        "normalization": NORMALIZATIONS.get(relative),
    }
    if relative in REPOSITORY_EXTENSIONS:
        record["source_relationship"] = "repository_owned_extension"
        record["extension_reason"] = REPOSITORY_EXTENSIONS[relative]
    return record


def source_record(path_text: str, metadata_seed: dict | None = None) -> dict:
    path = Path(path_text)
    st = path.lstat()
    common = {
        "path": path_text,
        "mode": f"{stat.S_IMODE(st.st_mode):04o}",
        "uid": st.st_uid,
        "gid": st.st_gid,
        "nlink": st.st_nlink,
    }
    if stat.S_ISLNK(st.st_mode):
        target_text = os.readlink(path)
        target = (path.parent / target_text).resolve(strict=True)
        target_stat = target.lstat()
        if st.st_nlink != 1 or not stat.S_ISREG(target_stat.st_mode) or target_stat.st_nlink != 1:
            raise RuntimeError(f"unsafe live symlink source node: {path_text}")
        record = {
            **common,
            "type": "symlink",
            "target": target_text,
            "resolved_path": str(target),
            "sha256": sha256(target),
            "size": target_stat.st_size,
            "target_uid": target_stat.st_uid,
            "target_gid": target_stat.st_gid,
            "target_mode": f"{stat.S_IMODE(target_stat.st_mode):04o}",
            "target_nlink": target_stat.st_nlink,
            "acl_sha256": acl_fingerprint(target),
        }
        if os.geteuid() != 0 and metadata_seed is not None:
            if (record["target"] != metadata_seed.get("target")
                    or record["resolved_path"] != metadata_seed.get("resolved_path")
                    or record["mode"] != metadata_seed.get("mode")
                    or record["target_mode"] != metadata_seed.get("target_mode")):
                raise RuntimeError("live source symlink drift: " + path_text)
            for key in ("uid", "gid", "acl_sha256", "target_uid", "target_gid", "target_nlink"):
                if key in metadata_seed:
                    record[key] = metadata_seed[key]
        return record
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise RuntimeError(f"unsafe live source node: {path_text}")
    record = {
        **common,
        "type": "regular",
        "sha256": sha256(path),
        "size": st.st_size,
        "acl_sha256": acl_fingerprint(path),
    }
    if os.geteuid() != 0 and metadata_seed is not None:
        if record["nlink"] != 1:
            raise RuntimeError("live source link-count drift: " + path_text)
        # Linux user namespaces can map host service groups to overflow IDs and
        # ACL principals to 4294967295. Preserve the root-captured identity
        # baseline while still checking bytes/type/mode/linkage here.
        record["uid"] = metadata_seed["uid"]
        record["gid"] = metadata_seed["gid"]
        record["acl_sha256"] = metadata_seed["acl_sha256"]
    return record


def expected_files() -> list[Path]:
    paths = [p for p in TREE.rglob("*") if p.is_file() and p != MANIFEST
             and "__pycache__" not in p.parts and p.suffix != ".pyc"]
    if any(p.is_symlink() for p in TREE.rglob("*")):
        raise RuntimeError("symlink found in adopted source tree")
    return sorted(paths, key=lambda p: p.relative_to(ROOT).as_posix())


def refresh_bundle_records() -> None:
    """Refresh only repository file records, preserving host-captured metadata."""
    data = json.loads(MANIFEST.read_text(encoding="utf-8"))
    source_map = bindings()
    files = expected_files()
    rels = {p.relative_to(ROOT).as_posix() for p in files}
    missing = sorted(set(source_map) - rels)
    if missing:
        raise RuntimeError("source bindings missing from bundle: " + ", ".join(missing))
    data["bundle_files"] = [file_record(p, source_map.get(p.relative_to(ROOT).as_posix())) for p in files]
    MANIFEST.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def build_manifest() -> dict:
    prior = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {}
    prior_sources = {r["path"]: r for r in prior.get("live_sources", [])}
    prior_sources.update({r["file"]["path"]: r["file"] for r in prior.get("external_references", [])})

    def seed_for(path_text: str) -> dict | None:
        if path_text in prior_sources:
            return prior_sources[path_text]
        parent = str(Path(path_text).parent) + "/"
        sibling = next((record for key, record in prior_sources.items() if key.startswith(parent)), None)
        if sibling:
            return sibling
        if path_text.startswith("/srv/projects/nocturne-bot/"):
            return next((record for key, record in prior_sources.items()
                         if key.startswith("/srv/projects/nocturne-bot/")), None)
        return None

    source_map = bindings()
    files = expected_files()
    rels = {p.relative_to(ROOT).as_posix() for p in files}
    missing = sorted(set(source_map) - rels)
    if missing:
        raise RuntimeError("source bindings missing from bundle: " + ", ".join(missing))
    records = [file_record(p, source_map.get(p.relative_to(ROOT).as_posix())) for p in files]
    live = {source for source in source_map.values()}
    sources = [source_record(p, seed_for(p)) for p in sorted(live)]
    references = []
    for label, (path, lines) in EXTERNAL_REFERENCES.items():
        references.append({"label": label, "file": source_record(path, seed_for(path)), "line_ranges": lines})
    return {
        "schema_version": 1,
        "purpose": "source-only adoption; not an installer payload",
        "metadata_capture": "host-root" if os.geteuid() == 0 else "namespace-view-not-authoritative",
        "bundle_files": records,
        "live_sources": sources,
        "external_references": references,
        "excluded_classes": [
            "databases and SQLite sidecars", "credentials and environment files",
            "active LKG/config cache", "logs and reports", "__pycache__ and bytecode",
            "uploaded challenge artwork", "runtime state and generated JSON",
        ],
    }


def git_value(*args: str) -> str:
    proc = subprocess.run(["/usr/bin/git", "-c", f"safe.directory={ROOT}", "-C", str(ROOT), *args], check=False,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode:
        raise RuntimeError("Git verification failed: " + proc.stderr.strip()[:300])
    return proc.stdout.strip()


def verify_bundle(expected: dict | None = None) -> None:
    manifest = expected if expected is not None else json.loads(MANIFEST.read_text(encoding="utf-8"))
    actual_paths = expected_files()
    records = manifest.get("bundle_files")
    if not isinstance(records, list):
        raise RuntimeError("manifest bundle_files schema invalid")
    listed = [r.get("path") for r in records if isinstance(r, dict)]
    actual_names = [p.relative_to(ROOT).as_posix() for p in actual_paths]
    if sorted(listed) != actual_names or len(listed) != len(set(listed)):
        raise RuntimeError("bundle completeness/uniqueness mismatch")
    sources_by_path = {r.get("path"): r for r in manifest.get("live_sources", []) if isinstance(r, dict)}
    expected_sources = bindings()
    for record, path in zip(records, actual_paths):
        current = file_record(path, record.get("source"))
        if current != record:
            raise RuntimeError("adopted file drift: " + current["path"])
        if record.get("source") != expected_sources.get(current["path"]):
            raise RuntimeError("source binding mismatch: " + current["path"])
        relationship = record.get("source_relationship")
        if relationship not in {None, "repository_owned_extension"}:
            raise RuntimeError("unknown source relationship: " + current["path"])
        if relationship == "repository_owned_extension":
            if (current["path"] not in REPOSITORY_EXTENSIONS
                    or record.get("extension_reason") != REPOSITORY_EXTENSIONS[current["path"]]
                    or record.get("normalization")):
                raise RuntimeError("invalid repository extension record: " + current["path"])
            continue
        rule = record.get("normalization")
        if rule:
            if rule not in {"strip_trailing_horizontal_whitespace", "normalize_known_global_css_whitespace"} or not record.get("source"):
                raise RuntimeError("unknown source normalization: " + current["path"])
            live = Path(record["source"]).read_bytes()
            normalizer = {
                "strip_trailing_horizontal_whitespace": strip_trailing_horizontal_whitespace,
                "normalize_known_global_css_whitespace": normalize_known_global_css_whitespace,
            }[rule]
            normalized = normalizer(live)
            if path.read_bytes() != normalized:
                raise RuntimeError("source normalization mismatch: " + current["path"])
    for path, recorded in sources_by_path.items():
        if source_record(path, recorded) != recorded:
            raise RuntimeError("live implementation drift: " + path)
    for record in manifest.get("external_references", []):
        if source_record(record["file"]["path"], record["file"]) != record["file"]:
            raise RuntimeError("external integration reference drift: " + record["label"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument("--refresh-manifest", action="store_true", help="write only the repository source manifest")
    group.add_argument("--refresh-bundle-files-only", action="store_true",
                       help="refresh repository bundle file hashes without recapturing live metadata")
    group.add_argument("--verify-bundle", action="store_true", help="verify bundle against its manifest without Git/live gates")
    group.add_argument("--commit", help="read-only dry-run bound to this exact published full commit; defaults to HEAD")
    args = parser.parse_args(argv)
    try:
        if args.refresh_manifest:
            data = build_manifest()
            MANIFEST.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")
            print(f"manifest_refreshed files={len(data['bundle_files'])} sources={len(data['live_sources'])}")
            return 0
        if args.refresh_bundle_files_only:
            refresh_bundle_records()
            print("bundle_file_records_refreshed live_metadata=preserved")
            return 0
        if args.verify_bundle:
            verify_bundle()
            print("status=bundle_verified production=not_modified")
            return 0
        if os.geteuid() != 0:
            raise RuntimeError("run the live metadata dry-run as root; no files or services are changed")
        requested_commit = args.commit or git_value("rev-parse", "HEAD")
        if not FULL_SHA.fullmatch(requested_commit):
            raise RuntimeError("commit must be a full 40-character lowercase SHA")
        if git_value("branch", "--show-current") != "development":
            raise RuntimeError("branch must be development")
        head = git_value("rev-parse", "HEAD")
        remote = git_value("rev-parse", "origin/development")
        if head != requested_commit or remote != requested_commit:
            raise RuntimeError("requested commit, HEAD and origin/development must match exactly")
        if git_value("status", "--porcelain"):
            raise RuntimeError("worktree must be clean")
        if json.loads(MANIFEST.read_text(encoding="utf-8")).get("metadata_capture") != "host-root":
            raise RuntimeError("manifest ownership/ACL capture is not authoritative; refresh it from the host root view first")
        verify_bundle()
        print(f"status=verified commit={head} files={len(json.loads(MANIFEST.read_text())['bundle_files'])}")
        print("mode=dry-run production_mutations=none databases=untouched services=untouched")
        print("plan=verify-pinned-source-bytes-and-live-metadata; no install/apply mode exists")
        print("backup=required-for-future-reviewed-deployment rollback=restore-exact-prestate")
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"status=blocked reason={str(exc)[:400]}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
