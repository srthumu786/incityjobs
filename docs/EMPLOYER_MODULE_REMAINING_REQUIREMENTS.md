# Employer Module: remaining requirements (for review; nothing here is implemented)

**Basis:** code on `fix/db-pooling-tables` as of 2026-11-10. This document lists what's still missing, what each piece depends on, and the decisions needed before the next phase. No work starts until it's approved.

## 1. What exists today
| Area | Status |
|---|---|
| Employer registration, passwords, signed sessions, revocation on credential change | Done |
| Manager verification of employers, which gates job posting | Done (manual verification) |
| Job posting with geocoding (no fallback coordinates), requirements, radius | Done |
| Recommended Candidates (consent, anonymised, isolated, audited) | Done |
| **Payments** | **None.** Each job stores only a pricing-tier label (`$300` / `$750` / `$1,500`). |
| **Billing** | **None** |
| **Interviews / calendar** | **None.** No code. |
| **Communication with candidates** | **None.** No code. Contact disclosure is reserved (flag defaults off) and has no flow. |
| Email notifications | **None.** No mail gateway. The `DELIVERED` status is reserved for this. |

## 2. Fix before any launch (small; found in this review)
| # | Issue | Where | Why it matters |
|---|---|---|---|
| K1 | Registration form says "Deferred 14-Day Settlement active via Stripe framework parameters upon validation" | `app.py`, registration form | No Stripe integration exists. The same kind of misleading claim we removed from the confirmation block. |
| K2 | The employer's placement-fee tier is shown to **candidates** as "System Billing Value" on the Employee Portal map | `app.py`, Employee Portal match cards | Commercial employer information exposed to candidates |
| K3 | "JOB REQUISITION SYSTEM DEPLOYED LIVE TO NEON CLUSTER!" | `app.py`, job publish | Technical and over-stated. Should be a plain "Job published". |
| K4 | Matching only works for short, keyword-dense resumes | `matching.py` | See `docs/MATCHING_ENGINE_ASSESSMENT.md` (R1–R5) |

## 3. Payment-method verification
**Goal:** a verified employer has a valid payment method on file before taking on billable placements.

**Proposed approach (Stripe; to be confirmed):**
- One Stripe *Customer* per employer, created at verification or at first payment setup.
- Collect and verify the payment method on **Stripe-hosted pages** (Checkout in *setup* mode / a SetupIntent). Card or bank details then never touch InCityJobs servers, which keeps PCI scope minimal. Only Stripe ids and status are stored.
- New table `employer_billing_accounts`: employer_id, stripe_customer_id, payment_method_status (`NONE`/`PENDING`/`VERIFIED`/`FAILED`/`REMOVED`), last4/brand for display only, timestamps. Every change is audited.
- Gate the agreed action (e.g. publishing a paid-tier job, or confirming a hire) on `VERIFIED`. Re-check on every request, the same way as employer verification.

**Architecture constraint: incoming webhooks.**
- Stripe reports results through webhooks (HTTPS POST). The app runs on Streamlit Community Cloud, which serves the Streamlit app, not custom endpoints.
- Streamlit's newer `st.App` can mount custom routes. Its docs page is for v1.67 (we pin 1.66), and Community Cloud support isn't documented. **Needs a spike to confirm.**
- Fallback options:
  - (a) A small separate webhook service, e.g. a serverless function, that verifies Stripe signatures and writes to Neon.
  - (b) No webhooks: query Stripe's API when the employer returns from the hosted page and when billing pages load. Simpler, but slower to reflect changes.

**Secrets:** Stripe secret key and webhook secret, in Streamlit secrets or the webhook service. Use test-mode keys on staging.

**Decisions needed:**
1. Is Stripe the provider?
2. Which action requires a verified payment method?
3. Cards only, or bank (ACH) too?
4. Webhook hosting: (a), (b) or `st.App`?

## 4. Billing dashboard
**Goal:** employers see what they owe and have paid. Managers see receivables.

**Needs a business definition first:**
- **What is billable?** A confirmed hire? A candidate reaching an agreed stage? The current UI mentions "Pay after 2 weeks" and "14-day settlement", which suggests a fee due 14 days after a hire. That needs confirming.
- Fee per tier ($300 / $750 / $1,500): set when the job is posted (stored today as text). Should it become a numeric price with currency, locked per job?
- Refunds and guarantees (e.g. the hire leaves within N days), disputes, taxes, invoicing entity.

**Proposed scope:**
- Append-only `billing_events` ledger: placement confirmed, invoice issued, paid, refunded, disputed. Each event linked to employer, job and the Stripe ids.
- Invoices through Stripe Invoicing or PaymentIntents, with receipts emailed by Stripe.
- **Employer dashboard:** open and paid invoices, upcoming charges, placements per job. "Manage payment method" links to the Stripe Customer Portal.
- **Manager view:** receivables, failed payments, disputes.
- Reconciliation check: ledger vs Stripe.

**Dependencies:**
- Payment-method verification (section 3).
- A hire-confirmation step. That needs the interview and contact flows, so there's a real placement to bill.

