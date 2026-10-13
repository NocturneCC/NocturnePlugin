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

    def test_existing_challenge_schema_without_plugin_catalog_is_supported_predecessor(self):
        automatic.migrate_schema(self.conn)
        from plugin_tracking_catalog import rollback_schema as rollback_catalog
        rollback_catalog(self.conn)
        self.assertEqual("catalog_absent", deploy._schema_state(self.conn, automatic))

    def test_pre_numeric_plugin_tracking_schema_is_supported_and_migrates_additively(self):
        automatic.migrate_schema(self.conn)
        catalog_version = self.conn.execute(
            "SELECT catalog_version_id FROM plugin_tracking_catalog_versions WHERE status='active'"
        ).fetchone()[0]
        self.conn.execute("""INSERT INTO plugin_tracking_observations(
            schema_version,provenance,client_event_id,canonical_payload_sha256,
            reporter_normalized_rsn,activity_key,mode_key,occurred_at,room_time_ms,
            overall_time_ms,selected_timing_scope,selected_duration_ms,observed_group_size,
            catalog_version_id,plugin_version,created_at)
            VALUES(1,'nocturne_runelite_tracking','retained-event','%s','reporter',
            'phosanis_nightmare','phosani_nightmare','2026-10-13T12:00:00Z',NULL,
            358200,'overall',358200,1,?,'0.3.2','2026-10-13T12:00:00Z')""" % ("a" * 64),
            (catalog_version,))
        observation_id = self.conn.execute(
            "SELECT observation_id FROM plugin_tracking_observations WHERE client_event_id='retained-event'"
        ).fetchone()[0]
        self.conn.execute(
            "INSERT INTO plugin_tracking_observation_participants VALUES(?,?,?)",
            (observation_id, 1, "reporter"))
        self.conn.commit()
        self.conn.execute("DROP TABLE plugin_tracking_completion_participants")
        self.conn.execute("DROP TABLE plugin_tracking_completion_observations")
        self.assertEqual("legacy_tracking", deploy._schema_state(self.conn, automatic))
        automatic.migrate_schema(self.conn)
        self.assertEqual("present", deploy._schema_state(self.conn, automatic))
        self.assertEqual(0, self.conn.execute(
            "SELECT COUNT(*) FROM plugin_tracking_completion_observations").fetchone()[0])
        self.assertEqual(("retained-event", 358200), self.conn.execute(
            "SELECT client_event_id,selected_duration_ms FROM plugin_tracking_observations"
        ).fetchone())
        self.assertEqual([(observation_id, 1, "reporter")], self.conn.execute(
            "SELECT observation_id,participant_order,normalized_rsn "
            "FROM plugin_tracking_observation_participants"
        ).fetchall())

    def test_rollback_before_observation_data_removes_only_additive_tables(self):
        self.conn.execute("CREATE TABLE retained_manual_submission(id INTEGER PRIMARY KEY)")
        self.conn.execute("INSERT INTO retained_manual_submission VALUES(1)")
        self.conn.commit()
        automatic.migrate_schema(self.conn)
        automatic.rollback_schema(self.conn)
        self.assertEqual("absent", deploy._schema_state(self.conn, automatic))
        self.assertEqual(0, self.conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'plugin_tracking_%'").fetchone()[0])
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
                               "baseline": baseline, "predecessor_kind": "absent" if index == 0 else "manifest",
                               "allow_absent": baseline is None})
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

    def classify_dispositions(self, dispositions):
        profile = {"uid": 1001, "gid": 33, "mode": "0664", "nlink": 1,
                   "acl_sha256": "fixture-acl", "type": "regular"}
        live = {}
        items = []
        for index, disposition in enumerate(dispositions):
            if index == 0:
                name = "challenge_automatic_intake.py"
                source_relative = "dev/challenges/service/challenge_automatic_intake.py"
            elif index == 1:
                name = "challenge_intake_api.py"
                source_relative = "dev/challenges/service/challenge_intake_api.py"
            else:
                name = f"target-{index}.conf"
                source_relative = f"dev/challenges/fixture/{name}"
            target = self.root / name
            source = self.root / f"desired-{index}"
            after_bytes = f"desired-{index}".encode()
            source.write_bytes(after_bytes)
            after_hash = hashlib.sha256(after_bytes).hexdigest()
            if disposition == "new":
                baseline = None
                predecessor_kind = "absent"
                item = {"allow_absent": True}
            else:
                before_bytes = (after_bytes if disposition == "stable"
                                else f"predecessor-{index}".encode())
                baseline = {**profile, "sha256": hashlib.sha256(before_bytes).hexdigest(),
                            "size": len(before_bytes)}
                predecessor_kind = "manifest"
                item = {}
                if disposition == "predecessor":
                    live[target] = {**baseline, "acl_text": "fixture ACL\n"}
                else:
                    live[target] = {**profile, "sha256": after_hash,
                                    "size": len(after_bytes), "acl_text": "fixture ACL\n"}
            items.append({**item, "source_relative": source_relative, "source": source,
                          "target": target, "after_sha256": after_hash,
                          "after_size": len(after_bytes), "baseline": baseline,
                          "predecessor_kind": predecessor_kind})

        def capture(path):
            if path not in live:
                raise FileNotFoundError(path)
            return live[path]

        support = SimpleNamespace(_safe_parent=lambda _path: None, capture_file=capture)
        return deploy._classify_files(items, support)

    def test_fresh_predecessor_and_already_installed_classification(self):
        self.write_before()
        state, items = deploy._classify_files(self.items, self.support)
        self.assertEqual("upgrade_predecessor", state)
        self.assertEqual(4, len(items))
        self.write_after()
        state, _items = deploy._classify_files(self.items, self.support)
        self.assertEqual("already_installed", state)

    def test_one_byte_or_partial_drift_fails_closed(self):
        self.write_before()
        self.paths[1].write_bytes(b"changed")
        with self.assertRaisesRegex(deploy.DeploymentError, "unsupported live file content or metadata drift"):
            deploy._classify_files(self.items, self.support)
        self.write_after()
        self.paths[2].write_bytes(b"changed")
        with self.assertRaisesRegex(deploy.DeploymentError, "unsupported live file content or metadata drift"):
            deploy._classify_files(self.items, self.support)

    def test_unknown_owner_mode_link_or_acl_metadata_fails_closed(self):
        target = self.paths[1]
        capture = self.support.capture_file
        for key, value in (("uid", 2000), ("gid", 2000), ("mode", "0600"),
                           ("nlink", 2), ("acl_sha256", "unknown-acl")):
            with self.subTest(field=key):
                self.write_before()
                baseline_capture = capture(target)
                changed = {**baseline_capture, key: value}
                self.support.capture_file = lambda path, changed=changed: (
                    changed if path == target else capture(path))
                with self.assertRaisesRegex(
                        deploy.DeploymentError, "unsupported live file content or metadata drift"):
                    deploy._classify_files(self.items, self.support)
        self.support.capture_file = capture

    def test_automatic_module_accepts_only_absent_exact_previous_or_installed(self):
        path = self.paths[0]
        previous = b"previous automatic intake"
        previous_hash = hashlib.sha256(previous).hexdigest()
        profile = {"sha256": previous_hash, "size": len(previous), "uid": 1001,
                   "gid": 33, "mode": "0664", "nlink": 1, "acl_sha256": "acl-api",
                   "type": "regular"}
        item = {**self.items[0], "baseline": profile, "allow_absent": True,
                "predecessor_kind": "absent_or_previous",
                "source_relative": "dev/challenges/service/challenge_automatic_intake.py"}
        others = []
        self.write_after()
        for other in self.items[1:]:
            current_bytes = other["source"].read_bytes()
            current_hash = hashlib.sha256(current_bytes).hexdigest()
            stable = {**other, "baseline": {**other["baseline"],
                                              "sha256": current_hash,
                                              "size": len(current_bytes)},
                      "after_sha256": current_hash,
                      "after_size": len(current_bytes)}
            others.append(stable)

        path.unlink()
        state, _ = deploy._classify_files([item, *others], self.support)
        self.assertEqual("upgrade_predecessor", state)

        path.write_bytes(previous)
        state, _ = deploy._classify_files([item, *others], self.support)
        self.assertEqual("upgrade_predecessor", state)

        path.write_bytes(self.hashes[path][1])
        self.write_after()
        state, _ = deploy._classify_files([item, *others], self.support)
        self.assertEqual("already_installed", state)

        path.write_bytes(b"one-byte-drift")
        with self.assertRaisesRegex(deploy.DeploymentError, "unsupported live file content or metadata drift"):
            deploy._classify_files([item, *others], self.support)

    def test_3cf99e7_installed_predecessor_file_set_is_supported(self):
        """Pin the committed deployment's automatic/API predecessor pair."""
        self.write_before()
        items = [dict(item) for item in self.items]
        auto_path, api_path = items[0]["target"], items[1]["target"]
        api_metadata = {key: items[1]["baseline"].get(key, "regular" if key == "type" else None)
                        for key in ("uid", "gid", "mode", "nlink", "acl_sha256", "type")}
        auto_before = {
            **api_metadata,
            "sha256": "52c6d06f732f64759e148119e3e93c53f4139a97a2d27242dcd8352cd643ede3",
            "size": 28178,
        }
        installed_auto = {
            **api_metadata,
            "sha256": "fe1d1b567eba44996bd2f6e75c9479961430052d3a8270271b5a408626577580",
            "size": 29232,
        }
        items[0].update({
            "baseline": auto_before,
            "predecessor_kind": "absent_or_previous",
            "allow_absent": True,
            "source_relative": "dev/challenges/service/challenge_automatic_intake.py",
            "supported_predecessors": [
                auto_before,
                {**api_metadata,
                 "sha256": "9008d57932b8addea6d17421aa93c7265b9d41603b1d7154cc5e8efd1c53db03",
                 "size": 27618},
                installed_auto,
            ],
        })
        items[1]["baseline"] = {
            **api_metadata,
            "sha256": "9b6ecdbc48dd4fe4c03714d341b6123a32d6f6facbeb5d1e6db71c117da1a6ff",
            "size": 25201,
        }
        items[1]["source_relative"] = "dev/challenges/service/challenge_intake_api.py"
        original_capture = self.support.capture_file

        def capture(path):
            if path == auto_path:
                return {**installed_auto, "acl_text": "fixture ACL\n"}
            if path == api_path:
                return {**items[1]["baseline"], "acl_text": "fixture ACL\n"}
            return original_capture(path)

        self.support.capture_file = capture
        state, observed = deploy._classify_files(items, self.support)
        self.assertEqual("upgrade_predecessor", state)
        self.assertEqual(installed_auto["sha256"], observed[0]["current_sha256"])
        self.assertEqual(items[1]["baseline"]["sha256"], observed[1]["current_sha256"])

    def test_table_driven_aggregate_classification(self):
        cases = [
            ("one stable", ["stable"], "already_installed"),
            ("many stable", ["stable"] * 9, "already_installed"),
            ("new catalog among stable targets", ["stable", "stable", "new"] + ["stable"] * 5,
             "upgrade_predecessor"),
            ("all predecessor", ["predecessor"] * 6, "upgrade_predecessor"),
            ("all installed", ["installed"] * 6, "already_installed"),
        ]
        for label, dispositions, expected in cases:
            with self.subTest(label=label):
                state, _observed = self.classify_dispositions(dispositions)
                self.assertEqual(expected, state)

    def test_table_driven_partial_installations_are_rejected(self):
        for dispositions in (["installed", "predecessor"],
                             ["installed", "new", "stable"]):
            with self.subTest(dispositions=dispositions), self.assertRaisesRegex(
                    deploy.DeploymentError, "mixed target file set"):
                self.classify_dispositions(dispositions)

    def test_exact_dcd_five_target_predecessor_shape(self):
        """The dcd release has three predecessors, one stable target, and one absent new module."""
        api_acl = "1ee74ae96020306a844213e0c3edb809eb71156ea17acfb2629c70b1ad5c6e89"
        leaderboard_acl = "46cfad4815f1a30e253add93c7dca649b84384c52e6a57319eaefc26a540a3d0"
        api_profile = {"uid": 1001, "gid": 33, "mode": "0664", "nlink": 1,
                       "acl_sha256": api_acl, "type": "regular"}
        leaderboard_profile = {"uid": 1001, "gid": 33, "mode": "0755", "nlink": 1,
                               "acl_sha256": leaderboard_acl, "type": "regular"}
        facts = [
            ("dev/challenges/service/challenge_automatic_intake.py",
             "challenge_automatic_intake.py", "52c6d06f732f64759e148119e3e93c53f4139a97a2d27242dcd8352cd643ede3",
             28178, api_profile, "predecessor"),
            ("dev/challenges/service/challenge_intake_api.py", "challenge_intake_api.py",
             "b7f839af4197e867b18149c0d36e820374682881e05e966c916f7fb68f1601f0",
             24095, api_profile, "predecessor"),
            ("dev/challenges/service/plugin_tracking_catalog.py", "plugin_tracking_catalog.py",
             None, None, None, "new"),
            ("dev/challenges/service/leaderboard_challenge_ingest.py", "leaderboard_challenge_ingest.py",
             "a827b62a7d4df316246a5a4ccbd4ba3d2cdada63002c480c8834d02ed78ac53c",
             24209, leaderboard_profile, "stable"),
            ("dev/challenges/integration/routes/nocturne-challenge-intake.location.conf", "routes.conf",
             "fac7b3d526ab5bd56a150612c170493f7aa9a77e774002dd64ae835df82270d8",
             1439, api_profile, "predecessor"),
        ]
        live = {}
        items = []
        for index, (source_relative, name, before_hash, before_size, profile, state) in enumerate(facts):
            target = self.root / name
            source = TREE.parents[1] / source_relative
            desired = source.read_bytes()
            after_hash = hashlib.sha256(desired).hexdigest()
            if state == "new":
                baseline = None
            else:
                baseline = {**profile, "sha256": before_hash, "size": before_size}
                if state == "predecessor":
                    live[target] = {**baseline, "acl_text": "fixture ACL\n"}
                else:
                    # Stable targets retain their exact manifest bytes and metadata.
                    self.assertEqual(before_hash, after_hash)
                    self.assertEqual(before_size, len(desired))
                    live[target] = {**baseline, "acl_text": "fixture ACL\n"}
            items.append({"source_relative": source_relative, "source": source, "target": target,
                          "after_sha256": after_hash, "after_size": len(desired),
                          "baseline": baseline, "predecessor_kind": "absent" if baseline is None else "manifest",
                          "allow_absent": baseline is None})
        # The automatic-intake target's older supported hashes remain explicitly pinned.
        items[0]["predecessor_kind"] = "absent_or_previous"
        items[0]["allow_absent"] = True
        items[0]["supported_predecessors"] = [items[0]["baseline"], {
            **items[0]["baseline"],
            "sha256": "9008d57932b8addea6d17421aa93c7265b9d41603b1d7154cc5e8efd1c53db03",
            "size": 27618,
        }]
        def capture(path):
            if path not in live:
                raise FileNotFoundError(path)
            return live[path]

        support = SimpleNamespace(_safe_parent=lambda _path: None, capture_file=capture)
        state, observed = deploy._classify_files(items, support)
        self.assertEqual("upgrade_predecessor", state)
        self.assertEqual(5, len(observed))
        self.assertIsNone(observed[2]["current_sha256"])

    def test_exact_384_failed_forward_installed_state_is_supported(self):
        """Reproduce the five installed target bytes left by the 384 forward attempt."""
        source_manifest = json.loads((TREE / "source-manifest.json").read_text())
        live_records = {record["path"]: record
                        for record in source_manifest["live_sources"]}
        api_profile = live_records["/srv/projects/nocturne-services/challenge_intake_api.py"]
        leaderboard_profile = live_records[
            "/srv/projects/nocturne-services/leaderboard_challenge_ingest.py"]
        nginx_profile = live_records[
            "/srv/projects/nocturne-services/nginx/nocturne-challenge-intake.location.conf"]
        profiles = {
            "dev/challenges/service/challenge_automatic_intake.py": api_profile,
            "dev/challenges/service/challenge_intake_api.py": api_profile,
            "dev/challenges/service/plugin_tracking_catalog.py": api_profile,
            "dev/challenges/service/leaderboard_challenge_ingest.py": leaderboard_profile,
            "dev/challenges/integration/routes/nocturne-challenge-intake.location.conf": nginx_profile,
        }
        target_paths = {
            relative: (self.root / Path(target).name)
            for relative, target, _kind in deploy.TARGETS
        }
        nginx_patch = mock.patch.object(
            deploy, "NGINX_INCLUDE",
            target_paths["dev/challenges/integration/routes/nocturne-challenge-intake.location.conf"])
        nginx_patch.start()
        self.addCleanup(nginx_patch.stop)

        items = []
        live = {}
        for relative, target, kind in deploy.TARGETS:
            source = TREE.parents[1] / relative
            desired = source.read_bytes()
            expected_after = hashlib.sha256(desired).hexdigest()
            prior_hash, prior_size = deploy.PREVIOUS_DEPLOYMENT_TARGETS[relative]
            if relative in {
                    "dev/challenges/service/challenge_automatic_intake.py",
                    "dev/challenges/service/leaderboard_challenge_ingest.py",
                    "dev/challenges/integration/routes/nocturne-challenge-intake.location.conf"}:
                self.assertEqual(expected_after, prior_hash)
            profile = profiles[relative]
            metadata = {key: profile[key] for key in
                        ("uid", "gid", "mode", "nlink", "acl_sha256")}
            previous = {**metadata, "sha256": prior_hash, "size": prior_size,
                        "type": "regular"}
            original = (None if kind in {"absent", "absent_or_previous"}
                        else live_records[str(target)])
            supported = [previous]
            if isinstance(original, dict) and any(
                    original.get(key) != previous.get(key) for key in
                    ("sha256", "size", "uid", "gid", "mode", "nlink", "acl_sha256", "type")):
                supported.append(original)
            if relative.endswith("challenge_automatic_intake.py"):
                supported.extend({**metadata, "sha256": digest, "size": size, "type": "regular"}
                                 for digest, size in (*deploy.LEGACY_AUTOMATIC_INTAKE_PREDECESSORS,
                                                      (deploy.PREVIOUS_AUTOMATIC_INTAKE_SHA256,
                                                       deploy.PREVIOUS_AUTOMATIC_INTAKE_SIZE)))
            baseline = previous
            item = {
                "source_relative": relative, "source": source,
                "target": target_paths[relative], "after_sha256": expected_after,
                "after_size": len(desired), "baseline": baseline,
                "predecessor_kind": kind,
                "allow_absent": kind in {"absent", "absent_or_previous"},
                "supported_predecessors": supported,
            }
            items.append(item)
            live[item["target"]] = {**previous, "type": "regular",
                                     "acl_text": "fixture ACL\\n"}

        def capture(path):
            if path not in live:
                raise FileNotFoundError(path)
            return live[path]

        support = SimpleNamespace(_safe_parent=lambda _path: None, capture_file=capture)
        state, observed = deploy._classify_files(items, support)
        self.assertEqual("upgrade_predecessor", state)
        self.assertEqual(5, len(observed))
        self.assertEqual(
            deploy.PREVIOUS_DEPLOYMENT_TARGETS[
                "dev/challenges/service/challenge_intake_api.py"][0],
            observed[1]["current_sha256"])
        self.assertEqual(
            deploy.PREVIOUS_DEPLOYMENT_TARGETS[
                "dev/challenges/service/plugin_tracking_catalog.py"][0],
            observed[2]["current_sha256"])

        # Unknown metadata remains a hard failure even with authentic predecessor bytes.
        api_target = target_paths["dev/challenges/service/challenge_intake_api.py"]
        original_capture = support.capture_file
        support.capture_file = lambda path: (
            {**live[path], "gid": 999} if path == api_target else original_capture(path))
        with self.assertRaisesRegex(
                deploy.DeploymentError, "unsupported live file content or metadata drift"):
            deploy._classify_files(items, support)

    def test_installed_prior_release_file_set_can_be_captured_for_backup(self):
        """Exercise a later exact-pinned predecessor through backup capture."""
        self.write_after()
        old_bytes = b"fixture bytes for an exact later automatic-intake predecessor"
        self.paths[0].write_bytes(old_bytes)
        api_profile = self.items[1]["baseline"]
        prior = {
            "sha256": hashlib.sha256(old_bytes).hexdigest(), "size": len(old_bytes),
            "uid": api_profile["uid"], "gid": api_profile["gid"],
            "mode": api_profile["mode"], "nlink": 1,
            "acl_sha256": api_profile["acl_sha256"], "type": "regular",
        }
        older = {**prior,
                 "sha256": "9008d57932b8addea6d17421aa93c7265b9d41603b1d7154cc5e8efd1c53db03",
                 "size": 27618}
        automatic = {**self.items[0], "baseline": prior,
                     "supported_predecessors": [older, prior], "allow_absent": True,
                     "source_relative": "dev/challenges/service/challenge_automatic_intake.py"}
        stable = [automatic]
        for item in self.items[1:]:
            data = item["source"].read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            stable.append({**item, "baseline": {**item["baseline"],
                                                   "sha256": digest, "size": len(data)},
                           "after_sha256": digest, "after_size": len(data)})
        # Capture the old module with the same trusted metadata profile as the
        # installed API; companions are unchanged at their exact target bytes.
        def capture(path):
            if path == self.paths[0]:
                return {**prior, "acl_text": "user::rw-\ngroup::rw-\nother::r--\n"}
            return self.capture(path)
        self.support.capture_file = capture
        self.support._read_regular = lambda path, _limit: path.read_bytes()
        self.support._write_private = lambda path, data: path.write_bytes(data)
        self.support._verify_private_file = lambda _path, _uid: None
        self.support._fsync_dir = lambda _path: None
        txdir = self.root / "transaction"
        txdir.mkdir()
        tx = deploy.Transaction(directory=txdir, commit="a" * 40, items=stable,
                                prior_units={}, prior_nginx={}, database_before={},
                                database_backup=txdir / "database.backup")

        deploy._create_file_backups(tx, self.support)

        self.assertEqual(4, len(tx.file_backups))
        self.assertEqual(prior["sha256"], tx.file_backups[0]["before_sha256"])
        self.assertEqual(old_bytes, Path(tx.file_backups[0]["backup_path"]).read_bytes())

    def test_unsupported_auto_target_drift_is_rejected(self):
        self.write_after()
        profile = {"sha256": "a" * 64, "size": 10, "uid": 1001, "gid": 33,
                   "mode": "0664", "nlink": 1, "acl_sha256": "acl-api", "type": "regular"}
        auto = {**self.items[0], "baseline": profile, "supported_predecessors": [profile],
                "allow_absent": True,
                "source_relative": "dev/challenges/service/challenge_automatic_intake.py"}
        self.support.capture_file = lambda path: (
            {**profile, "sha256": "b" * 64, "size": 11}
            if path == self.paths[0] else self.capture(path))
        stable = []
        for item in self.items[1:]:
            data = item["source"].read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            stable.append({**item, "baseline": {**item["baseline"],
                                                   "sha256": digest, "size": len(data)},
                           "after_sha256": digest, "after_size": len(data)})
        with self.assertRaisesRegex(deploy.DeploymentError, "unsupported live file content or metadata drift"):
            deploy._classify_files([auto, *stable], self.support)

    def test_unchanged_targets_are_idempotently_classified_as_installed(self):
        self.write_after()
        # One installed source may intentionally have identical before/after
        # bytes; classify it as current rather than creating a mixed state.
        item = dict(self.items[1])
        item["after_sha256"] = item["baseline"]["sha256"]
        item["after_size"] = item["baseline"]["size"]
        self.paths[1].write_bytes(self.hashes[self.paths[1]][0])
        state, _ = deploy._classify_files([self.items[0], item, *self.items[2:]], self.support)
        self.assertEqual("already_installed", state)


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
        self.assertEqual("file_set_classification_failed", deploy._diagnostic_category(
            deploy.DeploymentError("preflight failed diagnostic_category=file_set_classification_failed")))


