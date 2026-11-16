# Migration version ledger and guarded runner (B01-T1)

Authorized Nov 16, 2026. Local branch `b01/t1-migration-ledger` from `9486780`, for independent
review. Not pushed, merged or deployed; no Neon database contacted; no credentials changed.

## Pieces

| File | Role |
|---|---|
| `db_migrations.py` | Migration definitions and rules, ledger table, advisory lock, schema verification. No Streamlit. Never imported by the app. |
| `migrations/` | Future migrations, `NNNN_short_name.sql`, one transaction-safe script each (rules in `migrations/README.md`). Empty today. |
| `scripts/migrate.py` | The administrator command. Same target guards and TLS as `scripts/check_migration.py` (it reuses them). |
| `tests/test_migration_ledger.py` | 100 checks, the ninth suite. |

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

**Fence against the older tool.** The advisory lock only stops other `migrate.py` runs. To stop
`scripts/check_migration.py` (which takes no advisory lock) from changing the schema at the same
time, `migrate.py` also takes `LOCK TABLE beta_employers IN SHARE UPDATE EXCLUSIVE MODE` (3 s
wait) in the same transaction. That mode doesn't conflict with the app's reads and writes, but it
does conflict with any DDL on the table, and the old tool's migration always starts with
`ALTER TABLE beta_employers`. So:

- old tool already running: `migrate.py` can't take the fence and exits 4, having done nothing;
- `migrate.py` running: the old tool's first ALTER waits (up to its 10 s `lock_timeout`) until
  `migrate.py` commits or rolls back. Then it either runs on the finished schema or, if it
  waited too long, rolls back all-or-nothing.

The two never change the schema concurrently. Both cases are tested, including a real
`check_migration.py --apply` started while `migrate.py` holds the fence.

## Proposal for the older tool (not implemented; needs approval)

The safest path keeps `check_migration.py` usable for its reviewed purpose while making it unable
to diverge from the ledger:

1. **Now (no change to the old tool):** the fence above, plus a documented rule that
   `migrate.py` is the tool for every ledgered database.
2. **Next (small change, after review):** `check_migration.py --apply` takes the same advisory
   lock (`pg_try_advisory_xact_lock(db_migrations.LOCK_KEY)`, exit 4 if held), and refuses
   (exit 2) when `schema_migration_ledger` exists, pointing to `migrate.py`. Its dry run stays
   available as a read-only second opinion.
3. **Later:** once staging and production are ledgered, retire `--apply` from
   `check_migration.py` (keep the dry run), or remove the tool, and update
   `docs/STAGING_DEPLOYMENT.md`.

Not recommended: deleting the old tool now (its dry run is the reviewed path for the Nov 14
staging state), or having it write ledger rows (two writers of one audit record).

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
  actual structure first, strictly (next section). Only if everything matches does it create the
  ledger and record version 1 as `baselined` (no migration SQL runs). A partly migrated, weakened,
  altered or duplicate-blocked schema is refused and nothing is created.

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
problem, 6 insufficient privilege.

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

- **The fence relies on the old tool's first statement.** It works because the old tool's migration
  always starts by altering `beta_employers`. On an empty database (no `beta_employers` yet)
  there is no fence; two tools creating the same tables at once would collide, and one would roll
  back.
- **Any role can take the advisory lock.** That includes `icj_app`: it blocks migrations (exit 4)
  but cannot damage anything.
- **The file safety check is a pattern.** It would wrongly refuse a PL/pgSQL function body
  containing lines that start with `BEGIN` or `END`, and cannot see statements built dynamically
  inside `DO` blocks.
- **Checksums are byte-exact.** Line-ending conversion (CRLF) shows up as MODIFIED.
- **Strict version-1 checks stop at version 1.** There are no per-version definition snapshots
  yet. When a version 2 exists, its expected catalog has to be added, and so does the startup
  suite's `REQUIRED_SCHEMA` sync step.
- **The app's start-up check is less strict.** It doesn't see, for example, a `WHEN (false)`
  condition added to an audit trigger; the strict baseline check does.
- **The ledger is not tamper-proof against the owner or a superuser,** who can drop or recreate it.
- **Untested:**
  - the real psycopg2 driver and Neon (including its pooler) — all tests use the local adapter and
    the Neon API stand-in;
  - a dropped connection mid-run, or a crash while holding the lock;
  - very large tables, where the 120 s statement limit applies.
