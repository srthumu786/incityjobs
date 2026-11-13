"""Least-privilege database roles (scripts/apply_db_roles.py, scripts/db_roles_grants.sql).

Runs against the throwaway local cluster, set up like Neon: the database and all
tables belong to a NON-superuser `neondb_owner` with CREATEROLE, and the script is
run as that role (never as a superuser). Covers: the production guard, plan mode,
apply + exact privilege matrix, idempotence, behaviour of each role (allowed and
refused statements), the CURRENT app under icj_app (startup migration needs owner
rights, so it stops with ICJ-ERR-511 and changes nothing), the app's main
workflows under icj_app once startup is verify-only (simulated here; that code
change is NOT part of this branch), and rollback (--remove) leaving data intact."""
import os
import runpy
import stat
import subprocess
import sys

from _common import Checks, REPO, fresh_db, load_legacy_schema, require_local_postgres, sql

require_local_postgres()
import psycopg2  # local-only test adapter
import auth
import db_schema

c = Checks("db role tests")
DB = "roles"
SUPER = os.environ.get("PGSHIM_USER", "postgres")
PW = os.environ["PGPASSWORD"]                  # per-run random test password
PROD_EP, TEST_EP = "ep-prod-0000", "ep-testbranch-1111"
OWNER_URL = f"postgresql://neondb_owner:{PW}@{TEST_EP}-pooler.c-2.us-east-2.aws.neon.tech/neondb?sslmode=require"
CREDS = os.path.join(os.environ["PGSHIM_HOST"], "roles-creds.txt")
SCRIPT = os.path.join(REPO, "scripts", "apply_db_roles.py")


def as_user(user, text):
    os.environ["PGSHIM_USER"] = user
    try:
        return sql(DB, text)
    finally:
        os.environ["PGSHIM_USER"] = SUPER


def q(text): return sql(DB, text)
def owner(text): return as_user("neondb_owner", text)


