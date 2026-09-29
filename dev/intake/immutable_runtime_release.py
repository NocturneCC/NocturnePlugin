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
import time
from uuid import uuid4

from derived_review_support import (_apply_metadata, _capture_safe_metadata,
                                    _verify_metadata)
from deployment_trust import verify_checkout
from emoji_route_support import candidate_site
from runtime_identity import (
    GUNICORN_LOCK_SHA256, GUNICORN_LOCK_TEXT, GUNICORN_RUNTIME_NAME,
    GUNICORN_VERSION, GUNICORN_WHEEL_NAME, GUNICORN_WHEEL_SHA256,
    LEGACY_GUNICORN_LOCK_SHA256, LEGACY_GUNICORN_RUNTIME_NAME,
    PILLOW_LOCK_SHA256, PILLOW_LOCK_TEXT, PILLOW_RUNTIME_NAME,
    PILLOW_VERSION, PILLOW_WHEEL_SHA256, PYTHON_MACHINE, PYTHON_SOABI,
    PYTHON_VERSION,
)

PURPOSE = "nocturne-immutable-runtime-v1"
VENV_PURPOSE = "nocturne-runtime-venv-v2"
LEGACY_VENV_PURPOSE = "nocturne-runtime-venv-v1"
VENV_NAME = GUNICORN_RUNTIME_NAME
LEGACY_VENV_NAME = LEGACY_GUNICORN_RUNTIME_NAME
VENV_MARKER = "PREPARATION_INCOMPLETE"
VENV_MANIFEST = "VENV-MANIFEST.json"
CORE_UNITS = ("nocturne-plugin-writer.service", "nocturne-plugin-dev.service")
EMOJI_UNITS = ("nocturne-plugin-emoji-sync.service",
               "nocturne-plugin-emoji-sync.timer")
UNITS = CORE_UNITS + EMOJI_UNITS
UNIT_MANIFEST = "STAGED-UNITS-MANIFEST.json"
ROUTE_MANIFEST = "STAGED-NGINX-MANIFEST.json"
UNIT_STAGE_PURPOSE = "nocturne-commit-scoped-units-v1"
ROUTE_STAGE_PURPOSE = "nocturne-commit-scoped-emoji-route-v1"
EMOJI_VENV_NAME = PILLOW_RUNTIME_NAME
EMOJI_WHEEL_SHA256 = PILLOW_WHEEL_SHA256
EMOJI_ROUTE = "nginx-emojis-location.conf"
ACTIVATION_CONFIRMATION = (
    "intake, writer, emoji synchronizer, emoji timer, and Nginx reload activity "
    "must be quiesced for activation or rollback")
SERVICE_UNITS = ("nocturne-plugin-dev.service", "nocturne-plugin-writer.service",
                 "nocturne-plugin-emoji-sync.service", "nocturne-plugin-emoji-sync.timer")
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


def _fsync_file(path):
    with Path(path).open("rb") as value:
        os.fsync(value.fileno())


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)


def _replace_json_fsync(path, value, mode, uid, gid):
    path = Path(path)
    descriptor, name = tempfile.mkstemp(prefix=".activation-record-", dir=path.parent)
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "w") as output:
            output.write(json.dumps(value, sort_keys=True) + "\n")
            output.flush(); os.fsync(output.fileno())
        os.chown(staged, uid, gid); staged.chmod(mode); _fsync_file(staged)
        os.replace(staged, path); _fsync_directory(path.parent)
    finally:
        staged.unlink(missing_ok=True)


def command(args, **kwargs):
    return subprocess.run(args, check=True, timeout=60, **kwargs)


def _systemd_unit_state(name, run=command):
    result = run(["systemctl", "show", name, "--no-pager",
                  "--property=Id", "--property=LoadState",
                  "--property=ActiveState", "--property=SubState",
                  "--property=MainPID"], stdout=subprocess.PIPE, text=True)
    fields = {}
    for line in result.stdout.splitlines():
        if "=" not in line:
            raise ValueError("systemd service-state output is malformed")
        key, value = line.split("=", 1)
        if key in fields:
            raise ValueError("systemd service-state output has duplicate fields")
        fields[key] = value
    base_fields = {"Id", "LoadState", "ActiveState", "SubState"}
    observed_fields = set(fields)
    if name.endswith(".service"):
        if observed_fields != base_fields | {"MainPID"}:
            raise ValueError("systemd service-state output is incomplete")
    elif name.endswith(".timer"):
        if (not base_fields <= observed_fields
                or not observed_fields <= base_fields | {"MainPID"}):
            raise ValueError("systemd service-state output is incomplete")
    else:
        raise ValueError(f"unsupported systemd unit type: {name}")
    if fields["Id"] != name:
        raise ValueError(f"required unit identity is invalid: {name}")
    return fields


def _safely_inactive(name, fields):
    allowed_load = {"loaded"} if name in CORE_UNITS else {"loaded", "not-found"}
    return (fields["LoadState"] in allowed_load
            and fields["ActiveState"] == "inactive" and fields["SubState"] == "dead"
            and ("MainPID" not in fields or fields["MainPID"] == "0"))


def verify_inactive_services(run=command):
    evidence = {}
    for name in SERVICE_UNITS:
        fields = _systemd_unit_state(name, run)
        if not _safely_inactive(name, fields):
            raise ValueError(f"required unit is not inactive: {name}")
        evidence[name] = fields
    return evidence


def prepare_rollback_service_state(run=command):
    """Clear only a stopped failed emoji oneshot before committed rollback.

    This helper never restores files or selectors.  It deliberately skips an
    absent timer and returns only after the ordinary immutable rollback guard
    verifies the complete four-unit set as safely inactive.
    """
    for name in SERVICE_UNITS:
        fields = _systemd_unit_state(name, run)
        if _safely_inactive(name, fields):
            continue
        failed_oneshot = (
            name == "nocturne-plugin-emoji-sync.service"
            and fields["LoadState"] == "loaded"
            and fields["ActiveState"] == "failed"
            and fields["SubState"] == "failed"
            and fields.get("MainPID") == "0"
        )
        if not failed_oneshot:
            raise ValueError(f"required unit is not safely resettable: {name}")
        run(["systemctl", "reset-failed", name])
    return verify_inactive_services(run)


def wait_for_writer_socket(path, *, attempts=40, delay=0.25, expected_uid=None,
                           expected_gid=None, expected_mode=0o666,
                           sleep=time.sleep):
    """Wait a bounded interval for the exact pending-writer socket."""
    if type(attempts) is not int or attempts < 1 or not 0 <= delay <= 5:
        raise ValueError("invalid writer readiness bound")
    path = Path(path)
    for attempt in range(attempts):
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            metadata = None
        except OSError as error:
            raise ValueError("writer socket metadata is unavailable") from error
        if metadata is not None:
            if (path.is_symlink() or not stat.S_ISSOCK(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or stat.S_IMODE(metadata.st_mode) != expected_mode
                    or (expected_uid is not None and metadata.st_uid != expected_uid)
                    or (expected_gid is not None and metadata.st_gid != expected_gid)):
                raise ValueError("writer socket metadata is unsafe")
            return {"path": str(path), "uid": metadata.st_uid, "gid": metadata.st_gid,
                    "mode": f"{stat.S_IMODE(metadata.st_mode):04o}"}
        if attempt + 1 < attempts:
            sleep(delay)
    raise TimeoutError(f"writer socket did not become ready: {path}")


def wait_for_intake_health(probe, *, attempts=40, delay=0.25,
                           sleep=time.sleep):
    """Wait for a caller-supplied bounded local health probe to return True."""
    if not callable(probe) or type(attempts) is not int or attempts < 1 \
            or not 0 <= delay <= 5:
        raise ValueError("invalid intake readiness bound")
    for attempt in range(attempts):
        try:
            if probe() is True:
                return True
        except (OSError, RuntimeError):
            pass
        if attempt + 1 < attempts:
            sleep(delay)
    raise TimeoutError("intake did not become healthy within the readiness bound")


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
    if (path != resolved or not stat.S_ISREG(metadata.st_mode) or not os.access(path, os.X_OK)
            or (metadata.st_uid, metadata.st_gid) != (0, 0)
            or metadata.st_mode & 0o022):
        raise ValueError("system Python is missing or unsafe")
    return resolved


def _safe_input_file(path, description, uid, gid):
    metadata = _regular_file(path, description)
    if ((metadata.st_uid, metadata.st_gid) != (uid, gid) or
            stat.S_IMODE(metadata.st_mode) & 0o022):
        raise ValueError(f"unsafe {description} metadata")


def validate_requirements_lock(requirements, *, expected_lock, uid=0, gid=0,
                               run=command):
    requirements = Path(requirements)
    _safe_input_file(requirements, "requirements lock", uid, gid)
    lock_metadata = requirements.stat()
    if lock_metadata.st_nlink != 1 or stat.S_IMODE(lock_metadata.st_mode) != 0o444:
        raise ValueError("requirements lock must be an immutable regular file")
    _safe_acl(requirements, run)
    if requirements.read_text() != expected_lock:
        raise ValueError("requirements lock content mismatch")
    return requirements


def validate_runtime_wheel(wheel, *, expected_name, expected_sha256,
                           expected_directory, uid=0, gid=0, run=command):
    wheel = Path(wheel)
    parent = wheel.parent
    if parent.name != expected_directory:
        raise ValueError("wheel is not in its exact versioned wheelhouse")
    _safe_directory_node(parent, {0o755}, uid, gid, run)
    entries = list(parent.iterdir())
    if len(entries) != 1 or entries[0].name != expected_name:
        raise ValueError("versioned wheelhouse has missing or extra entries")
    metadata = _regular_file(wheel, "runtime wheel")
    if (wheel.is_symlink() or wheel.name != expected_name or metadata.st_nlink != 1
            or os.path.ismount(wheel) or metadata.st_dev != parent.stat().st_dev
            or (metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode))
                != (uid, gid, 0o444)):
        raise ValueError("runtime wheel metadata mismatch")
    _safe_acl(wheel, run)
    if digest(wheel) != expected_sha256:
        raise ValueError("runtime wheel digest mismatch")
    return wheel


