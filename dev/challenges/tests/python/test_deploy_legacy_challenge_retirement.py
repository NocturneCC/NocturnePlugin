from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TREE = Path(__file__).resolve().parents[2]
TEST_COMMIT = "4f5f5dffee60490fa42d96c231b0f81870381192"
sys.path.insert(0, str(TREE / "website"))
import legacy_challenge_retirement as transforms

spec = importlib.util.spec_from_file_location("deploy_legacy_retirement_test", TREE / "deploy_legacy_challenge_retirement.py")
deploy = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = deploy
spec.loader.exec_module(deploy)

timing_spec = importlib.util.spec_from_file_location(
    "retirement_test_timing_support", TREE / "deploy_timing_metadata.py")
timing_support = importlib.util.module_from_spec(timing_spec)
assert timing_spec and timing_spec.loader
sys.modules[timing_spec.name] = timing_support
timing_spec.loader.exec_module(timing_support)


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class TransformIdempotenceTests(unittest.TestCase):
    def test_member_consumer_removal_is_idempotent_but_partial_shapes_fail(self):
        source = """async function loadNocturneChallengeCard(rsn) {
  return fetch(`/api/nocturne-challenges/summary?rsn=${encodeURIComponent(rsn)}`);
}

async function loadMember() {
  loadNocturneChallengeCard(member.rsn || rsn);
}
"""
        once = transforms.remove_legacy_member_summary_consumer(source)
        self.assertEqual(once, transforms.remove_legacy_member_summary_consumer(once))
        self.assertNotIn("loadNocturneChallengeCard", once)
        with self.assertRaises(ValueError):
            transforms.remove_legacy_member_summary_consumer(
                "unrecognized member viewer content\n"
            )


class OutputPlanningTests(unittest.TestCase):
    def setUp(self):
        self.live = {}
        self.predecessors = {}
        for path, spec in deploy.LIVE_TARGETS.items():
            if spec["kind"] == "member":
                data = (b"async function loadNocturneChallengeCard(rsn) {\n"
                        b"  return fetch(`/api/nocturne-challenges/summary?rsn=${rsn}`);\n}\n\n"
                        b"async function loadMember() {\n  loadNocturneChallengeCard(member.rsn || rsn);\n}\n")
            elif spec["kind"] == "links":
                data = b'<a href="/nocturne-challenge-progress.html">Challenges</a>\n'
            else:
                data = ("old:" + path).encode()
            self.live[path] = data
            self.predecessors[path] = digest(data)

    def test_exact_target_set_transforms_deterministically_and_repeat_is_current(self):
        api = b"published active-config-backed routes"
        redirect = transforms.REDIRECT_PAGE.encode()
        first = deploy.build_file_outputs(self.live, api, redirect, transforms,
                                          predecessor_hashes=self.predecessors)
        second = deploy.build_file_outputs(self.live, api, redirect, transforms,
                                           predecessor_hashes=self.predecessors)
        self.assertEqual(list(deploy.LIVE_TARGETS), [row["target"] for row in first])
        self.assertEqual([(x["after_sha256"], x["after_data"]) for x in first],
                         [(x["after_sha256"], x["after_data"]) for x in second])
        self.assertTrue(all(row["changed"] for row in first))
        by_target = {row["target"]: row for row in first}
        self.assertEqual(api, by_target["/srv/projects/nocturne-services/challenge_config_api.py"]["after_data"])
        self.assertEqual(redirect, by_target["/srv/projects/website/nocturne-challenge-progress.html"]["after_data"])
        self.assertNotIn(b"nocturne-challenge-progress.html", by_target["/srv/projects/website/index.html"]["after_data"])
        self.assertNotIn(b"/api/nocturne-challenges/summary", by_target["/srv/projects/website/member-viewer.html"]["after_data"])
        installed = {path: by_target[path]["after_data"] for path in deploy.LIVE_TARGETS}
        after_hashes = {path: digest(data) for path, data in installed.items()}
        again = deploy.build_file_outputs(installed, api, redirect, transforms,
                                          predecessor_hashes=after_hashes)
        self.assertFalse(any(row["changed"] for row in again))

    def test_unknown_live_shape_and_one_byte_drift_fail_closed(self):
        broken = dict(self.live)
        target = "/srv/projects/website/member-viewer.html"
        broken[target] = b"unknown member viewer shape"
        with self.assertRaises(deploy.RetirementError) as caught:
            deploy.build_file_outputs(broken, b"api", b"redirect", transforms,
                                      predecessor_hashes=self.predecessors)
        self.assertEqual("website_source_shape_unsupported", caught.exception.category)
        drift = dict(self.live)
        target = "/srv/projects/website/index.html"
        drift[target] += b"x"
        with self.assertRaises(deploy.RetirementError) as caught:
            deploy.build_file_outputs(drift, b"api", b"redirect", transforms,
                                      predecessor_hashes=self.predecessors)
        self.assertEqual("target_hash_drift", caught.exception.category)


