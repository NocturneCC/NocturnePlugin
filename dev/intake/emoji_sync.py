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
import errno
from io import BytesIO
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import unicodedata
from uuid import uuid4
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import warnings
import zlib

from PIL import Image, __version__ as PILLOW_VERSION


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
REQUIRED_PILLOW_VERSION = "12.3.0"
NAME = re.compile(r"[a-z0-9_]{1,32}\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
DISCORD_ID = re.compile(r"[0-9]{1,24}\Z")
API_BASE = "https://discord.com/api/v10"
CDN_BASE = "https://cdn.discordapp.com/emojis"
DISCORD_USER_AGENT = "DiscordBot (https://github.com/NocturneCC/NocturnePlugin, 0.3.2)"

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

    def __init__(self, timeout=10, opener=None):
        if not 1 <= timeout <= 30:
            raise ValueError("invalid Discord timeout")
        self.timeout = timeout
        self.opener = opener or build_opener(ProxyHandler({}), NoRedirect())

    def list_emojis(self, guild_id, token):
        if not isinstance(guild_id, str) or not DISCORD_ID.fullmatch(guild_id):
            raise SyncFailure("invalid_guild_id")
        url = f"{API_BASE}/guilds/{guild_id}/emojis"
        request = Request(url, method="GET", headers={
            "Authorization": f"Bot {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": DISCORD_USER_AGENT,
        })
        return self._request(request, MAX_INPUT_BYTES)

    def fetch_asset(self, emoji_id, animated):
        if (not isinstance(emoji_id, str) or not DISCORD_ID.fullmatch(emoji_id)
                or type(animated) is not bool):
            raise SyncFailure("invalid_emoji_id")
        extension = "gif" if animated else "png"
        request = Request(f"{CDN_BASE}/{emoji_id}.{extension}", method="GET", headers={
            "Accept": "image/gif,image/png",
            "User-Agent": DISCORD_USER_AGENT,
        })
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
            try:
                retry = (_retry_after(error.headers.get("Retry-After"))
                         if error.code == 429 else None)
            finally:
                error.close()
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


def _gif_container(raw):
    if len(raw) < 14 or not raw.startswith((b"GIF87a", b"GIF89a")):
        return False
    offset = 13
    packed = raw[10]
    if packed & 0x80:
        offset += 3 * (1 << ((packed & 0x07) + 1))
    seen_image = False
    while offset < len(raw):
        marker = raw[offset]
        offset += 1
        if marker == 0x3b:
            return seen_image and offset == len(raw)
        if marker == 0x2c:
            if offset + 9 > len(raw):
                return False
            local_packed = raw[offset + 8]
            offset += 9
            if local_packed & 0x80:
                offset += 3 * (1 << ((local_packed & 0x07) + 1))
            if offset >= len(raw):
                return False
            offset += 1  # LZW minimum code size.
            seen_image = True
        elif marker == 0x21:
            if offset >= len(raw):
                return False
            offset += 1  # Extension label.
        else:
            return False
        while offset < len(raw):
            block_size = raw[offset]
            offset += 1
            if block_size == 0:
                break
            if offset + block_size > len(raw):
                return False
            offset += block_size
        else:
            return False
    return False


def normalize_image(raw, content_type, animated_source):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_INPUT_BYTES:
        raise SyncFailure("invalid_image")
    expected = "image/gif" if animated_source else "image/png"
    media_type = (content_type or "").split(";", 1)[0].strip().lower()
    if media_type != expected:
        raise SyncFailure("invalid_image_type")
    if animated_source:
        if not _gif_container(raw):
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


def _safe_private_file(path, maximum):
    path = Path(path)
    try:
        value = path.lstat()
    except OSError as error:
        raise ValueError("invalid private file") from error
    if (not stat.S_ISREG(value.st_mode) or value.st_nlink != 1
            or value.st_uid not in {0, os.geteuid()}
            or stat.S_IMODE(value.st_mode) & 0o077 or not 1 <= value.st_size <= maximum):
        raise ValueError("unsafe private file")
    return path.read_bytes()


def _public_node(path, root_stat, *, directory, mode):
    try:
        value = Path(path).lstat()
    except OSError:
        return False
    expected = stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode)
    return (expected and value.st_dev == root_stat.st_dev
            and value.st_uid == root_stat.st_uid and value.st_gid == root_stat.st_gid
            and stat.S_IMODE(value.st_mode) == mode
            and (directory or value.st_nlink == 1)
            and _acl_free(path)
            and (not directory or not os.path.ismount(path)))


