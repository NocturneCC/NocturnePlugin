#!/usr/bin/env python3
"""Idempotently project approved Challenge submissions into leaderboard observations.

This process reads committed immutable Challenge submissions from Challenges.db.
It does not contact Discord or Google and does not read or write rank points.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_DATABASE = Path("/srv/projects/database/Challenges.db")
SOURCE_SYSTEM = "midgard_challenge_submission"

SCHEMA = """
CREATE TABLE IF NOT EXISTS leaderboard_challenge_submission_links (
  source_submission_id INTEGER PRIMARY KEY,
  canonical_submission_id INTEGER NOT NULL,
  observation_id INTEGER NOT NULL,
  link_kind TEXT NOT NULL CHECK(link_kind IN ('created','existing_observation','mirror_of_direct')),
  ingest_fingerprint TEXT NOT NULL,
  linked_at TEXT NOT NULL,
  FOREIGN KEY(source_submission_id) REFERENCES challenge_submissions(submission_id),
  FOREIGN KEY(canonical_submission_id) REFERENCES challenge_submissions(submission_id),
  FOREIGN KEY(observation_id) REFERENCES leaderboard_observations(observation_id)
);
CREATE INDEX IF NOT EXISTS idx_leaderboard_challenge_links_observation
  ON leaderboard_challenge_submission_links(observation_id);
CREATE TRIGGER IF NOT EXISTS leaderboard_challenge_links_no_update
BEFORE UPDATE ON leaderboard_challenge_submission_links BEGIN
  SELECT RAISE(ABORT,'leaderboard challenge source links are immutable');
END;
CREATE TRIGGER IF NOT EXISTS leaderboard_challenge_links_no_delete
BEFORE DELETE ON leaderboard_challenge_submission_links BEGIN
  SELECT RAISE(ABORT,'leaderboard challenge source links are immutable');
END;

CREATE TABLE IF NOT EXISTS leaderboard_challenge_ingest_issues (
  issue_id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_submission_id INTEGER NOT NULL,
  issue_class TEXT NOT NULL,
  details_json TEXT NOT NULL,
  details_sha256 TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  UNIQUE(source_submission_id,issue_class,details_sha256),
  FOREIGN KEY(source_submission_id) REFERENCES challenge_submissions(submission_id)
);
CREATE TRIGGER IF NOT EXISTS leaderboard_challenge_issues_no_update
BEFORE UPDATE ON leaderboard_challenge_ingest_issues BEGIN
  SELECT RAISE(ABORT,'leaderboard challenge ingest issues are append-only');
END;
CREATE TRIGGER IF NOT EXISTS leaderboard_challenge_issues_no_delete
BEFORE DELETE ON leaderboard_challenge_ingest_issues BEGIN
  SELECT RAISE(ABORT,'leaderboard challenge ingest issues are append-only');
END;
"""


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    text = value if isinstance(value, str) else canonical(value)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_time_ms(value: str | None) -> int | None:
    text = str(value or "").strip()
    match = re.fullmatch(r"(?:(\d+):)?([0-5]?\d):([0-5]\d)(?:\.(\d{1,3}))?", text)
    if not match:
        return None
    hours = int(match.group(1) or 0)
    minutes = int(match.group(2))
    seconds = int(match.group(3))
    fraction = (match.group(4) or "").ljust(3, "0")
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + int(fraction or 0)


@dataclass(frozen=True)
class Candidate:
    source: sqlite3.Row
    canonical: sqlite3.Row
    mode: sqlite3.Row
    participants: tuple[sqlite3.Row, ...]
    metric_value: int
    metric_display: str
    fingerprint: str


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _active_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT config_version_id FROM challenge_config_versions WHERE status='active' ORDER BY config_version_id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise RuntimeError("active challenge configuration not found")
    return int(row[0])


def _legacy_cutoff(conn: sqlite3.Connection) -> datetime:
    row = conn.execute(
        """SELECT snapshot_captured_at FROM leaderboard_import_runs
             WHERE source_system LIKE 'google_legacy_csv:%'
             ORDER BY import_run_id DESC LIMIT 1"""
    ).fetchone()
    parsed = parse_datetime(row[0] if row else None)
    if parsed is None:
        raise RuntimeError("legacy leaderboard import cutoff is unavailable")
    return parsed


def _after_cutoff(row: sqlite3.Row, cutoff: datetime) -> bool:
    approved = parse_datetime(row["source_approved_at"])
    observed = parse_datetime(row["first_observed_at"])
    if approved is not None and "T" in str(row["source_approved_at"] or ""):
        return approved > cutoff
    # Date-only Google timestamps have no reliable intra-day ordering.  The
    # local first-observed time is therefore the deciding watermark on the
    # cutoff date; strong observation matching still prevents duplication.
    if approved is not None:
        return approved.date() >= cutoff.date() and bool(observed and observed > cutoff)
    return bool(observed and observed > cutoff)


def _canonical_submission_id(conn: sqlite3.Connection, row: sqlite3.Row) -> int:
    submission_id = int(row["submission_id"])
    if str(row["source_system"]) == "discord_direct":
        return submission_id
    linked = conn.execute(
        """SELECT direct_submission_id
             FROM challenge_observation_reconciliation_links
            WHERE google_submission_id=?
              AND match_class IN ('MATCH','GOOGLE_VALUE_DIFFERENCE','TIER_DIFFERENCE','PARTY_DIFFERENCE')
            ORDER BY CASE match_class WHEN 'MATCH' THEN 0 ELSE 1 END,link_id LIMIT 1""",
        (submission_id,),
    ).fetchone()
    return int(linked[0]) if linked else submission_id


def _participants(conn: sqlite3.Connection, submission_id: int) -> tuple[sqlite3.Row, ...]:
    return tuple(conn.execute(
        """SELECT participant_id,subject_key,member_id,discord_id,rsn_snapshot,
                  identity_resolution_method,participant_role
             FROM challenge_submission_participants WHERE submission_id=?
             ORDER BY CASE participant_role WHEN 'submitter' THEN 0 ELSE 1 END,participant_id""",
        (submission_id,),
    ).fetchall())


def _mode_for(conn: sqlite3.Connection, version_id: int, boss_key: str, party_size: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT * FROM leaderboard_mode_versions
            WHERE config_version_id=? AND is_active=1 AND boss_key=?
              AND ? BETWEEN party_size_min AND party_size_max
            ORDER BY display_order,mode_key""",
        (version_id, boss_key, party_size),
    ).fetchall()


