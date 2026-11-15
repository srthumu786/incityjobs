-- READ-ONLY schema fingerprint for the InCityJobs database (staging or local test).
-- One line per kind of schema object (count + md5 of its definitions), then a TOTAL line.
-- Covers schemas, tables and views (owner, grants), columns (type, NOT NULL, default),
-- indexes, constraints, triggers (definition and enabled state), functions, sequences
-- (owner and grants, NOT their current values), extensions and event triggers.
-- Reads the system catalog only: never row data, so it shows no personal information.
--
-- Run it before and after an operation that must not change the schema (e.g. starting the
-- app): identical output means no schema object was created, altered or dropped. Run in the
-- Neon SQL Editor (branch selector = the TEST branch) or with:
--   psql "$INCITY_DB_URL" -f scripts/schema_fingerprint.sql
-- The read-only transaction makes PostgreSQL refuse any change.
BEGIN TRANSACTION READ ONLY;
WITH nsp AS (
  SELECT oid, nspname FROM pg_namespace
  WHERE nspname NOT IN ('pg_catalog', 'information_schema') AND nspname NOT LIKE 'pg_toast%' AND nspname NOT LIKE 'pg_temp%'
), items(kind, def) AS (
  SELECT 'schemas', n.nspname || ' owner=' || pg_get_userbyid(s.nspowner) || ' acl=' || coalesce(s.nspacl::text, '')
    FROM nsp n JOIN pg_namespace s ON s.oid = n.oid
  UNION ALL
  SELECT 'relations', n.nspname || '.' || c.relname || ' kind=' || c.relkind::text || ' owner=' || pg_get_userbyid(c.relowner)
         || ' acl=' || coalesce(c.relacl::text, '') || ' rls=' || c.relrowsecurity::text
    FROM pg_class c JOIN nsp n ON n.oid = c.relnamespace WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f', 'S')
  UNION ALL
  SELECT 'columns', n.nspname || '.' || c.relname || '.' || a.attname || ' ' || format_type(a.atttypid, a.atttypmod)
         || CASE WHEN a.attnotnull THEN ' NOT NULL' ELSE '' END
         || coalesce(' DEFAULT ' || pg_get_expr(d.adbin, d.adrelid), '') || ' acl=' || coalesce(a.attacl::text, '')
    FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN nsp n ON n.oid = c.relnamespace
    LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
    WHERE c.relkind IN ('r', 'p', 'v', 'm', 'f') AND a.attnum > 0 AND NOT a.attisdropped
  UNION ALL
  SELECT 'indexes', pg_get_indexdef(i.indexrelid) || ' valid=' || i.indisvalid::text || ' ready=' || i.indisready::text
    FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid JOIN nsp n ON n.oid = c.relnamespace
  UNION ALL
  SELECT 'constraints', n.nspname || '.' || con.conrelid::regclass::text || ' ' || con.conname || ' '
         || pg_get_constraintdef(con.oid) || ' validated=' || con.convalidated::text
    FROM pg_constraint con JOIN nsp n ON n.oid = con.connamespace
  UNION ALL
  SELECT 'triggers', pg_get_triggerdef(t.oid) || ' enabled=' || t.tgenabled::text
    FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN nsp n ON n.oid = c.relnamespace WHERE NOT t.tgisinternal
  UNION ALL
  SELECT 'functions', pg_get_functiondef(p.oid) || ' owner=' || pg_get_userbyid(p.proowner) || ' acl=' || coalesce(p.proacl::text, '')
    FROM pg_proc p JOIN nsp n ON n.oid = p.pronamespace WHERE p.prokind IN ('f', 'p')
  UNION ALL
  SELECT 'extensions', e.extname || ' ' || e.extversion FROM pg_extension e
  UNION ALL
  SELECT 'event triggers', e.evtname || ' ' || e.evtevent || ' enabled=' || e.evtenabled::text FROM pg_event_trigger e
), kinds(kind) AS (
  VALUES ('schemas'), ('relations'), ('columns'), ('indexes'), ('constraints'), ('triggers'), ('functions'),
         ('extensions'), ('event triggers')
)
SELECT k.kind || '|' || count(i.def) || '|' || coalesce(md5(string_agg(i.def, E'\n' ORDER BY i.def)), '-')
FROM kinds k LEFT JOIN items i ON i.kind = k.kind GROUP BY k.kind
UNION ALL
SELECT 'TOTAL|' || count(*) || '|' || md5(string_agg(kind || ':' || def, E'\n' ORDER BY kind, def)) FROM items
ORDER BY 1;
ROLLBACK;
