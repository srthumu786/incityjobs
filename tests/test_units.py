"""Unit tests for auth.py and matching.py (no database needed)."""
import json
import math
import time

from _common import Checks
import auth
from matching import (CandidateRecord, JobCriteria, LEGACY_FALLBACK_COORDS, build_push_payload,
                      evaluate_match, is_legacy_fallback)

c = Checks("unit tests")
KEY = b"k" * 32


def haversine(a, b):
    r = 3958.8
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


# ------------------------------------------------------------------ passwords
h1 = auth.hash_password("correct horse battery")
h2 = auth.hash_password("correct horse battery")
c.ok("security", h1.startswith("pbkdf2_sha256$600000$") and h1 != h2, "PBKDF2-SHA256, 600k iterations, random salt per hash")
c.ok("security", auth.verify_password("correct horse battery", h1), "correct password verifies")
c.ok("security", not auth.verify_password("correct horse batterY", h1), "wrong password fails")
c.ok("security", not any(auth.verify_password("x", bad) for bad in ["", "garbage", "md5$1$a$b", None]), "malformed/foreign hashes fail closed")
c.ok("security", auth.password_problem("short", "short") and auth.password_problem("long-enough-pw", "different-pw")
     and not auth.password_problem("long-enough-pw", "long-enough-pw"), "password policy: >= 12 chars and confirmation must match")

# ------------------------------------------------------------------ tokens
tok = auth.issue_token(KEY, 7, auth.ROLE_EMPLOYER, 60, extra={"cred": "abc"})
claims = auth.verify_token(KEY, tok, auth.ROLE_EMPLOYER)
c.ok("security", claims and claims["sub"] == "7" and claims["cred"] == "abc", "token round-trip keeps subject and credential claim")
body, sig = tok.split(".")
forged_body = auth._b64e(json.dumps({**claims, "sub": "8"}).encode())
c.ok("security", auth.verify_token(KEY, f"{forged_body}.{sig}", auth.ROLE_EMPLOYER) is None, "edited claims with old signature rejected")
c.ok("security", auth.verify_token(b"z" * 32, tok, auth.ROLE_EMPLOYER) is None, "token signed with another key rejected")
c.ok("security", auth.verify_token(KEY, tok, auth.ROLE_MANAGER) is None, "employer token rejected for manager role")
c.ok("security", auth.verify_token(KEY, auth.issue_token(KEY, 7, auth.ROLE_EMPLOYER, -1), auth.ROLE_EMPLOYER) is None, "expired token rejected")
c.ok("security", all(auth.verify_token(KEY, t, auth.ROLE_EMPLOYER) is None for t in [None, "", "a.b.c", "nodot", 42]), "malformed tokens rejected")

# ------------------------------------------------------------------ credential fingerprints (session revocation)
f1, f2 = auth.credential_fingerprint(KEY, h1), auth.credential_fingerprint(KEY, h2)
c.ok("security", f1 != f2 and f1 == auth.credential_fingerprint(KEY, h1), "re-hashing (even the same password) changes the fingerprint")
c.ok("security", auth.credential_fingerprint(b"other" * 8, h1) != f1 and h1 not in f1, "fingerprint is keyed and does not expose the hash")

# ------------------------------------------------------------------ setup codes
code = auth.new_setup_code()
c.ok("security", len(code) >= 12 and auth.hash_setup_code(code) == auth.hash_setup_code(f"  {code} ") and auth.hash_setup_code(code) != code,
     "setup codes are random, stored hashed, whitespace-tolerant")

# ------------------------------------------------------------------ matching rules
job = JobCriteria(job_id=1, title="Python Developer", requirements="python sql docker microservices",
                  latitude=34.5251, longitude=-86.8947, radius_miles=25, employer_id=3, employer_email="hr@apex.com", company_name="Apex")
def cand(**kw):
    base = dict(profile_id=10, name="Jane", contact_email="jane@x.com", role="Dev",
                resume="python developer sql docker microservices", latitude=34.60, longitude=-86.98)
    base.update(kw)
    return CandidateRecord(**base)
def ev(candidate, j=job):
    return evaluate_match(j, candidate, distance_fn=haversine)

