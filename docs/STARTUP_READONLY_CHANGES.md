# Read-only application startup: migrations separated from the app

Approved Nov 15, 2026 (Option A: an eighth, dedicated test suite). Local change for review only:
not pushed, merged or deployed; no Neon database was contacted.

## What changed

Before: every database request went through `app.ensure_schema()`, which ran
`db_schema.apply_schema(conn)` (all of `BASE_SCHEMA_SQL`: CREATE/ALTER TABLE, CREATE/DROP INDEX,
CREATE OR REPLACE FUNCTION/TRIGGER, UNIQUE index rebuilds) once per server process, with the
rights of whatever role the connection string named.

After:

| Part | Behaviour |
|---|---|
| `app.verify_schema()` (replaces `ensure_schema`) | Calls only `db_schema.check_schema()`. Never migrates. Cached per process on success; a failure is not cached, so the app recovers on the next request after an administrator migrates. |
| `db_schema.check_schema()` (new) | `SET TRANSACTION READ ONLY`, confirms `transaction_read_only = on` from the server, runs catalog SELECTs only, always rolls back. PostgreSQL itself refuses DDL or writes there (SQLSTATE 25006). |
| `db_schema.REQUIRED_SCHEMA` and related lists (new) | What this app version needs: 7 tables and all their columns; exact type, NOT NULL and default of the five consent and version columns (consent must default to FALSE); 5 named indexes; 2 UNIQUE and 2 CHECK constraints on the recommendation and audit tables; the audit function (must raise) and both audit triggers (right events, right function, enabled, not replica-only). |
| Schema incomplete or weakened | ICJ-ERR-511 (same code and user message), no database page served, nothing written. The server log names each missing object (names only, never row data). |
| Structure complete, email uniqueness not verified (duplicates) | Unchanged behaviour: app serves with app-level duplicate checks, ICJ-ERR-518 in the log and one telemetry row (a data row, not DDL, written after the read-only check). The app no longer creates the UNIQUE index itself, even after cleanup: the next administrator migration does. |
| `db_schema.apply_schema()` | Unchanged. Its only caller is the administrator tool `scripts/check_migration.py` (unchanged), run after explicit authorization. |
| `diagnostics` | ICJ-ERR-511 description: "Database schema check failed or schema not migrated for this app version (an administrator must run the migration; the app never migrates)". Stage `startup.schema_migration` renamed `startup.schema_check`. Class name `SchemaMigrationError` kept. |
| `scripts/schema_fingerprint.sql` (new) | Read-only catalog fingerprint (schemas, tables, columns, indexes, constraints, triggers incl. enabled state, functions, sequences' owners and grants, extensions, event triggers). No row data. For before/after comparison on staging and in tests. |
| System Checks tab | New "Database schema: compatible (read-only check; the app never migrates)" line; the duplicate-email note now says the constraint is added by the next administrator migration (not "on the next app restart"). |

Data, authentication, authorization, candidate consent, recommendations and the append-only
audit log behave exactly as before; every query the app runs is unchanged.

## How "no DDL at startup" is proven (tests/test_startup_readonly.py, 36 checks)

No single piece of evidence is relied on. In each phase the suite combines:

1. **PostgreSQL READ ONLY transaction** around the check; injected DDL and an injected write are
   refused with SQLSTATE 25006.
2. **Before-and-after schema fingerprints** (`scripts/schema_fingerprint.sql`).
3. **Server log with `log_statement = 'ddl'`** for the test database. PostgreSQL logs each DDL
   statement as it starts, outside any transaction, so a rolled-back attempt still leaves
   evidence. A positive control proves this: DDL that is rolled back appears in the log and
   leaves no table. (Event-trigger log rows were not used: a rollback would remove them.)
4. **The adapter's statement log** (every statement the app sent), also non-transactional.
5. **Least privilege**: the full workflows run as `icj_app` (no CREATE on the schema or
   database, owns nothing), and also as the table owner, who has every right a migration needs.

Mutation testing: each of these changes made the suite fail: restoring the `apply_schema` call;
rolled-back DDL on every request (also with the SQL text disguised so the static check misses
it, and as a TEMP table that fingerprints don't show: caught by the server and adapter logs);
removing the READ ONLY transaction; dropping a column from `REQUIRED_SCHEMA`; accepting a
replica-only trigger; not checking the consent default; not checking the audit function body;
continuing despite missing objects (in `check_schema` or in `app.py`); not checking the UNIQUE
constraints.

## Existing assertions that changed, and why

The 398 existing checks are all kept (same suites, same counts, all passing). 13 application
checks changed (1 in `test_e2e.py`, 9 in `test_diagnostics.py`, 1 in `test_units.py`, 2 in
`test_db_roles.py`); `test_recommendations.py` changed only in setup; 3 reporter checks were
updated for the new totals. None was removed or weakened.

| Suite: check | Before | After | Why it had to change |
|---|---|---|---|
| `test_e2e.py`: data check after the first public page load | "app startup migration ran on the legacy copy without altering legacy rows" | "administrator migration of the legacy copy kept legacy rows unchanged; app startup (read-only check) changed nothing" (same assertion: legacy rows byte-identical) | The app no longer migrates. Setup now runs the migration as the separate administrator step (`migrate_as_administrator`, the same `apply_schema` that `check_migration.py --apply` runs) before the app starts. |
| `test_recommendations.py`: setup and "existing candidate has no consent after migration" | The app migrated on the first intake | Migration runs in setup, after the existing candidate is inserted; assertion unchanged | Same reason; the check still proves an existing candidate gets no consent from the migration. |
| `test_diagnostics.py`: scenario 4 (5 checks via `check_scenario` + 3 checks) | "migration failure": a dropped primary key made the startup migration fail; stage `startup.schema_migration`; cause "there is no unique constraint…" | "schema not migrated": the read-only check refuses the legacy schema; stage `startup.schema_check`; cause "Missing: column beta_employers.password_hash" | A startup migration can no longer fail, because there is none. The scenario keeps testing the same things: ICJ-ERR-511 only (never 504/512/501), traceback and cause in the server log, nothing sensitive shown or logged, no rows written. |
| `test_diagnostics.py`: recovery | "after fixing the cause, the next request migrates and registration succeeds" | "after the administrator migration, the next request passes the check and registration succeeds (failure was not cached)" | Recovery now comes from the administrator migration, not the app; it still proves failures are not cached. |
| `test_units.py`: codes check message | "511 = schema migration" | "511 = schema check failed / not migrated" | Message text only; the assertion logic (code values, classes) is unchanged. |
| `test_db_roles.py`: app under `icj_app` at startup | "CURRENT app as icj_app: startup migration needs owner rights -> ICJ-ERR-511, nothing written" | "app as icj_app: read-only startup check passes without owner rights; no DDL sent, schema unchanged" | This check documented the limitation the change removes. It now proves the opposite on the same role: no owner rights needed, no DDL sent (statement log), schema fingerprint unchanged, the intake saved. |
| `test_db_roles.py`: workflows under `icj_app` | Ran with `apply_schema` monkeypatched to a simulated verify-only function | Run against the real read-only startup code (simulation deleted) | The simulated change now exists; testing the real code is stronger. Assertions unchanged. |
| `test_db_roles.py`: label of one refused statement | "add column if not exists (startup migration)" | "add column if not exists (as a migration would)" | Label only. |
| `test_ci_summary.py`: expected totals | 398; `[41, 34, 72, 33, 59, 69, 90]` | 434; `[…, 36]` | The eighth suite adds 36 checks. |
| `test_ci_summary.py`: CLI good-run check | "All seven suites passed: 398/398", seven passed rows | "All eight suites passed: 434/434", eight passed rows | Same reason. |

The CI reporter's own suite (`test_ci_summary.py`) now has 52 checks: the 51 existing ones,
plus one generated automatically by its existing missing-suite loop for the eighth suite.

## Risks

1. **Deploy order.** The new app refuses (ICJ-ERR-511) a database that is not migrated, which is
   an outage. Every deployment must run the administrator migration first. Production also
   lacks the c20a682 objects; `check_migration.py` deliberately refuses production, so a
   production migration needs its own, separately approved procedure before the code reaches it.
2. **Keeping the list in step.** A future column added to `BASE_SCHEMA_SQL` but not to
   `REQUIRED_SCHEMA` would let the app start and fail on its first query. The sync checks fail
   CI in that case (both directions).
3. **Schema damage after start-up** is detected only at the next server start (success is
   cached per process). Same as before.
4. **Scope.** This removes DDL from normal operation. The app still writes data in normal
   requests, as it must.
5. **Untested error path.** If the ICJ-ERR-518 telemetry insert itself fails, the app logs it
   and carries on. That path is not exercised by a test.

## Staging verification (each step needs its own authorization)

`staging-test-gate` was migrated on Nov 14, 2026 (reported by the owner; independently
verified). Do not repeat the migration unless a verified deficiency requires it and the owner
explicitly authorizes it.

1. Read-only: run `scripts/schema_status.sql` on `staging-test-gate`; all 11 markers present.
2. Read-only: run `scripts/schema_fingerprint.sql`; keep the output.
3. Deploy the candidate branch to the staging app (Streamlit can't switch an app's branch:
   delete and redeploy), with the secret pointing at `staging-test-gate`, ideally as `icj_app`.
4. System Checks shows "Schema compatible"; run the main workflows (STAGING_DEPLOYMENT.md step 7).
5. Read-only: run `scripts/schema_fingerprint.sql` again; every line must match step 2.

Production is not involved.
