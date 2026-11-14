"""Completing a PARTLY migrated database (the staging state found on Nov 13, 2026).

Staging-test-gate was migrated by code older than commit c20a682, so it has the Nov 8
schema (passwords, verification, telemetry, unique emails) but not the consent columns,
employer_recommendations or privacy_audit_log. This suite rebuilds that exact state with
records in every table, plus two unrelated tables the app never touches, then proves that
the existing, reviewed tool `scripts/check_migration.py` (code at a58a528, unchanged):

- dry run: executes the whole migration on the real state, rolls back, changes nothing;
- --apply: adds only the c20a682 objects; every existing table, column and row is
  byte-identical afterwards; unrelated tables untouched; new consent fields default to
  NO authorization; new tables empty; audit log append-only; email uniqueness still enforced;
- a second --apply changes nothing (idempotent);
- scripts/schema_status.sql reports MISSING before and present after, is read-only, and flags
  a missing consent column, a missing trigger or a disabled trigger;
- the endpoint guard refuses production, unknown hosts and unexpected endpoints before connecting;
- a failure part-way through the migration rolls back everything;
- ON DELETE CASCADE removes a deleted job's recommendations but never audit history;
- a database with a duplicate email is reported, blocked and left with its rows intact.
"""
import os
import re
import subprocess
import sys

from _common import Checks, REPO, TESTS, fresh_db, load_legacy_schema, require_local_postgres, sql

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


def check_migration(db, *args, guard=True, url=DSN):
    env = dict(os.environ, PGSHIM_DB=db, INCITY_DB_URL=url)
    flags = (["--production-endpoint", "ep-prod-0000"] + (["--expect-endpoint", "ep-testbranch-1111"] if "--apply" in args else [])) if guard else []
    p = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "check_migration.py"), *flags, *args],
                       capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr


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

# ================================================================== endpoint guard (point 2): refused before connecting
for label, args, kw in [
        ("no --production-endpoint", (), {"guard": False}),
        ("target IS production", ("--production-endpoint", "ep-testbranch-1111"), {"guard": False}),
        ("--apply without --expect-endpoint", ("--production-endpoint", "ep-prod-0000", "--apply"), {"guard": False}),
        ("--apply, wrong expected endpoint", ("--production-endpoint", "ep-prod-0000", "--expect-endpoint", "ep-other-2222", "--apply"), {"guard": False}),
        ("host is not a Neon endpoint", ("--apply",), {"url": f"postgresql://{USER}:{PW}@localhost/neondb"}),
        ("key=value DSN pointing at production", ("--production-endpoint", "ep-prod-0000"),
         {"guard": False, "url": f"host=ep-prod-0000-pooler.c-2.us-east-2.aws.neon.tech user={USER} password={PW} dbname=neondb"})]:
    code, out = check_migration(DB, *args, **kw)
    c.ok("security", code == 2 and snapshot(DB, ORIGINAL) == BEFORE_ROWS and objects(DB) == BEFORE_OBJECTS
         and ("Refused before connecting" in out or "required" in out) and PW not in out,
         f"guard refuses ({label}); nothing contacted or changed; no password printed", out)

# ================================================================== dry run (point 1)
code, out = check_migration(DB)
c.ok("processing", code == 0 and "DRY RUN (rolled back, nothing changed)" in out and "All UNIQUE constraints can be enforced." in out,
     "dry run on the partial state: whole migration executes, then rolls back", out)
c.ok("data", snapshot(DB, ORIGINAL) == BEFORE_ROWS and objects(DB) == BEFORE_OBJECTS and
     {t: columns(DB, t) for t in TABLES} == ORIGINAL, "dry run changed no table, column, index or row")

# ================================================================== apply
code, out = check_migration(DB, "--apply")
c.ok("processing", code == 0 and "Migration APPLIED" in out and "MIGRATION COMPLETE" in out,
     "--apply completes and verifies uniqueness (catalog + rolled-back probe)", out)
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

# ================================================================== duplicates block, rows kept
DB2 = "partial_dup"
build_partial_state(DB2, duplicate=True)
ORIG2 = {t: columns(DB2, t) for t in TABLES}
ROWS2 = snapshot(DB2, ORIG2)
code, out = check_migration(DB2)
c.ok("data", code == 1 and "duplicate: 'owner+k1@example.com' x2" in out and snapshot(DB2, ORIG2) == ROWS2,
     "dry run with a duplicate email: reported, blocked (exit 1), nothing changed", out)
code, out = check_migration(DB2, "--apply")
c.ok("data", code == 1 and "MIGRATION NOT COMPLETE" in out and snapshot(DB2, ORIG2) == ROWS2,
     "--apply with a duplicate: never declared complete; every existing row still intact", out)

c.summary()
