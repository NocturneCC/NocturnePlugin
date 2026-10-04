# Nocturne Challenges source adoption (baseline snapshot)

This directory is a repository-owned adoption of the live Challenge
configuration/editor workflow as observed on 2026-10-01. Its initial imported
files were byte-preserving where noted below; explicitly listed repository
extensions are not represented as byte-equivalent to their live baselines. The
Challenge backend, website, bot, and API directories were deployment trees,
not Git repositories. This snapshot is the reviewable source baseline; it is
not installed or activated by this commit.

## Source map

- `service/challenge_config.py`: canonical draft/config schema, validation,
  immutable publication and audit implementation. Its SQL schema is embedded
  in this module; there is no separate checked-in JSON Schema or SQL migration
  that defines the Challenge document.
- `service/challenge_intake_api.py` and its sibling imports: authenticated
  internal draft/publication API and approved direct-intake compatibility.
- `service/challenge_config_api.py`, `challenge_member_view.py`, and the
  registration fragment: public active-config and member-view blueprint.
- `service/leaderboard_*.py`: existing observation projection and public
  leaderboard consumers; these are included to preserve the established
  import/projection contract. `service/leaderboard_proof_404.json` is the
  resolver's immutable, schema-versioned historical-404 decision catalog
  (observation IDs, source indexes, HTTP-404 assertion and URL digests), not a
  generated leaderboard/cache snapshot. It is required to preserve projection
  behavior and is hash-pinned to its live source.
- `website/challenge-admin.html` and `challenge-admin-state.js`: live admin
  editor and deterministic browser-side draft helpers. The CSS, navbar,
  favicon and complete boss-icon catalog consumed by that page are included.
  The live `styles.css` has trailing horizontal whitespace removed, and
  `nocturne-global.css` has two redundant blank lines removed at a pinned
  location and EOF. These deterministic whitespace-only normalizations are
  checked against pinned sources; stylesheet rules/declarations are unchanged.
- The timing/capture metadata change is a repository-owned extension in
  `service/challenge_config.py`, `tests/python/test_challenge_config.py`,
  `website/challenge-admin-state.js`, `website/challenge-admin.html`, and
  `website/tests/challenge-admin-state.test.js`. For these five files the
  manifest records the unchanged live file as the provenance baseline while
  explicitly marking the repository file as an extension; it does not claim
  byte equivalence. No activity timing mappings are inferred: legacy time
  definitions normalize to `unconfigured` and `manual_only`, while numeric
  definitions remain `manual_only` until their meaning is configured.
- The admin editor calls the stored `overall` timing scope “Overall time” and
  explains it as the full activity completion time. It calls stored `segment`
  “Room time”: the combined total of timed rooms/encounters, not an individual
  room record. “Unconfigured” remains unchanged. These are display labels only;
  they do not rewrite stored enum values or configure an activity. The intended
  future mapping is Theatre of Blood Hard Mode as `overall` and regular Theatre
  of Blood as `segment`; no active configuration is changed here.
- `service/challenge_automatic_intake.py` and
  `AUTOMATIC_OBSERVATIONS.md` define the repository-only RuneLite completion
  observation boundary. The client reports bounded observations and public
  RSNs; the server selects policy and metric from the active Challenge
  configuration. No RuneLite parser, production migration, or deployment is
  included in this source change.
- `integration/routes/`: bounded source excerpts for the authenticated admin
  proxy (`admin_app_challenge_routes.fragment.py`), public blueprint
  registration, navigation link, and the existing approved Challenge intake
  Nginx include. The admin proxy excerpt captures the existing `staff`
  authorization, bearer-forwarding boundary, draft lifecycle, and artwork
  routes; the full shared `admin_app.py` hash and supporting line ranges are
  pinned as an external reference. The excerpts are documentary integration
  inputs, not standalone files to install.
- `integration/units/`: Challenge intake and admin/API unit inputs. The
  active admin unit differs from the service-tree copy; both are retained with
  distinct names so that drift is visible, not silently reconciled.
- `consumers/nocturne-bot/`: exact bot-side config, metric, recipient,
  Midgard-intake, and manual Challenge/drop submission consumers, with Node
  tests retained under the source-relative `tests/` directory.
- `tests/python/` plus the website and bot `tests/` directories: existing
  focused tests copied unchanged from the live source trees and the adoption
  integrity tests.

`source-manifest.json` deterministically binds the included files and copied
live files to SHA-256, file type, numeric owner/group, mode, link count and ACL
fingerprint. It also records source-file hashes for the documented route and
navigation excerpts and external source references. Regenerate it only when
intentionally updating this source snapshot:

```sh
sudo /usr/bin/python3.14 -B dev/challenges/prepare.py --refresh-manifest
```

The normal invocation is a dry-run against HEAD and requires a matching
published `development` commit and clean checkout. An explicit full SHA can be
provided to pin that dry-run. It verifies repository content and live source
hashes/metadata. It has no production apply mode; future deployment must use a
separately reviewed immutable-release installer.

```sh
sudo /usr/bin/python3.14 -B dev/challenges/prepare.py --commit <full-commit-sha>
```

