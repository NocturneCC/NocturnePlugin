"""Narrow Unix-socket publisher for validated public announcement snapshots."""
import argparse
import errno
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import socket
import stat
import struct
import uuid

from announcements import MAX_SNAPSHOT_BYTES, validate_snapshot_bytes


SOCKET_PATH = "/run/nocturne-announcement-snapshot-writer/publish.sock"
PUBLISH_ROOT = "/run/nocturne-announcement-public"
PUBLISH_DESTINATION = PUBLISH_ROOT + "/announcements-v1.json"
HOST_PUBLIC_ROOT = "/srv/projects/nocturne-plugin-announcements-public"
PROJECT_PARENT = "/srv/projects"
ACL_VERSION = 2
ACL_USER_OBJ, ACL_USER, ACL_GROUP_OBJ, ACL_MASK, ACL_OTHER = 0x01, 0x02, 0x04, 0x10, 0x20
ACL_UNDEFINED_ID = 0xFFFFFFFF
MAX_PEER_REQUEST = MAX_SNAPSHOT_BYTES
MAX_REPLY = 512


def _acl_entries(value):
    if value is None:
        return None
    if len(value) < 4 or len(value) % 8 != 4 or struct.unpack_from("<I", value)[0] != ACL_VERSION:
        raise ValueError("malformed POSIX ACL metadata")
    entries = {}
    for offset in range(4, len(value), 8):
        tag, permissions, identifier = struct.unpack_from("<HHI", value, offset)
        key = (tag, identifier if tag in (ACL_USER, 0x08) else ACL_UNDEFINED_ID)
        if tag not in (ACL_USER_OBJ, ACL_USER, ACL_GROUP_OBJ, 0x08, ACL_MASK, ACL_OTHER):
            raise ValueError("unsupported POSIX ACL entry")
        if permissions & ~0x7 or key in entries:
            raise ValueError("ambiguous POSIX ACL entry")
        entries[key] = permissions
    return entries


def _acl(fd, name):
    try:
        raw = os.getxattr(fd, name)
    except OSError as error:
        if error.errno in (errno.ENODATA, getattr(errno, "ENOATTR", errno.ENODATA)):
            return None
        raise ValueError("cannot verify POSIX ACL metadata") from error
    return _acl_entries(raw)


def _entries(owner, named_uid=None, named_permissions=None, group=0o5,
             mask=None, other=0o5):
    value = {(ACL_USER_OBJ, ACL_UNDEFINED_ID): owner,
             (ACL_GROUP_OBJ, ACL_UNDEFINED_ID): group,
             (ACL_OTHER, ACL_UNDEFINED_ID): other}
    if named_uid is not None:
        value[(ACL_USER, named_uid)] = named_permissions
        value[(ACL_MASK, ACL_UNDEFINED_ID)] = group if mask is None else mask
    elif mask is not None:
        value[(ACL_MASK, ACL_UNDEFINED_ID)] = mask
    return value


def _production_acl_profile(named_uid):
    return {
        "parent_access": _entries(0o7, named_uid, 0o7, group=0o5, mask=0o7, other=0o5),
        "parent_default": _entries(0o7, named_uid, 0o7, group=0o5, mask=0o7, other=0o5),
        "directory_access": _entries(0o7, named_uid, 0o7, group=0o5, mask=0o5, other=0o5),
        "directory_default": _entries(0o7, named_uid, 0o7, group=0o5, mask=0o7, other=0o5),
        "file_access": _entries(0o6, named_uid, 0o7, group=0o5, mask=0o4, other=0o4),
    }


def _check_acl(fd, access, default=None):
    if (_acl(fd, "system.posix_acl_access") != access
            or _acl(fd, "system.posix_acl_default") != default):
        raise ValueError("announcement snapshot ACL profile differs from the verified host profile")


def _open_directory(path, *, dir_fd=None):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, dir_fd=dir_fd)
    named = (os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
             if dir_fd is not None else Path(path).lstat())
    opened = os.fstat(descriptor)
    if (not stat.S_ISDIR(named.st_mode) or stat.S_ISLNK(named.st_mode)
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)):
        os.close(descriptor)
        raise ValueError("announcement directory changed while opening")
    return descriptor, opened


