"""Versioned Plugin Tracking activity catalog, independent of Challenge rows."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any

KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_GROUP_SIZE = 10
CATALOG_TABLES = ("plugin_tracking_catalog_versions", "plugin_tracking_catalog_drafts",
                  "plugin_tracking_observations", "plugin_tracking_observation_participants")


def default_document() -> dict[str, Any]:
    return {"schema_version": 1, "activities": [
        {"activity_key": "phosanis_nightmare", "display_name": "Phosani's Nightmare",
         "aliases": ["Phosani", "Phosanis"], "modes": [{
             "mode_key": "phosani_nightmare", "display_name": "Solo",
             "source_names": ["Phosani's Nightmare", "Phosani’s Nightmare"],
             "supports_solo": True, "supports_group": False,
             "loot_tracking_enabled": True, "ttk_tracking_enabled": True,
             "timing_scope": "overall", "challenge_boss_key": "phosanis",
             "challenge_mode_key": "phosanis_nightmare",
             "challenge_content_key": "phosanis_nightmare"}]},
        {"activity_key": "corrupted_gauntlet", "display_name": "Corrupted Gauntlet",
         "aliases": ["CG", "Gauntlet"], "modes": [{
             "mode_key": "corrupted_gauntlet", "display_name": "Corrupted",
             "source_names": ["Corrupted Gauntlet", "The Corrupted Gauntlet"],
             "supports_solo": True, "supports_group": False,
             "loot_tracking_enabled": True, "ttk_tracking_enabled": True,
             "timing_scope": "overall", "challenge_boss_key": "gauntlet",
             "challenge_mode_key": "corrupted_gauntlet",
             "challenge_content_key": "corrupted_gauntlet"}]},
        {"activity_key": "nightmare", "display_name": "The Nightmare",
         "aliases": ["Nightmare"], "modes": [{
             "mode_key": "nightmare", "display_name": "Regular",
             "source_names": ["The Nightmare", "Nightmare"],
             "supports_solo": True, "supports_group": True,
             "loot_tracking_enabled": True, "ttk_tracking_enabled": True,
             "timing_scope": "overall", "challenge_boss_key": None,
             "challenge_mode_key": None, "challenge_content_key": None}]},
    ]}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_document(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema_version", "activities"}:
        raise ValueError("catalog_schema_invalid")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError("catalog_schema_unsupported")
    activities = value["activities"]
    if not isinstance(activities, list) or not 1 <= len(activities) <= 300:
        raise ValueError("catalog_activities_invalid")
    normalized = []
    activity_keys: set[str] = set()
    source_names: set[str] = set()
    for activity in activities:
        if not isinstance(activity, dict) or set(activity) != {"activity_key", "display_name", "aliases", "modes"}:
            raise ValueError("catalog_activity_schema_invalid")
        key, name = activity["activity_key"], activity["display_name"]
        if not isinstance(key, str) or not KEY_RE.fullmatch(key) or key in activity_keys:
            raise ValueError("catalog_activity_key_invalid")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
            raise ValueError("catalog_display_name_invalid")
        activity_keys.add(key)
        aliases = _names(activity["aliases"], allow_empty=True)
        modes = activity["modes"]
        if not isinstance(modes, list) or not 1 <= len(modes) <= 20:
            raise ValueError("catalog_modes_invalid")
        mode_keys: set[str] = set()
        clean_modes = []
        for mode in modes:
            expected = {"mode_key", "display_name", "source_names", "supports_solo",
                        "supports_group", "loot_tracking_enabled", "ttk_tracking_enabled",
                        "timing_scope", "challenge_boss_key", "challenge_mode_key",
                        "challenge_content_key"}
            if not isinstance(mode, dict) or set(mode) != expected:
                raise ValueError("catalog_mode_schema_invalid")
            mode_key = mode["mode_key"]
            if not isinstance(mode_key, str) or not KEY_RE.fullmatch(mode_key) or mode_key in mode_keys:
                raise ValueError("catalog_mode_key_invalid")
            mode_keys.add(mode_key)
            mode_name = mode["display_name"]
            if not isinstance(mode_name, str) or not mode_name.strip() or len(mode_name.strip()) > 120:
                raise ValueError("catalog_mode_name_invalid")
            for flag in ("supports_solo", "supports_group", "loot_tracking_enabled", "ttk_tracking_enabled"):
                if type(mode[flag]) is not bool:
                    raise ValueError("catalog_flag_invalid")
            if not mode["supports_solo"] and not mode["supports_group"]:
                raise ValueError("catalog_capability_missing")
            scope = mode["timing_scope"]
            if scope not in {"overall", "segment", "unconfigured"}:
                raise ValueError("catalog_timing_scope_invalid")
            if mode["ttk_tracking_enabled"] and scope == "unconfigured":
                raise ValueError("catalog_enabled_timing_unconfigured")
            boss, challenge_mode, content = (mode["challenge_boss_key"],
                                             mode["challenge_mode_key"],
                                             mode["challenge_content_key"])
            overlay = (boss is not None, challenge_mode is not None, content is not None)
            if any(overlay) and not all(overlay):
                raise ValueError("catalog_challenge_overlay_incomplete")
            if boss is not None and any(not isinstance(item, str) or not KEY_RE.fullmatch(item)
                                        for item in (boss, challenge_mode, content)):
                raise ValueError("catalog_challenge_overlay_invalid")
            mode_sources = _names(mode["source_names"])
            for source in mode_sources:
                folded = normalize_source(source)
                if folded in source_names:
                    raise ValueError("catalog_source_name_duplicate")
                source_names.add(folded)
            clean_modes.append({**mode, "display_name": mode_name.strip(),
                                "source_names": mode_sources})
        normalized.append({**activity, "display_name": name.strip(), "aliases": aliases,
                           "modes": clean_modes})
    return {"schema_version": 1, "activities": normalized}


def _names(value: Any, *, allow_empty: bool = False) -> list[str]:
    if not isinstance(value, list) or len(value) > 30 or (not allow_empty and not value):
        raise ValueError("catalog_source_names_invalid")
    result: list[str] = []
    seen: set[str] = set()
    for name in value:
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 120:
            raise ValueError("catalog_source_name_invalid")
        normalized = name.strip()
        folded = normalize_source(normalized)
        if folded in seen:
            continue
        seen.add(folded)
        result.append(normalized)
    return result


def normalize_source(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())


def migrate_schema(conn: sqlite3.Connection) -> None:
    """Add the independent catalog version/draft tables and seed once."""
    conn.execute("PRAGMA foreign_keys=ON")
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        present = {str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?,?,?)", CATALOG_TABLES
        )}
        if present and present != set(CATALOG_TABLES):
            raise RuntimeError("partial plugin tracking catalog schema")
        conn.execute("""CREATE TABLE IF NOT EXISTS plugin_tracking_catalog_versions(
            catalog_version_id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT NOT NULL CHECK(status IN ('active','retired')),
            catalog_json TEXT NOT NULL,
            catalog_sha256 TEXT NOT NULL CHECK(length(catalog_sha256)=64),
            published_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS plugin_tracking_catalog_drafts(
            draft_id INTEGER PRIMARY KEY CHECK(draft_id=1),
            base_version_id INTEGER NOT NULL,
            revision INTEGER NOT NULL CHECK(revision>0),
            state TEXT NOT NULL CHECK(state IN ('draft','published')),
            catalog_json TEXT NOT NULL,
            updated_by TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(base_version_id) REFERENCES plugin_tracking_catalog_versions(catalog_version_id)
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS plugin_tracking_observations(
            observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
            schema_version INTEGER NOT NULL CHECK(schema_version=1),
            provenance TEXT NOT NULL CHECK(provenance='nocturne_runelite_tracking'),
            client_event_id TEXT NOT NULL UNIQUE,
            canonical_payload_sha256 TEXT NOT NULL CHECK(length(canonical_payload_sha256)=64),
            reporter_normalized_rsn TEXT NOT NULL,
            activity_key TEXT NOT NULL,
            mode_key TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            room_time_ms INTEGER,
            overall_time_ms INTEGER,
            selected_timing_scope TEXT NOT NULL CHECK(selected_timing_scope IN ('overall','segment')),
            selected_duration_ms INTEGER NOT NULL CHECK(selected_duration_ms BETWEEN 1 AND 86400000),
            observed_group_size INTEGER NOT NULL CHECK(observed_group_size BETWEEN 1 AND 10),
            catalog_version_id INTEGER NOT NULL,
            plugin_version TEXT NOT NULL,
            created_at TEXT NOT NULL,
            CHECK((room_time_ms IS NULL OR room_time_ms BETWEEN 1 AND 86400000)
              AND (overall_time_ms IS NULL OR overall_time_ms BETWEEN 1 AND 86400000)),
            FOREIGN KEY(catalog_version_id) REFERENCES plugin_tracking_catalog_versions(catalog_version_id)
        )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_plugin_tracking_observations_created ON plugin_tracking_observations(created_at)")
        conn.execute("""CREATE TABLE IF NOT EXISTS plugin_tracking_observation_participants(
            observation_id INTEGER NOT NULL,
            participant_order INTEGER NOT NULL CHECK(participant_order BETWEEN 1 AND 10),
            normalized_rsn TEXT NOT NULL CHECK(length(normalized_rsn) BETWEEN 1 AND 12),
            PRIMARY KEY(observation_id,participant_order),
            UNIQUE(observation_id,normalized_rsn),
            FOREIGN KEY(observation_id) REFERENCES plugin_tracking_observations(observation_id)
        )""")
        for name, table, operation in (
            ("plugin_tracking_observations_no_update", "plugin_tracking_observations", "UPDATE"),
            ("plugin_tracking_observations_no_delete", "plugin_tracking_observations", "DELETE"),
            ("plugin_tracking_participants_no_update", "plugin_tracking_observation_participants", "UPDATE"),
            ("plugin_tracking_participants_no_delete", "plugin_tracking_observation_participants", "DELETE"),
        ):
            conn.execute(f"CREATE TRIGGER IF NOT EXISTS {name} BEFORE {operation} ON {table} "
                         "BEGIN SELECT RAISE(ABORT,'Plugin Tracking observations are immutable'); END")
        active = conn.execute(
            "SELECT catalog_version_id FROM plugin_tracking_catalog_versions WHERE status='active'"
        ).fetchall()
        if len(active) > 1:
            raise RuntimeError("active plugin tracking catalog is ambiguous")
        if not active:
            document = normalize_document(default_document())
            encoded = canonical_json(document)
            digest = hashlib.sha256(encoded.encode()).hexdigest()
            cursor = conn.execute(
                "INSERT INTO plugin_tracking_catalog_versions(status,catalog_json,catalog_sha256) VALUES('active',?,?)",
                (encoded, digest),
            )
            conn.execute(
                "INSERT INTO plugin_tracking_catalog_drafts(draft_id,base_version_id,revision,state,catalog_json,updated_by) VALUES(1,?,1,'draft',?,'system')",
                (int(cursor.lastrowid), encoded),
            )
        expected = {
            "plugin_tracking_observations": {
                "observation_id", "schema_version", "provenance", "client_event_id",
                "canonical_payload_sha256", "reporter_normalized_rsn", "activity_key", "mode_key",
                "occurred_at", "room_time_ms", "overall_time_ms", "selected_timing_scope",
                "selected_duration_ms", "observed_group_size", "catalog_version_id",
                "plugin_version", "created_at",
            },
            "plugin_tracking_observation_participants": {
                "observation_id", "participant_order", "normalized_rsn",
            },
        }
        for table, columns in expected.items():
            actual = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
            if actual != columns:
                raise RuntimeError("Plugin Tracking observation schema mismatch")
        if owns_transaction:
            conn.commit()
    except Exception:
        if owns_transaction:
            conn.rollback()
        raise


def rollback_schema(conn: sqlite3.Connection) -> None:
    """Remove only an unused default catalog; preserve published/custom data."""
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        present = {str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?,?,?)", CATALOG_TABLES
        )}
        if not present:
            if owns_transaction:
                conn.commit()
            return
        if present != set(CATALOG_TABLES):
            raise RuntimeError("partial Plugin Tracking schema prevents rollback")
        if conn.execute("SELECT 1 FROM plugin_tracking_observations LIMIT 1").fetchone():
            raise RuntimeError("Plugin Tracking observations prevent rollback")
        versions = conn.execute("SELECT catalog_json FROM plugin_tracking_catalog_versions").fetchall()
        if len(versions) != 1 or json.loads(versions[0][0]) != normalize_document(default_document()):
            raise RuntimeError("published Plugin Tracking catalog prevents rollback")
        draft = conn.execute("SELECT catalog_json FROM plugin_tracking_catalog_drafts WHERE draft_id=1").fetchone()
        if not draft or json.loads(draft[0]) != normalize_document(default_document()):
            raise RuntimeError("edited Plugin Tracking draft prevents rollback")
        conn.execute("DROP TABLE plugin_tracking_observation_participants")
        conn.execute("DROP TABLE plugin_tracking_observations")
        conn.execute("DROP TABLE plugin_tracking_catalog_drafts")
        conn.execute("DROP TABLE plugin_tracking_catalog_versions")
        if owns_transaction:
            conn.commit()
    except Exception:
        if owns_transaction:
            conn.rollback()
        raise


