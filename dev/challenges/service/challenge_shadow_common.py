#!/usr/bin/env python3
"""Shared schema, configuration, and parsing for the challenge shadow engine.

This module deliberately has no network, Google, Discord, or rank-point writer.
The only writable database it knows about is Challenges.db.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_CHALLENGES_DB = Path("/srv/projects/database/Challenges.db")
DEFAULT_REGULAR_DB = Path("/srv/projects/database/RegularSubmissions.db")
DEFAULT_MEMBERS_DB = Path("/srv/projects/database/Members.db")

CONFIG_VERSION_KEY = "google-live-2026-09-28"
CONFIG_SOURCE = "Noctracker v2.1 / live challenge requirements cache"
PARSER_VERSION = "phase3a-centiseconds-v1"

TIERS = (
    ("bronze", "Bronze", 1, 10, 10),
    ("silver", "Silver", 2, 20, 30),
    ("gold", "Gold", 3, 30, 60),
    ("platinum", "Platinum", 4, 40, 100),
    ("ascendant", "Ascendant", 5, 50, 150),
)

SYSTEM_TIERS = (
    ("bronze", "Bronze", 1, 150, 0, 75),
    ("silver", "Silver", 2, 450, 0, 150),
    ("gold", "Gold", 3, 900, 0, 225),
    ("platinum", "Platinum", 4, 1500, 0, 300),
    ("ascendant", "Ascendant", 5, None, 1, 500),
)


def time_ms(value: str) -> int:
    parts = value.strip().split(":")
    if len(parts) == 4:
        # Historical Apps Script rows used H:MM:SS:CC, where CC is
        # centiseconds. Keep this deliberately strict; malformed or descriptive
        # suffixes remain quarantined rather than guessed.
        if not all(re.fullmatch(r"\d+", part) for part in parts):
            raise ValueError(f"Non-numeric legacy time component: {value!r}")
        hours, minutes, seconds, centiseconds = map(int, parts)
        if minutes >= 60 or seconds >= 60 or centiseconds >= 100:
            raise ValueError(f"Out-of-range legacy time component: {value!r}")
        return ((hours * 3600 + minutes * 60 + seconds) * 1000) + centiseconds * 10
    if len(parts) == 3:
        hours, minutes, seconds = int(parts[0]), int(parts[1]), float(parts[2])
    elif len(parts) == 2:
        hours, minutes, seconds = 0, int(parts[0]), float(parts[1])
    else:
        raise ValueError(f"Unsupported time: {value!r}")
    return int(round((hours * 3600 + minutes * 60 + seconds) * 1000))


BOSS_CONFIG: tuple[dict[str, Any], ...] = (
    {"key": "gauntlet", "name": "Gauntlet", "metric": "time", "direction": "lower", "requirements": ("Completion", "0:08:30", "0:07:30", "0:06:30", "0:06:00")},
    {"key": "colosseum", "name": "Colosseum", "metric": "time", "direction": "lower", "requirements": ("Completion", "0:32:00", "0:28:00", "0:24:00", "0:20:00")},
    {"key": "delve", "name": "Delve", "metric": "numeric", "direction": "higher", "requirements": ("Completion", "10", "16", "22", "30")},
    {"key": "phosanis", "name": "Phosani's", "metric": "time", "direction": "lower", "requirements": ("Completion", "0:08:00", "0:07:30", "0:06:00", "0:05:00")},
    {"key": "cox_1", "name": "Cox 1", "metric": "time", "direction": "lower", "requirements": ("Completion", "0:20:00", "0:17:00", "0:15:30", "0:14:30")},
    {"key": "cm_1", "name": "Cm 1", "metric": "time", "direction": "lower", "requirements": ("Completion", "1:10:00", "0:45:00", "0:38:30", "0:33:00")},
    {"key": "cm_3", "name": "Cm 3", "metric": "time", "direction": "lower", "requirements": ("Completion", "0:40:00", "0:35:00", "0:27:00", "0:22:30")},
    {"key": "cm_5", "name": "Cm 5", "metric": "time", "direction": "lower", "requirements": ("Completion", "0:38:30", "0:30:00", "0:25:00", "0:21:00")},
    {"key": "tob_2", "name": "Tob 2", "metric": "time", "direction": "lower", "requirements": ("Completion", "0:30:00", "0:28:00", "0:26:00", "0:22:00")},
    {"key": "tob_3", "name": "Tob 3", "metric": "time", "direction": "lower", "requirements": ("Completion", "0:23:00", "0:20:00", "0:17:30", "0:15:45")},
    {"key": "tob_5", "name": "Tob 5", "metric": "time", "direction": "lower", "requirements": ("Completion", "0:17:00", "0:16:00", "0:14:15", "0:12:45")},
    {"key": "hmt_5", "name": "Hmt 5", "metric": "time", "direction": "lower", "requirements": ("Completion", "0:22:00", "0:19:30", "0:19:00", "0:17:45")},
    {"key": "toa_1_300", "name": "Toa 1 300", "metric": "time", "direction": "lower", "requirements": ("Completion", "0:30:00", "0:27:00", "0:25:00", "0:22:00")},
    {"key": "jad", "name": "Jad", "metric": "time", "direction": "lower", "requirements": ("Completion", "0:40:00", "0:30:00", "0:26:30", "0:23:00")},
    {"key": "zuk", "name": "Zuk", "metric": "time", "direction": "lower", "requirements": ("Completion", "1:30:00", "1:15:00", "1:05:00", "0:55:00")},
)

BOSS_BY_KEY = {b["key"]: b for b in BOSS_CONFIG}
BOSS_KEY_BY_NORMALIZED_NAME = {
    re.sub(r"[^a-z0-9]+", "", b["name"].lower()): b["key"] for b in BOSS_CONFIG
}
TIER_BY_NAME = {name.lower(): (key, display, rank, incremental, cumulative) for key, display, rank, incremental, cumulative in TIERS for name in (key, display)}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_rsn(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def ro_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def rw_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS challenge_settings (
    setting_key TEXT PRIMARY KEY,
    setting_value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_by TEXT NOT NULL DEFAULT 'challenge_shadow_migration'
);

CREATE TABLE IF NOT EXISTS challenge_config_versions (
    config_version_id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_key TEXT NOT NULL UNIQUE,
    source_name TEXT NOT NULL,
    source_snapshot_sha256 TEXT NOT NULL UNIQUE,
    config_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','active','retired')),
    effective_from TEXT,
    effective_to TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_challenge_config_one_active
ON challenge_config_versions((1)) WHERE status='active';

CREATE TABLE IF NOT EXISTS challenge_tiers (
    challenge_tier_id INTEGER PRIMARY KEY AUTOINCREMENT,
    config_version_id INTEGER NOT NULL,
    boss_key TEXT NOT NULL,
    tier_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    tier_rank INTEGER NOT NULL CHECK(tier_rank BETWEEN 1 AND 5),
    source_submission_points INTEGER NOT NULL,
    cumulative_progression_points INTEGER NOT NULL,
    requirement_metric_type TEXT NOT NULL CHECK(requirement_metric_type IN ('completion','time','numeric')),
    requirement_operator TEXT NOT NULL CHECK(requirement_operator IN ('complete','lte','gte')),
    requirement_value INTEGER,
    requirement_unit TEXT NOT NULL,
    requirement_display TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(config_version_id,boss_key,tier_key),
    UNIQUE(config_version_id,boss_key,tier_rank),
    FOREIGN KEY(config_version_id) REFERENCES challenge_config_versions(config_version_id),
    FOREIGN KEY(boss_key) REFERENCES challenge_bosses(boss_key)
);

CREATE TABLE IF NOT EXISTS challenge_system_tiers (
    system_tier_id INTEGER PRIMARY KEY AUTOINCREMENT,
    config_version_id INTEGER NOT NULL,
    system_tier_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    tier_rank INTEGER NOT NULL CHECK(tier_rank BETWEEN 1 AND 5),
    min_progression_points INTEGER,
    require_all_active_challenges INTEGER NOT NULL DEFAULT 0 CHECK(require_all_active_challenges IN (0,1)),
    one_time_rank_bonus INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(config_version_id,system_tier_key),
    UNIQUE(config_version_id,tier_rank),
    FOREIGN KEY(config_version_id) REFERENCES challenge_config_versions(config_version_id)
);

CREATE TABLE IF NOT EXISTS challenge_submissions (
    submission_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_system TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    legacy_regular_submission_id INTEGER NOT NULL,
    source_external_id TEXT,
    source_snapshot_hash TEXT NOT NULL,
    ingest_fingerprint TEXT NOT NULL UNIQUE,
    supersedes_submission_id INTEGER,
    config_version_id INTEGER NOT NULL,
    boss_key TEXT,
    boss_display_name TEXT,
    earned_tier_key TEXT,
    earned_tier_rank INTEGER,
    source_submission_points INTEGER,
    metric_type TEXT,
    metric_value INTEGER,
    metric_unit TEXT,
    metric_display TEXT,
    party_key TEXT,
    party_display TEXT,
    submitter_discord_id TEXT,
    approver_discord_id TEXT,
    approver_display TEXT,
    evidence_url TEXT,
    raw_notes TEXT,
    source_submitted_at TEXT,
    source_approved_at TEXT,
    first_observed_at TEXT NOT NULL,
    raw_payload_json TEXT NOT NULL,
    raw_payload_sha256 TEXT NOT NULL,
    record_state TEXT NOT NULL DEFAULT 'approved' CHECK(record_state IN ('approved','corrected','retracted')),
    parse_status TEXT NOT NULL CHECK(parse_status IN ('parsed','partial','failed')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_system,source_record_id,source_snapshot_hash),
    FOREIGN KEY(supersedes_submission_id) REFERENCES challenge_submissions(submission_id),
    FOREIGN KEY(config_version_id) REFERENCES challenge_config_versions(config_version_id),
    FOREIGN KEY(boss_key) REFERENCES challenge_bosses(boss_key)
);
CREATE INDEX IF NOT EXISTS idx_challenge_submissions_boss ON challenge_submissions(boss_key,earned_tier_rank);
CREATE INDEX IF NOT EXISTS idx_challenge_submissions_party ON challenge_submissions(party_key);
CREATE INDEX IF NOT EXISTS idx_challenge_submissions_legacy ON challenge_submissions(legacy_regular_submission_id);

CREATE TABLE IF NOT EXISTS challenge_submission_participants (
    participant_id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id INTEGER NOT NULL,
    subject_key TEXT NOT NULL,
    member_id INTEGER,
    discord_id TEXT,
    rsn_snapshot TEXT,
    normalized_rsn_snapshot TEXT,
    participant_role TEXT NOT NULL CHECK(participant_role IN ('submitter','party_member')),
    identity_resolution_method TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(submission_id,subject_key),
    FOREIGN KEY(submission_id) REFERENCES challenge_submissions(submission_id)
);
CREATE INDEX IF NOT EXISTS idx_challenge_participants_subject ON challenge_submission_participants(subject_key);

CREATE TABLE IF NOT EXISTS challenge_member_tier_achievements (
    achievement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_key TEXT NOT NULL,
    member_id INTEGER,
    boss_key TEXT NOT NULL,
    tier_key TEXT NOT NULL,
    tier_rank INTEGER NOT NULL,
    first_qualifying_submission_id INTEGER NOT NULL,
    config_version_id INTEGER NOT NULL,
    earned_at TEXT,
    achievement_basis TEXT NOT NULL CHECK(achievement_basis IN ('explicit_submission','inferred_from_higher_tier')),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(subject_key,boss_key,tier_key),
    FOREIGN KEY(first_qualifying_submission_id) REFERENCES challenge_submissions(submission_id),
    FOREIGN KEY(config_version_id) REFERENCES challenge_config_versions(config_version_id),
    FOREIGN KEY(boss_key) REFERENCES challenge_bosses(boss_key)
);

CREATE TABLE IF NOT EXISTS challenge_member_bests (
    subject_key TEXT NOT NULL,
    member_id INTEGER,
    boss_key TEXT NOT NULL,
    metric_type TEXT NOT NULL,
    metric_value INTEGER NOT NULL,
    metric_unit TEXT NOT NULL,
    metric_display TEXT,
    submission_id INTEGER NOT NULL,
    evaluated_config_version_id INTEGER NOT NULL,
    calculated_at TEXT NOT NULL,
    PRIMARY KEY(subject_key,boss_key,metric_type),
    FOREIGN KEY(submission_id) REFERENCES challenge_submissions(submission_id),
    FOREIGN KEY(evaluated_config_version_id) REFERENCES challenge_config_versions(config_version_id)
);

CREATE TABLE IF NOT EXISTS challenge_member_progress (
    subject_key TEXT NOT NULL,
    member_id INTEGER,
    config_version_id INTEGER NOT NULL,
    challenge_progression_points INTEGER NOT NULL,
    active_challenges_completed INTEGER NOT NULL,
    active_challenge_count INTEGER NOT NULL,
    current_system_tier_key TEXT,
    current_system_tier_rank INTEGER NOT NULL DEFAULT 0,
    bronze_complete INTEGER NOT NULL DEFAULT 0,
    silver_complete INTEGER NOT NULL DEFAULT 0,
    gold_complete INTEGER NOT NULL DEFAULT 0,
    platinum_complete INTEGER NOT NULL DEFAULT 0,
    ascendant_eligible INTEGER NOT NULL DEFAULT 0,
    source_watermark INTEGER NOT NULL,
    calculation_hash TEXT NOT NULL,
    calculated_at TEXT NOT NULL,
    PRIMARY KEY(subject_key,config_version_id),
    FOREIGN KEY(config_version_id) REFERENCES challenge_config_versions(config_version_id)
);

CREATE TABLE IF NOT EXISTS challenge_tier_awards (
    award_id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_key TEXT NOT NULL,
    member_id INTEGER,
    system_tier_key TEXT NOT NULL,
    eligibility_config_version_id INTEGER NOT NULL,
    award_state TEXT NOT NULL CHECK(award_state IN ('grandfathered','shadow_eligible','suppressed','queued','delivered','failed')),
    would_award_points INTEGER NOT NULL DEFAULT 0,
    awarded_points INTEGER NOT NULL DEFAULT 0,
    legacy_regular_submission_id INTEGER UNIQUE,
    idempotency_key TEXT NOT NULL UNIQUE,
    historical_source TEXT,
    historical_timestamp TEXT,
    eligible_at TEXT,
    delivered_by_midgard_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(subject_key,system_tier_key),
    FOREIGN KEY(eligibility_config_version_id) REFERENCES challenge_config_versions(config_version_id)
);

CREATE TABLE IF NOT EXISTS challenge_ingest_runs (
    ingest_run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK(status IN ('running','complete','failed')),
    source_rows_observed INTEGER NOT NULL DEFAULT 0,
    submissions_inserted INTEGER NOT NULL DEFAULT 0,
    participants_inserted INTEGER NOT NULL DEFAULT 0,
    unresolved_identities INTEGER NOT NULL DEFAULT 0,
    parse_failures INTEGER NOT NULL DEFAULT 0,
    duplicate_fingerprints_blocked INTEGER NOT NULL DEFAULT 0,
    source_watermark INTEGER NOT NULL DEFAULT 0,
    error_text TEXT
);

CREATE TABLE IF NOT EXISTS challenge_ingest_issues (
    issue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_system TEXT NOT NULL,
    source_record_id TEXT NOT NULL,
    source_snapshot_hash TEXT NOT NULL,
    issue_class TEXT NOT NULL,
    issue_detail TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_system,source_record_id,source_snapshot_hash,issue_class)
);

CREATE TABLE IF NOT EXISTS challenge_metric_parse_corrections (
    correction_id INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id INTEGER NOT NULL UNIQUE,
    parser_version TEXT NOT NULL,
    original_token_sha256 TEXT NOT NULL,
    corrected_metric_type TEXT,
    corrected_metric_value INTEGER,
    corrected_metric_unit TEXT,
    corrected_metric_display TEXT,
    correction_state TEXT NOT NULL CHECK(correction_state IN ('applied','quarantined')),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(submission_id) REFERENCES challenge_submissions(submission_id)
);

CREATE TABLE IF NOT EXISTS challenge_audit_log (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    actor_type TEXT NOT NULL,
    actor_id TEXT,
    entity_type TEXT NOT NULL,
    entity_id TEXT,
    correlation_id TEXT,
    reason TEXT,
    event_payload_json TEXT NOT NULL,
    event_payload_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TRIGGER IF NOT EXISTS challenge_submissions_no_update
BEFORE UPDATE ON challenge_submissions BEGIN SELECT RAISE(ABORT,'challenge_submissions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS challenge_submissions_no_delete
BEFORE DELETE ON challenge_submissions BEGIN SELECT RAISE(ABORT,'challenge_submissions is append-only'); END;
CREATE TRIGGER IF NOT EXISTS challenge_participants_no_update
BEFORE UPDATE ON challenge_submission_participants BEGIN SELECT RAISE(ABORT,'challenge_submission_participants is append-only'); END;
CREATE TRIGGER IF NOT EXISTS challenge_participants_no_delete
BEFORE DELETE ON challenge_submission_participants BEGIN SELECT RAISE(ABORT,'challenge_submission_participants is append-only'); END;
CREATE TRIGGER IF NOT EXISTS challenge_achievements_no_update
BEFORE UPDATE ON challenge_member_tier_achievements BEGIN SELECT RAISE(ABORT,'challenge_member_tier_achievements is append-only'); END;
CREATE TRIGGER IF NOT EXISTS challenge_achievements_no_delete
BEFORE DELETE ON challenge_member_tier_achievements BEGIN SELECT RAISE(ABORT,'challenge_member_tier_achievements is append-only'); END;
CREATE TRIGGER IF NOT EXISTS challenge_audit_no_update
BEFORE UPDATE ON challenge_audit_log BEGIN SELECT RAISE(ABORT,'challenge_audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS challenge_audit_no_delete
BEFORE DELETE ON challenge_audit_log BEGIN SELECT RAISE(ABORT,'challenge_audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS challenge_metric_corrections_no_update
BEFORE UPDATE ON challenge_metric_parse_corrections BEGIN SELECT RAISE(ABORT,'challenge_metric_parse_corrections is append-only'); END;
CREATE TRIGGER IF NOT EXISTS challenge_metric_corrections_no_delete
BEFORE DELETE ON challenge_metric_parse_corrections BEGIN SELECT RAISE(ABORT,'challenge_metric_parse_corrections is append-only'); END;

CREATE TRIGGER IF NOT EXISTS challenge_awards_shadow_insert_guard
BEFORE INSERT ON challenge_tier_awards
WHEN NEW.award_state IN ('queued','delivered')
 AND COALESCE((SELECT setting_value FROM challenge_settings WHERE setting_key='award_mode'),'shadow') <> 'live'
BEGIN SELECT RAISE(ABORT,'award delivery forbidden while award_mode is not live'); END;

CREATE TRIGGER IF NOT EXISTS challenge_awards_shadow_update_guard
BEFORE UPDATE OF award_state ON challenge_tier_awards
WHEN NEW.award_state IN ('queued','delivered')
 AND COALESCE((SELECT setting_value FROM challenge_settings WHERE setting_key='award_mode'),'shadow') <> 'live'
BEGIN SELECT RAISE(ABORT,'award delivery forbidden while award_mode is not live'); END;
"""


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _add_boss_columns(conn: sqlite3.Connection) -> None:
    wanted = {
        "comparison_direction": "TEXT NOT NULL DEFAULT 'lower'",
        "supports_groups": "INTEGER NOT NULL DEFAULT 1",
        "archived_at": "TEXT",
        "created_at": "TEXT",
        "updated_at": "TEXT",
    }
    existing = _column_names(conn, "challenge_bosses")
    for column, declaration in wanted.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE challenge_bosses ADD COLUMN {column} {declaration}")


