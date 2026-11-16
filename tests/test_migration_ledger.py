"""Migration version ledger and guarded runner (db_migrations.py, scripts/migrate.py; B01-T1).

Runs scripts/migrate.py in subprocesses against the throwaway local cluster, through the
test-only Neon API stand-in (tests/fake_neon_api.py, via tests/_migration_tool.py), exactly as
an administrator would run it against a Neon test branch. Covers: the code-side rules
(duplicate, badly named, empty, unsafe and missing migrations; the pinned baseline checksum);
status, dry run and apply on a legacy database; ledger contents and append-only enforcement;
later SQL migrations; MODIFIED, MISSING, OUT OF ORDER and DUPLICATE detection with nothing run;
all-or-nothing failure with a recorded success = false attempt; the advisory lock (held lock and
two concurrent runners); baselining an already-migrated database only after verifying its actual
structure (refused for partly migrated, weakened or duplicate-blocked schemas); target guards;
redacted output; post-commit re-verification; and that the app never runs any of it."""
import ast
import concurrent.futures
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

from _common import (Checks, REPO, TESTS, fresh_db, load_legacy_schema, migrate_as_administrator,
                     require_local_postgres, sql)

require_local_postgres()
import psycopg2  # local-only test adapter
import _migration_tool as tool
import db_migrations as dbm
import db_schema

c = Checks("migration ledger tests")
SUPER = os.environ.get("PGSHIM_USER", "postgres")
PW = os.environ["PGPASSWORD"]
SERVER_LOG = os.environ.get("INCITY_TEST_SERVER_LOG", "")
FINGERPRINT_SQL = os.path.join(REPO, "scripts", "schema_fingerprint.sql")
PARTIAL = os.path.join(TESTS, "fixtures", "pre_c20a682_partial_schema.sql")
LEDGER = dbm.LEDGER_TABLE
if not SERVER_LOG or not os.path.isfile(SERVER_LOG):
    raise SystemExit("INCITY_TEST_SERVER_LOG is not set to the throwaway cluster's server log; run: bash tests/run_local.sh")


def migrate(db, *args, migrations=None, **kw):
    extra = ["--migrations-dir", migrations] if migrations else []
    return tool.run(db, *args, *extra, script="migrate.py", **kw)


def mdir(files):
    """A migrations directory with {file name: SQL}."""
    d = tempfile.mkdtemp(dir=os.environ["PGSHIM_HOST"], prefix="migrations-")
    for name, text in files.items():
        with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
            fh.write(text)
    return d


def schema_fp(db):
    return subprocess.run(["psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-h", os.environ["PGSHIM_HOST"],
                           "-p", os.environ.get("PGSHIM_PORT", "5432"), "-U", SUPER, "-d", db, "-f", FINGERPRINT_SQL],
                          capture_output=True, text=True, check=True).stdout


def app_data_fp(db):
    tables = [t for t in sql(db, "SELECT string_agg(tablename, ' ' ORDER BY tablename) FROM pg_tables "
                                 "WHERE schemaname = 'public';").split() if t != LEDGER]
    return sql(db, " UNION ALL ".join(f"SELECT '{t}:' || coalesce(md5(string_agg(x::text, '|' ORDER BY x::text)), '-') FROM {t} x"
                                      for t in tables) + ";")


def ledger(db, where="true"):
    if sql(db, f"SELECT to_regclass('{LEDGER}') IS NULL;") == "t":
        return None
    return sql(db, f"SELECT coalesce(string_agg(version || ':' || method || ':' || success, ',' ORDER BY id), '') "
                   f"FROM {LEDGER} WHERE {where};")


def contacted(log):
    return any(line.startswith("db connect") for line in log)


def legacy_db(db):
    fresh_db(db)
    load_legacy_schema(db)
    sql(db, """INSERT INTO beta_employers (company_name, admin_email) VALUES ('Legacy Co', 'Hiring@Legacy.com');
               INSERT INTO candidate_profiles (name, role, resume, address, latitude, longitude, status)
               VALUES ('Legacy Person', 'Dev', 'python', 'Decatur', 34.6, -86.98, 'active_matching_pool');""")


# ================================================================== 1. code-side rules (no database)
c.ok("code", dbm.baseline().checksum == dbm.BASELINE_CHECKSUM and len(dbm.BASELINE_CHECKSUM) == 64,
     "baseline checksum matches the pinned value (BASE_SCHEMA_SQL and UNIQUE constraints unchanged)")
original_sql = db_schema.BASE_SCHEMA_SQL
db_schema.BASE_SCHEMA_SQL = original_sql + "\nALTER TABLE beta_employers ADD COLUMN IF NOT EXISTS sneaky TEXT;\n"
_, edited = dbm.load_code_migrations(mdir({}))
db_schema.BASE_SCHEMA_SQL = original_sql
c.ok("code", any("MODIFIED in code: the baseline schema" in p for p in edited),
     "editing BASE_SCHEMA_SQL instead of adding a migration file is refused", edited)
code, problems = dbm.load_code_migrations(dbm.MIGRATIONS_DIR)
c.ok("code", problems == [] and [m.version for m in code] == [1] and os.path.isfile(os.path.join(dbm.MIGRATIONS_DIR, "README.md")),
     "the repository's migrations/ is consistent: version 1 (baseline) only", problems)
