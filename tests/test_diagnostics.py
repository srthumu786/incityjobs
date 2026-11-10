"""Startup-diagnostics regression tests.

Runs the real app.py (simulated Streamlit runtime) against the isolated local
PostgreSQL started by tests/run_local.sh, which requires SCRAM password
authentication like Neon. Each scenario is a fresh "server process" (the
st.cache_resource cache is cleared), exactly like a reboot on Streamlit Cloud.

Checks, per scenario: the diagnostic code the public user sees, that startup
failures are never shown as ICJ-ERR-504, what reaches the protected server log
(traceback present, secrets and emails redacted), what reaches telemetry, and
that no data changes."""
import os
import runpy

from _common import Checks, REPO, fresh_db, load_legacy_schema, require_local_postgres, sql

require_local_postgres()
import psycopg2  # local-only test adapter
from _streamlit_sim import RESOURCE_CACHE, Rerun, StopScript, clear_server_log, server_log, ui

c = Checks("diagnostics tests")
DB = "diag"
GOOD_DSN = "postgresql://ignored-by-test-adapter"  # adapter supplies the local test password
WRONG_PASSWORD = "Wr0ng-Staging-Pa55-91f3"
WRONG_CREDENTIALS_DSN = f"postgresql://postgres:{WRONG_PASSWORD}@ep-demo-pooler.c-2.us-east-2.aws.neon.tech/neondb?sslmode=require"
MALFORMED_PASSWORD = "Echoed-Secret-77ab"
MALFORMED_DSN = f"neondb_owner:{MALFORMED_PASSWORD}@ep-demo.us-east-2.aws.neon.tech/neondb"  # not a URI: libpq echoes it

REGISTER, LOCK = "INITIALIZE BETA PROFILE", "Lock Profile & Validate Location Paths"
EMPLOYER = {"Legal Business / Company Name": "Apex", "Corporate Headquarters Address (Billing/Primary Operations)": "1 Main St",
            "Primary Administrator Email Address": "hr@apex.com", "Local Direct Phone Number": "555",
            "Create Employer Password (at least 12 characters)": "apex-strong-pass-1", "Repeat Employer Password": "apex-strong-pass-1",
            "I explicitly agree to the National Terms of Service.": True, "Type Authorized Representative Full Name to Sign": "Ann"}
CANDIDATE = {"Full Name (Absolute Identity Mapping)": "Jane", "Secure Communication Email Node": "jane@x.com",
             "Target Role / Job Title": "Dev", "Paste Resume Text / Background Metrics": "python sql",
             "Target City": "Priceville", "Target State": "AL", "Target Zip Code": "35603"}
SIGN_IN = {"employer_sign_in_email": "hr@apex.com", "employer_sign_in_password": "apex-strong-pass-1"}

# Things a public user must never see in an error message
NEVER_SHOWN_TO_USERS = ["Traceback", "psycopg2", "OperationalError", "ProgrammingError", "KeyError", "FATAL",
                        "password authentication", "connection_string", "postgresql://", "neon.tech", "ep-demo",
                        WRONG_PASSWORD, MALFORMED_PASSWORD, "neondb_owner", "hr@apex.com", "jane@x.com", "@"]


def fresh_server():
    """Simulate a (re)started Streamlit server: empty st.cache_resource, new session."""
    RESOURCE_CACHE.clear()
    ui.session_state.clear()


def run(nav, inputs=None, clicks=(), secrets=None):
    ui.out.clear(); ui.forms.clear(); clear_server_log()
    ui.nav, ui.inputs, ui.clicks = nav, inputs or {}, set(clicks)
    ui.secrets.clear(); ui.secrets.update({"connection_string": GOOD_DSN} if secrets is None else secrets)
    try:
        runpy.run_path(os.path.join(REPO, "app.py"), run_name="__main__")
    except (StopScript, Rerun):
        pass


def shown(kind=None):
    return [t for k, t in ui.out if kind is None or k == kind]


def codes_shown():
    return sorted({word.strip(").,") for t in shown("error") + shown("warning") for word in t.split() if word.startswith("ICJ-ERR-")})


def leaks_to_user():
    errors = " ".join(shown("error") + shown("warning"))
    return [s for s in NEVER_SHOWN_TO_USERS if s in errors]


def q(text):
    return sql(DB, text)


def count(table):
    return 0 if q(f"SELECT to_regclass('{table}') IS NULL") == "t" else int(q(f"SELECT count(*) FROM {table}"))


def migrated():
    return q("SELECT count(*) FROM information_schema.columns WHERE table_name = 'beta_employers' AND column_name = 'verified_at'") == "1"


def debug():
    return {"shown": ui.out[-6:], "log": server_log()[-1500:]}


def check_scenario(name, expected_code, stage, cause_in_log, secrets_not_in_log=()):
    log = server_log()
    c.ok("classification", codes_shown() == [expected_code], f"{name}: user sees only {expected_code} (not ICJ-ERR-504) -> {codes_shown()}", debug)
    c.ok("classification", f"{expected_code} [{stage} (while employer_portal.registration.duplicate_check)]" in log,
         f"{name}: server log classifies by failed operation ({stage}) and records where it surfaced", debug)
    c.ok("logging", "Traceback (most recent call last)" in log and cause_in_log in log,
         f"{name}: server log has logger.exception traceback with the root cause ({cause_in_log!r})", debug)
    c.ok("security", leaks_to_user() == [], f"{name}: nothing sensitive or technical shown to the user {leaks_to_user()}", debug)
    leaked = [s for s in secrets_not_in_log if s in log]
    c.ok("security", leaked == [] and "hr@apex.com" not in log, f"{name}: no secrets or personal data in the server log {leaked}", debug)


