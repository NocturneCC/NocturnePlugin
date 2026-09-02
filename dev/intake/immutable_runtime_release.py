"""Prepare and activate immutable Nocturne runtime releases; dry-run by default."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from uuid import uuid4

PURPOSE = "nocturne-immutable-runtime-v1"
VENV_PURPOSE = "nocturne-runtime-venv-v1"
VENV_NAME = "python3.14-gunicorn-26.2.0"
VENV_MARKER = "PREPARATION_INCOMPLETE"
VENV_MANIFEST = "VENV-MANIFEST.json"
GUNICORN_VERSION = "26.2.0"
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


def _write_json_fsync(path, value, mode, uid, gid):
    path = Path(path)
    with path.open("x") as output:
        output.write(json.dumps(value, sort_keys=True) + "\n")
        output.flush(); os.fsync(output.fileno())
    os.chown(path, uid, gid); path.chmod(mode)
    directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(directory_fd)
    finally: os.close(directory_fd)


def command(args, **kwargs):
    return subprocess.run(args, check=True, timeout=60, **kwargs)


def _regular_file(path, description):
    try: metadata = path.lstat()
    except FileNotFoundError as error: raise ValueError(f"unsafe {description}") from error
    if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
        raise ValueError(f"unsafe {description}")
    return metadata


def _system_python(path):
    path = Path(path)
    try: resolved = path.resolve(strict=True)
    except (FileNotFoundError, RuntimeError) as error:
        raise ValueError("system Python is missing or unsafe") from error
    metadata = resolved.lstat()
    if not stat.S_ISREG(metadata.st_mode) or not os.access(path, os.X_OK):
        raise ValueError("system Python is missing or unsafe")
    return resolved


def _safe_input_file(path, description, uid, gid):
    metadata = _regular_file(path, description)
    if ((metadata.st_uid, metadata.st_gid) != (uid, gid) or
            stat.S_IMODE(metadata.st_mode) & 0o022):
        raise ValueError(f"unsafe {description} metadata")


def _safe_acl(root, run=command):
    result = run(["getfacl", "-Rcp", str(root)], stdout=subprocess.PIPE, text=True)
    if any(line.startswith("default:") or
           (line.startswith("user:") and not line.startswith("user::")) or
           (line.startswith("group:") and not line.startswith("group::"))
           for line in result.stdout.splitlines()):
        raise ValueError("runtime venv has unsafe named or default ACLs")


def _inside(path, root):
    return path == root or root in path.parents


def _safe_owned_directory(path, mode, uid, gid, run=command):
    path = Path(path); metadata = path.lstat()
    if (path.is_symlink() or not stat.S_ISDIR(metadata.st_mode) or
            (metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode)) !=
            (uid, gid, mode) or os.path.ismount(path)):
        raise ValueError("unsafe runtime directory")
    _safe_acl(path, run)


def _validate_venv_tree(venv, *, uid=0, gid=0, approved_python=None, run=command):
    venv = Path(venv)
    metadata = venv.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or venv.is_symlink():
        raise ValueError("runtime venv target is not a regular directory")
    if (metadata.st_uid, metadata.st_gid) != (uid, gid):
        raise ValueError("runtime venv ownership mismatch")
    if os.path.ismount(venv) or metadata.st_dev != venv.parent.stat().st_dev:
        raise ValueError("runtime venv target is a mount")
    _safe_acl(venv, run)
    root = venv.resolve(strict=True)
    approved = {Path(value).resolve(strict=True) for value in (approved_python or ())}
    for path in [venv, *venv.rglob("*")]:
        item = path.lstat()
        if not path.is_symlink() and os.path.ismount(path):
            raise ValueError("runtime venv contains a mount")
        if (item.st_uid, item.st_gid) != (uid, gid):
            raise ValueError("runtime venv contains unexpected ownership")
        if stat.S_ISLNK(item.st_mode):
            try:
                resolved = path.resolve(strict=True)
            except (FileNotFoundError, RuntimeError) as error:
                raise ValueError("runtime venv contains a dangling symlink") from error
            if not _inside(resolved, root) and resolved not in approved:
                raise ValueError("runtime venv symlink escapes approved targets")
        elif stat.S_ISDIR(item.st_mode) or stat.S_ISREG(item.st_mode):
            if item.st_mode & 0o022:
                raise ValueError("runtime venv contains group/other-writable content")
        else:
            raise ValueError("runtime venv contains an unsupported file type")


def _venv_manifest(venv, python, lock, wheel):
    return {
        "purpose": VENV_PURPOSE,
        "target": str(Path(venv)),
        "python": str(Path(python).resolve(strict=True)),
        "requirements_sha256": digest(lock),
        "wheel_sha256": digest(wheel),
        "gunicorn_version": GUNICORN_VERSION,
    }


def _validate_venv_runtime(venv, release, python, lock, wheel, *, uid=0, gid=0,
                           run=command, allow_incomplete=False):
    venv, release = Path(venv), Path(release)
    approved_python = {Path(python).resolve(strict=True)}
    _validate_venv_tree(venv, uid=uid, gid=gid, approved_python=approved_python, run=run)
    interpreter = venv / "bin/python"
    if not interpreter.exists() or not os.access(interpreter, os.X_OK):
        raise ValueError("runtime venv interpreter is missing")
    probe = run([str(interpreter), "-B", "-c",
                 "import pathlib,sys;print(pathlib.Path(sys.executable).resolve());print(pathlib.Path(sys.prefix).resolve())"],
                stdout=subprocess.PIPE, text=True)
    lines = probe.stdout.splitlines()
    if lines != [str(Path(python).resolve(strict=True)), str(venv.resolve(strict=True))]:
        raise ValueError("runtime venv interpreter path mismatch")
    run([str(interpreter), "-B", "-m", "pip", "check"], stdout=subprocess.PIPE, text=True)
    version = run([str(interpreter), "-B", "-m", "gunicorn", "--version"],
                  stdout=subprocess.PIPE, text=True).stdout.strip()
    if version != f"gunicorn (version {GUNICORN_VERSION})":
        raise ValueError("runtime gunicorn version mismatch")
    environment = os.environ.copy()
    environment.update({"PYTHONPATH": str(release / "dev/intake"), "PYTHONDONTWRITEBYTECODE": "1"})
    run([str(interpreter), "-B", "-c", "import intake; import pending_writer"],
        cwd=release / "dev/intake", env=environment, stdout=subprocess.PIPE, text=True)
    expected = _venv_manifest(venv, python, lock, wheel)
    manifest_path = venv / VENV_MANIFEST
    _regular_file(manifest_path, "runtime venv manifest")
    if json.loads(manifest_path.read_text()) != expected:
        raise ValueError("runtime venv manifest mismatch")
    if not allow_incomplete and ((venv / VENV_MARKER).exists() or
                                 (venv / VENV_MARKER).is_symlink()):
        raise ValueError("runtime venv preparation is incomplete")
    return expected


def prepare_venv(runtime_root, release, python, lock, wheel, *, apply=False,
                 uid=0, gid=0, run=command, fail=None):
    runtime_root, release = Path(runtime_root), Path(release)
    python, lock, wheel = map(Path, (python, lock, wheel))
    target = runtime_root / "venvs" / VENV_NAME
    report = {"dry_run": not apply, "target": str(target), "state": "not_prepared"}
    _system_python(python)
    for path, label in ((lock, "requirements lock"), (wheel, "wheel")):
        _safe_input_file(path, label, uid, gid)
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_dir():
            raise ValueError("runtime venv target exists in an unknown state")
        marker = target / VENV_MARKER
        if marker.exists() or marker.is_symlink():
            raise ValueError("runtime venv preparation is incomplete; explicit recovery is required")
        _validate_venv_runtime(target, release, python, lock, wheel, uid=uid, gid=gid, run=run)
        return {**report, "state": "already_prepared"}
    if not apply:
        return report
    parent = target.parent
    parent.mkdir(parents=True, mode=0o755, exist_ok=True)
    _safe_owned_directory(runtime_root, 0o755, uid, gid, run)
    _safe_owned_directory(parent, 0o755, uid, gid, run)
    target.mkdir(mode=0o755)
    os.chown(target, uid, gid)
    marker = target / VENV_MARKER
    _write_json_fsync(marker, {"purpose": VENV_PURPOSE, "target": str(target)},
                      0o600, uid, gid)
    if fail: fail("after_incomplete_marker")
    # The final path is intentional: venv console scripts embed this absolute path.
    previous_umask = os.umask(0o022)
    try: run([str(python), "-B", "-m", "venv", str(target)])
    finally: os.umask(previous_umask)
    if fail: fail("after_venv_creation")
    interpreter = target / "bin/python"
    previous_umask = os.umask(0o022)
    try:
        run([str(interpreter), "-B", "-m", "pip", "install", "--disable-pip-version-check",
             "--no-index", "--find-links", str(wheel.parent), "--require-hashes", "--no-deps",
             "-r", str(lock)])
    finally: os.umask(previous_umask)
    if fail: fail("after_dependency_install")
    for path in target.rglob("*"):
        os.chown(path, uid, gid, follow_symlinks=False)
        if not path.is_symlink(): path.chmod((path.stat().st_mode & 0o777) & ~0o022)
    manifest = target / VENV_MANIFEST
    _write_json_fsync(manifest, _venv_manifest(target, python, lock, wheel),
                      0o644, uid, gid)
    _validate_venv_runtime(target, release, python, lock, wheel, uid=uid, gid=gid,
                           run=run, allow_incomplete=True)
    if fail: fail("after_validation")
    marker.unlink()
    directory_fd = os.open(target, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(directory_fd)
    finally: os.close(directory_fd)
    return {**report, "dry_run": False, "state": "prepared"}


def recover_incomplete_venv(runtime_root, target, *, apply=False, uid=0, gid=0, run=command):
    runtime_root, target = Path(runtime_root), Path(target)
    expected = runtime_root / "venvs" / VENV_NAME
    if target != expected or target.parent != runtime_root / "venvs":
        raise ValueError("recovery target is not the exact versioned runtime venv")
    for directory in (runtime_root, target.parent):
        try: _safe_owned_directory(directory, 0o755, uid, gid, run)
        except ValueError as error:
            raise ValueError(f"unsafe runtime venv recovery path: {error}") from error
    marker = target / VENV_MARKER
    completion = target / VENV_MANIFEST
    _validate_venv_tree(target, uid=uid, gid=gid,
                        approved_python={Path("/usr/bin/python3.14")}, run=run)
    recovery_kind = "marked"
    if marker.exists() or marker.is_symlink():
        marker_metadata = _regular_file(marker, "incomplete marker")
        if (marker_metadata.st_uid, marker_metadata.st_gid,
                stat.S_IMODE(marker_metadata.st_mode)) != (uid, gid, 0o600):
            raise ValueError("incomplete marker metadata mismatch")
        expected_marker = {"purpose": VENV_PURPOSE, "target": str(target)}
        if json.loads(marker.read_text()) != expected_marker:
            raise ValueError("incomplete marker contents mismatch")
    else:
        # One narrowly identified legacy state was produced by the original
        # preparation command: venv was renamed and its Gunicorn shebang still
        # names the now-absent .venv-<commit>.<pid> staging directory.
        launcher = target / "bin/gunicorn"
        _regular_file(launcher, "legacy Gunicorn launcher")
        first_line = launcher.read_text(errors="strict").splitlines()[0]
        pattern = (r"^#!" + re.escape(str(target.parent)) +
                   r"/\.venv-[0-9a-f]{40}\.[0-9]+/bin/python$")
        embedded = Path(first_line[2:]) if re.fullmatch(pattern, first_line) else None
        if embedded is None or embedded.exists() or completion.exists():
            raise ValueError("existing runtime venv is not a verified incomplete preparation")
        recovery_kind = "legacy_renamed_bad_interpreter"
    if completion.exists() or completion.is_symlink():
        _regular_file(completion, "incomplete venv manifest")
        value = json.loads(completion.read_text())
        if value.get("purpose") != VENV_PURPOSE or value.get("target") != str(target):
            raise ValueError("incomplete venv manifest mismatch")
    quarantine_parent = runtime_root / "quarantine"
    quarantine = quarantine_parent / "incomplete-venvs"
    report = {"dry_run": not apply, "target": str(target), "state": "verified_incomplete",
              "recovery_kind": recovery_kind}
    if not apply: return report
    for directory in (quarantine_parent, quarantine):
        if directory.exists() or directory.is_symlink():
            metadata = directory.lstat()
            if (directory.is_symlink() or not stat.S_ISDIR(metadata.st_mode) or
                    (metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode)) !=
                    (uid, gid, 0o700)):
                raise ValueError("unsafe runtime venv quarantine")
            _safe_acl(directory, run)
        else:
            directory.mkdir(mode=0o700)
            os.chown(directory, uid, gid); os.chmod(directory, 0o700)
    if quarantine.stat().st_dev != target.stat().st_dev or os.path.ismount(quarantine):
        raise ValueError("quarantine is not on the runtime filesystem")
    destination = quarantine / (VENV_NAME + "-" + uuid4().hex)
    os.replace(target, destination)
    return {**report, "dry_run": False, "state": "quarantined", "quarantine": str(destination)}


def select_venv(runtime_root, target, *, apply=False):
    runtime_root, target = Path(runtime_root), Path(target)
    expected = runtime_root / "venvs" / VENV_NAME
    if (target != expected or not target.is_dir() or target.is_symlink() or
            (target / VENV_MARKER).exists()):
        raise ValueError("only the completed versioned runtime venv may be selected")
    link = runtime_root / "venv"
    if link.is_symlink():
        if link.resolve(strict=True) != target.resolve(strict=True):
            raise ValueError("runtime venv selector points to another environment")
        return {"dry_run": not apply, "state": "already_selected", "target": str(target)}
    if link.exists(): raise ValueError("runtime venv selector is not a symlink")
    if not apply: return {"dry_run": True, "state": "not_selected", "target": str(target)}
    staged = runtime_root / (".venv-link-" + uuid4().hex)
    staged.symlink_to(Path("venvs") / VENV_NAME)
    os.replace(staged, link)
    return {"dry_run": False, "state": "selected", "target": str(target)}


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
        if name == "nocturne-plugin-dev.service":
            launcher = old_root + "/.venv/bin/gunicorn "
            if text.count("ExecStart=" + launcher) != 1:
                raise ValueError("intake Gunicorn launcher is missing or ambiguous")
            text = text.replace("ExecStart=" + launcher,
                                "ExecStart=" + old_root + "/.venv/bin/python -m gunicorn ", 1)
        text = text.replace(old_root + "/.venv", str(runtime_root / "venv"))
        text = text.replace(old_root, current)
        if old_root in text: raise ValueError(f"mutable checkout remains in unit: {name}")
        for line in REQUIRED_UNIT_LINES:
            if text.count(line) != 1: raise ValueError(f"unit sandbox line missing or ambiguous: {line}")
        if name == "nocturne-plugin-dev.service":
            expected_start = "ExecStart=" + str(runtime_root / "venv/bin/python") + " -m gunicorn "
            if text.count(expected_start) != 1: raise ValueError("intake module launcher changed")
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
    venv = Path(venv)
    if (venv / VENV_MARKER).exists() or (venv / VENV_MARKER).is_symlink():
        raise ValueError("runtime venv preparation is incomplete")
    python = venv / "bin/python"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("reproducible runtime venv is missing")
    version = run([str(python), "-B", "-m", "gunicorn", "--version"],
                  stdout=subprocess.PIPE, text=True).stdout.strip()
    if version != f"gunicorn (version {GUNICORN_VERSION})": raise ValueError("runtime gunicorn version mismatch")
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
    parser.add_argument("--prepare-venv", action="store_true")
    parser.add_argument("--check-venv", action="store_true")
    parser.add_argument("--recover-incomplete-venv")
    parser.add_argument("--apply-recovery", action="store_true")
    parser.add_argument("--python", default="/usr/bin/python3.14")
    parser.add_argument("--requirements-lock")
    parser.add_argument("--wheel")
    parser.add_argument("--rollback-record"); args=parser.parse_args()
    if args.apply_recovery and not args.recover_incomplete_venv:
        raise SystemExit("--apply-recovery requires --recover-incomplete-venv")
    selected=sum((args.prepare,args.activate,args.prepare_venv,args.check_venv,bool(args.rollback_record),
                  bool(args.recover_incomplete_venv)))
    if selected>1: raise SystemExit("choose one mutating mode")
    sha=full_commit(Path(args.repo),args.commit)
    if args.rollback_record: result=rollback_activation(args.rollback_record,args.runtime_root,args.systemd_dir,apply=True)
    elif args.activate: result=activate(args.runtime_root,args.systemd_dir,sha,apply=True)
    elif args.recover_incomplete_venv:
        result=recover_incomplete_venv(args.runtime_root,args.recover_incomplete_venv,
                                       apply=args.apply_recovery)
    elif args.prepare_venv or args.check_venv:
        if not args.requirements_lock or not args.wheel:
            raise SystemExit("--prepare-venv requires --requirements-lock and --wheel")
        release=Path(args.runtime_root)/"releases"/sha
        verify_release(release,sha)
        result=prepare_venv(args.runtime_root,release,args.python,args.requirements_lock,
                            args.wheel,apply=args.prepare_venv)
        if args.prepare_venv:
            result["selection"] = select_venv(args.runtime_root,
                                               Path(args.runtime_root)/"venvs"/VENV_NAME,
                                               apply=True)
    else: result=prepare(args.repo,args.runtime_root,sha,apply=args.prepare)
    print(json.dumps(result,sort_keys=True))
    if (not selected or args.check_venv or
            (args.recover_incomplete_venv and not args.apply_recovery)):
        print("Dry run only; no release, venv, symlink, unit, or service state was changed.")


if __name__ == "__main__": main()