CODE_CASES = {
    "duplicate version": ({"0002_a.sql": "SELECT 1;", "0002_b.sql": "SELECT 2;"}, "DUPLICATE: version 2"),
    "file claims version 1 (the baseline)": ({"0001_mine.sql": "SELECT 1;"}, "DUPLICATE: 0001_mine.sql uses version 1"),
    "badly named file": ({"2_add.sql": "SELECT 1;"}, "badly named migration file"),
    "upper-case name": ({"0002_Add.sql": "SELECT 1;"}, "badly named migration file"),
    "gap in versions": ({"0002_a.sql": "SELECT 1;", "0004_c.sql": "SELECT 1;"}, "MISSING in code: version(s) [3]"),
    "empty file": ({"0002_a.sql": "  \n"}, "is empty"),
    "BEGIN inside a file": ({"0002_a.sql": "BEGIN;\nCREATE TABLE t (id int);\n"}, "transaction control"),
    "COMMIT inside a file": ({"0002_a.sql": "CREATE TABLE t (id int);\nCOMMIT;\n"}, "transaction control"),
    "CREATE INDEX CONCURRENTLY": ({"0002_a.sql": "CREATE INDEX CONCURRENTLY i ON beta_employers (id);"}, "transaction control"),
    "VACUUM": ({"0002_a.sql": "VACUUM beta_employers;"}, "transaction control"),
}
for label, (files, expected) in CODE_CASES.items():
    _, problems = dbm.load_code_migrations(mdir(files))
    c.ok("code", any(expected in p for p in problems), f"code rule: {label} is refused", problems)
code_ok, problems_ok = dbm.load_code_migrations(mdir({"0002_add_x.sql": "SELECT 1;", "0003_add_y.sql": "SELECT 2;", "notes.txt": "x"}))
c.ok("code", problems_ok == [] and [m.version for m in code_ok] == [1, 2, 3] and code_ok[1].name == "add_x",
     "well-formed files 0002, 0003 are loaded in order; non-.sql files are ignored", problems_ok)

DB = "ledger"
legacy_db(DB)
BAD = mdir({"0002_a.sql": "SELECT 1;", "0002_b.sql": "SELECT 2;"})
code_, out, log = migrate(DB, "--apply", migrations=BAD)
c.ok("code", code_ == 5 and not contacted(log) and not [l for l in log if l.startswith("api")] and ledger(DB) is None,
     "inconsistent code: exit 5 before any Neon API call or database contact", out)

# ================================================================== 2. guards (same as check_migration.py)
code_, out, log = migrate(DB, "--apply", "--production-endpoint", tool.STAGING_EP)
c.ok("guard", code_ == 2 and not contacted(log) and ledger(DB) is None, "target IS the production endpoint: refused, nothing contacted", out)
code_, out, log = migrate(DB, "--apply", url=tool.good_url("require"))
c.ok("guard", code_ == 2 and "sslmode=require is not allowed" in out and not contacted(log), "sslmode=require refused", out)
scen = tool.scenario(patch={f"/projects/{tool.PROJECT}/branches/br-staging": {"name": "production-copy"}})
code_, out, log = migrate(DB, "--apply", scen=scen)
c.ok("guard", code_ == 2 and not contacted(log), "Neon reports a different branch name: refused before connecting", out)
code_, out, log = migrate(DB, "--apply", "--expect-database", "otherdb")
c.ok("guard", code_ == 2 and ledger(DB) is None, "connected database is not the expected one: refused, nothing created", out)

# ================================================================== 3. status, dry run, apply on a legacy database
SCHEMA_0, DATA_0 = schema_fp(DB), app_data_fp(DB)
LEGACY_Q = "SELECT md5(string_agg(t::text, '|' ORDER BY id)) FROM (SELECT id, name, role, latitude, longitude FROM candidate_profiles) t;"
LEGACY_E = "SELECT md5(string_agg(t::text, '|' ORDER BY id)) FROM (SELECT id, company_name, admin_email FROM beta_employers) t;"
LEGACY_ROWS = (sql(DB, LEGACY_Q), sql(DB, LEGACY_E))
code_, out, log = migrate(DB, "--status")
c.ok("status", code_ == 1 and "STATUS: no ledger" in out and "not complete" in out and ledger(DB) is None and schema_fp(DB) == SCHEMA_0,
     "status on a legacy database: no ledger, schema incomplete, exit 1, read-only (schema unchanged)", out)
code_, out, log = migrate(DB)
c.ok("dry-run", code_ == 0 and "DRY RUN" in out and ledger(DB) is None and schema_fp(DB) == SCHEMA_0 and app_data_fp(DB) == DATA_0,
     "dry run: baseline runs and verifies, then everything is rolled back (no ledger, schema and data unchanged)", out)
code_, out, log = migrate(DB, "--apply")
row = sql(DB, f"""SELECT version, name, checksum, method, success, execution_ms >= 0, finished_at >= started_at, applied_by,
                         runner, coalesce(git_commit, 'none'), database_name, detail->'verification'->>'ok',
                         detail->'probe'->>'beta_employers' FROM {LEDGER};""").split("|")
c.ok("apply", code_ == 0 and "APPLIED AND RE-VERIFIED" in out and row[:5] == ["1", "baseline_schema", dbm.BASELINE_CHECKSUM, "applied", "t"],
     "apply: baseline committed with one ledger row (version, name, checksum, method, success)", out)
