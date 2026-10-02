from __future__ import annotations

import hashlib
import contextlib
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

    def test_wait_inactive_accepts_timer_without_mainpid_but_not_service_without_it(self):
        class TimerOnlySystemd(deploy.Systemd):
            def __init__(self, state):
                self.state = state

            def show(self, _unit):
                return dict(self.state)

        timer = TimerOnlySystemd({"Id": "challenge.timer", "LoadState": "loaded",
                                  "ActiveState": "inactive", "SubState": "dead"})
        timer.wait_inactive("challenge.timer", timeout=0.02)
        service = TimerOnlySystemd({"Id": "challenge.service", "LoadState": "loaded",
                                    "ActiveState": "inactive", "SubState": "dead"})
        with self.assertRaisesRegex(deploy.DeployError, "MainPID is missing"):
            service.wait_inactive("challenge.service", timeout=0.02)

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


class ReadOnlyDatabaseProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "Challenges.db"

    def create_db(self, journal="WAL"):
        conn = sqlite3.connect(self.db)
        conn.execute(f"PRAGMA journal_mode={journal}")
        conn.execute("CREATE TABLE sample(value TEXT)")
        conn.commit()
        conn.close()
        self.db.chmod(0o664)

    def profile(self, *, expected="wal", fake_rows=None, fake_error=None):
        if fake_rows is None and fake_error is None:
            return deploy._inspect_sqlite_profile(self.db, expected_journal_mode=expected)

        class Cursor:
            def __init__(self, rows=None, error=None):
                self.rows, self.error = rows, error

            def fetchall(self):
                if self.error:
                    raise self.error
                return self.rows

        class Connection:
            def execute(_self, sql):
                if "integrity_check" in sql:
                    return Cursor(fake_rows if fake_rows is not None else [("ok",)], fake_error)
                if "journal_mode" in sql:
                    return Cursor([("wal",)])
                return Cursor([("normal",)])

            def close(_self):
                pass

        with mock.patch.object(deploy.sqlite3, "connect", return_value=Connection()):
            return deploy._inspect_sqlite_profile(self.db, expected_journal_mode=expected)

    def test_wal_aware_read_only_sees_committed_wal_that_immutable_misses(self):
        setup = sqlite3.connect(self.db)
        setup.execute("CREATE TABLE sample(value TEXT)")
        setup.commit()
        setup.close()
        writer = sqlite3.connect(self.db)
        self.addCleanup(writer.close)
        self.assertEqual("wal", writer.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower())
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO sample VALUES('committed-in-wal')")
        writer.commit()
        self.db.chmod(0o664)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(self.db) + suffix)
            self.assertTrue(sidecar.is_file())
            sidecar.chmod(0o664)

        def snapshot():
            paths = [self.db, Path(str(self.db) + "-wal"), Path(str(self.db) + "-shm")]
            return {str(path): (hashlib.sha256(path.read_bytes()).hexdigest(),
                                path.stat().st_ino, path.stat().st_size, stat.S_IMODE(path.stat().st_mode),
                                path.stat().st_uid, path.stat().st_gid, path.stat().st_nlink)
                    for path in paths}

        before = snapshot()
        profile = deploy._inspect_sqlite_profile(self.db, expected_journal_mode="wal")
        self.assertEqual({"integrity": "ok", "journal_mode": "wal", "locking_mode": "normal"}, profile)
        with deploy._readonly_database_snapshot(self.db) as ro:
            self.assertEqual(1, ro.execute("SELECT COUNT(*) FROM sample").fetchone()[0])
        immutable = sqlite3.connect(self.db.as_uri() + "?mode=ro&immutable=1", uri=True)
        try:
            self.assertEqual(0, immutable.execute("SELECT COUNT(*) FROM sample").fetchone()[0])
        finally:
            immutable.close()
        self.assertEqual(before, snapshot(), "read-only dry-run inspection changed DB or sidecar state")

    def test_healthy_wal_normal_integrity_profile_passes(self):
        self.create_db()
        self.assertEqual({"integrity": "ok", "journal_mode": "wal", "locking_mode": "normal"},
                         deploy._inspect_sqlite_profile(self.db, expected_journal_mode="wal"))

    def test_missing_database_has_distinct_category(self):
        with self.assertRaisesRegex(deploy.DeployError, "diagnostic_category=database_missing"):
            deploy._inspect_sqlite_profile(self.db, expected_journal_mode="wal")

    def test_unsafe_nodes_and_sidecars_are_rejected(self):
        self.create_db(journal="DELETE")
        link = self.root / "alias.db"
        link.symlink_to(self.db)
        with self.assertRaisesRegex(deploy.DeployError, "diagnostic_category=unsafe_metadata"):
            deploy._inspect_sqlite_profile(link)
        extra = self.root / "hardlink.db"
        os.link(self.db, extra)
        with self.assertRaisesRegex(deploy.DeployError, "diagnostic_category=unsafe_metadata"):
            deploy._inspect_sqlite_profile(self.db)
        extra.unlink()

        self.db.chmod(0o600)
        with mock.patch.object(deploy, "DB", self.db):
            with self.assertRaisesRegex(deploy.DeployError, "diagnostic_category=unsafe_metadata"):
                deploy._inspect_sqlite_profile(self.db)

    def test_present_sidecar_must_match_main_owner_group_and_mode(self):
        self.create_db()
        wal = Path(str(self.db) + "-wal")
        wal.write_bytes(b"fixture")
        wal.chmod(0o600)
        with self.assertRaisesRegex(deploy.DeployError, "diagnostic_category=unsafe_metadata"):
            deploy._database_metadata(self.db)

    def test_integrity_journal_locking_and_malformed_profiles_fail_closed(self):
        self.create_db()
        def assert_profile_failure(category, integrity=None, journal="wal", locking="normal"):
            if integrity is None:
                integrity = [("ok",)]
            class Cursor:
                def __init__(self, rows): self.rows = rows
                def fetchall(self): return self.rows
            class Connection:
                def execute(_self, sql):
                    if "integrity_check" in sql: return Cursor(integrity)
                    if "journal_mode" in sql: return Cursor([(journal,)])
                    return Cursor([(locking,)])
                def close(_self): pass
            with mock.patch.object(deploy.sqlite3, "connect", return_value=Connection()):
                with self.assertRaisesRegex(deploy.DeployError, f"diagnostic_category={category}"):
                    deploy._inspect_sqlite_profile(self.db, expected_journal_mode="wal")

        assert_profile_failure("integrity_failed", integrity=[("corrupt",)])
        assert_profile_failure("malformed_result", integrity=[("ok",), ("extra",)])
        assert_profile_failure("unsupported_journal_mode", journal="delete")
        assert_profile_failure("unsupported_locking_mode", locking="exclusive")
        assert_profile_failure("malformed_result", integrity=[])

    def test_busy_error_is_bounded_and_sanitized(self):
        self.create_db()
        observed = {}
        class Connection:
            def execute(self, _sql): raise sqlite3.OperationalError("database is locked: private detail")
            def close(self): pass
        def connect(db_uri, **kwargs):
            observed.update(uri=db_uri, kwargs=kwargs)
            return Connection()
        with mock.patch.object(deploy.sqlite3, "connect", side_effect=connect):
            with self.assertRaisesRegex(deploy.DeployError, "diagnostic_category=database_busy") as caught:
                deploy._inspect_sqlite_profile(self.db, expected_journal_mode="wal")
        self.assertNotIn("private detail", str(caught.exception))
        self.assertTrue(observed["uri"].startswith("file:") and observed["uri"].endswith("?mode=ro"))
        self.assertNotIn("immutable=1", observed["uri"])
        self.assertEqual(10, observed["kwargs"]["timeout"])


