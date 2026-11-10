# Employer Module: Recommended Candidates (approved plan)

**Status:** approved 2026-11-10 with the decisions below. Development on `fix/db-pooling-tables` only.
Production is untouched, nothing is merged into `main`, and no external notifications are activated.

## Scope
**In scope:** employer-facing recommendations. That covers generating them, their statuses, anonymised profiles, employer isolation, candidate consent capture and revocation (the minimal shared parts), and the audit trail.

**Out of scope (Candidate Module):** resume upload and storage (PDF/DOCX), resume editing and polishing, candidate accounts, per-employer contact-disclosure approval screens, and multi-state location preference screens. The architecture below is prepared for these.

## Approved decisions

### 1. Consent
- **Matching consent is explicit and mandatory** before an anonymised profile can be shown to any employer.
- **Existing candidates stay QUEUED** until they consent. A previous resume submission is not authorization.
- **New registrations** get a clear opt-in checkbox, unticked by default. The consent timestamp and policy version are stored.
- **Consent is revocable.** In this phase the candidate can withdraw in their own session right after submitting, and a manager can record a withdrawal the candidate requested. The Candidate Module will add self-service later. Withdrawal blocks employer access immediately.
- **Contact information** (name, email, phone, address) needs a separate authorization. That is the column `contact_disclosure_consent`, which defaults to FALSE. No code path in this phase discloses contact information.

### 2. Recommendation statuses
| Status | Meaning |
|---|---|
| `QUEUED` | Identified as a match, but not eligible for employer display. For example: no candidate consent, or the employer isn't verified. |
| `AVAILABLE` | Eligible and visible on the employer's dashboard. |
| `VIEWED` | The employer opened the anonymised profile. |
| `WITHDRAWN` | No longer eligible for display: consent withdrawn, verification revoked, or no longer a match. |
| `DELIVERED` | **Reserved** for confirmed external notification delivery in a future messaging integration. Not used, and rejected by the database CHECK constraint. |

## Design

### Generation (existing matching engine preserved)
- **Triggers:**
  - a job is published
  - a candidate submits a profile
  - a candidate grants or withdraws consent
  - an employer is verified, or verification is revoked
  - a manager runs matching (still available).
- **Matching:** `matching.evaluate_match` is unchanged. A recommendation is created only for pairs that pass it.
- **Idempotency:** one row per (job, candidate), enforced by a UNIQUE constraint. Rows are created with `INSERT … ON CONFLICT DO NOTHING`, then locked (`SELECT … FOR UPDATE`) before any transition. Every batch processes rows in (job_id, candidate_id) order, so concurrent batches can't deadlock. Deadlock and serialization errors are retried.
- **No repeat notifications:** each recommendation stores a fingerprint of exactly what the employer would see. The "new" badge is raised only when that fingerprint differs from the last one the employer was notified about. Withdraw/restore cycles and unchanged recomputations raise nothing.

### Current eligibility, enforced twice
1. **Write time.** Every trigger recomputes statuses: QUEUED, AVAILABLE or VIEWED, and WITHDRAWN.
2. **Read time.** Every dashboard and profile query joins live eligibility, so a missed status update can never expose a profile:
   - the job belongs to the signed-in employer
   - the employer is verified
   - the candidate has consented
   - the status is AVAILABLE or VIEWED.

### Employer isolation
- The Recommended Candidates tab only lists the signed-in, **verified** employer's own jobs. Authorization is re-checked from the database on every request (signed token, unchanged credential, verified).
- No search, no browsing, no export; at most 20 per job, ranked by score.
- Opening a profile re-checks ownership and eligibility server-side. Forged button keys, forged session state and other employers' record ids return nothing.
- Each recommendation has a random opaque reference (e.g. `C-7F3A9B2C1D`). The candidate's database id is never shown, and references can't be linked across employers or jobs.

### Anonymised profile (allow-list only)
| Shown | Never shown |
|---|---|
| Opaque reference | Name, email, phone, street address, city, state, ZIP, exact coordinates |
| The employer's own job title | Free-text role, resume text, employers, schools, dates, URLs |
| Qualifications from the fixed matching dictionary | Exact score, exact distance |
| Score band (`50–69`, `70–84`, `85+`) and distance band (`<5`, `5–10`, `10–25`, `25–50` mi) | Internal ledger payloads (`semantic_matches.payload`) |

Free-text fields are never shown, even redacted, because they can carry indirect identifiers. New ledger payloads also stop copying the candidate's name and email.

### Audit (append-only)
`privacy_audit_log` records:
- consent granted and withdrawn
- recommendations queued, made available, refreshed, restored and withdrawn
- every profile view.

A database trigger rejects `UPDATE`, `DELETE` and `TRUNCATE`. Rows hold ids, actor type and reference, event type and non-personal detail.

### Prepared for the Candidate Module
- `candidate_profiles.resume_version` is included in the fingerprint, so resume updates re-surface once.
- Matching accepts a list of candidate locations, ready for multi-state preferences.
- `contact_disclosure_consent` is reserved for the per-employer disclosure flow.

## Database changes (additive; existing rows unchanged)
| Object | Change |
|---|---|
| `candidate_profiles` | + `employer_matching_consent BOOLEAN NOT NULL DEFAULT FALSE`, `employer_matching_consent_at`, `employer_matching_consent_policy`, `contact_disclosure_consent BOOLEAN NOT NULL DEFAULT FALSE`, `resume_version INTEGER NOT NULL DEFAULT 1` |
| `employer_recommendations` | New. Status (CHECK), reason, opaque ref, anonymised profile, fingerprint, notified and viewed fingerprints, score, distance, timestamps. UNIQUE(job_id, candidate_profile_id). |
| `privacy_audit_log` | New, append-only (trigger) |
| `semantic_matches.payload` | New rows only: no candidate name or email |

## Tests
- **Isolation:** cross-employer requests; forged session tokens, button keys and session state; unverified employers.
- **Anonymisation:** direct and indirect identifiers. Every word of a candidate's name, contact details, address, free-text role and resume, apart from the fixed qualification dictionary, must be absent from employer output and stored profiles.
- **Statuses:** every transition, including consent withdrawal and verification revocation blocking access immediately, plus read-time enforcement when statuses are stale.
- **Duplicates, idempotency and concurrency:** repeated and parallel generation, with exactly one row and one audit event per real transition.
- **Matching:** engine unchanged, with documented weaknesses across occupations (`docs/MATCHING_ENGINE_ASSESSMENT.md`).
- **Existing suites:** unit, migration, end-to-end and diagnostics all still pass.
