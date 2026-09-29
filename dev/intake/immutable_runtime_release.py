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

from derived_review_support import (_apply_metadata, _capture_safe_metadata,
                                    _verify_metadata)
from emoji_route_support import candidate_site

PURPOSE = "nocturne-immutable-runtime-v1"
VENV_PURPOSE = "nocturne-runtime-venv-v1"
VENV_NAME = "python3.14-gunicorn-26.2.0"
VENV_MARKER = "PREPARATION_INCOMPLETE"
VENV_MANIFEST = "VENV-MANIFEST.json"
GUNICORN_VERSION = "26.2.0"
CORE_UNITS = ("nocturne-plugin-writer.service", "nocturne-plugin-dev.service")
EMOJI_UNITS = ("nocturne-plugin-emoji-sync.service",
               "nocturne-plugin-emoji-sync.timer")
UNITS = CORE_UNITS + EMOJI_UNITS
UNIT_MANIFEST = "STAGED-UNITS-MANIFEST.json"
ROUTE_MANIFEST = "STAGED-NGINX-MANIFEST.json"
UNIT_STAGE_PURPOSE = "nocturne-commit-scoped-units-v1"
ROUTE_STAGE_PURPOSE = "nocturne-commit-scoped-emoji-route-v1"
EMOJI_VENV_NAME = "emoji-python3.14-pillow-12.3.0"
EMOJI_WHEEL_SHA256 = "251bf95b67017e27b13d82f5b326234ca62d70f9cf4c2b9032de2358a3b12c7b"
EMOJI_ROUTE = "nginx-emojis-location.conf"
ACTIVATION_CONFIRMATION = (
    "intake, writer, emoji synchronizer, emoji timer, and Nginx reload activity "
    "must be quiesced for activation or rollback")
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
            if stat.S_ISREG(item.st_mode) and item.st_nlink != 1:
                raise ValueError("runtime venv contains a hard-linked file")
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
            public_bind = "BindReadOnlyPaths=/var/lib/nocturne-plugin-emojis/public:/run/nocturne-plugin-emojis"
            if text.count(public_bind) != 1:
                raise ValueError("intake public emoji mirror bind is missing or ambiguous")
            for forbidden in ("emoji-sync-config", "discord-token",
                              "/etc/nocturne-plugin/credentials"):
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
            for line in ("LoadCredential=emoji-sync-config:",
                         "LoadCredential=discord-token:", "StateDirectoryMode=0755"):
                if text.count(line) != 1:
                    raise ValueError(f"emoji synchronizer boundary missing: {line}")
        elif name == "nocturne-plugin-emoji-sync.timer":
            if text.count("Unit=nocturne-plugin-emoji-sync.service") != 1:
                raise ValueError("emoji timer target is missing or ambiguous")
        if old_root in text:
            raise ValueError(f"mutable checkout remains in generated unit: {name}")
        target = generated / name; target.write_text(text); target.chmod(0o444)


def _artifact_manifest(purpose, commit, release, artifacts, uid, gid):
    return {"purpose": purpose, "commit": commit,
            "release_manifest_sha256": digest(release / "RELEASE-MANIFEST.json"),
            "owner_uid": uid, "owner_gid": gid,
            "artifacts": {name: digest(path) for name, path in sorted(artifacts.items())}}


