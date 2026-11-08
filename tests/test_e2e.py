"""End-to-end security, data-safety and processing tests.

The whole app.py runs on a simulated Streamlit runtime (widgets, session state,
reruns, cache_resource) against a REAL isolated local PostgreSQL database that
starts as a legacy copy of production (tests/fixtures/legacy_main_schema.sql),
so the app's own startup migration runs too. Geocoding is simulated: no network."""
import json
import math
import os
import runpy
import sys
import time
import types

from _common import Checks, REPO, fresh_db, load_legacy_schema, require_local_postgres, sql

require_local_postgres()
import psycopg2  # local-only test adapter
import auth

c = Checks("end-to-end tests")
DB = "e2e"
fresh_db(DB)
load_legacy_schema(DB)
# Legacy production-like rows: one candidate and one job stored at the old fallback point
sql(DB, """
INSERT INTO candidate_profiles (name, role, resume, address, latitude, longitude, status)
VALUES ('Legacy Fallback Candidate', 'Dev', 'Python developer: sql, docker, microservices', 'Unknown', 34.5262, -86.9556, 'active_matching_pool');
INSERT INTO employer_jobs (title, department, pricing_tier, address, latitude, longitude)
VALUES ('Legacy Fallback Job', 'Logistics', 'Tier 1', 'Unknown', 34.5262, -86.9556);
""")
LEGACY_ROWS = sql(DB, "SELECT md5(string_agg(t::text, '|')) FROM (SELECT id, name, latitude, longitude FROM candidate_profiles ORDER BY id) t;")

def q(text): return sql(DB, text)
def count(table, where="true"):
    if q(f"SELECT to_regclass('{table}') IS NULL;") == "t":
        return 0
    return int(q(f"SELECT count(*) FROM {table} WHERE {where};"))

# ------------------------------------------------------------------ simulated geopy (no network)
class GeocoderServiceError(Exception): pass
PLACES = {"priceville": (34.5251, -86.8947), "decatur": (34.6059, -86.9833), "austin": (30.2672, -97.7431)}
class _Loc:
    def __init__(self, la, lo): self.latitude, self.longitude = la, lo
class Nominatim:
    def __init__(self, user_agent): pass
    def geocode(self, query, timeout=5):
        if "SERVICEDOWN" in query: raise GeocoderServiceError("503 Service Unavailable")
        for name, coords in PLACES.items():
            if name in query.lower(): return _Loc(*coords)
        return None
class geodesic:
    def __init__(self, a, b):
        r = 3958.8; la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
        h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
        self.miles = 2 * r * math.asin(math.sqrt(h))
geopy = types.ModuleType("geopy"); _gc = types.ModuleType("geopy.geocoders"); _gd = types.ModuleType("geopy.distance")
_gc.Nominatim, _gd.geodesic = Nominatim, geodesic
geopy.geocoders, geopy.distance = _gc, _gd
sys.modules.update({"geopy": geopy, "geopy.geocoders": _gc, "geopy.distance": _gd})

try:
    import pandas  # noqa: F401  (used by st.map in the Employee Portal)
except ImportError:  # tiny stand-in so the suite also runs without pandas
    _pd = types.ModuleType("pandas")
    class DataFrame(list):
        def to_dict(self, orient): return list(self)
    _pd.DataFrame = DataFrame
    sys.modules["pandas"] = _pd

# ------------------------------------------------------------------ simulated streamlit
class StopScript(BaseException): pass
class Rerun(BaseException): pass
class SessionState(dict):
    __getattr__ = dict.get
    def __setattr__(self, k, v): self[k] = v
class _Ctx:
    def __init__(self, ui): self.ui = ui
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def __getattr__(self, n): return getattr(self.ui, n)

