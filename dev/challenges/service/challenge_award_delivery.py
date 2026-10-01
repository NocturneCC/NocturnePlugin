#!/usr/bin/env python3
"""Challenge award planning, guarded rank delivery, and Google mirror primitives.

Production defaults are deliberately inert.  The live database is migrated with
``award_delivery_mode=dry_run`` and ``google_mirror_mode=disabled``.  No caller
in this module contacts Google, Discord, or any network service.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from challenge_legacy_baseline import future_award_eligibility, latest_snapshot
from challenge_shadow_common import canonical_json, sha256_text, utc_now


BOSS_TIER_POINTS = {"bronze": 10, "silver": 20, "gold": 30, "platinum": 40, "ascendant": 50}
SYSTEM_TIER_POINTS = {"bronze": 75, "silver": 150, "gold": 225, "platinum": 300, "ascendant": 500}
EXECUTION_STATES = {
    "queued", "rank_write_pending", "rank_written", "google_mirror_pending",
    "google_mirrored", "delivered", "failed_retryable", "failed_terminal",
}
ALL_STATES = {
    "eligible", "planned", *EXECUTION_STATES,
    "blocked_manual_review", "blocked_legacy_floor", "blocked_grandfathered",
}
LIVE_CAPABILITY = "EXPLICIT_CHALLENGE_LIVE_RANK_WRITE"
GOOGLE_WRITE_CAPABILITY = "EXPLICIT_CHALLENGE_GOOGLE_SHEETS_WRITE"
DEFAULT_GOOGLE_BRIDGE = Path("/srv/projects/nocturne-services/challenge_google_sheets_bridge.js")
DEFAULT_GOOGLE_CREDENTIAL = Path("/srv/projects/nocturne-bot/nocturnesheets-313c26c4ef3b.json")
CLAN_INFORMATION_SPREADSHEET_ID = "1_bX3yvQ_fC2HYidbLroe61XCVHZThq5O6vn_OIZPRu8"


SCHEMA = """
CREATE TABLE IF NOT EXISTS challenge_legacy_boss_tier_floors (
    floor_id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_key TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    member_id INTEGER,
    boss_key TEXT NOT NULL,
    tier_key TEXT NOT NULL,
    tier_rank INTEGER NOT NULL,
    source_achievement_id INTEGER,
    source_submission_id INTEGER,
    awardable INTEGER NOT NULL DEFAULT 0 CHECK(awardable=0),
    source_provenance_json TEXT NOT NULL,
    source_provenance_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(snapshot_key,subject_key,boss_key,tier_key),
    FOREIGN KEY(snapshot_key) REFERENCES challenge_legacy_baseline_snapshots(snapshot_key),
    FOREIGN KEY(source_achievement_id) REFERENCES challenge_member_tier_achievements(achievement_id),
    FOREIGN KEY(source_submission_id) REFERENCES challenge_submissions(submission_id)
);

