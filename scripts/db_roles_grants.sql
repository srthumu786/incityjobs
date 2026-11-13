-- InCityJobs least-privilege database roles: privileges (PROPOSED, P0).
--
-- Run ONLY through scripts/apply_db_roles.py, which creates the two roles with
-- SQL (so Neon does NOT make them members of neon_superuser), checks that the
-- target is a Neon TEST branch, runs this file in one transaction, and verifies
-- the result. Run as the database owner (neondb_owner on Neon).
--
-- Roles
--   neondb_owner  existing owner. Keeps ownership of all tables. Used ONLY for
--                 migrations and administration, never by the running app.
--   icj_app       the running Streamlit app. Reads and writes rows. Cannot
--                 delete, truncate, change the schema, create objects, or
--                 change the append-only privacy audit log.
--   icj_readonly  owner/auditor review. Reads rows, except the two password
--                 and setup-code hash columns. Cannot write anything.
--
-- New tables added by later migrations get NO privileges automatically; each
-- migration must grant what it needs (deliberate: nothing is exposed by default).

-- Nobody but the owner may create objects in the public schema (already the
-- default on PostgreSQL 15+; stated explicitly so the result is the same everywhere).
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- Start clean so re-running this file always produces exactly the same grants.
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM icj_app, icj_readonly;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM icj_app, icj_readonly;
REVOKE ALL ON SCHEMA public FROM icj_app, icj_readonly;

GRANT USAGE ON SCHEMA public TO icj_app, icj_readonly;
-- CONNECT is granted explicitly (no reliance on PUBLIC's default).
DO $$ BEGIN
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO icj_app, icj_readonly', current_database());
END $$;

-- ---------------------------------------------------------------- icj_app
-- UPDATE is also needed where the app uses SELECT ... FOR UPDATE (row locks).
GRANT SELECT, INSERT, UPDATE ON beta_employers           TO icj_app;  -- registration, verification, password/setup codes
GRANT SELECT, INSERT, UPDATE ON candidate_profiles       TO icj_app;  -- intake, consent (FOR UPDATE)
GRANT SELECT, INSERT         ON employer_jobs            TO icj_app;  -- job posting; jobs are never edited by the app
GRANT SELECT, INSERT         ON semantic_matches         TO icj_app;  -- match ledger: insert-only (ON CONFLICT DO NOTHING)
GRANT SELECT, INSERT, UPDATE ON employer_recommendations TO icj_app;  -- status changes (FOR UPDATE)
GRANT SELECT, INSERT         ON privacy_audit_log        TO icj_app;  -- append-only: no UPDATE or DELETE privilege at all
GRANT INSERT                 ON system_telemetry_logs    TO icj_app;  -- write-only: the app never reads telemetry
-- nextval() for SERIAL/BIGSERIAL ids; USAGE only (no setval)
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO icj_app;

-- ---------------------------------------------------------------- icj_readonly
GRANT SELECT ON candidate_profiles, employer_jobs, semantic_matches,
                employer_recommendations, privacy_audit_log, system_telemetry_logs
  TO icj_readonly;
-- beta_employers: every column EXCEPT password_hash and setup_code_hash
DO $$
DECLARE cols text;
BEGIN
  SELECT string_agg(quote_ident(column_name), ', ' ORDER BY ordinal_position) INTO cols
  FROM information_schema.columns
  WHERE table_schema = 'public' AND table_name = 'beta_employers'
    AND column_name NOT IN ('password_hash', 'setup_code_hash');
  EXECUTE format('GRANT SELECT (%1$s) ON beta_employers TO icj_readonly', cols);
END $$;
