"""Public, read-only Midgard leaderboard API.

This module deliberately opens both SQLite sources in immutable/query-only mode and
has no imports from the Google or Discord integrations.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from flask import Blueprint, jsonify

from leaderboard_proof_resolver import resolve_proof


CHALLENGES_DB = Path("/srv/projects/database/Challenges.db")
MEMBERS_DB = Path("/srv/projects/database/Members.db")

bp = Blueprint("leaderboards", __name__)


def _ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _normalize_rsn(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _active_version(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        """SELECT config_version_id
             FROM challenge_config_versions
            WHERE status='active'
            ORDER BY config_version_id DESC
            LIMIT 1"""
    ).fetchone()
    return int(row[0]) if row else None


def _mode_rows(conn: sqlite3.Connection, version_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT mv.config_version_id,mv.mode_key,mv.boss_key,mv.content_key,
                  mv.display_name,mv.is_active,mv.display_order,
                  COALESCE(mv.effective_display_order,mv.display_order) AS resolved_display_order,
                  mv.custom_order_override,mv.metric_type,
                  mv.comparison_direction,mv.metric_unit,mv.party_size_min,
                  mv.party_size_max,mv.top_n,mv.inherit_boss_icon,
                  mv.publication_group_key,mv.publication_group_name,
                  mv.publication_group_order,mv.group_icon_url,
                  CASE WHEN mv.inherit_boss_icon=1
                       THEN COALESCE(cb.icon_url,mv.icon_url)
                       ELSE mv.icon_url END AS icon_url
             FROM leaderboard_mode_versions mv
             LEFT JOIN challenge_config_bosses cb
               ON cb.config_version_id=mv.config_version_id
              AND cb.boss_key=mv.boss_key
            WHERE mv.config_version_id=?
            ORDER BY COALESCE(mv.effective_display_order,mv.display_order),
                     mv.display_name COLLATE NOCASE,mv.mode_key""",
        (version_id,),
    ).fetchall()


def _mode_payload(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "mode_key": str(row["mode_key"]),
        "display_name": str(row["display_name"]),
        "icon_url": row["icon_url"],
        "active": bool(row["is_active"]),
        "display_order": int(row["resolved_display_order"]),
        "metric_type": str(row["metric_type"]),
        "comparison_direction": str(row["comparison_direction"]),
        "metric_unit": str(row["metric_unit"]),
        "party_size_min": int(row["party_size_min"]),
        "party_size_max": int(row["party_size_max"]),
        "top_n": int(row["top_n"]),
        "boss_key": row["boss_key"],
        "content_key": str(row["content_key"]),
        "config_version_id": int(row["config_version_id"]),
        "publication_group_key": str(row["publication_group_key"]),
        "publication_group_name": str(row["publication_group_name"]),
        "publication_group_order": int(row["publication_group_order"]),
        "group_icon_url": row["group_icon_url"],
    }


def _member_directory(member_ids: set[int]) -> dict[int, dict[str, Any]]:
    if not member_ids:
        return {}
    placeholders = ",".join("?" for _ in member_ids)
    with closing(_ro(MEMBERS_DB)) as conn:
        rows = conn.execute(
            f"""SELECT member_id,rsn,COALESCE(NULLIF(display_name,''),rsn) display_name
                  FROM members WHERE member_id IN ({placeholders})""",
            tuple(sorted(member_ids)),
        ).fetchall()
    return {
        int(row["member_id"]): {
            "member_id": int(row["member_id"]),
            "primary_rsn": str(row["rsn"]),
            "display_name": str(row["display_name"]),
        }
        for row in rows
    }


