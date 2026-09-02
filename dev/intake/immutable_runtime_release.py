"""Prepare and activate immutable Nocturne runtime releases; dry-run by default."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tarfile
import tempfile
from uuid import uuid4

PURPOSE = "nocturne-immutable-runtime-v1"
UNITS = ("nocturne-plugin-writer.service", "nocturne-plugin-dev.service")
REQUIRED_UNIT_LINES = (
    "NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=yes",
    "PrivateTmp=yes", "PrivateDevices=yes", "UMask=0077",
)


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""): value.update(block)
    return value.hexdigest()


def command(args, **kwargs):
    return subprocess.run(args, check=True, timeout=60, **kwargs)


def full_commit(repo, revision, run=command):
    result = run(["git", "-C", str(repo), "rev-parse", "--verify", revision + "^{commit}"],
                 stdout=subprocess.PIPE, text=True)
    value = result.stdout.strip().lower()
    if len(value) != 40 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("revision did not resolve to a full commit SHA")
    return value


def release_units(release, runtime_root):
    generated = release / "deployment-units"; generated.mkdir(mode=0o755)
    old_root = "/srv/projects/nocturne-plugin-intake"
    current = str(runtime_root / "current")
    for name in UNITS:
        source = release / "dev/intake" / name
        text = source.read_text()
        if text.count(old_root) < 1: raise ValueError(f"unit lacks mutable checkout path: {name}")
        text = text.replace(old_root + "/.venv", str(runtime_root / "venv"))
        text = text.replace(old_root, current)
        if old_root in text: raise ValueError(f"mutable checkout remains in unit: {name}")
        for line in REQUIRED_UNIT_LINES:
            if text.count(line) != 1: raise ValueError(f"unit sandbox line missing or ambiguous: {line}")
        if name == "nocturne-plugin-dev.service":
            allowlist = 'Environment="NOCTURNE_TEST_RSNS=Simons Alt,RoatBefAuJu"'
            if text.count(allowlist) != 1: raise ValueError("intake allowlist changed")
            for line in ("MemoryMax=160M", "TasksMax=32", "InaccessiblePaths=/srv/projects/database"):
                if text.count(line) != 1: raise ValueError(f"intake limit missing: {line}")
        else:
            for line in ("MemoryMax=128M", "TasksMax=24", "PrivateNetwork=yes",
                         "ReadWritePaths=/srv/projects/database"):
                if text.count(line) != 1: raise ValueError(f"writer limit missing: {line}")
        target = generated / name; target.write_text(text); target.chmod(0o444)


def build_manifest(release, commit):
    files = {}
    for path in sorted(release.rglob("*")):
        if path.is_symlink(): raise ValueError("release contains a symlink")
        if path.is_file() and path.name != "RELEASE-MANIFEST.json":
            files[str(path.relative_to(release))] = digest(path)
    value = {"purpose": PURPOSE, "commit": commit, "files": files,
             "runtime_venv": {"path": "/srv/nocturne-plugin/venv",
                              "requirements": "dev/intake/runtime-requirements.txt",
                              "copied_virtualenv_allowed": False}}
    target = release / "RELEASE-MANIFEST.json"
    target.write_text(json.dumps(value, sort_keys=True) + "\n")
    return value


def verify_release(release, expected_commit=None):
    release = Path(release); manifest_path = release / "RELEASE-MANIFEST.json"
    if not release.is_dir() or release.is_symlink() or not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("release or manifest is missing or unsafe")
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("purpose") != PURPOSE: raise ValueError("release manifest purpose mismatch")
    if expected_commit and manifest.get("commit") != expected_commit: raise ValueError("release commit mismatch")
    actual_names = {str(p.relative_to(release)) for p in release.rglob("*") if p.is_file() and p != manifest_path}
    if actual_names != set(manifest.get("files", {})): raise ValueError("release manifest file set mismatch")
    for name, expected in manifest["files"].items():
        path = release / name
        if path.is_symlink() or not path.is_file() or digest(path) != expected: raise ValueError("release checksum mismatch")
    return manifest


def _make_read_only(root, uid, gid):
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_symlink(): raise ValueError("release contains a symlink")
        os.chown(path, uid, gid, follow_symlinks=False)
        os.chmod(path, 0o555 if path.is_dir() else 0o444)
    os.chown(root, uid, gid); os.chmod(root, 0o555)


def _discard_staging(root):
    if not root.exists(): return
    for path in root.rglob("*"):
        if not path.is_symlink(): path.chmod(0o755 if path.is_dir() else 0o644)
    root.chmod(0o755)
    shutil.rmtree(root)


def prepare(repo, runtime_root, revision="HEAD", *, apply=False, uid=0, gid=0, run=command, fail=None):
    repo, runtime_root = Path(repo), Path(runtime_root)
    commit = full_commit(repo, revision, run); release = runtime_root / "releases" / commit
    report = {"dry_run": not apply, "commit": commit, "release": str(release),
              "current": str(runtime_root / "current"), "venv": str(runtime_root / "venv")}
    if release.exists():
        verify_release(release, commit); return {**report, "state": "already_prepared"}
    if not apply: return {**report, "state": "not_prepared"}
    releases = runtime_root / "releases"; releases.mkdir(parents=True, mode=0o755, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".release-", dir=releases)); archive = staging.parent / (staging.name + ".tar")
    try:
        run(["git", "-C", str(repo), "archive", "--format=tar", "--output", str(archive), commit])
        if fail: fail("after_archive")
        with tarfile.open(archive, "r") as bundle:
            for member in bundle.getmembers():
                target = (staging / member.name).resolve()
                if staging.resolve() not in target.parents and target != staging.resolve(): raise ValueError("archive path escapes release")
                if member.issym() or member.islnk() or not (member.isdir() or member.isfile()): raise ValueError("archive contains unsupported entry")
            bundle.extractall(staging, filter="data")
        release_units(staging, runtime_root)
        manifest = build_manifest(staging, commit)
        if fail: fail("after_manifest")
        verify_release(staging, commit)
        _make_read_only(staging, uid, gid)
        if fail: fail("before_release_activation")
        os.replace(staging, release)
        verify_release(release, commit)
        return {**report, "state": "prepared", "manifest_sha256": digest(release / "RELEASE-MANIFEST.json"),
                "file_count": len(manifest["files"])}
    finally:
        archive.unlink(missing_ok=True)
        _discard_staging(staging)


def validate_venv(venv, run=command):
    python, gunicorn = Path(venv) / "bin/python", Path(venv) / "bin/gunicorn"
    if not python.is_file() or not os.access(python, os.X_OK) or not gunicorn.is_file() or not os.access(gunicorn, os.X_OK):
        raise ValueError("reproducible runtime venv is missing")
    version = run([str(gunicorn), "--version"], stdout=subprocess.PIPE, text=True).stdout.strip()
    if version != "gunicorn (version 26.2.0)": raise ValueError("runtime gunicorn version mismatch")
    run([str(python), "-B", "-m", "pip", "check"], stdout=subprocess.PIPE, text=True)


def activate(runtime_root, systemd, commit, *, apply=False, venv=None, fail=None, unit_uid=0, unit_gid=0):
    runtime_root, systemd = Path(runtime_root), Path(systemd); venv = Path(venv or runtime_root / "venv")
    release = runtime_root / "releases" / commit; verify_release(release, commit); validate_venv(venv)
    current = runtime_root / "current"
    previous = current.resolve().name if current.is_symlink() else None
    for name in UNITS:
        target = systemd / name
        if not target.is_file() or target.is_symlink(): raise ValueError(f"unsafe active unit: {target}")
    report = {"dry_run": not apply, "commit": commit, "previous_commit": previous}
    if not apply: return report
    record = runtime_root / "activation-records" / uuid4().hex; record.mkdir(parents=True, mode=0o700)
    backups = {}
    for name in UNITS:
        source, saved = systemd / name, record / name
        shutil.copy2(source, saved); backups[name] = digest(saved)
    state = {"purpose": PURPOSE, "commit": commit, "previous_commit": previous, "unit_sha256": backups}
    (record / "ACTIVATION.json").write_text(json.dumps(state, sort_keys=True) + "\n")
    staged_units = []
    old_link = current.readlink() if current.is_symlink() else None
    try:
        for name in UNITS:
            source, target = release / "deployment-units" / name, systemd / name
            fd, raw = tempfile.mkstemp(prefix="." + name, dir=systemd); os.close(fd); staged = Path(raw)
            shutil.copyfile(source, staged); os.chown(staged, unit_uid, unit_gid); os.chmod(staged, 0o644); staged_units.append((staged, target))
        link = runtime_root / (".current-" + uuid4().hex); link.symlink_to(Path("releases") / commit)
        if fail: fail("before_activation")
        os.replace(link, current)
        if fail: fail("after_symlink")
        for index, (staged, target) in enumerate(staged_units):
            os.replace(staged, target)
            if fail: fail(f"after_unit_{index}")
        return {**report, "dry_run": False, "activation_record": str(record)}
    except BaseException:
        rollback_link = runtime_root / (".current-restore-" + uuid4().hex)
        if old_link is not None:
            rollback_link.symlink_to(old_link); os.replace(rollback_link, current)
        else:
            current.unlink(missing_ok=True)
        for name in UNITS: shutil.copy2(record / name, systemd / name)
        raise
    finally:
        for staged, _target in staged_units: staged.unlink(missing_ok=True)


def rollback_activation(record, runtime_root, systemd, *, apply=False, fail=None, unit_uid=0, unit_gid=0):
    record, runtime_root, systemd = Path(record), Path(runtime_root), Path(systemd)
    state = json.loads((record / "ACTIVATION.json").read_text())
    if state.get("purpose") != PURPOSE: raise ValueError("activation record purpose mismatch")
    current = runtime_root / "current"
    if not current.is_symlink() or current.resolve().name != state["commit"]: raise ValueError("active release lineage mismatch")
    previous = state.get("previous_commit")
    if not previous: raise ValueError("activation has no previous release")
    verify_release(runtime_root / "releases" / previous, previous)
    for name, expected in state["unit_sha256"].items():
        if digest(record / name) != expected: raise ValueError("activation backup checksum mismatch")
        if digest(systemd / name) != digest(runtime_root / "releases" / state["commit"] / "deployment-units" / name):
            raise ValueError("active unit changed since activation")
    if not apply: return {"dry_run": True, "current_commit": state["commit"], "restore_commit": previous}
    active_units = {name: (systemd / name).read_bytes() for name in UNITS}
    try:
        link = runtime_root / (".current-rollback-" + uuid4().hex); link.symlink_to(Path("releases") / previous)
        os.replace(link, current)
        if fail: fail("after_rollback_symlink")
        for index,name in enumerate(UNITS):
            staged = systemd / ("." + name + ".rollback")
            staged.write_bytes((record / name).read_bytes()); os.chown(staged,unit_uid,unit_gid); os.chmod(staged,0o644)
            os.replace(staged,systemd/name)
            if fail: fail(f"after_rollback_unit_{index}")
        return {"dry_run": False, "current_commit": previous, "restored_from": str(record)}
    except BaseException:
        link = runtime_root / (".current-reinstate-" + uuid4().hex); link.symlink_to(Path("releases") / state["commit"]); os.replace(link,current)
        for name,content in active_units.items():
            staged=systemd/("."+name+".reinstate"); staged.write_bytes(content); os.chown(staged,unit_uid,unit_gid); os.chmod(staged,0o644); os.replace(staged,systemd/name)
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--repo",default="/srv/projects/nocturne-plugin-intake")
    parser.add_argument("--runtime-root",default="/srv/nocturne-plugin"); parser.add_argument("--systemd-dir",default="/etc/systemd/system")
    parser.add_argument("--commit",default="HEAD"); parser.add_argument("--prepare",action="store_true"); parser.add_argument("--activate",action="store_true")
    parser.add_argument("--rollback-record"); args=parser.parse_args()
    selected=sum((args.prepare,args.activate,bool(args.rollback_record)))
    if selected>1: raise SystemExit("choose one mutating mode")
    sha=full_commit(Path(args.repo),args.commit)
    if args.rollback_record: result=rollback_activation(args.rollback_record,args.runtime_root,args.systemd_dir,apply=True)
    elif args.activate: result=activate(args.runtime_root,args.systemd_dir,sha,apply=True)
    else: result=prepare(args.repo,args.runtime_root,sha,apply=args.prepare)
    print(json.dumps(result,sort_keys=True))
    if not selected: print("Dry run only; no release, symlink, unit, or service state was changed.")


if __name__ == "__main__": main()
