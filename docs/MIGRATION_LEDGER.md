# Migration version ledger and guarded runner (B01-T1)

Authorized Nov 16, 2026. Local branch `b01/t1-migration-ledger` from `9486780`, for independent
review. Not pushed, merged or deployed; no Neon database contacted; no credentials changed.

## Pieces

| File | Role |
|---|---|
| `db_migrations.py` | Migration definitions and rules, ledger table, advisory lock, schema verification. No Streamlit. Never imported by the app. |
| `migrations/` | Future migrations, `NNNN_short_name.sql`, one transaction-safe script each (rules in `migrations/README.md`). Empty today. |
| `scripts/migrate.py` | The administrator command. Same target guards and TLS as `scripts/check_migration.py` (it reuses them). |
| `tests/test_migration_ledger.py` | 64 checks, the ninth suite. |

**Version 1 (`baseline_schema`)** is the schema `db_schema.apply_schema()` creates. Its checksum
(SHA-256 of `BASE_SCHEMA_SQL` plus the UNIQUE email index definitions) is pinned in
`db_migrations.BASELINE_CHECKSUM`. Editing `BASE_SCHEMA_SQL` is refused as "MODIFIED in code";
schema changes go in a new `migrations/NNNN_*.sql` file.

## The ledger: `schema_migration_ledger`

One row per attempt: `version`, `name`, `checksum` (SHA-256), `method` (`applied` or
`baselined`), `success`, `started_at`, `finished_at`, `execution_ms`, `applied_by` (database
role), `runner`, `git_commit` (of the checkout that ran it), `database_name`, `detail` (JSON:
verification summary and probe result, or for a failure only the error type and SQLSTATE; never
row data). A partial UNIQUE index allows one successful row per version. Triggers reject UPDATE,
DELETE and TRUNCATE (append-only, like `privacy_audit_log`). The app does not read it.

## Integrity rules (checked before anything runs; any problem: exit 5)

| Problem | Meaning |
|---|---|
| DUPLICATE (code) | Two files with one version, or a file claiming version 1 |
| MISSING in code | A gap in the versions (1, 2, 4) |
| MODIFIED in code | The pinned baseline changed |
| unsafe file | `BEGIN`/`COMMIT`/`ROLLBACK`/`SAVEPOINT`/`VACUUM`/`CONCURRENTLY`/... (would break the single transaction), empty or badly named file |
| MODIFIED | An applied version's checksum or name differs from the code |
| MISSING | The ledger records a version this code doesn't have (older code, or a removed file) |
| OUT OF ORDER | A version is unapplied while a later one is applied |
| DUPLICATE (ledger) | Two successful rows for one version |
| untrusted ledger | Missing columns, missing one-success index, or disabled append-only triggers |

## Locking

`pg_try_advisory_xact_lock` on a fixed key, taken first in the migration transaction. A second
runner exits 4 at once instead of waiting. Transaction-scoped, so it is released at commit or
rollback and also works through Neon's transaction-mode pooler. (Advisory locks are per
database.) `lock_timeout` 10 s and `statement_timeout` 120 s still apply to everything else.

## Behaviour

- **`--status`**: read-only transaction; ledger, pending versions, problems, actual schema.
- **Dry run (default)**: everything below in one transaction, then rolled back.
- **`--apply`**: lock, integrity check, run every pending migration in order, verify the actual
  schema (everything the app checks on start-up plus both UNIQUE email constraints active),
  run the duplicate-insert probe, write the ledger rows, and commit only if all of that
  passed. A migration error rolls back everything; then one `success = false` row records
  the attempt. After a commit, a read-only re-verification (exit 3 if it fails).
- **No ledger but a complete schema** (staging-test-gate's state): `--apply` refuses to re-run
  the baseline and says to use `--baseline`.
- **`--baseline [--apply]`**: only for a database with no successful ledger rows. Verifies the
  actual structure first. Only if it is complete does it create the ledger and record version 1
  as `baselined` (no migration SQL runs). A partly migrated, weakened or duplicate-blocked
  schema is refused and nothing is created.

Exit codes: 0 done, 1 not complete (nothing committed), 2 refused by target guards, 3 committed
but re-verification failed, 4 lock held by another runner, 5 integrity problem.

## Proposed use on staging (each step needs its own authorization; NOT done)

`staging-test-gate` was migrated on Nov 14 by `check_migration.py --apply` (no ledger). Recording
it needs no schema change:

1. `python scripts/migrate.py $T --status`: expected "no ledger. The schema is complete".
2. `python scripts/migrate.py $T --baseline`: dry run; nothing recorded.
3. With authorization: `python scripts/migrate.py $T --baseline --apply`. This creates only the
   ledger table (with its index, function and two triggers) and one row. It doesn't change any
   app table.
4. `--status`: "up to date". `scripts/schema_fingerprint.sql` before and after should differ
   only by the ledger objects.

## Not in this change (follow-ups)

- Roles on a ledgered database (checked locally): `scripts/apply_db_roles.py --apply` and
  `--verify` still pass. Neither `icj_app` nor `icj_readonly` can read or write the ledger. Only
  the ledger's id sequence gets USAGE for `icj_app`, through the existing "ALL SEQUENCES" grant;
  without INSERT on the table that grants nothing useful. Whether `icj_readonly` should read
  the ledger (e.g. for a System Checks line) is a separate decision.
- `tests/test_startup_readonly.py` keeps `REQUIRED_SCHEMA` in step with `apply_schema` (version
  1). When a version 2 exists, that check should migrate with all versions.
- `scripts/check_migration.py` still works (baseline only, no ledger). From now on use
  `scripts/migrate.py`; consider retiring the older tool after review.
- Production still needs its own approved procedure; both tools refuse the production endpoint.
