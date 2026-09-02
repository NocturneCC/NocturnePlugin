# Isolated development intake

Prepared for Midgard; not deployed by committing this directory. This is server
code, excluded from the RuneLite JAR. It uses Python's standard library as a WSGI
application and gunicorn as the HTTP server. No Flask/live API modifications.

POST `/api/plugin/dev/drops` accepts only the reporting RSN, item IDs/quantities,
source, timestamp, schema version and event UUID. Extra fields (including group
names) are rejected. Test RSNs are allowlisted; **this is not authentication**.
There is no member lookup, points processing, public read endpoint or Discord login.
Claims remain unverified. The optional local writer can route eligible reports
into the existing review queue as pending proposals; it never approves them or
updates rank totals.

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
**Send drops to test intake** in RuneLite. Only `Simons Alt` and `RoatBefAuJu`
are allowed by the supplied service. There is no scoring or group-name upload on
this route.

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

Payload version 3 may also include one bounded JPEG captured from the RuneLite
canvas after the user explicitly enables screenshot attachments. The plugin
requests the next rendered frame only for a locally
price-eligible loot event. Chat is excluded by default by cropping to the game
viewport; users must explicitly enable full-canvas capture. The intake verifies
the JPEG markers, dimensions, SHA-256 digest and decoded 240 KiB limit. Image
bytes are never stored in the public intake SQLite database. The private writer
persists an image only when it creates an eligible pending submission, using an
administrator-authenticated URL; excluded loot retains no image.
The private directory must be owned by the writer with mode `0700` and is capped
at 2,000 JPEGs. At the cap, pending submissions continue without an attachment.

After pulling the reviewed screenshot release, install the server integration:

```bash
sudo python3 -B dev/intake/install_screenshot_support.py
```

The installer first runs the full intake test suite, verifies all four services,
compiles the staged admin API, and validates nginx. It backs up `admin_app.py`
and the active nginx site, changes only the existing intake body limit, installs
the `noc_super_admin` image route, and rolls both files and services back on any
failure.

### Pending-submission preview and manual importer

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

Manual `--apply` keeps a full SQLite backup. The automated timer/ACL importer was
retired after its ACL changed `/var/lib/private` to a mode that conflicts with
systemd `DynamicUser` state directories. Do not enable the obsolete
`nocturne-plugin-import.timer` unit.

### Local pending writer

`pending_writer.py` replaces the retired timer with a Unix-socket handoff. The
public intake stores and validates the report, then sends only its canonical
payload to `/run/nocturne-plugin-writer/pending.sock`. The writer independently
validates it again, reads the existing member/item catalogues, and may insert an
idempotent `status=pending`, `source_type=runelite` proposal. It does not read the
private intake database, approve submissions, update `rank_totals`, or listen on
a TCP port.

The writer runs as `randal`, matching the existing trusted database-writing API.
Its systemd sandbox has no network namespace and permits writes only beneath the
database directory for SQLite's database and rollback-journal operations. The
public `DynamicUser` receives `InaccessiblePaths=/srv/projects/database`; it can
reach the world-connectable local socket but cannot open Midgard's databases.

Install once from the checked-out `development` branch:

```sh
sudo python3 -B dev/intake/install_writer.py
```

The installer first runs a read-only schema check as `randal`. It backs up the
installed unit files under a unique `/etc/nocturne-plugin-backups/unix-writer-*`
directory, disables
and removes the obsolete timer units, verifies both replacement services, starts
the writer, waits for its socket, then hardens and restarts the public intake.
Any failure restores the previous intake and obsolete unit files. It does not
change ACLs or permissions under `/var/lib/private`.

Eligible ordinary and fixed-reward reports enter review with the same conservative
1x proposal used by the manual importer. Group and event context remain unresolved.
Low-value items, legacy payloads, synthetic tests, inactive/unmatched identities,
unknown items and unavailable prices remain intake-only. If the writer is down,
the intake keeps the idempotent event but returns HTTP 503; sending the same UUID
again retries the handoff.

The preview accepts timezone-qualified ISO timestamps and the existing API's
SQLite `CURRENT_TIMESTAMP` format (`YYYY-MM-DD HH:MM:SS`), interpreted as UTC
according to [SQLite's specification](https://www.sqlite.org/lang_createtable.html#the_default_clause).
It does not reinterpret these timestamps using Midgard's local timezone or
replace stale timestamps with the current time.
