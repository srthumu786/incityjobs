# Staging deployment manual: `fix/db-pooling-tables`

How to run this branch as a **private staging app** on Streamlit Community Cloud, connected
**only** to an isolated **Neon test branch**, before anything is merged into `main`.

Expected time: 30–45 minutes.

---

## 0. Ground rules (read first)

| Rule | Why |
|---|---|
| Never paste the **production** connection string anywhere in this manual. | Every step here must touch only the test branch. |
| Write down your production endpoint ID now (the `ep-…` part of the production host, from Neon → **Connect** with the production branch selected). | Used in step 2 to prove the staging string is *not* production. |
| The staging app must be **private** (step 6). | The Neon test branch is a full copy of production, including real candidate names and emails. Apps from a public repo are public by default. |
| Delete the staging app and the Neon test branch when review ends (step 9). | Avoids leaving a second copy of personal data around. |

You need:
- Access to the Neon project and the Streamlit Community Cloud workspace that deploys `srthumu786/incityjobs`.
- On your computer: Python 3.11 or newer, `git`, and a terminal. (macOS/Linux commands below.)

---

## 1. Create the Neon test branch

1. Neon Console → select the InCityJobs project.
2. Sidebar → **Branches** → **New branch**.
3. **Parent branch**: your production branch (the project's default branch unless you changed it).
4. **Name**: `staging-review-db-pooling`.
5. **What to include**: **Current data**. This gives the migration a faithful copy of production, including any duplicate emails it must detect.
6. **Auto-deletion**: the Console defaults to deleting the branch after **1 day**. Choose **7 days**, or untick it and delete it yourself in step 9.
7. **Create**.

A Neon branch is a copy-on-write clone: nothing done on it affects production.

## 2. Get the test branch connection string, and prove it isn't production

1. Neon Console → **Connect**.
2. Select **Branch: `staging-review-db-pooling`**, database `neondb`, role `neondb_owner`.
3. If the dialog offers connection pooling, turn it on. The host then contains `-pooler`.
4. Copy the string. It looks like
   `postgresql://neondb_owner:…@ep-XXXX-pooler.c-2.us-east-2.aws.neon.tech/neondb?sslmode=require`
5. **Check:** the `ep-…` endpoint ID must be **different** from the production one you wrote down in step 0. If it's the same, stop: you copied the production string.

## 3. Migrate and verify the test branch from your computer

```bash
git clone https://github.com/srthumu786/incityjobs.git
cd incityjobs
git checkout fix/db-pooling-tables

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export INCITY_DB_URL='<test-branch connection string from step 2>'

# 3a. Read-only dry run (runs inside a transaction that is always rolled back)
python scripts/check_migration.py
```

- **Exit 0** with `All UNIQUE constraints can be enforced.`: go to 3b.
- **Exit 1** with `duplicate: 'someone@example.com' x2 (row ids [...])`: production has duplicate
  emails. Decide, row by row, which record to keep, and fix the extra rows **on the test branch**
  with SQL in the Neon SQL Editor (branch `staging-review-db-pooling`), for example by correcting
  the email. Keep notes: the same fixes must later be applied to production before merging.
  Re-run the dry run until it exits 0. (If you skip this, the app still runs, but the
  constraint stays off and System Checks will say so.)

```bash
# 3b. Apply the migration and verify it
python scripts/check_migration.py --apply
```

Expected end of output:
```
Enforcement probe (rolled back): {'beta_employers': 'enforced', 'candidate_profiles': 'enforced'}

MIGRATION COMPLETE: database-level email uniqueness verified active.
```
`--apply` declares completion only after (1) the system catalog confirms both UNIQUE indexes
are unique/valid/ready/live on `lower(btrim(email))`, and (2) a probe inserting the same new email
twice is refused (the probe is rolled back; it only advances the id sequences).

## 4. Create a manager account

```bash
python scripts/hash_password.py
```
Enter a username (e.g. `alice`) and a long password. Copy the printed line, e.g.
`alice = "pbkdf2_sha256$600000$…"`. Repeat for each reviewer who needs the Manager Console.

## 5. Deploy the staging app

1. Go to <https://share.streamlit.io> → **Create app** → **Yup, I have an app**.
2. **Repository**: `srthumu786/incityjobs` · **Branch**: `fix/db-pooling-tables` · **Main file path**: `app.py`.
3. **App URL**: e.g. `incityjobs-staging` (anything clearly marked staging).
4. **Advanced settings**:
   - **Python version**: 3.12 (the default) or any version ≥ 3.11. pandas 3 needs 3.11+.
   - **Secrets**: paste, filling in your values:
     ```toml
     connection_string = "<test-branch connection string from step 2>"

     [managers]
     alice = "pbkdf2_sha256$600000$..."
     ```
5. **Save** / **Deploy** and wait for the app to start (a few minutes).

## 6. Make the staging app private immediately

App → **Settings** → **Sharing** → **Who can view this app** → **Only specific people can view this app**
→ add the reviewers' emails. (Community Cloud allows one private app at a time.)

## 7. Verify on the live staging app

Sign in through **🔐 Manager Access** in the sidebar, open **Manager Console → 🩺 System Checks**:

- [ ] **Dependencies**: all five packages show ✅ (`streamlit 1.66.0`, `pandas 3.0.6`, `psycopg2-binary 2.9.11`, `geopy 2.4.1`, `openpyxl 3.1.5`). Note the Python and PostgreSQL versions shown.
- [ ] **UNIQUE email constraints**: "Migration complete: database-level email uniqueness verified active".
- [ ] **Historical fallback-location records**: counts shown (these rows are kept but excluded from matching).
- [ ] Take a screenshot of this tab for the review record.

Functional checks (use test emails you control, e.g. `you+staging1@yourdomain.com`):

- [ ] **Employer Portal → Corporate Registration**: register a test company. You're signed in, but **Post a Job** shows "Verification pending" and no form.
- [ ] **Manager Console → 🔑 Employer Access**: select it → **Mark as verified**. Back in **Post a Job**, the form now appears.
- [ ] Post a job with a misspelled city (e.g. `Pricevile`): it's **not** published and you're told how to format the location. Post again correctly: it's published.
- [ ] **Employee Portal**: submit a candidate near the job. The map shows only your candidate and nearby verified jobs.
- [ ] Open the app in a private/incognito window: the Employee Portal map is empty (no one else's data).
- [ ] **🎯 Live Job Matching**: select your job → **Run matching**. Matches are logged with "No notifications were sent". Run again: "already in the ledger".
- [ ] **🧪 Simulation Sandbox** → **Run simulation**: results are shown; nothing is logged.
- [ ] Session revocation: sign the test employer in on two browsers. In **Employer Access**, generate a setup code and use it in browser B to set a new password. Browser A is signed out on its next action.
- [ ] Manager revocation: replace your manager hash in **Secrets** with a newly generated one (same or new password) → your open Manager Console session ends.

Optional: confirm in the Neon SQL Editor (test branch) that `system_telemetry_logs` contains only expected codes.

## 8. Record the result

Add to the pull request: the screenshot from step 7, the `check_migration.py --apply` output from
step 3b, and any duplicate-email fixes you made (they must be repeated on production).

## 9. Tear down

1. Streamlit: app → **⋮** → **Delete**.
2. Neon: **Branches** → `staging-review-db-pooling` → **⋮** → **Delete** (unless it auto-deletes).
3. Your computer: `unset INCITY_DB_URL`.

## 10. When approved (production)

1. Neon: create a backup branch of production first (**New branch** → parent = production, **Current data**, auto-deletion off, name `backup-before-db-pooling`). It's your rollback point.
2. Apply the same duplicate-email fixes you made in step 3 to production.
3. Add the `[managers]` section to the **production** app's Secrets.
4. Merge the pull request. Production migrates itself on its first request.
5. Open **System Checks** on production and confirm the same results as step 7.
6. Existing employers start **unverified**: verify each one in **Employer Access**. Legacy employers also need a setup code to set a password.
