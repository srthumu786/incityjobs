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


# ================================================================== recommendation backend (commit 3)
import json
import re
import threading

PUBLISH, REGISTER = "PUBLISH LIVE REQUISITION", "INITIALIZE BETA PROFILE"
DSN = "postgresql://ignored-by-test-adapter"


def employer_form(company, email, pw="employer-pass-123"):
    return {"Legal Business / Company Name": company, "Corporate Headquarters Address (Billing/Primary Operations)": "1 Main St",
            "Primary Administrator Email Address": email, "Local Direct Phone Number": "555",
            "Create Employer Password (at least 12 characters)": pw, "Repeat Employer Password": pw,
            "I explicitly agree to the National Terms of Service.": True, "Type Authorized Representative Full Name to Sign": "Ann"}


def job_form(title="Python Developer", req="python sql docker microservices developer", city="Priceville"):
    return {"Job Position Title": title, "Job Requirements / Description (used for candidate matching)": req,
            "Worksite Street Address": "450 Logistics Blvd", "Worksite City": city, "Worksite State": "AL", "Worksite Zip Code": "35603"}


def with_cursor(fn, *args, **kwargs):
    """Call a recommendations.* backend function in its own committed transaction."""
    conn = psycopg2.connect(DSN)
    try:
        with conn.cursor() as cur:
            out = fn(cur, *args, **kwargs)
        conn.commit()
        return out
    finally:
        conn.close()


def rec(candidate_id, job_id):
    return q(f"SELECT status || '|' || coalesce(status_reason, '-') || '|' || is_new FROM employer_recommendations "
             f"WHERE candidate_profile_id = {candidate_id} AND job_id = {job_id}")


def events(event, where="true"):
    return int(q(f"SELECT count(*) FROM privacy_audit_log WHERE event_type = '{event}' AND {where}"))


# --- an employer account, verified by a manager, posts a job (all through the app)
q("UPDATE candidate_profiles SET contact_email = 'existing@x.com' WHERE name = 'Existing Person';")  # staging-era candidate
new_session()
run("Employer Portal", employer_form("Apex", "hr@apex.com"), {REGISTER})
APEX = int(q("SELECT id FROM beta_employers WHERE admin_email = 'hr@apex.com'"))
APEX_SESSION = session()
new_session(ALICE)
run("Manager Console", {"Employer": f"#{APEX}"}, {"verify_employer"})
new_session(APEX_SESSION)
run("Employer Portal", job_form(), {PUBLISH})
JOB = int(q("SELECT id FROM employer_jobs WHERE title = 'Python Developer' AND employer_id = %d" % APEX))
NOCON = int(q("SELECT id FROM candidate_profiles WHERE contact_email = 'noconsent@x.com'"))
EXIST = int(q("SELECT id FROM candidate_profiles WHERE contact_email = 'existing@x.com'"))
c.ok("generation", rec(JANE, JOB) == "AVAILABLE|-|true", f"job published -> consenting matching candidate AVAILABLE with a 'new' badge ({rec(JANE, JOB)})", debug)
c.ok("generation", rec(NOCON, JOB) == "QUEUED|awaiting_candidate_consent|false" and rec(EXIST, JOB) == "QUEUED|awaiting_candidate_consent|false",
     "non-consenting candidates, new AND existing, stay QUEUED (awaiting consent)")
c.ok("audit", events("RECOMMENDATION_AVAILABLE", f"job_id = {JOB}") == 1 and events("RECOMMENDATION_QUEUED", f"job_id = {JOB}") == 2,
     "each creation audited once (1 AVAILABLE, 2 QUEUED)")

# --- idempotency: repeating generation changes nothing
snapshot = lambda: q("SELECT md5(string_agg(t::text, '|' ORDER BY t.id)) FROM (SELECT id, status, status_reason, is_new, fingerprint, "
                     "notified_fingerprint, employer_ref FROM employer_recommendations) t") + q("SELECT count(*) FROM privacy_audit_log")
before = snapshot()
for _ in range(3):
    with_cursor(recs.generate_for_job, JOB)
    with_cursor(recs.generate_for_candidate, JANE)
new_session(ALICE); run("Manager Console", clicks={"recompute_all_recommendations"})
c.ok("duplicates", snapshot() == before, "repeated generation (3x job, 3x candidate, manager recompute): no new rows, refs, badges or audit events", debug)

