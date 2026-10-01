#!/usr/bin/env python3
"""Direct Discord challenge shadow intake and Google reconciliation.

The module has no Google, Discord, HTTP-client, or rank-point writer. Members.db
is accepted only as a read-only connection. Challenges.db is the sole write
target.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from challenge_shadow_common import (
    canonical_json,
    normalize_rsn,
    sha256_text,
    time_ms,
    utc_now,
)
from challenge_config import config_document as authoritative_config_document, resolve_boss


DIRECT_SOURCE_SYSTEM = "discord_direct"
DIRECT_PROVIDER = "discord"
MAX_PARTY_MEMBERS = 10
DISCORD_ID_RE = re.compile(r"^[0-9]{10,25}$")
EVENT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
RSN_RE = re.compile(r"^[A-Za-z0-9 _-]{1,12}$")

RECON_STATES = {
    "DIRECT_WAITING_FOR_GOOGLE",
    "MATCH",
    "GOOGLE_VALUE_DIFFERENCE",
    "IDENTITY_DIFFERENCE",
    "TIER_DIFFERENCE",
    "PARTY_DIFFERENCE",
    "DUPLICATE_DIRECT",
    "GOOGLE_ONLY_NEW",
}

PHASE3B_SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS challenge_direct_intake_metadata (
    submission_id INTEGER PRIMARY KEY,
    provider TEXT NOT NULL,
    provider_event_id TEXT NOT NULL,
    discord_guild_id TEXT,
    discord_channel_id TEXT,
    discord_message_id TEXT,
    declared_tier_key TEXT NOT NULL,
    raw_metric_text TEXT,
    producer_normalized_metric_json TEXT,
    canonical_payload_sha256 TEXT NOT NULL,
    content_fingerprint TEXT NOT NULL,
    party_payload_sha256 TEXT NOT NULL,
    approval_timestamp TEXT NOT NULL,
    google_source_watermark_at_intake INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(provider,provider_event_id),
    UNIQUE(content_fingerprint),
    FOREIGN KEY(submission_id) REFERENCES challenge_submissions(submission_id)
);

CREATE TABLE IF NOT EXISTS challenge_intake_request_audit (
    request_audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN (
        'accepted','idempotent','duplicate_direct','auth_failure',
        'validation_failure','rate_limited','conflict','internal_error'
    )),
    http_status INTEGER NOT NULL,
    request_body_sha256 TEXT,
    provider_event_id_sha256 TEXT,
    remote_address_sha256 TEXT,
    detail_code TEXT NOT NULL,
    submission_id INTEGER,
    body_size INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(submission_id) REFERENCES challenge_submissions(submission_id)
);
CREATE INDEX IF NOT EXISTS idx_challenge_intake_audit_outcome
ON challenge_intake_request_audit(outcome,occurred_at);

CREATE TABLE IF NOT EXISTS challenge_observation_reconciliation (
    reconciliation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    direct_submission_id INTEGER UNIQUE,
    google_submission_id INTEGER UNIQUE,
    reconciliation_state TEXT NOT NULL CHECK(reconciliation_state IN (
        'DIRECT_WAITING_FOR_GOOGLE','MATCH','GOOGLE_VALUE_DIFFERENCE',
        'IDENTITY_DIFFERENCE','TIER_DIFFERENCE','PARTY_DIFFERENCE',
        'DUPLICATE_DIRECT','GOOGLE_ONLY_NEW'
    )),
    match_method TEXT,
    first_seen_at TEXT NOT NULL,
    last_checked_at TEXT NOT NULL,
    reconciled_at TEXT,
    details_json TEXT NOT NULL,
    details_sha256 TEXT NOT NULL,
    CHECK(direct_submission_id IS NOT NULL OR google_submission_id IS NOT NULL),
    FOREIGN KEY(direct_submission_id) REFERENCES challenge_submissions(submission_id),
    FOREIGN KEY(google_submission_id) REFERENCES challenge_submissions(submission_id)
);

CREATE TABLE IF NOT EXISTS challenge_observation_reconciliation_history (
    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    reconciliation_id INTEGER,
    direct_submission_id INTEGER,
    google_submission_id INTEGER,
    reconciliation_state TEXT NOT NULL,
    match_method TEXT,
    details_json TEXT NOT NULL,
    details_sha256 TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY(reconciliation_id) REFERENCES challenge_observation_reconciliation(reconciliation_id)
);

CREATE TABLE IF NOT EXISTS challenge_observation_reconciliation_links (
    link_id INTEGER PRIMARY KEY AUTOINCREMENT,
    direct_submission_id INTEGER NOT NULL,
    google_submission_id INTEGER NOT NULL UNIQUE,
    participant_subject_key TEXT,
    match_class TEXT NOT NULL CHECK(match_class IN (
        'MATCH','GOOGLE_VALUE_DIFFERENCE','IDENTITY_DIFFERENCE',
        'TIER_DIFFERENCE','PARTY_DIFFERENCE','SUPERSEDED'
    )),
    match_method TEXT NOT NULL,
    details_json TEXT NOT NULL,
    details_sha256 TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_checked_at TEXT NOT NULL,
    reconciled_at TEXT,
    UNIQUE(direct_submission_id,google_submission_id),
    FOREIGN KEY(direct_submission_id) REFERENCES challenge_submissions(submission_id),
    FOREIGN KEY(google_submission_id) REFERENCES challenge_submissions(submission_id)
);
CREATE INDEX IF NOT EXISTS idx_challenge_reconciliation_links_direct
ON challenge_observation_reconciliation_links(direct_submission_id,match_class);

CREATE TABLE IF NOT EXISTS challenge_award_reviews (
    award_review_id INTEGER PRIMARY KEY AUTOINCREMENT,
    award_id INTEGER NOT NULL UNIQUE,
    review_state TEXT NOT NULL DEFAULT 'manual_review_required'
      CHECK(review_state IN ('manual_review_required','approved','suppressed')),
    reason TEXT NOT NULL,
    reviewed_by TEXT,
    reviewed_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(award_id) REFERENCES challenge_tier_awards(award_id)
);

CREATE TABLE IF NOT EXISTS challenge_legacy_baselines (
    baseline_id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_key TEXT NOT NULL,
    subject_key TEXT NOT NULL,
    member_id INTEGER,
    boss_key TEXT NOT NULL,
    highest_tier_key TEXT,
    highest_tier_rank INTEGER NOT NULL DEFAULT 0,
    progression_points INTEGER NOT NULL DEFAULT 0,
    metric_type TEXT,
    metric_value INTEGER,
    metric_unit TEXT,
    metric_display TEXT,
    source_system TEXT NOT NULL DEFAULT 'google_legacy_baseline',
    source_provenance_json TEXT NOT NULL,
    source_provenance_sha256 TEXT NOT NULL,
    config_version_id INTEGER NOT NULL,
    snapshot_timestamp TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(snapshot_key,subject_key,boss_key),
    FOREIGN KEY(config_version_id) REFERENCES challenge_config_versions(config_version_id),
    FOREIGN KEY(boss_key) REFERENCES challenge_bosses(boss_key)
);

CREATE TRIGGER IF NOT EXISTS challenge_direct_metadata_no_update
BEFORE UPDATE ON challenge_direct_intake_metadata
BEGIN SELECT RAISE(ABORT,'challenge_direct_intake_metadata is append-only'); END;
CREATE TRIGGER IF NOT EXISTS challenge_direct_metadata_no_delete
BEFORE DELETE ON challenge_direct_intake_metadata
BEGIN SELECT RAISE(ABORT,'challenge_direct_intake_metadata is append-only'); END;
CREATE TRIGGER IF NOT EXISTS challenge_intake_audit_no_update
BEFORE UPDATE ON challenge_intake_request_audit
BEGIN SELECT RAISE(ABORT,'challenge_intake_request_audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS challenge_intake_audit_no_delete
BEFORE DELETE ON challenge_intake_request_audit
BEGIN SELECT RAISE(ABORT,'challenge_intake_request_audit is append-only'); END;
CREATE TRIGGER IF NOT EXISTS challenge_reconciliation_history_no_update
BEFORE UPDATE ON challenge_observation_reconciliation_history
BEGIN SELECT RAISE(ABORT,'challenge_observation_reconciliation_history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS challenge_reconciliation_history_no_delete
BEFORE DELETE ON challenge_observation_reconciliation_history
BEGIN SELECT RAISE(ABORT,'challenge_observation_reconciliation_history is append-only'); END;
CREATE TRIGGER IF NOT EXISTS challenge_legacy_baselines_no_update
BEFORE UPDATE ON challenge_legacy_baselines
BEGIN SELECT RAISE(ABORT,'challenge_legacy_baselines is append-only'); END;
CREATE TRIGGER IF NOT EXISTS challenge_legacy_baselines_no_delete
BEFORE DELETE ON challenge_legacy_baselines
BEGIN SELECT RAISE(ABORT,'challenge_legacy_baselines is append-only'); END;

CREATE TRIGGER IF NOT EXISTS challenge_awards_manual_review_insert_guard
BEFORE INSERT ON challenge_tier_awards
WHEN NEW.award_state IN ('queued','delivered')
BEGIN SELECT RAISE(ABORT,'award delivery requires a prior approved manual review'); END;

CREATE TRIGGER IF NOT EXISTS challenge_awards_manual_review_update_guard
BEFORE UPDATE OF award_state ON challenge_tier_awards
WHEN NEW.award_state IN ('queued','delivered')
 AND COALESCE((SELECT review_state FROM challenge_award_reviews WHERE award_id=OLD.award_id),'manual_review_required') <> 'approved'
BEGIN SELECT RAISE(ABORT,'award delivery requires an approved manual review'); END;
"""


