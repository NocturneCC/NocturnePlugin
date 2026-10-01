"""Read-only member Challenge projections built from Midgard-derived state."""

from __future__ import annotations

import re
import sqlite3
from typing import Any

from challenge_config import config_document
from challenge_legacy_baseline import legacy_baselines


def normalize_rsn(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def format_time_ms(value: int) -> str:
    milliseconds = max(0, int(value))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, remainder = divmod(remainder, 1_000)
    centiseconds = remainder // 10
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"


def resolve_member(
    members: sqlite3.Connection,
    *,
    rsn: str | None = None,
    member_id: int | None = None,
) -> dict[str, Any] | None:
    if member_id is not None:
        row = members.execute(
            """SELECT member_id,rsn,COALESCE(NULLIF(display_name,''),rsn) AS display_name,status
                 FROM members WHERE member_id=?""",
            (int(member_id),),
        ).fetchone()
    else:
        normalized = normalize_rsn(rsn or "")
        if not normalized:
            return None
        row = members.execute(
            """SELECT member_id,rsn,display_name,status FROM (
                 SELECT m.member_id,m.rsn,COALESCE(NULLIF(m.display_name,''),m.rsn) display_name,
                        m.status,0 priority
                   FROM members m WHERE m.normalized_rsn=?
                 UNION ALL
                 SELECT m.member_id,m.rsn,COALESCE(NULLIF(m.display_name,''),m.rsn),m.status,1
                   FROM member_accounts a JOIN members m ON m.member_id=a.member_id
                  WHERE a.normalized_rsn=? AND COALESCE(a.is_active,1)=1
                 UNION ALL
                 SELECT m.member_id,m.rsn,COALESCE(NULLIF(m.display_name,''),m.rsn),m.status,2
                   FROM member_aliases a JOIN members m ON m.member_id=a.member_id
                  WHERE a.normalized_alias_rsn=?
               ) ORDER BY priority LIMIT 1""",
            (normalized, normalized, normalized),
        ).fetchone()
    if not row:
        return None
    return {
        "member_id": int(row["member_id"]),
        "display_name": str(row["display_name"] or row["rsn"]),
        "primary_rsn": str(row["rsn"]),
        "rsn": str(row["rsn"]),
    }


def search_members(members: sqlite3.Connection, query: str, limit: int = 12) -> list[dict[str, Any]]:
    normalized = normalize_rsn(query)
    if not normalized:
        return []
    like = f"%{normalized}%"
    rows = members.execute(
        """SELECT DISTINCT m.member_id,m.rsn,COALESCE(NULLIF(m.display_name,''),m.rsn) display_name
             FROM members m
             LEFT JOIN member_accounts a ON a.member_id=m.member_id AND COALESCE(a.is_active,1)=1
             LEFT JOIN member_aliases x ON x.member_id=m.member_id
            WHERE COALESCE(m.status,'active')!='left'
              AND (m.normalized_rsn LIKE ? OR a.normalized_rsn LIKE ? OR x.normalized_alias_rsn LIKE ?)
            ORDER BY CASE WHEN m.normalized_rsn=? THEN 0 ELSE 1 END,m.rsn COLLATE NOCASE
            LIMIT ?""",
        (like, like, like, normalized, max(1, min(int(limit), 20))),
    ).fetchall()
    return [{
        "member_id": int(row["member_id"]),
        "display_name": str(row["display_name"] or row["rsn"]),
        "primary_rsn": str(row["rsn"]),
    } for row in rows]


def _pb_for(boss: dict[str, Any], best: dict[str, Any] | None, highest_rank: int) -> dict[str, Any] | None:
    metric = str(boss["metric_type"])
    if metric == "completion":
        if highest_rank < 1:
            return None
        return {
            "display": "Completed", "normalized": 1, "unit": "boolean", "metric_type": metric,
            "metric_display": "Completed", "metric_value": 1, "metric_unit": "boolean",
        }
    if not best or best.get("metric_value") is None:
        return None
    value = int(best["metric_value"])
    display = format_time_ms(value) if metric == "time" else str(value)
    return {
        "display": display,
        "normalized": value,
        "unit": best.get("metric_unit"),
        "metric_type": metric,
        "metric_display": display,
        "metric_value": value,
        "metric_unit": best.get("metric_unit"),
        "calculated_at": best.get("calculated_at"),
    }


def build_boss_projection(
    config: dict[str, Any],
    achievements: dict[tuple[str, str], dict[str, Any]],
    bests: dict[tuple[str, str], dict[str, Any]],
    baselines: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    baselines = baselines or {}
    projected: list[dict[str, Any]] = []
    for boss in config["bosses"]:
        if not boss["active"]:
            continue
        boss_key = str(boss["boss_key"])
        earned_for_boss = {
            key[1]: row for key, row in achievements.items() if key[0] == boss_key
        }
        evidence_rank = max(
            (int(row["tier_rank"]) for row in earned_for_boss.values()),
            default=0,
        )
        baseline = baselines.get((boss_key, str(boss["metric_type"]))) or baselines.get((boss_key, ""))
        baseline_rank = int(baseline["highest_tier_rank"]) if baseline else 0
        highest_rank = max(evidence_rank, baseline_rank)
        highest_config = next(
            (tier for tier in boss["tiers"] if int(tier["rank"]) == highest_rank),
            None,
        )
        tiers = []
        for tier in boss["tiers"]:
            evidence_earned = str(tier["tier_key"]) in earned_for_boss
            baseline_earned = baseline_rank >= int(tier["rank"])
            earned = evidence_earned or baseline_earned
            achievement = earned_for_boss.get(str(tier["tier_key"]))
            tiers.append({
                "tier_key": tier["tier_key"],
                "tier": tier["tier"],
                "rank": int(tier["rank"]),
                "earned": earned,
                "is_highest": earned and int(tier["rank"]) == highest_rank,
                "earned_at": achievement.get("earned_at") if achievement else None,
                "evidence_backed": evidence_earned,
                "achievement_source": "immutable_submission" if evidence_earned else (
                    "legacy_baseline" if baseline_earned else None
                ),
                "requirement": {
                    "display": tier["threshold_display"],
                    "normalized": tier["threshold"],
                    "metric_type": tier["metric_type"],
                    "operator": tier["operator"],
                    "unit": tier["unit"],
                },
                "submission_points": int(tier["points"]),
                "progression_points": int(tier["progression_points"]),
                "discord_emoji": tier.get("discord_emoji"),
            })
        progression_points = int(highest_config["progression_points"]) if highest_config else 0
        best = bests.get((boss_key, str(boss["metric_type"])))
        if not best and baseline and baseline.get("metric_value") is not None:
            best = baseline
        pb = _pb_for(boss, best, highest_rank)
        projected.append({
            "boss_key": boss_key,
            "display_name": boss["display_name"],
            "active": True,
            "display_order": int(boss["display_order"]),
            "metric_type": boss["metric_type"],
            "comparison_direction": boss["comparison_direction"],
            "description": boss.get("description"),
            "help_text": boss.get("help_text"),
            "icon_url": boss.get("icon_url"),
            "discord_emoji": boss.get("discord_emoji"),
            "pb": pb,
            "personal_best": pb,
            "highest_tier": highest_config["tier"] if highest_config else None,
            "highest_tier_key": highest_config["tier_key"] if highest_config else None,
            "highest_tier_rank": highest_rank,
            "highest_tier_source": (
                "legacy_baseline" if baseline_rank > evidence_rank else
                "immutable_submission" if evidence_rank else None
            ),
            "progression_points": progression_points,
            "complete": highest_rank > 0,
            "tiers": tiers,
        })
    return projected


def build_overall(config: dict[str, Any], bosses: list[dict[str, Any]]) -> dict[str, Any]:
    points = sum(int(boss["progression_points"]) for boss in bosses)
    completed = sum(1 for boss in bosses if boss["complete"])
    total = len(bosses)
    missing = [{"boss_key": boss["boss_key"], "display_name": boss["display_name"]}
               for boss in bosses if not boss["complete"]]
    statuses = []
    current = None
    next_tier = None
    for rule in sorted(config["system_tiers"], key=lambda item: int(item["tier_rank"])):
        requires_all = bool(rule["require_all_active_challenges"])
        threshold = rule["min_progression_points"]
        complete = completed == total and total > 0 if requires_all else points >= int(threshold or 0)
        status = {
            "tier_key": rule["system_tier_key"],
            "display_name": rule["display_name"],
            "rank": int(rule["tier_rank"]),
            "complete": complete,
            "requires_all_active_challenges": requires_all,
            "threshold_points": None if threshold is None else int(threshold),
            "points_remaining": None if requires_all else max(0, int(threshold or 0) - points),
            "percentage": None if requires_all else min(100.0, round(points * 100 / max(1, int(threshold or 0)), 1)),
            "missing_bosses": missing if requires_all and not complete else [],
        }
        status["remaining_points"] = status["points_remaining"]
        status["required_points"] = status["threshold_points"]
        status["remaining_challenges"] = len(missing) if requires_all else None
        statuses.append(status)
        if complete:
            current = {key: status[key] for key in ("tier_key", "display_name", "rank")}
        elif next_tier is None:
            next_tier = status
    counts = {str(tier["system_tier_key"]): 0 for tier in config["system_tiers"]}
    for boss in bosses:
        key = boss.get("highest_tier_key")
        if key in counts:
            counts[key] += 1
    return {
        "progression_points": points,
        "challenge_progression_points": points,
        "current_tier": current,
        "current_system_tier_key": current["tier_key"] if current else None,
        "current_system_tier_rank": current["rank"] if current else 0,
        "next_tier": next_tier,
        "active_bosses_completed": completed,
        "active_bosses_total": total,
        "active_challenge_count": total,
        "ascendant_requirements_met": not missing and total > 0,
        "ascendant_eligible": int(not missing and total > 0),
        "ascendant_missing_bosses": missing,
        "highest_tier_counts": counts,
        "system_tiers": statuses,
    }


def member_progress_payload(
    challenge: sqlite3.Connection,
    member: dict[str, Any],
) -> dict[str, Any]:
    config = config_document(challenge)
    subject = f"member:{int(member['member_id'])}"
    achievements = {
        (str(row["boss_key"]), str(row["tier_key"])): dict(row)
        for row in challenge.execute(
            """SELECT boss_key,tier_key,tier_rank,earned_at,achievement_basis,config_version_id
                 FROM challenge_member_tier_achievements WHERE subject_key=?""",
            (subject,),
        )
    }
    bests = {
        (str(row["boss_key"]), str(row["metric_type"])): dict(row)
        for row in challenge.execute(
            """SELECT boss_key,metric_type,metric_value,metric_unit,metric_display,calculated_at
                 FROM challenge_member_bests WHERE subject_key=?""",
            (subject,),
        )
    }
    subject_baselines = {
        (boss_key, str(row.get("metric_type") or "")): row
        for (baseline_subject, boss_key), row in legacy_baselines(challenge).items()
        if baseline_subject == subject
    }
    bosses = build_boss_projection(config, achievements, bests, subject_baselines)
    overall = build_overall(config, bosses)
    cached = challenge.execute(
        """SELECT calculated_at FROM challenge_member_progress
            WHERE subject_key=? AND config_version_id=?""",
        (subject, config["version_id"]),
    ).fetchone()
    overall["calculated_at"] = cached["calculated_at"] if cached else None
    return {
        "ok": True,
        "found": True,
        "member": member,
        "config_version_id": int(config["version_id"]),
        "config_published_at": config["published_at"],
        "overall": overall,
        "bosses": bosses,
    }


def leaderboard_payload(
    challenge: sqlite3.Connection,
    members: sqlite3.Connection,
) -> dict[str, Any]:
    config = config_document(challenge)
    identities = {int(row["member_id"]): {
        "member_id": int(row["member_id"]),
        "display_name": str(row["display_name"] or row["rsn"]),
        "primary_rsn": str(row["rsn"]),
    } for row in members.execute(
        """SELECT member_id,rsn,COALESCE(NULLIF(display_name,''),rsn) display_name
             FROM members WHERE COALESCE(status,'active')!='left'"""
    )}
    entries = []
    rows = challenge.execute(
        """SELECT member_id,challenge_progression_points,active_challenges_completed,
                  active_challenge_count,current_system_tier_key,current_system_tier_rank
             FROM challenge_member_progress
            WHERE config_version_id=? AND member_id IS NOT NULL""",
        (config["version_id"],),
    )
    tier_names = {str(row["system_tier_key"]): str(row["display_name"])
                  for row in config["system_tiers"]}
    for row in rows:
        identity = identities.get(int(row["member_id"]))
        if not identity:
            continue
        key = row["current_system_tier_key"]
        entries.append({
            "member": identity,
            "progression_points": int(row["challenge_progression_points"]),
            "current_tier_key": key,
            "current_tier": tier_names.get(str(key)) if key else None,
            "current_tier_rank": int(row["current_system_tier_rank"]),
            "bosses_completed": int(row["active_challenges_completed"]),
            "active_bosses_total": int(row["active_challenge_count"]),
        })
    entries.sort(key=lambda item: (
        -item["progression_points"], -item["bosses_completed"],
        item["member"]["primary_rsn"].casefold(),
    ))
    for index, entry in enumerate(entries, 1):
        entry["position"] = index
    return {"ok": True, "config_version_id": int(config["version_id"]), "entries": entries}
