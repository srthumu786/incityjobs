"""Completing a PARTLY migrated database (the staging state found on Nov 13, 2026).

Staging-test-gate was migrated by code older than commit c20a682, so it has the Nov 8 schema
but not the consent columns, employer_recommendations or privacy_audit_log. This suite rebuilds
that exact state with records in every table, plus two unrelated tables the app never touches,
then proves for scripts/check_migration.py (db_schema.py, the migration itself, is unchanged):

- target verification refuses, before any database contact, every wrong target: a malformed or
  look-alike host, weak sslmode, the production endpoint (URI and key=value), another endpoint,
  a missing API key, an unreachable or refusing Neon API, and any Neon answer showing the
  branch is production, default or primary, a different branch name, project, host, a
  read-only or disabled endpoint, or a production endpoint on the same branch;
- dry run: executes the whole migration, rolls back, changes nothing;
- --apply: one transaction; commits only after catalog verification and an in-transaction
  probe; adds only the c20a682 objects; every existing row byte-identical; unrelated tables
  untouched; consent defaults to NO; audit log append-only; then re-verifies read-only;
- all-or-nothing: a duplicate email, a probe that cannot prove enforcement, or a failure
  part-way through all leave the database exactly as before;
- a second --apply changes nothing;
- scripts/schema_status.sql is read-only and flags a missing consent column, a missing
  trigger or a disabled trigger;
- ON DELETE CASCADE removes a deleted job's recommendations but never audit history.
"""
import os
import re
import subprocess
import sys

from _common import Checks, REPO, TESTS, fresh_db, load_legacy_schema, require_local_postgres, sql
import _migration_tool

require_local_postgres()
c = Checks("partial-migration tests")
PW = os.environ["PGPASSWORD"]
USER = os.environ.get("PGSHIM_USER", "postgres")
DSN = f"postgresql://{USER}:{PW}@ep-testbranch-1111.c-2.us-east-2.aws.neon.tech/neondb?sslmode=require"
PARTIAL = os.path.join(TESTS, "fixtures", "pre_c20a682_partial_schema.sql")
STATUS_SQL = os.path.join(REPO, "scripts", "schema_status.sql")
TABLES = ("beta_employers", "candidate_profiles", "employer_jobs", "semantic_matches",
          "system_telemetry_logs", "unrelated_table_a", "unrelated_table_b")
NEW_COLUMNS = {"employer_matching_consent", "employer_matching_consent_at", "employer_matching_consent_policy",
               "contact_disclosure_consent", "resume_version"}


def build_partial_state(db, duplicate=False):
    fresh_db(db)
    load_legacy_schema(db)
    sql(db, open(PARTIAL, encoding="utf-8").read())
    sql(db, f"""
        INSERT INTO beta_employers (company_name, corporate_hq, admin_email, admin_phone, password_hash, verified_at, verified_by)
          VALUES ('Owner Test Co', '1 Main St', 'owner+e1@example.com', '555', 'pbkdf2_sha256$600000$s$h', now(), 'admin'),
                 ('Unverified Co', '2 Main St', 'owner+e2@example.com', '556', NULL, NULL, NULL);
        INSERT INTO candidate_profiles (name, contact_email, role, resume, address, latitude, longitude, status)
          VALUES ('Test Candidate', 'owner+k1@example.com', 'Dev', 'python sql', 'Priceville, AL', 34.5251, -86.8947, 'active_matching_pool'),
                 ('Legacy Fallback', NULL, 'Dev', 'python', 'Unknown', 34.5262, -86.9556, 'active_matching_pool');
        INSERT INTO employer_jobs (title, department, pricing_tier, address, latitude, longitude, employer_id, requirements)
          VALUES ('Python Developer', 'Tech', 'Tier 1', '450 Logistics Blvd', 34.5251, -86.8947, 1, 'python sql');
        INSERT INTO semantic_matches (candidate_profile_id, job_id, candidate_email, employer_email, match_score, status, payload)
          VALUES (1, 1, 'owner+k1@example.com', 'owner+e1@example.com', 90, 'QUEUED_PENDING_GATEWAY', '{{"candidate": {{"profile_id": 1}}}}');
        INSERT INTO system_telemetry_logs (diagnostic_error_code, error_context_vector, error_detail)
          VALUES ('ICJ-ERR-504', 'employer_portal.registration.duplicate_check', 'staging incident');
        CREATE TABLE unrelated_table_a (id SERIAL PRIMARY KEY, note TEXT);
        CREATE TABLE unrelated_table_b (id SERIAL PRIMARY KEY, amount NUMERIC);
        INSERT INTO unrelated_table_a (note) VALUES ('kept'), ('also kept');
        INSERT INTO unrelated_table_b (amount) VALUES (1.50);
    """)
    if duplicate:
        # A duplicate that the earlier code could not have accepted: insert it with the
        # unique index removed, as a database migrated before uniqueness existed might hold.
        sql(db, "DROP INDEX uq_candidate_profiles_contact_email;"
                "INSERT INTO candidate_profiles (name, contact_email) VALUES ('Dup', ' OWNER+K1@example.com ');")