RESOURCE_CACHE = {}  # like st.cache_resource: survives reruns, per server process
class UI:
    def __init__(self):
        self.out, self.inputs, self.clicks, self.forms = [], {}, set(), set()
        self.nav, self.radio_options = None, None
        self.session_state, self.secrets = SessionState(), {}
    def _rec(kind):
        def f(self, *a, **k): self.out.append((kind, " ".join(str(x) for x in a)))
        return f
    title = _rec("title"); subheader = _rec("subheader"); markdown = _rec("markdown"); write = _rec("write")
    caption = _rec("caption"); info = _rec("info"); success = _rec("success"); warning = _rec("warning")
    error = _rec("error"); text = _rec("text"); toast = _rec("toast"); balloons = _rec("balloons"); code = _rec("code")
    def json(self, obj): self.out.append(("json", json.dumps(obj)))
    def metric(self, label, value, **k): self.out.append(("metric", f"{label}={value}"))
    def dataframe(self, data, **k): self.out.append(("dataframe", json.dumps(data, default=str)))
    def map(self, df, **k):
        assert "use_container_width" not in k
        self.out.append(("map", json.dumps(df.to_dict("records"))))
    def columns(self, spec): return [_Ctx(self) for _ in range(spec if isinstance(spec, int) else len(spec))]
    def tabs(self, labels): return [_Ctx(self) for _ in labels]
    def form(self, key, **k): self.forms.add(key); return _Ctx(self)
    def expander(self, label, **k): self.out.append(("expander", label)); return _Ctx(self)
    def spinner(self, *a, **k): return _Ctx(self)
    def container(self): return _Ctx(self)
    def text_input(self, label, key=None, **k): return self.inputs.get(key or label, "")
    def text_area(self, label, value="", **k): return self.inputs.get(label, value)
    def checkbox(self, label, **k): return self.inputs.get(label, False)
    def selectbox(self, label, options, **k):
        want = self.inputs.get(label)
        return next((o for o in options if want and o.startswith(want)), options[0])
    def slider(self, label, a, b, v, **k): return self.inputs.get(label, v)
    def radio(self, label, options, **k):
        if label == "Navigate Portals:":
            self.radio_options = list(options)
            return self.nav if self.nav in options else options[0]
        return options[0]
    def form_submit_button(self, label="", **k): return label in self.clicks
    def button(self, label, key=None, disabled=False, **k):
        return not disabled and (label in self.clicks or (key in self.clicks if key else False))
    def stop(self): raise StopScript()
    def rerun(self): raise Rerun()
    def cache_resource(self, show_spinner=False):
        def deco(fn):
            def wrapper():
                if fn.__name__ not in RESOURCE_CACHE:
                    RESOURCE_CACHE[fn.__name__] = fn()  # exceptions are not cached
                return RESOURCE_CACHE[fn.__name__]
            wrapper.__name__ = fn.__name__
            return wrapper
        return deco

ui = UI()
st = types.ModuleType("streamlit")
for _name in dir(UI):
    if not _name.startswith("__"):
        setattr(st, _name, getattr(ui, _name))
st.session_state, st.secrets, st.sidebar = ui.session_state, ui.secrets, ui
sys.modules["streamlit"] = st
time.sleep = lambda s: None  # skip the 1-second failed-login delay

MANAGER_PASSWORD = "correct horse battery staple"
SECRETS = {"connection_string": "postgresql://ignored-by-test-adapter", "managers": {"alice": auth.hash_password(MANAGER_PASSWORD)}}

def run(nav="Home Portal", inputs=None, clicks=(), secrets=None):
    """One Streamlit script run for the current session."""
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
def sql_log(): return list(psycopg2.connection.log)
def new_session(state=None): ui.session_state.clear(); ui.session_state.update(state or {})
def session(): return dict(ui.session_state)
def signing_key(): return RESOURCE_CACHE["_session_signing_key"]
def debug(): return {"out": ui.out[-8:], "sql": sql_log()[-5:]}