def validate_locked_wheel(requirements, wheel, *, expected_lock, expected_name,
                          expected_sha256, expected_directory, uid=0, gid=0,
                          run=command):
    requirements = validate_requirements_lock(
        requirements, expected_lock=expected_lock, uid=uid, gid=gid, run=run)
    wheel = validate_runtime_wheel(
        wheel, expected_name=expected_name, expected_sha256=expected_sha256,
        expected_directory=expected_directory, uid=uid, gid=gid, run=run)
    return requirements, wheel


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


def _safe_directory_node(path, modes, uid, gid, run=command):
    path = Path(path); metadata = path.lstat()
    if (path.is_symlink() or not stat.S_ISDIR(metadata.st_mode)
            or os.path.ismount(path)
            or (metadata.st_uid, metadata.st_gid) != (uid, gid)
            or stat.S_IMODE(metadata.st_mode) not in set(modes)):
        raise ValueError("unsafe runtime directory node")
    result = run(["getfacl", "-cp", str(path)], stdout=subprocess.PIPE, text=True)
    if any(line.startswith("default:")
           or (line.startswith("user:") and not line.startswith("user::"))
           or (line.startswith("group:") and not line.startswith("group::"))
           for line in result.stdout.splitlines()):
        raise ValueError("runtime directory node has unsafe ACLs")


def _create_runtime_directory(path, mode, uid, gid, *, run=command,
                              allow_existing=False):
    """Create one approved runtime directory independent of the caller umask."""
    path = Path(path)
    if path.exists() or path.is_symlink():
        if not allow_existing:
            raise ValueError("runtime directory target unexpectedly exists")
        _safe_directory_node(path, {mode}, uid, gid, run)
        return False
    old_umask = os.umask(0)
    try:
        try:
            path.mkdir(mode=mode)
        except FileExistsError as error:
            raise ValueError("runtime directory appeared during creation") from error
    finally:
        os.umask(old_umask)
    os.chown(path, uid, gid)
    os.chmod(path, mode)
    _safe_directory_node(path, {mode}, uid, gid, run)
    _fsync_directory(path.parent)
    return True


def _validate_venv_tree(venv, *, uid=0, gid=0, approved_python=None, run=command,
                        root_modes=frozenset({0o755})):
    venv = Path(venv)
    metadata = venv.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or venv.is_symlink():
        raise ValueError("runtime venv target is not a regular directory")
    if ((metadata.st_uid, metadata.st_gid) != (uid, gid)
            or stat.S_IMODE(metadata.st_mode) not in set(root_modes)):
        raise ValueError("runtime venv ownership or root mode mismatch")
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
            if stat.S_ISREG(item.st_mode) and item.st_nlink != 1:
                raise ValueError("runtime venv contains a hard-linked file")
            if item.st_mode & 0o022:
                raise ValueError("runtime venv contains group/other-writable content")
        else:
            raise ValueError("runtime venv contains an unsupported file type")


def _runtime_probe(interpreter, run=command):
    script = (
        "import importlib.metadata as m,json,pathlib,platform,sys,sysconfig;"
        "packages=sorted([[(d.metadata.get('Name') or '').lower().replace('_','-'),d.version] "
        "for d in m.distributions()]);"
        "print(json.dumps({'version':[sys.version_info.major,sys.version_info.minor],"
        "'machine':platform.machine(),'soabi':sysconfig.get_config_var('SOABI'),"
        "'executable':str(pathlib.Path(sys.executable).resolve()),"
        "'prefix':str(pathlib.Path(sys.prefix).resolve()),"
        "'packages':packages},sort_keys=True))")
    result = run([str(interpreter), "-B", "-c", script],
                 stdout=subprocess.PIPE, text=True)
    try:
        value = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("runtime identity probe returned invalid data") from error
    return value


def _validate_runtime_probe(value, venv, python):
    expected_keys = {"version", "machine", "soabi", "executable", "prefix", "packages"}
    packages = value.get("packages")
    if (set(value) != expected_keys or value.get("version") != PYTHON_VERSION
            or value.get("machine") != PYTHON_MACHINE
            or value.get("soabi") != PYTHON_SOABI
            or value.get("executable") != str(Path(python).resolve(strict=True))
            or value.get("prefix") != str(Path(venv).resolve(strict=True))
            or not isinstance(packages, list)
            or len(packages) != 2
            or packages[0] != ["gunicorn", GUNICORN_VERSION]
            or not isinstance(packages[1], list)
            or len(packages[1]) != 2
            or packages[1][0] != "pip"
            or not isinstance(packages[1][1], str)
            or not packages[1][1]):
        raise ValueError("runtime Python ABI, path, or installed package set mismatch")
    return value


def _validate_runtime_launchers(venv, python):
    venv, python = Path(venv), Path(python).resolve(strict=True)
    pip = venv / "bin/pip"
    if not pip.is_file() or pip.is_symlink() or pip.stat().st_nlink != 1:
        raise ValueError("runtime pip launcher is missing or unsafe")
    for launcher in (venv / "bin").iterdir():
        if launcher.name in {"python", "python3", "python3.14"}:
            if not launcher.is_symlink() or launcher.resolve(strict=True) != python:
                raise ValueError("runtime interpreter launcher is not exact")
        elif launcher.is_file() and not launcher.is_symlink() and os.access(launcher, os.X_OK):
            with launcher.open("rb") as source:
                first = source.readline(4096)
            allowed = {("#!" + str(venv / "bin/python") + "\n").encode(),
                       ("#!" + str(venv / "bin/python3.14") + "\n").encode()}
            if first not in allowed:
                raise ValueError("runtime launcher does not name its final venv")


def _venv_manifest(venv, python, lock, wheel, probe):
    return {
        "schema_version": 2,
        "purpose": VENV_PURPOSE,
        "target": str(Path(venv)),
        "python": str(Path(python).resolve(strict=True)),
        "python_version": PYTHON_VERSION,
        "soabi": PYTHON_SOABI,
        "machine": PYTHON_MACHINE,
        "requirements_sha256": digest(lock),
        "wheel_sha256": digest(wheel),
        "gunicorn_version": GUNICORN_VERSION,
        "installed_packages": probe["packages"],
    }