def _participants_for(
    conn: sqlite3.Connection, observation_ids: list[int]
) -> dict[int, list[dict[str, Any]]]:
    if not observation_ids:
        return {}
    placeholders = ",".join("?" for _ in observation_ids)
    rows = conn.execute(
        f"""SELECT observation_id,participant_order,member_id,submitted_rsn,subject_key
              FROM leaderboard_observation_participants
             WHERE observation_id IN ({placeholders})
             ORDER BY observation_id,participant_order""",
        tuple(observation_ids),
    ).fetchall()
    member_ids = {int(row["member_id"]) for row in rows if row["member_id"] is not None}
    members = _member_directory(member_ids)
    result: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        member_id = int(row["member_id"]) if row["member_id"] is not None else None
        member = members.get(member_id) if member_id is not None else None
        public = {
            "member_id": member_id,
            "primary_rsn": member["primary_rsn"] if member else row["submitted_rsn"],
            "display_name": member["display_name"] if member else row["submitted_rsn"],
        }
        result.setdefault(int(row["observation_id"]), []).append(public)
    return result


def _resolve_member(value: str) -> dict[str, Any] | None:
    normalized = _normalize_rsn(value)
    with closing(_ro(MEMBERS_DB)) as conn:
        row = None
        if str(value).isdigit():
            row = conn.execute(
                """SELECT member_id,rsn,COALESCE(NULLIF(display_name,''),rsn) display_name
                     FROM members WHERE member_id=?""",
                (int(value),),
            ).fetchone()
        if row is None and normalized:
            row = conn.execute(
                """SELECT member_id,rsn,display_name FROM (
                     SELECT m.member_id,m.rsn,COALESCE(NULLIF(m.display_name,''),m.rsn) display_name,0 priority
                       FROM members m WHERE m.normalized_rsn=?
                     UNION ALL
                     SELECT m.member_id,m.rsn,COALESCE(NULLIF(m.display_name,''),m.rsn),1
                       FROM member_accounts a JOIN members m ON m.member_id=a.member_id
                      WHERE a.normalized_rsn=? AND COALESCE(a.is_active,1)=1
                     UNION ALL
                     SELECT m.member_id,m.rsn,COALESCE(NULLIF(m.display_name,''),m.rsn),2
                       FROM member_aliases a JOIN members m ON m.member_id=a.member_id
                      WHERE a.normalized_alias_rsn=?
                   ) ORDER BY priority LIMIT 1""",
                (normalized, normalized, normalized),
            ).fetchone()
    if row is None:
        return None
    return {
        "member_id": int(row["member_id"]),
        "primary_rsn": str(row["rsn"]),
        "display_name": str(row["display_name"]),
    }


def _entry(row: sqlite3.Row, participants: list[dict[str, Any]]) -> dict[str, Any]:
    proof = resolve_proof(
        row["proof_url"], row["source_rows_json"], int(row["observation_id"])
    )
    return {
        "rank": int(row["position"]),
        "observation_id": int(row["observation_id"]),
        "config_version_id": int(row["config_version_id"]),
        "participants": participants,
        "metric": {
            "type": str(row["metric_type"]),
            "normalized": int(row["metric_value"]),
            "unit": str(row["metric_unit"]),
            "display": str(row["metric_display"]),
        },
        "proof_url": proof["proof_url"],
        "proof_status": proof["proof_status"],
        "occurred_at": row["occurred_at"],
        "calculated_at": str(row["calculated_at"]),
    }


@bp.get("/api/leaderboards/modes")
def leaderboard_modes():
    with closing(_ro(CHALLENGES_DB)) as conn:
        version_id = _active_version(conn)
        if version_id is None:
            return jsonify({"ok": False, "error": "active_config_not_found"}), 503
        modes = [_mode_payload(row) for row in _mode_rows(conn, version_id)]
    return jsonify({"ok": True, "config_version_id": version_id, "modes": modes})