def applier(*args):
    env = dict(os.environ, PGSHIM_DB=DB)
    p = subprocess.run([sys.executable, SCRIPT, "--db-url", OWNER_URL, "--credentials-file", CREDS, *args],
                       capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr


def creds():
    out = {}
    for line in open(CREDS):
        if " = " in line and not line.startswith("#"):
            k, v = line.strip().split(" = ", 1)
            out[k] = v
    return out


def attempt(url, statement):
    """Run one statement as the role in url inside a transaction that is always
    rolled back. Returns 'ok' or the SQLSTATE of the error."""
    try:
        conn = psycopg2.connect(url)
    except psycopg2.OperationalError as exc:
        return "login refused" if "password authentication failed" in str(exc) else f"connect error: {exc}"
    try:
        with conn.cursor() as cur:
            cur.execute(statement)
        return "ok"
    except psycopg2.Error as exc:
        return exc.pgcode or "error"
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()


def fingerprint():
    """Hash of every row of every table plus the column list: proves nothing changed."""
    return q("""SELECT md5(string_agg(x, '|' ORDER BY x)) FROM (
        SELECT 'cols:' || string_agg(table_name || '.' || column_name, ',' ORDER BY table_name, column_name) AS x
          FROM information_schema.columns WHERE table_schema = 'public'
        UNION ALL SELECT 'e:' || md5(string_agg(t::text, '|' ORDER BY t.id)) FROM beta_employers t
        UNION ALL SELECT 'c:' || md5(string_agg(t::text, '|' ORDER BY t.id)) FROM candidate_profiles t
        UNION ALL SELECT 'j:' || md5(string_agg(t::text, '|' ORDER BY t.id)) FROM employer_jobs t
        UNION ALL SELECT 'm:' || coalesce(md5(string_agg(t::text, '|' ORDER BY t.id)), '-') FROM semantic_matches t
        UNION ALL SELECT 'r:' || coalesce(md5(string_agg(t::text, '|' ORDER BY t.id)), '-') FROM employer_recommendations t
        UNION ALL SELECT 'a:' || coalesce(md5(string_agg(t::text, '|' ORDER BY t.id)), '-') FROM privacy_audit_log t
    ) s;""")


# ================================================================== Neon-like setup
fresh_db(DB)
q(f"""DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'neondb_owner') THEN
        CREATE ROLE neondb_owner LOGIN CREATEROLE NOSUPERUSER PASSWORD '{PW}'; END IF; END $$;
      DROP ROLE IF EXISTS icj_app; DROP ROLE IF EXISTS icj_readonly;
      ALTER DATABASE {DB} OWNER TO neondb_owner;""")
os.environ["PGSHIM_USER"] = "neondb_owner"
load_legacy_schema(DB)                              # tables owned by neondb_owner, like production
os.environ["PGSHIM_USER"] = SUPER
c.ok("security", q("SELECT rolsuper FROM pg_roles WHERE rolname = 'neondb_owner';") == "f"
     and q("SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tableowner <> 'neondb_owner';") == "0",
     "setup: non-superuser neondb_owner owns the database and every table (as on Neon)")
conn = psycopg2.connect(OWNER_URL)
report = db_schema.apply_schema(conn)
conn.close()
c.ok("data", report["complete"], "owner runs the branch's migration (as check_migration.py --apply does)")
owner("""INSERT INTO beta_employers (company_name, corporate_hq, admin_email, admin_phone, password_hash, verified_at, verified_by)
           VALUES ('Seed Co', '1 Main', 'seed@co.com', '555', 'pbkdf2_sha256$600000$seedsalt$seedhash', now(), 'alice');
         INSERT INTO candidate_profiles (name, role, resume, address, latitude, longitude, status, contact_email)
           VALUES ('Seed Person', 'Dev', 'python', 'Decatur', 34.6, -86.98, 'active_matching_pool', 'seed@p.com');""")

# ================================================================== production guard and plan mode
code, out = applier("--production-endpoint", TEST_EP, "--apply")
c.ok("security", code == 2 and "Refused" in out and q("SELECT count(*) FROM pg_roles WHERE rolname LIKE 'icj_%';") == "0",
     "guard: refuses when the target endpoint IS the production endpoint; nothing created", out)
code, out = applier("--production-endpoint", "neondb", "--apply")
c.ok("security", code == 2 and "Refused" in out, "guard: refuses a production endpoint that is not an ep- id", out)
code, out = applier("--production-endpoint", PROD_EP)
c.ok("processing", code == 0 and "Plan (nothing changed)" in out and "icj_app: missing" in out
     and q("SELECT count(*) FROM pg_roles WHERE rolname LIKE 'icj_%';") == "0", "plan mode reports and changes nothing", out)
code, out = applier("--production-endpoint", PROD_EP, "--verify")
c.ok("processing", code == 1, "verify fails before apply", out)

# ================================================================== apply, exact matrix, idempotence
BEFORE = fingerprint()
code, out = applier("--production-endpoint", PROD_EP, "--apply")
c.ok("processing", code == 0 and "Applied and verified" in out, "apply as non-superuser neondb_owner: roles created and verified", out)
c.ok("security", PW not in out and "icj_app = " not in out and not any(len(w) == 40 for w in out.split()),
     "apply prints no password or connection string", out)
c.ok("security", stat.S_IMODE(os.stat(CREDS).st_mode) == 0o600 and set(creds()) == {"icj_app", "icj_readonly"},
     "credentials written only to a private file (mode 600)")
APP, RO = creds()["icj_app"], creds()["icj_readonly"]
c.ok("security", q("SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member WHERE r.rolname LIKE 'icj_%';") == "0"
     and q("SELECT bool_or(rolsuper OR rolcreatedb OR rolcreaterole OR rolbypassrls OR rolreplication) FROM pg_roles WHERE rolname LIKE 'icj_%';") == "f",
     "roles have no administrative attributes and belong to no other role (not neon_superuser)")
code, out = applier("--production-endpoint", PROD_EP, "--verify")
c.ok("security", code == 0 and "Verification passed" in out, "verify: privilege matrix matches the design exactly", out)
c.ok("data", fingerprint() == BEFORE, "applying roles changed no table, column or row")
code, out = applier("--production-endpoint", PROD_EP, "--apply")
APP, RO = creds()["icj_app"], creds()["icj_readonly"]
code2, out2 = applier("--production-endpoint", PROD_EP, "--verify")
c.ok("processing", code == 0 and code2 == 0, "re-running apply is safe (idempotent) and rotates both passwords", out + out2)
c.ok("security", attempt(OWNER_URL.replace("neondb_owner", "icj_app"), "SELECT 1;") == "login refused",
     "a wrong password for icj_app is refused")
# drift detection: an extra grant is caught
owner("GRANT DELETE ON candidate_profiles TO icj_app;")
code, out = applier("--production-endpoint", PROD_EP, "--verify")
c.ok("security", code == 1 and "icj_app DELETE on candidate_profiles: granted, expected absent" in out, "verify detects an extra grant (drift)", out)
owner("REVOKE DELETE ON candidate_profiles FROM icj_app;")

# ================================================================== icj_app behaviour
ALLOWED = {
    "read employers": "SELECT id, admin_email, password_hash FROM beta_employers;",
    "register employer": "INSERT INTO beta_employers (company_name, admin_email) VALUES ('X', 'x@x.com');",
    "update employer": "UPDATE beta_employers SET verified_by = 'alice' WHERE id = 1;",
    "lock candidate row (FOR UPDATE)": "SELECT employer_matching_consent FROM candidate_profiles WHERE id = 1 FOR UPDATE;",
    "insert candidate": "INSERT INTO candidate_profiles (name, contact_email) VALUES ('N', 'n@x.com');",
    "insert job": "INSERT INTO employer_jobs (title, employer_id) VALUES ('T', 1);",
    "match ledger insert (as the app writes it)": """INSERT INTO semantic_matches (candidate_profile_id, job_id, match_score) VALUES (1, NULL, 1)
        ON CONFLICT (candidate_profile_id, job_id) WHERE candidate_profile_id IS NOT NULL AND job_id IS NOT NULL
        DO NOTHING RETURNING id;""",
    "append audit row": "INSERT INTO privacy_audit_log (event_type, actor_type, actor_ref) VALUES ('TEST', 'system', 't');",
    "read audit log (manager screen)": "SELECT count(*) FROM privacy_audit_log;",
    "write telemetry": "INSERT INTO system_telemetry_logs (diagnostic_error_code, error_context_vector) VALUES ('ICJ-ERR-599', 't');",
    "catalog checks used by System Checks": "SELECT 1 FROM pg_indexes WHERE schemaname = current_schema();",
}
for label, stmt in ALLOWED.items():
    r = attempt(APP, stmt)
    c.ok("processing", r == "ok", f"icj_app allowed: {label} ({r})")
REFUSED = {
    "delete candidate": "DELETE FROM candidate_profiles;",
    "delete employer": "DELETE FROM beta_employers WHERE id = 1;",
    "truncate jobs": "TRUNCATE employer_jobs CASCADE;",
    "drop table": "DROP TABLE semantic_matches;",
    "create table": "CREATE TABLE evil (id int);",
    "alter table": "ALTER TABLE candidate_profiles ADD COLUMN evil int;",
    "add column if not exists (startup migration)": "ALTER TABLE beta_employers ADD COLUMN IF NOT EXISTS password_hash TEXT;",
    "replace audit trigger function": "CREATE OR REPLACE FUNCTION incity_reject_audit_change() RETURNS trigger LANGUAGE plpgsql AS $f$ BEGIN RETURN NEW; END $f$;",
    "update audit log": "UPDATE privacy_audit_log SET event_type = 'TAMPERED';",
    "delete audit log": "DELETE FROM privacy_audit_log;",
    "read telemetry": "SELECT * FROM system_telemetry_logs;",
    "edit job": "UPDATE employer_jobs SET title = 'X';",
    "edit match ledger": "UPDATE semantic_matches SET match_score = 0;",
    "reset id sequence": "SELECT setval('candidate_profiles_id_seq', 1);",
    "create role": "CREATE ROLE evil;",
    "become owner": "SET ROLE neondb_owner;",
}
for label, stmt in REFUSED.items():
    r = attempt(APP, stmt)
    c.ok("security", r == "42501", f"icj_app refused: {label} ({r})")

conn_ = psycopg2.connect(APP)
with conn_.cursor() as cur_:
    cur_.execute("GRANT DELETE ON candidate_profiles TO icj_app;")   # PostgreSQL only warns: no privileges granted
conn_.commit(); conn_.close()
c.ok("security", q("SELECT has_table_privilege('icj_app', 'candidate_profiles', 'DELETE');") == "f",
     "icj_app cannot grant itself extra privileges (GRANT has no effect)")

# ================================================================== icj_readonly behaviour
for label, stmt in {"read candidates": "SELECT name, contact_email FROM candidate_profiles;",
                    "read employers without secrets": "SELECT id, company_name, admin_email, verified_at FROM beta_employers;",
                    "read audit log": "SELECT count(*) FROM privacy_audit_log;",
                    "read telemetry": "SELECT count(*) FROM system_telemetry_logs;"}.items():
    c.ok("processing", attempt(RO, stmt) == "ok", f"icj_readonly allowed: {label}")
for label, stmt in {"read password hashes": "SELECT password_hash FROM beta_employers;",
                    "read setup-code hashes": "SELECT setup_code_hash FROM beta_employers;",
                    "SELECT * on employers (includes hashes)": "SELECT * FROM beta_employers;",
                    "insert": "INSERT INTO candidate_profiles (name) VALUES ('x');",
                    "update": "UPDATE beta_employers SET company_name = 'x';",
                    "delete": "DELETE FROM employer_jobs;",
                    "use id sequence": "SELECT nextval('candidate_profiles_id_seq');",
                    "create table": "CREATE TABLE evil (id int);"}.items():
    c.ok("security", attempt(RO, stmt) == "42501", f"icj_readonly refused: {label}")

# ================================================================== the CURRENT app under icj_app
from _streamlit_sim import RESOURCE_CACHE, Rerun, StopScript, ui  # noqa: E402

MANAGER_PASSWORD = "correct horse battery staple"
SECRETS = {"connection_string": APP, "managers": {"alice": auth.hash_password(MANAGER_PASSWORD)}}


def run(nav="Home Portal", inputs=None, clicks=()):
    ui.out.clear(); ui.forms.clear(); psycopg2.connection.log.clear()
    ui.nav, ui.inputs, ui.clicks = nav, inputs or {}, set(clicks)
    ui.secrets.clear(); ui.secrets.update(SECRETS)
    try:
        runpy.run_path(os.path.join(REPO, "app.py"), run_name="__main__")
        return "ok"
    except StopScript:
        return "stopped"
    except Rerun:
        return "rerun"


def texts(kind=None): return [t for k, t in ui.out if kind is None or k == kind]
def new_session(state=None): ui.session_state.clear(); ui.session_state.update(state or {})
def debug(): return {"out": ui.out[-8:], "sql": list(psycopg2.connection.log)[-5:]}
def errors(): return [t for k, t in ui.out if k in ("error", "warning") and ("ICJ-ERR" in t or "denied" in t.lower())]


BEFORE = fingerprint()
RESOURCE_CACHE.clear(); new_session()
run("Employee Portal", {"Full Name (Absolute Identity Mapping)": "Jane", "Secure Communication Email Node": "jane@x.com",
                        "Target Role / Job Title": "Dev", "Paste Resume Text / Background Metrics": "Python developer: sql, docker",
                        "Target City": "Priceville", "Target State": "AL", "Target Zip Code": "35603"},
    {"Lock Profile & Validate Location Paths"})
c.ok("security", any("ICJ-ERR-511" in t for t in errors()) and fingerprint() == BEFORE,
     "CURRENT app as icj_app: startup migration needs owner rights -> ICJ-ERR-511, nothing written", debug)

# ================================================================== app workflows under icj_app, startup verify-only
# Simulates the proposed follow-up change (NOT in this branch): at startup the app
# only VERIFIES the schema; migrations run separately as neondb_owner.
_real_apply = db_schema.apply_schema
def verify_only(conn, commit=True):
    report = {"constraints": {}}
    with conn.cursor() as cur:
        for index_name, table, column in db_schema.UNIQUE_EMAIL_CONSTRAINTS:
            check = db_schema.verify_unique_index(cur, index_name, table, column)
            report["constraints"][table] = {"index": index_name, "column": column, "verification": check,
                                            "status": "enforced" if check["active"] else "failed",
                                            "profile": db_schema.email_profile(cur, table, column), "duplicates": []}
        report["legacy_fallback_rows"] = db_schema.legacy_fallback_counts(cur)
    report["complete"] = all(e["status"] == "enforced" for e in report["constraints"].values())
    conn.rollback()
    return report
db_schema.apply_schema = verify_only
RESOURCE_CACHE.clear(); new_session()

REGISTER = {"Legal Business / Company Name": "Apex", "Corporate Headquarters Address (Billing/Primary Operations)": "1 Main St",
            "Primary Administrator Email Address": "hr@apex.com", "Local Direct Phone Number": "555",
            "Create Employer Password (at least 12 characters)": "apex-strong-pass-1", "Repeat Employer Password": "apex-strong-pass-1",
            "I explicitly agree to the National Terms of Service.": True, "Type Authorized Representative Full Name to Sign": "Ann"}
run("Employer Portal", REGISTER, {"INITIALIZE BETA PROFILE"})
APEX = q("SELECT id FROM beta_employers WHERE admin_email = 'hr@apex.com';")
c.ok("processing", APEX.isdigit() and not errors(), "icj_app: employer registers", debug)
APEX_SESSION = dict(ui.session_state)

new_session()
run("Home Portal", {"Manager username": "alice", "Manager password": MANAGER_PASSWORD}, {"Sign in"})
ALICE = dict(ui.session_state)
new_session(ALICE)
run("Manager Console", {"Employer": f"#{APEX}"}, {"verify_employer"})
c.ok("processing", q(f"SELECT verified_by FROM beta_employers WHERE id = {APEX};") == "alice" and not errors(),
     "icj_app: manager verifies the employer", debug)

new_session(APEX_SESSION)
run("Employer Portal")
run("Employer Portal", {"Job Position Title": "Python Developer",
                        "Job Requirements / Description (used for candidate matching)": "python sql docker microservices developer",
                        "Worksite Street Address": "450 Logistics Blvd", "Worksite City": "Priceville",
                        "Worksite State": "AL", "Worksite Zip Code": "35603"}, {"PUBLISH LIVE REQUISITION"})
JOB = q("SELECT id FROM employer_jobs WHERE title = 'Python Developer';")
c.ok("processing", JOB.isdigit() and "✅ Job published." in texts("success") and not errors(), "icj_app: verified employer publishes a job", debug)

new_session()
run("Employee Portal", {"Full Name (Absolute Identity Mapping)": "Jane", "Secure Communication Email Node": "jane@x.com",
                        "Target Role / Job Title": "Dev", "Paste Resume Text / Background Metrics": "Python developer: sql, docker, microservices",
                        "Target City": "Priceville", "Target State": "AL", "Target Zip Code": "35603",
                        "candidate_matching_consent": True}, {"Lock Profile & Validate Location Paths"})
c.ok("processing", q("SELECT employer_matching_consent FROM candidate_profiles WHERE contact_email = 'jane@x.com';") == "t"
     and texts("map") and not errors(), "icj_app: candidate submits with consent and sees the match map", debug)
c.ok("processing", int(q(f"SELECT count(*) FROM employer_recommendations WHERE job_id = {JOB};")) >= 1
     and int(q("SELECT count(*) FROM privacy_audit_log WHERE event_type LIKE 'CONSENT%';")) >= 1,
     "icj_app: recommendation generated and consent audited")
run("Employee Portal", clicks={"withdraw_own_consent"})
c.ok("processing", q("SELECT employer_matching_consent FROM candidate_profiles WHERE contact_email = 'jane@x.com';") == "f"
     and q(f"SELECT r.status FROM employer_recommendations r JOIN candidate_profiles p ON p.id = r.candidate_profile_id "
       f"WHERE r.job_id = {JOB} AND p.contact_email = 'jane@x.com';") == "WITHDRAWN" and not errors(),
     "icj_app: consent withdrawal updates candidate and recommendation (row locks work)",
     lambda: {"consent": q("SELECT employer_matching_consent FROM candidate_profiles WHERE contact_email = 'jane@x.com';"),
              "recs": q(f"SELECT string_agg(id || ':' || status, ',') FROM employer_recommendations WHERE job_id = {JOB};"),
              "errors": errors()})
run("Employee Portal", clicks={"grant_own_consent"})

new_session(APEX_SESSION)
run("Employer Portal")
c.ok("processing", not errors(), "icj_app: employer opens the dashboard and Recommended Candidates", debug)

new_session(ALICE)
run("Manager Console", {"Job to match": f"#{JOB}"}, {"run_live_matching"})
c.ok("processing", int(q("SELECT count(*) FROM semantic_matches;")) >= 1 and not errors(), "icj_app: manager runs live matching (ledger insert)",
     lambda: {"warn": texts("warning"), "err": texts("error"), "cap": texts("caption")[-3:], "df": texts("dataframe")[:1],
              "sql": [x[:120] for x in psycopg2.connection.log][-12:], "tele": q("SELECT string_agg(diagnostic_error_code || ' ' || error_context_vector || ' ' || coalesce(left(error_detail, 200), ''), ' ;; ') FROM system_telemetry_logs;")})
run("Manager Console", clicks={"recompute_all_recommendations"})
run("Manager Console")
page = " ".join(texts())
c.ok("processing", "verified active" in page and not errors(), "icj_app: System Checks and audit screens load", debug)
c.ok("data", q("SELECT count(*) FROM system_telemetry_logs WHERE error_detail ILIKE '%permission denied%';") == "0",
     "no permission-denied failure was logged during the workflows")
db_schema.apply_schema = _real_apply

# ================================================================== rollback
BEFORE = fingerprint()
code, out = applier("--production-endpoint", PROD_EP, "--remove")
c.ok("processing", code == 0 and q("SELECT count(*) FROM pg_roles WHERE rolname LIKE 'icj_%';") == "0",
     "--remove drops both roles", out)
c.ok("data", fingerprint() == BEFORE, "rollback changed no table, column or row")
c.ok("security", attempt(APP, "SELECT 1;") == "login refused", "removed role can no longer connect")

sys.exit(c.summary())
