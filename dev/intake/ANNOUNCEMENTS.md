# Clan announcement support

This source is prepared but is not installed or activated by a commit.

## Data and access boundary

`announcements.py` owns three schema-version-1 tables in the existing
`event_schedule.db` and two append-only audit triggers:

- `plugin_announcement_meta`
- `plugin_announcements`
- `plugin_announcement_audit`
- `plugin_announcement_audit_no_update`
- `plugin_announcement_audit_no_delete`

Existing event, milestone, and notes rows are not reused. Every create, draft
edit, publish/schedule, withdrawal, or explicit expiration increments the global
revision and writes its announcement change and immutable audit row in one SQLite
transaction. The event database remains available only to its existing trusted
processes.

The trusted admin side exports a public-data-only JSON snapshot with a forced
temporary-file write and atomic rename under
`/srv/projects/nocturne-plugin-announcements-public/`. The isolated intake binds
that dedicated directory read-only and never opens or traverses
`/srv/projects/database`. Binding the SQLite file alone is intentionally
prohibited: a file bind is pinned across database replacement and cannot expose
rollback journals, hot-journal recovery, or WAL/SHM sidecars.

## Public endpoint

`GET` or `HEAD /api/plugin/v1/announcements` returns deterministic bounded JSON
for the current revision, with an ETag and boundary-aware `Cache-Control` that
never caches through the next activation or expiration. The payload contains
only schema version, global revision, generated timestamp, and at most three
currently active announcements. Each announcement contains only its stable ID,
revision, optional title, message, severity, start/expiration timestamps, and an
optional structured allowlisted HTTPS link.

The endpoint accepts no request body and receives no RSN, account/profile value,
chat, raid data, telemetry, read receipt, or persistent client identifier. Normal
HTTP operation still exposes the caller's IP address to `nocturne.events`.

## Guarded administration

`announcement_backend_support.py` is dry-run by default. It checks the database
journal mode and integrity, reports exact file hashes and schema objects, and
does not alter the active admin or database. An applying run requires `--apply`,
`--maintenance-confirmed`, and both exact `--stopped-service` confirmations. The
operator must stop `osrs-drops-admin.service` and `osrs-drops-api.service`; both
can write or replace event-schedule state. The tool then:

1. captures UID/GID/mode/ACL metadata;
2. creates and verifies a consistent SQLite backup and exact admin-file backup;
3. stages and compiles the repository-owned announcement module and minimal
   authenticated blueprint registration;
4. creates only the dedicated schema objects transactionally;
5. creates an initial bounded public snapshot in its dedicated directory;
6. atomically installs the staged source while preserving metadata; and
7. verifies the final schema, hashes, and metadata.

The admin routes retain the active `require_auth`, event-admin role check, and
trusted admin-name resolver. Request bodies reject duplicate and unknown fields.
Edits and transitions require the exact current announcement revision so
concurrent administrators cannot silently overwrite one another. Listing
includes current/historical announcements and their audit entries. State changes
are transactional. The tool never restarts a service.

Dry run:

```sh
python3 -B dev/intake/announcement_backend_support.py
```

After a separately approved maintenance window and verified writer shutdown:

```sh
sudo python3 -B dev/intake/announcement_backend_support.py --apply \
  --maintenance-confirmed \
  --stopped-service osrs-drops-admin.service \
  --stopped-service osrs-drops-api.service
```

The output identifies the exact verified backup. Independent rollback requires
the same writer shutdown and explicit confirmation:

```sh
sudo python3 -B dev/intake/announcement_backend_support.py \
  --rollback-backup /exact/verified/backup --maintenance-confirmed \
  --stopped-service osrs-drops-admin.service \
  --stopped-service osrs-drops-api.service
```

## Nginx support

`nginx-announcements-location.conf` is an exact-match location that permits only
GET/HEAD, buffers and rejects request bodies, applies existing per-IP and total
rate limits, uses bounded proxy timeouts, and passes through ETag, 304, content
type, and cache headers. It proxies only to the isolated intake on loopback.

`announcement_route_support.py` is dry-run by default. Apply makes an exact
metadata-preserving backup, atomically stages one route, runs `nginx -t`, and
restores the verified original if validation fails. It never reloads or restarts
Nginx. The verified backup supports independent rollback, which also syntax-tests
the restored file but never reloads Nginx.

```sh
python3 -B dev/intake/announcement_route_support.py
sudo python3 -B dev/intake/announcement_route_support.py --apply
sudo python3 -B dev/intake/announcement_route_support.py \
  --rollback-backup /exact/verified/backup
```

Activation remains a separate operator decision: prepare an immutable release,
inspect the generated service unit, apply the guarded backend and route changes,
perform explicit service/Nginx control outside these tools, then validate public
200/304/HEAD behavior. Roll back the route, backend, and immutable release along
their independent boundaries if validation fails.