head = subprocess.run(["git", "-C", REPO, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
EXPECTED_COMMIT = head if len(head) == 40 else "none"   # a checkout without .git records no commit
c.ok("apply", row[5:] == ["t", "t", SUPER, dbm.RUNNER, EXPECTED_COMMIT, DB, "true", "enforced"],
     "ledger metadata: duration, start <= finish, database role, runner, git commit, database, verification, probe", row)
os.environ["PGSHIM_DB"] = DB
conn = psycopg2.connect(tool.good_url())
check = db_schema.check_schema(conn)
conn.close()
c.ok("apply", check["missing"] == [] and check["complete"] and (sql(DB, LEGACY_Q), sql(DB, LEGACY_E)) == LEGACY_ROWS
     and sql(DB, "SELECT employer_matching_consent FROM candidate_profiles WHERE name = 'Legacy Person';") == "f",
     "after apply: the app's read-only check passes with the ledger present; legacy rows unchanged, no consent given")
code_, out, log = migrate(DB, "--apply")
c.ok("apply", code_ == 0 and "Nothing to do" in out and ledger(DB) == "1:applied:true", "re-running apply: nothing to do, no new row", out)
code_, out, log = migrate(DB, "--status")
c.ok("status", code_ == 0 and "STATUS: up to date" in out, "status after apply: up to date (exit 0)", out)

# ================================================================== 4. the ledger is append-only and one success per version
outcomes = {}
for label, statement in (("UPDATE", f"UPDATE {LEDGER} SET name = 'x';"), ("DELETE", f"DELETE FROM {LEDGER};"),
                         ("TRUNCATE", f"TRUNCATE {LEDGER};")):
    try:
        sql(DB, statement)
        outcomes[label] = "accepted"
    except SystemExit as exc:
        outcomes[label] = "rejected" if "append-only" in str(exc) else str(exc)[:80]
c.ok("ledger", outcomes == {"UPDATE": "rejected", "DELETE": "rejected", "TRUNCATE": "rejected"} and ledger(DB) == "1:applied:true",
     "UPDATE, DELETE and TRUNCATE on the ledger are rejected, even for a superuser", outcomes)
try:
    sql(DB, f"INSERT INTO {LEDGER} (version, name, checksum, method, success, started_at, runner) "
            f"VALUES (1, 'baseline_schema', '{dbm.BASELINE_CHECKSUM}', 'applied', true, now(), 'x');")
    dup = "accepted"
except SystemExit as exc:
    dup = "rejected" if "uq_schema_migration_ledger_version_success" in str(exc) else str(exc)[:80]
c.ok("ledger", dup == "rejected", "a second successful row for the same version is rejected by the database", dup)

# ================================================================== 5. later SQL migrations
M2 = "CREATE TABLE ledger_test_extra (id SERIAL PRIMARY KEY, note TEXT);\n"
M3 = "ALTER TABLE ledger_test_extra ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now();\n"
TWO = mdir({"0002_add_extra.sql": M2})
code_, out, log = migrate(DB, migrations=TWO)
c.ok("sql", code_ == 0 and "Pending: 2 add_extra" in out and sql(DB, "SELECT to_regclass('ledger_test_extra') IS NULL;") == "t",
     "dry run of 0002: runs, verifies and rolls back", out)
code_, out, log = migrate(DB, "--apply", migrations=TWO)
c.ok("sql", code_ == 0 and ledger(DB) == "1:applied:true,2:applied:true" and sql(DB, "SELECT to_regclass('ledger_test_extra') IS NOT NULL;") == "t"
     and sql(DB, f"SELECT checksum FROM {LEDGER} WHERE version = 2;") == dbm.checksum(M2),
     "apply 0002: only the pending version runs; its checksum is the file's SHA-256", out)

# ================================================================== 6. integrity problems: nothing runs
NEXT = "CREATE TABLE ledger_should_not_exist (id int);\n"
cases = {
    "MODIFIED": (mdir({"0002_add_extra.sql": M2 + "-- edited after it was applied\n", "0003_next.sql": NEXT}), "MODIFIED: version 2"),
    "renamed (MODIFIED)": (mdir({"0002_add_extra_renamed.sql": M2, "0003_next.sql": NEXT}), "MODIFIED: version 2"),
    "MISSING": (mdir({}), "MISSING: version 2 (add_extra)"),
}
for label, (directory, expected) in cases.items():
    before = (schema_fp(DB), ledger(DB))
    for mode in (("--status",), ("--apply",)):
        code_, out, log = migrate(DB, *mode, migrations=directory)
        c.ok("integrity", code_ == 5 and expected in out and (schema_fp(DB), ledger(DB)) == before
             and sql(DB, "SELECT to_regclass('ledger_should_not_exist') IS NULL;") == "t",
             f"{label} ({mode[0]}): exit 5, reported, nothing run or recorded", out)
# OUT OF ORDER: version 3 recorded (e.g. applied from another branch) while 2 ... here: 3 recorded, 2 not yet
OOO = "ledger_ooo"
legacy_db(OOO)
migrate(OOO, "--apply")
M3_OOO = "CREATE TABLE ledger_ooo_three (id int);\n"
sql(OOO, f"INSERT INTO {LEDGER} (version, name, checksum, method, success, started_at, runner) "
         f"VALUES (3, 'three', '{dbm.checksum(M3_OOO)}', 'applied', true, now(), 'another checkout');")
before = schema_fp(OOO)
code_, out, log = migrate(OOO, "--apply", migrations=mdir({"0002_two.sql": "CREATE TABLE ledger_ooo_two (id int);\n", "0003_three.sql": M3_OOO}))
c.ok("integrity", code_ == 5 and "OUT OF ORDER: version(s) [2] not applied although version 3 is" in out and schema_fp(OOO) == before
     and sql(OOO, "SELECT to_regclass('ledger_ooo_two') IS NULL;") == "t", "OUT OF ORDER: version 3 applied but 2 not: exit 5, nothing run", out)
# DUPLICATE ledger rows (the unique index was removed by hand)
sql(OOO, f"DROP INDEX uq_{LEDGER}_version_success;")
sql(OOO, f"INSERT INTO {LEDGER} (version, name, checksum, method, success, started_at, runner) "
         f"VALUES (1, 'baseline_schema', '{dbm.BASELINE_CHECKSUM}', 'applied', true, now(), 'x');")
code_, out, log = migrate(OOO, "--status")
c.ok("integrity", code_ == 5 and "DUPLICATE: version 1 has more than one successful ledger entry" in out
     and "UNIQUE index missing or invalid" in out, "DUPLICATE ledger entries and a missing ledger index: exit 5", out)
sql(OOO, f"ALTER TABLE {LEDGER} DISABLE TRIGGER {LEDGER}_append_only;")
code_, out, log = migrate(OOO, "--apply")
c.ok("integrity", code_ == 5 and "append-only triggers missing or disabled" in out, "ledger with a disabled append-only trigger is not trusted", out)

# ================================================================== 7. failure is all-or-nothing and recorded
BREAKS = mdir({"0002_add_extra.sql": M2, "0003_breaks_audit.sql":
               "ALTER TABLE privacy_audit_log DISABLE TRIGGER privacy_audit_log_append_only;\n"})
before = (schema_fp(DB), ledger(DB))
code_, out, log = migrate(DB, "--apply", migrations=BREAKS)
c.ok("failure", code_ == 1 and "NOT APPLIED: verification or probe failed" in out and (schema_fp(DB), ledger(DB)) == before,
     "a migration that leaves the app schema incomplete (audit trigger disabled) is rolled back, not recorded", out)
code_, out, log = migrate(DB, "--apply", migrations=mdir({"0002_add_extra.sql": M2, "0003_harmless.sql": "SELECT 1;\n"}),
                          break_probe=True)
c.ok("failure", code_ == 1 and "NOT APPLIED" in out and (schema_fp(DB), ledger(DB)) == before,
     "uniqueness enforcement not proven by the probe: rolled back, nothing recorded", out)
TABLES_Q = "SELECT string_agg(tablename, ',' ORDER BY tablename) FROM pg_tables WHERE schemaname = 'public';"
FAIL = mdir({"0002_add_extra.sql": M2, "0003_ok_part.sql": "CREATE TABLE ledger_partial_ok (id int);\n",
             "0004_broken.sql": "ALTER TABLE no_such_table ADD COLUMN x int;\n"})
before_tables, before_data = sql(DB, TABLES_Q), app_data_fp(DB)
code_, out, log = migrate(DB, "--apply", migrations=FAIL)
c.ok("failure", code_ == 1 and "ERROR in migration 4 broken" in out and "NOT APPLIED" in out
     and sql(DB, "SELECT to_regclass('ledger_partial_ok') IS NULL;") == "t" and ledger(DB, "success") == "1:applied:true,2:applied:true",
     "0003 succeeds, 0004 fails: both rolled back (0003's table absent, no success rows)", out)
c.ok("failure", ledger(DB, "NOT success") == "4:applied:false"
     and re.fullmatch(r"[A-Za-z]+ \(SQLSTATE 42P01\)", sql(DB, f"SELECT detail->>'error' FROM {LEDGER} WHERE NOT success;"))
     and sql(DB, f"SELECT string_agg(k, ',' ORDER BY k) FROM {LEDGER}, jsonb_object_keys(detail) k WHERE NOT success;") == "error,rolled_back"
     and app_data_fp(DB) == before_data,
     "the failed attempt is recorded once: success = false, error type and SQLSTATE only; app data unchanged", out)
c.ok("failure", sql(DB, TABLES_Q) == before_tables and "ledger_partial_ok" not in before_tables,
     "after the failure the set of tables is exactly as before (nothing from 0003 or 0004 kept)")
FIXED = mdir({"0002_add_extra.sql": M2, "0003_ok_part.sql": "CREATE TABLE ledger_partial_ok (id int);\n",
              "0004_broken.sql": "ALTER TABLE ledger_partial_ok ADD COLUMN x int;\n"})
code_, out, log = migrate(DB, "--apply", migrations=FIXED)
c.ok("failure", code_ == 0 and ledger(DB, "success") == "1:applied:true,2:applied:true,3:applied:true,4:applied:true",
     "a failed attempt does not block: after the fix, 0003 and 0004 apply together", out)
DUP = "ledger_dup"
legacy_db(DUP)
sql(DUP, "INSERT INTO beta_employers (company_name, admin_email) VALUES ('Dup', '  hiring@legacy.COM ');")
code_, out, log = migrate(DUP, "--apply")
c.ok("failure", code_ == 1 and "cannot be enforced" in out and ledger(DUP, "success") == ""
     and sql(DUP, "SELECT to_regclass('employer_recommendations') IS NULL;") == "t" and "hiring@legacy" not in out.lower(),
     "duplicate emails block the baseline: nothing committed, attempt recorded, no email printed", out)

# ================================================================== 8. locking
os.environ["PGSHIM_DB"] = DB        # advisory locks are per database: hold it in the target database
holder = psycopg2.connect(tool.good_url())
with holder.cursor() as cur:
    cur.execute("SELECT pg_advisory_xact_lock(%s);", (dbm.LOCK_KEY,))
before = (schema_fp(DB), ledger(DB))
t0 = time.monotonic()
code_, out, log = migrate(DB, "--apply", migrations=mdir({**{n: open(os.path.join(FIXED, n)).read() for n in os.listdir(FIXED)},
                                                          "0005_more.sql": "CREATE TABLE ledger_locked_out (id int);\n"}))
elapsed = time.monotonic() - t0
holder.rollback(); holder.close()
c.ok("lock", code_ == 4 and "holds the migration lock" in out and (schema_fp(DB), ledger(DB)) == before and elapsed < 30,
     "another session holds the migration lock: exit 4 at once, nothing run or recorded", out)
SLOW = {**{n: open(os.path.join(FIXED, n)).read() for n in os.listdir(FIXED)},
        "0005_slow.sql": "CREATE TABLE ledger_concurrent (id int);\nSELECT pg_sleep(3);\n"}
SLOW_DIR = mdir(SLOW)
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    futures = [pool.submit(migrate, DB, "--apply", migrations=SLOW_DIR) for _ in range(2)]
    results = sorted(f.result()[0] for f in futures)
c.ok("lock", results == [0, 4] and sql(DB, f"SELECT count(*) FROM {LEDGER} WHERE version = 5;") == "1",
     "two runners started together: one applies, the other exits 4; version 5 recorded exactly once", results)

# ================================================================== 9. baselining an already-migrated database
def baseline_db(name, extra_sql=None, partial=False):
    fresh_db(name)
    load_legacy_schema(name)
    sql(name, f"ALTER DATABASE {name} SET log_statement = 'ddl';")
    if partial:
        sql(name, open(PARTIAL, encoding="utf-8").read())
    else:
        migrate_as_administrator(name)            # as staging-test-gate was migrated on Nov 14 (no ledger)
    if extra_sql:
        sql(name, extra_sql)


BASE = "ledger_base"
baseline_db(BASE)
SCHEMA_B, DATA_B = schema_fp(BASE), app_data_fp(BASE)
code_, out, log = migrate(BASE, "--status")
c.ok("baseline", code_ == 1 and "The schema is complete: record it with --baseline" in out and ledger(BASE) is None,
     "status: no ledger, schema complete -> advises --baseline", out)
code_, out, log = migrate(BASE, "--apply")
c.ok("baseline", code_ == 1 and "Record it with --baseline" in out,
     "--apply refuses to re-run the baseline on an unledgered, complete schema", out)
c.ok("baseline", ledger(BASE) is None and schema_fp(BASE) == SCHEMA_B, "... and changes nothing")
code_, out, log = migrate(BASE, "--baseline")
c.ok("baseline", code_ == 0 and "DRY RUN: version 1 would be recorded" in out and ledger(BASE) is None and schema_fp(BASE) == SCHEMA_B,
     "--baseline without --apply: verified, nothing recorded or created", out)
offset = os.path.getsize(SERVER_LOG)
code_, out, log = migrate(BASE, "--baseline", "--apply")
with open(SERVER_LOG, encoding="utf-8", errors="replace") as fh:
    fh.seek(offset)
    ddl = [l for l in fh if "statement:" in l]
c.ok("baseline", code_ == 0 and "BASELINED" in out and ledger(BASE) == "1:baselined:true"
     and sql(BASE, f"SELECT detail->'verification'->>'ok' || '|' || (detail->>'note') FROM {LEDGER};").startswith("true|existing schema verified"),
     "--baseline --apply: version 1 recorded as baselined, with the verification summary", out)
c.ok("baseline", app_data_fp(BASE) == DATA_B and ddl and all(LEDGER in l or "incity_reject_ledger_change" in l for l in ddl)
     and not any(re.search(r"\b(beta_employers|candidate_profiles|employer_jobs|privacy_audit_log|employer_recommendations)\b", l) for l in ddl),
     "baselining ran no migration SQL: the only DDL (server log) created the ledger; app data unchanged", [l[:120] for l in ddl])
code_, out, log = migrate(BASE, "--baseline", "--apply")
c.ok("baseline", code_ == 5 and "already records version(s) [1]" in out and ledger(BASE) == "1:baselined:true",
     "a second baseline is refused (exit 5)", out)
code_, out, log = migrate(BASE, "--apply", migrations=TWO)
c.ok("baseline", code_ == 0 and ledger(BASE) == "1:baselined:true,2:applied:true", "after a baseline, --apply runs only the later migrations", out)
for label, kwargs, expected in (
        ("partly migrated (pre-c20a682, as staging was on Nov 13)", {"partial": True}, "missing: table employer_recommendations"),
        ("audit trigger disabled", {"extra_sql": "ALTER TABLE privacy_audit_log DISABLE TRIGGER privacy_audit_log_append_only;"},
         "missing: trigger privacy_audit_log_append_only (not enabled)"),
        ("consent defaults to TRUE", {"extra_sql": "ALTER TABLE candidate_profiles ALTER COLUMN employer_matching_consent SET DEFAULT TRUE;"},
         "missing: column candidate_profiles.employer_matching_consent definition"),
        ("UNIQUE email index absent", {"extra_sql": "DROP INDEX uq_candidate_profiles_contact_email;"},
         "'uq_candidate_profiles_contact_email': False")):
    name = "ledger_bad_base"
    baseline_db(name, **kwargs)
    before = schema_fp(name)
    code_, out, log = migrate(name, "--baseline", "--apply")
    c.ok("baseline", code_ == 1 and expected in out and ledger(name) is None and schema_fp(name) == before,
         f"baseline refused, nothing recorded or created: {label}", out)

# ================================================================== 10. output, post-commit check, the app never migrates
code_, out, log = migrate(DB, "--status")
c.ok("output", PW not in out and tool.API_KEY not in out and "postgresql://" not in out,
     "no database password, API key or connection string in the output", out)
DAMAGE = "ledger_damage"
legacy_db(DAMAGE)
migrate(DAMAGE, "--apply")
code_, out, log = migrate(DAMAGE, "--apply", migrations=TWO, env_extra={"FAKE_DAMAGE_AFTER_COMMIT": "1"})
c.ok("output", code_ == 3 and "RE-VERIFICATION FAILED" in out and "Do not re-run" in out,
     "damage right after the commit: exit 3, the commit is reported as standing", out)
APP = ast.parse(open(os.path.join(REPO, "app.py"), encoding="utf-8").read())
imports = {a.name for n in ast.walk(APP) if isinstance(n, ast.Import) for a in n.names} | \
          {n.module or "" for n in ast.walk(APP) if isinstance(n, ast.ImportFrom)}
names = {n.id for n in ast.walk(APP) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(APP) if isinstance(n, ast.Attribute)}
c.ok("app", "db_migrations" not in imports and not names & {"db_migrations", "LEDGER_SQL", "try_lock", "record", "load_code_migrations"}
     and "migrate.py" not in open(os.path.join(REPO, "app.py"), encoding="utf-8").read(),
     "app.py never imports or runs the migration runner or the ledger")
c.ok("app", "streamlit" not in open(os.path.join(REPO, "db_migrations.py"), encoding="utf-8").read()
     and "import streamlit" not in open(os.path.join(REPO, "scripts", "migrate.py"), encoding="utf-8").read(),
     "the runner and ledger module have no Streamlit dependency (administrator command line only)")

# ================================================================== 11. ledger times come from the DATABASE clock
def db_now(db):
    return sql(db, "SELECT clock_timestamp()::text;")


MIG = open(os.path.join(REPO, "scripts", "migrate.py"), encoding="utf-8").read()
c.ok("clock", not re.search(r"^\s*(import|from)\s+(datetime|time)\b", MIG, re.M) and "clock_timestamp()" in
     open(os.path.join(REPO, "db_migrations.py"), encoding="utf-8").read(),
     "scripts/migrate.py imports no local clock (datetime/time); ledger times use the database's clock_timestamp()")
for minutes in (+2, -2, +600):
    name = "ledger_clock"
    legacy_db(name)
    low = db_now(name)
    code_, out, log = migrate(name, "--apply", env_extra={"FAKE_CLOCK_OFFSET_MINUTES": str(minutes)})
    high = db_now(name)
    ok_window = sql(name, f"SELECT started_at >= '{low}'::timestamptz AND finished_at <= '{high}'::timestamptz "
                          f"AND finished_at >= started_at AND execution_ms = round(extract(epoch FROM finished_at - started_at) * 1000) "
                          f"FROM {LEDGER} WHERE version = 1;")
    c.ok("clock", code_ == 0 and ok_window == "t",
         f"administrator clock {minutes:+} min from the database: apply succeeds; start/finish lie within the database's "
         "own time window; duration = finish - start", out)
name = "ledger_clock_base"
baseline_db(name)
low = db_now(name)
code_, out, log = migrate(name, "--baseline", "--apply", env_extra={"FAKE_CLOCK_OFFSET_MINUTES": "30"})
high = db_now(name)
c.ok("clock", code_ == 0 and sql(name, f"SELECT started_at BETWEEN '{low}'::timestamptz AND '{high}'::timestamptz "
                                       f"AND finished_at = started_at FROM {LEDGER};") == "t",
     "baseline with the administrator clock 30 min ahead: recorded with the database's time", out)
name = "ledger_clock_fail"
legacy_db(name)
migrate(name, "--apply")
low = db_now(name)
code_, out, log = migrate(name, "--apply", migrations=mdir({"0002_broken.sql": "ALTER TABLE no_such_table ADD COLUMN x int;\n"}),
                          env_extra={"FAKE_CLOCK_OFFSET_MINUTES": "-45"})
high = db_now(name)
c.ok("clock", code_ == 1 and sql(name, f"SELECT started_at >= '{low}'::timestamptz AND finished_at <= '{high}'::timestamptz "
                                       f"AND finished_at >= started_at FROM {LEDGER} WHERE NOT success;") == "t",
     "failed attempt with the administrator clock 45 min behind: failure row times from the database", out)

# ================================================================== 12. privileges are reported as privileges, not as missing objects
ROLES = "ledger_roles"
fresh_db(ROLES)
if sql(ROLES, "SELECT count(*) FROM pg_roles WHERE rolname IN ('icj_app', 'icj_readonly');") != "0":
    # roles are cluster-wide: an earlier suite may have left grants in its own throwaway database
    for other in sql(ROLES, "SELECT string_agg(datname, ' ') FROM pg_database WHERE NOT datistemplate AND datallowconn;").split():
        sql(other, "DROP OWNED BY icj_app, icj_readonly;")
sql(ROLES, f"""DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'neondb_owner') THEN
              CREATE ROLE neondb_owner LOGIN CREATEROLE NOSUPERUSER PASSWORD '{PW}'; END IF; END $$;
            DROP ROLE IF EXISTS icj_app; DROP ROLE IF EXISTS icj_readonly; ALTER DATABASE {ROLES} OWNER TO neondb_owner;""")
os.environ["PGSHIM_USER"] = "neondb_owner"
load_legacy_schema(ROLES)
migrate_as_administrator(ROLES)
os.environ["PGSHIM_USER"] = SUPER
OWNER = tool.good_url().replace(f"//{SUPER}:", "//neondb_owner:")
CREDS = os.path.join(tempfile.mkdtemp(dir=os.environ["PGSHIM_HOST"]), "roles.txt")
applied = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "apply_db_roles.py"), "--db-url",
                          OWNER.replace("verify-full", "require"), "--credentials-file", CREDS, "--production-endpoint", tool.PROD_EP,
                          "--apply"], capture_output=True, text=True, env=dict(os.environ, PGSHIM_DB=ROLES))
