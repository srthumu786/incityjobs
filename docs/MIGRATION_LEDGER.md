# Migration version ledger and guarded runner (B01-T1)

Authorized Nov 16, 2026. Local branch `b01/t1-migration-ledger` from `9486780`, for independent
review. Not pushed, merged or deployed; no Neon database contacted; no credentials changed.

## Pieces

| File | Role |
|---|---|
| `db_migrations.py` | Migration definitions and rules, ledger table, advisory lock, schema verification. No Streamlit. Never imported by the app. |
| `migrations/` | Future migrations, `NNNN_short_name.sql`, one transaction-safe script each (rules in `migrations/README.md`). Empty today. |
| `scripts/migrate.py` | The administrator command. Same target guards and TLS as `scripts/check_migration.py` (it reuses them). |
| `tests/test_migration_ledger.py` | 123 checks, the ninth suite. |

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

**Each successful row records a schema snapshot** (`detail.schema_snapshot`: SHA-256 and
the object list). The snapshot holds the sorted, normalized definitions of every table, column
(type, NOT NULL, default), constraint (definition, VALID), index, trigger (definition, enabled)
and function (signature, language, return type, SECURITY DEFINER, SHA-256 of the body) in the
schema, except the ledger's own objects. Catalog only, no row data.

**Drift detection.** `--status`, `--apply` and the post-commit re-verification compare the live
catalog with the newest recorded snapshot. Any difference is **SCHEMA DRIFT (exit 7)**, listed
object by object, whichever version the database is at. With drift, `--apply` runs nothing,
unless `--accept-drift` is given after review and approval; then the pending migrations run and
the accepted differences are recorded in their ledger rows. Reverting the change by hand clears
the drift. (Tested with a simulated version 2: a hand-restored v1 function, a changed default,
a dropped or disabled audit trigger, and an added index or column are all detected.)

**Times come from the database's clock, never the administrator's computer.** Each migration's
start and finish are read with `clock_timestamp()` on the database server, and the duration is
computed by the database from them. (Correction of Nov 16: the first version took the start
time from the computer running the tool, so a computer clock ahead of the database made every
`--apply` fail on the ledger's `finished_at >= started_at` rule.)

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

## Privileges versus missing objects

All structure checks read PostgreSQL's own catalog (`pg_attribute`, `pg_class`, `pg_constraint`,
`pg_index`, `pg_trigger`, `pg_proc`), which shows every object whatever the role's privileges.
(Correction of Nov 16: `information_schema` hides columns a role may not read, so the hash
columns were reported "missing" to `icj_readonly`, and the ledger "untrusted" to both app roles.
The app's own start-up check used the same query and is corrected too.)

Before doing anything, the runner checks what the connecting role may do and reports a lack as
**INSUFFICIENT PRIVILEGE (exit 6)**, worded as "this is not a schema problem":

| Mode | Needs |
|---|---|
| `--status` | SELECT on the ledger, if it exists |
| `--baseline` | CREATE on the schema if there is no ledger yet; SELECT and INSERT on the ledger if there is |
| `--apply` / dry run | CREATE on the schema; ownership of every existing app table (directly or by role membership); SELECT and INSERT on the ledger |

A permission error raised by a migration itself (42501) is also reported as exit 6. It is
rolled back like any failure, and the attempt is recorded with `success = false`.

## Locking

`pg_try_advisory_xact_lock` on a fixed key, taken first in the migration transaction. A second
runner exits 4 at once instead of waiting. Transaction-scoped, so it is released at commit or
rollback and also works through Neon's transaction-mode pooler. (Advisory locks are per
database.) `lock_timeout` 10 s and `statement_timeout` 120 s still apply to everything else.

**The older tool on ledgered databases (correction of Nov 16).** `scripts/check_migration.py` is
now only for databases WITHOUT a ledger. Inside its single transaction it:

1. takes the same advisory lock first, never waiting: exit 4 if `migrate.py` holds it;
2. only then checks for `schema_migration_ledger`: exit 2 if it exists, for both the dry run and
   `--apply`. The message points to `migrate.py`;
3. only then runs its migration, exactly as before.