def columns(db, table):
    return sql(db, f"SELECT string_agg(column_name, ',' ORDER BY ordinal_position) FROM information_schema.columns "
                   f"WHERE table_schema = 'public' AND table_name = '{table}';").split(",")


def snapshot(db, original_columns):
    """md5 of every row of every pre-existing table, over its ORIGINAL columns only."""
    out = {}
    for table, cols in original_columns.items():
        col_list = ", ".join(f'"{c_}"' for c_ in cols)
        out[table] = sql(db, f"SELECT count(*) || ':' || coalesce(md5(string_agg(t::text, '|' ORDER BY t::text)), '-') "
                             f"FROM (SELECT {col_list} FROM {table}) t;")
    return out


def objects(db):
    return sql(db, "SELECT string_agg(c.relkind::text || ':' || c.relname::text, ',' ORDER BY c.relname) FROM pg_class c "
                   "JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public';")


def status(db):
    out = subprocess.run(["psql", "-X", "-q", "-At", "-F", " | ", "-h", os.environ["PGSHIM_HOST"],
                          "-p", os.environ.get("PGSHIM_PORT", "5432"), "-U", USER, "-d", db, "-f", STATUS_SQL],
                         capture_output=True, text=True)
    rows = {}
    for line in out.stdout.splitlines():
        parts = line.split(" | ", 2)
        if len(parts) == 3:
            rows[parts[1]] = parts[2]
    return rows, out.returncode


def check_migration(db, *args, **kw):
    """check_migration.py via the test-only Neon API stand-in. Returns (exit code, output)."""
    code, out, _ = _migration_tool.run(db, *args, **kw)
    return code, out


def marker_values(rows):
    return [v for k, v in sorted(((k, v) for k, v in rows.items() if k.startswith("marker")), key=lambda kv: int(kv[0].split()[1]))]


# ================================================================== reproduce the staging state
DB = "partial"
build_partial_state(DB)
ORIGINAL = {t: columns(DB, t) for t in TABLES}
before_status, rc = status(DB)
c.ok("data", rc == 0 and marker_values(before_status) == ["present"] * 6 + ["MISSING"] * 5,
     "fixture matches staging: markers 1-6 present, 7-11 (c20a682) missing", before_status)
c.ok("data", "employer_recommendations" not in before_status.get("tables, all schemas", "")
     and "public.system_telemetry_logs" in before_status.get("tables, all schemas", ""),
     "fixture matches staging: telemetry table exists, recommendations table does not")
BEFORE_ROWS, BEFORE_OBJECTS = snapshot(DB, ORIGINAL), objects(DB)
c.ok("security", status(DB)[0] == before_status and snapshot(DB, ORIGINAL) == BEFORE_ROWS and objects(DB) == BEFORE_OBJECTS,
     "schema_status.sql is read-only: running it changes nothing")