# --- consent withdrawal / re-grant (through the app)
new_session(JANE_SESSION)
run("Employee Portal", clicks={"withdraw_own_consent"})
c.ok("status", rec(JANE, JOB) == "WITHDRAWN|awaiting_candidate_consent|true" and events("RECOMMENDATION_WITHDRAWN", f"candidate_profile_id = {JANE}") == 1,
     "consent withdrawn -> recommendation WITHDRAWN immediately (audited)", debug)
c.ok("status", with_cursor(recs.employer_recommendations, APEX) == [], "withdrawn: employer read returns nothing")
run("Employee Portal", clicks={"grant_own_consent"})
c.ok("duplicates", rec(JANE, JOB) == "AVAILABLE|-|true" and events("RECOMMENDATION_RESTORED", f"candidate_profile_id = {JANE}") == 1
     and events("RECOMMENDATION_AVAILABLE", f"candidate_profile_id = {JANE}") == 1,
     "consent re-granted -> restored; unchanged content raises no second 'available' notification")

# --- employer opens the profile (backend; UI in commit 4)
ref = q(f"SELECT employer_ref FROM employer_recommendations WHERE candidate_profile_id = {JANE} AND job_id = {JOB}")
opened = with_cursor(recs.open_recommendation, APEX, ref, actor_ref=str(APEX))
c.ok("status", opened is not None and rec(JANE, JOB) == "VIEWED|-|false" and events("PROFILE_VIEWED", f"recommendation_id IS NOT NULL AND candidate_profile_id = {JANE}") == 1,
     "employer opens profile -> VIEWED, badge cleared, view audited")
new_session(JANE_SESSION)
run("Employee Portal", clicks={"withdraw_own_consent"}); run("Employee Portal", clicks={"grant_own_consent"})
c.ok("duplicates", rec(JANE, JOB) == "VIEWED|-|false", "withdraw + re-grant of an already-viewed, unchanged profile restores VIEWED with no badge")

# --- resume update (Candidate Module will bump resume_version) re-surfaces exactly once
q(f"UPDATE candidate_profiles SET resume_version = 2 WHERE id = {JANE}")
with_cursor(recs.generate_for_candidate, JANE)
c.ok("duplicates", rec(JANE, JOB) == "AVAILABLE|-|true" and events("RECOMMENDATION_REFRESHED", f"candidate_profile_id = {JANE}") == 1,
     "resume update -> AVAILABLE with badge, audited as REFRESHED")
with_cursor(recs.generate_for_candidate, JANE); with_cursor(recs.generate_for_job, JOB)
c.ok("duplicates", events("RECOMMENDATION_REFRESHED", f"candidate_profile_id = {JANE}") == 1, "unchanged afterwards: no repeated refresh notification")

# --- employer verification revoked / restored (through the Manager Console)
new_session(ALICE)
run("Manager Console", {"Employer": f"#{APEX}"}, {"revoke_employer_verification"})
c.ok("status", rec(JANE, JOB) == "WITHDRAWN|employer_unverified|true" and with_cursor(recs.employer_recommendations, APEX) == [],
     "verification revoked -> WITHDRAWN (employer_unverified) and nothing readable", debug)
run("Manager Console", {"Employer": f"#{APEX}"}, {"verify_employer"})
c.ok("status", rec(JANE, JOB) == "AVAILABLE|-|true", "re-verified -> restored")

# --- candidate no longer matches (e.g. rejected by the state machine)
q(f"UPDATE candidate_profiles SET application_status = 'REJECTED' WHERE id = {JANE}")
with_cursor(recs.generate_for_candidate, JANE)
c.ok("status", rec(JANE, JOB).startswith("WITHDRAWN|no_longer_matches"), "candidate no longer matches -> WITHDRAWN (no_longer_matches)")
q(f"UPDATE candidate_profiles SET application_status = 'PENDING' WHERE id = {JANE}")
with_cursor(recs.generate_for_candidate, JANE)

# --- read-time guard: stale statuses can never expose a profile
q(f"UPDATE candidate_profiles SET employer_matching_consent = FALSE WHERE id = {JANE}")  # bypasses the app: no reconcile
c.ok("security", rec(JANE, JOB).startswith("AVAILABLE") and with_cursor(recs.employer_recommendations, APEX) == []
     and with_cursor(recs.open_recommendation, APEX, ref) is None and events("PROFILE_ACCESS_DENIED", f"employer_id = {APEX}") >= 1,
     "status still AVAILABLE but consent revoked out-of-band: list empty, open denied (and audited)")
