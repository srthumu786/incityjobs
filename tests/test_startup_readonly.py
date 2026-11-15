"""Read-only application startup: the app CHECKS the schema and never migrates it.

Runs the real app.py (simulated Streamlit runtime) against the throwaway local cluster,
set up like Neon: a non-superuser `neondb_owner` owns the database and every table.
No single piece of evidence is relied on to show that startup ran no DDL. Each phase
combines:
  1. a PostgreSQL READ ONLY transaction around the check (the server refuses DDL there);
  2. before-and-after schema fingerprints (scripts/schema_fingerprint.sql, read-only);
  3. the server log with log_statement = 'ddl' for this database: PostgreSQL logs each DDL
     statement when it starts, outside any transaction, so a rolled-back attempt still
     leaves evidence (proved by a positive control below);
  4. the statement log of the test adapter (every statement the app sent);
  5. running the app as the least-privilege role icj_app, which has no CREATE right,
     as well as as the table owner, which has every right a migration needs.
Also: static checks of app.py, the fail-closed matrix for incomplete or weakened schemas,
recovery after the administrator migration, and REQUIRED_SCHEMA kept in step with the
migration (BASE_SCHEMA_SQL)."""
import ast
import inspect
import os
import re
import runpy
import stat
import subprocess
import sys

from _common import (Checks, REPO, TESTS, fresh_db, load_legacy_schema, migrate_as_administrator,
                     require_local_postgres, sql)

require_local_postgres()
import psycopg2  # local-only test adapter
import auth
import db_schema
from _streamlit_sim import RESOURCE_CACHE, Rerun, StopScript, clear_server_log, server_log, ui

c = Checks("startup read-only tests")
SUPER = os.environ.get("PGSHIM_USER", "postgres")
PW = os.environ["PGPASSWORD"]                           # per-run random test password
SERVER_LOG = os.environ.get("INCITY_TEST_SERVER_LOG", "")
PROD_EP, TEST_EP = "ep-prod-0000", "ep-testbranch-1111"
OWNER_URL = f"postgresql://neondb_owner:{PW}@{TEST_EP}-pooler.c-2.us-east-2.aws.neon.tech/neondb?sslmode=require"
CREDS = os.path.join(os.environ["PGSHIM_HOST"], "startup-creds.txt")
FINGERPRINT_SQL = os.path.join(REPO, "scripts", "schema_fingerprint.sql")
PARTIAL = os.path.join(TESTS, "fixtures", "pre_c20a682_partial_schema.sql")
DDL_WORDS = ("CREATE", "ALTER", "DROP", "TRUNCATE", "GRANT", "REVOKE", "COMMENT", "REINDEX", "CLUSTER",
             "VACUUM", "SECURITY LABEL", "REFRESH", "IMPORT", "DO", "CALL", "SET ROLE", "SET SESSION AUTHORIZATION")
DDL_START = re.compile(r"^\s*(" + "|".join(w.replace(" ", r"\s+") for w in DDL_WORDS) + r")\b", re.IGNORECASE)
STATIC_DDL = re.compile(r"^\s*(CREATE|ALTER|DROP|TRUNCATE|GRANT|REVOKE|COMMENT ON|REINDEX|CLUSTER|VACUUM|SECURITY LABEL|DO \$)")  # SQL is upper case
ALLOWED_DURING_CHECK = re.compile(r"^\s*(SELECT|SHOW|SET TRANSACTION READ ONLY)\b", re.IGNORECASE)
MANAGER_PASSWORD = "correct horse battery staple"
MANAGERS = {"alice": auth.hash_password(MANAGER_PASSWORD)}
LOCK, REGISTER, PUBLISH = "Lock Profile & Validate Location Paths", "INITIALIZE BETA PROFILE", "PUBLISH LIVE REQUISITION"

if not SERVER_LOG or not os.path.isfile(SERVER_LOG):
    sys.exit("INCITY_TEST_SERVER_LOG is not set to the throwaway cluster's server log; run: bash tests/run_local.sh")


# ------------------------------------------------------------------ helpers
def psql_file(db, path):
    return subprocess.run(["psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-h", os.environ["PGSHIM_HOST"],
                           "-p", os.environ.get("PGSHIM_PORT", "5432"), "-U", SUPER, "-d", db, "-f", path],
                          capture_output=True, text=True, check=True).stdout


