from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

HERE = Path(__file__).resolve()
TREE = HERE.parents[2]
SERVICE = TREE / "service"
os.environ["CHALLENGE_INTAKE_SKIP_DEFAULT_APP"] = "1"
sys.path.insert(0, str(SERVICE))
spec = importlib.util.spec_from_file_location("deploy_automatic_observations_test", TREE / "deploy_automatic_observations.py")
deploy = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = deploy
spec.loader.exec_module(deploy)
import challenge_automatic_intake as automatic


class AutomaticSchemaTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_fresh_migration_is_additive_complete_and_idempotent(self):
        self.assertEqual("absent", deploy._schema_state(self.conn, automatic))
        automatic.migrate_schema(self.conn)
        self.assertEqual("present", deploy._schema_state(self.conn, automatic))
        expected = deploy._expected_schema_signature(automatic)
        automatic.migrate_schema(self.conn)
        self.assertEqual("present", deploy._schema_state(self.conn, automatic))
        self.assertEqual(expected, deploy._expected_schema_signature(automatic))
        self.assertEqual((0,), self.conn.execute("SELECT COUNT(*) FROM challenge_automatic_observations").fetchone())
        self.assertEqual((0,), self.conn.execute("SELECT COUNT(*) FROM challenge_automatic_observation_participants").fetchone())

    def test_partial_or_modified_schema_is_unsupported(self):
        self.conn.execute("CREATE TABLE challenge_automatic_observations(observation_id INTEGER)")
        self.assertEqual("unsupported", deploy._schema_state(self.conn, automatic))

    def test_rollback_before_observation_data_removes_only_additive_tables(self):
        self.conn.execute("CREATE TABLE retained_manual_submission(id INTEGER PRIMARY KEY)")
        self.conn.execute("INSERT INTO retained_manual_submission VALUES(1)")
        self.conn.commit()
        automatic.migrate_schema(self.conn)
        automatic.rollback_schema(self.conn)
        self.assertEqual("absent", deploy._schema_state(self.conn, automatic))
        self.assertEqual(1, self.conn.execute("SELECT COUNT(*) FROM retained_manual_submission").fetchone()[0])

    def test_rollback_refuses_after_observation_exists(self):
        automatic.migrate_schema(self.conn)
        self.conn.execute("PRAGMA foreign_keys=OFF")
        self.conn.execute("""INSERT INTO challenge_automatic_observations(
            schema_version,provenance,client_event_id,canonical_payload_sha256,semantic_fingerprint,
            reporter_normalized_rsn,activity_key,mode_key,occurred_at,observed_group_size,
            plugin_version,config_version_id,disposition,created_at)
            VALUES(1,'nocturne_runelite_automatic','event','%s','%s','test','theatre_of_blood',
            'tob_duo','2026-10-03T00:00:00Z',1,'0.3.2',1,'ignored_manual_only','2026-10-03T00:00:00Z')""" % ("a" * 64, "b" * 64))
        self.conn.commit()
        with self.assertRaisesRegex(RuntimeError, "data prevents"):
            automatic.rollback_schema(self.conn)
        self.assertEqual("present", deploy._schema_state(self.conn, automatic))


class TargetClassificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service_patch = mock.patch.object(deploy, "SERVICE_ROOT", self.root)
        self.service_patch.start()
        self.addCleanup(self.service_patch.stop)
        self.paths = [self.root / name for name in ("challenge_automatic_intake.py", "challenge_intake_api.py",
                                                     "leaderboard_challenge_ingest.py", "routes.conf")]
        self.items = []
        self.hashes = {}
        for index, path in enumerate(self.paths):
            source = self.root / f"source-{index}"
            source.write_bytes(f"new-{index}".encode())
            after = hashlib.sha256(source.read_bytes()).hexdigest()
            baseline_data = f"old-{index}".encode()
            baseline_hash = hashlib.sha256(baseline_data).hexdigest()
            baseline = None if index == 0 else {
                "sha256": baseline_hash, "uid": 1001, "gid": 33, "mode": "0664" if index != 2 else "0755",
                "nlink": 1, "size": len(baseline_data), "acl_sha256": "acl-api", "acl_text": "user::rw-\n"}
            self.hashes[path] = (baseline_data, source.read_bytes(), baseline)
            self.items.append({"source_relative": f"source-{index}", "source": source, "target": path,
                               "after_sha256": after, "after_size": len(source.read_bytes()),
                               "baseline": baseline, "predecessor_kind": "absent" if index == 0 else "manifest"})
        self.support = SimpleNamespace(_safe_parent=lambda _path: None, capture_file=self.capture)

    def capture(self, path):
        if not path.exists():
            raise FileNotFoundError(path)
        data = path.read_bytes()
        _old, _new, baseline = self.hashes[path]
        return {"sha256": hashlib.sha256(data).hexdigest(), "uid": 1001, "gid": 33,
                "mode": baseline["mode"] if baseline else "0664", "nlink": 1, "size": len(data),
                "acl_sha256": "acl-api", "acl_text": "user::rw-\n"}

    def write_before(self):
        for path, (old, _new, baseline) in zip(self.paths, self.hashes.values()):
            if baseline is not None:
                path.write_bytes(old)

    def write_after(self):
        for path, (_old, new, _baseline) in zip(self.paths, self.hashes.values()):
            path.write_bytes(new)

    def test_fresh_predecessor_and_already_installed_classification(self):
        self.write_before()
        state, items = deploy._classify_files(self.items, self.support)
        self.assertEqual("predecessor", state)
        self.assertEqual(4, len(items))
        self.write_after()
        state, _items = deploy._classify_files(self.items, self.support)
        self.assertEqual("installed", state)

    def test_one_byte_or_partial_drift_fails_closed(self):
        self.write_before()
        self.paths[1].write_bytes(b"changed")
        with self.assertRaisesRegex(deploy.DeploymentError, "unsupported live file content drift"):
            deploy._classify_files(self.items, self.support)
        self.write_after()
        self.paths[2].write_bytes(b"changed")
        with self.assertRaisesRegex(deploy.DeploymentError, "unsupported live file content drift"):
            deploy._classify_files(self.items, self.support)


