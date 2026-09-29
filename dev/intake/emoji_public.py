"""Read-only WSGI publication of the sanitized Discord emoji mirror."""
from __future__ import annotations

import hashlib
import json
import os
import errno
from datetime import datetime
from pathlib import Path
import re
import stat


SCHEMA_VERSION = 1
MANIFEST_PATH = "/api/plugin/v1/emojis"
ASSET_PATH = re.compile(r"/api/plugin/v1/emojis/assets/([0-9a-f]{64})\.png\Z")
DIGEST = re.compile(r"[0-9a-f]{64}\Z")
NAME = re.compile(r"[a-z0-9_]{1,32}\Z")
MAX_EMOJIS = 256
MAX_MANIFEST_BYTES = 256 * 1024
MAX_ASSET_BYTES = 8 * 1024
MAX_TOTAL_BYTES = MAX_EMOJIS * MAX_ASSET_BYTES


def _acl_free(path):
    try:
        names = os.listxattr(path, follow_symlinks=False)
    except OSError as error:
        return error.errno in {errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}
    return not {"system.posix_acl_access", "system.posix_acl_default"}.intersection(names)


def _public_node(path, root_stat, *, directory):
    try:
        value = Path(path).lstat()
    except OSError:
        return False
    expected = stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode)
    mode = 0o755 if directory else 0o644
    return (expected and value.st_dev == root_stat.st_dev
            and value.st_uid == root_stat.st_uid and value.st_gid == root_stat.st_gid
            and stat.S_IMODE(value.st_mode) == mode
            and (directory or value.st_nlink == 1)
            and _acl_free(path)
            and (not directory or not os.path.ismount(path)))


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _generation(root):
    root = Path(root)
    current = root / "current"
    if not current.is_symlink():
        raise ValueError("emoji generation unavailable")
    target = os.readlink(current)
    match = re.fullmatch(r"generations/([0-9a-f]{64})", target)
    if not match:
        raise ValueError("unsafe current generation")
    generation = root / "generations" / match.group(1)
    resolved_root = root.resolve(strict=True)
    resolved = generation.resolve(strict=True)
    if resolved.parent != (resolved_root / "generations").resolve(strict=True):
        raise ValueError("emoji generation escaped mirror")
    root_stat = root.lstat()
    if not _public_node(generation, root_stat, directory=True):
        raise ValueError("unsafe emoji generation")
    return generation, match.group(1), root_stat