PUBLISH, REGISTER, LOCK = "PUBLISH LIVE REQUISITION", "INITIALIZE BETA PROFILE", "Lock Profile & Validate Location Paths"
def employer_form(email="HR@Apex.com", pw="apex-strong-pass-1", pw2=None):
    return {"Legal Business / Company Name": "Apex", "Corporate Headquarters Address (Billing/Primary Operations)": "1 Main St",
            "Primary Administrator Email Address": email, "Local Direct Phone Number": "555",
            "Create Employer Password (at least 12 characters)": pw, "Repeat Employer Password": pw if pw2 is None else pw2,
            "I explicitly agree to the National Terms of Service.": True, "Type Authorized Representative Full Name to Sign": "Ann"}
def job_form(city="Priceville", title="Python Developer", req="python sql docker microservices developer", street="450 Logistics Blvd"):
    return {"Job Position Title": title, "Job Requirements / Description (used for candidate matching)": req,
            "Worksite Street Address": street, "Worksite City": city, "Worksite State": "AL", "Worksite Zip Code": "35603"}
def candidate_form(name="Jane", email=" Jane@X.com ", city="Priceville", resume="Python developer: sql, docker, microservices"):
    return {"Full Name (Absolute Identity Mapping)": name, "Secure Communication Email Node": email,
            "Target Role / Job Title": "Dev", "Paste Resume Text / Background Metrics": resume,
            "Target City": city, "Target State": "AL", "Target Zip Code": "35603"}
def sign_in_employer(email, password):
    return run("Employer Portal", {"employer_sign_in_email": email, "employer_sign_in_password": password}, {"Sign In"})
def manager_sign_in(password=MANAGER_PASSWORD, user="alice", secrets=None):
    return run("Home Portal", {"Manager username": user, "Manager password": password}, {"Sign in"}, secrets)

# ================================================================== public access
c.ok("security", run("Home Portal", secrets={"connection_string": "x"}) == "ok" and "Manager Console" not in ui.radio_options,
     "public visitor: no Manager Console in navigation")
c.ok("security", any("disabled" in t for t in texts("caption")), "no [managers] secret: Manager Console disabled")
run("Employer Portal", job_form(), {PUBLISH})
c.ok("security", "job_requisition_form" not in ui.forms and "employer_sign_in_form" in ui.forms and count("employer_jobs") == 1,
     "signed-out employer: no job form, PUBLISH inserts nothing", debug)
c.ok("data", q(f"SELECT md5(string_agg(t::text, '|')) FROM (SELECT id, name, latitude, longitude FROM candidate_profiles ORDER BY id) t;") == LEGACY_ROWS,
     "app startup migration ran on the legacy copy without altering legacy rows")

# forged / tampered / wrong-role / expired employer tokens
key = signing_key()
valid = auth.issue_token(key, 1, auth.ROLE_EMPLOYER, 3600, extra={"cred": "x"})
forgeries = {
    "random string": "not-a-token",
    "signed with another key": auth.issue_token(b"x" * 32, 1, auth.ROLE_EMPLOYER, 3600),
    "manager token as employer": auth.issue_token(key, 1, auth.ROLE_MANAGER, 3600),
    "expired": auth.issue_token(key, 1, auth.ROLE_EMPLOYER, -1),
    "claims edited, old signature": auth._b64e(json.dumps({"sub": "2", "role": "employer", "exp": 9999999999}).encode()) + "." + valid.split(".")[1],
    "valid signature, wrong credential fingerprint": valid,
}
for label, token in forgeries.items():
    new_session({"employer_token": token})
    run("Employer Portal", job_form(), {PUBLISH})
    c.ok("security", "job_requisition_form" not in ui.forms and count("employer_jobs") == 1, f"forged employer session rejected: {label}")

# ================================================================== registration -> unverified (no posting privileges)
new_session()
run("Employer Portal", employer_form(pw="short"), {REGISTER})
c.ok("security", count("beta_employers") == 0, "weak password (<12 chars) rejected at registration")
run("Employer Portal", employer_form(pw2="different-pass-99"), {REGISTER})
c.ok("security", count("beta_employers") == 0, "mismatched password confirmation rejected")
run("Employer Portal", employer_form(), {REGISTER})
stored = q("SELECT admin_email || ' | ' || password_hash || ' | ' || (verified_at IS NULL) FROM beta_employers;")
c.ok("security", stored.startswith("hr@apex.com | pbkdf2_sha256$600000$") and stored.endswith("| true") and "apex-strong-pass-1" not in stored,
     "new employer: lowercase email, PBKDF2 hash, UNVERIFIED")
