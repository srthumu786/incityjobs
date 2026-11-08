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

c.summary()