class MetadataAndRollbackTests(unittest.TestCase):
    ACL = "user::rw-\nuser:1003:rw-\ngroup::r--\nmask::rw-\nother::r--\n"

    def test_target_acl_and_metadata_profile_is_exact(self):
        path = Path("/srv/projects/website/index.html")
        meta = {"uid": 1001, "gid": 33, "mode": "0664", "nlink": 1,
                "acl_text": self.ACL, "acl_sha256": "unused"}
        support = SimpleNamespace(_acl_entries=lambda value: {
            "user::": "rw-", "user:1003:": "rw-", "group::": "r--",
            "mask::": "rw-", "other::": "r--"})
        deploy._file_profile(path, meta, None, support)
        for changed in ({"nlink": 2}, {"mode": "0666"}):
            with self.assertRaises(deploy.RetirementError):
                deploy._file_profile(path, {**meta, **changed}, None, support)
        bad_acl = SimpleNamespace(_acl_entries=lambda _value: {"user::": "rwx"})
        with self.assertRaises(deploy.RetirementError):
            deploy._file_profile(path, meta, None, bad_acl)

    def test_api_predecessor_acl_digest_is_bound_to_manifest(self):
        path = Path("/srv/projects/nocturne-services/challenge_config_api.py")
        acl = ("user::rw-\nuser:1003:rwx\t#effective:rw-\n"
               "group::rwx\t#effective:rw-\nmask::rw-\nother::r--\n")
        meta = {"uid": 1001, "gid": 33, "mode": "0664", "nlink": 1,
                "acl_text": acl, "acl_sha256": timing_support._acl_hash(acl)}
        parsed = {"user::": "rw-", "user:1003:": "rwx", "group::": "rwx",
                  "mask::": "rw-", "other::": "r--"}
        support = SimpleNamespace(_acl_entries=timing_support._acl_entries)
        source_manifest = json.loads((TREE / "source-manifest.json").read_text())
        manifest_records = [row for row in source_manifest["live_sources"]
                            if row.get("path") == str(path)]
        self.assertEqual(1, len(manifest_records))
        manifest_record = manifest_records[0]
        self.assertEqual(timing_support._acl_hash(acl), manifest_record["acl_sha256"])
        deploy._file_profile(path, meta, manifest_record, support)
        with self.assertRaises(deploy.RetirementError):
            deploy._file_profile(path, meta, {"acl_sha256": "0" * 64}, support)

        # Exact getfacl -cpn form: the named user's raw rwx is masked to rw-;
        # it must not be normalized to rw- in the profile comparison.
        actual = timing_support._acl_entries(acl)
        self.assertEqual(parsed, actual)
        altered_profiles = (
            acl.replace("user:1003:rwx\t#effective:rw-\n", ""),
            acl.replace("other::r--\n", "other::r--\nuser:2000:r--\n"),
            acl.replace("mask::rw-", "mask::rwx"),
            acl.replace("user:1003:rwx", "user:1003:rw-"),
        )
        for altered in altered_profiles:
            altered_digest = timing_support._acl_hash(altered)
            altered_meta = {**meta, "acl_text": altered, "acl_sha256": altered_digest}
            with self.subTest(acl=altered):
                with self.assertRaises(deploy.RetirementError):
                    deploy._file_profile(path, altered_meta,
                                         {"acl_sha256": altered_digest}, support)
        for changed in ({"uid": 1000}, {"gid": 34}, {"mode": "0666"}, {"nlink": 2}):
            with self.subTest(metadata=changed):
                with self.assertRaises(deploy.RetirementError):
                    deploy._file_profile(path, {**meta, **changed},
                                         {"acl_sha256": "pinned-acl"}, support)

    def test_api_acl_file_type_remains_regular_only(self):
        # _file_profile receives capture_file metadata only after its O_NOFOLLOW
        # regular-file/single-link checks; preserve an explicit regression for
        # those guard predicates at the capture boundary.
        with tempfile.TemporaryDirectory() as directory_text:
            root = Path(directory_text)
            regular = root / "regular"
            regular.write_text("safe")
            alias = root / "alias"
            alias.symlink_to(regular)
            with self.assertRaises(deploy.RetirementError):
                deploy._read_nofollow(alias)
            linked = root / "linked"
            os.link(regular, linked)
            with self.assertRaises(deploy.RetirementError):
                deploy._read_nofollow(linked)

    def test_nofollow_capture_rejects_symlink_and_hardlink_ambiguity(self):
        with tempfile.TemporaryDirectory() as directory_text:
            root = Path(directory_text)
            ordinary = root / "ordinary"
            ordinary.write_bytes(b"safe")
            symlink = root / "symlink"
            symlink.symlink_to(ordinary)
            with self.assertRaises(deploy.RetirementError):
                deploy._read_nofollow(symlink)
            linked = root / "linked"
            linked.write_bytes(b"linked")
            second = root / "linked-copy"
            second.hardlink_to(linked)
            with self.assertRaises(deploy.RetirementError):
                deploy._read_nofollow(linked)

    def test_rollback_restores_exact_backup_bytes_and_metadata(self):
        with tempfile.TemporaryDirectory() as directory_text:
            directory = Path(directory_text)
            target, backup = directory / "target", directory / "backup"
            target.write_bytes(b"new bytes")
            backup.write_bytes(b"old bytes")
            before_meta = {"uid": 1001, "gid": 33, "mode": "0664", "nlink": 1,
                           "acl_sha256": "acl-old", "acl_text": self.ACL}
            entry = {"target": str(target), "backup_name": "backup",
                     "before_sha256": digest(b"old bytes"), "after_sha256": digest(b"new bytes"),
                     "backup_sha256": digest(b"old bytes"), "before_metadata": before_meta}
            captured = {**before_meta, "sha256": digest(b"new bytes")}
            seen = []

            def replace(path, data, metadata):
                seen.append(dict(metadata))
                path.write_bytes(data)

            support = SimpleNamespace(capture_file=lambda _path: captured,
                                      _atomic_replace=replace)
            with mock.patch.object(deploy, "_verify_backup", return_value=None):
                deploy._atomic_restore_files([entry], directory, support)
            self.assertEqual(b"old bytes", target.read_bytes())
            self.assertEqual(before_meta, seen[0])

    def test_rollback_rejects_metadata_drift_even_when_hash_is_pinned(self):
        with tempfile.TemporaryDirectory() as directory_text:
            directory = Path(directory_text)
            target, backup = directory / "target", directory / "backup"
            target.write_bytes(b"new bytes")
            backup.write_bytes(b"old bytes")
            meta = {"uid": 1001, "gid": 33, "mode": "0664", "nlink": 1,
                    "acl_sha256": "acl-old", "acl_text": self.ACL}
            entry = {"target": str(target), "backup_name": "backup",
                     "before_sha256": digest(b"old bytes"), "after_sha256": digest(b"new bytes"),
                     "backup_sha256": digest(b"old bytes"), "before_metadata": meta}
            support = SimpleNamespace(capture_file=lambda _path: {**meta, "mode": "0600",
                                                                   "sha256": digest(b"new bytes")})
            with self.assertRaises(deploy.RetirementError) as caught:
                deploy._atomic_restore_files([entry], directory, support)
            self.assertEqual("rollback_target_metadata_drift", caught.exception.category)
            self.assertEqual(b"new bytes", target.read_bytes())