def _validate_venv_runtime(venv, release, python, lock, wheel, *, uid=0, gid=0,
                           run=command, allow_incomplete=False,
                           root_modes=frozenset({0o755})):
    venv, release = Path(venv), Path(release)
    approved_python = {Path(python).resolve(strict=True)}
    _validate_venv_tree(venv, uid=uid, gid=gid, approved_python=approved_python,
                        run=run, root_modes=root_modes)
    interpreter = venv / "bin/python"
    if not interpreter.exists() or not os.access(interpreter, os.X_OK):
        raise ValueError("runtime venv interpreter is missing")
    probe = _validate_runtime_probe(_runtime_probe(interpreter, run), venv, python)
    run([str(interpreter), "-B", "-m", "pip", "check"], stdout=subprocess.PIPE, text=True)
    version = run([str(interpreter), "-B", "-m", "gunicorn", "--version"],
                  stdout=subprocess.PIPE, text=True).stdout.strip()
    if version != f"gunicorn (version {GUNICORN_VERSION})":
        raise ValueError("runtime gunicorn version mismatch")
    environment = os.environ.copy()
    environment.update({"PYTHONPATH": str(release / "dev/intake"), "PYTHONDONTWRITEBYTECODE": "1"})
    run([str(interpreter), "-B", "-c", "import intake; import pending_writer"],
        cwd=release / "dev/intake", env=environment, stdout=subprocess.PIPE, text=True)
    _validate_runtime_launchers(venv, python)
    expected = _venv_manifest(venv, python, lock, wheel, probe)
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
    expected_lock = release / "dev/intake/runtime-requirements.lock"
    expected_wheel = runtime_root / "wheelhouse" / VENV_NAME / GUNICORN_WHEEL_NAME
    if lock != expected_lock or wheel != expected_wheel:
        raise ValueError("core runtime inputs are not the exact commit-scoped paths")
    _safe_directory_node(runtime_root, {0o755}, uid, gid, run)
    _safe_directory_node(runtime_root / "wheelhouse", {0o755}, uid, gid, run)
    report = {"dry_run": not apply, "target": str(target), "state": "not_prepared"}
    _system_python(python)
    lock, wheel = validate_locked_wheel(
        lock, wheel, expected_lock=GUNICORN_LOCK_TEXT,
        expected_name=GUNICORN_WHEEL_NAME,
        expected_sha256=GUNICORN_WHEEL_SHA256,
        expected_directory=VENV_NAME, uid=uid, gid=gid, run=run)
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
    _safe_owned_directory(runtime_root, 0o755, uid, gid, run)
    _create_runtime_directory(parent, 0o755, uid, gid, run=run,
                              allow_existing=True)
    _create_runtime_directory(target, 0o755, uid, gid, run=run)
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
    probe = _validate_runtime_probe(_runtime_probe(interpreter, run), target, python)
    _write_json_fsync(manifest, _venv_manifest(target, python, lock, wheel, probe),
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
    if not marker.exists() and not marker.is_symlink():
        raise ValueError("verified incomplete marker is required at the exact new runtime target")
    marker_metadata = _regular_file(marker, "incomplete marker")
    if (marker_metadata.st_uid, marker_metadata.st_gid,
            stat.S_IMODE(marker_metadata.st_mode)) != (uid, gid, 0o600):
        raise ValueError("incomplete marker metadata mismatch")
    expected_marker = {"purpose": VENV_PURPOSE, "target": str(target)}
    if json.loads(marker.read_text()) != expected_marker:
        raise ValueError("incomplete marker contents mismatch")
    if completion.exists() or completion.is_symlink():
        _regular_file(completion, "incomplete venv manifest")
        value = json.loads(completion.read_text())
        if value.get("purpose") != VENV_PURPOSE or value.get("target") != str(target):
            raise ValueError("incomplete venv manifest mismatch")
    quarantine_parent = runtime_root / "quarantine"
    quarantine = quarantine_parent / "incomplete-venvs"
    report = {"dry_run": not apply, "target": str(target), "state": "verified_incomplete",
              "recovery_kind": "marked_content_addressed_target"}
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
    for _attempt in range(32):
        reservation = quarantine / (VENV_NAME + "-" + uuid4().hex)
        try:
            reservation.mkdir(mode=0o700)
            break
        except FileExistsError:
            continue
    else:
        raise RuntimeError("could not reserve a unique runtime quarantine path")
    os.chown(reservation, uid, gid)
    destination = reservation / "runtime"
    try:
        os.rename(target, destination)
        _fsync_directory(quarantine)
    except BaseException:
        reservation.rmdir()
        raise
    return {**report, "dry_run": False, "state": "quarantined", "quarantine": str(destination)}


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
        if name in CORE_UNITS and text.count(old_root) < 1:
            raise ValueError(f"unit lacks mutable checkout path: {name}")
        if name == "nocturne-plugin-dev.service":
            launcher = old_root + "/.venv/bin/gunicorn "
            if text.count("ExecStart=" + launcher) != 1:
                raise ValueError("intake Gunicorn launcher is missing or ambiguous")
            text = text.replace("ExecStart=" + launcher,
                                "ExecStart=" + old_root + "/.venv/bin/python -m gunicorn ", 1)
        versioned_runtime = runtime_root / "venvs" / VENV_NAME
        text = text.replace(old_root + "/.venv", str(versioned_runtime))
        text = text.replace(old_root, current)
        # Emoji templates already use the canonical immutable root. Relocate
        # those paths too so an explicitly selected runtime root remains a
        # single coherent namespace (and disposable fixtures are meaningful).
        text = text.replace("/srv/nocturne-plugin/venvs/", str(runtime_root / "venvs") + "/")
        text = text.replace("/srv/nocturne-plugin/current", current)
        if old_root in text: raise ValueError(f"mutable checkout remains in unit: {name}")
        if name.endswith(".service"):
            for line in REQUIRED_UNIT_LINES:
                if text.count(line) != 1:
                    raise ValueError(f"unit sandbox line missing or ambiguous: {line}")
        if name == "nocturne-plugin-dev.service":
            expected_start = "ExecStart=" + str(versioned_runtime / "bin/python") + " -m gunicorn "
            if text.count(expected_start) != 1: raise ValueError("intake module launcher changed")
            allowlist = 'Environment="NOCTURNE_TEST_RSNS=Simons Alt,RoatBefAuJu"'
            if text.count(allowlist) != 1: raise ValueError("intake allowlist changed")
            for line in ("MemoryMax=160M", "TasksMax=32", "InaccessiblePaths=/srv/projects/database"):
                if text.count(line) != 1: raise ValueError(f"intake limit missing: {line}")
            for private_path in ("-/var/lib/nocturne-plugin-emojis",
                                 "/etc/nocturne-plugin/emoji-sync.json",
                                 "/etc/nocturne-plugin/credentials"):
                if text.count("InaccessiblePaths=" + private_path) != 1:
                    raise ValueError("intake private emoji state denial is missing")
            public_bind = "BindReadOnlyPaths=-/var/lib/nocturne-plugin-emojis/public:/run/nocturne-plugin-emojis"
            if text.count(public_bind) != 1:
                raise ValueError("intake public emoji mirror bind is missing or ambiguous")
            for forbidden in ("LoadCredential=", "%d/emoji-sync-config", "%d/discord-token"):
                if forbidden in text:
                    raise ValueError("intake unit exposes emoji private state")
        elif name == "nocturne-plugin-writer.service":
            for line in ("MemoryMax=128M", "TasksMax=24", "PrivateNetwork=yes",
                         "ReadWritePaths=/srv/projects/database"):
                if text.count(line) != 1: raise ValueError(f"writer limit missing: {line}")
        elif name == "nocturne-plugin-emoji-sync.service":
            expected_python = str(runtime_root / "venvs" / EMOJI_VENV_NAME / "bin/python")
            expected_code = current + "/dev/intake/emoji_sync.py"
            if text.count("ExecStart=" + expected_python + " -B " + expected_code + " ") != 1:
                raise ValueError("emoji synchronizer does not use versioned runtime and immutable code")
            expected_initialize = ("ExecStartPre=" + expected_python + " -B " + expected_code
                                   + " --initialize-output ")
            if text.count(expected_initialize) != 1:
                raise ValueError("emoji public output initializer is missing or ambiguous")
            for line in ("LoadCredential=emoji-sync-config:",
                         "LoadCredential=discord-token:",
                         "StateDirectory=nocturne-plugin-emojis/public",
                         "StateDirectoryMode=0755",
                         "--config-file=${CREDENTIALS_DIRECTORY}/emoji-sync-config",
                         "--token-file=${CREDENTIALS_DIRECTORY}/discord-token"):
                if text.count(line) != 1:
                    raise ValueError(f"emoji synchronizer boundary missing: {line}")
            if "%d/" in text:
                raise ValueError("emoji synchronizer uses a noncanonical credential path")
        elif name == "nocturne-plugin-emoji-sync.timer":
            if text.count("Unit=nocturne-plugin-emoji-sync.service") != 1:
                raise ValueError("emoji timer target is missing or ambiguous")
        if old_root in text:
            raise ValueError(f"mutable checkout remains in generated unit: {name}")
        target = generated / name; target.write_text(text); target.chmod(0o444)


def _artifact_manifest(purpose, commit, release, artifacts, uid, gid):
    return {"schema_version": 1, "purpose": purpose, "commit": commit,
            "release_manifest_sha256": digest(release / "RELEASE-MANIFEST.json"),
            "directory": {"uid": uid, "gid": gid, "mode": "0555", "acl": "basic"},
            "manifest": {"uid": uid, "gid": gid, "mode": "0444", "acl": "basic"},
            "artifacts": {name: {"sha256": digest(path), "uid": uid, "gid": gid,
                                  "mode": "0444", "acl": "basic"}
                          for name, path in sorted(artifacts.items())}}


def _verify_stage(path, manifest_name, purpose, commit):
    path = Path(path); manifest_path = path / manifest_name
    if path.is_symlink() or not path.is_dir() or manifest_path.is_symlink() \
            or not manifest_path.is_file():
        raise ValueError("staged deployment artifact is missing or unsafe")
    _safe_acl(path)
    value = json.loads(manifest_path.read_text())
    expected_keys = {"schema_version", "purpose", "commit",
                     "release_manifest_sha256", "directory", "manifest", "artifacts"}
    if purpose == UNIT_STAGE_PURPOSE:
        expected_keys.add("emoji_runtime")
        expected_keys.add("core_runtime")
    if (set(value) != expected_keys or value.get("schema_version") != 1
            or value.get("purpose") != purpose or value.get("commit") != commit):
        raise ValueError("staged deployment manifest lineage mismatch")
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("staged deployment manifest is malformed")
    actual = {item.name for item in path.iterdir()}
    if actual != set(artifacts) | {manifest_name}:
        raise ValueError("staged deployment file set mismatch")
    metadata = path.lstat()
    directory = value.get("directory")
    if ((metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode)) !=
            (directory.get("uid"), directory.get("gid"), 0o555)
            or directory.get("mode") != "0555" or directory.get("acl") != "basic"):
        raise ValueError("staged deployment directory metadata mismatch")
    manifest_metadata = manifest_path.stat()
    manifest_expectation = value.get("manifest")
    if ((manifest_metadata.st_uid, manifest_metadata.st_gid,
         stat.S_IMODE(manifest_metadata.st_mode)) !=
            (manifest_expectation.get("uid"), manifest_expectation.get("gid"), 0o444)
            or manifest_expectation.get("mode") != "0444"
            or manifest_expectation.get("acl") != "basic"):
        raise ValueError("staged deployment manifest metadata mismatch")
    for name, expected in artifacts.items():
        if (not isinstance(expected, dict)
                or set(expected) != {"sha256", "uid", "gid", "mode", "acl"}
                or expected.get("mode") != "0444" or expected.get("acl") != "basic"):
            raise ValueError("staged deployment artifact metadata is malformed")
        item = path / name
        if (not item.is_file() or item.is_symlink() or item.stat().st_nlink != 1
                or digest(item) != expected["sha256"]
                or (item.stat().st_uid, item.stat().st_gid, stat.S_IMODE(item.stat().st_mode))
                    != (expected["uid"], expected["gid"], 0o444)):
            raise ValueError("staged deployment checksum mismatch")
    return value


def verify_staged_deployment(runtime_root, commit):
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("exact staged release commit is required")
    runtime_root = Path(runtime_root)
    release = runtime_root / "releases" / commit
    verify_release(release, commit)
    units = _verify_stage(runtime_root / "staged-units" / commit, UNIT_MANIFEST,
                          UNIT_STAGE_PURPOSE, commit)
    route = _verify_stage(runtime_root / "staged-nginx" / commit, ROUTE_MANIFEST,
                          ROUTE_STAGE_PURPOSE, commit)
    if ((route["directory"]["uid"], route["directory"]["gid"]) !=
            (units["directory"]["uid"], units["directory"]["gid"])):
        raise ValueError("staged deployment ownership lineage mismatch")
    verify_release_ownership(release, units["directory"]["uid"],
                             units["directory"]["gid"])
    expected_release = digest(release / "RELEASE-MANIFEST.json")
    if (units.get("release_manifest_sha256") != expected_release
            or route.get("release_manifest_sha256") != expected_release):
        raise ValueError("staged deployment release digest mismatch")
    return {"units": units, "route": route}


def _stage_directory(parent, target, artifacts, manifest_name, manifest, uid, gid):
    if target.exists() or target.is_symlink():
        return False
    _create_runtime_directory(parent, 0o755, uid, gid, allow_existing=True)
    temporary = Path(tempfile.mkdtemp(prefix=".stage-", dir=parent))
    try:
        for name, source in artifacts.items():
            destination = temporary / name
            shutil.copyfile(source, destination)
            os.chown(destination, uid, gid); destination.chmod(0o444)
        _write_json_fsync(temporary / manifest_name, manifest, 0o444, uid, gid)
        os.chown(temporary, uid, gid); temporary.chmod(0o555)
        os.replace(temporary, target)
        _fsync_directory(parent)
        return True
    finally:
        _discard_staging(temporary)


