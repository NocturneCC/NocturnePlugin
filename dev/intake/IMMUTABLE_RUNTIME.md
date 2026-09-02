# Immutable intake runtime

`immutable_runtime_release.py` is dry-run-only unless an explicit mode is
selected. It resolves a full Git commit, uses `git archive` rather than the
working tree, rejects links and unsafe archive entries, hashes every release
file, and prepares root-owned read-only source beneath
`/srv/nocturne-plugin/releases/<full-commit-sha>`.

The generated units retain the committed sandboxing, resource limits, socket,
database isolation and exact two-RSN development allowlist. Their working
directory is `/srv/nocturne-plugin/current/dev/intake`; Python and Gunicorn come
from `/srv/nocturne-plugin/venv`. Activation atomically switches `current` and
stages unit replacements, but deliberately does not reload systemd or control a
service. Its activation record contains the previous release SHA and verified
unit backups for guarded rollback.

## Runtime virtual environment

Never copy the development `.venv`: Python virtual environments embed absolute
paths and may contain host- or interpreter-specific binaries. Build the shared
runtime environment independently with the host's managed Python 3.14, the
committed `runtime-requirements.txt`, and an operator-reviewed local wheelhouse.
For a reproducible deployment, generate a hash-locked requirements file from
that wheelhouse, create the venv directly at its final versioned path, install with
`pip --require-hashes --no-index --find-links <wheelhouse>`, run `pip check`,
verify imports and permissions, then atomically select it as
`/srv/nocturne-plugin/venv`. Never rename a venv: generated launchers contain
absolute interpreter paths. The tool invokes Gunicorn as `python -m gunicorn`
and requires exactly Gunicorn 26.2.0.

`prepare_immutable_runtime.sh` is the root-only preparation entry point. It is
read-only with `--check`; `--prepare` builds the release, builds or validates the
exact venv, and stages units without changing `current`, active units, databases,
or services:

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
  --commit "$SHA" --recover-incomplete-venv "$TARGET"
sudo python3.14 -B dev/intake/immutable_runtime_release.py \
  --commit "$SHA" --recover-incomplete-venv "$TARGET" --apply-recovery
sudo /bin/bash dev/intake/prepare_immutable_runtime.sh --prepare "$SHA"
```

Recovery accepts either a valid new incomplete marker or the narrowly verified
legacy failure whose Gunicorn shebang points at an absent
`.venv-<40-hex-commit>.<pid>/bin/python`. It refuses every other unmarked state.
Quarantined environments remain beneath
`/srv/nocturne-plugin/quarantine/incomplete-venvs/` for operator inspection and
are not removed automatically.

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
