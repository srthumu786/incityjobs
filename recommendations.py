"""Candidate consent, privacy audit and employer recommendations for InCityJobs.

No Streamlit imports: every function takes a DB cursor, so the same code serves
the app, the Manager Console and the tests. See docs/EMPLOYER_RECOMMENDATIONS_PLAN.md.
"""
import json

# --------------------------------------------------------------------------- consent
CONSENT_POLICY_VERSION = "employer-matching-v1-2026-11"
CONSENT_LABEL = (
    "I agree that InCityJobs may show an anonymized version of my profile to verified employers whose "
    "job openings match me. Employers see only a reference code, matched qualifications, a match-strength "
    "band and a distance band: never my name, contact details, address or resume text."
)
CONSENT_FOOTNOTE = (
    "Optional. Your contact details are never shared without your separate permission. "
    "You can withdraw this consent at any time. Policy version: " + CONSENT_POLICY_VERSION
)

ACTOR_TYPES = ("system", "candidate", "employer", "manager")


def record_event(cur, event_type, actor_type, actor_ref=None, *, candidate_id=None, employer_id=None,
                 job_id=None, recommendation_id=None, detail=None):
    """Append one row to privacy_audit_log (the table rejects updates and deletes).
    `detail` must not contain personal data: ids, statuses, versions and reasons only."""
    if actor_type not in ACTOR_TYPES:
        raise ValueError(f"unknown actor type {actor_type!r}")
    cur.execute("""
        INSERT INTO privacy_audit_log (event_type, actor_type, actor_ref, candidate_profile_id,
                                       employer_id, job_id, recommendation_id, detail)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb);
    """, (event_type, actor_type, actor_ref, candidate_id, employer_id, job_id, recommendation_id,
          json.dumps(detail or {}, sort_keys=True)))


def set_matching_consent(cur, candidate_id, granted, actor_type, actor_ref=None) -> bool:
    """Grant or withdraw a candidate's employer-matching consent and audit the change.
    Returns True if the consent state changed. Raises LookupError for an unknown candidate."""
    cur.execute("SELECT employer_matching_consent FROM candidate_profiles WHERE id = %s FOR UPDATE;", (candidate_id,))
    row = cur.fetchone()
    if row is None:
        raise LookupError(f"candidate {candidate_id} not found")
    if bool(row[0]) == bool(granted):
        return False
    if granted:
        cur.execute("""
            UPDATE candidate_profiles
            SET employer_matching_consent = TRUE, employer_matching_consent_at = now(),
                employer_matching_consent_policy = %s
            WHERE id = %s;
        """, (CONSENT_POLICY_VERSION, candidate_id))
    else:
        cur.execute("""
            UPDATE candidate_profiles
            SET employer_matching_consent = FALSE, employer_matching_consent_at = now()
            WHERE id = %s;
        """, (candidate_id,))
    record_event(cur, "CONSENT_GRANTED" if granted else "CONSENT_WITHDRAWN", actor_type, actor_ref,
                 candidate_id=candidate_id, detail={"policy_version": CONSENT_POLICY_VERSION})
    return True


def consent_state(cur, candidate_id):
    """(consented: bool, changed_at, policy_version) or None if the candidate doesn't exist."""
    cur.execute("""
        SELECT employer_matching_consent, employer_matching_consent_at, employer_matching_consent_policy
        FROM candidate_profiles WHERE id = %s;
    """, (candidate_id,))
    row = cur.fetchone()
    return (bool(row[0]), row[1], row[2]) if row else None


# =========================================================================== recommendations
import hashlib
import secrets as _secrets
from dataclasses import dataclass

import db_schema
from matching import CandidateRecord, JobCriteria, Phase4SemanticEngine, evaluate_match

VISIBLE_STATUSES = ("AVAILABLE", "VIEWED")
MAX_RECOMMENDATIONS_PER_JOB = 20
CANDIDATE_POOL_LIMIT = 500
PROFILE_SCHEMA_VERSION = 1
# The ONLY keys an employer-facing profile may contain (allow-list).
PROFILE_KEYS = ("job_title", "qualifications", "score_band", "distance_band", "profile_schema")
_QUALIFICATION_VOCABULARY = frozenset(Phase4SemanticEngine().skill_keywords)

