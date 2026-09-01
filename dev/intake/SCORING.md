# Scoring rules and integration status

Confirmed by Simon on 2026-09-01. `scoring.py` is a pure, tested calculation module.
It is not invoked by the receiving API and has no database writes. No client
update is required to test these Python rules.

## Ordinary drops

- Under 500,000 GP: zero points.
- At/above 500,000 GP: nearest whole million, half-up. Integer formula:
  `(value_gp + 500000) // 1000000`.
- Base points are rounded before the multiplier. No final share rounding is
  implemented pending confirmation.
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

Simon added a 200-point cap on any one item. Working interpretation: cap each
player's award after applying the multiplier and splitting. This ordering is
implemented only in a separate draft arithmetic helper and needs confirmation.
A 1.5B item for five all-clan participants would be 1500 base × 1.5 / 5 = 450,
capped to 200 each under this interpretation. Do not cap the whole group pool
at 200 first unless Simon confirms that alternative. Fixed personal awards also
respect the stated any-item cap. No cap is imposed across unrelated items.

## Allocation still to resolve

Simon described members splitting the points in a mixed party; our working
interpretation is that the recipient count consists of eligible clan participants.
The arithmetic helper accepts an explicit recipient count and does not select
participants or infer a denominator. Group-size/RSN capture is not yet sent to the
server or established as reliable evidence of eligibility.

A 1M drop to an all-clan duo has 1 base point and a 1.5-point pool, or 0.75 each.
Confirm whether shares should retain fractions or be rounded, and at what stage,
before enabling awards. Exact fractions are retained in calculation tests only.
Existing `final_points` and rank-total columns are INTEGER, and some sync/admin
paths explicitly cast to int. Merely writing decimals into SQLite would not fix
truncation by those downstream paths. A consistent representation and rebuild
policy must be selected first.

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