role_dsn = dict(l.strip().split(" = ", 1) for l in open(CREDS) if " = " in l and not l.startswith("#"))
def role_url(role):
    user_pw = role_dsn[role].split("//", 1)[1].split("@", 1)[0]
    return f"postgresql://{user_pw}@{tool.STAGING_EP}-pooler.c-2.us-east-2.aws.neon.tech/neondb?sslmode=verify-full"
os.environ["PGSHIM_DB"] = ROLES
conn = psycopg2.connect(role_url("icj_readonly"))
cur = conn.cursor()
missing_ro = db_schema._missing_objects(cur)
strict_ro = dbm.schema_verification(cur, strict_version1=True)
conn.rollback(); conn.close()
c.ok("privilege", applied.returncode == 0 and missing_ro == [] and strict_ro["missing"] == [] and strict_ro["definition_problems"] == [],
     "regression: icj_readonly (no access to the hash columns) now sees the complete schema: nothing reported missing",
     {"missing": missing_ro, "strict": strict_ro["definition_problems"], "roles": applied.stdout[-200:]})
conn = psycopg2.connect(role_url("icj_readonly"))
app_check = db_schema.check_schema(conn)
conn.close()
c.ok("privilege", app_check["missing"] == [], "regression: the app's read-only startup check is also privilege-independent", app_check["missing"])
before = schema_fp(ROLES)
code_, out, log = migrate(ROLES, "--baseline", "--apply", url=role_url("icj_readonly"))
c.ok("privilege", code_ == 6 and "INSUFFICIENT PRIVILEGE (this is not a schema problem)" in out and "no CREATE privilege" in out
     and "missing" not in out.split("INSUFFICIENT")[0].lower().replace("missing:", "") and schema_fp(ROLES) == before and ledger(ROLES) is None,
     "icj_readonly --baseline --apply: exit 6, reported as insufficient privilege (no CREATE), not as missing objects", out)
