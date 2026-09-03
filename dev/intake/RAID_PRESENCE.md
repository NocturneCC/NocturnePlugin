# CoX raid-presence protocol v1

`POST /api/plugin/dev/raid-presence` is a separate, development-only protocol.
It does not alter loot payload v4 and has no approval, award, rank-total, or
regular-submission write path.

Each enabled client reports only its own normalized RSN and locally observed
session facts. Heartbeats replace the prior heartbeat row; receipts make a
check-in UUID idempotent. Sessions expire after two hours. A result is complete
only when consistent completed check-ins from unique RSNs equal the reported
final party size. The server recomputes consistency and requires at least 500
basis points for every credited self-reporter in a normal group. Solo and raids
whose maximum authoritative scale exceeds 20 remain personal-only.

Participant identity is account-scoped: unique normalized RSNs are compared to
the game's final party size, even when two linked accounts resolve to the same
clan member. Each check-in retains its resolved member ID. Recipient accounting
is separately member-scoped and deduplicates eligible accounts by member ID.
Responses expose participant account count, verified account count, and distinct
eligible member count, but never create shares or write points in this phase.

The matching key uses raid type, world, party-group holder, and a bounded local
raid-start bucket. The holder is not assumed to be cross-client authoritative;
that assumption must be tested with multiple real clients. Final party size,
team total, completion timing, and scoring mode must also agree before group
verification can qualify. Indistinguishable simultaneous sessions fail closed
when their completion facts disagree. This is corroborating client evidence,
not cryptographic proof of an authentic RuneLite binary.

No client sends names observed in the game roster or instance. `GAME_ROSTER`,
`INSTANCE_OBSERVED`, and `NOCTURNE_VERIFIED` remain separate evidence classes.

The SQLite file is `raid-presence-v1.sqlite3` inside the intake's private state
directory. It stores one current row per normalized self-reporting RSN/session
and idempotency receipts, not heartbeat history. Expired sessions and their
dependent rows are removed during later check-ins. No screenshot or loot item is
stored in this file.

The existing public Nginx configuration uses an exact-match loot route, so the
presence endpoint needs its own exact-match route. `raid_presence_route_support.py`
is dry-run by default, refuses a changed/partial route, preserves exact file
metadata and ACLs, creates a verified backup, validates Nginx syntax after an
explicit `--apply`, and restores the file on failure. It never reloads Nginx or
controls any service.
