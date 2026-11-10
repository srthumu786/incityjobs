# Pull request: `fix/db-pooling-tables` → `main`

**Title:** Pooled DB access, employer verification & authentication, signed revocable sessions, verified UNIQUE emails, candidate privacy, live vs simulated matching

**Status:** ready for final review. **Not merged.** `main` is unchanged at `0039934`.
The final commit ID is reported with the review submission (this file is part of that commit).

## Commits on this branch
| Commit | Summary |
|---|---|
| `2798bcc` | Pooled connections, unified tables, run-once schema setup |
| `2282c70` | Per-session candidate privacy, manager gate, telemetry, pinned dependencies |
| `263836d` | Employer auth, signed manager sessions, UNIQUE emails, no geocode fallback, live vs simulated matching |
| `566c2f0` | Review requirements 1–7 below |
| `a08c2d5` | Registration confirmation shows only confirmed facts; regression test |
| *(latest)* | Startup diagnostics: ICJ-ERR-500/511 separated from ICJ-ERR-504, redacted server logging |

## Review requirements → changes
| # | Requirement | Change | Files |
|---|---|---|---|
| 1 | Reproducible test suites | `tests/`: unit, migration and end-to-end suites, local-only psql adapter, legacy-schema fixture from `main`, one-command runner, instructions | `tests/*` |
| 2 | No posting for unverified employers | New `beta_employers.verified_at/verified_by`. Every employer starts unverified (existing ones too). Job form and submit both require verification, re-read from the DB on every request. Managers verify or revoke in **Employer Access**. Jobs of unverified employers are hidden from candidates and refused by live matching. | `app.py`, `db_schema.py` |
| 3 | Isolate historical fallback data | Rows at the old fallback point (34.5262, -86.9556) are kept unchanged but excluded from live matching, the candidate map and the matching rules. Counts are shown in System Checks and the preflight report. | `matching.py`, `db_schema.py`, `app.py` |
| 4 | Revoke sessions on credential change | Session tokens carry a keyed fingerprint of the account's current password hash, checked on every request. Any new employer password (setup code or DB change), a replaced manager hash in secrets, or a removed manager revokes all existing sessions immediately. | `auth.py`, `app.py` |
| 5 | Verify DB uniqueness before "complete" | After creating (or repairing) each UNIQUE index, the system catalog is checked: unique, valid, ready, live, on `lower(btrim(email))`, with the NULL/blank predicate. `report["complete"]` is true only if both pass. `check_migration.py --apply` also runs a rolled-back duplicate-insert probe and prints `MIGRATION COMPLETE` (exit 0) only then. Wrong same-named indexes are rebuilt when there are no duplicates, or reported as `invalid_index` when there are. | `db_schema.py`, `scripts/check_migration.py`, `app.py` |
| 6 | PR document on the branch | This file | `docs/PR_db-pooling-tables.md` |
| 7 | Staging manual | Private staging app on a Neon test branch, step by step, with teardown and production cut-over | `docs/STAGING_DEPLOYMENT.md` |

## Startup diagnostics (follow-up from the staging incident)
On staging, a wrong database password was reported as ICJ-ERR-504, the employer duplicate-check code. Errors are now classified by the operation that failed (`diagnostics.py`):

| Code | Raised when |
|---|---|
| ICJ-ERR-500 | Opening the database connection fails, including a missing or invalid `connection_string` secret |
| ICJ-ERR-511 | The schema migration fails (rolled back as before) |
| ICJ-ERR-504 | Only the employer duplicate-check query itself fails |
| ICJ-ERR-518 | UNIQUE email constraint not verified active. This was ICJ-ERR-511 before; it was moved because 511 now means migration failure. |

- `get_db_cursor()` raises typed startup errors before the caller's query runs. `report_failure()` always shows a startup error's own code and message, so no page can relabel it (verified from registration, sign-in and candidate intake).
- Details go to the protected server log through `logger.exception()`, with the full traceback including chained causes. A logging filter redacts connection-string passwords, `password=` values, the configured secret, emails and password hashes from the message and the traceback. Telemetry details are redacted the same way.
- Users see a plain message, the code and what to do next. No exception text is shown, including on the manager-only System Checks tab.
- Unchanged: registration safeguards, connection pooling, migration behaviour, database records and telemetry. Startup failures still skip the telemetry write, because the database is unavailable.

## Registration confirmation (follow-up from staging review)
The block shown after a successful employer registration claimed things that weren't true or weren't built yet. These were "Fully Verified & Logged Nationwide", "Secure Travel Mode", "Deferred Settlement Active", "Merchant Routing Network", "Database Routing ID: ICJ-GLOBAL-…", "Cryptographic Contract Seal", and a promise of automated matching notifications. It now states only confirmed facts: registration successful, signed in, verification pending, and job posting unavailable until a manager approves the company. Registration security, the stored record (including `signature_hash`) and payment logic are unchanged. `tests/test_e2e.py` fails if any of those claims reappear for an unverified employer.

## Data preservation
- Migrations are additive only: `CREATE … IF NOT EXISTS` and `ADD COLUMN IF NOT EXISTS`. No `UPDATE` or `DELETE` of existing rows.
- Duplicate emails are reported, never merged or removed.
- Historical fallback rows are excluded by query, never modified.
- Verified by `tests/test_migration.py`: row counts plus an md5 over every original column of every table are identical before and after the migration, and after re-running it.

## Test evidence
Run: `bash tests/run_local.sh` (throwaway PostgreSQL 16.15, Unix socket only, SCRAM password auth)
```
unit tests:        32/32 passed (security 23/23, processing 6/6, data 3/3)
migration tests:   27/27 passed (data 22/22, security 5/5)
end-to-end tests:  69/69 passed (security 40/40, data 14/14, processing 15/15)
diagnostics tests: 33/33 passed (classification 12/12, logging 5/5, security 9/9, data 4/4, processing 1/1, telemetry 2/2)
ALL SUITES PASSED   (161/161)
```

## Not verified yet (needs the staging app)
Real Streamlit Cloud runtime, real `psycopg2` driver, real Nominatim, installation of the pinned versions, and the migration on a copy of the actual production data. Follow `docs/STAGING_DEPLOYMENT.md`.

## Known limits
- Employer verification is a manual manager decision. There's no automated email verification yet (no mail gateway).
- Sign-in lockout is per browser session.
- A server restart signs everyone out (the token signing key lives in memory).
- Nominatim allows about 1 request per second.
- Live matching evaluates the 500 most recent candidates; the job list shows the 200 most recent jobs.
- After merge, every existing employer must be verified by a manager before posting. Legacy employers also need a setup code.
- The enforcement probe advances id sequences (gaps in ids only).

🤖 Generated with [Claude Code](https://claude.com/claude-code)

https://claude.ai/code/session_016zFHMYQHCXqy3mu56zRQmy
