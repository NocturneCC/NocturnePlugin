"""Preview or import qualifying RuneLite reports as pending submissions.

No rank totals or approvals are written. Only version 2/3 reports with an active
member match are eligible. Apply mode backs up RegularSubmissions.db first.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from preview import build_report, normalize, readonly
from scoring import capped_recipient_share, fixed_recipient_award


REQUIRED_COLUMNS = {
    "submission_id", "member_id", "rsn", "normalized_rsn", "discord_id",
    "item_id", "osrs_item_id", "item_name", "normalized_item_name",
    "item_price", "base_points", "multiplier", "final_points", "category",
    "source_type", "notes", "status", "submitted_at", "identity_match_method",
    "identity_match_notes", "identity_review_status", "external_id",
    "screenshot_url",
    "price_source", "valuation_rule_id", "valuation_catalogue_version",
    "finished_output_item_id", "finished_output_item_name",
    "finished_output_market_price_gp", "derived_unit_price_gp",
}

RUNELITE_SOURCE_TYPES = {"runelite", "runelite_pilot"}


def candidate(event, item, source_type="runelite"):
    if source_type not in RUNELITE_SOURCE_TYPES:
        raise ValueError("invalid RuneLite source type")
    if event.get("payload_version") not in (2, 3, 4) or event.get("member", {}).get("status") != "matched":
        return None
    price_source = item.get("price", {}).get("source")
    ordinary = item.get("status") == "needs_context" and price_source in {
        "runelite_client", "runelite_market", "runelite_derived_full_output", "runelite_derived_equal_share"
    }
    fixed = item.get("fixed_catalogue")
    fixed_reward = (item.get("status") == "fixed_reward_review"
                    and isinstance(fixed, list) and len(fixed) == 1)
    if not ordinary and not fixed_reward:
        return None
    matched_rsn = event["member"].get("matched_rsn") or event["rsn"]
    common = {
        "external_id": item["external_id"],
        "member_id": event["member"]["member_id"],
        "rsn": matched_rsn,
        "normalized_rsn": normalize(matched_rsn),
        "discord_id": event["member"].get("discord_id"),
        "item_id": item["catalogue_item_id"],
        "osrs_item_id": item["osrs_item_id"],
        "item_name": item["item_name"],
        "normalized_item_name": item["normalized_item_name"],
        "source_type": source_type,
        "status": "pending",
        "submitted_at": event["occurred_at"],
        "identity_match_method": "runelite_" + event["member"]["method"],
        "identity_match_notes": (
            f"Active {event['member']['method']} match to member {event['member']['member_id']}; "
            "client report is not proof of ownership or drop legitimacy."
        ),
        "identity_review_status": "eligible_unverified_report",
    }
    if ordinary:
        base = item["base_points"]
        price = item["price"]
        return dict(common, item_price=price["unit_price_gp"], price_source=price_source,
                    valuation_rule_id=price.get("valuation_rule_id"),
                    valuation_catalogue_version=price.get("valuation_catalogue_version"),
                    finished_output_item_id=price.get("finished_output_item_id"),
                    finished_output_item_name=price.get("finished_output_item_name"),
                    finished_output_market_price_gp=price.get("finished_output_market_price_gp"),
                    derived_unit_price_gp=price.get("derived_unit_price_gp"),
                    base_points=base, multiplier=1.0,
                    final_points=capped_recipient_share(base, 1),
                    category="drop", notes=(
                        f"RuneLite loot from {event['source']}; captured price. "
                        "Group and event context unresolved; points are a pending 1x proposal."
                    ))
    if fixed_reward:
        points = fixed[0]["points"]
        return dict(common, item_price=0, base_points=points, multiplier=1.0,
                    final_points=fixed_recipient_award(points),
                    category=fixed[0]["category"], notes=(
                        f"RuneLite loot from {event['source']}; fixed personal reward. "
                        "Event applicability remains pending review; never share this award."
                    ))
    return None


def plan(report):
    candidates = []
    excluded = {}
    for event in report["events"]:
        for item in event.get("items", []):
            proposed = candidate(event, item)
            if proposed is not None:
                candidates.append(proposed)
            else:
                status = item.get("status", event.get("status", "excluded"))
                excluded[status] = excluded.get(status, 0) + 1
    return candidates, excluded


def schema_columns(database):
    with readonly(database) as db:
        return {row[1] for row in db.execute("PRAGMA table_info(regular_submissions)")}


def backup_database(database):
    directory = Path(database).parent / "backups" / "nocturne-runelite-import"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    filename = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8] + ".sqlite3"
    destination = directory / filename
    destination.touch(mode=0o600, exist_ok=False)
    try:
        with readonly(database) as source:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination


def apply_candidates(database, candidates, backup=True, after_insert=None):
    missing = REQUIRED_COLUMNS - schema_columns(database)
    if missing:
        raise ValueError("regular_submissions schema missing: " + ", ".join(sorted(missing)))
    if not candidates:
        return {"inserted": 0, "duplicates": 0, "backup": None,
                "backup_policy": "not_needed_no_candidates"}
    backup_path = backup_database(database) if backup else None
    columns = [
        "member_id", "rsn", "normalized_rsn", "discord_id", "item_id",
        "osrs_item_id", "item_name", "normalized_item_name", "item_price",
        "base_points", "multiplier", "final_points", "category", "source_type",
        "notes", "status", "submitted_at", "identity_match_method",
        "identity_match_notes", "identity_review_status", "external_id",
        "screenshot_url",
        "price_source", "valuation_rule_id", "valuation_catalogue_version",
        "finished_output_item_id", "finished_output_item_name",
        "finished_output_market_price_gp", "derived_unit_price_gp",
    ]
    inserted = duplicates = 0
    db = sqlite3.connect(Path(database).resolve().as_uri() + "?mode=rw", uri=True, timeout=5)
    try:
        db.execute("BEGIN IMMEDIATE")
        for row in candidates:
            if db.execute("SELECT 1 FROM regular_submissions WHERE external_id=? LIMIT 1",
                          (row["external_id"],)).fetchone():
                duplicates += 1
                continue
            placeholders = ",".join("?" for _ in columns)
            cursor = db.execute(f"INSERT INTO regular_submissions ({','.join(columns)}) VALUES ({placeholders})",
                                tuple(row.get(name) for name in columns))
            if after_insert:
                after_insert(db, row, cursor.lastrowid)
            inserted += 1
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()
    return {"inserted": inserted, "duplicates": duplicates,
            "backup": str(backup_path) if backup_path else None,
            "backup_policy": "sqlite_backup" if backup else "disabled_for_pending_service"}


def run(intake, database_dir, limit, apply=False, backup=True):
    root = Path(database_dir)
    missing = REQUIRED_COLUMNS - schema_columns(root / "RegularSubmissions.db")
    if missing:
        raise ValueError("regular_submissions schema missing: " + ", ".join(sorted(missing)))
    report = build_report(intake, root, limit=limit)
    candidates, excluded = plan(report)
    result = {
        "mode": "apply_pending" if apply else "preview_pending",
        "automatic_awards_enabled": False,
        "rank_total_writes": 0,
        "candidate_count": len(candidates),
        "excluded": excluded,
        "candidates": candidates,
    }
    if apply:
        result.update(apply_candidates(root / "RegularSubmissions.db", candidates, backup=backup))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intake-db", default="/var/lib/nocturne-plugin-dev/test-drops.sqlite3")
    parser.add_argument("--database-dir", default="/srv/projects/database")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--no-backup", action="store_true",
                        help="Skip the large SQLite copy; intended only for the restricted pending-row service")
    args = parser.parse_args()
    try:
        if args.no_backup and not args.apply:
            raise ValueError("--no-backup requires --apply")
        result = run(args.intake_db, args.database_dir, args.limit,
                     args.apply, backup=not args.no_backup)
    except (OSError, sqlite3.Error, ValueError) as error:
        raise SystemExit(f"Pending import stopped: {error}")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