def schema_fp(db):
    """scripts/schema_fingerprint.sql: catalog definitions only, read-only."""
    return psql_file(db, FINGERPRINT_SQL)


def data_fp(db):
    """md5 of every row of every table in the public schema."""
    tables = sql(db, "SELECT string_agg(tablename, ' ' ORDER BY tablename) FROM pg_tables WHERE schemaname = 'public';").split()
    parts = [f"SELECT '{t}:' || coalesce(md5(string_agg(x::text, '|' ORDER BY x::text)), '-') FROM {t} x" for t in tables]
    return sql(db, " UNION ALL ".join(parts) + ";") if parts else ""


def owner(db, text):
    os.environ["PGSHIM_USER"] = "neondb_owner"
    try:
        return sql(db, text)
    finally:
        os.environ["PGSHIM_USER"] = SUPER


def neon_like_db(db, legacy=True):
    """Database and tables owned by the non-superuser neondb_owner, DDL logged by the server."""
    fresh_db(db)
    sql(db, f"""DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'neondb_owner') THEN
                  CREATE ROLE neondb_owner LOGIN CREATEROLE NOSUPERUSER PASSWORD '{PW}'; END IF; END $$;
                ALTER DATABASE {db} OWNER TO neondb_owner;
                ALTER DATABASE {db} SET log_statement = 'ddl';""")
    if legacy:
        os.environ["PGSHIM_USER"] = "neondb_owner"
        try:
            load_legacy_schema(db)
        finally:
            os.environ["PGSHIM_USER"] = SUPER


class Window:
    """Collects what happened between start() and the checks: statements the app sent
    (adapter log) and DDL statements the server logged (non-transactional evidence)."""
    def __init__(self):
        self.sent, self.offset = [], 0

    def start(self):
        self.sent = []
        self.offset = os.path.getsize(SERVER_LOG)
        return self

    def server_ddl(self):
        with open(SERVER_LOG, encoding="utf-8", errors="replace") as fh:
            fh.seek(self.offset)
            return [line.strip()[:160] for line in fh if "statement:" in line]

    def sent_ddl(self):
        return [s[:160] for s in self.sent if DDL_START.match(s)]


W = Window()


def run(nav="Home Portal", inputs=None, clicks=(), dsn=OWNER_URL):
    ui.out.clear(); ui.forms.clear(); clear_server_log(); psycopg2.connection.log.clear()
    ui.nav, ui.inputs, ui.clicks = nav, inputs or {}, set(clicks)
    ui.secrets.clear(); ui.secrets.update({"connection_string": dsn, "managers": MANAGERS})
    try:
        runpy.run_path(os.path.join(REPO, "app.py"), run_name="__main__")
    except (StopScript, Rerun):
        pass
    finally:
        W.sent += list(psycopg2.connection.log)


def fresh_server():
    RESOURCE_CACHE.clear(); ui.session_state.clear()


def texts(kind=None): return [t for k, t in ui.out if kind is None or k == kind]
def codes(): return sorted({w.strip(").,") for t in texts("error") + texts("warning") for w in t.split() if w.startswith("ICJ-ERR-")})
def errors(): return [t for k, t in ui.out if k in ("error", "warning") and ("ICJ-ERR" in t or "denied" in t.lower())]
def debug(): return {"out": ui.out[-6:], "log": server_log()[-1200:], "sent_ddl": W.sent_ddl(), "server_ddl": W.server_ddl()}


def employer_form(email):
    return {"Legal Business / Company Name": "Apex", "Corporate Headquarters Address (Billing/Primary Operations)": "1 Main St",
            "Primary Administrator Email Address": email, "Local Direct Phone Number": "555",
            "Create Employer Password (at least 12 characters)": "apex-strong-pass-1", "Repeat Employer Password": "apex-strong-pass-1",
            "I explicitly agree to the National Terms of Service.": True, "Type Authorized Representative Full Name to Sign": "Ann"}


def candidate_form(email, consent=True):
    return {"Full Name (Absolute Identity Mapping)": "Jane", "Secure Communication Email Node": email,
            "Target Role / Job Title": "Dev", "Paste Resume Text / Background Metrics": "Python developer: sql, docker, microservices",
            "Target City": "Priceville", "Target State": "AL", "Target Zip Code": "35603", "candidate_matching_consent": consent}