Root is used for the final `--commit` dry-run so the verifier observes the
real numeric owner/group and ACL metadata rather than an unprivileged
namespace's mapped IDs. Git is
invoked with a per-command `safe.directory` exception; no Git configuration is
written. `--verify-bundle` validates local content and the pinned live source
metadata; it does not change either.

The manifest currently records `metadata_capture=namespace-view-not-authoritative`
because this authoring sandbox cannot perform interactive sudo. Consequently
the final `--commit` gate intentionally refuses this snapshot until an operator
refreshes the manifest from the real host root view, reviews the resulting
manifest diff, and reruns the exact dry-run. This does not block local bundle
tests, but the snapshot must not be committed/published before that gate passes.

## Data intentionally excluded

Never add the following to Git: `Challenges.db`, `Members.db`, any SQLite
journal/WAL/SHM file, `db/challenge_config_lkg.json`, published/draft database
rows, runtime configuration or `.env` files, `/etc/nocturne/*` credentials,
uploaded `website/media/challenge-bosses/` artwork, `__pycache__`, bytecode,
logs, reports, generated challenge/leaderboard projections, or service runtime
state. The resolver decision catalog above is the sole reviewed static JSON
data dependency in this tree; do not replace it with a runtime export or add
other generated JSON.
The LKG file is generated by the bot's active-config consumer and is not the
canonical editor source. The active version-10 equivalence check is recorded
as a digest-only evidence note, not as a copied config document.

## Deployment/rollback boundary

This adoption is source-only. A future deployment must build from a verified
commit into an immutable release; verify every staged file hash and exact live
pre-state before touching a target; make metadata-preserving backups outside
the release; install atomically; validate the Challenge API and admin routes;
and restore the complete prior file set if validation fails. No active unit,
Nginx route, website file, database, credential, LKG cache, or `current`
symlink is changed by `prepare.py`.

The editor is `/challenge-admin.html`; its authenticated API is proxied by the
existing admin application and forwards to the Challenge intake service. The
canonical configuration/version/audit tables are created and managed by
`challenge_config.py`; the config document at active version 10 was parsed
from the database read-only and compared with the consumer LKG document (equal
canonical digest recorded in `evidence/config-v10-equivalence.json`). The
bot's `challenge_config_lkg.json` is generated consumer state and is excluded.

The active `Challenges.db` remains the sole published-config state. Updates
must continue through the admin draft → validate → diff → explicit publish
workflow. Do not edit the LKG or active database directly as part of source
adoption.

### Timing-metadata deployment support

`deploy_timing_metadata.py` is a guarded, default-dry-run installer for the
repository-owned timing/capture fields. It must be run only from a clean,
published `development` checkout whose exact commit is also prepared as
`/srv/nocturne-plugin/releases/<commit>`. The tool accepts the full commit SHA;
it verifies the prepared release and its release/source manifests again before
apply. The commit containing this tool is a direct child of the timing-metadata
source baseline `4a48efec3149dd66e55fb02371e2aaeaf966a31f`. Preparing or applying
that later tool commit is a separate operator action; this source commit does
neither.

The only installed source files are the immutable-release copies of:

- `dev/challenges/service/challenge_config.py` →
  `/srv/projects/nocturne-services/challenge_config.py`
- `dev/challenges/website/challenge-admin.html` →
  `/srv/projects/website/challenge-admin.html`
- `dev/challenges/website/challenge-admin-state.js` →
  `/srv/projects/website/challenge-admin-state.js`

The live files must still match their pinned source-manifest SHA-256, owner,
group, mode, single-link and ACL profile. Replacement is same-directory,
atomic, fsynced, and preserves each file's existing ownership, mode and ACL.
The tool does not change Nginx, systemd unit files, routes, the plugin `current`
link, or the Challenge LKG cache.

`challenge_config.py` is imported by `osrs-drops-api.service` (through
`challenge_config_api.py`) and `nocturne-challenge-intake.service` (through
`challenge_intake_api.py`). Those two long-running consumers are the only
services restarted to load the changed Python module. During the additive
SQLite migration, the tool pauses the active timers for the Challenge shadow
sync and leaderboard shadow renderer, drains any currently running one-shot
jobs, and stops API/intake. It restores only the previously active API/intake
services and timers. The admin app does not import the changed
module and is not restarted; its short read-only SQLite operations are allowed
to drain against SQLite's exclusive migration lock. Nginx is not restarted.
The shadow-sync jobs are not rerun by the installer; their originally active
timers resume afterward and load the installed module on their next ordinary
run. The legacy Google Sheet Challenge progress importer is retired from
deployment control. Its script and unit remain available for recovery, but
future Challenge installers neither pause nor restore its timer/service.

Before migration the installer uses SQLite's online backup API to create and
verify a private, transactionally consistent `Challenges.db` backup under
`/var/backups/challenge-timing-metadata/<commit>/<transaction>/`. The migration
is limited to `ALTER TABLE challenge_config_bosses ADD COLUMN <field> TEXT` for
the seven nullable timing/capture fields; it performs no row updates, inserts,
publication, draft rewrite, or LKG write. It verifies integrity, active version
10, its normalized legacy defaults (`unconfigured`/`manual_only`), every user
table's row count, and the semantic active document. Any failed install,
migration, service start, or read-only runtime check restores the backed-up
database and all three file snapshots before restoring the prior unit state.
Backups are retained for operator review; the tool never prunes them.