# ================================================================== target verification: refused BEFORE any database contact
T, P = _migration_tool, f"/projects/{_migration_tool.PROJECT}"
EP_PATH, BR_PATH, PROD_PATH = f"{P}/endpoints/{T.STAGING_EP}", f"{P}/branches/br-staging", f"{P}/endpoints/{T.PROD_EP}"
good_url = f"postgresql://{USER}:{PW}@{T.STAGING_EP}-pooler.c-2.us-east-2.aws.neon.tech/neondb?sslmode=require"
REFUSALS = [
    # (label, run kwargs, extra args, expected text) -- static checks (no network, no database)
    ("host is not a Neon host", {"url": f"postgresql://{USER}:{PW}@localhost/neondb"}, (), "not a well-formed Neon host"),
    ("look-alike host", {"url": f"postgresql://{USER}:{PW}@{T.STAGING_EP}.neon.tech.evil.example.com/neondb"}, (), "not a well-formed Neon host"),
    ("weak sslmode", {"url": good_url.replace("sslmode=require", "sslmode=prefer")}, (), "too weak"),
    ("target IS production (URI)", {"url": good_url.replace(T.STAGING_EP, T.PROD_EP)}, (), "IS the production endpoint"),
    ("target IS production (key=value DSN)", {"url": f"host={T.PROD_EP}-pooler.c-2.us-east-2.aws.neon.tech user={USER} password={PW} dbname=neondb"},
     (), "IS the production endpoint"),
    ("expected endpoint = production", {"guard": False}, ("--neon-project-id", T.PROJECT, "--production-endpoint", T.PROD_EP,
     "--expect-endpoint", T.PROD_EP, "--expect-branch", "staging-test-gate"), "are the same"),
    ("target is another endpoint", {"url": good_url.replace(T.STAGING_EP, "ep-other-2222")}, (), "not the expected test endpoint"),
    ("missing required flags", {"guard": False}, ("--production-endpoint", T.PROD_EP), "required"),
    # API checks (network stand-in, still no database)
    ("no Neon API key", {"key": None}, (), "NEON_API_KEY is not set"),
    ("Neon API unreachable", {"scen": T.scenario(network_error=True)}, (), "could not be checked"),
    ("Neon API rejects the key", {"scen": T.scenario(http_errors={EP_PATH: 401})}, (), "HTTP 401"),
    ("branch name is production", {"scen": T.scenario(patch={BR_PATH: {"name": "production"}})}, (), "serves branch 'production'"),
    ("branch is the default branch", {"scen": T.scenario(patch={BR_PATH: {"default": True}})}, (), "DEFAULT branch"),
    ("branch is the primary branch", {"scen": T.scenario(patch={BR_PATH: {"primary": True}})}, (), "PRIMARY branch"),
    ("production endpoint serves the same branch", {"scen": T.scenario(patch={PROD_PATH: {"branch_id": "br-staging"}})}, (), "same branch"),
    ("Neon host differs from the host connected to", {"scen": T.scenario(patch={EP_PATH: {"host": "ep-testbranch-1111.c-9.eu-west-1.aws.neon.tech"}})},
     (), "not the host being connected to"),
    ("endpoint in another project", {"scen": T.scenario(patch={EP_PATH: {"project_id": "proj-other"}})}, (), "different Neon project"),
    ("read-only endpoint", {"scen": T.scenario(patch={EP_PATH: {"type": "read_only"}})}, (), "not read-write"),
    ("disabled endpoint", {"scen": T.scenario(patch={EP_PATH: {"disabled": True}})}, (), "disabled"),
    ("wrong expected branch name", {"guard": False}, ("--neon-project-id", T.PROJECT, "--production-endpoint", T.PROD_EP,
     "--expect-endpoint", T.STAGING_EP, "--expect-branch", "staging-review-db-pooling", "--expect-database", DB), "not 'staging-review-db-pooling'"),
]
for mode in ((), ("--apply",)):
    for label, kw, extra, text in REFUSALS:
        code, out, log = _migration_tool.run(DB, *extra, *mode, **kw)
        c.ok("security", code == 2 and text in out and "db connect" not in log and PW not in out and T.API_KEY not in out
             and snapshot(DB, ORIGINAL) == BEFORE_ROWS and objects(DB) == BEFORE_OBJECTS,
             f"{' '.join(mode) or 'dry run'} refused, no database contact, nothing changed, no secret printed: {label}", (out[-400:], log))
