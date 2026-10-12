from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path

TREE = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "automatic_observation_scope_test", TREE / "verify_automatic_observation_scope.py"
)
scope = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(scope)


class AutomaticObservationScopeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        records = []
        for relative in scope.SCOPE_BUNDLE_PATHS:
            source = TREE.parents[1] / relative
            target = self.repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            data = target.read_bytes()
            records.append({"path": relative, "type": "regular", "size": len(data),
                            "sha256": hashlib.sha256(data).hexdigest()})
        for relative in scope.REPOSITORY_RUNTIME_PINS:
            source = TREE.parents[1] / relative
            target = self.repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        live_records = []
        for _source, target, kind in scope.EXPECTED_TARGETS:
            if kind == "manifest":
                live_records.append({"path": target, "type": "regular", "size": 1,
                                     "sha256": "a" * 64})
        manifest_path = self.repo / scope.MANIFEST
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps({
            "bundle_files": records,
            "live_sources": live_records,
            # Deliberately malformed and unrelated: the scoped feature gate
            # must not consult whole-product external references.
            "external_references": [{"file": {"sha256": "not-a-digest"}}],
        }))

    def test_scope_covers_deployer_runtime_units_and_all_install_targets(self):
        result = scope.verify_scope(self.repo)
        self.assertEqual("scoped_bundle_verified", result["status"])
        self.assertEqual(len(scope.EXPECTED_TARGETS), result["install_targets"])
        self.assertEqual(0, result["external_references_checked"])
        deployed_sources = {source for source, _target, _kind in scope.EXPECTED_TARGETS}
        self.assertTrue(deployed_sources.issubset(scope.SCOPE_SET))
        self.assertTrue(set(scope.UNIT_REFERENCES).issubset(scope.SCOPE_SET))

    def test_plugin_tracking_editor_is_separate_and_staff_protected(self):
        routes = self.repo / "dev/challenges/integration/routes/admin_app_challenge_routes.fragment.py"
        source = routes.read_text()
        marker = "def register_plugin_tracking_admin_routes():"
        before, tracking = source.split(marker, 1)
        routes.write_text(before + marker + tracking.replace("@require_noc_super_admin", "@login_required", 1))
        manifest_path = self.repo / scope.MANIFEST
        manifest = json.loads(manifest_path.read_text())
        record = next(row for row in manifest["bundle_files"] if row["path"] == str(routes.relative_to(self.repo)))
        data = routes.read_bytes()
        record["size"] = len(data)
        record["sha256"] = hashlib.sha256(data).hexdigest()
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(scope.ScopeError, "staff editor surface"):
            scope.verify_scope(self.repo)

    def test_unrelated_midgard_products_are_excluded(self):
        excluded = {
            "dev/challenges/consumers/nocturne-bot/utils/dropSubmissionUtils.js",
            "dev/challenges/website/navbar.html",
            "/srv/projects/api/admin_app.py",
            "/srv/projects/nocturne-bot/buttonHandler.js",
        }
        self.assertFalse(excluded & scope.SCOPE_SET)
        self.assertFalse(any(path.startswith("/srv/projects/") and "admin_app" in path
                             for path in scope.SCOPE_SET))
        self.assertFalse(any("navbar" in path or "dropSubmission" in path
                             for path in scope.SCOPE_SET))
        # This repository-owned fragment is the narrowly scoped Plugin
        # Tracking editor integration; the live admin app is not in scope.
        self.assertIn("dev/challenges/integration/routes/admin_app_challenge_routes.fragment.py",
                      scope.SCOPE_SET)

    def test_unrelated_external_reference_drift_does_not_block_scope(self):
        manifest_path = self.repo / scope.MANIFEST
        manifest = json.loads(manifest_path.read_text())
        manifest["external_references"] = [
            {"label": "unrelated", "file": {"sha256": "malformed", "path": "/outside"}}
        ]
        manifest_path.write_text(json.dumps(manifest))
        self.assertEqual("scoped_bundle_verified", scope.verify_scope(self.repo)["status"])

    def test_relevant_automatic_observation_dependency_drift_blocks_scope(self):
        relative = "dev/challenges/service/challenge_automatic_intake.py"
        with (self.repo / relative).open("ab") as output:
            output.write(b"# drift\n")
        with self.assertRaisesRegex(scope.ScopeError, "scoped file digest mismatch"):
            scope.verify_scope(self.repo)

    def test_direct_runtime_gate_drift_blocks_scope(self):
        relative = "dev/intake/immutable_runtime_check.py"
        with (self.repo / relative).open("ab") as output:
            output.write(b"# drift\n")
        with self.assertRaisesRegex(scope.ScopeError, "runtime reference drift"):
            scope.verify_scope(self.repo)

    def test_membership_database_path_and_read_only_service_boundary_are_pinned(self):
        relative = "dev/challenges/service/challenge_intake_api.py"
        source = self.repo / relative
        source.write_text(source.read_text().replace(
            "/srv/projects/database/Members.db", "/tmp/untrusted-members.db"))
        manifest_path = self.repo / scope.MANIFEST
        manifest = json.loads(manifest_path.read_text())
        record = next(row for row in manifest["bundle_files"] if row["path"] == relative)
        data = source.read_bytes()
        record["size"] = len(data)
        record["sha256"] = hashlib.sha256(data).hexdigest()
        manifest_path.write_text(json.dumps(manifest))
        with self.assertRaisesRegex(scope.ScopeError, "membership database boundary"):
            scope.verify_scope(self.repo)


if __name__ == "__main__":
    unittest.main()