# ================================================================== legacy copy (as on staging), never migrated yet
fresh_db(DB)
load_legacy_schema(DB)

# 1. Missing connection_string secret ---------------------------------------
fresh_server()
run("Employer Portal", EMPLOYER, {REGISTER}, secrets={"managers": {}})
check_scenario("missing connection_string", "ICJ-ERR-500", "startup.database_connection", "Streamlit secret 'connection_string' is missing")
c.ok("data", not migrated() and count("beta_employers") == 0 and count("system_telemetry_logs") == 0,
     "missing connection_string: database untouched (no migration, no rows, no telemetry)")
run("Employee Portal", CANDIDATE, {LOCK}, secrets={})
c.ok("classification", codes_shown() == ["ICJ-ERR-500"] and "while employee_portal.intake.duplicate_check" in server_log(),
     "missing connection_string during candidate intake: ICJ-ERR-500, not ICJ-ERR-501", debug)

# 2. Invalid database credentials (the staging incident) ---------------------
fresh_server()
run("Employer Portal", EMPLOYER, {REGISTER}, secrets={"connection_string": WRONG_CREDENTIALS_DSN})
check_scenario("invalid credentials", "ICJ-ERR-500", "startup.database_connection", "password authentication failed",
               secrets_not_in_log=(WRONG_PASSWORD, WRONG_CREDENTIALS_DSN))
c.ok("data", not migrated() and count("beta_employers") == 0, "invalid credentials: database untouched")
print("     example, public user sees :", next(t for t in shown("error") if "ICJ-ERR-" in t))
print("     example, server log line  :", next(l for l in server_log().splitlines() if l.startswith("ERROR ICJ-ERR-500")))

# 3. Malformed connection string that the driver echoes back (redaction) -----
fresh_server()
run("Employer Portal", EMPLOYER, {REGISTER}, secrets={"connection_string": MALFORMED_DSN})
check_scenario("malformed connection string", "ICJ-ERR-500", "startup.database_connection", "invalid dsn: missing",
               secrets_not_in_log=(MALFORMED_PASSWORD, MALFORMED_DSN))

# 4. Migration failure --------------------------------------------------------
q("ALTER TABLE beta_employers DROP CONSTRAINT beta_employers_pkey;")  # makes the new foreign key impossible
fresh_server()
run("Employer Portal", EMPLOYER, {REGISTER})
check_scenario("migration failure", "ICJ-ERR-511", "startup.schema_migration", "there is no unique constraint matching given keys")
c.ok("data", not migrated() and count("beta_employers") == 0 and count("system_telemetry_logs") == 0,
     "migration failure: fully rolled back, no rows written")
run("Employer Portal", SIGN_IN, {"Sign In"})
c.ok("classification", codes_shown() == ["ICJ-ERR-511"] and "while employer_portal.sign_in.lookup" in server_log(),
     "migration failure during employer sign-in: ICJ-ERR-511, not ICJ-ERR-512", debug)
run("Employee Portal", CANDIDATE, {LOCK})
c.ok("classification", codes_shown() == ["ICJ-ERR-511"], "migration failure during candidate intake: ICJ-ERR-511, not ICJ-ERR-501", debug)

# 5. Recovery: fix the cause, the same server retries (failures are not cached) ---
q("ALTER TABLE beta_employers ADD PRIMARY KEY (id);")
run("Employer Portal", EMPLOYER, {REGISTER})
c.ok("processing", migrated() and count("beta_employers") == 1 and any("Registration successful" in t for t in shown("success")) and codes_shown() == [],
     "after fixing the cause, the next request migrates and registration succeeds (failure was not cached)", debug)

# 6. Actual duplicate-check query failure ------------------------------------
q("ALTER TABLE beta_employers RENAME COLUMN admin_email TO admin_email_tmp;")  # breaks only the request's own query
run("Employer Portal", {**EMPLOYER, "Primary Administrator Email Address": "second@apex.com"}, {REGISTER})
log = server_log()
c.ok("classification", codes_shown() == ["ICJ-ERR-504"] and "ICJ-ERR-504 [employer_portal.registration.duplicate_check] ProgrammingError" in log
     and "startup." not in log, "real duplicate-check query failure: ICJ-ERR-504 with the query's own context", debug)
c.ok("logging", "Traceback (most recent call last)" in log and 'column "admin_email" does not exist' in log, "504: traceback and root cause in the server log", debug)
c.ok("security", leaks_to_user() == [] and "second@apex.com" not in log, f"504: nothing sensitive shown to the user or logged {leaks_to_user()}", debug)
telemetry = q("SELECT diagnostic_error_code || ' | ' || error_context_vector || ' | ' || error_detail FROM system_telemetry_logs")
c.ok("telemetry", telemetry == 'ICJ-ERR-504 | employer_portal.registration.duplicate_check | ProgrammingError: column "admin_email" does not exist',
     f"504 written to system_telemetry_logs (telemetry still works): {telemetry}")
q("ALTER TABLE beta_employers RENAME COLUMN admin_email_tmp TO admin_email;")
c.ok("data", count("beta_employers") == 1, "504: registration not saved (safeguard kept)")

# 7. Telemetry contains no startup rows (they can't be written) and nothing sensitive
rows = q("SELECT coalesce(string_agg(diagnostic_error_code, ','), '') FROM system_telemetry_logs")
details = q("SELECT coalesce(string_agg(error_detail, ' '), '') FROM system_telemetry_logs")
c.ok("telemetry", rows == "ICJ-ERR-504" and not any(s in details for s in (WRONG_PASSWORD, MALFORMED_PASSWORD, "@")),
     f"telemetry: only the 504 row; no passwords or emails ({rows})")

c.summary()
