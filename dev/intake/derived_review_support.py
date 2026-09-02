"""Safely install derived-value review support; dry-run unless --apply is given."""
import argparse
from contextlib import closing
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from uuid import uuid4


COLUMNS = {
    "price_source": "TEXT",
    "valuation_rule_id": "TEXT",
    "valuation_catalogue_version": "INTEGER",
    "finished_output_item_id": "INTEGER",
    "finished_output_item_name": "TEXT",
    "finished_output_market_price_gp": "INTEGER",
    "derived_unit_price_gp": "INTEGER",
}
API_FIELDS = """                    price_source,
                    valuation_rule_id,
                    valuation_catalogue_version,
                    finished_output_item_id,
                    finished_output_item_name,
                    finished_output_market_price_gp,
                    derived_unit_price_gp,
"""
REVIEW_HELPER = '''
        function derivedValueDetails(sub) {
            const source = String(sub.price_source || "");
            if (!source.startsWith("runelite_derived_")) return "";
            return `<div class="meta-item" style="grid-column:1/-1">
                <span class="meta-label">Derived valuation</span>
                <span class="meta-value">
                    ${escHtml(sub.item_name || "")} → ${escHtml(sub.finished_output_item_name || "")}
                    (${formatGp(sub.finished_output_market_price_gp)})<br>
                    Rule ${escHtml(sub.valuation_rule_id || "")} · catalogue v${Number(sub.valuation_catalogue_version || 0)}
                    · derived ${formatGp(sub.derived_unit_price_gp)}
                </span>
            </div>`;
        }

'''
MIN_FREE_MARGIN = 512 * 1024 * 1024
LONG_CHECK_WARNING = ("The 1.5 GB database backup and integrity checks may take several minutes; "
                      "do not interrupt them.")


def candidate_admin(original):
    marker = '@app.route("/admin/api/nocturne/regular-submissions", methods=["GET"])'
    if original.count(marker) != 1:
        raise ValueError("admin route marker is missing or ambiguous")
    start = original.index(marker)
    end = original.index("\n@app.route", start + len(marker))
    route = original[start:end]
    anchor = "                    item_price,\n                    base_points,"
    if route.count(anchor) != 1 or "valuation_rule_id" in route:
        raise ValueError("admin regular-submission query differs from expected shape")
    route = route.replace(anchor, "                    item_price,\n" + API_FIELDS + "                    base_points,")
    return original[:start] + route + original[end:]


def candidate_review_page(original):
    helper_anchor = "        function buildCard(sub) {\n"
    value_anchor = '''                        <div class="meta-item">
                            <span class="meta-label">Item Value</span>
                            <span class="meta-value">${formatGp(sub.item_price)}</span>
                        </div>
'''
    if original.count(helper_anchor) != 1 or original.count(value_anchor) != 1 or "derivedValueDetails" in original:
        raise ValueError("review page differs from expected shape")
    changed = original.replace(helper_anchor, REVIEW_HELPER + helper_anchor, 1)
    return changed.replace(value_anchor, value_anchor + "                        ${derivedValueDetails(sub)}\n", 1)


def _run(command, **kwargs):
    return subprocess.run(command, check=True, timeout=30, **kwargs)


def _progress(message):
    print(message, flush=True)


def _acl(path, run=_run):
    return run(["getfacl", "-cp", str(path)], capture_output=True, text=True).stdout


def _metadata(path, run=_run):
    before = path.lstat()
    acl = _acl(path, run)
    after = path.lstat()
    identity = lambda value: (value.st_dev, value.st_ino, value.st_uid, value.st_gid,
                              stat.S_IFMT(value.st_mode), stat.S_IMODE(value.st_mode))
    if identity(before) != identity(after):
        raise ValueError(f"metadata changed while inspecting {path}")
    return {"uid": after.st_uid, "gid": after.st_gid,
            "mode": stat.S_IMODE(after.st_mode), "acl": acl}


def _permissions(value):
    if (len(value) != 3 or value[0] not in "r-" or
            value[1] not in "w-" or value[2] not in "x-"):
        raise ValueError(f"invalid ACL permissions: {value}")
    return sum(bit for character, bit in zip(value, (4, 2, 1)) if character != "-")


