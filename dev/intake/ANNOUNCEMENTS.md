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

The source tree now also contains `plugin-announcements-admin.html`, a small
responsive authoring page linked into the existing Event Admin Tools navigation
by `announcement_admin_ui_support.py`. The page uses the existing authenticated
announcement API; it adds no login/session scheme. Mutating requests require an
exact first-party HTTPS `Origin` and reject cross-site Fetch Metadata. The page
uses DOM `textContent` for announcement content and offers a separate confirm
step before publish/schedule or withdrawal. The API is the final authority for
all validation.

The supported editor schema is exactly six fields:

| Field | Accepted values |
|---|---|
| `title` | optional plain text, at most 80 characters and one line |
| `message` | required plain text, at most 500 characters and four lines |
| `severity` | `info`, `notice`, `warning`, or `urgent` |
| `starts_at`, `expires_at` | timezone-aware ISO-8601 timestamps, normalized to UTC seconds; start must precede expiry |
| `link` | `null`, or exactly `{label,url}`; label at most 48 plain-text characters and URL at most 256 characters |

The only link destinations are `https://nocturne.events/` and
`https://nocturne.events/event-board.html`. The API also accepts no arbitrary
JSON fields, duplicate keys, HTML/RuneLite markup, control characters, or
stale edit revisions. IDs are server-generated 32-character UUID hex values;
announcement revisions and the global revision are server-managed integers.
The public snapshot root remains exactly `schema_version`, `revision`,
`generated_at`, and `announcements`; the public route further filters to at
most three active items and returns a valid empty list when none are active.

### Isolated snapshot publication

The admin API process cannot safely write the public snapshot directory. The
blueprint therefore sends only the bounded, server-generated snapshot document
to a fixed Unix socket; it never sends browser JSON to that socket. The
`announcement_snapshot_writer.py` daemon authenticates both peer UID and
effective GID (`randal:www-data`), validates the exact versioned schema again,
and can write only the one fixed snapshot in
`/srv/projects/nocturne-plugin-announcements-public/`. It has no database,
Discord, or network access. The socket is owned by `randal:randal`, mode 0600;
the daemon requires peer UID `randal` and GID `www-data`. Its only writable
namespace path is `/run/nocturne-announcement-public`, a bind of the exact
public snapshot directory. All of `/srv/projects` is inaccessible in the
writer namespace. The snapshot and directory remain `randal:www-data`, modes
0644 and 0755. The inherited ACL is checked against the established host profile
(including the observed named `glob` grant and masks); each replacement inherits
and verifies that exact file ACL. Unexpected ACL entries fail closed. The admin
service itself masks the public snapshot directory because its code publishes
only through the socket and the public reader is the separate plugin intake.
Atomic publication uses a same-directory temporary file, file and directory
fsync, atomic rename, and strict metadata, ACL, schema, link-count, and read-back
checks. No `sudo` is invoked by the web application. The persistent writer
identity is never `nobody:nogroup`.

The code and units are prepared by `announcement_publication_support.py` (dry
run by default). Its apply operation requires root, the exact clean source
commit, explicit maintenance confirmation, and confirmation that
`osrs-drops-admin.service`, `nocturne-announcement-snapshot-writer.service`, and
`nocturne-announcement-snapshot-writer.socket` are inactive. It preserves a
verified backup and does not reload systemd or control services. After an approved install, the operator
must run `systemctl daemon-reload`, start/enable
`nocturne-announcement-snapshot-writer.socket`, verify its socket permissions,
and then start the admin service. Rollback is explicit and restores only the
verified API module, writer code, socket/service units, and admin-service
hardening drop-in (`osrs-drops-admin.service.d/20-nocturne-announcement-snapshot-writer.conf`); it requires the same three stopped-unit confirmations and likewise
does not reload or control services. The guarded commands are:

```sh
python3 -B dev/intake/announcement_publication_support.py --commit <full-sha>
sudo python3 -B dev/intake/announcement_publication_support.py --commit <full-sha> \
  --expected-module-sha256 <sha256-reported-by-dry-run> \
  --apply --maintenance-confirmed \
  --stopped-service osrs-drops-admin.service \
  --stopped-service nocturne-announcement-snapshot-writer.service \
  --stopped-service nocturne-announcement-snapshot-writer.socket
sudo systemctl daemon-reload
sudo systemctl enable --now nocturne-announcement-snapshot-writer.socket
```

Rollback uses only the exact backup reported by apply:

```sh
sudo python3 -B dev/intake/announcement_publication_support.py --commit <full-sha> \
  --rollback-backup /exact/verified/backup --maintenance-confirmed \
  --stopped-service osrs-drops-admin.service \
  --stopped-service nocturne-announcement-snapshot-writer.service \
  --stopped-service nocturne-announcement-snapshot-writer.socket
```

The website page/navigation are installed separately by
`announcement_admin_ui_support.py`, dry-run by default. Apply preserves the
existing `admin.html` owner/group/mode/ACL, backs it up, atomically adds the
single navigation card, and creates the static page using matching safe
metadata. It does not change Nginx or restart a service. Rollback verifies the
installed hashes before restoring/removing those two exact files. The page is
static and reveals no announcement data by itself; all data and actions remain
behind the existing authenticated, event-admin API.

After publishing this repository commit, prepare and apply the website change
separately; the first command is read-only:

```sh
python3 -B dev/intake/announcement_admin_ui_support.py --commit <full-sha>
sudo python3 -B dev/intake/announcement_admin_ui_support.py --commit <full-sha> --apply
```

No visual authoring tool was present in the deployed website inventory; the
authenticated announcement API and schema already existed. The source-owned UI
is available after the separate guarded website install at
`https://nocturne.events/plugin-announcements-admin.html`.

Safe manual smoke test after deployment: create a draft whose title begins
`[TEST ONLY]`, message says it is a temporary admin-tool test, and expiry is a
few minutes after its start. Verify the chat/sidebar previews, save it as a
draft, then use the explicit publish confirmation only in an approved test
window. Confirm the public RuneLite endpoint contains only its supported fields
while active, then use **Unpublish** and verify the endpoint returns an empty
announcement list. The action is audited; do not use real operational copy for
this test.

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
