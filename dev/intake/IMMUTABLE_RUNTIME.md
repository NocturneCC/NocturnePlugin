# Immutable intake runtime

Every repository preparation/helper command first requires a clean checkout
whose `HEAD` is the exact lowercase 40-character SHA supplied by the operator.
Abbreviations, local changes, untracked files and mismatched checkouts are
rejected. `immutable_runtime_release.py` is dry-run-only unless an explicit mode
is selected. It uses `git archive` rather than the
working tree, rejects links and unsafe archive entries, hashes every release
file, and prepares root-owned read-only source beneath
`/srv/nocturne-plugin/releases/<full-commit-sha>`.

One commit-scoped staging operation produces the immutable intake unit,
immutable writer unit, emoji synchronizer service, emoji timer and emoji Nginx
route as a coherent set. The units retain the committed sandboxing, resource
limits, socket, database isolation and exact two-RSN development allowlist.
Code comes only from `/srv/nocturne-plugin/current`; Python comes from verified
versioned environments beneath `/srv/nocturne-plugin/venvs/`. No generated unit
names the mutable checkout or its `.venv`.

Staged units live at `/srv/nocturne-plugin/staged-units/<full-sha>` and the
route at `/srv/nocturne-plugin/staged-nginx/<full-sha>`. Each has a SHA-256
manifest bound to the matching release manifest. Activation verifies all
release, staging and runtime records again, atomically switches `current`, and
replaces all four units and the Nginx file as one guarded operation. Its record
contains the prior selector and every replaced file. Systemd supplies exact
inactive/PID-zero evidence before and immediately before mutation; a Boolean
confirmation alone is insufficient. Apply also requires the digest emitted by
the read-only preflight. Failure restores the entire prior set; rollback failure
reinstates the verified applied set. Fsynced `prepared` and
`rollback_prepared` records support explicit recovery after process or host
failure. Neither staging nor activation reloads a daemon or controls a service.

## Runtime virtual environment

Never copy the development `.venv`: Python virtual environments embed absolute
paths and may contain host- or interpreter-specific binaries. Build the shared
runtime environment independently with the host's managed Python 3.14, the
committed `runtime-requirements.lock`, and the single root-owned mode-0444
Gunicorn wheel in its versioned wheelhouse. The lock and wheel digest are fixed
in source. Create the venv directly at its final versioned path and install with
`pip --require-hashes --no-index --find-links <wheelhouse>`, run `pip check`,
verify imports and permissions, and keep it at
`/srv/nocturne-plugin/venvs/python3.14-gunicorn-26.2.0`. Never rename a venv:
generated launchers contain absolute interpreter paths. Generated units name
the versioned environment directly. The tool invokes Gunicorn as
`python -m gunicorn` and requires exactly Gunicorn 26.2.0.

The emoji environment is independently prepared at
`/srv/nocturne-plugin/venvs/emoji-python3.14-pillow-12.3.0`. Architecture,
CPython ABI and glibc are checked before target creation or pip. Only the
binary-only hash lock and exact verified Pillow wheel are accepted. A mode-0600
incomplete marker remains until imports, versions, ownership, modes, ACLs,
links, mounts, launchers and the dependency record pass. Recovery verifies and
quarantines the exact incomplete artifact instead of deleting it.

The wrapper runs the emoji CPython version, ABI, host architecture and glibc
preflight before it creates a release or invokes either dependency installer.
Both installers use no index, required hashes, binary-only inputs and exact
versioned single-wheel directories. Missing, extra, linked, mounted, writable,
incorrectly owned or ACL-extended inputs fail closed.

`prepare_immutable_runtime.sh` is the root-only preparation entry point. It is
read-only with `--check`; `--prepare` builds the release, builds or validates
both exact versioned environments, and stages all four units and the emoji route
without changing `current`, active units/routes, databases, or services:

```bash
cd /srv/projects/nocturne-plugin-intake
sudo /bin/bash dev/intake/prepare_immutable_runtime.sh --check FULL_SHA
sudo /bin/bash dev/intake/prepare_immutable_runtime.sh --prepare FULL_SHA
```

The venv is created at
`/srv/nocturne-plugin/venvs/python3.14-gunicorn-26.2.0` with a root-owned mode
0600 `PREPARATION_INCOMPLETE` marker. The marker remains after interruption or
validation failure. A completed target has a verified `VENV-MANIFEST.json` and
no incomplete marker; reruns validate and reuse it. Ordinary venv links are
accepted only when they resolve within that venv or to `/usr/bin/python3.14`.
Dangling links, escaping links, mounts, unsafe ACLs, unexpected ownership and
unknown pre-existing targets fail closed.