def _validate_safe_metadata(path, metadata, *, directory=False):
    value = path.lstat()
    expected_type = stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode)
    if not expected_type or stat.S_ISLNK(value.st_mode):
        raise ValueError(f"unsafe target type or symlink: {path}")
    if metadata["mode"] & 0o002:
        raise ValueError(f"world-writable target is unsafe: {path}")
    if metadata["mode"] & 0o7000 and not directory:
        raise ValueError(f"special permission bits are unsafe: {path}")
    try:
        pwd.getpwuid(metadata["uid"])
        grp.getgrgid(metadata["gid"])
    except KeyError as error:
        raise ValueError(f"target has unresolved ownership: {path}") from error

    acl = {}
    named = []
    for raw_line in metadata["acl"].splitlines():
        line = raw_line.split("#", 1)[0].rstrip()
        if not line:
            continue
        parts = line.split(":")
        if len(parts) != 3 or parts[0] not in ("user", "group", "mask", "other"):
            raise ValueError(f"unrecognized ACL entry for {path}: {raw_line}")
        kind, principal, permissions = parts
        bits = _permissions(permissions)
        if principal:
            if kind not in ("user", "group"):
                raise ValueError(f"unsafe named ACL entry for {path}: {raw_line}")
            named.append((raw_line, bits))
        else:
            if kind in acl:
                raise ValueError(f"duplicate ACL entry for {path}: {kind}")
            acl[kind] = bits
    owner_bits = (metadata["mode"] >> 6) & 7
    group_bits = (metadata["mode"] >> 3) & 7
    other_bits = metadata["mode"] & 7
    if acl.get("user") != owner_bits or acl.get("other") != other_bits:
        raise ValueError(f"ACL owner/other entries disagree with mode for {path}")
    if named:
        if set(acl) != {"user", "group", "mask", "other"} or acl["mask"] != group_bits:
            raise ValueError(f"named ACL mask disagrees with mode for {path}")
        for raw_line, bits in named:
            if bits & ~group_bits:
                raise ValueError(f"unsafe named ACL grant for {path}: {raw_line}")
    elif set(acl) != {"user", "group", "other"} or acl["group"] != group_bits:
        raise ValueError(f"ACL group entry disagrees with mode for {path}")
    return metadata


def _capture_safe_metadata(path, run=_run, *, directory=False):
    return _validate_safe_metadata(path, _metadata(path, run), directory=directory)


def _verify_metadata(path, expected, run=_run):
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode) or stat.S_ISLNK(value.st_mode):
        raise ValueError(f"unsafe replacement type or symlink: {path}")
    actual = _metadata(path, run)
    if actual != expected:
        raise ValueError(f"unexpected ownership, mode, or ACL for {path}: {actual}")


def _apply_metadata(path, expected, run=_run):
    os.chown(path, expected["uid"], expected["gid"])
    os.chmod(path, expected["mode"])
    run(["setfacl", "--set-file=-", str(path)], input=expected["acl"], text=True)
    _verify_metadata(path, expected, run)


def _columns(database):
    uri = f"file:{database}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        return {row[1]: row[2].upper() for row in db.execute("PRAGMA table_info(regular_submissions)")}


def _database_check(database):
    uri = f"file:{database}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        journal_mode = db.execute("PRAGMA journal_mode").fetchone()[0]
        result = [row[0] for row in db.execute("PRAGMA quick_check")]
    if result != ["ok"]:
        raise ValueError(f"SQLite quick_check failed for {database}: {result[:3]}")
    return journal_mode


def _state(admin_text, review_text, columns):
    schema = {name: columns.get(name) for name in COLUMNS}
    absent = all(value is None for value in schema.values())
    complete = schema == COLUMNS
    admin_old = "valuation_rule_id" not in admin_text
    review_old = "derivedValueDetails" not in review_text
    if admin_old and review_old and absent:
        candidate_admin(admin_text)
        candidate_review_page(review_text)
        return "not_applied"
    if not admin_old and not review_old and complete:
        marker = '@app.route("/admin/api/nocturne/regular-submissions", methods=["GET"])'
        if admin_text.count(marker) != 1:
            raise ValueError("applied admin route marker is missing or ambiguous")
        start = admin_text.index(marker)
        end = admin_text.index("\n@app.route", start + len(marker))
        route = admin_text[start:end]
        admin_exact = all(route.count(f"                    {name},") == 1 for name in COLUMNS)
        review_exact = (review_text.count("function derivedValueDetails(sub)") == 1 and
                        review_text.count("${derivedValueDetails(sub)}") == 1 and
                        all(review_text.count(name) >= 1 for name in (
                            "finished_output_item_name", "finished_output_market_price_gp",
                            "valuation_rule_id", "derived_unit_price_gp")))
        if not admin_exact or not review_exact:
            raise ValueError("applied file markers are ambiguous")
        return "already_applied"
    raise ValueError("ambiguous partially applied derived-review state")


