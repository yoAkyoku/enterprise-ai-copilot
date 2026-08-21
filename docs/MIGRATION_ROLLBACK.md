# Migration and rollback procedure

The production Compose profile uses forward-compatible PostgreSQL migrations;
the repository also retains SQLite migrations for local and explicit
single-node deployments. Operators must record the exact image, migration set
and backup for the selected adapter.

## Preflight

1. Confirm the target image digest, current schema version and migration list.
2. Verify a recent encrypted backup can be read and that its integrity check
   passes in an isolated environment.
3. Review migration SQL for tenant/workspace scope, indexes, lock duration and
   backward compatibility with the currently running image.
4. Announce the maintenance window and pause schedules or writes that are not
   idempotent.

## Apply and verify

Run `scripts/migrate_postgres.py` against the production database (or
`scripts/migrate.py` for explicit SQLite mode), capture stdout, exit code and
schema version, then verify integrity, startup, `/health`, `/ready`,
authenticated reads, audit writes, queue delivery and attachment metadata
access. Keep the previous immutable image available until the smoke tests and a
short observation window pass.

## Rollback

Prefer rolling back the application image while retaining a backward-compatible
schema. Do not reverse a migration by hand when it could discard data. If the
database itself must be restored, stop writes, preserve the failed database and
audit evidence, restore the last verified encrypted backup into an isolated
database, run integrity and scope checks, and switch traffic only after an
operator approves the recovery record. Reconcile post-backup queue and provider
operations by idempotency key and external receipt.

## Evidence

Record backup digest, migration files, image digest, start/end times, operator,
verification commands, rollback decision and any data-loss assessment. A
successful process exit without a restore test is not proof that recovery works.