REASON_CONSENT = "awaiting_candidate_consent"
REASON_EMPLOYER = "employer_unverified"
REASON_NO_MATCH = "no_longer_matches"


def score_band(score) -> str:
    for floor, label in ((85, "85+"), (70, "70-84"), (50, "50-69")):
        if score >= floor:
            return label
    return "below 50"


def distance_band(miles) -> str:
    for ceiling, label in ((5, "under 5 mi"), (10, "5-10 mi"), (25, "10-25 mi"), (50, "25-50 mi")):
        if miles < ceiling:
            return label
    return "50+ mi"


def anonymized_profile(job_title: str, result) -> dict:
    """Employer-facing profile built ONLY from allow-listed, non-identifying facets:
    the employer's own job title, qualifications from the fixed matching
    dictionary, a score band and a distance band. No free-text candidate field
    (name, role, resume, address) is ever copied, because free text can carry
    indirect identifiers."""
    return {
        "job_title": job_title,
        "qualifications": sorted(q for q in result.matched_skills if q in _QUALIFICATION_VOCABULARY),
        "score_band": score_band(result.semantic_percentage),
        "distance_band": distance_band(result.distance_miles if result.distance_miles is not None else 999),
        "profile_schema": PROFILE_SCHEMA_VERSION,
    }


