"""Versioned, bounded CoX raid-presence pilot storage. No award side effects."""
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from uuid import UUID

VERSION = 1
TTL_SECONDS = 2 * 60 * 60
START_BUCKET_SECONDS = 5 * 60
COMPLETION_SKEW_SECONDS = 120
RAIDS = {"COX", "COX_CM"}
STATES = {"entry", "heartbeat", "completion", "reward_observed"}
MODES = {"SOLO_PERSONAL_ONLY", "NORMAL_GROUP", "MASS_PERSONAL_ONLY", "INVALID"}
RSN = re.compile(r"[A-Za-z0-9 _-]{1,12}\Z")


def schema(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS raid_presence_sessions(
      fingerprint TEXT PRIMARY KEY, raid_type TEXT NOT NULL, world INTEGER NOT NULL,
      party_holder INTEGER NOT NULL, start_bucket INTEGER NOT NULL,
      created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS raid_presence_checkins(
      fingerprint TEXT NOT NULL, rsn TEXT NOT NULL, member_key TEXT NOT NULL,
      raid_epoch TEXT NOT NULL, state TEXT NOT NULL, observed_at INTEGER NOT NULL,
      max_scale INTEGER NOT NULL, final_party_size INTEGER,
      final_personal_points INTEGER, final_team_points INTEGER,
      contribution_bp INTEGER, scoring_mode TEXT NOT NULL,
      completion_at INTEGER, reward_observed_at INTEGER, payload_hash TEXT NOT NULL,
      PRIMARY KEY(fingerprint,rsn),
      FOREIGN KEY(fingerprint) REFERENCES raid_presence_sessions(fingerprint) ON DELETE CASCADE);
    CREATE TABLE IF NOT EXISTS raid_presence_receipts(
      checkin_id TEXT PRIMARY KEY, payload_hash TEXT NOT NULL, fingerprint TEXT NOT NULL,
      received_at INTEGER NOT NULL,
      FOREIGN KEY(fingerprint) REFERENCES raid_presence_sessions(fingerprint) ON DELETE CASCADE);
    CREATE INDEX IF NOT EXISTS raid_presence_expiry ON raid_presence_sessions(expires_at);
    """)


def _integer(data, name, low, high, optional=False):
    value = data.get(name)
    if optional and value is None:
        return None
    if type(value) is not int or not low <= value <= high:
        raise ValueError("invalid " + name)
    return value


def validate(data, now):
    required = {"presence_version", "checkin_id", "rsn", "raid_type", "state", "world",
                "party_group_holder", "raid_epoch", "raid_started_at", "observed_at",
                "max_scale", "final_party_size", "final_personal_points", "final_team_points",
                "contribution_basis_points", "proposed_scoring_mode", "completion_at",
                "reward_observed_at"}
    if not isinstance(data, dict) or set(data) != required or data.get("presence_version") != VERSION:
        raise ValueError("invalid fields")
    if str(UUID(data["checkin_id"])) != data["checkin_id"] or str(UUID(data["raid_epoch"])) != data["raid_epoch"]:
        raise ValueError("invalid UUID")
    if not isinstance(data["rsn"], str) or not RSN.fullmatch(data["rsn"]):
        raise ValueError("invalid RSN")
    rsn = " ".join(data["rsn"].replace("_", " ").lower().split())
    if data["raid_type"] not in RAIDS or data["state"] not in STATES or data["proposed_scoring_mode"] not in MODES:
        raise ValueError("invalid enum")
    world = _integer(data, "world", 1, 3000)
    holder = _integer(data, "party_group_holder", -1, 2**31 - 1)
    started = _integer(data, "raid_started_at", 0, 2**63 - 1)
    observed = _integer(data, "observed_at", 0, 2**63 - 1)
    if abs(now - observed) > 120 or observed < started or observed - started > TTL_SECONDS:
        raise ValueError("invalid timing")
    maximum = _integer(data, "max_scale", 0, 100)
    expected_mode = ("SOLO_PERSONAL_ONLY" if maximum == 1 else
                     "NORMAL_GROUP" if 2 <= maximum <= 20 else
                     "MASS_PERSONAL_ONLY" if maximum > 20 else "INVALID")
    if data["proposed_scoring_mode"] != expected_mode:
        raise ValueError("inconsistent scoring mode")
    size = _integer(data, "final_party_size", 1, 100, True)
    personal = _integer(data, "final_personal_points", 0, 2**31 - 1, True)
    team = _integer(data, "final_team_points", 1, 2**31 - 1, True)
    basis = _integer(data, "contribution_basis_points", 0, 10000, True)
    completion = _integer(data, "completion_at", 0, 2**63 - 1, True)
    reward = _integer(data, "reward_observed_at", 0, 2**63 - 1, True)
    if data["state"] in {"completion", "reward_observed"}:
        if None in (size, personal, team, basis, completion) or abs(completion - observed) > 120:
            raise ValueError("missing completion fields")
        calculated = min(10000, personal * 10000 // team)
        if calculated != basis or maximum < size:
            raise ValueError("inconsistent completion")
        if expected_mode in {"SOLO_PERSONAL_ONLY", "MASS_PERSONAL_ONLY"} and personal <= 0:
            raise ValueError("invalid personal-only contribution")
    elif any(value is not None for value in (size, personal, team, basis, completion, reward)):
        raise ValueError("premature completion fields")
    bucket = started // START_BUCKET_SECONDS
    material = f"{data['raid_type']}|{world}|{holder}|{bucket}"
    fingerprint = hashlib.sha256(material.encode()).hexdigest()
    canonical = json.dumps(dict(data, rsn=rsn), sort_keys=True, separators=(",", ":"))
    return rsn, fingerprint, canonical


def process(db_path, data, identity_resolver, now=None):
    now = int(datetime.now(timezone.utc).timestamp()) if now is None else int(now)
    rsn, fingerprint, canonical = validate(data, now)
    member_key = identity_resolver(rsn)
    if member_key is None:
        raise PermissionError("identity not uniquely eligible")
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    with sqlite3.connect(db_path) as db:
        db.execute("PRAGMA foreign_keys=ON")
        schema(db)
        db.execute("DELETE FROM raid_presence_sessions WHERE expires_at < ?", (now,))
        receipt = db.execute("SELECT payload_hash,fingerprint FROM raid_presence_receipts WHERE checkin_id=?",
                             (data["checkin_id"],)).fetchone()
        if receipt:
            if receipt != (digest, fingerprint):
                raise ValueError("check-in replay collision")
            return _result(db, fingerprint, duplicate=True)
        started = data["raid_started_at"] // START_BUCKET_SECONDS
        db.execute("INSERT OR IGNORE INTO raid_presence_sessions VALUES(?,?,?,?,?,?,?)",
                   (fingerprint, data["raid_type"], data["world"], data["party_group_holder"],
                    started, now, now + TTL_SECONDS))
        existing = db.execute("SELECT raid_epoch,final_party_size,final_team_points,completion_at "
                              "FROM raid_presence_checkins WHERE fingerprint=? AND rsn=?",
                              (fingerprint, rsn)).fetchone()
        incoming = (data["raid_epoch"], data["final_party_size"], data["final_team_points"], data["completion_at"])
        if existing and existing[0] != incoming[0]:
            raise ValueError("adjacent raid collision")
        if existing and existing[1] is not None and incoming[1] is not None and existing[1:] != incoming[1:]:
            raise ValueError("completion changed")
        db.execute("""INSERT INTO raid_presence_checkins VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
          ON CONFLICT(fingerprint,rsn) DO UPDATE SET
          state=CASE
            WHEN state='reward_observed' THEN state
            WHEN state='completion' AND excluded.state IN ('entry','heartbeat') THEN state
            ELSE excluded.state END,
          observed_at=max(observed_at,excluded.observed_at),max_scale=max(max_scale,excluded.max_scale),
          final_party_size=coalesce(excluded.final_party_size,final_party_size),
          final_personal_points=coalesce(excluded.final_personal_points,final_personal_points),
          final_team_points=coalesce(excluded.final_team_points,final_team_points),
          contribution_bp=coalesce(excluded.contribution_bp,contribution_bp),
          scoring_mode=excluded.scoring_mode,completion_at=coalesce(excluded.completion_at,completion_at),
          reward_observed_at=coalesce(excluded.reward_observed_at,reward_observed_at),payload_hash=excluded.payload_hash""",
                   (fingerprint, rsn, str(member_key), data["raid_epoch"], data["state"], data["observed_at"],
                    data["max_scale"], data["final_party_size"], data["final_personal_points"],
                    data["final_team_points"], data["contribution_basis_points"], data["proposed_scoring_mode"],
                    data["completion_at"], data["reward_observed_at"], digest))
        db.execute("INSERT INTO raid_presence_receipts VALUES(?,?,?,?)",
                   (data["checkin_id"], digest, fingerprint, now))
        return _result(db, fingerprint, duplicate=False)


def _result(db, fingerprint, duplicate):
    rows = db.execute("SELECT rsn,state,final_party_size,final_team_points,contribution_bp,scoring_mode,completion_at "
                      "FROM raid_presence_checkins WHERE fingerprint=?", (fingerprint,)).fetchall()
    completed = [row for row in rows if row[1] in {"completion", "reward_observed"}]
    sizes = {row[2] for row in completed}
    totals = {row[3] for row in completed}
    times = [row[6] for row in completed]
    consistent = bool(completed) and len(sizes) == len(totals) == 1 and None not in sizes | totals \
        and None not in times and max(times) - min(times) <= COMPLETION_SKEW_SECONDS
    expected = next(iter(sizes)) if consistent else 0
    complete = consistent and len(completed) == expected and len({row[0] for row in completed}) == expected
    mode = completed[0][5] if consistent else "INVALID"
    contributions = all(row[4] is not None and row[4] >= 500 for row in completed)
    same_mode = len({row[5] for row in completed}) == 1
    qualifies = complete and same_mode and mode == "NORMAL_GROUP" and contributions
    if not consistent:
        reason = "inconsistent completion evidence"
    elif not complete:
        reason = "not all Nocturne clients verified"
    elif not same_mode:
        reason = "inconsistent proposed scoring mode"
    elif mode != "NORMAL_GROUP":
        reason = "personal-only scoring mode"
    elif not contributions:
        reason = "one or more self-reporters are below five percent"
    else:
        reason = "all Nocturne clients verified"
    return {"status": "duplicate" if duplicate else "stored", "presence_version": VERSION,
            "fingerprint": fingerprint, "verified": len(completed), "expected": expected,
            "consistent": consistent, "group_qualified": qualifies,
            "personal_only": mode in {"SOLO_PERSONAL_ONLY", "MASS_PERSONAL_ONLY"},
            "reason": reason, "automatic_awards_enabled": False, "point_writes": 0}