@bp.get("/api/leaderboards/<mode_key>")
def leaderboard(mode_key: str):
    with closing(_ro(CHALLENGES_DB)) as conn:
        version_id = _active_version(conn)
        if version_id is None:
            return jsonify({"ok": False, "error": "active_config_not_found"}), 503
        mode_row = next(
            (row for row in _mode_rows(conn, version_id) if row["mode_key"] == mode_key),
            None,
        )
        if mode_row is None or not bool(mode_row["is_active"]):
            return jsonify({"ok": False, "error": "leaderboard_mode_not_found"}), 404
        rows = conn.execute(
            """SELECT pb.position,o.observation_id,o.config_version_id,o.metric_type,
                      o.metric_value,o.metric_unit,o.metric_display,o.proof_url,
                      o.source_rows_json,
                      o.occurred_at,ir.imported_at AS calculated_at
                 FROM leaderboard_personal_bests pb
                 JOIN leaderboard_observations o ON o.observation_id=pb.observation_id
                 JOIN leaderboard_import_runs ir ON ir.import_run_id=o.import_run_id
                WHERE pb.mode_key=? AND pb.position IS NOT NULL AND pb.position<=?
                ORDER BY pb.position,pb.competitor_key""",
            (mode_key, int(mode_row["top_n"])),
        ).fetchall()
        participants = _participants_for(
            conn, [int(row["observation_id"]) for row in rows]
        )
        entries = [
            _entry(row, participants.get(int(row["observation_id"]), [])) for row in rows
        ]
        calculated_at = max((entry["calculated_at"] for entry in entries), default=None)
    return jsonify({
        "ok": True,
        "config_version_id": version_id,
        "mode": _mode_payload(mode_row),
        "top_n": int(mode_row["top_n"]),
        "last_calculated_at": calculated_at,
        "entries": entries,
    })


@bp.get("/api/leaderboards/player/<path:member>")
def player_leaderboards(member: str):
    resolved = _resolve_member(member)
    if resolved is None:
        return jsonify({"ok": False, "error": "member_not_found"}), 404
    member_id = int(resolved["member_id"])
    with closing(_ro(CHALLENGES_DB)) as conn:
        version_id = _active_version(conn)
        if version_id is None:
            return jsonify({"ok": False, "error": "active_config_not_found"}), 503
        modes = {str(row["mode_key"]): row for row in _mode_rows(conn, version_id)}
        rows = conn.execute(
            """WITH member_pbs AS (
                 SELECT pb.mode_key,pb.position,o.observation_id,o.config_version_id,
                        o.metric_type,o.metric_value,o.metric_unit,o.metric_display,
                        o.proof_url,o.source_rows_json,o.occurred_at,
                        ir.imported_at AS calculated_at,
                        ROW_NUMBER() OVER (
                          PARTITION BY pb.mode_key
                          ORDER BY pb.position,pb.metric_value,o.observation_id
                        ) choice
                   FROM leaderboard_personal_bests pb
                   JOIN leaderboard_observations o ON o.observation_id=pb.observation_id
                   JOIN leaderboard_observation_participants p
                     ON p.observation_id=o.observation_id
                   JOIN leaderboard_import_runs ir ON ir.import_run_id=o.import_run_id
                  WHERE p.member_id=? AND pb.position IS NOT NULL
               ) SELECT * FROM member_pbs WHERE choice=1""",
            (member_id,),
        ).fetchall()
        rows = [row for row in rows if row["mode_key"] in modes and bool(modes[row["mode_key"]]["is_active"])]
        rows.sort(key=lambda row: (int(modes[row["mode_key"]]["display_order"]), int(row["position"])))
        participants = _participants_for(
            conn, [int(row["observation_id"]) for row in rows]
        )
        personal_bests = []
        for row in rows:
            mode_row = modes[str(row["mode_key"])]
            item = _entry(row, participants.get(int(row["observation_id"]), []))
            item["mode"] = _mode_payload(mode_row)
            personal_bests.append(item)
        active_modes_total = sum(1 for row in modes.values() if bool(row["is_active"]))
    return jsonify({
        "ok": True,
        "config_version_id": version_id,
        "member": resolved,
        "modes_completed": len(personal_bests),
        "active_modes_total": active_modes_total,
        "personal_bests": personal_bests,
    })