def profile_fingerprint(profile: dict, resume_version: int) -> str:
    """Fingerprint of exactly what the employer would see, plus the resume version
    (so a resume update re-surfaces once). Unchanged content -> same fingerprint."""
    canonical = json.dumps({"profile": profile, "resume_version": int(resume_version or 1)}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Transition:
    status: str
    set_new: bool               # raise the "new" badge
    notified_fingerprint: object
    event: object               # audit event type, or None if nothing visible changed


def plan_transition(current, *, matches, eligible, fingerprint, notified_fingerprint, viewed_fingerprint):
    """Pure status rules (see the plan). `current` is None for a pair with no row.
    Returns a Transition, or None when nothing should be created."""
    if current is None and not matches:
        return None
    if matches and eligible:
        if fingerprint != notified_fingerprint:  # new or changed content: notify exactly once
            event = "RECOMMENDATION_REFRESHED" if current in VISIBLE_STATUSES else "RECOMMENDATION_AVAILABLE"
            return Transition("AVAILABLE", True, fingerprint, event)
        if current in VISIBLE_STATUSES:
            return Transition(current, False, notified_fingerprint, None)
        restored = "VIEWED" if viewed_fingerprint == fingerprint else "AVAILABLE"
        return Transition(restored, False, notified_fingerprint, "RECOMMENDATION_RESTORED")
    if matches and current in (None, "QUEUED"):
        return Transition("QUEUED", False, notified_fingerprint, None if current == "QUEUED" else "RECOMMENDATION_QUEUED")
    if current == "WITHDRAWN":
        return Transition("WITHDRAWN", False, notified_fingerprint, None)
    return Transition("WITHDRAWN", False, notified_fingerprint, "RECOMMENDATION_WITHDRAWN")


def candidate_locations(latitude, longitude):
    """All target locations of a candidate. Today a single one; the Candidate Module's
    multi-state Job Location Preferences will return several."""
    return [(latitude, longitude)]


def _best_match(job: JobCriteria, row):
    """Evaluate a candidate against a job over all their target locations; return the
    best passing result, or the best failing one."""
    best = None
    for lat, lon in candidate_locations(row["latitude"], row["longitude"]):
        record = CandidateRecord(row["id"], row["name"], row["contact_email"], row["role"], row["resume"], lat, lon)
        result = evaluate_match(job, record)
        if best is None or (result.passed, result.semantic_percentage) > (best.passed, best.semantic_percentage):
            best = result
    return best


_CANDIDATE_SQL = f"""
    SELECT c.id, c.name, c.contact_email, c.role, c.resume, c.latitude, c.longitude,
           c.employer_matching_consent, c.resume_version
    FROM candidate_profiles c
    WHERE c.contact_email IS NOT NULL AND btrim(c.contact_email) <> ''
      AND c.application_status <> 'REJECTED'
      AND c.latitude IS NOT NULL AND c.longitude IS NOT NULL
      AND {db_schema.trusted_location_sql('c')}
"""
_CANDIDATE_FIELDS = ("id", "name", "contact_email", "role", "resume", "latitude", "longitude", "consent", "resume_version")


def _candidate_rows(cur, candidate_id=None) -> dict:
    if candidate_id is None:
        cur.execute(_CANDIDATE_SQL + " ORDER BY c.id DESC LIMIT %s;", (CANDIDATE_POOL_LIMIT,))
    else:
        cur.execute(_CANDIDATE_SQL + " AND c.id = %s;", (candidate_id,))
    return {row[0]: dict(zip(_CANDIDATE_FIELDS, row)) for row in cur.fetchall()}


def _load_job(cur, job_id):
    cur.execute("""
        SELECT j.id, j.title, j.department, j.requirements, j.latitude, j.longitude, j.match_radius_miles,
               j.employer_id, e.verified_at IS NOT NULL, e.admin_email, e.company_name
        FROM employer_jobs j LEFT JOIN beta_employers e ON e.id = j.employer_id
        WHERE j.id = %s;
    """, (job_id,))
    row = cur.fetchone()
    if row is None or row[7] is None:  # unknown job, or legacy job without an employer account
        return None
    criteria = JobCriteria(job_id=row[0], title=row[1] or "", department=row[2] or "", requirements=row[3] or "",
                           latitude=row[4], longitude=row[5], radius_miles=float(row[6] or 25),
                           employer_id=row[7], employer_email=row[9], company_name=row[10])
    return {"criteria": criteria, "employer_id": row[7], "employer_verified": bool(row[8])}


def _fresh_eligibility(cur, candidate_id, employer_id):
    """Consent and verification as committed RIGHT NOW. Read after the recommendation
    row is locked, so a concurrent consent withdrawal or verification revocation is
    never overwritten by a batch that started earlier."""
    cur.execute("""
        SELECT (SELECT employer_matching_consent FROM candidate_profiles WHERE id = %s),
               (SELECT verified_at IS NOT NULL FROM beta_employers WHERE id = %s);
    """, (candidate_id, employer_id))
    consent, verified = cur.fetchone()
    return bool(consent), bool(verified)


def _decide(matches, consent, verified):
    eligible = matches and consent and verified
    if not matches:
        return eligible, REASON_NO_MATCH
    if not verified:
        return eligible, REASON_EMPLOYER
    if not consent:
        return eligible, REASON_CONSENT
    return eligible, None


def _apply(cur, job, candidate_id, row, counts, actor_type, actor_ref):
    """Create or update the single recommendation for (job, candidate_id), under a row lock."""
    criteria = job["criteria"]
    result = _best_match(criteria, row) if row is not None else None
    matches = bool(result and result.passed)
    eligible, reason = _decide(matches, bool(row and row["consent"]), job["employer_verified"])
    profile = anonymized_profile(criteria.title, result) if matches else None
    fingerprint = profile_fingerprint(profile, row["resume_version"]) if matches else None

    def lock_existing():
        cur.execute("""
            SELECT id, status, fingerprint, notified_fingerprint, viewed_fingerprint
            FROM employer_recommendations WHERE job_id = %s AND candidate_profile_id = %s FOR UPDATE;
        """, (criteria.job_id, candidate_id))
        return cur.fetchone()

    existing = lock_existing()
    if existing is None:
        plan = plan_transition(None, matches=matches, eligible=eligible, fingerprint=fingerprint,
                               notified_fingerprint=None, viewed_fingerprint=None)
        if plan is None:
            return
        cur.execute("""
            INSERT INTO employer_recommendations
                (job_id, employer_id, candidate_profile_id, status, status_reason, employer_ref, profile, fingerprint,
                 notified_fingerprint, is_new, score, distance_miles, available_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s,
                    CASE WHEN %s = 'AVAILABLE' THEN now() END)
            ON CONFLICT (job_id, candidate_profile_id) DO NOTHING
            RETURNING id;
        """, (criteria.job_id, job["employer_id"], candidate_id, plan.status, reason,
              "C-" + _secrets.token_hex(5).upper(), json.dumps(profile, sort_keys=True), fingerprint,
              plan.notified_fingerprint, plan.set_new, result.semantic_percentage, result.distance_miles, plan.status))
        created = cur.fetchone()
        if created:
            record_event(cur, plan.event, actor_type, actor_ref, candidate_id=candidate_id, employer_id=job["employer_id"],
                         job_id=criteria.job_id, recommendation_id=created[0], detail={"status": plan.status, "reason": reason})
            counts[plan.status] = counts.get(plan.status, 0) + 1
            return
        existing = lock_existing()  # a concurrent run inserted it first: continue as an update

    rec_id, current, stored_fp, notified_fp, viewed_fp = existing
    eligible, reason = _decide(matches, *_fresh_eligibility(cur, candidate_id, job["employer_id"]))
    plan = plan_transition(current, matches=matches, eligible=eligible, fingerprint=fingerprint if matches else stored_fp,
                           notified_fingerprint=notified_fp, viewed_fingerprint=viewed_fp)
    cur.execute("""
        UPDATE employer_recommendations SET
            status = %s,
            status_reason = %s,
            profile = CASE WHEN %s THEN %s::jsonb ELSE profile END,
            fingerprint = CASE WHEN %s THEN %s ELSE fingerprint END,
            score = CASE WHEN %s THEN %s ELSE score END,
            distance_miles = CASE WHEN %s THEN %s ELSE distance_miles END,
            notified_fingerprint = %s,
            is_new = CASE WHEN %s THEN TRUE ELSE is_new END,
            available_at = CASE WHEN %s = 'AVAILABLE' AND (status <> 'AVAILABLE' OR %s) THEN now() ELSE available_at END,
            withdrawn_at = CASE WHEN %s = 'WITHDRAWN' AND status <> 'WITHDRAWN' THEN now() ELSE withdrawn_at END,
            updated_at = CASE WHEN %s THEN now() ELSE updated_at END
        WHERE id = %s;
    """, (plan.status, reason,
          matches, json.dumps(profile, sort_keys=True) if matches else None,
          matches, fingerprint,
          matches, result.semantic_percentage if matches else None,
          matches, result.distance_miles if matches else None,
          plan.notified_fingerprint, plan.set_new,
          plan.status, plan.set_new,
          plan.status,
          plan.event is not None or plan.status != current, rec_id))
    if plan.event:
        record_event(cur, plan.event, actor_type, actor_ref, candidate_id=candidate_id, employer_id=job["employer_id"],
                     job_id=criteria.job_id, recommendation_id=rec_id,
                     detail={"from": current, "status": plan.status, "reason": reason})
        counts[plan.status] = counts.get(plan.status, 0) + 1


def generate_for_job(cur, job_id, actor_type="system", actor_ref=None) -> dict:
    """(Re)compute every recommendation of one job. Rows are processed in
    candidate-id order (global order: job, candidate), so concurrent runs can't deadlock."""
    counts = {}
    job = _load_job(cur, job_id)
    if job is None:
        return counts
    candidates = _candidate_rows(cur)
    cur.execute("SELECT candidate_profile_id FROM employer_recommendations WHERE job_id = %s;", (job_id,))
    existing = {r[0] for r in cur.fetchall()}
    for candidate_id in sorted(set(candidates) | existing):
        row = candidates.get(candidate_id)
        if row is None and candidate_id in existing:
            row = _candidate_rows(cur, candidate_id).get(candidate_id)  # outside the pool cap, or no longer eligible
        _apply(cur, job, candidate_id, row, counts, actor_type, actor_ref)
    return counts


def generate_for_candidate(cur, candidate_id, actor_type="system", actor_ref=None) -> dict:
    """(Re)compute every recommendation of one candidate across all employer jobs,
    in job-id order (same global order as generate_for_job)."""
    counts = {}
    row = _candidate_rows(cur, candidate_id).get(candidate_id)
    cur.execute("SELECT id FROM employer_jobs WHERE employer_id IS NOT NULL ORDER BY id;")
    job_ids = [r[0] for r in cur.fetchall()]
    for job_id in job_ids:
        job = _load_job(cur, job_id)
        if job is not None:
            _apply(cur, job, candidate_id, row, counts, actor_type, actor_ref)
    return counts


def reconcile_employer(cur, employer_id, actor_type="system", actor_ref=None) -> dict:
    """After a verification change: recompute all of the employer's jobs (ascending)."""
    counts = {}
    cur.execute("SELECT id FROM employer_jobs WHERE employer_id = %s ORDER BY id;", (employer_id,))
    for (job_id,) in cur.fetchall():
        for status, n in generate_for_job(cur, job_id, actor_type, actor_ref).items():
            counts[status] = counts.get(status, 0) + n
    return counts


# --------------------------------------------------------------------------- employer read side
# Every employer read joins LIVE eligibility, so a stale status can never expose a
# profile: own job, verified employer, consenting candidate, visible status.
_ELIGIBLE_FROM = f"""
    FROM employer_recommendations r
    JOIN employer_jobs j ON j.id = r.job_id AND j.employer_id = r.employer_id
    JOIN beta_employers e ON e.id = r.employer_id AND e.verified_at IS NOT NULL
    JOIN candidate_profiles c ON c.id = r.candidate_profile_id AND c.employer_matching_consent
    WHERE r.employer_id = %s
      AND r.status IN ('AVAILABLE', 'VIEWED')
      AND {db_schema.trusted_location_sql('j')}
"""


def employer_recommendations(cur, employer_id) -> list:
    """Ranked recommendations for ONE employer's own jobs (max 20 per job).
    Returns only opaque refs, the employer's job id/title and the anonymized profile."""
    cur.execute(f"""
        SELECT employer_ref, job_id, job_title, status, is_new, profile FROM (
            SELECT r.employer_ref, r.job_id, j.title AS job_title, r.status, r.is_new, r.profile,
                   row_number() OVER (PARTITION BY r.job_id ORDER BY r.score DESC, r.id) AS rank
            {_ELIGIBLE_FROM}
        ) ranked
        WHERE rank <= %s
        ORDER BY job_id DESC, rank;
    """, (employer_id, MAX_RECOMMENDATIONS_PER_JOB))
    rows = []
    for ref, job_id, title, status, is_new, profile in cur.fetchall():
        profile = profile if isinstance(profile, dict) else json.loads(profile)
        rows.append({"ref": ref, "job_id": job_id, "job_title": title, "status": status, "is_new": bool(is_new),
                     "profile": {k: profile[k] for k in PROFILE_KEYS if k in profile}})
    return rows


def open_recommendation(cur, employer_id, employer_ref, actor_ref=None):
    """Open one anonymized profile for an employer. Ownership and eligibility are
    re-checked here; anything else returns None and audits the denied attempt."""
    cur.execute(f"""
        SELECT r.id, r.status, r.fingerprint, r.profile, r.job_id, r.candidate_profile_id
        {_ELIGIBLE_FROM} AND r.employer_ref = %s
        FOR UPDATE OF r;
    """, (employer_id, str(employer_ref or "")[:16]))
    row = cur.fetchone()
    if row is None:
        record_event(cur, "PROFILE_ACCESS_DENIED", "employer", actor_ref, employer_id=employer_id,
                     detail={"requested_ref": str(employer_ref or "")[:16]})
        return None
    rec_id, status, fingerprint, profile, job_id, candidate_id = row
    first_view = status == "AVAILABLE"
    cur.execute("""
        UPDATE employer_recommendations
        SET status = 'VIEWED', is_new = FALSE, viewed_fingerprint = %s,
            viewed_at = now(), updated_at = now()
        WHERE id = %s;
    """, (fingerprint, rec_id))
    record_event(cur, "PROFILE_VIEWED", "employer", actor_ref, candidate_id=candidate_id, employer_id=employer_id,
                 job_id=job_id, recommendation_id=rec_id, detail={"first_view": first_view})
    profile = profile if isinstance(profile, dict) else json.loads(profile)
    return {k: profile[k] for k in PROFILE_KEYS if k in profile}