def full_workflow(db, tag, dsn):
    """Main app workflows; returns a list of problems (empty = all worked)."""
    q = lambda text: sql(db, text)  # noqa: E731
    problems = []
    def step(label, ok):
        if not ok:
            problems.append(f"{label}: {errors() or ui.out[-3:]}")
    fresh_server()
    run("Employer Portal", employer_form(f"hr-{tag}@apex.com"), {REGISTER}, dsn=dsn)
    emp = q(f"SELECT id FROM beta_employers WHERE admin_email = 'hr-{tag}@apex.com';")
    step("employer registers", emp.isdigit() and not errors())
    employer_session = dict(ui.session_state)
    ui.session_state.clear()
    run("Home Portal", {"Manager username": "alice", "Manager password": MANAGER_PASSWORD}, {"Sign in"}, dsn=dsn)
    manager_session = dict(ui.session_state)
    run("Manager Console", {"Employer": f"#{emp}"}, {"verify_employer"}, dsn=dsn)
    step("manager verifies the employer", q(f"SELECT verified_by FROM beta_employers WHERE id = {emp or 0};") == "alice")
    ui.session_state.clear(); ui.session_state.update(employer_session)
    run("Employer Portal", dsn=dsn)
    run("Employer Portal", {"Job Position Title": f"Python Developer {tag}",
                            "Job Requirements / Description (used for candidate matching)": "python sql docker microservices developer",
                            "Worksite Street Address": "450 Logistics Blvd", "Worksite City": "Priceville",
                            "Worksite State": "AL", "Worksite Zip Code": "35603"}, {PUBLISH}, dsn=dsn)
    job = q(f"SELECT id FROM employer_jobs WHERE title = 'Python Developer {tag}';")
    step("verified employer publishes a job", job.isdigit() and "✅ Job published." in texts("success"))
    ui.session_state.clear()
    run("Employee Portal", candidate_form(f"jane-{tag}@x.com"), {LOCK}, dsn=dsn)
    step("candidate submits with consent",
         q(f"SELECT employer_matching_consent FROM candidate_profiles WHERE contact_email = 'jane-{tag}@x.com';") == "t" and not errors())
    run("Employee Portal", clicks={"withdraw_own_consent"}, dsn=dsn)
    step("candidate withdraws consent",
         q(f"SELECT employer_matching_consent FROM candidate_profiles WHERE contact_email = 'jane-{tag}@x.com';") == "f" and not errors())
    run("Employee Portal", clicks={"grant_own_consent"}, dsn=dsn)
    step("candidate re-grants consent", not errors())
    step("recommendation generated and consent audited",
         int(q(f"SELECT count(*) FROM employer_recommendations WHERE job_id = {job or 0};")) >= 1
         and int(q("SELECT count(*) FROM privacy_audit_log WHERE event_type LIKE 'CONSENT%';")) >= 2)
    ui.session_state.clear(); ui.session_state.update(employer_session)
    run("Employer Portal", dsn=dsn)
    step("employer opens Recommended Candidates", not errors())
    ui.session_state.clear(); ui.session_state.update(manager_session)
    run("Manager Console", {"Job to match": f"#{job}"}, {"run_live_matching"}, dsn=dsn)
    step("manager runs live matching", int(q(f"SELECT count(*) FROM semantic_matches WHERE job_id = {job or 0};")) >= 1 and not errors())
    run("Manager Console", clicks={"recompute_all_recommendations"}, dsn=dsn)
    run("Manager Console", dsn=dsn)
    page = " ".join(texts())
    step("System Checks and Consent & Audit load", "Schema compatible with this app version" in page and not errors())
    step("no 'next app restart' migration promise on System Checks", "next app restart" not in page)
    return problems


def missing_from_log():
    m = re.search(r"Missing: (.*)", server_log())
    return m.group(1) if m else ""


# ================================================================== 1. static: app.py cannot invoke the migration
APP_SRC = open(os.path.join(REPO, "app.py"), encoding="utf-8").read()
APP_TREE = ast.parse(APP_SRC)
names = {n.attr for n in ast.walk(APP_TREE) if isinstance(n, ast.Attribute)} | {n.id for n in ast.walk(APP_TREE) if isinstance(n, ast.Name)}
forbidden = sorted(names & {"apply_schema", "BASE_SCHEMA_SQL", "_create_unique_index", "probe_unique_enforcement",
                            "probe_in_transaction", "check_migration"})
