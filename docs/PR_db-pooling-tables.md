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
| *(latest)* | Registration confirmation shows only confirmed facts; regression test |

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

## Registration confirmation (follow-up from staging review)
The block shown after a successful employer registration claimed things that weren't true or weren't built yet. These were "Fully Verified & Logged Nationwide", "Secure Travel Mode", "Deferred Settlement Active", "Merchant Routing Network", "Database Routing ID: ICJ-GLOBAL-…", "Cryptographic Contract Seal", and a promise of automated matching notifications. It now states only confirmed facts: registration successful, signed in, verification pending, and job posting unavailable until a manager approves the company. Registration security, the stored record (including `signature_hash`) and payment logic are unchanged. `tests/test_e2e.py` fails if any of those claims reappear for an unverified employer.

## Data preservation
- Migrations are additive only: `CREATE … IF NOT EXISTS` and `ADD COLUMN IF NOT EXISTS`. No `UPDATE` or `DELETE` of existing rows.
- Duplicate emails are reported, never merged or removed.
- Historical fallback rows are excluded by query, never modified.
- Verified by `tests/test_migration.py`: row counts plus an md5 over every original column of every table are identical before and after the migration, and after re-running it.

## Test evidence
Run: `bash tests/run_local.sh` (throwaway PostgreSQL 16.15, Unix socket only)
```
unit tests:        23/23 passed (security 14/14, processing 6/6, data 3/3)
migration tests:   27/27 passed (data 22/22, security 5/5)
end-to-end tests:  69/69 passed (security 40/40, data 14/14, processing 15/15)
ALL SUITES PASSED   (119/119)
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
