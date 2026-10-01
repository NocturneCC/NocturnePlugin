#!/usr/bin/env python3
"""Ingest approved Google-mirrored challenge rows into the local shadow engine.

Safety boundary: RegularSubmissions.db and Members.db are opened read-only. This
program contains no Google, Discord, HTTP, or production rank-point writer.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from challenge_shadow_common import (
    DEFAULT_CHALLENGES_DB,
    DEFAULT_MEMBERS_DB,
    DEFAULT_REGULAR_DB,
    canonical_json,
    migrate_schema,
    normalize_rsn,
    parse_challenge,
    ro_connection,
    rw_connection,
    sha256_text,
    utc_now,
)
from challenge_direct_intake import ensure_award_reviews, reconcile_direct_observations
from challenge_config import evaluation_catalog
from challenge_legacy_baseline import future_award_eligibility, legacy_baselines


EXPECTED_AWARDS = {
    "Bronze Awarded": ("bronze", 28, 75),
    "Silver Awarded": ("silver", 17, 150),
    "Gold Awarded": ("gold", 8, 225),
    "Platinum Awarded": ("platinum", 2, 300),
    "Ascendant Awarded": ("ascendant", 1, 500),
}


def resolve_identity(row: sqlite3.Row, members: sqlite3.Connection) -> tuple[int | None, str, str]:
    source_member_id = row["member_id"]
    if source_member_id is not None:
        found = members.execute("SELECT member_id FROM members WHERE member_id=?", (source_member_id,)).fetchone()
        if found:
            member_id = int(found[0])
            return member_id, f"member:{member_id}", "regular_submissions.member_id"

    discord_id = str(row["discord_id"] or "").strip()
    if discord_id:
        found = members.execute("SELECT member_id FROM members WHERE CAST(discord_id AS TEXT)=? LIMIT 1", (discord_id,)).fetchone()
        if found:
            member_id = int(found[0])
            return member_id, f"member:{member_id}", "members.discord_id"

    nrsn = normalize_rsn(row["rsn"])
    if nrsn:
        found = members.execute(
            "SELECT member_id FROM member_accounts WHERE REPLACE(REPLACE(LOWER(normalized_rsn),'_',''),'-','')=? ORDER BY is_primary DESC,is_active DESC LIMIT 1",
            (nrsn,),
        ).fetchone()
        if found:
            member_id = int(found[0])
            return member_id, f"member:{member_id}", "member_accounts.normalized_rsn"
        found = members.execute(
            "SELECT member_id FROM member_aliases WHERE REPLACE(REPLACE(LOWER(normalized_alias_rsn),'_',''),'-','')=? LIMIT 1",
            (nrsn,),
        ).fetchone()
        if found:
            member_id = int(found[0])
            return member_id, f"member:{member_id}", "member_aliases.normalized_alias_rsn"

    if discord_id:
        return None, f"discord:{discord_id}", "unresolved_discord"
    if nrsn:
        return None, f"rsn:{nrsn}", "unresolved_rsn"
    return None, f"source:{row['submission_id']}", "unresolved_source"


def issue(conn: sqlite3.Connection, source_record_id: str, snapshot_hash: str, issue_class: str, detail: str) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO challenge_ingest_issues
           (source_system,source_record_id,source_snapshot_hash,issue_class,issue_detail)
           VALUES('regular_submissions_sheet_sync',?,?,?,?)""",
        (source_record_id, snapshot_hash, issue_class, detail[:500]),
    )