imported = sorted({a.name for n in ast.walk(APP_TREE) if isinstance(n, ast.Import) for a in n.names}
                  | {n.module or "" for n in ast.walk(APP_TREE) if isinstance(n, ast.ImportFrom)})
calls = sorted({ast.unparse(n.func) for n in ast.walk(APP_TREE) if isinstance(n, ast.Call)
                and ast.unparse(n.func) in ("runpy.run_path", "subprocess.run", "subprocess.Popen", "os.system", "exec", "eval")})
c.ok("static", forbidden == [] and not any("check_migration" in m or m == "subprocess" for m in imported) and calls == [],
     "app.py never references or launches the migration (apply_schema, BASE_SCHEMA_SQL, index creation, check_migration)",
     {"names": forbidden, "imports": imported, "calls": calls})


def string_constants(tree):
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            out.append(node.value)
    return out


ddl_strings = [s[:80] for s in string_constants(APP_TREE) if STATIC_DDL.match(s)]
c.ok("static", ddl_strings == [], "app.py contains no SQL text that starts with a DDL keyword", ddl_strings)
CHECK_FUNCS = (db_schema.check_schema, db_schema._missing_objects, db_schema.verify_unique_index,
               db_schema.email_profile, db_schema.find_duplicate_emails, db_schema.legacy_fallback_counts)
check_sql = [s.strip() for f in CHECK_FUNCS for s in string_constants(ast.parse(inspect.getsource(f)))
             if re.match(r"^\s*(SELECT|SHOW|SET|INSERT|UPDATE|DELETE|CREATE|ALTER|DROP|WITH)\b", s, re.IGNORECASE)]
bad = [s[:80] for s in check_sql if not ALLOWED_DURING_CHECK.match(s)]
c.ok("static", check_sql and bad == [], f"check_schema and its helpers contain only SELECT/SHOW/SET TRANSACTION READ ONLY ({len(check_sql)} statements)", bad)
verify_src = ast.get_source_segment(APP_SRC, next(n for n in APP_TREE.body if isinstance(n, ast.FunctionDef) and n.name == "verify_schema"))
cursor_src = ast.get_source_segment(APP_SRC, next(n for n in APP_TREE.body if isinstance(n, ast.FunctionDef) and n.name == "get_db_cursor"))
check_calls = [n for n in ast.walk(APP_TREE) if isinstance(n, ast.Call) and ast.unparse(n.func) == "db_schema.check_schema"]
c.ok("static", len(check_calls) == 1 and "db_schema.check_schema(conn)" in verify_src
     and "verify_schema()" in cursor_src, "every database request goes through verify_schema(), which only calls check_schema()")

# ================================================================== 2. REQUIRED_SCHEMA is in step with the migration
SYNC = "startup_sync"
neon_like_db(SYNC, legacy=False)
os.environ["PGSHIM_USER"] = "neondb_owner"
sync_report = migrate_as_administrator(SYNC)
os.environ["PGSHIM_USER"] = SUPER
conn = psycopg2.connect(OWNER_URL)
sync_check = db_schema.check_schema(conn)
conn.close()
c.ok("sync", sync_report["complete"] and sync_check["missing"] == [] and sync_check["read_only"] is True and sync_check["complete"],
     "empty database migrated by the administrator: read-only check finds nothing missing (server-confirmed READ ONLY)", sync_check["missing"])
catalog_cols = {}
for line in sql(SYNC, "SELECT table_name || '.' || column_name FROM information_schema.columns WHERE table_schema = 'public';").splitlines():
    t, col = line.split(".", 1)
    catalog_cols.setdefault(t, set()).add(col)
required_cols = {t: set(cols) for t, cols in db_schema.REQUIRED_SCHEMA.items()}
c.ok("sync", catalog_cols == required_cols, "REQUIRED_SCHEMA lists exactly the tables and columns the migration creates (both directions)",
     {"only in catalog": {t: sorted(catalog_cols.get(t, set()) - required_cols.get(t, set())) for t in catalog_cols},
      "only in REQUIRED_SCHEMA": {t: sorted(required_cols[t] - catalog_cols.get(t, set())) for t in required_cols}})
named_indexes = set(sql(SYNC, """SELECT c.relname FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
    JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'public'
    AND NOT EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conindid = i.indexrelid);""").splitlines())
