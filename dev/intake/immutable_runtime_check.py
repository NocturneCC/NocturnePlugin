"""Read-only readiness classification for immutable runtime preparation."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import stat

import emoji_runtime_release as emoji_runtime
import immutable_runtime_release as runtime
from deployment_trust import verify_checkout


EXIT_CODES = {
    "prepared": 0,
    "not_prepared": 3,
    "recoverable_incomplete": 4,
    "unsafe_blocking": 1,
}
STATE_PRIORITY = {
    "prepared": 0,
    "not_prepared": 1,
    "recoverable_incomplete": 2,
    "unsafe_blocking": 3,
}
CONTAINER_MODES = {
    "releases": 0o755,
    "venvs": 0o755,
    "wheelhouse": 0o755,
    "staged-units": 0o755,
    "staged-nginx": 0o755,
    "activation-records": 0o700,
    "quarantine": 0o700,
}


def _clean(value):
    return " ".join(str(value).split())[:500]


def _readonly_git(args, **kwargs):
    environment = os.environ.copy()
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    kwargs["env"] = environment
    return runtime.command(args, **kwargs)


def _observed(path):
    path = Path(path)
    try:
        value = path.lstat()
    except FileNotFoundError:
        return "absent"
    kind = ("symlink" if stat.S_ISLNK(value.st_mode) else
            "directory" if stat.S_ISDIR(value.st_mode) else
            "regular_file" if stat.S_ISREG(value.st_mode) else "unsupported")
    return (f"{kind} uid={value.st_uid} gid={value.st_gid} "
            f"mode={stat.S_IMODE(value.st_mode):04o} links={value.st_nlink}")


class Report:
    def __init__(self):
        self.state = "prepared"
        self.diagnostics = []

    def add(self, state, phase, path, expected, observed, action):
        if STATE_PRIORITY[state] > STATE_PRIORITY[self.state]:
            self.state = state
        self.diagnostics.append({
            "phase": _clean(phase),
            "path": _clean(path),
            "expected": _clean(expected),
            "observed": _clean(observed),
            "operator_action": _clean(action),
            "classification": state,
        })

    def render(self):
        lines = [f"status={self.state}", "check_mode=read_only"]
        if not self.diagnostics:
            self.add("prepared", "summary", "-", "all immutable runtime artifacts verified",
                     "all required artifacts are prepared and verified", "none")
        for item in self.diagnostics:
            lines.append("diagnostic_begin")
            lines.extend(f"{key}={value}" for key, value in item.items())
            lines.append("diagnostic_end")
        return "\n".join(lines)


def _missing(report, phase, path, expected):
    report.add("not_prepared", phase, path, expected, "absent", "prepare")


def _unsafe(report, phase, path, expected, error):
    report.add("unsafe_blocking", phase, path, expected,
               f"validation_failed: {_clean(error)}", "stop")


def _regular_input(report, phase, path, expected, validator):
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        _missing(report, phase, path, expected)
        return False
    try:
        value = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
            raise ValueError(_observed(path))
        validator()
    except Exception as error:
        _unsafe(report, phase, path, expected, error)
        return False
    return True


def _validate_checkout_bound_wheel(lock, wheel, *, expected_lock,
                                   expected_name, expected_sha256,
                                   expected_directory, uid, gid, run):
    """Validate a prepositioned wheel before its immutable release exists."""
    lock = Path(lock)
    metadata = lock.lstat()
    if (lock.is_symlink() or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1 or lock.read_text() != expected_lock):
        raise ValueError("committed requirements lock is missing or changed")
    return runtime.validate_runtime_wheel(
        wheel, expected_name=expected_name, expected_sha256=expected_sha256,
        expected_directory=expected_directory, uid=uid, gid=gid, run=run)


def _incomplete_entries(report, parent, pattern, phase):
    parent = Path(parent)
    if not parent.exists() or parent.is_symlink() or not parent.is_dir():
        return
    try:
        entries = sorted(item for item in parent.iterdir() if item.name.startswith(pattern))
    except Exception as error:
        _unsafe(report, phase, parent, "readable directory with no incomplete staging entries", error)
        return
    for item in entries[:20]:
        report.add("unsafe_blocking", phase, item,
                   "no unverified interrupted staging artifact",
                   _observed(item), "stop and inspect")
    if len(entries) > 20:
        report.add("unsafe_blocking", phase, parent, "at most 20 inspectable interrupted entries",
                   f"more_than_20_entries count_at_least={len(entries)}", "stop and inspect")


def inspect(repo, runtime_root, commit, *, python=Path("/usr/bin/python3.14"),
            uid=0, gid=0, run=runtime.command):
    """Inspect readiness without creating, deleting, renaming, or chmodding anything."""
    repo, root, python = Path(repo), Path(runtime_root), Path(python)
    report = Report()

    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        report.add("unsafe_blocking", "git_checkout", repo,
                   "exact lowercase 40-character commit", repr(commit), "stop")
        return report
    try:
        verify_checkout(repo, commit, run=_readonly_git)
    except Exception as error:
        _unsafe(report, "git_checkout", repo,
                f"clean checkout at exact commit {commit}", error)
        return report

    try:
        runtime._safe_directory_node(root, {0o755}, uid, gid, run)
    except Exception as error:
        _unsafe(report, "runtime_root", root,
                f"non-symlink directory uid={uid} gid={gid} mode=0755 basic ACL not mounted",
                error)
        return report

    container_safe = {}
    for name, mode in CONTAINER_MODES.items():
        path = root / name
        if not path.exists() and not path.is_symlink():
            container_safe[name] = None
            continue
        try:
            runtime._safe_directory_node(path, {mode}, uid, gid, run)
            container_safe[name] = True
        except Exception as error:
            container_safe[name] = False
            _unsafe(report, "runtime_container", path,
                    f"non-symlink directory uid={uid} gid={gid} mode={mode:04o} basic ACL not mounted",
                    error)

    try:
        emoji_runtime.validate_host(python, run)
    except Exception as error:
        _unsafe(report, "host_runtime", python,
                "CPython 3.14 cpython-314-x86_64-linux-gnu with glibc >= 2.27", error)

    release = root / "releases" / commit
    release_available = release.is_dir() and not release.is_symlink()
    gunicorn_lock = ((release / "dev/intake/runtime-requirements.lock")
                     if release_available else
                     (repo / "dev/intake/runtime-requirements.lock"))
    gunicorn_wheel = (root / "wheelhouse" / runtime.VENV_NAME /
                       runtime.GUNICORN_WHEEL_NAME)
    if container_safe["wheelhouse"] is not False:
        _regular_input(
            report, "gunicorn_wheel", gunicorn_wheel,
            f"root-owned mode-0444 single-link wheel sha256={runtime.GUNICORN_WHEEL_SHA256}",
            lambda: (runtime.validate_locked_wheel(
                gunicorn_lock, gunicorn_wheel,
                expected_lock=runtime.GUNICORN_LOCK_TEXT,
                expected_name=runtime.GUNICORN_WHEEL_NAME,
                expected_sha256=runtime.GUNICORN_WHEEL_SHA256,
                expected_directory=runtime.VENV_NAME, uid=uid, gid=gid, run=run)
                if release_available else _validate_checkout_bound_wheel(
                    gunicorn_lock, gunicorn_wheel,
                    expected_lock=runtime.GUNICORN_LOCK_TEXT,
                    expected_name=runtime.GUNICORN_WHEEL_NAME,
                    expected_sha256=runtime.GUNICORN_WHEEL_SHA256,
                    expected_directory=runtime.VENV_NAME,
                    uid=uid, gid=gid, run=run)))

    emoji_lock = ((release / "dev/intake/emoji-sync-requirements.txt")
                  if release_available else
                  (repo / "dev/intake/emoji-sync-requirements.txt"))
    emoji_wheel = (root / "wheelhouse" / emoji_runtime.TARGET_NAME /
                   emoji_runtime.WHEEL_NAME)
    if container_safe["wheelhouse"] is not False:
        _regular_input(
            report, "pillow_wheel", emoji_wheel,
            f"root-owned mode-0444 single-link wheel sha256={emoji_runtime.WHEEL_SHA256}",
            lambda: (emoji_runtime.validate_inputs(
                emoji_lock, emoji_wheel, uid=uid, gid=gid, run=run)
                if release_available else _validate_checkout_bound_wheel(
                    emoji_lock, emoji_wheel,
                    expected_lock=emoji_runtime.REQUIREMENTS_TEXT,
                    expected_name=emoji_runtime.WHEEL_NAME,
                    expected_sha256=emoji_runtime.WHEEL_SHA256,
                    expected_directory=emoji_runtime.TARGET_NAME,
                    uid=uid, gid=gid, run=run)))

    release_ready = False
    if container_safe["releases"] is False:
        pass
    elif not release.exists() and not release.is_symlink():
        _missing(report, "release", release,
                 "verified root-owned immutable release for the exact commit")
    else:
        try:
            runtime.verify_release(release, commit)
            runtime.verify_release_ownership(release, uid, gid)
            release_ready = True
        except Exception as error:
            _unsafe(report, "release", release,
                    "verified root-owned immutable release for the exact commit", error)

    legacy_target = root / "venvs" / runtime.LEGACY_VENV_NAME
    selector = root / "venv"
    legacy_relevant = False
    if selector.exists() or selector.is_symlink():
        if not selector.is_symlink():
            _unsafe(report, "legacy_runtime_selector", selector,
                    "absent or exact symlink to the immutable legacy runtime",
                    _observed(selector))
        else:
            try:
                expected_link = Path("venvs") / runtime.LEGACY_VENV_NAME
                if selector.readlink() != expected_link:
                    raise ValueError(f"unexpected selector target {selector.readlink()}")
                if selector.resolve(strict=True) != legacy_target.resolve(strict=True):
                    raise ValueError("selector resolution mismatch")
                legacy_relevant = True
            except Exception as error:
                _unsafe(report, "legacy_runtime_selector", selector,
                        "exact symlink to the immutable legacy runtime", error)
    if legacy_target.exists() or legacy_target.is_symlink():
        try:
            record = runtime.validate_legacy_venv(
                legacy_target, uid=uid, gid=gid, run=run)
            report.add(
                "prepared", "legacy_gunicorn_runtime", legacy_target,
                "independently safe immutable predecessor runtime",
                ("valid_predecessor legacy_record=true relevant_to_selector="
                 f"{str(legacy_relevant).lower()} requirements_sha256="
                 f"{record['requirements_sha256']}"),
                "none")
        except Exception as error:
            _unsafe(report, "legacy_gunicorn_runtime", legacy_target,
                    "independently safe predecessor retained for rollback", error)
    elif legacy_relevant:
        _unsafe(report, "legacy_gunicorn_runtime", legacy_target,
                "selector target must exist and validate", "legacy runtime is absent")

    core_target = root / "venvs" / runtime.VENV_NAME
    if container_safe["venvs"] is False:
        pass
    elif not core_target.exists() and not core_target.is_symlink():
        _missing(report, "gunicorn_runtime", core_target,
                 "completed and verified versioned Gunicorn virtual environment")
    elif core_target.is_dir() and not core_target.is_symlink() and (
            (core_target / runtime.VENV_MARKER).exists()
            or (core_target / runtime.VENV_MARKER).is_symlink()):
        try:
            runtime.recover_incomplete_venv(root, core_target, uid=uid, gid=gid, run=run)
            report.add("recoverable_incomplete", "gunicorn_runtime", core_target,
                       "completed versioned Gunicorn virtual environment",
                       "verified incomplete artifact", "recover")
        except Exception as error:
            _unsafe(report, "gunicorn_runtime", core_target,
                    "completed or verifiably recoverable Gunicorn virtual environment", error)
    else:
        try:
            result = runtime.validate_venv(core_target, uid=uid, gid=gid, run=run)
            if result.get("requirements_sha256") != runtime.digest(gunicorn_lock):
                raise ValueError("Gunicorn runtime requirements digest mismatch")
        except Exception as error:
            _unsafe(report, "gunicorn_runtime", core_target,
                    "completed and verified versioned Gunicorn virtual environment", error)

    emoji_target = root / "venvs" / emoji_runtime.TARGET_NAME
    if container_safe["venvs"] is False:
        pass
    elif not emoji_target.exists() and not emoji_target.is_symlink():
        _missing(report, "emoji_runtime", emoji_target,
                 "completed and verified versioned Pillow virtual environment")
    elif emoji_target.is_dir() and not emoji_target.is_symlink() and (
            (emoji_target / emoji_runtime.MARKER).exists()
            or (emoji_target / emoji_runtime.MARKER).is_symlink()):
        try:
            emoji_runtime.recover_incomplete(root, emoji_target,
                                             uid=uid, gid=gid, run=run)
            report.add("recoverable_incomplete", "emoji_runtime", emoji_target,
                       "completed versioned Pillow virtual environment",
                       "verified incomplete artifact", "recover")
        except Exception as error:
            _unsafe(report, "emoji_runtime", emoji_target,
                    "completed or verifiably recoverable Pillow virtual environment", error)
    else:
        try:
            result = emoji_runtime.validate_runtime(
                emoji_target, uid=uid, gid=gid, approved_python={python}, run=run)
            if result.get("requirements_sha256") != runtime.digest(emoji_lock):
                raise ValueError("Pillow runtime requirements digest mismatch")
        except Exception as error:
            _unsafe(report, "emoji_runtime", emoji_target,
                    "completed and verified versioned Pillow virtual environment", error)

    staged_units = root / "staged-units" / commit
    staged_nginx = root / "staged-nginx" / commit
    for phase, path, expected in (
            ("staged_units", staged_units, "verified commit-scoped four-unit staging set"),
            ("staged_nginx", staged_nginx, "verified commit-scoped emoji Nginx route")):
        container = "staged-units" if phase == "staged_units" else "staged-nginx"
        if container_safe[container] is False:
            continue
        if not path.exists() and not path.is_symlink():
            _missing(report, phase, path, expected)
        elif path.is_symlink() or not path.is_dir():
            _unsafe(report, phase, path, expected, _observed(path))

    unit_stage_ready = (container_safe["staged-units"] is not False
                        and staged_units.is_dir() and not staged_units.is_symlink())
    nginx_stage_ready = (container_safe["staged-nginx"] is not False
                         and staged_nginx.is_dir() and not staged_nginx.is_symlink())
    for phase, path, ready, manifest, purpose in (
            ("staged_units", staged_units, unit_stage_ready, runtime.UNIT_MANIFEST,
             runtime.UNIT_STAGE_PURPOSE),
            ("staged_nginx", staged_nginx, nginx_stage_ready, runtime.ROUTE_MANIFEST,
             runtime.ROUTE_STAGE_PURPOSE)):
        if ready:
            try:
                value = runtime._verify_stage(path, manifest, purpose, commit)
                if not release_ready:
                    raise ValueError("commit-scoped stage exists without its verified release")
                if value.get("release_manifest_sha256") != runtime.digest(
                        release / "RELEASE-MANIFEST.json"):
                    raise ValueError("staged release digest mismatch")
            except Exception as error:
                _unsafe(report, phase, path, "verified commit-scoped staging artifact", error)

    if unit_stage_ready and nginx_stage_ready:
        try:
            runtime.verify_staged_deployment(root, commit)
        except Exception as error:
            _unsafe(report, "staged_deployment", root,
                    "matching verified unit and Nginx staging manifests", error)

    if container_safe["releases"] is not False:
        _incomplete_entries(report, root / "releases", ".release-", "release_staging")
    if container_safe["staged-units"] is not False:
        _incomplete_entries(report, root / "staged-units", ".stage-", "unit_staging")
    if container_safe["staged-nginx"] is not False:
        _incomplete_entries(report, root / "staged-nginx", ".stage-", "nginx_staging")

    for phase, path in (
            ("gunicorn_quarantine", root / "quarantine/incomplete-venvs"),
            ("emoji_quarantine", root / "quarantine/incomplete-emoji-venvs")):
        if container_safe["quarantine"] is not False and (path.exists() or path.is_symlink()):
            if path.is_symlink() or not path.is_dir():
                _unsafe(report, phase, path, "private non-symlink quarantine directory",
                        _observed(path))
            else:
                try:
                    runtime._safe_directory_node(path, {0o700}, uid, gid, run)
                    count = 0
                    for count, _item in enumerate(path.iterdir(), start=1):
                        if count >= 100:
                            break
                    if count:
                        report.add("prepared", phase, path,
                                   "bounded retained quarantine artifacts",
                                   f"present entries={count}", "none")
                except Exception as error:
                    _unsafe(report, phase, path, "readable private quarantine directory", error)

    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default="/srv/projects/nocturne-plugin-intake")
    parser.add_argument("--runtime-root", default="/srv/nocturne-plugin")
    parser.add_argument("--commit", required=True)
    parser.add_argument("--python", default="/usr/bin/python3.14")
    args = parser.parse_args(argv)
    try:
        report = inspect(args.repo, args.runtime_root, args.commit, python=Path(args.python))
    except Exception as error:
        report = Report()
        _unsafe(report, "readiness_check", args.runtime_root,
                "complete structured read-only inspection", error)
    print(report.render())
    return EXIT_CODES[report.state]


if __name__ == "__main__":
    raise SystemExit(main())