APEX = int(q("SELECT id FROM beta_employers WHERE admin_email = 'hr@apex.com';"))
APEX_SESSION = session()
run("Employer Portal", job_form(), {PUBLISH})
c.ok("security", "job_requisition_form" not in ui.forms and any("Verification pending" in t for t in texts("warning")) and count("employer_jobs") == 1,
     "unverified employer is signed in but has NO posting privileges (no form, PUBLISH inserts nothing)", debug)

new_session()
run("Employer Portal", employer_form(email="  hr@APEX.com "), {REGISTER})
c.ok("data", count("beta_employers") == 1 and any("REDUNDANCY" in t for t in texts("warning")), "duplicate registration (other case/spaces) blocked")

# ================================================================== manager sign-in and verification
new_session()
manager_sign_in("wrong")
c.ok("security", "manager_token" not in ui.session_state, "wrong manager password rejected")
manager_sign_in(user="mallory")
c.ok("security", "manager_token" not in ui.session_state, "unknown manager username rejected")
c.ok("processing", manager_sign_in() == "rerun" and run("Manager Console") == "ok" and "Manager Console" in ui.radio_options, "manager alice signs in")
ALICE = session()
for label, token in {
    "random string": "nope",
    "signed with another key": auth.issue_token(b"y" * 32, "alice", auth.ROLE_MANAGER, 3600),
    "employer token as manager": auth.issue_token(signing_key(), "alice", auth.ROLE_EMPLOYER, 3600),
    "expired": auth.issue_token(signing_key(), "alice", auth.ROLE_MANAGER, -1),
    "valid signature, missing credential fingerprint": auth.issue_token(signing_key(), "alice", auth.ROLE_MANAGER, 3600),
}.items():
    new_session({"manager_token": token})
    run("Manager Console")
    c.ok("security", "Manager Console" not in ui.radio_options and not any("FROM candidate_profiles" in s for s in sql_log()),
         f"forged manager session rejected, no candidate data read: {label}")

new_session(ALICE)
run("Manager Console", {"Employer": f"#{APEX}"}, {"verify_employer"})
c.ok("processing", q(f"SELECT verified_by FROM beta_employers WHERE id = {APEX};") == "alice", "manager verifies Apex (verified_by recorded)")

# ================================================================== verified employer posts jobs; geocoding without fallback
new_session(APEX_SESSION)
run("Employer Portal")
c.ok("processing", "job_requisition_form" in ui.forms, "after verification the same session gets the job form")
run("Employer Portal", job_form(city="Nowhereville", street="1 Nowhere Rd"), {PUBLISH})
c.ok("data", count("employer_jobs") == 1 and any("Priceville, AL 35603" in t for t in texts("warning")),
     "ungeocodable worksite: NOT saved, user told how to format the city")
run("Employer Portal", job_form(city="SERVICEDOWN"), {PUBLISH})
c.ok("data", count("employer_jobs") == 1 and any("ICJ-ERR-506" in t for t in texts("warning"))
     and count("system_telemetry_logs", "diagnostic_error_code = 'ICJ-ERR-506'") == 1, "geocoder outage: NOT saved, ICJ-ERR-506 shown and logged")
run("Employer Portal", job_form(), {PUBLISH})
job = q("SELECT id || '|' || employer_id || '|' || latitude || '|' || longitude || '|' || source_channel FROM employer_jobs WHERE title = 'Python Developer';")
APEX_JOB = int(job.split("|")[0])
c.ok("processing", job.endswith(f"|{APEX}|34.5251|-86.8947|employer_portal"), f"job saved with employer_id + geocoded coordinates: {job}")
run("Employer Portal", job_form(title="Far Job", city="Austin"), {PUBLISH})