class DatabaseMetadataSafetyTests(unittest.TestCase):
    SIDECAR_ACL_EXPLICIT = (
        "user::rw-\n"
        "user:1003:rw-\n"
        "group::rw-\n"
        "mask::rw-\n"
        "other::r--\n"
    )
    SIDECAR_ACL_INHERITED = (
        "user::rw-\n"
        "user:1003:rwx\t#effective:rw-\n"
        "group::rwx\t#effective:rw-\n"
        "mask::rw-\n"
        "other::r--\n"
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "Challenges.db"
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE sample(value TEXT)")
        conn.commit()
        conn.close()
        self.db.chmod(0o664)

    def capture_as_production_db(self, acl_text):
        def lstat(path):
            current = path.lstat()
            return os.stat_result((current.st_mode, current.st_ino, current.st_dev,
                                   current.st_nlink, 1001, 33, current.st_size,
                                   current.st_atime, current.st_mtime, current.st_ctime))

        with (mock.patch.object(deploy, "DB", self.db),
              mock.patch.object(deploy, "_db_ancestry_snapshot", return_value=()),
              mock.patch.object(deploy, "_db_lstat", side_effect=lstat),
              mock.patch.object(deploy, "_acl_text", side_effect=acl_text),
              mock.patch.object(deploy, "_safe_file_digest", return_value="a" * 64)):
            return deploy._database_metadata(self.db)

    def test_optional_sidecars_safely_absent(self):
        captured = deploy._database_metadata(self.db)
        self.assertEqual({"main"}, set(captured))

    def test_wal_and_shm_present_are_validated_and_fingerprinted(self):
        writer = sqlite3.connect(self.db)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("INSERT INTO sample VALUES('fixture')")
        writer.commit()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(self.db) + suffix)
            self.assertTrue(sidecar.is_file())
            sidecar.chmod(0o664)
        captured = deploy._database_metadata(self.db)
        self.assertTrue({"main", "-wal", "-shm"}.issubset(captured))
        self.assertTrue(all(len(captured[label]) == 10 for label in ("main", "-wal", "-shm")))

    def test_sidecar_disappearance_during_capture_is_busy(self):
        wal = Path(str(self.db) + "-wal")
        wal.write_bytes(b"stable fixture")
        wal.chmod(0o664)
        original_digest = deploy._safe_file_digest

        def digest(path, identity):
            result = original_digest(path, identity)
            if path == wal:
                wal.unlink()
            return result

        with mock.patch.object(deploy, "_safe_file_digest", side_effect=digest):
            with self.assertRaisesRegex(deploy.DeployError, "diagnostic_category=database_busy node=wal"):
                deploy._database_metadata(self.db)

    def test_sidecar_change_during_capture_is_busy(self):
        wal = Path(str(self.db) + "-wal")
        wal.write_bytes(b"before")
        wal.chmod(0o664)
        original_digest = deploy._safe_file_digest

        def digest(path, identity):
            result = original_digest(path, identity)
            if path == wal:
                wal.write_bytes(b"changed-during-capture")
            return result

        with mock.patch.object(deploy, "_safe_file_digest", side_effect=digest):
            with self.assertRaisesRegex(deploy.DeployError, "diagnostic_category=database_busy node=wal"):
                deploy._database_metadata(self.db)

    def test_sidecar_appearance_during_capture_is_busy(self):
        wal = Path(str(self.db) + "-wal")
        original_present = deploy._sidecar_present

        def appears(path):
            if path == wal:
                wal.write_bytes(b"appeared")
                wal.chmod(0o664)
                return True
            return original_present(path)

        with mock.patch.object(deploy, "_sidecar_present", side_effect=appears):
            with self.assertRaisesRegex(deploy.DeployError, "diagnostic_category=database_busy node=wal"):
                deploy._database_metadata(self.db)

    def test_established_numeric_file_acl_is_accepted_and_fingerprinted(self):
        expected = {
            "user::": "rw-", "user:1003:": "rw-", "group::": "rw-",
            "mask::": "rw-", "other::": "r--",
        }
        text = "user::rw-\nuser:1003:rw-\ngroup::rw-\nmask::rw-\nother::r--\n"
        self.assertEqual(deploy._acl_hash(text), deploy._validate_acl_profile(text, expected))
        with self.assertRaises(ValueError):
            deploy._validate_acl_profile(text + "default:user::rw-\n", expected)
        with self.assertRaises(ValueError):
            deploy._validate_acl_profile(text.replace("user:1003:rw-", "user:1002:rwx"), expected)

    def test_literal_database_wal_shm_acl_profiles_use_effective_permissions(self):
        for acl in (self.SIDECAR_ACL_EXPLICIT, self.SIDECAR_ACL_INHERITED):
            with self.subTest(acl=acl):
                digest = deploy._validate_db_sidecar_acl(acl)
                self.assertEqual(hashlib.sha256(acl.encode("utf-8")).hexdigest(), digest)
        parsed = deploy._acl_entries(self.SIDECAR_ACL_INHERITED)
        self.assertEqual("rwx", parsed["user:1003:"])
        self.assertEqual("rwx", parsed["group::"])

    def test_sidecar_acl_rejects_effective_execute_and_unknown_principals(self):
        effective_execute = self.SIDECAR_ACL_INHERITED.replace(
            "mask::rw-", "mask::rwx").replace(
            "#effective:rw-", "#effective:rwx")
        unknown_user = self.SIDECAR_ACL_INHERITED.replace("user:1003:rwx", "user:1002:rwx")
        for label, acl in (("effective execute", effective_execute), ("unknown user", unknown_user)):
            with self.subTest(label=label), self.assertRaises(ValueError):
                deploy._validate_db_sidecar_acl(acl)

    def test_zero_length_wal_and_shm_accept_literal_inherited_acl_profile(self):
        wal = Path(str(self.db) + "-wal")
        shm = Path(str(self.db) + "-shm")
        wal.touch()
        wal.chmod(0o664)
        shm.write_bytes(bytes(32768))
        shm.chmod(0o664)
        main_acl = "user::rw-\nuser:1003:rw-\ngroup::rw-\nmask::rw-\nother::r--\n"

        def acl_text(path):
            return main_acl if path == self.db else self.SIDECAR_ACL_INHERITED

        captured = self.capture_as_production_db(acl_text)
        self.assertTrue({"main", "-wal", "-shm"}.issubset(captured))
        self.assertEqual(0, captured["-wal"][6])
        self.assertEqual(32768, captured["-shm"][6])
        expected_fingerprint = deploy._raw_numeric_acl_hash(self.SIDECAR_ACL_INHERITED)
        self.assertEqual(expected_fingerprint, captured["-wal"][8])
        self.assertEqual(expected_fingerprint, captured["-shm"][8])

    def test_sidecar_raw_acl_fingerprint_detects_profile_change_during_capture(self):
        wal = Path(str(self.db) + "-wal")
        wal.write_bytes(b"")
        wal.chmod(0o664)
        calls = 0
        main_acl = "user::rw-\nuser:1003:rw-\ngroup::rw-\nmask::rw-\nother::r--\n"

        def acl_text(path):
            nonlocal calls
            if path == self.db:
                return main_acl
            if path == wal:
                calls += 1
                return self.SIDECAR_ACL_INHERITED if calls == 1 else self.SIDECAR_ACL_EXPLICIT
            raise FileNotFoundError

        with self.assertRaisesRegex(deploy.DeployError, "diagnostic_category=unsafe_metadata node=wal"):
            self.capture_as_production_db(acl_text)

    def test_numeric_getfacl_digest_matches_initial_adopted_named_manifest_fingerprint(self):
        numeric = (
            "user::rw-\nuser:1003:rwx\t#effective:rw-\n"
            "group::rwx\t#effective:rw-\nmask::rw-\nother::r--\n")
        named = numeric.replace("user:1003:", "user:glob:")
        expected = "1ee74ae96020306a844213e0c3edb809eb71156ea17acfb2629c70b1ad5c6e89"
        self.assertEqual(expected, deploy._acl_hash(numeric))
        self.assertEqual(expected, deploy._acl_hash(named))
        # Name-resolved identities remain invalid input to the strict parser.
        with self.assertRaises(ValueError):
            deploy._acl_entries(named)

    def test_authoritative_live_file_metadata_passes_the_static_predicates(self):
        """The supplied settled live profile itself is not an unsafe node."""
        canonical = deploy.DB
        fake = os.stat_result((stat.S_IFREG | 0o664, 42, 9, 1, 1001, 33, 8175616, 0, 0, 0))
        acl = "user::rw-\nuser:1003:rw-\ngroup::rw-\nmask::rw-\nother::r--\n"
        def lstat(path):
            if path == canonical:
                return fake
            raise FileNotFoundError
        with (mock.patch.object(deploy, "_db_ancestry_snapshot", return_value=()),
              mock.patch.object(deploy, "_db_lstat", side_effect=lstat),
              mock.patch.object(deploy, "_acl_text", return_value=acl),
              mock.patch.object(deploy, "_safe_file_digest", return_value="a" * 64),
              mock.patch.object(deploy, "_sidecar_present", return_value=False)):
            metadata = deploy._database_metadata(canonical)
        self.assertEqual({"main"}, set(metadata))
        self.assertEqual(1001, metadata["main"][2])
        self.assertEqual(33, metadata["main"][3])
        self.assertEqual(0o664, metadata["main"][4])

    @staticmethod
    def ancestry_acl(access_users, default_users, group_acl):
        access = ["user::rwx", *(f"user:{uid}:rwx" for uid in sorted(access_users)),
                  f"group::{group_acl}", "mask::rwx", "other::r-x"]
        default_base = ["user::rwx", *(f"user:{uid}:rwx" for uid in sorted(default_users)),
                        f"group::{group_acl}", "mask::rwx", "other::r-x"]
        default = [f"default:{line}" for line in default_base]
        return "\n".join(access + default) + "\n"

    def ancestry_fixture(self, *, unsafe_projects=False, mutate_acl=False):
        nodes = list(deploy._DB_ANCESTRY)
        acl_by_node = {
            Path("/srv"): "user::rwx\ngroup::r-x\nother::r-x\n",
            Path("/srv/projects"): self.ancestry_acl({1003}, {1003}, "r-x"),
            Path("/srv/projects/database"): self.ancestry_acl({1000, 1003}, {1003}, "rwx"),
        }
        stats = {}
        for index, (node, (uid, gid, mode, _access_users, _default_users, _group_acl)) in enumerate(deploy._DB_ANCESTRY.items(), 1):
            if unsafe_projects and node == Path("/srv/projects"):
                mode = 0o0777
            stats[node] = os.stat_result((stat.S_IFDIR | mode, index, 1, 3, uid, gid, 4096, 0, 0, 0))
        lstat_calls = {}

        def lstat(path):
            return stats[path]

        def acl(path):
            if mutate_acl and path == nodes[0]:
                lstat_calls[path] = lstat_calls.get(path, 0) + 1
                return acl_by_node[path] if lstat_calls[path] == 1 else "user::rwx\ngroup::rwx\nother::r-x\n"
            return acl_by_node[path]

        return mock.patch.object(deploy, "_db_lstat", side_effect=lstat), mock.patch.object(deploy, "_acl_text", side_effect=acl)

    def test_approved_setgid_ancestry_and_named_default_acls_pass(self):
        lstat_patch, acl_patch = self.ancestry_fixture()
        with lstat_patch, acl_patch:
            result = deploy._db_ancestry_snapshot(deploy.DB)
        self.assertEqual(3, len(result))
        self.assertTrue(all(len(item[-1]) == 64 for item in result))

    def test_literal_getfacl_numeric_profiles_parse_and_match_authoritative_ancestry(self):
        literal = {
            Path("/srv"): "user::rwx\ngroup::r-x\nother::r-x\n",
            Path("/srv/projects"): (
                "user::rwx\nuser:1003:rwx\ngroup::r-x\nmask::rwx\nother::r-x\n"
                "default:user::rwx\ndefault:user:1003:rwx\ndefault:group::r-x\n"
                "default:mask::rwx\ndefault:other::r-x\n"),
            Path("/srv/projects/database"): (
                "user::rwx\nuser:1000:rwx\nuser:1003:rwx\ngroup::rwx\nmask::rwx\nother::r-x\n"
                "default:user::rwx\ndefault:user:1003:rwx\ndefault:group::rwx\n"
                "default:mask::rwx\ndefault:other::r-x\n"),
        }
        lstat_patch, _ = self.ancestry_fixture()
        with lstat_patch, mock.patch.object(deploy, "_acl_text", side_effect=literal.__getitem__):
            self.assertEqual(3, len(deploy._db_ancestry_snapshot(deploy.DB)))
        parsed = deploy._acl_entries(literal[Path("/srv/projects/database")])
        self.assertEqual("rwx", parsed["user:1000:"])
        self.assertEqual("rwx", parsed["default:user:1003:"])
        self.assertEqual("rwx", parsed["default:user::"])

    def test_acl_collection_uses_absolute_numeric_getfacl_and_path_terminator(self):
        path = Path("/srv/projects/database")
        proc = __import__("subprocess").CompletedProcess(
            ["/usr/bin/getfacl"], 0, "user::rwx\ngroup::rwx\nother::r-x\n", "")
        with mock.patch.object(deploy, "_run", return_value=proc) as run:
            self.assertEqual(proc.stdout, deploy._acl_text(path))
        run.assert_called_once_with(
            ["/usr/bin/getfacl", "-cpn", "--", "/srv/projects/database"], timeout=15)

    def test_acl_text_collection_fails_closed_on_command_error_or_oversized_output(self):
        path = Path("/srv/projects")
        for proc in (
                __import__("subprocess").CompletedProcess([], 1, "", "getfacl failed"),
                __import__("subprocess").CompletedProcess(
                    [], 0, "x" * (deploy.MAX_ACL_OUTPUT_BYTES + 1), ""),
                __import__("subprocess").CompletedProcess(
                    [], 0, "", "x" * (deploy.MAX_ACL_OUTPUT_BYTES + 1))):
            with self.subTest(returncode=proc.returncode, outlen=len(proc.stdout), errlen=len(proc.stderr)), \
                    mock.patch.object(deploy, "_run", return_value=proc):
                with self.assertRaisesRegex(deploy.DeployError, "ACL inspection failed safely"):
                    deploy._acl_text(path)

    def test_acl_parser_accepts_numeric_and_rejects_name_resolved_principals(self):
        numeric = "user::rwx\nuser:1003:rwx\ngroup::r-x\nmask::rwx\nother::r-x\n"
        self.assertEqual("rwx", deploy._acl_entries(numeric)["user:1003:"])
        for name in ("glob", "randal"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                deploy._acl_entries(
                    "user::rwx\n" + f"user:{name}:rwx\n" +
                    "group::r-x\nmask::rwx\nother::r-x\n")

    def test_authoritative_ancestry_acl_rejections_are_fail_closed(self):
        projects = Path("/srv/projects")
        database = Path("/srv/projects/database")
        good = {
            Path("/srv"): "user::rwx\ngroup::r-x\nother::r-x\n",
            projects: self.ancestry_acl({1003}, {1003}, "r-x"),
            database: self.ancestry_acl({1000, 1003}, {1003}, "rwx"),
        }
        invalid_profiles = {
            "unknown UID": good[projects].replace("user:1003:rwx", "user:9999:rwx"),
            "duplicate entry": good[projects] + "user:1003:rwx\n",
            "malformed default": good[projects].replace("default:user:1003:rwx", "default:user:1003"),
            "excess permission": good[projects].replace("default:group::r-x", "default:group::rwx"),
            "missing required named access": good[projects].replace("user:1003:rwx\n", ""),
        }
        for label, bad_projects_acl in invalid_profiles.items():
            acl_values = dict(good)
            acl_values[projects] = bad_projects_acl
            lstat_patch, _ = self.ancestry_fixture()
            with self.subTest(label=label), lstat_patch, mock.patch.object(
                    deploy, "_acl_text", side_effect=acl_values.__getitem__):
                with self.assertRaisesRegex(
                        deploy.DeployError,
                        "diagnostic_category=unsafe_metadata node=ancestry"):
                    deploy._db_ancestry_snapshot(deploy.DB)

    def test_initially_adopted_challenge_config_hash_is_accepted_prestate(self):
        manifest = json.loads((deploy.REPO / "dev/challenges/source-manifest.json").read_text())
        before = next(item for item in manifest["live_sources"]
                      if item["path"] == "/srv/projects/nocturne-services/challenge_config.py")
        self.assertEqual(
            "81421a75ca6cccc1a08011e8a9f02fa43833d9534ad2f58ab9f3181c3de5f0e5",
            before["sha256"])
        target_record = next(item for item in manifest["bundle_files"]
                             if item["path"] == "dev/challenges/service/challenge_config.py")
        actual = {"sha256": before["sha256"], "uid": before["uid"], "gid": before["gid"],
                  "mode": before["mode"], "size": before["size"], "nlink": before["nlink"],
                  "acl_sha256": before["acl_sha256"]}
        item = {"target": Path(before["path"]), "before": before,
                "after_sha256": target_record["sha256"], "after_size": target_record["size"]}
        with mock.patch.object(deploy, "capture_file", return_value=actual):
            self.assertEqual("before", deploy._validate_file_prestate([item]))
        with mock.patch.object(deploy, "capture_file", return_value={
                **actual, "sha256": "0" * 64, "size": target_record["size"]}):
            with self.assertRaisesRegex(deploy.DeployError, "live target content drift"):
                deploy._validate_file_prestate([item])

    def test_unexpected_writable_ancestry_and_acl_mutation_fail_closed(self):
        for kwargs in ({"unsafe_projects": True}, {"mutate_acl": True}):
            lstat_patch, acl_patch = self.ancestry_fixture(**kwargs)
            with self.subTest(kwargs=kwargs), lstat_patch, acl_patch:
                with self.assertRaisesRegex(deploy.DeployError, "diagnostic_category=unsafe_metadata node=ancestry"):
                    deploy._db_ancestry_snapshot(deploy.DB)

    def test_metadata_capture_does_not_change_fixture_hash_or_metadata(self):
        def fingerprint():
            paths = [self.db, *(Path(str(self.db) + suffix) for suffix in ("-wal", "-shm"))]
            return {
                str(path): None if not path.exists() else (
                    hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_dev,
                    path.stat().st_ino, path.stat().st_uid, path.stat().st_gid,
                    stat.S_IMODE(path.stat().st_mode), path.stat().st_nlink,
                    deploy._acl_hash(deploy._acl_text(path)))
                for path in paths
            }
        before = fingerprint()
        source_hash = deploy._sha_file(self.db)
        deploy._database_metadata(self.db)
        self.assertEqual(before, fingerprint())
        self.assertEqual(source_hash, deploy._sha_file(self.db))


class DeploymentLockTests(unittest.TestCase):
    @staticmethod
    def fake_stat(file_type, mode, *, uid=0, gid=0, nlink=1):
        return os.stat_result((file_type | mode, 1, 2, nlink, uid, gid, 0, 0, 0, 0))

    def test_lock_path_uses_private_run_directory(self):
        self.assertEqual(Path("/run/nocturne-challenge-timing-deploy.lock"), deploy.LOCK_PATH)
        self.assertEqual(Path("/run"), deploy.LOCK_PATH.parent)

    def test_safe_parent_and_root_owned_private_lock_file_are_accepted(self):
        parent = self.fake_stat(stat.S_IFDIR, 0o755)
        lock = self.fake_stat(stat.S_IFREG, 0o600)
        with (mock.patch.object(Path, "lstat", return_value=parent),
              mock.patch.object(deploy.os, "open", return_value=17) as open_file,
              mock.patch.object(deploy.os, "fstat", return_value=lock),
              mock.patch.object(deploy.fcntl, "flock") as flock):
            self.assertEqual(17, deploy._deployment_lock(deploy.LOCK_PATH))
        self.assertEqual(
            (deploy.LOCK_PATH, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600),
            open_file.call_args.args)
        flock.assert_called_once_with(17, deploy.fcntl.LOCK_EX | deploy.fcntl.LOCK_NB)

    def test_unsafe_parent_metadata_is_rejected_before_open(self):
        unsafe_parents = (
            self.fake_stat(stat.S_IFDIR, 0o755, uid=1000),
            self.fake_stat(stat.S_IFDIR, 0o1777),
            self.fake_stat(stat.S_IFLNK, 0o777),
        )
        for parent in unsafe_parents:
            with self.subTest(parent=parent), mock.patch.object(Path, "lstat", return_value=parent), \
                    mock.patch.object(deploy.os, "open") as open_file:
                with self.assertRaisesRegex(deploy.DeployError, "deployment lock parent is unsafe"):
                    deploy._deployment_lock(deploy.LOCK_PATH)
                open_file.assert_not_called()

    def test_unsafe_lock_file_metadata_is_rejected(self):
        parent = self.fake_stat(stat.S_IFDIR, 0o755)
        unsafe_files = (
            self.fake_stat(stat.S_IFDIR, 0o600),
            self.fake_stat(stat.S_IFREG, 0o640),
            self.fake_stat(stat.S_IFREG, 0o600, uid=1000),
            self.fake_stat(stat.S_IFREG, 0o600, nlink=2),
        )
        for lock in unsafe_files:
            with self.subTest(lock=lock), mock.patch.object(Path, "lstat", return_value=parent), \
                    mock.patch.object(deploy.os, "open", return_value=17), \
                    mock.patch.object(deploy.os, "fstat", return_value=lock), \
                    mock.patch.object(deploy.os, "close") as close_file:
                with self.assertRaisesRegex(deploy.DeployError, "deployment lock file is unsafe"):
                    deploy._deployment_lock(deploy.LOCK_PATH)
                close_file.assert_called_once_with(17)

    def test_cli_acquires_lock_before_apply_entrypoint(self):
        events = []
        plan = {"target": FIXTURE_COMMIT}
        output = __import__("io").StringIO()
        with (mock.patch.object(sys, "argv", ["deploy_timing_metadata.py", "--apply", "--commit", FIXTURE_COMMIT]),
              mock.patch.object(deploy, "make_plan", side_effect=lambda **_kwargs: events.append("plan") or plan),
              mock.patch.object(deploy, "_deployment_lock", side_effect=lambda: events.append("lock") or 19),
              mock.patch.object(deploy, "apply_install", side_effect=lambda *_args, **_kwargs: events.append("apply") or {"status": "installed"}),
              mock.patch.object(deploy.os, "geteuid", return_value=0),
              mock.patch.object(deploy.fcntl, "flock") as flock,
              mock.patch.object(deploy.os, "close") as close_file,
              contextlib.redirect_stdout(output)):
            self.assertEqual(0, deploy._cli(), output.getvalue())
        self.assertEqual(["plan", "lock", "plan", "apply"], events)
        flock.assert_called_once_with(19, deploy.fcntl.LOCK_UN)
        close_file.assert_called_once_with(19)


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
    def __init__(self, fail_start_once: str | None = None, *, fail_stop: str | None = None,
                 fail_drain: str | None = None):
        self.states = {}
        self.actions = []
        self.fail_start_once = fail_start_once
        self.fail_stop = fail_stop
        self.fail_drain = fail_drain
        for unit in deploy.CONTROLLED:
            timer = unit.endswith(".timer")
            self.states[unit] = {"Id": unit, "LoadState": "loaded", "ActiveState": "active",
                                 "SubState": "waiting" if timer else ("running" if unit in deploy.LONG_SERVICES else "exited"),
                                 "Result": "success", "MainPID": "0" if timer or unit in deploy.WRITER_SERVICES else "101"}

    def show(self, unit):
        return dict(self.states[unit])

    def stop(self, unit):
        self.actions.append(("stop", unit))
        if unit == self.fail_stop:
            raise deploy.DeployError("simulated service stop failure")
        self.states[unit].update(ActiveState="inactive", SubState="dead", MainPID="0")

    def start(self, unit):
        self.actions.append(("start", unit))
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
        if unit == self.fail_drain:
            raise deploy.DeployError("simulated one-shot drain failure")
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
        self.db.chmod(0o664)
        self.backup_root = self.root / "backups"
        self.systemd = FakeSystemd()
        self.patches = [
            mock.patch.object(deploy, "FILES", tuple((rel, target) for rel, _src, target, _hash, _meta in self.targets)),
            mock.patch.object(deploy, "verify_git", return_value=None),
            mock.patch.object(deploy, "_prepared_check", return_value="status=prepared\n"),
            mock.patch.object(deploy, "_load_config_module", return_value=FakeConfig),
            mock.patch.object(deploy, "_http_json", side_effect=self.fake_http),
            mock.patch.object(deploy, "_database_open_holders", return_value=set()),
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

    def test_healthy_active_wal_database_is_accepted_by_premaintenance_plan(self):
        connection = sqlite3.connect(self.db)
        self.assertEqual("wal", connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower())
        connection.execute("INSERT INTO challenge_submissions VALUES(2)")
        connection.commit()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(self.db) + suffix)
            self.assertTrue(sidecar.is_file())
            sidecar.chmod(0o664)
        plan = self.make_plan()
        self.assertEqual("dry_run", plan["status"])
        self.assertEqual("ok", plan["database_integrity"])
        connection.close()

    def test_sidecar_gate_runs_after_lock_timers_drain_and_import_services(self):
        events = []
        self.db.chmod(0o664)
        connection = sqlite3.connect(self.db)
        self.assertEqual("wal", connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower())
        connection.execute("INSERT INTO challenge_submissions VALUES(2)")
        connection.commit()
        for suffix in ("-wal", "-shm"):
            Path(str(self.db) + suffix).chmod(0o664)
        plan = self.make_plan()
        original_gate = deploy._wait_for_no_sqlite_sidecars
        original_backups = deploy._backup_files
        original_migrate = deploy._migrate

        def gate(path, systemd, snapshot, initial_sidecars, **kwargs):
            deploy._verify_migration_maintenance(systemd, snapshot)
            for unit in deploy.LONG_SERVICES:
                self.assertEqual("inactive", systemd.show(unit)["ActiveState"], unit)
            events.append("sidecar_gate")
            connection.close()
            return original_gate(path, systemd, snapshot, initial_sidecars, **kwargs)

        def backups(*args, **kwargs):
            events.append("backup")
            return original_backups(*args, **kwargs)

        def migrate(*args, **kwargs):
            events.append("migration")
            return original_migrate(*args, **kwargs)

        with (mock.patch.object(deploy, "_database_open_holders", return_value=set()),
              mock.patch.object(deploy, "_wait_for_no_sqlite_sidecars", side_effect=gate),
              mock.patch.object(deploy, "_backup_files", side_effect=backups),
              mock.patch.object(deploy, "_migrate", side_effect=migrate)):
            result = deploy.apply_install(
                plan, commit=FIXTURE_COMMIT, repo=self.repo, runtime_root=self.root,
                database=self.db, backup_root=self.backup_root, release=self.release,
                systemd=self.systemd, _testing_owner_uid=os.geteuid())
        self.assertEqual("installed", result["status"])
        self.assertEqual(["sidecar_gate", "backup", "migration"], events)

    def test_full_apply_accepts_real_wal_metadata_and_timer_without_mainpid(self):
        class SystemdWithoutTimerMainPID(FakeSystemd):
            # Exercise the production waiter rather than the permissive fake
            # waiter: systemd omits MainPID for timer units on this host.
            wait_inactive = deploy.Systemd.wait_inactive

            def show(self, unit):
                state = dict(self.states[unit])
                if unit.endswith(".timer"):
                    state.pop("MainPID", None)
                return state

            def stop(self, unit):
                super().stop(unit)
                if unit.endswith(".timer"):
                    self.states[unit].pop("MainPID", None)

        events = []
        connection = sqlite3.connect(self.db)
        self.assertEqual("wal", connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower())
        connection.execute("INSERT INTO challenge_submissions VALUES(2)")
        connection.commit()
        for suffix in ("-wal", "-shm"):
            Path(str(self.db) + suffix).chmod(0o664)
        # Use the real metadata routine/return shape (main plus optional
        # sidecars, each with identity, raw ACL digest, and content digest).
        metadata = deploy._database_metadata(self.db)
        self.assertEqual({"main", "-wal", "-shm"}, set(metadata))
        self.assertTrue(all(len(value) == 10 for value in metadata.values()))
        plan = self.make_plan()
        original_gate = deploy._wait_for_no_sqlite_sidecars
        original_backup = deploy._backup_files
        original_migrate = deploy._migrate

        def gate(*args, **kwargs):
            events.append("sidecar_gate")
            connection.close()
            return original_gate(*args, **kwargs)

        def backup(*args, **kwargs):
            events.append("backup")
            return original_backup(*args, **kwargs)

        def migrate(*args, **kwargs):
            events.append("migration")
            return original_migrate(*args, **kwargs)

        systemd = SystemdWithoutTimerMainPID()
        with (mock.patch.object(deploy, "_wait_for_no_sqlite_sidecars", side_effect=gate),
              mock.patch.object(deploy, "_backup_files", side_effect=backup),
              mock.patch.object(deploy, "_migrate", side_effect=migrate)):
            result = deploy.apply_install(
                plan, commit=FIXTURE_COMMIT, repo=self.repo, runtime_root=self.root,
                database=self.db, backup_root=self.backup_root, release=self.release,
                systemd=systemd, _testing_owner_uid=os.geteuid())
        self.assertEqual("installed", result["status"])
        self.assertEqual(["sidecar_gate", "backup", "migration"], events)

    def test_prebackup_failure_restores_units_and_leaves_schema_files_unchanged(self):
        connection = sqlite3.connect(self.db)
        self.assertEqual("wal", connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower())
        connection.execute("INSERT INTO challenge_submissions VALUES(2)")
        connection.commit()
        for suffix in ("-wal", "-shm"):
            Path(str(self.db) + suffix).chmod(0o664)
        plan = self.make_plan()
        database_sha = deploy._sha_file(self.db)
        sidecar_shas = {suffix: deploy._sha_file(Path(str(self.db) + suffix))
                        for suffix in ("-wal", "-shm")}
        targets = [target for _rel, _src, target, _sha, _meta in self.targets]
        target_shas = [deploy._sha_file(target) for target in targets]
        before_units = {unit: self.systemd.show(unit) for unit in deploy.CONTROLLED}
        with mock.patch.object(deploy, "_wait_for_no_sqlite_sidecars",
                               side_effect=deploy.DeployError("fixture pre-backup gate failure")):
            with self.assertRaisesRegex(deploy.DeployError, "rolled back"):
                deploy.apply_install(
                    plan, commit=FIXTURE_COMMIT, repo=self.repo, runtime_root=self.root,
                    database=self.db, backup_root=self.backup_root, release=self.release,
                    systemd=self.systemd, _testing_owner_uid=os.geteuid())
        self.assertEqual(database_sha, deploy._sha_file(self.db))
        self.assertEqual(sidecar_shas, {suffix: deploy._sha_file(Path(str(self.db) + suffix))
                                        for suffix in ("-wal", "-shm")})
        self.assertEqual(target_shas, [deploy._sha_file(target) for target in targets])
        self.assertFalse(deploy._has_columns(self.db))
        for unit, state in before_units.items():
            after = self.systemd.show(unit)
            self.assertEqual(state["LoadState"], after["LoadState"])
            self.assertEqual(state["ActiveState"], after["ActiveState"])
            self.assertEqual(state["SubState"], after["SubState"])
            if unit.endswith(".timer"):
                self.assertEqual(state["MainPID"], after["MainPID"])
            elif unit in deploy.LONG_SERVICES and state["ActiveState"] == "active":
                self.assertGreater(int(after["MainPID"]), 0)
        transaction_dirs = list(self.backup_root.glob(f"{FIXTURE_COMMIT}/*"))
        self.assertEqual(1, len(transaction_dirs))
        self.assertEqual([], list(transaction_dirs[0].iterdir()))
        connection.close()

    def test_persistent_sidecar_aborts_before_database_mutation_and_restores_units(self):
        self.db.chmod(0o664)
        connection = sqlite3.connect(self.db)
        self.assertEqual("wal", connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower())
        connection.execute("INSERT INTO challenge_submissions VALUES(2)")
        connection.commit()
        for suffix in ("-wal", "-shm"):
            Path(str(self.db) + suffix).chmod(0o664)
        plan = self.make_plan()
        wal = Path(str(self.db) + "-wal")
        db_before = deploy._sha_file(self.db)
        wal_before = deploy._sha_file(wal)
        unit_states_before = {unit: self.systemd.show(unit) for unit in deploy.CONTROLLED}
        original_unlink = Path.unlink
        original_os_unlink = os.unlink

        def forbid_sidecar_unlink(path, *args, **kwargs):
            if path in {wal, Path(str(self.db) + "-shm"), Path(str(self.db) + "-journal")}:
                raise AssertionError("maintenance must never unlink SQLite sidecars")
            return original_unlink(path, *args, **kwargs)

        def forbid_os_sidecar_unlink(path, *args, **kwargs):
            if Path(path) in {wal, Path(str(self.db) + "-shm"), Path(str(self.db) + "-journal")}:
                raise AssertionError("maintenance must never unlink SQLite sidecars")
            return original_os_unlink(path, *args, **kwargs)

        with (mock.patch.object(deploy, "SIDECAR_DRAIN_TIMEOUT_SECONDS", 0.002),
              mock.patch.object(deploy, "SIDECAR_DRAIN_INTERVAL_SECONDS", 0.001),
              mock.patch.object(Path, "unlink", forbid_sidecar_unlink),
              mock.patch.object(os, "unlink", forbid_os_sidecar_unlink)):
            with self.assertRaisesRegex(deploy.DeployError, "rolled back"):
                deploy.apply_install(
                    plan, commit=FIXTURE_COMMIT, repo=self.repo, runtime_root=self.root,
                    database=self.db, backup_root=self.backup_root, release=self.release,
                    systemd=self.systemd, _testing_owner_uid=os.geteuid())
        self.assertEqual(db_before, deploy._sha_file(self.db))
        self.assertEqual(wal_before, deploy._sha_file(wal))
        self.assertTrue(wal.exists())
        for unit, before in unit_states_before.items():
            after = self.systemd.show(unit)
            self.assertEqual(before["LoadState"], after["LoadState"])
            self.assertEqual(before["ActiveState"], after["ActiveState"])
            self.assertEqual(before["SubState"], after["SubState"])
        connection.close()

    def test_stop_and_drain_failures_restore_captured_active_state(self):
        scenarios = (
            ("stop", FakeSystemd(fail_stop=deploy.LONG_SERVICES[0])),
            ("drain", FakeSystemd(fail_drain=deploy.WRITER_SERVICES[0])),
        )
        scenarios[1][1].states[deploy.WRITER_SERVICES[0]].update(
            ActiveState="active", SubState="running", MainPID="303")
        for label, systemd in scenarios:
            with self.subTest(label=label):
                before = {unit: systemd.show(unit) for unit in deploy.CONTROLLED}
                with mock.patch.object(deploy, "_wait_for_no_sqlite_sidecars") as sidecar_gate:
                    with self.assertRaisesRegex(deploy.DeployError, "rolled back"):
                        self.apply(systemd=systemd)
                    sidecar_gate.assert_not_called()
                for unit, prior in before.items():
                    after = systemd.show(unit)
                    self.assertEqual(prior["ActiveState"], after["ActiveState"])
                    self.assertEqual(prior["SubState"], after["SubState"])

    def test_unknown_database_holder_blocks_after_sidecars_disappear(self):
        systemd = FakeSystemd()
        snapshot = {unit: systemd.show(unit) for unit in deploy.CONTROLLED}
        for unit in deploy.CONTROLLED:
            systemd.states[unit].update(ActiveState="inactive", SubState="dead", MainPID="0")
        with mock.patch.object(deploy, "_database_open_holders", return_value={9876}):
            with self.assertRaisesRegex(deploy.DeployError, "unknown process still holds"):
                deploy._wait_for_no_sqlite_sidecars(self.db, systemd, snapshot, {}, timeout=0.01)

    def test_unexpected_sidecar_appearance_and_post_stop_change_fail_closed(self):
        systemd = FakeSystemd()
        snapshot = {unit: systemd.show(unit) for unit in deploy.CONTROLLED}
        for unit in deploy.CONTROLLED:
            systemd.states[unit].update(ActiveState="inactive", SubState="dead", MainPID="0")
        wal = Path(str(self.db) + "-wal")
        wal.write_bytes(b"")
        wal.chmod(0o664)
        with self.assertRaisesRegex(deploy.DeployError, "unexpected SQLite sidecar"):
            deploy._wait_for_no_sqlite_sidecars(self.db, systemd, snapshot, {}, timeout=0.01)

        initial = deploy._sidecar_state_snapshot(self.db)
        changed = dict(initial)
        value = list(changed["-wal"])
        value[6] += 1
        changed["-wal"] = tuple(value)
        with (mock.patch.object(deploy, "_sidecar_state_snapshot", side_effect=[initial, changed]),
              mock.patch.object(deploy.time, "monotonic", side_effect=[0.0, 0.2, 0.2]),
              mock.patch.object(deploy.time, "sleep")):
            with self.assertRaisesRegex(deploy.DeployError, "sidecar changed while holders were stopped"):
                deploy._wait_for_no_sqlite_sidecars(self.db, systemd, snapshot, initial,
                                                    timeout=1.0, interval=0.1)

    def test_main_database_identity_cannot_change_after_service_stop(self):
        systemd = FakeSystemd()
        snapshot = {unit: systemd.show(unit) for unit in deploy.CONTROLLED}
        for unit in deploy.CONTROLLED:
            systemd.states[unit].update(ActiveState="inactive", SubState="dead", MainPID="0")
        st = self.db.lstat()
        changed_stat = list(st)
        changed_stat[1] += 1
        with mock.patch.object(deploy, "_db_lstat", return_value=os.stat_result(changed_stat)):
            with self.assertRaisesRegex(deploy.DeployError, "identity changed during maintenance"):
                deploy._wait_for_no_sqlite_sidecars(
                    self.db, systemd, snapshot, {}, main_identity=deploy._stat_identity(st),
                    timeout=0.01, interval=0.001)

    def test_clean_install_is_additive_and_version_10_is_preserved(self):
        conn = sqlite3.connect(self.db)
        before_counts = deploy._table_counts(conn)
        conn.close()
        result = self.apply()
        self.assertEqual("installed", result["status"])
        self.assertEqual(list(deploy.LONG_SERVICES), result["services_restarted"])
        self.assertEqual(("stop", "osrs-drops-admin.service"),
                         next(action for action in self.systemd.actions
                              if action == ("stop", "osrs-drops-admin.service")))
        self.assertIn(("start", "osrs-drops-admin.service"), self.systemd.actions)
        admin_state = self.systemd.show("osrs-drops-admin.service")
        self.assertEqual(("loaded", "active", "running"),
                         (admin_state["LoadState"], admin_state["ActiveState"], admin_state["SubState"]))
        self.assertGreater(int(admin_state["MainPID"]), 0)
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

    def test_admin_initially_inactive_is_not_stopped_or_restarted(self):
        admin = "osrs-drops-admin.service"
        systemd = FakeSystemd()
        systemd.states[admin].update(ActiveState="inactive", SubState="dead", MainPID="0")
        plan = deploy.make_plan(commit=FIXTURE_COMMIT, repo=self.repo, runtime_root=self.root,
                                release=self.release, database=self.db, systemd=systemd)
        self.assertIn(admin, plan["stop_and_restart_if_active"])
        self.assertNotIn(admin, plan["services_restarted_for_imports"])
        result = self.apply(systemd=systemd)
        self.assertEqual("installed", result["status"])
        self.assertNotIn(admin, result["services_restarted"])
        self.assertNotIn(("stop", admin), systemd.actions)
        self.assertNotIn(("start", admin), systemd.actions)
        self.assertEqual(("loaded", "inactive", "dead", "0"), tuple(
            systemd.show(admin)[key] for key in ("LoadState", "ActiveState", "SubState", "MainPID")))

    def test_admin_stop_failure_restores_captured_state(self):
        admin = "osrs-drops-admin.service"
        systemd = FakeSystemd(fail_stop=admin)
        before = {unit: systemd.show(unit) for unit in deploy.CONTROLLED}
        with self.assertRaisesRegex(deploy.DeployError,
                                    "phase=maintenance_stop diagnostic_category=maintenance_failure"):
            self.apply(systemd=systemd)
        for unit, state in before.items():
            after = systemd.show(unit)
            self.assertEqual((state["LoadState"], state["ActiveState"], state["SubState"]),
                             (after["LoadState"], after["ActiveState"], after["SubState"]))
        self.assertIn(("start", "osrs-drops-api.service"), systemd.actions)
        self.assertNotIn(("start", admin), systemd.actions)

    def test_admin_restart_failure_rolls_back_database_files_and_services(self):
        admin = "osrs-drops-admin.service"
        before_files = [deploy._sha_file(item[2]) for item in self.targets]
        before_conn = sqlite3.connect(self.db)
        before_counts = deploy._table_counts(before_conn)
        before_conn.close()
        systemd = FakeSystemd(fail_start_once=admin)
        with self.assertRaisesRegex(deploy.DeployError,
                                    "phase=service_restoration diagnostic_category=service_restoration_failure"):
            self.apply(systemd=systemd)
        self.assertEqual(before_files, [deploy._sha_file(item[2]) for item in self.targets])
        restored = sqlite3.connect(self.db)
        self.assertEqual("ok", restored.execute("PRAGMA integrity_check").fetchone()[0])
        self.assertEqual(before_counts, deploy._table_counts(restored))
        self.assertEqual(10, restored.execute("SELECT config_version_id FROM challenge_config_versions WHERE status='active'").fetchone()[0])
        self.assertFalse(set(deploy.NEW_COLUMNS).issubset({row[1] for row in restored.execute("PRAGMA table_info(challenge_config_bosses)")}))
        restored.close()
        for unit in deploy.LONG_SERVICES:
            state = systemd.show(unit)
            self.assertEqual(("loaded", "active", "running"),
                             (state["LoadState"], state["ActiveState"], state["SubState"]))
            self.assertGreater(int(state["MainPID"]), 0)
        for unit in deploy.TIMERS:
            self.assertEqual("active", systemd.show(unit)["ActiveState"])

    def test_rollback_after_migration_restores_admin_if_previously_active(self):
        admin = "osrs-drops-admin.service"
        before_files = [deploy._sha_file(item[2]) for item in self.targets]

        def fail(phase):
            if phase == "migration_complete":
                raise RuntimeError("private fixture detail")

        with self.assertRaisesRegex(deploy.DeployError,
                                    "phase=schema_migration diagnostic_category=unexpected_failure") as caught:
            self.apply(failpoint=fail)
        self.assertNotIn("private fixture detail", str(caught.exception))
        self.assertEqual(before_files, [deploy._sha_file(item[2]) for item in self.targets])
        self.assertFalse(deploy._has_columns(self.db))
        self.assertEqual(("active", "running"), tuple(
            self.systemd.show(admin)[key] for key in ("ActiveState", "SubState")))
        self.assertGreater(int(self.systemd.show(admin)["MainPID"]), 0)

    def test_website_asset_failure_rolls_back(self):
        before = [deploy._sha_file(item[2]) for item in self.targets]
        def fail(phase):
            if phase == "file:challenge-admin-state.js": raise RuntimeError("website fixture")
        with self.assertRaises(deploy.DeployError): self.apply(failpoint=fail)
        self.assertEqual(before, [deploy._sha_file(item[2]) for item in self.targets])


if __name__ == "__main__":
    unittest.main()
