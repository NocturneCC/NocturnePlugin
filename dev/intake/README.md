# Isolated development intake

Prepared for Midgard; not deployed by committing this directory. This is server
code, excluded from the RuneLite JAR. It uses Python's standard library as a WSGI
application and gunicorn as the HTTP server. No Flask/live API modifications.

POST `/api/plugin/dev/drops` accepts only the reporting RSN, item IDs/quantities,
source, timestamp, schema version and event UUID. Extra fields (including group
names) are rejected. Test RSNs are allowlisted; **this is not authentication**.
There is no member lookup, points processing, public read endpoint or Discord login.
Claims remain unverified. Do not route these records into live scoring.

Limits: 8 KiB requests, 64 unique item stacks, recent timestamps, 20 new reports
per RSN/minute, 10,000 rows, approximately 16 MiB SQLite file, seven-day cleanup
on new inserts, two workers and nginx request limits. Identical event UUID+payload
gets an idempotent receipt; a changed payload using that UUID returns 409.
Separate genuine drops must have distinct UUIDs. Limits mitigate resource usage;
they do not prevent forged submissions or network-level denial of service.

## Test without Midgard

```sh
cd dev/intake
python3 -m unittest -v
```

## Midgard deployment preparation

First check port 5072 is unused and inspect nginx's active site/include layout.
The earlier listing included a `nocturne.backup` file in sites-enabled; determine
whether it is an active duplicate before reloading. Do not overwrite existing
nginx files or disable unrelated services.

Create a separate clone at `/srv/projects/nocturne-plugin-intake`, branch
`development`. Create `.venv` there and install a current vetted gunicorn release
into it; it must be readable/executable by the service's DynamicUser. The service
requires Python 3.9+ and writes only its StateDirectory. Existing live databases
are never opened. Do not use the RuneLite project checkout on Windows as a server.

Review `nocturne-plugin-dev.service` (including the test RSN allowlist) before
installing it into `/etc/systemd/system/`. `nginx-limits.conf` belongs in nginx's
http context; add the location snippet inside the existing HTTPS server block.
After the local intake test succeeds, the inspected Midgard nginx layout can be
updated using:

```sh
sudo python3 dev/intake/install_nginx.py
```

The installer requires the exact inspected HTTPS header, no existing intake
route/limits file, an active intake service, and an unchanged backup (or only the
observed `client_max_body_size 15M` difference). It saves originals under
`/etc/nginx/nocturne-plugin-backups/`, moves `nocturne.backup` out of the active
include directory, adds the exact intake location and rate-limit definitions,
validates with `nginx -t`, and requests a reload. Validation or reload errors
restore prior files; a reload failure also attempts to reload the restored config.
A second invocation stops for inspection rather than duplicating the route.
Installer tests exercise temporary files and simulated commands, not live nginx.

Verify a stored receipt through HTTPS after installation before enabling
**Send drops to test intake** in RuneLite. Only `Simons Alt` is initially allowed
by the supplied service. There is no scoring or group-name upload on this route.

A successful response is HTTP 201 with `status=stored`, `storage=development`,
and the matching `event_id`, after the database commits. A repeated identical
request returns HTTP 200 with `status=duplicate`. The plugin requires this receipt;
a generic HTTP 200 page is not success. Timeouts and 5xx responses are uncertain,
since a write could have committed before its acknowledgement was lost.

To inspect locally as administrator, query `/var/lib/nocturne-plugin-dev/test-drops.sqlite3`:

```sql
SELECT event_id, rsn, datetime(received_at, 'unixepoch'), payload
FROM test_drops ORDER BY received_at DESC LIMIT 10;
```

Turn the plugin submission toggle off to stop new requests. Cancelling a request
cannot undo storage that already happened. No automatic retries or disk queue
are included in this preview. Clearing the client history does not delete server
records. Stop this separate service/remove its nginx location to retire the test.

## Scoring preparation

See [SCORING.md](SCORING.md) for the confirmed rules and unresolved allocation
details. The pure `scoring.py` module is tested but is not connected to this API
or the live submission databases.

## Read-only processing preview

After actual game loot appears in the test intake, run this separately on Midgard:

```sh
sudo python3 -B dev/intake/preview.py --limit 10
```

