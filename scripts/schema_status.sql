-- READ-ONLY schema status for the InCityJobs database (staging or local test). Shows the
-- connection, databases, all tables, and one marker per schema commit. Lists names only, never rows.
-- The read-only transaction makes PostgreSQL refuse any change. Run in the Neon SQL Editor
-- (branch selector = the TEST branch) or with: psql "$INCITY_DB_URL" -f scripts/schema_status.sql
BEGIN TRANSACTION READ ONLY;
SELECT * FROM (
  SELECT 1 AS n, 'connection' AS item,
         current_database() || ' | schema ' || current_schema() || ' | user ' || current_user || ' | search_path ' || current_setting('search_path') AS detail
  UNION ALL SELECT 2, 'databases on this branch', string_agg(datname, ', ' ORDER BY datname) FROM pg_database WHERE NOT datistemplate
  UNION ALL SELECT 3, 'tables, all schemas', coalesce(string_agg(table_schema || '.' || table_name, ', ' ORDER BY table_schema, table_name), 'none')
         FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog', 'information_schema') AND table_type = 'BASE TABLE'
  UNION ALL SELECT 3 + m.k, 'marker ' || m.label, CASE WHEN m.ok THEN 'present' ELSE 'MISSING' END FROM (VALUES
    (1, '1 base tables (Sep 2026)', to_regclass('public.beta_employers') IS NOT NULL AND to_regclass('public.candidate_profiles') IS NOT NULL
                                    AND to_regclass('public.employer_jobs') IS NOT NULL AND to_regclass('public.semantic_matches') IS NOT NULL),
    (2, '2 telemetry table (2282c70, Nov 8)', to_regclass('public.system_telemetry_logs') IS NOT NULL),
    (3, '3 employer passwords (263836d, Nov 8)', EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'beta_employers' AND column_name = 'password_hash')),
    (4, '4 match ledger unique index (263836d, Nov 8)', to_regclass('public.uq_semantic_matches_candidate_job') IS NOT NULL),
    (5, '5 employer verification (566c2f0, Nov 8)', EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'beta_employers' AND column_name = 'verified_at')),
    (6, '6 unique email indexes (Nov 8)', to_regclass('public.uq_beta_employers_admin_email') IS NOT NULL AND to_regclass('public.uq_candidate_profiles_contact_email') IS NOT NULL),
    (7, '7 consent columns (c20a682, Nov 10)', EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'candidate_profiles' AND column_name = 'employer_matching_consent')),
    (8, '8 recommendations table (c20a682, Nov 10)', to_regclass('public.employer_recommendations') IS NOT NULL),
    (9, '9 privacy audit log table (c20a682, Nov 10)', to_regclass('public.privacy_audit_log') IS NOT NULL),
    (10, '10 audit append-only triggers (c20a682, Nov 10)', EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'privacy_audit_log_append_only'))
  ) AS m(k, label, ok)
  UNION ALL SELECT 14, 'recommendation or audit tables in any schema',
         coalesce(string_agg(n.nspname || '.' || c.relname, ', '), 'none')
         FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE c.relkind = 'r' AND c.relname IN ('employer_recommendations', 'privacy_audit_log')
) d ORDER BY n;
ROLLBACK;
