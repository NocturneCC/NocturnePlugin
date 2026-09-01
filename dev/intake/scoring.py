"""Pure scoring rules confirmed by Simon on 2026-09-01.

Not connected to the intake or live databases. Inputs must be resolved server-side.
This module does not authenticate reports, establish eligibility, value items,
or decide who participated. Fractional points must not be cast to integers.
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
    Ownership, sharing and applicability of bonuses for these awards are separate.
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
    """Draft cap order: split the multiplied item pool, then cap each recipient.

    Confirm this interpretation with Simon before enabling awards. This does not
    round fractional shares. Apply independently for each item award, not to the
    sum of a player's unrelated drops. Never use this group helper for fixed awards.
    """
    return min(exact_share(pool, recipient_count), Fraction(PER_PLAYER_ITEM_CAP))


def fixed_recipient_award(catalogue_points):
    """Personal pet/kit/jar award: no party-size input or redistribution.

    Uses the fixed catalogue value with the per-item cap. Event multiplier policy
    for fixed rewards remains unresolved; this helper covers the ordinary case.
    """
    return min(fixed_base_points(catalogue_points), PER_PLAYER_ITEM_CAP)