def import_grandfathered_awards(
    challenge: sqlite3.Connection,
    regular: sqlite3.Connection,
    members: sqlite3.Connection,
    config_id: int,
) -> dict[str, int]:
    placeholders = ",".join("?" for _ in EXPECTED_AWARDS)
    rows = regular.execute(
        f"""SELECT submission_id,member_id,rsn,discord_id,item_name,final_points,
                   submitted_at,source_type,external_id
              FROM regular_submissions
             WHERE status='approved' AND item_name IN ({placeholders})
             ORDER BY submission_id""",
        tuple(EXPECTED_AWARDS),
    ).fetchall()

    observed: dict[str, tuple[int, int]] = {}
    for label in EXPECTED_AWARDS:
        matching = [r for r in rows if r["item_name"] == label]
        observed[label] = (len(matching), sum(int(r["final_points"] or 0) for r in matching))
        _tier_key, expected_count, expected_points = EXPECTED_AWARDS[label]
        if observed[label] != (expected_count, expected_count * expected_points):
            raise RuntimeError(f"Historical award assertion failed for {label}: {observed[label]}")

    if len(rows) != 56 or sum(int(r["final_points"] or 0) for r in rows) != 7550:
        raise RuntimeError("Historical award total assertion failed")

    unresolved = 0
    for row in rows:
        member_id, subject_key, method = resolve_identity(row, members)
        if member_id is None:
            unresolved += 1
            continue
        tier_key, _expected_count, expected_points = EXPECTED_AWARDS[row["item_name"]]
        if int(row["final_points"] or 0) != expected_points:
            raise RuntimeError(f"Unexpected historical award amount for source row {row['submission_id']}")
        idempotency_key = f"grandfathered:regular_submissions:{row['submission_id']}"
        challenge.execute(
            """INSERT INTO challenge_tier_awards
               (subject_key,member_id,system_tier_key,eligibility_config_version_id,
                award_state,would_award_points,awarded_points,legacy_regular_submission_id,
                idempotency_key,historical_source,historical_timestamp,eligible_at)
               VALUES(?,?,?,?, 'grandfathered',0,?,?,?,?,?,?)
               ON CONFLICT(legacy_regular_submission_id) DO NOTHING""",
            (
                subject_key,
                member_id,
                tier_key,
                config_id,
                expected_points,
                int(row["submission_id"]),
                idempotency_key,
                f"RegularSubmissions.db:{row['source_type']}:{method}",
                row["submitted_at"],
                row["submitted_at"],
            ),
        )

    if unresolved:
        raise RuntimeError(f"Historical awards contain {unresolved} unresolved identities")

    totals = challenge.execute(
        """SELECT COUNT(*) AS rows,COUNT(DISTINCT subject_key||':'||system_tier_key) AS unique_pairs,
                  SUM(awarded_points) AS points,
                  SUM(CASE WHEN award_state='queued' THEN 1 ELSE 0 END) AS queued,
                  SUM(CASE WHEN award_state='delivered' THEN 1 ELSE 0 END) AS delivered
             FROM challenge_tier_awards WHERE award_state='grandfathered'"""
    ).fetchone()
    result = {k: int(totals[k] or 0) for k in totals.keys()}
    if result != {"rows": 56, "unique_pairs": 56, "points": 7550, "queued": 0, "delivered": 0}:
        raise RuntimeError(f"Grandfathered award reconciliation failed: {result}")
    return result


def source_payload(row: sqlite3.Row) -> dict[str, object]:
    keys = (
        "submission_id", "member_id", "rsn", "normalized_rsn", "discord_id",
        "item_name", "final_points", "category", "source_type", "screenshot_url",
        "notes", "status", "submitted_at", "reviewed_at", "reviewed_by", "external_id",
    )
    return {key: row[key] for key in keys}


def stable_ingest_fingerprint(row: sqlite3.Row, parsed: object) -> str:
    # Excludes mutable Google row position/external_id and local submission_id.
    stable = {
        "source_system": "google_clan_information_submissions",
        "discord_id": str(row["discord_id"] or "").strip(),
        "normalized_rsn": normalize_rsn(row["rsn"]),
        "boss_key": parsed.boss_key,
        "tier_key": parsed.tier_key,
        "item_name": str(row["item_name"] or "").strip(),
        "submitted_at": str(row["submitted_at"] or "").strip(),
        "metric_type": parsed.metric_type,
        "metric_value": parsed.metric_value,
        "party_key": parsed.party_key,
        "notes": str(row["notes"] or "").strip(),
        "evidence_url": str(row["screenshot_url"] or "").strip(),
        "source_submission_points": int(row["final_points"] or 0),
    }
    return sha256_text(canonical_json(stable))


