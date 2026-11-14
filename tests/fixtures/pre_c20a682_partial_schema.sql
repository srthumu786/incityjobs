-- InCityJobs schema as left by the migration code BEFORE commit c20a682 (db_schema.py at 783e39a),
-- i.e. the partly migrated state found on the staging branch on Nov 13, 2026. Apply AFTER
-- legacy_main_schema.sql. Generated from git history; test fixture only.
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

-- Non-unique email indexes from earlier revisions of this branch are replaced by
-- the UNIQUE ones below.
DROP INDEX IF EXISTS idx_beta_employers_admin_email;
DROP INDEX IF EXISTS idx_candidate_profiles_email;
DROP INDEX IF EXISTS idx_candidate_profiles_contact_email;

CREATE UNIQUE INDEX IF NOT EXISTS uq_beta_employers_admin_email ON beta_employers (lower(btrim(admin_email))) WHERE admin_email IS NOT NULL AND btrim(admin_email) <> '';
CREATE UNIQUE INDEX IF NOT EXISTS uq_candidate_profiles_contact_email ON candidate_profiles (lower(btrim(contact_email))) WHERE contact_email IS NOT NULL AND btrim(contact_email) <> '';
