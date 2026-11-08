"""Migration safety tests (isolated local PostgreSQL only; see tests/README.md).

Starts from the schema the current `main` creates (tests/fixtures/legacy_main_schema.sql),
seeds messy legacy data, and runs the real migration code (db_schema.apply_schema and
scripts/check_migration.py) through the local-only psql adapter."""
import os
import subprocess
import sys

from _common import Checks, REPO, fresh_db, load_legacy_schema, require_local_postgres, sql

require_local_postgres()
import psycopg2  # the local-only test adapter (PYTHONPATH set by run_local.sh)
import db_schema

c = Checks("migration tests")
CHECK_SCRIPT = os.path.join(REPO, "scripts", "check_migration.py")

SNAPSHOT = {  # every column the legacy schema has, per table
    "beta_employers": "id, company_name, corporate_hq, admin_email, admin_phone, signature_hash, timestamp",
    "candidate_profiles": "id, name, role, resume, address, current_address, latitude, longitude, status, timestamp",
    "employer_jobs": "id, title, department, pricing_tier, address, latitude, longitude, timestamp",
    "semantic_matches": "id, candidate_email, employer_email, match_score, distance_miles, status, timestamp",
}


def fingerprint(db):
    """Row count + md5 over every original column of every table."""
    return {t: sql(db, f"SELECT count(*) || ':' || coalesce(md5(string_agg(t::text, '|' ORDER BY id)), '-') "
                       f"FROM (SELECT {cols} FROM {t}) t;") for t, cols in SNAPSHOT.items()}


def run_check(*args):
    env = dict(os.environ, INCITY_DB_URL="postgresql://local-test-cluster")
    return subprocess.run([sys.executable, CHECK_SCRIPT, *args], env=env, capture_output=True, text=True)


def insert_fails_unique(conn, statement):
    try:
        with conn.cursor() as cur:
            cur.execute(statement)
        conn.commit()
        return False
    except psycopg2.IntegrityError as exc:
        conn.rollback()
        return exc.pgcode == "23505"


# ================================================================ legacy copy of production
DB = "legacy_copy"
fresh_db(DB)
load_legacy_schema(DB)
sql(DB, """
INSERT INTO beta_employers (company_name, admin_email) VALUES
  ('Legacy Co', 'Hiring@Legacy.com'),
  ('Legacy Co (again)', ' hiring@legacy.COM '),   -- same email: different case + spaces
  ('Other Co', 'other@co.com'),
  ('No Email Co', NULL),
  ('Blank Email Co', ''),
  ('Blank Email Co 2', '   ');
INSERT INTO candidate_profiles (name, role, resume, address, latitude, longitude, status) VALUES
  ('Fallback Person', 'Dev', 'python sql', 'Unknown', 34.5262, -86.9556, 'active_matching_pool'),
  ('Real Person', 'Ops', 'forklift', 'Decatur, AL', 34.6059, -86.9833, 'active_matching_pool');
INSERT INTO employer_jobs (title, department, pricing_tier, address, latitude, longitude) VALUES
  ('Fallback Job', 'Logistics', 'Tier 1', 'Unknown', 34.5262, -86.9556),
  ('Real Job', 'Logistics', 'Tier 1', 'Decatur, AL', 34.6059, -86.9833);
INSERT INTO semantic_matches (candidate_email, employer_email, match_score, distance_miles, status) VALUES
  ('x@y.com', 'recruiter@mycityjobs.us', 80, 0, 'autonomous_pushed_dual');
""")
before = fingerprint(DB)

# 1. Dry run changes nothing
r = run_check()
c.ok("data", r.returncode == 1 and "DRY RUN" in r.stdout and "'hiring@legacy.com' x2" in r.stdout,
     "preflight dry run reports the case/whitespace duplicate and exits 1", r.stdout)
