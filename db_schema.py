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
"""
import psycopg2

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


def apply_schema(conn, commit: bool = True) -> dict:
    """Create/migrate all tables, then add the UNIQUE email constraints where the
    existing data allows it. Commits on success unless commit=False (dry run:
    the caller rolls back). Returns a report:
        {"constraints": {table: {"status": ..., "profile": {...}, "duplicates": [...]}}}
    status is one of: "enforced", "skipped_duplicates", "failed".
    """
    report = {"constraints": {}}
    with conn.cursor() as cur:
        cur.execute(BASE_SCHEMA_SQL)
        for index_name, table, column in UNIQUE_EMAIL_CONSTRAINTS:
            entry = {"index": index_name, "column": column, "profile": email_profile(cur, table, column), "duplicates": []}
            if unique_index_exists(cur, index_name):
                entry["status"] = "enforced"
            else:
                duplicates = find_duplicate_emails(cur, table, column)
                if duplicates:
                    entry["status"] = "skipped_duplicates"
                    entry["duplicates"] = duplicates
                else:
                    cur.execute("SAVEPOINT add_unique_email;")
                    try:
                        cur.execute(f"""
                            CREATE UNIQUE INDEX {index_name}
                            ON {table} ({email_key(column)})
                            WHERE {column} IS NOT NULL AND btrim({column}) <> '';
                        """)
                        cur.execute("RELEASE SAVEPOINT add_unique_email;")
                        entry["status"] = "enforced"
                    except psycopg2.Error as exc:
                        # e.g. a duplicate inserted between the check and the CREATE
                        cur.execute("ROLLBACK TO SAVEPOINT add_unique_email;")
                        entry["status"] = "failed"
                        entry["error"] = f"{type(exc).__name__}: {exc}"
            report["constraints"][table] = entry
    if commit:
        conn.commit()
    return report


def is_unique_violation(exc: BaseException) -> bool:
    return getattr(exc, "pgcode", None) == "23505"
