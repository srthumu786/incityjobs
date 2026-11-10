"""Employer Recommended Candidates: consent, audit, generation, anonymization,
isolation, idempotency and concurrency (docs/EMPLOYER_RECOMMENDATIONS_PLAN.md).

Runs the real app.py on the simulated Streamlit runtime against the isolated,
password-protected local PostgreSQL started by tests/run_local.sh."""
import os
import runpy

from _common import Checks, REPO, fresh_db, load_legacy_schema, require_local_postgres, sql

require_local_postgres()
import psycopg2  # local-only test adapter
import auth
import recommendations as recs
from _streamlit_sim import RESOURCE_CACHE, Rerun, StopScript, ui

c = Checks("recommendation tests")
DB = "recs"
fresh_db(DB)
load_legacy_schema(DB)
# An existing (pre-consent) candidate, as on production today
sql(DB, """INSERT INTO candidate_profiles (name, role, resume, address, latitude, longitude, status)
           VALUES ('Existing Person', 'Dev', 'python sql docker developer', 'Priceville, AL', 34.5251, -86.8947, 'active_matching_pool');""")

MANAGER_PASSWORD = "correct horse battery staple"
SECRETS = {"connection_string": "postgresql://ignored-by-test-adapter", "managers": {"alice": auth.hash_password(MANAGER_PASSWORD)}}
LOCK = "Lock Profile & Validate Location Paths"


def run(nav="Home Portal", inputs=None, clicks=(), secrets=None):
    ui.out.clear(); ui.forms.clear(); psycopg2.connection.log.clear()
    ui.nav, ui.inputs, ui.clicks = nav, inputs or {}, set(clicks)
    ui.secrets.clear(); ui.secrets.update(SECRETS if secrets is None else secrets)
    try:
        runpy.run_path(os.path.join(REPO, "app.py"), run_name="__main__")
        return "ok"
    except StopScript:
        return "stopped"
    except Rerun:
        return "rerun"


def texts(kind=None): return [t for k, t in ui.out if kind is None or k == kind]
def new_session(state=None): ui.session_state.clear(); ui.session_state.update(state or {})
def session(): return dict(ui.session_state)
def q(text): return sql(DB, text)
def debug(): return {"out": ui.out[-8:], "sql": list(psycopg2.connection.log)[-5:]}


def candidate_form(name="Jane", email="jane@x.com", consent=False, city="Priceville", resume="Python developer: sql, docker, microservices"):
    form = {"Full Name (Absolute Identity Mapping)": name, "Secure Communication Email Node": email,
            "Target Role / Job Title": "Dev", "Paste Resume Text / Background Metrics": resume,
            "Target City": city, "Target State": "AL", "Target Zip Code": "35603"}
    if consent:
        form["candidate_matching_consent"] = True
    return form


def consent_row(email):
    return q(f"SELECT employer_matching_consent || '|' || coalesce(employer_matching_consent_policy, '-') || '|' || "
             f"(employer_matching_consent_at IS NOT NULL) FROM candidate_profiles WHERE contact_email = '{email}'")


def audit(event=None, where="true"):
    cond = f"event_type = '{event}' AND {where}" if event else where
    return int(q(f"SELECT count(*) FROM privacy_audit_log WHERE {cond}"))


# ================================================================== consent capture (commit 2)
new_session()
run("Employee Portal")
c.ok("consent", "Employer Matching Consent" in " ".join(texts("markdown")) and recs.CONSENT_POLICY_VERSION in " ".join(texts("caption")),
     "intake form shows the opt-in checkbox with its policy version", debug)
run("Employee Portal", candidate_form("No Consent", "noconsent@x.com", consent=False), {LOCK})  # first DB use: app migrates
c.ok("consent", q("SELECT employer_matching_consent FROM candidate_profiles WHERE name = 'Existing Person'") == "f",
     "existing candidate has no consent after migration (previous submission is not authorization)")
