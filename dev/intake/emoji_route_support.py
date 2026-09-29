"""Guarded emoji-route installation; dry-run by default and never reloads Nginx."""
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


ANCHOR_ROUTE = "/api/plugin/v1/announcements"
MANIFEST_ROUTE = "/api/plugin/v1/emojis"
MARKER = "    # Nocturne plugin development intake\n"
PURPOSE = "nocturne_plugin_emoji_nginx_routes_v1"


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _indented(snippet):
    return "\n".join("    " + line if line else "" for line in snippet.strip().splitlines()) + "\n"


def candidate_site(original, announcement_snippet, emoji_snippet):
    if original.count(MARKER) != 1 or original.count(ANCHOR_ROUTE) != 1:
        raise ValueError("expected installed announcement route exactly once")
    if MANIFEST_ROUTE in original or "/api/plugin/v1/emojis/assets/" in original:
        raise ValueError("emoji routes are already or partially present")
    installed = _indented(announcement_snippet)
    if original.count(installed) != 1:
        raise ValueError("active announcement route differs from committed source")
    return original.replace(installed, installed + "\n" + _indented(emoji_snippet), 1)


def install(target=Path("/etc/nginx/sites-enabled/nocturne"), source_dir=None,
            backup_root=Path("/etc/nocturne-plugin-backups"), *, apply=False,
            validate=lambda: subprocess.run(["/usr/sbin/nginx", "-t"], check=True, timeout=30)):
    target = Path(target)
    source_dir = Path(source_dir or Path(__file__).resolve().parent)
    backup_root = Path(backup_root)
    for path in (target, source_dir / "nginx-announcements-location.conf",
                 source_dir / "nginx-emojis-location.conf", backup_root):
        if not path.exists() or path.is_symlink():
            raise ValueError(f"missing or unsafe route source/target: {path}")
    if not target.is_file() or not backup_root.is_dir():
        raise ValueError("emoji route target paths have unexpected types")
    metadata = _capture_safe_metadata(target)
    original = target.read_bytes()
    installed = _indented((source_dir / "nginx-emojis-location.conf").read_text())
    if original.decode().count(installed) == 1:
        return {"dry_run": not apply, "state": "already_applied", "target": str(target),
                "sha256": _digest(original)}
    candidate = candidate_site(original.decode(),
        (source_dir / "nginx-announcements-location.conf").read_text(),
        (source_dir / "nginx-emojis-location.conf").read_text()).encode()
    result = {"dry_run": not apply, "state": "not_applied", "target": str(target),
              "before_sha256": _digest(original), "after_sha256": _digest(candidate)}
    if not apply:
        return result

    backup = backup_root / ("plugin-emoji-routes-" + uuid4().hex[:8])
    backup.mkdir(mode=0o700)
    saved = backup / "nocturne.before"
    shutil.copyfile(target, saved)
    _apply_metadata(saved, metadata)
    if saved.read_bytes() != original:
        raise ValueError("emoji route backup verification failed")
    _verify_metadata(saved, metadata)
    manifest = {"purpose": PURPOSE, "status": "verified", "target": str(target),
                "before_sha256": result["before_sha256"],
                "after_sha256": result["after_sha256"], "metadata": metadata}
    manifest_path = backup / "MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    with manifest_path.open("rb") as value:
        os.fsync(value.fileno())

    descriptor, staged_name = tempfile.mkstemp(prefix=".nocturne.emojis.", dir=target.parent)
    staged = Path(staged_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(candidate)
            output.flush()
            os.fsync(output.fileno())
        _apply_metadata(staged, metadata)
        if target.read_bytes() != original:
            raise ValueError("active Nginx target changed after emoji preflight")
        _verify_metadata(target, metadata)
        os.replace(staged, target)
        if target.read_bytes() != candidate:
            raise ValueError("emoji route activation verification failed")
        _verify_metadata(target, metadata)
        validate()
    except BaseException:
        if target.read_bytes() != original:
            restore = target.parent / (".nocturne.emojis.restore." + uuid4().hex)
            shutil.copyfile(saved, restore)
            _apply_metadata(restore, metadata)
            os.replace(restore, target)
            if target.read_bytes() != original:
                raise RuntimeError("emoji route restoration could not be verified")
            _verify_metadata(target, metadata)
        raise
    finally:
        staged.unlink(missing_ok=True)
    result.update(state="applied", dry_run=False, backup=str(backup))
    return result


def rollback(backup, target=None,
             validate=lambda: subprocess.run(["/usr/sbin/nginx", "-t"], check=True, timeout=30)):
    backup = Path(backup)
    if not backup.is_dir() or backup.is_symlink():
        raise ValueError("rollback backup must be an exact regular directory")
    manifest = json.loads((backup / "MANIFEST.json").read_text())
    if manifest.get("purpose") != PURPOSE or manifest.get("status") != "verified":
        raise ValueError("wrong or unverified emoji route backup")
    target = Path(target or manifest["target"])
    if str(target) != manifest["target"] or not target.is_file() or target.is_symlink():
        raise ValueError("rollback target differs from verified manifest")
    saved = backup / "nocturne.before"
    if _digest(saved.read_bytes()) != manifest["before_sha256"]:
        raise ValueError("emoji route backup checksum mismatch")
    current = _digest(target.read_bytes())
    if current == manifest["before_sha256"]:
        return {"state": "not_applied", "already_restored": True}
    if current != manifest["after_sha256"]:
        raise ValueError("active Nginx target changed since emoji route apply")
    metadata = manifest["metadata"]
    descriptor, staged_name = tempfile.mkstemp(prefix=".nocturne.emojis.rollback.",
                                                dir=target.parent)
    staged = Path(staged_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(saved.read_bytes())
            output.flush()
            os.fsync(output.fileno())
        _apply_metadata(staged, metadata)
        os.replace(staged, target)
        _verify_metadata(target, metadata)
        if _digest(target.read_bytes()) != manifest["before_sha256"]:
            raise RuntimeError("emoji route rollback verification failed")
        validate()
    finally:
        staged.unlink(missing_ok=True)
    return {"state": "not_applied", "already_restored": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback-backup")
    args = parser.parse_args()
    if args.apply and args.rollback_backup:
        raise SystemExit("choose --apply or --rollback-backup")
    result = rollback(args.rollback_backup) if args.rollback_backup else install(apply=args.apply)
    print(json.dumps(result, sort_keys=True))
    if not (args.apply or args.rollback_backup):
        print("Dry run only; no Nginx file or service was changed.")


if __name__ == "__main__":
    main()