def stage_deployment(runtime_root, commit, *, apply=False, uid=0, gid=0):
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("exact staged release commit is required")
    runtime_root = Path(runtime_root); release = runtime_root / "releases" / commit
    _safe_directory_node(runtime_root, {0o755}, uid, gid)
    verify_release(release, commit)
    verify_release_ownership(release, uid, gid)
    unit_artifacts = {name: release / "deployment-units" / name for name in UNITS}
    route_artifacts = {EMOJI_ROUTE: release / "dev/intake" / EMOJI_ROUTE}
    unit_manifest = _artifact_manifest(UNIT_STAGE_PURPOSE, commit, release,
                                       unit_artifacts, uid, gid)
    unit_manifest["emoji_runtime"] = {
        "schema_version": 2,
        "purpose": "nocturne-emoji-runtime-v2",
        "path": str(runtime_root / "venvs" / EMOJI_VENV_NAME),
        "pillow": PILLOW_VERSION,
        "python_version": PYTHON_VERSION,
        "soabi": PYTHON_SOABI,
        "machine": PYTHON_MACHINE,
        "wheel_sha256": EMOJI_WHEEL_SHA256,
        "requirements_sha256": digest(
            release / "dev/intake/emoji-sync-requirements.txt")}
    unit_manifest["core_runtime"] = {
        "schema_version": 2,
        "purpose": VENV_PURPOSE,
        "path": str(runtime_root / "venvs" / VENV_NAME),
        "gunicorn": GUNICORN_VERSION,
        "python_version": PYTHON_VERSION,
        "soabi": PYTHON_SOABI,
        "machine": PYTHON_MACHINE,
        "wheel_sha256": GUNICORN_WHEEL_SHA256,
        "requirements_sha256": digest(
            release / "dev/intake/runtime-requirements.lock")}
    route_manifest = _artifact_manifest(ROUTE_STAGE_PURPOSE, commit, release,
                                        route_artifacts, uid, gid)
    targets = {"units": runtime_root / "staged-units" / commit,
               "nginx": runtime_root / "staged-nginx" / commit}
    if not apply:
        states = {}
        for key, target in targets.items(): states[key] = "present" if target.exists() else "absent"
        return {"dry_run": True, "commit": commit, "targets": {k: str(v) for k,v in targets.items()},
                "states": states}
    _stage_directory(targets["units"].parent, targets["units"], unit_artifacts,
                     UNIT_MANIFEST, unit_manifest, uid, gid)
    _stage_directory(targets["nginx"].parent, targets["nginx"], route_artifacts,
                     ROUTE_MANIFEST, route_manifest, uid, gid)
    verify_staged_deployment(runtime_root, commit)
    return {"dry_run": False, "commit": commit,
            "targets": {key: str(value) for key, value in targets.items()},
            "state": "prepared"}