Recovery never deletes the target. First verify the exact incomplete target in
dry-run mode, then explicitly move it into the root-only quarantine:

```bash
cd /srv/projects/nocturne-plugin-intake
SHA=FULL_SHA
TARGET=/srv/nocturne-plugin/venvs/python3.14-gunicorn-26.2.0
sudo python3.14 -B dev/intake/immutable_runtime_release.py \
  --repo /srv/projects/nocturne-plugin-intake --commit "$SHA" \
  --recover-incomplete-venv "$TARGET"
sudo python3.14 -B dev/intake/immutable_runtime_release.py \
  --repo /srv/projects/nocturne-plugin-intake --commit "$SHA" \
  --recover-incomplete-venv "$TARGET" --apply-recovery
sudo /bin/bash dev/intake/prepare_immutable_runtime.sh --prepare "$SHA"
```

Recovery accepts either a valid new incomplete marker or the narrowly verified
legacy failure whose Gunicorn shebang points at an absent
`.venv-<40-hex-commit>.<pid>/bin/python`. It refuses every other unmarked state.
Quarantined environments remain beneath
`/srv/nocturne-plugin/quarantine/incomplete-venvs/` for operator inspection and
are not removed automatically.

## Ownership inspection

`runtime_ownership.py` reports the runtime root, release, venv and staging
ownership separately from selector symlink ownership and resolved-target
ownership. Its optional migration is dry-run by default and can change only an
enumerated set of exact runtime container nodes from a specified UID/GID to
root. It rejects links, mounts, unexpected modes or owners and never recursively
chowns unresolved paths. Immutable release source must already be root-owned
and read-only; service-user-owned source is rejected and must be rebuilt. Apply
requires UID 0 and exact source UID/GID values, rechecks every selected inode,
device, link count, mode, mount and ACL immediately before changing it, and
rolls back a partial failure. The exact `current` selector may be lchowned
without following it, but its ownership is never treated as target ownership.
Run this maintenance only while the four plugin
units are inactive even though the exact directory-mode preservation is designed
not to change access to the current release.

Future read-only checks for a published SHA are:

```bash
SHA=FULL_SHA
sudo /bin/bash dev/intake/prepare_immutable_runtime.sh --check "$SHA"
sudo python3.14 -B dev/intake/runtime_ownership.py \
  --repo /srv/projects/nocturne-plugin-intake \
  --runtime-root /srv/nocturne-plugin --commit "$SHA"
sudo python3.14 -B dev/intake/immutable_runtime_release.py \
  --runtime-root /srv/nocturne-plugin --commit "$SHA" --check-deployment
sudo python3.14 -B dev/intake/emoji_runtime_release.py \
  --runtime-root /srv/nocturne-plugin --python /usr/bin/python3.14 \
  --requirements "/srv/nocturne-plugin/releases/$SHA/dev/intake/emoji-sync-requirements.txt" \
  --wheel /srv/nocturne-plugin/wheelhouse/emoji-python3.14-pillow-12.3.0/pillow-12.3.0-cp314-cp314-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl
```

Standalone emoji-unit or route repair is not an alternate activation path.
Each helper requires the matching applied activation record and revalidates the
current selector, immutable release, both staging manifests and every artifact
outside its narrow repair scope. Unit repair additionally verifies all four
plugin units are inactive.

## One maintenance window

1. Publish and select the reviewed full commit SHA; run release preparation in
   dry-run mode.
2. Stop intake, writer and admin once. Record submission/rank baselines.
3. Run Phase 1 dry-run, apply Phase 1, and rerun its dry-run expecting fully
   applied state.
4. Run retention dry-run, apply retention, and rerun its dry-run expecting six
   retention objects and schema version 1.
5. Require SQLite integrity, verified backup manifests, exact file metadata and
   ACLs, and a passing writer `--check`.
6. Prepare the immutable release, validate its manifest and runtime venv, then
   activate its symlink and staged unit files. Run `systemd-analyze verify`
   before the separately approved daemon reload.
7. Start admin first and prove RuneLite approval returns conflict without rank
   changes. Start writer, require its socket, then start intake.
8. Run the bounded synthetic compatibility/idempotency verifier. Keep screenshot
   cleanup in dry-run mode.

For full rollback, keep all services stopped, use the activation record to
select the previous code release, roll retention back with its exact verified
backup, verify Phase 1 remains applied, then roll Phase 1 back if required.
Verify hashes, ACLs, six-to-zero retention objects, seven-to-zero Phase 1
objects, SQLite integrity and the previous writer compatibility before starting
admin, writer and intake in that order.
