#!/usr/bin/env python3
"""Render Midgard leaderboard Discord payloads without contacting Discord.

The only live data source is the read-only Midgard HTTP API.  This process has
no Discord dependency, token, client, send method, or message-edit method.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


DEFAULT_API_BASE = "http://127.0.0.1:5002"
DEFAULT_PUBLIC_BASE = "https://nocturne.events"
DEFAULT_STATE_DIR = Path("/srv/projects/nocturne-services/state")
MEDALS = ("🥇", "🥈", "🥉")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ApiError(RuntimeError):
    pass


class MidgardApi:
    def __init__(self, base_url: str = DEFAULT_API_BASE, opener: Callable[..., Any] | None = None):
        self.base_url = base_url.rstrip("/")
        self.opener = opener or urllib.request.urlopen

    def get(self, path: str) -> dict[str, Any]:
        request = urllib.request.Request(
            self.base_url + path,
            headers={"Accept": "application/json", "User-Agent": "nocturne-leaderboard-shadow/1"},
            method="GET",
        )
        try:
            with self.opener(request, timeout=15) as response:
                if int(getattr(response, "status", 200)) != 200:
                    raise ApiError(f"Midgard returned HTTP {response.status} for {path}")
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise ApiError(f"Midgard request failed for {path}: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise ApiError(f"Midgard returned an invalid payload for {path}")
        return payload


def absolute_icon(icon_url: str | None, public_base: str = DEFAULT_PUBLIC_BASE) -> str | None:
    if not icon_url:
        return None
    if icon_url.startswith("/"):
        return public_base.rstrip("/") + icon_url
    return icon_url


def render_mode(board: dict[str, Any], public_base: str = DEFAULT_PUBLIC_BASE) -> dict[str, Any]:
    mode = board["mode"]
    lines: list[str] = []
    semantic_entries: list[dict[str, Any]] = []
    for entry in board.get("entries", [])[: int(board.get("top_n", 3))]:
        rank = int(entry["rank"])
        names = [str(item.get("display_name") or item.get("primary_rsn") or "Unknown") for item in entry.get("participants", [])]
        member_ids = sorted(int(item["member_id"]) for item in entry.get("participants", []) if item.get("member_id") is not None)
        metric = str(entry["metric"]["display"])
        proof = entry.get("proof_url")
        medal = MEDALS[rank - 1] if 1 <= rank <= len(MEDALS) else f"#{rank}"
        line = f"{medal} — {metric} — {', '.join(names) or 'Unknown'}"
        if proof:
            line += f" — [Proof]({proof})"
        elif entry.get("proof_status") == "unavailable":
            line += " — Proof unavailable"
        lines.append(line)
        semantic_entries.append({
            "rank": rank,
            "observation_id": int(entry["observation_id"]),
            "metric_value": int(entry["metric"]["normalized"]),
            "metric_display": metric,
            "participant_member_ids": member_ids,
            "participant_names": names,
            "participants": [
                {
                    "member_id": (
                        int(participant["member_id"])
                        if participant.get("member_id") is not None else None
                    ),
                    "primary_rsn": participant.get("primary_rsn"),
                    "display_name": participant.get("display_name"),
                }
                for participant in entry.get("participants", [])
            ],
            "proof_url": proof,
            "proof_status": entry.get("proof_status", "original" if proof else "unavailable"),
            "proof_present": bool(proof),
        })
    if lines:
        field_value = "\n".join(lines)
        if len(field_value) > 1024:
            raise ValueError(f"Discord leaderboard field exceeds 1024 characters for {mode['mode_key']}")
        fields = [{"name": "Leaderboard", "value": field_value, "inline": False}]
    else:
        fields = [{"name": "No records yet", "value": "No Midgard leaderboard records found for this category.", "inline": False}]
    embed: dict[str, Any] = {
        "title": str(mode["display_name"]),
        "color": 0x00BFFF,
        "fields": fields,
        "footer": {"text": f"Midgard leaderboard · Config v{board['config_version_id']}"},
    }
    icon = absolute_icon(mode.get("icon_url"), public_base)
    if len(embed["title"]) > 256:
        raise ValueError(f"Discord embed title exceeds 256 characters for {mode['mode_key']}")
    if icon:
        embed["thumbnail"] = {"url": icon}
    return {
        "mode_key": str(mode["mode_key"]),
        "config_version_id": int(board["config_version_id"]),
        "last_calculated_at": board.get("last_calculated_at"),
        "mode": mode,
        "embed": embed,
        "semantic_entries": semantic_entries,
        "render_sha256": digest(embed),
    }


def render_dashboard(config_version_id: int, rendered: list[dict[str, Any]]) -> dict[str, Any]:
    if len(rendered) > 25:
        raise ValueError("Discord select menus support at most 25 leaderboard modes")
    options = []
    for item in rendered:
        mode = item["mode"]
        label = str(mode["display_name"])
        key = str(mode["mode_key"])
        if not label or len(label) > 100 or not key or len(key) > 100:
            raise ValueError(f"Discord option limits exceeded for {key!r}")
        options.append({
            "label": label,
            "value": key,
            "description": f"Top {int(mode['top_n'])} · {str(mode['metric_type']).title()}",
        })
    return {
        "embeds": [{
            "title": "Nocturne Leaderboards",
            "description": "Choose a category below to view the current Midgard leaderboard.",
            "color": 0x00BFFF,
            "footer": {"text": f"Published leaderboard config v{config_version_id}"},
        }],
        "components": [{
            "type": 1,
            "components": [{
                "type": 3,
                "custom_id": f"midgard:leaderboard:select:v{config_version_id}",
                "placeholder": "Choose a leaderboard",
                "min_values": 1,
                "max_values": 1,
                "options": options,
            }],
        }],
    }


def _embed_characters(embed: dict[str, Any]) -> int:
    total = len(str(embed.get("title") or "")) + len(str(embed.get("description") or ""))
    total += len(str((embed.get("footer") or {}).get("text") or ""))
    total += len(str((embed.get("author") or {}).get("name") or ""))
    for field in embed.get("fields", []):
        total += len(str(field.get("name") or "")) + len(str(field.get("value") or ""))
    return total


def render_groups(config_version_id: int, rendered: list[dict[str, Any]], public_base: str = DEFAULT_PUBLIC_BASE) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    seen_modes: set[str] = set()
    for item in rendered:
        mode = item["mode"]
        key = str(mode.get("publication_group_key") or "")
        name = str(mode.get("publication_group_name") or "")
        order = int(mode.get("publication_group_order"))
        if not key or not name:
            raise ValueError(f"Missing publication group metadata for {mode['mode_key']}")
        signature = (name, order, mode.get("group_icon_url"))
        group = groups.setdefault(key, {"signature": signature, "modes": []})
        if group["signature"] != signature:
            raise ValueError(f"Inconsistent publication group metadata for {key}")
        mode_key = str(mode["mode_key"])
        if mode_key in seen_modes:
            raise ValueError(f"Duplicate rendered mode {mode_key}")
        seen_modes.add(mode_key)
        group["modes"].append(item)
    result = []
    for key, group in sorted(groups.items(), key=lambda pair: (pair[1]["signature"][1], pair[0])):
        name, order, icon_url = group["signature"]
        modes = sorted(group["modes"], key=lambda item: (int(item["mode"]["display_order"]), item["mode_key"]))
        header: dict[str, Any] = {
            "title": f"🏆 {name}",
            "color": 0x00BFFF,
            "footer": {"text": f"Midgard leaderboard · Config v{config_version_id}"},
        }
        icon = absolute_icon(icon_url, public_base)
        if icon:
            header["thumbnail"] = {"url": icon}
        mode_embeds = []
        for item in modes:
            embed = json.loads(json.dumps(item["embed"]))
            embed.pop("footer", None)
            mode_embeds.append(embed)
        embeds = [header, *mode_embeds]
        if len(embeds) > 10:
            raise ValueError(f"Discord group {key} exceeds 10 embeds")
        if sum(_embed_characters(embed) for embed in embeds) > 6000:
            raise ValueError(f"Discord group {key} exceeds 6000 embed characters")
        payload = {"embeds": embeds, "components": []}
        result.append({
            "publication_group_key": key,
            "publication_group_name": name,
            "publication_group_order": order,
            "group_icon_url": icon_url,
            "mode_keys": [item["mode_key"] for item in modes],
            "ranked_position_count": sum(len(item["semantic_entries"]) for item in modes),
            "payload": payload,
            "payload_sha256": digest(payload),
        })
    if sum(len(group["mode_keys"]) for group in result) != len(rendered):
        raise ValueError("Grouped renderer did not preserve every active mode")
    return result


def compare_reference(rendered: list[dict[str, Any]], reference: dict[str, Any] | None) -> dict[str, Any]:
    if not reference:
        return {"available": False, "summary": {}, "modes": []}
    expected = {str(item["mode_key"]): item for item in reference.get("modes", [])}
    actual = {str(item["mode_key"]): item for item in rendered}
    details = []
    summary = {"MATCH": 0, "METRIC_DIFFERENCE": 0, "PARTICIPANT_DIFFERENCE": 0, "FORMAT_ONLY": 0}
    for key in sorted(set(expected) | set(actual)):
        old = expected.get(key)
        new = actual.get(key)
        reasons: list[str] = []
        classification = "MATCH"
        if old is None or new is None:
            classification = "FORMAT_ONLY"
            reasons.append("mode_presence_changed")
        else:
            old_entries = old.get("entries", [])
            new_entries = new.get("semantic_entries", [])
            old_metrics = [(int(row["rank"]), int(row["metric_value"])) for row in old_entries]
            new_metrics = [(int(row["rank"]), int(row["metric_value"])) for row in new_entries]
            if old_metrics != new_metrics:
                classification = "METRIC_DIFFERENCE"
                reasons.append("rank_or_metric_changed")
            else:
                old_parties = [(int(row["rank"]), sorted(row.get("participant_member_ids", []))) for row in old_entries]
                new_parties = [(int(row["rank"]), sorted(row.get("participant_member_ids", []))) for row in new_entries]
                if old_parties != new_parties:
                    classification = "PARTICIPANT_DIFFERENCE"
                    reasons.append("participant_set_changed")
                else:
                    if old.get("title") != new["embed"].get("title"):
                        reasons.append("title_format_changed")
                    if old.get("icon_url") != new["embed"].get("thumbnail", {}).get("url"):
                        reasons.append("icon_source_changed")
                    old_proofs = [(int(row["rank"]), bool(row.get("proof_url"))) for row in old_entries]
                    new_proofs = [(int(row["rank"]), bool(row.get("proof_url"))) for row in new_entries]
                    if old_proofs != new_proofs:
                        reasons.append("proof_availability_changed")
                    elif any(
                        str(a.get("proof_url") or "") != str(b.get("proof_url") or "")
                        for a, b in zip(old_entries, new_entries)
                    ):
                        reasons.append("proof_url_representation_changed")
                    if any(row.get("participant_rendering") == "discord_mentions" for row in old_entries):
                        reasons.append("names_render_as_rsn_in_shadow")
                    if reasons:
                        classification = "FORMAT_ONLY"
        summary[classification] += 1
        details.append({"mode_key": key, "classification": classification, "reasons": sorted(set(reasons))})
    return {"available": True, "reference_sha256": digest(reference), "summary": summary, "modes": details}


@dataclass
class ShadowRenderer:
    api: MidgardApi
    state_dir: Path = DEFAULT_STATE_DIR
    public_base: str = DEFAULT_PUBLIC_BASE

    @property
    def cache_path(self) -> Path:
        return self.state_dir / "leaderboard_shadow_lkg.json"

    @property
    def report_path(self) -> Path:
        return self.state_dir / "leaderboard_shadow_report.json"

    def fetch_snapshot(self) -> dict[str, Any]:
        catalog = self.api.get("/api/leaderboards/modes")
        modes = sorted(
            (mode for mode in catalog.get("modes", []) if mode.get("active")),
            key=lambda mode: (int(mode["display_order"]), str(mode["mode_key"])),
        )
        boards = [self.api.get(f"/api/leaderboards/{urllib.parse.quote(str(mode['mode_key']), safe='')}") for mode in modes]
        snapshot = {
            "config_version_id": int(catalog["config_version_id"]),
            "modes": modes,
            "boards": boards,
        }
        snapshot["snapshot_sha256"] = digest(snapshot)
        return snapshot

    def run(self, reference: dict[str, Any] | None = None) -> dict[str, Any]:
        previous = None
        if self.cache_path.exists():
            previous = json.loads(self.cache_path.read_text(encoding="utf-8"))
        try:
            snapshot = self.fetch_snapshot()
            rendered = [render_mode(board, self.public_base) for board in snapshot["boards"]]
            dashboard = render_dashboard(int(snapshot["config_version_id"]), rendered)
            grouped = render_groups(int(snapshot["config_version_id"]), rendered, self.public_base)
            source = "midgard_api"
            atomic_json(self.cache_path, snapshot)
            error = None
        except (ApiError, KeyError, TypeError, ValueError) as exc:
            if previous is None:
                raise
            snapshot = previous
            source = "last_known_good"
            error = str(exc)
            rendered = [render_mode(board, self.public_base) for board in snapshot["boards"]]
            dashboard = render_dashboard(int(snapshot["config_version_id"]), rendered)
            grouped = render_groups(int(snapshot["config_version_id"]), rendered, self.public_base)
        changed = bool(previous and previous.get("snapshot_sha256") != snapshot.get("snapshot_sha256"))
        report = {
            "generated_at": utc_now(),
            "shadow_mode": True,
            "discord_write_capability": False,
            "source": source,
            "source_error": error,
            "config_version_id": int(snapshot["config_version_id"]),
            "mode_count": len(rendered),
            "config_changed_since_previous": changed,
            "snapshot_sha256": snapshot.get("snapshot_sha256"),
            "dashboard": dashboard,
            "rendered_modes": rendered,
            "rendered_groups": grouped,
            "parity": compare_reference(rendered, reference),
        }
        report["report_sha256"] = digest(report)
        atomic_json(self.report_path, report)
        return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-base", default=DEFAULT_API_BASE)
    parser.add_argument("--public-base", default=DEFAULT_PUBLIC_BASE)
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--java-reference", type=Path)
    args = parser.parse_args()
    reference = json.loads(args.java_reference.read_text(encoding="utf-8")) if args.java_reference else None
    report = ShadowRenderer(MidgardApi(args.api_base), args.state_dir, args.public_base).run(reference)
    print(json.dumps({
        "ok": True,
        "shadow_mode": report["shadow_mode"],
        "discord_write_capability": report["discord_write_capability"],
        "source": report["source"],
        "config_version_id": report["config_version_id"],
        "mode_count": report["mode_count"],
        "parity": report["parity"].get("summary", {}),
        "report_path": str(args.state_dir / "leaderboard_shadow_report.json"),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
