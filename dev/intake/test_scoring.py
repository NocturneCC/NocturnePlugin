import unittest
from fractions import Fraction
from scoring import (GroupKind, UnresolvedEligibility, ordinary_base_points,
                     fixed_base_points, drop_multiplier, ordinary_drop_pool, exact_share,
                     capped_recipient_share, fixed_recipient_award, qualifying_quantity)


class ScoringTest(unittest.TestCase):
    def test_gp_boundaries_round_half_up(self):
        for gp, points in [(0, 0), (499999, 0), (500000, 1), (999999, 1),
                           (1000000, 1), (1499999, 1), (1500000, 2),
                           (1999999, 2), (2499999, 2), (2500000, 3),
                           (10000000, 10), (1000000000, 1000)]:
            with self.subTest(gp=gp):
                self.assertEqual(points, ordinary_base_points(gp))

    def test_fixed_values_do_not_use_market_value(self):
        self.assertEqual(75, fixed_base_points(75))
        self.assertEqual(0, fixed_base_points(0))
        with self.assertRaises(ValueError):
            fixed_base_points(None)

    def test_solo_has_no_all_clan_bonus(self):
        self.assertEqual(1, drop_multiplier(qualifying_event_drop=False, group=GroupKind.SOLO))
        self.assertEqual(Fraction(3, 2), drop_multiplier(qualifying_event_drop=False, group=GroupKind.ALL_CLAN))
        self.assertEqual(1, drop_multiplier(qualifying_event_drop=False, group=GroupKind.MIXED))

    def test_event_multiplier_overrides_group_and_does_not_stack(self):
        for group in GroupKind:
            self.assertEqual(2, drop_multiplier(qualifying_event_drop=True, group=group))
        self.assertEqual(20, ordinary_drop_pool(10000000, qualifying_event_drop=True, group=GroupKind.ALL_CLAN))

    def test_nonqualifying_event_drop_uses_normal_group_rules(self):
        self.assertEqual(15, ordinary_drop_pool(10000000, qualifying_event_drop=False, group=GroupKind.ALL_CLAN))

    def test_low_value_drop_does_not_become_eligible_from_event_bonus(self):
        self.assertEqual(0, ordinary_drop_pool(499999, qualifying_event_drop=True, group=GroupKind.ALL_CLAN))

    def test_unknown_evidence_is_not_treated_as_solo_or_all_clan(self):
        with self.assertRaises(UnresolvedEligibility):
            drop_multiplier(qualifying_event_drop=False, group=GroupKind.UNKNOWN)
        with self.assertRaises(UnresolvedEligibility):
            drop_multiplier(qualifying_event_drop=None, group=GroupKind.SOLO)

    def test_split_retains_fractional_points_without_rounding(self):
        pool = ordinary_drop_pool(1000000, qualifying_event_drop=False, group=GroupKind.ALL_CLAN)
        self.assertEqual(Fraction(3, 4), exact_share(pool, 2))
        self.assertEqual(pool, exact_share(pool, 2) * 2)
        self.assertEqual(Fraction(2, 3), exact_share(2, 3))

    def test_per_recipient_cap_follows_multiplier_and_split(self):
        # Confirmed: 1.5B all-clan five-person split awards 200 each.
        pool = ordinary_drop_pool(1500000000, qualifying_event_drop=False, group=GroupKind.ALL_CLAN)
        self.assertEqual(200, capped_recipient_share(pool, 5))
        self.assertEqual(113, capped_recipient_share(pool, 20))
        self.assertEqual(200, capped_recipient_share(1500, 1))
        event_pool = ordinary_drop_pool(1500000000, qualifying_event_drop=True, group=GroupKind.MIXED)
        self.assertEqual(200, capped_recipient_share(event_pool, 5))

    def test_pity_point_for_positive_shares_and_half_up_for_larger_shares(self):
        for pool, members, points in [(Fraction(3, 2), 2, 1), (1, 10, 1),
                                      (Fraction(5, 2), 2, 1), (3, 2, 2),
                                      (5, 2, 3), (0, 2, 0)]:
            with self.subTest(pool=pool, members=members):
                self.assertEqual(points, capped_recipient_share(pool, members))

    def test_pity_point_does_not_award_ineligible_drops(self):
        pool = ordinary_drop_pool(499999, qualifying_event_drop=True, group=GroupKind.ALL_CLAN)
        self.assertEqual(0, capped_recipient_share(pool, 2))

    def test_unit_value_gate_cannot_be_met_by_a_large_cheap_stack(self):
        self.assertEqual(0, qualifying_quantity(100000, 100))
        self.assertEqual(0, qualifying_quantity(499999, 3))
        self.assertEqual(3, qualifying_quantity(500000, 3))
        self.assertEqual(3, qualifying_quantity(600000, 3))

    def test_unit_value_gate_rejects_invalid_counts_and_prices(self):
        for count in [0, -1, True, 1.5]:
            with self.assertRaises(ValueError):
                qualifying_quantity(500000, count)
        for price in [-1, True, 499999.99, None]:
            with self.assertRaises(ValueError):
                qualifying_quantity(price, 2)

    def test_fixed_reward_stays_personal_and_respects_item_cap(self):
        self.assertEqual(75, fixed_recipient_award(75))
        self.assertEqual(200, fixed_recipient_award(250))
        # There is deliberately no group-size argument for fixed rewards.
        with self.assertRaises(TypeError):
            fixed_recipient_award(75, 5)

    def test_rejects_lossy_or_invalid_inputs(self):
        for value in [-1, True, 1.5, "1000000", None]:
            with self.assertRaises(ValueError):
                ordinary_base_points(value)
        for count in [0, -1, True, 1.5]:
            with self.assertRaises(ValueError):
                exact_share(1, count)
        with self.assertRaises(ValueError):
            exact_share(1.5, 2)


if __name__ == "__main__":
    unittest.main()
