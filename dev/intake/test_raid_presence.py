import tempfile
import unittest
import sqlite3
from pathlib import Path
from uuid import uuid4
from raid_presence import process

class RaidPresenceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.db = Path(self.temp.name) / "presence.db"
        self.now = 2_000_000_000
    def tearDown(self): self.temp.cleanup()
    def payload(self, rsn="One", holder=7, epoch=None, state="completion", personal=500, team=10000,
                size=3, started=None, observed=None, mode="NORMAL_GROUP"):
        started = self.now - 100 if started is None else started; observed = self.now if observed is None else observed
        complete = state in {"completion", "reward_observed"}
        return {"presence_version":1,"checkin_id":str(uuid4()),"rsn":rsn,"raid_type":"COX",
                "state":state,"world":420,"party_group_holder":holder,"raid_epoch":epoch or str(uuid4()),
                "raid_started_at":started,"observed_at":observed,"max_scale":size,
                "final_party_size":size if complete else None,"final_personal_points":personal if complete else None,
                "final_team_points":team if complete else None,
                "contribution_basis_points":personal*10000//team if complete else None,
                "proposed_scoring_mode":mode,"completion_at":observed if complete else None,
                "reward_observed_at":observed if state=="reward_observed" else None}
    def send(self,p): return process(self.db,p,lambda rsn: 2 if rsn in {"one","two","three"} else None,self.now)
    def test_three_clients_complete_consistently_and_exact_five_percent_passes(self):
        results=[self.send(self.payload(rsn,personal=500)) for rsn in ("One","Two","Three")]
        self.assertTrue(results[-1]["group_qualified"]); self.assertEqual(3,results[-1]["verified"])
        self.assertEqual((3, 3, 1), (results[-1]["participant_account_count"],
            results[-1]["verified_account_count"], results[-1]["distinct_eligible_member_count"]))
        self.assertEqual(1, results[-1]["proposed_recipient_count"])
    def test_missing_client_and_just_below_do_not_qualify(self):
        self.send(self.payload("One",personal=499)); result=self.send(self.payload("Two"))
        self.assertFalse(result["group_qualified"]); self.assertEqual(2,result["verified"])
    def test_duplicate_replay_and_same_world_separation(self):
        p=self.payload(); first=self.send(p); self.assertEqual("duplicate",self.send(p)["status"])
        changed=dict(p,world=421)
        with self.assertRaises(ValueError): self.send(changed)
        other=self.send(self.payload("Two",holder=8)); self.assertNotEqual(first["fingerprint"],other["fingerprint"])
    def test_stale_and_adjacent_epoch_fail(self):
        with self.assertRaises(ValueError): self.send(self.payload(observed=self.now-1000))
        p=self.payload(state="heartbeat"); self.send(p)
        with self.assertRaises(ValueError): self.send(dict(self.payload(state="heartbeat"),checkin_id=str(uuid4())))
    def test_mismatched_totals_sizes_times_and_unknown_identity(self):
        self.send(self.payload("One")); r=self.send(self.payload("Two",team=11000))
        self.assertFalse(r["consistent"])
        with self.assertRaises(PermissionError): self.send(self.payload("Unknown"))
    def test_mass_is_personal_only_and_never_group_qualified(self):
        p=self.payload(size=21,personal=1,team=1000000,mode="MASS_PERSONAL_ONLY")
        r=self.send(p); self.assertTrue(r["personal_only"]); self.assertFalse(r["group_qualified"])
        with self.assertRaises(ValueError):
            self.send(self.payload(size=21, personal=0, team=1000000,
                                   mode="MASS_PERSONAL_ONLY"))

    def test_server_recomputes_scoring_mode_from_maximum_scale(self):
        with self.assertRaises(ValueError):
            self.send(self.payload(size=3, mode="MASS_PERSONAL_ONLY"))
        scaled = self.payload(size=3)
        scaled["max_scale"] = 21
        scaled["proposed_scoring_mode"] = "MASS_PERSONAL_ONLY"
        self.assertTrue(self.send(scaled)["personal_only"])

    def test_party_holder_change_and_two_same_world_raids_do_not_merge(self):
        scouting = self.payload("One", holder=10, state="heartbeat")
        first = self.send(scouting)
        assembled = self.payload("One", holder=11)
        second = self.send(assembled)
        other = self.send(self.payload("Two", holder=12))
        self.assertNotEqual(first["fingerprint"], second["fingerprint"])
        self.assertNotEqual(second["fingerprint"], other["fingerprint"])
        self.assertEqual(1, second["verified"])

    def test_scaler_leaving_uses_max_scale_but_final_three_checkins(self):
        results = []
        for rsn in ("One", "Two", "Three"):
            payload = self.payload(rsn, size=3)
            payload["max_scale"] = 4
            results.append(self.send(payload))
        self.assertTrue(results[-1]["group_qualified"])
        self.assertEqual(3, results[-1]["expected"])

    def test_disconnect_same_epoch_recovers_but_new_epoch_fails_closed(self):
        epoch = str(uuid4())
        self.send(self.payload("One", epoch=epoch, state="heartbeat"))
        result = self.send(self.payload("One", epoch=epoch))
        self.assertEqual(1, result["verified"])
        with self.assertRaises(ValueError):
            self.send(self.payload("One", epoch=str(uuid4()), state="heartbeat"))

    def test_storage_is_bounded_and_has_no_point_or_submission_tables(self):
        self.send(self.payload("One"))
        with sqlite3.connect(self.db) as db:
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual({"raid_presence_sessions", "raid_presence_checkins",
                          "raid_presence_receipts"}, tables)

    def test_linked_accounts_count_as_participants_but_one_member_recipient(self):
        self.send(self.payload("One", size=2, personal=500))
        both = self.send(self.payload("Two", size=2, personal=500))
        self.assertEqual((2, 2, 1, 1), (both["participant_account_count"],
            both["verified_account_count"], both["distinct_eligible_member_count"],
            both["proposed_recipient_count"]))
        self.assertTrue(both["group_qualified"])
        with sqlite3.connect(self.db) as db:
            rows = db.execute("SELECT rsn,member_key FROM raid_presence_checkins ORDER BY rsn").fetchall()
        self.assertEqual([("one", "2"), ("two", "2")], rows)

    def test_linked_account_contributions_stay_account_specific(self):
        self.send(self.payload("One", size=2, personal=500))
        below = self.send(self.payload("Two", size=2, personal=499))
        self.assertEqual((2, 1), (below["verified_account_count"],
                                  below["distinct_eligible_member_count"]))
        self.assertFalse(below["group_qualified"])

    def test_zero_point_scaler_counts_as_account_but_not_eligible_member(self):
        self.send(self.payload("One", size=2, personal=500))
        zero = self.send(self.payload("Two", size=2, personal=0))
        self.assertEqual((2, 2, 1), (zero["participant_account_count"],
            zero["verified_account_count"], zero["distinct_eligible_member_count"]))
        self.assertFalse(zero["group_qualified"])

    def test_late_heartbeat_cannot_erase_completion_and_expiry_cascades(self):
        epoch = str(uuid4())
        self.send(self.payload("One", epoch=epoch))
        heartbeat = self.payload("One", epoch=epoch, state="heartbeat")
        self.send(heartbeat)
        with sqlite3.connect(self.db) as db:
            self.assertEqual("completion", db.execute(
                "SELECT state FROM raid_presence_checkins").fetchone()[0])
        later = self.now + 3 * 60 * 60
        fresh = self.payload("Two", holder=99, started=later - 10, observed=later,
                             state="heartbeat")
        process(self.db, fresh, lambda _: 2, later)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(1, db.execute("SELECT count(*) FROM raid_presence_sessions").fetchone()[0])

    def test_completion_time_skew_and_modes_fail_closed(self):
        started = self.now - 300
        first = self.payload("One", started=started, observed=self.now - 200)
        process(self.db, first, lambda _: 2, self.now - 200)
        second = self.payload("Two", started=started, observed=self.now)
        result = process(self.db, second, lambda _: 2, self.now)
        self.assertFalse(result["consistent"])
        third = self.payload("Three", started=started, mode="SOLO_PERSONAL_ONLY")
        with self.assertRaises(ValueError):
            process(self.db, third, lambda _: 2, self.now)

if __name__ == "__main__": unittest.main()