def active_document(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT catalog_version_id,catalog_json,catalog_sha256 FROM plugin_tracking_catalog_versions WHERE status='active' ORDER BY catalog_version_id DESC"
    ).fetchall()
    if len(rows) != 1:
        raise RuntimeError("active plugin tracking catalog is ambiguous")
    raw = str(rows[0][1])
    if hashlib.sha256(raw.encode()).hexdigest() != str(rows[0][2]):
        raise RuntimeError("active plugin tracking catalog digest mismatch")
    return {"version_id": int(rows[0][0]), **normalize_document(json.loads(raw))}


def client_document(conn: sqlite3.Connection) -> dict[str, Any]:
    """Minimal public policy; it contains no administrative or secret fields."""
    active = active_document(conn)
    return {"schema_version": active["schema_version"],
            "catalog_version": active["version_id"],
            "activities": active["activities"]}


def match_source(conn: sqlite3.Connection, source: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    folded = normalize_source(source)
    if not folded:
        return None
    document = active_document(conn)
    matches = []
    for activity in document["activities"]:
        for mode in activity["modes"]:
            names = [activity["display_name"], *activity["aliases"], *mode["source_names"]]
            if folded in {normalize_source(name) for name in names}:
                matches.append((activity, mode))
    return matches[0] if len(matches) == 1 else None


def draft_document(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM plugin_tracking_catalog_drafts WHERE draft_id=1").fetchone()
    if not row or str(row["state"]) == "published":
        active = active_document(conn)
        encoded = canonical_json({k: active[k] for k in ("schema_version", "activities")})
        if row:
            conn.execute("UPDATE plugin_tracking_catalog_drafts SET base_version_id=?,revision=revision+1,state='draft',catalog_json=?,updated_by='system',updated_at=CURRENT_TIMESTAMP WHERE draft_id=1",
                         (active["version_id"], encoded))
        else:
            conn.execute("INSERT INTO plugin_tracking_catalog_drafts(draft_id,base_version_id,revision,state,catalog_json,updated_by) VALUES(1,?,1,'draft',?,'system')",
                         (active["version_id"], encoded))
        row = conn.execute("SELECT * FROM plugin_tracking_catalog_drafts WHERE draft_id=1").fetchone()
    return {"draft_id": 1, "base_version_id": int(row["base_version_id"]),
            "revision": int(row["revision"]), "state": str(row["state"]),
            "catalog": normalize_document(json.loads(row["catalog_json"]))}


def save_draft(conn: sqlite3.Connection, document: Any, actor: str, revision: int) -> dict[str, Any]:
    normalized = normalize_document(document)
    current = draft_document(conn)
    if current["state"] != "draft" or current["revision"] != revision:
        raise RuntimeError("plugin_tracking_revision_conflict")
    encoded = canonical_json(normalized)
    conn.execute("UPDATE plugin_tracking_catalog_drafts SET revision=revision+1,catalog_json=?,updated_by=?,updated_at=CURRENT_TIMESTAMP WHERE draft_id=1",
                 (encoded, str(actor)[:128],))
    return draft_document(conn)


def publish_draft(conn: sqlite3.Connection, actor: str, revision: int, confirmed: bool) -> dict[str, Any]:
    if not confirmed:
        raise ValueError("plugin_tracking_publish_confirmation_required")
    draft = draft_document(conn)
    if draft["state"] != "draft" or draft["revision"] != revision:
        raise RuntimeError("plugin_tracking_revision_conflict")
    document = normalize_document(draft["catalog"])
    encoded = canonical_json(document)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    current = active_document(conn)
    if digest == hashlib.sha256(canonical_json({k: current[k] for k in ("schema_version", "activities")}).encode()).hexdigest():
        raise ValueError("plugin_tracking_no_changes")
    conn.execute("UPDATE plugin_tracking_catalog_versions SET status='retired' WHERE status='active'")
    cursor = conn.execute(
        "INSERT INTO plugin_tracking_catalog_versions(status,catalog_json,catalog_sha256) VALUES('active',?,?)",
        (encoded, digest),
    )
    version_id = int(cursor.lastrowid)
    conn.execute("UPDATE plugin_tracking_catalog_drafts SET base_version_id=?,revision=revision+1,state='published',updated_by=?,updated_at=CURRENT_TIMESTAMP WHERE draft_id=1",
                 (version_id, str(actor)[:128]))
    return active_document(conn)


def process_tracking_observation(conn: sqlite3.Connection, members: sqlite3.Connection,
                                value: Any, *, now=None) -> dict[str, str]:
    """Persist an activity timing observation without creating Challenge credit."""
    from datetime import datetime, timezone
    from challenge_automatic_intake import EVENT_RE, MAX_DURATION_MS, RSN_RE, VERSION_RE, _iso_time
    from challenge_shadow_common import normalize_rsn

    def reject(reason: str) -> None:
        raise TrackingObservationError(reason)

    if not conn.in_transaction:
        raise RuntimeError("tracking observation requires caller transaction")
    allowed = {"schema_version", "event_id", "reporter_rsn", "activity_key", "mode_key",
               "occurred_at", "room_time_ms", "overall_time_ms", "roster", "group_size",
               "completion_count", "plugin_version"}
    if not isinstance(value, dict) or set(value) - allowed or (allowed - {"room_time_ms", "overall_time_ms", "completion_count"}) - set(value):
        reject("invalid_schema")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        reject("unsupported_schema")
    event_id = value.get("event_id")
    if not isinstance(event_id, str) or not EVENT_RE.fullmatch(event_id):
        reject("invalid_event_id")
    reporter_raw = value.get("reporter_rsn")
    if not isinstance(reporter_raw, str) or not RSN_RE.fullmatch(reporter_raw.strip()):
        reject("invalid_reporter")
    reporter = normalize_rsn(reporter_raw.strip())
    activity_key, mode_key = value.get("activity_key"), value.get("mode_key")
    if not isinstance(activity_key, str) or not isinstance(mode_key, str):
        reject("unsupported_activity")
    active = active_document(conn)
    matched = [(activity, mode) for activity in active["activities"]
               for mode in activity["modes"]
               if activity["activity_key"] == activity_key and mode["mode_key"] == mode_key]
    if len(matched) != 1:
        reject("unsupported_activity")
    activity, mode = matched[0]
    if not mode["ttk_tracking_enabled"] or mode["timing_scope"] == "unconfigured":
        return {"state": "ignored", "reason": "unconfigured"}
    group_size, roster_raw = value.get("group_size"), value.get("roster")
    if type(group_size) is not int or not 1 <= group_size <= MAX_GROUP_SIZE:
        reject("invalid_group_size")
    if not isinstance(roster_raw, list) or len(roster_raw) != group_size:
        reject("incomplete_roster")
    roster = []
    for rsn in roster_raw:
        if not isinstance(rsn, str) or not RSN_RE.fullmatch(rsn.strip()):
            reject("invalid_roster")
        roster.append(normalize_rsn(rsn.strip()))
    if len(set(roster)) != len(roster):
        reject("duplicate_participant")
    if roster.count(reporter) != 1:
        reject("reporter_not_in_roster")
    if group_size == 1 and not mode["supports_solo"] or group_size > 1 and not mode["supports_group"]:
        reject("invalid_group_size")
    metrics = {}
    for key in ("room_time_ms", "overall_time_ms"):
        metric = value.get(key)
        if metric is not None and (type(metric) is not int or not 1 <= metric <= MAX_DURATION_MS):
            reject("invalid_duration")
        metrics[key] = metric
    scope = mode["timing_scope"]
    selected = metrics["overall_time_ms" if scope == "overall" else "room_time_ms"]
    if selected is None:
        reject("selected_metric_missing")
    plugin_version = value.get("plugin_version")
    if not isinstance(plugin_version, str) or not VERSION_RE.fullmatch(plugin_version):
        reject("invalid_plugin_version")
    now = now or datetime.now(timezone.utc)
    occurred = _iso_time(value.get("occurred_at"), now)
    member_id, _canonical, _method = __import__("challenge_automatic_intake")._resolve_member(members, reporter)
    if member_id is None:
        reject("reporter_not_eligible")
    payload = {"schema_version": 1, "event_id": event_id, "reporter_rsn": reporter,
               "activity_key": activity_key, "mode_key": mode_key, "occurred_at": occurred,
               **metrics, "roster": sorted(roster), "group_size": group_size,
               "completion_count": value.get("completion_count"), "plugin_version": plugin_version}
    if payload["completion_count"] is not None and (type(payload["completion_count"]) is not int
            or not 1 <= payload["completion_count"] <= 1_000_000):
        reject("invalid_completion_count")
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    previous = conn.execute("SELECT canonical_payload_sha256 FROM plugin_tracking_observations WHERE client_event_id=?",
                            (event_id,)).fetchone()
    if previous:
        if str(previous[0]) != digest:
            raise TrackingObservationError("event_id_payload_conflict", "idempotency_conflict")
        return {"state": "duplicate"}
    cursor = conn.execute(
        """INSERT INTO plugin_tracking_observations(schema_version,provenance,client_event_id,
           canonical_payload_sha256,reporter_normalized_rsn,activity_key,mode_key,occurred_at,
           room_time_ms,overall_time_ms,selected_timing_scope,selected_duration_ms,
           observed_group_size,catalog_version_id,plugin_version,created_at)
           VALUES(1,'nocturne_runelite_tracking',?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (event_id, digest, reporter, activity_key, mode_key, occurred, metrics["room_time_ms"],
         metrics["overall_time_ms"], scope, selected, group_size, active["version_id"],
         plugin_version, datetime.now(timezone.utc).isoformat(timespec="milliseconds")),
    )
    observation_id = int(cursor.lastrowid)
    conn.executemany("INSERT INTO plugin_tracking_observation_participants VALUES(?,?,?)",
                     ((observation_id, index, rsn) for index, rsn in enumerate(sorted(roster), 1)))
    return {"state": "accepted"}


class TrackingObservationError(ValueError):
    def __init__(self, reason: str, state: str = "invalid"):
        super().__init__(reason)
        self.reason = reason
        self.state = state
