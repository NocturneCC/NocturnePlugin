import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import immutable_runtime_release as releases
import runtime_ownership as ownership


class RuntimeOwnershipTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "runtime"
        self.root.mkdir(mode=0o755)
        (self.root / "releases").mkdir(mode=0o755)
        self.commit = "a" * 40
        self.release = self.root / "releases" / self.commit
        self.release.mkdir(mode=0o755)
        payload = self.release / "payload"
        payload.write_text("fixture\n")
        releases.build_manifest(self.release, self.commit)
        payload.chmod(0o444)
        (self.release / "RELEASE-MANIFEST.json").chmod(0o444)
        self.release.chmod(0o555)

    def test_inspection_separates_selector_from_resolved_target_ownership(self):
        (self.root / "current").symlink_to(Path("releases") / self.commit)
        report = ownership.inspect(self.root, self.commit)
        current = next(node for node in report["nodes"]
                       if node["path"] == str(self.root / "current"))
        self.assertEqual("symlink", current["type"])
        self.assertEqual(str(self.release.resolve()), current["resolved_target"]["path"])
        self.assertIn("uid", current)
        self.assertIn("uid", current["resolved_target"])
        runtime_root = next(node for node in report["nodes"]
                            if node["path"] == str(self.root))
        self.assertEqual("basic", runtime_root["acl"])
        self.assertFalse(runtime_root["is_mount"])

    def test_dry_run_lists_only_exact_container_nodes_and_never_release_source(self):
        uid, gid = os.getuid(), os.getgid()
        report = ownership.migrate(self.root, self.commit, from_uid=uid, from_gid=gid,
                                   _root_uid=uid, _root_gid=gid)
        self.assertTrue(report["dry_run"])
        self.assertEqual(2, report["node_count"])
        self.assertEqual(uid, self.release.stat().st_uid)

    def test_service_owned_release_is_rejected_instead_of_chowned(self):
        uid, gid = os.getuid(), os.getgid()
        with self.assertRaisesRegex(ValueError, "immutable release ownership"):
            ownership._migration_nodes(self.root, self.commit, uid, gid,
                                       root_uid=uid + 1, root_gid=gid)

    def test_apply_is_root_guarded_and_failure_restores_changed_nodes(self):
        uid, gid = os.getuid(), os.getgid()
        if uid != 0:
            with patch("runtime_ownership._migration_nodes", return_value=[self.root]), \
                    self.assertRaises(PermissionError):
                ownership.migrate(self.root, self.commit, from_uid=uid, from_gid=gid,
                                  apply=True)
        calls = []
        real_chown = os.chown
        def failing(path, target_uid, target_gid, *, follow_symlinks):
            calls.append((Path(path), target_uid, target_gid))
            if len(calls) == 2:
                raise RuntimeError("fixture chown failure")
            real_chown(path, target_uid, target_gid, follow_symlinks=follow_symlinks)
        with patch("runtime_ownership.os.chown", side_effect=failing), \
                self.assertRaisesRegex(RuntimeError, "fixture chown failure"):
            ownership.migrate(self.root, self.commit, from_uid=uid, from_gid=gid,
                              apply=True, _root_uid=uid, _root_gid=gid)
        self.assertEqual((uid, gid), (self.root.stat().st_uid, self.root.stat().st_gid))


if __name__ == "__main__":
    unittest.main()