email_indexes = {name for name, _, _ in db_schema.UNIQUE_EMAIL_CONSTRAINTS}
triggers = set(sql(SYNC, "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal;").splitlines())
checks = set(sql(SYNC, """SELECT t.relname || '.' || a.attname FROM pg_constraint k JOIN pg_class t ON t.oid = k.conrelid
    JOIN pg_attribute a ON a.attrelid = k.conrelid AND a.attnum = ANY (k.conkey) WHERE k.contype = 'c';""").splitlines())
c.ok("sync", named_indexes == set(db_schema.REQUIRED_INDEXES) | email_indexes and triggers == set(db_schema.REQUIRED_TRIGGERS)
     and checks == {f"{t}.{col}" for t, col in db_schema.REQUIRED_CHECK_CONSTRAINTS},
     "REQUIRED_INDEXES, REQUIRED_TRIGGERS and REQUIRED_CHECK_CONSTRAINTS match what the migration creates",
     {"indexes": sorted(named_indexes), "triggers": sorted(triggers), "checks": sorted(checks)})

# ================================================================== 3. the evidence sources work (positive controls)
DB = "startup"
neon_like_db(DB)
W.start()
subprocess.run(["psql", "-X", "-q", "-h", os.environ["PGSHIM_HOST"], "-p", os.environ.get("PGSHIM_PORT", "5432"), "-U", SUPER,
                "-d", DB, "-c", "BEGIN; CREATE TABLE ddl_evidence_control (id int); ROLLBACK;"], check=True, capture_output=True)
control = psycopg2.connect(OWNER_URL)
with control.cursor() as cur:
    cur.execute("CREATE TABLE ddl_evidence_control_2 (id int);")
control.rollback(); control.close()
W.sent = list(psycopg2.connection.log)
c.ok("evidence", any("ddl_evidence_control (id int)" in line for line in W.server_ddl())
     and any("ddl_evidence_control_2" in line for line in W.server_ddl())
     and any("ddl_evidence_control_2" in s for s in W.sent_ddl())
     and sql(DB, "SELECT to_regclass('ddl_evidence_control') IS NULL AND to_regclass('ddl_evidence_control_2') IS NULL;") == "t",
     "positive control: DDL that was ROLLED BACK still appears in the server log and the adapter log (and left no table)",
     lambda: {"server": W.server_ddl(), "sent": W.sent_ddl()})

# ================================================================== 4. partly migrated (pre-c20a682, as staging was on Nov 13)
PART = "startup_partial"
neon_like_db(PART)
owner(PART, open(PARTIAL, encoding="utf-8").read())
SCHEMA_P = schema_fp(PART)
os.environ["PGSHIM_DB"] = PART
fresh_server(); W.start()
run("Employee Portal", candidate_form("jane@x.com"), {LOCK})
expected_missing = {"column candidate_profiles.employer_matching_consent", "column candidate_profiles.employer_matching_consent_at",
                    "column candidate_profiles.employer_matching_consent_policy", "column candidate_profiles.contact_disclosure_consent",
                    "column candidate_profiles.resume_version", "table employer_recommendations", "table privacy_audit_log",
                    f"audit trigger function {db_schema.AUDIT_FUNCTION} (absent or does not reject changes)"}
listed = set(missing_from_log().split("; "))
c.ok("fail-closed", codes() == ["ICJ-ERR-511"] and expected_missing <= listed and W.sent_ddl() == [] and W.server_ddl() == []
     and schema_fp(PART) == SCHEMA_P and int(sql(PART, "SELECT count(*) FROM candidate_profiles;")) == 0,
     "partly migrated schema: ICJ-ERR-511, the c20a682 objects named, nothing written, no DDL, schema unchanged", debug)

# ================================================================== 5. not migrated, app has the owner's full rights: refuses, changes nothing
os.environ["PGSHIM_DB"] = DB
SCHEMA_0, DATA_0 = schema_fp(DB), data_fp(DB)
fresh_server(); W.start()
results = []
for nav, inputs, clicks in (("Employer Portal", employer_form("hr@apex.com"), {REGISTER}),
                            ("Employer Portal", {"employer_sign_in_email": "hr@apex.com", "employer_sign_in_password": "apex-strong-pass-1"}, {"Sign In"}),
                            ("Employee Portal", candidate_form("jane@x.com"), {LOCK})):
    run(nav, inputs, clicks)
    results.append(codes())