class IntakeValidationError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class NormalizedIntake:
    payload: dict[str, Any]
    payload_json: str
    payload_sha256: str
    content_fingerprint: str
    party_sha256: str
    provider: str
    event_id: str
    guild_id: str | None
    channel_id: str | None
    message_id: str | None
    submitter_discord_id: str
    submitted_rsn: str | None
    boss_key: str
    boss_display_name: str
    declared_tier_key: str
    declared_tier_rank: int
    earned_tier_key: str
    earned_tier_rank: int
    source_points: int
    metric_type: str
    metric_value: int
    metric_unit: str
    metric_display: str
    producer_normalized_metric_json: str | None
    party_members: tuple[dict[str, str | None], ...]
    approver_discord_id: str
    evidence_url: str | None
    approval_timestamp: str
    raw_notes: str | None
    config_version_id: int


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def migrate_phase3b_schema(conn: sqlite3.Connection) -> dict[str, int | str]:
    mode = conn.execute(
        "SELECT setting_value FROM challenge_settings WHERE setting_key='award_mode'"
    ).fetchone()
    if not mode or mode[0] != "shadow":
        raise RuntimeError("Phase 3B migration requires award_mode=shadow")
    try:
        conn.executescript("BEGIN IMMEDIATE;\n" + PHASE3B_SCHEMA_SQL)
        watermark = int(
            conn.execute(
                "SELECT COALESCE(MAX(legacy_regular_submission_id),0) FROM challenge_submissions WHERE source_system='regular_submissions_sheet_sync'"
            ).fetchone()[0]
        )
        settings = (
            ("phase3b_started_at", utc_now(), "phase3b_migration"),
            ("phase3b_google_source_watermark", str(watermark), "phase3b_migration"),
            ("direct_intake_mode", "shadow", "phase3b_migration"),
        )
        for key, value, actor in settings:
            conn.execute(
                """INSERT INTO challenge_settings(setting_key,setting_value,updated_by)
                   VALUES(?,?,?) ON CONFLICT(setting_key) DO NOTHING""",
                (key, value, actor),
            )
        conn.execute(
            """INSERT OR IGNORE INTO challenge_observation_reconciliation_links
               (direct_submission_id,google_submission_id,participant_subject_key,
                match_class,match_method,details_json,details_sha256,
                first_seen_at,last_checked_at,reconciled_at)
               SELECT r.direct_submission_id,r.google_submission_id,
                      (SELECT p.subject_key
                         FROM challenge_submission_participants p
                        WHERE p.submission_id=r.google_submission_id
                        ORDER BY CASE p.participant_role WHEN 'submitter' THEN 0 ELSE 1 END,
                                 p.participant_id LIMIT 1),
                      r.reconciliation_state,COALESCE(r.match_method,'legacy_summary'),
                      r.details_json,r.details_sha256,r.first_seen_at,r.last_checked_at,
                      r.reconciled_at
                 FROM challenge_observation_reconciliation r
                WHERE r.direct_submission_id IS NOT NULL
                  AND r.google_submission_id IS NOT NULL
                  AND r.reconciliation_state IN (
                      'MATCH','GOOGLE_VALUE_DIFFERENCE','IDENTITY_DIFFERENCE',
                      'TIER_DIFFERENCE','PARTY_DIFFERENCE'
                  )"""
        )
        review_count = ensure_award_reviews(conn)
        link_count = int(conn.execute(
            "SELECT COUNT(*) FROM challenge_observation_reconciliation_links"
        ).fetchone()[0])
        payload = {
            "schema": "phase3b",
            "award_mode": "shadow",
            "direct_intake_mode": "shadow",
            "google_watermark": watermark,
            "legacy_baseline_rows_created": 0,
            "manual_review_rows": review_count,
            "reconciliation_link_rows": link_count,
        }
        payload_json = canonical_json(payload)
        if not conn.execute(
            "SELECT 1 FROM challenge_audit_log WHERE event_type='phase3b_schema_migrated'"
        ).fetchone():
            conn.execute(
                """INSERT INTO challenge_audit_log
                   (event_type,actor_type,actor_id,entity_type,entity_id,reason,
                    event_payload_json,event_payload_sha256)
                   VALUES('phase3b_schema_migrated','service','challenge_phase3b_migration',
                          'database','Challenges.db','Additive direct shadow intake schema',?,?)""",
                (payload_json, sha256_text(payload_json)),
            )
        if not conn.execute(
            "SELECT 1 FROM challenge_audit_log WHERE event_type='phase3b_reconciliation_links_migrated'"
        ).fetchone():
            conn.execute(
                """INSERT INTO challenge_audit_log
                   (event_type,actor_type,actor_id,entity_type,entity_id,reason,
                    event_payload_json,event_payload_sha256)
                   VALUES('phase3b_reconciliation_links_migrated','service',
                          'challenge_phase3b_migration','database','Challenges.db',
                          'Additive one-to-many reconciliation link model',?,?)""",
                (payload_json, sha256_text(payload_json)),
            )
        conn.commit()
        return payload
    except Exception:
        conn.rollback()
        raise


