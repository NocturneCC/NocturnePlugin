"""Safely install derived-value review support; dry-run unless --apply is given."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
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
EXPECTED = {
    "admin_app": {"uid": 65534, "gid": 65534, "mode": 0o775,
                  "acl": "user::rwx\ngroup::rwx\nother::r-x\n"},
    "review_page": {"uid": 1001, "gid": 65534, "mode": 0o664,
                    "acl": "user::rw-\nuser:4294967295:rw-\ngroup::r--\nmask::rw-\nother::r--\n"},
    "database": {"uid": 65534, "gid": 65534, "mode": 0o664,
                 "acl": "user::rw-\nuser:4294967295:rw-\ngroup::rw-\nmask::rw-\nother::r--\n"},
    "backup_dir": {"uid": 65534, "gid": 65534, "mode": 0o755,
                   "acl": "user::rwx\ngroup::r-x\nother::r-x\n"},
}


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


def _acl(path, run=_run):
    return run(["getfacl", "-cp", str(path)], capture_output=True, text=True).stdout


def _metadata(path, run=_run):
    value = path.stat()
    return {"uid": value.st_uid, "gid": value.st_gid,
            "mode": stat.S_IMODE(value.st_mode), "acl": _acl(path, run)}


def _verify_metadata(path, expected, run=_run):
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
    _database_check(destination)
    if _columns(source) != _columns(destination):
        raise ValueError("database backup schema verification failed")


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
        except Exception:
            db.rollback()
            raise


def install(admin_app, review_page, database, backup_dir, *, apply=False,
            admin_python="/srv/projects/api/venv-simon/bin/python", expected=EXPECTED,
            source_files=None, disk_usage=shutil.disk_usage, run=_run, fail=None):
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
    for name, path in paths.items():
        _verify_metadata(path, expected[name], run)

    admin_text = paths["admin_app"].read_text()
    review_text = paths["review_page"].read_text()
    columns = _columns(paths["database"])
    state = _state(admin_text, review_text, columns)
    journal_mode = _database_check(paths["database"])
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
    backup.mkdir(mode=0o700)
    staged = []
    mutation_started = False
    try:
        saved_admin, saved_review, saved_database = (backup / paths[name].name for name in
                                                      ("admin_app", "review_page", "database"))
        _backup_file(paths["admin_app"], saved_admin, expected["admin_app"], run)
        _backup_file(paths["review_page"], saved_review, expected["review_page"], run)
        _backup_database(paths["database"], saved_database, expected["database"], run)
        manifest = {path.name: _digest(path) for path in (saved_admin, saved_review, saved_database)}
        (backup / "SHA256SUMS.json").write_text(json.dumps(manifest, sort_keys=True) + "\n")
        _fsync(backup / "SHA256SUMS.json")
        if fail:
            fail("after_backup")

        staged_admin = _stage_bytes(paths["admin_app"], new_admin, expected["admin_app"], run)
        staged.append(staged_admin)
        candidate = staged_admin.read_text()
        if candidate.count("valuation_rule_id") < 1:
            raise ValueError("staged admin markers failed validation")
        syntax_check = ("from pathlib import Path; import sys; "
                        "compile(Path(sys.argv[1]).read_bytes(), sys.argv[1], 'exec')")
        run([str(admin_python), "-B", "-c", syntax_check, str(staged_admin)], capture_output=True, text=True)
        if fail:
            fail("after_admin_stage")
        staged_review = _stage_bytes(paths["review_page"], new_review, expected["review_page"], run)
        staged.append(staged_review)
        if staged_review.read_text().count("derivedValueDetails") != 2:
            raise ValueError("staged review markers failed validation")
        if fail:
            fail("after_review_stage")

        mutation_started = True
        migrate_database(paths["database"], fail)
        os.replace(staged_admin, paths["admin_app"])
        staged.remove(staged_admin)
        if fail:
            fail("first_replacement")
        os.replace(staged_review, paths["review_page"])
        staged.remove(staged_review)
        if fail:
            fail("second_replacement")
        _verify_metadata(paths["admin_app"], expected["admin_app"], run)
        _verify_metadata(paths["review_page"], expected["review_page"], run)
        _verify_metadata(paths["database"], expected["database"], run)
        if _state(paths["admin_app"].read_text(), paths["review_page"].read_text(),
                  _columns(paths["database"])) != "already_applied":
            raise ValueError("post-install state verification failed")
        _database_check(paths["database"])
        report.update(state="already_applied", dry_run=False, backup=str(backup))
        return report
    except Exception as error:
        restoration = "not_required"
        rollback_errors = []
        if mutation_started:
            restorations = (
                ("admin_app", lambda: _restore_file(
                    saved_admin, paths["admin_app"], expected["admin_app"], run)),
                ("review_page", lambda: _restore_file(
                    saved_review, paths["review_page"], expected["review_page"], run)),
                ("database", lambda: _restore_database(
                    saved_database, paths["database"], expected["database"], run)),
            )
            for name, restore in restorations:
                try:
                    restore()
                except Exception as caught:
                    rollback_errors.append(f"{name}: {caught}")
            try:
                if _state(paths["admin_app"].read_text(), paths["review_page"].read_text(),
                          _columns(paths["database"])) != "not_applied":
                    raise ValueError("rolled-back state is not consistently old")
            except Exception as caught:
                rollback_errors.append(f"final verification: {caught}")
            restoration = "FAILED" if rollback_errors else "succeeded"
        message = f"installation failed; mutation_started={mutation_started}; restoration={restoration}; cause={error}"
        if rollback_errors:
            message += "; rollback_errors=" + " | ".join(rollback_errors)
        raise RuntimeError(message) from error
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
