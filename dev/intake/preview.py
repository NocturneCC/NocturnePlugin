"""Read-only inspection of received loot against Midgard's existing databases.

No network calls, writes, scoring approval, or inference of a complete group.
Run separately from the restricted public intake service, using sudo if needed.
"""
import argparse
from collections import Counter
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import json
import re
from pathlib import Path
import sqlite3

from intake import validate
from scoring import ordinary_stack_base_points


@contextmanager
def readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=3)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.create_function("norm_rsn", 1, normalize, deterministic=True)
        yield db
    finally:
        db.close()


def normalize(value):
    return "_".join(str(value or "").strip().lower().replace("-", " ").replace("_", " ").split())


def identity(db, rsn):
    rows = db.execute("""
        SELECT member_id, rsn AS primary_rsn, status, 1 AS account_active, 'primary' AS method
        FROM members WHERE norm_rsn(rsn)=?
        UNION ALL
        SELECT m.member_id, m.rsn, m.status, a.is_active, 'linked_account'
        FROM member_accounts a JOIN members m ON m.member_id=a.member_id
        WHERE norm_rsn(a.rsn)=?
    """, (normalize(rsn), normalize(rsn))).fetchall()
    ids = {row["member_id"] for row in rows}
    if not ids:
        return {"status": "unmatched"}
    if len(ids) > 1:
        return {"status": "ambiguous", "candidate_member_ids": sorted(ids)}
    active = [row for row in rows if row["status"] == "active" and row["account_active"] == 1]
    if not active:
        return {"status": "inactive", "member_id": next(iter(ids))}
    row = active[0]
    return {"status": "matched", "member_id": row["member_id"],
            "primary_rsn": row["primary_rsn"], "method": row["method"]}


def price_info(item, now, max_age_hours):
    price = item["latest_price"]
    if type(price) is not int or price < 0:
        return {"status": "price_unavailable", "unit_price_gp": None}
    info = {"unit_price_gp": price, "checked_at": item["latest_price_checked_at"]}
    try:
        raw_stamp = item["latest_price_checked_at"]
        stamp = datetime.fromisoformat(raw_stamp.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            # Existing app.py writes SQLite CURRENT_TIMESTAMP: UTC without an
            # explicit suffix. Never interpret this value as the host's local time.
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}", raw_stamp):
                raise ValueError("Unrecognized timestamp without timezone")
            stamp = stamp.replace(tzinfo=timezone.utc)
        age = now - stamp.timestamp()
        info["age_hours"] = round(age / 3600, 2)
        info["status"] = "current" if -120 <= age <= max_age_hours * 3600 else "price_stale"
    except (ValueError, TypeError, AttributeError, OverflowError):
        info["status"] = "price_timestamp_unavailable"
    return info


def inspect_item(items_db, submissions_db, event_id, stack, member, now, max_age_hours):
    item_id, quantity = stack["item_id"], stack["quantity"]
    external_id = f"runelite:{event_id}:{item_id}"
    result = {"osrs_item_id": item_id, "quantity": quantity, "external_id": external_id,
              "final_points": None, "would_insert": False}
    previous = submissions_db.execute(
        "SELECT submission_id, status FROM regular_submissions WHERE external_id=? LIMIT 2", (external_id,)
    ).fetchall()
    if previous:
        result.update(status="already_imported", existing_submissions=[dict(row) for row in previous])
        return result
    item = items_db.execute("SELECT * FROM items WHERE osrs_item_id=?", (item_id,)).fetchone()
    if item is None:
        result["status"] = "item_unknown"
        return result
    result["item_name"] = item["item_name"]
    if item["is_active"] != 1:
        result["status"] = "item_inactive"
        return result
    fixed = items_db.execute("""SELECT name, points, category FROM pet_kit_point_values
        WHERE is_active=1 AND (norm_rsn(name)=? OR normalized_name=?) LIMIT 2""",
        (normalize(item["item_name"]), item["normalized_item_name"])).fetchall()
    if fixed:
        # Do not silently use GP prices for known catalogue rewards. Capture type
        # and event applicability are not available in the current intake schema.
        result.update(status="fixed_reward_review", fixed_catalogue=[dict(row) for row in fixed], personal=True)
        return result
    price = price_info(item, now, max_age_hours)
    result["price"] = price
    if price["status"] != "current":
        result["status"] = price["status"]
        return result
    base = ordinary_stack_base_points(price["unit_price_gp"], quantity)
    result["base_points"] = base
    if base == 0:
        result.update(status="excluded_below_unit_threshold", final_points=0)
    elif member["status"] != "matched":
        result["status"] = "identity_review"
    else:
        result.update(status="needs_context", pending_checks=[
            "event eligibility", "group participation and eligible recipients",
            "manual-submission duplicate check", "unverified-report award policy"
        ])
    return result


def build_report(intake_path, database_dir, limit=10, max_age_hours=24, now=None):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be 1..100")
    if type(max_age_hours) is not int or not 1 <= max_age_hours <= 168:
        raise ValueError("max price age must be 1..168 hours")
    now = datetime.now(timezone.utc).timestamp() if now is None else now
    root = Path(database_dir)
    result = {"mode": "read_only_preview", "max_price_age_hours": max_age_hours,
              "writes": 0, "automatic_awards_enabled": False,
              "identity_note": "RSN matching establishes eligibility, not ownership or proof of a drop.",
              "events": []}
    with ExitStack() as stack:
        intake_db = stack.enter_context(readonly(intake_path))
        members_db = stack.enter_context(readonly(root / "Members.db"))
        items_db = stack.enter_context(readonly(root / "Items.db"))
        submissions_db = stack.enter_context(readonly(root / "RegularSubmissions.db"))
        rows = intake_db.execute("SELECT event_id, received_at, payload FROM test_drops ORDER BY received_at DESC, event_id LIMIT ?", (limit,)).fetchall()
        for row in rows:
            event = {"event_id": row["event_id"]}
            result["events"].append(event)
            try:
                data = json.loads(row["payload"])
                validate(data, row["received_at"])
                if data["event_id"] != row["event_id"]:
                    raise ValueError("ID mismatch")
            except (ValueError, TypeError, AttributeError, OverflowError, RecursionError):
                event["status"] = "invalid_record"
                continue
            event.update(rsn=data["rsn"], source=data["source"], occurred_at=data["occurred_at"])
            if data["source"].startswith("Synthetic "):
                event["status"] = "synthetic_test_excluded"
                continue
            member = identity(members_db, data["rsn"])
            event.update(status="inspected", member=member, items=[
                inspect_item(items_db, submissions_db, row["event_id"], item, member, now, max_age_hours)
                for item in data["items"]
            ])
    result["item_status_counts"] = dict(Counter(item["status"] for event in result["events"] for item in event.get("items", [])))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--intake-db", default="/var/lib/nocturne-plugin-dev/test-drops.sqlite3")
    parser.add_argument("--database-dir", default="/srv/projects/database")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--max-price-age-hours", type=int, default=24)
    args = parser.parse_args()
    try:
        result = build_report(args.intake_db, args.database_dir, args.limit, args.max_price_age_hours)
    except (sqlite3.Error, ValueError, OSError) as error:
        raise SystemExit(f"Preview stopped without writes: {error}")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
