"""Resolve immutable leaderboard proof evidence to safe public display URLs."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


PROOF_ROOT = Path("/srv/projects/website/proofs")
PUBLIC_ORIGIN = "https://nocturne.events"
PUBLIC_PREFIX = "/proofs/"
UNAVAILABLE_MAP_PATH = Path(__file__).with_name("leaderboard_proof_404.json")


def _load_verified_404_map() -> dict[int, str]:
    """Load the reviewed historical 404 set; never perform network checks here."""
    try:
        payload = json.loads(UNAVAILABLE_MAP_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if payload.get("schema_version") != 1 or int(payload.get("unavailable_count", -1)) != len(payload.get("entries", [])):
        return {}
    result: dict[int, str] = {}
    for item in payload["entries"]:
        try:
            if int(item.get("verified_http_status")) != 404:
                continue
            result[int(item["observation_id"])] = str(item["stored_proof_url_sha256"])
        except (KeyError, TypeError, ValueError):
            continue
    return result


VERIFIED_HISTORICAL_404S = _load_verified_404_map()


def _image_type(path: Path) -> str | None:
    """Return the verified image type without trusting the filename/MIME value."""
    try:
        with path.open("rb") as handle:
            head = handle.read(32)
    except OSError:
        return None
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def _explicit_local_references(source_rows_json: Any) -> list[str]:
    try:
        rows = json.loads(str(source_rows_json or "[]"))
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(rows, list):
        return []
    refs = {
        str(row.get("local_proof_reference") or "").strip()
        for row in rows
        if isinstance(row, dict) and row.get("local_proof_reference")
    }
    return sorted(ref for ref in refs if ref)


def _cached_path(reference: str) -> Path | None:
    parsed = urlparse(reference)
    if parsed.scheme or parsed.netloc or not parsed.path.startswith(PUBLIC_PREFIX):
        return None
    filename = parsed.path.removeprefix(PUBLIC_PREFIX)
    if not filename or Path(filename).name != filename:
        return None
    return PROOF_ROOT / filename


def _http_url(value: Any) -> str | None:
    url = str(value or "").strip()
    parsed = urlparse(url)
    return url if parsed.scheme in {"http", "https"} and parsed.netloc else None


def _source_indexes(source_rows_json: Any) -> list[str]:
    try:
        rows = json.loads(str(source_rows_json or "[]"))
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(rows, list):
        return []
    values = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        value = row.get("google_index", row.get("source_index", row.get("Index")))
        if value is not None and str(value).isdigit():
            values.add(str(value))
    return sorted(values)


def resolve_proof(
    original_url: Any,
    source_rows_json: Any,
    observation_id: int | None = None,
) -> dict[str, str | None]:
    """Resolve local evidence first, then original URL unless this exact row is known 404."""
    references = _explicit_local_references(source_rows_json)
    for reference in references:
        path = _cached_path(reference)
        if path is not None and path.is_file() and _image_type(path):
            return {"proof_url": f"{PUBLIC_ORIGIN}{reference}", "proof_status": "cached"}

    # The historical importer and Java bot both use this exact source-index naming
    # convention. This also recognizes any newly recovered image without changing
    # immutable observation evidence.
    for index in _source_indexes(source_rows_json):
        reference = f"{PUBLIC_PREFIX}proof_{index}.png"
        path = _cached_path(reference)
        if path is not None and path.is_file() and _image_type(path):
            return {"proof_url": f"{PUBLIC_ORIGIN}{reference}", "proof_status": "cached"}

    original = _http_url(original_url)
    if observation_id is not None and original:
        expected_hash = VERIFIED_HISTORICAL_404S.get(int(observation_id))
        actual_hash = hashlib.sha256(str(original_url).encode("utf-8")).hexdigest()
        if expected_hash and expected_hash == actual_hash:
            return {"proof_url": None, "proof_status": "unavailable"}
    if original:
        return {"proof_url": original, "proof_status": "original"}
    return {"proof_url": None, "proof_status": "unavailable"}


def resolve_proof_url(
    original_url: Any,
    source_rows_json: Any,
    observation_id: int | None = None,
) -> str | None:
    """Backward-compatible URL-only wrapper around :func:`resolve_proof`."""
    return resolve_proof(original_url, source_rows_json, observation_id)["proof_url"]
