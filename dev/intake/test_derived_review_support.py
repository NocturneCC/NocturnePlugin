from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from derived_review_support import candidate_admin, candidate_review_page, migrate_database, COLUMNS


class DerivedReviewSupportTest(unittest.TestCase):
    def test_admin_query_exposes_derived_columns(self):
        source = '''@app.route("/admin/api/nocturne/regular-submissions", methods=["GET"])
def route():
    query = """SELECT
                    item_price,
                    base_points,
                FROM regular_submissions"""
@app.route("/next")
'''
        changed = candidate_admin(source)
        self.assertIn("valuation_rule_id", changed)
        self.assertIn("finished_output_market_price_gp", changed)

    def test_review_page_labels_drop_output_price_rule_and_derived_value(self):
        source = '''        function buildCard(sub) {
                        <div class="meta-item">
                            <span class="meta-label">Item Value</span>
                            <span class="meta-value">${formatGp(sub.item_price)}</span>
                        </div>
'''
        changed = candidate_review_page(source)
        for text in ("Derived valuation", "finished_output_item_name", "finished_output_market_price_gp",
                     "valuation_rule_id", "derived_unit_price_gp"):
            self.assertIn(text, changed)

    def test_migration_adds_metadata_columns_without_touching_existing_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "submissions.sqlite3"
            with closing(sqlite3.connect(path)) as db:
                db.execute("CREATE TABLE regular_submissions(submission_id INTEGER PRIMARY KEY, item_name TEXT)")
                db.execute("INSERT INTO regular_submissions VALUES(1,'Existing')")
                db.commit()
            migrate_database(path)
            with closing(sqlite3.connect(path)) as db:
                columns = {row[1] for row in db.execute("PRAGMA table_info(regular_submissions)")}
                self.assertTrue(set(COLUMNS).issubset(columns))
                self.assertEqual((1, "Existing"), db.execute(
                    "SELECT submission_id,item_name FROM regular_submissions").fetchone())


if __name__ == "__main__":
    unittest.main()