def ingest_submissions(
    challenge: sqlite3.Connection,
    regular: sqlite3.Connection,
    members: sqlite3.Connection,
    config_id: int,
) -> dict[str, int]:
    catalog = evaluation_catalog(challenge,config_id)
    rows = regular.execute(
        """SELECT submission_id,member_id,rsn,normalized_rsn,discord_id,item_name,
                  final_points,category,source_type,screenshot_url,notes,status,
                  submitted_at,reviewed_at,reviewed_by,external_id
             FROM regular_submissions
            WHERE status='approved' AND source_type='sheet_sync' AND LOWER(category)='diary'
            ORDER BY submission_id"""
    ).fetchall()

    run_started = utc_now()
    watermark = max((int(r["submission_id"]) for r in rows), default=0)
    run_id = challenge.execute(
        "INSERT INTO challenge_ingest_runs(started_at,status,source_rows_observed,source_watermark) VALUES(?,'running',?,?)",
        (run_started, len(rows), watermark),
    ).lastrowid

    inserted = participants = unresolved = parse_failures = duplicate_fingerprints = 0
    observed_at = utc_now()
    for row in rows:
        parsed = parse_challenge(row["item_name"], row["notes"], catalog)
        payload = source_payload(row)
        payload_json = canonical_json(payload)
        payload_hash = sha256_text(payload_json)
        snapshot_hash = payload_hash
        source_record_id = f"regular_submissions:{row['submission_id']}"
        fingerprint = stable_ingest_fingerprint(row, parsed)
        member_id, subject_key, identity_method = resolve_identity(row, members)
        if member_id is None:
            unresolved += 1
            issue(challenge, source_record_id, snapshot_hash, "IDENTITY_UNRESOLVED", identity_method)
        for issue_class, detail in parsed.issues:
            issue(challenge, source_record_id, snapshot_hash, issue_class, detail)
        if parsed.parse_status == "failed":
            parse_failures += 1

        existing_revision = challenge.execute(
            """SELECT submission_id,source_snapshot_hash FROM challenge_submissions
               WHERE source_system='regular_submissions_sheet_sync' AND source_record_id=?
               ORDER BY submission_id DESC LIMIT 1""",
            (source_record_id,),
        ).fetchone()
        if existing_revision and existing_revision["source_snapshot_hash"] == snapshot_hash:
            continue

        supersedes = int(existing_revision["submission_id"]) if existing_revision else None
        record_state = "corrected" if supersedes else "approved"
        try:
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
                    "regular_submissions_sheet_sync", source_record_id, int(row["submission_id"]), row["external_id"],
                    snapshot_hash, fingerprint, supersedes, config_id,
                    parsed.boss_key, parsed.boss_display_name, parsed.tier_key, parsed.tier_rank, parsed.source_points,
                    parsed.metric_type, parsed.metric_value, parsed.metric_unit, parsed.metric_display,
                    parsed.party_key, None, row["discord_id"], parsed.approver_discord_id,
                    parsed.approver_display or row["reviewed_by"], row["screenshot_url"], row["notes"],
                    row["submitted_at"], row["reviewed_at"] or row["submitted_at"], observed_at,
                    payload_json, payload_hash, record_state, parsed.parse_status,
                ),
            )
        except sqlite3.IntegrityError as exc:
            if "ingest_fingerprint" not in str(exc):
                raise
            duplicate_fingerprints += 1
            issue(challenge, source_record_id, snapshot_hash, "DUPLICATE_FINGERPRINT_BLOCKED", "Stable source fingerprint already ingested")
            continue

        submission_id = int(cur.lastrowid)
        challenge.execute(
            """INSERT INTO challenge_submission_participants
               (submission_id,subject_key,member_id,discord_id,rsn_snapshot,
                normalized_rsn_snapshot,participant_role,identity_resolution_method)
               VALUES(?,?,?,?,?,?, 'submitter', ?)""",
            (submission_id,subject_key,member_id,row["discord_id"],row["rsn"],normalize_rsn(row["rsn"]),identity_method),
        )
        inserted += 1
        participants += 1

    challenge.execute(
        """UPDATE challenge_ingest_runs SET completed_at=?,status='complete',submissions_inserted=?,
                  participants_inserted=?,unresolved_identities=?,parse_failures=?,
                  duplicate_fingerprints_blocked=? WHERE ingest_run_id=?""",
        (utc_now(),inserted,participants,unresolved,parse_failures,duplicate_fingerprints,run_id),
    )
    return {
        "source_rows_observed": len(rows),
        "submissions_inserted": inserted,
        "participants_inserted": participants,
        "unresolved_identities": unresolved,
        "parse_failures": parse_failures,
        "duplicate_fingerprints_blocked": duplicate_fingerprints,
        "source_watermark": watermark,
    }