r = ev(cand())
c.ok("processing", r.passed and r.distance_miles < 25 and r.semantic_percentage >= 50, f"matching candidate passes ({r.semantic_percentage}%, {r.distance_miles} mi)")
c.ok("processing", not ev(cand(latitude=30.27, longitude=-97.74)).passed, "candidate outside the job's radius is gated")
c.ok("processing", not ev(cand(resume="forklift warehouse")).passed, "low semantic score is gated")
c.ok("processing", not ev(cand(contact_email=" ")).passed, "missing contact email is gated")
c.ok("processing", not ev(cand(latitude=None)).passed, "unknown candidate location is gated (no longer treated as distance 0)")
c.ok("data", is_legacy_fallback(*LEGACY_FALLBACK_COORDS) and not is_legacy_fallback(34.5251, -86.8947), "legacy fallback point detected exactly")
c.ok("data", "fallback" in ev(cand(latitude=LEGACY_FALLBACK_COORDS[0], longitude=LEGACY_FALLBACK_COORDS[1])).reason, "candidate at legacy fallback point is never matched")
fallback_job = JobCriteria(**{**job.__dict__, "latitude": LEGACY_FALLBACK_COORDS[0], "longitude": LEGACY_FALLBACK_COORDS[1]})
c.ok("data", not ev(cand(), fallback_job).passed, "job at legacy fallback point is never matched")
p = build_push_payload(job, r, "2026-11-08T00:00:00+00:00")
c.ok("processing", p["delivery_status"] == "NOT_SENT_GATEWAY_PENDING" and p["job"]["job_id"] == 1 and p["employer"]["contact_email"] == "hr@apex.com",
     "push payload carries job, employer and pending (unsent) delivery status")

# ------------------------------------------------------------------ diagnostics: codes and redaction
import io
import logging
from diagnostics import (DIAGNOSTIC_CODES, DatabaseConnectionError, RedactingFilter, SchemaMigrationError,
                         StartupDiagnosticError, redact)

c.ok("security", DatabaseConnectionError.code == "ICJ-ERR-500" and SchemaMigrationError.code == "ICJ-ERR-511"
     and "duplicate-check query" in DIAGNOSTIC_CODES["ICJ-ERR-504"] and "UNIQUE" in DIAGNOSTIC_CODES["ICJ-ERR-518"]
     and issubclass(DatabaseConnectionError, StartupDiagnosticError) and issubclass(SchemaMigrationError, StartupDiagnosticError),
     "codes: 500 = connection init, 511 = schema migration, 504 = duplicate-check query, 518 = uniqueness not verified")
c.ok("security", all("Traceback" not in e.user_message and "ICJ" not in e.user_message and "try again" in e.user_message.lower()
                     and "contact" in e.user_message.lower() for e in (DatabaseConnectionError, SchemaMigrationError)),
     "startup user messages: plain explanation + next action, no technical detail")

SECRET_PW = "Npg-S3cretPw-XYZ"
SECRET_DSN = f"postgresql://neondb_owner:{SECRET_PW}@ep-demo-pooler.c-2.us-east-2.aws.neon.tech/neondb?sslmode=require"
libpq_echo = f'invalid dsn: missing "=" after "{SECRET_DSN}" in connection info string'
c.ok("security", SECRET_PW not in redact(libpq_echo) and "neondb_owner:***@" in redact(libpq_echo),
     "redact: password inside a postgresql:// URI (as libpq echoes a malformed DSN)")
c.ok("security", redact("host=h user=u password=hunter2-Secret dbname=d") == "host=h user=u password=*** dbname=d"
     and redact("PGPASSWORD='quoted secret value'") == "PGPASSWORD=***", "redact: password= / PGPASSWORD= values (plain and quoted)")
c.ok("security", redact("connection failed for token abcdefSECRETVALUE", secrets=("abcdefSECRETVALUE",)) == "connection failed for token ***",
     "redact: exact configured secret values")
c.ok("security", redact('DETAIL: Key (lower(btrim(admin_email)))=(hr@apex.com) already exists.') == 'DETAIL: Key (lower(btrim(admin_email)))=(<email>) already exists.',
     "redact: email addresses (personal information) in database error details")
c.ok("security", "<password-hash>" in redact(f"stored {auth.hash_password('x' * 12, iterations=1)}") and "$pbkdf2-sha256$600000$ab$cd" not in redact("$pbkdf2-sha256$600000$ab$cd"),
     "redact: password hashes (both formats)")

