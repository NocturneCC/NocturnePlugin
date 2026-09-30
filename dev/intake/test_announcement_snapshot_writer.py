import json
import os
import socket
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

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
        _handle_connection(connection, allowed_uid=os.geteuid(), publisher=publisher)
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

    def test_private_or_identity_fields_are_rejected_from_snapshot(self):
        value = json.loads(self.snapshot())
        value["actor"] = "unexpected"
        with self.assertRaisesRegex(ValueError, "snapshot fields"):
            validate_snapshot_bytes(json.dumps(value).encode())


if __name__ == "__main__":
    unittest.main()