def _acl_free(path):
    try:
        names = os.listxattr(path, follow_symlinks=False)
    except OSError as error:
        return error.errno in {errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}
    return not {"system.posix_acl_access", "system.posix_acl_default"}.intersection(names)


def _verified_generation(path, manifest, assets, root_stat):
    path = Path(path)
    if not _public_node(path, root_stat, directory=True, mode=0o755):
        return False
    asset_dir = path / "assets"
    if not _public_node(asset_dir, root_stat, directory=True, mode=0o755):
        return False
    expected_root = {"assets", "manifest.json"}
    expected_assets = {f"{digest}.png" for digest in assets}
    try:
        if {value.name for value in path.iterdir()} != expected_root:
            return False
        if {value.name for value in asset_dir.iterdir()} != expected_assets:
            return False
        manifest_path = path / "manifest.json"
        if not _public_node(manifest_path, root_stat, directory=False, mode=0o644):
            return False
        if manifest_path.read_bytes() != _canonical(manifest):
            return False
        for digest, raw in assets.items():
            asset = asset_dir / f"{digest}.png"
            if (not _public_node(asset, root_stat, directory=False, mode=0o644)
                    or asset.read_bytes() != raw or hashlib.sha256(raw).hexdigest() != digest):
                return False
    except OSError:
        return False
    return True


def _safe_removable_tree(path, root_stat):
    path = Path(path)
    if not _public_node(path, root_stat, directory=True, mode=0o755):
        return False
    try:
        for value in path.rglob("*"):
            metadata = value.lstat()
            if (metadata.st_dev != root_stat.st_dev or metadata.st_uid != root_stat.st_uid
                    or metadata.st_gid != root_stat.st_gid or stat.S_ISLNK(metadata.st_mode)
                    or (stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1)
                    or (stat.S_ISDIR(metadata.st_mode) and os.path.ismount(value))
                    or not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode))):
                return False
    except OSError:
        return False
    return True