def _digest(path):
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _fsync(path):
    with path.open("rb") as value:
        os.fsync(value.fileno())


def _backup_status(backup, state, detail):
    content = json.dumps({"state": state, "detail": detail}, sort_keys=True) + "\n"
    descriptor, name = tempfile.mkstemp(prefix=".BACKUP_STATUS-", dir=backup)
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "w") as value:
            value.write(content)
            value.flush()
            os.fsync(value.fileno())
        os.replace(staged, backup / "BACKUP_STATUS.json")
    finally:
        staged.unlink(missing_ok=True)


def _stage_bytes(target, content, metadata, run=_run):
    descriptor, name = tempfile.mkstemp(prefix=f".{target.name}.derived-", dir=target.parent)
    staged = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as value:
            value.write(content)
            value.flush()
            os.fsync(value.fileno())
        _apply_metadata(staged, metadata, run)
        return staged
    except Exception:
        staged.unlink(missing_ok=True)
        raise


def _backup_file(source, destination, metadata, run=_run):
    shutil.copyfile(source, destination)
    _fsync(destination)
    _apply_metadata(destination, metadata, run)
    if _digest(source) != _digest(destination):
        raise ValueError(f"backup verification failed for {source}")


def _backup_database(source, destination, metadata, run=_run):
    with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as original:
        original.execute("PRAGMA query_only=ON")
        with closing(sqlite3.connect(destination)) as saved:
            original.backup(saved)
    _fsync(destination)
    _apply_metadata(destination, metadata, run)


def _restore_file(saved, target, metadata, run=_run):
    staged = _stage_bytes(target, saved.read_bytes(), metadata, run)
    os.replace(staged, target)
    if _digest(saved) != _digest(target):
        raise ValueError(f"restoration verification failed for {target}")
    _verify_metadata(target, metadata, run)


def _restore_database(saved, target, metadata, run=_run):
    with closing(sqlite3.connect(f"file:{saved}?mode=ro", uri=True)) as backup:
        backup.execute("PRAGMA query_only=ON")
        with closing(sqlite3.connect(target)) as active:
            backup.backup(active)
    _apply_metadata(target, metadata, run)
    _database_check(target)
    if _columns(saved) != _columns(target):
        raise ValueError("database restoration verification failed")


def migrate_database(database, fail=None):
    """Apply all seven additions in one explicit transaction."""
    with closing(sqlite3.connect(database)) as db:
        try:
            db.execute("BEGIN EXCLUSIVE")
            existing = {row[1]: row[2].upper() for row in db.execute("PRAGMA table_info(regular_submissions)")}
            if not existing or any(name in existing for name in COLUMNS):
                raise ValueError("schema changed since preflight")
            for name, kind in COLUMNS.items():
                db.execute(f'ALTER TABLE regular_submissions ADD COLUMN "{name}" {kind}')
            if fail:
                fail("schema_application")
            db.commit()
        except BaseException:
            db.rollback()
            raise