def load_manifest(root):
    generation, revision, root_stat = _generation(root)
    path = generation / "manifest.json"
    if not _public_node(path, root_stat, directory=False) \
            or not 1 <= path.stat().st_size <= MAX_MANIFEST_BYTES:
        raise ValueError("invalid emoji manifest file")
    raw = path.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8", errors="strict"), object_pairs_hook=_strict_object,
                           parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
    except (UnicodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("invalid emoji manifest") from error
    if not isinstance(value, dict) or set(value) != {
            "schema_version", "revision", "generated_at", "source_status", "emojis"}:
        raise ValueError("invalid emoji manifest fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise ValueError("unsupported emoji schema")
    if value["revision"] != revision or value["source_status"] not in {"ok", "ok_empty"}:
        raise ValueError("invalid emoji generation identity")
    if not isinstance(value["generated_at"], str) or len(value["generated_at"]) > 40:
        raise ValueError("invalid generated timestamp")
    generated = datetime.fromisoformat(value["generated_at"].replace("Z", "+00:00"))
    if generated.tzinfo is None or generated.utcoffset() is None:
        raise ValueError("generated timestamp lacks timezone")
    entries = value["emojis"]
    if not isinstance(entries, list) or len(entries) > MAX_EMOJIS:
        raise ValueError("invalid emoji count")
    names = set()
    digests = set()
    total = 0
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
                "name", "sha256", "width", "height", "byte_length",
                "animated_source", "asset_path"}:
            raise ValueError("invalid emoji entry fields")
        name, digest = entry["name"], entry["sha256"]
        if (not isinstance(name, str) or not NAME.fullmatch(name) or name in names
                or not isinstance(digest, str) or not DIGEST.fullmatch(digest)
                or type(entry["width"]) is not int or entry["width"] != 20
                or type(entry["height"]) is not int or entry["height"] != 20
                or type(entry["byte_length"]) is not int
                or not 1 <= entry["byte_length"] <= MAX_ASSET_BYTES
                or type(entry["animated_source"]) is not bool
                or entry["asset_path"] != f"{MANIFEST_PATH}/assets/{digest}.png"):
            raise ValueError("invalid emoji entry")
        names.add(name)
        digests.add(digest)
        total += entry["byte_length"]
    if entries != sorted(entries, key=lambda entry: entry["name"]) or total > MAX_TOTAL_BYTES:
        raise ValueError("emoji manifest order or size invalid")
    if revision != hashlib.sha256(_canonical(entries)).hexdigest():
        raise ValueError("emoji revision mismatch")
    canonical = _canonical(value)
    if len(canonical) > MAX_MANIFEST_BYTES:
        raise ValueError("emoji response too large")
    return generation, value, canonical, frozenset(digests), root_stat


def _asset(generation, digest, allowed, root_stat):
    if digest not in allowed:
        raise FileNotFoundError("unknown emoji digest")
    asset_directory = generation / "assets"
    if not _public_node(asset_directory, root_stat, directory=True):
        raise ValueError("unsafe emoji asset directory")
    path = asset_directory / f"{digest}.png"
    if not _public_node(path, root_stat, directory=False) \
            or not 1 <= path.stat().st_size <= MAX_ASSET_BYTES:
        raise FileNotFoundError("emoji asset unavailable")
    raw = path.read_bytes()
    if (hashlib.sha256(raw).hexdigest() != digest
            or not raw.startswith(b"\x89PNG\r\n\x1a\n") or not raw.endswith(b"IEND\xaeB`\x82")):
        raise ValueError("invalid emoji asset")
    return raw


def _empty(start_response, status, headers=()):
    values = list(headers) + [("Content-Length", "0")]
    start_response(status, values)
    return [b""]


def public_wsgi(root, environ, start_response):
    method = environ.get("REQUEST_METHOD", "")
    if method not in {"GET", "HEAD"}:
        return _empty(start_response, "405 Method Not Allowed", (("Allow", "GET, HEAD"),))
    if environ.get("CONTENT_LENGTH") not in (None, "", "0") or environ.get("HTTP_TRANSFER_ENCODING"):
        return _empty(start_response, "400 Bad Request")
    path = environ.get("PATH_INFO", "")
    asset_match = ASSET_PATH.fullmatch(path)
    if path != MANIFEST_PATH and asset_match is None:
        return _empty(start_response, "404 Not Found")
    try:
        generation, _manifest, raw, digests, root_stat = load_manifest(root)
        if asset_match is None:
            etag = '"' + hashlib.sha256(raw).hexdigest() + '"'
            common = (("ETag", etag),
                      ("Cache-Control", "public, max-age=300, must-revalidate"))
            if environ.get("HTTP_IF_NONE_MATCH") == etag:
                return _empty(start_response, "304 Not Modified", common)
            headers = common + (("Content-Type", "application/json; charset=utf-8"),
                                ("Content-Length", str(len(raw))))
        else:
            digest = asset_match.group(1)
            raw = _asset(generation, digest, digests, root_stat)
            etag = '"' + digest + '"'
            common = (("ETag", etag),
                      ("Cache-Control", "public, max-age=31536000, immutable"))
            if environ.get("HTTP_IF_NONE_MATCH") == etag:
                return _empty(start_response, "304 Not Modified", common)
            headers = common + (("Content-Type", "image/png"),
                                ("Content-Length", str(len(raw))))
    except FileNotFoundError:
        return _empty(start_response, "404 Not Found")
    except (OSError, ValueError, TypeError):
        return _empty(start_response, "503 Service Unavailable",
                      (("Cache-Control", "no-store"),))
    start_response("200 OK", list(headers))
    return [b"" if method == "HEAD" else raw]