class RouteVerificationTests(unittest.TestCase):
    def test_route_contract_and_no_store_retirement(self):
        with tempfile.TemporaryDirectory() as directory_text:
            temp = Path(directory_text)
            release = temp / "release"
            release.mkdir()
            # Dispatch by URL to model all eight read-only probes with safe,
            # bounded fixture response bodies; no socket is opened.
            def public_get(url, expected, _tmp, _probe, _state):
                if "nocturne-challenge-progress.html" in url:
                    status = 200
                    body = (b"new URLSearchParams(location.search) encodeURIComponent(rsn) "
                            b"/challenge-member.html?rsn= '/challenges.html'")
                    return status, body, b"HTTP/1.1 200 OK\r\n\r\n"
                if url.endswith(("/summary", "/progress", "/leaderboards")):
                    return 410, json.dumps({"ok": False, "error": "legacy_endpoint_retired"}).encode(), \
                        b"HTTP/1.1 410 Gone\r\nCache-Control: no-store\r\n\r\n"
                if url.endswith("/bosses"):
                    return 200, b'{"ok":true,"bosses":[]}', b"HTTP/1.1 200 OK\r\n\r\n"
                return 200, b'{"ok":true}', b"HTTP/1.1 200 OK\r\n\r\n"

            with mock.patch.object(deploy, "_load_module", return_value=SimpleNamespace(request=lambda *_a, **_k: 200)), \
                    mock.patch.object(deploy, "_write_private", side_effect=lambda p, d: Path(p).write_bytes(d)), \
                    mock.patch.object(deploy, "_public_get", side_effect=public_get):
                result = deploy._verify_public_routes(temp, release)
            self.assertEqual(8, len(result))
            self.assertEqual(410, result["legacy_summary"])

    def test_retired_route_requires_no_store_header(self):
        with tempfile.TemporaryDirectory() as directory_text:
            temp = Path(directory_text)
            release = temp / "release"
            release.mkdir()

            def public_get(url, _expected, *_args):
                if "nocturne-challenge-progress.html" in url:
                    return 200, b"new URLSearchParams(location.search) encodeURIComponent(rsn) /challenge-member.html?rsn= '/challenges.html'", b""
                if url.endswith(("/summary", "/progress", "/leaderboards")):
                    return 410, b'{"error":"legacy_endpoint_retired"}', b"HTTP/1.1 410 Gone\r\n\r\n"
                return 200, b'{"ok":true,"bosses":[]}', b"HTTP/1.1 200 OK\r\n\r\n"

            with mock.patch.object(deploy, "_load_module", return_value=SimpleNamespace(request=lambda *_a, **_k: 200)), \
                    mock.patch.object(deploy, "_write_private", side_effect=lambda p, d: Path(p).write_bytes(d)), \
                    mock.patch.object(deploy, "_public_get", side_effect=public_get):
                with self.assertRaises(deploy.RetirementError) as caught:
                    deploy._verify_public_routes(temp, release)
            self.assertEqual("legacy_route_not_retired", caught.exception.category)


