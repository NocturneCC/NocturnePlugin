"""Narrow Unix-socket publisher for validated public announcement snapshots."""
import argparse
import json
import os
from pathlib import Path
import pwd
import socket
import stat
import struct
import tempfile

from announcements import (DEFAULT_PUBLIC_SNAPSHOT, MAX_SNAPSHOT_BYTES,
                           validate_snapshot_bytes)


SOCKET_PATH = "/run/nocturne-announcement-snapshot-writer/publish.sock"
MAX_PEER_REQUEST = MAX_SNAPSHOT_BYTES
MAX_REPLY = 512


def _read_exact(connection, length):
    result = bytearray()
    while len(result) < length:
        chunk = connection.recv(length - len(result))
        if not chunk:
            raise ValueError("truncated request")
        result.extend(chunk)
    return bytes(result)


def _safe_directory(path, expected_uid, expected_gid, *, fixture=False):
    target = Path(path)
    if not fixture and str(target) != DEFAULT_PUBLIC_SNAPSHOT:
        raise ValueError("unexpected public snapshot destination")
    parts = ((target.parent,) if fixture else
             (Path("/srv"), Path("/srv/projects"), target.parent))
    for component in parts:
        info = component.lstat()
        if not component.is_dir() or component.is_symlink():
            raise ValueError("unsafe public snapshot directory")
        if component == target.parent:
            if (info.st_uid, info.st_gid, info.st_mode & 0o7777) != (
                    expected_uid, expected_gid, 0o755) or os.path.ismount(component):
                raise ValueError("unsafe public snapshot directory metadata")
        elif not fixture and component == Path("/srv/projects"):
            # Midgard's shared project parent is intentionally setgid and
            # group-writable; the service remains confined to the exact child
            # by its systemd read/write path namespace.
            if (info.st_uid, info.st_gid, info.st_mode & 0o7777) != (
                    expected_uid, expected_gid, 0o2775):
                raise ValueError("unsafe shared project parent metadata")
        elif info.st_mode & 0o022:
            raise ValueError("unsafe public snapshot parent ownership")
    return target


def _existing_target(target, expected_uid, expected_gid, *, dir_fd=None, parent_device=None):
    try:
        info = (target.lstat() if dir_fd is None else
                os.stat(target.name, dir_fd=dir_fd, follow_symlinks=False))
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
            or (info.st_uid, info.st_gid, info.st_mode & 0o7777)
            != (expected_uid, expected_gid, 0o644)
            or (parent_device is not None and info.st_dev != parent_device)):
        raise ValueError("unsafe existing public snapshot")
    return info