def calculate_current_progress(
    catalog: dict[str, object],
    boss_ranks: dict[str, int],
) -> tuple[int, int, int]:
    """Return active-only points, completed count, and Ascendant eligibility."""
    active_bosses = {
        key for key, boss in catalog["bosses"].items() if boss["active"]
    }
    points = sum(
        int(catalog["bosses"][boss]["tiers_by_rank"][rank]["progression_points"])
        for boss, rank in boss_ranks.items()
        if boss in active_bosses
        and rank in catalog["bosses"][boss]["tiers_by_rank"]
    )
    completed = sum(1 for boss in active_bosses if int(boss_ranks.get(boss, 0)) > 0)
    ascendant = int(completed == len(active_bosses) and bool(active_bosses))
    return points, completed, ascendant


def rebuild_derived(challenge: sqlite3.Connection, config_id: int) -> dict[str, int]:
    catalog = evaluation_catalog(challenge,config_id)
    active_submissions = challenge.execute(
        """SELECT s.*,p.subject_key,p.member_id,
                  COALESCE(c.corrected_metric_type,s.metric_type) AS effective_metric_type,
                  COALESCE(c.corrected_metric_value,s.metric_value) AS effective_metric_value,
                  COALESCE(c.corrected_metric_unit,s.metric_unit) AS effective_metric_unit,
                  COALESCE(c.corrected_metric_display,s.metric_display) AS effective_metric_display
             FROM challenge_submissions s
             JOIN challenge_submission_participants p ON p.submission_id=s.submission_id
             LEFT JOIN challenge_metric_parse_corrections c
               ON c.submission_id=s.submission_id AND c.correction_state='applied'
            WHERE s.boss_key IS NOT NULL AND s.earned_tier_rank IS NOT NULL
              AND s.record_state!='retracted'
              AND NOT EXISTS (SELECT 1 FROM challenge_submissions n WHERE n.supersedes_submission_id=s.submission_id)
            ORDER BY COALESCE(s.source_submitted_at,''),s.submission_id"""
    ).fetchall()

    for row in active_submissions:
        max_rank = int(row["earned_tier_rank"])
        boss_config = catalog["bosses"].get(str(row["boss_key"]))
        if not boss_config:
            continue
        for rank in range(1, max_rank + 1):
            tier_config = boss_config["tiers_by_rank"].get(rank)
            if not tier_config:
                continue
            tier_key = tier_config["tier_key"]
            basis = "explicit_submission" if rank == max_rank else "inferred_from_higher_tier"
            challenge.execute(
                """INSERT OR IGNORE INTO challenge_member_tier_achievements
                   (subject_key,member_id,boss_key,tier_key,tier_rank,first_qualifying_submission_id,
                    config_version_id,earned_at,achievement_basis)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (row["subject_key"],row["member_id"],row["boss_key"],tier_key,rank,row["submission_id"],config_id,row["source_submitted_at"],basis),
            )

    challenge.execute("DELETE FROM challenge_member_bests")
    best: dict[tuple[str,str,str], sqlite3.Row] = {}
    for row in active_submissions:
        if row["effective_metric_type"] not in ("time", "numeric") or row["effective_metric_value"] is None:
            continue
        key = (row["subject_key"],row["boss_key"],row["effective_metric_type"])
        current = best.get(key)
        boss_config = catalog["bosses"].get(str(row["boss_key"]))
        if not boss_config:
            continue
        direction = boss_config["comparison_direction"]
        if current is None or (direction == "lower" and row["effective_metric_value"] < current["effective_metric_value"]) or (direction == "higher" and row["effective_metric_value"] > current["effective_metric_value"]):
            best[key] = row
    calculated_at = utc_now()
    for (subject_key,boss_key,metric_type), row in best.items():
        challenge.execute(
            """INSERT INTO challenge_member_bests
               (subject_key,member_id,boss_key,metric_type,metric_value,metric_unit,metric_display,
                submission_id,evaluated_config_version_id,calculated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (subject_key,row["member_id"],boss_key,metric_type,row["effective_metric_value"],row["effective_metric_unit"],row["effective_metric_display"],row["submission_id"],config_id,calculated_at),
        )

    challenge.execute("DELETE FROM challenge_member_progress WHERE config_version_id=?", (config_id,))
    achievements = challenge.execute(
        """SELECT subject_key,MAX(member_id) AS member_id,boss_key,MAX(tier_rank) AS max_rank
             FROM challenge_member_tier_achievements GROUP BY subject_key,boss_key"""
    ).fetchall()
    by_subject: dict[str, dict[str, object]] = {}
    for row in achievements:
        entry = by_subject.setdefault(row["subject_key"], {"member_id": row["member_id"], "bosses": {}})
        if entry["member_id"] is None and row["member_id"] is not None:
            entry["member_id"] = row["member_id"]
        entry["bosses"][row["boss_key"]] = int(row["max_rank"])

    # A legacy baseline is a non-awarding floor.  It participates in current
    # display/progression but is never converted into an achievement row.
    for (subject_key, boss_key), baseline in legacy_baselines(challenge).items():
        entry = by_subject.setdefault(
            subject_key,
            {"member_id": baseline.get("member_id"), "bosses": {}},
        )
        if entry["member_id"] is None and baseline.get("member_id") is not None:
            entry["member_id"] = baseline["member_id"]
        entry["bosses"][boss_key] = max(
            int(entry["bosses"].get(boss_key, 0)),
            int(baseline["highest_tier_rank"]),
        )

    active_bosses = {key for key,boss in catalog["bosses"].items() if boss["active"]}
    active_boss_count = len(active_bosses)
    watermark = int(challenge.execute("SELECT COALESCE(MAX(legacy_regular_submission_id),0) FROM challenge_submissions").fetchone()[0])
    progress_rows = 0
    shadow_awards_created = 0
    system_rules = {str(row["system_tier_key"]): {
        "rank":int(row["tier_rank"]),"min":row["min_progression_points"],
        "all":bool(row["require_all_active_challenges"]),"bonus":int(row["one_time_rank_bonus"]),
    } for row in catalog["system_tiers"]}
    for subject_key, entry in by_subject.items():
        bosses = entry["bosses"]
        points, completed, ascendant = calculate_current_progress(catalog, bosses)
        complete = {key:int(ascendant if rule["all"] else points >= int(rule["min"] or 0))
                    for key,rule in system_rules.items()}
        current_key = None
        current_rank = 0
        for key in sorted(system_rules,key=lambda item:system_rules[item]["rank"]):
            if complete[key] and system_rules[key]["rank"] > current_rank:
                current_key, current_rank = key, system_rules[key]["rank"]
        calc_payload = {"subject_key":subject_key,"bosses":bosses,"points":points,"complete":complete,"watermark":watermark,"config_id":config_id}
        calc_hash = sha256_text(canonical_json(calc_payload))
        challenge.execute(
            """INSERT INTO challenge_member_progress
               (subject_key,member_id,config_version_id,challenge_progression_points,
                active_challenges_completed,active_challenge_count,current_system_tier_key,
                current_system_tier_rank,bronze_complete,silver_complete,gold_complete,
                platinum_complete,ascendant_eligible,source_watermark,calculation_hash,calculated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (subject_key,entry["member_id"],config_id,points,completed,active_boss_count,current_key,current_rank,
             complete.get("bronze",0),complete.get("silver",0),complete.get("gold",0),complete.get("platinum",0),complete.get("ascendant",0),watermark,calc_hash,calculated_at),
        )
        progress_rows += 1
        for tier_key, is_complete in complete.items():
            if not is_complete:
                continue
            eligible, _eligibility_reason = future_award_eligibility(
                challenge, subject_key, tier_key
            )
            if not eligible:
                continue
            rule = system_rules[tier_key]
            idempotency_key = f"challenge-system-tier:{subject_key}:{tier_key}"
            cur = challenge.execute(
                """INSERT OR IGNORE INTO challenge_tier_awards
                   (subject_key,member_id,system_tier_key,eligibility_config_version_id,
                    award_state,would_award_points,awarded_points,idempotency_key,eligible_at)
                   VALUES(?,?,?,?, 'shadow_eligible',?,0,?,?)""",
                (subject_key,entry["member_id"],tier_key,config_id,rule["bonus"],idempotency_key,calculated_at),
            )
            shadow_awards_created += max(0, cur.rowcount)

    return {
        "tier_achievements": int(challenge.execute("SELECT COUNT(*) FROM challenge_member_tier_achievements").fetchone()[0]),
        "personal_bests": len(best),
        "progress_rows": progress_rows,
        "shadow_awards_created": shadow_awards_created,
    }


def run(challenges_db: Path, regular_db: Path, members_db: Path, migrate: bool = True) -> dict[str, object]:
    challenge = rw_connection(challenges_db)
    regular = ro_connection(regular_db)
    members = ro_connection(members_db)
    try:
        config_id = migrate_schema(challenge) if migrate else int(challenge.execute("SELECT config_version_id FROM challenge_config_versions WHERE status='active'").fetchone()[0])
        challenge.execute("BEGIN IMMEDIATE")
        try:
            awards = import_grandfathered_awards(challenge, regular, members, config_id)
            ingestion = ingest_submissions(challenge, regular, members, config_id)
            derived = rebuild_derived(challenge, config_id)
            manual_reviews = ensure_award_reviews(challenge)
            direct_reconciliation = reconcile_direct_observations(challenge)
            audit_payload = {"awards": awards, "ingestion": ingestion, "derived": derived,
                             "manual_reviews": manual_reviews,
                             "direct_reconciliation": direct_reconciliation,
                             "config_version_id": config_id}
            audit_json = canonical_json(audit_payload)
            challenge.execute(
                """INSERT INTO challenge_audit_log
                   (event_type,actor_type,actor_id,entity_type,entity_id,reason,event_payload_json,event_payload_sha256)
                   VALUES('shadow_sync_completed','service','challenge_shadow_sync','ingest_run',NULL,
                          'Read-only source ingestion and shadow derivation',?,?)""",
                (audit_json,sha256_text(audit_json)),
            )
            challenge.commit()
        except Exception:
            challenge.rollback()
            raise
        return {"config_version_id":config_id,"awards":awards,"ingestion":ingestion,
                "derived":derived,"manual_reviews":manual_reviews,
                "direct_reconciliation":direct_reconciliation}
    finally:
        members.close()
        regular.close()
        challenge.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--challenges-db", type=Path, default=DEFAULT_CHALLENGES_DB)
    parser.add_argument("--regular-db", type=Path, default=DEFAULT_REGULAR_DB)
    parser.add_argument("--members-db", type=Path, default=DEFAULT_MEMBERS_DB)
    parser.add_argument("--no-migrate", action="store_true")
    args = parser.parse_args()
    try:
        result = run(args.challenges_db,args.regular_db,args.members_db,migrate=not args.no_migrate)
    except Exception as exc:
        print(json.dumps({"ok":False,"error":str(exc)},sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps({"ok":True,**result},sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