Dry-run and apply interfaces (the placeholder must be a full exact SHA):

```sh
sudo /usr/bin/python3.14 -B \
  /srv/nocturne-plugin/releases/<commit>/dev/challenges/deploy_timing_metadata.py \
  --dry-run --commit <commit>

sudo /usr/bin/python3.14 -B \
  /srv/nocturne-plugin/releases/<commit>/dev/challenges/deploy_timing_metadata.py \
  --apply --commit <commit>
```

The expected brief service interruption is limited to the two Challenge API
consumers and the two scheduled Challenge writers while SQLite schema DDL is
performed. The installer must report `status=already_current` on a repeat
without rewriting files or creating another backup.

## Legacy CSV-backed progress retirement

The former `/nocturne-challenge-progress.html` page and the member-viewer
summary card consume the old CSV-import contract. The replacement
`website/nocturne-challenge-progress.html` redirects the former page to
`/challenges.html` (or `/challenge-member.html?rsn=...`). The deterministic
transforms in `website/legacy_challenge_retirement.py` retarget internal links
and remove the member-viewer card that calls the retired summary API. These are
source artifacts for a separately reviewed website deployment and were not
applied to the live website by this change.

The public `/api/nocturne-challenges/bosses` compatibility route now derives
its catalog from the active published Midgard config. Legacy `summary`,
`progress`, and `leaderboards` response shapes are not semantically equivalent
to the current projection model; they return bounded HTTP 410 responses with
replacement routes and `Cache-Control: no-store`, never a stale CSV snapshot.
The public Challenge blueprint is registered before the legacy inline handlers
in the production API, so its exact routes take precedence after the source is
installed. Do not restore the old inline handlers as fallbacks.

Current config, member progress, leaderboard, RuneLite, bot, and projection
reads use published config versions, `challenge_legacy_baselines`, accepted
submissions, and derived projection tables, not the importer-refreshed summary,
progress, or leaderboard rows. Those three tables remain referenced only by
historical migration/import/review tooling. `challenge_bosses` is a retained
compatibility key catalog maintained from published configuration and used by
existing submission foreign keys; it must not be dropped, and is no longer
authoritative from Google imports. This change neither drops nor rewrites any
of the four tables. The sheet-sync script and unit remain on disk for recovery,
but are no longer captured, paused, restored, or reported by future Challenge
deployment helpers.

### Guarded legacy progress retirement deployment

`deploy_legacy_challenge_retirement.py` is default-dry-run and bound to the
exact retirement commit. It reads only the matching prepared release for the
API source, redirect page, and deterministic website transforms. Before apply
it requires the clean published checkout and canonical immutable `--check`,
the pinned prior live hashes and ACL/metadata profiles, and healthy active
configuration/leaderboard state. It makes root-private file backups plus a
SQLite online backup; the database is never migrated or restored because this
deployment does not write it. Only `osrs-drops-api.service` is restarted: its
`api:app` imports `challenge_config_api.py`. Nginx is unchanged.

The sheet-sync timer is paused only for this retirement transaction and then
returned to its captured active/enablement state. A running one-shot is allowed
to drain naturally. Because rerunning it could rewrite retired CSV-derived
tables, an apply that captured the one-shot already running aborts before file
installation, restores the timer/API prestate, and asks for a retry after the
one-shot is inactive. On any later failure the helper restores the exact file
backups atomically and restores the API and timer states. The service is not
disabled and its unit/script remain available for recovery; future general
Challenge deployment control sets continue to exclude both legacy units.

After publishing this installer commit, Simon should use that exact commit SHA
for preparation and deployment so the installer itself, retirement sources,
and support interfaces all come from one prepared immutable release. Replace
the placeholder consistently below with that full SHA. Preparation and apply
are operator actions and were not run while preparing this source change:

```sh
sudo /bin/bash \
  /srv/projects/nocturne-plugin-intake/dev/intake/prepare_immutable_runtime.sh \
  --prepare <commit>

sudo /bin/bash \
  /srv/nocturne-plugin/releases/<commit>/dev/intake/prepare_immutable_runtime.sh \
  --check <commit>

sudo /usr/bin/python3.14 -B \
  /srv/nocturne-plugin/releases/<commit>/dev/challenges/deploy_legacy_challenge_retirement.py \
  --dry-run --commit <commit>

sudo /usr/bin/python3.14 -B \
  /srv/nocturne-plugin/releases/<commit>/dev/challenges/deploy_legacy_challenge_retirement.py \
  --apply --commit <commit>
```

The apply result prints a transaction-record path. If an operator later
explicitly requests reversal, the matching immutable helper accepts that
record with `--rollback-record <printed-transaction.json> --commit <commit>`;
rollback verifies every saved
backup and refuses database-state or live-file drift.