def config_payload() -> dict[str, Any]:
    return {
        "version_key": CONFIG_VERSION_KEY,
        "bosses": list(BOSS_CONFIG),
        "tiers": list(TIERS),
        "system_tiers": list(SYSTEM_TIERS),
        "notes": "Current live Google challenge rules. Times normalized to integer milliseconds; Delve to integer waves.",
    }


def migrate_schema(conn: sqlite3.Connection) -> int:
    conn.execute("BEGIN IMMEDIATE")
    try:
        if _table_exists(conn, "challenge_submissions") and "ingest_fingerprint" not in _column_names(conn, "challenge_submissions"):
            row_count = int(conn.execute("SELECT COUNT(*) FROM challenge_submissions").fetchone()[0])
            if row_count != 0:
                raise RuntimeError("Legacy challenge_submissions is unexpectedly non-empty")
            if not _table_exists(conn, "challenge_submissions_phase1_legacy"):
                conn.execute("ALTER TABLE challenge_submissions RENAME TO challenge_submissions_phase1_legacy")
            else:
                conn.execute("DROP TABLE challenge_submissions")

        _add_boss_columns(conn)
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            """INSERT INTO challenge_settings(setting_key,setting_value,updated_by)
               VALUES('award_mode','shadow','phase2_migration')
               ON CONFLICT(setting_key) DO UPDATE SET setting_value='shadow',updated_at=CURRENT_TIMESTAMP,updated_by='phase2_migration'"""
        )

        payload = config_payload()
        payload_json = canonical_json(payload)
        payload_hash = sha256_text(payload_json)
        conn.execute(
            """INSERT INTO challenge_config_versions
               (version_key,source_name,source_snapshot_sha256,config_json,status,effective_from)
               VALUES(?,?,?,?, 'active', ?)
               ON CONFLICT(version_key) DO NOTHING""",
            (CONFIG_VERSION_KEY, CONFIG_SOURCE, payload_hash, payload_json, "2026-09-28"),
        )
        config_id = int(conn.execute("SELECT config_version_id FROM challenge_config_versions WHERE version_key=?", (CONFIG_VERSION_KEY,)).fetchone()[0])

        for sort_order, boss in enumerate(BOSS_CONFIG, start=1):
            conn.execute(
                """INSERT INTO challenge_bosses
                   (boss_key,display_name,metric_type,sort_order,is_active,comparison_direction,supports_groups,created_at,updated_at)
                   VALUES(?,?,?,?,1,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
                   ON CONFLICT(boss_key) DO UPDATE SET
                     display_name=excluded.display_name,metric_type=excluded.metric_type,
                     sort_order=excluded.sort_order,is_active=1,
                     comparison_direction=excluded.comparison_direction,
                     supports_groups=excluded.supports_groups,updated_at=CURRENT_TIMESTAMP""",
                (boss["key"], boss["name"], boss["metric"], sort_order * 10, boss["direction"], 1),
            )
            for tier, requirement in zip(TIERS, boss["requirements"]):
                tier_key, display, rank, source_points, cumulative = tier
                if rank == 1:
                    metric_type, operator, value, unit = "completion", "complete", 1, "boolean"
                elif boss["metric"] == "time":
                    metric_type, operator, value, unit = "time", "lte", time_ms(requirement), "milliseconds"
                else:
                    metric_type, operator, value, unit = "numeric", "gte", int(requirement), "waves"
                conn.execute(
                    """INSERT INTO challenge_tiers
                       (config_version_id,boss_key,tier_key,display_name,tier_rank,
                        source_submission_points,cumulative_progression_points,
                        requirement_metric_type,requirement_operator,requirement_value,
                        requirement_unit,requirement_display)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(config_version_id,boss_key,tier_key) DO NOTHING""",
                    (config_id,boss["key"],tier_key,display,rank,source_points,cumulative,metric_type,operator,value,unit,requirement),
                )

        for key, display, rank, min_points, all_active, bonus in SYSTEM_TIERS:
            conn.execute(
                """INSERT INTO challenge_system_tiers
                   (config_version_id,system_tier_key,display_name,tier_rank,min_progression_points,
                    require_all_active_challenges,one_time_rank_bonus)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(config_version_id,system_tier_key) DO NOTHING""",
                (config_id,key,display,rank,min_points,all_active,bonus),
            )

        audit_payload = {"config_version": CONFIG_VERSION_KEY, "award_mode": "shadow", "schema": "phase2"}
        audit_json = canonical_json(audit_payload)
        if not conn.execute("SELECT 1 FROM challenge_audit_log WHERE event_type='phase2_schema_migrated'").fetchone():
            conn.execute(
                """INSERT INTO challenge_audit_log
                   (event_type,actor_type,actor_id,entity_type,entity_id,reason,event_payload_json,event_payload_sha256)
                   VALUES('phase2_schema_migrated','service','challenge_shadow_migration','database','Challenges.db',?,?,?)""",
                ("Phase 2 additive shadow schema migration", audit_json, sha256_text(audit_json)),
            )
        conn.commit()
        return config_id
    except Exception:
        conn.rollback()
        raise