code, out, log = _migration_tool.run(DB, "--expect-database", "some_other_db")
c.ok("security", code == 2 and "expected 'some_other_db'" in out and snapshot(DB, ORIGINAL) == BEFORE_ROWS and objects(DB) == BEFORE_OBJECTS,
     "connected to an unexpected database name: refused before any change", out[-300:])
code, out, log = _migration_tool.run(DB)
c.ok("security", "Neon confirms branch 'staging-test-gate' (br-staging), not default, not primary, created as 'schema-only'" in out
     and [line for line in log if line.startswith("api")] == [f"api {EP_PATH}", f"api {PROD_PATH}", f"api {BR_PATH}"] and log[-1] == "db connect",
     "passing run: Neon is asked about the target endpoint, the production endpoint and the branch BEFORE connecting", (out[:400], log))

# ================================================================== dry run (point 1)
code, out = check_migration(DB)
c.ok("processing", code == 0 and "DRY RUN (rolled back, nothing changed)" in out and "All UNIQUE constraints can be enforced." in out,
     "dry run on the partial state: whole migration executes, then rolls back", out)
c.ok("data", snapshot(DB, ORIGINAL) == BEFORE_ROWS and objects(DB) == BEFORE_OBJECTS and
     {t: columns(DB, t) for t in TABLES} == ORIGINAL, "dry run changed no table, column, index or row")

# ================================================================== apply
code, out = check_migration(DB, "--apply")
c.ok("processing", code == 0 and "Migration APPLIED (committed as one transaction)" in out and "MIGRATION COMPLETE: committed as one transaction, then re-verified" in out,
     "--apply commits once, only after catalog verification + in-transaction probe, then re-verifies read-only", out)
after_status, _ = status(DB)
c.ok("processing", marker_values(after_status) == ["present"] * 11,
     "all eleven markers present after --apply (five consent columns, both triggers enabled, function)", after_status)
c.ok("data", snapshot(DB, ORIGINAL) == BEFORE_ROWS,
     "every pre-existing row byte-identical in its original columns (7 tables, incl. 2 unrelated)")
added = {t: set(columns(DB, t)) - set(ORIGINAL[t]) for t in TABLES}
c.ok("data", added == {**{t: set() for t in TABLES}, "candidate_profiles": NEW_COLUMNS},
     f"only the 5 consent/version columns were added, all on candidate_profiles: {added}")
c.ok("data", [columns(DB, t)[:len(ORIGINAL[t])] for t in TABLES] == [ORIGINAL[t] for t in TABLES],
     "existing columns kept their names and order")
new_objs = set(objects(DB).split(",")) - set(BEFORE_OBJECTS.split(","))
expected_new = {"r:employer_recommendations", "r:privacy_audit_log", "S:employer_recommendations_id_seq",
                "S:privacy_audit_log_id_seq", "i:employer_recommendations_pkey", "i:privacy_audit_log_pkey",
                "i:employer_recommendations_employer_ref_key", "i:employer_recommendations_job_id_candidate_profile_id_key",
                "i:idx_employer_recommendations_employer_status", "i:idx_employer_recommendations_candidate",
                "i:idx_privacy_audit_log_candidate", "i:idx_privacy_audit_log_recommendation"}
c.ok("data", new_objs == expected_new and set(BEFORE_OBJECTS.split(",")) <= set(objects(DB).split(",")),
     f"only the c20a682 tables, sequences and indexes were added; nothing removed: {sorted(new_objs ^ expected_new)}")
