"""Server-authoritative RuneLite Challenge completion observations.

The public endpoint accepts bounded observations, never approval or PB claims.
All persistence, Challenge projections, and leaderboard ingestion share the
caller-owned SQLite transaction.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from challenge_config import config_document
from challenge_shadow_common import normalize_rsn

PROVENANCE = "nocturne_runelite_automatic"
SOURCE_SYSTEM = "runelite_automatic"
MAX_GROUP_SIZE = 10
MAX_DURATION_MS = 86_400_000
MAX_EVENT_AGE = timedelta(hours=24)
MAX_FUTURE_SKEW = timedelta(minutes=2)
RSN_RE = re.compile(r"^[A-Za-z0-9 _-]{1,12}$")
MODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
EVENT_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]{1,16})?$")

# These are the only content identities the client completion parser will be
# allowed to report. The selected metric is additionally checked against the
# active published Challenge definition below.
ACTIVITIES = {
    "theatre_of_blood": ("theatre_of_blood", "segment"),
    "theatre_of_blood_hard_mode": ("theatre_of_blood_hard_mode", "overall"),
    "tombs_of_amascut": ("tombs_of_amascut", "overall"),
    "chambers_of_xeric": ("chambers_of_xeric", "overall"),
    "chambers_of_xeric_challenge_mode": ("chambers_of_xeric_cm", "overall"),
}

MIGRATION_TABLES = ("challenge_automatic_observations", "challenge_automatic_observation_participants")


class ObservationError(ValueError):
    def __init__(self, state: str, reason: str):
        super().__init__(reason)
        self.state = state
        self.reason = reason


def migrate_schema(conn: sqlite3.Connection) -> None:
    """Install the additive, reversible observation schema atomically."""
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("BEGIN IMMEDIATE")
    try:
        present = {str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?)", MIGRATION_TABLES
        )}
        if present and present != set(MIGRATION_TABLES):
            raise RuntimeError("partial automatic observation schema")
        if not present:
            conn.execute("""CREATE TABLE challenge_automatic_observations (
                observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
                schema_version INTEGER NOT NULL CHECK(schema_version=1),
                provenance TEXT NOT NULL CHECK(provenance='nocturne_runelite_automatic'),
                client_event_id TEXT NOT NULL UNIQUE,
                canonical_payload_sha256 TEXT NOT NULL CHECK(length(canonical_payload_sha256)=64),
                semantic_fingerprint TEXT NOT NULL CHECK(length(semantic_fingerprint)=64),
                reporter_normalized_rsn TEXT NOT NULL,
                activity_key TEXT NOT NULL,
                mode_key TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                room_time_ms INTEGER,
                overall_time_ms INTEGER,
                selected_timing_scope TEXT CHECK(selected_timing_scope IN ('overall','segment')),
                selected_duration_ms INTEGER,
                observed_group_size INTEGER NOT NULL CHECK(observed_group_size BETWEEN 1 AND 10),
                completion_count INTEGER CHECK(completion_count BETWEEN 1 AND 1000000),
                plugin_version TEXT NOT NULL,
                config_version_id INTEGER NOT NULL,
                disposition TEXT NOT NULL CHECK(disposition IN (
                    'accepted','ignored_manual_only','ignored_unconfigured','ignored_disabled')),
                submission_id INTEGER UNIQUE,
                created_at TEXT NOT NULL,
                CHECK((room_time_ms IS NULL OR room_time_ms BETWEEN 1 AND 86400000)
                  AND (overall_time_ms IS NULL OR overall_time_ms BETWEEN 1 AND 86400000)
                  AND (selected_duration_ms IS NULL OR selected_duration_ms BETWEEN 1 AND 86400000)),
                CHECK((disposition='accepted' AND selected_timing_scope IS NOT NULL
                       AND selected_duration_ms IS NOT NULL AND submission_id IS NOT NULL)
                   OR (disposition!='accepted' AND submission_id IS NULL)),
                FOREIGN KEY(config_version_id) REFERENCES challenge_config_versions(config_version_id),
                FOREIGN KEY(submission_id) REFERENCES challenge_submissions(submission_id)
            )""")
            conn.execute("CREATE UNIQUE INDEX uq_challenge_auto_semantic_count ON challenge_automatic_observations(reporter_normalized_rsn,activity_key,mode_key,completion_count) WHERE completion_count IS NOT NULL")
            conn.execute("CREATE INDEX idx_challenge_auto_created ON challenge_automatic_observations(created_at)")
            conn.execute("""CREATE TABLE challenge_automatic_observation_participants (
                observation_id INTEGER NOT NULL,
                participant_order INTEGER NOT NULL CHECK(participant_order BETWEEN 1 AND 10),
                normalized_rsn TEXT NOT NULL CHECK(length(normalized_rsn) BETWEEN 1 AND 12),
                PRIMARY KEY(observation_id,participant_order),
                UNIQUE(observation_id,normalized_rsn),
                FOREIGN KEY(observation_id) REFERENCES challenge_automatic_observations(observation_id)
            )""")
            conn.execute("""CREATE TRIGGER challenge_automatic_observations_no_update
                BEFORE UPDATE ON challenge_automatic_observations BEGIN
                SELECT RAISE(ABORT,'automatic observations are immutable'); END""")
            conn.execute("""CREATE TRIGGER challenge_automatic_observations_no_delete
                BEFORE DELETE ON challenge_automatic_observations BEGIN
                SELECT RAISE(ABORT,'automatic observations are immutable'); END""")
            conn.execute("""CREATE TRIGGER challenge_automatic_participants_no_update
                BEFORE UPDATE ON challenge_automatic_observation_participants BEGIN
                SELECT RAISE(ABORT,'automatic observation participants are immutable'); END""")
            conn.execute("""CREATE TRIGGER challenge_automatic_participants_no_delete
                BEFORE DELETE ON challenge_automatic_observation_participants BEGIN
                SELECT RAISE(ABORT,'automatic observation participants are immutable'); END""")
        else:
            expected = {
                "challenge_automatic_observations": {
                    "observation_id", "schema_version", "provenance", "client_event_id",
                    "canonical_payload_sha256", "semantic_fingerprint", "reporter_normalized_rsn",
                    "activity_key", "mode_key", "occurred_at", "room_time_ms", "overall_time_ms",
                    "selected_timing_scope", "selected_duration_ms", "observed_group_size",
                    "completion_count", "plugin_version", "config_version_id", "disposition",
                    "submission_id", "created_at",
                },
                "challenge_automatic_observation_participants": {
                    "observation_id", "participant_order", "normalized_rsn",
                },
            }
            for table, expected_columns in expected.items():
                found = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
                if found != expected_columns:
                    raise RuntimeError("automatic observation schema mismatch")
            trigger_names = {str(row[0]) for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'challenge_automatic_%'"
            )}
            required_triggers = {
                "challenge_automatic_observations_no_update", "challenge_automatic_observations_no_delete",
                "challenge_automatic_participants_no_update", "challenge_automatic_participants_no_delete",
            }
            if not required_triggers <= trigger_names:
                raise RuntimeError("automatic observation immutability guard missing")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def rollback_schema(conn: sqlite3.Connection) -> None:
    """Reverse the migration only while it contains no observation data."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        present = {str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?)", MIGRATION_TABLES
        )}
        if not present:
            conn.commit()
            return
        if present != set(MIGRATION_TABLES):
            raise RuntimeError("partial automatic observation schema")
        if conn.execute("SELECT 1 FROM challenge_automatic_observations LIMIT 1").fetchone():
            raise RuntimeError("automatic observation data prevents migration rollback")
        if conn.execute("SELECT 1 FROM challenge_automatic_observation_participants LIMIT 1").fetchone():
            raise RuntimeError("automatic observation participant data prevents migration rollback")
        conn.execute("DROP TABLE challenge_automatic_observation_participants")
        conn.execute("DROP TABLE challenge_automatic_observations")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _iso_time(value: Any, now: datetime) -> str:
    if not isinstance(value, str) or len(value) > 40:
        raise ObservationError("invalid", "invalid_timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ObservationError("invalid", "invalid_timestamp") from exc
    if parsed.tzinfo is None:
        raise ObservationError("invalid", "invalid_timestamp")
    parsed = parsed.astimezone(timezone.utc)
    if parsed < now - MAX_EVENT_AGE or parsed > now + MAX_FUTURE_SKEW:
        raise ObservationError("invalid", "timestamp_out_of_range")
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def normalize_payload(payload: Any, now: datetime | None = None) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ObservationError("invalid", "invalid_schema")
    allowed = {
        "schema_version", "event_id", "reporter_rsn", "activity_key", "mode_key",
        "occurred_at", "room_time_ms", "overall_time_ms", "roster", "group_size",
        "completion_count", "plugin_version",
    }
    if set(payload) - allowed or set(payload) != allowed:
        # The two semantic metrics are optional, all other keys are required.
        required = allowed - {"room_time_ms", "overall_time_ms", "completion_count"}
        if set(payload) - allowed or required - set(payload):
            raise ObservationError("invalid", "invalid_schema")
    if type(payload.get("schema_version")) is not int or payload["schema_version"] != 1:
        raise ObservationError("invalid", "unsupported_schema")
    event_id = payload.get("event_id")
    if not isinstance(event_id, str) or not EVENT_RE.fullmatch(event_id):
        raise ObservationError("invalid", "invalid_event_id")
    reporter = payload.get("reporter_rsn")
    if not isinstance(reporter, str) or not RSN_RE.fullmatch(reporter.strip()):
        raise ObservationError("invalid", "invalid_reporter")
    reporter = normalize_rsn(reporter.strip())
    activity = payload.get("activity_key")
    if not isinstance(activity, str) or activity not in ACTIVITIES:
        raise ObservationError("invalid", "unsupported_activity")
    mode = payload.get("mode_key")
    if not isinstance(mode, str) or not MODE_RE.fullmatch(mode):
        raise ObservationError("invalid", "unsupported_mode")
    group_size = payload.get("group_size")
    if type(group_size) is not int or not 1 <= group_size <= MAX_GROUP_SIZE:
        raise ObservationError("invalid", "invalid_group_size")
    roster = payload.get("roster")
    if not isinstance(roster, list) or not 1 <= len(roster) <= MAX_GROUP_SIZE or len(roster) != group_size:
        raise ObservationError("invalid", "incomplete_roster")
    normalized_roster = []
    for item in roster:
        if not isinstance(item, str) or not RSN_RE.fullmatch(item.strip()):
            raise ObservationError("invalid", "invalid_roster")
        normalized_roster.append(normalize_rsn(item.strip()))
    if len(set(normalized_roster)) != len(normalized_roster):
        raise ObservationError("invalid", "duplicate_participant")
    if normalized_roster.count(reporter) != 1:
        raise ObservationError("invalid", "reporter_not_in_roster")
    # Roster order is not semantically meaningful. Store and hash it in a
    # deterministic order while retaining every normalized public RSN.
    normalized_roster.sort()
    metrics: dict[str, int | None] = {}
    for key in ("room_time_ms", "overall_time_ms"):
        value = payload.get(key)
        if value is None:
            metrics[key] = None
        elif type(value) is not int or not 1 <= value <= MAX_DURATION_MS:
            raise ObservationError("invalid", "invalid_duration")
        else:
            metrics[key] = value
    if all(value is None for value in metrics.values()):
        raise ObservationError("invalid", "missing_metric")
    completion_count = payload.get("completion_count")
    if completion_count is not None and (type(completion_count) is not int or not 1 <= completion_count <= 1_000_000):
        raise ObservationError("invalid", "invalid_completion_count")
    plugin_version = payload.get("plugin_version")
    if not isinstance(plugin_version, str) or not VERSION_RE.fullmatch(plugin_version):
        raise ObservationError("invalid", "invalid_plugin_version")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    occurred = _iso_time(payload.get("occurred_at"), now)
    return {
        "schema_version": 1, "event_id": event_id, "reporter_rsn": reporter,
        "activity_key": activity, "mode_key": mode, "occurred_at": occurred,
        **metrics, "roster": normalized_roster, "group_size": group_size,
        "completion_count": completion_count, "plugin_version": plugin_version,
    }


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def _resolve_member(members: sqlite3.Connection, normalized_rsn: str) -> tuple[int | None, str | None, str | None]:
    """Resolve one public RSN against current, non-left member/account/alias rows."""
    rows = members.execute(
        """SELECT DISTINCT m.member_id,m.rsn
             FROM members m
             LEFT JOIN member_accounts a ON a.member_id=m.member_id AND a.normalized_rsn=? AND COALESCE(a.is_active,1)=1
             LEFT JOIN member_aliases x ON x.member_id=m.member_id AND x.normalized_alias_rsn=?
            WHERE COALESCE(m.status,'active')!='left'
              AND (m.normalized_rsn=? OR a.member_id IS NOT NULL OR x.member_id IS NOT NULL)
            ORDER BY m.member_id""",
        (normalized_rsn, normalized_rsn, normalized_rsn),
    ).fetchall()
    if len(rows) != 1:
        return None, None, None
    return int(rows[0]["member_id"]), str(rows[0]["rsn"] or normalized_rsn), "members_rsn_alias"


def _active_policy(conn: sqlite3.Connection, payload: dict[str, Any]) -> tuple[int, dict[str, Any], dict[str, Any], str | None]:
    versions = conn.execute(
        "SELECT config_version_id FROM challenge_config_versions WHERE status='active' ORDER BY config_version_id DESC"
    ).fetchall()
    if len(versions) != 1:
        raise RuntimeError("active Challenge configuration is ambiguous")
    version_id = int(versions[0][0])
    document = config_document(conn, version_id)
    if int(document.get("version_id", -1)) != version_id or document.get("status") != "active":
        raise RuntimeError("active Challenge configuration is invalid")
    content, expected_scope = ACTIVITIES[payload["activity_key"]]
    modes = [row for row in document.get("leaderboard_modes", []) if row.get("mode_key") == payload["mode_key"]]
    if len(modes) != 1 or modes[0].get("content_key") != content:
        raise ObservationError("invalid", "unsupported_mode")
    mode = modes[0]
    if not mode.get("active"):
        return version_id, {}, mode, "disabled"
    boss_key = mode.get("boss_key")
    if not isinstance(boss_key, str) or not boss_key:
        raise ObservationError("invalid", "unsupported_mode")
    bosses = [row for row in document.get("bosses", []) if row.get("boss_key") == boss_key]
    if len(bosses) != 1:
        raise RuntimeError("active Challenge mode has ambiguous boss configuration")
    boss = bosses[0]
    if not boss.get("active") or not boss.get("submission_enabled"):
        return version_id, boss, mode, "disabled"
    capture_mode = boss.get("automatic_capture")
    if boss.get("timing_scope") == "unconfigured":
        return version_id, boss, mode, "unconfigured"
    if capture_mode == "manual_only":
        return version_id, boss, mode, "manual_only"
    if capture_mode is None:
        return version_id, boss, mode, "unconfigured"
    if capture_mode != "enabled":
        raise RuntimeError("active Challenge capture policy is malformed")
    if boss.get("timing_scope") not in {"overall", "segment"}:
        raise RuntimeError("active Challenge timing policy is malformed")
    if boss.get("timing_scope") != expected_scope:
        raise ObservationError("invalid", "unsupported_timing_policy")
    if (mode.get("metric_type") != "time" or mode.get("metric_unit") != "milliseconds"
            or mode.get("comparison_direction") != "lower" or boss.get("metric_type") != "time"):
        raise ObservationError("invalid", "unsupported_mode")
    if not int(mode.get("party_size_min", 0)) <= payload["group_size"] <= int(mode.get("party_size_max", -1)):
        raise ObservationError("invalid", "invalid_group_size")
    if payload["group_size"] > 1 and not boss.get("supports_groups"):
        raise ObservationError("invalid", "invalid_group_size")
    if payload["group_size"] < int(boss.get("min_party_size", 1)):
        raise ObservationError("invalid", "invalid_group_size")
    selected = "room_time_ms" if boss["timing_scope"] == "segment" else "overall_time_ms"
    if payload.get(selected) is None:
        raise ObservationError("invalid", "selected_metric_missing")
    return version_id, boss, mode, None


def _metric_display(milliseconds: int) -> str:
    hours, rest = divmod(milliseconds, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    seconds, rest = divmod(rest, 1_000)
    centiseconds = rest // 10
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"


def _earned_tier(boss: dict[str, Any], duration_ms: int) -> dict[str, Any]:
    qualifying = []
    for tier in boss.get("tiers", []):
        rank = int(tier.get("rank", 0))
        if rank == 1:
            qualifying.append(tier)
            continue
        if tier.get("metric_type") != "time" or tier.get("operator") != "lte":
            continue
        threshold = tier.get("threshold")
        if type(threshold) is int and duration_ms <= threshold:
            qualifying.append(tier)
    if not qualifying:
        raise ObservationError("invalid", "unsupported_tier_policy")
    return max(qualifying, key=lambda item: int(item["rank"]))


def _insert_observation(
    conn: sqlite3.Connection, payload: dict[str, Any], digest: str,
    semantic_digest: str, version_id: int, disposition: str,
    scope: str | None, selected_ms: int | None, submission_id: int | None,
    created_at: str,
) -> int:
    cur = conn.execute(
        """INSERT INTO challenge_automatic_observations(
           schema_version,provenance,client_event_id,canonical_payload_sha256,semantic_fingerprint,
           reporter_normalized_rsn,activity_key,mode_key,occurred_at,room_time_ms,overall_time_ms,
           selected_timing_scope,selected_duration_ms,observed_group_size,completion_count,
           plugin_version,config_version_id,disposition,submission_id,created_at)
           VALUES(1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (PROVENANCE, payload["event_id"], digest, semantic_digest,
         payload["reporter_rsn"], payload["activity_key"], payload["mode_key"], payload["occurred_at"],
         payload["room_time_ms"], payload["overall_time_ms"], scope, selected_ms,
         payload["group_size"], payload["completion_count"], payload["plugin_version"],
         version_id, disposition, submission_id, created_at),
    )
    observation_id = int(cur.lastrowid)
    conn.executemany(
        "INSERT INTO challenge_automatic_observation_participants(observation_id,participant_order,normalized_rsn) VALUES(?,?,?)",
        ((observation_id, index, rsn) for index, rsn in enumerate(payload["roster"], 1)),
    )
    return observation_id


def process_observation(
    conn: sqlite3.Connection,
    members: sqlite3.Connection,
    payload_value: Any,
    *,
    now: datetime | None = None,
) -> dict[str, str]:
    """Validate and persist one observation within an already-open write transaction."""
    if not conn.in_transaction:
        raise RuntimeError("automatic observation requires caller transaction")
    payload = normalize_payload(payload_value, now)
    version_id, boss, mode, policy_state = _active_policy(conn, payload)
    encoded = canonical_json(payload)
    request_digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    semantic = dict(payload)
    semantic.pop("event_id", None)
    semantic_digest = hashlib.sha256(canonical_json(semantic).encode("utf-8")).hexdigest()
    previous = conn.execute(
        "SELECT canonical_payload_sha256 FROM challenge_automatic_observations WHERE client_event_id=?",
        (payload["event_id"],),
    ).fetchone()
    if previous:
        if str(previous[0]) != request_digest:
            raise ObservationError("idempotency_conflict", "event_id_payload_conflict")
        return {"state": "duplicate"}
    if payload["completion_count"] is not None:
        semantic_previous = conn.execute(
            """SELECT semantic_fingerprint FROM challenge_automatic_observations
                 WHERE reporter_normalized_rsn=? AND activity_key=? AND mode_key=? AND completion_count=?""",
            (payload["reporter_rsn"], payload["activity_key"], payload["mode_key"], payload["completion_count"]),
        ).fetchone()
        if semantic_previous:
            if str(semantic_previous[0]) == semantic_digest:
                return {"state": "duplicate"}
            raise ObservationError("idempotency_conflict", "semantic_replay_conflict")

    resolved: dict[str, tuple[int, str, str] | None] = {}
    by_member: set[int] = set()
    for rsn in payload["roster"]:
        member_id, canonical_rsn, method = _resolve_member(members, rsn)
        if member_id is not None:
            if member_id in by_member:
                raise ObservationError("invalid", "duplicate_member_identity")
            by_member.add(member_id)
            resolved[rsn] = (member_id, canonical_rsn or rsn, method or "members_rsn_alias")
        else:
            resolved[rsn] = None
    reporter_identity = resolved.get(payload["reporter_rsn"])
    if reporter_identity is None:
        raise ObservationError("invalid", "reporter_not_eligible")

    created_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")
    if policy_state:
        disposition = {
            "manual_only": "ignored_manual_only",
            "unconfigured": "ignored_unconfigured",
            "disabled": "ignored_disabled",
        }[policy_state]
        _insert_observation(conn, payload, request_digest, semantic_digest, version_id,
                            disposition, None, None, None, created_at)
        return {"state": "ignored", "reason": policy_state}

    scope = str(boss["timing_scope"])
    selected_field = "room_time_ms" if scope == "segment" else "overall_time_ms"
    selected_ms = int(payload[selected_field])
    tier = _earned_tier(boss, selected_ms)
    # Re-check the direct-intake shadow gate: this contract must not bypass the
    # existing Challenge submission safety mode.
    direct_mode = conn.execute(
        "SELECT setting_value FROM challenge_settings WHERE setting_key='direct_intake_mode'"
    ).fetchone()
    if not direct_mode or direct_mode[0] != "shadow":
        _insert_observation(conn, payload, request_digest, semantic_digest, version_id,
                            "ignored_disabled", None, None, None, created_at)
        return {"state": "ignored", "reason": "disabled"}

    party_subjects = sorted(f"member:{resolved[rsn][0]}" for rsn in payload["roster"] if resolved[rsn])
    raw_payload = canonical_json(payload)
    metric_display = _metric_display(selected_ms)
    event_key = payload["event_id"]
    source_fingerprint = hashlib.sha256(
        f"{PROVENANCE}:{request_digest}".encode("utf-8")
    ).hexdigest()
    cur = conn.execute(
        """INSERT INTO challenge_submissions(
           source_system,source_record_id,legacy_regular_submission_id,source_external_id,
           source_snapshot_hash,ingest_fingerprint,config_version_id,boss_key,boss_display_name,
           earned_tier_key,earned_tier_rank,source_submission_points,metric_type,metric_value,
           metric_unit,metric_display,party_key,party_display,submitter_discord_id,approver_discord_id,
           approver_display,evidence_url,raw_notes,source_submitted_at,source_approved_at,
           first_observed_at,raw_payload_json,raw_payload_sha256,record_state,parse_status)
           VALUES(?,?,0,?,?,?,?,?,?,?,?,?,'time',?, 'milliseconds', ?, ?, ?, NULL,NULL,NULL,NULL,NULL,?,?,?,?,?,'approved','parsed')""",
        (SOURCE_SYSTEM, f"runelite:{event_key}", event_key, request_digest, source_fingerprint,
         version_id, boss["boss_key"], boss["display_name"], tier["tier_key"], int(tier["rank"]),
         int(tier["points"]), selected_ms, metric_display,
         ",".join(party_subjects), ", ".join(resolved[rsn][1] for rsn in payload["roster"] if resolved[rsn]),
         payload["occurred_at"], None, created_at, raw_payload, request_digest),
    )
    submission_id = int(cur.lastrowid)
    for rsn in payload["roster"]:
        identity = resolved[rsn]
        if identity is None:
            continue
        member_id, canonical_rsn, method = identity
        conn.execute(
            """INSERT INTO challenge_submission_participants(
               submission_id,subject_key,member_id,discord_id,rsn_snapshot,normalized_rsn_snapshot,
               participant_role,identity_resolution_method)
               VALUES(?,?,?,NULL,?,?,?,?)""",
            (submission_id, f"member:{member_id}", member_id, canonical_rsn, rsn,
             "submitter" if rsn == payload["reporter_rsn"] else "party_member", method),
        )
    _insert_observation(conn, payload, request_digest, semantic_digest, version_id,
                        "accepted", scope, selected_ms, submission_id, created_at)
    from challenge_direct_intake import ensure_award_reviews
    from challenge_shadow_sync import rebuild_derived
    from leaderboard_challenge_ingest import ingest_connection

    rebuild_derived(conn, version_id)
    ensure_award_reviews(conn)
    ingest_connection(conn, apply=True, source_submission_id=submission_id, manage_transaction=False)
    return {"state": "accepted"}
