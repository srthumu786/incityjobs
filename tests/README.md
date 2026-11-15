# InCityJobs test suites

Seven suites, run together with one command against a **throwaway local PostgreSQL**
cluster. They never connect to Neon or any network database.

| Suite | What it covers |
|---|---|
| `test_units.py` | Recommendation status rules, anonymized-profile allow-list, bands and fingerprints; the matching engine's pinned behaviour (`docs/MATCHING_ENGINE_ASSESSMENT.md`, set in `matching_eval_set.py`); diagnostic codes and log redaction (URI passwords, `password=`, configured secrets, emails, hashes; traceback included), password hashing, signed session tokens, credential fingerprints (session revocation), setup codes, matching rules, legacy-fallback exclusion. No database. |
| `test_migration.py` | Starts from the schema the current `main` creates (`fixtures/legacy_main_schema.sql`) with messy data (mixed-case/space-padded duplicate emails, NULL/blank emails, fallback-point rows). Checks: dry run changes nothing; migration keeps every legacy row byte-identical; duplicates block the UNIQUE constraint and are reported; catalog verification catches wrong or non-unique indexes; `check_migration.py --apply` only reports completion after verification and a rolled-back probe. |
| `test_e2e.py` | Runs the whole `app.py` on a simulated Streamlit runtime against the real local database, starting from a legacy copy so the app's own startup migration runs. Covers forged/expired/wrong-role tokens, unverified employers, verification and revocation, session revocation on credential change (employers and managers), setup codes, lockout, geocoding without fallback, candidate privacy, XSS escaping, live matching vs simulation, legacy-fallback exclusion, telemetry, System Checks. |
| `test_diagnostics.py` | Startup diagnostics: missing `connection_string` and invalid credentials -> ICJ-ERR-500; migration failure -> ICJ-ERR-511; a real duplicate-check query failure -> ICJ-ERR-504. Startup errors are never relabeled by the page that hit them (checked from registration, sign-in and candidate intake). Server log has the traceback with secrets and emails redacted; users see only a plain message, the code and what to do; telemetry still records request-level failures. |
| `test_recommendations.py` | Employer Recommended Candidates: opt-in consent (default off, revocable, audited), append-only audit, generation on every trigger, status rules, read-time eligibility guard, anonymization against direct and indirect identifiers, ledger privacy, 10-worker concurrency with consent withdrawals, employer isolation (cross-employer, forged clicks, forged session state, forged tokens, unverified employers), immediate effect of consent withdrawal and verification revocation, 20-per-job cap, no repeated notifications. |
| `test_db_roles.py` | Least-privilege roles (`scripts/apply_db_roles.py`, `scripts/db_roles_grants.sql`) on a Neon-like setup (non-superuser `neondb_owner` owns everything): production-endpoint guard, plan mode, exact privilege matrix and drift detection, idempotent apply, allowed and refused statements for `icj_app` and `icj_readonly`, the current app under `icj_app` (ICJ-ERR-511, nothing written), the main workflows under `icj_app` with verify-only startup (simulated), and rollback leaving every row unchanged. |
| `test_migration_partial.py` | Completing a partly migrated database (staging's Nov 13 state: Nov 8 schema, no c20a682 objects), starting from `fixtures/pre_c20a682_partial_schema.sql` with records in every table plus two unrelated tables. `scripts/check_migration.py` dry run changes nothing; `--apply` adds only the c20a682 columns, tables, indexes and triggers, keeps every existing row byte-identical, gives existing candidates no consent; second run changes nothing; a duplicate email blocks completion with rows intact; `scripts/schema_status.sql` is read-only. | Target verification (host format, sslmode, endpoint ids, and Neon's API confirming the branch name and that it is not the default/primary branch, via the stand-in `fake_neon_api.py`) refuses every wrong target before any database contact; `--apply` is all-or-nothing (a duplicate or an unproven probe commits nothing); the database connection must be `sslmode=verify-full` with a trusted CA bundle; shareable output is redacted; a failed post-commit re-verification exits 3. |
## Run them

