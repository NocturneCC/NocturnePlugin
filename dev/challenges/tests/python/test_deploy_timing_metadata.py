from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock


HERE = Path(__file__).resolve()
TREE = HERE.parents[2]
spec = importlib.util.spec_from_file_location("deploy_timing_metadata", TREE / "deploy_timing_metadata.py")
deploy = importlib.util.module_from_spec(spec)
assert spec.loader
import sys
sys.modules[spec.name] = deploy
spec.loader.exec_module(deploy)
FIXTURE_COMMIT = "a" * 40


class SystemdStateParsingTests(unittest.TestCase):
    @staticmethod
    def output(unit: str, load: str, active: str, sub: str, pid: str | None = None) -> str:
        fields = [f"Id={unit}", f"LoadState={load}", f"ActiveState={active}", f"SubState={sub}"]
        if pid is not None:
            fields.append(f"MainPID={pid}")
        return "\n".join(fields) + "\n"

    def show(self, unit: str, output: str):
        proc = __import__("subprocess").CompletedProcess([], 0, output, "")
        with mock.patch.object(deploy, "_run", return_value=proc):
            return deploy._systemd_show(unit)

    def test_absent_timer_may_omit_mainpid(self):
        state = self.show("nocturne-challenge-shadow-sync.timer",
                          self.output("nocturne-challenge-shadow-sync.timer", "not-found", "inactive", "dead"))
        self.assertNotIn("MainPID", state)

    def test_installed_inactive_timer_may_omit_mainpid(self):
        state = self.show("nocturne-challenge-shadow-sync.timer",
                          self.output("nocturne-challenge-shadow-sync.timer", "loaded", "inactive", "dead"))
        self.assertEqual("inactive", state["ActiveState"])

    def test_installed_inactive_timer_accepts_zero_mainpid(self):
        state = self.show("nocturne-challenge-shadow-sync.timer",
                          self.output("nocturne-challenge-shadow-sync.timer", "loaded", "inactive", "dead", "0"))
        self.assertEqual("0", state["MainPID"])

    def test_timer_rejects_process_or_ambiguous_substate(self):
        for output in (
            self.output("nocturne-challenge-shadow-sync.timer", "loaded", "inactive", "dead", "12"),
            self.output("nocturne-challenge-shadow-sync.timer", "loaded", "active", "waiting", "bad"),
            self.output("nocturne-challenge-shadow-sync.timer", "loaded", "active", "running", "0"),
            self.output("nocturne-challenge-shadow-sync.timer", "loaded", "inactive", "waiting"),
            self.output("nocturne-challenge-shadow-sync.timer", "not-found", "active", "waiting"),
        ):
            with self.subTest(output=output), self.assertRaises(deploy.DeployError):
                self.show("nocturne-challenge-shadow-sync.timer", output)

    def test_service_still_requires_valid_mainpid(self):
        unit = "nocturne-challenge-intake.service"
        for pid in (None, "bad", "-1"):
            output = self.output(unit, "loaded", "inactive", "dead", pid)
            with self.subTest(pid=pid), self.assertRaises(deploy.DeployError):
                self.show(unit, output)
        state = self.show(unit, self.output(unit, "loaded", "inactive", "dead", "0"))
        self.assertEqual("0", state["MainPID"])


