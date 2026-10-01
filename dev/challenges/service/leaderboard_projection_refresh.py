#!/usr/bin/env python3
"""Rebuild the derived Midgard leaderboard PB projection safely.

Leaderboard observations are immutable.  This command only replaces the
rebuildable ``leaderboard_personal_bests`` projection, and only when its
deterministic contents differ from the current projection.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


DEFAULT_DATABASE = Path("/srv/projects/database/Challenges.db")


def _source_order(raw: str, observation_id: int) -> tuple[int, int]:
    """Return a stable legacy source order, with observation id as fallback."""
    try:
        values = json.loads(raw)
        indexes = [int(value) for value in values]
        if indexes:
            return min(indexes), observation_id
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return observation_id, observation_id


def calculate_projection(rows: Iterable[sqlite3.Row]) -> list[tuple[str, str, int, int, int]]:
    """Calculate one best observation per mode/competitor and ranked positions."""
    best: dict[tuple[str, str], sqlite3.Row] = {}
    directions: dict[str, str] = {}
    for row in rows:
        direction = str(row["comparison_direction"])
        if direction not in {"lower", "higher"}:
            raise ValueError(f"invalid comparison direction for {row['mode_key']}: {direction}")
        prior_direction = directions.setdefault(str(row["mode_key"]), direction)
        if prior_direction != direction:
            raise ValueError(f"mixed comparison directions for {row['mode_key']}")
        key = str(row["mode_key"]), str(row["competitor_key"])
        existing = best.get(key)
        if existing is None:
            best[key] = row
            continue
        current_value = int(row["metric_value"])
        existing_value = int(existing["metric_value"])
        better = current_value < existing_value if direction == "lower" else current_value > existing_value
        if better or (
            current_value == existing_value
            and _source_order(str(row["source_indexes_json"]), int(row["observation_id"]))
            < _source_order(str(existing["source_indexes_json"]), int(existing["observation_id"]))
        ):
            best[key] = row

    by_mode: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in best.values():
        by_mode[str(row["mode_key"])].append(row)

    projection: list[tuple[str, str, int, int, int]] = []
    for mode_key in sorted(by_mode):
        direction = directions[mode_key]
        rows_for_mode = by_mode[mode_key]
        rows_for_mode.sort(
            key=lambda row: (
                -int(row["metric_value"]) if direction == "higher" else int(row["metric_value"]),
                _source_order(str(row["source_indexes_json"]), int(row["observation_id"])),
                str(row["competitor_key"]),
            )
        )
        for position, row in enumerate(rows_for_mode, 1):
            projection.append((
                mode_key,
                str(row["competitor_key"]),
                int(row["observation_id"]),
                int(row["metric_value"]),
                position,
            ))
    return sorted(projection)


def refresh_projection(database: Path, *, dry_run: bool = False) -> dict[str, Any]:
    conn = sqlite3.connect(database, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("BEGIN IMMEDIATE")
        observations = conn.execute(
            """SELECT observation_id,mode_key,competitor_key,metric_value,
                      comparison_direction,source_indexes_json
                 FROM leaderboard_observations
                WHERE identity_state='resolved'
                ORDER BY observation_id"""
        ).fetchall()
        desired = calculate_projection(observations)
        current = [tuple(row) for row in conn.execute(
            """SELECT mode_key,competitor_key,observation_id,metric_value,position
                 FROM leaderboard_personal_bests
                ORDER BY mode_key,competitor_key"""
        ).fetchall()]
        changed = desired != current
        if changed and not dry_run:
            conn.execute("DELETE FROM leaderboard_personal_bests")
            conn.executemany(
                """INSERT INTO leaderboard_personal_bests
                   (mode_key,competitor_key,observation_id,metric_value,position)
                   VALUES(?,?,?,?,?)""",
                desired,
            )
            foreign_key_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_errors:
                raise RuntimeError(f"foreign key check failed: {len(foreign_key_errors)} row(s)")
            conn.commit()
        else:
            conn.rollback()
        return {
            "ok": True,
            "dry_run": dry_run,
            "changed": changed,
            "resolved_observations": len(observations),
            "projected_personal_bests": len(desired),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(json.dumps(refresh_projection(args.database, dry_run=args.dry_run), sort_keys=True))


if __name__ == "__main__":
    main()
