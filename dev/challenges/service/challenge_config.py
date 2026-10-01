#!/usr/bin/env python3
"""Versioned authoritative Nocturne Challenge configuration.

Published rows are immutable snapshots. Drafts are JSON documents edited by the
admin API and copied into normalized snapshot tables only after validation.
This module has no Google, Discord, or rank-point delivery capability.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from urllib.parse import unquote
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_DB = Path("/srv/projects/database/Challenges.db")
BOSS_ICON_DIR = Path("/srv/projects/website/media/boss_icons")
BOSS_ICON_URL_PREFIX = "/media/boss_icons"
TIER_DEFINITIONS = (
    ("bronze", "Bronze", 1),
    ("silver", "Silver", 2),
    ("gold", "Gold", 3),
    ("platinum", "Platinum", 4),
    ("ascendant", "Ascendant", 5),
)
TIER_EMOJIS = {
    "bronze": "<:bronzeCAs:1480958939284901918>",
    "silver": "<:silverCAs:1480958973376200838>",
    "gold": "<:goldCAs:1480959001779765489>",
    "platinum": "<:platinumCAs:1480959032205512845>",
    "ascendant": "<:ascendantCAs:1480959072646987862>",
}
TIME_TIER_DEFAULTS = {2: "", 3: "", 4: "", 5: ""}
NUMERIC_TIER_DEFAULTS = {2: None, 3: None, 4: None, 5: None}
TIME_INPUT_FORMATS = ("MM:SS.xx", "HH:MM:SS.xx")
INITIAL_DISCORD = {
    "gauntlet": ("<:CG:1384976641230639176>", 1),
    "colosseum": ("<:Sol:1384979613725360320>", 1),
    "delve": ("<:delve:1493262510910869565>", 1),
    "phosanis": ("<:littlenightmare:1401057285161357332>", 1),
    "cox_1": ("<:Cox2:1384978390607597608>", 1),
    "cm_1": ("<:metadust:1384978959132921916>", 1),
    "cm_3": ("<:metadust:1384978959132921916>", 3),
    "cm_5": ("<:metadust:1384978959132921916>", 5),
    "tob_2": ("<:tob3:1384979180738969690>", 2),
    "tob_3": ("<:tob3:1384979180738969690>", 3),
    "tob_5": ("<:tob3:1384979180738969690>", 5),
    "hmt_5": ("<:sangdust:1401272120113238036>", 5),
    "toa_1_300": ("<:toa2:1384979827416895639>", 1),
    "jad": ("<:Jad:1384973606341185758>", 1),
    "zuk": ("<:Zuk:1384973606341185758>", 1),
}
INITIAL_LEADERBOARD_MODES = (
    # mode_key, boss_key, content_key, display_name, metric_type, direction,
    # unit, exact party size, icon override, aliases
    ("tob_solo", None, "theatre_of_blood", "Theatre of Blood: Solo", "time", "lower", "milliseconds", 1, "/media/boss_icons/Theatre_of_blood.png", ("Tob 1", "Tob Solo", "Theatre of Blood Solo")),
    ("tob_duo", "tob_2", "theatre_of_blood", "Theatre of Blood: Duo", "time", "lower", "milliseconds", 2, None, ("Tob 2", "Tob Duo", "Theatre of Blood Duo")),
    ("tob_trio", "tob_3", "theatre_of_blood", "Theatre of Blood: Trio", "time", "lower", "milliseconds", 3, None, ("Tob 3", "Tob Trio", "Theatre of Blood Trio")),
    ("tob_4man", None, "theatre_of_blood", "Theatre of Blood: 4 Man", "time", "lower", "milliseconds", 4, "/media/boss_icons/Theatre_of_blood.png", ("Tob 4", "Tob 4 Man", "Theatre of Blood 4 Man")),
    ("tob_5man", "tob_5", "theatre_of_blood", "Theatre of Blood: 5 Man", "time", "lower", "milliseconds", 5, None, ("Tob 5", "Tob 5 Man", "Theatre of Blood 5 Man")),
    ("cox_solo", "cox_1", "chambers_of_xeric", "Chambers of Xeric: Solo", "time", "lower", "milliseconds", 1, None, ("Cox 1", "Chambers of Xeric Solo")),
    ("cox_cm_solo", "cm_1", "chambers_of_xeric_cm", "Chambers of Xeric CM: Solo", "time", "lower", "milliseconds", 1, None, ("Cm 1", "Cox Cm 1", "Chambers of Xeric CM Solo")),
    ("cox_cm_trio", "cm_3", "chambers_of_xeric_cm", "Chambers of Xeric CM: Trio", "time", "lower", "milliseconds", 3, None, ("Cm 3", "Cox Cm 3", "Chambers of Xeric CM Trio")),
    ("cox_cm_5man", "cm_5", "chambers_of_xeric_cm", "Chambers of Xeric CM: 5 Man", "time", "lower", "milliseconds", 5, None, ("Cm 5", "Cox Cm 5", "Chambers of Xeric CM 5 Man")),
    ("toa_expert_solo", "toa_1_300", "tombs_of_amascut", "Tombs of Amascut Expert: Solo", "time", "lower", "milliseconds", 1, None, ("Toa 1 300", "Toa Expert Solo", "Tombs of Amascut Expert Solo")),
    ("zuk", "zuk", "inferno", "TzKal-Zuk", "time", "lower", "milliseconds", 1, None, ("Zuk", "TzKal-Zuk")),
    ("jad", "jad", "fight_caves", "TzTok-Jad", "time", "lower", "milliseconds", 1, None, ("Jad", "TzTok-Jad")),
    ("fortis_colosseum", "colosseum", "fortis_colosseum", "Fortis Colosseum", "time", "lower", "milliseconds", 1, None, ("Colosseum", "Fortis Colosseum")),
    ("corrupted_gauntlet", "gauntlet", "corrupted_gauntlet", "Corrupted Gauntlet", "time", "lower", "milliseconds", 1, None, ("Gauntlet", "Corrupted Gauntlet", "CG")),
    ("phosani_nightmare", "phosanis", "phosanis_nightmare", "Phosani's Nightmare", "time", "lower", "milliseconds", 1, None, ("Phosani's", "Phosani", "Phosanis")),
    ("hmt_5man", "hmt_5", "theatre_of_blood_hard_mode", "Hard Mode Theatre of Blood: 5 Man", "time", "lower", "milliseconds", 5, None, ("Hmt 5", "Hard Mode Theatre of Blood 5 Man")),
    ("doom_of_mokhaiotl", "delve", "doom_of_mokhaiotl", "Doom of Mokhaiotl: Deepest Delve", "numeric", "higher", "waves", 1, None, ("Delve", "Doom", "Doom of Mokhaiotl")),
)
INITIAL_LEADERBOARD_GROUPS = {
    "tob_solo": ("theatre_of_blood", "Theatre of Blood", 10, None),
    "tob_duo": ("theatre_of_blood", "Theatre of Blood", 10, None),
    "tob_trio": ("theatre_of_blood", "Theatre of Blood", 10, None),
    "tob_4man": ("theatre_of_blood", "Theatre of Blood", 10, None),
    "tob_5man": ("theatre_of_blood", "Theatre of Blood", 10, None),
    "hmt_5man": ("theatre_of_blood", "Theatre of Blood", 10, None),
    "cox_solo": ("chambers_of_xeric", "Chambers of Xeric", 20, None),
    "cox_cm_solo": ("chambers_of_xeric", "Chambers of Xeric", 20, None),
    "cox_cm_trio": ("chambers_of_xeric", "Chambers of Xeric", 20, None),
    "cox_cm_5man": ("chambers_of_xeric", "Chambers of Xeric", 20, None),
    "toa_expert_solo": ("tombs_of_amascut", "Tombs of Amascut", 30, None),
    "zuk": ("solo_pvm", "Solo PvM", 40, None),
    "jad": ("solo_pvm", "Solo PvM", 40, None),
    "fortis_colosseum": ("solo_pvm", "Solo PvM", 40, None),
    "corrupted_gauntlet": ("solo_pvm", "Solo PvM", 40, None),
    "phosani_nightmare": ("solo_pvm", "Solo PvM", 40, None),
    "doom_of_mokhaiotl": ("solo_pvm", "Solo PvM", 40, None),
}
SCHEMA_SQL = r"""
CREATE TABLE IF NOT EXISTS challenge_config_bosses (
    config_version_id INTEGER NOT NULL,
    boss_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    is_active INTEGER NOT NULL CHECK(is_active IN (0,1)),
    display_order INTEGER NOT NULL,
    effective_display_order INTEGER,
    custom_order_override INTEGER NOT NULL DEFAULT 0 CHECK(custom_order_override IN (0,1)),
    metric_type TEXT NOT NULL CHECK(metric_type IN ('time','numeric','completion')),
    comparison_direction TEXT NOT NULL CHECK(comparison_direction IN ('lower','higher','complete')),
    description TEXT,
    help_text TEXT,
    icon_url TEXT,
    discord_label TEXT,
    discord_emoji TEXT,
    discord_group TEXT,
    submission_enabled INTEGER NOT NULL DEFAULT 1 CHECK(submission_enabled IN (0,1)),
    supports_groups INTEGER NOT NULL DEFAULT 1 CHECK(supports_groups IN (0,1)),
    min_party_size INTEGER NOT NULL DEFAULT 1 CHECK(min_party_size BETWEEN 1 AND 100),
    time_input_format TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(config_version_id,boss_key),
    FOREIGN KEY(config_version_id) REFERENCES challenge_config_versions(config_version_id),
    FOREIGN KEY(boss_key) REFERENCES challenge_bosses(boss_key)
);
CREATE INDEX IF NOT EXISTS idx_challenge_config_bosses_active
ON challenge_config_bosses(config_version_id,is_active,submission_enabled,display_order);

CREATE TABLE IF NOT EXISTS challenge_config_aliases (
    config_version_id INTEGER NOT NULL,
    boss_key TEXT NOT NULL,
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(config_version_id,boss_key,normalized_alias),
    UNIQUE(config_version_id,normalized_alias),
    FOREIGN KEY(config_version_id,boss_key)
        REFERENCES challenge_config_bosses(config_version_id,boss_key)
);

