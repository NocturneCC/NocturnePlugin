"""Prepare the exact Nginx route needed by raid-presence v1. Dry-run by default.

This tool changes one explicit regular file and never reloads Nginx or controls a
service. The operator retains explicit syntax-test, reload, and rollback control.
"""
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

OLD_ROUTE = "/api/plugin/dev/drops"
NEW_ROUTE = "/api/plugin/dev/raid-presence"
MARKER = "    # Nocturne plugin development intake\n"


def _indented(snippet):
    return "\n".join("    " + line if line else "" for line in snippet.strip().splitlines()) + "\n"


def candidate_site(original, drop_snippet, presence_snippet):
    if original.count(OLD_ROUTE) != 1 or original.count(MARKER) != 1:
        raise ValueError("expected installed development intake route exactly once")
    if NEW_ROUTE in original:
        raise ValueError("raid-presence route is already present")
    installed_drop = _indented(drop_snippet)
    if original.count(installed_drop) != 1:
        raise ValueError("active drop route differs from the committed source")
    return original.replace(installed_drop, installed_drop + "\n" + _indented(presence_snippet), 1)


def install(target=Path("/etc/nginx/sites-enabled/nocturne"), source_dir=None,
            backup_root=Path("/etc/nocturne-plugin-backups"), apply=False,
            validate=lambda: subprocess.run(["/usr/sbin/nginx", "-t"], check=True, timeout=30)):
    target = Path(target)
    source_dir = Path(source_dir or Path(__file__).resolve().parent)
    metadata = _capture_safe_metadata(target)
    original = target.read_bytes()
    installed_presence = _indented((source_dir / "nginx-raid-presence-location.conf").read_text())
    if original.decode().count(installed_presence) == 1:
        return {"dry_run": not apply, "state": "already_applied", "target": str(target),
                "sha256": hashlib.sha256(original).hexdigest()}
    candidate = candidate_site(original.decode(),
        (source_dir / "nginx-location.conf").read_text(),
        (source_dir / "nginx-raid-presence-location.conf").read_text()).encode()
    result = {"dry_run": not apply, "state": "not_applied", "target": str(target),
              "before_sha256": hashlib.sha256(original).hexdigest(),
              "after_sha256": hashlib.sha256(candidate).hexdigest()}
    if not apply:
        return result
    backup_root = Path(backup_root)
    backup_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    backup = backup_root / ("raid-presence-route-" + uuid4().hex[:8])
    backup.mkdir(mode=0o700)
    saved = backup / "nocturne.before"
    shutil.copyfile(target, saved)
    _apply_metadata(saved, metadata)
    if saved.read_bytes() != original:
        raise ValueError("backup verification failed")
    _verify_metadata(saved, metadata)
    manifest = {"purpose": "raid_presence_nginx_route", "status": "verified",
                "target": str(target), "sha256": result["before_sha256"], "metadata": metadata}
    (backup / "MANIFEST.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
    fd, staged_name = tempfile.mkstemp(prefix=".nocturne.raid-presence.", dir=target.parent)
    staged = Path(staged_name)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(candidate); output.flush(); os.fsync(output.fileno())
        _apply_metadata(staged, metadata)
        if target.read_bytes() != original:
            raise ValueError("active target changed after preflight")
        _verify_metadata(target, metadata)
        os.replace(staged, target)
        if target.read_bytes() != candidate:
            raise ValueError("activated route verification failed")
        _verify_metadata(target, metadata)
        validate()
    except BaseException:
        if target.read_bytes() != original:
            restore = target.parent / (".nocturne.raid-presence.restore." + uuid4().hex)
            shutil.copyfile(saved, restore); _apply_metadata(restore, metadata); os.replace(restore, target)
            if target.read_bytes() != original:
                raise RuntimeError("route activation failed and restoration could not be verified")
            _verify_metadata(target, metadata)
        raise
    finally:
        staged.unlink(missing_ok=True)
    result.update(state="applied", backup=str(backup))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(install(apply=args.apply), sort_keys=True))
    if not args.apply: print("Dry run only; no Nginx file or service was changed.")


if __name__ == "__main__":
    main()