# ================================================================== session revocation on credential change
new_session()
c.ok("processing", sign_in_employer(" HR@apex.com", "apex-strong-pass-1") == "rerun", "Apex signs in on a second device")
DEVICE_2 = session()
new_session(ALICE)
run("Manager Console", {"Employer": f"#{APEX}"}, {"issue_setup_code"})
code = texts("code")[0]
new_session()
c.ok("processing", run("Employer Portal", {"setup_code_email": "hr@apex.com", "setup_code_value": code, "setup_code_password": "new-apex-pass-22",
                                           "setup_code_password_confirm": "new-apex-pass-22"}, {"Set Password & Sign In"}) == "rerun",
     "password changed via manager setup code (device 3)")
DEVICE_3 = session()
for label, state in {"device 1 (registration session)": APEX_SESSION, "device 2": DEVICE_2}.items():
    new_session(state)
    run("Employer Portal", job_form(title="Revoked post"), {PUBLISH})
    c.ok("security", "job_requisition_form" not in ui.forms and count("employer_jobs", "title = 'Revoked post'") == 0,
         f"credential change revoked the existing session on {label}")
new_session(DEVICE_3)
run("Employer Portal")
c.ok("processing", "job_requisition_form" in ui.forms, "the session that set the new password stays valid")
new_session()
c.ok("security", sign_in_employer("hr@apex.com", "apex-strong-pass-1") == "ok" and "Incorrect" in " ".join(texts("error")), "old password no longer works")

# direct credential change in the database also revokes
q(f"UPDATE beta_employers SET password_hash = '{auth.hash_password('db-reset-pass-33')}' WHERE id = {APEX};")
new_session(DEVICE_3)
run("Employer Portal")
c.ok("security", "job_requisition_form" not in ui.forms, "password replaced directly in the DB: existing session revoked")
new_session(); sign_in_employer("hr@apex.com", "db-reset-pass-33")
APEX_SESSION = session()

# manager credential replaced in secrets (same password, new hash) revokes the manager session
rotated = {"connection_string": "x", "managers": {"alice": auth.hash_password(MANAGER_PASSWORD)}}
new_session(ALICE)
run("Manager Console", secrets=rotated)
c.ok("security", "Manager Console" not in ui.radio_options, "manager password hash replaced in secrets: existing manager session revoked")
new_session()
c.ok("processing", manager_sign_in(secrets=rotated) == "rerun", "manager signs in again with the new credential")
SECRETS["managers"] = rotated["managers"]
ALICE = session()
new_session(ALICE)
run("Manager Console", secrets={"connection_string": "x", "managers": {"bob": SECRETS["managers"]["alice"]}})
c.ok("security", "Manager Console" not in ui.radio_options, "manager removed from secrets: session revoked")

# ================================================================== verification revoked -> privileges removed, jobs inactive
new_session(ALICE)
run("Manager Console", {"Employer": f"#{APEX}"}, {"revoke_employer_verification"})
new_session(APEX_SESSION)
run("Employer Portal", job_form(title="After revoke"), {PUBLISH})
c.ok("security", "job_requisition_form" not in ui.forms and count("employer_jobs", "title = 'After revoke'") == 0,
     "verification revoked: posting privileges removed immediately")
new_session()
run("Employee Portal", candidate_form(name="Early Bird", email="early@x.com"), {LOCK})
c.ok("security", not texts("map") and any("No job openings" in t for t in texts("info")), "jobs of an unverified employer are not shown to candidates")
new_session(ALICE)
run("Manager Console", {"Job to match": f"#{APEX_JOB}"}, {"run_live_matching"})
c.ok("security", any("not verified" in t for t in texts("warning")) and count("semantic_matches") == 0, "live matching refuses jobs of an unverified employer")
run("Manager Console", {"Employer": f"#{APEX}"}, {"verify_employer"})