def _verify_stage(path, manifest_name, purpose, commit):
    path = Path(path); manifest_path = path / manifest_name
    if path.is_symlink() or not path.is_dir() or manifest_path.is_symlink() \
            or not manifest_path.is_file():
        raise ValueError("staged deployment artifact is missing or unsafe")
    _safe_acl(path)
    value = json.loads(manifest_path.read_text())
    if value.get("purpose") != purpose or value.get("commit") != commit:
        raise ValueError("staged deployment manifest lineage mismatch")
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("staged deployment manifest is malformed")
    actual = {item.name for item in path.iterdir()}
    if actual != set(artifacts) | {manifest_name}:
        raise ValueError("staged deployment file set mismatch")
    metadata = path.lstat()
    if ((metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode)) !=
            (value.get("owner_uid"), value.get("owner_gid"), 0o555)):
        raise ValueError("staged deployment directory metadata mismatch")
    manifest_metadata = manifest_path.stat()
    if ((manifest_metadata.st_uid, manifest_metadata.st_gid,
         stat.S_IMODE(manifest_metadata.st_mode)) !=
            (value["owner_uid"], value["owner_gid"], 0o444)):
        raise ValueError("staged deployment manifest metadata mismatch")
    for name, expected in artifacts.items():
        item = path / name
        if (not item.is_file() or item.is_symlink() or item.stat().st_nlink != 1
                or digest(item) != expected
                or (item.stat().st_uid, item.stat().st_gid, stat.S_IMODE(item.stat().st_mode))
                    != (value["owner_uid"], value["owner_gid"], 0o444)):
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
    if ((route.get("owner_uid"), route.get("owner_gid")) !=
            (units.get("owner_uid"), units.get("owner_gid"))):
        raise ValueError("staged deployment ownership lineage mismatch")
    verify_release_ownership(release, units["owner_uid"], units["owner_gid"])
    expected_release = digest(release / "RELEASE-MANIFEST.json")
    if (units.get("release_manifest_sha256") != expected_release
            or route.get("release_manifest_sha256") != expected_release):
        raise ValueError("staged deployment release digest mismatch")
    return {"units": units, "route": route}


def _stage_directory(parent, target, artifacts, manifest_name, manifest, uid, gid):
    if target.exists() or target.is_symlink():
        return False
    parent.mkdir(parents=True, mode=0o755, exist_ok=True)
    _safe_directory_node(parent, {0o755}, uid, gid)
    temporary = Path(tempfile.mkdtemp(prefix=".stage-", dir=parent))
    try:
        for name, source in artifacts.items():
            destination = temporary / name
            shutil.copyfile(source, destination)
            os.chown(destination, uid, gid); destination.chmod(0o444)
        _write_json_fsync(temporary / manifest_name, manifest, 0o444, uid, gid)
        os.chown(temporary, uid, gid); temporary.chmod(0o555)
        os.replace(temporary, target)
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
        "path": str(runtime_root / "venvs" / EMOJI_VENV_NAME),
        "pillow": "12.3.0", "wheel_sha256": EMOJI_WHEEL_SHA256,
        "requirements_sha256": digest(
            release / "dev/intake/emoji-sync-requirements.txt")}
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
    commit = full_commit(repo, revision, run); release = runtime_root / "releases" / commit
    report = {"dry_run": not apply, "commit": commit, "release": str(release),
              "current": str(runtime_root / "current"),
              "venv": str(runtime_root / "venvs" / VENV_NAME)}
    if release.exists():
        verify_release(release, commit); verify_release_ownership(release, uid, gid)
        return {**report, "state": "already_prepared"}
    if not apply: return {**report, "state": "not_prepared"}
    _safe_owned_directory(runtime_root, 0o755, uid, gid, run)
    releases = runtime_root / "releases"
    if not releases.exists():
        releases.mkdir(mode=0o755); os.chown(releases, uid, gid)
    _safe_owned_directory(releases, 0o755, uid, gid, run)
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


def validate_venv(venv, run=command, *, uid=0, gid=0):
    venv = Path(venv)
    _validate_venv_tree(venv, uid=uid, gid=gid,
                        approved_python={Path("/usr/bin/python3.14")}, run=run)
    if (venv / VENV_MARKER).exists() or (venv / VENV_MARKER).is_symlink():
        raise ValueError("runtime venv preparation is incomplete")
    python = venv / "bin/python"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError("reproducible runtime venv is missing")
    version = run([str(python), "-B", "-m", "gunicorn", "--version"],
                  stdout=subprocess.PIPE, text=True).stdout.strip()
    if version != f"gunicorn (version {GUNICORN_VERSION})": raise ValueError("runtime gunicorn version mismatch")
    run([str(python), "-B", "-m", "pip", "check"], stdout=subprocess.PIPE, text=True)
    manifest = venv / VENV_MANIFEST
    if (not manifest.is_file() or manifest.is_symlink() or manifest.stat().st_nlink != 1):
        raise ValueError("runtime venv manifest is missing or unsafe")
    value = json.loads(manifest.read_text())
    if (value.get("purpose") != VENV_PURPOSE or value.get("target") != str(venv)
            or value.get("gunicorn_version") != GUNICORN_VERSION
            or value.get("python") != "/usr/bin/python3.14"):
        raise ValueError("runtime venv manifest mismatch")


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
        os.replace(staged, target); _verify_metadata(target, metadata)
    finally:
        staged.unlink(missing_ok=True)


