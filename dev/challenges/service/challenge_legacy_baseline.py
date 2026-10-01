"""Immutable legacy Challenge baselines and future-award safety gates.

This module never writes Members.db, RegularSubmissions.db, Google, Discord, or
rank points.  Baselines are display/progression floors, never award events.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from typing import Any

from challenge_config import evaluation_catalog
from challenge_shadow_common import canonical_json, normalize_rsn, sha256_text, utc_now


SCHEMA = """
CREATE TABLE IF NOT EXISTS challenge_legacy_baseline_snapshots (
    snapshot_key TEXT PRIMARY KEY,
    config_version_id INTEGER NOT NULL,
    snapshot_timestamp TEXT NOT NULL,
    challenge_aggregate_sha256 TEXT NOT NULL,
    members_rank_points_sha256 TEXT NOT NULL,
    regular_source_sha256 TEXT NOT NULL,
    immutable_submissions_sha256 TEXT NOT NULL,
    source_rows_observed INTEGER NOT NULL,
    mapped_rows INTEGER NOT NULL,
    unresolved_rows INTEGER NOT NULL,
    imported_baseline_rows INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(config_version_id) REFERENCES challenge_config_versions(config_version_id)
);

CREATE TABLE IF NOT EXISTS challenge_legacy_system_tier_floors (
    floor_id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_key TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    member_id INTEGER,
    system_tier_key TEXT NOT NULL,
    system_tier_rank INTEGER NOT NULL,
    historically_complete INTEGER NOT NULL CHECK(historically_complete=1),
    historically_awarded INTEGER NOT NULL CHECK(historically_awarded IN (0,1)),
    grandfathered_award_id INTEGER,
    awardable INTEGER NOT NULL DEFAULT 0 CHECK(awardable=0),
    source_provenance_json TEXT NOT NULL,
    source_provenance_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(snapshot_key,subject_key,system_tier_key),
    FOREIGN KEY(snapshot_key) REFERENCES challenge_legacy_baseline_snapshots(snapshot_key),
    FOREIGN KEY(grandfathered_award_id) REFERENCES challenge_tier_awards(award_id)
);

CREATE TABLE IF NOT EXISTS challenge_rank_point_baseline_members (
    snapshot_key TEXT NOT NULL,
    member_id INTEGER NOT NULL,
    rank_points INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(snapshot_key,member_id),
    FOREIGN KEY(snapshot_key) REFERENCES challenge_legacy_baseline_snapshots(snapshot_key)
);

CREATE TABLE IF NOT EXISTS challenge_award_review_evidence (
    review_evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
    award_review_id INTEGER NOT NULL UNIQUE,
    category TEXT NOT NULL,
    supporting_state_json TEXT NOT NULL,
    supporting_state_sha256 TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(award_review_id) REFERENCES challenge_award_reviews(award_review_id)
);

CREATE TRIGGER IF NOT EXISTS challenge_legacy_snapshot_no_update
BEFORE UPDATE ON challenge_legacy_baseline_snapshots
BEGIN SELECT RAISE(ABORT,'challenge_legacy_baseline_snapshots is append-only'); END;
CREATE TRIGGER IF NOT EXISTS challenge_legacy_snapshot_no_delete
BEFORE DELETE ON challenge_legacy_baseline_snapshots
BEGIN SELECT RAISE(ABORT,'challenge_legacy_baseline_snapshots is append-only'); END;
CREATE TRIGGER IF NOT EXISTS challenge_legacy_floor_no_update
BEFORE UPDATE ON challenge_legacy_system_tier_floors
BEGIN SELECT RAISE(ABORT,'challenge_legacy_system_tier_floors is append-only'); END;
CREATE TRIGGER IF NOT EXISTS challenge_legacy_floor_no_delete
BEFORE DELETE ON challenge_legacy_system_tier_floors
BEGIN SELECT RAISE(ABORT,'challenge_legacy_system_tier_floors is append-only'); END;
CREATE TRIGGER IF NOT EXISTS challenge_rank_baseline_no_update
BEFORE UPDATE ON challenge_rank_point_baseline_members
BEGIN SELECT RAISE(ABORT,'challenge_rank_point_baseline_members is append-only'); END;
CREATE TRIGGER IF NOT EXISTS challenge_rank_baseline_no_delete
BEFORE DELETE ON challenge_rank_point_baseline_members
BEGIN SELECT RAISE(ABORT,'challenge_rank_point_baseline_members is append-only'); END;
CREATE TRIGGER IF NOT EXISTS challenge_review_evidence_no_update
BEFORE UPDATE ON challenge_award_review_evidence
BEGIN SELECT RAISE(ABORT,'challenge_award_review_evidence is append-only'); END;
CREATE TRIGGER IF NOT EXISTS challenge_review_evidence_no_delete
BEFORE DELETE ON challenge_award_review_evidence
BEGIN SELECT RAISE(ABORT,'challenge_award_review_evidence is append-only'); END;
CREATE TRIGGER IF NOT EXISTS challenge_awards_legacy_floor_insert_guard
BEFORE INSERT ON challenge_tier_awards
WHEN NEW.award_state='shadow_eligible'
 AND EXISTS (
   SELECT 1 FROM challenge_legacy_system_tier_floors f
    WHERE f.subject_key=NEW.subject_key AND f.system_tier_key=NEW.system_tier_key
 )