class NginxAndSmokeTests(unittest.TestCase):
    def test_nginx_candidate_has_one_observation_route_and_preserves_approved(self):
        valid = (b"location = /api/challenges/intake/approved {\n}\n"
                 b"location = /api/challenges/intake/observations {\n"
                 b"proxy_pass http://127.0.0.1:5011/api/challenges/intake/observations;\n}\n"
                 b"location = /api/plugin/tracking/config {\n"
                 b"proxy_pass http://127.0.0.1:5011/api/plugin/tracking/config;\n}\n"
                 b"location = /api/plugin/tracking/observations {\n"
                 b"proxy_pass http://127.0.0.1:5011/api/plugin/tracking/observations;\n}\n")
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
        self.assertEqual(4, len(obs))
        self.assertEqual([b"{}", b"{}", b" " * 8193, b"{}"], [x[1]["body"] for x in obs])
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

    def run_apply(self, failure=None, rollback=None):
        config = object(); automatic_module = object()
        verification = failure if callable(failure) else lambda *_a,**_kw: {"ok":True}
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
             mock.patch.object(deploy, "verify_live", side_effect=verification), \
             mock.patch.object(deploy, "_rollback", side_effect=(
                 rollback if rollback is not None else
                 (lambda *_a,**_kw: self.calls.append("rollback")))):
            return deploy.apply(commit="a"*40, plan=self.plan, support=self.support, systemd=self.systemd,
                                runtime_root=self.runtime_root, release=self.release, backup_root=self.root / "backup")

    def test_observation_data_rollback_refusal_preserves_original_apply_failure(self):
        def fail_live_verification(*_args, **_kwargs):
            raise deploy.DeploymentError("private endpoint detail must not be recorded")

        def refuse_rollback(tx, *_args, **_kwargs):
            self.assertEqual("live_verification", tx.failure_phase)
            self.assertEqual("live_verification_failure", tx.failure_category)
            tx.write_record("rollback_refused_observation_data", self.support)
            raise deploy.DeploymentError("rollback refused: automatic observation data exists")

        with self.assertRaisesRegex(
                deploy.DeploymentError,
                "deployment failed phase=live_verification rollback=refused"):
            self.run_apply(failure=fail_live_verification, rollback=refuse_rollback)

        record = json.loads((self.backup_dir / "transaction.json").read_text())
        self.assertEqual(2, record["schema"])
        self.assertEqual("rollback_refused_observation_data", record["state"])
        self.assertEqual("live_verification", record["failure_phase"])
        self.assertEqual("live_verification_failure", record["failure_category"])
        self.assertNotIn("private endpoint detail", json.dumps(record))

    def test_legacy_transaction_schema_remains_supported(self):
        # The rollback reader continues to accept exact v1 records; v2 adds
        # only the two bounded failure-context fields.
        legacy = {"schema":1, "target_commit":"a"*40, "state":"files_installed",
                  "database_backup":"b", "database_backup_sha256":"c",
                  "database_before":{}, "prior_units":{}, "prior_nginx":{}, "files":[],
                  "schema_migrated":False, "nginx_reloaded":False, "restoration":{}}
        self.assertFalse(deploy._validate_transaction_record_schema(legacy))
        current = {**legacy, "schema":2, "failure_phase":"schema_migration",
                   "failure_category":"schema_migration_failure"}
        self.assertTrue(deploy._validate_transaction_record_schema(current))
        with self.assertRaisesRegex(deploy.DeploymentError, "failure context rejected"):
            deploy._validate_transaction_record_schema(
                {**current, "failure_category":"raw exception text"})
        with self.assertRaisesRegex(deploy.DeploymentError, "record schema rejected"):
            deploy._validate_transaction_record_schema({**current, "unrecognized":"field"})

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

    def test_existing_observation_refuses_database_rollback_but_restores_units_and_timers(self):
        counts = {name: 0 for name in (*deploy.SCHEMA_TABLES, *deploy.TRACKING_TABLES)}
        counts["plugin_tracking_completion_observations"] = 1
        self.support._systemd_show = lambda unit: self.units[unit]
        active_timer = deploy.TIMERS[0]
        self.units[active_timer] = {"Id": active_timer, "LoadState": "loaded",
                                    "ActiveState": "active", "SubState": "waiting", "MainPID": "0"}
        prior_units = {name: dict(state) for name, state in self.units.items()}
        tx = deploy.Transaction(directory=self.backup_dir, commit="a" * 40, items=self.items,
                                prior_units=prior_units, prior_nginx=self.nginx,
                                database_before={"database_file_meta": {}}, database_backup=self.backup)
        records = []
        tx.write_record = lambda state, _support: records.append((state, dict(tx.restoration)))
        restored = []
        self.support._restore_units = lambda *_: restored.append("services_and_timers")
        with mock.patch.object(deploy, "_observation_counts", return_value=counts), \
             mock.patch.object(deploy, "_verify_restored_units", side_effect=lambda *_: restored.append("verified")), \
             mock.patch.object(deploy, "_restore_target_files", side_effect=AssertionError("accepted data must be retained")), \
             mock.patch.object(deploy, "_atomic_database_migration", side_effect=AssertionError("database must be retained")):
            with self.assertRaisesRegex(deploy.DeploymentError, "rollback refused: automatic observation data exists"):
                deploy._rollback(tx, self.systemd, self.support, self.release, object(), database=self.root / "db")
        self.assertEqual(["services_and_timers", "verified"], restored)
        self.assertEqual("restored-to-prestate", tx.restoration["services"])
        self.assertEqual("restored-to-prestate", tx.restoration["timers"])
        for timer in deploy.TIMERS:
            self.assertEqual(prior_units[timer]["ActiveState"], self.systemd.show(timer)["ActiveState"])
            self.assertEqual(prior_units[timer]["SubState"], self.systemd.show(timer)["SubState"])
        self.assertEqual("rollback_refused_observation_data", records[0][0])

    def test_refused_database_rollback_records_restoration_failure_after_attempt(self):
        counts = {name: 0 for name in (*deploy.SCHEMA_TABLES, *deploy.TRACKING_TABLES)}
        counts["plugin_tracking_observations"] = 1
        self.support._systemd_show = lambda unit: self.units[unit]
        tx = deploy.Transaction(directory=self.backup_dir, commit="a" * 40, items=self.items,
                                prior_units=self.units, prior_nginx=self.nginx,
                                database_before={"database_file_meta": {}}, database_backup=self.backup)
        records = []
        tx.write_record = lambda state, _support: records.append((state, dict(tx.restoration)))
        attempts = []
        def failed_restore(*_):
            attempts.append("restore_attempted")
            raise deploy.DeploymentError("fixture failure")
        self.support._restore_units = failed_restore
        with mock.patch.object(deploy, "_observation_counts", return_value=counts), \
             mock.patch.object(deploy, "_verify_restored_units", side_effect=AssertionError("verify after failed restore")):
            with self.assertRaisesRegex(deploy.DeploymentError, "restoration failed"):
                deploy._rollback(tx, self.systemd, self.support, self.release, object(), database=self.root / "db")
        self.assertEqual(["restore_attempted"], attempts)
        self.assertEqual("rollback_failed", records[0][0])
        self.assertEqual("restore_failed", tx.restoration["services"])
        self.assertEqual("restore_failed", tx.restoration["timers"])

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