code_, out, log = migrate(ROLES, "--apply", url=role_url("icj_app"))
c.ok("privilege", code_ == 6 and "does not own" in out and "no CREATE privilege" in out and schema_fp(ROLES) == before,
     "icj_app --apply: exit 6, names the missing CREATE privilege and table ownership", out)
code_, out, log = migrate(ROLES, "--baseline", "--apply", url=OWNER)
c.ok("privilege", code_ == 0 and ledger(ROLES) == "1:baselined:true", "the owner baselines the same database", out)
before = schema_fp(ROLES)
for role in ("icj_app", "icj_readonly"):
    code_, out, log = migrate(ROLES, "--status", url=role_url(role))
    c.ok("privilege", code_ == 6 and f"role '{role}' cannot read the ledger" in out and "INTEGRITY" not in out,
         f"{role} --status on a ledgered database: exit 6 'cannot read the ledger', not an integrity problem", out)
code_, out, log = migrate(ROLES, "--apply", url=role_url("icj_app"), migrations=mdir({"0002_x.sql": "CREATE TABLE ledger_role_x (id int);\n"}))
c.ok("privilege", code_ == 6 and "cannot read the ledger" in out and "cannot write the ledger" in out and schema_fp(ROLES) == before,
     "icj_app --apply with a pending migration on a ledgered database: exit 6, nothing run", out)