BEGIN SELECT RAISE(ABORT,'legacy completion floor cannot create automatic award eligibility'); END;
"""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def migrate_legacy_schema(conn: sqlite3.Connection) -> None:
    """Add only Phase 4C tables/columns; published/history tables are untouched."""
    conn.executescript(SCHEMA)
    existing = _columns(conn, "challenge_legacy_baselines")
    additions = {
        "source_record_reference": "TEXT",
        "reason": "TEXT NOT NULL DEFAULT 'legacy aggregate without immutable evidence'",
        "awardable": "INTEGER NOT NULL DEFAULT 0 CHECK(awardable=0)",
    }
    for column, declaration in additions.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE challenge_legacy_baselines ADD COLUMN {column} {declaration}")


def digest_query(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    for row in conn.execute(sql, params):
        digest.update(json.dumps(list(row), ensure_ascii=False, separators=(",", ":"), default=str).encode())
        digest.update(b"\n")
        count += 1
    return count, digest.hexdigest()


def identity_index(members: sqlite3.Connection) -> dict[str, set[int]]:
    index: dict[str, set[int]] = defaultdict(set)
    for row in members.execute("SELECT member_id,normalized_rsn,rsn FROM members"):
        index[normalize_rsn(row["normalized_rsn"] or row["rsn"])].add(int(row["member_id"]))
    for row in members.execute("SELECT member_id,normalized_rsn FROM member_accounts WHERE COALESCE(is_active,1)=1"):
        index[normalize_rsn(row["normalized_rsn"])].add(int(row["member_id"]))
    for row in members.execute("SELECT member_id,normalized_alias_rsn FROM member_aliases"):
        index[normalize_rsn(row["normalized_alias_rsn"])].add(int(row["member_id"]))
    return index


def evidence_ranks(conn: sqlite3.Connection) -> dict[tuple[str, str], int]:
    return {
        (str(row["subject_key"]), str(row["boss_key"])): int(row["rank"])
        for row in conn.execute(
            """SELECT subject_key,boss_key,MAX(tier_rank) rank
                 FROM challenge_member_tier_achievements GROUP BY subject_key,boss_key"""
        )
    }


def legacy_baselines(conn: sqlite3.Connection) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for row in conn.execute(
        """SELECT b.* FROM challenge_legacy_baselines b
             JOIN (SELECT subject_key,boss_key,MAX(highest_tier_rank) rank
                     FROM challenge_legacy_baselines GROUP BY subject_key,boss_key) m
               ON m.subject_key=b.subject_key AND m.boss_key=b.boss_key
              AND m.rank=b.highest_tier_rank
            ORDER BY b.baseline_id"""
    ):
        result[(str(row["subject_key"]), str(row["boss_key"]))] = dict(row)
    return result


def merged_rank_map(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    merged: dict[str, dict[str, int]] = defaultdict(dict)
    for (subject, boss), rank in evidence_ranks(conn).items():
        merged[subject][boss] = rank
    for (subject, boss), row in legacy_baselines(conn).items():
        merged[subject][boss] = max(int(merged[subject].get(boss, 0)), int(row["highest_tier_rank"]))
    return dict(merged)


def calculate_progress(catalog: dict[str, Any], boss_ranks: dict[str, int]) -> tuple[int, int, bool, dict[str, bool]]:
    active = {key for key, boss in catalog["bosses"].items() if boss["active"]}
    points = sum(
        int(catalog["bosses"][boss]["tiers_by_rank"][rank]["progression_points"])
        for boss, rank in boss_ranks.items()
        if boss in active and rank in catalog["bosses"][boss]["tiers_by_rank"]
    )
    completed = sum(1 for boss in active if int(boss_ranks.get(boss, 0)) > 0)
    ascendant = bool(active) and completed == len(active)
    complete = {
        str(rule["system_tier_key"]): (
            ascendant if bool(rule["require_all_active_challenges"])
            else points >= int(rule["min_progression_points"] or 0)
        )
        for rule in catalog["system_tiers"]
    }
    return points, completed, ascendant, complete


def import_rank_baseline(
    conn: sqlite3.Connection,
    members: sqlite3.Connection,
    snapshot_key: str,
) -> int:
    rows = members.execute("SELECT member_id,COALESCE(rank_points,0) rank_points FROM members ORDER BY member_id").fetchall()
    conn.executemany(
        """INSERT OR IGNORE INTO challenge_rank_point_baseline_members
           (snapshot_key,member_id,rank_points) VALUES(?,?,?)""",
        [(snapshot_key, int(row["member_id"]), int(row["rank_points"])) for row in rows],
    )
    return len(rows)


def import_progress_baselines(
    conn: sqlite3.Connection,
    members: sqlite3.Connection,
    snapshot_key: str,
    snapshot_timestamp: str,
    config_id: int,
) -> dict[str, int]:
    identities = identity_index(members)
    evidence = evidence_ranks(conn)
    catalog = evaluation_catalog(conn, config_id)
    observed = mapped = unresolved = imported = already_covered = zero_rank = 0
    for row in conn.execute("SELECT * FROM challenge_progress ORDER BY id"):
        observed += 1
        google_rank = int(row["tier_rank"] or 0)
        if google_rank <= 0:
            zero_rank += 1
            continue
        candidates = identities.get(normalize_rsn(row["normalized_rsn"] or row["rsn"]), set())
        if len(candidates) != 1:
            unresolved += 1
            continue
        mapped += 1
        member_id = next(iter(candidates))
        subject = f"member:{member_id}"
        boss_key = str(row["boss_key"])
        evidence_rank = int(evidence.get((subject, boss_key), 0))
        if evidence_rank >= google_rank:
            already_covered += 1
            continue
        boss = catalog["bosses"].get(boss_key)
        tier = boss["tiers_by_rank"].get(google_rank) if boss else None
        if not tier:
            unresolved += 1
            continue
        provenance = {
            "source_table": "challenge_progress",
            "source_row_id": int(row["id"]),
            "source_updated_at": row["updated_at"],
            "source_identity_sha256": sha256_text(normalize_rsn(row["normalized_rsn"] or row["rsn"])),
            "google_tier_rank": google_rank,
            "evidence_tier_rank_at_snapshot": evidence_rank,
        }
        rendered = canonical_json(provenance)
        cur = conn.execute(
            """INSERT OR IGNORE INTO challenge_legacy_baselines
               (snapshot_key,subject_key,member_id,boss_key,highest_tier_key,highest_tier_rank,
                progression_points,source_system,source_provenance_json,source_provenance_sha256,
                config_version_id,snapshot_timestamp,source_record_reference,reason,awardable)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)""",
            (snapshot_key, subject, member_id, boss_key, tier["tier_key"], google_rank,
             int(tier["progression_points"]), "google_legacy_baseline", rendered,
             sha256_text(rendered), config_id, snapshot_timestamp,
             f"challenge_progress:{int(row['id'])}",
             "Google aggregate tier exceeded immutable evidence at Phase 4C baseline"),
        )
        imported += max(0, cur.rowcount)
    return {
        "source_rows_observed": observed,
        "zero_rank_rows": zero_rank,
        "positive_rows_mapped": mapped,
        "positive_rows_unresolved": unresolved,
        "positive_rows_already_evidence_covered": already_covered,
        "imported_baseline_rows": imported,
    }


def import_system_tier_floors(
    conn: sqlite3.Connection,
    snapshot_key: str,
    config_id: int,
) -> int:
    catalog = evaluation_catalog(conn, config_id)
    rules = {str(rule["system_tier_key"]): rule for rule in catalog["system_tiers"]}
    member_ids = {
        str(row["subject_key"]): row["member_id"]
        for row in conn.execute(
            """SELECT subject_key,MAX(member_id) member_id FROM (
                 SELECT subject_key,member_id FROM challenge_member_tier_achievements
                 UNION ALL SELECT subject_key,member_id FROM challenge_legacy_baselines
               ) GROUP BY subject_key"""
        )
    }
    inserted = 0
    for subject, bosses in merged_rank_map(conn).items():
        points, completed, ascendant, complete = calculate_progress(catalog, bosses)
        for tier_key, is_complete in complete.items():
            if not is_complete:
                continue
            award = conn.execute(
                """SELECT award_id FROM challenge_tier_awards
                    WHERE subject_key=? AND system_tier_key=? AND award_state='grandfathered'""",
                (subject, tier_key),
            ).fetchone()
            provenance = {
                "calculation": "merged_evidence_and_non_awarding_baseline",
                "progression_points": points,
                "active_bosses_completed": completed,
                "ascendant_eligible": ascendant,
                "config_version_id": config_id,
            }
            rendered = canonical_json(provenance)
            cur = conn.execute(
                """INSERT OR IGNORE INTO challenge_legacy_system_tier_floors
                   (snapshot_key,subject_key,member_id,system_tier_key,system_tier_rank,
                    historically_complete,historically_awarded,grandfathered_award_id,awardable,
                    source_provenance_json,source_provenance_sha256)
                   VALUES(?,?,?,?,?,1,?,?,0,?,?)""",
                (snapshot_key, subject, member_ids.get(subject), tier_key,
                 int(rules[tier_key]["tier_rank"]), int(award is not None),
                 int(award["award_id"]) if award else None, rendered, sha256_text(rendered)),
            )
            inserted += max(0, cur.rowcount)
    return inserted


def capture_manual_review_evidence(conn: sqlite3.Connection, members: sqlite3.Connection, captured_at: str) -> dict[str, int]:
    normalized_by_member = {
        int(row["member_id"]): normalize_rsn(row["normalized_rsn"] or row["rsn"])
        for row in members.execute("SELECT member_id,normalized_rsn,rsn FROM members")
    }
    categories: dict[str, int] = defaultdict(int)
    rows = conn.execute(
        """SELECT r.award_review_id,r.review_state,a.award_id,a.member_id,a.subject_key,
                  a.system_tier_key,a.would_award_points,a.eligibility_config_version_id,
                  p.challenge_progression_points,p.active_challenges_completed,p.active_challenge_count,
                  p.ascendant_eligible
             FROM challenge_award_reviews r JOIN challenge_tier_awards a ON a.award_id=r.award_id
             LEFT JOIN challenge_member_progress p ON p.subject_key=a.subject_key
                  AND p.config_version_id=a.eligibility_config_version_id
            WHERE r.review_state='manual_review_required' AND a.award_state='shadow_eligible'
            ORDER BY r.award_review_id"""
    ).fetchall()
    for row in rows:
        tier = str(row["system_tier_key"])
        identity = normalized_by_member.get(int(row["member_id"])) if row["member_id"] is not None else None
        summary = conn.execute(
            "SELECT * FROM challenge_member_summary WHERE normalized_rsn=?",
            (identity,),
        ).fetchone() if identity else None
        checkbox = bool(summary[f"{tier}_awarded"]) if summary else False
        complete = bool(summary[f"{tier}_complete"]) if summary else False
        ledger = conn.execute(
            """SELECT COUNT(*) FROM challenge_tier_awards
                WHERE subject_key=? AND system_tier_key=? AND award_state='grandfathered'""",
            (row["subject_key"], tier),
        ).fetchone()[0]
        if row["member_id"] is None:
            category = "identity_uncertainty"
        elif checkbox and not ledger:
            category = "google_checkbox_without_ledger"
        elif complete and not ledger:
            category = "potential_historical_omission"
        else:
            category = "other_manual_review"
        support = {
            "award_id": int(row["award_id"]),
            "subject_key": str(row["subject_key"]),
            "member_id": row["member_id"],
            "system_tier_key": tier,
            "would_award_points": int(row["would_award_points"]),
            "current_progression_points": int(row["challenge_progression_points"] or 0),
            "active_bosses_completed": int(row["active_challenges_completed"] or 0),
            "active_bosses_total": int(row["active_challenge_count"] or 0),
            "ascendant_eligible": bool(row["ascendant_eligible"] or 0),
            "google_complete": complete,
            "google_awarded_checkbox": checkbox,
            "point_bearing_ledger_rows": int(ledger),
            "delivery_blocked": True,
        }
        rendered = canonical_json(support)
        conn.execute(
            """INSERT OR IGNORE INTO challenge_award_review_evidence
               (award_review_id,category,supporting_state_json,supporting_state_sha256,captured_at)
               VALUES(?,?,?,?,?)""",
            (int(row["award_review_id"]), category, rendered, sha256_text(rendered), captured_at),
        )
        categories[category] += 1
    return dict(categories)


def latest_snapshot(conn: sqlite3.Connection) -> sqlite3.Row | None:
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='challenge_legacy_baseline_snapshots'"
    ).fetchone():
        return None
    return conn.execute(
        "SELECT * FROM challenge_legacy_baseline_snapshots ORDER BY snapshot_timestamp DESC,snapshot_key DESC LIMIT 1"
    ).fetchone()


def post_baseline_evidence_improvement(conn: sqlite3.Connection, subject_key: str) -> bool:
    snapshot = latest_snapshot(conn)
    if not snapshot:
        return False
    cutoff = str(snapshot["snapshot_timestamp"])
    baseline = {
        str(row["boss_key"]): int(row["rank"])
        for row in conn.execute(
            """SELECT boss_key,MAX(highest_tier_rank) rank FROM challenge_legacy_baselines
                WHERE subject_key=? GROUP BY boss_key""",
            (subject_key,),
        )
    }
    for row in conn.execute(
        """SELECT boss_key,tier_rank,earned_at FROM challenge_member_tier_achievements
            WHERE subject_key=? AND earned_at IS NOT NULL AND earned_at>?""",
        (subject_key, cutoff),
    ):
        if int(row["tier_rank"]) > int(baseline.get(str(row["boss_key"]), 0)):
            return True
    return False


def future_award_eligibility(conn: sqlite3.Connection, subject_key: str, system_tier_key: str) -> tuple[bool, str]:
    if conn.execute(
        """SELECT 1 FROM challenge_tier_awards WHERE subject_key=? AND system_tier_key=?
             AND award_state IN ('grandfathered','delivered')""",
        (subject_key, system_tier_key),
    ).fetchone():
        return False, "historically_or_midgard_awarded"
    snapshot = latest_snapshot(conn)
    if not snapshot:
        # Rolling-deployment compatibility: before the Phase 4C snapshot is
        # installed, retain the existing shadow-only calculation behavior.
        return True, "pre_phase4c_shadow_behavior"
    if conn.execute(
        """SELECT 1 FROM challenge_legacy_system_tier_floors
            WHERE subject_key=? AND system_tier_key=?""",
        (subject_key, system_tier_key),
    ).fetchone():
        return False, "covered_by_legacy_completion_floor"
    review = conn.execute(
        """SELECT 1 FROM challenge_award_reviews r JOIN challenge_tier_awards a ON a.award_id=r.award_id
            WHERE a.subject_key=? AND a.system_tier_key=? AND r.review_state='manual_review_required'""",
        (subject_key, system_tier_key),
    ).fetchone()
    if review:
        return False, "manual_review_required"
    if not post_baseline_evidence_improvement(conn, subject_key):
        return False, "no_post_baseline_evidence_improvement"
    return True, "new_evidence_after_baseline"