def _validate_host_tree(expected_uid, expected_gid, profile):
    expected_nodes = (("/srv", 0, 0, 0o755, None, None),
                      (PROJECT_PARENT, expected_uid, expected_gid, 0o2775,
                       profile["parent_access"], profile["parent_default"]),
                      (HOST_PUBLIC_ROOT, expected_uid, expected_gid, 0o755,
                       profile["directory_access"], profile["directory_default"]))
    previous_device = None
    for path, uid, gid, mode, access, default in expected_nodes:
        node = Path(path)
        before = node.lstat()
        if (not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode)
                or (before.st_uid, before.st_gid, stat.S_IMODE(before.st_mode)) != (uid, gid, mode)
                or (path == HOST_PUBLIC_ROOT and before.st_nlink != 2)
                or os.path.ismount(node)
                or (previous_device is not None and before.st_dev != previous_device)):
            raise ValueError("unsafe announcement snapshot directory metadata")
        descriptor, opened = _open_directory(path)
        try:
            if (opened.st_dev, opened.st_ino, opened.st_uid, opened.st_gid,
                    stat.S_IMODE(opened.st_mode), opened.st_nlink) != (
                    before.st_dev, before.st_ino, uid, gid, mode, before.st_nlink):
                raise ValueError("announcement snapshot directory changed during validation")
            _check_acl(descriptor, access, default)
        finally:
            os.close(descriptor)
        previous_device = before.st_dev


def validate_live_output():
    """Read-only validation of the established host directory and snapshot ACL profile."""
    user = pwd.getpwnam("randal")
    group = grp.getgrnam("www-data")
    acl_user = pwd.getpwnam("glob")
    profile = _production_acl_profile(acl_user.pw_uid)
    _validate_host_tree(user.pw_uid, group.gr_gid, profile)
    directory_fd, directory = _open_directory(HOST_PUBLIC_ROOT)
    try:
        info = _existing_target(Path(HOST_PUBLIC_ROOT) / "announcements-v1.json",
                                user.pw_uid, group.gr_gid, dir_fd=directory_fd,
                                parent_device=directory.st_dev)
        if info is None:
            raise ValueError("live announcement snapshot is missing")
        raw = _read_existing(Path(HOST_PUBLIC_ROOT) / "announcements-v1.json",
                             user.pw_uid, group.gr_gid, dir_fd=directory_fd,
                             expected=info, expected_acl=profile["file_access"])
        validate_snapshot_bytes(raw)
    finally:
        os.close(directory_fd)
    return {"uid": user.pw_uid, "gid": group.gr_gid,
            "snapshot_sha256": hashlib.sha256(raw).hexdigest(),
            "snapshot_bytes": len(raw)}


def _read_exact(connection, length):
    result = bytearray()
    while len(result) < length:
        chunk = connection.recv(length - len(result))
        if not chunk:
            raise ValueError("truncated request")
        result.extend(chunk)
    return bytes(result)


def _safe_directory(path, expected_uid, expected_gid, *, fixture=False, acl_profile=None):
    target = Path(path)
    expected_path = str(target) if fixture else PUBLISH_DESTINATION
    if str(target) != expected_path:
        raise ValueError("unexpected public snapshot destination")
    profile = acl_profile or {"directory_access": None, "directory_default": None,
                              "file_access": None}
    info = target.parent.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))
            != (expected_uid, expected_gid, 0o755)):
        raise ValueError("unsafe public snapshot directory metadata")
    if fixture:
        if os.path.ismount(target.parent):
            raise ValueError("fixture snapshot directory is a mount")
    elif (str(target) != PUBLISH_DESTINATION or info.st_nlink != 2
          or not os.path.ismount(target.parent)):
        raise ValueError("public output is not the verified systemd bind directory")
    descriptor, opened = _open_directory(target.parent)
    try:
        if (opened.st_dev, opened.st_ino, opened.st_uid, opened.st_gid,
                stat.S_IMODE(opened.st_mode), opened.st_nlink) != (
                info.st_dev, info.st_ino, expected_uid, expected_gid,
                stat.S_IMODE(info.st_mode), info.st_nlink):
            raise ValueError("public snapshot directory changed while validating")
        _check_acl(descriptor, profile["directory_access"], profile["directory_default"])
    finally:
        os.close(descriptor)
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


