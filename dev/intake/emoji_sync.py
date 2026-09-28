"""Bounded Discord guild-emoji synchronizer with atomic public generations.

The runtime transport is deliberately tiny. Tests inject a fake transport and
never contact Discord. Only the guild emoji list and eligible image bytes are
consumed; raw Discord responses are never written to the public mirror.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from io import BytesIO
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import tempfile
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler
import warnings
import zlib

from PIL import Image


SCHEMA_VERSION = 1
MAX_EMOJIS = 256
MAX_SOURCE_EMOJIS = 512
MAX_INPUT_BYTES = 256 * 1024
MAX_OUTPUT_BYTES = 8 * 1024
MAX_DECODED_PIXELS = 512 * 512
MAX_DIMENSION = 512
CANVAS_SIZE = 20
MAX_GENERATIONS = 3
MAX_TOTAL_GENERATION_BYTES = MAX_EMOJIS * MAX_OUTPUT_BYTES + 256 * 1024
NAME = re.compile(r"[a-z0-9_]{1,32}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
DISCORD_ID = re.compile(r"[0-9]{1,24}\Z")
API_BASE = "https://discord.com/api/v10"
CDN_BASE = "https://cdn.discordapp.com/emojis"

log = logging.getLogger("nocturne-emoji-sync")


class SyncFailure(RuntimeError):
    """A bounded synchronization failure safe to summarize operationally."""

    def __init__(self, category, retry_after=None):
        super().__init__(category)
        self.category = category
        self.retry_after = retry_after


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class DiscordTransport:
    """Production HTTP boundary. Authorization is used only for the list call."""

    def __init__(self, timeout=10):
        self.timeout = timeout
        self.opener = build_opener(NoRedirect())

    def list_emojis(self, guild_id, token):
        url = f"{API_BASE}/guilds/{guild_id}/emojis"
        request = Request(url, headers={"Authorization": f"Bot {token}",
                                        "Accept": "application/json"})
        return self._request(request, MAX_INPUT_BYTES)

    def fetch_asset(self, emoji_id, animated):
        extension = "gif" if animated else "png"
        request = Request(f"{CDN_BASE}/{emoji_id}.{extension}",
                          headers={"Accept": "image/gif,image/png"})
        return self._request(request, MAX_INPUT_BYTES)

    def _request(self, request, maximum):
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(maximum + 1)
                if len(raw) > maximum:
                    raise SyncFailure("response_too_large")
                return response.status, dict(response.headers.items()), raw
        except HTTPError as error:
            # Do not retain or expose response bodies/headers. Retry-After is the
            # only operational field consumed, and is normalized immediately.
            retry = _retry_after(error.headers.get("Retry-After")) if error.code == 429 else None
            raise SyncFailure("rate_limited" if error.code == 429 else "api_error", retry) from None
        except (URLError, TimeoutError, OSError):
            raise SyncFailure("transport_error") from None


def _retry_after(value):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if 1 <= parsed <= 3600 else None


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _strict_json(raw):
    try:
        return json.loads(raw.decode("utf-8", errors="strict"),
                          object_pairs_hook=_strict_object,
                          parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise SyncFailure("malformed_json") from error


def normalize_name(value):
    if not isinstance(value, str):
        return None
    normalized = unicodedata.normalize("NFKC", value).lower()
    return normalized if NAME.fullmatch(normalized) else None


def parse_denylist(values):
    result = set()
    for raw in values:
        value = raw.strip()
        normalized = normalize_name(value)
        if DISCORD_ID.fullmatch(value) or normalized is not None:
            result.add(value if DISCORD_ID.fullmatch(value) else normalized)
        elif value:
            raise ValueError("invalid denylist entry")
        if len(result) > MAX_EMOJIS:
            raise ValueError("denylist exceeds limit")
    return frozenset(result)


def _png_container(raw):
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return False
    offset = 8
    seen_iend = False
    while offset + 12 <= len(raw):
        length = int.from_bytes(raw[offset:offset + 4], "big")
        kind = raw[offset + 4:offset + 8]
        end = offset + 12 + length
        if length > MAX_INPUT_BYTES or end > len(raw):
            return False
        payload = raw[offset + 8:offset + 8 + length]
        crc = int.from_bytes(raw[offset + 8 + length:end], "big")
        if zlib.crc32(kind + payload) & 0xffffffff != crc:
            return False
        offset = end
        if kind == b"IEND":
            seen_iend = True
            break
    return seen_iend and offset == len(raw)


def normalize_image(raw, content_type, animated_source):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_INPUT_BYTES:
        raise SyncFailure("invalid_image")
    expected = "image/gif" if animated_source else "image/png"
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if media_type != expected:
        raise SyncFailure("invalid_image_type")
    if animated_source:
        if not raw.startswith((b"GIF87a", b"GIF89a")) or not raw.endswith(b";"):
            raise SyncFailure("invalid_image_container")
    elif not _png_container(raw):
        raise SyncFailure("invalid_image_container")

    previous_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = MAX_DECODED_PIXELS
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as source:
                if source.format not in ({"GIF"} if animated_source else {"PNG"}):
                    raise SyncFailure("invalid_image_type")
                width, height = source.size
                if (width < 1 or height < 1 or width > MAX_DIMENSION or height > MAX_DIMENSION
                        or width * height > MAX_DECODED_PIXELS):
                    raise SyncFailure("invalid_image_dimensions")
                source.seek(0)
                frame = source.convert("RGBA")
                frame.thumbnail((CANVAS_SIZE, CANVAS_SIZE), Image.Resampling.LANCZOS)
                canvas = Image.new("RGBA", (CANVAS_SIZE, CANVAS_SIZE), (0, 0, 0, 0))
                canvas.alpha_composite(frame, ((CANVAS_SIZE - frame.width) // 2,
                                               (CANVAS_SIZE - frame.height) // 2))
                output = BytesIO()
                canvas.save(output, format="PNG", optimize=False, compress_level=9)
    except SyncFailure:
        raise
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise SyncFailure("invalid_image") from error
    finally:
        Image.MAX_IMAGE_PIXELS = previous_limit
    normalized = output.getvalue()
    if len(normalized) > MAX_OUTPUT_BYTES or not _png_container(normalized):
        raise SyncFailure("normalized_image_too_large")
    return normalized


def _headers_content_type(headers):
    for key, value in headers.items():
        if key.lower() == "content-type":
            return value
    return None


def eligible_metadata(raw, denylist):
    values = _strict_json(raw)
    if not isinstance(values, list) or len(values) > MAX_SOURCE_EMOJIS:
        raise SyncFailure("invalid_emoji_count")
    candidates = []
    id_counts = Counter()
    name_counts = Counter()
    for value in values:
        if not isinstance(value, dict):
            continue
        emoji_id = value.get("id")
        name = normalize_name(value.get("name"))
        roles = value.get("roles", [])
        animated = value.get("animated", False)
        available = value.get("available", True)
        managed = value.get("managed", False)
        if (not isinstance(emoji_id, str) or not DISCORD_ID.fullmatch(emoji_id)
                or name is None or type(animated) is not bool or type(available) is not bool
                or type(managed) is not bool or not isinstance(roles, list)):
            continue
        if not available or managed or roles or emoji_id in denylist or name in denylist:
            continue
        candidate = (emoji_id, name, animated)
        candidates.append(candidate)
        id_counts[emoji_id] += 1
        name_counts[name] += 1
    return [value for value in candidates
            if id_counts[value[0]] == 1 and name_counts[value[1]] == 1]


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _fsync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write(path, raw, mode=0o644):
    with path.open("xb") as output:
        os.fchmod(output.fileno(), mode)
        output.write(raw)
        output.flush()
        os.fsync(output.fileno())


def read_current(root):
    root = Path(root)
    current = root / "current"
    if not current.is_symlink():
        return None
    target = os.readlink(current)
    match = re.fullmatch(r"generations/([0-9a-f]{64})", target)
    if not match:
        return None
    manifest = root / target / "manifest.json"
    if not manifest.is_file() or manifest.is_symlink() or manifest.stat().st_size > 256 * 1024:
        return None
    try:
        value = json.loads(manifest.read_text(encoding="utf-8"), object_pairs_hook=_strict_object)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return None
    return value if value.get("revision") == match.group(1) else None


@contextmanager
def sync_lock(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o755)
    lock_path = root / ".sync.lock"
    with lock_path.open("a+b") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncFailure("sync_in_progress") from None
        yield


class EmojiSynchronizer:
    def __init__(self, output, transport, clock=None, denylist=()):
        self.output = Path(output)
        self.transport = transport
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.denylist = parse_denylist(denylist)

    def synchronize(self, guild_id, token):
        if not isinstance(guild_id, str) or not DISCORD_ID.fullmatch(guild_id):
            raise ValueError("invalid guild ID")
        if not isinstance(token, str) or not token or len(token) > 4096:
            raise ValueError("invalid credential")
        with sync_lock(self.output):
            try:
                status, headers, raw = self.transport.list_emojis(guild_id, token)
            except SyncFailure:
                raise
            except Exception:
                raise SyncFailure("transport_error") from None
            if status == 429:
                raise SyncFailure("rate_limited", _retry_after(_header(headers, "retry-after")))
            if status != 200:
                raise SyncFailure("api_error")
            if _media_type(_header(headers, "content-type")) != "application/json":
                raise SyncFailure("malformed_json")
            candidates = eligible_metadata(raw, self.denylist)
            if len(candidates) > MAX_EMOJIS:
                raise SyncFailure("eligible_count_exceeded")
            entries = []
            assets = {}
            total = 0
            for emoji_id, name, animated in sorted(candidates, key=lambda item: (item[1], item[0])):
                try:
                    asset_status, asset_headers, source = self.transport.fetch_asset(emoji_id, animated)
                    if asset_status == 429:
                        raise SyncFailure("rate_limited", _retry_after(_header(asset_headers, "retry-after")))
                    if asset_status >= 500:
                        raise SyncFailure("asset_transport_error")
                    if asset_status != 200:
                        continue
                    normalized = normalize_image(source, _headers_content_type(asset_headers), animated)
                except SyncFailure as error:
                    if error.category in {"rate_limited", "transport_error", "asset_transport_error"}:
                        raise
                    continue
                except Exception:
                    raise SyncFailure("asset_transport_error") from None
                digest = hashlib.sha256(normalized).hexdigest()
                assets[digest] = normalized
                total += len(normalized)
                if total > MAX_TOTAL_GENERATION_BYTES:
                    raise SyncFailure("generation_size_exceeded")
                entries.append({"name": name, "sha256": digest, "width": CANVAS_SIZE,
                                "height": CANVAS_SIZE, "byte_length": len(normalized),
                                "animated_source": animated,
                                "asset_path": f"/api/plugin/v1/emojis/assets/{digest}.png"})
            entries.sort(key=lambda value: value["name"])
            revision = hashlib.sha256(_canonical(entries)).hexdigest()
            current = read_current(self.output)
            if current is not None and current.get("revision") == revision:
                return {"status": "unchanged", "revision": revision,
                        "emoji_count": len(entries)}
            generated = self.clock()
            if not isinstance(generated, datetime) or generated.tzinfo is None:
                raise ValueError("clock must return an aware datetime")
            manifest = {"schema_version": SCHEMA_VERSION, "revision": revision,
                        "generated_at": generated.astimezone(timezone.utc).replace(microsecond=0)
                        .isoformat().replace("+00:00", "Z"),
                        "source_status": "ok_empty" if not entries else "ok",
                        "emojis": entries}
            self._publish(revision, manifest, assets)
            self._cleanup(revision)
            return {"status": "published", "revision": revision,
                    "emoji_count": len(entries)}

    def _publish(self, revision, manifest, assets):
        generations = self.output / "generations"
        generations.mkdir(parents=True, exist_ok=True, mode=0o755)
        temporary = Path(tempfile.mkdtemp(prefix=".generation-", dir=self.output))
        try:
            os.chmod(temporary, 0o700)
            asset_dir = temporary / "assets"
            asset_dir.mkdir(mode=0o700)
            for digest, raw in sorted(assets.items()):
                _write(asset_dir / f"{digest}.png", raw)
            _fsync_directory(asset_dir)
            _write(temporary / "manifest.json", _canonical(manifest))
            _fsync_directory(temporary)
            for path in asset_dir.iterdir():
                path.chmod(0o644)
            asset_dir.chmod(0o755)
            (temporary / "manifest.json").chmod(0o644)
            temporary.chmod(0o755)
            target = generations / revision
            if target.exists():
                shutil.rmtree(temporary)
            else:
                os.replace(temporary, target)
                _fsync_directory(generations)
            staged_link = self.output / f".current-{os.getpid()}"
            staged_link.unlink(missing_ok=True)
            os.symlink(f"generations/{revision}", staged_link)
            os.replace(staged_link, self.output / "current")
            _fsync_directory(self.output)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def _cleanup(self, current):
        generations = self.output / "generations"
        values = sorted((path for path in generations.iterdir()
                         if path.is_dir() and not path.is_symlink() and DIGEST.fullmatch(path.name)),
                        key=lambda path: path.stat().st_mtime_ns, reverse=True)
        keep = {current}
        for path in values:
            if len(keep) < MAX_GENERATIONS:
                keep.add(path.name)
        for path in values:
            if path.name not in keep:
                shutil.rmtree(path)
        _fsync_directory(generations)


def _header(headers, name):
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None


def _media_type(value):
    return (value or "").split(";", 1)[0].strip().lower()


def _read_credential(path):
    path = Path(path)
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 4096:
        raise ValueError("invalid credential file")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError("empty credential")
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guild-id", default=os.environ.get("NOCTURNE_DISCORD_GUILD_ID"))
    parser.add_argument("--output", default=os.environ.get("NOCTURNE_EMOJI_PUBLIC_ROOT"))
    parser.add_argument("--token-file", default=None)
    parser.add_argument("--deny", action="append", default=[])
    args = parser.parse_args(argv)
    if not args.guild_id or not args.output or not args.token_file:
        parser.error("guild ID, output, and token credential file are required")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        result = EmojiSynchronizer(args.output, DiscordTransport(), denylist=args.deny).synchronize(
            args.guild_id, _read_credential(args.token_file))
        log.info("emoji synchronization %s: count=%d revision=%s",
                 result["status"], result["emoji_count"], result["revision"][:12])
    except SyncFailure as error:
        log.error("emoji synchronization failed: category=%s retry_after=%s",
                  error.category, error.retry_after if error.retry_after is not None else "none")
        return 75
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
