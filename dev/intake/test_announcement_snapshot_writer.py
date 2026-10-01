import json
import os
import socket
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

import announcement_snapshot_writer as writer
from announcement_snapshot_writer import _handle_connection, _publish
from announcements import MAX_SNAPSHOT_BYTES, validate_snapshot_bytes


class AnnouncementSnapshotWriterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o755)
        self.target = self.root / "announcements-v1.json"
        self.uid, self.gid = os.geteuid(), os.getegid()

    def snapshot(self, revision=7):
        return json.dumps({
            "schema_version": 1,
            "revision": revision,
            "generated_at": "2026-09-30T12:00:00Z",
            "announcements": [],
        }, sort_keys=True, separators=(",", ":")).encode()

    def publish(self, raw, **kwargs):
        return _publish(raw, destination=self.target, expected_uid=self.uid,
                        expected_gid=self.gid, fixture=True, **kwargs)

    def acl_profile(self, named_uid):
        return writer._production_acl_profile(named_uid)

    def test_empty_snapshot_is_valid_and_atomic_publication_has_exact_metadata(self):
        raw = self.snapshot()
        self.assertEqual(7, self.publish(raw))
        self.assertEqual(raw, self.target.read_bytes())
        info = self.target.lstat()
        self.assertEqual((self.uid, self.gid, 1, 0o644),
                         (info.st_uid, info.st_gid, info.st_nlink, info.st_mode & 0o777))
        self.assertEqual(7, validate_snapshot_bytes(self.target.read_bytes())["revision"])

    def test_failed_atomic_replace_preserves_previous_snapshot(self):
        previous = self.snapshot(7)
        self.publish(previous)

        def fail_replace(*_args, **_kwargs):
            raise OSError("fixture replace failure")

        with self.assertRaises(OSError):
            self.publish(self.snapshot(8), replace=fail_replace)
        self.assertEqual(previous, self.target.read_bytes())
        self.assertEqual({"announcements-v1.json"}, {path.name for path in self.root.iterdir()})

    def test_failed_post_rename_verification_restores_previous_snapshot(self):
        previous = self.snapshot(7)
        self.publish(previous)
        original_validator = writer.validate_snapshot_bytes
        calls = 0

        def fail_installed_readback(raw):
            nonlocal calls
            calls += 1
            if calls == 4:
                raise ValueError("fixture read-back rejection")
            return original_validator(raw)

        with patch.object(writer, "validate_snapshot_bytes", side_effect=fail_installed_readback):
            with self.assertRaisesRegex(ValueError, "read-back rejection"):
                self.publish(self.snapshot(8))
        self.assertEqual(previous, self.target.read_bytes())
        self.assertEqual({"announcements-v1.json"}, {path.name for path in self.root.iterdir()})

    def test_expected_acl_is_preserved_across_atomic_replacement(self):
        named_uid = self.uid + 12345
        profile = self.acl_profile(named_uid)
        def inherited_acl(fd, name):
            if os.fstat(fd).st_mode & 0o170000 == 0o040000:
                return profile["directory_access"] if name.endswith("access") else profile["directory_default"]
            return profile["file_access"] if name.endswith("access") else None
        with patch.object(writer, "_acl", side_effect=inherited_acl):
            self.publish(self.snapshot(7), acl_profile=profile)
            self.publish(self.snapshot(8), acl_profile=profile)
            fd = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                writer._check_acl(fd, profile["directory_access"], profile["directory_default"])
            finally:
                os.close(fd)
            file_fd = os.open(self.target, os.O_RDONLY)
            try:
                writer._check_acl(file_fd, profile["file_access"], None)
            finally:
                os.close(file_fd)
        self.assertEqual(8, validate_snapshot_bytes(self.target.read_bytes())["revision"])

    def test_unexpected_acl_rejected_and_previous_snapshot_preserved(self):
        named_uid = self.uid + 12345
        profile = self.acl_profile(named_uid)
        def inherited_acl(fd, name):
            if os.fstat(fd).st_mode & 0o170000 == 0o040000:
                return profile["directory_access"] if name.endswith("access") else profile["directory_default"]
            return profile["file_access"] if name.endswith("access") else None
        previous = self.snapshot(7)
        with patch.object(writer, "_acl", side_effect=inherited_acl):
            self.publish(previous, acl_profile=profile)
        altered = dict(profile)
        altered["directory_access"] = dict(profile["directory_access"])
        altered["directory_access"][(writer.ACL_OTHER, writer.ACL_UNDEFINED_ID)] = 0
        with patch.object(writer, "_acl", side_effect=inherited_acl):
            with self.assertRaisesRegex(ValueError, "ACL profile"):
                self.publish(self.snapshot(8), acl_profile=altered)
        self.assertEqual(previous, self.target.read_bytes())

    def test_live_contract_never_selects_nobody_and_uses_randal_www_data(self):
        expected_uid, expected_gid = 1234, 33
        with unittest.mock.patch.object(writer.pwd, "getpwnam", side_effect=lambda name:
                                        type("Passwd", (), {"pw_uid": expected_uid})()
                                        if name == "randal" else
                                        type("Passwd", (), {"pw_uid": 9876})()), \
             unittest.mock.patch.object(writer.grp, "getgrnam", return_value=type(
                 "Group", (), {"gr_gid": expected_gid})()), \
             unittest.mock.patch.object(writer.os, "geteuid", return_value=expected_uid), \
             unittest.mock.patch.object(writer.os, "getegid", return_value=expected_gid), \
             unittest.mock.patch.object(writer, "_publish", return_value=1) as publish:
            self.assertEqual(1, writer.publish(self.snapshot()))
        self.assertEqual(expected_uid, publish.call_args.kwargs["expected_uid"])
        self.assertEqual(expected_gid, publish.call_args.kwargs["expected_gid"])

    def test_nobody_writer_identity_is_rejected(self):
        with unittest.mock.patch.object(writer.pwd, "getpwnam", side_effect=lambda name:
                                        type("Passwd", (), {"pw_uid": 1234 if name == "randal" else 65534})()), \
             unittest.mock.patch.object(writer.grp, "getgrnam", return_value=type(
                 "Group", (), {"gr_gid": 33})()), \
             unittest.mock.patch.object(writer.os, "geteuid", return_value=65534), \
             unittest.mock.patch.object(writer.os, "getegid", return_value=65534):
            with self.assertRaises(PermissionError):
                writer.publish(self.snapshot())

    def test_symlinks_wrong_metadata_hardlinks_and_oversize_fail_closed(self):
        link = self.root / "alias"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            _publish(self.snapshot(), destination=link / "snapshot.json", expected_uid=self.uid,
                     expected_gid=self.gid, fixture=True)

        self.root.chmod(0o700)
        with self.assertRaisesRegex(ValueError, "directory metadata"):
            self.publish(self.snapshot())
        self.root.chmod(0o755)

        self.publish(self.snapshot())
        second = self.root / "second-link"
        os.link(self.target, second)
        with self.assertRaisesRegex(ValueError, "existing public snapshot"):
            self.publish(self.snapshot(8))
        second.unlink()

        with self.assertRaisesRegex(ValueError, "size"):
            validate_snapshot_bytes(b" " * (MAX_SNAPSHOT_BYTES + 1))

    def test_owner_mismatch_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "directory metadata"):
            _publish(self.snapshot(), destination=self.target, expected_uid=self.uid + 1,
                     expected_gid=self.gid, fixture=True)
        with self.assertRaisesRegex(ValueError, "directory metadata"):
            _publish(self.snapshot(), destination=self.target, expected_uid=self.uid,
                     expected_gid=self.gid + 1, fixture=True)

    def test_ordinary_mount_output_is_rejected(self):
        with patch.object(writer.os.path, "ismount", return_value=True):
            with self.assertRaisesRegex(ValueError, "fixture snapshot directory is a mount"):
                self.publish(self.snapshot())

    def test_socket_protocol_accepts_only_authenticated_peer_and_snapshot(self):
        received = []
        publisher = Mock(side_effect=lambda raw: received.append(raw) or 9)
        raw = self.snapshot(9)

        class FakeConnection:
            def __init__(self):
                self.incoming = bytearray(len(raw).to_bytes(4, "big") + raw)
                self.outgoing = bytearray()

            def getsockopt(self, _level, _option, _size):
                return (1).to_bytes(4, byteorder=os.sys.byteorder, signed=True) + \
                    os.geteuid().to_bytes(4, byteorder=os.sys.byteorder, signed=True) + \
                    os.getegid().to_bytes(4, byteorder=os.sys.byteorder, signed=True)

            def settimeout(self, _timeout):
                pass

            def recv(self, length):
                if not self.incoming:
                    return b""
                value = bytes(self.incoming[:length])
                del self.incoming[:length]
                return value

            def sendall(self, value):
                self.outgoing.extend(value)

        connection = FakeConnection()
        _handle_connection(connection, allowed_uid=os.geteuid(), allowed_gid=os.getegid(),
                           publisher=publisher)
        response_length = int.from_bytes(connection.outgoing[:4], "big")
        response = json.loads(connection.outgoing[4:4 + response_length])
        self.assertEqual([raw], received)
        self.assertEqual({"ok": True, "revision": 9}, response)

    def test_socket_protocol_rejects_other_peer_uid_before_publication(self):
        called = []
        raw = self.snapshot(10)

        class FakeConnection:
            def __init__(self):
                self.incoming = bytearray(len(raw).to_bytes(4, "big") + raw)

            def getsockopt(self, _level, _option, _size):
                return (1).to_bytes(4, byteorder=os.sys.byteorder, signed=True) + \
                    (os.geteuid() + 1).to_bytes(4, byteorder=os.sys.byteorder, signed=True) + \
                    os.getegid().to_bytes(4, byteorder=os.sys.byteorder, signed=True)

            def settimeout(self, _timeout):
                pass

        with self.assertRaises(PermissionError):
            _handle_connection(FakeConnection(), allowed_uid=os.geteuid(),
                               publisher=lambda data: called.append(data))
        self.assertEqual([], called)

    def test_socket_protocol_rejects_wrong_peer_gid(self):
        raw = self.snapshot()

        class FakeConnection:
            def getsockopt(self, *_args):
                return struct.pack("3i", 1, os.geteuid(), os.getegid() + 1)

        import struct
        with self.assertRaises(PermissionError):
            _handle_connection(FakeConnection(), allowed_uid=os.geteuid(),
                               allowed_gid=os.getegid(), publisher=Mock())

    def test_units_constrain_writer_and_admin_to_exact_boundary(self):
        root = Path(__file__).parent
        service = (root / "nocturne-announcement-snapshot-writer.service").read_text()
        socket_unit = (root / "nocturne-announcement-snapshot-writer.socket").read_text()
        admin_dropin = (root / "osrs-drops-admin-announcement-writer.conf").read_text()
        self.assertIn("User=randal\nGroup=www-data", service)
        self.assertIn("RuntimeDirectory=nocturne-announcement-public", service)
        self.assertIn("InaccessiblePaths=/srv/projects", service)
        self.assertIn("BindPaths=/srv/projects/nocturne-plugin-announcements-public:/run/nocturne-announcement-public", service)
        self.assertIn("ReadWritePaths=/run/nocturne-announcement-public", service)
        self.assertNotIn("ReadWritePaths=/srv/projects", service)
        self.assertIn("SocketUser=randal", socket_unit)
        self.assertIn("SocketGroup=randal", socket_unit)
        self.assertIn("SocketMode=0600", socket_unit)
        self.assertIn("InaccessiblePaths=/srv/projects/nocturne-plugin-announcements-public", admin_dropin)

    def test_private_or_identity_fields_are_rejected_from_snapshot(self):
        value = json.loads(self.snapshot())
        value["actor"] = "unexpected"
        with self.assertRaisesRegex(ValueError, "snapshot fields"):
            validate_snapshot_bytes(json.dumps(value).encode())


if __name__ == "__main__":
    unittest.main()