class NewFileMetadataTests(unittest.TestCase):
    ACL_TEXT = "user::rw-\ngroup::rw-\nother::r--\n"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.service_patch = mock.patch.object(deploy, "SERVICE_ROOT", self.root)
        self.service_patch.start()
        self.addCleanup(self.service_patch.stop)
        self.api = self.root / "challenge_intake_api.py"
        self.new_module = self.root / "challenge_automatic_intake.py"
        self.api_source = self.root / "api-source.py"
        self.api_backup = self.root / "api-backup"
        self.api_bytes = b"verified predecessor API"
        self.api_new_bytes = b"updated API source"
        self.new_bytes = b"new automatic intake module"
        self.api.write_bytes(self.api_bytes)
        self.api_source.write_bytes(self.api_new_bytes)
        self.api_backup.write_bytes(self.api_bytes)
        self.source = self.root / "source.py"
        self.source.write_bytes(self.new_bytes)
        self.acl_sha = hashlib.sha256(self.ACL_TEXT.encode()).hexdigest()
        self.baseline = {
            "type": "regular", "sha256": hashlib.sha256(self.api_bytes).hexdigest(),
            "uid": 1001, "gid": 33, "mode": "0664", "nlink": 1,
            "size": len(self.api_bytes), "acl_sha256": self.acl_sha,
        }
        self.before = {**self.baseline, "acl_text": self.ACL_TEXT}
        self.api_record = {
            "source_relative": "dev/challenges/service/challenge_intake_api.py",
            "source": self.api_source, "target": self.api,
            "after_sha256": hashlib.sha256(self.api_new_bytes).hexdigest(),
            "after_size": len(self.api_new_bytes),
            "baseline": self.baseline, "predecessor_kind": "manifest",
            "was_absent": False, "before_sha256": self.baseline["sha256"],
            "before_metadata": self.before,
            "backup_path": str(self.api_backup),
            "backup_sha256": hashlib.sha256(self.api_bytes).hexdigest(),
        }
        self.new_record = {
            "source_relative": "dev/challenges/service/challenge_automatic_intake.py",
            "source": self.source, "target": self.new_module,
            "after_sha256": hashlib.sha256(self.new_bytes).hexdigest(),
            "after_size": len(self.new_bytes), "baseline": None,
            "predecessor_kind": "absent", "was_absent": True,
            "before_sha256": None, "before_metadata": None,
        }
        self.installed_meta = {}
        self.support = SimpleNamespace(
            _acl_hash=lambda text: hashlib.sha256(text.encode()).hexdigest(),
            _read_regular=lambda path, *_args: Path(path).read_bytes(),
            _sha_file=lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            _safe_parent=lambda _path: None,
            _fsync_dir=lambda _path: None,
            _atomic_replace=self.atomic_replace,
            capture_file=self.capture,
        )

    def atomic_replace(self, path, data, metadata):
        path.write_bytes(data)
        self.installed_meta[path] = dict(metadata)

    def capture(self, path):
        if not path.exists():
            raise FileNotFoundError(path)
        data = path.read_bytes()
        if path == self.api:
            metadata = self.before
        else:
            metadata = self.installed_meta[path]
        return {**metadata, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}

    def records(self):
        return [dict(self.new_record), dict(self.api_record)]

    def test_manifest_baseline_without_acl_text_uses_verified_capture(self):
        self.assertNotIn("acl_text", self.baseline)
        self.assertEqual(
            {key: self.before[key] for key in deploy._NEW_FILE_META_KEYS},
            deploy._expected_new_file_meta(self.records(), self.support),
        )

    def test_install_uses_captured_api_owner_mode_and_acl(self):
        records = self.records()
        tx = SimpleNamespace(file_backups=records)
        deploy._install_files(tx, records, self.support)
        self.assertEqual(self.new_bytes, self.new_module.read_bytes())
        self.assertEqual({key: self.before[key] for key in deploy._NEW_FILE_META_KEYS},
                         self.installed_meta[self.new_module])

    def test_rollback_removes_new_target_using_same_captured_profile(self):
        records = self.records()
        tx = SimpleNamespace(file_backups=records)
        deploy._install_files(tx, records, self.support)
        deploy._restore_target_files(tx, self.support)
        self.assertFalse(self.new_module.exists())
        self.assertEqual(self.api_bytes, self.api.read_bytes())

    def test_incomplete_ambiguous_or_unsafe_capture_fails_closed(self):
        cases = []
        missing_acl = self.records()
        missing_acl[1]["before_metadata"] = dict(self.before)
        missing_acl[1]["before_metadata"].pop("acl_text")
        cases.append(("missing acl text", missing_acl))
        missing_capture = self.records()
        missing_capture[1]["before_metadata"] = None
        cases.append(("missing capture", missing_capture))
        duplicate = self.records()
        duplicate.append(dict(self.api_record))
        cases.append(("duplicate API capture", duplicate))
        mismatched_acl = self.records()
        mismatched_acl[1]["before_metadata"] = {**self.before, "acl_sha256": "f" * 64}
        cases.append(("ACL digest mismatch", mismatched_acl))
        mismatched_acl_profile = self.records()
        mismatched_acl_profile[1]["before_metadata"] = {**self.before, "acl_text": "user::rwx\n"}
        cases.append(("ACL text mismatch", mismatched_acl_profile))
        unsafe_link = self.records()
        unsafe_link[1]["before_metadata"] = {**self.before, "nlink": 2}
        cases.append(("unsafe link count", unsafe_link))
        unsafe_mode = self.records()
        unsafe_mode[1]["before_metadata"] = {**self.before, "mode": "0999"}
        cases.append(("unsafe mode", unsafe_mode))
        unsafe_owner = self.records()
        unsafe_owner[1]["before_metadata"] = {**self.before, "uid": "1001"}
        cases.append(("unsafe owner type", unsafe_owner))
        wrong_target = self.records()
        wrong_target[1]["target"] = self.root / "different.py"
        cases.append(("wrong API target", wrong_target))
        missing_api = [self.new_record]
        cases.append(("missing API capture", missing_api))
        for label, records in cases:
            with self.subTest(label=label), self.assertRaises(deploy.DeploymentError):
                deploy._expected_new_file_meta(records, self.support)

    def test_diagnostic_categories_are_fixed_and_preserve_explicit_values(self):
        self.assertEqual("file_installation_failure", deploy._diagnostic_category(
            deploy.DeploymentError("deployment failed phase=file_installation rollback=complete")))
        self.assertEqual("sidecar_quiescence_failure", deploy._diagnostic_category(
            deploy.DeploymentError("diagnostic_category=sidecar_quiescence_failure")))
        self.assertEqual("preflight_blocked", deploy._diagnostic_category(
            deploy.DeploymentError("some private raw detail")))


