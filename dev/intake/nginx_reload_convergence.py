#!/usr/bin/python3
"""Bounded, read-only verification that an Nginx reload reached new workers."""
import argparse
import json
import math
import os
from pathlib import Path
import re
import stat
import subprocess
import time


PURPOSE = "nocturne-nginx-worker-generation-v1"
SERVICE = "nginx.service"
_PID = re.compile(r"[1-9][0-9]*\Z")
_WORKER = b"nginx: worker process"
_KNOWN_NON_WORKERS = (b"nginx: cache manager process", b"nginx: cache loader process")


def _command(args, **kwargs):
    return subprocess.run(args, check=True, timeout=5, **kwargs)


def _service_state(run=_command):
    result = run(["/usr/bin/systemctl", "show", SERVICE, "--no-pager",
                  "--property=Id", "--property=LoadState",
                  "--property=ActiveState", "--property=SubState",
                  "--property=MainPID"], stdout=subprocess.PIPE, text=True)
    fields = {}
    for line in result.stdout.splitlines():
        if "=" not in line:
            raise ValueError("malformed Nginx service evidence")
        key, value = line.split("=", 1)
        if key in fields:
            raise ValueError("duplicate Nginx service evidence")
        fields[key] = value
    required = {"Id", "LoadState", "ActiveState", "SubState", "MainPID"}
    if set(fields) != required:
        raise ValueError("incomplete Nginx service evidence")
    if (fields["Id"] != SERVICE or fields["LoadState"] != "loaded"
            or fields["ActiveState"] != "active" or fields["SubState"] != "running"
            or not _PID.fullmatch(fields["MainPID"])):
        raise RuntimeError("Nginx is not safely active")
    return int(fields["MainPID"])


def _read_bounded(path, limit=4096):
    with Path(path).open("rb") as source:
        value = source.read(limit + 1)
    if len(value) > limit:
        raise ValueError("oversized Nginx process evidence")
    return value


def _parent_pid(path, expected_pid):
    raw = _read_bounded(path).decode("ascii", errors="strict").strip()
    match = re.fullmatch(r"([1-9][0-9]*) \(.+\) [A-Za-z] ([1-9][0-9]*) .+", raw)
    if not match or int(match.group(1)) != expected_pid:
        raise ValueError("malformed Nginx process identity")
    return int(match.group(2))


def _worker_generation(master_pid, proc_root=Path("/proc")):
    if type(master_pid) is not int or master_pid < 1:
        raise ValueError("invalid Nginx master PID")
    root = Path(proc_root)
    children_raw = _read_bounded(root / str(master_pid) / "task" /
                                 str(master_pid) / "children").decode("ascii", errors="strict")
    values = children_raw.split()
    if not values or any(not _PID.fullmatch(value) for value in values):
        raise ValueError("malformed or empty Nginx child evidence")
    children = [int(value) for value in values]
    if len(children) != len(set(children)):
        raise ValueError("duplicate Nginx child evidence")
    workers = []
    for pid in children:
        process = root / str(pid)
        if _parent_pid(process / "stat", pid) != master_pid:
            raise ValueError("Nginx child parent changed")
        command = _read_bounded(process / "cmdline").split(b"\0", 1)[0]
        if command.startswith(_WORKER):
            workers.append(pid)
        elif not any(command.startswith(value) for value in _KNOWN_NON_WORKERS):
            raise ValueError("ambiguous Nginx child process")
    if not workers:
        raise ValueError("Nginx worker generation is empty")
    return tuple(sorted(workers))


def capture_generation(*, run=_command, proc_root=Path("/proc")):
    master = _service_state(run)
    workers = _worker_generation(master, proc_root)
    if _service_state(run) != master:
        raise RuntimeError("Nginx master changed while capturing workers")
    return {"schema_version": 1, "purpose": PURPOSE,
            "master_pid": master, "worker_pids": list(workers)}


def wait_for_reload(before, *, attempts=40, delay=0.25, run=_command,
                    proc_root=Path("/proc"), sleep=time.sleep):
    if (not isinstance(before, dict) or set(before) != {
            "schema_version", "purpose", "master_pid", "worker_pids"}
            or before.get("schema_version") != 1 or before.get("purpose") != PURPOSE
            or type(before.get("master_pid")) is not int or before["master_pid"] < 1
            or not isinstance(before.get("worker_pids"), list)
            or not before["worker_pids"]
            or any(type(pid) is not int or pid < 1 for pid in before["worker_pids"])
            or len(before["worker_pids"]) != len(set(before["worker_pids"]))):
        raise ValueError("invalid pre-reload Nginx generation")
    if type(attempts) is not int or not 1 <= attempts <= 600 \
            or not isinstance(delay, (int, float)) or not 0 <= delay <= 5:
        raise ValueError("invalid Nginx convergence bound")
    previous = set(before["worker_pids"])
    master = before["master_pid"]
    for attempt in range(attempts):
        if _service_state(run) != master:
            raise RuntimeError("Nginx master changed during reload")
        workers = _worker_generation(master, proc_root)
        if previous.isdisjoint(workers):
            if _service_state(run) != master:
                raise RuntimeError("Nginx master changed after worker turnover")
            return {"schema_version": 1, "purpose": PURPOSE,
                    "master_pid": master, "worker_pids": list(workers),
                    "polls": attempt + 1}
        if attempt + 1 < attempts:
            sleep(delay)
    raise TimeoutError("Nginx workers did not converge after reload")


def _load_generation(path):
    path = Path(path)
    metadata = path.lstat()
    if (path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid() or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > 4096):
        raise ValueError("unsafe pre-reload generation file")
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate pre-reload generation field")
            result[key] = value
        return result
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--capture", action="store_true")
    modes.add_argument("--wait", action="store_true")
    parser.add_argument("--before")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--interval", type=float, default=0.25)
    args = parser.parse_args()
    if args.capture:
        if args.before is not None:
            raise SystemExit("--capture does not accept --before")
        result = capture_generation()
    else:
        if args.before is None or not 0 < args.timeout <= 30 \
                or not 0.05 <= args.interval <= 1:
            raise SystemExit("--wait requires safe bounded arguments")
        attempts = max(1, math.ceil(args.timeout / args.interval))
        result = wait_for_reload(_load_generation(args.before), attempts=attempts,
                                 delay=args.interval)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