missing_text = missing_from_log()
c.ok("fail-closed", results == [["ICJ-ERR-511"]] * 3, "legacy schema: registration, sign-in and intake all stop with ICJ-ERR-511 only", results)
c.ok("fail-closed", all(x in missing_text for x in ("column candidate_profiles.employer_matching_consent", "table employer_recommendations",
                                                     "table privacy_audit_log", "column beta_employers.password_hash")),
     "the server log names what is missing (consent columns, recommendations and audit tables, authentication columns)", debug)
c.ok("no-ddl", W.sent_ddl() == [] and W.server_ddl() == [] and all(ALLOWED_DURING_CHECK.match(s) for s in W.sent),
     "legacy schema: the app sent only SELECT/SHOW/SET TRANSACTION READ ONLY; server log shows no DDL", debug)
c.ok("no-ddl", "SET TRANSACTION READ ONLY;" in W.sent, "the startup check ran inside a READ ONLY transaction")
c.ok("fingerprint", schema_fp(DB) == SCHEMA_0 and data_fp(DB) == DATA_0,
     "legacy schema: schema and data fingerprints identical before and after (no migration, no rows, no telemetry)")

# ================================================================== 6. administrator migration, then the SAME server recovers
os.environ["PGSHIM_USER"] = "neondb_owner"
migrate_report = migrate_as_administrator(DB)
os.environ["PGSHIM_USER"] = SUPER
SCHEMA_M = schema_fp(DB)
W.start()
run("Employee Portal", candidate_form("first@x.com"), {LOCK})   # no fresh_server(): the failure was not cached
c.ok("recovery", migrate_report["complete"] and codes() == []
     and sql(DB, "SELECT count(*) FROM candidate_profiles WHERE contact_email = 'first@x.com';") == "1"
     and W.sent_ddl() == [] and W.server_ddl() == [] and schema_fp(DB) == SCHEMA_M,
     "after the administrator migration, the same server's next request passes the check and saves (no DDL by the app)", debug)

# ================================================================== 7. weakened schemas fail closed (consent, audit, matching integrity)
AUDIT_FN = db_schema.BASE_SCHEMA_SQL[db_schema.BASE_SCHEMA_SQL.index("CREATE OR REPLACE FUNCTION incity_reject_audit_change"):
                                     db_schema.BASE_SCHEMA_SQL.index("CREATE OR REPLACE TRIGGER privacy_audit_log_append_only")]