def _normalized_metric(source: sqlite3.Row, mode: sqlite3.Row) -> tuple[int, str] | None:
    source_type = str(source["metric_type"] or "")
    source_unit = str(source["metric_unit"] or "")
    mode_type = str(mode["metric_type"])
    mode_unit = str(mode["metric_unit"])
    if source["metric_value"] is not None and source_type == mode_type and source_unit == mode_unit:
        return int(source["metric_value"]), str(source["metric_display"] or source["metric_value"])
    if mode_type == "time" and mode_unit == "milliseconds":
        parsed = parse_time_ms(source["metric_display"])
        if parsed is not None:
            return parsed, str(source["metric_display"])
    return None


def _candidate(conn: sqlite3.Connection, source: sqlite3.Row, version_id: int) -> tuple[Candidate | None, str, dict[str, Any]]:
    canonical_id = _canonical_submission_id(conn, source)
    canonical_row = conn.execute("SELECT * FROM challenge_submissions WHERE submission_id=?", (canonical_id,)).fetchone()
    if canonical_row is None:
        return None, "source_missing", {"canonical_submission_id": canonical_id}
    participants = _participants(conn, canonical_id)
    if not participants or any(row["member_id"] is None for row in participants):
        return None, "unresolved_identity", {"canonical_submission_id": canonical_id, "participant_count": len(participants)}
    modes = _mode_for(conn, version_id, str(canonical_row["boss_key"] or ""), len(participants))
    if not modes:
        boss_modes = conn.execute(
            "SELECT mode_key,party_size_min,party_size_max FROM leaderboard_mode_versions WHERE config_version_id=? AND is_active=1 AND boss_key=?",
            (version_id, canonical_row["boss_key"]),
        ).fetchall()
        return None, "party_mismatch" if boss_modes else "unsupported_mode", {
            "canonical_submission_id": canonical_id,
            "boss_key": canonical_row["boss_key"],
            "participant_count": len(participants),
            "configured_modes": [dict(row) for row in boss_modes],
        }
    if len(modes) != 1:
        return None, "ambiguous_mode", {"mode_keys": [str(row["mode_key"]) for row in modes]}
    mode = modes[0]
    normalized = _normalized_metric(canonical_row, mode)
    if normalized is None:
        return None, "invalid_metric", {
            "canonical_submission_id": canonical_id,
            "source_metric_type": canonical_row["metric_type"],
            "source_metric_unit": canonical_row["metric_unit"],
            "mode_metric_type": mode["metric_type"],
            "mode_metric_unit": mode["metric_unit"],
        }
    metric_value, metric_display = normalized
    subjects = sorted(str(row["subject_key"]) for row in participants)
    fingerprint_payload = {
        "source_system": SOURCE_SYSTEM,
        "canonical_submission_id": canonical_id,
        "mode_key": str(mode["mode_key"]),
        "participants": subjects,
        "metric_value": metric_value,
        "proof_url": str(canonical_row["evidence_url"] or ""),
    }
    return Candidate(source, canonical_row, mode, participants, metric_value, metric_display, digest(fingerprint_payload)), "eligible", {}