class IdempotencyTests(unittest.TestCase):
    def test_apply_already_current_does_not_create_backup_or_control_units(self):
        with tempfile.TemporaryDirectory() as directory_text:
            lock = Path(directory_text) / "lock"

            def take_lock(_path):
                return os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)

            support = SimpleNamespace(_deployment_lock=take_lock,
                                      _prepared_check=lambda *_args: None)
            systemd = SimpleNamespace()
            plan = {"target_commit": TEST_COMMIT, "status": "already_current", "_items": []}
            with mock.patch.object(deploy.os, "geteuid", return_value=0), \
                    mock.patch.object(deploy, "make_plan", return_value=plan):
                result = deploy.apply_plan(commit=TEST_COMMIT, plan=plan,
                                           support=support, systemd=systemd)
            self.assertEqual({"status": "already_current", "target_commit": TEST_COMMIT,
                              "rewritten_files": 0}, result)

    def test_full_commit_sha_is_required(self):
        with self.assertRaises(deploy.RetirementError) as caught:
            deploy.make_plan(commit="short", support=SimpleNamespace(), systemd=SimpleNamespace())
        self.assertEqual("invalid_commit", caught.exception.category)


class ApplicationStateMachineTests(unittest.TestCase):
    def _run(self, *, verification_fails=False, sheet_running=False):
        temp_ctx = tempfile.TemporaryDirectory()
        self.addCleanup(temp_ctx.cleanup)
        root = Path(temp_ctx.name)
        target = root / "challenge_config_api.py"
        target.write_bytes(b"prior API")
        before_meta = {"uid": 1001, "gid": 33, "mode": "0664", "nlink": 1,
                       "acl_sha256": "acl", "acl_text": "user::rw-\ngroup::rw-\nother::r--\n"}
        after_data = b"retired API"
        before_capture = {**before_meta, "type": "regular", "sha256": digest(b"prior API"),
                          "size": len(b"prior API")}
        item = {"target": str(target), "before_sha256": digest(b"prior API"),
                "after_sha256": digest(after_data), "before_data": b"prior API",
                "after_data": after_data, "before_metadata": before_capture,
                "changed": True, "kind": "api"}
        plan = {"target_commit": TEST_COMMIT, "status": "upgrade_required", "_items": [item]}
        database_profile = {"integrity": "ok", "journal_mode": "wal", "locking_mode": "normal",
                            "active_config_version_id": 10}
        backup_root = root / "backups"
        backup_root.mkdir()
        units = {
            deploy.SERVICE: {"Id": deploy.SERVICE, "LoadState": "loaded", "ActiveState": "active",
                             "SubState": "running", "MainPID": "123", "UnitFileState": "enabled"},
            deploy.SHEET_TIMER: {"Id": deploy.SHEET_TIMER, "LoadState": "loaded", "ActiveState": "active",
                                 "SubState": "waiting", "MainPID": "0", "UnitFileState": "enabled"},
            deploy.SHEET_SERVICE: {"Id": deploy.SHEET_SERVICE, "LoadState": "loaded",
                                   "ActiveState": "active" if sheet_running else "inactive",
                                   "SubState": "running" if sheet_running else "dead",
                                   "MainPID": "222" if sheet_running else "0", "UnitFileState": "static"},
        }
        plan["services"] = {unit: dict(state) for unit, state in units.items()}
        calls = []
        self.last_target = target
        self.last_calls = calls

        def capture(path):
            data = Path(path).read_bytes()
            return {**before_meta, "type": "regular", "sha256": digest(data), "size": len(data)}

        def atomic_replace(path, data, metadata):
            self.assertEqual(before_capture, metadata)
            Path(path).write_bytes(data)

        def backup_dir(_root, commit):
            self.assertEqual(TEST_COMMIT, commit)
            value = root / "backups" / "transaction"
            value.mkdir()
            return value

        def backup_database(_source, destination, _uid):
            Path(destination).write_bytes(b"sqlite-consistent-snapshot-fixture")

        def write_private(path, data):
            Path(path).write_bytes(data)
            Path(path).chmod(0o600)

        support = SimpleNamespace(
            _deployment_lock=lambda _path: os.open(root / "lock", os.O_CREAT | os.O_RDWR, 0o600),
            _prepared_check=lambda *_args: None,
            _create_backup_dir=backup_dir,
            _backup_database=backup_database,
            capture_file=capture,
            _verify_private_file=lambda *_args: None,
            _require_basic_acl=lambda *_args: None,
            _sha_file=lambda path: digest(Path(path).read_bytes()),
            _atomic_replace=atomic_replace,
            _run=lambda argv, **_kwargs: SimpleNamespace(
                returncode=0, stdout=("static\n" if deploy.SHEET_SERVICE in argv else "enabled\n")),
        )

        class FakeSystemd:
            def show(self, unit):
                return dict(units[unit])

            def wait_inactive(self, *_args, **_kwargs):
                return None

            def wait_active(self, *_args, **_kwargs):
                return None

            def wait_job_idle(self, *_args, **_kwargs):
                units[deploy.SHEET_SERVICE].update(ActiveState="inactive", SubState="dead", MainPID="0")
                return None

        def systemctl(_support, action, unit):
            calls.append((action, unit))
            if unit == deploy.SHEET_TIMER:
                if action == "stop":
                    units[unit].update(ActiveState="inactive", SubState="dead")
                elif action == "start":
                    units[unit].update(ActiveState="active", SubState="waiting")
            if unit == deploy.SERVICE:
                if action == "stop":
                    units[unit].update(ActiveState="inactive", SubState="dead", MainPID="0")
                elif action == "start":
                    units[unit].update(ActiveState="active", SubState="running", MainPID="456")

        def restore_api(prior, _systemd, _support):
            self.assertEqual("active", prior["ActiveState"])
            units[deploy.SERVICE].update(ActiveState="active", SubState="running", MainPID="789")
            calls.append(("restore", deploy.SERVICE))

        def restore_timer(prior, _systemd, _support):
            self.assertEqual("active", prior["ActiveState"])
            units[deploy.SHEET_TIMER].update(ActiveState="active", SubState="waiting")
            calls.append(("restore", deploy.SHEET_TIMER))

        systemd = FakeSystemd()
        patches = [
            mock.patch.object(deploy, "LIVE_TARGETS", {str(target): {"kind": "api"}}),
            mock.patch.object(deploy, "make_plan", return_value=plan),
            mock.patch.object(deploy, "_database_summary", return_value=database_profile),
            mock.patch.object(deploy, "_capture_unit_state", side_effect=lambda _sd, _s, unit, **_kw: dict(units[unit])),
            mock.patch.object(deploy, "_systemctl", side_effect=systemctl),
            mock.patch.object(deploy, "_restore_api", side_effect=restore_api),
            mock.patch.object(deploy, "_restore_timer", side_effect=restore_timer),
            mock.patch.object(deploy, "_write_private", side_effect=write_private),
            mock.patch.object(deploy, "_write_record", side_effect=lambda path, value: write_private(path, json.dumps(value).encode())),
            mock.patch.object(deploy, "_verify_backup", return_value=None),
            mock.patch.object(deploy.os, "geteuid", return_value=0),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        verifier = (lambda *_args: (_ for _ in ()).throw(deploy.RetirementError("fixture_route_failure"))) \
            if verification_fails else (lambda *_args: {"routes": "ok"})
        original_lstat = Path.lstat

        def fixture_lstat(path):
            if path.name == "Challenges.db.snapshot":
                return SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_nlink=1, st_uid=0,
                                       st_gid=0, st_size=len(b"sqlite-consistent-snapshot-fixture"))
            return original_lstat(path)

        with mock.patch.object(deploy, "_fsync_dir", return_value=None):
            with mock.patch.object(Path, "lstat", autospec=True, side_effect=fixture_lstat):
                return deploy.apply_plan(commit=TEST_COMMIT, plan=plan, support=support,
                                         systemd=systemd, database=root / "Challenges.db",
                                         backup_root=backup_root, verify_live=verifier), target, calls

    def test_apply_restarts_only_api_and_restores_sheet_timer_state(self):
        result, target, calls = self._run()
        self.assertEqual("applied", result["status"])
        self.assertEqual(b"retired API", target.read_bytes())
        self.assertIn(("stop", deploy.SHEET_TIMER), calls)
        self.assertIn(("restore", deploy.SHEET_TIMER), calls)
        self.assertTrue(all(unit in {deploy.SERVICE, deploy.SHEET_TIMER} for _action, unit in calls))

    def test_failed_live_route_verification_rolls_files_and_services_back(self):
        with self.assertRaises(deploy.RetirementError):
            self._run(verification_fails=True)
        self.assertEqual(b"prior API", self.last_target.read_bytes())
        self.assertIn(("restore", deploy.SERVICE), self.last_calls)
        self.assertIn(("restore", deploy.SHEET_TIMER), self.last_calls)

    def test_running_oneshot_is_drained_but_not_replayed_or_installed(self):
        with self.assertRaises(deploy.RetirementError) as caught:
            self._run(sheet_running=True)
        self.assertEqual(b"prior API", self.last_target.read_bytes())
        self.assertEqual("rollback_complete", caught.exception.category)
        self.assertEqual("maintenance_stop", caught.exception.phase)
        self.assertNotIn(("start", deploy.SHEET_SERVICE), self.last_calls)
        self.assertIn(("restore", deploy.SHEET_TIMER), self.last_calls)



if __name__ == "__main__":
    unittest.main()
