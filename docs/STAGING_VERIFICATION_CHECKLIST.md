# Live staging verification checklist: `fix/db-pooling-tables`

Use this to verify the branch on the **private staging app** connected to the **Neon test branch**, before any merge. Set-up steps (Neon branch, secrets, deploy, privacy) are in `docs/STAGING_DEPLOYMENT.md`. Record each result in the sign-off table at the end.

**Ground rules**
- Staging only. Before starting, confirm the `ep-…` endpoint in the staging secret is **not** the production endpoint.
- Use test identities you control (e.g. `you+emp1@yourdomain.com`). Don't enter real candidates' data.
- SQL below is meant for the **Neon SQL Editor on the staging branch**. Everything is read-only except item B4, which is a write attempt the database must reject.
- Geocoding uses the public Nominatim service (about 1 request per second). Leave a few seconds between submissions.

---

## A. Deployment and startup
| # | Check | Expected |
|---|---|---|
| A1 | Streamlit Cloud: app → **Manage app** → logs after the first page load | Dependencies install. No `ICJ-ERR-500` or `ICJ-ERR-511` lines. |
| A2 | Manager Console → **🩺 System Checks** → Dependencies | ✅ for `streamlit 1.66.0`, `pandas 3.0.6`, `psycopg2-binary 2.9.11`, `geopy 2.4.1`, `openpyxl 3.1.5`. Note the Python and PostgreSQL versions. |
| A3 | System Checks → UNIQUE email constraints | "Migration complete: database-level email uniqueness verified active". Both constraints show ✅ **enforced**. |
| A4 | System Checks → Historical fallback-location records | The counts are shown (rows are kept but excluded). Note them. |
| A5 | *Optional negative test:* temporarily change one character of the password in `connection_string`, reboot, then try an employer registration | User sees **ICJ-ERR-500**, not ICJ-ERR-504. Log line `ICJ-ERR-500 [startup.database_connection …] … password authentication failed` contains **no password or connection string**. Then restore the secret and reboot. |

## B. Schema and audit (SQL, staging branch)
```sql
-- B1: new consent fields exist and existing candidates have NO authorization
SELECT count(*) AS total,
       count(*) FILTER (WHERE employer_matching_consent) AS matching_consent,
       count(*) FILTER (WHERE contact_disclosure_consent) AS contact_consent
FROM candidate_profiles;          -- before any new sign-ups: matching_consent = 0, contact_consent = 0

-- B2: new tables exist
SELECT to_regclass('employer_recommendations'), to_regclass('privacy_audit_log');

-- B3: append-only triggers are installed
SELECT tgname FROM pg_trigger WHERE tgrelid = 'privacy_audit_log'::regclass AND NOT tgisinternal;
-- expected: privacy_audit_log_append_only, privacy_audit_log_no_truncate

-- B4 (write attempt that MUST fail): tamper with the audit log
UPDATE privacy_audit_log SET event_type = 'TAMPERED' WHERE id = (SELECT min(id) FROM privacy_audit_log);
-- expected: ERROR: privacy_audit_log is append-only
```
| # | Check | Expected |
|---|---|---|
| B1–B3 | Run the queries | As commented |
| B4 | Run the UPDATE | Rejected with "append-only". No rows changed. |

## C. Employer registration and verification
| # | Check | Expected |
|---|---|---|
| C1 | Register employer **E1** | "✅ Registration successful. You're signed in." and "⏳ Verification pending…". None of: "Fully Verified", "Secure Travel Mode", "Deferred Settlement", "Merchant Routing", "ICJ-GLOBAL", "Cryptographic Contract Seal". |
| C2 | E1 → **Post a Job** and **⭐ Recommended Candidates** tabs | Both locked with a verification-pending message. No job form. |
| C3 | Manager → **🔑 Employer Access** → E1 → **Mark as verified** | E1 shown as VERIFIED |
| C4 | E1 posts a job with a **misspelled city** (e.g. `Pricevile`) | Not published. Message explains how to format the location. |
| C5 | E1 posts a job with a real address. Use the requirements text in section E. | Published |

## D. Candidate consent
| # | Check | Expected |
|---|---|---|
| D1 | Employee Portal: submit candidate **K1** with the consent box **unticked** | "Employer matching: OFF" |
| D2 | Submit candidate **K2** with the box **ticked** | "Employer matching: ON" |
| D3 | SQL: `SELECT contact_email, employer_matching_consent, employer_matching_consent_policy, employer_matching_consent_at IS NOT NULL FROM candidate_profiles WHERE contact_email IN ('<K1>','<K2>');` | K1: false / null / false. K2: true / `employer-matching-v1-2026-11` / true |
| D4 | In K2's same browser session, click **Withdraw consent**, then **turn on** again | Both succeed |
| D5 | Manager → **🛡️ Consent & Audit** → look up K2 → **Record consent withdrawal requested by the candidate** | Consent OFF. Audit shows `CONSENT_WITHDRAWN` with actor `manager:<name>`. No grant button exists for managers. |
| D6 | Re-enable K2 from K2's session for section E | Consent ON |