def build_manifest(release, commit):
    files = {}
    for path in sorted(release.rglob("*")):
        if path.is_symlink(): raise ValueError("release contains a symlink")
        if path.is_file() and path.name != "RELEASE-MANIFEST.json":
            files[str(path.relative_to(release))] = digest(path)
    value = {"purpose": PURPOSE, "commit": commit, "files": files,
             "runtime_venv": {"path": "/srv/nocturne-plugin/venvs/" + VENV_NAME,
                              "requirements": "dev/intake/runtime-requirements.lock",
                              "requirements_sha256": GUNICORN_LOCK_SHA256,
                              "wheel_sha256": GUNICORN_WHEEL_SHA256,
                              "python_version": PYTHON_VERSION,
                              "soabi": PYTHON_SOABI,
                              "machine": PYTHON_MACHINE,
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


def verify_release_ownership(release, uid=0, gid=0):
    release = Path(release)
    _safe_acl(release)
    for path in [release, *release.rglob("*")]:
        value = path.lstat()
        expected_mode = 0o555 if stat.S_ISDIR(value.st_mode) else 0o444
        if (path.is_symlink() or os.path.ismount(path)
                or not (stat.S_ISDIR(value.st_mode) or stat.S_ISREG(value.st_mode))
                or (stat.S_ISREG(value.st_mode) and value.st_nlink != 1)
                or (value.st_uid, value.st_gid, stat.S_IMODE(value.st_mode))
                    != (uid, gid, expected_mode)):
            raise ValueError("immutable release ownership or mode mismatch")


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
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("release preparation requires an exact full commit SHA")
    verify_checkout(repo, revision)
    commit = full_commit(repo, revision, run)
    if commit != revision:
        raise ValueError("release preparation commit resolution changed")
    release = runtime_root / "releases" / commit
    report = {"dry_run": not apply, "commit": commit, "release": str(release),
              "current": str(runtime_root / "current"),
              "venv": str(runtime_root / "venvs" / VENV_NAME)}
    if release.exists():
        verify_release(release, commit); verify_release_ownership(release, uid, gid)
        validate_requirements_lock(
            release / "dev/intake/runtime-requirements.lock",
            expected_lock=GUNICORN_LOCK_TEXT, uid=uid, gid=gid, run=run)
        validate_requirements_lock(
            release / "dev/intake/emoji-sync-requirements.txt",
            expected_lock=PILLOW_LOCK_TEXT, uid=uid, gid=gid, run=run)
        return {**report, "state": "already_prepared"}
    if not apply: return {**report, "state": "not_prepared"}
    _safe_owned_directory(runtime_root, 0o755, uid, gid, run)
    releases = runtime_root / "releases"
    _create_runtime_directory(releases, 0o755, uid, gid, run=run,
                              allow_existing=True)
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
        validate_requirements_lock(
            staging / "dev/intake/runtime-requirements.lock",
            expected_lock=GUNICORN_LOCK_TEXT, uid=uid, gid=gid, run=run)
        validate_requirements_lock(
            staging / "dev/intake/emoji-sync-requirements.txt",
            expected_lock=PILLOW_LOCK_TEXT, uid=uid, gid=gid, run=run)
        if fail: fail("before_release_activation")
        os.replace(staging, release)
        _fsync_directory(releases)
        verify_release(release, commit)
        return {**report, "state": "prepared", "manifest_sha256": digest(release / "RELEASE-MANIFEST.json"),
                "file_count": len(manifest["files"])}
    finally:
        archive.unlink(missing_ok=True)
        _discard_staging(staging)


def validate_venv(venv, run=command, *, uid=0, gid=0,
                  root_modes=frozenset({0o755})):
    venv = Path(venv)
    if venv.name != VENV_NAME:
        raise ValueError("runtime venv does not match the requested lock identity")
    _validate_venv_tree(venv, uid=uid, gid=gid,
                        approved_python={Path("/usr/bin/python3.14")}, run=run,
                        root_modes=root_modes)
    if (venv / VENV_MARKER).exists() or (venv / VENV_MARKER).is_symlink():
        raise ValueError("runtime venv preparation is incomplete")
    python = venv / "bin/python"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("reproducible runtime venv is missing")
    probe = _validate_runtime_probe(
        _runtime_probe(python, run), venv, Path("/usr/bin/python3.14"))
    version = run([str(python), "-B", "-m", "gunicorn", "--version"],
                  stdout=subprocess.PIPE, text=True).stdout.strip()
    if version != f"gunicorn (version {GUNICORN_VERSION})": raise ValueError("runtime gunicorn version mismatch")
    run([str(python), "-B", "-m", "pip", "check"], stdout=subprocess.PIPE, text=True)
    _validate_runtime_launchers(venv, Path("/usr/bin/python3.14"))
    manifest = venv / VENV_MANIFEST
    if (not manifest.is_file() or manifest.is_symlink() or manifest.stat().st_nlink != 1):
        raise ValueError("runtime venv manifest is missing or unsafe")
    value = json.loads(manifest.read_text())
    if (set(value) != {"schema_version", "purpose", "target", "python",
                       "python_version", "soabi", "machine", "requirements_sha256",
                       "wheel_sha256", "gunicorn_version", "installed_packages"}
            or value.get("schema_version") != 2
            or value.get("purpose") != VENV_PURPOSE or value.get("target") != str(venv)
            or value.get("gunicorn_version") != GUNICORN_VERSION
            or value.get("python") != "/usr/bin/python3.14"
            or value.get("python_version") != PYTHON_VERSION
            or value.get("soabi") != PYTHON_SOABI
            or value.get("machine") != PYTHON_MACHINE
            or value.get("requirements_sha256") != GUNICORN_LOCK_SHA256
            or value.get("wheel_sha256") != GUNICORN_WHEEL_SHA256
            or value.get("installed_packages") != probe["packages"]):
        raise ValueError("runtime venv manifest mismatch")
    return value


def validate_legacy_venv(venv, run=command, *, uid=0, gid=0):
    """Validate the immutable predecessor without adopting it into the new identity."""
    venv = Path(venv)
    if venv.name != LEGACY_VENV_NAME:
        raise ValueError("legacy runtime path is not exact")
    _validate_venv_tree(venv, uid=uid, gid=gid,
                        approved_python={Path("/usr/bin/python3.14")}, run=run)
    if (venv / VENV_MARKER).exists() or (venv / VENV_MARKER).is_symlink():
        raise ValueError("legacy runtime is incomplete")
    python = venv / "bin/python"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("legacy runtime interpreter is missing")
    probe = _validate_runtime_probe(
        _runtime_probe(python, run), venv, Path("/usr/bin/python3.14"))
    version = run([str(python), "-B", "-m", "gunicorn", "--version"],
                  stdout=subprocess.PIPE, text=True).stdout.strip()
    if version != f"gunicorn (version {GUNICORN_VERSION})":
        raise ValueError("legacy runtime Gunicorn version mismatch")
    run([str(python), "-B", "-m", "pip", "check"],
        stdout=subprocess.PIPE, text=True)
    _validate_runtime_launchers(venv, Path("/usr/bin/python3.14"))
    manifest = venv / VENV_MANIFEST
    if (not manifest.is_file() or manifest.is_symlink() or manifest.stat().st_nlink != 1):
        raise ValueError("legacy runtime manifest is missing or unsafe")
    value = json.loads(manifest.read_text())
    if (set(value) != {"purpose", "target", "python", "requirements_sha256",
                       "wheel_sha256", "gunicorn_version"}
            or value.get("purpose") != LEGACY_VENV_PURPOSE
            or value.get("target") != str(venv)
            or value.get("python") != "/usr/bin/python3.14"
            or value.get("gunicorn_version") != GUNICORN_VERSION
            or value.get("wheel_sha256") != GUNICORN_WHEEL_SHA256
            or value.get("requirements_sha256") != LEGACY_GUNICORN_LOCK_SHA256
            or probe["packages"][0] != ["gunicorn", GUNICORN_VERSION]):
        raise ValueError("legacy runtime dependency record mismatch")
    return value


def _stage_bytes(target, raw, metadata):
    descriptor, name = tempfile.mkstemp(prefix=".nocturne-activate-", dir=target.parent)
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(raw); output.flush(); os.fsync(output.fileno())
        _apply_metadata(staged, metadata)
        return staged
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def _replace_bytes(target, raw, metadata):
    staged = _stage_bytes(target, raw, metadata)
    try:
        os.replace(staged, target)
        _fsync_directory(target.parent)
        _verify_metadata(target, metadata)
    finally:
        staged.unlink(missing_ok=True)


def _restore_record_entries(entries, record):
    for entry in reversed(entries):
        target = Path(entry["target"])
        if entry["existed"]:
            _replace_bytes(target, (record / entry["backup"]).read_bytes(), entry["metadata"])
        else:
            target.unlink(missing_ok=True)
            _fsync_directory(target.parent)


def _verify_predecessor_units(previous_release, systemd):
    generated = Path(previous_release) / "deployment-units"
    result = []
    for name in UNITS:
        source = generated / name
        target = Path(systemd) / name
        source_exists = source.is_file() and not source.is_symlink() and source.stat().st_nlink == 1
        target_exists = target.is_file() and not target.is_symlink() and target.stat().st_nlink == 1
        if source_exists:
            if not target_exists or digest(target) != digest(source):
                raise ValueError(f"active unit does not match the previous release: {name}")
        elif name in CORE_UNITS:
            raise ValueError(f"previous release lacks required core unit: {name}")
        elif target.exists() or target.is_symlink():
            raise ValueError(f"unexpected pre-existing emoji unit: {name}")
        result.append({"name": name, "existed": target_exists,
                       "sha256": digest(target) if target_exists else None})
    return result


def _prestate_digest(previous, unit_state, nginx_sha256, services):
    value = {"previous_commit": previous, "units": unit_state,
             "nginx_sha256": nginx_sha256, "services": services}
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _candidate_nginx_site(original, announcement, emoji):
    text = original.decode("utf-8", errors="strict")
    if "/api/plugin/v1/announcements" in text:
        return candidate_site(text, announcement, emoji).encode()
    marker = "    # Nocturne plugin development intake\n"
    if (text.count(marker) != 1 or "/api/plugin/v1/emojis" in text
            or "/api/plugin/v1/emojis/assets/" in text):
        raise ValueError("active Nginx site lacks a safe immutable route anchor")
    def indented(value):
        return "\n".join("    " + line if line else ""
                         for line in value.strip().splitlines()) + "\n"
    return text.replace(marker, marker + indented(announcement) + "\n" +
                        indented(emoji), 1).encode()


def _verify_activation_record_files(record, state):
    expected = {"ACTIVATION.json", "nginx.before", "nginx.after"}
    units = state.get("units")
    if isinstance(units, list):
        expected.update(entry.get("backup") for entry in units if entry.get("backup"))
    if {item.name for item in Path(record).iterdir()} != expected:
        raise ValueError("activation record file set mismatch")


def _validate_activation_state_shape(state, statuses):
    required = {"purpose", "status", "commit", "previous_commit",
                "release_manifest_sha256", "staged_unit_manifest_sha256",
                "staged_route_manifest_sha256", "prestate_sha256", "service_state",
                "nginx_reload_required", "units", "nginx"}
    unit_keys = {"name", "target", "existed", "before_sha256", "after_sha256",
                 "metadata", "backup"}
    nginx_keys = {"target", "before_sha256", "after_sha256", "metadata", "backup",
                  "applied"}
    digests = (state.get("release_manifest_sha256"),
               state.get("staged_unit_manifest_sha256"),
               state.get("staged_route_manifest_sha256"), state.get("prestate_sha256"))
    units = state.get("units", [])
    unit_values_valid = all(
        isinstance(entry.get("existed"), bool)
        and isinstance(entry.get("after_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", entry["after_sha256"])
        and ((entry["existed"] and isinstance(entry.get("before_sha256"), str)
              and re.fullmatch(r"[0-9a-f]{64}", entry["before_sha256"])
              and isinstance(entry.get("name"), str)
              and entry.get("backup") == entry.get("name", "") + ".before")
             or (not entry["existed"] and entry.get("before_sha256") is None
                 and entry.get("backup") is None))
        and isinstance(entry.get("metadata"), dict)
        for entry in units if isinstance(entry, dict))
    nginx = state.get("nginx", {})
    if (set(state) != required or state.get("purpose") != PURPOSE
            or state.get("status") not in statuses
            or state.get("nginx_reload_required") is not True
            or not re.fullmatch(r"[0-9a-f]{40}", state.get("commit", ""))
            or not re.fullmatch(r"[0-9a-f]{40}", state.get("previous_commit", ""))
            or any(not isinstance(value, str)
                   or not re.fullmatch(r"[0-9a-f]{64}", value) for value in digests)
            or not isinstance(state.get("service_state"), dict)
            or not isinstance(state.get("units"), list)
            or any(not isinstance(entry, dict) or set(entry) != unit_keys
                   for entry in units)
            or not unit_values_valid
            or not isinstance(state.get("nginx"), dict)
            or set(nginx) != nginx_keys
            or nginx.get("backup") != "nginx.before"
            or nginx.get("applied") != "nginx.after"
            or any(not isinstance(nginx.get(key), str)
                   or not re.fullmatch(r"[0-9a-f]{64}", nginx[key])
                   for key in ("before_sha256", "after_sha256"))
            or not isinstance(nginx.get("metadata"), dict)):
        raise ValueError("activation record schema is invalid")


def verify_applied_activation(record, runtime_root, systemd, commit, *,
                              allow_unit_drift=(), allow_nginx_drift=False,
                              nginx_target=None, uid=0, gid=0):
    """Authorize a narrow repair only inside a verified applied activation."""
    record, runtime_root, systemd = Path(record), Path(runtime_root), Path(systemd)
    records = runtime_root / "activation-records"
    if record.parent != records or record.is_symlink():
        raise ValueError("repair activation record is outside its exact store")
    _safe_directory_node(records, {0o700}, uid, gid)
    _safe_directory_node(record, {0o700}, uid, gid)
    state_path = record / "ACTIVATION.json"
    metadata = _regular_file(state_path, "repair activation record")
    if ((metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode),
         metadata.st_nlink) != (uid, gid, 0o600, 1)):
        raise ValueError("repair activation record metadata mismatch")
    state = json.loads(state_path.read_text())
    _validate_activation_state_shape(state, {"applied"})
    if state.get("commit") != commit:
        raise ValueError("repair requires the matching applied activation record")
    _verify_activation_record_files(record, state)
    release = runtime_root / "releases" / commit
    current = runtime_root / "current"
    if (not current.is_symlink() or current.readlink() != Path("releases") / commit
            or current.resolve(strict=True) != release.resolve(strict=True)
            or (current.lstat().st_uid, current.lstat().st_gid) != (uid, gid)):
        raise ValueError("repair release selector mismatch")
    verify_release(release, commit); verify_release_ownership(release, uid, gid)
    if digest(release / "RELEASE-MANIFEST.json") != state["release_manifest_sha256"]:
        raise ValueError("repair release manifest changed")
    verify_staged_deployment(runtime_root, commit)
    unit_stage = runtime_root / "staged-units" / commit
    route_stage = runtime_root / "staged-nginx" / commit
    if (digest(unit_stage / UNIT_MANIFEST) != state["staged_unit_manifest_sha256"]
            or digest(route_stage / ROUTE_MANIFEST) != state["staged_route_manifest_sha256"]):
        raise ValueError("repair staging record changed")
    allowed = set(allow_unit_drift)
    if not allowed <= set(EMOJI_UNITS):
        raise ValueError("repair cannot tolerate core-unit drift")
    entries = state.get("units")
    if not isinstance(entries, list) or [item.get("name") for item in entries] != list(UNITS):
        raise ValueError("repair activation unit set is invalid")
    for entry in entries:
        if entry["target"] != str(systemd / entry["name"]):
            raise ValueError("repair activation unit target mismatch")
        if entry["existed"]:
            backup = record / entry["backup"]
            if (not backup.is_file() or backup.is_symlink() or backup.stat().st_nlink != 1
                    or digest(backup) != entry["before_sha256"]):
                raise ValueError("repair activation unit backup changed")
        if entry["name"] in allowed:
            continue
        target = Path(entry["target"])
        if (not target.is_file() or target.is_symlink() or target.stat().st_nlink != 1
                or digest(target) != entry["after_sha256"]):
            raise ValueError("non-repair unit differs from applied activation")
    recorded_nginx = Path(state["nginx"].get("target", ""))
    if nginx_target is not None and recorded_nginx != Path(nginx_target):
        raise ValueError("repair Nginx target mismatch")
    if not allow_nginx_drift and (not recorded_nginx.is_file()
            or recorded_nginx.is_symlink() or recorded_nginx.stat().st_nlink != 1
            or digest(recorded_nginx) != state["nginx"]["after_sha256"]):
        raise ValueError("non-repair Nginx file differs from applied activation")
    for key, field in (("backup", "before_sha256"), ("applied", "after_sha256")):
        saved = record / state["nginx"][key]
        if (not saved.is_file() or saved.is_symlink() or saved.stat().st_nlink != 1
                or digest(saved) != state["nginx"][field]):
            raise ValueError("repair activation Nginx record changed")
    return state


def activate(runtime_root, systemd, commit, *, nginx_target=None, apply=False,
             venv=None, emoji_venv=None, fail=None, unit_uid=0, unit_gid=0,
             confirmed_services_stopped=False, expected_prestate_sha256=None,
             service_state_verifier=verify_inactive_services):
    runtime_root, systemd = Path(runtime_root), Path(systemd)
    _safe_directory_node(runtime_root, {0o755}, unit_uid, unit_gid)
    _safe_directory_node(systemd, {0o755}, unit_uid, unit_gid)
    venv = Path(venv or runtime_root / "venvs" / VENV_NAME)
    emoji_venv = Path(emoji_venv or runtime_root / "venvs" / EMOJI_VENV_NAME)
    release = runtime_root / "releases" / commit
    verify_release(release, commit); verify_release_ownership(release, unit_uid, unit_gid)
    core_record = validate_venv(venv, uid=unit_uid, gid=unit_gid)
    from emoji_runtime_release import validate_runtime as validate_emoji_runtime
    emoji_record = validate_emoji_runtime(emoji_venv, uid=unit_uid, gid=unit_gid)
    staged = verify_staged_deployment(runtime_root, commit)
    units_dir = runtime_root / "staged-units" / commit
    route_dir = runtime_root / "staged-nginx" / commit
    if set(staged["units"]["artifacts"]) != set(UNITS):
        raise ValueError("staged unit set is incomplete")
    runtime_requirement = staged["units"].get("emoji_runtime")
    if (not isinstance(runtime_requirement, dict)
            or runtime_requirement.get("schema_version") != 2
            or runtime_requirement.get("purpose") != emoji_record.get("purpose")
            or runtime_requirement.get("path") != str(emoji_venv)
            or runtime_requirement.get("pillow") != emoji_record.get("pillow_version")
            or runtime_requirement.get("python_version") != emoji_record.get("python_version")
            or runtime_requirement.get("soabi") != emoji_record.get("soabi")
            or runtime_requirement.get("machine") != emoji_record.get("machine")
            or runtime_requirement.get("wheel_sha256") != emoji_record.get("wheel_sha256")
            or runtime_requirement.get("requirements_sha256") !=
                emoji_record.get("requirements_sha256")):
        raise ValueError("emoji runtime does not match staged dependency record")
    core_requirement = staged["units"].get("core_runtime")
    if (not isinstance(core_requirement, dict)
            or core_requirement.get("schema_version") != 2
            or core_requirement.get("purpose") != core_record.get("purpose")
            or core_requirement.get("path") != str(venv)
            or core_requirement.get("gunicorn") != core_record.get("gunicorn_version")
            or core_requirement.get("python_version") != core_record.get("python_version")
            or core_requirement.get("soabi") != core_record.get("soabi")
            or core_requirement.get("machine") != core_record.get("machine")
            or core_requirement.get("wheel_sha256") != core_record.get("wheel_sha256")
            or core_requirement.get("requirements_sha256") !=
                core_record.get("requirements_sha256")):
        raise ValueError("core runtime does not match staged dependency record")
    for value in staged.values():
        directory = value.get("directory", {})
        if (directory.get("uid"), directory.get("gid")) != (unit_uid, unit_gid):
            raise ValueError("staged deployment is not root-owned")
    nginx_target = Path(nginx_target) if nginx_target else None
    if (nginx_target is None or not nginx_target.is_file() or nginx_target.is_symlink()
            or nginx_target.stat().st_nlink != 1):
        raise ValueError("safe active Nginx target is required")
    _safe_directory_node(nginx_target.parent, {0o700, 0o750, 0o755},
                         unit_uid, unit_gid)
    nginx_before = nginx_target.read_bytes()
    current = runtime_root / "current"
    if not current.is_symlink():
        raise ValueError("active immutable release selector is required")
    current_link = current.readlink()
    previous = current.resolve(strict=True).name
    if (not re.fullmatch(r"[0-9a-f]{40}", previous)
            or current_link != Path("releases") / previous
            or current.resolve(strict=True) != (runtime_root / "releases" / previous).resolve(strict=True)
            or (current.lstat().st_uid, current.lstat().st_gid) != (unit_uid, unit_gid)):
        raise ValueError("active immutable release selector is unsafe")
    if previous == commit:
        for name in UNITS:
            target = systemd / name
            if (not target.is_file() or target.is_symlink() or target.stat().st_nlink != 1
                    or digest(target) != digest(units_dir / name)):
                raise ValueError("already-active release has mismatched units")
        emoji_route = "\n".join(
            "    " + line if line else ""
            for line in (route_dir / EMOJI_ROUTE).read_text().strip().splitlines()) + "\n"
        if nginx_before.decode().count(emoji_route) != 1:
            raise ValueError("already-active release has mismatched Nginx route")
        services = service_state_verifier()
        return {"dry_run": not apply, "state": "already_active", "commit": commit,
                "previous_commit": commit, "units": len(UNITS),
                "nginx_target": str(nginx_target), "service_state": services,
                "nginx_reload_required": False}
    nginx_after = _candidate_nginx_site(
        nginx_before,
        (release / "dev/intake/nginx-announcements-location.conf").read_text(),
        (route_dir / EMOJI_ROUTE).read_text())
    previous_release = runtime_root / "releases" / previous
    verify_release(previous_release, previous)
    verify_release_ownership(previous_release, unit_uid, unit_gid)
    predecessor_units = _verify_predecessor_units(previous_release, systemd)
    default_metadata = {"uid": unit_uid, "gid": unit_gid, "mode": 0o644,
                        "acl": "user::rw-\ngroup::r--\nother::r--\n\n"}
    entries = []
    for name in UNITS:
        target = systemd / name
        if ((target.exists() or target.is_symlink())
                and (not target.is_file() or target.is_symlink()
                     or target.stat().st_nlink != 1)):
            raise ValueError(f"unsafe active unit: {target}")
        before = target.read_bytes() if target.exists() else None
        entries.append({"name": name, "target": str(target), "existed": before is not None,
                        "before_sha256": None if before is None else hashlib.sha256(before).hexdigest(),
                        "after_sha256": digest(units_dir / name),
                        "metadata": _capture_safe_metadata(target) if before is not None else default_metadata,
                        "backup": name + ".before" if before is not None else None})
    nginx_metadata = _capture_safe_metadata(nginx_target)
    services = service_state_verifier()
    prestate_sha256 = _prestate_digest(
        previous, predecessor_units, hashlib.sha256(nginx_before).hexdigest(), services)
    report = {"dry_run": not apply, "commit": commit, "previous_commit": previous,
              "units": len(entries), "nginx_target": str(nginx_target),
              "prestate_sha256": prestate_sha256,
              "required_stop_confirmation": ACTIVATION_CONFIRMATION,
              "nginx_reload_required": True}
    if not apply: return report
    if not confirmed_services_stopped:
        raise ValueError(ACTIVATION_CONFIRMATION)
    if (not isinstance(expected_prestate_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_prestate_sha256)
            or expected_prestate_sha256 != prestate_sha256):
        raise ValueError("activation requires the exact read-only prestate digest")
    if service_state_verifier() != services:
        raise ValueError("service state changed after activation preflight")
    if (not current.is_symlink() or current.readlink() != current_link
            or current.resolve(strict=True) != previous_release.resolve(strict=True)):
        raise ValueError("active release selector changed after activation preflight")
    records = runtime_root / "activation-records"
    if not records.exists():
        records.mkdir(mode=0o700); os.chown(records, unit_uid, unit_gid)
        _fsync_directory(runtime_root)
    _safe_directory_node(records, {0o700}, unit_uid, unit_gid)
    record = records / uuid4().hex
    record.mkdir(mode=0o700); os.chown(record, unit_uid, unit_gid)
    _fsync_directory(records)
    for entry in entries:
        if entry["existed"]:
            saved = record / entry["backup"]
            saved.write_bytes(Path(entry["target"]).read_bytes())
            _apply_metadata(saved, entry["metadata"])
            _fsync_file(saved)
    nginx_saved = record / "nginx.before"
    nginx_saved.write_bytes(nginx_before); _apply_metadata(nginx_saved, nginx_metadata)
    _fsync_file(nginx_saved)
    nginx_applied = record / "nginx.after"
    nginx_applied.write_bytes(nginx_after); _apply_metadata(nginx_applied, nginx_metadata)
    _fsync_file(nginx_applied)
    state = {"purpose": PURPOSE, "status": "prepared",
             "commit": commit, "previous_commit": previous,
             "release_manifest_sha256": digest(release / "RELEASE-MANIFEST.json"),
             "staged_unit_manifest_sha256": digest(units_dir / UNIT_MANIFEST),
             "staged_route_manifest_sha256": digest(route_dir / ROUTE_MANIFEST),
             "prestate_sha256": prestate_sha256, "service_state": services,
             "nginx_reload_required": True,
             "units": entries,
             "nginx": {"target": str(nginx_target),
                       "before_sha256": hashlib.sha256(nginx_before).hexdigest(),
                       "after_sha256": hashlib.sha256(nginx_after).hexdigest(),
                       "metadata": nginx_metadata, "backup": nginx_saved.name,
                       "applied": nginx_applied.name}}
    _write_json_fsync(record / "ACTIVATION.json", state, 0o600, unit_uid, unit_gid)
    _fsync_directory(record); _fsync_directory(records)
    old_link = current_link; changed = []
    try:
        if fail: fail("after_activation_record")
        if (not current.is_symlink() or current.readlink() != old_link
                or current.resolve(strict=True) != previous_release.resolve(strict=True)):
            raise ValueError("active release selector changed before activation")
        for entry in entries:
            target = Path(entry["target"])
            if entry["existed"]:
                if (not target.is_file() or target.is_symlink() or target.stat().st_nlink != 1
                        or digest(target) != entry["before_sha256"]):
                    raise ValueError("active unit changed before activation")
                _verify_metadata(target, entry["metadata"])
            elif target.exists() or target.is_symlink():
                raise ValueError("new unit target appeared before activation")
        if digest(nginx_target) != state["nginx"]["before_sha256"]:
            raise ValueError("active Nginx file changed before activation")
        _verify_metadata(nginx_target, nginx_metadata)
        link = runtime_root / (".current-" + uuid4().hex)
        link.symlink_to(Path("releases") / commit)
        if fail: fail("before_activation")
        os.replace(link, current)
        _fsync_directory(runtime_root)
        if fail: fail("after_symlink")
        for index, entry in enumerate(entries):
            changed.append(entry)
            _replace_bytes(Path(entry["target"]), (units_dir / entry["name"]).read_bytes(),
                           entry["metadata"])
            if fail: fail(f"after_unit_{index}")
        _replace_bytes(nginx_target, nginx_after, nginx_metadata)
        changed.append(state["nginx"])
        if fail: fail("after_nginx")
        state["status"] = "applied"
        _replace_json_fsync(record / "ACTIVATION.json", state, 0o600,
                            unit_uid, unit_gid)
        if fail: fail("after_activation_record_applied")
        return {**report, "dry_run": False, "activation_record": str(record)}
    except BaseException:
        try:
            restore_link = runtime_root / (".current-restore-" + uuid4().hex)
            restore_link.symlink_to(old_link); os.replace(restore_link, current)
            _fsync_directory(runtime_root)
            if state["nginx"] in changed:
                _replace_bytes(nginx_target, nginx_before, nginx_metadata)
                changed.remove(state["nginx"])
            _restore_record_entries(changed, record)
            state["status"] = "restored"
            _replace_json_fsync(record / "ACTIVATION.json", state, 0o600,
                                unit_uid, unit_gid)
        except BaseException as restore_error:
            raise RuntimeError(
                "activation failed and automatic restoration is incomplete; "
                f"recover from {record}") from restore_error
        raise


def recover_interrupted_activation(record, runtime_root, systemd, *,
                                   nginx_target=None, apply=False, fail=None,
                                   unit_uid=0, unit_gid=0,
                                   confirmed_services_stopped=False,
                                   expected_recovery_sha256=None,
                                   service_state_verifier=verify_inactive_services):
    """Restore the exact pre-activation state from a durable prepared record."""
    record, runtime_root, systemd = Path(record), Path(runtime_root), Path(systemd)
    records = runtime_root / "activation-records"
    _safe_directory_node(runtime_root, {0o755}, unit_uid, unit_gid)
    _safe_directory_node(systemd, {0o755}, unit_uid, unit_gid)
    _safe_directory_node(records, {0o700}, unit_uid, unit_gid)
    if record.parent != records or record.is_symlink():
        raise ValueError("activation record path is outside the runtime record store")
    _safe_directory_node(record, {0o700}, unit_uid, unit_gid)
    state_path = record / "ACTIVATION.json"
    metadata = _regular_file(state_path, "activation record")
    if ((metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode),
         metadata.st_nlink) != (unit_uid, unit_gid, 0o600, 1)):
        raise ValueError("activation record metadata mismatch")
    state = json.loads(state_path.read_text())
    _validate_activation_state_shape(
        state, {"prepared", "restored", "rollback_prepared"})
    _verify_activation_record_files(record, state)
    for value in (state["commit"], state["previous_commit"]):
        release = runtime_root / "releases" / value
        verify_release(release, value)
        verify_release_ownership(release, unit_uid, unit_gid)
    release = runtime_root / "releases" / state["commit"]
    if digest(release / "RELEASE-MANIFEST.json") != state["release_manifest_sha256"]:
        raise ValueError("activation release manifest changed")
    verify_staged_deployment(runtime_root, state["commit"])
    unit_stage = runtime_root / "staged-units" / state["commit"]
    route_stage = runtime_root / "staged-nginx" / state["commit"]
    if (digest(unit_stage / UNIT_MANIFEST) != state["staged_unit_manifest_sha256"]
            or digest(route_stage / ROUTE_MANIFEST) != state["staged_route_manifest_sha256"]):
        raise ValueError("activation staging record changed")
    if (not isinstance(state["units"], list)
            or [entry.get("name") for entry in state["units"]] != list(UNITS)):
        raise ValueError("activation unit record is incomplete or reordered")
    observed_units = []
    for entry in state["units"]:
        if (entry.get("target") != str(systemd / entry["name"])
                or entry.get("after_sha256") != digest(unit_stage / entry["name"])
                or entry.get("backup") !=
                    (entry["name"] + ".before" if entry.get("existed") else None)):
            raise ValueError("activation unit recovery lineage mismatch")
        if entry["existed"]:
            backup = record / entry["backup"]
            if (not backup.is_file() or backup.is_symlink() or backup.stat().st_nlink != 1
                    or digest(backup) != entry["before_sha256"]):
                raise ValueError("activation unit recovery backup mismatch")
        target = Path(entry["target"])
        if target.exists() or target.is_symlink():
            if not target.is_file() or target.is_symlink() or target.stat().st_nlink != 1:
                raise ValueError("unsafe interrupted unit state")
            raw = target.read_bytes(); current_digest = hashlib.sha256(raw).hexdigest()
            if current_digest not in {entry["before_sha256"], entry["after_sha256"]}:
                raise ValueError("interrupted unit is neither recorded state")
            observed_units.append((entry, raw, _capture_safe_metadata(target)))
        else:
            if entry["before_sha256"] is not None:
                raise ValueError("interrupted unit unexpectedly disappeared")
            observed_units.append((entry, None, None))
    nginx = state["nginx"]
    recorded_nginx = Path(nginx.get("target", ""))
    expected_nginx = Path(nginx_target) if nginx_target is not None else recorded_nginx
    saved_nginx = record / nginx.get("backup", "")
    applied_nginx = record / nginx.get("applied", "")
    if (recorded_nginx != expected_nginx or nginx.get("backup") != "nginx.before"
            or nginx.get("applied") != "nginx.after"
            or not saved_nginx.is_file() or saved_nginx.is_symlink()
            or saved_nginx.stat().st_nlink != 1
            or digest(saved_nginx) != nginx.get("before_sha256")
            or not applied_nginx.is_file() or applied_nginx.is_symlink()
            or applied_nginx.stat().st_nlink != 1
            or digest(applied_nginx) != nginx.get("after_sha256")):
        raise ValueError("activation Nginx recovery record is invalid")
    if (not recorded_nginx.is_file() or recorded_nginx.is_symlink()
            or recorded_nginx.stat().st_nlink != 1):
        raise ValueError("unsafe interrupted Nginx state")
    nginx_raw = recorded_nginx.read_bytes()
    if hashlib.sha256(nginx_raw).hexdigest() not in {
            nginx["before_sha256"], nginx["after_sha256"]}:
        raise ValueError("interrupted Nginx file is neither recorded state")
    nginx_metadata = _capture_safe_metadata(recorded_nginx)
    current = runtime_root / "current"
    if not current.is_symlink():
        raise ValueError("interrupted release selector is unsafe")
    current_name = current.resolve(strict=True).name
    if (current_name not in {state["commit"], state["previous_commit"]}
            or current.readlink() != Path("releases") / current_name
            or current.resolve(strict=True) !=
                (runtime_root / "releases" / current_name).resolve(strict=True)
            or (current.lstat().st_uid, current.lstat().st_gid) != (unit_uid, unit_gid)):
        raise ValueError("interrupted release selector is neither recorded state")
    services = service_state_verifier()
    observed = {
        "current": current_name,
        "units": [{"name": entry["name"], "sha256": None if raw is None else
                   hashlib.sha256(raw).hexdigest()} for entry, raw, _meta in observed_units],
        "nginx_sha256": hashlib.sha256(nginx_raw).hexdigest(), "services": services}
    recovery_sha256 = hashlib.sha256(json.dumps(
        observed, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    recover_applied = state["status"] == "rollback_prepared"
    restore_commit = state["commit"] if recover_applied else state["previous_commit"]
    report = {"dry_run": not apply, "state": state["status"],
              "commit": state["commit"], "restore_commit": restore_commit,
              "recovery_sha256": recovery_sha256,
              "required_stop_confirmation": ACTIVATION_CONFIRMATION}
    if state["status"] == "restored":
        if (current_name != state["previous_commit"]
                or any((hashlib.sha256(raw).hexdigest() if raw is not None else None)
                       != entry["before_sha256"]
                       for entry, raw, _meta in observed_units)
                or hashlib.sha256(nginx_raw).hexdigest() != nginx["before_sha256"]):
            raise ValueError("restored activation state changed")
        return {**report, "already_restored": True}
    if not apply:
        return report
    if not confirmed_services_stopped:
        raise ValueError(ACTIVATION_CONFIRMATION)
    if (not isinstance(expected_recovery_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_recovery_sha256)
            or expected_recovery_sha256 != recovery_sha256):
        raise ValueError("recovery requires the exact read-only recovery digest")
    if service_state_verifier() != services:
        raise ValueError("service state changed after recovery preflight")
    old_link = current.readlink()
    try:
        replacement = runtime_root / (".current-recover-" + uuid4().hex)
        replacement.symlink_to(Path("releases") / restore_commit)
        os.replace(replacement, current); _fsync_directory(runtime_root)
        if fail: fail("after_recovery_symlink")
        for index, entry in enumerate(state["units"]):
            target = Path(entry["target"])
            if recover_applied:
                _replace_bytes(target, (unit_stage / entry["name"]).read_bytes(),
                               entry["metadata"])
            elif entry["existed"]:
                _replace_bytes(target, (record / entry["backup"]).read_bytes(),
                               entry["metadata"])
            else:
                target.unlink(missing_ok=True); _fsync_directory(target.parent)
            if fail: fail(f"after_recovery_unit_{index}")
        desired_nginx = applied_nginx if recover_applied else saved_nginx
        _replace_bytes(recorded_nginx, desired_nginx.read_bytes(), nginx["metadata"])
        if fail: fail("after_recovery_nginx")
        state["status"] = "applied" if recover_applied else "restored"
        _replace_json_fsync(state_path, state, 0o600, unit_uid, unit_gid)
        return {**report, "dry_run": False, "state": state["status"]}
    except BaseException:
        replacement = runtime_root / (".current-recovery-reinstate-" + uuid4().hex)
        replacement.symlink_to(old_link); os.replace(replacement, current)
        _fsync_directory(runtime_root)
        for entry, raw, old_metadata in observed_units:
            target = Path(entry["target"])
            if raw is None:
                target.unlink(missing_ok=True); _fsync_directory(target.parent)
            else:
                _replace_bytes(target, raw, old_metadata)
        _replace_bytes(recorded_nginx, nginx_raw, nginx_metadata)
        raise


def rollback_activation(record, runtime_root, systemd, *, nginx_target=None,
                        apply=False, fail=None, unit_uid=0, unit_gid=0,
                        confirmed_services_stopped=False,
                        service_state_verifier=verify_inactive_services):
    record, runtime_root, systemd = Path(record), Path(runtime_root), Path(systemd)
    _safe_directory_node(runtime_root, {0o755}, unit_uid, unit_gid)
    _safe_directory_node(systemd, {0o755}, unit_uid, unit_gid)
    records = runtime_root / "activation-records"
    _safe_directory_node(records, {0o700}, unit_uid, unit_gid)
    if record.parent != records or record.is_symlink():
        raise ValueError("activation record path is outside the runtime record store")
    _safe_directory_node(record, {0o700}, unit_uid, unit_gid)
    state_path = record / "ACTIVATION.json"
    state_metadata = _regular_file(state_path, "activation record")
    if ((state_metadata.st_uid, state_metadata.st_gid,
         stat.S_IMODE(state_metadata.st_mode), state_metadata.st_nlink)
            != (unit_uid, unit_gid, 0o600, 1)):
        raise ValueError("activation record metadata mismatch")
    state = json.loads(state_path.read_text())
    if state.get("purpose") != PURPOSE:
        raise ValueError("activation record purpose mismatch")
    _validate_activation_state_shape(state, {"applied", "rolled_back"})
    _verify_activation_record_files(record, state)
    current = runtime_root / "current"
    expected_current = (state["previous_commit"] if state["status"] == "rolled_back"
                        else state["commit"])
    if (not current.is_symlink() or current.readlink() != Path("releases") / expected_current
            or current.resolve(strict=True) !=
                (runtime_root / "releases" / expected_current).resolve(strict=True)
            or (current.lstat().st_uid, current.lstat().st_gid) != (unit_uid, unit_gid)):
        raise ValueError("active release lineage mismatch")
    previous = state.get("previous_commit")
    if not previous: raise ValueError("activation has no previous release")
    previous_release = runtime_root / "releases" / previous
    verify_release(previous_release, previous)
    verify_release_ownership(previous_release, unit_uid, unit_gid)
    applied_release = runtime_root / "releases" / state["commit"]
    verify_release(applied_release, state["commit"])
    verify_release_ownership(applied_release, unit_uid, unit_gid)
    if digest(applied_release / "RELEASE-MANIFEST.json") != state["release_manifest_sha256"]:
        raise ValueError("activation release manifest changed")
    verify_staged_deployment(runtime_root, state["commit"])
    units_dir = runtime_root / "staged-units" / state["commit"]
    route_dir = runtime_root / "staged-nginx" / state["commit"]
    if (digest(units_dir / UNIT_MANIFEST) != state["staged_unit_manifest_sha256"]
            or digest(route_dir / ROUTE_MANIFEST) != state["staged_route_manifest_sha256"]):
        raise ValueError("activation staging record changed")
    if (not isinstance(state["units"], list)
            or [entry.get("name") for entry in state["units"]] != list(UNITS)):
        raise ValueError("activation unit record is incomplete or reordered")
    for entry in state["units"]:
        if (entry.get("target") != str(systemd / entry["name"])
                or entry.get("after_sha256") != digest(units_dir / entry["name"])
                or entry.get("backup") !=
                    (entry["name"] + ".before" if entry.get("existed") else None)):
            raise ValueError("activation unit record lineage mismatch")
        target = Path(entry["target"])
        expected_digest = (entry["before_sha256"] if state["status"] == "rolled_back"
                           else entry["after_sha256"])
        if expected_digest is None:
            if target.exists() or target.is_symlink():
                raise ValueError("rolled-back unit unexpectedly exists")
        elif (not target.is_file() or target.is_symlink()
              or hashlib.sha256(target.read_bytes()).hexdigest() != expected_digest):
            raise ValueError("active unit changed since activation")
        if entry["existed"]:
            saved = record / entry["backup"]
            if (not saved.is_file() or saved.is_symlink() or saved.stat().st_nlink != 1
                    or digest(saved) != entry["before_sha256"]):
                raise ValueError("activation backup checksum mismatch")
    nginx = state["nginx"]
    recorded_nginx = Path(nginx["target"])
    expected_nginx = Path(nginx_target) if nginx_target is not None else recorded_nginx
    if (recorded_nginx != expected_nginx or nginx.get("backup") != "nginx.before"
            or nginx.get("applied") != "nginx.after"):
        raise ValueError("activation Nginx record target mismatch")
    saved_nginx = record / nginx["backup"]
    applied_nginx_file = record / nginx["applied"]
    expected_nginx_digest = (nginx["before_sha256"] if state["status"] == "rolled_back"
                             else nginx["after_sha256"])
    if (not recorded_nginx.is_file() or recorded_nginx.is_symlink()
            or hashlib.sha256(recorded_nginx.read_bytes()).hexdigest() != expected_nginx_digest
            or not saved_nginx.is_file() or saved_nginx.is_symlink()
            or saved_nginx.stat().st_nlink != 1
            or digest(saved_nginx) != nginx["before_sha256"]
            or not applied_nginx_file.is_file() or applied_nginx_file.is_symlink()
            or applied_nginx_file.stat().st_nlink != 1
            or digest(applied_nginx_file) != nginx["after_sha256"]):
        raise ValueError("active Nginx file or backup changed since activation")
    if state["status"] == "rolled_back":
        return {"dry_run": not apply, "current_commit": previous,
                "already_restored": True, "restored_from": str(record)}
    services = service_state_verifier()
    if not apply:
        return {"dry_run": True, "current_commit": state["commit"],
                "restore_commit": previous,
                "required_stop_confirmation": ACTIVATION_CONFIRMATION,
                "service_state": services}
    if not confirmed_services_stopped:
        raise ValueError(ACTIVATION_CONFIRMATION)
    if service_state_verifier() != services:
        raise ValueError("service state changed after rollback preflight")
    applied_units = {entry["name"]: Path(entry["target"]).read_bytes()
                     for entry in state["units"]}
    applied_nginx = recorded_nginx.read_bytes()
    state["status"] = "rollback_prepared"
    _replace_json_fsync(state_path, state, 0o600, unit_uid, unit_gid)
    if fail: fail("after_rollback_record_prepared")
    try:
        link = runtime_root / (".current-rollback-" + uuid4().hex)
        link.symlink_to(Path("releases") / previous); os.replace(link, current)
        _fsync_directory(runtime_root)
        if fail: fail("after_rollback_symlink")
        for index, entry in enumerate(state["units"]):
            target = Path(entry["target"])
            if entry["existed"]:
                _replace_bytes(target, (record / entry["backup"]).read_bytes(), entry["metadata"])
            else:
                target.unlink(missing_ok=True)
                _fsync_directory(target.parent)
            if fail: fail(f"after_rollback_unit_{index}")
        _replace_bytes(recorded_nginx, (record / nginx["backup"]).read_bytes(), nginx["metadata"])
        if fail: fail("after_rollback_nginx")
        state["status"] = "rolled_back"
        _replace_json_fsync(state_path, state, 0o600, unit_uid, unit_gid)
        if fail: fail("after_rollback_record")
        return {"dry_run": False, "current_commit": previous, "restored_from": str(record)}
    except BaseException:
        link = runtime_root / (".current-reinstate-" + uuid4().hex)
        link.symlink_to(Path("releases") / state["commit"]); os.replace(link, current)
        _fsync_directory(runtime_root)
        for entry in state["units"]:
            _replace_bytes(Path(entry["target"]), applied_units[entry["name"]], entry["metadata"])
        _replace_bytes(recorded_nginx, applied_nginx, nginx["metadata"])
        state["status"] = "applied"
        _replace_json_fsync(state_path, state, 0o600, unit_uid, unit_gid)
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--repo",default="/srv/projects/nocturne-plugin-intake")
    parser.add_argument("--runtime-root",default="/srv/nocturne-plugin"); parser.add_argument("--systemd-dir",default="/etc/systemd/system")
    parser.add_argument("--commit",required=True); parser.add_argument("--prepare",action="store_true"); parser.add_argument("--activate",action="store_true")
    parser.add_argument("--stage-deployment", action="store_true")
    parser.add_argument("--check-deployment", action="store_true")
    parser.add_argument("--prepare-venv", action="store_true")
    parser.add_argument("--check-venv", action="store_true")
    parser.add_argument("--recover-incomplete-venv")
    parser.add_argument("--apply-recovery", action="store_true")
    parser.add_argument("--python", default="/usr/bin/python3.14")
    parser.add_argument("--requirements-lock")
    parser.add_argument("--wheel")
    parser.add_argument("--nginx-target", default="/etc/nginx/sites-enabled/nocturne")
    parser.add_argument("--emoji-venv")
    parser.add_argument("--confirm-services-stopped", action="store_true")
    parser.add_argument("--expected-prestate-sha256")
    parser.add_argument("--expected-recovery-sha256")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback-record")
    parser.add_argument("--recover-activation-record"); args=parser.parse_args()
    if args.apply_recovery and not args.recover_incomplete_venv:
        raise SystemExit("--apply-recovery requires --recover-incomplete-venv")
    selected=sum((args.prepare,args.activate,args.stage_deployment,args.check_deployment,
                  args.prepare_venv,args.check_venv,bool(args.rollback_record),
                  bool(args.recover_incomplete_venv),
                  bool(args.recover_activation_record)))
    if selected>1: raise SystemExit("choose one mutating mode")
    verify_checkout(args.repo, args.commit)
    sha=full_commit(Path(args.repo),args.commit)
    if args.rollback_record: result=rollback_activation(
        args.rollback_record,args.runtime_root,args.systemd_dir,
        nginx_target=args.nginx_target,apply=args.apply,
        confirmed_services_stopped=args.confirm_services_stopped)
    elif args.recover_activation_record: result=recover_interrupted_activation(
        args.recover_activation_record,args.runtime_root,args.systemd_dir,
        nginx_target=args.nginx_target,apply=args.apply,
        confirmed_services_stopped=args.confirm_services_stopped,
        expected_recovery_sha256=args.expected_recovery_sha256)
    elif args.activate: result=activate(args.runtime_root,args.systemd_dir,sha,
                                        nginx_target=args.nginx_target,
                                        emoji_venv=args.emoji_venv,apply=args.apply,
                                        confirmed_services_stopped=args.confirm_services_stopped,
                                        expected_prestate_sha256=args.expected_prestate_sha256)
    elif args.stage_deployment: result=stage_deployment(args.runtime_root,sha,apply=True)
    elif args.check_deployment:
        result={"dry_run":True,"commit":sha,
                "staged":verify_staged_deployment(args.runtime_root,sha)}
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
    else: result=prepare(args.repo,args.runtime_root,sha,apply=args.prepare)
    print(json.dumps(result,sort_keys=True))
    if (not selected or args.check_venv or args.check_deployment or
            (args.activate and not args.apply) or
            (args.rollback_record and not args.apply) or
            (args.recover_activation_record and not args.apply) or
            (args.recover_incomplete_venv and not args.apply_recovery)):
        print("Dry run only; no release, venv, symlink, unit, or service state was changed.")


if __name__ == "__main__": main()