### 1. Install prerequisites (once)

- **Python 3.11+**. pandas is optional (a stand-in is used if it's missing). No `psycopg2` and no Streamlit are needed.
- **PostgreSQL 14+ server binaries** (`initdb`, `pg_ctl`, `psql`):
  - macOS (Homebrew): `brew install postgresql@16`
    Homebrew doesn't put these on your PATH, so set `export PG_BIN="$(brew --prefix postgresql@16)/bin"`
  - Ubuntu/Debian: `sudo apt-get install postgresql` (binaries are found automatically under `/usr/lib/postgresql/<version>/bin`)

You don't need to start a PostgreSQL service; the runner creates its own.

### 2. Run

From the repository root:

```bash
bash tests/run_local.sh
```

The runner:
1. creates a new cluster in `/tmp/incity-pg.XXXXXX`, listening **only on a Unix socket** (no TCP), UTF-8 like Neon, with SCRAM password authentication and a random per-run password;
2. runs `test_units.py`, `test_migration.py`, `test_e2e.py`, `test_diagnostics.py`, `test_recommendations.py`, `test_db_roles.py`, `test_migration_partial.py` (set `INCITY_SUITES` to run a subset);
3. stops the cluster and deletes the directory, even if a test fails.

Expected last lines:

```
unit tests: 41/41 passed (...)
migration tests: 34/34 passed (...)
end-to-end tests: 72/72 passed (...)
diagnostics tests: 33/33 passed (...)
recommendation tests: 59/59 passed (...)
db role tests: 69/69 passed (...)
partial-migration tests: 90/90 passed (...)
ALL SUITES PASSED
```

The exit code is 0 only when every suite passes. Each suite stops at its first failure and prints `FAIL [...]` with debug output.

### Options

| Variable | Default | Use |
|---|---|---|
| `PG_BIN` | auto-detected | Folder containing `initdb`/`pg_ctl`/`psql` |
| `INCITY_TEST_PGPORT` | `54329` | Port number used for the socket name (change if another local test cluster uses it) |

## How the database access works in tests

The app imports `psycopg2`. Under `tests/run_local.sh`, `PYTHONPATH` puts
`tests/pg_adapter/` first, so the app gets a **test-only adapter** that executes its
SQL through a persistent `psql` session on the throwaway cluster. It uses the user and password from the connection string the app passes (so wrong credentials fail for real), never its host or database, and reports the first error of a multi-statement script with its SQLSTATE, like the real driver. It keeps psycopg2's
transaction behaviour (implicit `BEGIN`, `commit`/`rollback`, savepoints) and raises
errors with real PostgreSQL `pgcode`s (e.g. `23505` for UNIQUE violations).
It **refuses to connect** unless `PGSHIM_HOST` is a local socket directory, and it
ignores any connection string the app passes, so the suites can't reach Neon.

## What these tests do not cover

- The real Streamlit server, the real `psycopg2` driver, real Nominatim geocoding,
  and installing the pinned versions. These are verified on the staging app; see
  `docs/STAGING_DEPLOYMENT.md`, step 7.
- Real concurrent requests (races between two users submitting at once). The UNIQUE
  indexes are what protect against those.

## Continuous integration

`.github/workflows/tests.yml` runs `bash tests/run_local.sh` on a GitHub-hosted Ubuntu 24.04 runner on
every push to `main`, `fix/**` and `p0/**`, on pull requests to `main` and `fix/**`, and on demand.
It uses no repository secrets, no environments and no Neon access; the token is read-only
(`contents: read`) and is not kept after checkout; the only action is GitHub's `actions/checkout`,
pinned to a full commit SHA. `tests/ci_summary.py` writes a per-suite pass/fail table to the run's
summary page and marks each failing check as an error annotation.