This opens the intake, Members.db, Items.db and RegularSubmissions.db with
SQLite `mode=ro` and `query_only=ON`. It does not change service permissions,
make network requests, create missing databases, insert submissions, or update
points. Run it with `-B` to suppress Python bytecode writes. The public intake
service retains its isolation from the live databases.

The report checks current primary/active linked RSNs (not historical aliases),
conflicting identities, catalogue item IDs, existing per-item external IDs, fixed
reward catalogue entries, and prices. Active member matching establishes
eligibility only; it does not prove sender identity or drop legitimacy.

Version 2 reports carry RuneLite's unit price captured with the loot. The preview
uses it for the 500k eligibility gate and ordinary base-point calculation. It is
client-reported evidence, not an authenticated value or proof of a drop. A zero
price is treated as unavailable and never becomes a zero-point award.

Version 1 reports remain supported during rollout and use the existing catalogue
price. The 24-hour maximum catalogue price age is configurable up to seven days.
This fallback reflects the current catalogue, not a historical price locked at
drop time.

- `excluded_below_unit_threshold`: reported/fallback unit price is under 500k; zero points.
- `price_stale`, `price_unavailable`, `price_timestamp_unavailable`: cannot safely
  value; not a zero-point result. Fix the price source before processing.
- `needs_context`: ordinary base points can be calculated, but event eligibility,
  group/recipient evidence, manual duplicate checks and an award trust policy
  remain unresolved. No final multiplier or award is invented.
- `identity_review`: an otherwise eligible item has an unmatched/inactive/ambiguous
  identity; do not map historical aliases or take the first conflicting match.
- `already_imported`: the proposed per-item external ID already exists.
- `fixed_reward_review`: catalogue reward remains personal; do not use GP prices
  without resolving capture type and fixed-reward event applicability.
- `item_unknown` / `item_inactive`: catalogue lookup needs attention.
- `synthetic_test_excluded`: setup records are omitted from scoring analysis.

`would_insert` stays false in this first preview. It is diagnostic only; it does
not claim high-value drops are ready for automatic awards. Current website
submission pages do not read the test-intake database, so even a stored receipt
is not a website submission. Eventually excluded low-value loot should remain
intake-only, while qualified awards enter the existing submission workflow.

### Pending-submission importer

`import_pending.py` bridges eligible version 2 reports into the existing
`RegularSubmissions.db` review workflow. It previews by default:

```sh
sudo python3 -B dev/intake/import_pending.py --limit 50
```

Use `--apply` only after reviewing the candidate list. Apply mode verifies the
live table shape, backs up `RegularSubmissions.db`, opens one immediate
transaction, checks each `runelite:<event UUID>:<item ID>` external ID, and
inserts new rows with `status=pending` and `source_type=runelite`.

It never updates `rank_totals`, approves a submission, or turns a client report
into verified evidence. Ordinary candidates store a 1x proposal with unresolved
group/event context in the notes. Fixed pets, kits and jars use their personal
catalogue points and the 200-point item cap. Low-value, unavailable-price,
unmatched, ambiguous, legacy and already-imported reports remain intake-only.

The automated service runs the importer once per minute as `randal`, the existing
trusted owner used by Nocturne's database-writing API. Its systemd sandbox reads
the private intake through a narrow ACL and permits SQLite journal writes in the
database directory. The public web intake retains no access to live databases.

Install it once from the checked-out development branch:

```sh
sudo python3 -B dev/intake/install_importer.py
```

The installer verifies the inspected paths, refuses to overwrite an existing
unit, blocks the public intake from `/srv/projects/database`, grants only `randal`
read access to the private intake, runs a non-writing candidate preview as that
account, verifies the units, and enables the timer. Any failure before completion
removes the new units and ACL entries and restores the inspected intake unit.

Because `RegularSubmissions.db` is about 1.5 GB, timer runs use `--no-backup` for
these reversible pending inserts. SQLite still protects each batch with an
immediate transaction and its normal rollback journal. Manual `--apply` keeps
the full SQLite backup unless the restricted service explicitly disables it.

The preview accepts timezone-qualified ISO timestamps and the existing API's
SQLite `CURRENT_TIMESTAMP` format (`YYYY-MM-DD HH:MM:SS`), interpreted as UTC
according to [SQLite's specification](https://www.sqlite.org/lang_createtable.html#the_default_clause).
It does not reinterpret these timestamps using Midgard's local timezone or
replace stale timestamps with the current time.
