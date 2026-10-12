# Plugin Tracking

Plugin Tracking is an independent, versioned catalog of RuneLite activities. It
defines canonical activity and mode keys, source-name aliases, solo/group
capabilities, loot and TTK switches, and the selected timing scope. It is not a
Challenge configuration and an activity does not need to be a Challenge boss
to appear in Recent Activity or to have a Plugin Tracking timing record.

Challenge configuration may separately project a catalog activity through an
explicit `challenge_boss_key`, `challenge_mode_key`, and
`challenge_content_key` overlay. The automatic Challenge intake remains the
authority for whether that Challenge result is accepted and scored. The Plugin
Tracking observation route never creates a Challenge submission, points, tier,
or leaderboard row.

## Public read and observation routes

- `GET /api/plugin/tracking/config` returns the active client-safe catalog with
  an ETag. It contains no credentials, draft state, or admin identity.
- `POST /api/plugin/tracking/observations` accepts the same bounded v1 semantic
  observation body used by the client. The server chooses the configured timing
  scope, validates the current catalog and reporter membership, and stores an
  immutable activity timing fact. Same event ID plus identical normalized JSON
  is a duplicate; reuse with changed content is an idempotency conflict.

The POST route accepts only `application/json`, at most 8 KiB, exact v1 fields,
bounded positive millisecond durations, one complete roster of at most ten
normalized public RSNs, and a bounded timestamp/plugin version. It returns
`200 accepted`, `200 duplicate`, `200 ignored` with a bounded `disabled` or
`unconfigured` reason, `422 invalid`, `409 idempotency_conflict`, `429
rate_limited`, or a generic `500 server_failure`. It never accepts a Challenge
token, PB claim, score, Discord identity, raw chat, or client-selected catalog
version.

The client sends only the local reporter and a complete stable public roster
when a supported group context exists. It does not infer a roster from nearby
players. If roster, mode, activity, scope, or metric selection is ambiguous,
timing submission fails closed; local loot history remains available.

## Initial catalog

| Activity | Mode | Loot | TTK | Scope | Challenge overlay |
| --- | --- | --- | --- | --- | --- |
| Phosani's Nightmare | `phosani_nightmare` | enabled | enabled | overall | `phosanis` |
| Corrupted Gauntlet | `corrupted_gauntlet` | enabled | enabled | overall | `gauntlet` |
| The Nightmare | `nightmare` | enabled | enabled | overall | none |

The Nightmare supports solo and group tracking in the catalog. Group timing is
submitted only when the client has a complete matched roster from stable party
context; otherwise it is retained as local Recent Activity without TTK upload.

The administrative editor is `/plugin-tracking.html`, labeled **Plugin
Tracking**, and uses separate staff-authorized draft/publish routes. Challenge
configuration editing remains separate.