@dataclass(frozen=True)
class ParsedChallenge:
    boss_key: str | None
    boss_display_name: str | None
    tier_key: str | None
    tier_rank: int | None
    source_points: int | None
    metric_type: str | None
    metric_value: int | None
    metric_unit: str | None
    metric_display: str | None
    party_key: str | None
    approver_discord_id: str | None
    approver_display: str | None
    parse_status: str
    issues: tuple[tuple[str, str], ...]


def parse_time_text(value: str) -> int:
    return time_ms(value)


def apply_phase3a_parse_corrections(conn: sqlite3.Connection) -> dict[str, int]:
    """Record derived corrections for historical failures without touching source rows."""
    mode = conn.execute(
        "SELECT setting_value FROM challenge_settings WHERE setting_key='award_mode'"
    ).fetchone()
    if not mode or mode[0] != "shadow":
        raise RuntimeError("Phase 3A corrections require award_mode=shadow")

    conn.execute("BEGIN IMMEDIATE")
    try:
        # The additive object definitions are repeated here so the deployed
        # timer may continue using --no-migrate after this one-time migration.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS challenge_metric_parse_corrections (
                correction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                submission_id INTEGER NOT NULL UNIQUE,
                parser_version TEXT NOT NULL,
                original_token_sha256 TEXT NOT NULL,
                corrected_metric_type TEXT,
                corrected_metric_value INTEGER,
                corrected_metric_unit TEXT,
                corrected_metric_display TEXT,
                correction_state TEXT NOT NULL CHECK(correction_state IN ('applied','quarantined')),
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(submission_id) REFERENCES challenge_submissions(submission_id)
            )"""
        )
        conn.execute(
            """CREATE TRIGGER IF NOT EXISTS challenge_metric_corrections_no_update
               BEFORE UPDATE ON challenge_metric_parse_corrections
               BEGIN SELECT RAISE(ABORT,'challenge_metric_parse_corrections is append-only'); END"""
        )
        conn.execute(
            """CREATE TRIGGER IF NOT EXISTS challenge_metric_corrections_no_delete
               BEFORE DELETE ON challenge_metric_parse_corrections
               BEGIN SELECT RAISE(ABORT,'challenge_metric_parse_corrections is append-only'); END"""
        )
        pattern = re.compile(r"TIME\s+SUBMISSION\s*\|\s*([^|]+)", re.I)
        rows = conn.execute(
            """SELECT DISTINCT s.submission_id,s.raw_notes
                 FROM challenge_ingest_issues i
                 JOIN challenge_submissions s
                   ON s.source_record_id=i.source_record_id
                  AND s.source_snapshot_hash=i.source_snapshot_hash
                WHERE i.issue_class='PB_PARSE_FAILURE'
                ORDER BY s.submission_id"""
        ).fetchall()
        applied = quarantined = existing = 0
        for row in rows:
            if conn.execute(
                "SELECT 1 FROM challenge_metric_parse_corrections WHERE submission_id=?",
                (row["submission_id"],),
            ).fetchone():
                existing += 1
                continue
            match = pattern.search(str(row["raw_notes"] or ""))
            token = match.group(1).strip() if match else ""
            try:
                value = time_ms(token)
                if len(token.split(":")) != 4:
                    raise ValueError("Not an approved legacy H:MM:SS:CC format")
                state = "applied"
                reason = "Unambiguous historical H:MM:SS:CC centisecond format"
                applied += 1
            except Exception as exc:
                value = None
                state = "quarantined"
                reason = f"Ambiguous historical time retained for manual review: {exc}"
                quarantined += 1
            conn.execute(
                """INSERT INTO challenge_metric_parse_corrections
                   (submission_id,parser_version,original_token_sha256,corrected_metric_type,
                    corrected_metric_value,corrected_metric_unit,corrected_metric_display,
                    correction_state,reason)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    row["submission_id"], PARSER_VERSION, sha256_text(token),
                    "time" if state == "applied" else None, value,
                    "milliseconds" if state == "applied" else None,
                    token if state == "applied" else None, state, reason,
                ),
            )
        payload = {
            "parser_version": PARSER_VERSION,
            "historical_failures_observed": len(rows),
            "applied": applied,
            "quarantined": quarantined,
            "already_recorded": existing,
            "source_submissions_modified": 0,
        }
        payload_json = canonical_json(payload)
        conn.execute(
            """INSERT INTO challenge_audit_log
               (event_type,actor_type,actor_id,entity_type,entity_id,reason,
                event_payload_json,event_payload_sha256)
               VALUES('phase3a_parse_corrections','service','challenge_phase3a_migration',
                      'database','Challenges.db','Append-only historical PB parser corrections',?,?)""",
            (payload_json, sha256_text(payload_json)),
        )
        conn.commit()
        return payload
    except Exception:
        conn.rollback()
        raise