c.ok("data", sql(DB, "SELECT count(*) FROM information_schema.columns WHERE table_name='beta_employers' AND column_name='verified_at'") == "0"
     and fingerprint(DB) == before, "dry run: no schema change, every legacy row byte-identical")

# 2. Real migration with duplicates present
conn = psycopg2.connect("local")
rep = db_schema.apply_schema(conn)
emp, cand = rep["constraints"]["beta_employers"], rep["constraints"]["candidate_profiles"]
c.ok("data", emp["status"] == "skipped_duplicates" and emp["duplicates"][0][:2] == ("hiring@legacy.com", 2),
     "beta_employers: UNIQUE skipped, duplicate group reported (hiring@legacy.com x2)")
c.ok("data", emp["profile"] == {"rows": 6, "null_or_blank_emails": 3, "mixed_case_or_whitespace_emails": 2},
     f"NULL/blank (3) and mixed-case/whitespace (2) emails counted: {emp['profile']}")
c.ok("data", cand["status"] == "enforced" and cand["verification"]["active"], "candidate_profiles: UNIQUE created and verified active in the catalog")
c.ok("data", rep["complete"] is False and emp["verification"]["active"] is False,
     "migration NOT declared complete while beta_employers uniqueness is unverified")
c.ok("data", fingerprint(DB) == before, "no legacy row dropped or altered (row counts + md5 of all original columns identical)")
c.ok("data", rep["legacy_fallback_rows"] == {"candidate_profiles": 1, "employer_jobs": 1}, "historical fallback-point rows detected (1 candidate, 1 job) and kept")
c.ok("data", sql(DB, f"SELECT string_agg(name, ',') FROM candidate_profiles WHERE {db_schema.trusted_location_sql()}") == "Real Person"
     and sql(DB, f"SELECT string_agg(title, ',') FROM employer_jobs j WHERE {db_schema.trusted_location_sql('j')}") == "Real Job",
     "trusted_location_sql() excludes exactly the fallback-point rows")
c.ok("data", sql(DB, "SELECT count(*) FROM beta_employers WHERE verified_at IS NOT NULL") == "0", "existing employers start unverified (new column NULL, nothing overwritten)")
c.ok("data", sql(DB, "SELECT string_agg(DISTINCT application_status, ',') FROM candidate_profiles") == "PENDING"
     and sql(DB, "SELECT string_agg(DISTINCT match_radius_miles::text, ',') FROM employer_jobs") == "25",
     "new columns get defaults (application_status PENDING, radius 25) without touching original values")

r = run_check("--apply")
c.ok("data", r.returncode == 1 and "MIGRATION NOT COMPLETE" in r.stdout, "check_migration --apply refuses to declare completion with duplicates present", r.stdout)

# 3. Idempotent
c.ok("data", db_schema.apply_schema(conn)["constraints"]["beta_employers"]["status"] == "skipped_duplicates" and fingerprint(DB) == before,
     "re-running the migration is safe (idempotent, data unchanged)")

# 4. Database-level enforcement
with conn.cursor() as cur:
    cur.execute("INSERT INTO candidate_profiles (name, contact_email) VALUES ('A', 'jane@x.com');")
conn.commit()
c.ok("security", insert_fails_unique(conn, "INSERT INTO candidate_profiles (name, contact_email) VALUES ('B', '  JANE@x.COM ');"),
     "database rejects '  JANE@x.COM ' after 'jane@x.com' (23505), bypassing the app")
with conn.cursor() as cur:
    cur.execute("INSERT INTO candidate_profiles (name, contact_email) VALUES ('N1', NULL), ('N2', NULL), ('B1', ''), ('B2', ' ');")
conn.commit()
c.ok("data", True, "multiple NULL / blank emails still allowed (partial index)")

# 5. Manual cleanup -> constraint created, verified, probed
sql(DB, "UPDATE beta_employers SET admin_email = 'hiring2@legacy.com' WHERE company_name = 'Legacy Co (again)';")
rep = db_schema.apply_schema(conn)
c.ok("data", rep["complete"] and rep["constraints"]["beta_employers"]["status"] == "enforced",
     "after the duplicate is resolved, the next start creates + verifies UNIQUE; migration complete")
