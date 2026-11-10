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