def parse_challenge(item_name: str, notes: str | None, catalog: dict[str, Any] | None = None) -> ParsedChallenge:
    issues: list[tuple[str, str]] = []
    raw_item = str(item_name or "").strip()
    if " - " not in raw_item:
        return ParsedChallenge(None,None,None,None,None,None,None,None,None,None,None,None,"failed",(("UNKNOWN_CHALLENGE_FORMAT","Drop does not contain boss and tier"),))
    boss_text, tier_text = raw_item.rsplit(" - ", 1)
    boss_norm = re.sub(r"[^a-z0-9]+", "", boss_text.lower())
    if catalog is None:
        boss_key = BOSS_KEY_BY_NORMALIZED_NAME.get(boss_norm)
        tier = TIER_BY_NAME.get(tier_text.strip().lower())
        boss_display = BOSS_BY_KEY[boss_key]["name"] if boss_key else boss_text
        boss_metric = BOSS_BY_KEY[boss_key]["metric"] if boss_key else None
    else:
        boss_key = catalog["aliases"].get(boss_norm)
        boss = catalog["bosses"].get(boss_key) if boss_key else None
        tier_row = boss["tiers_by_name"].get(tier_text.strip().lower()) if boss else None
        tier = None if tier_row is None else (
            tier_row["tier_key"],tier_row["tier"],int(tier_row["rank"]),
            int(tier_row["points"]),int(tier_row["progression_points"]),
        )
        boss_display = boss["display_name"] if boss else boss_text
        boss_metric = boss["metric_type"] if boss else None
    if not boss_key:
        issues.append(("UNKNOWN_BOSS", boss_text))
    if not tier:
        issues.append(("UNKNOWN_TIER", tier_text))
    if not boss_key or not tier:
        return ParsedChallenge(boss_key,boss_text,tier[0] if tier else None,tier[2] if tier else None,tier[3] if tier else None,None,None,None,None,None,None,None,"failed",tuple(issues))

    tier_key, _display, tier_rank, source_points, _cumulative = tier
    note = str(notes or "").strip()
    party_match = re.search(r"\bparty_key\s+([^|\s]+)", note, re.I)
    approver_match = re.search(r"\bapproved\s+by\s+([^|]+)", note, re.I)
    party_key = party_match.group(1).strip() if party_match else None
    approver = approver_match.group(1).strip() if approver_match else None
    approver_discord = approver if approver and approver.isdigit() else None
    approver_display = None if approver_discord else approver

    metric_type = "completion" if tier_rank == 1 else boss_metric
    metric_value: int | None = 1 if tier_rank == 1 else None
    metric_unit = "boolean" if tier_rank == 1 else ("milliseconds" if metric_type == "time" else "waves")
    metric_display = "Completion" if tier_rank == 1 else None

    metric_match = re.search(r"TIME\s+SUBMISSION\s*\|\s*([^|]+)", note, re.I)
    if metric_match:
        candidate = metric_match.group(1).strip()
        try:
            if metric_type == "time":
                metric_value = parse_time_text(candidate)
            elif metric_type == "numeric":
                number = re.search(r"\d+", candidate)
                if not number:
                    raise ValueError("No integer wave found")
                metric_value = int(number.group(0))
            metric_display = candidate
        except Exception as exc:
            issues.append(("PB_PARSE_FAILURE", str(exc)))
    elif tier_rank > 1:
        issues.append(("LEGACY_NO_EVIDENCE", "No parseable metric in source Notes"))

    parse_status = "parsed" if not issues else ("failed" if any(k in {"UNKNOWN_BOSS","UNKNOWN_TIER","UNKNOWN_CHALLENGE_FORMAT"} for k,_ in issues) else "partial")
    return ParsedChallenge(boss_key,boss_display,tier_key,tier_rank,source_points,metric_type,metric_value,metric_unit,metric_display,party_key,approver_discord,approver_display,parse_status,tuple(issues))