def read_current(root):
    root = Path(root)
    current = root / "current"
    if not current.is_symlink():
        return None
    target = os.readlink(current)
    match = re.fullmatch(r"generations/([0-9a-f]{64})", target)
    if not match:
        return None
    generation = root / target
    manifest = generation / "manifest.json"
    try:
        root_stat = root.lstat()
    except OSError:
        return None
    if (not _public_node(generation, root_stat, directory=True, mode=0o755)
            or not _public_node(manifest, root_stat, directory=False, mode=0o644)
            or manifest.stat().st_size > 256 * 1024):
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
    root_stat = root.lstat()
    if (not stat.S_ISDIR(root_stat.st_mode) or root_stat.st_uid != os.geteuid()
            or not _acl_free(root)
            or root_stat.st_mode & 0o022):
        raise SyncFailure("unsafe_output_directory")
    os.chmod(root, 0o755)
    lock_path = root / ".sync.lock"
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as error:
        raise SyncFailure("unsafe_sync_lock") from error
    with os.fdopen(descriptor, "a+b") as lock:
        value = os.fstat(lock.fileno())
        if (not stat.S_ISREG(value.st_mode) or value.st_nlink != 1
                or value.st_uid != os.geteuid() or stat.S_IMODE(value.st_mode) != 0o600):
            raise SyncFailure("unsafe_sync_lock")
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
        root_stat = self.output.lstat()
        try:
            generations.mkdir(mode=0o700)
        except FileExistsError:
            value = generations.lstat()
            if (not stat.S_ISDIR(value.st_mode) or value.st_dev != root_stat.st_dev
                    or value.st_uid != root_stat.st_uid or value.st_gid != root_stat.st_gid
                    or stat.S_IMODE(value.st_mode) not in {0o700, 0o755}
                    or not _acl_free(generations) or os.path.ismount(generations)):
                raise SyncFailure("unsafe_generation_directory") from None
        os.chmod(generations, 0o755)
        if not _public_node(generations, root_stat, directory=True, mode=0o755):
            raise SyncFailure("unsafe_generation_directory")
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
            if not _verified_generation(temporary, manifest, assets, root_stat):
                raise SyncFailure("generation_verification_failed")
            target = generations / revision
            if target.exists() and (not target.is_dir() or target.is_symlink()):
                raise SyncFailure("unsafe_generation_target")
            if target.exists():
                if not _verified_generation(target, manifest, assets, root_stat):
                    raise SyncFailure("unsafe_generation_target")
                shutil.rmtree(temporary)
            else:
                os.replace(temporary, target)
                _fsync_directory(generations)
            staged_link = self.output / f".current-{os.getpid()}-{uuid4().hex}"
            try:
                os.symlink(f"generations/{revision}", staged_link)
                os.replace(staged_link, self.output / "current")
                _fsync_directory(self.output)
            finally:
                staged_link.unlink(missing_ok=True)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)

    def _cleanup(self, current):
        generations = self.output / "generations"
        root_stat = self.output.lstat()
        values = sorted((path for path in generations.iterdir()
                         if path.is_dir() and not path.is_symlink() and DIGEST.fullmatch(path.name)),
                        key=lambda path: path.stat().st_mtime_ns, reverse=True)
        keep = {current}
        selected = self.output / "current"
        if selected.is_symlink():
            match = re.fullmatch(r"generations/([0-9a-f]{64})", os.readlink(selected))
            if match:
                keep.add(match.group(1))
        for path in values:
            if len(keep) < MAX_GENERATIONS:
                keep.add(path.name)
        for path in values:
            if path.name not in keep and _safe_removable_tree(path, root_stat):
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
    try:
        value = _safe_private_file(path, 4096).decode("ascii", errors="strict").rstrip("\r\n")
    except (OSError, UnicodeError) as error:
        raise ValueError("invalid credential file") from error
    if not re.fullmatch(r"[\x21-\x7e]{1,4096}", value):
        raise ValueError("invalid credential")
    return value


def _read_config(path):
    try:
        value = _strict_json(_safe_private_file(path, 8192))
    except SyncFailure as error:
        raise ValueError("invalid synchronization config") from error
    if (not isinstance(value, dict) or set(value) - {"guild_id", "denylist"}
            or "guild_id" not in value or not isinstance(value["guild_id"], str)
            or not DISCORD_ID.fullmatch(value["guild_id"])):
        raise ValueError("invalid synchronization config")
    denylist = value.get("denylist", [])
    if (not isinstance(denylist, list) or len(denylist) > MAX_EMOJIS
            or any(not isinstance(item, str) for item in denylist)):
        raise ValueError("invalid synchronization config")
    return value["guild_id"], parse_denylist(denylist)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=os.environ.get("NOCTURNE_EMOJI_PUBLIC_ROOT"))
    parser.add_argument("--config-file", default=None)
    parser.add_argument("--token-file", default=None)
    args = parser.parse_args(argv)
    if not args.config_file or not args.output or not args.token_file:
        parser.error("configuration, output, and token credential files are required")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if PILLOW_VERSION != REQUIRED_PILLOW_VERSION or sys.version_info[:2] != (3, 14):
        log.error("emoji synchronization failed: category=unsupported_runtime")
        return 78
    try:
        guild_id, denylist = _read_config(args.config_file)
        result = EmojiSynchronizer(args.output, DiscordTransport(), denylist=denylist).synchronize(
            guild_id, _read_credential(args.token_file))
        log.info("emoji synchronization %s: count=%d revision=%s",
                 result["status"], result["emoji_count"], result["revision"][:12])
    except SyncFailure as error:
        log.error("emoji synchronization failed: category=%s retry_after=%s",
                  error.category, error.retry_after if error.retry_after is not None else "none")
        return 75
    except (OSError, ValueError):
        log.error("emoji synchronization failed: category=invalid_configuration")
        return 78
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