code_, out, log = migrate(ROLES, "--apply", url=OWNER, migrations=mdir({"0002_needs_superuser.sql": "ALTER ROLE postgres NOSUPERUSER;\n"}))
c.ok("privilege", code_ == 6 and "INSUFFICIENT PRIVILEGE" in out and "42501" in out and ledger(ROLES, "success") == "1:baselined:true"
     and ledger(ROLES, "NOT success") == "2:applied:false",
     "a migration that needs more than the owner's rights: exit 6, rolled back, the attempt recorded (success = false)", out)

# ================================================================== 13. strict version-1 definitions (baseline)
FRESH = "ledger_v1_fresh"
fresh_db(FRESH)
migrate_as_administrator(FRESH)
for label, name in (("an empty database migrated", FRESH), ("a migrated legacy copy", "ledger_base")):
    os.environ["PGSHIM_DB"] = name
    conn = psycopg2.connect(tool.good_url()); cur = conn.cursor()
    problems_, extras_ = dbm.version1_definition_problems(cur)
    conn.rollback(); conn.close()
    c.ok("v1-spec", problems_ == [] and (extras_ == [] or name == "ledger_base"),
         f"VERSION1_* matches {label} exactly", {"problems": problems_, "extras": extras_})
cols = sql(FRESH, """SELECT count(*) FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
                     WHERE n.nspname = 'public' AND c.relkind = 'r' AND a.attnum > 0 AND NOT a.attisdropped;""")