CASES = [  # (label, break as owner, restore as owner, text expected in the missing list)
    ("consent column dropped", "ALTER TABLE candidate_profiles DROP COLUMN employer_matching_consent;",
     "ALTER TABLE candidate_profiles ADD COLUMN employer_matching_consent BOOLEAN NOT NULL DEFAULT FALSE;",
     "column candidate_profiles.employer_matching_consent"),
    ("consent defaults to TRUE", "ALTER TABLE candidate_profiles ALTER COLUMN employer_matching_consent SET DEFAULT TRUE;",
     "ALTER TABLE candidate_profiles ALTER COLUMN employer_matching_consent SET DEFAULT FALSE;",
     "column candidate_profiles.employer_matching_consent definition"),
    ("contact-disclosure consent nullable", "ALTER TABLE candidate_profiles ALTER COLUMN contact_disclosure_consent DROP NOT NULL;",
     "ALTER TABLE candidate_profiles ALTER COLUMN contact_disclosure_consent SET NOT NULL;",
     "column candidate_profiles.contact_disclosure_consent definition"),
    ("append-only audit trigger disabled", "ALTER TABLE privacy_audit_log DISABLE TRIGGER privacy_audit_log_append_only;",
     "ALTER TABLE privacy_audit_log ENABLE TRIGGER privacy_audit_log_append_only;", "trigger privacy_audit_log_append_only (not enabled)"),
    ("no-truncate audit trigger replica-only", "ALTER TABLE privacy_audit_log ENABLE REPLICA TRIGGER privacy_audit_log_no_truncate;",
     "ALTER TABLE privacy_audit_log ENABLE TRIGGER privacy_audit_log_no_truncate;", "trigger privacy_audit_log_no_truncate (not enabled)"),
    ("audit function no longer rejects changes",
     "CREATE OR REPLACE FUNCTION incity_reject_audit_change() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN OLD; END; $$;",
     AUDIT_FN, "audit trigger function incity_reject_audit_change"),
    ("match-ledger unique index dropped", "DROP INDEX uq_semantic_matches_candidate_job;",
     "CREATE UNIQUE INDEX uq_semantic_matches_candidate_job ON semantic_matches (candidate_profile_id, job_id) "
     "WHERE candidate_profile_id IS NOT NULL AND job_id IS NOT NULL;", "index uq_semantic_matches_candidate_job"),
    ("recommendation status CHECK dropped", "ALTER TABLE employer_recommendations DROP CONSTRAINT employer_recommendations_status_check;",
     "ALTER TABLE employer_recommendations ADD CONSTRAINT employer_recommendations_status_check "
     "CHECK (status IN ('QUEUED', 'AVAILABLE', 'VIEWED', 'WITHDRAWN'));", "CHECK constraint employer_recommendations.status"),
    ("one-recommendation-per-candidate UNIQUE dropped",
     "ALTER TABLE employer_recommendations DROP CONSTRAINT employer_recommendations_job_id_candidate_profile_id_key;",
     "ALTER TABLE employer_recommendations ADD CONSTRAINT employer_recommendations_job_id_candidate_profile_id_key "
     "UNIQUE (job_id, candidate_profile_id);", "UNIQUE constraint employer_recommendations (job_id, candidate_profile_id)"),
]
for label, break_sql, restore_sql, expected in CASES:
    owner(DB, break_sql)
    weakened_schema, weakened_data = schema_fp(DB), data_fp(DB)
    fresh_server(); W.start()
    run("Employee Portal", candidate_form("weak@x.com"), {LOCK})
    c.ok("fail-closed", codes() == ["ICJ-ERR-511"] and expected in missing_from_log() and W.sent_ddl() == [] and W.server_ddl() == []
         and schema_fp(DB) == weakened_schema and data_fp(DB) == weakened_data,
         f"{label}: ICJ-ERR-511, named in the log, nothing written, app did not repair it (no DDL)", debug)
    owner(DB, restore_sql)
c.ok("fingerprint", schema_fp(DB) == SCHEMA_M, "every weakened object restored: schema fingerprint equals the migrated one again")

# ================================================================== 8. full workflows as the table OWNER: still no DDL
SCHEMA_BEFORE = schema_fp(DB)
W.start()
problems = full_workflow(DB, "owner", OWNER_URL)
c.ok("workflow", problems == [], "as owner: register, verify, publish, consent grant/withdraw, recommendations, matching, System Checks", problems)
c.ok("no-ddl", W.sent_ddl() == [] and W.server_ddl() == [] and schema_fp(DB) == SCHEMA_BEFORE,
     "as owner (who could migrate): no DDL sent, none in the server log, schema fingerprint unchanged", debug)
audit_rows = sql(DB, "SELECT count(*) FROM privacy_audit_log;")
try:
    owner(DB, "UPDATE privacy_audit_log SET actor_ref = 'x';")
    audit_blocked = False
except SystemExit as exc:
    audit_blocked = "append-only" in str(exc)
c.ok("workflow", int(audit_rows) >= 2 and audit_blocked and sql(DB, "SELECT count(*) FROM privacy_audit_log;") == audit_rows,
     "audit log still append-only after the workflows (UPDATE rejected even for the owner)")

# ================================================================== 9. least privilege: the app as icj_app
env = dict(os.environ, PGSHIM_DB=DB, PGSHIM_USER="neondb_owner")
sql(DB, "DROP ROLE IF EXISTS icj_app; DROP ROLE IF EXISTS icj_readonly;")
applied = subprocess.run([sys.executable, os.path.join(REPO, "scripts", "apply_db_roles.py"), "--db-url", OWNER_URL,
                          "--credentials-file", CREDS, "--production-endpoint", PROD_EP, "--apply"],
                         capture_output=True, text=True, env=env)
APP_URL = dict(line.strip().split(" = ", 1) for line in open(CREDS) if " = " in line and not line.startswith("#"))["icj_app"]
c.ok("least-privilege", applied.returncode == 0 and stat.S_IMODE(os.stat(CREDS).st_mode) == 0o600
     and sql(DB, """SELECT has_schema_privilege('icj_app', 'public', 'CREATE') OR has_database_privilege('icj_app', current_database(), 'CREATE')
                   OR EXISTS (SELECT 1 FROM pg_class WHERE relowner = 'icj_app'::regrole) OR (SELECT rolsuper FROM pg_roles WHERE rolname = 'icj_app');""") == "f",
     "icj_app: no CREATE on the schema or database, owns nothing, not a superuser", applied.stdout + applied.stderr)