def install(admin_app, review_page, database, backup_dir, *, apply=False,
            admin_python="/srv/projects/api/venv-simon/bin/python",
            source_files=None, disk_usage=shutil.disk_usage, run=_run, fail=None,
            progress=_progress):
    """Preflight and optionally apply; restore all targets after any mutating failure."""
    paths = {"admin_app": Path(admin_app), "review_page": Path(review_page),
             "database": Path(database), "backup_dir": Path(backup_dir)}
    sources = list(map(Path, source_files or (
        Path(__file__), Path(__file__).parents[2] / "shared/derived-value-catalogue.json",
        Path(__file__).with_name("derived_values.py"))))
    for path in list(paths.values()) + sources:
        if not path.exists() or path.is_symlink() or (path != paths["backup_dir"] and not path.is_file()):
            raise ValueError(f"missing, wrong-type, or symlinked source/target: {path}")
    admin_python_path = Path(admin_python)
    if not admin_python_path.exists() or not admin_python_path.resolve().is_file() or not os.access(admin_python_path, os.X_OK):
        raise ValueError(f"active admin interpreter is missing or not executable: {admin_python_path}")
    if not paths["backup_dir"].is_dir():
        raise ValueError(f"backup path is not a directory: {paths['backup_dir']}")
    captured = {name: _capture_safe_metadata(
        path, run, directory=(name == "backup_dir")) for name, path in paths.items()}

    admin_text = paths["admin_app"].read_text()
    review_text = paths["review_page"].read_text()
    columns = _columns(paths["database"])
    state = _state(admin_text, review_text, columns)
    progress(LONG_CHECK_WARNING)
    progress("Source database integrity check: starting")
    try:
        if fail:
            fail("source_database_integrity")
        journal_mode = _database_check(paths["database"])
    except BaseException:
        progress("Installation interrupted or failed during preflight before backup creation or mutation; "
                 "active targets were untouched")
        raise
    progress("Source database integrity check: completed")
    database_size = paths["database"].stat().st_size
    required_free = database_size + max(MIN_FREE_MARGIN, database_size // 4)
    free = disk_usage(paths["backup_dir"]).free
    report = {"state": state, "dry_run": not apply, "database_size": database_size,
              "database_journal_mode": journal_mode, "free_space": free,
              "required_free_space": required_free, "backup": None}
    if state == "not_applied" and free < required_free:
        raise ValueError(f"insufficient backup space: need {required_free}, have {free}")
    if state == "already_applied" or not apply:
        return report

    new_admin = candidate_admin(admin_text).encode()
    new_review = candidate_review_page(review_text).encode()
    backup = paths["backup_dir"] / ("derived-values-" + uuid4().hex[:8])
    staged = []
    mutation_started = False
    backup_verified = False
    try:
        backup.mkdir(mode=0o700)
        _backup_status(backup, "incomplete", "Backup creation or verification has not completed")
        progress(f"Backup directory: {backup} (marked incomplete until every verification passes)")
        saved_admin, saved_review, saved_database = (backup / paths[name].name for name in
                                                      ("admin_app", "review_page", "database"))
        _backup_file(paths["admin_app"], saved_admin, captured["admin_app"], run)
        _backup_file(paths["review_page"], saved_review, captured["review_page"], run)
        progress(LONG_CHECK_WARNING)
        progress("Database backup copy: starting")
        if fail:
            fail("database_backup_copy")
        _backup_database(paths["database"], saved_database, captured["database"], run)
        progress("Database backup copy: completed")
        progress("Backup checksum: starting")
        if fail:
            fail("backup_checksum")
        manifest = {saved_admin.name: _digest(saved_admin), saved_review.name: _digest(saved_review),
                    saved_database.name: _digest(saved_database)}
        (backup / "SHA256SUMS.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
        _fsync(backup / "SHA256SUMS.json")
        progress("Backup checksum: completed")
        progress(LONG_CHECK_WARNING)
        progress("Backup database integrity check: starting")
        if fail:
            fail("backup_database_integrity")
        _database_check(saved_database)
        if _columns(paths["database"]) != _columns(saved_database):
            raise ValueError("database backup schema verification failed")
        progress("Backup database integrity check: completed")
        _backup_status(backup, "verified", "All backup copies, checksums, and integrity checks completed")
        backup_verified = True
        if fail:
            fail("after_backup")

        progress("Staged-file validation: starting")
        staged_admin = _stage_bytes(paths["admin_app"], new_admin, captured["admin_app"], run)
        staged.append(staged_admin)
        candidate = staged_admin.read_text()
        if candidate.count("valuation_rule_id") < 1:
            raise ValueError("staged admin markers failed validation")
        syntax_check = ("from pathlib import Path; import sys; "
                        "compile(Path(sys.argv[1]).read_bytes(), sys.argv[1], 'exec')")
        run([str(admin_python), "-B", "-c", syntax_check, str(staged_admin)], capture_output=True, text=True)
        if fail:
            fail("after_admin_stage")
        staged_review = _stage_bytes(paths["review_page"], new_review, captured["review_page"], run)
        staged.append(staged_review)
        if staged_review.read_text().count("derivedValueDetails") != 2:
            raise ValueError("staged review markers failed validation")
        if fail:
            fail("after_review_stage")
        progress("Staged-file validation: completed")

        for name, path in paths.items():
            current = _capture_safe_metadata(path, run, directory=(name == "backup_dir"))
            if current != captured[name]:
                raise ValueError(f"metadata changed after preflight for {path}")
        mutation_started = True
        progress("Schema transaction: starting")
        migrate_database(paths["database"], fail)
        progress("Schema transaction: completed")
        if fail:
            fail("after_schema")
        progress("Active admin file activation: starting")
        os.replace(staged_admin, paths["admin_app"])
        staged.remove(staged_admin)
        progress("Active admin file activation: completed")
        if fail:
            fail("first_replacement")
        progress("Active review file activation: starting")
        os.replace(staged_review, paths["review_page"])
        staged.remove(staged_review)
        progress("Active review file activation: completed")
        if fail:
            fail("second_replacement")
        progress("Final verification: starting")
        _verify_metadata(paths["admin_app"], captured["admin_app"], run)
        _verify_metadata(paths["review_page"], captured["review_page"], run)
        _verify_metadata(paths["database"], captured["database"], run)
        if _state(paths["admin_app"].read_text(), paths["review_page"].read_text(),
                  _columns(paths["database"])) != "already_applied":
            raise ValueError("post-install state verification failed")
        _database_check(paths["database"])
        if fail:
            fail("final_verification")
        progress("Final verification: completed")
        report.update(state="already_applied", dry_run=False, backup=str(backup))
        return report
    except BaseException as error:
        restoration = "not_required"
        rollback_errors = []
        if not backup_verified and backup.is_dir():
            try:
                _backup_status(backup, "incomplete", f"Interrupted or failed before verification: {type(error).__name__}")
            except BaseException as caught:
                rollback_errors.append(f"backup status: {caught}")
        if mutation_started:
            progress(f"Installation interrupted or failed after mutation began; rollback starting: {type(error).__name__}")
            restorations = (
                ("admin_app", lambda: _restore_file(
                    saved_admin, paths["admin_app"], captured["admin_app"], run)),
                ("review_page", lambda: _restore_file(
                    saved_review, paths["review_page"], captured["review_page"], run)),
                ("database", lambda: _restore_database(
                    saved_database, paths["database"], captured["database"], run)),
            )
            for name, restore in restorations:
                try:
                    restore()
                except BaseException as caught:
                    rollback_errors.append(f"{name}: {caught}")
            try:
                if _state(paths["admin_app"].read_text(), paths["review_page"].read_text(),
                          _columns(paths["database"])) != "not_applied":
                    raise ValueError("rolled-back state is not consistently old")
            except BaseException as caught:
                rollback_errors.append(f"final verification: {caught}")
            restoration = "FAILED" if rollback_errors else "succeeded"
            progress(f"Rollback restoration: {restoration}")
        else:
            backup_description = ("verified" if backup_verified else
                                  "incomplete/unverified" if backup.is_dir() else "not created")
            progress(f"Installation interrupted or failed before mutation; active targets were untouched; "
                     f"backup={backup} ({backup_description})")
        message = f"installation failed; mutation_started={mutation_started}; restoration={restoration}; cause={error}"
        if rollback_errors:
            message += "; rollback_errors=" + " | ".join(rollback_errors)
        if isinstance(error, Exception):
            raise RuntimeError(message) from error
        raise
    finally:
        for path in staged:
            path.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--admin-app", default="/srv/projects/api/admin_app.py")
    parser.add_argument("--review-page", default="/srv/projects/website/regular-submissions-review.html")
    parser.add_argument("--database", default="/srv/projects/database/RegularSubmissions.db")
    parser.add_argument("--backup-dir", default="/etc/nocturne-plugin-backups")
    args = parser.parse_args()
    result = install(args.admin_app, args.review_page, args.database, args.backup_dir, apply=args.apply)
    print(json.dumps(result, sort_keys=True))
    if not args.apply:
        print("Dry run only; no files or database were changed.")


if __name__ == "__main__":
    main()