cons = sql(FRESH, "SELECT count(*) FROM pg_constraint con JOIN pg_namespace n ON n.oid = con.connamespace WHERE n.nspname = 'public';")
c.ok("v1-spec", int(cols) == sum(len(v) for v in dbm.VERSION1_COLUMNS.values()) == 86 and int(cons) == len(dbm.VERSION1_CONSTRAINTS) == 16
     and len(dbm.VERSION1_INDEXES) == 7 and len(dbm.VERSION1_TRIGGERS) == 2,
     "the spec covers every column (86) and constraint (16) the migration creates, plus 7 indexes and 2 triggers")
c.ok("v1-spec", not any(t == "semantic_matches" and k == "foreign key" and "candidate_profile_id" in d for t, k, d in dbm.VERSION1_CONSTRAINTS)
     and not any(t == "beta_employers" and v[1] for t, cols_ in dbm.VERSION1_COLUMNS.items() for n, v in cols_.items()
                 if t == "beta_employers" and n == "admin_email"),
     "the spec adds no constraint version 1 lacks (e.g. no FK on semantic_matches.candidate_profile_id, admin_email stays nullable)")
DEVIATIONS = [
    ("foreign key dropped", "ALTER TABLE employer_jobs DROP CONSTRAINT employer_jobs_employer_id_fkey;",
     "missing or different foreign key on employer_jobs"),
    ("foreign key ON DELETE rule changed", "ALTER TABLE semantic_matches DROP CONSTRAINT semantic_matches_job_id_fkey; "
     "ALTER TABLE semantic_matches ADD CONSTRAINT semantic_matches_job_id_fkey FOREIGN KEY (job_id) REFERENCES employer_jobs(id) ON DELETE CASCADE;",
     "missing or different foreign key on semantic_matches"),
    ("foreign key NOT VALID", "ALTER TABLE employer_recommendations DROP CONSTRAINT employer_recommendations_job_id_fkey; "
     "ALTER TABLE employer_recommendations ADD CONSTRAINT employer_recommendations_job_id_fkey FOREIGN KEY (job_id) "
     "REFERENCES employer_jobs(id) ON DELETE CASCADE NOT VALID;", "is NOT VALID"),
    ("primary key dropped", "ALTER TABLE system_telemetry_logs DROP CONSTRAINT system_telemetry_logs_pkey;",
     "missing or different primary key on system_telemetry_logs"),
    ("column type changed", "ALTER TABLE employer_jobs ALTER COLUMN match_radius_miles TYPE bigint;",
     "column employer_jobs.match_radius_miles: type is 'bigint'"),
    ("NOT NULL removed", "ALTER TABLE candidate_profiles ALTER COLUMN application_status DROP NOT NULL;",
     "column candidate_profiles.application_status: NOT NULL is False"),
    ("default changed", "ALTER TABLE employer_jobs ALTER COLUMN match_radius_miles SET DEFAULT 50;",
     "column employer_jobs.match_radius_miles: default is '50'"),
    ("CHECK definition widened", "ALTER TABLE employer_recommendations DROP CONSTRAINT employer_recommendations_status_check; "
     "ALTER TABLE employer_recommendations ADD CONSTRAINT employer_recommendations_status_check "
     "CHECK (status IN ('QUEUED', 'AVAILABLE', 'VIEWED', 'WITHDRAWN', 'DELIVERED'));",
     "missing or different CHECK constraint on employer_recommendations"),
    ("index definition changed", "DROP INDEX idx_employer_recommendations_candidate; "
     "CREATE INDEX idx_employer_recommendations_candidate ON employer_recommendations (job_id);",
     "index idx_employer_recommendations_candidate is"),
    ("trigger given a WHEN (false) condition (same events, never fires)", "DROP TRIGGER privacy_audit_log_append_only ON privacy_audit_log; "
     "CREATE TRIGGER privacy_audit_log_append_only BEFORE UPDATE OR DELETE ON privacy_audit_log FOR EACH ROW WHEN (false) "
     "EXECUTE FUNCTION incity_reject_audit_change();", "trigger privacy_audit_log_append_only is"),
    ("audit function body changed (still raises)", "CREATE OR REPLACE FUNCTION incity_reject_audit_change() RETURNS trigger LANGUAGE plpgsql "
     "AS $$ BEGIN RAISE EXCEPTION 'nope'; END; $$;", "function incity_reject_audit_change differs from version 1"),
]
for label, change, expected in DEVIATIONS:
    name = "ledger_v1_dev"
    baseline_db(name, extra_sql=change)
    before = schema_fp(name)
    code_, out, log = migrate(name, "--baseline", "--apply")
    c.ok("v1-spec", code_ == 1 and expected in out and "does not match version 1" in out and ledger(name) is None and schema_fp(name) == before,
         f"baseline refused, nothing recorded or created: {label}", out)
