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