def _read_existing(target, expected_uid, expected_gid, *, dir_fd, expected, expected_acl=None):
    file_fd = os.open(target.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
    try:
        opened = os.fstat(file_fd)
        if (opened.st_dev, opened.st_ino, opened.st_uid, opened.st_gid,
                opened.st_nlink, opened.st_mode & 0o7777) != (
                expected.st_dev, expected.st_ino, expected_uid, expected_gid, 1, 0o644):
            raise ValueError("public snapshot changed while opening")
        _check_acl(file_fd, expected_acl, None)
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


def _new_temp(parent_fd, prefix, raw, expected_uid, expected_gid, expected_acl):
    descriptor = None
    for _attempt in range(8):
        name = prefix + uuid.uuid4().hex
        try:
            descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                 | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=parent_fd)
            break
        except FileExistsError:
            continue
    if descriptor is None:
        raise OSError("unable to allocate a unique snapshot staging file")
    try:
        os.fchown(descriptor, expected_uid, expected_gid)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = None
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
            os.fchmod(output.fileno(), 0o644)
            _check_acl(output.fileno(), expected_acl, None)
        return name
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(name, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        raise


def _publish(raw, *, destination, expected_uid, expected_gid, fixture=False,
             acl_profile=None, replace=os.replace):
    document = validate_snapshot_bytes(raw)
    profile = acl_profile or {"directory_access": None, "directory_default": None,
                              "file_access": None}
    target = _safe_directory(destination, expected_uid, expected_gid,
                             fixture=fixture, acl_profile=profile)
    parent = target.parent
    parent_fd, opened_parent = _open_directory(parent)
    temp_name = None
    try:
        named_parent = parent.lstat()
        if ((opened_parent.st_dev, opened_parent.st_ino) !=
                (named_parent.st_dev, named_parent.st_ino)):
            raise ValueError("public snapshot directory changed")
        _check_acl(parent_fd, profile["directory_access"], profile["directory_default"])
        prior = _existing_target(target, expected_uid, expected_gid, dir_fd=parent_fd,
                                 parent_device=opened_parent.st_dev)
        prior_raw = None if prior is None else _read_existing(
            target, expected_uid, expected_gid, dir_fd=parent_fd, expected=prior,
            expected_acl=profile["file_access"])
        if prior_raw is not None:
            validate_snapshot_bytes(prior_raw)
        replaced = False
        try:
            temp_name = _new_temp(parent_fd, ".announcements-v1-", raw,
                                  expected_uid, expected_gid, profile["file_access"])
            staged_target = parent / temp_name
            staged_info = _existing_target(staged_target, expected_uid, expected_gid,
                                           dir_fd=parent_fd, parent_device=opened_parent.st_dev)
            if staged_info is None:
                raise ValueError("staged public snapshot disappeared")
            validate_snapshot_bytes(_read_existing(staged_target, expected_uid, expected_gid,
                                                    dir_fd=parent_fd, expected=staged_info,
                                                    expected_acl=profile["file_access"]))
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
                    expected=installed, expected_acl=profile["file_access"])) != document:
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
                    restore_name = None
                    try:
                        restore_name = _new_temp(parent_fd, ".announcements-restore-", prior_raw,
                                                 expected_uid, expected_gid,
                                                 profile["file_access"])
                        os.replace(restore_name, target.name,
                                   src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                        os.fsync(parent_fd)
                    finally:
                        try:
                            if restore_name is not None:
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
    """Publish under the existing randal:www-data owner, inside a narrow unit sandbox."""
    writer = pwd.getpwnam("randal")
    group = grp.getgrnam("www-data")
    acl_user = pwd.getpwnam("glob")
    if (os.geteuid(), os.getegid()) != (writer.pw_uid, group.gr_gid):
        raise PermissionError("snapshot writer identity is invalid")
    return _publish(raw, destination=PUBLISH_DESTINATION, expected_uid=writer.pw_uid,
                    expected_gid=group.gr_gid,
                    acl_profile=_production_acl_profile(acl_user.pw_uid))


def _handle_connection(connection, *, allowed_uid, allowed_gid=None, publisher=publish):
    peer_size = struct.calcsize("3i")
    _pid, peer_uid, peer_gid = struct.unpack(
        "3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, peer_size))
    if peer_uid != allowed_uid or (allowed_gid is not None and peer_gid != allowed_gid):
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


def serve(listener, *, allowed_uid, allowed_gid=None, publisher=publish):
    while True:
        connection, _ = listener.accept()
        with connection:
            try:
                _handle_connection(connection, allowed_uid=allowed_uid,
                                   allowed_gid=allowed_gid, publisher=publisher)
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
    parser.add_argument("--allowed-group", required=True)
    args = parser.parse_args()
    try:
        allowed_uid = pwd.getpwnam(args.allowed_user).pw_uid
    except KeyError as error:
        raise SystemExit("configured publisher user is unavailable") from error
    if allowed_uid <= 0:
        raise SystemExit("allowed publisher identity is invalid")
    try:
        allowed_gid = grp.getgrnam(args.allowed_group).gr_gid
    except KeyError as error:
        raise SystemExit("configured publisher group is unavailable") from error
    serve(systemd_listener(), allowed_uid=allowed_uid, allowed_gid=allowed_gid)


if __name__ == "__main__":
    main()
