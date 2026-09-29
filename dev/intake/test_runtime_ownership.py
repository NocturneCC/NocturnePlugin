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
        (self.root / "current").symlink_to(Path("releases") / self.commit)

    def test_inspection_separates_selector_from_resolved_target_ownership(self):
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
        nodes=[self.root,self.root/"releases"]
        with patch("runtime_ownership._migration_nodes",return_value=nodes):
            report = ownership.migrate(self.root, self.commit, from_uid=uid, from_gid=gid)
        self.assertTrue(report["dry_run"])
        self.assertEqual(2, report["node_count"])
        self.assertEqual(uid, self.release.stat().st_uid)

    def test_service_owned_release_is_rejected_instead_of_chowned(self):
        uid, gid = os.getuid(), os.getgid()
        with self.assertRaisesRegex(ValueError, "immutable release ownership"):
            ownership._migration_nodes(self.root, self.commit, uid, gid)

    def test_apply_is_root_guarded_and_failure_restores_changed_nodes(self):
        uid, gid = os.getuid(), os.getgid()
        if uid != 0:
            with patch("runtime_ownership._migration_nodes", return_value=[self.root]), \
                    self.assertRaises(PermissionError):
                ownership.migrate(self.root, self.commit, from_uid=uid, from_gid=gid,
                                  apply=True)
        calls = []; owners={self.root:(uid,gid),self.root/"releases":(uid,gid)}
        def snapshot(path):
            path=Path(path); owner=owners[path]
            return {"path":str(path),"dev":1,"ino":1 if path==self.root else 2,
                    "nlink":2,"uid":owner[0],"gid":owner[1],"mode":0o755,
                    "type":0o040000,"mount":False,"acl":"basic","link_target":None}
        def failing(path, target_uid, target_gid, *, follow_symlinks):
            calls.append((Path(path), target_uid, target_gid))
            if len(calls) == 2:
                raise RuntimeError("fixture chown failure")
            owners[Path(path)]=(target_uid,target_gid)
        with patch("runtime_ownership._migration_nodes",return_value=list(owners)), \
                patch("runtime_ownership._snapshot",side_effect=snapshot), \
                patch("runtime_ownership.verify_release"), \
                patch("runtime_ownership.verify_release_ownership"), \
                self.assertRaisesRegex(RuntimeError, "fixture chown failure"):
            ownership.migrate(self.root, self.commit, from_uid=uid, from_gid=gid,
                              apply=True,_geteuid=lambda:0,_chown=failing)
        self.assertEqual((uid,gid),owners[self.root])

    def test_rollback_failure_is_reported_without_recursive_chown(self):
        uid,gid=os.getuid(),os.getgid(); nodes=[self.root,self.root/"releases"]
        values={path:(uid,gid) for path in nodes}; calls=[]
        def snapshot(path):
            owner=values[Path(path)]
            return {"path":str(path),"dev":1,"ino":nodes.index(Path(path))+1,
                    "nlink":2,"uid":owner[0],"gid":owner[1],"mode":0o755,
                    "type":0o040000,"mount":False,"acl":"basic","link_target":None}
        def chown(path,target_uid,target_gid,*,follow_symlinks):
            calls.append((Path(path),target_uid,target_gid,follow_symlinks))
            if len(calls)==2: raise OSError("apply failure")
            if len(calls)==3: raise OSError("rollback failure")
            values[Path(path)]=(target_uid,target_gid)
        with patch("runtime_ownership._migration_nodes",return_value=nodes), \
                patch("runtime_ownership._snapshot",side_effect=snapshot), \
                patch("runtime_ownership.verify_release"), \
                patch("runtime_ownership.verify_release_ownership"), \
                self.assertRaisesRegex(RuntimeError,"rollback failed"):
            ownership.migrate(self.root,self.commit,from_uid=uid,from_gid=gid,
                              apply=True,_geteuid=lambda:0,_chown=chown)
        self.assertTrue(all(call[3] is False for call in calls))


if __name__ == "__main__":
    unittest.main()
