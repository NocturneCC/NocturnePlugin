"""Pure scoring rules confirmed by Simon on 2026-09-01.

Not connected to the intake or live databases. Inputs must be resolved server-side.
This module does not authenticate reports, establish eligibility, value items,
or decide who participated. Preserve exact intermediate shares until award rounding.
"""
from enum import Enum
from fractions import Fraction


class GroupKind(Enum):
    SOLO = "solo"
    ALL_CLAN = "all_clan"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class UnresolvedEligibility(ValueError):
    pass


def nonnegative_int(value, label):
    if type(value) is not int or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def ordinary_base_points(value_gp):
    """500k minimum, then nearest million with .5 rounded upward."""
    nonnegative_int(value_gp, "value_gp")
    return (value_gp + 500_000) // 1_000_000


def fixed_base_points(catalogue_points):
    """Use a resolved pet/kit/jar catalogue value, never a GP estimate.

    A missing catalogue entry is unresolved, not a zero-point result.
    These rewards are personal; event bonus applicability is a separate policy.
    """
    return nonnegative_int(catalogue_points, "catalogue_points")


def drop_multiplier(*, qualifying_event_drop, group):
    """Event qualification includes both activity time and event item eligibility.

    None means event qualification could not be resolved; do not assume False.
    ALL_CLAN must refer to an established group with at least two participants.
    A nearby-name snapshot cannot establish ALL_CLAN, MIXED or SOLO.
    """
    if not isinstance(group, GroupKind):
        raise ValueError("group must be a GroupKind")
    if qualifying_event_drop is None:
        raise UnresolvedEligibility("Event qualification is unresolved")
    if type(qualifying_event_drop) is not bool:
        raise ValueError("qualifying_event_drop must be bool or None")
    if qualifying_event_drop:
        return Fraction(2)
    if group is GroupKind.UNKNOWN:
        raise UnresolvedEligibility("Group composition is unresolved")
    if group is GroupKind.ALL_CLAN:
        return Fraction(3, 2)
    return Fraction(1)


def ordinary_drop_pool(value_gp, *, qualifying_event_drop, group):
    """Round base points, then multiply. Event 2x replaces group 1.5x."""
    base = ordinary_base_points(value_gp)
    if base == 0:
        return Fraction(0)
    return base * drop_multiplier(qualifying_event_drop=qualifying_event_drop, group=group)


def exact_share(pool, recipient_count):
    """Arithmetic helper only: caller must establish the actual recipient count.

    No final rounding policy or recipient selection is implied. Keeping a Fraction
    here prevents silent point loss in existing INTEGER fields and int() rebuilds.
    """
    if type(pool) not in (int, Fraction) or pool < 0:
        raise ValueError("pool must be nonnegative exact points")
    if type(recipient_count) is not int or recipient_count < 1:
        raise ValueError("recipient_count must be a positive integer")
    return Fraction(pool) / recipient_count


PER_PLAYER_ITEM_CAP = 200


def capped_recipient_share(pool, recipient_count):
    """Split, round half-up with a one-point minimum, then cap per recipient.

    A zero pool stays zero. The pity point applies only to a positive qualifying
    award. Rounding can make the sum of awards exceed the unrounded item pool.
    Apply independently for each item award; never share fixed pet/kit/jar awards.
    """
    share = exact_share(pool, recipient_count)
    if share == 0:
        return 0
    rounded = (2 * share.numerator + share.denominator) // (2 * share.denominator)
    return min(max(1, rounded), PER_PLAYER_ITEM_CAP)


def fixed_recipient_award(catalogue_points):
    """Personal pet/kit/jar award: no party-size input or redistribution.

    Uses the fixed catalogue value with the per-item cap. Event multiplier policy
    for fixed rewards remains unresolved; this helper covers the ordinary case.
    """
    return min(fixed_base_points(catalogue_points), PER_PLAYER_ITEM_CAP)


def qualifying_quantity(unit_value_gp, quantity):
    """Ordinary-loot eligibility is based on UNIT price, not combined stack value.

    Returns the quantity eligible for per-unit scoring. Never use it for
    catalogue-based personal rewards such as pets, kits or jars.
    """
    nonnegative_int(unit_value_gp, "unit_value_gp")
    if type(quantity) is not int or quantity < 1:
        raise ValueError("quantity must be a positive integer")
    return quantity if unit_value_gp >= 500_000 else 0


def ordinary_stack_base_points(unit_value_gp, quantity):
    """Round each eligible unit, then multiply by quantity, before bonuses/splits.

    Three 600k units yield 3 base points, not 2 from rounding their combined value.
    This helper does not apply a party multiplier, recipient rounding or a cap.
    """
    eligible = qualifying_quantity(unit_value_gp, quantity)
    return ordinary_base_points(unit_value_gp) * eligible