## 5. Interview calendar
**Goal:** an employer proposes interview times for a recommended candidate, and the candidate accepts, declines or reschedules.

**Hard dependencies:**
- **Candidate Module accounts**, because candidates must be able to sign in and respond.
- **Contact-disclosure consent per employer**. The interview step is where the candidate decides to reveal their identity to that employer.
- **A notification channel** (email at minimum) to tell the candidate about a proposal and send calendar invites. This brings in the reserved `DELIVERED` status.

**Proposed scope:**
- `interview_slots`: proposed by the employer, in time zones, each with a duration.
- `interviews`: status `PROPOSED` / `ACCEPTED` / `DECLINED` / `RESCHEDULE_REQUESTED` / `CANCELLED` / `COMPLETED` / `NO_SHOW`.
- Double-booking prevention through database constraints and row locks, the same as recommendations.
- **Calendar delivery:** `.ics` attachments by email (no OAuth needed), or optional Google/Microsoft calendar connection (OAuth, more scope and review).
- Reminders, which need a scheduler. Streamlit Cloud has none, so this needs the same external service as webhooks, or a scheduled job.
- Everything audited.

**Decisions needed:**
1. Video link generation (none, or Zoom/Meet/Teams)?
2. `.ics` only, or calendar OAuth?
3. Can candidates propose times?

## 6. Communication rooms
**Goal:** messaging between an employer and a candidate about a specific job.

**Privacy model (decision needed):**
- (a) No messaging until the candidate authorises contact disclosure for that employer. Then messages show real names.
- (b) Anonymous messaging before disclosure (employer sees `C-…`). This needs content filtering, because people can share phone numbers or emails in free text.

**Proposed scope:**
- `conversations` (employer, candidate, job) and `messages`: sender, body, timestamps, read state.
- Access checks on every read, re-validating employer verification and the candidate's disclosure consent, like Recommended Candidates.
- Either party can close a conversation. Retention period and deletion on request.
- Abuse reporting and manager moderation view. Rate limits.
- **Attachments:** exclude at first; resumes stay in the Candidate Module.
- **Real-time:** Streamlit reruns per interaction. Options:
  - periodic refresh using `st.fragment(run_every=…)`
  - a third-party chat service (adds a processor of personal data, needs review)
- Email notification of new messages needs the mail gateway.

**Dependencies:** Candidate Module accounts, contact-disclosure flow, mail gateway.

## 7. Other remaining Employer Module items
| Item | Notes |
|---|---|
| **Employer email verification** | Verification is currently a manager decision only. An emailed confirmation link would need the mail gateway. |
| **Self-service password reset** | Today only a manager-issued setup code works. Needs the mail gateway. |
| **Job lifecycle** | Edit, close, expire and reopen jobs. Closing must withdraw that job's recommendations; the existing rules support this. |
| **Multiple users per employer** | Roles: admin, recruiter. Today one login per company. |
| **Data retention and deletion** | Policies for candidates, jobs, recommendations and messages. Audit-log retention is append-only by design, so its retention period needs deciding. |
| **Legal texts** | Terms of Service and Privacy Policy, with versioning like the consent policy version. The registration checkbox references "National Terms of Service", which doesn't exist yet. |
| **Notifications for employers** | e.g. new recommendations by email. Needs the mail gateway and preferences. Would use `DELIVERED`. |

## 8. Shared infrastructure the remaining features need
1. **Mail gateway** (transactional email provider): verification emails, password reset, interview invitations, notifications. This unblocks `DELIVERED`.
2. **Endpoint for incoming webhooks plus a scheduler**: Stripe webhooks, reminders, retries. Either `st.App` (to be verified on Community Cloud) or a small separate service.
3. **Candidate Module**: accounts, resume upload, contact-disclosure per employer, location preferences.

## 9. Suggested order (for discussion)
1. **K1–K3 copy fixes**: small, no new dependencies.
2. **Spike:** webhook hosting and mail gateway choice. Decides the architecture for 3–6.
3. **Payment-method verification** (Stripe test mode on staging).
4. **Candidate Module** (accounts, uploads, disclosure) plus **matching improvements R1–R3**. Could run in parallel.
5. **Interview calendar**, then **communication rooms**.
6. **Billing dashboard**, once a confirmed-hire event exists to bill.

## 10. Decisions requested
| # | Decision |
|---|---|
| D1 | Payment provider (Stripe?) and which action requires a verified payment method |
| D2 | Definition of a billable placement, fee timing ("pay after 2 weeks"?), refunds and guarantees |
| D3 | Webhook and scheduler hosting: `st.App` (after a spike), a separate service, or polling |
| D4 | Mail gateway provider |
| D5 | Messaging privacy model: after disclosure only, or anonymous before disclosure |
| D6 | Calendar delivery: `.ics` email only, or Google/Microsoft OAuth; video links |
| D7 | Whether to start the matching improvements (R1–R5) before or alongside the Candidate Module |
