# Automatic-observation deployment procedure

This support tool is a separately reviewed, root-only installer. It does not
prepare an immutable release. Its source and database schema always come from
the exact full commit supplied to the tool, and that commit must already be
published, clean on `development`, and pass the canonical immutable
`prepare_immutable_runtime.sh --check` gate. The operator must prepare that
commit separately before applying it.

## Exact live deployment map

| Immutable source | Live target | Supported predecessor |
| --- | --- | --- |
| `dev/challenges/service/challenge_automatic_intake.py` | `/srv/projects/nocturne-services/challenge_automatic_intake.py` | Absent only |
| `dev/challenges/service/challenge_intake_api.py` | `/srv/projects/nocturne-services/challenge_intake_api.py` | Exact source-manifest baseline only |
| `dev/challenges/service/leaderboard_challenge_ingest.py` | `/srv/projects/nocturne-services/leaderboard_challenge_ingest.py` | Exact source-manifest baseline only |
| `dev/challenges/integration/routes/nocturne-challenge-intake.location.conf` | `/srv/projects/nocturne-services/nginx/nocturne-challenge-intake.location.conf` | Exact source-manifest baseline only |

The active HTTPS site already includes the last path at
`/etc/nginx/sites-enabled/nocturne`; this deployment changes only the included
fragment, never the site file. The target fragment retains the approved intake
location and adds exactly one `POST /api/challenges/intake/observations`
location to `127.0.0.1:5011`.

When the include changes, apply runs `nginx -t`, records the active master PID,
reloads Nginx, and invokes the immutable release's
`dev/intake/nginx_reload_convergence.py` for a bounded 10-second worker
generation turnover check before any routed smoke request. Rollback applies
the same convergence check when it reloads Nginx.

The `nocturne-challenge-intake.service` unit runs
`challenge_intake_api:app` from `/srv/projects/nocturne-services`. The API
imports the automatic intake module for observation requests. The
`nocturne-leaderboard-shadow-renderer.service` oneshot imports
`leaderboard_challenge_ingest.py` from its `ExecStartPre`; its timer is paused
and the job drained while SQLite is quiesced, but the deployment does not
force-run the projection. The public configuration and leaderboard read APIs
are imported by `osrs-drops-api.service` and their sources do not change.

`osrs-drops-api.service`, `nocturne-challenge-intake.service`, and
`osrs-drops-admin.service` are stopped only as the established SQLite-holder
maintenance set. The three Challenge writer timers are paused, and active
oneshot jobs are drained. Unknown SQLite holders or persistent/changed
sidecars abort; the installer never unlinks a live sidecar. Services/timers
are restored to their captured safe pre-state. The intake process necessarily
starts from the replaced source; the API/admin are restored only because they
were stopped for the database maintenance window.

## Database effects and rollback

The only migration is `challenge_automatic_intake.migrate_schema()` from the
immutable release. It creates the two exact observation tables, indexes, and
immutability triggers described in `AUTOMATIC_OBSERVATIONS.md`; it does not
alter existing Challenge config, manual submissions, leaderboard tables, or
award data. The migration is idempotent and the helper rejects partial or
different schemas. A SQLite online backup is made after all known holders have
stopped and sidecars have naturally disappeared. No external checkpoint,
sidecar deletion, active config publication, or manual/leaderboard write is
performed.

The root-only transaction directory is beneath
`/var/backups/challenge-automatic-observations/<full-commit>/`, mode 0700. It
contains mode-0600 file/SQLite backups and a private transaction record. Any
failure before automatic-observation data exists restores the exact backed-up
database image, files, route fragment, and captured service/timer state. If an
observation row appears, rollback refuses to drop/replace the new schema or
erase data and leaves the transaction record for operator recovery.

The only active HTTP mutation probe is a deliberately unauthenticated request
to the existing approved-intake route; it must return 401 and produces one
normal bounded auth-failure audit entry. Observation probes are only `{}`
(expected documented `422 invalid`), wrong content type (415), and an
oversized body (413). Row counts must remain zero. No syntactically valid
observation or manual submission is sent. The auth-probe audit increment is
the only permitted row-count difference; manual submissions, leaderboard
rows, active config identity, and observation counts are invariant.

## Commands

Default mode and `--dry-run` are read-only. The helper does not call
`--prepare`.

```sh
sudo /usr/bin/python3.14 -B dev/challenges/deploy_automatic_observations.py \
  --dry-run --commit <exact-prepared-full-commit-sha>
sudo /usr/bin/python3.14 -B dev/challenges/deploy_automatic_observations.py \
  --apply --commit <exact-prepared-full-commit-sha>
sudo /usr/bin/python3.14 -B dev/challenges/deploy_automatic_observations.py \
  --rollback-record /var/backups/challenge-automatic-observations/<sha>/<transaction>/transaction.json \
  --commit <exact-transaction-full-commit-sha>
```

The first command must be reviewed before any apply. Apply and explicit
rollback acquire the same `/run/nocturne-challenge-timing-deploy.lock` used by
the established Challenge timing installer, re-plan under the lock, and refuse
unsupported source, target, schema, metadata, ACL, service, or database state.

## Read-only validation performed while authoring this helper

The published feature baseline was `2ce4362f229418f9a9d63477b85da70a99309f72`.
The sandbox observed the existing API, leaderboard-ingest, and Nginx include
bytes matching the corresponding source-manifest predecessor digests. The new
automatic-intake module was absent at that predecessor. The read-only SQLite
connection reported integrity `ok`, `journal_mode=delete`, one active config
version, and no observation tables. Host ACL ownership and systemd state could
not be treated as authoritative inside the no-new-privileges sandbox; the
helper keeps those checks mandatory, and no production dry-run/apply or
immutable preparation was performed here.
