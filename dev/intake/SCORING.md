# Scoring rules and integration status

Confirmed by Simon on 2026-09-01. `scoring.py` is a pure, tested calculation module.
It is not invoked by the receiving API and has no database writes. No client
update is required to test these Python rules.

## Ordinary drops

- For ordinary loot, the unit price must be at least 500,000 GP. A stack cannot
  qualify through quantity alone: 100 items worth 100,000 each remain ineligible.
  Three items worth 600,000 each pass the unit-price filter. This applies to
  ordinary loot only, not the fixed-value pets/kits/jars catalogue.
- Under 500,000 GP per unit: zero points.
- At/above 500,000 GP: nearest whole million, half-up. Integer formula:
  `(value_gp + 500000) // 1000000`.
- Base points are rounded before the multiplier. Individual positive shares get
  a one-point minimum (pity point); larger shares use the discussed nearest-whole
  rounding with halves upward. Zero/ineligible awards remain zero. Cap the final
  integer award at 200. The minimum can make total awards exceed the original pool.
- For qualifying multi-quantity stacks, whether to round each unit separately or
  round their combined value still needs clarification. The unit-price gate is
  implemented independently so it does not preempt that choice.
- Ordinary solo: 1x.
- All-clan group: 1.5x. Solo membership alone does not qualify as group content.
- Mixed member/nonmember group: 1x.
- Drop obtained during an event AND relevant to that event: 2x, overriding group
  multiplier. It does not stack to 3x and does not waive the 500k minimum.
- Determine value, qualifying event and group evidence on the backend. Unknown
  group evidence cannot be treated as proof of a solo or an all-clan group.

## Pets, kits, jars

Use their resolved fixed catalogue points. Do not apply the ordinary GP formula
or turn missing catalogue entries into a zero-point award. The supplied sheet
formula looks up the item in Pets/Kits and otherwise returns column D; it does not
itself demonstrate the upstream calculation of D or group allocation. Pets, kits and jars are personal awards and must never be split. The normal
fixed-award helper has no party-size parameter. Whether an event multiplier
applies to these fixed awards still needs confirmation before wiring that path. The current RuneLite preview does not capture these reward types yet.

## Per-item cap

Simon confirmed a 200-point cap per player, per item, AFTER applying the
multiplier and splitting. A 1.5B item for five all-clan participants is 1500 base
× 1.5 / 5 = 450, capped to **200 each**. Do not cap the whole group pool first.
Fixed personal awards also respect the stated any-item cap. No cap is imposed
across unrelated items. The calculation helper remains disconnected from live
awards while stack valuation and integration are resolved.

## Allocation still to resolve

Simon described members splitting the points in a mixed party; our working
interpretation is that the recipient count consists of eligible clan participants.
The arithmetic helper accepts an explicit recipient count and does not select
participants or infer a denominator. Group-size/RSN capture is not yet sent to the
server or established as reliable evidence of eligibility.

A 1M drop to an all-clan duo has 1 base point and a 1.5-point pool, or 0.75 each.
Simon requested one point each as a pity point. A positive 0.1 share likewise
receives the one-point minimum. A 1.5 share rounds half-up to 2; a zero pool stays
zero. Exact fractions are kept for intermediate math, and final awarded shares
are integers. Existing total calculations must sum those final awards rather than
recompute/truncate intermediate shares.

Stack scoring remains unresolved: three units at 600k could produce 2 base points
from their combined 1.8M value, or 3 base points from rounding each 600k unit.
Cap granularity across multiple identical units also follows from that decision.
Do not wire the multi-quantity scorer into automatic awards before resolving it.

## Existing database integration

Target: RegularSubmissions.db / regular_submissions with source_type=runelite,
source_event_type=NULL for regular rank awards, linked member/account identity,
and stable per-item external IDs. A plugin retry ID only deduplicates retries;
it cannot by itself correlate the same loot submitted manually via Sheets.
Sheet deletion is scoped to sheet_sync and Google Sheet external IDs.

Do not use the current rank_point_rules seed ranges: 10M=3 differs from the
confirmed 10M=10 ordinary formula. Do not overwrite that table without checking
its consumers. The displayed Items.db price rows were last refreshed June 4;
valuation needs refresh/freshness handling before live scoring.

Existing rebuilds disagree about event rows: the sheet sync excludes event rows
and recreates totals without the component columns; the admin full rebuild sums
all approved rows; manual editing additionally preserves an existing event bonus.
Unify the intended regular-rank aggregation before automatic awards. Event-board
scores in ROTW/SOTW databases are separate from the 2x regular-rank multiplier.