c.ok("consent", consent_row("noconsent@x.com") == "false|-|false" and audit(where="event_type LIKE 'CONSENT%'") == 0,
     "checkbox left unticked: consent FALSE, no timestamp/policy, nothing audited", debug)
c.ok("consent", any("Employer matching: OFF" in t for t in texts("info")), "candidate is told matching is OFF")

new_session()
run("Employee Portal", candidate_form("Jane", "jane@x.com", consent=True), {LOCK})
JANE = int(q("SELECT id FROM candidate_profiles WHERE contact_email = 'jane@x.com'"))
c.ok("consent", consent_row("jane@x.com") == f"true|{recs.CONSENT_POLICY_VERSION}|true", "explicit opt-in: consent TRUE with timestamp and policy version", debug)
c.ok("audit", audit("CONSENT_GRANTED", f"candidate_profile_id = {JANE} AND actor_type = 'candidate' AND detail->>'policy_version' = '{recs.CONSENT_POLICY_VERSION}'") == 1,
     "CONSENT_GRANTED audited (actor candidate, policy version)")
c.ok("consent", q("SELECT contact_disclosure_consent FROM candidate_profiles WHERE id = %d" % JANE) == "f",
     "matching consent does NOT authorize contact disclosure (separate flag stays FALSE)")
JANE_SESSION = session()

# revocable in the candidate's own session
run("Employee Portal", clicks={"withdraw_own_consent"})
c.ok("consent", consent_row("jane@x.com").startswith("false|") and audit("CONSENT_WITHDRAWN", f"candidate_profile_id = {JANE} AND actor_type = 'candidate'") == 1,
     "candidate withdraws consent in own session: FALSE and audited", debug)
run("Employee Portal", clicks={"grant_own_consent"})
c.ok("consent", consent_row("jane@x.com").startswith("true|") and audit("CONSENT_GRANTED", f"candidate_profile_id = {JANE}") == 2,
     "candidate can opt back in (audited again)")
run("Employee Portal", clicks={"grant_own_consent"})
c.ok("audit", audit("CONSENT_GRANTED", f"candidate_profile_id = {JANE}") == 2, "repeating the same choice writes no extra audit row (idempotent)")

new_session()
run("Employee Portal")
c.ok("security", "withdraw_own_consent" not in str(ui.out) and not any("Your Employer Matching Consent" in t for t in texts("markdown")),
     "a different session sees no consent controls (only your own profile)")

# manager records a candidate-requested withdrawal; managers can't grant
new_session()
run("Home Portal", {"Manager username": "alice", "Manager password": MANAGER_PASSWORD}, {"Sign in"})
ALICE = session()
run("Manager Console", {"consent_lookup_email": " JANE@x.com "}, {"Look up consent status"})
run("Manager Console", clicks={"manager_withdraw_consent"})
c.ok("consent", consent_row("jane@x.com").startswith("false|") and audit("CONSENT_WITHDRAWN", f"candidate_profile_id = {JANE} AND actor_type = 'manager' AND actor_ref = 'alice'") == 1,
     "manager records candidate-requested withdrawal (actor manager:alice)", debug)
run("Manager Console")
c.ok("consent", "manager_withdraw_consent" not in str(ui.out) and not any("grant" in t.lower() and "button" in t.lower() for t in texts()),
     "no manager control exists to grant consent on a candidate's behalf")
audit_table = [t for t in texts("dataframe") if "CONSENT_WITHDRAWN" in t]
c.ok("audit", audit_table and "jane@x.com" not in audit_table[0] and "Jane" not in audit_table[0],
     "Manager Console shows the audit trail (events, actors, ids) with no names or emails")

new_session(JANE_SESSION)
run("Employee Portal", clicks={"grant_own_consent"})
c.ok("consent", consent_row("jane@x.com").startswith("true|"), "candidate can opt in again after a manager-recorded withdrawal")

c.summary()