class NginxAndSmokeTests(unittest.TestCase):
    def test_nginx_candidate_has_one_observation_route_and_preserves_approved(self):
        valid = (b"location = /api/challenges/intake/approved {\n}\n"
                 b"location = /api/challenges/intake/observations {\n"
                 b"proxy_pass http://127.0.0.1:5011/api/challenges/intake/observations;\n}\n")
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "routes.conf"
            path.write_bytes(valid)
            deploy._validate_nginx_fragment(path, SimpleNamespace(_read_regular=lambda p, _n: p.read_bytes()))
            path.write_bytes(valid + b"location = /api/challenges/intake/observations {\n}\n")
            with self.assertRaisesRegex(deploy.DeploymentError, "route fragment"):
                deploy._validate_nginx_fragment(path, SimpleNamespace(_read_regular=lambda p, _n: p.read_bytes()))

    def test_malformed_smokes_are_bounded_and_create_no_observation_rows(self):
        observations = []
        def request(url, **kwargs):
            observations.append((url, kwargs))
            if url.endswith("/health"):
                return 200, b'{"ok":true}'
            if url.endswith("/approved"):
                return 401, b'{"ok":false}'
            if kwargs.get("content_type") == "text/plain":
                return 415, b'{"state":"invalid"}'
            if len(kwargs.get("body") or b"") > deploy.MAX_OBSERVATION_BODY:
                return 413, b'{"state":"invalid"}'
            if url.endswith("/observations"):
                return 422, b'{"state":"invalid"}'
            return 200, b'{"ok":true}'
        support = SimpleNamespace(_readonly_database_snapshot=lambda _db: None)
        systemd = SimpleNamespace(show=lambda _unit: {"LoadState":"loaded", "ActiveState":"active",
                                                        "SubState":"running", "MainPID":"77"})
        prior = {"config_module": object(), "automatic_module": object(), "active_version_id": 10,
                 "active_config_sha256":"cfg", "table_counts":{"challenge_intake_request_audit":3,
                 "challenge_submissions":8,"leaderboard_observations":5}, "observation_counts":{},
                 "integrity":"ok"}
        with mock.patch.object(deploy, "_observation_counts", return_value={deploy.SCHEMA_TABLES[0]:0,
                                                                              deploy.SCHEMA_TABLES[1]:0}), \
             mock.patch.object(deploy, "_database_snapshot", return_value={
                 "integrity":"ok", "active_version_id":10, "active_config_sha256":"cfg",
                 "table_counts":{"challenge_intake_request_audit":4,"challenge_submissions":8,
                                 "leaderboard_observations":5},
                 "observation_counts":{deploy.SCHEMA_TABLES[0]:0,deploy.SCHEMA_TABLES[1]:0}}):
            result = deploy.verify_live(Path("unused"), systemd, "77", prior,
                                        SimpleNamespace(), request=request)
        self.assertFalse(result["valid_observation_sent"])
        obs = [entry for entry in observations if entry[0].endswith("/observations")]
        self.assertEqual(3, len(obs))
        self.assertEqual([b"{}", b"{}", b" " * 8193], [x[1]["body"] for x in obs])
        self.assertEqual("invalid/no rows", result["observation_smokes"])

    def test_nginx_reload_waits_for_committed_worker_generation_convergence(self):
        events = []
        generation = {"master_pid": 77, "worker_pids": [80, 81]}
        helper = SimpleNamespace(
            capture_generation=lambda: events.append("capture") or generation,
            wait_for_reload=lambda before, **bounds: events.append(("converged", before, bounds)))
        systemd = SimpleNamespace(show=lambda _unit: {
            "LoadState":"loaded", "ActiveState":"active", "SubState":"running", "MainPID":"77"})
        support = SimpleNamespace(_run=lambda argv, timeout: events.append(("reload", argv, timeout))
                                  or SimpleNamespace(returncode=0))
        with mock.patch.object(deploy, "_load_nginx_convergence", return_value=helper):
            deploy._reload_nginx(systemd, {"MainPID":"77"}, support, Path("/release"))
        self.assertEqual("capture", events[0])
        self.assertEqual("reload", events[1][0])
        self.assertEqual("converged", events[2][0])
        self.assertEqual({"attempts":40,"delay":0.25}, events[2][2])


