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
bounded positive millisecond durations for time observations, or a catalog-keyed
positive numeric completion value for completion observations; each has one
complete roster of at most ten normalized public RSNs and a bounded
timestamp/plugin version. Time and numeric results are distinct metric records
and are never coerced into each other. It returns
`200 accepted`, `200 duplicate`, `200 ignored` with a bounded `disabled` or
`unconfigured` reason, `422 invalid`, `409 idempotency_conflict`, `429
rate_limited`, or a generic `500 server_failure`. It never accepts a Challenge
token, PB claim, score, Discord identity, raw chat, or client-selected catalog
version.

The client sends only the local reporter and a complete stable public roster
when a supported group context exists. It does not infer a roster from nearby
players. If roster, mode, activity, scope, or metric selection is ambiguous,
timing submission fails closed; local loot history remains available.

Numeric completion observations use this alternate exact v1 shape (the normal
time fields are not present):

```json
{
  "schema_version": 1,
  "event_id": "stable UUID for one completed run",
  "reporter_rsn": "Local Reporter",
  "activity_key": "doom_of_mokhaiotl",
  "mode_key": "doom_of_mokhaiotl",
  "occurred_at": "2026-10-13T12:00:00Z",
  "completion_metric_key": "deepest_level",
  "completion_value": 7,
  "roster": ["Local Reporter"],
  "group_size": 1,
  "plugin_version": "0.3.2"
}
```

The server validates the active catalog on every request and requires the
completion metric key to match an enabled numeric metric. It stores these in
`plugin_tracking_completion_observations` and
`plugin_tracking_completion_participants`, separate from the millisecond time
tables. Same event ID plus same canonical payload is a duplicate; changed
payload is an idempotency conflict. Challenge projection is not implied by
Plugin Tracking and requires a separately configured and supported Challenge
policy.

## Initial catalog

| Activity | Mode | Loot | TTK | Scope | Challenge overlay |
| --- | --- | --- | --- | --- | --- |
| Phosani's Nightmare | `phosani_nightmare` | enabled | enabled | overall | `phosanis` |
| Corrupted Gauntlet | `corrupted_gauntlet` | enabled | enabled | overall | `gauntlet` |
| The Nightmare | `nightmare` | enabled | enabled | overall | none |
| Doom of Mokhaiotl | `doom_of_mokhaiotl` | enabled | disabled | unconfigured | none; numeric `deepest_level` completion metric enabled |

For Doom of Mokhaiotl, the client reads RuneLite's named
`InterfaceID.DomEndLevelUi.DELVE_HEADER` only when the
`DomEndLevelUi` interface loads and accepts the anchored `Level N Complete!`
success text. It never uses the currently entered floor or best-time varbits
as a run result. The local activity card advances to the highest distinct
successfully completed level. Death, explicit leave, reward claim, and session
exit finalize that value; a claim may link the following reward event once to
the same run. That link has no short timeout, but is discarded when a new run
first confirmed completion is observed, so a late reward is never assigned to
a different completed run. Local recording
is retained even when remote completion submission is disabled. Numeric
`deepest_level` is not a TTK duration and does not itself enable Challenge
scoring.

The Nightmare supports solo and group tracking in the catalog. Group timing is
submitted only when the client has a complete matched roster from stable party
context; otherwise it is retained as local Recent Activity without TTK upload.

The administrative editor is `/plugin-tracking.html`, labeled **Plugin
Tracking**, and uses separate staff-authorized draft/publish routes. Challenge
configuration editing remains separate. Numeric completion metrics can be
enabled and edited with the catalog's metric key, display label, and unit
controls; the **Add or enable Doom of Mokhaiotl** action inserts the canonical
solo activity into an older draft or enables its `deepest_level` metric. Staff
can save the draft and publish it without editing JSON. Advanced catalog JSON
remains available for other catalog fields.

The automatic-observation installer permits additive migration of the deployed
legacy Plugin Tracking schema. It creates the completion-observation tables
without rewriting or removing existing time observations or participants. If a
deployment later fails after accepted automatic data exists, rollback retains
the installed schema/files and records the refusal, but still restores the
captured services and timers to their pre-deployment state.