## E. Recommendations
> **Matching limitation:** the current engine only matches short, keyword-dense resumes (see `docs/MATCHING_ENGINE_ASSESSMENT.md`). For this test, use a pair like the one below. A realistic full resume won't match, and that's the known limitation, not a staging fault.

- **Job (E1):** Title `Python Developer` · Department `Technical Services` · Requirements `Python, SQL databases, Docker containers, REST APIs, AWS cloud.`
- **Candidate (K2):** Resume `Python developer: SQL, Docker, REST APIs, AWS, 4 years backend services.` · target location within the job's radius.

| # | Check | Expected |
|---|---|---|
| E1 | After E1's job and K2's consenting profile exist: E1 → **⭐ Recommended Candidates** | K2 listed as `C-XXXXXXXXXX 🆕` with a match-strength band, a distance band, and qualifications like `aws, docker, python, sql`. **No name, email, address, city or resume text.** |
| E2 | K1 (no consent) with a matching resume | **Not** listed. SQL shows K1's recommendation as `QUEUED / awaiting_candidate_consent`. |
| E3 | E1 clicks **Open profile C-…** | Profile panel opens. The 🆕 badge is cleared. Status shows "Viewed". |
| E4 | Reload the page twice | Still open. SQL: exactly **one** `PROFILE_VIEWED` audit row for that reference. |
| E5 | Manager → Consent & Audit → **Recompute all recommendations**, then E1 reloads | No new 🆕 badge for unchanged recommendations |

```sql
-- E2/E4 helpers
SELECT employer_ref, status, status_reason, is_new FROM employer_recommendations ORDER BY id DESC LIMIT 10;
SELECT event_type, actor_type, actor_ref, recommendation_id, created_at FROM privacy_audit_log ORDER BY id DESC LIMIT 20;
```

## F. Isolation
| # | Check | Expected |
|---|---|---|
| F1 | Register and verify employer **E2**, post a different job | E2 sees only its own job's recommendations. Nothing of E1's. |
| F2 | The same consenting candidate matched by both employers | Different `C-…` reference for each employer |
| F3 | Private/incognito window, signed out → Employer Portal → Recommended Candidates | Sign-in prompt only |
| F4 | Register employer **E3**, leave unverified | Recommended Candidates shows the verification notice only |

## G. Immediate revocation
| # | Check | Expected |
|---|---|---|
| G1 | E1 has K2's profile open. K2 withdraws consent in K2's session. E1 reloads. | K2 is gone and the open profile closes. SQL: K2's recommendation is `WITHDRAWN`. |
| G2 | Manager revokes E1's verification. E1 reloads. | Dashboard shows the verification notice. No candidates. |
| G3 | Manager re-verifies E1. K2 re-consents. E1 reloads. | K2 is restored. No new 🆕 badge if the content is unchanged and it was already viewed. |

## H. Anonymization spot-check
| # | Check | Expected |
|---|---|---|
| H1 | Candidate **K3** (consenting, matching) with identifying details in role, address and resume: a full name, phone number, employer name, school, LinkedIn handle | E1's page shows none of these words. Only the reference, bands, the job title and dictionary qualifications. |

## I. Ledger privacy and telemetry
```sql
-- I1: new semantic_matches payloads contain no candidate name/email (run after a manager "Run matching")
SELECT id, payload->'candidate' FROM semantic_matches ORDER BY id DESC LIMIT 5;   -- expected: {"profile_id": N} only
-- I2: no unexpected errors
SELECT diagnostic_error_code, error_context_vector, count(*) FROM system_telemetry_logs GROUP BY 1, 2 ORDER BY 3 DESC;
```

## J. Sign-off
| Section | Result (pass/fail + notes) | Evidence (screenshot / query output) | Verified by | Date |
|---|---|---|---|---|
| A Deployment & startup | | | | |
| B Schema & audit | | | | |
| C Employer registration & verification | | | | |
| D Candidate consent | | | | |
| E Recommendations | | | | |
| F Isolation | | | | |
| G Immediate revocation | | | | |
| H Anonymization | | | | |
| I Ledger & telemetry | | | | |

**Teardown after review:** follow `docs/STAGING_DEPLOYMENT.md`, step 9. Deleting the Neon test branch removes all test data, including its audit rows, which can't be deleted individually by design. Production is unaffected.
