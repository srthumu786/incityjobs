# Least-privilege database roles: test on a Neon test branch (P0, proposed)

**Status:** proposed for review. Test branch only. Production is not touched, and the app's
secrets are not changed by these steps.

## What it creates

| Role | Used by | Can | Cannot |
|---|---|---|---|
| `neondb_owner` (existing) | Migrations and administration only, from your computer | Everything (owner) | Must never be put in the app's secrets once the change below is approved |
| `icj_app` (new) | The running Streamlit app | Read and write rows the app needs; append to the privacy audit log; write telemetry | Delete or truncate anything; change the schema; edit jobs or the match ledger; change or delete audit rows; read telemetry; reset id sequences; create roles or objects; become another role |
| `icj_readonly` (new) | Your review and audits | Read every table | Read `password_hash` or `setup_code_hash`; write anything |

Exact privileges: `scripts/db_roles_grants.sql`. Automated proof: `tests/test_db_roles.py` (69 checks).

**Why SQL and not the Neon Console:** Neon makes roles created in its Console, CLI or API
members of `neon_superuser` (administrator rights). Roles created with SQL get only what is
granted. On Neon a role belongs to the branch it was created on, so creating them on the
test branch leaves production untouched.

## Known limitation (must be fixed before the app can use `icj_app`)

On every start, the app runs its own schema migration (`db_schema.apply_schema`), which needs
owner rights. Under `icj_app` the current app therefore stops with **ICJ-ERR-511** and changes
nothing (proven by the test). Switching the app to `icj_app` needs a small, separately approved
code change: at start-up the app only *verifies* the schema, and migrations run as
`neondb_owner` through `scripts/check_migration.py --apply`. The test simulates that change
and shows every main workflow works under `icj_app`.

## Steps (you run these; about 15 minutes)

You need the same setup as `docs/STAGING_DEPLOYMENT.md` step 3 (Python and `requirements.txt`).

1. **Neon:** use the test branch `staging-review-db-pooling` (or create a new branch from
   production). Do not use the production branch.
2. Copy the test branch's **owner** connection string (Neon → Connect → branch = test branch,
   role = `neondb_owner`). Check its `ep-…` id is **not** your production endpoint id.
3. From the repository folder:

```bash
export INCITY_DB_URL='<test-branch owner connection string>'
python scripts/check_migration.py --neon-project-id <PROJECT-ID> --production-endpoint ep-<PRODUCTION-ID> --expect-endpoint ep-<TEST-ID> --expect-branch <TEST-BRANCH-NAME> --apply  # if not already migrated (needs NEON_API_KEY)
python scripts/apply_db_roles.py --production-endpoint ep-<PRODUCTION-ID>  # plan: changes nothing
python scripts/apply_db_roles.py --production-endpoint ep-<PRODUCTION-ID> --apply
python scripts/apply_db_roles.py --production-endpoint ep-<PRODUCTION-ID> --verify
```

`--apply` generates two strong passwords on your computer and writes both connection strings
to `~/incityjobs-test-branch-roles.txt` (readable only by you). It prints only the file path.
Never paste that file into a chat, email or the repository.

4. **Behaviour check in the Neon SQL Editor is not possible as `icj_app`** (the editor uses the
   owner). Instead, from your computer, with `psql` if you have it:

```bash
psql "<icj_app string from the file>" -c "DELETE FROM candidate_profiles WHERE false;"   # expect: permission denied
psql "<icj_app string from the file>" -c "SELECT count(*) FROM beta_employers;"            # expect: a number
psql "<icj_readonly string>" -c "SELECT password_hash FROM beta_employers LIMIT 1;"        # expect: permission denied
```

5. **Send for review (no secrets):** the terminal output of the plan, `--apply` and `--verify`
   commands, and the three `psql` results. The output contains no passwords.

6. **Roll back when done** (or if anything looks wrong):

```bash
python scripts/apply_db_roles.py --production-endpoint ep-<PRODUCTION-ID> --remove
rm ~/incityjobs-test-branch-roles.txt
```

`--remove` revokes and drops only the two roles. Tables and rows are not touched (tested).

## Acceptance (what "passed" means)

- Plan mode changes nothing; `--apply` ends with "Applied and verified"; `--verify` passes.
- The `--production-endpoint` guard refuses the production endpoint.
- `icj_app` DELETE is refused; SELECT works. `icj_readonly` cannot read password hashes.
- `--remove` leaves no `icj_` roles: `SELECT rolname FROM pg_roles WHERE rolname LIKE 'icj_%';` returns no rows.