name = "ledger_v1_extra"
baseline_db(name, extra_sql="ALTER TABLE candidate_profiles ADD COLUMN email VARCHAR(255); CREATE INDEX legacy_extra_idx ON employer_jobs (title);")
code_, out, log = migrate(name, "--baseline", "--apply")
c.ok("v1-spec", code_ == 0 and "note (allowed): extra column candidate_profiles.email" in out and "extra index legacy_extra_idx" in out
     and "candidate_profiles.email" in sql(name, f"SELECT detail->>'allowed_extras' FROM {LEDGER};"),
     "extra legacy objects (e.g. the old optional email column) are allowed, reported and noted in the ledger", out)
name = "ledger_v1_later"
legacy_db(name)
migrate(name, "--apply")
code_, out, log = migrate(name, "--apply", migrations=mdir({"0002_change_default.sql":
                                                            "ALTER TABLE employer_jobs ALTER COLUMN match_radius_miles SET DEFAULT 30;\n"}))
c.ok("v1-spec", code_ == 0 and ledger(name) == "1:applied:true,2:applied:true" and "strict version-1" not in out,
     "once a later version is applied, the exact version-1 definitions no longer apply (a v2 may change them)", out)

# ================================================================== 14. the two migration tools cannot change the schema at the same time
FENCE = "ledger_fence"
legacy_db(FENCE)
migrate(FENCE, "--apply")
os.environ["PGSHIM_DB"] = FENCE
before = (schema_fp(FENCE), ledger(FENCE))                        # read before the lock below is taken
old_tool = psycopg2.connect(tool.good_url())                      # what check_migration.py --apply does first
with old_tool.cursor() as cur:
    cur.execute("ALTER TABLE beta_employers ADD COLUMN IF NOT EXISTS password_hash TEXT;")
t0 = time.monotonic()
code_, out, log = migrate(FENCE, "--apply", migrations=mdir({"0002_fenced.sql": "CREATE TABLE ledger_fenced (id int);\n"}))
waited = time.monotonic() - t0
old_tool.rollback(); old_tool.close()
c.ok("fence", code_ == 4 and "another session is changing the schema of beta_employers" in out and (schema_fp(FENCE), ledger(FENCE)) == before
     and waited < 30, "old tool mid-migration (holding its lock on beta_employers): migrate.py exits 4 after a short wait, nothing done", out)
SLOW_FENCE = mdir({"0002_slow.sql": "CREATE TABLE ledger_fence_slow (id int);\nSELECT pg_sleep(4);\n"})
FENCE_HELD = ("SELECT count(*) FROM pg_locks l JOIN pg_class c ON c.oid = l.relation WHERE c.relname = 'beta_employers' "
              "AND l.mode = 'ShareUpdateExclusiveLock' AND l.granted;")
def wait_for_fence():
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if sql(FENCE, FENCE_HELD) != "0":
            return True
        time.sleep(0.1)
    return False
def new_tool_run():
    result = migrate(FENCE, "--apply", migrations=SLOW_FENCE)
    return result[0], time.monotonic(), result[1]
with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
    new_f = pool.submit(new_tool_run)
    fence_seen = wait_for_fence()                                 # migrate.py is inside its transaction now
    conn_ = psycopg2.connect(tool.good_url())
    t = time.monotonic()
    with conn_.cursor() as cur_:                                  # an ordinary app write while the fence is held
        cur_.execute("INSERT INTO candidate_profiles (name, contact_email) VALUES ('Fence Test', 'fence@x.com');")
    conn_.commit(); conn_.close()
    app_seconds = time.monotonic() - t
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool2:
        old_f = pool2.submit(tool.run, FENCE, "--apply")           # the real scripts/check_migration.py --apply
        waited = False
        while not old_f.done() and not waited:                     # is its DDL waiting for migrate.py's fence?
            waited = sql(FENCE, "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                                "AND wait_event_type = 'Lock' AND query LIKE '%ALTER TABLE beta_employers%';") != "0"
            time.sleep(0.05)
        old_code, old_out, _ = old_f.result()
    new_code, new_end, new_out = new_f.result()
c.ok("fence", fence_seen and waited and new_code == 0 and old_code == 0,
     "check_migration.py started while migrate.py held the fence: its DDL was seen waiting on the lock, then both "
     "completed (serialized, never concurrent)", {"fence_seen": fence_seen, "waited": waited, "new": new_out[-300:], "old": old_out[-300:]})
c.ok("fence", fence_seen and app_seconds < 2, f"the fence does not block the app: an INSERT while it was held took {app_seconds:.2f} s")
code_, out, log = migrate(FENCE, "--status", migrations=SLOW_FENCE)
c.ok("fence", code_ == 0 and "up to date" in out, "after both runs: ledger and schema consistent (status up to date)", out)

for d in [p for p in os.listdir(os.environ["PGSHIM_HOST"]) if p.startswith("migrations-")]:
    shutil.rmtree(os.path.join(os.environ["PGSHIM_HOST"], d), ignore_errors=True)
c.summary()