The order makes the check safe. `migrate.py` creates the ledger only while holding the lock, in
its main transaction, in a baseline, or when recording a failure. So a ledger is either already
committed, and the old tool sees it once it has the lock, or it is being created under the lock
and the old tool is refused by the lock. There is no moment between the check and the old tool's
schema changes in which a ledger can appear. Tested: a ledger created but not yet committed under
the lock (exit 4), then committed (exit 2), with nothing changed either time. On a database without
a ledger the old tool works exactly as before (tested).

**Fence, for older copies of the tool.** A copy of `check_migration.py` from before this
correction takes no advisory lock. `migrate.py` therefore also takes `LOCK TABLE beta_employers
IN SHARE UPDATE EXCLUSIVE MODE` (3 s wait). That mode doesn't block the app's reads and writes,
but it conflicts with any DDL on the table, and every version of the old tool starts with
`ALTER TABLE beta_employers`. So:

- an old copy that is already running makes `migrate.py` exit 4;
- an old copy started while `migrate.py` runs waits for it to finish.

An old copy run AFTERWARDS on a ledgered database is not prevented. Its changes are caught by the
drift check below.

**Still open:** once staging and production are ledgered, retire the old tool's `--apply` (keep
its dry run for databases without a ledger) or remove the tool. That needs a separate approval.

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
- **No ledger and existing objects that DIFFER from version 1** (as opposed to objects that are
  simply missing): the dry run and `--apply` list every deviation first, then refuse with
  **exit 7**. Nothing is repaired silently. `--repair-version1-drift`, given after review, lets
  version 1 overwrite what it defines (functions, triggers, indexes) and records the repaired
  deviations. Deviations version 1 cannot fix, such as a changed column default, still fail the
  verification, and everything rolls back. A database with only missing objects (legacy, or the
  Nov 13 partial state) migrates as before.
- **`--baseline [--apply]`**: only for a database with no successful ledger rows. Verifies the
  actual structure first, strictly (next section). Only if everything matches does it create the
  ledger and record version 1 as `baselined` (no migration SQL runs). A partly migrated, weakened,
  altered or duplicate-blocked schema is refused and nothing is created.

## Override options: separate administrative approval required

`--accept-drift` and `--repair-version1-drift` override safety refusals (exit 7). Each requires
**separate administrative approval for each use**, given after the listed differences have been
reviewed. They must **not** be used on the Neon staging branch or on production without explicit
written authorization naming that database and the differences being accepted or repaired.
Approval to run migrations is not approval to use these options. So far they have only been run
against throwaway test databases in `tests/test_migration_ledger.py`.

## Exact version-1 definitions (`db_migrations.VERSION1_*`)

These are **checks of the existing version-1 schema only**: exactly what `BASE_SCHEMA_SQL` and
the UNIQUE email indexes create. They add nothing to any table. They were generated from a freshly
migrated database, and are identical for a migrated legacy copy and for the partly migrated Nov 13
state; the suite checks this in both directions.

| Checked | Count |
|---|---|
| Columns: type (with length), NOT NULL, default | 86 |
| Primary keys | 7 |
| Foreign keys, including ON DELETE (SET NULL / CASCADE); must be VALID | 5 |
| UNIQUE constraints | 2 |
| CHECK definitions (recommendation status, audit actor type) | 2 |
| Index definitions (columns, expression, condition, uniqueness), valid/ready/live | 7 |
| Trigger definitions (events, level, WHEN condition, function), enabled | 2 |
| Audit function body, return type, language | 1 |

Extra columns, indexes or constraints on these tables, such as the old optional `email` column,
are allowed. They are printed as notes and kept in the ledger row's `allowed_extras`. The strict
comparison applies while the newest version is 1 (baselining, `--apply` of version 1, `--status`,
re-verification). After a later version is applied it no longer applies, because a version 2 may
legitimately change a version-1 object. The app's start-up check (`REQUIRED_*`) is unchanged and
less strict by design.

### Constraints proposed for future migrations (NOT checked, NOT applied)

Possible tightenings found while writing the definitions. Each would need its own reviewed
migration and data clean-up first:

| Table | Possible constraint | Why it isn't in version 1 |
|---|---|---|
| `semantic_matches` | FK `candidate_profile_id` → `candidate_profiles(id)` | legacy ledger rows have no candidate id |
| `privacy_audit_log` | FKs on `candidate_profile_id`, `employer_id`, `job_id`, `recommendation_id` | the audit log must outlive deleted rows |
| `beta_employers` | `admin_email` NOT NULL | legacy employers may lack one |
| `candidate_profiles` | CHECK on `application_status` values | legacy values not yet surveyed |
| `employer_jobs` | `match_radius_miles` > 0 CHECK | not yet decided |

Exit codes: 0 done, 1 not complete (nothing committed), 2 refused by target guards, 3 committed
but re-verification failed, 4 lock or fence held by another runner or tool, 5 integrity
problem, 6 insufficient privilege, 7 schema drift or deviation (explicit action required).

## How future migrations define and verify their expected schema

Version 1's expected result is `VERSION1_*`. Every later version must declare its own expected
result **before it is applied anywhere**. The rule:

1. **Migration file:** `migrations/NNNN_name.sql`, transaction-safe. For now, a function body
   must be written on one line: the file safety check refuses any line that begins with `BEGIN`
   or `END`, indented or not.
2. **Expected result:** a `VERSION_CHANGES[N]` entry in `db_migrations.py`, in the same
   normalized forms as `VERSION1_*`, listing what version N adds, changes and removes:
   - columns: type, NOT NULL, default;
   - constraints: table, kind, definition, VALID;
   - indexes: definition;
   - triggers: definition including timing, events, level, WHEN and function, plus the enabled
     state;
   - functions: signature, language, return type, SECURITY DEFINER, and the whitespace-normalized
     body.

   The expected schema after version N is `VERSION1_*` with changes 2..N applied in order. A
   version that redefines a version-1 object, such as the audit function, lists it as changed.
3. **Verification before commit:** after the pending migrations run, the runner compares the live
   catalog with that expected schema exactly, as it already does for version 1. Any mismatch rolls
   everything back.
4. **Recorded after commit:** the catalog snapshot in the ledger row. From then on, drift
   detection compares against it.
5. **Tests in the same commit:** migrating an empty database, a legacy copy and the current
   staging-shaped state through version N must each produce exactly the expected schema; the
   startup suite's `REQUIRED_SCHEMA` sync step migrates through version N; and the app's
   `REQUIRED_*` are updated if the app depends on the change.

Steps 2, 3 and 5 are **not implemented yet**, because no version 2 exists. They are a
precondition for adding the first `migrations/0002_*.sql`. Until then, a later version is
verified only against `REQUIRED_*` before commit and protected by the recorded snapshot
afterwards.

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
- `scripts/check_migration.py` still works (baseline only, no ledger) and is fenced off as
  described above. The proposal for it is above.
- Production still needs its own approved procedure; both tools refuse the production endpoint.

## Known limitations (after the Nov 16 corrections)

- **Older copies of the old tool (from before this correction)** take no shared lock and don't
  check for the ledger. They are held off only by the fence while `migrate.py` runs, and only when
  `beta_employers` exists. Run afterwards, they can still change a ledgered database. That is
  caught as drift (exit 7) but not prevented. Use only the current tool.
- **Snapshots start with this code.** Ledger rows written before this correction have none, so
  for them drift detection reports "none recorded yet" until the next migration or baseline.
- **Any role can take the advisory lock.** That includes `icj_app`: it blocks migrations (exit 4)
  but cannot damage anything.
- **The file safety check is a pattern.** It would wrongly refuse a PL/pgSQL function body
  containing lines that start with `BEGIN` or `END`, and cannot see statements built dynamically
  inside `DO` blocks.
- **Checksums are byte-exact.** Line-ending conversion (CRLF) shows up as MODIFIED.
- **No exact expected schema for version 2 or later yet:** see "How future migrations define and
  verify their expected schema". A version 2's result is currently checked only against
  `REQUIRED_*` before commit, though after commit any change is caught as drift.
- **The app's start-up check is less strict.** It doesn't see, for example, a `WHEN (false)`
  condition added to an audit trigger; the strict baseline check does.
- **The ledger is not tamper-proof against the owner or a superuser,** who can drop or recreate it.
- **Untested:**
  - the real psycopg2 driver and Neon (including its pooler) — all tests use the local adapter and
    the Neon API stand-in;
  - a dropped connection mid-run, or a crash while holding the lock;
  - very large tables, where the 120 s statement limit applies.
