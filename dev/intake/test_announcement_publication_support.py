import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import unittest
from unittest.mock import patch

import announcement_publication_support as support


class AnnouncementPublicationSupportTest(unittest.TestCase):
    def _apply_fixture(self, mutation=None, *, api_current=False):
        repository = Path(__file__).resolve().parents[2]
        source_root = repository / "dev" / "intake"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = root / "release" / "dev" / "intake"
            release.mkdir(parents=True)
            for source_name in set(support.EXPECTED_ARTIFACT_SOURCE_MAP.values()):
                shutil.copyfile(source_root / source_name, release / source_name)
            api = root / "api"
            api.mkdir()
            api_target = api / "nocturne_announcements.py"
            api_target.write_bytes((release / "announcements.py").read_bytes()
                                  if api_current else b"prior installed module\n")
            api_target.chmod(0o644)
            library_parent = root / "usr-local-lib"
            library_parent.mkdir()
            library_parent.chmod(0o755)
            library = library_parent / "nocturne-plugin"
            systemd = root / "systemd"
            systemd.mkdir()
            systemd.chmod(0o755)
            admin_dropins = systemd / "osrs-drops-admin.service.d"
            admin_dropins.mkdir(mode=0o755)
            backups = root / "backups"
            backups.mkdir(mode=0o700)
            backups.chmod(0o700)
            file_metadata = {"uid": os.geteuid(), "gid": os.getegid(), "mode": 0o644,
                             "acl": "user::rw-\ngroup::r--\nother::r--\n\n"}
            args = dict(
                repo=repository, commit="a" * 40, source_dir=release,
                api_dir=api, library_dir=library, systemd_dir=systemd,
                admin_dropin_dir=admin_dropins, backup_root=backups, apply=True,
                maintenance_confirmed=True, stopped_services=support.STOPPED_SERVICES,
                expected_module_sha256=hashlib.sha256(api_target.read_bytes()).hexdigest(),
                service_active=lambda _unit: False)
            real_lstat = Path.lstat

            def fixture_lstat(path):
                result = real_lstat(path)
                if stat.S_ISDIR(result.st_mode):
                    fields = list(result)
                    fields[4] = 0
                    fields[5] = 0
                    return os.stat_result(fields)
                return result

            changed = False

            def capture_metadata(_path, _run, *, directory=False):
                if directory:
                    return {"uid": 0, "gid": 0, "mode": 0o755,
                            "acl": "user::rwx\ngroup::r-x\nother::r-x\n\n"}
                return file_metadata

            def service_active(unit):
                nonlocal changed
                if mutation is not None and not changed:
                    changed = True
                    mutation(api_target, systemd, release)
                return False

            args["service_active"] = service_active
            patches = (
                patch.object(support, "_source_trust"),
                patch.object(support, "validate_live_output", return_value={"uid": 1000, "gid": 33}),
                patch.object(support, "_capture_safe_metadata", side_effect=capture_metadata),
                patch.object(support, "_apply_metadata"),
                patch.object(support, "_verify_metadata"),
                patch.object(support, "_require_basic_acl"),
                patch.object(support.os, "geteuid", return_value=0),
                patch.object(support.os, "chown"),
                patch.object(Path, "lstat", fixture_lstat),
            )
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                    patches[5], patches[6], patches[7], patches[8]:
                if mutation is None:
                    result = support.install(**args)
                    self.assertEqual("already_current" if api_current else "upgrade_required",
                                     result["api_state"])
                    self.assertEqual("installed", result["state"])
                else:
                    with self.assertRaises(ValueError):
                        support.install(**args)
                    self.assertEqual([], list(backups.iterdir()))
                    self.assertFalse(library.exists())

    def test_complete_install_dry_run_uses_canonical_release_to_target_mapping(self):
        repository = Path(__file__).resolve().parents[2]
        source_root = repository / "dev" / "intake"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            release = root / "release" / "dev" / "intake"
            release.mkdir(parents=True)
            for source_name in set(support.EXPECTED_ARTIFACT_SOURCE_MAP.values()):
                shutil.copyfile(source_root / source_name, release / source_name)
            api = root / "api"
            api.mkdir()
            (api / "nocturne_announcements.py").write_bytes(b"prior installed module\n")
            (api / "nocturne_announcements.py").chmod(0o644)
            library_parent = root / "usr-local-lib"
            library_parent.mkdir()
            library_parent.chmod(0o755)
            library = library_parent / "nocturne-plugin"
            systemd = root / "systemd"
            systemd.mkdir()
            systemd.chmod(0o755)
            admin_dropins = systemd / "osrs-drops-admin.service.d"
            backups = root / "backups"
            backups.mkdir(mode=0o700)
            backups.chmod(0o700)
            real_lstat = Path.lstat

            def fixture_lstat(path):
                result = real_lstat(path)
                if stat.S_ISDIR(result.st_mode):
                    fields = list(result)
                    fields[4] = 0
                    fields[5] = 0
                    return os.stat_result(fields)
                return result

            file_metadata = {"uid": os.geteuid(), "gid": os.getegid(), "mode": 0o644,
                             "acl": "user::rw-\ngroup::r--\nother::r--\n\n"}
            args = dict(
                repo=repository, commit="a" * 40, source_dir=release,
                api_dir=api, library_dir=library, systemd_dir=systemd,
                admin_dropin_dir=admin_dropins, backup_root=backups)
            before = self._tree_state(root)
            with patch.object(support, "_source_trust"), \
                 patch.object(support, "validate_live_output", return_value={"uid": 1000, "gid": 33}), \
                 patch.object(support, "_capture_safe_metadata", return_value=file_metadata), \
                 patch.object(Path, "lstat", fixture_lstat):
                self.assertEqual((0, 0), (systemd.lstat().st_uid, systemd.lstat().st_gid))
                self.assertEqual((0, 0), (backups.lstat().st_uid, backups.lstat().st_gid))
                self.assertEqual(0, systemd.lstat().st_mode & 0o022)
                self.assertEqual(0, backups.lstat().st_mode & 0o022)
                first = support.install(**args)
                second = support.install(**args)
            self.assertEqual(first, second)
            self.assertTrue(first["dry_run"])
            self.assertEqual("upgrade_required", first["api_state"])
            self.assertEqual(set(support.EXPECTED_ARTIFACT_SOURCE_MAP), set(first["artifacts"]))
            for target_name, source_name in support.EXPECTED_ARTIFACT_SOURCE_MAP.items():
                item = first["artifacts"][target_name]
                self.assertEqual(hashlib.sha256((release / source_name).read_bytes()).hexdigest(),
                                 item["after_sha256"])
                self.assertEqual("install", item["state"])
            self.assertEqual(hashlib.sha256((release / "announcements.py").read_bytes()).hexdigest(),
                             first["artifacts"]["nocturne_announcements.py"]["after_sha256"])
            self.assertEqual(api / "nocturne_announcements.py",
                             Path(first["artifacts"]["nocturne_announcements.py"]["target"]))
            self.assertEqual(library / "announcements.py",
                             Path(first["artifacts"]["announcements.py"]["target"]))
            self.assertEqual(admin_dropins / "20-nocturne-announcement-snapshot-writer.conf",
                             Path(first["artifacts"][support.ADMIN_DROPIN_NAME]["target"]))
            self.assertEqual(before, self._tree_state(root))

    def _tree_state(self, root):
        result = []
        for path in sorted(root.rglob("*")):
            info = path.lstat()
            result.append((str(path.relative_to(root)), info.st_mode, info.st_uid, info.st_gid,
                           info.st_nlink, path.read_bytes() if stat.S_ISREG(info.st_mode) else None))
        return result

    def test_artifact_mapping_rejects_incomplete_duplicate_and_unknown_pairs(self):
        pairs = support.ARTIFACT_SOURCE_PAIRS
        with self.assertRaisesRegex(ValueError, "incomplete or unexpected"):
            support._validated_artifact_source_map(pairs[:-1])
        with self.assertRaisesRegex(ValueError, "duplicate target"):
            support._validated_artifact_source_map((*pairs, pairs[0]))
        with self.assertRaisesRegex(ValueError, "incomplete or unexpected"):
            support._validated_artifact_source_map((*pairs[:-1],
                ("unrecognized-target", "announcement_snapshot_writer.py")))

    def test_unchanged_upgrade_required_target_passes_apply(self):
        self._apply_fixture()

    def test_unchanged_already_current_target_passes_apply(self):
        self._apply_fixture(api_current=True)

    def test_apply_rejects_target_state_changes_after_preflight(self):
        def changed_contents(target, _systemd, _release):
            target.write_bytes(b"changed after preflight\n")

        def changed_metadata(target, _systemd, _release):
            target.chmod(0o600)

        def disappeared(target, _systemd, _release):
            target.unlink()

        def symlink_substitution(target, _systemd, _release):
            target.unlink()
            target.symlink_to("replacement.py")

        def appeared(_target, systemd, release):
            unit = systemd / "nocturne-announcement-snapshot-writer.service"
            unit.write_bytes((release / unit.name).read_bytes())
            unit.chmod(0o644)

        for mutation in (changed_contents, changed_metadata, disappeared,
                         symlink_substitution, appeared):
            with self.subTest(mutation=mutation.__name__):
                self._apply_fixture(mutation)

    def test_sources_include_admin_dropin_and_exact_expected_stopped_set(self):
        source = Path(__file__).parent
        files = support._source_files(source)
        self.assertEqual({"nocturne_announcements.py", "announcement_snapshot_writer.py",
                          "announcements.py",
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
            names = ["nocturne_announcements.py", "announcement_snapshot_writer.py", "announcements.py",
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
            manifest = {"targets": {name: str(target) for name, target in targets.items()},
                        "before": before, "after": after}
            self.assertEqual(set(names), set(manifest["targets"]))
            self.assertIn("nocturne_announcements.py", manifest["targets"])
            self.assertNotIn("nocturne_announcements.py", support.EXPECTED_ARTIFACT_SOURCE_MAP.values())
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