CREATE TABLE IF NOT EXISTS challenge_award_deliveries (
    delivery_id INTEGER PRIMARY KEY AUTOINCREMENT,
    award_kind TEXT NOT NULL CHECK(award_kind IN ('boss_tier','system_tier')),
    subject_key TEXT NOT NULL,
    member_id INTEGER,
    boss_key TEXT,
    tier_key TEXT NOT NULL,
    tier_rank INTEGER NOT NULL,
    config_version_id INTEGER NOT NULL,
    points INTEGER NOT NULL CHECK(points>=0),
    delivery_state TEXT NOT NULL CHECK(delivery_state IN (
      'eligible','planned','queued','rank_write_pending','rank_written',
      'google_mirror_pending','google_mirrored','delivered',
      'blocked_manual_review','blocked_legacy_floor','blocked_grandfathered',
      'failed_retryable','failed_terminal'
    )),
    idempotency_key TEXT NOT NULL UNIQUE,
    rank_write_key TEXT NOT NULL UNIQUE,
    evidence_achievement_id INTEGER,
    evidence_submission_id INTEGER,
    system_tier_award_id INTEGER,
    expected_rank_points INTEGER,
    blocked_reason TEXT,
    dry_run INTEGER NOT NULL DEFAULT 1 CHECK(dry_run IN (0,1)),
    eligible_at TEXT,
    planned_at TEXT,
    delivered_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(config_version_id) REFERENCES challenge_config_versions(config_version_id),
    FOREIGN KEY(evidence_achievement_id) REFERENCES challenge_member_tier_achievements(achievement_id),
    FOREIGN KEY(evidence_submission_id) REFERENCES challenge_submissions(submission_id),
    FOREIGN KEY(system_tier_award_id) REFERENCES challenge_tier_awards(award_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_challenge_award_boss_tier
ON challenge_award_deliveries(subject_key,boss_key,tier_key)
WHERE award_kind='boss_tier';
CREATE UNIQUE INDEX IF NOT EXISTS uq_challenge_award_system_tier
ON challenge_award_deliveries(subject_key,tier_key)
WHERE award_kind='system_tier';
CREATE INDEX IF NOT EXISTS idx_challenge_award_delivery_state
ON challenge_award_deliveries(delivery_state,award_kind);

CREATE TABLE IF NOT EXISTS challenge_award_transition_log (
    transition_id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id INTEGER NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    details_json TEXT NOT NULL,
    details_sha256 TEXT NOT NULL,
    transitioned_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(delivery_id) REFERENCES challenge_award_deliveries(delivery_id)
);

CREATE TABLE IF NOT EXISTS challenge_rank_write_receipts (
    receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id INTEGER NOT NULL UNIQUE,
    write_key TEXT NOT NULL UNIQUE,
    member_id INTEGER NOT NULL,
    points_delta INTEGER NOT NULL CHECK(points_delta>0),
    expected_before INTEGER NOT NULL,
    actual_before INTEGER NOT NULL,
    actual_after INTEGER NOT NULL,
    committed_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(delivery_id) REFERENCES challenge_award_deliveries(delivery_id)
);

CREATE TABLE IF NOT EXISTS challenge_google_mirror_operations (
    mirror_id INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_id INTEGER,
    submission_id INTEGER,
    operation_kind TEXT NOT NULL CHECK(operation_kind IN (
      'challenge_submission','boss_tier_award','system_tier_award',
      'challenge_progress','leaderboard_projection'
    )),
    idempotency_key TEXT NOT NULL UNIQUE,
    target_range TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    mirror_status TEXT NOT NULL CHECK(mirror_status IN (
      'disabled','planned','pending','written_unverified','verified',
      'failed_retryable','failed_terminal'
    )),
    mirror_attempts INTEGER NOT NULL DEFAULT 0,
    google_reference TEXT,
    last_error_code TEXT,
    last_error_sha256 TEXT,
    verification_method TEXT,
    verification_details_sha256 TEXT,
    google_row_sha256 TEXT,
    verified_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(delivery_id) REFERENCES challenge_award_deliveries(delivery_id),
    FOREIGN KEY(submission_id) REFERENCES challenge_submissions(submission_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_challenge_mirror_delivery_kind
ON challenge_google_mirror_operations(delivery_id,operation_kind)
WHERE delivery_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_challenge_mirror_submission_kind
ON challenge_google_mirror_operations(submission_id,operation_kind)
WHERE submission_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS challenge_google_mirror_attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
    mirror_id INTEGER NOT NULL,
    attempt_number INTEGER NOT NULL,
    outcome TEXT NOT NULL,
    detail_code TEXT,
    detail_sha256 TEXT,
    google_reference TEXT,
    attempted_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(mirror_id,attempt_number),
    FOREIGN KEY(mirror_id) REFERENCES challenge_google_mirror_operations(mirror_id)
);

CREATE TRIGGER IF NOT EXISTS challenge_legacy_boss_floor_no_update
BEFORE UPDATE ON challenge_legacy_boss_tier_floors
BEGIN SELECT RAISE(ABORT,'challenge_legacy_boss_tier_floors is append-only'); END;
CREATE TRIGGER IF NOT EXISTS challenge_legacy_boss_floor_no_delete
BEFORE DELETE ON challenge_legacy_boss_tier_floors
BEGIN SELECT RAISE(ABORT,'challenge_legacy_boss_tier_floors is append-only'); END;
CREATE TRIGGER IF NOT EXISTS challenge_award_transition_no_update
BEFORE UPDATE ON challenge_award_transition_log
BEGIN SELECT RAISE(ABORT,'challenge_award_transition_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS challenge_award_transition_no_delete
BEFORE DELETE ON challenge_award_transition_log
BEGIN SELECT RAISE(ABORT,'challenge_award_transition_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS challenge_rank_receipt_no_update
BEFORE UPDATE ON challenge_rank_write_receipts
BEGIN SELECT RAISE(ABORT,'challenge_rank_write_receipts is append-only'); END;
CREATE TRIGGER IF NOT EXISTS challenge_rank_receipt_no_delete
BEFORE DELETE ON challenge_rank_write_receipts
BEGIN SELECT RAISE(ABORT,'challenge_rank_write_receipts is append-only'); END;
CREATE TRIGGER IF NOT EXISTS challenge_google_attempt_no_update
BEFORE UPDATE ON challenge_google_mirror_attempts
BEGIN SELECT RAISE(ABORT,'challenge_google_mirror_attempts is append-only'); END;
CREATE TRIGGER IF NOT EXISTS challenge_google_attempt_no_delete
BEFORE DELETE ON challenge_google_mirror_attempts
BEGIN SELECT RAISE(ABORT,'challenge_google_mirror_attempts is append-only'); END;

CREATE TRIGGER IF NOT EXISTS challenge_award_delivery_shadow_insert_guard
BEFORE INSERT ON challenge_award_deliveries
WHEN NEW.delivery_state IN (
  'queued','rank_write_pending','rank_written','google_mirror_pending',
  'google_mirrored','delivered','failed_retryable','failed_terminal'
) AND (
  COALESCE((SELECT setting_value FROM challenge_settings WHERE setting_key='award_mode'),'shadow')!='live'
  OR COALESCE((SELECT setting_value FROM challenge_settings WHERE setting_key='award_delivery_mode'),'dry_run')!='live'
)
BEGIN SELECT RAISE(ABORT,'challenge award delivery is disabled'); END;

CREATE TRIGGER IF NOT EXISTS challenge_award_delivery_shadow_update_guard
BEFORE UPDATE OF delivery_state ON challenge_award_deliveries
WHEN NEW.delivery_state IN (
  'queued','rank_write_pending','rank_written','google_mirror_pending',
  'google_mirrored','delivered','failed_retryable','failed_terminal'
) AND (
  COALESCE((SELECT setting_value FROM challenge_settings WHERE setting_key='award_mode'),'shadow')!='live'
  OR COALESCE((SELECT setting_value FROM challenge_settings WHERE setting_key='award_delivery_mode'),'dry_run')!='live'
  OR NEW.dry_run!=0
)
BEGIN SELECT RAISE(ABORT,'challenge award delivery is disabled'); END;

CREATE TRIGGER IF NOT EXISTS challenge_google_mirror_disabled_guard
BEFORE UPDATE OF mirror_status ON challenge_google_mirror_operations
WHEN NEW.mirror_status IN ('pending','written_unverified','verified','failed_retryable','failed_terminal')
 AND COALESCE((SELECT setting_value FROM challenge_settings WHERE setting_key='google_mirror_mode'),'disabled')!='live'
BEGIN SELECT RAISE(ABORT,'challenge Google mirror is disabled'); END;

CREATE TRIGGER IF NOT EXISTS challenge_delivery_mode_live_guard
BEFORE UPDATE OF setting_value ON challenge_settings
WHEN NEW.setting_key='award_delivery_mode' AND NEW.setting_value='live'
 AND COALESCE((SELECT setting_value FROM challenge_settings WHERE setting_key='award_mode'),'shadow')!='live'
BEGIN SELECT RAISE(ABORT,'award_mode must be live before award delivery can be enabled'); END;

CREATE TRIGGER IF NOT EXISTS challenge_google_mode_live_guard
BEFORE UPDATE OF setting_value ON challenge_settings
WHEN NEW.setting_key='google_mirror_mode' AND NEW.setting_value='live'
 AND COALESCE((SELECT setting_value FROM challenge_settings WHERE setting_key='award_mode'),'shadow')!='live'
BEGIN SELECT RAISE(ABORT,'award_mode must be live before Google mirroring can be enabled'); END;
"""


@dataclass(frozen=True)
class AwardCandidate:
    award_kind: str
    subject_key: str
    member_id: int | None
    boss_key: str | None
    tier_key: str
    tier_rank: int
    config_version_id: int
    points: int
    classification: str
    reason: str
    idempotency_key: str
    evidence_achievement_id: int | None = None
    evidence_submission_id: int | None = None
    system_tier_award_id: int | None = None


class DeliveryDisabled(RuntimeError):
    pass


class ConcurrentRankChange(RuntimeError):
    pass


class MirrorAmbiguousWrite(RuntimeError):
    pass


class MirrorRetryable(RuntimeError):
    pass


class MirrorTerminal(RuntimeError):
    pass


@dataclass(frozen=True)
class MirrorLookup:
    reference: str
    method: str
    row_sha256: str | None = None


def migrate_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(challenge_google_mirror_operations)")}
    for name in ("verification_method", "verification_details_sha256", "google_row_sha256"):
        if name not in columns:
            conn.execute(f"ALTER TABLE challenge_google_mirror_operations ADD COLUMN {name} TEXT")
    conn.execute(
        "INSERT OR IGNORE INTO challenge_settings(setting_key,setting_value) VALUES('award_delivery_mode','dry_run')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO challenge_settings(setting_key,setting_value) VALUES('google_mirror_mode','disabled')"
    )


def _latest_snapshot_key(conn: sqlite3.Connection) -> str:
    snapshot = latest_snapshot(conn)
    if not snapshot:
        raise RuntimeError("Phase 4C legacy snapshot is required before award delivery preparation")
    return str(snapshot["snapshot_key"])


def capture_existing_boss_tier_floors(conn: sqlite3.Connection) -> int:
    """Freeze every existing achievement as non-awardable historical progress."""
    snapshot_key = _latest_snapshot_key(conn)
    inserted = 0
    for row in conn.execute(
        """SELECT achievement_id,subject_key,member_id,boss_key,tier_key,tier_rank,
                  first_qualifying_submission_id,config_version_id,earned_at
             FROM challenge_member_tier_achievements ORDER BY achievement_id"""
    ):
        provenance = canonical_json({
            "classification": "pre_authoritative_midgard_boss_tier_floor",
            "achievement_id": int(row["achievement_id"]),
            "submission_id": int(row["first_qualifying_submission_id"]),
            "config_version_id": int(row["config_version_id"]),
            "earned_at": row["earned_at"],
        })
        cur = conn.execute(
            """INSERT OR IGNORE INTO challenge_legacy_boss_tier_floors
               (snapshot_key,subject_key,member_id,boss_key,tier_key,tier_rank,
                source_achievement_id,source_submission_id,awardable,
                source_provenance_json,source_provenance_sha256)
               VALUES(?,?,?,?,?,?,?,?,0,?,?)""",
            (snapshot_key, row["subject_key"], row["member_id"], row["boss_key"],
             row["tier_key"], int(row["tier_rank"]), int(row["achievement_id"]),
             int(row["first_qualifying_submission_id"]), provenance, sha256_text(provenance)),
        )
        inserted += max(0, cur.rowcount)
    return inserted


def _active_config_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT config_version_id FROM challenge_config_versions WHERE status='active'").fetchone()
    if not row:
        raise RuntimeError("No active challenge config")
    return int(row[0])


def _tier_rows(conn: sqlite3.Connection, config_id: int, boss_key: str) -> dict[int, sqlite3.Row]:
    return {
        int(row["tier_rank"]): row
        for row in conn.execute(
            "SELECT * FROM challenge_tiers WHERE config_version_id=? AND boss_key=? ORDER BY tier_rank",
            (config_id, boss_key),
        )
    }


def _existing_delivery(conn: sqlite3.Connection, idempotency_key: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM challenge_award_deliveries WHERE idempotency_key=?", (idempotency_key,)
    ).fetchone())


def _cutover_incident_block(
    conn: sqlite3.Connection, subject_key: str, boss_key: str, tier_key: str
) -> sqlite3.Row | None:
    """Return an explicit append-only cutover disposition, when present."""
    try:
        return conn.execute(
            """SELECT classification,manual_review_required,item_key
                 FROM challenge_cutover_incident_items
                WHERE subject_key=? AND boss_key=? AND tier_key=?
                  AND automatic_award_blocked=1
                ORDER BY incident_item_id DESC LIMIT 1""",
            (subject_key, boss_key, tier_key),
        ).fetchone()
    except sqlite3.OperationalError:
        # Older test databases and pre-migration copies have no incident table.
        return None


def _candidate_exclusion(
    conn: sqlite3.Connection, idempotency_key: str
) -> sqlite3.Row | None:
    """Return an immutable operator exclusion for a non-deliverable artifact."""
    try:
        return conn.execute(
            """SELECT exclusion_key,reason_code FROM challenge_award_candidate_exclusions
                WHERE idempotency_key=? AND automatic_award_blocked=1""",
            (idempotency_key,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None


def _parsed_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text or len(text) <= 10:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _active_rank_authority_cutover(conn: sqlite3.Connection) -> sqlite3.Row | None:
    try:
        return conn.execute(
            """SELECT regular_submission_watermark,activated_at
                 FROM challenge_rank_authority_cutovers WHERE status='active'"""
        ).fetchone()
    except sqlite3.OperationalError:
        return None


def _achievement_is_pre_authority(
    conn: sqlite3.Connection,
    achievement: sqlite3.Row,
    legacy_cutoff: str | None,
) -> bool:
    """Classify chronology from immutable provenance, never a date-only guess."""
    source = conn.execute(
        """SELECT source_system,legacy_regular_submission_id,first_observed_at
             FROM challenge_submissions WHERE submission_id=?""",
        (int(achievement["first_qualifying_submission_id"]),),
    ).fetchone()
    cutover = _active_rank_authority_cutover(conn)
    if cutover and source:
        legacy_id = int(source["legacy_regular_submission_id"] or 0)
        if source["source_system"] == "regular_submissions_sheet_sync" and legacy_id > 0:
            return legacy_id <= int(cutover["regular_submission_watermark"])
        observed = _parsed_timestamp(source["first_observed_at"])
        boundary = _parsed_timestamp(cutover["activated_at"])
        if observed and boundary:
            return observed <= boundary
        # Ambiguous provenance stays blocked rather than guessing intra-day
        # order or converting a date-only value into a timestamp.
        return True

    if legacy_cutoff:
        boundary = _parsed_timestamp(legacy_cutoff)
        observed = _parsed_timestamp(source["first_observed_at"] if source else None)
        if observed and boundary:
            return observed <= boundary
        earned = _parsed_timestamp(achievement["earned_at"])
        if earned and boundary:
            return earned <= boundary
        return True
    return False


def boss_tier_candidates(conn: sqlite3.Connection) -> list[AwardCandidate]:
    candidates: list[AwardCandidate] = []
    snapshot = latest_snapshot(conn)
    cutoff = str(snapshot["snapshot_timestamp"]) if snapshot else None
    maxima = conn.execute(
        """SELECT subject_key,MAX(member_id) member_id,boss_key,MAX(tier_rank) max_rank
             FROM challenge_member_tier_achievements
            GROUP BY subject_key,boss_key ORDER BY subject_key,boss_key"""
    ).fetchall()
    for maximum in maxima:
        subject = str(maximum["subject_key"])
        boss = str(maximum["boss_key"])
        member_id = maximum["member_id"]
        max_rank = int(maximum["max_rank"])
        top = conn.execute(
            """SELECT * FROM challenge_member_tier_achievements
                WHERE subject_key=? AND boss_key=? AND tier_rank=?
                ORDER BY achievement_id LIMIT 1""",
            (subject, boss, max_rank),
        ).fetchone()
        if not top:
            continue
        config_id = int(top["config_version_id"])
        tiers = _tier_rows(conn, config_id, boss)
        if not tiers:
            config_id = _active_config_id(conn)
            tiers = _tier_rows(conn, config_id, boss)
        for rank in range(1, max_rank + 1):
            tier = tiers.get(rank)
            tier_key = str(tier["tier_key"]) if tier else f"rank-{rank}"
            idempotency = f"challenge-boss-tier:{subject}:{boss}:{tier_key}"
            achievement = conn.execute(
                """SELECT * FROM challenge_member_tier_achievements
                    WHERE subject_key=? AND boss_key=? AND tier_rank=?
                    ORDER BY achievement_id LIMIT 1""",
                (subject, boss, rank),
            ).fetchone() or top
            classification, reason = "eligible", "new evidence-backed boss tier"
            if member_id is None:
                classification, reason = "blocked_identity", "member identity is unresolved"
            elif not tier:
                classification, reason = "blocked_config", "tier is absent from its configuration version"
            elif _existing_delivery(conn, idempotency):
                classification, reason = "idempotent_existing", "award idempotency key already exists"
            elif exclusion := _candidate_exclusion(conn, idempotency):
                classification = "blocked_operator_exclusion"
                reason = (
                    str(exclusion["reason_code"]) + ": " + str(exclusion["exclusion_key"])
                )
            elif incident_block := _cutover_incident_block(conn, subject, boss, tier_key):
                if int(incident_block["manual_review_required"]):
                    classification = "blocked_manual_review"
                    reason = (
                        "cutover incident requires administrator review: "
                        + str(incident_block["item_key"])
                    )
                else:
                    classification = "blocked_legacy_floor"
                    reason = (
                        "cutover incident is preserved and non-awardable: "
                        + str(incident_block["item_key"])
                    )
            elif conn.execute(
                """SELECT 1 FROM challenge_legacy_boss_tier_floors
                    WHERE subject_key=? AND boss_key=? AND tier_rank>=?""", (subject, boss, rank)
            ).fetchone():
                classification, reason = "blocked_legacy_floor", "boss tier is covered by the frozen pre-cutover floor"
            elif conn.execute(
                """SELECT 1 FROM challenge_legacy_baselines
                    WHERE subject_key=? AND boss_key=? AND highest_tier_rank>=?""", (subject, boss, rank)
            ).fetchone():
                classification, reason = "blocked_legacy_floor", "boss tier is covered by a legacy aggregate floor"
            elif _achievement_is_pre_authority(conn, achievement, cutoff):
                classification, reason = "blocked_legacy_floor", "achievement predates the authoritative-award baseline"
            points = int(tier["source_submission_points"]) if tier else int(BOSS_TIER_POINTS.get(tier_key, 0))
            candidates.append(AwardCandidate(
                award_kind="boss_tier", subject_key=subject,
                member_id=int(member_id) if member_id is not None else None,
                boss_key=boss, tier_key=tier_key, tier_rank=rank,
                config_version_id=config_id, points=points,
                classification=classification, reason=reason,
                idempotency_key=idempotency,
                evidence_achievement_id=int(achievement["achievement_id"]),
                evidence_submission_id=int(achievement["first_qualifying_submission_id"]),
            ))
    return candidates


def system_tier_candidates(conn: sqlite3.Connection) -> list[AwardCandidate]:
    config_id = _active_config_id(conn)
    rules = conn.execute(
        "SELECT * FROM challenge_system_tiers WHERE config_version_id=? ORDER BY tier_rank", (config_id,)
    ).fetchall()
    candidates: list[AwardCandidate] = []
    for progress in conn.execute(
        "SELECT * FROM challenge_member_progress WHERE config_version_id=? ORDER BY subject_key", (config_id,)
    ):
        subject = str(progress["subject_key"])
        member_id = progress["member_id"]
        for rule in rules:
            tier_key = str(rule["system_tier_key"])
            complete = bool(progress["ascendant_eligible"]) if rule["require_all_active_challenges"] else (
                int(progress["challenge_progression_points"]) >= int(rule["min_progression_points"] or 0)
            )
            if not complete:
                continue
            idempotency = f"challenge-system-tier:{subject}:{tier_key}"
            existing_award = conn.execute(
                """SELECT * FROM challenge_tier_awards
                    WHERE subject_key=? AND system_tier_key=?""", (subject, tier_key)
            ).fetchone()
            manual_review = bool(existing_award and conn.execute(
                """SELECT 1 FROM challenge_award_reviews
                    WHERE award_id=? AND review_state='manual_review_required'""",
                (existing_award["award_id"],),
            ).fetchone())
            classification, reason = "eligible", "new evidence-backed system tier"
            if member_id is None:
                classification, reason = "blocked_identity", "member identity is unresolved"
            elif existing_award and existing_award["award_state"] in ("grandfathered", "delivered"):
                classification, reason = "blocked_grandfathered", "system tier was already historically or locally awarded"
            elif manual_review:
                classification, reason = "blocked_manual_review", "historical ambiguity requires administrator review"
            elif _existing_delivery(conn, idempotency):
                classification, reason = "idempotent_existing", "award idempotency key already exists"
            else:
                eligible, gate_reason = future_award_eligibility(conn, subject, tier_key)
                if not eligible:
                    classification = {
                        "covered_by_legacy_completion_floor": "blocked_legacy_floor",
                        "historically_or_midgard_awarded": "blocked_grandfathered",
                        "manual_review_required": "blocked_manual_review",
                    }.get(gate_reason, "blocked_not_new")
                    reason = gate_reason
            candidates.append(AwardCandidate(
                award_kind="system_tier", subject_key=subject,
                member_id=int(member_id) if member_id is not None else None,
                boss_key=None, tier_key=tier_key, tier_rank=int(rule["tier_rank"]),
                config_version_id=config_id, points=int(rule["one_time_rank_bonus"]),
                classification=classification, reason=reason,
                idempotency_key=idempotency,
                system_tier_award_id=int(existing_award["award_id"]) if existing_award else None,
            ))
    return candidates


def dry_run_rehearsal(conn: sqlite3.Connection) -> dict[str, Any]:
    boss = boss_tier_candidates(conn)
    system = system_tier_candidates(conn)
    all_candidates = boss + system
    counts = Counter(item.classification for item in all_candidates)
    eligible = [item for item in all_candidates if item.classification == "eligible"]
    return {
        "mode": "dry_run",
        "eligible_awards": len(eligible),
        "eligible_points": sum(item.points for item in eligible),
        "planned_rank_writes": len(eligible),
        "planned_google_award_rows": len(eligible),
        "classifications": dict(sorted(counts.items())),
        "boss_tier_candidates": [asdict(item) for item in boss],
        "system_tier_candidates": [asdict(item) for item in system],
    }


def _record_transition(
    conn: sqlite3.Connection, delivery_id: int, from_state: str | None, to_state: str,
    actor: str, reason: str, details: dict[str, Any] | None = None,
) -> None:
    payload = canonical_json(details or {})
    conn.execute(
        """INSERT INTO challenge_award_transition_log
           (delivery_id,from_state,to_state,actor_type,actor_id,reason,
            details_json,details_sha256,transitioned_at)
           VALUES(?,?,?,'service',?,?,?,?,?)""",
        (delivery_id, from_state, to_state, actor, reason, payload, sha256_text(payload), utc_now()),
    )


def create_planned_delivery(
    conn: sqlite3.Connection, candidate: AwardCandidate, expected_rank_points: int,
    *, dry_run: bool = True, actor: str = "challenge-award-planner",
) -> int:
    if candidate.classification != "eligible":
        raise ValueError("only eligible candidates may become delivery records")
    now = utc_now()
    cur = conn.execute(
        """INSERT OR IGNORE INTO challenge_award_deliveries
           (award_kind,subject_key,member_id,boss_key,tier_key,tier_rank,config_version_id,
            points,delivery_state,idempotency_key,rank_write_key,evidence_achievement_id,
            evidence_submission_id,system_tier_award_id,expected_rank_points,dry_run,
            eligible_at,planned_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,'planned',?,?,?,?,?,?,?, ?,?,?)""",
        (candidate.award_kind, candidate.subject_key, candidate.member_id, candidate.boss_key,
         candidate.tier_key, candidate.tier_rank, candidate.config_version_id, candidate.points,
         candidate.idempotency_key, "rank-write:" + candidate.idempotency_key,
         candidate.evidence_achievement_id, candidate.evidence_submission_id,
         candidate.system_tier_award_id, expected_rank_points, int(dry_run), now, now, now),
    )
    if cur.rowcount:
        delivery_id = int(cur.lastrowid)
        _record_transition(conn, delivery_id, None, "planned", actor, "eligible award planned", {
            "dry_run": dry_run, "idempotency_key": candidate.idempotency_key,
        })
    else:
        delivery_id = int(conn.execute(
            "SELECT delivery_id FROM challenge_award_deliveries WHERE idempotency_key=?",
            (candidate.idempotency_key,),
        ).fetchone()[0])
    return delivery_id


def transition_delivery(
    conn: sqlite3.Connection, delivery_id: int, to_state: str, *, actor: str,
    reason: str, details: dict[str, Any] | None = None,
) -> None:
    if to_state not in ALL_STATES:
        raise ValueError("invalid delivery state")
    row = conn.execute(
        "SELECT delivery_state FROM challenge_award_deliveries WHERE delivery_id=?", (delivery_id,)
    ).fetchone()
    if not row:
        raise LookupError("award delivery not found")
    before = str(row[0])
    conn.execute(
        "UPDATE challenge_award_deliveries SET delivery_state=?,updated_at=? WHERE delivery_id=?",
        (to_state, utc_now(), delivery_id),
    )
    _record_transition(conn, delivery_id, before, to_state, actor, reason, details)


def rank_write_preview(conn: sqlite3.Connection, members: sqlite3.Connection, delivery_id: int) -> dict[str, Any]:
    delivery = conn.execute("SELECT * FROM challenge_award_deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()
    if not delivery or delivery["member_id"] is None:
        raise LookupError("resolved delivery not found")
    member = members.execute(
        "SELECT COALESCE(rank_points,0) rank_points FROM members WHERE member_id=?", (delivery["member_id"],)
    ).fetchone()
    if not member:
        raise LookupError("member not found")
    before = int(member[0])
    return {
        "dry_run": True,
        "delivery_id": int(delivery_id),
        "member_id": int(delivery["member_id"]),
        "expected_before": int(delivery["expected_rank_points"]),
        "observed_before": before,
        "points_delta": int(delivery["points"]),
        "would_be_after": before + int(delivery["points"]),
        "would_execute_update": False,
    }


def apply_rank_write(
    challenges_db: Path, members_db: Path, delivery_id: int, *, capability: str,
) -> dict[str, Any]:
    """Future live adapter. Tests use temporary databases; production remains disabled."""
    if capability != LIVE_CAPABILITY:
        raise DeliveryDisabled("explicit live rank-write capability is required")
    conn = sqlite3.connect(challenges_db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("ATTACH DATABASE ? AS members_db", (str(members_db),))
    try:
        conn.execute("BEGIN IMMEDIATE")
        settings = {str(r[0]): str(r[1]) for r in conn.execute(
            "SELECT setting_key,setting_value FROM challenge_settings WHERE setting_key IN ('award_mode','award_delivery_mode')"
        )}
        if settings.get("award_mode") != "live" or settings.get("award_delivery_mode") != "live":
            raise DeliveryDisabled("rank writes are disabled by database settings")
        delivery = conn.execute(
            "SELECT * FROM challenge_award_deliveries WHERE delivery_id=?", (delivery_id,)
        ).fetchone()
        if not delivery or delivery["dry_run"] or delivery["member_id"] is None:
            raise DeliveryDisabled("delivery is dry-run or unresolved")
        receipt = conn.execute(
            "SELECT * FROM challenge_rank_write_receipts WHERE delivery_id=?", (delivery_id,)
        ).fetchone()
        if receipt:
            conn.rollback()
            return {"idempotent": True, "actual_after": int(receipt["actual_after"])}
        if delivery["delivery_state"] != "rank_write_pending":
            raise DeliveryDisabled("delivery is not rank_write_pending")
        expected = int(delivery["expected_rank_points"])
        points = int(delivery["points"])
        cur = conn.execute(
            """UPDATE members_db.members SET rank_points=COALESCE(rank_points,0)+?
                WHERE member_id=? AND COALESCE(rank_points,0)=?""",
            (points, int(delivery["member_id"]), expected),
        )
        if cur.rowcount != 1:
            raise ConcurrentRankChange("member rank points changed since the award was planned")
        after = expected + points
        now = utc_now()
        conn.execute(
            """INSERT INTO challenge_rank_write_receipts
               (delivery_id,write_key,member_id,points_delta,expected_before,
                actual_before,actual_after,committed_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (delivery_id, delivery["rank_write_key"], int(delivery["member_id"]),
             points, expected, expected, after, now),
        )
        conn.execute(
            "UPDATE challenge_award_deliveries SET delivery_state='rank_written',updated_at=? WHERE delivery_id=?",
            (now, delivery_id),
        )
        _record_transition(conn, delivery_id, "rank_write_pending", "rank_written",
                           "challenge-rank-writer", "guarded additive rank write committed",
                           {"expected_before": expected, "points_delta": points, "actual_after": after})
        conn.commit()
        return {"idempotent": False, "actual_after": after}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


LEGACY_FORMULA = "=IFERROR(VLOOKUP(B:B,Leaderboards!A:B,2,FALSE), 0)"


def _date_only(value: str | None) -> str:
    return str(value or utc_now())[:10]


def _mirror_notes(base: str, tags: list[str], *, maximum: int = 45000) -> str:
    """Keep stable identifiers intact while staying below the Sheets cell limit."""
    suffix = " | ".join(tag.strip() for tag in tags if tag.strip())
    clean = str(base or "").strip()
    if not clean:
        return suffix
    available = maximum - len(suffix) - 3
    if available < 0:
        raise ValueError("Midgard mirror metadata exceeds the Sheets note limit")
    if len(clean) > available:
        clean = clean[: max(0, available - 15)].rstrip() + " [truncated]"
    return f"{clean} | {suffix}"


def build_challenge_submission_row(submission: sqlite3.Row, discord_id: str, points: int) -> dict[str, Any]:
    source_record = str(submission["source_record_id"] or "").strip()
    note = _mirror_notes(str(submission["raw_notes"] or ""), [
        "midgard_challenge_managed=1",
        f"midgard_submission_id={int(submission['submission_id'])}",
        f"midgard_source_record_id={source_record}",
    ])
    return {
        "target_range": "submissions!A:G",
        "values": [[LEGACY_FORMULA, discord_id,
                    f"{submission['boss_display_name']} - {str(submission['earned_tier_key']).title()}",
                    int(points), _date_only(submission["source_approved_at"]),
                    str(submission["evidence_url"] or ""), note]],
        "rank_bearing": True,
        "coalesces_with_exact_boss_tier_award": True,
        "duplicate_policy": "stable_identifiers_only",
        "midgard_submission_id": int(submission["submission_id"]),
    }


def build_boss_tier_award_row(delivery: sqlite3.Row, display_name: str, discord_id: str, evidence_url: str = "") -> dict[str, Any]:
    tags = [
        "MIDGARD CHALLENGE AWARD",
        "midgard_challenge_managed=1",
        f"midgard_award_id={int(delivery['delivery_id'])}",
        f"midgard_idempotency_key={delivery['idempotency_key']}",
        f"midgard_config_version_id={int(delivery['config_version_id'])}",
    ]
    if delivery["evidence_submission_id"] is not None:
        tags.append(f"midgard_source_submission_id={int(delivery['evidence_submission_id'])}")
    note = _mirror_notes("", tags)
    return {
        "target_range": "submissions!A:G",
        "values": [[LEGACY_FORMULA, discord_id,
                    f"{display_name} - {str(delivery['tier_key']).title()}",
                    int(delivery["points"]), _date_only(delivery["eligible_at"]), evidence_url, note]],
        "rank_bearing": True,
        "duplicate_policy": "stable_or_legacy_award_equivalent",
        "midgard_award_id": int(delivery["delivery_id"]),
    }


def build_system_tier_award_row(delivery: sqlite3.Row, discord_id: str) -> dict[str, Any]:
    note = _mirror_notes("", [
        "MIDGARD SYSTEM TIER AWARD",
        "midgard_challenge_managed=1",
        f"midgard_award_id={int(delivery['delivery_id'])}",
        f"midgard_idempotency_key={delivery['idempotency_key']}",
        f"midgard_config_version_id={int(delivery['config_version_id'])}",
    ])
    return {
        "target_range": "submissions!A:G",
        "values": [[LEGACY_FORMULA, discord_id, f"{str(delivery['tier_key']).title()} Awarded",
                    int(delivery["points"]), _date_only(delivery["eligible_at"]), "", note]],
        "rank_bearing": True,
        "duplicate_policy": "stable_or_legacy_award_equivalent",
        "midgard_award_id": int(delivery["delivery_id"]),
    }


def build_progress_projection(progress: sqlite3.Row) -> dict[str, Any]:
    return {
        "target_range": "Challenge_Progress",
        "key": str(progress["subject_key"]),
        "values": {
            "challenge_progression_points": int(progress["challenge_progression_points"]),
            "active_challenges_completed": int(progress["active_challenges_completed"]),
            "active_challenge_count": int(progress["active_challenge_count"]),
            "current_system_tier_key": progress["current_system_tier_key"],
            "ascendant_eligible": bool(progress["ascendant_eligible"]),
            "config_version_id": int(progress["config_version_id"]),
        },
        "rank_bearing": False,
    }


def prepare_mirror_operation(
    conn: sqlite3.Connection, *, operation_kind: str, idempotency_key: str,
    target_range: str, payload: dict[str, Any], delivery_id: int | None = None,
    submission_id: int | None = None,
) -> int:
    if operation_kind not in {
        "challenge_submission", "boss_tier_award", "system_tier_award",
        "challenge_progress", "leaderboard_projection",
    }:
        raise ValueError("invalid Google mirror operation kind")
    rendered = canonical_json(payload)
    cur = conn.execute(
        """INSERT OR IGNORE INTO challenge_google_mirror_operations
           (delivery_id,submission_id,operation_kind,idempotency_key,target_range,
            payload_json,payload_sha256,mirror_status,updated_at)
           VALUES(?,?,?,?,?,?,?,'planned',?)""",
        (delivery_id, submission_id, operation_kind, idempotency_key, target_range,
         rendered, sha256_text(rendered), utc_now()),
    )
    if cur.rowcount:
        return int(cur.lastrowid)
    existing = conn.execute(
        "SELECT mirror_id FROM challenge_google_mirror_operations WHERE idempotency_key=?",
        (idempotency_key,),
    ).fetchone()
    if not existing:
        raise RuntimeError("mirror operation could not be created")
    return int(existing[0])


class MirrorSink(Protocol):
    def find(self, idempotency_key: str, payload: dict[str, Any] | None = None) -> MirrorLookup | str | None: ...
    def write(self, idempotency_key: str, payload: dict[str, Any], behavior: str = "success") -> str: ...


class InMemoryMirrorSink:
    """Test-only sink; it performs no network or Google API calls."""
    def __init__(self) -> None:
        self.rows: dict[str, tuple[str, dict[str, Any]]] = {}
        self.write_calls = 0

    def find(self, idempotency_key: str, payload: dict[str, Any] | None = None) -> MirrorLookup | None:
        row = self.rows.get(idempotency_key)
        return MirrorLookup(row[0], "stable_idempotency_key", sha256_text(canonical_json(row[1]))) if row else None

    def write(self, idempotency_key: str, payload: dict[str, Any], behavior: str = "success") -> str:
        self.write_calls += 1
        if behavior == "timeout_before_write":
            raise MirrorRetryable("simulated timeout before write")
        if idempotency_key in self.rows:
            return self.rows[idempotency_key][0]
        reference = f"mock-row-{len(self.rows) + 1}"
        self.rows[idempotency_key] = (reference, payload)
        if behavior == "response_lost":
            raise MirrorAmbiguousWrite("simulated response loss after write")
        return reference


class SheetsBridgeRunner:
    """JSON/stdin bridge so credential values and row payloads never enter argv."""

    def __init__(self, bridge: Path = DEFAULT_GOOGLE_BRIDGE):
        configured = os.environ.get("NOCTURNE_CHALLENGE_GOOGLE_BRIDGE")
        self.bridge = Path(configured) if configured else Path(bridge)

    def __call__(self, request: dict[str, Any]) -> dict[str, Any]:
        completed = subprocess.run(
            ["/usr/bin/node", str(self.bridge)],
            input=canonical_json(request), text=True, capture_output=True,
            check=False, timeout=45,
            env={**os.environ, "NODE_NO_WARNINGS": "1"},
        )
        if completed.returncode != 0:
            try:
                detail = json.loads(completed.stderr.strip() or completed.stdout.strip())
                code = str(detail.get("error_code") or "BRIDGE_FAILED")
            except Exception:
                code = "BRIDGE_FAILED"
            if code == "GOOGLE_APPEND_AMBIGUOUS":
                raise MirrorAmbiguousWrite("Google append outcome is ambiguous and requires verification")
            if code in {"GOOGLE_APPEND_REJECTED", "RANGE_NOT_ALLOWED", "ROW_CONTRACT_INVALID",
                        "SPREADSHEET_NOT_ALLOWED", "CREDENTIAL_UNAVAILABLE"}:
                raise MirrorTerminal(f"Google Sheets bridge rejected operation: {code}")
            raise MirrorRetryable(f"Google Sheets bridge failed: {code}")
        result = json.loads(completed.stdout)
        if not result.get("ok"):
            raise MirrorRetryable(f"Google Sheets bridge rejected operation: {result.get('error_code','UNKNOWN')}")
        return result


class GoogleSheetsMirrorSink:
    """Production Sheets sink with independent database and capability guards."""

    def __init__(
        self, conn: sqlite3.Connection, *, runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        spreadsheet_id: str = CLAN_INFORMATION_SPREADSHEET_ID,
        credential_path: Path = DEFAULT_GOOGLE_CREDENTIAL,
        write_capability: str | None = None,
    ) -> None:
        self.conn = conn
        self.runner = runner or SheetsBridgeRunner()
        self.spreadsheet_id = spreadsheet_id
        self.credential_path = Path(credential_path)
        self.write_capability = write_capability
        self.write_calls = 0

    def _settings(self) -> dict[str, str]:
        return {str(row[0]): str(row[1]) for row in self.conn.execute(
            "SELECT setting_key,setting_value FROM challenge_settings WHERE setting_key IN "
            "('award_mode','award_delivery_mode','google_mirror_mode')"
        )}

    def _write_enabled(self) -> bool:
        settings = self._settings()
        return (
            settings.get("award_mode") == "live"
            and settings.get("award_delivery_mode") == "live"
            and settings.get("google_mirror_mode") == "live"
            and self.write_capability == GOOGLE_WRITE_CAPABILITY
        )

    @staticmethod
    def _expected_row(payload: dict[str, Any]) -> list[Any]:
        values = payload.get("values")
        if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], list):
            raise ValueError("Google mirror payload must contain exactly one row")
        if len(values[0]) != 7:
            raise ValueError("Submissions mirror rows must contain exactly seven columns")
        return values[0]

    def verify_contract(self) -> dict[str, Any]:
        return self.runner({
            "action": "verify",
            "spreadsheet_id": self.spreadsheet_id,
            "credential_path": str(self.credential_path),
            "challenges_db": "",
        })

    def find(self, idempotency_key: str, payload: dict[str, Any] | None = None) -> MirrorLookup | None:
        if payload is None:
            raise ValueError("payload is required for production duplicate verification")
        expected = self._expected_row(payload)
        result = self.runner({
            "action": "find",
            "spreadsheet_id": self.spreadsheet_id,
            "credential_path": str(self.credential_path),
            "target_range": payload.get("target_range"),
            "idempotency_key": idempotency_key,
            "award_id": payload.get("midgard_award_id"),
            "expected_row": expected,
            "allow_legacy_equivalent": payload.get("duplicate_policy") == "stable_or_legacy_award_equivalent",
        })
        match = result.get("match")
        if not match:
            return None
        return MirrorLookup(str(match["reference"]), str(match["method"]), match.get("row_sha256"))

    def write(self, idempotency_key: str, payload: dict[str, Any], behavior: str = "success") -> str:
        if behavior != "success":
            raise ValueError("production Sheets sink does not accept synthetic behaviors")
        if not self._write_enabled():
            raise DeliveryDisabled("production Google Sheets writes are disabled")
        self.write_calls += 1
        result = self.runner({
            "action": "append",
            "spreadsheet_id": self.spreadsheet_id,
            "credential_path": str(self.credential_path),
            "challenges_db": str(self.conn.execute("PRAGMA database_list").fetchone()[2]),
            "target_range": payload.get("target_range"),
            "idempotency_key": idempotency_key,
            "expected_row": self._expected_row(payload),
            "write_capability": self.write_capability,
        })
        return str(result["reference"])


class GoogleMirrorAdapter:
    """Idempotent mirror coordinator for test and production sinks."""
    def __init__(self, conn: sqlite3.Connection, sink: MirrorSink):
        self.conn = conn
        self.sink = sink

    def _attempt(self, mirror_id: int, outcome: str, code: str | None, reference: str | None) -> None:
        number = int(self.conn.execute(
            "SELECT mirror_attempts FROM challenge_google_mirror_operations WHERE mirror_id=?", (mirror_id,)
        ).fetchone()[0]) + 1
        detail_hash = sha256_text(code or "") if code else None
        self.conn.execute(
            """INSERT INTO challenge_google_mirror_attempts
               (mirror_id,attempt_number,outcome,detail_code,detail_sha256,google_reference,attempted_at)
               VALUES(?,?,?,?,?,?,?)""",
            (mirror_id, number, outcome, code, detail_hash, reference, utc_now()),
        )
        self.conn.execute(
            "UPDATE challenge_google_mirror_operations SET mirror_attempts=?,updated_at=? WHERE mirror_id=?",
            (number, utc_now(), mirror_id),
        )

    def mirror_once(self, mirror_id: int, *, behavior: str = "success") -> dict[str, Any]:
        mode = self.conn.execute(
            "SELECT setting_value FROM challenge_settings WHERE setting_key='google_mirror_mode'"
        ).fetchone()
        if not mode or mode[0] != "live":
            raise DeliveryDisabled("Google mirror is disabled")
        row = self.conn.execute(
            "SELECT * FROM challenge_google_mirror_operations WHERE mirror_id=?", (mirror_id,)
        ).fetchone()
        if not row:
            raise LookupError("mirror operation not found")
        payload = json.loads(row["payload_json"])
        existing = self.sink.find(str(row["idempotency_key"]), payload)
        if existing:
            lookup = existing if isinstance(existing, MirrorLookup) else MirrorLookup(str(existing), "stable_idempotency_key")
            detail_hash = sha256_text(canonical_json({"method": lookup.method, "row_sha256": lookup.row_sha256}))
            self._attempt(mirror_id, "verified_existing", lookup.method, lookup.reference)
            self.conn.execute(
                """UPDATE challenge_google_mirror_operations
                    SET mirror_status='verified',google_reference=?,verification_method=?,
                        verification_details_sha256=?,google_row_sha256=?,verified_at=?,updated_at=?
                    WHERE mirror_id=?""",
                (lookup.reference, lookup.method, detail_hash, lookup.row_sha256,
                 utc_now(), utc_now(), mirror_id)
            )
            return {"idempotent": True, "google_reference": lookup.reference, "match_method": lookup.method}
        try:
            reference = self.sink.write(str(row["idempotency_key"]), payload, behavior)
        except MirrorAmbiguousWrite as exc:
            self._attempt(mirror_id, "ambiguous", "response_lost", None)
            self.conn.execute(
                """UPDATE challenge_google_mirror_operations
                    SET mirror_status='written_unverified',last_error_code='response_lost',
                        last_error_sha256=?,updated_at=? WHERE mirror_id=?""",
                (sha256_text(str(exc)), utc_now(), mirror_id),
            )
            return {"idempotent": False, "verification_required": True}
        except MirrorRetryable as exc:
            self._attempt(mirror_id, "retryable_failure", "timeout_before_write", None)
            self.conn.execute(
                """UPDATE challenge_google_mirror_operations
                    SET mirror_status='failed_retryable',last_error_code='timeout_before_write',
                        last_error_sha256=?,updated_at=? WHERE mirror_id=?""",
                (sha256_text(str(exc)), utc_now(), mirror_id),
            )
            return {"idempotent": False, "retryable": True}
        except MirrorTerminal as exc:
            self._attempt(mirror_id, "terminal_failure", "google_append_rejected", None)
            self.conn.execute(
                """UPDATE challenge_google_mirror_operations
                    SET mirror_status='failed_terminal',last_error_code='google_append_rejected',
                        last_error_sha256=?,updated_at=? WHERE mirror_id=?""",
                (sha256_text(str(exc)), utc_now(), mirror_id),
            )
            return {"idempotent": False, "terminal": True}
        verified = self.sink.find(str(row["idempotency_key"]), payload)
        verified_lookup = verified if isinstance(verified, MirrorLookup) else (
            MirrorLookup(str(verified), "stable_idempotency_key") if verified else None
        )
        if not verified_lookup or verified_lookup.reference != reference:
            raise MirrorRetryable("post-write verification failed")
        self._attempt(mirror_id, "verified_written", None, reference)
        self.conn.execute(
            """UPDATE challenge_google_mirror_operations
                SET mirror_status='verified',google_reference=?,verification_method=?,google_row_sha256=?,
                    verified_at=?,last_error_code=NULL,last_error_sha256=NULL,updated_at=? WHERE mirror_id=?""",
            (reference, verified_lookup.method, verified_lookup.row_sha256,
             utc_now(), utc_now(), mirror_id),
        )
        return {"idempotent": False, "google_reference": reference}


def diagnostics(conn: sqlite3.Connection) -> dict[str, Any]:
    settings = {str(r[0]): str(r[1]) for r in conn.execute(
        """SELECT setting_key,setting_value FROM challenge_settings
            WHERE setting_key IN ('award_mode','direct_intake_mode','award_delivery_mode','google_mirror_mode')"""
    )}
    rehearsal = dry_run_rehearsal(conn)
    delivery_states = {str(r[0]): int(r[1]) for r in conn.execute(
        "SELECT delivery_state,COUNT(*) FROM challenge_award_deliveries GROUP BY delivery_state"
    )}
    mirror_states = {str(r[0]): int(r[1]) for r in conn.execute(
        "SELECT mirror_status,COUNT(*) FROM challenge_google_mirror_operations GROUP BY mirror_status"
    )}
    manual_reviews = [
        {
            "member_id": row["member_id"],
            "subject_key": row["subject_key"],
            "system_tier_key": row["system_tier_key"],
            "hypothetical_points": int(row["would_award_points"]),
            "reason": row["reason"],
            "block_state": row["review_state"],
            "evidence_snapshot": {
                "award_state": row["award_state"],
                "eligibility_config_version_id": int(row["eligibility_config_version_id"]),
                "eligible_at": row["eligible_at"],
                "historical_source": row["historical_source"],
            },
        }
        for row in conn.execute(
            """SELECT a.member_id,a.subject_key,a.system_tier_key,a.would_award_points,
                      a.award_state,a.eligibility_config_version_id,a.eligible_at,a.historical_source,
                      r.reason,r.review_state
                 FROM challenge_award_reviews r JOIN challenge_tier_awards a ON a.award_id=r.award_id
                WHERE r.review_state='manual_review_required'
                ORDER BY a.subject_key,a.system_tier_key"""
        )
    ]
    return {
        **settings,
        "dry_run": {
            "eligible_awards": rehearsal["eligible_awards"],
            "eligible_points": rehearsal["eligible_points"],
            "planned_rank_writes": rehearsal["planned_rank_writes"],
            "planned_google_award_rows": rehearsal["planned_google_award_rows"],
            "classifications": rehearsal["classifications"],
        },
        "delivery_states": delivery_states,
        "mirror_states": mirror_states,
        "rank_write_receipts": int(conn.execute("SELECT COUNT(*) FROM challenge_rank_write_receipts").fetchone()[0]),
        "mirror_attempts": int(conn.execute("SELECT COUNT(*) FROM challenge_google_mirror_attempts").fetchone()[0]),
        "boss_tier_legacy_floors": int(conn.execute("SELECT COUNT(*) FROM challenge_legacy_boss_tier_floors").fetchone()[0]),
        "manual_review_required": int(conn.execute(
            "SELECT COUNT(*) FROM challenge_award_reviews WHERE review_state='manual_review_required'"
        ).fetchone()[0]),
        "manual_review_details": manual_reviews,
        "grandfathered": int(conn.execute(
            "SELECT COUNT(*) FROM challenge_tier_awards WHERE award_state='grandfathered'"
        ).fetchone()[0]),
        "queued": int(conn.execute(
            "SELECT COUNT(*) FROM challenge_award_deliveries WHERE delivery_state='queued'"
        ).fetchone()[0]),
        "delivered": int(conn.execute(
            "SELECT COUNT(*) FROM challenge_award_deliveries WHERE delivery_state='delivered'"
        ).fetchone()[0]),
        "integrity_check": conn.execute("PRAGMA integrity_check").fetchone()[0],
    }