def _existing_observation(conn: sqlite3.Connection, candidate: Candidate) -> int | None:
    source_id = int(candidate.source["submission_id"])
    linked = conn.execute(
        "SELECT observation_id FROM leaderboard_challenge_submission_links WHERE source_submission_id=?",
        (source_id,),
    ).fetchone()
    if linked:
        return int(linked[0])
    proof = str(candidate.canonical["evidence_url"] or "")
    if not proof:
        return None
    subjects = sorted(str(row["subject_key"]) for row in candidate.participants)
    rows = conn.execute(
        """SELECT observation_id FROM leaderboard_observations
            WHERE mode_key=? AND metric_value=? AND metric_unit=? AND proof_url=?""",
        (candidate.mode["mode_key"], candidate.metric_value, candidate.mode["metric_unit"], proof),
    ).fetchall()
    for row in rows:
        existing_subjects = sorted(str(item[0]) for item in conn.execute(
            "SELECT subject_key FROM leaderboard_observation_participants WHERE observation_id=?",
            (int(row[0]),),
        ))
        if existing_subjects == subjects:
            return int(row[0])
    return None


def _record_issue(conn: sqlite3.Connection, submission_id: int, issue_class: str, details: dict[str, Any], now: str) -> None:
    details_json = canonical(details)
    conn.execute(
        """INSERT OR IGNORE INTO leaderboard_challenge_ingest_issues
           (source_submission_id,issue_class,details_json,details_sha256,observed_at)
           VALUES(?,?,?,?,?)""",
        (submission_id, issue_class, details_json, digest(details_json), now),
    )


