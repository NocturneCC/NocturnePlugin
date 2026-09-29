"""Guarded emoji systemd-unit staging; dry-run by default, never reloads systemd."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from uuid import uuid4

from derived_review_support import (_apply_metadata, _capture_safe_metadata,
                                    _verify_metadata)


PURPOSE = "nocturne_plugin_emoji_systemd_units_v1"
UNITS = ("nocturne-plugin-dev.service", "nocturne-plugin-emoji-sync.service",
         "nocturne-plugin-emoji-sync.timer")
NEW_METADATA = {"uid": 0, "gid": 0, "mode": 0o644,
                "acl": "user::rw-\ngroup::r--\nother::r--\n"}
STOP_CONFIRMATION = ("nocturne-plugin-dev.service, nocturne-plugin-emoji-sync.service, "
                     "and nocturne-plugin-emoji-sync.timer must be stopped for apply or rollback")


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _sources(source_dir):
    source_dir = Path(source_dir)
    result = {}
    for name in UNITS:
        path = source_dir / name
        if not path.is_file() or path.is_symlink() or path.stat().st_nlink != 1:
            raise ValueError(f"missing or unsafe unit source: {path}")
        result[name] = path.read_bytes()
    return result


def _validate_units(paths):
    subprocess.run(["systemd-analyze", "verify", *map(str, paths)], check=True, timeout=30)


def install(target_dir=Path("/etc/systemd/system"), source_dir=None,
            backup_root=Path("/etc/nocturne-plugin-backups"), *, apply=False,
            confirmed_services_stopped=False, validate=_validate_units):
    target_dir = Path(target_dir)
    source_dir = Path(source_dir or Path(__file__).resolve().parent)
    backup_root = Path(backup_root)
    if not target_dir.is_dir() or target_dir.is_symlink() \
            or not backup_root.is_dir() or backup_root.is_symlink():
        raise ValueError("unsafe unit target or backup directory")
    sources = _sources(source_dir)
    validate([source_dir / name for name in UNITS])
    entries = []
    for name in UNITS:
        target = target_dir / name
        if target.exists() and (not target.is_file() or target.is_symlink()
                                or target.stat().st_nlink != 1):
            raise ValueError(f"unsafe unit target: {target}")
        before = target.read_bytes() if target.exists() else None
        entries.append({"name": name, "target": str(target), "existed": before is not None,
                        "before_sha256": None if before is None else _digest(before),
                        "after_sha256": _digest(sources[name]),
                        "state": "already_applied" if before == sources[name] else "not_applied"})
    result = {"dry_run": not apply, "state": "already_applied" if all(
        entry["state"] == "already_applied" for entry in entries) else "not_applied",
              "required_stop_confirmation": STOP_CONFIRMATION, "units": entries}
    if not apply or result["state"] == "already_applied":
        return result
    if not confirmed_services_stopped:
        raise ValueError(STOP_CONFIRMATION)

    backup = backup_root / ("plugin-emoji-units-" + uuid4().hex[:8])
    backup.mkdir(mode=0o700)
    manifest_entries = []
    for entry in entries:
        target = Path(entry["target"])
        metadata = _capture_safe_metadata(target) if entry["existed"] else NEW_METADATA
        saved_name = None
        if entry["existed"]:
            saved = backup / (entry["name"] + ".before")
            shutil.copyfile(target, saved)
            _apply_metadata(saved, metadata)
            _verify_metadata(saved, metadata)
            if _digest(saved.read_bytes()) != entry["before_sha256"]:
                raise ValueError("unit backup verification failed")
            saved_name = saved.name
        manifest_entries.append(dict(entry, metadata=metadata, backup=saved_name))
    manifest = {"purpose": PURPOSE, "status": "verified", "entries": manifest_entries,
                "stop_confirmation": STOP_CONFIRMATION}
    manifest_path = backup / "MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    with manifest_path.open("rb") as value:
        os.fsync(value.fileno())

    changed = []
    try:
        for entry in manifest_entries:
            if entry["state"] == "already_applied":
                continue
            target = Path(entry["target"])
            if entry["existed"] and _digest(target.read_bytes()) != entry["before_sha256"]:
                raise ValueError("unit target changed after preflight")
            descriptor, staged_name = tempfile.mkstemp(prefix=".nocturne.emoji-unit.",
                                                        dir=target_dir)
            staged = Path(staged_name)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(sources[entry["name"]])
                    output.flush()
                    os.fsync(output.fileno())
                _apply_metadata(staged, entry["metadata"])
                # Record the target before the atomic replacement so every
                # interruption at or after replace is covered by restoration.
                changed.append(entry)
                os.replace(staged, target)
                _verify_metadata(target, entry["metadata"])
                if _digest(target.read_bytes()) != entry["after_sha256"]:
                    raise ValueError("unit activation verification failed")
            finally:
                staged.unlink(missing_ok=True)
        validate([Path(entry["target"]) for entry in manifest_entries])
    except BaseException:
        _restore(changed, backup)
        raise
    result.update(state="applied", dry_run=False, backup=str(backup))
    return result


def _restore(entries, backup):
    for entry in reversed(entries):
        target = Path(entry["target"])
        if entry["existed"]:
            saved = backup / entry["backup"]
            descriptor, staged_name = tempfile.mkstemp(prefix=".nocturne.emoji-restore.",
                                                        dir=target.parent)
            staged = Path(staged_name)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(saved.read_bytes())
                    output.flush()
                    os.fsync(output.fileno())
                _apply_metadata(staged, entry["metadata"])
                os.replace(staged, target)
                _verify_metadata(target, entry["metadata"])
            finally:
                staged.unlink(missing_ok=True)
        else:
            target.unlink(missing_ok=True)


def rollback(backup, *, confirmed_services_stopped=False, validate=_validate_units):
    if not confirmed_services_stopped:
        raise ValueError(STOP_CONFIRMATION)
    backup = Path(backup)
    if not backup.is_dir() or backup.is_symlink():
        raise ValueError("unsafe unit rollback backup")
    manifest = json.loads((backup / "MANIFEST.json").read_text())
    if manifest.get("purpose") != PURPOSE or manifest.get("status") != "verified":
        raise ValueError("wrong or unverified unit backup")
    entries = manifest.get("entries")
    if not isinstance(entries, list) or {entry.get("name") for entry in entries} != set(UNITS):
        raise ValueError("invalid unit backup manifest")
    for entry in entries:
        target = Path(entry["target"])
        if not target.is_file() or target.is_symlink() or target.stat().st_nlink != 1 \
                or _digest(target.read_bytes()) != entry["after_sha256"]:
            raise ValueError("unit target changed since apply")
        if entry["existed"]:
            saved = backup / entry["backup"]
            if not saved.is_file() or saved.is_symlink() or saved.stat().st_nlink != 1 \
                    or _digest(saved.read_bytes()) != entry["before_sha256"]:
                raise ValueError("unit rollback backup checksum mismatch")
    applied = {entry["name"]: Path(entry["target"]).read_bytes() for entry in entries}
    try:
        _restore(entries, backup)
        remaining = [Path(entry["target"]) for entry in entries if entry["existed"]]
        if remaining:
            validate(remaining)
    except BaseException:
        for entry in entries:
            target = Path(entry["target"])
            descriptor, staged_name = tempfile.mkstemp(prefix=".nocturne.emoji-reapply.",
                                                        dir=target.parent)
            staged = Path(staged_name)
            try:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(applied[entry["name"]])
                    output.flush()
                    os.fsync(output.fileno())
                _apply_metadata(staged, entry["metadata"])
                os.replace(staged, target)
                _verify_metadata(target, entry["metadata"])
                if _digest(target.read_bytes()) != entry["after_sha256"]:
                    raise RuntimeError("unit applied-state restoration failed")
            finally:
                staged.unlink(missing_ok=True)
        raise
    return {"state": "not_applied", "restored": len(remaining),
            "removed": len(entries) - len(remaining)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback-backup")
    parser.add_argument("--confirm-services-stopped", action="store_true")
    args = parser.parse_args()
    if args.apply and args.rollback_backup:
        raise SystemExit("choose --apply or --rollback-backup")
    result = (rollback(args.rollback_backup,
                       confirmed_services_stopped=args.confirm_services_stopped)
              if args.rollback_backup else install(apply=args.apply,
                  confirmed_services_stopped=args.confirm_services_stopped))
    print(json.dumps(result, sort_keys=True))
    if not (args.apply or args.rollback_backup):
        print("Dry run only; no unit, daemon state, or service was changed.")


if __name__ == "__main__":
    main()