rows_before_probe = sql(DB, "SELECT (SELECT count(*) FROM beta_employers) || '/' || (SELECT count(*) FROM candidate_profiles)")
probe = db_schema.probe_unique_enforcement(conn)
c.ok("security", probe == {"beta_employers": "enforced", "candidate_profiles": "enforced"}, f"behavioural probe: duplicate insert refused on both tables {probe}")
c.ok("data", sql(DB, "SELECT (SELECT count(*) FROM beta_employers) || '/' || (SELECT count(*) FROM candidate_profiles)") == rows_before_probe,
     "probe rolled back (no rows left behind)")
r = run_check("--apply")
c.ok("data", r.returncode == 0 and "MIGRATION COMPLETE" in r.stdout and "Enforcement probe (rolled back): {'beta_employers': 'enforced', 'candidate_profiles': 'enforced'}" in r.stdout,
     "check_migration --apply prints MIGRATION COMPLETE only after catalog verification + probe", r.stdout)

# ================================================================ tampered / wrong indexes are detected and repaired
DB2 = "bad_index"
fresh_db(DB2)
load_legacy_schema(DB2)
sql(DB2, "ALTER TABLE candidate_profiles ADD COLUMN contact_email VARCHAR(255);"
         "CREATE INDEX uq_candidate_profiles_contact_email ON candidate_profiles (contact_email);"  # same name, NOT unique
         "CREATE UNIQUE INDEX uq_beta_employers_admin_email ON beta_employers (admin_email);")      # unique, but case-sensitive
conn2 = psycopg2.connect("local")
with conn2.cursor() as cur:
    cur.execute(db_schema.BASE_SCHEMA_SQL)
    pre_c = db_schema.verify_unique_index(cur, "uq_candidate_profiles_contact_email", "candidate_profiles", "contact_email")
    pre_e = db_schema.verify_unique_index(cur, "uq_beta_employers_admin_email", "beta_employers", "admin_email")
conn2.rollback()
c.ok("security", not pre_c["active"] and "index is not UNIQUE" in pre_c["problems"], "catalog check catches a same-named NON-unique index")
c.ok("security", not pre_e["active"] and any("key expression" in p for p in pre_e["problems"]), "catalog check catches a case-sensitive unique index")
rep2 = db_schema.apply_schema(conn2)
c.ok("data", rep2["complete"], "migration rebuilds both wrong indexes (no duplicates present) and verifies them")
c.ok("security", insert_fails_unique(conn2, "INSERT INTO beta_employers (admin_email) VALUES ('a@b.com'), ('A@B.com');"),
     "rebuilt index enforces case-insensitive uniqueness")

sql(DB2, "DROP INDEX uq_candidate_profiles_contact_email;"
         "CREATE INDEX uq_candidate_profiles_contact_email ON candidate_profiles (contact_email);"  # tampered again
         "INSERT INTO candidate_profiles (contact_email) VALUES ('dup@x.com'), ('DUP@x.com');")
rep3 = db_schema.apply_schema(conn2)
c.ok("data", rep3["constraints"]["candidate_profiles"]["status"] == "invalid_index" and not rep3["complete"],
     "wrong index + duplicates: reported as invalid_index, not complete, nothing dropped")
c.ok("data", sql(DB2, "SELECT count(*) FROM candidate_profiles WHERE lower(contact_email)='dup@x.com'") == "2", "duplicate rows preserved")

# ================================================================ fresh database
fresh_db("fresh_db")
rep4 = db_schema.apply_schema(psycopg2.connect("local"))
c.ok("data", rep4["complete"], "fresh database: both UNIQUE constraints created and verified")
c.ok("data", run_check().returncode == 0, "preflight on a clean database exits 0")

c.summary()
