import io
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import subprocess

import immutable_runtime_check as check
import immutable_runtime_release as runtime
import emoji_runtime_release as emoji_runtime


COMMIT = "a" * 40


class ImmutableRuntimeCheckTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.root = self.base / "runtime"
        (self.repo / "dev/intake").mkdir(parents=True)
        self.root.mkdir(mode=0o755)
        (self.repo / "dev/intake/runtime-requirements.lock").write_text(
            runtime.GUNICORN_LOCK_TEXT)
        (self.repo / "dev/intake/emoji-sync-requirements.txt").write_text(
            emoji_runtime.REQUIREMENTS_TEXT)

    def tearDown(self):
        self.temp.cleanup()

    def tree_state(self):
        result = []
        for path in sorted((self.base, *self.base.rglob("*"))):
            value = path.lstat()
            result.append((str(path.relative_to(self.base)), value.st_ino,
                           value.st_mode, value.st_uid, value.st_gid,
                           value.st_size, value.st_mtime_ns))
        return result

    def command_run(self, args, **_kwargs):
        if args[0] == "getfacl":
            return SimpleNamespace(stdout="user::rwx\ngroup::r-x\nother::r-x\n")
        return SimpleNamespace(stdout="")

    def inspect(self):
        with patch("immutable_runtime_check.verify_checkout"), \
                patch("immutable_runtime_check.emoji_runtime.validate_host",
                      return_value={"version": [3, 14]}):
            return check.inspect(self.repo, self.root, COMMIT,
                                 python=Path("/usr/bin/python3.14"),
                                 uid=os.getuid(), gid=os.getgid(), run=self.command_run)

    def test_fully_unprepared_reports_every_prerequisite_without_mutation(self):
        before = self.tree_state()
        report = self.inspect()
        self.assertEqual(before, self.tree_state())
        self.assertEqual("not_prepared", report.state)
        by_phase = {item["phase"]: item for item in report.diagnostics}
        self.assertEqual({
            "gunicorn_wheel", "pillow_wheel", "release", "gunicorn_runtime",
            "emoji_runtime", "staged_units", "staged_nginx",
        }, set(by_phase))
        for item in by_phase.values():
            self.assertEqual("not_prepared", item["classification"])
            self.assertEqual("absent", item["observed"])
            self.assertEqual("prepare", item["operator_action"])
            self.assertTrue(item["path"])
            self.assertTrue(item["expected"])
        rendered = report.render()
        self.assertIn("status=not_prepared", rendered)
        self.assertIn("check_mode=read_only", rendered)

    def test_safe_legacy_predecessor_does_not_collide_with_absent_new_runtime(self):
        legacy = self.root / "venvs" / runtime.LEGACY_VENV_NAME
        legacy.mkdir(parents=True)
        (self.root / "venvs").chmod(0o755)
        (self.root / "venv").symlink_to(
            Path("venvs") / runtime.LEGACY_VENV_NAME)
        before = self.tree_state()
        with patch("immutable_runtime_check.verify_checkout"), \
                patch("immutable_runtime_check.emoji_runtime.validate_host",
                      return_value={"version": [3, 14]}), \
                patch("immutable_runtime_check.runtime.validate_legacy_venv",
                      return_value={"requirements_sha256":
                                    runtime.LEGACY_GUNICORN_LOCK_SHA256}):
            report = check.inspect(self.repo, self.root, COMMIT,
                                   uid=os.getuid(), gid=os.getgid(), run=self.command_run)
        self.assertEqual(before, self.tree_state())
        self.assertEqual("not_prepared", report.state)
        by_phase = {item["phase"]: item for item in report.diagnostics}
        self.assertEqual("prepared", by_phase["legacy_gunicorn_runtime"]["classification"])
        self.assertIn("valid_predecessor", by_phase["legacy_gunicorn_runtime"]["observed"])
        self.assertEqual("not_prepared", by_phase["gunicorn_runtime"]["classification"])
        self.assertEqual("absent", by_phase["gunicorn_runtime"]["observed"])
        self.assertIn(runtime.VENV_NAME, by_phase["gunicorn_runtime"]["path"])

    def test_corrupt_selected_legacy_predecessor_fails_closed_without_mutation(self):
        legacy = self.root / "venvs" / runtime.LEGACY_VENV_NAME
        legacy.mkdir(parents=True)
        (self.root / "venvs").chmod(0o755)
        (self.root / "venv").symlink_to(
            Path("venvs") / runtime.LEGACY_VENV_NAME)
        before = self.tree_state()
        with patch("immutable_runtime_check.verify_checkout"), \
                patch("immutable_runtime_check.emoji_runtime.validate_host",
                      return_value={"version": [3, 14]}), \
                patch("immutable_runtime_check.runtime.validate_legacy_venv",
                      side_effect=ValueError("legacy launcher mismatch")):
            report = check.inspect(self.repo, self.root, COMMIT,
                                   uid=os.getuid(), gid=os.getgid(), run=self.command_run)
        self.assertEqual(before, self.tree_state())
        self.assertEqual("unsafe_blocking", report.state)
        item = next(value for value in report.diagnostics
                    if value["phase"] == "legacy_gunicorn_runtime")
        self.assertIn("legacy launcher mismatch", item["observed"])

    def test_legacy_and_current_lock_runtimes_can_coexist(self):
        legacy = self.root / "venvs" / runtime.LEGACY_VENV_NAME
        current = self.root / "venvs" / runtime.VENV_NAME
        legacy.mkdir(parents=True)
        current.mkdir()
        (self.root / "venvs").chmod(0o755)
        (self.root / "venv").symlink_to(
            Path("venvs") / runtime.LEGACY_VENV_NAME)
        before = self.tree_state()
        with patch("immutable_runtime_check.verify_checkout"), \
                patch("immutable_runtime_check.emoji_runtime.validate_host",
                      return_value={"version": [3, 14]}), \
                patch("immutable_runtime_check.runtime.validate_legacy_venv",
                      return_value={"requirements_sha256":
                                    runtime.LEGACY_GUNICORN_LOCK_SHA256}), \
                patch("immutable_runtime_check.runtime.validate_venv",
                      return_value={"requirements_sha256":
                                    runtime.GUNICORN_LOCK_SHA256}):
            report = check.inspect(self.repo, self.root, COMMIT,
                                   uid=os.getuid(), gid=os.getgid(), run=self.command_run)
        self.assertEqual(before, self.tree_state())
        self.assertNotEqual("unsafe_blocking", report.state)
        phases = [item["phase"] for item in report.diagnostics]
        self.assertIn("legacy_gunicorn_runtime", phases)
        self.assertNotIn("gunicorn_runtime", phases)

    def test_git_mismatch_is_unsafe_and_diagnostic(self):
        before = self.tree_state()
        with patch("immutable_runtime_check.verify_checkout",
                   side_effect=ValueError("HEAD mismatch")), \
                patch("immutable_runtime_check.emoji_runtime.validate_host",
                      return_value={"version": [3, 14]}):
            report = check.inspect(self.repo, self.root, COMMIT,
                                   uid=os.getuid(), gid=os.getgid(), run=self.command_run)
        self.assertEqual(before, self.tree_state())
        item = next(value for value in report.diagnostics
                    if value["phase"] == "git_checkout")
        self.assertEqual("unsafe_blocking", report.state)
        self.assertIn("HEAD mismatch", item["observed"])
        self.assertEqual("stop", item["operator_action"])

    def test_runtime_root_mode_and_acl_fail_closed_with_diagnostics(self):
        self.root.chmod(0o700)
        report = self.inspect()
        item = next(value for value in report.diagnostics
                    if value["phase"] == "runtime_root")
        self.assertEqual("unsafe_blocking", item["classification"])
        self.assertIn("0755", item["expected"])
        self.root.chmod(0o755)

        def extended_acl(args, **_kwargs):
            if args[0] == "getfacl":
                return SimpleNamespace(stdout=(
                    "user::rwx\nuser:other:r-x\ngroup::r-x\nother::r-x\n"))
            return SimpleNamespace(stdout="")
        with patch("immutable_runtime_check.verify_checkout"), \
                patch("immutable_runtime_check.emoji_runtime.validate_host",
                      return_value={"version": [3, 14]}):
            report = check.inspect(self.repo, self.root, COMMIT,
                                   uid=os.getuid(), gid=os.getgid(), run=extended_acl)
        item = next(value for value in report.diagnostics
                    if value["phase"] == "runtime_root")
        self.assertEqual("unsafe_blocking", item["classification"])
        self.assertIn("ACL", item["observed"])

    def test_verified_incomplete_runtimes_are_recoverable_and_read_only(self):
        core = self.root / "venvs" / runtime.VENV_NAME
        emoji = self.root / "venvs" / emoji_runtime.TARGET_NAME
        core.mkdir(parents=True)
        emoji.mkdir()
        (self.root / "venvs").chmod(0o755)
        (core / runtime.VENV_MARKER).write_text("marker")
        (emoji / emoji_runtime.MARKER).write_text("marker")
        before = self.tree_state()
        with patch("immutable_runtime_check.verify_checkout"), \
                patch("immutable_runtime_check.emoji_runtime.validate_host",
                      return_value={"version": [3, 14]}), \
                patch("immutable_runtime_check.runtime.recover_incomplete_venv",
                      return_value={"state": "verified_incomplete"}), \
                patch("immutable_runtime_check.emoji_runtime.recover_incomplete",
                      return_value={"state": "verified_incomplete"}):
            report = check.inspect(self.repo, self.root, COMMIT,
                                   uid=os.getuid(), gid=os.getgid(), run=self.command_run)
        self.assertEqual(before, self.tree_state())
        self.assertEqual("recoverable_incomplete", report.state)
        runtimes = [item for item in report.diagnostics
                    if item["phase"] in {"gunicorn_runtime", "emoji_runtime"}]
        self.assertEqual(2, len(runtimes))
        self.assertTrue(all(item["operator_action"] == "recover" for item in runtimes))

    def test_unverified_incomplete_runtime_is_unsafe(self):
        core = self.root / "venvs" / runtime.VENV_NAME
        core.mkdir(parents=True)
        (self.root / "venvs").chmod(0o755)
        (core / runtime.VENV_MARKER).write_text("bad")
        with patch("immutable_runtime_check.verify_checkout"), \
                patch("immutable_runtime_check.emoji_runtime.validate_host",
                      return_value={"version": [3, 14]}), \
                patch("immutable_runtime_check.runtime.recover_incomplete_venv",
                      side_effect=ValueError("marker mismatch")):
            report = check.inspect(self.repo, self.root, COMMIT,
                                   uid=os.getuid(), gid=os.getgid(), run=self.command_run)
        item = next(value for value in report.diagnostics
                    if value["phase"] == "gunicorn_runtime")
        self.assertEqual("unsafe_blocking", item["classification"])
        self.assertEqual("stop", item["operator_action"])

    def test_interrupted_staging_and_unsafe_existing_paths_are_blocking(self):
        interrupted = self.root / "staged-units/.stage-interrupted"
        interrupted.mkdir(parents=True)
        (self.root / "staged-nginx" / COMMIT).parent.mkdir(parents=True)
        (self.root / "staged-units").chmod(0o755)
        (self.root / "staged-nginx").chmod(0o755)
        (self.root / "staged-nginx" / COMMIT).symlink_to("missing")
        report = self.inspect()
        phases = {item["phase"] for item in report.diagnostics
                  if item["classification"] == "unsafe_blocking"}
        self.assertIn("unit_staging", phases)
        self.assertIn("staged_nginx", phases)

    def test_safe_prepared_state_requires_all_validators(self):
        gunicorn_wheel = (self.root / "wheelhouse" / runtime.VENV_NAME /
                           runtime.GUNICORN_WHEEL_NAME)
        emoji_wheel = (self.root / "wheelhouse" / emoji_runtime.TARGET_NAME /
                       emoji_runtime.WHEEL_NAME)
        for wheel in (gunicorn_wheel, emoji_wheel):
            wheel.parent.mkdir(parents=True, exist_ok=True)
            wheel.write_bytes(b"wheel")
        release = self.root / "releases" / COMMIT
        (release / "dev/intake").mkdir(parents=True)
        (release / "dev/intake/runtime-requirements.lock").write_text(
            runtime.GUNICORN_LOCK_TEXT)
        (release / "dev/intake/emoji-sync-requirements.txt").write_text(
            emoji_runtime.REQUIREMENTS_TEXT)
        (self.root / "venvs" / runtime.VENV_NAME).mkdir(parents=True)
        (self.root / "venvs" / emoji_runtime.TARGET_NAME).mkdir()
        (self.root / "staged-units" / COMMIT).mkdir(parents=True)
        (self.root / "staged-nginx" / COMMIT).mkdir(parents=True)
        with patch("immutable_runtime_check.verify_checkout"), \
                patch("immutable_runtime_check.runtime._safe_directory_node"), \
                patch("immutable_runtime_check.emoji_runtime.validate_host",
                      return_value={"version": [3, 14]}), \
                patch("immutable_runtime_check.runtime.validate_locked_wheel"), \
                patch("immutable_runtime_check.emoji_runtime.validate_inputs"), \
                patch("immutable_runtime_check.runtime.verify_release"), \
                patch("immutable_runtime_check.runtime.verify_release_ownership"), \
                patch("immutable_runtime_check.runtime.validate_venv",
                      return_value={"requirements_sha256": "digest"}), \
                patch("immutable_runtime_check.emoji_runtime.validate_runtime",
                      return_value={"requirements_sha256": "digest"}), \
                patch("immutable_runtime_check.runtime._verify_stage",
                      return_value={"release_manifest_sha256": "digest"}), \
                patch("immutable_runtime_check.runtime.digest", return_value="digest"), \
                patch("immutable_runtime_check.runtime.verify_staged_deployment"):
            report = check.inspect(self.repo, self.root, COMMIT,
                                   uid=os.getuid(), gid=os.getgid(), run=self.command_run)
        self.assertEqual("prepared", report.state)
        self.assertIn("status=prepared", report.render())
        self.assertIn("operator_action=none", report.render())

    def test_quarantine_is_reported_without_becoming_a_failure(self):
        quarantine = self.root / "quarantine/incomplete-venvs/item/runtime"
        quarantine.mkdir(parents=True)
        (self.root / "quarantine").chmod(0o700)
        (self.root / "quarantine/incomplete-venvs").chmod(0o700)
        report = self.inspect()
        item = next(value for value in report.diagnostics
                    if value["phase"] == "gunicorn_quarantine")
        self.assertEqual("prepared", item["classification"])
        self.assertEqual("none", item["operator_action"])
        self.assertEqual("not_prepared", report.state)

    def test_rendered_diagnostics_are_single_line_and_exit_codes_are_distinct(self):
        report = check.Report()
        report.add("unsafe_blocking", "phase\nname", "/path", "expected\nstate",
                   "bad\nstate", "stop")
        rendered = report.render()
        self.assertIn("phase=phase name", rendered)
        self.assertNotIn("expected\nstate", rendered)
        self.assertEqual(0, check.EXIT_CODES["prepared"])
        self.assertEqual(3, check.EXIT_CODES["not_prepared"])
        self.assertEqual(4, check.EXIT_CODES["recoverable_incomplete"])
        self.assertEqual(1, check.EXIT_CODES["unsafe_blocking"])

    def test_wrapper_propagates_structured_not_prepared_result(self):
        fake_bin = self.base / "bin"
        fake_bin.mkdir()
        (fake_bin / "id").write_text("#!/bin/sh\necho 0\n")
        (fake_bin / "git").write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            f"  *'rev-parse HEAD'*) echo {COMMIT} ;;\n"
            f"  *'rev-parse --verify {COMMIT}^{{commit}}'*) echo {COMMIT} ;;\n"
            "  *'status --porcelain=v1 --untracked-files=all'*) : ;;\n"
            "  *) exit 2 ;;\n"
            "esac\n")
        fake_python = self.base / "python3.14"
        fake_python.write_text(
            "#!/bin/sh\n"
            "printf 'status=not_prepared\\ncheck_mode=read_only\\n'\n"
            "printf 'phase=gunicorn_wheel\\npath=/missing\\nexpected=wheel\\n'\n"
            "printf 'observed=absent\\noperator_action=prepare\\n'\n"
            "exit 3\n")
        for path in (*fake_bin.iterdir(), fake_python):
            path.chmod(0o755)
        wrapper = Path(__file__).parent / "prepare_immutable_runtime.sh"
        wrapper_text = wrapper.read_text().replace("/usr/bin/python3.14", str(fake_python))
        wrapper_copy = self.base / "prepare.sh"
        wrapper_copy.write_text(wrapper_text)
        before = self.tree_state()
        result = subprocess.run(
            ["/bin/bash", str(wrapper_copy), "--check", COMMIT],
            env={"PATH": str(fake_bin) + ":/usr/bin:/bin"},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        self.assertEqual(3, result.returncode)
        self.assertIn("status=not_prepared", result.stdout)
        self.assertIn("phase=gunicorn_wheel", result.stdout)
        self.assertEqual("", result.stderr)
        self.assertEqual(before, self.tree_state())

    def test_wrapper_git_mismatch_is_never_silent(self):
        fake_bin = self.base / "bin"
        fake_bin.mkdir()
        (fake_bin / "id").write_text("#!/bin/sh\necho 0\n")
        (fake_bin / "git").write_text(
            "#!/bin/sh\n"
            "case \"$*\" in\n"
            "  *'rev-parse HEAD'*) printf '%040d\\n' 0 ;;\n"
            "  *) exit 2 ;;\n"
            "esac\n")
        for path in fake_bin.iterdir():
            path.chmod(0o755)
        wrapper = Path(__file__).parent / "prepare_immutable_runtime.sh"
        result = subprocess.run(
            ["/bin/bash", str(wrapper), "--check", COMMIT],
            env={"PATH": str(fake_bin) + ":/usr/bin:/bin"},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
        self.assertEqual(1, result.returncode)
        self.assertIn("status=unsafe_blocking", result.stderr)
        self.assertIn("phase=git_checkout", result.stderr)
        self.assertIn("operator_action=stop", result.stderr)


if __name__ == "__main__":
    unittest.main()
