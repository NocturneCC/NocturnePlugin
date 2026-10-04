import importlib.util
import json
import unittest
import copy
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "prepare.py"
SPEC = importlib.util.spec_from_file_location("challenge_prepare", SCRIPT)
prepare = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(prepare)


class SourceAdoptionTests(unittest.TestCase):
    def test_manifest_is_complete_deterministic_and_excludes_runtime_data(self):
        first = prepare.build_manifest()
        second = prepare.build_manifest()
        self.assertEqual(first, second)
        self.assertEqual(len(first["bundle_files"]), len({row["path"] for row in first["bundle_files"]}))
        names = {row["path"] for row in first["bundle_files"]}
        self.assertNotIn("db/challenge_config_lkg.json", names)
        self.assertFalse(any(".db" in name or ".env" in name or "__pycache__" in name or name.endswith(".pyc") for name in names))
        self.assertIn("dev/challenges/service/leaderboard_proof_404.json", names)
        self.assertEqual(len([n for n in names if "/boss_icons/" in n]), 90)

    def test_repository_timing_extensions_are_not_claimed_byte_equivalent(self):
        manifest = prepare.build_manifest()
        paths = {
            "dev/challenges/service/challenge_config.py",
            "dev/challenges/tests/python/test_challenge_config.py",
            "dev/challenges/website/challenge-admin-state.js",
            "dev/challenges/website/challenge-admin.html",
            "dev/challenges/website/tests/challenge-admin-state.test.js",
        }
        records = {item["path"]: item for item in manifest["bundle_files"]}
        self.assertTrue(paths <= records.keys())
        for path in paths:
            self.assertEqual("repository_owned_extension", records[path]["source_relationship"])
            self.assertIn("not byte-equivalent", records[path]["extension_reason"])

    def test_automatic_observation_sources_are_repository_owned_extensions(self):
        paths = {
            "dev/challenges/service/challenge_automatic_intake.py",
            "dev/challenges/service/challenge_intake_api.py",
            "dev/challenges/service/leaderboard_challenge_ingest.py",
            "dev/challenges/integration/routes/nocturne-challenge-intake.location.conf",
            "dev/challenges/tests/python/test_challenge_automatic_intake.py",
            "dev/challenges/AUTOMATIC_OBSERVATIONS.md",
        }
        reasons = prepare.REPOSITORY_EXTENSIONS
        self.assertTrue(paths <= reasons.keys())
        files = {p.relative_to(prepare.ROOT).as_posix(): p for p in prepare.expected_files()}
        for path in paths:
            record = prepare.file_record(files[path], prepare.bindings().get(path))
            self.assertEqual("repository_owned_extension", record["source_relationship"])
            self.assertIn("repository-owned", record["extension_reason"])

    def test_version_10_equivalence_evidence_is_digest_only(self):
        evidence = json.loads((SCRIPT.parent / "evidence/config-v10-equivalence.json").read_text())
        self.assertEqual(evidence["version_id"], 10)
        self.assertTrue(evidence["equal"])
        self.assertEqual(evidence["canonical_sha256"], "4886bd680d368567f8b3e25a3111b8c72ea91618e9804e2e88a06a4727278471")
        self.assertNotIn("bosses", evidence)
        self.assertNotIn("document", evidence)

    def test_manifest_verification_is_read_only(self):
        manifest = prepare.build_manifest()
        before = prepare.sha256(prepare.MANIFEST)
        prepare.verify_bundle(manifest)
        self.assertEqual(prepare.sha256(prepare.MANIFEST), before)

    def test_tampered_hash_manifest_fails_closed(self):
        manifest = copy.deepcopy(prepare.build_manifest())
        manifest["bundle_files"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "adopted file drift"):
            prepare.verify_bundle(manifest)

    def test_stylesheet_normalization_is_exactly_trailing_space_removal(self):
        live = Path("/srv/projects/website/styles.css").read_bytes()
        expected = prepare.strip_trailing_horizontal_whitespace(live)
        adopted = prepare.ROOT / "dev/challenges/website/assets/styles.css"
        self.assertEqual(adopted.read_bytes(), expected)

    def test_global_stylesheet_uses_only_documented_whitespace_normalization(self):
        live = Path("/srv/projects/website/nocturne-global.css").read_bytes()
        adopted = prepare.ROOT / "dev/challenges/website/assets/nocturne-global.css"
        self.assertEqual(adopted.read_bytes(), prepare.normalize_known_global_css_whitespace(live))

    def test_admin_proxy_excerpt_matches_pinned_live_line_ranges(self):
        source = Path("/srv/projects/api/admin_app.py").read_text(encoding="utf-8").splitlines()
        selected = []
        for start, end in ((39, 48), (241, 260), (293, 423), (426, 551)):
            selected.extend(source[start - 1:end])
        adopted = (SCRIPT.parent / "integration/routes/admin_app_challenge_routes.fragment.py").read_text(encoding="utf-8")
        excerpt = "\n".join(adopted.splitlines()[2:])
        self.assertEqual(excerpt.rstrip("\n"), "\n".join(selected).rstrip("\n"))


if __name__ == "__main__":
    unittest.main()
