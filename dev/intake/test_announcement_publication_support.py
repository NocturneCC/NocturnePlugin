import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import announcement_publication_support as support


class AnnouncementPublicationSupportTest(unittest.TestCase):
    def test_sources_include_admin_dropin_and_exact_expected_stopped_set(self):
        source = Path(__file__).parent
        files = support._source_files(source)
        self.assertEqual({"nocturne_announcements.py", "announcement_snapshot_writer.py",
                          *support.UNIT_FILES, support.ADMIN_DROPIN_NAME}, set(files))
        self.assertIn("InaccessiblePaths=/srv/projects/nocturne-plugin-announcements-public",
                      files[support.ADMIN_DROPIN_NAME].decode())
        self.assertEqual({"osrs-drops-admin.service",
                          "nocturne-announcement-snapshot-writer.service",
                          "nocturne-announcement-snapshot-writer.socket"},
                         support.STOPPED_SERVICES)

    def test_rollback_restores_writer_files_and_admin_dropin_completely(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backup = root / "backup"
            backup.mkdir()
            names = ["announcement_snapshot_writer.py", "announcements.py",
                     *sorted(support.UNIT_FILES), support.ADMIN_DROPIN_NAME]
            targets = {}
            before = {}
            after = {}
            for index, name in enumerate(names):
                target = root / "installed" / (name.replace("/", "_"))
                target.parent.mkdir(exist_ok=True)
                original = f"previous-{index}".encode()
                current = f"replacement-{index}".encode()
                target.write_bytes(current)
                target.chmod(0o644)
                saved_name = f"{index}.before"
                saved = backup / saved_name
                saved.write_bytes(original)
                saved.chmod(0o644)
                metadata = {"uid": os.geteuid(), "gid": os.getegid(), "mode": 0o644,
                            "acl": "user::rw-\ngroup::r--\nother::r--\n\n"}
                targets[name] = target
                before[name] = {"existed": True, "metadata": metadata,
                                "sha256": hashlib.sha256(original).hexdigest(),
                                "backup": saved_name}
                after[name] = hashlib.sha256(current).hexdigest()
            manifest = {"before": before, "after": after}
            with patch.object(support, "_apply_metadata"), patch.object(support, "_verify_metadata"):
                support._restore_files(manifest, backup, targets, names, lambda *_a, **_k: None)
            for index, name in enumerate(names):
                self.assertEqual(f"previous-{index}".encode(), targets[name].read_bytes())

    def test_rollback_removes_only_newly_created_dropin_and_writer_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backup = root / "backup"
            backup.mkdir()
            names = ["announcement_snapshot_writer.py", support.ADMIN_DROPIN_NAME]
            targets = {}
            before = {}
            after = {}
            for index, name in enumerate(names):
                target = root / f"target-{index}"
                raw = f"new-{index}".encode()
                target.write_bytes(raw)
                targets[name] = target
                before[name] = {"existed": False, "metadata": None,
                                "sha256": None, "backup": None}
                after[name] = hashlib.sha256(raw).hexdigest()
            support._restore_files({"before": before, "after": after}, backup,
                                   targets, names, lambda *_a, **_k: None)
            self.assertTrue(all(not path.exists() for path in targets.values()))


if __name__ == "__main__":
    unittest.main()