def _restore_record_entries(entries, record):
    for entry in reversed(entries):
        target = Path(entry["target"])
        if entry["existed"]:
            _replace_bytes(target, (record / entry["backup"]).read_bytes(), entry["metadata"])
        else:
            target.unlink(missing_ok=True)


def activate(runtime_root, systemd, commit, *, nginx_target=None, apply=False,
             venv=None, emoji_venv=None, fail=None, unit_uid=0, unit_gid=0,
             confirmed_services_stopped=False):
    runtime_root, systemd = Path(runtime_root), Path(systemd)
    _safe_directory_node(runtime_root, {0o755}, unit_uid, unit_gid)
    _safe_directory_node(systemd, {0o755}, unit_uid, unit_gid)
    venv = Path(venv or runtime_root / "venvs" / VENV_NAME)
    emoji_venv = Path(emoji_venv or runtime_root / "venvs" / EMOJI_VENV_NAME)
    release = runtime_root / "releases" / commit
    verify_release(release, commit); verify_release_ownership(release, unit_uid, unit_gid)
    validate_venv(venv, uid=unit_uid, gid=unit_gid)
    from emoji_runtime_release import validate_runtime as validate_emoji_runtime
    emoji_record = validate_emoji_runtime(emoji_venv, uid=unit_uid, gid=unit_gid)
    staged = verify_staged_deployment(runtime_root, commit)
    units_dir = runtime_root / "staged-units" / commit
    route_dir = runtime_root / "staged-nginx" / commit
    if set(staged["units"]["artifacts"]) != set(UNITS):
        raise ValueError("staged unit set is incomplete")
    runtime_requirement = staged["units"].get("emoji_runtime")
    if (not isinstance(runtime_requirement, dict)
            or runtime_requirement.get("path") != str(emoji_venv)
            or runtime_requirement.get("pillow") != emoji_record.get("pillow_version")
            or runtime_requirement.get("wheel_sha256") != emoji_record.get("wheel_sha256")
            or runtime_requirement.get("requirements_sha256") !=
                emoji_record.get("requirements_sha256")):
        raise ValueError("emoji runtime does not match staged dependency record")
    for value in staged.values():
        if (value.get("owner_uid"), value.get("owner_gid")) != (unit_uid, unit_gid):
            raise ValueError("staged deployment is not root-owned")
    nginx_target = Path(nginx_target) if nginx_target else None
    if (nginx_target is None or not nginx_target.is_file() or nginx_target.is_symlink()
            or nginx_target.stat().st_nlink != 1):
        raise ValueError("safe active Nginx target is required")
    _safe_directory_node(nginx_target.parent, {0o700, 0o750, 0o755},
                         unit_uid, unit_gid)
    nginx_before = nginx_target.read_bytes()
    nginx_after = candidate_site(
        nginx_before.decode(),
        (release / "dev/intake/nginx-announcements-location.conf").read_text(),
        (route_dir / EMOJI_ROUTE).read_text()).encode()
    current = runtime_root / "current"
    previous = current.resolve().name if current.is_symlink() else None
    if previous is None:
        raise ValueError("active immutable release selector is required")
    previous_release = runtime_root / "releases" / previous
    verify_release(previous_release, previous)
    verify_release_ownership(previous_release, unit_uid, unit_gid)
    default_metadata = {"uid": unit_uid, "gid": unit_gid, "mode": 0o644,
                        "acl": "user::rw-\ngroup::r--\nother::r--\n"}
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
    report = {"dry_run": not apply, "commit": commit, "previous_commit": previous,
              "units": len(entries), "nginx_target": str(nginx_target),
              "required_stop_confirmation": ACTIVATION_CONFIRMATION}
    if not apply: return report
    if not confirmed_services_stopped:
        raise ValueError(ACTIVATION_CONFIRMATION)
    records = runtime_root / "activation-records"
    if not records.exists():
        records.mkdir(mode=0o700); os.chown(records, unit_uid, unit_gid)
    _safe_directory_node(records, {0o700}, unit_uid, unit_gid)
    record = records / uuid4().hex
    record.mkdir(mode=0o700); os.chown(record, unit_uid, unit_gid)
    for entry in entries:
        if entry["existed"]:
            saved = record / entry["backup"]
            saved.write_bytes(Path(entry["target"]).read_bytes())
            _apply_metadata(saved, entry["metadata"])
    nginx_saved = record / "nginx.before"
    nginx_saved.write_bytes(nginx_before); _apply_metadata(nginx_saved, nginx_metadata)
    state = {"purpose": PURPOSE, "commit": commit, "previous_commit": previous,
             "release_manifest_sha256": digest(release / "RELEASE-MANIFEST.json"),
             "staged_unit_manifest_sha256": digest(units_dir / UNIT_MANIFEST),
             "staged_route_manifest_sha256": digest(route_dir / ROUTE_MANIFEST),
             "units": entries,
             "nginx": {"target": str(nginx_target),
                       "before_sha256": hashlib.sha256(nginx_before).hexdigest(),
                       "after_sha256": hashlib.sha256(nginx_after).hexdigest(),
                       "metadata": nginx_metadata, "backup": nginx_saved.name}}
    _write_json_fsync(record / "ACTIVATION.json", state, 0o600, unit_uid, unit_gid)
    old_link = current.readlink(); changed = []
    try:
        link = runtime_root / (".current-" + uuid4().hex)
        link.symlink_to(Path("releases") / commit)
        if fail: fail("before_activation")
        os.replace(link, current)
        if fail: fail("after_symlink")
        for index, entry in enumerate(entries):
            changed.append(entry)
            _replace_bytes(Path(entry["target"]), (units_dir / entry["name"]).read_bytes(),
                           entry["metadata"])
            if fail: fail(f"after_unit_{index}")
        _replace_bytes(nginx_target, nginx_after, nginx_metadata)
        changed.append(state["nginx"])
        if fail: fail("after_nginx")
        return {**report, "dry_run": False, "activation_record": str(record)}
    except BaseException:
        restore_link = runtime_root / (".current-restore-" + uuid4().hex)
        restore_link.symlink_to(old_link); os.replace(restore_link, current)
        if state["nginx"] in changed:
            _replace_bytes(nginx_target, nginx_before, nginx_metadata)
            changed.remove(state["nginx"])
        _restore_record_entries(changed, record)
        raise


