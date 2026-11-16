# InCityJobs migrations

Applied only by the administrator tool `scripts/migrate.py`, after explicit authorization.
The app never migrates (it runs a read-only schema check on start-up).

- Version 1 (`baseline_schema`) is the schema `db_schema.apply_schema()` creates. It lives in
  `db_schema.py`, is frozen, and its checksum is pinned in `db_migrations.py`. Do not edit
  `BASE_SCHEMA_SQL`: every ledgered database would report version 1 as MODIFIED.
- Every later change is a new file here: `NNNN_short_name.sql` (four digits, the next number,
  lower-case words joined by `_`), e.g. `0002_add_job_expiry.sql`.
- A file is one transaction-safe SQL script: no `BEGIN`, `COMMIT`, `ROLLBACK`, `SAVEPOINT`,
  `VACUUM` or `CONCURRENTLY`. All pending migrations run in one transaction with the ledger
  entries: everything commits, or nothing does.
- Never edit, rename, renumber or delete a file once it has been applied anywhere. Fix
  mistakes with a new migration.
- If a change needs `db_schema.REQUIRED_SCHEMA` (what the app checks on start-up) to change,
  update it in the same commit.
- Before the first file here is applied anywhere: add its expected resulting schema
  (`VERSION_CHANGES[N]` in `db_migrations.py`) and the tests that check it. See "How future
  migrations define and verify their expected schema" in `docs/MIGRATION_LEDGER.md`.
- For now, write function bodies on one line: the safety check refuses lines that begin with
  `BEGIN` or `END`.
