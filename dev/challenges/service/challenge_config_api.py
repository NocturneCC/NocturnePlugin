"""Public read-only Challenge configuration and member progress API."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from flask import Blueprint, jsonify, request

from challenge_config import config_document
from challenge_member_view import (
    leaderboard_payload,
    member_progress_payload,
    resolve_member,
    search_members,
)


bp = Blueprint("challenge_config_public", __name__)
CHALLENGES_DB = Path("/srv/projects/database/Challenges.db")
MEMBERS_DB = Path("/srv/projects/database/Members.db")


@contextmanager
def _ro(path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        yield conn
    finally:
        conn.close()


@bp.get("/api/challenges/config/active")
def active_config():
    with _ro(CHALLENGES_DB) as conn:
        document = config_document(conn)
    payload = {"ok": True, **document}
    body = json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(",",":"))
    response = jsonify(payload)
    response.headers["ETag"] = '"' + hashlib.sha256(body.encode()).hexdigest() + '"'
    response.headers["Cache-Control"] = "public, max-age=60, must-revalidate"
    return response


def _public_response(payload: dict, status: int = 200):
    response = jsonify(payload)
    response.status_code = status
    response.headers["Cache-Control"] = "public, max-age=60, must-revalidate"
    return response


def _member_response(*, rsn: str | None = None, member_id: int | None = None):
    with _ro(MEMBERS_DB) as members:
        member = resolve_member(members, rsn=rsn, member_id=member_id)
    if not member:
        return _public_response({
            "ok": False, "found": False, "error": "member_not_found",
            "member": None, "bosses": [], "overall": None,
        }, 404)
    with _ro(CHALLENGES_DB) as challenge:
        payload = member_progress_payload(challenge, member)
    return _public_response(payload)


@bp.get("/api/challenges/member")
def member_challenges():
    raw_member_id = str(request.args.get("member_id") or "").strip()
    rsn = str(request.args.get("rsn") or "").strip()
    if raw_member_id:
        if not raw_member_id.isdigit():
            return _public_response({"ok": False, "error": "invalid_member_id"}, 400)
        return _member_response(member_id=int(raw_member_id))
    if not rsn:
        return _public_response({"ok": False, "error": "rsn_or_member_id_required"}, 400)
    return _member_response(rsn=rsn)


@bp.get("/api/challenges/member/<path:identifier>")
def member_challenges_path(identifier: str):
    value = str(identifier or "").strip()
    if not value:
        return _public_response({"ok": False, "error": "member_identifier_required"}, 400)
    return _member_response(member_id=int(value)) if value.isdigit() else _member_response(rsn=value)


@bp.get("/api/challenges/members/search")
def challenge_member_search():
    query = str(request.args.get("q") or "").strip()
    if not query:
        return _public_response({"ok": True, "members": []})
    with _ro(MEMBERS_DB) as members:
        results = search_members(members, query)
    return _public_response({"ok": True, "members": results})


@bp.get("/api/challenges/leaderboard")
def challenge_leaderboard():
    with _ro(CHALLENGES_DB) as challenge, _ro(MEMBERS_DB) as members:
        payload = leaderboard_payload(challenge, members)
    return _public_response(payload)