def _read_existing(target, expected_uid, expected_gid, *, dir_fd, expected):
    file_fd = os.open(target.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
    try:
        opened = os.fstat(file_fd)
        if (opened.st_dev, opened.st_ino, opened.st_uid, opened.st_gid,
                opened.st_nlink, opened.st_mode & 0o7777) != (
                expected.st_dev, expected.st_ino, expected_uid, expected_gid, 1, 0o644):
            raise ValueError("public snapshot changed while opening")
        content = bytearray()
        while len(content) <= MAX_SNAPSHOT_BYTES:
            block = os.read(file_fd, min(8192, MAX_SNAPSHOT_BYTES + 1 - len(content)))
            if not block:
                break
            content.extend(block)
        if len(content) > MAX_SNAPSHOT_BYTES:
            raise ValueError("existing public snapshot exceeds limit")
        named = os.stat(target.name, dir_fd=dir_fd, follow_symlinks=False)
        if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
            raise ValueError("public snapshot name changed while reading")
        return bytes(content)
    finally:
        os.close(file_fd)


def _publish(raw, *, destination, expected_uid, expected_gid, fixture=False, replace=os.replace):
    document = validate_snapshot_bytes(raw)
    target = _safe_directory(destination, expected_uid, expected_gid, fixture=fixture)
    parent = target.parent
    parent_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_NOFOLLOW", 0))
    temp_name = None
    try:
        opened_parent = os.fstat(parent_fd)
        named_parent = parent.lstat()
        if (opened_parent.st_dev, opened_parent.st_ino) != (named_parent.st_dev, named_parent.st_ino):
            raise ValueError("public snapshot directory changed")
        prior = _existing_target(target, expected_uid, expected_gid, dir_fd=parent_fd,
                                 parent_device=opened_parent.st_dev)
        prior_raw = None if prior is None else _read_existing(
            target, expected_uid, expected_gid, dir_fd=parent_fd, expected=prior)
        if prior_raw is not None:
            validate_snapshot_bytes(prior_raw)
        descriptor, temp_path = tempfile.mkstemp(prefix=".announcements-v1-", dir=parent)
        temp_name = Path(temp_path).name
        replaced = False
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as output:
                os.fchown(output.fileno(), expected_uid, expected_gid)
                os.fchmod(output.fileno(), 0o600)
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
                os.fchmod(output.fileno(), 0o644)
            staged_target = parent / temp_name
            staged_info = _existing_target(staged_target, expected_uid, expected_gid,
                                           dir_fd=parent_fd, parent_device=opened_parent.st_dev)
            if staged_info is None:
                raise ValueError("staged public snapshot disappeared")
            validate_snapshot_bytes(_read_existing(staged_target, expected_uid, expected_gid,
                                                    dir_fd=parent_fd, expected=staged_info))
            current = _existing_target(target, expected_uid, expected_gid, dir_fd=parent_fd,
                                       parent_device=opened_parent.st_dev)
            if ((prior is None) != (current is None)
                    or (prior is not None and current is not None
                        and (prior.st_dev, prior.st_ino) != (current.st_dev, current.st_ino))):
                raise ValueError("public snapshot changed during publication")
            staged = os.stat(temp_name, dir_fd=parent_fd, follow_symlinks=False)
            if (staged.st_uid, staged.st_gid, staged.st_nlink, staged.st_mode & 0o7777) != (
                    expected_uid, expected_gid, 1, 0o644):
                raise ValueError("staged public snapshot metadata is unsafe")
            replace(temp_name, target.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            temp_name = None
            replaced = True
            os.fsync(parent_fd)
            installed = _existing_target(target, expected_uid, expected_gid, dir_fd=parent_fd,
                                         parent_device=opened_parent.st_dev)
            if installed is None or validate_snapshot_bytes(_read_existing(
                    target, expected_uid, expected_gid, dir_fd=parent_fd,
                    expected=installed)) != document:
                raise ValueError("published snapshot read-back failed")
        except BaseException:
            if replaced:
                if prior_raw is None:
                    try:
                        os.unlink(target.name, dir_fd=parent_fd)
                        os.fsync(parent_fd)
                    except FileNotFoundError:
                        pass
                else:
                    restore_fd, restore_name = tempfile.mkstemp(prefix=".announcements-restore-", dir=parent)
                    try:
                        with os.fdopen(restore_fd, "wb") as restore:
                            os.fchown(restore.fileno(), expected_uid, expected_gid)
                            os.fchmod(restore.fileno(), 0o600)
                            restore.write(prior_raw)
                            restore.flush()
                            os.fsync(restore.fileno())
                            os.fchmod(restore.fileno(), 0o644)
                        os.replace(Path(restore_name).name, target.name,
                                   src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                        os.fsync(parent_fd)
                    finally:
                        try:
                            os.unlink(restore_name, dir_fd=parent_fd)
                        except FileNotFoundError:
                            pass
            raise
    finally:
        if temp_name is not None:
            try:
                os.unlink(temp_name, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
        os.close(parent_fd)
    return document["revision"]


def publish(raw):
    """Production entry point; destination and ownership are not caller-selectable."""
    nobody = pwd.getpwnam("nobody")
    if (os.geteuid(), os.getegid()) != (nobody.pw_uid, nobody.pw_gid):
        raise PermissionError("snapshot writer identity is invalid")
    return _publish(raw, destination=DEFAULT_PUBLIC_SNAPSHOT, expected_uid=nobody.pw_uid,
                    expected_gid=nobody.pw_gid)


def _handle_connection(connection, *, allowed_uid, publisher=publish):
    peer_size = struct.calcsize("3i")
    _pid, peer_uid, _gid = struct.unpack(
        "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, peer_size))
    if peer_uid != allowed_uid:
        raise PermissionError("unauthorized snapshot publisher")
    connection.settimeout(3.0)
    length = struct.unpack("!I", _read_exact(connection, 4))[0]
    if length < 1 or length > MAX_PEER_REQUEST:
        raise ValueError("request size out of bounds")
    raw = _read_exact(connection, length)
    if connection.recv(1):
        raise ValueError("trailing request bytes")
    revision = publisher(raw)
    response = json.dumps({"ok": True, "revision": revision}, sort_keys=True,
                          separators=(",", ":")).encode("ascii")
    if len(response) > MAX_REPLY:
        raise ValueError("response size out of bounds")
    connection.sendall(struct.pack("!I", len(response)) + response)


def serve(listener, *, allowed_uid, publisher=publish):
    while True:
        connection, _ = listener.accept()
        with connection:
            try:
                _handle_connection(connection, allowed_uid=allowed_uid, publisher=publisher)
            except (OSError, ValueError, PermissionError):
                # Intentionally no exception text or request content in logs.
                try:
                    response = b'{"ok":false,"revision":0}'
                    connection.sendall(struct.pack("!I", len(response)) + response)
                except OSError:
                    pass


def systemd_listener():
    try:
        listen_pid = int(os.environ.get("LISTEN_PID", ""))
        listen_fds = int(os.environ.get("LISTEN_FDS", ""))
    except ValueError as error:
        raise RuntimeError("systemd socket activation metadata is invalid") from error
    if listen_pid != os.getpid() or listen_fds != 1:
        raise RuntimeError("expected one systemd-provided publisher socket")
    listener = socket.socket(fileno=3)
    if listener.family != socket.AF_UNIX or listener.type & socket.SOCK_STREAM != socket.SOCK_STREAM:
        listener.close()
        raise RuntimeError("systemd publisher socket type is invalid")
    return listener


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allowed-user", required=True)
    args = parser.parse_args()
    try:
        allowed_uid = pwd.getpwnam(args.allowed_user).pw_uid
    except KeyError as error:
        raise SystemExit("configured publisher user is unavailable") from error
    if allowed_uid <= 0:
        raise SystemExit("allowed publisher identity is invalid")
    serve(systemd_listener(), allowed_uid=allowed_uid)


if __name__ == "__main__":
    main()