# ================================================================== legacy employer onboarding via setup code
q("INSERT INTO beta_employers (company_name, admin_email) VALUES ('Legacy Co', 'Old@Legacy.com');")
LEGACY_EMP = int(q("SELECT id FROM beta_employers WHERE company_name = 'Legacy Co';"))
new_session()
sign_in_employer("old@legacy.com", "anything-at-all")
c.ok("security", "employer_token" not in ui.session_state, "legacy employer without password cannot sign in")
run("Employer Portal", {"setup_code_email": "old@legacy.com", "setup_code_value": "guess", "setup_code_password": "legacy-pass-123",
                        "setup_code_password_confirm": "legacy-pass-123"}, {"Set Password & Sign In"})
c.ok("security", "employer_token" not in ui.session_state, "guessed setup code rejected")
new_session(ALICE)
run("Manager Console", {"Employer": f"#{LEGACY_EMP}"}, {"issue_setup_code"})
code = texts("code")[0]
c.ok("security", count("beta_employers", f"id = {LEGACY_EMP} AND setup_code_hash = encode(sha256('{code}'::bytea), 'hex') "
                                         "AND setup_code_expires_at > now() + interval '47 hours'") == 1,
     "setup code stored only as SHA-256, expires in 48h")
new_session()
run("Employer Portal", {"setup_code_email": " OLD@legacy.com", "setup_code_value": code, "setup_code_password": "legacy-pass-123",
                        "setup_code_password_confirm": "legacy-pass-123"}, {"Set Password & Sign In"})
run("Employer Portal")
c.ok("security", "employer_token" in ui.session_state and "job_requisition_form" not in ui.forms,
     "legacy employer can sign in via setup code but stays UNVERIFIED (no posting) until a manager verifies")
new_session()
run("Employer Portal", {"setup_code_email": "old@legacy.com", "setup_code_value": code, "setup_code_password": "another-pass-456",
                        "setup_code_password_confirm": "another-pass-456"}, {"Set Password & Sign In"})
c.ok("security", "employer_token" not in ui.session_state, "setup code cannot be reused")

# ================================================================== lockout
new_session()
for _ in range(5):
    sign_in_employer("nobody@x.com", "whatever-pass")
run("Employer Portal")
c.ok("security", "employer_sign_in_form" not in ui.forms and any("Too many" in t for t in texts("error")), "5 failed sign-ins lock the session out")

# ================================================================== candidates: privacy, geocoding, XSS
new_session()
run("Employee Portal", candidate_form(city="Nowhereville"), {LOCK})
c.ok("data", count("candidate_profiles", "contact_email = 'jane@x.com'") == 0 and any("Priceville, AL 35603" in t for t in texts("warning")),
     "ungeocodable candidate location: NOT saved, format guidance shown")
run("Employee Portal", candidate_form(city="SERVICEDOWN"), {LOCK})
c.ok("data", count("candidate_profiles", "contact_email = 'jane@x.com'") == 0 and any("ICJ-ERR-503" in t for t in texts("warning")),
     "geocoder outage: candidate NOT saved, ICJ-ERR-503")
run("Employee Portal", candidate_form(name="<script>alert(1)</script>Jane"), {LOCK})
row = q("SELECT id || '|' || application_status FROM candidate_profiles WHERE contact_email = 'jane@x.com';")
JANE = int(row.split("|")[0])
c.ok("processing", row.endswith("|PENDING"), "candidate saved with normalized contact_email and PENDING status")
c.ok("security", any("&lt;script&gt;" in t for t in texts("markdown")) and not any("<script>" in t for t in texts("markdown")), "candidate input HTML-escaped")
points = [p["name"] for p in json.loads(texts("map")[0])]
c.ok("data", points == ["Candidate: <script>alert(1)</script>Jane", "Job: Python Developer"],
     f"map: own profile + verified in-radius jobs only; legacy fallback job and Austin job excluded {points}")
