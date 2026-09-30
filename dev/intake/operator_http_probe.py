#!/usr/bin/env python3
"""Run one bounded, paced operator HTTP probe without exposing response data.

The state file is shared by sequential invocations from one activation wrapper,
so separate GET/HEAD/conditional requests remain below the strictest committed
Nocturne route limit. Only HTTP 429 is retried; all other unexpected statuses
fail immediately.
"""

from __future__ import annotations

import argparse
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time


MIN_INTERVAL_SECONDS = 0.65  # Strictest current route: 2 requests/second.
MAX_INTERVAL_SECONDS = 10.0
MAX_TIMEOUT_SECONDS = 30.0
MAX_429_RETRIES = 5
STATE_FILENAME = "http-probe-state.json"
MAX_STATE_BYTES = 256


class ProbeFailure(RuntimeError):
    """A sanitized operator-probe failure."""


def _duplicate_rejecting_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate probe-state key")
        result[key] = value
    return result


def _validate_parent(parent: Path, owner: int) -> int:
    try:
        named = parent.lstat()
        if (not stat.S_ISDIR(named.st_mode) or stat.S_ISLNK(named.st_mode)
                or named.st_uid != owner or stat.S_IMODE(named.st_mode) != 0o700
                or parent.resolve(strict=True) != parent):
            raise ProbeFailure("probe state parent is unsafe")
        fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        opened = os.fstat(fd)
        current = parent.lstat()
        if ((opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
                or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)):
            os.close(fd)
            raise ProbeFailure("probe state parent changed")
        return fd
    except ProbeFailure:
        raise
    except (OSError, RuntimeError, ValueError) as error:
        raise ProbeFailure("probe state parent is unavailable") from error


def _open_state(state_path: Path, owner: int) -> tuple[int, int]:
    if state_path.name != STATE_FILENAME or not state_path.is_absolute():
        raise ProbeFailure("probe state path is not the approved file")
    parent_fd = _validate_parent(state_path.parent, owner)
    try:
        try:
            fd = os.open(state_path.name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
                         | os.O_CLOEXEC, 0o600, dir_fd=parent_fd)
        except OSError as error:
            raise ProbeFailure("probe state file is unsafe or unavailable") from error
        opened = os.fstat(fd)
        try:
            named = os.stat(state_path.name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            os.close(fd)
            raise ProbeFailure("probe state file changed") from error
        if (not stat.S_ISREG(opened.st_mode) or opened.st_uid != owner
                or stat.S_IMODE(opened.st_mode) != 0o600 or opened.st_nlink != 1
                or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)):
            os.close(fd)
            raise ProbeFailure("probe state file metadata is unsafe")
        return parent_fd, fd
    except BaseException:
        os.close(parent_fd)
        raise


def _reserve_request_slot(state_path: Path, interval: float, deadline: float,
                          *, clock=time.monotonic, sleep=time.sleep,
                          owner=None) -> None:
    owner = os.geteuid() if owner is None else owner
    parent_fd, fd = _open_state(state_path, owner)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN):
                    raise ProbeFailure("probe pacing lock failed") from error
                remaining = deadline - clock()
                if remaining <= 0:
                    raise ProbeFailure("probe pacing deadline exceeded")
                sleep(min(0.05, remaining))
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, MAX_STATE_BYTES + 1)
        if len(raw) > MAX_STATE_BYTES:
            raise ProbeFailure("probe state exceeds its size bound")
        last_ns = None
        if raw:
            try:
                value = json.loads(raw.decode("ascii"), object_pairs_hook=_duplicate_rejecting_object)
            except (UnicodeError, ValueError, json.JSONDecodeError) as error:
                raise ProbeFailure("probe state is malformed") from error
            if (type(value) is not dict or set(value) != {"last_request_monotonic_ns"}
                    or type(value["last_request_monotonic_ns"]) is not int
                    or value["last_request_monotonic_ns"] < 0):
                raise ProbeFailure("probe state schema is invalid")
            last_ns = value["last_request_monotonic_ns"]

        now = clock()
        delay = 0.0 if last_ns is None else max(0.0, last_ns / 1_000_000_000 + interval - now)
        if now + delay >= deadline:
            raise ProbeFailure("probe pacing deadline exceeded")
        if delay:
            sleep(delay)
        stamp = clock()
        if stamp >= deadline:
            raise ProbeFailure("probe pacing deadline exceeded")
        encoded = json.dumps({"last_request_monotonic_ns": int(stamp * 1_000_000_000)},
                             sort_keys=True, separators=(",", ":")).encode("ascii")
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        written = 0
        while written < len(encoded):
            written += os.write(fd, encoded[written:])
        os.fsync(fd)
    except ProbeFailure:
        raise
    except (OSError, OverflowError, ValueError) as error:
        raise ProbeFailure("probe pacing state could not be updated") from error
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
            os.close(parent_fd)


def request(expected_status: int, command: list[str], state_path: Path, *,
            interval: float = MIN_INTERVAL_SECONDS, timeout: float = 12.0,
            max_429_retries: int = 3, runner=subprocess.run,
            clock=time.monotonic, sleep=time.sleep, owner=None) -> int:
    """Run a curl-style command whose sole stdout value is ``%{http_code}``."""
    if (type(expected_status) is not int or not 100 <= expected_status <= 599
            or not MIN_INTERVAL_SECONDS <= interval <= MAX_INTERVAL_SECONDS
            or not 1.0 <= timeout <= MAX_TIMEOUT_SECONDS
            or type(max_429_retries) is not int or not 0 <= max_429_retries <= MAX_429_RETRIES
            or not command or not Path(command[0]).is_absolute()
            or Path(command[0]).name != "curl"):
        raise ProbeFailure("probe arguments are outside the supported bounds")

    deadline = clock() + timeout
    retries = 0
    while True:
        _reserve_request_slot(state_path, interval, deadline, clock=clock, sleep=sleep, owner=owner)
        remaining = deadline - clock()
        if remaining <= 0:
            raise ProbeFailure("probe deadline exceeded")
        try:
            result = runner(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=remaining, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ProbeFailure("HTTP probe transport failed") from error
        if result.returncode != 0:
            raise ProbeFailure("HTTP probe transport failed")
        output = result.stdout
        if isinstance(output, str):
            try:
                output = output.encode("ascii", "strict")
            except UnicodeError as error:
                raise ProbeFailure("HTTP probe status output is malformed") from error
        if not isinstance(output, bytes) or re.fullmatch(rb"[1-5][0-9]{2}", output) is None:
            raise ProbeFailure("HTTP probe status output is malformed")
        status = int(output)
        if status == expected_status:
            return status
        if status != 429:
            raise ProbeFailure(f"unexpected HTTP status {status}; expected {expected_status}")
        if retries >= max_429_retries:
            raise ProbeFailure("HTTP 429 persisted after bounded retries")
        retries += 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--expect", required=True, type=int)
    parser.add_argument("--interval", type=float, default=MIN_INTERVAL_SECONDS)
    parser.add_argument("--timeout", type=float, default=12.0)
    parser.add_argument("--max-429-retries", type=int, default=3)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    try:
        status = request(args.expect, command, args.state, interval=args.interval,
                         timeout=args.timeout, max_429_retries=args.max_429_retries)
    except ProbeFailure as error:
        print(f"operator_http_probe: {error}", file=sys.stderr)
        return 1
    print(status)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