def rollback_activation(record, runtime_root, systemd, *, nginx_target=None,
                        apply=False, fail=None, unit_uid=0, unit_gid=0,
                        confirmed_services_stopped=False):
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
    if (set(state) != {"purpose", "commit", "previous_commit",
                       "release_manifest_sha256", "staged_unit_manifest_sha256",
                       "staged_route_manifest_sha256", "units", "nginx"}
            or not re.fullmatch(r"[0-9a-f]{40}", state.get("commit", ""))
            or not re.fullmatch(r"[0-9a-f]{40}", state.get("previous_commit", ""))):
        raise ValueError("activation record schema or commit is invalid")
    current = runtime_root / "current"
    if not current.is_symlink() or current.resolve().name != state["commit"]:
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
        if (not target.is_file() or target.is_symlink()
                or hashlib.sha256(target.read_bytes()).hexdigest() != entry["after_sha256"]):
            raise ValueError("active unit changed since activation")
        if entry["existed"]:
            saved = record / entry["backup"]
            if (not saved.is_file() or saved.is_symlink() or saved.stat().st_nlink != 1
                    or digest(saved) != entry["before_sha256"]):
                raise ValueError("activation backup checksum mismatch")
    nginx = state["nginx"]
    recorded_nginx = Path(nginx["target"])
    expected_nginx = Path(nginx_target) if nginx_target is not None else recorded_nginx
    if recorded_nginx != expected_nginx or nginx.get("backup") != "nginx.before":
        raise ValueError("activation Nginx record target mismatch")
    saved_nginx = record / nginx["backup"]
    if (not recorded_nginx.is_file() or recorded_nginx.is_symlink()
            or hashlib.sha256(recorded_nginx.read_bytes()).hexdigest() != nginx["after_sha256"]
            or not saved_nginx.is_file() or saved_nginx.is_symlink()
            or saved_nginx.stat().st_nlink != 1
            or digest(saved_nginx) != nginx["before_sha256"]):
        raise ValueError("active Nginx file or backup changed since activation")
    if not apply:
        return {"dry_run": True, "current_commit": state["commit"],
                "restore_commit": previous,
                "required_stop_confirmation": ACTIVATION_CONFIRMATION}
    if not confirmed_services_stopped:
        raise ValueError(ACTIVATION_CONFIRMATION)
    applied_units = {entry["name"]: Path(entry["target"]).read_bytes()
                     for entry in state["units"]}
    applied_nginx = recorded_nginx.read_bytes()
    try:
        link = runtime_root / (".current-rollback-" + uuid4().hex)
        link.symlink_to(Path("releases") / previous); os.replace(link, current)
        if fail: fail("after_rollback_symlink")
        for index, entry in enumerate(state["units"]):
            target = Path(entry["target"])
            if entry["existed"]:
                _replace_bytes(target, (record / entry["backup"]).read_bytes(), entry["metadata"])
            else:
                target.unlink(missing_ok=True)
            if fail: fail(f"after_rollback_unit_{index}")
        _replace_bytes(recorded_nginx, (record / nginx["backup"]).read_bytes(), nginx["metadata"])
        if fail: fail("after_rollback_nginx")
        return {"dry_run": False, "current_commit": previous, "restored_from": str(record)}
    except BaseException:
        link = runtime_root / (".current-reinstate-" + uuid4().hex)
        link.symlink_to(Path("releases") / state["commit"]); os.replace(link, current)
        for entry in state["units"]:
            _replace_bytes(Path(entry["target"]), applied_units[entry["name"]], entry["metadata"])
        _replace_bytes(recorded_nginx, applied_nginx, nginx["metadata"])
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument("--repo",default="/srv/projects/nocturne-plugin-intake")
    parser.add_argument("--runtime-root",default="/srv/nocturne-plugin"); parser.add_argument("--systemd-dir",default="/etc/systemd/system")
    parser.add_argument("--commit",default="HEAD"); parser.add_argument("--prepare",action="store_true"); parser.add_argument("--activate",action="store_true")
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
    parser.add_argument("--rollback-record"); args=parser.parse_args()
    if args.apply_recovery and not args.recover_incomplete_venv:
        raise SystemExit("--apply-recovery requires --recover-incomplete-venv")
    selected=sum((args.prepare,args.activate,args.stage_deployment,args.check_deployment,
                  args.prepare_venv,args.check_venv,bool(args.rollback_record),
                  bool(args.recover_incomplete_venv)))
    if selected>1: raise SystemExit("choose one mutating mode")
    sha=full_commit(Path(args.repo),args.commit)
    if args.rollback_record: result=rollback_activation(
        args.rollback_record,args.runtime_root,args.systemd_dir,
        nginx_target=args.nginx_target,apply=True,
        confirmed_services_stopped=args.confirm_services_stopped)
    elif args.activate: result=activate(args.runtime_root,args.systemd_dir,sha,
                                        nginx_target=args.nginx_target,
                                        emoji_venv=args.emoji_venv,apply=True,
                                        confirmed_services_stopped=args.confirm_services_stopped)
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
            (args.recover_incomplete_venv and not args.apply_recovery)):
        print("Dry run only; no release, venv, symlink, unit, or service state was changed.")


if __name__ == "__main__": main()