buffer = io.StringIO()
test_logger = logging.getLogger("incityjobs.unit-redaction")
test_logger.propagate = False
handler = logging.StreamHandler(buffer); handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
test_logger.addHandler(handler)
test_logger.addFilter(RedactingFilter(lambda: (SECRET_DSN, SECRET_PW)))
try:
    try:
        raise ValueError(f"driver said: could not connect using {SECRET_DSN} for jane@x.com")
    except ValueError as inner:
        raise DatabaseConnectionError("could not open the database connection pool (ValueError)") from inner
except DatabaseConnectionError as exc:
    test_logger.exception("%s [%s] %s", "ICJ-ERR-500", "unit", f"cause: {exc.__cause__}")
logged = buffer.getvalue()
c.ok("security", logged.startswith("ERROR ICJ-ERR-500") and "Traceback (most recent call last)" in logged
     and "The above exception was the direct cause" in logged,
     "logger.exception output keeps the full traceback, including the chained cause")
c.ok("security", SECRET_PW not in logged and "jane@x.com" not in logged and "***" in logged and "<email>" in logged,
     "log message AND traceback are redacted (password, connection string, email)")

# ------------------------------------------------------------------ recommendations: pure rules
import recommendations as recs
from matching import MatchResult

def T(current, matches=True, eligible=True, fp="B", notified="A", viewed=None):
    t = recs.plan_transition(current, matches=matches, eligible=eligible, fingerprint=fp,
                             notified_fingerprint=notified, viewed_fingerprint=viewed)
    return None if t is None else (t.status, t.set_new, t.event)

cases = [
    ("no row, no match -> nothing created",                 T(None, matches=False),                         None),
    ("no row, match, not eligible -> QUEUED",               T(None, eligible=False, notified=None),        ("QUEUED", False, "RECOMMENDATION_QUEUED")),
    ("no row, eligible -> AVAILABLE + badge",               T(None, notified=None),                        ("AVAILABLE", True, "RECOMMENDATION_AVAILABLE")),
    ("QUEUED, consent granted -> AVAILABLE + badge",        T("QUEUED", notified=None),                    ("AVAILABLE", True, "RECOMMENDATION_AVAILABLE")),
    ("QUEUED, still not eligible -> no event",              T("QUEUED", eligible=False),                   ("QUEUED", False, None)),
    ("QUEUED, no longer matches -> WITHDRAWN",              T("QUEUED", matches=False, eligible=False),    ("WITHDRAWN", False, "RECOMMENDATION_WITHDRAWN")),
    ("AVAILABLE, unchanged -> no event, no badge",          T("AVAILABLE", fp="A", notified="A"),          ("AVAILABLE", False, None)),
    ("VIEWED, unchanged -> stays VIEWED",                   T("VIEWED", fp="A", notified="A", viewed="A"), ("VIEWED", False, None)),
    ("VIEWED, content changed -> AVAILABLE + badge once",   T("VIEWED", fp="B", notified="A", viewed="A"), ("AVAILABLE", True, "RECOMMENDATION_REFRESHED")),
    ("AVAILABLE, consent withdrawn -> WITHDRAWN",           T("AVAILABLE", eligible=False),                ("WITHDRAWN", False, "RECOMMENDATION_WITHDRAWN")),
    ("VIEWED, employer unverified -> WITHDRAWN",            T("VIEWED", eligible=False),                   ("WITHDRAWN", False, "RECOMMENDATION_WITHDRAWN")),
    ("WITHDRAWN, still ineligible -> no event",             T("WITHDRAWN", eligible=False),                ("WITHDRAWN", False, None)),
    ("WITHDRAWN -> restored, unchanged, was viewed -> VIEWED, no badge", T("WITHDRAWN", fp="A", notified="A", viewed="A"), ("VIEWED", False, "RECOMMENDATION_RESTORED")),
    ("WITHDRAWN -> restored, unchanged, never viewed -> AVAILABLE, no badge", T("WITHDRAWN", fp="A", notified="A"), ("AVAILABLE", False, "RECOMMENDATION_RESTORED")),
    ("WITHDRAWN -> restored with changed content -> badge once", T("WITHDRAWN", fp="B", notified="A"),    ("AVAILABLE", True, "RECOMMENDATION_AVAILABLE")),
]
bad = [(name, got, want) for name, got, want in cases if got != want]
c.ok("processing", not bad, f"recommendation status rules: {len(cases)} transitions as specified {bad}")
c.ok("security", all(s in ("QUEUED", "AVAILABLE", "VIEWED", "WITHDRAWN") for _, got, _ in cases if got for s in [got[0]]),
     "transitions never produce DELIVERED (reserved for a future messaging integration)")