class GitCheckoutGateTests(unittest.TestCase):
    commit = "d" * 40
    other = "e" * 40

    def run_gate(self, outputs):
        remaining = iter(outputs)
        with mock.patch.object(deploy.subprocess, "run", side_effect=lambda *args, **kwargs: next(remaining)) as run:
            deploy.verify_git(deploy.REPO, self.commit)
        return run

    @staticmethod
    def result(stdout="", returncode=0):
        return __import__("subprocess").CompletedProcess([], returncode, stdout, "redacted stderr fixture")

    def test_valid_exact_state_uses_root_safe_directory_argv(self):
        run = self.run_gate([self.result("development\n"), self.result(""),
                             self.result(self.commit + "\n"), self.result(self.commit + "\n")])
        expected_args = [
            ["/usr/bin/git", "-C", str(deploy.REPO), "-c", f"safe.directory={deploy.REPO}", "branch", "--show-current"],
            ["/usr/bin/git", "-C", str(deploy.REPO), "-c", f"safe.directory={deploy.REPO}", "status", "--porcelain=v1", "--untracked-files=all"],
            ["/usr/bin/git", "-C", str(deploy.REPO), "-c", f"safe.directory={deploy.REPO}", "rev-parse", "HEAD"],
            ["/usr/bin/git", "-C", str(deploy.REPO), "-c", f"safe.directory={deploy.REPO}", "rev-parse", "origin/development"],
        ]
        self.assertEqual(expected_args, [call.args[0] for call in run.call_args_list])
        for call in run.call_args_list:
            self.assertEqual({"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}, call.kwargs["env"])

    def test_wrong_branch_is_classified(self):
        with mock.patch.object(deploy.subprocess, "run", return_value=self.result("feature\n")):
            with self.assertRaisesRegex(deploy.DeployError, "diagnostic_category=wrong_branch"):
                deploy.verify_git(deploy.REPO, self.commit)

    def test_dirty_porcelain_is_classified(self):
        with mock.patch.object(deploy.subprocess, "run", side_effect=[self.result("development\n"), self.result(" M file\n")]):
            with self.assertRaisesRegex(deploy.DeployError, "diagnostic_category=dirty_worktree"):
                deploy.verify_git(deploy.REPO, self.commit)

    def test_head_mismatch_is_classified(self):
        with mock.patch.object(deploy.subprocess, "run", side_effect=[self.result("development\n"), self.result(""),
                                                                         self.result(self.other + "\n")]):
            with self.assertRaisesRegex(deploy.DeployError, "diagnostic_category=head_mismatch"):
                deploy.verify_git(deploy.REPO, self.commit)

    def test_origin_mismatch_is_classified(self):
        with mock.patch.object(deploy.subprocess, "run", side_effect=[self.result("development\n"), self.result(""),
                                                                         self.result(self.commit + "\n"),
                                                                         self.result(self.other + "\n")]):
            with self.assertRaisesRegex(deploy.DeployError, "diagnostic_category=origin_mismatch"):
                deploy.verify_git(deploy.REPO, self.commit)

    def test_malformed_command_output_is_classified_without_echo(self):
        for outputs in (([self.result("development\nextra\n")]),
                        ([self.result("development\n"), self.result(""), self.result("not-a-sha\n")])):
            with self.subTest(outputs=outputs), mock.patch.object(deploy.subprocess, "run", side_effect=outputs):
                with self.assertRaisesRegex(deploy.DeployError, "diagnostic_category=malformed_output") as caught:
                    deploy.verify_git(deploy.REPO, self.commit)
                self.assertNotIn("not-a-sha", str(caught.exception))

    def test_git_launch_and_nonzero_failures_are_sanitized(self):
        for failure in (OSError("private stderr detail"), self.result(returncode=128)):
            with self.subTest(failure=type(failure).__name__), mock.patch.object(
                    deploy.subprocess, "run", side_effect=failure):
                with self.assertRaisesRegex(deploy.DeployError, "diagnostic_category=git_launch_failed") as caught:
                    deploy.verify_git(deploy.REPO, self.commit)
                self.assertNotIn("private stderr detail", str(caught.exception))


class PreparedCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.commit = "a" * 40
        self.release = self.root / "releases" / self.commit
        checker = self.release / "dev/intake/prepare_immutable_runtime.sh"
        checker.parent.mkdir(parents=True)
        checker.write_text("#!/bin/bash\nexit 0\n", encoding="utf-8")
        self.runtime_patch = mock.patch.object(deploy, "RUNTIME", self.root)
        self.runtime_patch.start()
        self.addCleanup(self.runtime_patch.stop)
        self.addCleanup(self.tmp.cleanup)

    @staticmethod
    def completed(stdout: str, stderr: str = "", status: int = 0):
        return __import__("subprocess").CompletedProcess([], status, stdout, stderr)

    def test_prepared_success_uses_exact_release_wrapper_interface(self):
        stdout = "status=prepared\ncheck_mode=read_only\ndiagnostic_end\n"
        with mock.patch.object(deploy.subprocess, "run", return_value=self.completed(stdout)) as run:
            self.assertEqual(stdout, deploy._prepared_check(self.release, self.commit, self.root))
        args, kwargs = run.call_args
        self.assertEqual(["/bin/bash", str(self.release / "dev/intake/prepare_immutable_runtime.sh"),
                          "--check", self.commit], args[0])
        self.assertEqual(90, kwargs["timeout"])
        self.assertEqual({
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C",
            "GIT_OPTIONAL_LOCKS": "0", "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": str(deploy.REPO),
        }, kwargs["env"])

    def test_unprepared_target_reports_exit_and_safe_category(self):
        stdout = "status=not_prepared\ncheck_mode=read_only\n"
        with mock.patch.object(deploy.subprocess, "run", return_value=self.completed(stdout, status=3)):
            with self.assertRaisesRegex(deploy.DeployError, "exit_status=3 diagnostic_category=not_prepared"):
                deploy._prepared_check(self.release, self.commit, self.root)

    def test_nonzero_checker_exit_fails_even_if_status_says_prepared(self):
        stdout = "status=prepared\ncheck_mode=read_only\n"
        with mock.patch.object(deploy.subprocess, "run", return_value=self.completed(stdout, status=1)):
            with self.assertRaisesRegex(deploy.DeployError, "exit_status=1 diagnostic_category=checker_nonzero_with_prepared_status"):
                deploy._prepared_check(self.release, self.commit, self.root)

    def test_malformed_output_is_rejected(self):
        for stdout, category in (
            ("status=prepared\n", "missing_check_mode"),
            ("status=prepared\ncheck_mode=other\n", "unexpected_check_mode"),
            ("status=prepared\ncheck_mode=read_only\nstatus=prepared\n", "duplicate_status"),
            ("check_mode=read_only\n", "missing_status"),
        ):
            with self.subTest(category=category), mock.patch.object(
                    deploy.subprocess, "run", return_value=self.completed(stdout)):
                with self.assertRaisesRegex(deploy.DeployError, f"diagnostic_category={category}"):
                    deploy._prepared_check(self.release, self.commit, self.root)

    def test_commit_and_runtime_root_must_match_bound_release(self):
        with mock.patch.object(deploy.subprocess, "run") as run:
            with self.assertRaisesRegex(deploy.DeployError, "release_identity_mismatch"):
                deploy._prepared_check(self.release, "b" * 40, self.root)
            with self.assertRaisesRegex(deploy.DeployError, "wrong_runtime_root"):
                deploy._prepared_check(self.release, self.commit, self.root / "other")
        run.assert_not_called()

    def test_stderr_is_not_copied_into_failure_diagnostics(self):
        marker = "private checker diagnostic fixture only"
        stdout = "status=unsafe_blocking\ncheck_mode=read_only\n"
        with mock.patch.object(deploy.subprocess, "run",
                               return_value=self.completed(stdout, stderr=marker, status=1)):
            with self.assertRaises(deploy.DeployError) as caught:
                deploy._prepared_check(self.release, self.commit, self.root)
        self.assertNotIn(marker, str(caught.exception))
        self.assertIn("diagnostic_category=unsafe_blocking", str(caught.exception))

class FakeConfig:
    @staticmethod
    def config_document(conn):
        columns = {row[1] for row in conn.execute("PRAGMA table_info(challenge_config_bosses)")}
        bosses = []
        for row in conn.execute("SELECT boss_key,metric_type FROM challenge_config_bosses ORDER BY boss_key"):
            boss = {"boss_key": row[0], "metric_type": row[1]}
            if row[1] == "time":
                scope = conn.execute("SELECT timing_scope FROM challenge_config_bosses WHERE boss_key=?", (row[0],)).fetchone()[0] if "timing_scope" in columns else None
                capture = conn.execute("SELECT automatic_capture FROM challenge_config_bosses WHERE boss_key=?", (row[0],)).fetchone()[0] if "automatic_capture" in columns else None
                boss.update(timing_scope=scope or "unconfigured", automatic_capture=capture or "manual_only")
            elif row[1] == "numeric":
                capture = conn.execute("SELECT automatic_capture FROM challenge_config_bosses WHERE boss_key=?", (row[0],)).fetchone()[0] if "automatic_capture" in columns else None
                boss["automatic_capture"] = capture or "manual_only"
            bosses.append(boss)
        version = conn.execute("SELECT config_version_id FROM challenge_config_versions WHERE status='active'").fetchone()
        return {"version_id": int(version[0]), "version_key": "v10", "status": "published",
                "published_at": "fixture", "bosses": bosses, "system_tiers": [], "leaderboard_modes": []}


class FakeSystemd:
    def __init__(self, fail_start_once: str | None = None):
        self.states = {}
        self.fail_start_once = fail_start_once
        for unit in deploy.CONTROLLED:
            timer = unit.endswith(".timer")
            self.states[unit] = {"Id": unit, "LoadState": "loaded", "ActiveState": "active",
                                 "SubState": "waiting" if timer else ("running" if unit in deploy.LONG_SERVICES else "exited"),
                                 "Result": "success", "MainPID": "0" if timer or unit in deploy.WRITER_SERVICES else "101"}

    def show(self, unit):
        return dict(self.states[unit])

    def stop(self, unit):
        self.states[unit].update(ActiveState="inactive", SubState="dead", MainPID="0")

    def start(self, unit):
        if unit == self.fail_start_once:
            self.fail_start_once = None
            raise deploy.DeployError("simulated service start failure")
        timer = unit.endswith(".timer")
        self.states[unit].update(ActiveState="active", SubState="waiting" if timer else ("running" if unit in deploy.LONG_SERVICES else "exited"),
                                 MainPID="0" if timer or unit in deploy.WRITER_SERVICES else "202")

    def wait_inactive(self, unit, timeout=20):
        state = self.states[unit]
        if state["ActiveState"] != "inactive":
            raise deploy.DeployError("not inactive")

    def wait_active(self, unit, timeout=30):
        if self.states[unit]["ActiveState"] != "active":
            raise deploy.DeployError("not active")

    def wait_job_idle(self, unit, timeout=40):
        state = self.states[unit]
        if state["ActiveState"] == "active" and state["SubState"] == "running":
            state.update(ActiveState="inactive", SubState="dead", MainPID="0")


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.release = self.root / "releases" / FIXTURE_COMMIT
        self.release.mkdir(parents=True)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.targets = []
        self.manifest = {"bundle_files": [], "live_sources": []}
        for index, (rel, _target) in enumerate(deploy.FILES):
            src = self.release / rel
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text(f"immutable-source-{index}\n", encoding="utf-8")
            target = self.root / "live" / str(index) / Path(rel).name
            target.parent.mkdir(parents=True)
            target.write_text(f"live-before-{index}\n", encoding="utf-8")
            target.chmod(0o664)
            source_hash = hashlib.sha256(src.read_bytes()).hexdigest()
            live_meta = deploy.capture_file(target)
            self.targets.append((rel, src, target, source_hash, live_meta))
            self.manifest["bundle_files"].append({"path": rel, "type": "regular", "sha256": source_hash,
                                                   "size": src.stat().st_size,
                                                   "source_relationship": "repository_owned_extension"})
            self.manifest["live_sources"].append({"path": str(target), "type": "regular",
                "sha256": live_meta["sha256"], "uid": live_meta["uid"], "gid": live_meta["gid"],
                "mode": live_meta["mode"], "size": live_meta["size"], "nlink": 1,
                "acl_sha256": live_meta["acl_sha256"]})
        self.sm_path = self.release / deploy.SOURCE_MANIFEST
        self.sm_path.parent.mkdir(parents=True, exist_ok=True)
        self.sm_path.write_text(json.dumps(self.manifest, sort_keys=True), encoding="utf-8")
        files = {rel: digest for rel, _src, _target, digest, _meta in self.targets}
        files[deploy.SOURCE_MANIFEST.as_posix()] = hashlib.sha256(self.sm_path.read_bytes()).hexdigest()
        self.release_manifest_path = self.release / "RELEASE-MANIFEST.json"
        self.release_manifest_path.write_text(json.dumps({"purpose": "nocturne-immutable-runtime-v1", "commit": FIXTURE_COMMIT, "files": files}, sort_keys=True), encoding="utf-8")
        self.db = self.root / "Challenges.db"
        conn = sqlite3.connect(self.db)
        conn.executescript("""
            PRAGMA journal_mode=DELETE;
            CREATE TABLE challenge_config_versions(config_version_id INTEGER,status TEXT);
            INSERT INTO challenge_config_versions VALUES(10,'active');
            CREATE TABLE challenge_config_bosses(config_version_id INTEGER,boss_key TEXT,metric_type TEXT);
            INSERT INTO challenge_config_bosses VALUES(10,'test_time','time'),(10,'test_numeric','numeric');
            CREATE TABLE challenge_submissions(id INTEGER);
            INSERT INTO challenge_submissions VALUES(1);
            CREATE TABLE challenge_config_drafts(draft_id INTEGER, draft_json TEXT);
            INSERT INTO challenge_config_drafts VALUES(3,'{"revision":7}');
            CREATE TABLE challenge_submission_participants(id INTEGER);
            INSERT INTO challenge_submission_participants VALUES(4);
            CREATE TABLE challenge_tier_awards(id INTEGER);
            INSERT INTO challenge_tier_awards VALUES(5);
            CREATE TABLE challenge_audit_log(id INTEGER);
            INSERT INTO challenge_audit_log VALUES(6);
            CREATE TABLE leaderboard_mode_versions(id INTEGER);
            INSERT INTO leaderboard_mode_versions VALUES(7);
            CREATE TABLE leaderboard_observations(id INTEGER);
            INSERT INTO leaderboard_observations VALUES(8);
        """)
        conn.commit(); conn.close()
        self.backup_root = self.root / "backups"
        self.systemd = FakeSystemd()
        self.patches = [
            mock.patch.object(deploy, "FILES", tuple((rel, target) for rel, _src, target, _hash, _meta in self.targets)),
            mock.patch.object(deploy, "verify_git", return_value=None),
            mock.patch.object(deploy, "_prepared_check", return_value="status=prepared\n"),
            mock.patch.object(deploy, "_load_config_module", return_value=FakeConfig),
            mock.patch.object(deploy, "_http_json", side_effect=self.fake_http),
        ]
        for p in self.patches: p.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for p in reversed(self.patches): p.stop()
        self.tmp.cleanup()

    @staticmethod
    def fake_http(url, **kwargs):
        if url.endswith("/health"):
            return 200, {"ok": True}
        if url.endswith("/api/challenges/config/active"):
            return 200, {"ok": True, "version_id": 10, "version_key": "v10", "status": "published",
                "published_at": "fixture", "system_tiers": [], "leaderboard_modes": [], "bosses": [
                {"boss_key": "test_numeric", "metric_type": "numeric", "automatic_capture": "manual_only"},
                {"boss_key": "test_time", "metric_type": "time", "timing_scope": "unconfigured", "automatic_capture": "manual_only"}]}
        if url.endswith("/api/leaderboards/modes"):
            return 200, {"ok": True, "modes": []}
        return 401, None

    def make_plan(self):
        return deploy.make_plan(commit=FIXTURE_COMMIT, repo=self.repo, runtime_root=self.root,
                                release=self.release, database=self.db, systemd=self.systemd)

    def apply(self, failpoint=None, systemd=None):
        plan = self.make_plan()
        return deploy.apply_install(plan, commit=FIXTURE_COMMIT, repo=self.repo, runtime_root=self.root,
            database=self.db, backup_root=self.backup_root, release=self.release,
            systemd=systemd or self.systemd, failpoint=failpoint,
            _testing_owner_uid=os.geteuid())

    def test_dry_run_is_read_only(self):
        before_files = [deploy._sha_file(item[2]) for item in self.targets]
        db_before = deploy._sha_file(self.db)
        plan = self.make_plan()
        self.assertEqual("dry_run", plan["status"])
        self.assertEqual(10, plan["active_version_id"])
        self.assertFalse(self.backup_root.exists())
        self.assertEqual(before_files, [deploy._sha_file(item[2]) for item in self.targets])
        self.assertEqual(db_before, deploy._sha_file(self.db))

    def test_clean_install_is_additive_and_version_10_is_preserved(self):
        conn = sqlite3.connect(self.db)
        before_counts = deploy._table_counts(conn)
        conn.close()
        result = self.apply()
        self.assertEqual("installed", result["status"])
        self.assertEqual(10, result["active_version_id"])
        self.assertFalse(result["configuration_version_published"])
        conn = sqlite3.connect(self.db)
        columns = {r[1]: r for r in conn.execute("PRAGMA table_info(challenge_config_bosses)")}
        for name in deploy.NEW_COLUMNS:
            self.assertEqual("TEXT", columns[name][2].upper())
            self.assertEqual(0, columns[name][3])
            self.assertIsNone(columns[name][4])
        self.assertEqual(10, conn.execute("SELECT config_version_id FROM challenge_config_versions WHERE status='active'").fetchone()[0])
        self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM challenge_submissions").fetchone()[0])
        self.assertEqual('{"revision":7}', conn.execute("SELECT draft_json FROM challenge_config_drafts WHERE draft_id=3").fetchone()[0])
        self.assertEqual(before_counts, deploy._table_counts(conn))
        conn.close()
        for _rel, _source, target, expected, _meta in self.targets:
            self.assertEqual(expected, deploy._sha_file(target))

    def test_repeat_install_is_idempotent(self):
        self.apply()
        plan = self.make_plan()
        self.assertEqual("already_current", plan["status"])
        result = deploy.apply_install(plan, commit=FIXTURE_COMMIT, repo=self.repo, runtime_root=self.root,
            database=self.db, backup_root=self.backup_root, release=self.release,
            systemd=self.systemd, _testing_owner_uid=os.geteuid())
        self.assertEqual("already_current", result["status"])

    def test_live_file_drift_is_rejected_before_mutation(self):
        self.targets[0][2].write_text("operator drift\n", encoding="utf-8")
        with self.assertRaises(deploy.DeployError): self.make_plan()
        self.assertFalse(self.backup_root.exists())

    def test_database_backup_is_verified_and_retained(self):
        initial = sqlite3.connect(self.db)
        before_counts = deploy._table_counts(initial)
        initial.close()
        result = self.apply()
        record = json.loads(Path(result["backup_record"]).read_text())
        self.assertEqual("committed", record["state"])
        backup = Path(result["backup_record"]).parent / "Challenges.db.sqlite-backup"
        conn = sqlite3.connect(backup)
        self.assertEqual("ok", conn.execute("PRAGMA integrity_check").fetchone()[0])
        self.assertEqual(10, conn.execute("SELECT config_version_id FROM challenge_config_versions WHERE status='active'").fetchone()[0])
        self.assertFalse(set(deploy.NEW_COLUMNS).issubset({r[1] for r in conn.execute("PRAGMA table_info(challenge_config_bosses)")}))
        self.assertEqual(before_counts, deploy._table_counts(conn))
        conn.close()

    def test_partial_failure_rolls_back_files_database_and_service_state(self):
        before_files = [deploy._sha_file(item[2]) for item in self.targets]
        before_db = deploy._sha_file(self.db)
        def fail(phase):
            if phase == "file:challenge-admin.html": raise RuntimeError("fixture failure")
        with self.assertRaises(deploy.DeployError): self.apply(failpoint=fail)
        self.assertEqual(before_files, [deploy._sha_file(item[2]) for item in self.targets])
        self.assertEqual(before_db, deploy._sha_file(self.db))
        self.assertTrue(all(self.systemd.states[u]["ActiveState"] == "active" for u in deploy.LONG_SERVICES))
        self.assertTrue(all(self.systemd.states[u]["ActiveState"] == "active" for u in deploy.TIMERS))

    def test_service_restart_failure_rolls_back_all_artifacts(self):
        before_files = [deploy._sha_file(item[2]) for item in self.targets]
        before_db = deploy._sha_file(self.db)
        failing = FakeSystemd(fail_start_once="osrs-drops-api.service")
        with self.assertRaises(deploy.DeployError): self.apply(systemd=failing)
        self.assertEqual(before_files, [deploy._sha_file(item[2]) for item in self.targets])
        conn = sqlite3.connect(self.db)
        self.assertEqual("ok", conn.execute("PRAGMA integrity_check").fetchone()[0])
        self.assertEqual(10, conn.execute("SELECT config_version_id FROM challenge_config_versions WHERE status='active'").fetchone()[0])
        self.assertFalse(set(deploy.NEW_COLUMNS).issubset({r[1] for r in conn.execute("PRAGMA table_info(challenge_config_bosses)")}))
        self.assertEqual(1, conn.execute("SELECT COUNT(*) FROM challenge_submissions").fetchone()[0])
        self.assertEqual('{"revision":7}', conn.execute("SELECT draft_json FROM challenge_config_drafts WHERE draft_id=3").fetchone()[0])
        conn.close()
        self.assertTrue(all(failing.states[u]["ActiveState"] == "active" for u in deploy.LONG_SERVICES), failing.states)

    def test_website_asset_failure_rolls_back(self):
        before = [deploy._sha_file(item[2]) for item in self.targets]
        def fail(phase):
            if phase == "file:challenge-admin-state.js": raise RuntimeError("website fixture")
        with self.assertRaises(deploy.DeployError): self.apply(failpoint=fail)
        self.assertEqual(before, [deploy._sha_file(item[2]) for item in self.targets])


if __name__ == "__main__":
    unittest.main()
