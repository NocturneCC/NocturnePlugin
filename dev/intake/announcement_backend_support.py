"""Guarded announcement schema/admin installation; dry-run unless --apply is given."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from uuid import uuid4

from announcements import SCHEMA_OBJECTS, install_schema, schema_state
from derived_review_support import (_apply_metadata, _capture_safe_metadata,
                                    _verify_metadata)


BEGIN = "# NOCTURNE_PLUGIN_ANNOUNCEMENTS_BEGIN\n"
END = "# NOCTURNE_PLUGIN_ANNOUNCEMENTS_END\n"
REGISTRATION = (BEGIN
    + "from nocturne_announcements import create_admin_blueprint as _announcement_blueprint\n"
    + "app.register_blueprint(_announcement_blueprint(\n"
    + "    EVENT_SCHEDULE_DB, require_auth, _event_scheduler_allowed, current_admin_name))\n"
    + END)
PURPOSE = "nocturne_plugin_announcement_backend_v1"


def _digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def candidate_admin(original):
    anchors = ("EVENT_SCHEDULE_DB = ", "def _event_scheduler_allowed():", "def current_admin_name():")
    if any(original.count(anchor) != 1 for anchor in anchors):
        raise ValueError("active admin announcement integration anchors are missing or ambiguous")
    if BEGIN in original or END in original or "_announcement_blueprint" in original:
        raise ValueError("announcement registration is already or partially present")
    if not original.endswith("\n"):
        raise ValueError("active admin source must end with a newline")
    return original + "\n" + REGISTRATION


def _state(admin, module, source_module, database):
    admin_installed = admin.read_text().count(REGISTRATION) == 1
    module_installed = module.is_file() and not module.is_symlink() and _digest(module) == _digest(source_module)
    schema = schema_state(database)
    if not admin_installed and not module.exists() and schema == "not_applied":
        candidate_admin(admin.read_text())
        return "not_applied"
    if admin_installed and module_installed and schema == "already_applied":
        return "already_applied"
    raise ValueError("announcement backend is partially applied or differs from committed source")


def _fsync(path):
    with Path(path).open("rb") as value:
        os.fsync(value.fileno())


def _stage(target, content, metadata, run):
    descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.announcement-", dir=target.parent)
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        _apply_metadata(staged, metadata, run)
        return staged
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def _copy_verified(source, destination, metadata, run):
    shutil.copyfile(source, destination)
    _fsync(destination)
    _apply_metadata(destination, metadata, run)
    if _digest(source) != _digest(destination):
        raise ValueError("backup checksum verification failed")


def _backup_database(source, destination, metadata, run):
    with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as active:
        active.execute("PRAGMA query_only=ON")
        if active.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise ValueError("source announcement database integrity check failed")
        with closing(sqlite3.connect(destination)) as saved:
            active.backup(saved)
    _fsync(destination)
    _apply_metadata(destination, metadata, run)
    with closing(sqlite3.connect(f"file:{destination}?mode=ro", uri=True)) as saved:
        saved.execute("PRAGMA query_only=ON")
        if saved.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError("announcement database backup integrity check failed")


def _database_check(database):
    with closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        journal_mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        result = db.execute("PRAGMA quick_check").fetchone()
    if result != ("ok",):
        raise ValueError("event-schedule database quick_check failed")
    if journal_mode not in {"delete", "wal"}:
        raise ValueError("unsupported event-schedule database journal mode")
    return journal_mode


def _restore_database(saved, target, metadata, run):
    with closing(sqlite3.connect(f"file:{saved}?mode=ro", uri=True)) as backup:
        backup.execute("PRAGMA query_only=ON")
        with closing(sqlite3.connect(target, timeout=10)) as active:
            backup.backup(active)
    _apply_metadata(target, metadata, run)
    with closing(sqlite3.connect(f"file:{target}?mode=ro", uri=True)) as active:
        active.execute("PRAGMA query_only=ON")
        if active.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise ValueError("restored announcement database integrity check failed")


def _write_manifest(backup, manifest):
    path = backup / "MANIFEST.json"
    path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    _fsync(path)


def install(admin_app=Path("/srv/projects/api/admin_app.py"),
            module_target=Path("/srv/projects/api/nocturne_announcements.py"),
            database=Path("/srv/projects/database/event_schedule.db"),
            backup_root=Path("/etc/nocturne-plugin-backups"), source_module=None,
            *, apply=False, maintenance_confirmed=False, run=None, fail=None):
    from derived_review_support import _run
    run = run or _run
    admin, module, database, backup_root = map(Path, (admin_app, module_target, database, backup_root))
    source = Path(source_module or Path(__file__).with_name("announcements.py"))
    for path in (admin, database, source, backup_root):
        if not path.exists() or path.is_symlink():
            raise ValueError(f"missing or unsafe source/target: {path}")
    if not admin.is_file() or not database.is_file() or not source.is_file() or not backup_root.is_dir():
        raise ValueError("announcement installer paths have unexpected types")
    metadata = {
        "admin": _capture_safe_metadata(admin, run),
        "database": _capture_safe_metadata(database, run),
        "backup_root": _capture_safe_metadata(backup_root, run, directory=True),
    }
    state = _state(admin, module, source, database)
    journal_mode = _database_check(database)
    candidate = admin.read_text() if state == "already_applied" else candidate_admin(admin.read_text())
    report = {
        "state": state, "dry_run": not apply, "backup": None,
        "schema_objects": sorted(SCHEMA_OBJECTS),
        "admin_before_sha256": _digest(admin),
        "admin_after_sha256": hashlib.sha256(candidate.encode()).hexdigest(),
        "module_sha256": _digest(source),
        "database_size": database.stat().st_size,
        "database_journal_mode": journal_mode,
    }
    if state == "already_applied" or not apply:
        return report
    if not maintenance_confirmed:
        raise ValueError("apply requires --maintenance-confirmed after event-schedule writers are stopped")

    backup = backup_root / ("plugin-announcements-" + uuid4().hex[:8])
    staged = []
    mutation_started = False
    try:
        backup.mkdir(mode=0o700)
        saved_admin = backup / "admin_app.py.before"
        saved_database = backup / "event_schedule.db.before"
        _copy_verified(admin, saved_admin, metadata["admin"], run)
        _backup_database(database, saved_database, metadata["database"], run)
        manifest = {
            "purpose": PURPOSE, "status": "verified",
            "targets": {"admin": str(admin), "module": str(module), "database": str(database)},
            "before": {"admin": _digest(saved_admin), "database": _digest(saved_database),
                       "module": None},
            "installed_module_sha256": _digest(source),
            "metadata": metadata,
        }
        _write_manifest(backup, manifest)
        if fail:
            fail("after_backup")

        staged_admin = _stage(admin, candidate.encode(), metadata["admin"], run)
        staged.append(staged_admin)
        staged_module = _stage(module, source.read_bytes(), metadata["admin"], run)
        staged.append(staged_module)
        compile(staged_admin.read_bytes(), str(staged_admin), "exec")
        compile(staged_module.read_bytes(), str(staged_module), "exec")
        if fail:
            fail("after_stage")

        for name, target in (("admin", admin), ("database", database)):
            if _capture_safe_metadata(target, run) != metadata[name]:
                raise ValueError(f"target metadata changed after preflight: {target}")
        if module.exists() or _digest(admin) != report["admin_before_sha256"]:
            raise ValueError("active admin targets changed after preflight")

        mutation_started = True
        install_schema(database)
        if fail:
            fail("after_schema")
        os.replace(staged_module, module)
        staged.remove(staged_module)
        if fail:
            fail("after_module")
        os.replace(staged_admin, admin)
        staged.remove(staged_admin)
        if fail:
            fail("after_admin")
        if _state(admin, module, source, database) != "already_applied":
            raise ValueError("announcement backend final state verification failed")
        _verify_metadata(admin, metadata["admin"], run)
        _verify_metadata(module, metadata["admin"], run)
        _verify_metadata(database, metadata["database"], run)
        report.update(state="already_applied", dry_run=False, backup=str(backup))
        return report
    except BaseException as error:
        restoration_errors = []
        if mutation_started:
            try:
                restored_admin = _stage(admin, saved_admin.read_bytes(), metadata["admin"], run)
                os.replace(restored_admin, admin)
            except BaseException as caught:
                restoration_errors.append(f"admin: {caught}")
            try:
                if module.exists():
                    if module.is_symlink() or not module.is_file() or _digest(module) != _digest(source):
                        raise ValueError("refusing to remove changed installed module")
                    module.unlink()
            except BaseException as caught:
                restoration_errors.append(f"module: {caught}")
            try:
                _restore_database(saved_database, database, metadata["database"], run)
            except BaseException as caught:
                restoration_errors.append(f"database: {caught}")
        detail = f"announcement installation failed: {error}"
        if restoration_errors:
            detail += "; restoration errors: " + " | ".join(restoration_errors)
        raise RuntimeError(detail) from error
    finally:
        for path in staged:
            path.unlink(missing_ok=True)


def rollback(backup, admin_app=None, module_target=None, database=None, *,
             maintenance_confirmed=False, run=None):
    from derived_review_support import _run
    run = run or _run
    backup = Path(backup)
    if not backup.is_dir() or backup.is_symlink():
        raise ValueError("rollback backup must be an exact regular directory")
    manifest_path = backup / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("purpose") != PURPOSE or manifest.get("status") != "verified":
        raise ValueError("wrong or unverified announcement backup")
    if not maintenance_confirmed:
        raise ValueError("rollback requires --maintenance-confirmed after event-schedule writers are stopped")
    targets = manifest["targets"]
    admin = Path(admin_app or targets["admin"])
    module = Path(module_target or targets["module"])
    database = Path(database or targets["database"])
    if (str(admin) != targets["admin"] or str(module) != targets["module"]
            or str(database) != targets["database"]):
        raise ValueError("rollback targets differ from verified manifest")
    saved_admin, saved_database = backup / "admin_app.py.before", backup / "event_schedule.db.before"
    if _digest(saved_admin) != manifest["before"]["admin"] or _digest(saved_database) != manifest["before"]["database"]:
        raise ValueError("rollback backup checksum mismatch")
    source = Path(__file__).with_name("announcements.py")
    state = _state(admin, module, source, database)
    if state == "not_applied":
        return {"state": "not_applied", "already_restored": True}
    if _digest(module) != manifest["installed_module_sha256"]:
        raise ValueError("installed announcement module changed since apply")
    metadata = manifest["metadata"]
    restored_admin = _stage(admin, saved_admin.read_bytes(), metadata["admin"], run)
    os.replace(restored_admin, admin)
    module.unlink()
    _restore_database(saved_database, database, metadata["database"], run)
    if _state(admin, module, source, database) != "not_applied":
        raise RuntimeError("announcement rollback verification failed")
    return {"state": "not_applied", "already_restored": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--rollback-backup")
    parser.add_argument("--maintenance-confirmed", action="store_true")
    parser.add_argument("--admin-app", default="/srv/projects/api/admin_app.py")
    parser.add_argument("--module-target", default="/srv/projects/api/nocturne_announcements.py")
    parser.add_argument("--database", default="/srv/projects/database/event_schedule.db")
    parser.add_argument("--backup-dir", default="/etc/nocturne-plugin-backups")
    args = parser.parse_args()
    if args.apply and args.rollback_backup:
        raise SystemExit("choose --apply or --rollback-backup")
    result = (rollback(args.rollback_backup, args.admin_app, args.module_target, args.database,
                       maintenance_confirmed=args.maintenance_confirmed)
              if args.rollback_backup else install(args.admin_app, args.module_target, args.database,
                  args.backup_dir, apply=args.apply,
                  maintenance_confirmed=args.maintenance_confirmed))
    print(json.dumps(result, sort_keys=True))
    if not (args.apply or args.rollback_backup):
        print("Dry run only; no active file or database was changed.")


if __name__ == "__main__":
    main()
