"""Database schema and safe migrations for InCityJobs.

Used by app.py (once per server process) and by scripts/check_migration.py.
No Streamlit imports.

Migration safety rules:
- Only additive changes (CREATE ... IF NOT EXISTS, ADD COLUMN IF NOT EXISTS).
  No existing row is updated or deleted.
- UNIQUE email constraints are case- and whitespace-insensitive
  (lower(btrim(email))) and skip NULL/blank emails.
- Before creating a UNIQUE constraint, existing rows are checked for
  duplicates. If any exist, that constraint is skipped and reported, and the app
  keeps running with application-level duplicate checks. It is created on a
  later start once the duplicates have been cleaned up by hand.
- A UNIQUE constraint only counts as active after the catalog confirms the index
  is unique, valid, ready and live, on the expected expression and predicate.
  report["complete"] is True only when every constraint passes that check.
- Rows that carry the old geocoding fallback point are never modified; queries
  exclude them via trusted_location_sql().
"""
import uuid

import psycopg2

from matching import LEGACY_FALLBACK_COORDS

BASE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS beta_employers (
    id SERIAL PRIMARY KEY,
    company_name VARCHAR(255),
    corporate_hq TEXT,
    admin_email VARCHAR(255),
    admin_phone VARCHAR(50),
    signature_hash VARCHAR(64),
    password_hash TEXT,
    setup_code_hash VARCHAR(64),
    setup_code_expires_at TIMESTAMPTZ,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
-- Employer authentication
ALTER TABLE beta_employers ADD COLUMN IF NOT EXISTS password_hash TEXT;
ALTER TABLE beta_employers ADD COLUMN IF NOT EXISTS setup_code_hash VARCHAR(64);
ALTER TABLE beta_employers ADD COLUMN IF NOT EXISTS setup_code_expires_at TIMESTAMPTZ;
-- Employer verification: job posting requires verified_at to be set by a manager.
-- Existing employers start unverified (NULL); no existing value is changed.
ALTER TABLE beta_employers ADD COLUMN IF NOT EXISTS verified_at TIMESTAMPTZ;
ALTER TABLE beta_employers ADD COLUMN IF NOT EXISTS verified_by VARCHAR(255);

CREATE TABLE IF NOT EXISTS candidate_profiles (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255),
    contact_email VARCHAR(255),
    role VARCHAR(255),
    resume TEXT,
    address TEXT,
    current_address TEXT,
    latitude FLOAT,
    longitude FLOAT,
    status VARCHAR(50),
    application_status VARCHAR(32) NOT NULL DEFAULT 'PENDING',
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS contact_email VARCHAR(255);
ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS current_address TEXT;
ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS application_status VARCHAR(32) NOT NULL DEFAULT 'PENDING';

CREATE TABLE IF NOT EXISTS employer_jobs (
    id SERIAL PRIMARY KEY,
    title VARCHAR(255),
    department VARCHAR(100),
    pricing_tier VARCHAR(100),
    address TEXT,
    latitude FLOAT,
    longitude FLOAT,
    source_channel VARCHAR(64),
    external_tracking_id VARCHAR(128),
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
-- Attribution metrics for the Multi-Channel Push Architecture
ALTER TABLE employer_jobs ADD COLUMN IF NOT EXISTS source_channel VARCHAR(64);
ALTER TABLE employer_jobs ADD COLUMN IF NOT EXISTS external_tracking_id VARCHAR(128);
-- Structured matching parameters and ownership (legacy jobs keep NULL employer_id)
ALTER TABLE employer_jobs ADD COLUMN IF NOT EXISTS employer_id INTEGER REFERENCES beta_employers(id) ON DELETE SET NULL;
ALTER TABLE employer_jobs ADD COLUMN IF NOT EXISTS requirements TEXT;
ALTER TABLE employer_jobs ADD COLUMN IF NOT EXISTS match_radius_miles INTEGER NOT NULL DEFAULT 25;

CREATE TABLE IF NOT EXISTS semantic_matches (
    id SERIAL PRIMARY KEY,
    candidate_profile_id INTEGER,
    candidate_email VARCHAR(255),
    employer_email VARCHAR(255),
    match_score FLOAT,
    distance_miles FLOAT,
    status VARCHAR(100),
    payload JSONB,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE semantic_matches ADD COLUMN IF NOT EXISTS candidate_profile_id INTEGER;
ALTER TABLE semantic_matches ADD COLUMN IF NOT EXISTS payload JSONB;
ALTER TABLE semantic_matches ADD COLUMN IF NOT EXISTS job_id INTEGER REFERENCES employer_jobs(id) ON DELETE SET NULL;
-- One ledger row per candidate per job. Legacy rows have NULL job_id and are excluded.
CREATE UNIQUE INDEX IF NOT EXISTS uq_semantic_matches_candidate_job
    ON semantic_matches (candidate_profile_id, job_id)
    WHERE candidate_profile_id IS NOT NULL AND job_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS system_telemetry_logs (
    id SERIAL PRIMARY KEY,
    diagnostic_error_code VARCHAR(32) NOT NULL,
    error_context_vector TEXT NOT NULL,
    error_detail TEXT,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Candidate consent (docs/EMPLOYER_RECOMMENDATIONS_PLAN.md). Every new field
-- defaults to NO authorization, so existing candidates are not shown to employers.
ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS employer_matching_consent BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS employer_matching_consent_at TIMESTAMPTZ;
ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS employer_matching_consent_policy VARCHAR(64);
ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS contact_disclosure_consent BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS resume_version INTEGER NOT NULL DEFAULT 1;

-- Employer-facing recommendations: one row per (job, candidate). DELIVERED is
-- reserved for a future messaging integration and deliberately not allowed yet.
CREATE TABLE IF NOT EXISTS employer_recommendations (
    id BIGSERIAL PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES employer_jobs(id) ON DELETE CASCADE,
    employer_id INTEGER NOT NULL REFERENCES beta_employers(id) ON DELETE CASCADE,
    candidate_profile_id INTEGER NOT NULL REFERENCES candidate_profiles(id) ON DELETE CASCADE,
    status VARCHAR(16) NOT NULL CHECK (status IN ('QUEUED', 'AVAILABLE', 'VIEWED', 'WITHDRAWN')),
    status_reason VARCHAR(64),
    employer_ref VARCHAR(16) NOT NULL UNIQUE,
    profile JSONB NOT NULL,
    fingerprint CHAR(64) NOT NULL,
    notified_fingerprint CHAR(64),
    viewed_fingerprint CHAR(64),
    is_new BOOLEAN NOT NULL DEFAULT FALSE,
    score REAL,
    distance_miles REAL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    available_at TIMESTAMPTZ,
    viewed_at TIMESTAMPTZ,
    withdrawn_at TIMESTAMPTZ,
    UNIQUE (job_id, candidate_profile_id)
);
CREATE INDEX IF NOT EXISTS idx_employer_recommendations_employer_status
    ON employer_recommendations (employer_id, status);
CREATE INDEX IF NOT EXISTS idx_employer_recommendations_candidate
    ON employer_recommendations (candidate_profile_id);

-- Append-only audit of consent, recommendation visibility, profile views and
-- withdrawals. Ids and non-personal detail only. UPDATE/DELETE/TRUNCATE are
-- rejected by triggers (PostgreSQL 14+).
CREATE TABLE IF NOT EXISTS privacy_audit_log (
    id BIGSERIAL PRIMARY KEY,
    event_type VARCHAR(40) NOT NULL,
    actor_type VARCHAR(16) NOT NULL CHECK (actor_type IN ('system', 'candidate', 'employer', 'manager')),
    actor_ref VARCHAR(64),
    candidate_profile_id INTEGER,
    employer_id INTEGER,
    job_id INTEGER,
    recommendation_id BIGINT,
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_privacy_audit_log_candidate ON privacy_audit_log (candidate_profile_id, created_at);
CREATE INDEX IF NOT EXISTS idx_privacy_audit_log_recommendation ON privacy_audit_log (recommendation_id);
CREATE OR REPLACE FUNCTION incity_reject_audit_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'privacy_audit_log is append-only' USING ERRCODE = 'insufficient_privilege';
END;
$$;
CREATE OR REPLACE TRIGGER privacy_audit_log_append_only
    BEFORE UPDATE OR DELETE ON privacy_audit_log
    FOR EACH ROW EXECUTE FUNCTION incity_reject_audit_change();
CREATE OR REPLACE TRIGGER privacy_audit_log_no_truncate
    BEFORE TRUNCATE ON privacy_audit_log
    FOR EACH STATEMENT EXECUTE FUNCTION incity_reject_audit_change();

-- Non-unique email indexes from earlier revisions of this branch are replaced by
-- the UNIQUE ones below.
DROP INDEX IF EXISTS idx_beta_employers_admin_email;
DROP INDEX IF EXISTS idx_candidate_profiles_email;
DROP INDEX IF EXISTS idx_candidate_profiles_contact_email;
"""

# (index name, table, email column). Identifiers are constants, never user input.
UNIQUE_EMAIL_CONSTRAINTS = (
    ("uq_beta_employers_admin_email", "beta_employers", "admin_email"),
    ("uq_candidate_profiles_contact_email", "candidate_profiles", "contact_email"),
)


def email_key(column: str) -> str:
    """SQL expression used for every email comparison and for the UNIQUE index."""
    return f"lower(btrim({column}))"


def email_profile(cur, table: str, column: str) -> dict:
    """Counts describing the email column before the constraint is applied."""
    cur.execute(f"""
        SELECT count(*),
               count(*) FILTER (WHERE {column} IS NULL OR btrim({column}) = ''),
               count(*) FILTER (WHERE {column} IS NOT NULL AND btrim({column}) <> ''
                                  AND {column} <> {email_key(column)})
        FROM {table};
    """)
    total, null_or_blank, mixed_case_or_spaces = cur.fetchone()
    return {
        "rows": int(total),
        "null_or_blank_emails": int(null_or_blank),
        "mixed_case_or_whitespace_emails": int(mixed_case_or_spaces),
    }


def find_duplicate_emails(cur, table: str, column: str, limit: int = 50) -> list:
    """Groups of rows whose emails collide case-insensitively: [(email_key, copies, [ids])]."""
    key = email_key(column)
    cur.execute(f"""
        SELECT {key} AS email_key, count(*) AS copies, array_agg(id ORDER BY id) AS row_ids
        FROM {table}
        WHERE {column} IS NOT NULL AND btrim({column}) <> ''
        GROUP BY {key}
        HAVING count(*) > 1
        ORDER BY copies DESC, email_key
        LIMIT %s;
    """, (limit,))
    return [tuple(r) for r in cur.fetchall()]


def unique_index_exists(cur, index_name: str) -> bool:
    cur.execute("SELECT 1 FROM pg_indexes WHERE schemaname = current_schema() AND indexname = %s;", (index_name,))
    return cur.fetchone() is not None


def verify_unique_index(cur, index_name: str, table: str, column: str) -> dict:
    """Confirm from the system catalog that the index really enforces uniqueness:
    unique, valid, ready, live, on `table`, keyed on lower(btrim(column)), with the
    NULL/blank-excluding predicate. Returns {"active": bool, "problems": [...]}."""
    cur.execute("""
        SELECT i.indisunique, i.indisvalid, i.indisready, i.indislive, t.relname,
               pg_get_indexdef(i.indexrelid), coalesce(pg_get_expr(i.indpred, i.indrelid), '')
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_index i ON i.indexrelid = c.oid
        JOIN pg_class t ON t.oid = i.indrelid
        WHERE c.relname = %s AND n.nspname = current_schema();
    """, (index_name,))
    row = cur.fetchone()
    if row is None:
        return {"active": False, "problems": ["index does not exist"]}
    is_unique, is_valid, is_ready, is_live, on_table, definition, predicate = row
    problems = []
    if not is_unique:
        problems.append("index is not UNIQUE")
    if not (is_valid and is_ready and is_live):
        problems.append("index is not valid/ready/live")
    if on_table != table:
        problems.append(f"index is on {on_table}, expected {table}")
    if "lower(btrim(" not in definition or column not in definition:
        problems.append(f"unexpected key expression: {definition}")
    if column not in predicate or "btrim" not in predicate:
        problems.append(f"unexpected predicate: {predicate or 'none'}")
    return {"active": not problems, "problems": problems, "definition": definition}


def _create_unique_index(cur, index_name, table, column, replace=False):
    """Create (or rebuild) the UNIQUE index inside a savepoint. Returns an error string or None."""
    cur.execute("SAVEPOINT add_unique_email;")
    try:
        if replace:
            cur.execute(f"DROP INDEX {index_name};")
        cur.execute(f"""
            CREATE UNIQUE INDEX {index_name}
            ON {table} ({email_key(column)})
            WHERE {column} IS NOT NULL AND btrim({column}) <> '';
        """)
        cur.execute("RELEASE SAVEPOINT add_unique_email;")
        return None
    except psycopg2.Error as exc:
        # e.g. a duplicate inserted between the check and the CREATE
        cur.execute("ROLLBACK TO SAVEPOINT add_unique_email;")
        return f"{type(exc).__name__}: {exc}"


def trusted_location_sql(alias: str = "") -> str:
    """SQL condition that excludes rows stored at the old geocoding fallback point."""
    prefix = f"{alias}." if alias else ""
    lat, lon = LEGACY_FALLBACK_COORDS
    return f"NOT ({prefix}latitude IS NOT DISTINCT FROM {lat} AND {prefix}longitude IS NOT DISTINCT FROM {lon})"


def legacy_fallback_counts(cur) -> dict:
    lat, lon = LEGACY_FALLBACK_COORDS
    counts = {}
    for table in ("candidate_profiles", "employer_jobs"):
        cur.execute(f"SELECT count(*) FROM {table} WHERE latitude = {lat} AND longitude = {lon};")
        counts[table] = int(cur.fetchone()[0])
    return counts


def apply_schema(conn, commit: bool = True) -> dict:
    """Create/migrate all tables, then add the UNIQUE email constraints where the
    existing data allows it, and verify each one in the catalog. Commits on
    success unless commit=False (dry run: the caller rolls back). Returns:
        {"complete": bool,
         "constraints": {table: {"status", "verification", "profile", "duplicates"}},
         "legacy_fallback_rows": {table: count}}
    status is one of: "enforced" (verified active), "skipped_duplicates",
    "invalid_index" (exists but fails verification and can't be rebuilt), "failed".
    """
    report = {"constraints": {}}
    with conn.cursor() as cur:
        cur.execute(BASE_SCHEMA_SQL)
        for index_name, table, column in UNIQUE_EMAIL_CONSTRAINTS:
            entry = {"index": index_name, "column": column, "profile": email_profile(cur, table, column),
                     "duplicates": find_duplicate_emails(cur, table, column)}
            exists = unique_index_exists(cur, index_name)
            check = verify_unique_index(cur, index_name, table, column) if exists else None

            if exists and check["active"]:
                pass  # already enforced
            elif entry["duplicates"]:
                entry["status"] = "invalid_index" if exists else "skipped_duplicates"
            else:
                error = _create_unique_index(cur, index_name, table, column, replace=exists)
                if error:
                    entry["error"] = error
            check = verify_unique_index(cur, index_name, table, column)
            entry["verification"] = check
            entry.setdefault("status", "enforced" if check["active"] else "failed")
            report["constraints"][table] = entry
        report["legacy_fallback_rows"] = legacy_fallback_counts(cur)
    report["complete"] = all(e["status"] == "enforced" and e["verification"]["active"]
                             for e in report["constraints"].values())
    if commit:
        conn.commit()
    return report


def probe_unique_enforcement(conn) -> dict:
    """Behavioural check: try to insert the same new email twice (second copy in a
    different case with spaces) and confirm the database refuses it. Everything is
    rolled back. Meant for a Neon TEST branch or local database, not production
    (rolled-back inserts still advance the id sequences)."""
    results = {}
    with conn.cursor() as cur:
        for _, table, column in UNIQUE_EMAIL_CONSTRAINTS:
            email = f"probe-{uuid.uuid4().hex[:12]}@example.invalid"
            cur.execute("SAVEPOINT unique_probe;")
            try:
                cur.execute(f"INSERT INTO {table} ({column}) VALUES (%s);", (email,))
                cur.execute(f"INSERT INTO {table} ({column}) VALUES (%s);", (f"  {email.upper()} ",))
                results[table] = "NOT ENFORCED (duplicate accepted)"
            except psycopg2.Error as exc:
                results[table] = "enforced" if is_unique_violation(exc) else f"error: {exc}"
            cur.execute("ROLLBACK TO SAVEPOINT unique_probe;")
    conn.rollback()
    return results


def is_unique_violation(exc: BaseException) -> bool:
    return getattr(exc, "pgcode", None) == "23505"
