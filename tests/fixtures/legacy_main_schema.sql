-- Schema exactly as created by app.py on `main` at commit 0039934 (the version
-- currently deployed), extracted in source order. Used by tests/test_migration.py
-- and tests/test_e2e.py as the starting point for migration tests.
-- The old Phase 4 'ADD COLUMN IF NOT EXISTS email' only ran after a manual button
-- click, so it may or may not exist in production; tests cover the case without it.

CREATE TABLE IF NOT EXISTS beta_employers (
id SERIAL PRIMARY KEY,
company_name VARCHAR(255),
corporate_hq TEXT,
admin_email VARCHAR(255),
admin_phone VARCHAR(50),
signature_hash VARCHAR(64),
timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS candidate_profiles (
id SERIAL PRIMARY KEY,
name VARCHAR(255),
role VARCHAR(255),
resume TEXT,
address TEXT,
current_address TEXT,
latitude FLOAT,
longitude FLOAT,
status VARCHAR(50),
timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS current_address TEXT;

CREATE TABLE IF NOT EXISTS employer_jobs (
id SERIAL PRIMARY KEY,
title VARCHAR(255),
department VARCHAR(100),
pricing_tier VARCHAR(100),
address TEXT,
latitude FLOAT,
longitude FLOAT,
timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS latitude FLOAT;

ALTER TABLE candidate_profiles ADD COLUMN IF NOT EXISTS longitude FLOAT;

CREATE TABLE IF NOT EXISTS semantic_matches (
id SERIAL PRIMARY KEY,
candidate_email VARCHAR(255),
employer_email VARCHAR(255),
match_score FLOAT,
distance_miles FLOAT,
status VARCHAR(100),
timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
