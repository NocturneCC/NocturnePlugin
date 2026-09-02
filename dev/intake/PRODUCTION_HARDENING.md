# RuneLite production hardening: phase 1

Phase 1 prepares a fail-closed points interlock without changing the current
RuneLite client, development endpoint, authentication, socket, screenshots or
service configuration.

`production_review_hardening.py` is dry-run-only unless `--apply` is supplied.
It patches the active regular-submission API and review page, then applies these
objects to `RegularSubmissions.db` in one exclusive transaction:

- partial unique index `uq_regular_submissions_external_id_nonnull`;
- append-only table `runelite_review_transitions`;
- insert/update triggers rejecting `approved` for `runelite` and
  `runelite_pilot` rows;
- update/delete triggers protecting the audit table;
- an index on audit submission and transition IDs.

The audit table records submission ID, previous/new status, the authenticated
reviewer identity supplied by the existing Cottus-backed admin path, UTC
transition time and action. It contains no cookie, session, secret, payload or
screenshot fields.

Before mutation the installer requires exact source markers, a consistent old
or applied state, safe target metadata, SQLite integrity, no duplicate non-null
external IDs and sufficient backup space. It makes and verifies file and
consistent SQLite backups before mutation, compiles the staged admin file with
the active interpreter, atomically activates staged files, and restores all
targets after any safely catchable failure or interruption once mutation starts.
The verified backup can be independently restored with `--rollback-backup`.
Neither mode controls services.

Deployment order after separate approval:

1. Stop the intake and writer to prevent pending-row writes during the exclusive
   migration and verified backup.
2. Leave the admin API stopped during file/schema activation so no reviewer can
   race the migration.
3. Run the default dry run and record its reported state and duplicate count.
4. Run with `--apply`; do not interrupt the large database backup or checks.
5. Run the default dry run again and require `state=already_applied`.
6. Run Python tests, the writer database check, staged admin syntax validation
   and `systemd-analyze verify` before any restart.
7. Start the admin API first and verify development and pilot approval attempts
   return conflict without changing either points store.
8. Start the writer and intake, then verify denial/audit and ordinary approval.

Rollback stops the same services, runs `--rollback-backup` against the exact
verified backup directory, confirms the old file/schema state and database
integrity, and only then restarts the prior services. Backups are never deleted
automatically.