def ensure_award_reviews(conn: sqlite3.Connection) -> int:
    conn.execute(
        """INSERT OR IGNORE INTO challenge_award_reviews(award_id,review_state,reason)
           SELECT award_id,'manual_review_required',
                  'Shadow eligibility is not authoritative award history; explicit adjudication required'
             FROM challenge_tier_awards WHERE award_state='shadow_eligible'"""
    )
    return int(
        conn.execute(
            """SELECT COUNT(*) FROM challenge_award_reviews r
               JOIN challenge_tier_awards a ON a.award_id=r.award_id
              WHERE a.award_state='shadow_eligible' AND r.review_state='manual_review_required'"""
        ).fetchone()[0]
    )


def record_request_audit(
    conn: sqlite3.Connection,
    *,
    outcome: str,
    http_status: int,
    detail_code: str,
    body_hash: str | None,
    event_id: str | None,
    remote_address: str | None,
    submission_id: int | None,
    body_size: int,
) -> None:
    if outcome not in {
        "accepted", "idempotent", "duplicate_direct", "auth_failure",
        "validation_failure", "rate_limited", "conflict", "internal_error",
    }:
        raise ValueError("invalid intake audit outcome")
    conn.execute(
        """INSERT INTO challenge_intake_request_audit
           (occurred_at,outcome,http_status,request_body_sha256,
            provider_event_id_sha256,remote_address_sha256,detail_code,
            submission_id,body_size)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            utc_now(), outcome, http_status, body_hash,
            sha256_text(event_id) if event_id else None,
            sha256_text(remote_address) if remote_address else None,
            detail_code[:120], submission_id, int(body_size or 0),
        ),
    )


def _required_string(payload: dict[str, Any], key: str, max_len: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise IntakeValidationError(f"missing_{key}", f"{key} is required")
    value = value.strip()
    if len(value) > max_len:
        raise IntakeValidationError(f"invalid_{key}", f"{key} is too long")
    return value


def _discord_id(value: Any, key: str, required: bool = False) -> str | None:
    if value is None or str(value).strip() == "":
        if required:
            raise IntakeValidationError(f"missing_{key}", f"{key} is required")
        return None
    text = str(value).strip()
    if not DISCORD_ID_RE.fullmatch(text):
        raise IntakeValidationError(f"invalid_{key}", f"{key} must be a Discord snowflake")
    return text


def _optional_rsn(payload: dict[str, Any], key: str = "submitted_rsn") -> str | None:
    value = payload.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise IntakeValidationError(f"invalid_{key}", f"{key} must be a string")
    value = value.strip()
    if not value:
        return None
    if len(value) > 12 or not RSN_RE.fullmatch(value):
        raise IntakeValidationError(f"invalid_{key}", f"{key} contains unsupported characters")
    return value


def _iso_timestamp(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IntakeValidationError("invalid_approval_timestamp", "approval_timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise IntakeValidationError("invalid_approval_timestamp", "approval_timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")


def _evidence_url(value: Any) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    if len(text) > 2048:
        raise IntakeValidationError("invalid_evidence_url", "evidence_url is too long")
    parsed = urlparse(text)
    if parsed.scheme != "https" or not parsed.netloc:
        raise IntakeValidationError("invalid_evidence_url", "evidence_url must be HTTPS")
    return text


def _parse_metric(
    metric_type: str, raw: Any, time_input_format: str | None = None
) -> tuple[int, str, str]:
    display = str(raw).strip() if raw is not None else ""
    if metric_type == "completion":
        if raw not in (True, 1, "1", "true", "True", "completion", "Completion", "complete", "Complete"):
            raise IntakeValidationError("invalid_completion_metric", "completion metric must be true/1/completion")
        return 1, "boolean", "Completion"
    if metric_type == "time":
        if not display:
            raise IntakeValidationError("missing_metric", "a time metric is required")
        try:
            if time_input_format == "MM:SS.xx":
                match = re.fullmatch(r"(\d{2}):([0-5]\d)\.(\d{2})", display)
                if not match:
                    raise ValueError("time must use MM:SS.xx")
                minutes, seconds, centiseconds = map(int, match.groups())
                return ((minutes * 60 + seconds) * 1000) + centiseconds * 10, "milliseconds", display
            if time_input_format == "HH:MM:SS.xx":
                match = re.fullmatch(r"(\d{2}):([0-5]\d):([0-5]\d)\.(\d{2})", display)
                if not match:
                    raise ValueError("time must use HH:MM:SS.xx")
                hours, minutes, seconds, centiseconds = map(int, match.groups())
                return ((hours * 3600 + minutes * 60 + seconds) * 1000) + centiseconds * 10, "milliseconds", display
            return time_ms(display), "milliseconds", display
        except Exception as exc:
            raise IntakeValidationError("invalid_time_metric", str(exc)) from exc
    if metric_type == "numeric":
        if isinstance(raw, bool):
            raise IntakeValidationError("invalid_numeric_metric", "numeric metric must be an integer")
        if isinstance(raw, int):
            value = raw
        elif isinstance(raw, str) and re.fullmatch(r"\s*\d+\s*(?:waves?)?\s*", raw, re.I):
            value = int(re.search(r"\d+", raw).group(0))
        else:
            raise IntakeValidationError("invalid_numeric_metric", "numeric metric must be an integer wave")
        if value < 0 or value > 100000:
            raise IntakeValidationError("invalid_numeric_metric", "numeric metric is out of range")
        return value, "waves", display or str(value)
    raise IntakeValidationError("invalid_metric_type", "unsupported metric type")


def normalize_and_evaluate(conn: sqlite3.Connection, payload: Any) -> NormalizedIntake:
    if not isinstance(payload, dict):
        raise IntakeValidationError("invalid_json_object", "JSON body must be an object", 400)
    allowed = {
        "provider", "event_id", "submission_id", "discord_guild_id",
        "discord_channel_id", "discord_message_id", "submitter_discord_id",
        "submitted_rsn", "boss", "boss_key", "config_version_id", "earned_tier", "raw_metric",
        "normalized_metric", "party_members", "approver_discord_id",
        "evidence_url", "approval_timestamp", "raw_notes", "context",
    }
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise IntakeValidationError("unknown_fields", "Unknown fields: " + ", ".join(unknown))

    provider = str(payload.get("provider") or DIRECT_PROVIDER).strip().lower()
    if provider != DIRECT_PROVIDER:
        raise IntakeValidationError("invalid_provider", "provider must be discord")
    event_id = str(payload.get("event_id") or payload.get("submission_id") or "").strip()
    if not EVENT_ID_RE.fullmatch(event_id):
        raise IntakeValidationError("invalid_event_id", "event_id/submission_id is required and must be stable")
    guild_id = _discord_id(payload.get("discord_guild_id"), "discord_guild_id")
    channel_id = _discord_id(payload.get("discord_channel_id"), "discord_channel_id")
    message_id = _discord_id(payload.get("discord_message_id"), "discord_message_id")
    submitter_id = _discord_id(payload.get("submitter_discord_id"), "submitter_discord_id", True)
    approver_id = _discord_id(payload.get("approver_discord_id"), "approver_discord_id", True)
    if submitter_id == approver_id:
        raise IntakeValidationError(
            "submitter_cannot_approve_own_submission",
            "approver_discord_id must identify a different user than submitter_discord_id",
        )
    submitted_rsn = _optional_rsn(payload)
    boss_text = _required_string(payload, "boss", 80)
    requested_version = payload.get("config_version_id")
    if requested_version is not None and (isinstance(requested_version,bool) or not str(requested_version).isdigit()):
        raise IntakeValidationError("invalid_config_version_id", "config_version_id must be an integer")
    if requested_version is None:
        version_row = conn.execute("SELECT config_version_id FROM challenge_config_versions WHERE status='active'").fetchone()
        config_id = int(version_row[0]) if version_row else 0
    else:
        config_id = int(requested_version)
        version_row = conn.execute("SELECT config_version_id FROM challenge_config_versions WHERE config_version_id=? AND status IN ('active','retired')",(config_id,)).fetchone()
        if not version_row:
            raise IntakeValidationError("unknown_config_version", "config_version_id is not a published version")
    supplied_boss_key = str(payload.get("boss_key") or "").strip()
    if supplied_boss_key:
        active = conn.execute("SELECT * FROM challenge_config_bosses WHERE config_version_id=? AND boss_key=?",(config_id,supplied_boss_key)).fetchone()
        text_identity = resolve_boss(conn,boss_text,config_id)
        if not text_identity or str(text_identity["boss_key"]) != supplied_boss_key:
            raise IntakeValidationError("boss_identity_mismatch", "boss and boss_key do not identify the same published boss")
    else:
        active = resolve_boss(conn,boss_text,config_id)
    # Backward compatibility for approval embeds created before config metadata:
    # resolve the display/alias in the newest published snapshot that knows it.
    if not active and requested_version is None:
        for row in conn.execute("SELECT config_version_id FROM challenge_config_versions WHERE status IN ('active','retired') ORDER BY config_version_id DESC"):
            candidate=resolve_boss(conn,boss_text,int(row[0]))
            if candidate:
                active=candidate; config_id=int(row[0]); break
    if not active:
        raise IntakeValidationError("unknown_boss", "boss is not present in the published challenge catalog")
    boss_key = str(active["boss_key"])
    if not int(active["is_active"]):
        raise IntakeValidationError("inactive_boss", "boss is not active")
    tiers = conn.execute(
        """SELECT * FROM challenge_tiers
            WHERE config_version_id=? AND boss_key=? ORDER BY tier_rank""",
        (config_id, boss_key),
    ).fetchall()
    declared_text = _required_string(payload, "earned_tier", 30).lower()
    declared = next((row for row in tiers if row["tier_key"] == declared_text or str(row["display_name"]).lower() == declared_text), None)
    if not declared:
        raise IntakeValidationError("unknown_tier", "earned_tier is not configured")

    metric_type = "completion" if int(declared["tier_rank"]) == 1 and payload.get("raw_metric") in (None, "") else str(active["metric_type"])
    raw_metric = payload.get("raw_metric")
    if metric_type == "completion" and raw_metric in (None, ""):
        raw_metric = True
    metric_value, metric_unit, metric_display = _parse_metric(
        metric_type, raw_metric, active["time_input_format"]
    )

    qualifying = []
    for tier in tiers:
        rank = int(tier["tier_rank"])
        if rank == 1:
            qualifies = True
        elif metric_type == "completion":
            qualifies = False
        elif tier["requirement_operator"] == "lte":
            qualifies = metric_value <= int(tier["requirement_value"])
        elif tier["requirement_operator"] == "gte":
            qualifies = metric_value >= int(tier["requirement_value"])
        else:
            qualifies = False
        if qualifies:
            qualifying.append(tier)
    earned = max(qualifying, key=lambda row: int(row["tier_rank"]))
    if int(earned["tier_rank"]) < int(declared["tier_rank"]):
        raise IntakeValidationError("metric_below_declared_tier", "submitted metric does not qualify for earned_tier")

    party_raw = payload.get("party_members") or []
    if not isinstance(party_raw, list) or len(party_raw) > MAX_PARTY_MEMBERS:
        raise IntakeValidationError("invalid_party_members", f"party_members must be a list of at most {MAX_PARTY_MEMBERS}")
    party = []
    for index, item in enumerate(party_raw):
        if not isinstance(item, dict) or set(item) - {"discord_id", "rsn"}:
            raise IntakeValidationError("invalid_party_member", f"party_members[{index}] is invalid")
        discord_id = _discord_id(item.get("discord_id"), f"party_members_{index}_discord_id")
        rsn = str(item.get("rsn") or "").strip() or None
        if rsn and not RSN_RE.fullmatch(rsn):
            raise IntakeValidationError("invalid_party_member_rsn", f"party_members[{index}].rsn is invalid")
        if not discord_id and not rsn:
            raise IntakeValidationError("invalid_party_member", f"party_members[{index}] needs discord_id or rsn")
        party.append({"discord_id": discord_id, "rsn": rsn})
    participant_count = 1 + len(party)
    if not int(active["supports_groups"]) and party:
        raise IntakeValidationError(
            "party_not_allowed", "this published Challenge configuration is solo-only"
        )
    if int(active["supports_groups"]) and int(active["min_party_size"]) >= 2:
        if participant_count < int(active["min_party_size"]):
            raise IntakeValidationError(
                "party_too_small",
                f"this Challenge requires at least {int(active['min_party_size'])} participants",
            )

    normalized_metric = payload.get("normalized_metric")
    if normalized_metric is not None and not isinstance(normalized_metric, dict):
        raise IntakeValidationError("invalid_normalized_metric", "normalized_metric must be an object")
    normalized_metric_json = canonical_json(normalized_metric) if normalized_metric is not None else None
    approval_timestamp = _iso_timestamp(_required_string(payload, "approval_timestamp", 64))
    evidence = _evidence_url(payload.get("evidence_url"))
    raw_notes_value = payload.get("raw_notes", payload.get("context"))
    raw_notes = None if raw_notes_value is None else str(raw_notes_value)
    if raw_notes is not None and len(raw_notes) > 8000:
        raise IntakeValidationError("raw_notes_too_long", "raw_notes/context exceeds 8000 characters")

    canonical_payload = dict(payload)
    canonical_payload["provider"] = provider
    canonical_payload["event_id"] = event_id
    canonical_payload.pop("submission_id", None)
    payload_json = canonical_json(canonical_payload)
    payload_hash = sha256_text(payload_json)
    party_json = canonical_json(party)
    content = {
        "provider": provider,
        "guild_id": guild_id,
        "channel_id": channel_id,
        "message_id": message_id,
        "submitter_discord_id": submitter_id,
        "submitted_rsn": normalize_rsn(submitted_rsn),
        "boss_key": boss_key,
        "declared_tier_key": declared["tier_key"],
        "metric_type": metric_type,
        "metric_value": metric_value,
        "party": party,
        "approver_discord_id": approver_id,
        "evidence_url": evidence,
        "approval_timestamp": approval_timestamp,
    }
    return NormalizedIntake(
        canonical_payload, payload_json, payload_hash, sha256_text(canonical_json(content)),
        sha256_text(party_json), provider, event_id, guild_id, channel_id, message_id,
        submitter_id, submitted_rsn, boss_key, str(active["display_name"]),
        str(declared["tier_key"]), int(declared["tier_rank"]),
        str(earned["tier_key"]), int(earned["tier_rank"]), int(earned["source_submission_points"]),
        metric_type, metric_value, metric_unit, metric_display, normalized_metric_json,
        tuple(party), approver_id, evidence, approval_timestamp, raw_notes, config_id,
    )


def resolve_direct_identity(
    discord_id: str | None, rsn: str | None, members: sqlite3.Connection
) -> tuple[int | None, str, str]:
    if discord_id:
        row = members.execute(
            "SELECT member_id FROM members WHERE CAST(discord_id AS TEXT)=? LIMIT 1",
            (discord_id,),
        ).fetchone()
        if row:
            member_id = int(row[0])
            return member_id, f"member:{member_id}", "members.discord_id"
    nrsn = normalize_rsn(rsn)
    if nrsn:
        for sql, method in (
            ("SELECT member_id FROM members WHERE normalized_rsn=? LIMIT 1", "members.normalized_rsn"),
            ("SELECT member_id FROM member_accounts WHERE normalized_rsn=? ORDER BY is_primary DESC,is_active DESC LIMIT 1", "member_accounts.normalized_rsn"),
            ("SELECT member_id FROM member_aliases WHERE normalized_alias_rsn=? LIMIT 1", "member_aliases.normalized_alias_rsn"),
        ):
            row = members.execute(sql, (nrsn,)).fetchone()
            if row:
                member_id = int(row[0])
                return member_id, f"member:{member_id}", method
    if discord_id:
        return None, f"discord:{discord_id}", "unresolved_discord"
    if nrsn:
        return None, f"rsn:{nrsn}", "unresolved_rsn"
    raise IntakeValidationError("participant_identity_missing", "participant identity is missing")


def resolve_submitter_identity(
    discord_id: str, rsn: str | None, members: sqlite3.Connection
) -> tuple[int | None, str, str, str | None]:
    """Resolve the required submitter Discord ID and preserve RSN provenance.

    Discord is authoritative when it resolves. A supplied RSN is an additional
    consistency signal, but it is never replaced with the canonical Members.db
    RSN in the source snapshot.
    """
    discord_row = members.execute(
        "SELECT member_id,rsn FROM members WHERE CAST(discord_id AS TEXT)=? LIMIT 1",
        (discord_id,),
    ).fetchone()
    if discord_row:
        member_id = int(discord_row[0])
        if rsn:
            rsn_member_id, _subject, _method = resolve_direct_identity(None, rsn, members)
            if rsn_member_id is not None and rsn_member_id != member_id:
                raise IntakeValidationError(
                    "submitter_identity_conflict",
                    "submitted_rsn resolves to a different member than submitter_discord_id",
                )
        return member_id, f"member:{member_id}", "members.discord_id", str(discord_row[1])

    member_id, subject_key, method = resolve_direct_identity(discord_id, rsn, members)
    if member_id is None and rsn is None:
        raise IntakeValidationError(
            "submitter_identity_unresolved",
            "submitter_discord_id is not present in Members.db and no submitted_rsn was supplied",
        )
    canonical_rsn = None
    if member_id is not None:
        row = members.execute("SELECT rsn FROM members WHERE member_id=?", (member_id,)).fetchone()
        canonical_rsn = str(row[0]) if row else None
    return member_id, subject_key, method, canonical_rsn


def insert_direct_observation(
    challenge: sqlite3.Connection,
    members: sqlite3.Connection,
    normalized: NormalizedIntake,
) -> tuple[int, str, int]:
    existing = challenge.execute(
        """SELECT m.submission_id,m.canonical_payload_sha256
             FROM challenge_direct_intake_metadata m
            WHERE m.provider=? AND m.provider_event_id=?""",
        (normalized.provider, normalized.event_id),
    ).fetchone()
    if existing:
        if existing["canonical_payload_sha256"] == normalized.payload_sha256:
            return int(existing["submission_id"]), "idempotent", 0
        raise IntakeValidationError("event_id_payload_conflict", "event_id already exists with a different payload", 409)
    duplicate = challenge.execute(
        "SELECT submission_id FROM challenge_direct_intake_metadata WHERE content_fingerprint=?",
        (normalized.content_fingerprint,),
    ).fetchone()
    if duplicate:
        return int(duplicate[0]), "duplicate_direct", 0

    member_id, subject_key, method, _canonical_rsn = resolve_submitter_identity(
        normalized.submitter_discord_id, normalized.submitted_rsn, members
    )
    watermark = int(
        challenge.execute(
            "SELECT COALESCE(MAX(legacy_regular_submission_id),0) FROM challenge_submissions WHERE source_system='regular_submissions_sheet_sync'"
        ).fetchone()[0]
    )
    source_record_id = f"discord_direct:{normalized.provider}:{normalized.event_id}"
    cur = challenge.execute(
        """INSERT INTO challenge_submissions
           (source_system,source_record_id,legacy_regular_submission_id,source_external_id,
            source_snapshot_hash,ingest_fingerprint,supersedes_submission_id,config_version_id,
            boss_key,boss_display_name,earned_tier_key,earned_tier_rank,source_submission_points,
            metric_type,metric_value,metric_unit,metric_display,party_key,party_display,
            submitter_discord_id,approver_discord_id,approver_display,evidence_url,raw_notes,
            source_submitted_at,source_approved_at,first_observed_at,raw_payload_json,
            raw_payload_sha256,record_state,parse_status)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            DIRECT_SOURCE_SYSTEM, source_record_id, 0, normalized.event_id,
            normalized.payload_sha256, normalized.content_fingerprint, None,
            normalized.config_version_id, normalized.boss_key, normalized.boss_display_name,
            normalized.earned_tier_key, normalized.earned_tier_rank, normalized.source_points,
            normalized.metric_type, normalized.metric_value, normalized.metric_unit,
            normalized.metric_display, normalized.message_id or normalized.event_id, None,
            normalized.submitter_discord_id, normalized.approver_discord_id, None,
            normalized.evidence_url, normalized.raw_notes, normalized.approval_timestamp,
            normalized.approval_timestamp, utc_now(), normalized.payload_json,
            normalized.payload_sha256, "approved", "parsed",
        ),
    )
    submission_id = int(cur.lastrowid)
    challenge.execute(
        """INSERT INTO challenge_direct_intake_metadata
           (submission_id,provider,provider_event_id,discord_guild_id,discord_channel_id,
            discord_message_id,declared_tier_key,raw_metric_text,
            producer_normalized_metric_json,canonical_payload_sha256,content_fingerprint,
            party_payload_sha256,approval_timestamp,google_source_watermark_at_intake)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            submission_id, normalized.provider, normalized.event_id, normalized.guild_id,
            normalized.channel_id, normalized.message_id, normalized.declared_tier_key,
            normalized.metric_display, normalized.producer_normalized_metric_json,
            normalized.payload_sha256, normalized.content_fingerprint, normalized.party_sha256,
            normalized.approval_timestamp, watermark,
        ),
    )
    challenge.execute(
        """INSERT INTO challenge_submission_participants
           (submission_id,subject_key,member_id,discord_id,rsn_snapshot,
            normalized_rsn_snapshot,participant_role,identity_resolution_method)
           VALUES(?,?,?,?,?,?,'submitter',?)""",
        (
            submission_id, subject_key, member_id, normalized.submitter_discord_id,
            normalized.submitted_rsn,
            normalize_rsn(normalized.submitted_rsn) if normalized.submitted_rsn else None,
            method,
        ),
    )
    unresolved = int(member_id is None)
    seen = {subject_key}
    for party in normalized.party_members:
        party_member_id, party_subject, party_method = resolve_direct_identity(
            party["discord_id"], party["rsn"], members
        )
        if party_subject in seen:
            continue
        seen.add(party_subject)
        challenge.execute(
            """INSERT INTO challenge_submission_participants
               (submission_id,subject_key,member_id,discord_id,rsn_snapshot,
                normalized_rsn_snapshot,participant_role,identity_resolution_method)
               VALUES(?,?,?,?,?,?,'party_member',?)""",
            (
                submission_id, party_subject, party_member_id, party["discord_id"],
                party["rsn"], normalize_rsn(party["rsn"]), party_method,
            ),
        )
        unresolved += int(party_member_id is None)
    if unresolved:
        challenge.execute(
            """INSERT OR IGNORE INTO challenge_ingest_issues
               (source_system,source_record_id,source_snapshot_hash,issue_class,issue_detail)
               VALUES(?,?,?,'IDENTITY_UNRESOLVED',?)""",
            (DIRECT_SOURCE_SYSTEM, source_record_id, normalized.payload_sha256, f"{unresolved} direct participant(s) unresolved"),
        )
    return submission_id, "accepted", unresolved


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def _primary_subject(conn: sqlite3.Connection, submission_id: int) -> str | None:
    row = conn.execute(
        """SELECT subject_key FROM challenge_submission_participants
            WHERE submission_id=? ORDER BY CASE participant_role WHEN 'submitter' THEN 0 ELSE 1 END,participant_id LIMIT 1""",
        (submission_id,),
    ).fetchone()
    return str(row[0]) if row else None


def _party_subjects(conn: sqlite3.Connection, submission_id: int) -> set[str]:
    return {str(row[0]) for row in conn.execute(
        "SELECT subject_key FROM challenge_submission_participants WHERE submission_id=?", (submission_id,)
    )}


def _display_time_ms(row: sqlite3.Row) -> int | None:
    display = str(row["metric_display"] or "").strip()
    if not display:
        return None
    try:
        return time_ms(display)
    except Exception:
        return None


def _metric_comparison(direct: sqlite3.Row, google: sqlite3.Row) -> dict[str, Any]:
    direct_type = str(direct["metric_type"] or "")
    google_type = str(google["metric_type"] or "")
    direct_unit = str(direct["metric_unit"] or "")
    google_unit = str(google["metric_unit"] or "")
    direct_value = direct["metric_value"]
    google_value = google["metric_value"]
    same_type_unit = direct_type == google_type and direct_unit == google_unit
    comparison = "unavailable"
    equal = False
    comparable = False
    normalization_reason = None

    if same_type_unit and direct_value is not None and google_value is not None:
        comparable = True
        equal = int(direct_value) == int(google_value)
        comparison = "exact" if equal else "value_difference"
    elif direct_value is not None and google_value is not None:
        bronze_pair = (
            int(direct["earned_tier_rank"] or 0) == 1
            and int(google["earned_tier_rank"] or 0) == 1
            and {direct_type, google_type} == {"time", "completion"}
        )
        direct_display_ms = _display_time_ms(direct)
        google_display_ms = _display_time_ms(google)
        time_value = int(direct_value) if direct_type == "time" else int(google_value)
        evidence_equal = bool(
            direct["evidence_url"] and google["evidence_url"]
            and direct["evidence_url"] == google["evidence_url"]
        )
        semantic_context_equal = (
            _primary_value(direct, "boss_key") == _primary_value(google, "boss_key")
            and int(direct["earned_tier_rank"] or 0) == int(google["earned_tier_rank"] or 0)
            and evidence_equal
        )
        if (
            bronze_pair and semantic_context_equal
            and direct_display_ms is not None and google_display_ms is not None
            and direct_display_ms == google_display_ms == time_value
        ):
            comparable = True
            equal = True
            comparison = "semantic_equivalent"
            normalization_reason = "bronze_time_vs_completion_same_evidence_and_display_time"
        else:
            comparison = "incompatible"

    return {
        "metric_comparison": comparison,
        "metric_comparable": comparable,
        "metric_equal": equal,
        "metric_type_equal": direct_type == google_type,
        "metric_unit_equal": direct_unit == google_unit,
        "metric_normalization_reason": normalization_reason,
    }


def _primary_value(row: sqlite3.Row, key: str) -> Any:
    return row[key]


def _candidate_google_group(conn: sqlite3.Connection, direct: sqlite3.Row) -> list[tuple[sqlite3.Row, str]]:
    direct_id = int(direct["submission_id"])
    direct_subjects = _party_subjects(conn, direct_id)
    approval = _parse_datetime(direct["approval_timestamp"])
    event_tokens = [str(direct["provider_event_id"])]
    if direct["discord_message_id"]:
        event_tokens.append(str(direct["discord_message_id"]))
    rows = conn.execute(
        """SELECT s.* FROM challenge_submissions s
            WHERE s.source_system='regular_submissions_sheet_sync'
              AND s.legacy_regular_submission_id>?
              AND NOT EXISTS (
                  SELECT 1 FROM challenge_submissions newer
                   WHERE newer.supersedes_submission_id=s.submission_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM challenge_observation_reconciliation_links l
                   WHERE l.google_submission_id=s.submission_id
                     AND l.direct_submission_id<>?
              )
              AND NOT EXISTS (
                  SELECT 1 FROM challenge_observation_reconciliation r
                   WHERE r.google_submission_id=s.submission_id
                     AND r.direct_submission_id IS NOT NULL
                     AND r.direct_submission_id<>?
              )
            ORDER BY s.legacy_regular_submission_id,s.submission_id""",
        (int(direct["google_source_watermark_at_intake"]), direct_id, direct_id),
    ).fetchall()
    scored_by_subject: dict[str, list[tuple[int, int, sqlite3.Row, str]]] = {}
    for row in rows:
        stable = any(
            token and (str(row["source_external_id"] or "") == token or token in str(row["raw_notes"] or ""))
            for token in event_tokens
        )
        google_subject = _primary_subject(conn, int(row["submission_id"]))
        same_participant = bool(google_subject and google_subject in direct_subjects)
        same_boss = row["boss_key"] == direct["boss_key"]
        same_tier = int(row["earned_tier_rank"] or 0) == int(direct["earned_tier_rank"] or 0)
        when = _parse_datetime(row["first_observed_at"])
        if when is None:
            when = _parse_datetime(row["source_approved_at"] or row["source_submitted_at"])
        delay_seconds = abs((when - approval).total_seconds()) if approval and when else None
        within_window = delay_seconds is not None and delay_seconds <= 21600
        tight_window = delay_seconds is not None and delay_seconds <= 1800
        evidence_match = bool(
            direct["evidence_url"] and row["evidence_url"]
            and direct["evidence_url"] == row["evidence_url"]
        )
        both_without_evidence = not direct["evidence_url"] and not row["evidence_url"]
        party_key_match = bool(
            direct["party_key"] and row["party_key"]
            and direct["party_key"] == row["party_key"]
        )
        metric = _metric_comparison(direct, row)
        metric_agrees = metric["metric_comparison"] in {"exact", "semantic_equivalent"}

        if not stable:
            if not (same_participant and same_boss and within_window):
                continue
            if direct["evidence_url"] or row["evidence_url"]:
                if not evidence_match:
                    continue
            elif not (
                both_without_evidence and same_tier and metric_agrees
                and (party_key_match or (len(direct_subjects) == 1 and tight_window))
            ):
                continue

        score = 1000 if stable else 0
        score += 400 if evidence_match else 0
        score += 200 if same_participant else 0
        score += 100 if same_boss else 0
        score += 80 if same_tier else 0
        score += 50 if metric_agrees else 0
        score += 30 if party_key_match else 0
        score += 20 if tight_window else (10 if within_window else 0)
        method = "stable_discord_id" if stable else (
            "evidence_participant_boss_tier_time" if evidence_match
            else "participant_boss_tier_metric_party_time"
        )
        subject_key = google_subject or f"google:{int(row['submission_id'])}"
        scored_by_subject.setdefault(subject_key, []).append(
            (score, int(row["submission_id"]), row, method)
        )

    selected: list[tuple[sqlite3.Row, str]] = []
    for candidates in scored_by_subject.values():
        candidates.sort(key=lambda item: (-item[0], item[1]))
        if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
            continue
        selected.append((candidates[0][2], candidates[0][3]))
    selected.sort(key=lambda item: int(item[0]["submission_id"]))
    return selected


def _classification(conn: sqlite3.Connection, direct: sqlite3.Row, google: sqlite3.Row) -> tuple[str, dict[str, Any]]:
    direct_subject = _primary_subject(conn, int(direct["submission_id"]))
    google_subject = _primary_subject(conn, int(google["submission_id"]))
    direct_party = _party_subjects(conn, int(direct["submission_id"]))
    google_party = _party_subjects(conn, int(google["submission_id"]))
    identity_equal = bool(google_subject and google_subject in direct_party)
    metric = _metric_comparison(direct, google)
    details = {
        "identity_equal": identity_equal,
        "direct_primary_subject_equal": direct_subject == google_subject,
        "google_subject_in_direct_participants": identity_equal,
        "boss_equal": direct["boss_key"] == google["boss_key"],
        "tier_equal": int(direct["earned_tier_rank"] or 0) == int(google["earned_tier_rank"] or 0),
        "party_comparable": len(direct_party) > 1 and len(google_party) > 1,
        "party_equal": direct_party == google_party,
        **metric,
    }
    if not identity_equal:
        return "IDENTITY_DIFFERENCE", details
    if int(direct["earned_tier_rank"] or 0) != int(google["earned_tier_rank"] or 0):
        return "TIER_DIFFERENCE", details
    if details["metric_comparison"] in {"value_difference", "incompatible"}:
        return "GOOGLE_VALUE_DIFFERENCE", details
    if details["party_comparable"] and not details["party_equal"]:
        return "PARTY_DIFFERENCE", details
    return "MATCH", details


def _upsert_reconciliation_link(
    conn: sqlite3.Connection,
    direct_id: int,
    google_id: int,
    state: str,
    method: str,
    details: dict[str, Any],
) -> None:
    now = utc_now()
    rendered = canonical_json(details)
    participant_subject = _primary_subject(conn, google_id)
    conn.execute(
        """INSERT INTO challenge_observation_reconciliation_links
           (direct_submission_id,google_submission_id,participant_subject_key,
            match_class,match_method,details_json,details_sha256,
            first_seen_at,last_checked_at,reconciled_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(direct_submission_id,google_submission_id) DO UPDATE SET
             participant_subject_key=excluded.participant_subject_key,
             match_class=excluded.match_class,
             match_method=excluded.match_method,
             details_json=excluded.details_json,
             details_sha256=excluded.details_sha256,
             last_checked_at=excluded.last_checked_at,
             reconciled_at=COALESCE(challenge_observation_reconciliation_links.reconciled_at,
                                    excluded.reconciled_at)""",
        (
            direct_id,google_id,participant_subject,state,method,rendered,
            sha256_text(rendered),now,now,now,
        ),
    )


def _write_history(conn: sqlite3.Connection, row_id: int, direct_id: int | None, google_id: int | None, state: str, method: str | None, details: dict[str, Any]) -> None:
    rendered = canonical_json(details)
    conn.execute(
        """INSERT INTO challenge_observation_reconciliation_history
           (reconciliation_id,direct_submission_id,google_submission_id,reconciliation_state,
            match_method,details_json,details_sha256,recorded_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (row_id,direct_id,google_id,state,method,rendered,sha256_text(rendered),utc_now()),
    )


def _upsert_reconciliation(
    conn: sqlite3.Connection,
    direct_id: int | None,
    google_id: int | None,
    state: str,
    method: str | None,
    details: dict[str, Any],
) -> int:
    now = utc_now()
    rendered = canonical_json(details)
    existing = None
    if direct_id is not None:
        existing = conn.execute("SELECT * FROM challenge_observation_reconciliation WHERE direct_submission_id=?", (direct_id,)).fetchone()
    if not existing and google_id is not None:
        existing = conn.execute("SELECT * FROM challenge_observation_reconciliation WHERE google_submission_id=?", (google_id,)).fetchone()
    if existing:
        row_id = int(existing["reconciliation_id"])
        changed = (
            existing["direct_submission_id"] != direct_id or existing["google_submission_id"] != google_id
            or existing["reconciliation_state"] != state or existing["details_sha256"] != sha256_text(rendered)
        )
        conn.execute(
            """UPDATE challenge_observation_reconciliation
                  SET direct_submission_id=COALESCE(direct_submission_id,?),
                      google_submission_id=COALESCE(google_submission_id,?),
                      reconciliation_state=?,match_method=?,last_checked_at=?,
                      reconciled_at=CASE WHEN ? IN ('MATCH','GOOGLE_VALUE_DIFFERENCE','IDENTITY_DIFFERENCE','TIER_DIFFERENCE','PARTY_DIFFERENCE') THEN COALESCE(reconciled_at,?) ELSE reconciled_at END,
                      details_json=?,details_sha256=?
                WHERE reconciliation_id=?""",
            (direct_id,google_id,state,method,now,state,now,rendered,sha256_text(rendered),row_id),
        )
        if changed:
            _write_history(conn,row_id,direct_id or existing["direct_submission_id"],google_id or existing["google_submission_id"],state,method,details)
        return row_id
    cur = conn.execute(
        """INSERT INTO challenge_observation_reconciliation
           (direct_submission_id,google_submission_id,reconciliation_state,match_method,
            first_seen_at,last_checked_at,reconciled_at,details_json,details_sha256)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            direct_id,google_id,state,method,now,now,
            now if state not in {"DIRECT_WAITING_FOR_GOOGLE","GOOGLE_ONLY_NEW","DUPLICATE_DIRECT"} else None,
            rendered,sha256_text(rendered),
        ),
    )
    row_id = int(cur.lastrowid)
    _write_history(conn,row_id,direct_id,google_id,state,method,details)
    return row_id


def reconcile_direct_observations(conn: sqlite3.Connection) -> dict[str, int]:
    if not _table_exists(conn, "challenge_direct_intake_metadata"):
        return {"direct_events": 0, "waiting": 0, "matched": 0, "google_only_new": 0}
    direct_rows = conn.execute(
        """SELECT s.*,m.provider_event_id,m.discord_message_id,m.declared_tier_key,
                  m.approval_timestamp,m.google_source_watermark_at_intake
             FROM challenge_submissions s
             JOIN challenge_direct_intake_metadata m ON m.submission_id=s.submission_id
            ORDER BY s.submission_id"""
    ).fetchall()
    for direct in direct_rows:
        direct_id = int(direct["submission_id"])
        # Re-evaluate previously linked observations with the current classifier,
        # including observations later superseded by a corrected immutable row.
        # This preserves their historical link while keeping its classification
        # metadata honest after normalization rules evolve.
        for linked in conn.execute(
            """SELECT l.match_method,s.*
                 FROM challenge_observation_reconciliation_links l
                 JOIN challenge_submissions s ON s.submission_id=l.google_submission_id
                WHERE l.direct_submission_id=?
                ORDER BY s.submission_id""",
            (direct_id,),
        ).fetchall():
            state, details = _classification(conn, direct, linked)
            _upsert_reconciliation_link(
                conn,direct_id,int(linked["submission_id"]),state,
                str(linked["match_method"]),details,
            )
        for google, method in _candidate_google_group(conn, direct):
            google_id = int(google["submission_id"])
            state, details = _classification(conn, direct, google)
            _upsert_reconciliation_link(conn,direct_id,google_id,state,method,details)

        active_links = conn.execute(
            """SELECT l.*,s.*
                 FROM challenge_observation_reconciliation_links l
                 JOIN challenge_submissions s ON s.submission_id=l.google_submission_id
                WHERE l.direct_submission_id=?
                  AND NOT EXISTS (
                      SELECT 1 FROM challenge_submissions newer
                       WHERE newer.supersedes_submission_id=s.submission_id
                  )
                ORDER BY s.submission_id""",
            (direct_id,),
        ).fetchall()
        link_results: list[tuple[sqlite3.Row, str, dict[str, Any]]] = []
        for google in active_links:
            state, details = _classification(conn, direct, google)
            method = str(google["match_method"])
            _upsert_reconciliation_link(
                conn,direct_id,int(google["google_submission_id"]),state,method,details
            )
            link_results.append((google,state,details))

        if not link_results:
            _upsert_reconciliation(
                conn,direct_id,None,"DIRECT_WAITING_FOR_GOOGLE",None,
                {"reason":"No unambiguous Google mirror observed"},
            )
            continue

        direct_subjects = _party_subjects(conn, direct_id)
        observed_subjects = {
            str(row["participant_subject_key"])
            for row, _state, _details in link_results
            if row["participant_subject_key"]
        }
        missing_subjects = sorted(direct_subjects - observed_subjects)
        states = [state for _row,state,_details in link_results]
        priority = (
            "IDENTITY_DIFFERENCE","TIER_DIFFERENCE","GOOGLE_VALUE_DIFFERENCE",
            "PARTY_DIFFERENCE",
        )
        summary_state = next((state for state in priority if state in states), "MATCH")
        if len(direct_subjects) > 1 and missing_subjects and summary_state == "MATCH":
            summary_state = "DIRECT_WAITING_FOR_GOOGLE"
        primary_google_id = int(link_results[0][0]["google_submission_id"])
        summary_details = {
            "reconciliation_model": "one_to_many_v1",
            "active_link_count": len(link_results),
            "direct_participant_count": len(direct_subjects),
            "observed_participant_count": len(observed_subjects),
            "participant_coverage_complete": not missing_subjects,
            "missing_participant_subjects": missing_subjects,
            "link_details": [
                {
                    "google_submission_id": int(row["google_submission_id"]),
                    "match_class": state,
                    **details,
                }
                for row,state,details in link_results
            ],
        }
        _upsert_reconciliation(
            conn,direct_id,primary_google_id,summary_state,"one_to_many_links",summary_details
        )

    setting = conn.execute(
        "SELECT setting_value FROM challenge_settings WHERE setting_key='phase3b_google_source_watermark'"
    ).fetchone()
    watermark = int(setting[0]) if setting else 0
    for google in conn.execute(
        """SELECT s.submission_id FROM challenge_submissions s
            WHERE s.source_system='regular_submissions_sheet_sync'
              AND s.legacy_regular_submission_id>?
              AND NOT EXISTS (
                  SELECT 1 FROM challenge_submissions newer
                   WHERE newer.supersedes_submission_id=s.submission_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM challenge_observation_reconciliation_links l
                   WHERE l.google_submission_id=s.submission_id
              )
              AND NOT EXISTS (SELECT 1 FROM challenge_observation_reconciliation r WHERE r.google_submission_id=s.submission_id)
            ORDER BY s.legacy_regular_submission_id""",
        (watermark,),
    ).fetchall():
        _upsert_reconciliation(
            conn,None,int(google["submission_id"]),"GOOGLE_ONLY_NEW",None,
            {"reason":"New Google observation has no direct shadow counterpart"},
        )
    counts = {
        str(row["reconciliation_state"]): int(row["n"])
        for row in conn.execute(
            """SELECT reconciliation_state,COUNT(*) n
                 FROM (
                     SELECT r.reconciliation_state
                       FROM challenge_observation_reconciliation r
                      WHERE r.direct_submission_id IS NOT NULL
                     UNION ALL
                     SELECT 'GOOGLE_ONLY_NEW'
                       FROM challenge_observation_reconciliation r
                       JOIN challenge_submissions s ON s.submission_id=r.google_submission_id
                      WHERE r.reconciliation_state='GOOGLE_ONLY_NEW'
                        AND r.direct_submission_id IS NULL
                        AND NOT EXISTS (
                            SELECT 1 FROM challenge_submissions newer
                             WHERE newer.supersedes_submission_id=s.submission_id
                        )
                        AND NOT EXISTS (
                            SELECT 1 FROM challenge_observation_reconciliation_links l
                             WHERE l.google_submission_id=s.submission_id
                        )
                 ) current_states
                GROUP BY reconciliation_state"""
        )
    }
    return {
        "direct_events": len(direct_rows),
        "waiting": counts.get("DIRECT_WAITING_FOR_GOOGLE",0),
        "matched": counts.get("MATCH",0),
        "google_only_new": counts.get("GOOGLE_ONLY_NEW",0),
        "links": int(conn.execute(
            "SELECT COUNT(*) FROM challenge_observation_reconciliation_links"
        ).fetchone()[0]),
        "mismatches": sum(counts.get(key,0) for key in (
            "GOOGLE_VALUE_DIFFERENCE","IDENTITY_DIFFERENCE","TIER_DIFFERENCE","PARTY_DIFFERENCE"
        )),
    }


def active_config_document(conn: sqlite3.Connection) -> dict[str, Any]:
    document = authoritative_config_document(conn)
    # Keep the Phase 3B key for callers while exposing the stable Phase 4A shape.
    document["config_version_id"] = document["version_id"]
    return document