new_session()
run("Employee Portal", candidate_form(name="Bob", email="bob@x.com", city="Decatur", resume="forklift warehouse"), {LOCK})
c.ok("security", all("Jane" not in p["name"] for p in json.loads(texts("map")[0])), "second visitor never sees the first visitor")
new_session()
run("Employee Portal")
c.ok("security", not texts("map") and not any("FROM candidate_profiles" in s for s in sql_log()), "fresh session: no candidate data read")
run("Employee Portal", candidate_form(name="Jane again", email="JANE@x.com"), {LOCK})
c.ok("data", count("candidate_profiles", "lower(contact_email) = 'jane@x.com'") == 1 and ui.session_state.get("last_ingested_candidate") is None,
     "duplicate candidate email blocked; no access to the existing profile")

# ================================================================== live matching vs simulation; legacy fallback isolation
q("UPDATE candidate_profiles SET contact_email = 'legacy@x.com' WHERE name = 'Legacy Fallback Candidate';")  # make it otherwise eligible
LEGACY_JOB = int(q("SELECT id FROM employer_jobs WHERE title = 'Legacy Fallback Job';"))
q(f"UPDATE employer_jobs SET employer_id = {APEX}, requirements = 'python sql docker' WHERE id = {LEGACY_JOB};")  # even if attributed
new_session(ALICE)
run("Manager Console", {"Job to match": f"#{APEX_JOB}"}, {"run_live_matching"})
EARLY = int(q("SELECT id FROM candidate_profiles WHERE contact_email = 'early@x.com';"))
logged = q("SELECT string_agg(candidate_profile_id || '|' || job_id || '|' || employer_email || '|' || status || '|' || (payload->>'delivery_status'), ' ; ' ORDER BY candidate_profile_id) FROM semantic_matches;")
expected = " ; ".join(f"{cid}|{APEX_JOB}|hr@apex.com|QUEUED_PENDING_GATEWAY|NOT_SENT_GATEWAY_PENDING" for cid in sorted([JANE, EARLY]))
c.ok("processing", logged == expected,
     f"live matching uses the job's structured data and the employer's DB email (Jane + Early Bird matched, Bob gated): {logged}", debug)
MATCHED = count("semantic_matches")
evaluated = [r["Candidate"] for r in json.loads(texts("dataframe")[0])]
c.ok("data", "Legacy Fallback Candidate" not in evaluated, "legacy fallback-point candidate excluded from live matching (record kept)")
run("Manager Console", {"Job to match": f"#{LEGACY_JOB}"}, {"run_live_matching"})
c.ok("data", any("fallback location" in t for t in texts("warning")) and count("semantic_matches") == MATCHED, "legacy fallback-point job excluded from live matching")
run("Manager Console", {"Job to match": f"#{APEX_JOB}"}, {"run_live_matching"})
c.ok("data", count("semantic_matches") == MATCHED and any("already in the ledger" in t for t in texts("caption")), "re-running does not duplicate ledger rows")
run("Manager Console", clicks={"run_simulation"})
c.ok("data", count("semantic_matches") == MATCHED and texts("dataframe"), "simulation sandbox writes nothing")
c.ok("data", count("candidate_profiles", "latitude = 34.5262 AND longitude = -86.9556") == 1 and count("employer_jobs", "latitude = 34.5262 AND longitude = -86.9556") == 1,
     "historical fallback records still exist (not deleted)")
c.ok("processing", "securely sent" not in " ".join(texts()), "no 'securely sent' claims")

# ================================================================== system checks
run("Manager Console")
page = " ".join(texts())
c.ok("processing", "Migration complete: database-level email uniqueness verified active" in page, "System Checks: uniqueness verified active")
c.ok("processing", "**1** candidate profile(s), **1** job(s)" in page, "System Checks: reports the excluded historical fallback records")
deps = json.loads([t for t in texts("dataframe") if "Pinned" in t][0])
c.ok("processing", {d["Package"] for d in deps} == {"streamlit", "pandas", "psycopg2-binary", "geopy", "openpyxl"}, "System Checks lists all 5 pinned packages")
print("     installed in this test environment:", {d["Package"]: d["Installed"] for d in deps})
print("     telemetry rows:", q("SELECT string_agg(diagnostic_error_code || ' ' || error_context_vector, '; ' ORDER BY id) FROM system_telemetry_logs;"))

c.summary()