CREATE TABLE IF NOT EXISTS challenge_config_drafts (
    draft_id INTEGER PRIMARY KEY AUTOINCREMENT,
    base_config_version_id INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('draft','validated','published','discarded')),
    revision INTEGER NOT NULL DEFAULT 1,
    draft_json TEXT NOT NULL,
    draft_sha256 TEXT NOT NULL,
    validation_errors_json TEXT NOT NULL DEFAULT '[]',
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    validated_at TEXT,
    published_config_version_id INTEGER,
    published_at TEXT,
    FOREIGN KEY(base_config_version_id) REFERENCES challenge_config_versions(config_version_id),
    FOREIGN KEY(published_config_version_id) REFERENCES challenge_config_versions(config_version_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_challenge_config_one_open_draft
ON challenge_config_drafts((1)) WHERE state IN ('draft','validated');

CREATE TABLE IF NOT EXISTS leaderboard_modes (
    mode_key TEXT PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    archived_at TEXT
);

CREATE TABLE IF NOT EXISTS leaderboard_mode_versions (
    config_version_id INTEGER NOT NULL,
    mode_key TEXT NOT NULL,
    boss_key TEXT,
    content_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    is_active INTEGER NOT NULL CHECK(is_active IN (0,1)),
    display_order INTEGER NOT NULL,
    metric_type TEXT NOT NULL CHECK(metric_type IN ('time','numeric','completion')),
    comparison_direction TEXT NOT NULL CHECK(comparison_direction IN ('lower','higher','complete')),
    metric_unit TEXT NOT NULL,
    party_size_min INTEGER NOT NULL CHECK(party_size_min BETWEEN 1 AND 100),
    party_size_max INTEGER NOT NULL CHECK(party_size_max BETWEEN 1 AND 100),
    top_n INTEGER NOT NULL DEFAULT 3 CHECK(top_n BETWEEN 1 AND 25),
    inherit_boss_icon INTEGER NOT NULL DEFAULT 1 CHECK(inherit_boss_icon IN (0,1)),
    icon_url TEXT,
    publication_group_key TEXT,
    publication_group_name TEXT,
    publication_group_order INTEGER,
    group_icon_url TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(config_version_id,mode_key),
    FOREIGN KEY(config_version_id) REFERENCES challenge_config_versions(config_version_id),
    FOREIGN KEY(mode_key) REFERENCES leaderboard_modes(mode_key),
    FOREIGN KEY(config_version_id,boss_key)
        REFERENCES challenge_config_bosses(config_version_id,boss_key),
    CHECK(party_size_min <= party_size_max),
    CHECK(boss_key IS NOT NULL OR inherit_boss_icon = 0)
);
CREATE INDEX IF NOT EXISTS idx_leaderboard_mode_versions_active
ON leaderboard_mode_versions(config_version_id,is_active,display_order,mode_key);

CREATE TABLE IF NOT EXISTS leaderboard_mode_aliases (
    config_version_id INTEGER NOT NULL,
    mode_key TEXT NOT NULL,
    source_system TEXT NOT NULL DEFAULT 'any',
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(config_version_id,mode_key,source_system,normalized_alias),
    UNIQUE(config_version_id,source_system,normalized_alias),
    FOREIGN KEY(config_version_id,mode_key)
        REFERENCES leaderboard_mode_versions(config_version_id,mode_key)
);

CREATE TRIGGER IF NOT EXISTS challenge_config_versions_published_no_content_update
BEFORE UPDATE OF version_key,source_name,source_snapshot_sha256,config_json,effective_from
ON challenge_config_versions
WHEN OLD.status IN ('active','retired')
BEGIN SELECT RAISE(ABORT,'published challenge config versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS challenge_config_versions_published_no_delete
BEFORE DELETE ON challenge_config_versions
WHEN OLD.status IN ('active','retired')
BEGIN SELECT RAISE(ABORT,'published challenge config versions are immutable'); END;

CREATE TRIGGER IF NOT EXISTS challenge_config_bosses_no_update
BEFORE UPDATE ON challenge_config_bosses
BEGIN SELECT RAISE(ABORT,'published challenge config bosses are immutable'); END;
CREATE TRIGGER IF NOT EXISTS challenge_config_bosses_no_delete
BEFORE DELETE ON challenge_config_bosses
BEGIN SELECT RAISE(ABORT,'published challenge config bosses are immutable'); END;
CREATE TRIGGER IF NOT EXISTS challenge_config_aliases_no_update
BEFORE UPDATE ON challenge_config_aliases
BEGIN SELECT RAISE(ABORT,'published challenge config aliases are immutable'); END;
CREATE TRIGGER IF NOT EXISTS challenge_config_aliases_no_delete
BEFORE DELETE ON challenge_config_aliases
BEGIN SELECT RAISE(ABORT,'published challenge config aliases are immutable'); END;
CREATE TRIGGER IF NOT EXISTS leaderboard_mode_versions_no_update
BEFORE UPDATE ON leaderboard_mode_versions
BEGIN SELECT RAISE(ABORT,'published leaderboard mode versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS leaderboard_mode_versions_no_delete
BEFORE DELETE ON leaderboard_mode_versions
BEGIN SELECT RAISE(ABORT,'published leaderboard mode versions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS leaderboard_mode_aliases_no_update
BEFORE UPDATE ON leaderboard_mode_aliases
BEGIN SELECT RAISE(ABORT,'published leaderboard mode aliases are immutable'); END;
CREATE TRIGGER IF NOT EXISTS leaderboard_mode_aliases_no_delete
BEFORE DELETE ON leaderboard_mode_aliases
BEGIN SELECT RAISE(ABORT,'published leaderboard mode aliases are immutable'); END;
CREATE TRIGGER IF NOT EXISTS challenge_tiers_published_no_update
BEFORE UPDATE ON challenge_tiers
WHEN EXISTS (SELECT 1 FROM challenge_config_versions v
             WHERE v.config_version_id=OLD.config_version_id AND v.status IN ('active','retired'))
BEGIN SELECT RAISE(ABORT,'published challenge tiers are immutable'); END;
CREATE TRIGGER IF NOT EXISTS challenge_tiers_published_no_delete
BEFORE DELETE ON challenge_tiers
WHEN EXISTS (SELECT 1 FROM challenge_config_versions v
             WHERE v.config_version_id=OLD.config_version_id AND v.status IN ('active','retired'))
BEGIN SELECT RAISE(ABORT,'published challenge tiers are immutable'); END;
CREATE TRIGGER IF NOT EXISTS challenge_system_tiers_published_no_update
BEFORE UPDATE ON challenge_system_tiers
WHEN EXISTS (SELECT 1 FROM challenge_config_versions v
             WHERE v.config_version_id=OLD.config_version_id AND v.status IN ('active','retired'))
BEGIN SELECT RAISE(ABORT,'published challenge system tiers are immutable'); END;
CREATE TRIGGER IF NOT EXISTS challenge_system_tiers_published_no_delete
BEFORE DELETE ON challenge_system_tiers
WHEN EXISTS (SELECT 1 FROM challenge_config_versions v
             WHERE v.config_version_id=OLD.config_version_id AND v.status IN ('active','retired'))
BEGIN SELECT RAISE(ABORT,'published challenge system tiers are immutable'); END;
"""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_alias(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _stable_mode_aliases(mode: dict[str, Any]) -> list[str]:
    selected: dict[str, str] = {}
    for value in [mode.get("mode_key"), mode.get("display_name"), *(mode.get("aliases") or [])]:
        alias = str(value or "").strip()
        normalized = normalize_alias(alias)
        if normalized and normalized not in selected:
            selected[normalized] = alias
    return [selected[key] for key in sorted(selected)]


def initial_leaderboard_document() -> list[dict[str, Any]]:
    modes: list[dict[str, Any]] = []
    for order, item in enumerate(INITIAL_LEADERBOARD_MODES, start=1):
        (mode_key, boss_key, content_key, display_name, metric_type, direction,
         metric_unit, party_size, icon_url, aliases) = item
        modes.append({
            "mode_key": mode_key,
            "boss_key": boss_key,
            "content_key": content_key,
            "display_name": display_name,
            "active": True,
            "display_order": order * 10,
            "metric_type": metric_type,
            "comparison_direction": direction,
            "metric_unit": metric_unit,
            "party_size_min": party_size,
            "party_size_max": party_size,
            "top_n": 3,
            "inherit_boss_icon": boss_key is not None,
            "icon_url": icon_url,
            "aliases": list(aliases),
            "publication_group_key": INITIAL_LEADERBOARD_GROUPS[mode_key][0],
            "publication_group_name": INITIAL_LEADERBOARD_GROUPS[mode_key][1],
            "publication_group_order": INITIAL_LEADERBOARD_GROUPS[mode_key][2],
            "group_icon_url": INITIAL_LEADERBOARD_GROUPS[mode_key][3],
        })
    return modes


def normalize_leaderboard_mode(mode: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(canonical_json(mode))
    result["mode_key"] = str(result.get("mode_key") or "").strip()
    result["boss_key"] = str(result["boss_key"]).strip() if result.get("boss_key") else None
    result["content_key"] = str(result.get("content_key") or "").strip()
    result["display_name"] = str(result.get("display_name") or "").strip()
    result["active"] = bool(result.get("active", True))
    for field, default in (
        ("display_order", 0), ("party_size_min", 1),
        ("party_size_max", result.get("party_size_min", 1)), ("top_n", 3),
    ):
        candidate = result.get(field, default)
        try:
            result[field] = int(candidate)
        except (TypeError, ValueError):
            result[field] = candidate
    result["metric_type"] = str(result.get("metric_type") or "").strip().lower()
    result["custom_order_override"] = bool(result.get("custom_order_override", False))
    if result["boss_key"] is None:
        result["custom_order_override"] = False
    try:
        result["effective_display_order"] = int(
            result.get("effective_display_order", result.get("display_order", 0))
        )
    except (TypeError, ValueError):
        result["effective_display_order"] = result.get("effective_display_order")
    result["comparison_direction"] = str(result.get("comparison_direction") or "").strip().lower()
    result["metric_unit"] = str(result.get("metric_unit") or "").strip().lower()
    result["inherit_boss_icon"] = bool(result.get("inherit_boss_icon", result["boss_key"] is not None))
    result["icon_url"] = _optional_text(result.get("icon_url"))
    result["publication_group_key"] = str(result.get("publication_group_key") or "").strip()
    result["publication_group_name"] = str(result.get("publication_group_name") or "").strip()
    try:
        result["publication_group_order"] = int(result.get("publication_group_order", 0))
    except (TypeError, ValueError):
        result["publication_group_order"] = result.get("publication_group_order")
    result["group_icon_url"] = _optional_text(result.get("group_icon_url"))
    result["aliases"] = _stable_mode_aliases(result)
    return result


def _leaderboard_mode_sort_key(mode: dict[str, Any]) -> tuple[int, str]:
    try:
        order = int(mode.get("display_order", 0))
    except (TypeError, ValueError):
        order = 2_147_483_647
    return order, str(mode.get("mode_key") or "")


def apply_linked_leaderboard_order(
    bosses: list[dict[str, Any]],
    modes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Materialize deterministic draft order without changing configured mode order.

    Linked modes follow their Challenge boss unless an administrator explicitly
    opts that mode out. Modes sharing a boss retain their configured internal
    order. Leaderboard-only and overridden modes retain their configured order.
    Publication group order remains the independent outer ordering boundary.
    """
    boss_orders: dict[str, int] = {}
    for boss in bosses:
        try:
            boss_orders[str(boss.get("boss_key") or "")] = int(boss.get("display_order", 0))
        except (TypeError, ValueError):
            continue

    normalized = [normalize_leaderboard_mode(mode) for mode in modes]
    groups: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for mode in normalized:
        try:
            group_order = int(mode.get("publication_group_order", 0))
        except (TypeError, ValueError):
            group_order = 2_147_483_647
        group_key = str(mode.get("publication_group_key") or "")
        groups.setdefault((group_order, group_key), []).append(mode)

    output: list[dict[str, Any]] = []
    global_position = 0
    for group_identity in sorted(groups):
        def inside_group(mode: dict[str, Any]) -> tuple[int, int, str]:
            configured = int(mode.get("display_order", 0))
            boss_key = str(mode.get("boss_key") or "")
            if boss_key and not mode.get("custom_order_override") and boss_key in boss_orders:
                return boss_orders[boss_key], configured, str(mode.get("mode_key") or "")
            return configured, configured, str(mode.get("mode_key") or "")

        for mode in sorted(groups[group_identity], key=inside_group):
            global_position += 1
            mode["effective_display_order"] = global_position * 10
            output.append(mode)
    return output


def discord_emoji_icon_url(value: Any) -> str | None:
    """Return a PNG CDN URL for a configured custom Discord emoji."""
    match = re.fullmatch(r"<a?:[A-Za-z0-9_]+:(\d+)>", str(value or "").strip())
    return f"https://cdn.discordapp.com/emojis/{match.group(1)}.png?size=128" if match else None


def local_boss_icon_url(boss_key: Any) -> str | None:
    """Return the canonical local artwork URL when that published asset exists."""
    key = str(boss_key or "").strip()
    if not re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", key):
        return None
    return f"{BOSS_ICON_URL_PREFIX}/{key}.png" if (BOSS_ICON_DIR / f"{key}.png").is_file() else None


def reusable_boss_icon_url(value: Any) -> bool:
    """Accept only an existing PNG basename from the canonical artwork catalog."""
    url = str(value or "").strip()
    prefix = f"{BOSS_ICON_URL_PREFIX}/"
    if not url.startswith(prefix):
        return False
    filename = unquote(url[len(prefix):])
    if (
        not filename
        or filename != Path(filename).name
        or not filename.casefold().endswith(".png")
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._ -]*\.png", filename) is None
    ):
        return False
    return (BOSS_ICON_DIR / filename).is_file()


def parse_time_threshold(value: Any) -> tuple[int, str]:
    if isinstance(value, int) and not isinstance(value, bool):
        if value < 0:
            raise ValueError("time threshold must be non-negative")
        total_ms = value
    else:
        text = str(value or "").strip()
        hms = re.fullmatch(r"(\d+):([0-5]\d):([0-5]\d)(?:\.(\d{1,3}))?", text)
        ms = re.fullmatch(r"(\d+):([0-5]\d)(?:\.(\d{1,3}))?", text)
        if hms:
            hours, minutes, seconds = map(int, hms.group(1, 2, 3))
            fraction = (hms.group(4) or "").ljust(3, "0")
        elif ms:
            hours = 0
            minutes, seconds = map(int, ms.group(1, 2))
            fraction = (ms.group(3) or "").ljust(3, "0")
        else:
            raise ValueError("time threshold must use MM:SS.xx or HH:MM:SS.xx")
        total_ms = ((hours * 3600 + minutes * 60 + seconds) * 1000) + int(fraction or 0)
    seconds, millis = divmod(total_ms, 1000)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    display = f"{hours}:{minutes:02d}:{seconds:02d}.{millis:03d}"
    return total_ms, display


def format_time_threshold(total_ms: int, input_format: str | None) -> str:
    total_ms = int(total_ms)
    seconds, millis = divmod(total_ms, 1000)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    hundredths = millis // 10
    if input_format == "MM:SS.xx":
        return f"{hours * 60 + minutes:02d}:{seconds:02d}.{hundredths:02d}"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{hundredths:02d}"


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _stable_aliases(boss: dict[str, Any]) -> list[str]:
    """Return one deterministic display alias for each normalized identity."""
    selected: dict[str, str] = {}
    candidates = [
        boss.get("display_name"),
        boss.get("boss_key"),
        *(boss.get("aliases") or []),
    ]
    for value in candidates:
        alias = str(value or "").strip()
        normalized = normalize_alias(alias)
        if normalized and normalized not in selected:
            selected[normalized] = alias
    return [selected[key] for key in sorted(selected)]


def submission_mode(boss: dict[str, Any]) -> str:
    if not bool(boss.get("supports_groups", True)):
        return "solo"
    return "group" if int(boss.get("min_party_size", 1) or 1) >= 2 else "either"


def normalize_boss_submission_semantics(boss: dict[str, Any]) -> dict[str, Any]:
    mode = str(boss.get("submission_mode") or submission_mode(boss)).strip().lower()
    if mode == "solo":
        boss["supports_groups"] = False
        boss["min_party_size"] = 1
    elif mode == "group":
        boss["supports_groups"] = True
        try:
            boss["min_party_size"] = max(2, int(boss.get("min_party_size", 2)))
        except (TypeError, ValueError):
            boss["min_party_size"] = boss.get("min_party_size")
    elif mode == "either":
        boss["supports_groups"] = True
        boss["min_party_size"] = 1
    boss["submission_mode"] = mode
    if boss.get("metric_type") == "time":
        boss["time_input_format"] = str(
            boss.get("time_input_format") or "HH:MM:SS.xx"
        ).strip()
    else:
        boss["time_input_format"] = None
    return boss


def normalize_boss_metric_semantics(
    boss: dict[str, Any],
    previous_metric: str | None = None,
) -> dict[str, Any]:
    """Normalize hidden tier semantics, resetting thresholds on type changes."""
    metric = str(boss.get("metric_type") or "").strip().lower()
    changed = previous_metric is not None and previous_metric != metric
    boss["metric_type"] = metric
    boss["comparison_direction"] = (
        "higher" if metric == "numeric"
        else "complete" if metric == "completion"
        else "lower"
    )
    tiers = boss.get("tiers") if isinstance(boss.get("tiers"), list) else []
    for tier in tiers:
        if not isinstance(tier, dict):
            continue
        try:
            rank = int(tier.get("rank"))
        except (TypeError, ValueError):
            continue
        if rank == 1 or metric == "completion":
            tier.update({
                "threshold": 1,
                "threshold_display": "Completion",
                "metric_type": "completion",
                "operator": "complete",
                "unit": "boolean",
            })
            continue
        if metric == "time":
            candidate = (
                TIME_TIER_DEFAULTS.get(rank, "0:00:00")
                if changed
                else tier.get("threshold_display", tier.get("threshold"))
            )
            try:
                value, display = parse_time_threshold(candidate)
                tier["threshold"] = value
                tier["threshold_display"] = format_time_threshold(value, boss.get("time_input_format"))
            except ValueError:
                # Preserve the draft text so authoritative validation can
                # report the exact invalid field instead of hiding it.
                tier["threshold_display"] = str(candidate or "")
            tier.update({
                "metric_type": "time",
                "operator": "lte",
                "unit": "milliseconds",
            })
        elif metric == "numeric":
            candidate = (
                NUMERIC_TIER_DEFAULTS.get(rank, 0)
                if changed
                else tier.get("threshold")
            )
            try:
                value = int(candidate)
                tier["threshold"] = value
                tier["threshold_display"] = str(value)
            except (TypeError, ValueError):
                tier["threshold"] = candidate
                tier["threshold_display"] = str(candidate or "")
            tier.update({
                "metric_type": "numeric",
                "operator": "gte",
                "unit": "waves",
            })
    return boss


def normalize_draft_document(
    document: dict[str, Any],
    previous_document: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = json.loads(canonical_json(document))
    previous = {
        str(item.get("boss_key")): item
        for item in (previous_document or {}).get("bosses", [])
        if isinstance(item, dict)
    }
    for boss in result.get("bosses", []):
        key = str(boss.get("boss_key") or "")
        old = previous.get(key)
        normalize_boss_metric_semantics(
            boss,
            str(old.get("metric_type")) if old else None,
        )
        normalize_boss_submission_semantics(boss)
        boss["aliases"] = _stable_aliases(boss)
        boss["description"] = _optional_text(boss.get("description"))
        boss["help_text"] = _optional_text(boss.get("help_text"))
        boss["icon_url"] = _optional_text(boss.get("icon_url"))
        boss["discord_group"] = _optional_text(boss.get("discord_group"))
        boss["discord_emoji"] = _optional_text(boss.get("discord_emoji"))
    result["leaderboard_modes"] = apply_linked_leaderboard_order(
        result.get("bosses", []),
        result.get("leaderboard_modes", []),
    )
    return result


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _insert_leaderboard_snapshot(
    conn: sqlite3.Connection,
    version_id: int,
    modes: list[dict[str, Any]],
) -> int:
    inserted = 0
    for raw_mode in modes:
        mode = normalize_leaderboard_mode(raw_mode)
        conn.execute(
            "INSERT OR IGNORE INTO leaderboard_modes(mode_key) VALUES(?)",
            (mode["mode_key"],),
        )
        conn.execute(
            """INSERT INTO leaderboard_mode_versions
               (config_version_id,mode_key,boss_key,content_key,display_name,is_active,
                display_order,effective_display_order,custom_order_override,
                metric_type,comparison_direction,metric_unit,
                party_size_min,party_size_max,top_n,inherit_boss_icon,icon_url,
                publication_group_key,publication_group_name,publication_group_order,group_icon_url)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (version_id, mode["mode_key"], mode["boss_key"], mode["content_key"],
             mode["display_name"], int(mode["active"]), mode["display_order"],
             mode["effective_display_order"], int(mode["custom_order_override"]),
             mode["metric_type"], mode["comparison_direction"], mode["metric_unit"],
             mode["party_size_min"], mode["party_size_max"], mode["top_n"],
             int(mode["inherit_boss_icon"]), mode["icon_url"],
             mode["publication_group_key"], mode["publication_group_name"],
             mode["publication_group_order"], mode["group_icon_url"]),
        )
        for alias in mode["aliases"]:
            conn.execute(
                """INSERT INTO leaderboard_mode_aliases
                   (config_version_id,mode_key,source_system,alias,normalized_alias)
                   VALUES(?,?,'any',?,?)""",
                (version_id, mode["mode_key"], alias, normalize_alias(alias)),
            )
        inserted += 1
    return inserted


def migrate_schema(conn: sqlite3.Connection) -> dict[str, Any]:
    """Add Phase 4A tables and snapshot the existing active config once."""
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if "discord_emoji" not in _column_names(conn, "challenge_tiers"):
            conn.execute("ALTER TABLE challenge_tiers ADD COLUMN discord_emoji TEXT")
        conn.executescript(SCHEMA_SQL)
        leaderboard_columns = _column_names(conn, "leaderboard_mode_versions")
        for column, sql_type in (
            ("publication_group_key", "TEXT"),
            ("publication_group_name", "TEXT"),
            ("publication_group_order", "INTEGER"),
            ("group_icon_url", "TEXT"),
            ("effective_display_order", "INTEGER"),
            ("custom_order_override", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in leaderboard_columns:
                conn.execute(f"ALTER TABLE leaderboard_mode_versions ADD COLUMN {column} {sql_type}")
        if "time_input_format" not in _column_names(conn, "challenge_config_bosses"):
            conn.execute("ALTER TABLE challenge_config_bosses ADD COLUMN time_input_format TEXT")
        if "icon_url" not in _column_names(conn, "challenge_config_bosses"):
            conn.execute("ALTER TABLE challenge_config_bosses ADD COLUMN icon_url TEXT")
        active = conn.execute(
            "SELECT config_version_id FROM challenge_config_versions WHERE status='active'"
        ).fetchone()
        if not active:
            raise RuntimeError("No active challenge config version exists")
        version_id = int(active[0])
        seeded = 0
        if not conn.execute(
            "SELECT 1 FROM challenge_config_bosses WHERE config_version_id=? LIMIT 1", (version_id,)
        ).fetchone():
            for boss in conn.execute("SELECT * FROM challenge_bosses ORDER BY sort_order,boss_key").fetchall():
                emoji, min_party = INITIAL_DISCORD.get(str(boss["boss_key"]), (None, 1))
                metric = str(boss["metric_type"] or "time")
                direction = str(boss["comparison_direction"] or ("higher" if metric == "numeric" else "lower"))
                conn.execute(
                    """INSERT INTO challenge_config_bosses
                       (config_version_id,boss_key,display_name,is_active,display_order,metric_type,
                        comparison_direction,description,help_text,discord_label,discord_emoji,
                        discord_group,submission_enabled,supports_groups,min_party_size,time_input_format)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (version_id,boss["boss_key"],boss["display_name"],int(boss["is_active"]),
                     int(boss["sort_order"] or 999),metric,direction,None,None,boss["display_name"],
                     emoji,None,int(boss["is_active"]),int(boss["supports_groups"]),min_party,
                     None),
                )
                aliases = {str(boss["boss_key"]), str(boss["display_name"])}
                if str(boss["boss_key"]) == "phosanis":
                    aliases.update({"Phosanis", "Phosani"})
                distinct_aliases = {normalize_alias(alias): alias for alias in sorted(aliases)}
                for normalized_alias, alias in distinct_aliases.items():
                    conn.execute(
                        "INSERT INTO challenge_config_aliases(config_version_id,boss_key,alias,normalized_alias) VALUES(?,?,?,?)",
                        (version_id,boss["boss_key"],alias,normalized_alias),
                    )
                seeded += 1
        leaderboard_seeded = 0
        if not conn.execute(
            "SELECT 1 FROM leaderboard_mode_versions WHERE config_version_id=? LIMIT 1",
            (version_id,),
        ).fetchone():
            leaderboard_seeded = _insert_leaderboard_snapshot(
                conn, version_id, initial_leaderboard_document()
            )
        open_drafts_updated = 0
        for draft_row in conn.execute(
            """SELECT draft_id,draft_json FROM challenge_config_drafts
                 WHERE state IN ('draft','validated')"""
        ).fetchall():
            draft_doc = json.loads(draft_row["draft_json"])
            if "leaderboard_modes" in draft_doc:
                continue
            draft_doc["leaderboard_modes"] = config_document(conn, version_id)["leaderboard_modes"]
            payload = canonical_json(draft_doc)
            conn.execute(
                """UPDATE challenge_config_drafts
                      SET state='draft',revision=revision+1,draft_json=?,draft_sha256=?,
                          validation_errors_json='[]',validated_at=NULL,
                          updated_by='phase5c_schema_migration',updated_at=CURRENT_TIMESTAMP
                    WHERE draft_id=?""",
                (payload, sha256_text(payload), int(draft_row["draft_id"])),
            )
            open_drafts_updated += 1
        conn.commit()
        return {
            "active_version_id": version_id,
            "snapshot_bosses_seeded": seeded,
            "leaderboard_modes_seeded": leaderboard_seeded,
            "open_drafts_updated": open_drafts_updated,
        }
    except Exception:
        conn.rollback()
        raise


def _tier_document(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "tier_key": row["tier_key"],
        "tier": row["display_name"],
        "rank": int(row["tier_rank"]),
        "threshold": row["requirement_value"],
        "threshold_display": row["requirement_display"],
        "metric_type": row["requirement_metric_type"],
        "operator": row["requirement_operator"],
        "unit": row["requirement_unit"],
        "points": int(row["source_submission_points"]),
        "progression_points": int(row["cumulative_progression_points"]),
        "discord_emoji": row["discord_emoji"] or TIER_EMOJIS.get(str(row["tier_key"])),
    }


def config_document(conn: sqlite3.Connection, version_id: int | None = None, *, consumer: bool = False) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    if version_id is None:
        version = conn.execute("SELECT * FROM challenge_config_versions WHERE status='active'").fetchone()
    else:
        version = conn.execute("SELECT * FROM challenge_config_versions WHERE config_version_id=?", (version_id,)).fetchone()
    if not version:
        raise LookupError("Challenge config version not found")
    bosses = []
    rows = conn.execute(
        "SELECT * FROM challenge_config_bosses WHERE config_version_id=? ORDER BY display_order,boss_key",
        (version["config_version_id"],),
    ).fetchall()
    for boss in rows:
        if consumer and (not int(boss["is_active"]) or not int(boss["submission_enabled"])):
            continue
        time_input_format = (
            boss["time_input_format"]
            if "time_input_format" in boss.keys()
            else None
        )
        tiers = [_tier_document(row) for row in conn.execute(
            "SELECT * FROM challenge_tiers WHERE config_version_id=? AND boss_key=? ORDER BY tier_rank",
            (version["config_version_id"],boss["boss_key"]),
        ).fetchall()]
        aliases = [str(row[0]) for row in conn.execute(
            "SELECT alias FROM challenge_config_aliases WHERE config_version_id=? AND boss_key=? ORDER BY alias COLLATE NOCASE",
            (version["config_version_id"],boss["boss_key"]),
        )]
        bosses.append({
            "boss_key": boss["boss_key"], "display_name": boss["display_name"],
            "active": bool(boss["is_active"]), "display_order": int(boss["display_order"]),
            "metric_type": boss["metric_type"], "comparison_direction": boss["comparison_direction"],
            "description": boss["description"], "help_text": boss["help_text"], "aliases": aliases,
            "icon_url": (
                boss["icon_url"] if "icon_url" in boss.keys() and boss["icon_url"]
                else local_boss_icon_url(boss["boss_key"])
                or discord_emoji_icon_url(boss["discord_emoji"])
            ),
            "discord_label": boss["discord_label"], "discord_emoji": boss["discord_emoji"],
            "discord_group": boss["discord_group"], "submission_enabled": bool(boss["submission_enabled"]),
            "supports_groups": bool(boss["supports_groups"]), "min_party_size": int(boss["min_party_size"]),
            "submission_mode": submission_mode(dict(boss)),
            "time_input_format": (
                time_input_format or "HH:MM:SS.xx"
                if boss["metric_type"] == "time" else None
            ),
            "tiers": tiers,
        })
    system_tiers = [dict(row) for row in conn.execute(
        """SELECT system_tier_key,display_name,tier_rank,min_progression_points,
                  require_all_active_challenges,one_time_rank_bonus
             FROM challenge_system_tiers WHERE config_version_id=? ORDER BY tier_rank""",
        (version["config_version_id"],),
    )]
    for row in system_tiers:
        row["require_all_active_challenges"] = bool(row["require_all_active_challenges"])
    boss_icons = {str(item["boss_key"]): item.get("icon_url") for item in bosses}
    leaderboard_modes = []
    if _table_exists(conn, "leaderboard_mode_versions"):
        for mode in conn.execute(
            """SELECT * FROM leaderboard_mode_versions
                 WHERE config_version_id=? ORDER BY display_order,mode_key""",
            (version["config_version_id"],),
        ).fetchall():
            aliases = [str(row[0]) for row in conn.execute(
                """SELECT alias FROM leaderboard_mode_aliases
                     WHERE config_version_id=? AND mode_key=? AND source_system='any'
                     ORDER BY alias COLLATE NOCASE""",
                (version["config_version_id"], mode["mode_key"]),
            )]
            inherited_icon = boss_icons.get(str(mode["boss_key"])) if mode["boss_key"] else None
            leaderboard_modes.append({
                "mode_key": mode["mode_key"],
                "boss_key": mode["boss_key"],
                "content_key": mode["content_key"],
                "display_name": mode["display_name"],
                "active": bool(mode["is_active"]),
                "display_order": int(mode["display_order"]),
                "effective_display_order": int(
                    mode["effective_display_order"]
                    if "effective_display_order" in mode.keys()
                    and mode["effective_display_order"] is not None
                    else mode["display_order"]
                ),
                "custom_order_override": bool(
                    mode["custom_order_override"]
                    if "custom_order_override" in mode.keys()
                    else False
                ),
                "metric_type": mode["metric_type"],
                "comparison_direction": mode["comparison_direction"],
                "metric_unit": mode["metric_unit"],
                "party_size_min": int(mode["party_size_min"]),
                "party_size_max": int(mode["party_size_max"]),
                "top_n": int(mode["top_n"]),
                "inherit_boss_icon": bool(mode["inherit_boss_icon"]),
                "icon_url": mode["icon_url"],
                "effective_icon_url": inherited_icon if mode["inherit_boss_icon"] else mode["icon_url"],
                "publication_group_key": mode["publication_group_key"],
                "publication_group_name": mode["publication_group_name"],
                "publication_group_order": mode["publication_group_order"],
                "group_icon_url": mode["group_icon_url"],
                "aliases": aliases,
            })
    leaderboard_modes.sort(
        key=lambda item: (
            int(item["effective_display_order"]),
            str(item["mode_key"]),
        )
    )
    return {
        "version_id": int(version["config_version_id"]), "version_key": version["version_key"],
        "status": version["status"], "published_at": version["effective_from"] or version["created_at"],
        "bosses": bosses, "system_tiers": system_tiers,
        "leaderboard_modes": leaderboard_modes,
    }


def validate_document(document: Any) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    def add(path: str, code: str, message: str) -> None:
        errors.append({"path": path, "code": code, "message": message})
    if not isinstance(document, dict) or not isinstance(document.get("bosses"), list):
        return [{"path": "bosses", "code": "required", "message": "bosses must be an array"}]
    seen_keys: set[str] = set()
    aliases: dict[str, str] = {}
    orders: set[int] = set()
    discord_labels: dict[str,str] = {}
    submission_count = 0
    for i, boss in enumerate(document["bosses"]):
        path = f"bosses[{i}]"
        if not isinstance(boss, dict):
            add(path,"invalid","boss must be an object"); continue
        key = str(boss.get("boss_key") or "").strip()
        if not re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", key):
            add(path+".boss_key","invalid_boss_key","use lowercase letters, numbers, and underscores")
        elif key in seen_keys:
            add(path+".boss_key","duplicate_boss_key","boss_key must be unique")
        seen_keys.add(key)
        name = str(boss.get("display_name") or "").strip()
        if not name: add(path+".display_name","required","display name is required")
        metric = boss.get("metric_type")
        if metric not in {"time","numeric","completion"}:
            add(path+".metric_type","invalid_metric_type","metric_type must be time, numeric, or completion")
        expected_direction = {
            "time": "lower", "numeric": "higher", "completion": "complete",
        }.get(metric)
        if expected_direction and boss.get("comparison_direction") != expected_direction:
            add(path+".comparison_direction","invalid_comparison_direction",f"{metric} challenges must use {expected_direction}")
        try:
            order = int(boss.get("display_order"))
            if order < 0: raise ValueError
            if order in orders: add(path+".display_order","duplicate_order","display order must be unique")
            orders.add(order)
        except (TypeError,ValueError): add(path+".display_order","invalid_order","display order must be a non-negative integer")
        label = str(boss.get("discord_label") or name)
        if not label or len(label) > 100:
            add(path+".discord_label","invalid_discord_label","Discord label must contain 1-100 characters")
        elif bool(boss.get("active",True)) and bool(boss.get("submission_enabled",True)):
            submission_count += 1
            normalized_label=label.casefold()
            if normalized_label in discord_labels and discord_labels[normalized_label] != key:
                add(path+".discord_label","duplicate_discord_label",f"Discord label conflicts with {discord_labels[normalized_label]}")
            discord_labels[normalized_label]=key
        emoji = str(boss.get("discord_emoji") or "")
        if len(emoji) > 100 or (emoji and re.fullmatch(r"<a?:[A-Za-z0-9_]+:\d+>",emoji) is None and len(emoji) > 16):
            add(path+".discord_emoji","invalid_discord_emoji","Discord emoji must be a custom emoji token or short Unicode emoji")
        icon_url = str(boss.get("icon_url") or "").strip()
        valid_local_icon = re.fullmatch(
            r"/media/challenge-bosses/[a-z0-9]+(?:_[a-z0-9]+)*-[a-f0-9]{16,64}\.png",
            icon_url,
        )
        valid_canonical_icon = reusable_boss_icon_url(icon_url)
        valid_discord_icon = re.fullmatch(
            r"https://cdn\.discordapp\.com/emojis/\d+\.png(?:\?size=\d+)?",
            icon_url,
        )
        if icon_url and not (valid_local_icon or valid_canonical_icon or valid_discord_icon):
            add(
                path+".icon_url",
                "invalid_icon_url",
                "boss artwork must be a validated PNG upload",
            )
        try:
            party = int(boss.get("min_party_size",1))
            if party < 1 or party > 100: raise ValueError
        except (TypeError,ValueError): add(path+".min_party_size","invalid_party_size","min party size must be 1-100")
        mode = str(boss.get("submission_mode") or "")
        if mode not in {"solo","group","either"}:
            add(path+".submission_mode","invalid_submission_mode","submission mode must be solo, group, or either")
        else:
            supports = bool(boss.get("supports_groups", True))
            party_value = boss.get("min_party_size", 1)
            try: party_value = int(party_value)
            except (TypeError, ValueError): party_value = 0
            expected = (
                (not supports and party_value == 1) if mode == "solo" else
                (supports and party_value >= 2) if mode == "group" else
                (supports and party_value == 1)
            )
            if not expected:
                add(path+".submission_mode","invalid_submission_semantics",f"{mode} conflicts with supports_groups/min_party_size")
        time_format = boss.get("time_input_format")
        if metric == "time":
            if time_format not in TIME_INPUT_FORMATS:
                add(path+".time_input_format","invalid_time_input_format","timed bosses require MM:SS.xx or HH:MM:SS.xx")
        elif time_format is not None:
            add(path+".time_input_format","invalid_time_input_format","non-time bosses must not define a time input format")
        boss_aliases = boss.get("aliases",[])
        if not isinstance(boss_aliases,list):
            add(path+".aliases","invalid_aliases","aliases must be an array"); boss_aliases=[]
        for alias in [name,key,*boss_aliases]:
            if len(str(alias)) > 80:
                add(path+".aliases","invalid_alias","aliases must be 80 characters or fewer"); continue
            normalized = normalize_alias(alias)
            if not normalized:
                add(path+".aliases","invalid_alias","aliases cannot normalize to empty"); continue
            owner = aliases.get(normalized)
            if owner and owner != key:
                add(path+".aliases","alias_conflict",f"alias conflicts with {owner}")
            else:
                aliases[normalized] = key
        tiers = boss.get("tiers")
        if not isinstance(tiers,list):
            add(path+".tiers","required","all five tiers are required"); continue
        by_key = {str(t.get("tier_key") or "").lower(): t for t in tiers if isinstance(t,dict)}
        expected = {item[0] for item in TIER_DEFINITIONS}
        if set(by_key) != expected:
            add(path+".tiers","invalid_tiers","exactly Bronze, Silver, Gold, Platinum, and Ascendant are required")
            continue
        normalized_values: list[int] = []
        for tier_key,tier_name,rank in TIER_DEFINITIONS:
            tier = by_key[tier_key]; tpath = path+f".tiers.{tier_key}"
            if int(tier.get("rank",0) or 0) != rank: add(tpath+".rank","invalid_rank",f"rank must be {rank}")
            display_name = str(tier.get("tier") or "").strip()
            if not display_name or len(display_name) > 30:
                add(tpath+".tier","invalid_tier_name","tier name must contain 1-30 characters")
            try:
                points = int(tier.get("points"))
                if points < 0: raise ValueError
            except (TypeError,ValueError): add(tpath+".points","invalid_points","points must be a non-negative integer")
            if rank == 1:
                if (tier.get("metric_type") != "completion" or
                        tier.get("operator") != "complete" or
                        tier.get("unit") != "boolean" or
                        int(tier.get("threshold",1) or 0) != 1):
                    add(tpath,"invalid_completion","Bronze must use completion/complete/boolean with value 1")
                continue
            if metric == "time":
                try: value,_ = parse_time_threshold(tier.get("threshold_display",tier.get("threshold"))); normalized_values.append(value)
                except ValueError as exc: add(tpath+".threshold","invalid_time_threshold",str(exc))
                if tier.get("operator","lte") != "lte": add(tpath+".operator","invalid_operator","time tiers must use lte")
                if tier.get("metric_type") != "time" or tier.get("unit") != "milliseconds":
                    add(tpath,"invalid_metric_semantics","time tiers must use time/milliseconds semantics")
            elif metric == "numeric":
                try:
                    value = int(tier.get("threshold"));
                    if value < 0: raise ValueError
                    normalized_values.append(value)
                except (TypeError,ValueError): add(tpath+".threshold","invalid_numeric_threshold","numeric threshold must be a non-negative integer")
                if tier.get("operator","gte") != "gte": add(tpath+".operator","invalid_operator","numeric tiers must use gte")
                if tier.get("metric_type") != "numeric" or tier.get("unit") != "waves":
                    add(tpath,"invalid_metric_semantics","numeric tiers must use numeric/waves semantics")
            elif metric == "completion":
                if (tier.get("metric_type") != "completion" or
                        tier.get("operator") != "complete" or
                        tier.get("unit") != "boolean" or
                        int(tier.get("threshold",1) or 0) != 1):
                    add(tpath,"invalid_completion","completion tiers must use completion/complete/boolean with value 1")
        if len(normalized_values) == 4:
            if metric == "time" and not all(a > b for a,b in zip(normalized_values,normalized_values[1:])):
                add(path+".tiers","invalid_tier_order","time thresholds must strictly decrease from Silver to Ascendant")
            if metric == "numeric" and not all(a < b for a,b in zip(normalized_values,normalized_values[1:])):
                add(path+".tiers","invalid_tier_order","numeric thresholds must strictly increase from Silver to Ascendant")
    if submission_count > 25:
        add("bosses","discord_option_limit","Discord supports at most 25 submission-enabled active bosses per menu")
    modes = document.get("leaderboard_modes", [])
    if not isinstance(modes, list):
        add("leaderboard_modes", "invalid_leaderboard_modes", "leaderboard modes must be an array")
        return errors
    boss_by_key = {
        str(item.get("boss_key")): item
        for item in document["bosses"] if isinstance(item, dict)
    }
    mode_keys: set[str] = set()
    mode_orders: set[int] = set()
    mode_aliases: dict[str, str] = {}
    groups: dict[str, tuple[str, int, str | None]] = {}
    group_orders: dict[int, str] = {}
    for i, raw_mode in enumerate(modes):
        path = f"leaderboard_modes[{i}]"
        if not isinstance(raw_mode, dict):
            add(path, "invalid", "leaderboard mode must be an object")
            continue
        mode_key = str(raw_mode.get("mode_key") or "").strip()
        if not re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", mode_key):
            add(path+".mode_key", "invalid_mode_key", "use lowercase letters, numbers, and underscores")
        elif mode_key in mode_keys:
            add(path+".mode_key", "duplicate_mode_key", "mode key must be unique")
        mode_keys.add(mode_key)
        boss_key = str(raw_mode.get("boss_key") or "").strip() or None
        if boss_key is not None and boss_key not in boss_by_key:
            add(path+".boss_key", "missing_boss_reference", "referenced Challenge boss does not exist")
        content_key = str(raw_mode.get("content_key") or "").strip()
        if not re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", content_key):
            add(path+".content_key", "invalid_content_key", "content key is required and must use lowercase letters, numbers, and underscores")
        display_name = str(raw_mode.get("display_name") or "").strip()
        if not display_name or len(display_name) > 100:
            add(path+".display_name", "invalid_mode_display_name", "display name must contain 1-100 characters")
        try:
            order = int(raw_mode.get("display_order"))
            if order < 0:
                raise ValueError
            if order in mode_orders:
                add(path+".display_order", "duplicate_mode_order", "leaderboard display order must be unique")
            mode_orders.add(order)
        except (TypeError, ValueError):
            add(path+".display_order", "invalid_mode_order", "display order must be a non-negative integer")
        metric = str(raw_mode.get("metric_type") or "")
        direction = str(raw_mode.get("comparison_direction") or "")
        valid_directions = {"time": {"lower"}, "numeric": {"lower", "higher"}, "completion": {"complete"}}
        if metric not in valid_directions:
            add(path+".metric_type", "invalid_mode_metric_type", "metric type must be time, numeric, or completion")
        elif direction not in valid_directions[metric]:
            add(path+".comparison_direction", "invalid_mode_comparison", f"{metric} leaderboard mode cannot use {direction or 'an empty comparison'}")
        if boss_key and boss_key in boss_by_key:
            boss = boss_by_key[boss_key]
            if metric != boss.get("metric_type"):
                add(path+".metric_type", "boss_metric_mismatch", "leaderboard metric must match its Challenge boss")
            if direction != boss.get("comparison_direction"):
                add(path+".comparison_direction", "boss_comparison_mismatch", "leaderboard comparison must match its Challenge boss")
        expected_units = {"time": "milliseconds", "completion": "boolean"}
        if metric in expected_units and raw_mode.get("metric_unit") != expected_units[metric]:
            add(path+".metric_unit", "invalid_mode_unit", f"{metric} leaderboard modes must use {expected_units[metric]}")
        elif metric == "numeric" and not str(raw_mode.get("metric_unit") or "").strip():
            add(path+".metric_unit", "invalid_mode_unit", "numeric leaderboard modes require a unit")
        try:
            party_min = int(raw_mode.get("party_size_min"))
            party_max = int(raw_mode.get("party_size_max"))
            if party_min < 1 or party_max > 100 or party_min > party_max:
                raise ValueError
        except (TypeError, ValueError):
            add(path+".party_size", "invalid_party_range", "party sizes must be between 1 and 100, with minimum no greater than maximum")
        try:
            top_n = int(raw_mode.get("top_n", 3))
            if top_n < 1 or top_n > 25:
                raise ValueError
        except (TypeError, ValueError):
            add(path+".top_n", "invalid_top_n", "top N must be between 1 and 25")
        if bool(raw_mode.get("inherit_boss_icon")) and not boss_key:
            add(path+".inherit_boss_icon", "missing_icon_reference", "leaderboard-only modes cannot inherit a Challenge boss icon")
        icon_url = str(raw_mode.get("icon_url") or "").strip()
        if icon_url and not (reusable_boss_icon_url(icon_url) or re.fullmatch(
            r"/media/challenge-bosses/[a-z0-9]+(?:_[a-z0-9]+)*-[a-f0-9]{16,64}\.png", icon_url
        )):
            add(path+".icon_url", "invalid_mode_icon", "leaderboard artwork must use the existing PNG catalog")
        group_key = str(raw_mode.get("publication_group_key") or "").strip()
        group_name = str(raw_mode.get("publication_group_name") or "").strip()
        if not re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", group_key):
            add(path+".publication_group_key", "invalid_publication_group_key", "publication group key is required and must use lowercase letters, numbers, and underscores")
        if not group_name or len(group_name) > 100:
            add(path+".publication_group_name", "invalid_publication_group_name", "publication group name must contain 1-100 characters")
        try:
            group_order = int(raw_mode.get("publication_group_order"))
            if group_order < 0:
                raise ValueError
        except (TypeError, ValueError):
            add(path+".publication_group_order", "invalid_publication_group_order", "publication group order must be a non-negative integer")
            group_order = -1
        group_icon = str(raw_mode.get("group_icon_url") or "").strip() or None
        if group_icon and not (reusable_boss_icon_url(group_icon) or re.fullmatch(
            r"/media/challenge-bosses/[a-z0-9]+(?:_[a-z0-9]+)*-[a-f0-9]{16,64}\.png", group_icon
        )):
            add(path+".group_icon_url", "invalid_publication_group_icon", "publication group artwork must use the existing PNG catalog")
        signature = (group_name, group_order, group_icon)
        if group_key in groups and groups[group_key] != signature:
            add(path+".publication_group_key", "inconsistent_publication_group", "all modes in a publication group must use the same group name, order, and icon")
        elif group_key:
            groups[group_key] = signature
        if group_order >= 0:
            owner = group_orders.get(group_order)
            if owner and owner != group_key:
                add(path+".publication_group_order", "duplicate_publication_group_order", f"publication group order conflicts with {owner}")
            elif group_key:
                group_orders[group_order] = group_key
        aliases_value = raw_mode.get("aliases", [])
        if not isinstance(aliases_value, list):
            add(path+".aliases", "invalid_mode_aliases", "leaderboard aliases must be an array")
            aliases_value = []
        for alias in [mode_key, display_name, *aliases_value]:
            normalized = normalize_alias(alias)
            if not normalized:
                add(path+".aliases", "invalid_mode_alias", "leaderboard aliases cannot normalize to empty")
                continue
            owner = mode_aliases.get(normalized)
            if owner and owner != mode_key:
                add(path+".aliases", "leaderboard_alias_conflict", f"alias conflicts with {owner}")
            else:
                mode_aliases[normalized] = mode_key
    return errors


def create_draft(conn: sqlite3.Connection, actor: str) -> dict[str, Any]:
    open_row = conn.execute("SELECT * FROM challenge_config_drafts WHERE state IN ('draft','validated')").fetchone()
    if open_row:
        return draft_document(conn,int(open_row["draft_id"]))
    document = config_document(conn)
    payload = canonical_json(document)
    cur = conn.execute(
        """INSERT INTO challenge_config_drafts
           (base_config_version_id,state,draft_json,draft_sha256,created_by,updated_by)
           VALUES(?,'draft',?,?,?,?)""",
        (document["version_id"],payload,sha256_text(payload),actor,actor),
    )
    conn.commit()
    return draft_document(conn,int(cur.lastrowid))


def draft_document(conn: sqlite3.Connection, draft_id: int | None = None) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM challenge_config_drafts WHERE draft_id=?" if draft_id else
        "SELECT * FROM challenge_config_drafts WHERE state IN ('draft','validated') ORDER BY draft_id DESC LIMIT 1",
        (draft_id,) if draft_id else (),
    ).fetchone()
    if not row: raise LookupError("No open challenge config draft")
    revision = int(row["revision"])
    return {
        "draft_id": int(row["draft_id"]), "base_version_id": int(row["base_config_version_id"]),
        "state": row["state"], "revision": revision,
        "saved_revision": revision,
        "validated_revision": revision if row["state"] == "validated" else None,
        "updated_at": row["updated_at"], "validation_errors": json.loads(row["validation_errors_json"]),
        "config": json.loads(row["draft_json"]),
    }


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone() is not None


def assert_draft_boss_removable(conn: sqlite3.Connection, boss_key: str) -> None:
    if conn.execute(
        "SELECT 1 FROM challenge_config_bosses WHERE boss_key=? LIMIT 1",
        (boss_key,),
    ).fetchone():
        raise RuntimeError("published_boss_cannot_be_removed")
    for table in (
        "challenge_submissions",
        "challenge_member_tier_achievements",
        "challenge_member_bests",
    ):
        if _table_exists(conn, table) and conn.execute(
            f"SELECT 1 FROM {table} WHERE boss_key=? LIMIT 1",
            (boss_key,),
        ).fetchone():
            raise RuntimeError("historical_boss_cannot_be_removed")


def save_draft(conn: sqlite3.Connection, draft_id: int, document: Any, actor: str, expected_revision: int | None = None) -> dict[str, Any]:
    current = conn.execute("SELECT revision,state,draft_json FROM challenge_config_drafts WHERE draft_id=?",(draft_id,)).fetchone()
    if not current or current["state"] not in ("draft","validated"): raise LookupError("Editable draft not found")
    if expected_revision is not None and int(current["revision"]) != int(expected_revision): raise RuntimeError("draft_revision_conflict")
    if not isinstance(document,dict): raise ValueError("config must be an object")
    previous_document = json.loads(current["draft_json"])
    previous_keys = {
        str(item.get("boss_key"))
        for item in previous_document.get("bosses", [])
        if isinstance(item, dict)
    }
    supplied_keys = {
        str(item.get("boss_key"))
        for item in document.get("bosses", [])
        if isinstance(item, dict)
    }
    for removed_key in sorted(previous_keys - supplied_keys):
        assert_draft_boss_removable(conn, removed_key)
    normalized = normalize_draft_document(document, previous_document)
    payload = canonical_json(normalized)
    conn.execute(
        """UPDATE challenge_config_drafts SET state='draft',revision=revision+1,draft_json=?,draft_sha256=?,
                  validation_errors_json='[]',validated_at=NULL,updated_by=?,updated_at=CURRENT_TIMESTAMP
             WHERE draft_id=?""",
        (payload,sha256_text(payload),actor,draft_id),
    )
    conn.commit()
    return draft_document(conn,draft_id)


def validate_draft(
    conn: sqlite3.Connection,
    draft_id: int,
    actor: str,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    draft = draft_document(conn,draft_id)
    if expected_revision is not None and draft["revision"] != int(expected_revision):
        raise RuntimeError("draft_revision_conflict")
    errors = validate_document(draft["config"])
    existing={str(row[0]) for row in conn.execute("SELECT boss_key FROM challenge_config_bosses WHERE config_version_id=?",(draft["base_version_id"],))}
    supplied={str(item.get("boss_key")) for item in draft["config"].get("bosses",[]) if isinstance(item,dict)}
    for missing in sorted(existing-supplied):
        errors.append({"path":"bosses","code":"boss_removed","message":f"{missing} must be deactivated rather than removed"})
    state = "validated" if not errors else "draft"
    conn.execute(
        """UPDATE challenge_config_drafts SET state=?,validation_errors_json=?,validated_at=?,
                  updated_by=?,updated_at=CURRENT_TIMESTAMP WHERE draft_id=?""",
        (state,canonical_json(errors),now_iso() if not errors else None,actor,draft_id),
    )
    conn.commit()
    return {"valid": not errors,"errors":errors,"draft":draft_document(conn,draft_id)}


def _normalized_for_publish(document: dict[str, Any]) -> dict[str, Any]:
    working = normalize_draft_document(document)
    bosses = working.get("bosses", [])
    for boss in bosses:
        boss["active"] = bool(boss.get("active",True))
        boss["submission_enabled"] = bool(boss.get("submission_enabled",True))
        boss["supports_groups"] = bool(boss.get("supports_groups",True))
        normalize_boss_submission_semantics(boss)
        boss["display_order"] = int(boss["display_order"])
        boss["min_party_size"] = int(boss.get("min_party_size",1))
        boss["discord_label"] = str(boss.get("discord_label") or boss["display_name"]).strip()
        boss["aliases"] = _stable_aliases(boss)
        for tier in boss["tiers"]:
            tier["points"] = int(tier["points"])
            tier["rank"] = int(tier["rank"])
            tier["progression_points"] = int(
                tier.get("progression_points")
                or (5 * tier["rank"] * (tier["rank"] + 1))
            )
            tier["discord_emoji"] = (
                _optional_text(tier.get("discord_emoji"))
                or TIER_EMOJIS.get(str(tier.get("tier_key")))
            )
            if tier["rank"] == 1:
                tier.update({"threshold":1,"threshold_display":"Completion","metric_type":"completion","operator":"complete","unit":"boolean"})
            elif boss["metric_type"] == "time":
                value,display = parse_time_threshold(tier.get("threshold_display",tier.get("threshold")))
                tier.update({"threshold":value,"threshold_display":format_time_threshold(value,boss.get("time_input_format")),"metric_type":"time","operator":"lte","unit":"milliseconds"})
            elif boss["metric_type"] == "numeric":
                value = int(tier["threshold"])
                tier.update({"threshold":value,"threshold_display":str(value),"metric_type":"numeric","operator":"gte","unit":"waves"})
            else:
                tier.update({"threshold":1,"threshold_display":"Completion","metric_type":"completion","operator":"complete","unit":"boolean"})
        boss["tiers"] = sorted(boss["tiers"], key=lambda item: int(item["rank"]))
    bosses = sorted(bosses, key=lambda item: (int(item["display_order"]), str(item["boss_key"])))
    system_tiers = []
    for item in working.get("system_tiers", []):
        system_tiers.append({
            "system_tier_key": str(item["system_tier_key"]),
            "display_name": str(item["display_name"]),
            "tier_rank": int(item["tier_rank"]),
            "min_progression_points": (
                None if item.get("min_progression_points") is None
                else int(item["min_progression_points"])
            ),
            "require_all_active_challenges": bool(item.get("require_all_active_challenges")),
            "one_time_rank_bonus": int(item["one_time_rank_bonus"]),
        })
    system_tiers.sort(key=lambda item: (item["tier_rank"], item["system_tier_key"]))
    leaderboard_modes = []
    for raw_mode in working.get("leaderboard_modes", []):
        mode = normalize_leaderboard_mode(raw_mode)
        leaderboard_modes.append({
            key: mode[key] for key in (
                "mode_key", "boss_key", "content_key", "display_name", "active",
                "display_order", "effective_display_order", "custom_order_override",
                "metric_type", "comparison_direction", "metric_unit",
                "party_size_min", "party_size_max", "top_n", "inherit_boss_icon",
                "icon_url", "aliases", "publication_group_key", "publication_group_name",
                "publication_group_order", "group_icon_url",
            )
        })
    leaderboard_modes.sort(
        key=lambda item: (
            int(item["effective_display_order"]),
            str(item["mode_key"]),
        )
    )
    return {
        "bosses": bosses,
        "system_tiers": system_tiers,
        "leaderboard_modes": leaderboard_modes,
    }


BOSS_DIFF_FIELDS = (
    "display_name", "active", "display_order", "metric_type",
    "comparison_direction", "aliases", "description", "help_text",
    "icon_url", "discord_label", "discord_emoji", "discord_group",
    "submission_enabled", "submission_mode", "supports_groups",
    "min_party_size", "time_input_format",
)
TIER_DIFF_FIELDS = (
    "tier", "rank", "threshold", "threshold_display", "points",
    "metric_type", "operator", "unit", "progression_points",
    "discord_emoji",
)
LEADERBOARD_MODE_DIFF_FIELDS = (
    "boss_key", "content_key", "display_name", "active", "display_order",
    "effective_display_order", "custom_order_override",
    "metric_type", "comparison_direction", "metric_unit", "party_size_min",
    "party_size_max", "top_n", "inherit_boss_icon", "icon_url", "aliases",
    "publication_group_key", "publication_group_name", "publication_group_order", "group_icon_url",
)


def _field_changes(before: dict[str, Any], after: dict[str, Any], fields: tuple[str, ...]) -> list[dict[str, Any]]:
    return [
        {"field": field, "before": before.get(field), "after": after.get(field)}
        for field in fields
        if before.get(field) != after.get(field)
    ]


def config_document_diff(before_document: dict[str, Any], after_document: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized, admin-safe semantic diff between two snapshots."""
    before = _normalized_for_publish(before_document)
    after = _normalized_for_publish(after_document)
    before_bosses = {str(item["boss_key"]): item for item in before["bosses"]}
    after_bosses = {str(item["boss_key"]): item for item in after["bosses"]}
    boss_changes: list[dict[str, Any]] = []
    tier_change_count = 0
    for boss_key in sorted(set(before_bosses) | set(after_bosses)):
        old = before_bosses.get(boss_key)
        new = after_bosses.get(boss_key)
        if old is None:
            tiers = [
                {
                    "tier_key": str(tier["tier_key"]),
                    "change_type": "added",
                    "fields": _field_changes({}, tier, TIER_DIFF_FIELDS),
                }
                for tier in new.get("tiers", [])
            ]
            tier_change_count += len(tiers)
            boss_changes.append({
                "boss_key": boss_key, "change_type": "added",
                "display_name": new.get("display_name"),
                "fields": _field_changes({}, new, BOSS_DIFF_FIELDS),
                "tiers": tiers,
            })
            continue
        if new is None:
            tiers = [
                {
                    "tier_key": str(tier["tier_key"]),
                    "change_type": "removed",
                    "fields": _field_changes(tier, {}, TIER_DIFF_FIELDS),
                }
                for tier in old.get("tiers", [])
            ]
            tier_change_count += len(tiers)
            boss_changes.append({
                "boss_key": boss_key, "change_type": "removed",
                "display_name": old.get("display_name"),
                "fields": _field_changes(old, {}, BOSS_DIFF_FIELDS),
                "tiers": tiers,
            })
            continue
        fields = _field_changes(old, new, BOSS_DIFF_FIELDS)
        old_tiers = {str(item["tier_key"]): item for item in old.get("tiers", [])}
        new_tiers = {str(item["tier_key"]): item for item in new.get("tiers", [])}
        tiers: list[dict[str, Any]] = []
        for tier_key in sorted(set(old_tiers) | set(new_tiers)):
            old_tier, new_tier = old_tiers.get(tier_key), new_tiers.get(tier_key)
            if old_tier is None:
                tiers.append({"tier_key": tier_key, "change_type": "added", "fields": []})
            elif new_tier is None:
                tiers.append({"tier_key": tier_key, "change_type": "removed", "fields": []})
            else:
                changes = _field_changes(old_tier, new_tier, TIER_DIFF_FIELDS)
                if changes:
                    tiers.append({"tier_key": tier_key, "change_type": "modified", "fields": changes})
        if not fields and not tiers:
            continue
        tier_change_count += len(tiers)
        change_type = "modified"
        if bool(old.get("active")) and not bool(new.get("active")):
            change_type = "deactivated"
        elif not bool(old.get("active")) and bool(new.get("active")):
            change_type = "reactivated"
        boss_changes.append({
            "boss_key": boss_key, "change_type": change_type,
            "display_name": new.get("display_name"), "fields": fields, "tiers": tiers,
        })
    system_before = {item["system_tier_key"]: item for item in before["system_tiers"]}
    system_after = {item["system_tier_key"]: item for item in after["system_tiers"]}
    system_changes = []
    for key in sorted(set(system_before) | set(system_after)):
        old, new = system_before.get(key), system_after.get(key)
        if old is None or new is None:
            system_changes.append({
                "system_tier_key": key,
                "change_type": "added" if old is None else "removed",
                "fields": [],
            })
        else:
            changes = _field_changes(old, new, tuple(sorted(set(old) | set(new))))
            if changes:
                system_changes.append({
                    "system_tier_key": key, "change_type": "modified", "fields": changes,
                })
    modes_before = {item["mode_key"]: item for item in before["leaderboard_modes"]}
    modes_after = {item["mode_key"]: item for item in after["leaderboard_modes"]}
    mode_changes = []
    for key in sorted(set(modes_before) | set(modes_after)):
        old, new = modes_before.get(key), modes_after.get(key)
        if old is None or new is None:
            mode_changes.append({
                "mode_key": key,
                "change_type": "added" if old is None else "removed",
                "display_name": (new or old).get("display_name"),
                "fields": _field_changes(old or {}, new or {}, LEADERBOARD_MODE_DIFF_FIELDS),
            })
            continue
        changes = _field_changes(old, new, LEADERBOARD_MODE_DIFF_FIELDS)
        if changes:
            mode_changes.append({
                "mode_key": key,
                "change_type": "modified",
                "display_name": new.get("display_name"),
                "fields": changes,
            })
    counts = Counter(item["change_type"] for item in boss_changes)
    mode_counts = Counter(item["change_type"] for item in mode_changes)
    return {
        "has_changes": bool(boss_changes or system_changes or mode_changes),
        "summary": {
            "bosses_added": counts["added"],
            "bosses_modified": counts["modified"],
            "bosses_deactivated": counts["deactivated"],
            "bosses_reactivated": counts["reactivated"],
            "bosses_removed": counts["removed"],
            "tier_changes": tier_change_count,
            "system_tier_changes": len(system_changes),
            "leaderboard_modes_added": mode_counts["added"],
            "leaderboard_modes_modified": mode_counts["modified"],
            "leaderboard_modes_removed": mode_counts["removed"],
        },
        "bosses": boss_changes,
        "system_tiers": system_changes,
        "leaderboard_modes": mode_changes,
    }


def draft_diff(
    conn: sqlite3.Connection,
    draft_id: int,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    draft = draft_document(conn, draft_id)
    if expected_revision is not None and draft["revision"] != int(expected_revision):
        raise RuntimeError("draft_revision_conflict")
    base = config_document(conn, draft["base_version_id"])
    base_row = conn.execute(
        "SELECT config_json FROM challenge_config_versions WHERE config_version_id=?",
        (draft["base_version_id"],),
    ).fetchone()
    base_source = json.loads(base_row[0]) if base_row and base_row[0] else {}
    # Phase 5C staged additive rows against v3 so consumers could be tested
    # without mutating v3's immutable source JSON. For the first explicit
    # publication, absence from that source JSON means the modes are additions.
    if "leaderboard_modes" not in base_source:
        base["leaderboard_modes"] = []
    result = config_document_diff(base, draft["config"])
    return {
        "draft_id": draft["draft_id"],
        "revision": draft["revision"],
        "validated_revision": draft["validated_revision"],
        "state": draft["state"],
        "base_version_id": draft["base_version_id"],
        **result,
    }


def publish_draft(
    conn: sqlite3.Connection,
    draft_id: int,
    actor: str,
    *,
    confirmed: bool,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    if not confirmed: raise ValueError("publish_confirmation_required")
    draft = draft_document(conn,draft_id)
    if expected_revision is None:
        raise RuntimeError("draft_revision_required")
    if draft["revision"] != int(expected_revision):
        raise RuntimeError("draft_revision_conflict")
    if draft["state"] != "validated" or draft["validated_revision"] != draft["revision"]:
        raise RuntimeError("draft_revision_not_validated")
    errors = validate_document(draft["config"])
    existing={str(row[0]) for row in conn.execute("SELECT boss_key FROM challenge_config_bosses WHERE config_version_id=?",(draft["base_version_id"],))}
    supplied={str(item.get("boss_key")) for item in draft["config"].get("bosses",[]) if isinstance(item,dict)}
    for missing in sorted(existing-supplied):
        errors.append({"path":"bosses","code":"boss_removed","message":f"{missing} must be deactivated rather than removed"})
    if errors: raise ValueError(canonical_json(errors))
    document = _normalized_for_publish(draft["config"])
    active_row = conn.execute(
        "SELECT config_version_id FROM challenge_config_versions WHERE status='active'"
    ).fetchone()
    if not active_row or int(active_row[0]) != int(draft["base_version_id"]):
        raise RuntimeError("draft_base_version_is_no_longer_active")
    active_config = config_document(conn, int(active_row[0]))
    active_source_row = conn.execute(
        "SELECT config_json FROM challenge_config_versions WHERE config_version_id=?",
        (int(active_row[0]),),
    ).fetchone()
    active_source = json.loads(active_source_row[0]) if active_source_row and active_source_row[0] else {}
    if "leaderboard_modes" not in active_source:
        active_config["leaderboard_modes"] = []
    active_document = _normalized_for_publish(active_config)
    if canonical_json(document) == canonical_json(active_document):
        raise RuntimeError("no_changes_to_publish")
    published = canonical_json(document)
    digest = sha256_text(published)
    if conn.execute("SELECT 1 FROM challenge_config_versions WHERE source_snapshot_sha256=?",(digest,)).fetchone():
        raise ValueError("configuration_is_identical_to_an_existing_version")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    version_key = f"midgard-{stamp}-{digest[:8]}"
    now = now_iso()
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = conn.execute("SELECT config_version_id FROM challenge_config_versions WHERE status='active'").fetchone()
        if not current or int(current[0]) != int(draft["base_version_id"]):
            raise RuntimeError("draft_base_version_is_no_longer_active")
        locked_draft = conn.execute(
            "SELECT revision,state,draft_sha256 FROM challenge_config_drafts WHERE draft_id=?",
            (draft_id,),
        ).fetchone()
        if (
            not locked_draft
            or int(locked_draft["revision"]) != int(expected_revision)
            or locked_draft["state"] != "validated"
            or locked_draft["draft_sha256"] != sha256_text(canonical_json(draft["config"]))
        ):
            raise RuntimeError("draft_revision_conflict")
        conn.execute("UPDATE challenge_config_versions SET status='retired',effective_to=? WHERE status='active'",(now,))
        cur = conn.execute(
            """INSERT INTO challenge_config_versions
               (version_key,source_name,source_snapshot_sha256,config_json,status,effective_from)
               VALUES(?, 'Midgard Challenge Admin', ?, ?, 'active', ?)""",
            (version_key,digest,published,now),
        )
        version_id = int(cur.lastrowid)
        for boss in document["bosses"]:
            conn.execute(
                """INSERT INTO challenge_bosses
                   (boss_key,display_name,metric_type,sort_order,is_active,comparison_direction,
                    supports_groups,archived_at,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)
                   ON CONFLICT(boss_key) DO UPDATE SET display_name=excluded.display_name,
                    metric_type=excluded.metric_type,sort_order=excluded.sort_order,is_active=excluded.is_active,
                    comparison_direction=excluded.comparison_direction,supports_groups=excluded.supports_groups,
                    archived_at=excluded.archived_at,updated_at=CURRENT_TIMESTAMP""",
                (boss["boss_key"],boss["display_name"],boss["metric_type"],boss["display_order"],
                 int(boss["active"]),boss.get("comparison_direction") or ("higher" if boss["metric_type"]=="numeric" else "lower"),
                 int(boss["supports_groups"]),None if boss["active"] else now),
            )
            conn.execute(
                """INSERT INTO challenge_config_bosses
                   (config_version_id,boss_key,display_name,is_active,display_order,metric_type,
                    comparison_direction,description,help_text,icon_url,discord_label,discord_emoji,discord_group,
                    submission_enabled,supports_groups,min_party_size,time_input_format)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (version_id,boss["boss_key"],boss["display_name"],int(boss["active"]),boss["display_order"],
                 boss["metric_type"],boss.get("comparison_direction") or ("higher" if boss["metric_type"]=="numeric" else "lower"),
                 boss.get("description"),boss.get("help_text"),boss.get("icon_url"),boss.get("discord_label"),boss.get("discord_emoji"),
                 boss.get("discord_group"),int(boss["submission_enabled"]),int(boss["supports_groups"]),
                 boss["min_party_size"],boss.get("time_input_format")),
            )
            distinct_aliases = {normalize_alias(alias): alias for alias in boss["aliases"]}
            for normalized_alias, alias in distinct_aliases.items():
                conn.execute("INSERT INTO challenge_config_aliases(config_version_id,boss_key,alias,normalized_alias) VALUES(?,?,?,?)",
                             (version_id,boss["boss_key"],alias,normalized_alias))
            for tier in sorted(boss["tiers"],key=lambda item:int(item["rank"])):
                progression = int(tier.get("progression_points") or (5*int(tier["rank"])*(int(tier["rank"])+1)))
                conn.execute(
                    """INSERT INTO challenge_tiers
                       (config_version_id,boss_key,tier_key,display_name,tier_rank,source_submission_points,
                        cumulative_progression_points,requirement_metric_type,requirement_operator,
                        requirement_value,requirement_unit,requirement_display,discord_emoji)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (version_id,boss["boss_key"],tier["tier_key"],tier.get("tier") or tier["tier_key"].title(),
                     tier["rank"],tier["points"],progression,tier["metric_type"],tier["operator"],tier["threshold"],
                     tier["unit"],tier["threshold_display"],tier.get("discord_emoji")),
                )
        _insert_leaderboard_snapshot(conn, version_id, document["leaderboard_modes"])
        source_system = document.get("system_tiers") or config_document(conn,int(current[0]))["system_tiers"]
        for item in source_system:
            conn.execute(
                """INSERT INTO challenge_system_tiers
                   (config_version_id,system_tier_key,display_name,tier_rank,min_progression_points,
                    require_all_active_challenges,one_time_rank_bonus) VALUES(?,?,?,?,?,?,?)""",
                (version_id,item["system_tier_key"],item["display_name"],item["tier_rank"],
                 item.get("min_progression_points"),int(bool(item.get("require_all_active_challenges"))),item["one_time_rank_bonus"]),
            )
        conn.execute(
            """UPDATE challenge_config_drafts SET state='published',published_config_version_id=?,published_at=?,
                      updated_by=?,updated_at=CURRENT_TIMESTAMP WHERE draft_id=?""",
            (version_id,now,actor,draft_id),
        )
        audit = canonical_json({"draft_id":draft_id,"base_version_id":draft["base_version_id"],"published_version_id":version_id,"sha256":digest})
        conn.execute(
            """INSERT INTO challenge_audit_log
               (event_type,actor_type,actor_id,entity_type,entity_id,reason,event_payload_json,event_payload_sha256)
               VALUES('challenge_config_published','admin',?,'challenge_config_version',?,'Validated draft published',?,?)""",
            (actor,str(version_id),audit,sha256_text(audit)),
        )
        conn.commit()
        return config_document(conn,version_id)
    except Exception:
        conn.rollback(); raise


def resolve_boss(conn: sqlite3.Connection, value: str, version_id: int | None = None) -> sqlite3.Row | None:
    if version_id is None:
        row = conn.execute("SELECT config_version_id FROM challenge_config_versions WHERE status='active'").fetchone()
        if not row: return None
        version_id = int(row[0])
    normalized = normalize_alias(value)
    return conn.execute(
        """SELECT b.* FROM challenge_config_aliases a
             JOIN challenge_config_bosses b ON b.config_version_id=a.config_version_id AND b.boss_key=a.boss_key
            WHERE a.config_version_id=? AND a.normalized_alias=?""",
        (version_id,normalized),
    ).fetchone()


def version_history(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(
        """SELECT config_version_id AS version_id,version_key,status,source_name,effective_from,
                  effective_to,created_at,source_snapshot_sha256
             FROM challenge_config_versions ORDER BY config_version_id DESC"""
    )]


def evaluation_catalog(conn: sqlite3.Connection, version_id: int | None = None) -> dict[str, Any]:
    document = config_document(conn,version_id)
    bosses: dict[str,dict[str,Any]] = {}
    aliases: dict[str,str] = {}
    for boss in document["bosses"]:
        tiers_by_name: dict[str,dict[str,Any]] = {}
        tiers_by_rank: dict[int,dict[str,Any]] = {}
        for tier in boss["tiers"]:
            tiers_by_name[str(tier["tier_key"]).casefold()] = tier
            tiers_by_name[str(tier["tier"]).casefold()] = tier
            tiers_by_rank[int(tier["rank"])] = tier
        entry={**boss,"tiers_by_name":tiers_by_name,"tiers_by_rank":tiers_by_rank}
        bosses[str(boss["boss_key"])]=entry
        for alias in [boss["boss_key"],boss["display_name"],*boss["aliases"]]:
            aliases[normalize_alias(alias)]=str(boss["boss_key"])
    return {"version_id":document["version_id"],"bosses":bosses,"aliases":aliases,
            "system_tiers":document["system_tiers"]}