q(f"UPDATE candidate_profiles SET employer_matching_consent = TRUE WHERE id = {JANE}")
q(f"UPDATE beta_employers SET verified_at = NULL WHERE id = {APEX}")
c.ok("security", with_cursor(recs.employer_recommendations, APEX) == [] and with_cursor(recs.open_recommendation, APEX, ref) is None,
     "verification revoked out-of-band: nothing readable even before statuses update")
q(f"UPDATE beta_employers SET verified_at = now() WHERE id = {APEX}")

# --- anonymization against direct AND indirect identifiers
rich = {"name": "Dr. Maria Gonzalez-Whitfield", "email": "maria.gw@example.org",
        "role": "Senior Python Engineer at Acme Robotics (ex-Google), Huntsville; age 41; she/her; Auburn University class of 2009",
        # Repetitive on purpose: the current engine only matches short, keyword-dense resumes
        # (see docs/MATCHING_ENGINE_ASSESSMENT.md). Identifiers are still embedded throughout.
        "resume": ("Python developer python sql docker microservices developer python sql docker. "
                   "Maria Gonzalez-Whitfield 555-0147 linkedin mariagw Acme Google Auburn 2009 age41 veteran Tagalog GXPN4471"),
        "street": "1427 Magnolia Ridge Dr", "current_city": "Huntsville", "current_state": "AL"}
new_session()
run("Employee Portal", {**candidate_form(rich["name"], rich["email"], consent=True, resume=rich["resume"]),
                        "Target Role / Job Title": rich["role"], "Target Worksite Proximity Street Address": rich["street"],
                        "Current Street Address": "88 Oak Hollow Ln", "Current City": rich["current_city"], "Current State": rich["current_state"]}, {LOCK})
MARIA = int(q("SELECT id FROM candidate_profiles WHERE contact_email = 'maria.gw@example.org'"))
stored = q(f"SELECT profile::text FROM employer_recommendations WHERE candidate_profile_id = {MARIA} AND job_id = {JOB}")
visible = json.dumps(with_cursor(recs.employer_recommendations, APEX))
c.ok("anonymization", stored and set(json.loads(stored)) == set(recs.PROFILE_KEYS), f"stored profile has only allow-listed keys: {stored}")
source = " ".join([rich["name"], rich["email"], rich["role"], rich["resume"], rich["street"], "88 Oak Hollow Ln",
                   rich["current_city"], rich["current_state"], "Priceville", "35603"])
candidate_tokens = {t.lower() for t in re.findall(r"[A-Za-z0-9]{3,}", source)}
allowed = {w for w in recs._QUALIFICATION_VOCABULARY} | {"python", "developer"} | {"under", "mi", "profile_schema"}
leaked = sorted((candidate_tokens & {t.lower() for t in re.findall(r"[A-Za-z0-9]{3,}", stored + visible)}) - allowed)
c.ok("anonymization", leaked == [], f"no candidate word outside the qualification dictionary reaches employers (indirect identifiers: employer, school, "
     f"age, pronouns, veteran/ethnicity cues, language, cert number, phone, URL, address, city, ZIP) {leaked}")
visible_profiles = json.dumps([r["profile"] for r in json.loads(visible)])
exact = q(f"SELECT round(score::numeric, 2) || '|' || round(distance_miles::numeric, 1) FROM employer_recommendations "
          f"WHERE candidate_profile_id = {MARIA} AND job_id = {JOB}").split("|")
c.ok("anonymization", not re.search(r"\d{3}\D?\d{4}|@|linkedin|\b\d{5}\b", stored + visible_profiles)
     and not any(v in stored + visible_profiles for v in exact if v not in ("0.0",)),
     f"no phone/email/URL/ZIP patterns and no exact score or distance ({exact}) in employer-visible profiles")
c.ok("anonymization", "C-" in visible and str(MARIA) not in [r["ref"] for r in json.loads(visible)], "candidates appear only as opaque C-… references")

