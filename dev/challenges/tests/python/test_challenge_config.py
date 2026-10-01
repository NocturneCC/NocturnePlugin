from __future__ import annotations

import copy
import os
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import challenge_config
from challenge_config import (
    config_document, config_document_diff, create_draft, draft_diff, draft_document, evaluation_catalog,
    migrate_schema, normalize_draft_document, publish_draft, save_draft,
    validate_document, validate_draft, apply_linked_leaderboard_order,
)


SOURCE = Path(os.environ.get(
    "PHASE4A_TEST_DB",
    "/srv/projects/backups/database/Challenges/Challenges.db.before-phase4a1-20260929T025335Z",
))


class ChallengeConfigTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "Challenges.db"
        shutil.copy2(SOURCE,self.path)
        self.conn=sqlite3.connect(self.path)
        self.conn.row_factory=sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        migrate_schema(self.conn)
        self.base_id = int(self.conn.execute(
            "SELECT config_version_id FROM challenge_config_versions WHERE status='active'"
        ).fetchone()[0])

    def tearDown(self):
        self.conn.close(); self.temp.cleanup()

    def test_initial_snapshot_has_all_bosses_and_tiers(self):
        doc=config_document(self.conn)
        self.assertEqual(15,len(doc["bosses"]))
        self.assertEqual(75,sum(len(b["tiers"]) for b in doc["bosses"]))
        self.assertEqual(17,len(doc["leaderboard_modes"]))
        self.assertEqual([],validate_document(doc))
        self.assertEqual("gauntlet",evaluation_catalog(self.conn)["aliases"]["gauntlet"])

    def test_leaderboard_modes_support_challenge_backed_and_leaderboard_only(self):
        document=config_document(self.conn)
        modes={item["mode_key"]:item for item in document["leaderboard_modes"]}
        self.assertEqual("gauntlet",modes["corrupted_gauntlet"]["boss_key"])
        self.assertEqual("cm_3",modes["cox_cm_trio"]["boss_key"])
        self.assertEqual("tob_3",modes["tob_trio"]["boss_key"])
        self.assertTrue(modes["corrupted_gauntlet"]["inherit_boss_icon"])
        self.assertIsNone(modes["tob_solo"]["boss_key"])
        self.assertIsNone(modes["tob_4man"]["boss_key"])
        self.assertFalse(modes["tob_solo"]["inherit_boss_icon"])
        self.assertEqual((4,4),(modes["tob_4man"]["party_size_min"],modes["tob_4man"]["party_size_max"]))

    def test_linked_mode_order_follows_boss_and_preserves_same_boss_order(self):
        bosses = [
            {"boss_key": "jad", "display_order": 20},
            {"boss_key": "zuk", "display_order": 10},
        ]
        base = {
            "content_key": "test", "display_name": "Test", "active": True,
            "metric_type": "time", "comparison_direction": "lower",
            "metric_unit": "milliseconds", "party_size_min": 1,
            "party_size_max": 1, "top_n": 3, "inherit_boss_icon": True,
            "icon_url": None, "aliases": [], "publication_group_key": "solo_pvm",
            "publication_group_name": "Solo PvM", "publication_group_order": 40,
            "group_icon_url": None, "custom_order_override": False,
        }
        modes = [
            {**base, "mode_key": "jad", "boss_key": "jad", "display_order": 10},
            {**base, "mode_key": "zuk_main", "boss_key": "zuk", "display_order": 30},
            {**base, "mode_key": "zuk_alt", "boss_key": "zuk", "display_order": 20},
        ]
        ordered = apply_linked_leaderboard_order(bosses, modes)
        self.assertEqual(["zuk_alt", "zuk_main", "jad"], [item["mode_key"] for item in ordered])
        self.assertEqual([10, 20, 30], [item["effective_display_order"] for item in ordered])
        self.assertEqual([20, 30, 10], [item["display_order"] for item in ordered])

    def test_custom_and_leaderboard_only_modes_keep_configured_order(self):
        bosses = [{"boss_key": "jad", "display_order": 100}]
        base = {
            "content_key": "test", "display_name": "Test", "active": True,
            "metric_type": "time", "comparison_direction": "lower",
            "metric_unit": "milliseconds", "party_size_min": 1,
            "party_size_max": 1, "top_n": 3, "inherit_boss_icon": True,
            "icon_url": None, "aliases": [], "publication_group_key": "solo_pvm",
            "publication_group_name": "Solo PvM", "publication_group_order": 40,
            "group_icon_url": None,
        }
        modes = [
            {**base, "mode_key": "linked", "boss_key": "jad", "display_order": 10,
             "custom_order_override": False},
            {**base, "mode_key": "custom", "boss_key": "jad", "display_order": 5,
             "custom_order_override": True},
            {**base, "mode_key": "only", "boss_key": None, "display_order": 7,
             "inherit_boss_icon": False, "custom_order_override": True},
        ]
        ordered = apply_linked_leaderboard_order(bosses, modes)
        self.assertEqual(["custom", "only", "linked"], [item["mode_key"] for item in ordered])
        self.assertFalse(next(item for item in ordered if item["mode_key"] == "only")["custom_order_override"])

    def test_leaderboard_validation_rejects_duplicate_missing_range_and_comparison(self):
        document=config_document(self.conn)
        document["leaderboard_modes"].append(copy.deepcopy(document["leaderboard_modes"][0]))
        document["leaderboard_modes"][1]["boss_key"]="missing_boss"
        document["leaderboard_modes"][2]["party_size_min"]=6
        document["leaderboard_modes"][2]["party_size_max"]=2
        document["leaderboard_modes"][3]["comparison_direction"]="higher"
        codes={item["code"] for item in validate_document(document)}
        self.assertIn("duplicate_mode_key",codes)
        self.assertIn("missing_boss_reference",codes)
        self.assertIn("invalid_party_range",codes)
        self.assertIn("invalid_mode_comparison",codes)

    def test_leaderboard_publish_is_versioned_and_old_snapshot_immutable(self):
        draft=create_draft(self.conn,"tester")
        document=copy.deepcopy(draft["config"])
        mode=next(item for item in document["leaderboard_modes"] if item["mode_key"]=="cox_cm_trio")
        mode["top_n"]=5
        saved=save_draft(self.conn,draft["draft_id"],document,"tester",draft["revision"])
        validated=validate_draft(self.conn,draft["draft_id"],"tester",saved["revision"])
        self.assertTrue(validated["valid"],validated["errors"])
        published=publish_draft(self.conn,draft["draft_id"],"tester",confirmed=True,expected_revision=saved["revision"])
        changed=next(item for item in published["leaderboard_modes"] if item["mode_key"]=="cox_cm_trio")
        self.assertEqual(5,changed["top_n"])
        old=next(item for item in config_document(self.conn,self.base_id)["leaderboard_modes"] if item["mode_key"]=="cox_cm_trio")
        self.assertEqual(3,old["top_n"])
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE leaderboard_mode_versions SET top_n=9 WHERE config_version_id=? AND mode_key='cox_cm_trio'",
                (self.base_id,),
            )
        self.conn.rollback()

    def test_leaderboard_only_mode_never_changes_ascendant_boss_count(self):
        before=config_document(self.conn)
        active_before=sum(bool(item["active"]) for item in before["bosses"])
        draft=create_draft(self.conn,"tester")
        document=copy.deepcopy(draft["config"])
        extra=copy.deepcopy(document["leaderboard_modes"][0])
        extra.update({
            "mode_key":"leaderboard_only_test","boss_key":None,
            "content_key":"leaderboard_only_test","display_name":"Leaderboard Only Test",
            "display_order":9990,"inherit_boss_icon":False,
            "icon_url":"/media/boss_icons/Theatre_of_blood.png",
            "aliases":["Leaderboard Only Test"],
        })
        document["leaderboard_modes"].append(extra)
        saved=save_draft(self.conn,draft["draft_id"],document,"tester",draft["revision"])
        validated=validate_draft(self.conn,draft["draft_id"],"tester",saved["revision"])
        self.assertTrue(validated["valid"],validated["errors"])
        published=publish_draft(self.conn,draft["draft_id"],"tester",confirmed=True,expected_revision=saved["revision"])
        self.assertEqual(active_before,sum(bool(item["active"]) for item in published["bosses"]))
        ascendant=next(item for item in published["system_tiers"] if item["system_tier_key"]=="ascendant")
        self.assertTrue(ascendant["require_all_active_challenges"])

    def test_draft_save_validate_publish_and_immutability(self):
        draft=create_draft(self.conn,"tester")
        doc=copy.deepcopy(draft["config"])
        gauntlet=next(b for b in doc["bosses"] if b["boss_key"]=="gauntlet")
        gauntlet["description"]="Published from test"
        saved=save_draft(self.conn,draft["draft_id"],doc,"tester",draft["revision"])
        result=validate_draft(self.conn,draft["draft_id"],"tester")
        self.assertTrue(result["valid"])
        published=publish_draft(
            self.conn,draft["draft_id"],"tester",confirmed=True,
            expected_revision=result["draft"]["revision"],
        )
        self.assertEqual(self.base_id+1,published["version_id"])
        self.assertEqual(1,self.conn.execute("SELECT COUNT(*) FROM challenge_config_versions WHERE status='active'").fetchone()[0])
        self.assertEqual("retired",self.conn.execute("SELECT status FROM challenge_config_versions WHERE config_version_id=?",(self.base_id,)).fetchone()[0])
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE challenge_tiers SET requirement_value=0 WHERE config_version_id=?",(published["version_id"],))
        self.conn.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("UPDATE challenge_config_versions SET config_json='{}' WHERE config_version_id=?",(published["version_id"],))
        self.conn.rollback()
        self.assertEqual("published",draft_document(self.conn,draft["draft_id"])["state"])

    def test_deactivate_reactivate_is_versioned_not_deleted(self):
        draft=create_draft(self.conn,"tester"); doc=draft["config"]
        before=self.conn.execute("SELECT COUNT(*) FROM challenge_config_bosses WHERE boss_key='jad'").fetchone()[0]
        boss=next(b for b in doc["bosses"] if b["boss_key"]=="jad")
        boss["active"]=False; boss["submission_enabled"]=False
        saved=save_draft(self.conn,draft["draft_id"],doc,"tester",draft["revision"])
        result=validate_draft(self.conn,draft["draft_id"],"tester",saved["revision"])
        published=publish_draft(self.conn,draft["draft_id"],"tester",confirmed=True,expected_revision=result["draft"]["revision"])
        self.assertFalse(next(b for b in published["bosses"] if b["boss_key"]=="jad")["active"])
        self.assertEqual(before+1,self.conn.execute("SELECT COUNT(*) FROM challenge_config_bosses WHERE boss_key='jad'").fetchone()[0])

    def test_alias_conflict_and_discord_metadata_validation(self):
        doc=config_document(self.conn)
        doc["bosses"][0]["aliases"].append("Colosseum")
        doc["bosses"][1]["discord_label"]="x"*101
        codes={e["code"] for e in validate_document(doc)}
        self.assertIn("alias_conflict",codes); self.assertIn("invalid_discord_label",codes)

    def test_time_and_numeric_order_validation(self):
        doc=config_document(self.conn)
        gauntlet=next(b for b in doc["bosses"] if b["boss_key"]=="gauntlet")
        gauntlet["tiers"][2]["threshold_display"]="0:09:00"
        delve=next(b for b in doc["bosses"] if b["boss_key"]=="delve")
        delve["tiers"][2]["threshold"]=5
        errors=validate_document(doc)
        self.assertGreaterEqual(sum(e["code"]=="invalid_tier_order" for e in errors),2)

    def test_time_threshold_accepts_admin_display_format_and_normalizes_storage(self):
        value, display = challenge_config.parse_time_threshold("17:00.00")
        self.assertEqual(1020000, value)
        self.assertEqual("0:17:00.000", display)
        self.assertEqual("17:00.00", challenge_config.format_time_threshold(value, "MM:SS.xx"))
        self.assertEqual("00:17:00.00", challenge_config.format_time_threshold(value, "HH:MM:SS.xx"))

        doc = config_document(self.conn)
        gauntlet = next(b for b in doc["bosses"] if b["boss_key"] == "gauntlet")
        gauntlet["time_input_format"] = "MM:SS.xx"
        gauntlet["tiers"][1]["threshold_display"] = "17:00.00"
        normalized = normalize_draft_document(doc)
        tier = next(b for b in normalized["bosses"] if b["boss_key"] == "gauntlet")["tiers"][1]
        self.assertEqual(1020000, tier["threshold"])
        self.assertEqual("17:00.00", tier["threshold_display"])

    def test_completion_configuration(self):
        doc=config_document(self.conn)
        boss=doc["bosses"][0]; boss["metric_type"]="completion"; boss["comparison_direction"]="complete"; boss["time_input_format"]=None
        for tier in boss["tiers"]:
            tier.update({"threshold":1,"threshold_display":"Completion","metric_type":"completion","operator":"complete","unit":"boolean"})
        self.assertFalse(any(e["path"].startswith("bosses[0]") for e in validate_document(doc)))

    def test_publish_requires_explicit_confirmation(self):
        draft=create_draft(self.conn,"tester")
        with self.assertRaisesRegex(ValueError,"publish_confirmation_required"):
            publish_draft(self.conn,draft["draft_id"],"tester",confirmed=False)

    def test_publish_requires_current_validated_revision(self):
        draft=create_draft(self.conn,"tester")
        with self.assertRaisesRegex(RuntimeError,"draft_revision_not_validated"):
            publish_draft(
                self.conn,draft["draft_id"],"tester",confirmed=True,
                expected_revision=draft["revision"],
            )
        doc=copy.deepcopy(draft["config"])
        doc["bosses"][0]["description"]="revision test"
        saved=save_draft(self.conn,draft["draft_id"],doc,"tester",draft["revision"])
        validated=validate_draft(self.conn,draft["draft_id"],"tester",saved["revision"])
        self.assertEqual(saved["revision"],validated["draft"]["validated_revision"])
        with self.assertRaisesRegex(RuntimeError,"draft_revision_conflict"):
            publish_draft(
                self.conn,draft["draft_id"],"tester",confirmed=True,
                expected_revision=draft["revision"],
            )

    def test_noop_publish_is_rejected(self):
        draft=create_draft(self.conn,"tester")
        validated=validate_draft(self.conn,draft["draft_id"],"tester",draft["revision"])
        first=publish_draft(
            self.conn,draft["draft_id"],"tester",confirmed=True,
            expected_revision=validated["draft"]["revision"],
        )
        self.assertEqual(17,len(first["leaderboard_modes"]))
        draft=create_draft(self.conn,"tester")
        validated=validate_draft(self.conn,draft["draft_id"],"tester",draft["revision"])
        with self.assertRaisesRegex(RuntimeError,"no_changes_to_publish"):
            publish_draft(
                self.conn,draft["draft_id"],"tester",confirmed=True,
                expected_revision=validated["draft"]["revision"],
            )
        self.assertEqual(first["version_id"],self.conn.execute(
            "SELECT config_version_id FROM challenge_config_versions WHERE status='active'"
        ).fetchone()[0])

    def test_first_leaderboard_publication_diff_contains_no_challenge_changes(self):
        draft=create_draft(self.conn,"tester")
        result=draft_diff(self.conn,draft["draft_id"],draft["revision"])
        self.assertTrue(result["has_changes"])
        self.assertEqual([],result["bosses"])
        self.assertEqual([],result["system_tiers"])
        self.assertEqual(17,len(result["leaderboard_modes"]))
        self.assertEqual(17,result["summary"]["leaderboard_modes_added"])

    def _with_temporary_boss(self, draft, key="draft_only"):
        document=copy.deepcopy(draft["config"])
        temporary=copy.deepcopy(document["bosses"][0])
        temporary.update({
            "boss_key":key,"display_name":"Draft Only","aliases":["Draft Only"],
            "display_order":9990,"active":False,"submission_enabled":False,
        })
        document["bosses"].append(temporary)
        return save_draft(
            self.conn,draft["draft_id"],document,"tester",draft["revision"]
        )

    def test_unpublished_boss_removal_allowed(self):
        draft=create_draft(self.conn,"tester")
        saved=self._with_temporary_boss(draft)
        document=copy.deepcopy(saved["config"])
        document["bosses"]=[b for b in document["bosses"] if b["boss_key"]!="draft_only"]
        removed=save_draft(self.conn,draft["draft_id"],document,"tester",saved["revision"])
        self.assertNotIn("draft_only",{b["boss_key"] for b in removed["config"]["bosses"]})

    def test_published_boss_removal_rejected(self):
        draft=create_draft(self.conn,"tester")
        document=copy.deepcopy(draft["config"])
        document["bosses"]=[b for b in document["bosses"] if b["boss_key"]!="gauntlet"]
        with self.assertRaisesRegex(RuntimeError,"published_boss_cannot_be_removed"):
            save_draft(self.conn,draft["draft_id"],document,"tester",draft["revision"])

    def test_historical_unpublished_boss_removal_rejected(self):
        draft=create_draft(self.conn,"tester")
        saved=self._with_temporary_boss(draft,"historical_draft")
        submission_id=int(self.conn.execute("SELECT MIN(submission_id) FROM challenge_submissions").fetchone()[0])
        self.conn.execute(
            """INSERT INTO challenge_member_bests
               (subject_key,member_id,boss_key,metric_type,metric_value,metric_unit,
                metric_display,submission_id,evaluated_config_version_id,calculated_at)
               VALUES('test:historical',NULL,'historical_draft','completion',1,'boolean',
                      'Completion',?,?,CURRENT_TIMESTAMP)""",
            (submission_id,self.base_id),
        )
        self.conn.commit()
        document=copy.deepcopy(saved["config"])
        document["bosses"]=[b for b in document["bosses"] if b["boss_key"]!="historical_draft"]
        with self.assertRaisesRegex(RuntimeError,"historical_boss_cannot_be_removed"):
            save_draft(self.conn,draft["draft_id"],document,"tester",saved["revision"])

    def test_metric_type_transitions_normalize_hidden_semantics(self):
        draft=create_draft(self.conn,"tester")
        document=copy.deepcopy(draft["config"])
        gauntlet=next(b for b in document["bosses"] if b["boss_key"]=="gauntlet")
        gauntlet["metric_type"]="numeric"
        numeric=save_draft(self.conn,draft["draft_id"],document,"tester",draft["revision"])
        boss=next(b for b in numeric["config"]["bosses"] if b["boss_key"]=="gauntlet")
        self.assertEqual([None,None,None,None],[t["threshold"] for t in boss["tiers"][1:]])
        self.assertTrue(all(t["operator"]=="gte" and t["unit"]=="waves" for t in boss["tiers"][1:]))
        self.assertIn("invalid_numeric_threshold", {
            error["code"] for error in validate_document(numeric["config"])
        })
        document=copy.deepcopy(numeric["config"])
        boss=next(b for b in document["bosses"] if b["boss_key"]=="gauntlet")
        boss["metric_type"]="completion"
        completion=save_draft(self.conn,draft["draft_id"],document,"tester",numeric["revision"])
        boss=next(b for b in completion["config"]["bosses"] if b["boss_key"]=="gauntlet")
        self.assertTrue(all(t["threshold"]==1 and t["operator"]=="complete" and t["unit"]=="boolean" for t in boss["tiers"]))

    def test_invalid_tier_semantics_rejected_by_validation(self):
        document=config_document(self.conn)
        delve=next(b for b in document["bosses"] if b["boss_key"]=="delve")
        delve["tiers"][1]["operator"]="lte"
        codes={error["code"] for error in validate_document(document)}
        self.assertIn("invalid_operator",codes)

    def test_representation_normalization_is_stable(self):
        document=config_document(self.conn)
        once=normalize_draft_document(document)
        twice=normalize_draft_document(once)
        self.assertEqual(once,twice)
        for boss in once["bosses"]:
            normalized=["".join(ch for ch in alias.casefold() if ch.isalnum()) for alias in boss["aliases"]]
            self.assertEqual(len(normalized),len(set(normalized)))

    def test_submission_modes_and_time_input_formats_validate(self):
        document=config_document(self.conn)
        gauntlet=next(b for b in document["bosses"] if b["boss_key"]=="gauntlet")
        gauntlet.update({
            "submission_mode":"solo","supports_groups":False,
            "min_party_size":1,"time_input_format":"MM:SS.xx",
        })
        self.assertEqual([],validate_document(document))
        gauntlet.update({"submission_mode":"group","supports_groups":True,"min_party_size":1})
        self.assertIn("invalid_submission_semantics",{e["code"] for e in validate_document(document)})
        gauntlet.update({"min_party_size":2,"time_input_format":"seconds"})
        self.assertIn("invalid_time_input_format",{e["code"] for e in validate_document(document)})

    def test_publish_preserves_submission_metadata_in_immutable_snapshot(self):
        draft=create_draft(self.conn,"tester")
        document=copy.deepcopy(draft["config"])
        gauntlet=next(b for b in document["bosses"] if b["boss_key"]=="gauntlet")
        gauntlet.update({
            "display_name":"Corrupted Gauntlet","discord_label":"Corrupted Gauntlet",
            "aliases":["Gauntlet","CG"],"submission_mode":"solo",
            "supports_groups":False,"min_party_size":1,
            "time_input_format":"MM:SS.xx",
        })
        saved=save_draft(self.conn,draft["draft_id"],document,"tester",draft["revision"])
        validated=validate_draft(self.conn,draft["draft_id"],"tester",saved["revision"])
        self.assertTrue(validated["valid"],validated["errors"])
        published=publish_draft(self.conn,draft["draft_id"],"tester",confirmed=True,expected_revision=saved["revision"])
        boss=next(b for b in published["bosses"] if b["boss_key"]=="gauntlet")
        self.assertEqual("solo",boss["submission_mode"])
        self.assertEqual("MM:SS.xx",boss["time_input_format"])
        self.assertFalse(boss["supports_groups"])

    def test_png_artwork_is_versioned_and_invalid_urls_are_rejected(self):
        original=config_document(self.conn)
        original_boss=next(b for b in original["bosses"] if b["boss_key"]=="gauntlet")
        self.assertTrue(
            original_boss["icon_url"] == "/media/boss_icons/gauntlet.png"
            or original_boss["icon_url"].startswith("https://cdn.discordapp.com/emojis/"),
            original_boss["icon_url"],
        )
        draft=create_draft(self.conn,"tester")
        document=copy.deepcopy(draft["config"])
        boss=next(b for b in document["bosses"] if b["boss_key"]=="gauntlet")
        boss["icon_url"]="/media/challenge-bosses/gauntlet-0123456789abcdef01234567.png"
        saved=save_draft(self.conn,draft["draft_id"],document,"tester",draft["revision"])
        validated=validate_draft(self.conn,draft["draft_id"],"tester",saved["revision"])
        self.assertTrue(validated["valid"],validated["errors"])
        published=publish_draft(
            self.conn,draft["draft_id"],"tester",confirmed=True,
            expected_revision=saved["revision"],
        )
        new_boss=next(b for b in published["bosses"] if b["boss_key"]=="gauntlet")
        self.assertEqual(boss["icon_url"],new_boss["icon_url"])
        old_boss=next(b for b in config_document(self.conn,self.base_id)["bosses"] if b["boss_key"]=="gauntlet")
        self.assertEqual(original_boss["icon_url"],old_boss["icon_url"])

        invalid=copy.deepcopy(published)
        next(b for b in invalid["bosses"] if b["boss_key"]=="gauntlet")["icon_url"]="/media/challenge-bosses/gauntlet.jpg"
        self.assertIn("invalid_icon_url",{error["code"] for error in validate_document(invalid)})

    def test_canonical_local_boss_artwork_precedes_discord_fallback(self):
        icon_root=Path(self.temp.name)/"boss_icons"
        icon_root.mkdir()
        (icon_root/"gauntlet.png").write_bytes(b"fixture")
        original_root=challenge_config.BOSS_ICON_DIR
        challenge_config.BOSS_ICON_DIR=icon_root
        try:
            boss=next(
                item for item in config_document(self.conn)["bosses"]
                if item["boss_key"]=="gauntlet"
            )
        finally:
            challenge_config.BOSS_ICON_DIR=original_root
        self.assertEqual("/media/boss_icons/gauntlet.png",boss["icon_url"])
        self.assertEqual([],validate_document(config_document(self.conn)))

    def test_reusable_catalog_icon_may_differ_from_boss_key_but_must_exist(self):
        icon_root=Path(self.temp.name)/"boss_icons"
        icon_root.mkdir()
        (icon_root/"Theatre_of_blood.png").write_bytes(b"fixture")
        original_root=challenge_config.BOSS_ICON_DIR
        challenge_config.BOSS_ICON_DIR=icon_root
        try:
            document=config_document(self.conn)
            boss=next(item for item in document["bosses"] if item["boss_key"]=="gauntlet")
            boss["icon_url"]="/media/boss_icons/Theatre_of_blood.png"
            self.assertEqual([],validate_document(document))
            boss["icon_url"]="/media/boss_icons/missing.png"
            self.assertIn("invalid_icon_url",{item["code"] for item in validate_document(document)})
            boss["icon_url"]="/media/boss_icons/..%2Fsecret.png"
            self.assertIn("invalid_icon_url",{item["code"] for item in validate_document(document)})
        finally:
            challenge_config.BOSS_ICON_DIR=original_root

    def test_saved_draft_diff_reports_boss_alias_and_tier_changes(self):
        draft=create_draft(self.conn,"tester")
        document=copy.deepcopy(draft["config"])
        boss=next(b for b in document["bosses"] if b["boss_key"]=="gauntlet")
        boss["display_name"]="Gauntlet Configurator Test"
        boss["aliases"].append("GCT")
        boss["tiers"][2]["tier"]="Gilded"
        boss["tiers"][2]["points"]=31
        saved=save_draft(self.conn,draft["draft_id"],document,"tester",draft["revision"])
        result=draft_diff(self.conn,draft["draft_id"],saved["revision"])
        self.assertTrue(result["has_changes"])
        change=next(item for item in result["bosses"] if item["boss_key"]=="gauntlet")
        self.assertEqual("modified",change["change_type"])
        self.assertIn("display_name",{item["field"] for item in change["fields"]})
        tier=next(item for item in change["tiers"] if item["tier_key"]=="gold")
        self.assertEqual({"tier","points"},{item["field"] for item in tier["fields"]})
        self.assertEqual(1,result["summary"]["tier_changes"])

    def test_config_diff_is_empty_for_normalized_identical_documents(self):
        document=config_document(self.conn)
        result=config_document_diff(document,copy.deepcopy(document))
        self.assertFalse(result["has_changes"])
        self.assertEqual([],result["bosses"])

    def test_tier_names_ranks_and_comparison_direction_validate(self):
        document=config_document(self.conn)
        boss=next(b for b in document["bosses"] if b["boss_key"]=="gauntlet")
        boss["comparison_direction"]="higher"
        boss["tiers"][1]["rank"]=7
        boss["tiers"][2]["tier"]=""
        codes={error["code"] for error in validate_document(document)}
        self.assertIn("invalid_comparison_direction",codes)
        self.assertIn("invalid_rank",codes)
        self.assertIn("invalid_tier_name",codes)


if __name__ == "__main__": unittest.main()