SCHEMA_BEFORE = schema_fp(DB)
W.start()
problems = full_workflow(DB, "app", APP_URL)
c.ok("workflow", problems == [], "as icj_app: every main workflow works with the read-only startup check", problems)
c.ok("no-ddl", W.sent_ddl() == [] and W.server_ddl() == [] and schema_fp(DB) == SCHEMA_BEFORE,
     "as icj_app: no DDL sent, none in the server log, schema fingerprint unchanged", debug)

# ================================================================== 10. duplicate emails: app runs degraded, never creates the index itself
owner(DB, """DROP INDEX uq_candidate_profiles_contact_email;
             INSERT INTO candidate_profiles (name, contact_email) VALUES ('Dup One', 'dup@x.com'), ('Dup Two', ' DUP@x.com');""")
SCHEMA_D = schema_fp(DB)
telemetry_before = int(sql(DB, "SELECT count(*) FROM system_telemetry_logs WHERE diagnostic_error_code = 'ICJ-ERR-518';"))
fresh_server(); W.start()
run("Employee Portal", candidate_form("new@x.com"), {LOCK}, dsn=APP_URL)
c.ok("degraded", codes() == [] and "ICJ-ERR-518 [startup.schema.unique_email]" in server_log()
     and sql(DB, "SELECT count(*) FROM candidate_profiles WHERE contact_email = 'new@x.com';") == "1"
     and int(sql(DB, "SELECT count(*) FROM system_telemetry_logs WHERE diagnostic_error_code = 'ICJ-ERR-518';")) == telemetry_before + 1,
     "duplicates block uniqueness: app keeps serving with app-level checks, logs ICJ-ERR-518, one telemetry row", debug)
owner(DB, "DELETE FROM candidate_profiles WHERE name = 'Dup Two';")
fresh_server()                                            # a restart, with the owner's rights and no duplicates left
run("Employee Portal", candidate_form("new2@x.com"), {LOCK}, dsn=OWNER_URL)
c.ok("degraded", sql(DB, "SELECT to_regclass('uq_candidate_profiles_contact_email') IS NULL;") == "t" and schema_fp(DB) == SCHEMA_D
     and W.sent_ddl() == [] and W.server_ddl() == [] and "ICJ-ERR-518" in server_log(),
     "after cleanup the app still does not create the UNIQUE index (restart, owner rights): no DDL, schema unchanged", debug)
os.environ["PGSHIM_USER"] = "neondb_owner"
remigrate = migrate_as_administrator(DB)
os.environ["PGSHIM_USER"] = SUPER
fresh_server()
run("Employee Portal", candidate_form("new3@x.com"), {LOCK}, dsn=APP_URL)
c.ok("degraded", remigrate["complete"] and "ICJ-ERR-518" not in server_log() and codes() == [],
     "the administrator migration creates the index; the next app start reports uniqueness verified", debug)

# ================================================================== 11. the READ ONLY transaction is enforced by PostgreSQL
real_counts = db_schema.legacy_fallback_counts
for label, statement in (("DDL", "CREATE TABLE injected_during_check (id int);"),
                         ("write", "INSERT INTO system_telemetry_logs (diagnostic_error_code, error_context_vector) VALUES ('X', 'injected');")):
    def injected(cur, _statement=statement):
        cur.execute(_statement)
        return real_counts(cur)
    db_schema.legacy_fallback_counts = injected
    conn = psycopg2.connect(APP_URL if label == "write" else OWNER_URL)
    try:
        db_schema.check_schema(conn)
        outcome = "accepted"
    except psycopg2.Error as exc:
        outcome = exc.pgcode
    finally:
        db_schema.legacy_fallback_counts = real_counts
        conn.close()
    c.ok("read-only", outcome == "25006" and sql(DB, "SELECT to_regclass('injected_during_check') IS NULL AND NOT EXISTS "
                                                 "(SELECT 1 FROM system_telemetry_logs WHERE error_context_vector = 'injected');") == "t",
         f"a {label} injected into check_schema is refused by PostgreSQL (SQLSTATE 25006 read_only_sql_transaction)", outcome)

c.summary()