result = MatchResult(candidate=None, passed=True, reason="", semantic_percentage=91.3, distance_miles=7.9,
                     matched_skills=["python", "sql", "Jane Doe", "Acme Corp"])
profile = recs.anonymized_profile("Python Developer", result)
c.ok("security", set(profile) == set(recs.PROFILE_KEYS) and profile["qualifications"] == ["python", "sql"],
     "anonymized profile has only allow-listed keys; qualifications limited to the fixed dictionary")
c.ok("security", profile["score_band"] == "85+" and profile["distance_band"] == "5-10 mi" and "91.3" not in json.dumps(profile) and "7.9" not in json.dumps(profile),
     "exact score and distance are never included, only bands")
c.ok("processing", [recs.score_band(x) for x in (49.9, 50, 69.99, 70, 84.9, 85)] == ["below 50", "50-69", "50-69", "70-84", "70-84", "85+"]
     and [recs.distance_band(x) for x in (0, 4.99, 5, 9.9, 10, 24.9, 25, 49.9, 50)] == ["under 5 mi", "under 5 mi", "5-10 mi", "5-10 mi", "10-25 mi", "10-25 mi", "25-50 mi", "25-50 mi", "50+ mi"],
     "score and distance band boundaries")
fp1 = recs.profile_fingerprint(profile, 1)
c.ok("processing", fp1 == recs.profile_fingerprint(dict(reversed(list(profile.items()))), 1) and fp1 != recs.profile_fingerprint(profile, 2)
     and fp1 != recs.profile_fingerprint({**profile, "score_band": "70-84"}, 1),
     "fingerprint: stable for identical content (key order irrelevant), changes with resume version or visible content")


# ------------------------------------------------------------------ matching engine: pinned behaviour (docs/MATCHING_ENGINE_ASSESSMENT.md)
import inspect
import matching
from matching_eval_set import evaluate

# Exact current results. If the engine is changed on purpose, update these AND the
# assessment document together; an accidental change fails here.
PINNED = {
    ("Software developer", "short (qualified)"): 74.16, ("Software developer", "realistic (qualified)"): 37.30, ("Software developer", "unqualified"): 0.00,
    ("Warehouse / forklift", "short (qualified)"): 73.99, ("Warehouse / forklift", "realistic (qualified)"): 18.98, ("Warehouse / forklift", "unqualified"): 3.01,
    ("CDL truck driver", "short (qualified)"): 72.11, ("CDL truck driver", "realistic (qualified)"): 18.23, ("CDL truck driver", "unqualified"): 5.55,
    ("Registered nurse", "short (qualified)"): 87.67, ("Registered nurse", "realistic (qualified)"): 21.06, ("Registered nurse", "unqualified"): 3.01,
    ("Retail cashier", "short (qualified)"): 74.85, ("Retail cashier", "realistic (qualified)"): 6.54, ("Retail cashier", "unqualified"): 0.00,
    ("Welder", "short (qualified)"): 78.77, ("Welder", "realistic (qualified)"): 22.42, ("Welder", "unqualified"): 0.00,
    ("Administrative assistant", "short (qualified)"): 80.90, ("Administrative assistant", "realistic (qualified)"): 12.40, ("Administrative assistant", "unqualified"): 8.73,
}
evaluation = evaluate()
drift = [(r["occupation"], r["resume"], r["score"], PINNED[(r["occupation"], r["resume"])]) for r in evaluation
         if abs(r["score"] - PINNED[(r["occupation"], r["resume"])]) > 0.005]
c.ok("matching", len(evaluation) == len(PINNED) and drift == [], f"matching engine unchanged: all 21 assessment scores identical {drift}")
by_kind = lambda k: [r for r in evaluation if r["resume"] == k]
c.ok("matching", sum(r["passes"] for r in by_kind("short (qualified)")) == 7 and sum(r["passes"] for r in by_kind("realistic (qualified)")) == 0
     and sum(r["passes"] for r in by_kind("unqualified")) == 0,
     "documented behaviour: short summaries 7/7 match, realistic qualified resumes 0/7 (length penalty), unqualified 0/7")
c.ok("matching", matching.MIN_SEMANTIC_SCORE == 50.0 and len(matching.Phase4SemanticEngine().skill_keywords) == 11
     and "aws" in matching.Phase4SemanticEngine().skill_keywords,
     "documented parameters unchanged: 50% threshold, 11-term tech dictionary")

c.summary()
