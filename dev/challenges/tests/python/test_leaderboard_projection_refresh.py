from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from leaderboard_projection_refresh import refresh_projection


SCHEMA = """
CREATE TABLE leaderboard_observations(
 observation_id INTEGER PRIMARY KEY,
 mode_key TEXT NOT NULL,
 competitor_key TEXT NOT NULL,
 metric_value INTEGER NOT NULL,
 comparison_direction TEXT NOT NULL,
 source_indexes_json TEXT NOT NULL,
 identity_state TEXT NOT NULL
);
CREATE TABLE leaderboard_personal_bests(
 mode_key TEXT NOT NULL,
 competitor_key TEXT NOT NULL,
 observation_id INTEGER NOT NULL,
 metric_value INTEGER NOT NULL,
 position INTEGER,
 PRIMARY KEY(mode_key,competitor_key),
 FOREIGN KEY(observation_id) REFERENCES leaderboard_observations(observation_id)
);
"""


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "test.db"
        with sqlite3.connect(self.database) as conn:
            conn.executescript(SCHEMA)

    def tearDown(self):
        self.temp.cleanup()

    def insert(self, observation_id, mode, competitor, value, direction="lower", state="resolved"):
        with sqlite3.connect(self.database) as conn:
            conn.execute(
                "INSERT INTO leaderboard_observations VALUES(?,?,?,?,?,?,?)",
                (observation_id, mode, competitor, value, direction, f"[{observation_id}]", state),
            )

    def rows(self):
        with sqlite3.connect(self.database) as conn:
            return conn.execute(
                "SELECT mode_key,competitor_key,observation_id,metric_value,position "
                "FROM leaderboard_personal_bests ORDER BY mode_key,competitor_key"
            ).fetchall()

    def test_lower_metric_replaces_pb_and_ranks(self):
        self.insert(1, "time", "a", 120)
        self.insert(2, "time", "a", 100)
        self.insert(3, "time", "b", 110)
        result = refresh_projection(self.database)
        self.assertTrue(result["changed"])
        self.assertEqual(self.rows(), [("time", "a", 2, 100, 1), ("time", "b", 3, 110, 2)])

    def test_higher_metric_wins(self):
        self.insert(1, "waves", "a", 20, "higher")
        self.insert(2, "waves", "a", 30, "higher")
        self.insert(3, "waves", "b", 25, "higher")
        refresh_projection(self.database)
        self.assertEqual(self.rows(), [("waves", "a", 2, 30, 1), ("waves", "b", 3, 25, 2)])

    def test_unresolved_observation_is_excluded(self):
        self.insert(1, "time", "unresolved", 1, state="unresolved")
        result = refresh_projection(self.database)
        self.assertEqual(result["resolved_observations"], 0)
        self.assertEqual(self.rows(), [])

    def test_second_refresh_is_noop(self):
        self.insert(1, "time", "a", 100)
        self.assertTrue(refresh_projection(self.database)["changed"])
        self.assertFalse(refresh_projection(self.database)["changed"])

    def test_dry_run_does_not_write(self):
        self.insert(1, "time", "a", 100)
        result = refresh_projection(self.database, dry_run=True)
        self.assertTrue(result["changed"])
        self.assertEqual(self.rows(), [])

    def test_mixed_directions_fail_without_replacing_projection(self):
        self.insert(1, "mixed", "a", 100, "lower")
        refresh_projection(self.database)
        before = self.rows()
        self.insert(2, "mixed", "b", 200, "higher")
        with self.assertRaisesRegex(ValueError, "mixed comparison directions"):
            refresh_projection(self.database)
        self.assertEqual(self.rows(), before)


if __name__ == "__main__":
    unittest.main()
