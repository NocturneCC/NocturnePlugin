"""Prepare derived-value columns and reviewer presentation; never runs implicitly."""
import argparse
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
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


def candidate_admin(original):
    start = original.index('@app.route("/admin/api/nocturne/regular-submissions", methods=["GET"])')
    end = original.index("\n@app.route", start + 10)
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


def migrate_database(database):
    with closing(sqlite3.connect(database)) as db:
        existing = {row[1] for row in db.execute("PRAGMA table_info(regular_submissions)")}
        if not existing:
            raise ValueError("regular_submissions table is missing")
        for name, kind in COLUMNS.items():
            if name not in existing:
                db.execute(f"ALTER TABLE regular_submissions ADD COLUMN {name} {kind}")


def install(admin_app, review_page, database, backup_dir):
    admin_app, review_page, database = map(Path, (admin_app, review_page, database))
    backup = Path(backup_dir) / ("derived-values-" + uuid4().hex[:8])
    backup.mkdir(parents=True, mode=0o700)
    for path in (admin_app, review_page, database):
        if not path.is_file():
            raise ValueError(f"missing target: {path}")
        shutil.copy2(path, backup / path.name)
    new_admin = candidate_admin(admin_app.read_text())
    new_review = candidate_review_page(review_page.read_text())
    admin_app.write_text(new_admin)
    review_page.write_text(new_review)
    migrate_database(database)
    return backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--admin-app", default="/srv/projects/api/admin_app.py")
    parser.add_argument("--review-page", default="/srv/projects/website/regular-submissions-review.html")
    parser.add_argument("--database", default="/srv/projects/database/RegularSubmissions.db")
    parser.add_argument("--backup-dir", default="/etc/nocturne-plugin-backups")
    args = parser.parse_args()
    if not args.apply:
        raise SystemExit("Dry by default. Pass --apply only during an approved deployment window.")
    print(f"Prepared derived-value review support; backup: {install(args.admin_app, args.review_page, args.database, args.backup_dir)}")


if __name__ == "__main__":
    main()