c.ok("security", sql(DB, "SELECT count(*) FILTER (WHERE employer_matching_consent OR contact_disclosure_consent "
                         "OR employer_matching_consent_at IS NOT NULL OR employer_matching_consent_policy IS NOT NULL) "
                         "|| '|' || count(*) FILTER (WHERE resume_version = 1) || '|' || count(*) FROM candidate_profiles;") == "0|2|2",
     "existing candidates get NO consent (false/null) and resume_version 1")
c.ok("data", sql(DB, "SELECT (SELECT count(*) FROM employer_recommendations) + (SELECT count(*) FROM privacy_audit_log);") == "0",
     "new tables are empty (the uniqueness probe was rolled back)")
sql(DB, "INSERT INTO privacy_audit_log (event_type, actor_type) VALUES ('TEST', 'system');")
for label, stmt in {"UPDATE": "UPDATE privacy_audit_log SET event_type = 'X';",
                    "DELETE": "DELETE FROM privacy_audit_log;", "TRUNCATE": "TRUNCATE privacy_audit_log;"}.items():
    r = subprocess.run(["psql", "-X", "-q", "-At", "-h", os.environ["PGSHIM_HOST"], "-p", os.environ.get("PGSHIM_PORT", "5432"),
                        "-U", USER, "-d", DB, "-c", stmt], capture_output=True, text=True)
    c.ok("security", r.returncode != 0 and "append-only" in r.stderr, f"audit log refuses {label} after the migration")
c.ok("security", subprocess.run(["psql", "-X", "-q", "-h", os.environ["PGSHIM_HOST"], "-p", os.environ.get("PGSHIM_PORT", "5432"),
                                 "-U", USER, "-d", DB, "-c",
                                 "INSERT INTO candidate_profiles (name, contact_email) VALUES ('x', ' Owner+K1@Example.com');"],
                                capture_output=True, text=True).returncode != 0,
     "email uniqueness still enforced after the migration")

# ================================================================== idempotent
AFTER_ROWS = snapshot(DB, {t: columns(DB, t) for t in TABLES})
AFTER_OBJECTS = objects(DB)
code, out = check_migration(DB, "--apply")
c.ok("processing", code == 0 and "MIGRATION COMPLETE" in out
     and snapshot(DB, {t: columns(DB, t) for t in TABLES}) == AFTER_ROWS and objects(DB) == AFTER_OBJECTS,
     "a second --apply changes nothing", out)

# ================================================================== failure mid-migration rolls back everything (point 3)
DB3 = "partial_fail"
build_partial_state(DB3)
ORIG3 = {t: columns(DB3, t) for t in TABLES}
ROWS3, OBJS3 = snapshot(DB3, ORIG3), objects(DB3)
# A VIEW named privacy_audit_log makes CREATE TABLE IF NOT EXISTS skip it and the next
# statement (CREATE INDEX on it) fail, AFTER the candidate columns and the recommendations
# table were already created earlier in the same block.
sql(DB3, "CREATE VIEW privacy_audit_log AS SELECT 1 AS id, 2 AS candidate_profile_id, now() AS created_at, 3 AS recommendation_id;")
OBJS3 = objects(DB3)
for mode in ((), ("--apply",)):
    code, out = check_migration(DB3, *mode)
    c.ok("data", code != 0 and "MIGRATION COMPLETE" not in out and snapshot(DB3, ORIG3) == ROWS3 and objects(DB3) == OBJS3
         and {t: columns(DB3, t) for t in TABLES} == ORIG3 and PW not in out,
         f"a failure part-way through {' '.join(mode) or 'the dry run'} rolls back EVERYTHING: no new columns, tables or indexes; rows intact",
         out[-600:])

