"""Prepare the versioned, hash-locked Discord emoji image runtime."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from uuid import uuid4

from immutable_runtime_release import (_safe_acl, _safe_directory_node,
                                       _safe_owned_directory, _system_python,
                                       _validate_venv_tree, _write_json_fsync,
                                       command, digest, validate_locked_wheel)
from deployment_trust import verify_checkout
from runtime_identity import (
    PILLOW_LOCK_SHA256, PILLOW_LOCK_TEXT, PILLOW_RUNTIME_NAME,
    PILLOW_VERSION, PILLOW_WHEEL_NAME, PILLOW_WHEEL_SHA256,
    PYTHON_MACHINE, PYTHON_SOABI, PYTHON_VERSION,
)


PURPOSE = "nocturne-emoji-runtime-v2"
TARGET_NAME = PILLOW_RUNTIME_NAME
MARKER = "PREPARATION_INCOMPLETE"
MANIFEST = "EMOJI-RUNTIME-MANIFEST.json"
MACHINE = PYTHON_MACHINE
SOABI = PYTHON_SOABI
MIN_GLIBC = (2, 27)
WHEEL_NAME = PILLOW_WHEEL_NAME
WHEEL_SHA256 = PILLOW_WHEEL_SHA256
REQUIREMENTS_TEXT = PILLOW_LOCK_TEXT
REQUIREMENTS_SHA256 = PILLOW_LOCK_SHA256


def _probe(python, run=command):
    script = (
        "import json,os,platform,sys,sysconfig;"
        "print(json.dumps({'version':[sys.version_info.major,sys.version_info.minor],"
        "'machine':platform.machine(),'soabi':sysconfig.get_config_var('SOABI'),"
        "'glibc':os.confstr('CS_GNU_LIBC_VERSION'),"
        "'executable':str(__import__('pathlib').Path(sys.executable).resolve()),"
        "'prefix':str(__import__('pathlib').Path(sys.prefix).resolve())},sort_keys=True))")
    result = run([str(python), "-B", "-c", script], stdout=subprocess.PIPE, text=True)
    try: value = json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("Python runtime probe returned invalid data") from error
    return value


def _glibc_tuple(value):
    match = re.fullmatch(r"glibc ([0-9]+)\.([0-9]+)", value or "")
    if not match: raise ValueError("unsupported libc runtime")
    return int(match.group(1)), int(match.group(2))


def validate_host(python, run=command):
    resolved = _system_python(python)
    value = _probe(resolved, run)
    if value.get("version") != PYTHON_VERSION:
        raise ValueError("emoji runtime requires CPython 3.14")
    if value.get("machine") != MACHINE:
        raise ValueError("emoji runtime architecture mismatch")
    if value.get("soabi") != SOABI:
        raise ValueError("emoji runtime ABI mismatch")
    if _glibc_tuple(value.get("glibc")) < MIN_GLIBC:
        raise ValueError("emoji runtime glibc is too old for the pinned wheel")
    if value.get("executable") != str(resolved):
        raise ValueError("emoji runtime interpreter resolution mismatch")
    return value


def validate_inputs(requirements, wheel, *, uid=0, gid=0, run=command):
    return validate_locked_wheel(
        requirements, wheel, expected_lock=REQUIREMENTS_TEXT,
        expected_name=WHEEL_NAME, expected_sha256=WHEEL_SHA256,
        expected_directory=TARGET_NAME, uid=uid, gid=gid, run=run)


def dependency_record(target, host, python, requirements, wheel):
    return {"schema_version": 2, "purpose": PURPOSE, "target": str(Path(target)),
            "python": str(Path(python).resolve(strict=True)),
            "python_version": PYTHON_VERSION, "soabi": host["soabi"],
            "machine": host["machine"], "glibc": host["glibc"],
            "requirements_sha256": digest(requirements),
            "wheel": wheel.name, "wheel_sha256": digest(wheel),
            "pillow_version": PILLOW_VERSION, "copied_virtualenv_allowed": False}


def _runtime_probe(target, run=command):
    script = (
        "import json,platform,sys,sysconfig,PIL;"
        "print(json.dumps({'version':[sys.version_info.major,sys.version_info.minor],"
        "'machine':platform.machine(),'soabi':sysconfig.get_config_var('SOABI'),"
        "'pillow':PIL.__version__,'executable':str(__import__('pathlib').Path(sys.executable).resolve()),"
        "'prefix':str(__import__('pathlib').Path(sys.prefix).resolve())},sort_keys=True))")
    result = run([str(Path(target) / "bin/python"), "-B", "-c", script],
                 stdout=subprocess.PIPE, text=True)
    try: return json.loads(result.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("emoji virtualenv probe returned invalid data") from error


def validate_runtime(target, *, uid=0, gid=0, approved_python=None, run=command,
                     allow_incomplete=False, expected_record=None):
    target = Path(target)
    approved = approved_python or {Path("/usr/bin/python3.14")}
    _validate_venv_tree(target, uid=uid, gid=gid, approved_python=approved, run=run)
    marker = target / MARKER
    if not allow_incomplete and (marker.exists() or marker.is_symlink()):
        raise ValueError("emoji runtime preparation is incomplete")
    manifest = target / MANIFEST
    if not manifest.is_file() or manifest.is_symlink() or manifest.stat().st_nlink != 1:
        raise ValueError("emoji runtime dependency record is missing or unsafe")
    value = json.loads(manifest.read_text())
    expected_keys = {"schema_version", "purpose", "target", "python", "python_version", "soabi",
                     "machine", "glibc", "requirements_sha256", "wheel",
                     "wheel_sha256", "pillow_version", "copied_virtualenv_allowed"}
    if (set(value) != expected_keys or value.get("schema_version") != 2
            or value.get("purpose") != PURPOSE
            or value.get("target") != str(target)
            or value.get("python") != "/usr/bin/python3.14"
            or value.get("python_version") != PYTHON_VERSION
            or value.get("soabi") != SOABI or value.get("machine") != MACHINE
            or _glibc_tuple(value.get("glibc")) < MIN_GLIBC
            or value.get("requirements_sha256") != REQUIREMENTS_SHA256
            or value.get("wheel") != WHEEL_NAME
            or value.get("wheel_sha256") != WHEEL_SHA256
            or value.get("pillow_version") != PILLOW_VERSION
            or value.get("copied_virtualenv_allowed") is not False):
        raise ValueError("emoji runtime dependency record mismatch")
    if expected_record is not None and value != expected_record:
        raise ValueError("emoji runtime dependency record changed")
    probe = _runtime_probe(target, run)
    if (probe.get("version") != PYTHON_VERSION or probe.get("machine") != MACHINE
            or probe.get("soabi") != SOABI or probe.get("pillow") != PILLOW_VERSION
            or probe.get("prefix") != str(target.resolve(strict=True))):
        raise ValueError("emoji runtime import or ABI validation failed")
    run([str(target / "bin/python"), "-B", "-m", "pip", "check"],
        stdout=subprocess.PIPE, text=True)
    pip = target / "bin/pip"
    if not pip.is_file() or pip.is_symlink() or pip.stat().st_nlink != 1:
        raise ValueError("emoji runtime pip launcher is missing or unsafe")
    for launcher in (target / "bin").iterdir():
        if launcher.name in {"python", "python3", "python3.14"}:
            if (not launcher.is_symlink()
                    or launcher.resolve(strict=True) != Path("/usr/bin/python3.14")):
                raise ValueError("emoji runtime interpreter launcher is not exact")
        elif launcher.is_file() and not launcher.is_symlink():
            with launcher.open("rb") as source:
                first = source.readline(4096)
            if os.access(launcher, os.X_OK):
                expected = ("#!" + str(target / "bin/python3.14") + "\n").encode()
                if not first.startswith(b"#!") or first != expected:
                    raise ValueError("emoji runtime launcher does not name the final venv")
    return value


def prepare(runtime_root, python, requirements, wheel, *, apply=False,
            uid=0, gid=0, run=command, fail=None):
    runtime_root = Path(runtime_root); python = Path(python)
    requirements, wheel = Path(requirements), Path(wheel)
    expected_wheel = runtime_root / "wheelhouse" / TARGET_NAME / WHEEL_NAME
    if wheel != expected_wheel:
        raise ValueError("emoji wheel is not at its exact versioned runtime path")
    _safe_directory_node(runtime_root, {0o755}, uid, gid, run)
    _safe_directory_node(runtime_root / "wheelhouse", {0o755}, uid, gid, run)
    # Architecture and ABI are deliberately checked before target creation or pip.
    host = validate_host(python, run)
    requirements, wheel = validate_inputs(requirements, wheel, uid=uid, gid=gid, run=run)
    target = runtime_root / "venvs" / TARGET_NAME
    report = {"dry_run": not apply, "target": str(target), "state": "not_prepared",
              "host": {key: host[key] for key in ("version", "machine", "soabi", "glibc")}}
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_dir():
            raise ValueError("emoji runtime target exists in an unknown state")
        if (target / MARKER).exists() or (target / MARKER).is_symlink():
            raise ValueError("emoji runtime is incomplete; exact recovery is required")
        record = dependency_record(target, host, python, requirements, wheel)
        validate_runtime(target, uid=uid, gid=gid, approved_python={python}, run=run,
                         expected_record=record)
        return {**report, "state": "already_prepared"}
    if not apply: return report
    if os.geteuid() != 0 and uid == 0:
        raise PermissionError("emoji runtime preparation requires root")
    parent = runtime_root / "venvs"
    if not parent.exists():
        parent.mkdir(mode=0o755); os.chown(parent, uid, gid)
    _safe_owned_directory(parent, 0o755, uid, gid, run)
    target.mkdir(mode=0o755); os.chown(target, uid, gid)
    marker = target / MARKER
    _write_json_fsync(marker, {"purpose": PURPOSE, "target": str(target)}, 0o600, uid, gid)
    if fail: fail("after_incomplete_marker")
    old_umask = os.umask(0o022)
    try: run([str(python), "-B", "-m", "venv", str(target)])
    finally: os.umask(old_umask)
    if fail: fail("after_venv_creation")
    interpreter = target / "bin/python"
    old_umask = os.umask(0o022)
    try:
        run([str(interpreter), "-B", "-m", "pip", "install",
             "--disable-pip-version-check", "--no-index", "--find-links", str(wheel.parent),
             "--require-hashes", "--only-binary=:all:", "--no-deps", "-r", str(requirements)])
    finally: os.umask(old_umask)
    if fail: fail("after_dependency_install")
    for path in target.rglob("*"):
        os.chown(path, uid, gid, follow_symlinks=False)
        if not path.is_symlink(): path.chmod((path.stat().st_mode & 0o777) & ~0o022)
    record = dependency_record(target, host, python, requirements, wheel)
    _write_json_fsync(target / MANIFEST, record, 0o644, uid, gid)
    validate_runtime(target, uid=uid, gid=gid, approved_python={python}, run=run,
                     allow_incomplete=True, expected_record=record)
    if fail: fail("after_validation")
    marker.unlink()
    descriptor = os.open(target, os.O_RDONLY | os.O_DIRECTORY)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)
    return {**report, "dry_run": False, "state": "prepared",
            "dependency_record_sha256": digest(target / MANIFEST)}


def recover_incomplete(runtime_root, target, *, apply=False, uid=0, gid=0, run=command):
    runtime_root, target = Path(runtime_root), Path(target)
    expected = runtime_root / "venvs" / TARGET_NAME
    if target != expected or target.is_symlink() or not target.is_dir():
        raise ValueError("recovery target is not the exact emoji runtime")
    _safe_owned_directory(runtime_root, 0o755, uid, gid, run)
    _safe_owned_directory(target.parent, 0o755, uid, gid, run)
    _validate_venv_tree(target, uid=uid, gid=gid,
                        approved_python={Path("/usr/bin/python3.14")}, run=run)
    marker = target / MARKER
    marker_metadata = marker.stat() if marker.is_file() and not marker.is_symlink() else None
    if (marker_metadata is None or marker_metadata.st_nlink != 1
            or (marker_metadata.st_uid, marker_metadata.st_gid,
                stat.S_IMODE(marker_metadata.st_mode)) != (uid, gid, 0o600)):
        raise ValueError("verified emoji incomplete marker is required")
    expected_marker = {"purpose": PURPOSE, "target": str(target)}
    if json.loads(marker.read_text()) != expected_marker:
        raise ValueError("emoji incomplete marker contents mismatch")
    result = {"dry_run": not apply, "state": "verified_incomplete", "target": str(target)}
    if not apply: return result
    if os.geteuid() != 0 and uid == 0:
        raise PermissionError("emoji runtime recovery requires root")
    quarantine = runtime_root / "quarantine" / "incomplete-emoji-venvs"
    for directory in (quarantine.parent, quarantine):
        if directory.exists() or directory.is_symlink():
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError("emoji quarantine path is unsafe")
        else:
            directory.mkdir(mode=0o700); os.chown(directory, uid, gid)
        _safe_owned_directory(directory, 0o700, uid, gid, run)
    if quarantine.stat().st_dev != target.stat().st_dev or os.path.ismount(quarantine):
        raise ValueError("emoji quarantine is not on the runtime filesystem")
    for _attempt in range(32):
        reservation = quarantine / (TARGET_NAME + "-" + uuid4().hex)
        try:
            reservation.mkdir(mode=0o700)
            break
        except FileExistsError:
            continue
    else:
        raise RuntimeError("could not reserve a unique emoji quarantine path")
    os.chown(reservation, uid, gid)
    destination = reservation / "runtime"
    try:
        os.rename(target, destination)
    except BaseException:
        reservation.rmdir()
        raise
    return {**result, "dry_run": False, "state": "quarantined",
            "quarantine": str(destination)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", default="/srv/nocturne-plugin")
    parser.add_argument("--repo", default="/srv/projects/nocturne-plugin-intake")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--python", default="/usr/bin/python3.14")
    parser.add_argument("--requirements", required=True)
    parser.add_argument("--wheel", required=True)
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--host-preflight", action="store_true")
    parser.add_argument("--recover-incomplete")
    parser.add_argument("--apply-recovery", action="store_true")
    args = parser.parse_args(argv)
    verify_checkout(args.repo, args.commit)
    if not args.host_preflight:
        expected_requirements = (Path(args.runtime_root) / "releases" / args.commit /
                                 "dev/intake/emoji-sync-requirements.txt")
        if Path(args.requirements) != expected_requirements:
            parser.error("--requirements must name the requested release's committed lock")
    if args.apply_recovery and not args.recover_incomplete:
        parser.error("--apply-recovery requires --recover-incomplete")
    if sum((args.prepare, args.host_preflight, bool(args.recover_incomplete))) > 1:
        parser.error("choose preparation or recovery")
    if args.host_preflight:
        source = Path(args.repo) / "dev/intake/emoji-sync-requirements.txt"
        if Path(args.requirements) != source or source.read_text() != REQUIREMENTS_TEXT:
            parser.error("host preflight requires the exact committed emoji lock")
        host = validate_host(args.python)
        result = {"dry_run": True, "state": "host_compatible",
                  "host": {key: host[key] for key in ("version", "machine", "soabi", "glibc")}}
    elif args.recover_incomplete:
        result = recover_incomplete(args.runtime_root, args.recover_incomplete,
                                    apply=args.apply_recovery)
    else:
        result = prepare(args.runtime_root, args.python, args.requirements, args.wheel,
                         apply=args.prepare)
    print(json.dumps(result, sort_keys=True))
    if not args.prepare and not args.apply_recovery:
        print("Dry run only; no virtualenv or runtime state was changed.")


if __name__ == "__main__":
    main()