def _link(conn: sqlite3.Connection, source_id: int, canonical_id: int, observation_id: int, kind: str, fingerprint: str, now: str) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO leaderboard_challenge_submission_links
           (source_submission_id,canonical_submission_id,observation_id,link_kind,ingest_fingerprint,linked_at)
           VALUES(?,?,?,?,?,?)""",
        (source_id, canonical_id, observation_id, kind, fingerprint, now),
    )


def ingest(database: Path, *, apply: bool, allow_migrate: bool = False) -> dict[str, Any]:
    conn = sqlite3.connect(database, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        if allow_migrate:
            migrate(conn)
        elif conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='leaderboard_challenge_submission_links'").fetchone() is None:
            raise RuntimeError("leaderboard challenge ingestion schema is not installed")
        conn.execute("BEGIN IMMEDIATE")
        version_id = _active_version(conn)
        cutoff = _legacy_cutoff(conn)
        sources = conn.execute(
            """SELECT s.* FROM challenge_submissions s
                WHERE s.record_state IN ('approved','corrected') AND s.parse_status='parsed'
                  AND NOT EXISTS (SELECT 1 FROM challenge_submissions newer WHERE newer.supersedes_submission_id=s.submission_id)
                  AND NOT EXISTS (SELECT 1 FROM leaderboard_challenge_submission_links l WHERE l.source_submission_id=s.submission_id)
                ORDER BY CASE s.source_system WHEN 'discord_direct' THEN 0 ELSE 1 END,s.submission_id"""
        ).fetchall()
        sources = [row for row in sources if _after_cutoff(row, cutoff)]
        now = utc_now()
        counts = {
            "scanned": len(sources), "eligible": 0, "already_represented": 0,
            "created": 0, "mirror_linked": 0, "unresolved_identity": 0,
            "invalid_metric": 0, "unsupported_mode": 0, "party_mismatch": 0,
            "ambiguous_mode": 0, "source_missing": 0,
        }
        planned: list[tuple[Candidate, int | None]] = []
        for source in sources:
            candidate, classification, details = _candidate(conn, source, version_id)
            if candidate is None:
                counts[classification] = counts.get(classification, 0) + 1
                if apply:
                    _record_issue(conn, int(source["submission_id"]), classification, details, now)
                continue
            counts["eligible"] += 1
            existing = _existing_observation(conn, candidate)
            if existing is not None:
                counts["already_represented"] += 1
            planned.append((candidate, existing))

        to_create_by_canonical: dict[int, tuple[Candidate, int | None]] = {}
        for item in planned:
            if item[1] is None:
                to_create_by_canonical.setdefault(int(item[0].canonical["submission_id"]), item)
        to_create = list(to_create_by_canonical.values())
        import_run_id = None
        if apply and to_create:
            ids = [int(item[0].canonical["submission_id"]) for item in to_create]
            proofs = [str(item[0].canonical["evidence_url"] or "") for item in to_create]
            import_run_id = int(conn.execute(
                """INSERT INTO leaderboard_import_runs(
                     source_system,snapshot_captured_at,leaderboard_sha256,proof_sha256,
                     leaderboard_row_count,proof_row_count,leaderboard_headers_json,proof_headers_json,
                     leaderboard_max_index,proof_max_index,imported_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (SOURCE_SYSTEM, now, digest(ids), digest(proofs), len(ids), sum(bool(p) for p in proofs),
                 canonical(["challenge_submission_id"]), canonical(["evidence_url"]), max(ids), max(ids), now),
            ).lastrowid)

        canonical_observations: dict[int, int] = {}
        for candidate, existing in planned:
            source_id = int(candidate.source["submission_id"])
            canonical_id = int(candidate.canonical["submission_id"])
            if canonical_id in canonical_observations:
                observation_id = canonical_observations[canonical_id]
                kind = "mirror_of_direct"
                counts["mirror_linked"] += 1
            elif existing is not None:
                observation_id = existing
                canonical_observations[canonical_id] = observation_id
                kind = "existing_observation"
            elif not apply:
                continue
            else:
                subjects = sorted(str(row["subject_key"]) for row in candidate.participants)
                discord_ids = sorted(str(row["discord_id"]) for row in candidate.participants if row["discord_id"])
                party_key = ",".join(discord_ids or subjects)
                competitor_key = subjects[0] if len(subjects) == 1 else "party:" + digest(subjects)
                proof = str(candidate.canonical["evidence_url"] or "") or None
                proof_type = "proof_url" if proof else "source_submission_id"
                proof_identity = proof or f"challenge_submission:{canonical_id}"
                source_snapshot = {
                    "submission_id": canonical_id,
                    "source_system": candidate.canonical["source_system"],
                    "source_record_id": candidate.canonical["source_record_id"],
                    "boss_key": candidate.canonical["boss_key"],
                    "config_version_id": candidate.canonical["config_version_id"],
                    "metric_type": candidate.canonical["metric_type"],
                    "metric_value": candidate.metric_value,
                    "metric_display": candidate.metric_display,
                    "participant_subjects": subjects,
                    "evidence_url": proof,
                }
                cursor = conn.execute(
                    """INSERT INTO leaderboard_observations(
                       import_run_id,config_version_id,mode_key,metric_type,metric_value,metric_unit,metric_display,
                       comparison_direction,proof_url,proof_identity_type,proof_identity,occurred_at,last_occurred_at,
                       party_key,party_display,competitor_key,source_system,source_indexes_json,source_rows_json,
                       source_payload_hashes_json,source_row_count,ingest_fingerprint,identity_state,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (import_run_id, version_id, candidate.mode["mode_key"], candidate.mode["metric_type"],
                     candidate.metric_value, candidate.mode["metric_unit"], candidate.metric_display,
                     candidate.mode["comparison_direction"], proof, proof_type, proof_identity,
                     candidate.canonical["source_approved_at"], candidate.canonical["source_approved_at"],
                     party_key, ", ".join(str(row["rsn_snapshot"] or row["subject_key"]) for row in candidate.participants),
                     competitor_key, SOURCE_SYSTEM, canonical([f"challenge_submission:{canonical_id}"]),
                     canonical([source_snapshot]), canonical([candidate.canonical["raw_payload_sha256"]]), 1,
                     candidate.fingerprint, "resolved", now),
                )
                observation_id = int(cursor.lastrowid)
                canonical_observations[canonical_id] = observation_id
                kind = "created"
                counts["created"] += 1
                for order, participant in enumerate(candidate.participants, 1):
                    conn.execute(
                        """INSERT INTO leaderboard_observation_participants(
                           observation_id,participant_order,discord_id,submitted_rsn,member_id,
                           subject_key,resolution_method,source_indexes_json) VALUES(?,?,?,?,?,?,?,?)""",
                        (observation_id, order, participant["discord_id"], participant["rsn_snapshot"],
                         participant["member_id"], participant["subject_key"], participant["identity_resolution_method"],
                         canonical([f"challenge_submission:{canonical_id}"])),
                    )
            if apply:
                _link(conn, source_id, canonical_id, observation_id, kind, candidate.fingerprint, now)

        if apply:
            foreign_errors = conn.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_errors:
                raise RuntimeError(f"foreign key check failed: {len(foreign_errors)}")
            conn.commit()
        else:
            conn.rollback()
        return {
            "ok": True,
            "apply": apply,
            "active_config_version": version_id,
            "legacy_cutoff": cutoff.isoformat(),
            **counts,
            "would_create": len(to_create),
            "leaderboard_only_modes": [str(row[0]) for row in conn.execute(
                "SELECT mode_key FROM leaderboard_mode_versions WHERE config_version_id=? AND is_active=1 AND boss_key IS NULL ORDER BY display_order",
                (version_id,),
            )],
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--migrate", action="store_true")
    args = parser.parse_args()
    print(json.dumps(ingest(args.database, apply=args.apply, allow_migrate=args.migrate), sort_keys=True))


if __name__ == "__main__":
    main()