# ================================================================== status query catches incomplete objects (point 6)
for label, damage in {"one consent column missing": "ALTER TABLE candidate_profiles DROP COLUMN contact_disclosure_consent;",
                      "truncate trigger missing": "DROP TRIGGER privacy_audit_log_no_truncate ON privacy_audit_log;",
                      "append-only trigger disabled": "ALTER TABLE privacy_audit_log DISABLE TRIGGER privacy_audit_log_append_only;"}.items():
    DB4 = "partial_damaged"
    build_partial_state(DB4)
    check_migration(DB4, "--apply")
    sql(DB4, damage)
    rows, _ = status(DB4)
    vals = marker_values(rows)
    expect_missing = 7 if "column" in label else 10
    c.ok("security", vals[expect_missing - 1] == "MISSING" and vals.count("MISSING") == 1,
         f"schema_status.sql flags it: {label} -> marker {expect_missing} MISSING", rows)

# ================================================================== ON DELETE CASCADE (point 5)
sql(DB, """INSERT INTO employer_recommendations (job_id, employer_id, candidate_profile_id, status, employer_ref, profile, fingerprint)
           VALUES (1, 1, 1, 'QUEUED', 'C-CASCADE01', '{}', repeat('0', 64));
           INSERT INTO privacy_audit_log (event_type, actor_type, candidate_profile_id, job_id, recommendation_id)
           VALUES ('RECOMMENDATION_CREATED', 'system', 1, 1, currval('employer_recommendations_id_seq'));""")
audit_before = sql(DB, "SELECT count(*) FROM privacy_audit_log;")
sql(DB, "DELETE FROM employer_jobs WHERE id = 1;")
c.ok("data", sql(DB, "SELECT count(*) FROM employer_recommendations;") == "0" and sql(DB, "SELECT count(*) FROM privacy_audit_log;") == audit_before,
     "deleting a job removes its recommendations (CASCADE) but the audit history stays (audit log has no foreign keys)")
app_sql = "".join(open(os.path.join(REPO, f), encoding="utf-8").read() for f in ("app.py", "recommendations.py", "auth.py"))
c.ok("data", not re.search(r"\bDELETE\s+FROM\b|\bTRUNCATE\b", app_sql, re.IGNORECASE),
     "the application itself never deletes rows, so the cascade only acts on a deliberate owner/admin deletion")

# ================================================================== all-or-nothing: incomplete means NOTHING committed (point 2 of the review)
DB2 = "partial_dup"
build_partial_state(DB2, duplicate=True)
ORIG2 = {t: columns(DB2, t) for t in TABLES}
ROWS2, OBJS2 = snapshot(DB2, ORIG2), objects(DB2)
code, out = check_migration(DB2)
c.ok("data", code == 1 and "duplicate: 'owner+k1@example.com' x2" in out and "--apply would commit nothing" in out
     and snapshot(DB2, ORIG2) == ROWS2 and objects(DB2) == OBJS2,
     "dry run with a duplicate email: reported, blocked (exit 1), nothing changed", out)
code, out = check_migration(DB2, "--apply")
c.ok("data", code == 1 and "MIGRATION NOT COMPLETE: nothing was committed" in out
     and snapshot(DB2, ORIG2) == ROWS2 and objects(DB2) == OBJS2 and {t: columns(DB2, t) for t in TABLES} == ORIG2,
     "--apply with a duplicate commits NOTHING: no new columns, tables, indexes or triggers; rows intact", out)
c.ok("data", status(DB2)[0].get("marker 8 recommendations table (c20a682, Nov 10)") == "MISSING",
     "after the refused --apply the c20a682 objects are still absent (not half-applied)")

DB5 = "partial_probe"
build_partial_state(DB5)
ORIG5 = {t: columns(DB5, t) for t in TABLES}
ROWS5, OBJS5 = snapshot(DB5, ORIG5), objects(DB5)
code, out, _ = _migration_tool.run(DB5, "--apply", break_probe=True)
c.ok("data", code == 1 and "Enforcement probe" in out and "enforced'" not in out.split("Enforcement probe")[1].split("\n")[0] and "nothing was committed" in out
     and snapshot(DB5, ORIG5) == ROWS5 and objects(DB5) == OBJS5,
     "--apply whose uniqueness probe cannot prove enforcement commits NOTHING", out[-500:])

c.summary()