# --- internal ledger payloads no longer copy name/email
new_session(ALICE)
run("Manager Console", {"Job to match": f"#{JOB}"}, {"run_live_matching"})
payloads = q("SELECT coalesce(string_agg(payload::text, ' '), '') FROM semantic_matches")
c.ok("anonymization", payloads and "maria.gw@example.org" not in payloads and "Gonzalez" not in payloads and "jane@x.com" not in payloads,
     "new semantic_matches payloads reference candidates by id only (no name or email)")

# --- concurrency: parallel generation + consent changes on the same pairs
q(f"INSERT INTO employer_jobs (title, department, pricing_tier, address, latitude, longitude, source_channel, employer_id, requirements, match_radius_miles) "
  f"VALUES ('Concurrent Job', 'Technical Services', 'Tier 1', 'Priceville', 34.5251, -86.8947, 'test', {APEX}, 'python sql docker microservices developer', 25);")
CJOB = int(q("SELECT id FROM employer_jobs WHERE title = 'Concurrent Job'"))
q("INSERT INTO candidate_profiles (name, contact_email, role, resume, latitude, longitude, application_status, employer_matching_consent) "
  "SELECT 'Load ' || g, 'load' || g || '@x.com', 'Dev', 'Python developer: sql, docker, microservices', 34.5251, -86.8947, 'PENDING', TRUE "
  "FROM generate_series(1, 12) g;")
LOAD = [int(x) for x in q("SELECT string_agg(id::text, ',' ORDER BY id) FROM candidate_profiles WHERE contact_email LIKE 'load%@x.com'").split(",")]
errors, retries = [], []
barrier = threading.Barrier(10)


def worker(n):
    barrier.wait()
    for attempt in range(5):
        conn = psycopg2.connect(DSN)
        try:
            with conn.cursor() as cur:
                if n < 4:
                    recs.generate_for_job(cur, CJOB)
                elif n < 8:
                    recs.generate_for_candidate(cur, LOAD[n])
                else:  # withdraw consent for two candidates while others generate
                    target = LOAD[n - 8]
                    recs.set_matching_consent(cur, target, False, "candidate", "concurrency_test")
                    conn.commit()
                    recs.generate_for_candidate(cur, target)
            conn.commit()
            return
        except Exception as exc:
            conn.rollback()
            if getattr(exc, "pgcode", None) in ("40P01", "40001"):
                retries.append(n)
                continue
            errors.append(f"{n}: {type(exc).__name__}: {exc}")
            return
        finally:
            conn.close()


threads = [threading.Thread(target=worker, args=(n,)) for n in range(10)]
[t.start() for t in threads]
[t.join() for t in threads]
with_cursor(recs.generate_for_job, CJOB)  # what any later trigger would do
c.ok("concurrency", errors == [], f"10 concurrent workers finished without errors (deadlock retries: {len(retries)}) {errors}")
totals = q(f"SELECT count(*) || '/' || count(DISTINCT candidate_profile_id) FROM employer_recommendations WHERE job_id = {CJOB}").split("/")
per_load = q(f"SELECT count(*) FROM employer_recommendations WHERE job_id = {CJOB} AND candidate_profile_id IN ({','.join(map(str, LOAD))})")
c.ok("concurrency", totals[0] == totals[1] and per_load == str(len(LOAD)),
     f"exactly one recommendation per (job, candidate) after concurrent generation (rows {totals[0]}, distinct candidates {totals[1]}, load candidates {per_load}/{len(LOAD)})")
dup_events = q(f"SELECT count(*) FROM (SELECT recommendation_id FROM privacy_audit_log WHERE job_id = {CJOB} AND event_type = 'RECOMMENDATION_AVAILABLE' "
               f"GROUP BY recommendation_id HAVING count(*) > 1) d")
c.ok("concurrency", dup_events == "0", "no recommendation got a duplicate 'available' audit event")
stale = q(f"SELECT count(*) FROM employer_recommendations r JOIN candidate_profiles c ON c.id = r.candidate_profile_id "
          f"WHERE r.job_id = {CJOB} AND r.status IN ('AVAILABLE', 'VIEWED') AND NOT c.employer_matching_consent")
withdrawn_states = [rec(cid, CJOB) for cid in LOAD[:2]]
c.ok("concurrency", stale == "0" and all(st_.split("|")[0] in ("QUEUED", "WITHDRAWN") and st_.split("|")[1] == "awaiting_candidate_consent"
                                         for st_ in withdrawn_states),
     f"consent withdrawn during concurrent generation: final statuses reflect current consent, never visible {withdrawn_states}")

c.summary()
