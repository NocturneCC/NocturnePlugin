# Automatic Challenge observations

This repository-only contract prepares a future RuneLite client integration.
It does not deploy a route or migrate a live database. The active published
configuration remains authoritative; no privileged Challenge token is used by
the plugin.

## Endpoint

`POST /api/challenges/intake/observations` accepts `application/json`, is
limited to 8 KiB at the Nginx boundary and 10 requests per client address per
minute in the API process. The body is strict JSON with duplicate/unknown keys
rejected. The API resolves the active published Challenge version on every
request; it does not trust client-supplied PB, points, approval, clan-member or
configuration-version claims.

Version 1 fields:

```json
{
  "schema_version": 1,
  "event_id": "UUID",
  "reporter_rsn": "Public RuneScape name",
  "activity_key": "theatre_of_blood",
  "mode_key": "tob_duo",
  "occurred_at": "2026-10-03T12:34:56Z",
  "room_time_ms": 901230,
  "overall_time_ms": 1802460,
  "roster": ["Public RSN One", "Public RSN Two"],
  "group_size": 2,
  "completion_count": 42,
  "plugin_version": "0.3.2"
}
```

`room_time_ms`, `overall_time_ms`, and `completion_count` are optional fields;
at least one duration is required. Times are positive integer milliseconds up
to 24 hours. Timestamps must include a timezone, be no more than 24 hours old,
and no more than two minutes in the future. The complete roster has 1–10
distinct normalized RSNs, contains the reporter exactly once, and its length
must equal `group_size`. The reporter must resolve to a current eligible clan
member. Other names that do not resolve remain in the observation roster and
count toward group classification, but receive no member or leaderboard
association. Duplicate aliases resolving to one member fail closed.

The server maps only these canonical activities:

| Activity | Published content key | Required scope / selected metric |
| --- | --- | --- |
| `theatre_of_blood` | `theatre_of_blood` | `segment` / `room_time_ms` |
| `theatre_of_blood_hard_mode` | `theatre_of_blood_hard_mode` | `overall` / `overall_time_ms` |
| `tombs_of_amascut` | `tombs_of_amascut` | `overall` / `overall_time_ms` |
| `chambers_of_xeric` | `chambers_of_xeric` | `overall` / `overall_time_ms` |
| `chambers_of_xeric_challenge_mode` | `chambers_of_xeric_cm` | `overall` / `overall_time_ms` |
| `phosanis_nightmare` | `phosanis_nightmare` | `overall` / `overall_time_ms` |

Phosani's Nightmare uses the exact payload identity
`activity_key="phosanis_nightmare"`, `mode_key="phosani_nightmare"`. That
published solo mode must resolve to `boss_key="phosanis"`; the active boss
configuration must explicitly enable automatic capture with overall timing.
Its roster is exactly the reporting player's normalized public RSN (`group_size`
1). No party-member identities are accepted or inferred for this activity.

### Client completion correlation

The client must identify the completed activity from its existing encounter
and loot-tracking context, never from an exact boss-specific timing sentence.
Maintain a bounded ring buffer of eligible game/system chat messages and a
short-lived completion context with activity identity, completion timestamp,
party context, and stable encounter/event identity. Correlate eligible messages
before or after the completion event by message type, time proximity, and
current encounter; activity wording may help but is optional.

Use a generic duration recognizer for ordinary OSRS duration formats, including
fractional seconds. Exclude values identified as personal-best/reference values
and capture the current completion whether or not it is a PB. For ordinary
single-timer bosses, submit one unambiguous non-PB duration without requiring a
boss-specific phrase. For activities with multiple valid metrics (such as
ToB/HMT), use only the published timing policy and a small activity-level
metric-selection rule. If candidates remain ambiguous, fail closed and retain
only a bounded diagnostic reason. Deterministic event IDs must suppress replayed
chat and duplicate completion events. The PNM fixture
`Team size: Solo Fight duration: 5:58.20. Personal best: 4:48.60` is a parser
regression fixture, not a production message-format requirement: select
358200 ms and never select the PB value.

`mode_key` must identify exactly one active leaderboard mode in that same
published version and accept the full observed group size. The mode must link
to an active, submission-enabled time Challenge using milliseconds and lower
is better. Automatic capture must be explicitly enabled and the timing scope
must be configured as above. `manual_only`, `unconfigured`, disabled, missing,
ambiguous, or inconsistent policy never creates a submission.

The endpoint never accepts raw chat, screenshots, arbitrary metadata, Discord
IDs, member/database IDs, tokens, approval state, PB claims, points, placement,
or a client-selected configuration version. Public RSNs are retained only in
the bounded observation-participant table and in existing resolved Challenge
participant rows. Normal error responses and logs do not echo payloads or
identities.

## Responses

- `201 {"state":"accepted"}` — stored and projected transactionally.
- `200 {"state":"duplicate"}` — exact event replay or exact semantic replay.
- `200 {"state":"ignored","reason":"manual_only|unconfigured|disabled"}` — stored as a bounded audit observation but not submitted.
- `400`/`413`/`415`/`422 {"state":"invalid","reason":"..."}` — invalid or unsupported input/policy.
- `409 {"state":"idempotency_conflict"}` — event ID reused with different content or a conflicting completion-count replay.
- `429 {"state":"rate_limited"}` — request limit reached.
- `503 {"state":"server_failure"}` — internal/configuration/database failure; no detail is returned.

No public policy-read endpoint is needed. A future client may send observations
without fetching configuration; POST always enforces current server policy.

## Persistence and migration

`challenge_automatic_intake.migrate_schema(conn)` installs the additive
`challenge_automatic_observations` and
`challenge_automatic_observation_participants` tables. Each row records schema
version, fixed RuneLite automatic provenance, client event ID, canonical
payload hash, normalized reporter/activity/mode, timestamp, both observed
metrics, selected scope/duration, full group size, completion count, plugin
version, active Challenge version, disposition, optional linked submission,
and creation time. Participant rows contain only the normalized public RSNs.
Both tables are append-only. `rollback_schema(conn)` drops the migration only
when no observation rows exist; it refuses to erase captured data.

Accepted rows create an existing `challenge_submissions` row in `approved`
state with `source_system='runelite_automatic'`, `approver_discord_id=NULL`,
and no invented Discord approval metadata. The one-to-one observation link
distinguishes this server policy acceptance from Discord/manual approval.
`rebuild_derived` and the existing Challenge leaderboard ingester run inside
the same write transaction. The ingester uses the full observed group size for
automatic mode selection while associating only resolved eligible members;
legacy/manual submissions retain their historical participant-count fallback.

Run the repository Challenge database migration only through a separately
reviewed deployment procedure. This task does not prepare or apply it to
production.