class TransactionStateMachineTests(unittest.TestCase):
    def test_retired_sheet_sync_is_not_in_controlled_units(self):
        self.assertNotIn("nocturne-challenge-sheet-sync.service", deploy.CONTROLLED)
        self.assertNotIn("nocturne-challenge-sheet-sync.timer", deploy.CONTROLLED)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.calls = []
        self.backup_dir = self.root / "tx"
        self.backup_dir.mkdir()
        self.backup = self.backup_dir / "db.backup"
        self.items = [{"source_relative":"auto.py", "source":self.root / "source",
                       "target":self.root / "installed", "after_sha256":"after", "after_size":5,
                       "baseline":None, "was_absent":True}]
        self.units = {name:{"Id":name,"LoadState":"loaded","ActiveState":"active","SubState":"running","MainPID":"5"}
                      for name in deploy.LONG_SERVICES}
        self.units.update({name:{"Id":name,"LoadState":"loaded","ActiveState":"inactive","SubState":"dead","MainPID":"0"}
                           for name in (*deploy.WRITER_SERVICES,*deploy.TIMERS)})
        self.nginx = {"Id":"nginx.service","LoadState":"loaded","ActiveState":"active","SubState":"running","MainPID":"77"}
        self.support = SimpleNamespace(
            verify_git=lambda *_: None,
            capture_file=lambda _path: {"sha256":"db", "uid":1001, "gid":33, "mode":"0664",
                                        "nlink":1, "size":10, "acl_sha256":"acl", "acl_text":"acl"},
            _load_manifest=lambda *_: ({},{}),
            _prepared_check=lambda *_: "prepared",
            _create_backup_dir=lambda *_: self.backup_dir,
            _write_private=self.write_private,
            _verify_private_file=lambda *_: None,
            _fsync_dir=lambda *_: None,
            _backup_database=self.backup_database,
            _sha_file=lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest(),
            _stat_identity=lambda st: (st.st_dev,st.st_ino,st.st_uid,st.st_gid,stat.S_IMODE(st.st_mode),st.st_nlink,st.st_size,st.st_mtime_ns),
            _db_lstat=lambda _p: SimpleNamespace(st_dev=1,st_ino=2,st_uid=1001,st_gid=33,st_mode=stat.S_IFREG|0o664,st_nlink=1,st_size=10,st_mtime_ns=2),
            _sidecar_state_snapshot=lambda _p: {},
            _stop_for_migration=lambda *_: self.calls.append("stop"),
            _verify_migration_maintenance=lambda *_: self.calls.append("inactive_verified"),
            _wait_for_no_sqlite_sidecars=lambda *_a,**_kw: self.calls.append("sidecars_clear"),
            _restore_units=lambda *_: self.calls.append("restore"),
            Systemd=lambda: None)
        self.systemd = SimpleNamespace(show=lambda unit: self.nginx if unit == "nginx.service" else self.units[unit])
        self.snapshot = {"integrity":"ok","journal_mode":"delete","locking_mode":"normal",
                         "active_version_id":10,"active_config_sha256":"config","table_counts":{
                         "challenge_submissions":3,"leaderboard_observations":2,"challenge_intake_request_audit":1},
                         "schema_state":"absent","observation_counts":{
                         deploy.SCHEMA_TABLES[0]:0,deploy.SCHEMA_TABLES[1]:0},
                         "database_metadata":{},"database_file_meta":{}}
        self.plan = {"status":"dry_run","target_commit":"a"*40,"nginx_would_change":True}
        self.runtime_root = self.root / "runtime"
        self.release = self.runtime_root / "releases" / ("a" * 40)

    def write_private(self, path, data):
        Path(path).write_bytes(data)
        Path(path).chmod(0o600)

    def backup_database(self, _source, destination, _owner):
        Path(destination).write_bytes(b"verified backup")

    def fake_backups(self, tx, _support):
        self.calls.append("backups")
        tx.file_backups = [{"was_absent":True, "after_sha256":"after", "target":self.items[0]["target"],
                            "source_relative":"auto.py", "after_size":5, "source":self.items[0]["source"],
                            "baseline":None, "backup_path":None, "backup_sha256":None,
                            "before_metadata":None, "before_sha256":None}]

    def install(self, *_):
        self.calls.append("install")

    def run_apply(self, failure=None):
        config = object(); automatic_module = object()
        with mock.patch.object(deploy.os, "geteuid", return_value=0), \
             mock.patch.object(deploy, "make_plan", return_value=self.plan), \
             mock.patch.object(deploy, "_read_manifest", return_value=({},{})), \
             mock.patch.object(deploy, "_manifest_target_items", return_value=self.items), \
             mock.patch.object(deploy, "_unit_plan", return_value=(self.units,self.nginx)), \
             mock.patch.object(deploy, "_load_release_modules", return_value=(config,automatic_module)), \
             mock.patch.object(deploy, "_database_snapshot", return_value=self.snapshot), \
             mock.patch.object(deploy, "_create_file_backups", side_effect=self.fake_backups), \
             mock.patch.object(deploy, "_install_files", side_effect=self.install), \
             mock.patch.object(deploy, "_atomic_database_migration", side_effect=lambda *_: self.calls.append("migration")), \
             mock.patch.object(deploy, "_assert_schema_and_zero", side_effect=lambda *_: self.calls.append("schema_verified")), \
             mock.patch.object(deploy, "_verify_imports", side_effect=lambda *_: self.calls.append("imports")), \
             mock.patch.object(deploy, "_verify_restored_units", side_effect=lambda *_: self.calls.append("restored_verified")), \
             mock.patch.object(deploy, "_validate_nginx", side_effect=(failure if failure == "nginx" else lambda *_: self.calls.append("nginx_test"))), \
             mock.patch.object(deploy, "_reload_nginx", side_effect=lambda *_: self.calls.append("nginx_reload")), \
             mock.patch.object(deploy, "verify_live", side_effect=(failure if failure == "health" else lambda *_a,**_kw: {"ok":True})), \
             mock.patch.object(deploy, "_rollback", side_effect=lambda *_a,**_kw: self.calls.append("rollback")):
            return deploy.apply(commit="a"*40, plan=self.plan, support=self.support, systemd=self.systemd,
                                runtime_root=self.runtime_root, release=self.release, backup_root=self.root / "backup")

    def test_success_orders_quiescence_backup_migration_restore_nginx_verify(self):
        self.calls.clear()
        result = self.run_apply()
        self.assertEqual("installed", result["status"])
        self.assertLess(self.calls.index("stop"), self.calls.index("sidecars_clear"))
        self.assertLess(self.calls.index("sidecars_clear"), self.calls.index("backups"))
        self.assertLess(self.calls.index("backups"), self.calls.index("migration"))
        self.assertLess(self.calls.index("migration"), self.calls.index("restore"))
        self.assertLess(self.calls.index("nginx_test"), self.calls.index("nginx_reload"))

    def test_sidecar_quiescence_failure_restores_state_before_any_mutation(self):
        self.calls.clear()
        self.support._wait_for_no_sqlite_sidecars = mock.Mock(side_effect=deploy.DeploymentError("busy"))
        with self.assertRaisesRegex(deploy.DeploymentError, "rollback=complete"):
            self.run_apply()
        self.assertNotIn("backups", self.calls)
        self.assertNotIn("install", self.calls)
        self.assertNotIn("migration", self.calls)
        self.assertIn("restore", self.calls)

    def test_observation_racing_with_rollback_is_retained_after_quiescence(self):
        observations = {deploy.SCHEMA_TABLES[0]: 0, deploy.SCHEMA_TABLES[1]: 0}
        accepted = {deploy.SCHEMA_TABLES[0]: 1, deploy.SCHEMA_TABLES[1]: 1}
        self.support._systemd_show = lambda unit: self.units[unit]
        self.support._sidecar_state_snapshot = lambda _db: {}
        self.support._stop_for_migration = lambda *_: self.calls.append("stop")
        self.support._verify_migration_maintenance = lambda *_: self.calls.append("stopped_verified")
        self.support._wait_for_no_sqlite_sidecars = lambda *_a, **_kw: self.calls.append("quiescent")
        self.support._restore_units = lambda *_: self.calls.append("restore")
        tx = deploy.Transaction(directory=self.backup_dir, commit="a" * 40, items=self.items,
                                prior_units=self.units, prior_nginx=self.nginx,
                                database_before={"database_file_meta": {}}, database_backup=self.backup)
        records = []
        tx.write_record = lambda state, _support: records.append(state)
        with mock.patch.object(deploy, "_observation_counts", side_effect=[observations, accepted]), \
             mock.patch.object(deploy, "_verify_restored_units", side_effect=lambda *_: self.calls.append("verified")), \
             mock.patch.object(deploy, "_restore_target_files", side_effect=AssertionError("files must be retained")):
            with self.assertRaisesRegex(deploy.DeploymentError, "rollback refused: automatic observation data exists"):
                deploy._rollback(tx, self.systemd, self.support, self.release, object(), database=self.root / "db")
        self.assertEqual(["stop", "stopped_verified", "quiescent", "restore", "verified"], self.calls)
        self.assertEqual(["rollback_refused_observation_data"], records)

    def test_restoration_verifies_exact_active_services_and_timer_prestate(self):
        snapshot = {}
        current = {}
        for name in deploy.LONG_SERVICES:
            snapshot[name] = {"LoadState":"loaded", "ActiveState":"active"}
            current[name] = {"LoadState":"loaded", "ActiveState":"active", "SubState":"running", "MainPID":"17"}
        for name in deploy.WRITER_SERVICES:
            snapshot[name] = {"LoadState":"loaded", "ActiveState":"inactive"}
            current[name] = {"LoadState":"loaded", "ActiveState":"inactive", "SubState":"dead", "MainPID":"0"}
        for index, name in enumerate(deploy.TIMERS):
            active = index != 1
            snapshot[name] = {"LoadState":"loaded", "ActiveState":"active" if active else "inactive"}
            current[name] = {"LoadState":"loaded", "ActiveState":"active" if active else "inactive",
                             "SubState":"waiting" if active else "dead", "MainPID":"0"}
        systemd = SimpleNamespace(show=lambda unit: current[unit])
        deploy._verify_restored_units(systemd, snapshot, self.support)
        current[deploy.LONG_SERVICES[0]]["MainPID"] = "0"
        with self.assertRaisesRegex(deploy.DeploymentError, "captured unit state"):
            deploy._verify_restored_units(systemd, snapshot, self.support)

    def run_failure(self, phase):
        self.calls.clear()
        def fail_nginx(*_):
            self.calls.append("nginx_test")
            raise deploy.DeploymentError("fixture")
        def fail_restore(*_):
            self.calls.append("restore_failed")
            raise deploy.DeploymentError("fixture")
        def fail_health(*_a, **_kw):
            self.calls.append("health_failed")
            raise deploy.DeploymentError("fixture")
        restore = fail_restore if phase == "service" else lambda *_: self.calls.append("restore")
        nginx = fail_nginx if phase == "nginx" else lambda *_: self.calls.append("nginx_test")
        health = fail_health if phase == "health" else lambda *_a,**_kw: {"ok":True}
        with mock.patch.object(deploy.os, "geteuid", return_value=0), \
             mock.patch.object(deploy, "make_plan", return_value=self.plan), \
             mock.patch.object(deploy, "_read_manifest", return_value=({},{})), \
             mock.patch.object(deploy, "_manifest_target_items", return_value=self.items), \
             mock.patch.object(deploy, "_unit_plan", return_value=(self.units,self.nginx)), \
             mock.patch.object(deploy, "_load_release_modules", return_value=(object(),object())), \
             mock.patch.object(deploy, "_database_snapshot", return_value=self.snapshot), \
             mock.patch.object(deploy, "_create_file_backups", side_effect=self.fake_backups), \
             mock.patch.object(deploy, "_install_files", side_effect=self.install), \
             mock.patch.object(deploy, "_atomic_database_migration", side_effect=lambda *_: self.calls.append("migration")), \
             mock.patch.object(deploy, "_assert_schema_and_zero"), \
             mock.patch.object(deploy, "_verify_imports"), \
             mock.patch.object(self.support, "_restore_units", side_effect=restore), \
             mock.patch.object(deploy, "_verify_restored_units"), \
             mock.patch.object(deploy, "_validate_nginx", side_effect=nginx), \
             mock.patch.object(deploy, "_reload_nginx", side_effect=lambda *_: self.calls.append("reload")), \
             mock.patch.object(deploy, "verify_live", side_effect=health), \
             mock.patch.object(deploy, "_rollback", side_effect=lambda *_a,**_kw: self.calls.append("rollback")):
            with self.assertRaisesRegex(deploy.DeploymentError, "rollback=complete"):
                deploy.apply(commit="a"*40, plan=self.plan, support=self.support, systemd=self.systemd,
                             runtime_root=self.runtime_root, release=self.release, backup_root=self.root / "backup")

    def test_nginx_validation_service_restart_and_health_failures_route_to_rollback(self):
        for failure in ("nginx", "service", "health"):
            with self.subTest(failure=failure):
                self.run_failure(failure)
                self.assertIn("rollback", self.calls)

    def test_dry_run_plan_is_not_applied_when_already_current(self):
        self.plan["status"] = "already_installed"
        with mock.patch.object(deploy.os, "geteuid", return_value=0):
            result = deploy.apply(commit="a"*40, plan=self.plan, support=self.support, systemd=self.systemd)
        self.assertEqual("already_installed", result["status"])
        self.assertNotIn("stop", self.calls)


if __name__ == "__main__":
    unittest.main()
