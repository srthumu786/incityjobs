"""Versioned migrations and the migration ledger for InCityJobs (B01-T1).

No Streamlit imports. Used ONLY by the administrator tool scripts/migrate.py; app.py never
imports this module and never migrates (it runs db_schema.check_schema, read-only).

Migrations, in version order:
  1  baseline_schema  the schema db_schema.apply_schema() creates (BASE_SCHEMA_SQL plus the
                      verified UNIQUE email constraints). Frozen: its checksum is pinned below.
  2+ migrations/NNNN_short_name.sql files, one transaction-safe SQL script each.
New schema changes go in a new migrations/NNNN_*.sql file, never into BASE_SCHEMA_SQL (editing
it changes the baseline checksum, which every ledgered database then reports as MODIFIED).

The ledger (table schema_migration_ledger) keeps one row per attempt: version, name, checksum,
method (applied or baselined), success, start and finish time, duration, database role, runner,
git commit and a JSON detail (verification summary, or the error type and SQLSTATE: never row
data). At most one successful row per version (partial UNIQUE index). It is append-only:
UPDATE, DELETE and TRUNCATE are rejected by triggers.

Integrity rules (validate_code, validate_ledger): code versions must be 1..N without gaps or
duplicates, file names well-formed and files non-empty and transaction-safe; every successful
ledger row must match a code migration of the same version, name and checksum (else MISSING or
MODIFIED); applied versions must be exactly 1..k (else OUT OF ORDER); a version may succeed only
once (else DUPLICATE).
"""
import hashlib
import os
import re
from dataclasses import dataclass

import db_schema

LEDGER_TABLE = "schema_migration_ledger"
RUNNER = "scripts/migrate.py v1"
# pg_advisory_xact_lock key shared by every migration runner (transaction-scoped, so it also
# works through Neon's transaction-mode pooler and is released at commit or rollback).
LOCK_KEY = 4_712_202_611_150_001
MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")
FILE_NAME = re.compile(r"^(\d{4})_([a-z0-9]+(?:_[a-z0-9]+)*)\.sql$")
# Statements that would break the single all-or-nothing transaction.
UNSAFE_SQL = re.compile(r"(?im)^\s*(BEGIN|COMMIT|ROLLBACK|END|START\s+TRANSACTION|SAVEPOINT|RELEASE|PREPARE\s+TRANSACTION|"
                        r"VACUUM|CREATE\s+DATABASE|DROP\s+DATABASE|ALTER\s+SYSTEM)\b|\bCONCURRENTLY\b")

BASELINE_VERSION, BASELINE_NAME = 1, "baseline_schema"


def baseline_definition() -> str:
    """Canonical text of migration 1: the SQL and the UNIQUE email index definitions."""
    indexes = "\n".join(f"UNIQUE INDEX {name} ON {table} ({db_schema.email_key(column)}) "
                        f"WHERE {column} IS NOT NULL AND btrim({column}) <> ''"
                        for name, table, column in db_schema.UNIQUE_EMAIL_CONSTRAINTS)
    return f"-- InCityJobs migration 1: {BASELINE_NAME}\n{db_schema.BASE_SCHEMA_SQL}\n{indexes}\n"


def checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Pinned: tests fail if BASE_SCHEMA_SQL or the UNIQUE constraints change without a new migration.
BASELINE_CHECKSUM = "da87102ce1ebf9349d17918c7762cac15d0ff7785fab663c4793ea274b78b5b8"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    checksum: str
    sql: str = ""          # empty for the baseline (run by db_schema.apply_schema)
    source: str = ""       # file name, or "db_schema.py" for the baseline

    @property
    def is_baseline(self):
        return self.version == BASELINE_VERSION


def baseline() -> Migration:
    return Migration(BASELINE_VERSION, BASELINE_NAME, checksum(baseline_definition()), source="db_schema.py")


def load_code_migrations(directory=MIGRATIONS_DIR):
    """(migrations in version order, problems). Problems mean the code itself is inconsistent;
    nothing may run until they are fixed."""
    problems, found = [], {}
    names = sorted(os.listdir(directory)) if os.path.isdir(directory) else []
    for file_name in names:
        if not file_name.endswith(".sql"):
            continue
        m = FILE_NAME.match(file_name)
        if not m:
            problems.append(f"badly named migration file {file_name!r} (expected NNNN_short_name.sql)")
            continue
        version = int(m.group(1))
        if version == BASELINE_VERSION:
            problems.append(f"DUPLICATE: {file_name} uses version 1, which is the baseline schema")
            continue
        found.setdefault(version, []).append(file_name)
    migrations = [baseline()]
    if migrations[0].checksum != BASELINE_CHECKSUM:
        problems.append("MODIFIED in code: the baseline schema (db_schema.BASE_SCHEMA_SQL or the UNIQUE email "
                        "constraints) changed. Put schema changes in a new migrations/NNNN_*.sql file instead")
    for version in sorted(found):
        files = found[version]
        if len(files) > 1:
            problems.append(f"DUPLICATE: version {version} is defined by {', '.join(files)}")
            continue
        text = open(os.path.join(directory, files[0]), encoding="utf-8").read()
        if not text.strip():
            problems.append(f"{files[0]} is empty")
            continue
        if UNSAFE_SQL.search(text):
            problems.append(f"{files[0]} contains transaction control or a statement that cannot run inside "
                            "one transaction (BEGIN/COMMIT/ROLLBACK/SAVEPOINT/VACUUM/CONCURRENTLY/...)")
            continue
        migrations.append(Migration(version, FILE_NAME.match(files[0]).group(2), checksum(text), text, files[0]))
    versions = [m.version for m in migrations]
    expected = list(range(1, (max(found) if found else 1) + 1))
    gaps = sorted(set(expected) - set(versions) - {v for v in found if len(found[v]) > 1})
    if gaps and not problems:
        problems.append(f"MISSING in code: version(s) {gaps} (versions must run 1, 2, 3, ... without gaps)")
    return migrations, problems


def validate_ledger(code, rows):
    """Compare successful ledger rows [(version, name, checksum, method)] with the code migrations.
    Returns (pending migrations, problems)."""
    problems = []
    by_version = {m.version: m for m in code}
    applied = {}
    for version, name, digest, _method in rows:
        if version in applied:
            problems.append(f"DUPLICATE: version {version} has more than one successful ledger entry")
            continue
        applied[version] = (name, digest)
        migration = by_version.get(version)
        if migration is None:
            problems.append(f"MISSING: version {version} ({name}) is recorded as applied but is not in this code "
                            "(the code is older than the database, or a migration file was removed)")
        elif digest != migration.checksum or name != migration.name:
            problems.append(f"MODIFIED: version {version} ({migration.name}) differs from what was applied "
                            f"(ledger checksum {digest[:12]}..., code {migration.checksum[:12]}...)")
    if applied:
        top = max(applied)
        holes = [v for v in range(1, top + 1) if v not in applied]
        if holes:
            problems.append(f"OUT OF ORDER: version(s) {holes} not applied although version {top} is")
    pending = [m for m in code if m.version not in applied]
    if applied and pending and min(m.version for m in pending) < max(applied):
        if not any(p.startswith("OUT OF ORDER") for p in problems):
            problems.append(f"OUT OF ORDER: version {min(m.version for m in pending)} is pending below applied "
                            f"version {max(applied)}")
    return pending, problems


# --------------------------------------------------------------------------- ledger table
LEDGER_SQL = f"""CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (
    id BIGSERIAL PRIMARY KEY,
    version INTEGER NOT NULL CHECK (version > 0),
    name VARCHAR(100) NOT NULL,
    checksum CHAR(64) NOT NULL CHECK (checksum ~ '^[0-9a-f]{{64}}$'),
    method VARCHAR(16) NOT NULL CHECK (method IN ('applied', 'baselined')),
    success BOOLEAN NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    execution_ms INTEGER CHECK (execution_ms >= 0),
    applied_by VARCHAR(128) NOT NULL DEFAULT current_user,
    runner VARCHAR(64) NOT NULL,
    git_commit VARCHAR(40),
    database_name VARCHAR(128) NOT NULL DEFAULT current_database(),
    detail JSONB NOT NULL DEFAULT '{{}}'::jsonb,
    CHECK (finished_at >= started_at)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_{LEDGER_TABLE}_version_success ON {LEDGER_TABLE} (version) WHERE success;
CREATE OR REPLACE FUNCTION incity_reject_ledger_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '{LEDGER_TABLE} is append-only' USING ERRCODE = 'insufficient_privilege';
END;
$$;
CREATE OR REPLACE TRIGGER {LEDGER_TABLE}_append_only
    BEFORE UPDATE OR DELETE ON {LEDGER_TABLE} FOR EACH ROW EXECUTE FUNCTION incity_reject_ledger_change();
CREATE OR REPLACE TRIGGER {LEDGER_TABLE}_no_truncate
    BEFORE TRUNCATE ON {LEDGER_TABLE} FOR EACH STATEMENT EXECUTE FUNCTION incity_reject_ledger_change();
"""
LEDGER_COLUMNS = ("id", "version", "name", "checksum", "method", "success", "started_at", "finished_at",
                  "execution_ms", "applied_by", "runner", "git_commit", "database_name", "detail")


def ledger_exists(cur) -> bool:
    cur.execute("SELECT to_regclass(%s) IS NOT NULL;", (f"{LEDGER_TABLE}",))
    return bool(cur.fetchone()[0])


def ledger_problems(cur) -> list:
    """The ledger table must have its columns, its one-success-per-version index and both
    append-only triggers enabled, or it can't be trusted."""
    problems = []
    cur.execute("""SELECT a.attname FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid
                   JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname = current_schema() AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped;""",
                (LEDGER_TABLE,))
    have = {r[0] for r in cur.fetchall()}
    missing = [c for c in LEDGER_COLUMNS if c not in have]
    if missing:
        problems.append(f"ledger table lacks column(s) {missing}")
    cur.execute("""SELECT i.indisunique AND i.indisvalid, coalesce(pg_get_expr(i.indpred, i.indrelid), '')
                   FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
                   JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname = current_schema() AND c.relname = %s;""", (f"uq_{LEDGER_TABLE}_version_success",))
    row = cur.fetchone()
    if not row or not row[0] or "success" not in row[1]:
        problems.append("ledger one-success-per-version UNIQUE index missing or invalid")
    cur.execute("""SELECT count(*) FROM pg_trigger WHERE tgrelid = to_regclass(%s) AND NOT tgisinternal
                   AND tgenabled IN ('O', 'A') AND tgname IN (%s, %s);""",
                (LEDGER_TABLE, f"{LEDGER_TABLE}_append_only", f"{LEDGER_TABLE}_no_truncate"))
    if cur.fetchone()[0] != 2:
        problems.append("ledger append-only triggers missing or disabled")
    return problems


def ledger_rows(cur):
    cur.execute(f"SELECT version, name, checksum, method FROM {LEDGER_TABLE} WHERE success ORDER BY version, id;")
    return [(int(v), str(n), str(c).strip(), str(m)) for v, n, c, m in cur.fetchall()]


def db_clock(cur) -> str:
    """The DATABASE server's current time (clock_timestamp(), not the transaction start), as
    text. Every ledger time comes from here, never from the administrator's computer."""
    cur.execute("SELECT clock_timestamp()::text;")
    return str(cur.fetchone()[0])


def record(cur, migration, method, success, started_at, finished_at, git_commit, detail_json):
    """One ledger row. started_at / finished_at are db_clock() values; the duration is
    computed by the database from them."""
    cur.execute(f"""INSERT INTO {LEDGER_TABLE} (version, name, checksum, method, success, started_at, finished_at,
                                               execution_ms, runner, git_commit, detail)
                    VALUES (%s, %s, %s, %s, %s, %s::timestamptz, %s::timestamptz,
                            round(extract(epoch FROM (%s::timestamptz - %s::timestamptz)) * 1000)::integer,
                            %s, %s, %s::jsonb);""",
                (migration.version, migration.name, migration.checksum, method, success, started_at, finished_at,
                 finished_at, started_at, RUNNER, git_commit, detail_json))


def try_lock(cur) -> bool:
    """Transaction-scoped advisory lock: only one migration runner at a time. Never waits."""
    cur.execute("SELECT pg_try_advisory_xact_lock(%s);", (LOCK_KEY,))
    return bool(cur.fetchone()[0])


# --------------------------------------------------------------------------- privileges
def privilege_problems(cur, mode, ledger_present) -> list:
    """What the connecting role lacks for `mode` ('status', 'apply' or 'baseline'). Reported
    separately from missing schema objects. Catalog functions only."""
    problems = []
    cur.execute("SELECT current_user, has_schema_privilege(current_schema(), 'CREATE');")
    role, can_create = cur.fetchone()
    if ledger_present:
        cur.execute("SELECT has_table_privilege(%s, 'SELECT'), has_table_privilege(%s, 'INSERT');",
                    (LEDGER_TABLE, LEDGER_TABLE))
        can_read, can_insert = cur.fetchone()
        if not can_read:
            problems.append(f"role {role!r} cannot read the ledger {LEDGER_TABLE}")
        if mode != "status" and not can_insert:
            problems.append(f"role {role!r} cannot write the ledger {LEDGER_TABLE}")
    if mode == "status":
        return problems
    if not can_create and (mode == "apply" or not ledger_present):
        problems.append(f"role {role!r} has no CREATE privilege on schema {_schema(cur)!r} "
                        "(needed for the ledger and for new objects)")
    if mode == "apply":
        cur.execute("""SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                       WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p') AND c.relname = ANY (string_to_array(%s, ','))
                         AND NOT pg_has_role(current_user, c.relowner, 'USAGE') ORDER BY 1;""",
                    (",".join(db_schema.REQUIRED_SCHEMA),))
        not_owned = [r[0] for r in cur.fetchall()]
        if not_owned:
            problems.append(f"role {role!r} does not own (and is not a member of the owner of) {', '.join(not_owned)}; "
                            "migrations must run as the table owner")
    return problems


def _schema(cur):
    cur.execute("SELECT current_schema();")
    return cur.fetchone()[0]


# --------------------------------------------------------------------------- version-1 definitions
# EXACT catalog definition of every object version 1 (db_schema.BASE_SCHEMA_SQL plus the UNIQUE
# email indexes) creates, generated from a freshly migrated database and identical for a migrated
# legacy copy and a migrated partly-migrated copy (tests/test_migration_ledger.py keeps it in step).
# These are checks of the EXISTING schema only; they add no constraint to any table. Constraints a
# future migration might add are listed separately in docs/MIGRATION_LEDGER.md, not checked here.
# Extra columns, indexes or constraints on these tables (e.g. a legacy column) are reported, not refused.
VERSION1_COLUMNS = {  # table: {column: (type, NOT NULL, default)}
    'beta_employers': {
        'admin_email': ('character varying(255)', False, None),
        'admin_phone': ('character varying(50)', False, None),
        'company_name': ('character varying(255)', False, None),
        'corporate_hq': ('text', False, None),
        'id': ('integer', True, "nextval('beta_employers_id_seq'::regclass)"),
        'password_hash': ('text', False, None),
        'setup_code_expires_at': ('timestamp with time zone', False, None),
        'setup_code_hash': ('character varying(64)', False, None),
        'signature_hash': ('character varying(64)', False, None),
        'timestamp': ('timestamp without time zone', False, 'CURRENT_TIMESTAMP'),
        'verified_at': ('timestamp with time zone', False, None),
        'verified_by': ('character varying(255)', False, None),
    },
    'candidate_profiles': {
        'address': ('text', False, None),
        'application_status': ('character varying(32)', True, "'PENDING'::character varying"),
        'contact_disclosure_consent': ('boolean', True, 'false'),
        'contact_email': ('character varying(255)', False, None),
        'current_address': ('text', False, None),
        'employer_matching_consent': ('boolean', True, 'false'),
        'employer_matching_consent_at': ('timestamp with time zone', False, None),
        'employer_matching_consent_policy': ('character varying(64)', False, None),
        'id': ('integer', True, "nextval('candidate_profiles_id_seq'::regclass)"),
        'latitude': ('double precision', False, None),
        'longitude': ('double precision', False, None),
        'name': ('character varying(255)', False, None),
        'resume': ('text', False, None),
        'resume_version': ('integer', True, '1'),
        'role': ('character varying(255)', False, None),
        'status': ('character varying(50)', False, None),
        'timestamp': ('timestamp without time zone', False, 'CURRENT_TIMESTAMP'),
    },
    'employer_jobs': {
        'address': ('text', False, None),
        'department': ('character varying(100)', False, None),
        'employer_id': ('integer', False, None),
        'external_tracking_id': ('character varying(128)', False, None),
        'id': ('integer', True, "nextval('employer_jobs_id_seq'::regclass)"),
        'latitude': ('double precision', False, None),
        'longitude': ('double precision', False, None),
        'match_radius_miles': ('integer', True, '25'),
        'pricing_tier': ('character varying(100)', False, None),
        'requirements': ('text', False, None),
        'source_channel': ('character varying(64)', False, None),
        'timestamp': ('timestamp without time zone', False, 'CURRENT_TIMESTAMP'),
        'title': ('character varying(255)', False, None),
    },
    'employer_recommendations': {
        'available_at': ('timestamp with time zone', False, None),
        'candidate_profile_id': ('integer', True, None),
        'created_at': ('timestamp with time zone', True, 'now()'),
        'distance_miles': ('real', False, None),
        'employer_id': ('integer', True, None),
        'employer_ref': ('character varying(16)', True, None),
        'fingerprint': ('character(64)', True, None),
        'id': ('bigint', True, "nextval('employer_recommendations_id_seq'::regclass)"),
        'is_new': ('boolean', True, 'false'),
        'job_id': ('integer', True, None),
        'notified_fingerprint': ('character(64)', False, None),
        'profile': ('jsonb', True, None),
        'score': ('real', False, None),
        'status': ('character varying(16)', True, None),
        'status_reason': ('character varying(64)', False, None),
        'updated_at': ('timestamp with time zone', True, 'now()'),
        'viewed_at': ('timestamp with time zone', False, None),
        'viewed_fingerprint': ('character(64)', False, None),
        'withdrawn_at': ('timestamp with time zone', False, None),
    },
    'privacy_audit_log': {
        'actor_ref': ('character varying(64)', False, None),
        'actor_type': ('character varying(16)', True, None),
        'candidate_profile_id': ('integer', False, None),
        'created_at': ('timestamp with time zone', True, 'now()'),
        'detail': ('jsonb', True, "'{}'::jsonb"),
        'employer_id': ('integer', False, None),
        'event_type': ('character varying(40)', True, None),
        'id': ('bigint', True, "nextval('privacy_audit_log_id_seq'::regclass)"),
        'job_id': ('integer', False, None),
        'recommendation_id': ('bigint', False, None),
    },
    'semantic_matches': {
        'candidate_email': ('character varying(255)', False, None),
        'candidate_profile_id': ('integer', False, None),
        'distance_miles': ('double precision', False, None),
        'employer_email': ('character varying(255)', False, None),
        'id': ('integer', True, "nextval('semantic_matches_id_seq'::regclass)"),
        'job_id': ('integer', False, None),
        'match_score': ('double precision', False, None),
        'payload': ('jsonb', False, None),
        'status': ('character varying(100)', False, None),
        'timestamp': ('timestamp without time zone', False, 'CURRENT_TIMESTAMP'),
    },
    'system_telemetry_logs': {
        'diagnostic_error_code': ('character varying(32)', True, None),
        'error_context_vector': ('text', True, None),
        'error_detail': ('text', False, None),
        'id': ('integer', True, "nextval('system_telemetry_logs_id_seq'::regclass)"),
        'timestamp': ('timestamp without time zone', False, 'CURRENT_TIMESTAMP'),
    },
}
VERSION1_CONSTRAINTS = {  # (table, kind, definition). Constraint names are not compared.
    ('beta_employers', 'primary key', 'PRIMARY KEY (id)'),
    ('candidate_profiles', 'primary key', 'PRIMARY KEY (id)'),
    ('employer_jobs', 'foreign key', 'FOREIGN KEY (employer_id) REFERENCES beta_employers(id) ON DELETE SET NULL'),
    ('employer_jobs', 'primary key', 'PRIMARY KEY (id)'),
    ('employer_recommendations', 'check', "CHECK (((status)::text = ANY ((ARRAY['QUEUED'::character varying, 'AVAILABLE'::character varying, 'VIEWED'::character varying, 'WITHDRAWN'::character varying])::text[])))"),
    ('employer_recommendations', 'foreign key', 'FOREIGN KEY (candidate_profile_id) REFERENCES candidate_profiles(id) ON DELETE CASCADE'),
    ('employer_recommendations', 'foreign key', 'FOREIGN KEY (employer_id) REFERENCES beta_employers(id) ON DELETE CASCADE'),
    ('employer_recommendations', 'foreign key', 'FOREIGN KEY (job_id) REFERENCES employer_jobs(id) ON DELETE CASCADE'),
    ('employer_recommendations', 'primary key', 'PRIMARY KEY (id)'),
    ('employer_recommendations', 'unique', 'UNIQUE (employer_ref)'),
    ('employer_recommendations', 'unique', 'UNIQUE (job_id, candidate_profile_id)'),
    ('privacy_audit_log', 'check', "CHECK (((actor_type)::text = ANY ((ARRAY['system'::character varying, 'candidate'::character varying, 'employer'::character varying, 'manager'::character varying])::text[])))"),
    ('privacy_audit_log', 'primary key', 'PRIMARY KEY (id)'),
    ('semantic_matches', 'foreign key', 'FOREIGN KEY (job_id) REFERENCES employer_jobs(id) ON DELETE SET NULL'),
    ('semantic_matches', 'primary key', 'PRIMARY KEY (id)'),
    ('system_telemetry_logs', 'primary key', 'PRIMARY KEY (id)'),
}
VERSION1_INDEXES = {  # index name: definition (indexes behind a constraint are covered above)
    'idx_employer_recommendations_candidate':
        'CREATE INDEX idx_employer_recommendations_candidate ON employer_recommendations USING btree (candidate_profile_id)',
    'idx_employer_recommendations_employer_status':
        'CREATE INDEX idx_employer_recommendations_employer_status ON employer_recommendations USING btree (employer_id, status)',
    'idx_privacy_audit_log_candidate':
        'CREATE INDEX idx_privacy_audit_log_candidate ON privacy_audit_log USING btree (candidate_profile_id, created_at)',
    'idx_privacy_audit_log_recommendation':
        'CREATE INDEX idx_privacy_audit_log_recommendation ON privacy_audit_log USING btree (recommendation_id)',
    'uq_beta_employers_admin_email':
        "CREATE UNIQUE INDEX uq_beta_employers_admin_email ON beta_employers USING btree (lower(btrim((admin_email)::text))) WHERE ((admin_email IS NOT NULL) AND (btrim((admin_email)::text) <> ''::text))",
    'uq_candidate_profiles_contact_email':
        "CREATE UNIQUE INDEX uq_candidate_profiles_contact_email ON candidate_profiles USING btree (lower(btrim((contact_email)::text))) WHERE ((contact_email IS NOT NULL) AND (btrim((contact_email)::text) <> ''::text))",
    'uq_semantic_matches_candidate_job':
        'CREATE UNIQUE INDEX uq_semantic_matches_candidate_job ON semantic_matches USING btree (candidate_profile_id, job_id) WHERE ((candidate_profile_id IS NOT NULL) AND (job_id IS NOT NULL))',
}
VERSION1_TRIGGERS = {  # trigger name: definition (also required: enabled, not replica-only)
    'privacy_audit_log_append_only':
        'CREATE TRIGGER privacy_audit_log_append_only BEFORE DELETE OR UPDATE ON privacy_audit_log FOR EACH ROW EXECUTE FUNCTION incity_reject_audit_change()',
    'privacy_audit_log_no_truncate':
        'CREATE TRIGGER privacy_audit_log_no_truncate BEFORE TRUNCATE ON privacy_audit_log FOR EACH STATEMENT EXECUTE FUNCTION incity_reject_audit_change()',
}
VERSION1_FUNCTIONS = {  # function name: body (whitespace-normalized)
    "incity_reject_audit_change":
        "BEGIN RAISE EXCEPTION 'privacy_audit_log is append-only' USING ERRCODE = 'insufficient_privilege'; END;",
}


def _norm(text, schema):
    return " ".join(str(text).replace(f"{schema}.", "").split())


def version1_definition_problems(cur):
    """(problems, extras): every version-1 column type / NOT NULL / default, primary key,
    foreign key (with its ON DELETE rule), UNIQUE and CHECK definition, index definition,
    trigger definition and the audit function body, compared with VERSION1_*."""
    schema = _schema(cur)
    problems, extras = [], []
    cur.execute("""SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
                          pg_get_expr(d.adbin, d.adrelid)
                   FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid JOIN pg_namespace n ON n.oid = c.relnamespace
                   LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
                   WHERE n.nspname = current_schema() AND c.relkind IN ('r', 'p') AND a.attnum > 0 AND NOT a.attisdropped
                     AND c.relname = ANY (string_to_array(%s, ','));""", (",".join(VERSION1_COLUMNS),))
    found = {(t, c): (str(typ), bool(nn), None if d is None else _norm(d, schema)) for t, c, typ, nn, d in cur.fetchall()}
    for table, columns in VERSION1_COLUMNS.items():
        for column, expected in columns.items():
            actual = found.get((table, column))
            if actual is None:
                problems.append(f"missing column {table}.{column}")
                continue
            for label, want, have in zip(("type", "NOT NULL", "default"), expected, actual):
                if want != have:
                    problems.append(f"column {table}.{column}: {label} is {have!r}, version 1 defines {want!r}")
        extras += [f"extra column {t}.{c}" for (t, c) in found if t == table and c not in columns]

    cur.execute("""SELECT t.relname, CASE con.contype WHEN 'p' THEN 'primary key' WHEN 'f' THEN 'foreign key'
                                      WHEN 'u' THEN 'unique' ELSE 'check' END,
                          pg_get_constraintdef(con.oid), con.conname, con.convalidated
                   FROM pg_constraint con JOIN pg_class t ON t.oid = con.conrelid
                   JOIN pg_namespace n ON n.oid = t.relnamespace
                   WHERE n.nspname = current_schema() AND t.relname = ANY (string_to_array(%s, ',')) AND con.contype IN ('p', 'f', 'u', 'c');""",
                (",".join(VERSION1_COLUMNS),))
    have = {}
    for table, kind, definition, name, validated in cur.fetchall():
        definition = re.sub(r"\s+NOT VALID$", "", str(definition))   # reported through `validated` instead
        have[(table, kind, _norm(definition, schema))] = (name, validated)
    kinds = {"primary key": "primary key", "foreign key": "foreign key", "unique": "UNIQUE constraint", "check": "CHECK constraint"}
    for table, kind, definition in sorted(VERSION1_CONSTRAINTS):
        key = (table, kind, _norm(definition, schema))
        if key not in have:
            problems.append(f"missing or different {kinds[kind]} on {table}: version 1 defines {definition}")
        elif not have[key][1]:
            problems.append(f"{kinds[kind]} {have[key][0]} on {table} is NOT VALID (not checked against existing rows)")
    expected_keys = {(t, k, _norm(d, schema)) for t, k, d in VERSION1_CONSTRAINTS}
    extras += [f"extra {kinds[k]} on {t}: {d}" for (t, k, d) in have if (t, k, d) not in expected_keys]

    cur.execute("""SELECT c.relname, pg_get_indexdef(i.indexrelid), i.indisvalid AND i.indisready AND i.indislive
                   FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid JOIN pg_class t ON t.oid = i.indrelid
                   JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname = current_schema() AND t.relname = ANY (string_to_array(%s, ','))
                     AND NOT EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conindid = i.indexrelid);""",
                (",".join(VERSION1_COLUMNS),))
    indexes = {name: (_norm(d, schema), usable) for name, d, usable in cur.fetchall()}
    for name, definition in VERSION1_INDEXES.items():
        actual = indexes.get(name)
        if actual is None:
            problems.append(f"missing index {name}")
        elif actual[0] != _norm(definition, schema):
            problems.append(f"index {name} is {actual[0]!r}, version 1 defines {definition!r}")
        elif not actual[1]:
            problems.append(f"index {name} is not valid/ready/live")
    extras += [f"extra index {n}" for n in indexes if n not in VERSION1_INDEXES]

    cur.execute("""SELECT tg.tgname, pg_get_triggerdef(tg.oid), tg.tgenabled::text FROM pg_trigger tg
                   JOIN pg_class c ON c.oid = tg.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
                   WHERE n.nspname = current_schema() AND NOT tg.tgisinternal AND c.relname = ANY (string_to_array(%s, ','));""",
                (",".join(VERSION1_COLUMNS),))
    triggers = {name: (_norm(d, schema), enabled) for name, d, enabled in cur.fetchall()}
    for name, definition in VERSION1_TRIGGERS.items():
        actual = triggers.get(name)
        if actual is None:
            problems.append(f"missing trigger {name}")
        elif actual[0] != _norm(definition, schema):
            problems.append(f"trigger {name} is {actual[0]!r}, version 1 defines {definition!r}")
        elif actual[1] not in ("O", "A"):
            problems.append(f"trigger {name} is not enabled")
    extras += [f"extra trigger {n}" for n in triggers if n not in VERSION1_TRIGGERS]

    for name, body in VERSION1_FUNCTIONS.items():
        cur.execute("""SELECT p.prosrc, p.prorettype = 'trigger'::regtype, l.lanname FROM pg_proc p
                       JOIN pg_namespace n ON n.oid = p.pronamespace JOIN pg_language l ON l.oid = p.prolang
                       WHERE n.nspname = current_schema() AND p.proname = %s;""", (name,))
        row = cur.fetchone()
        if row is None:
            problems.append(f"missing function {name}")
        elif _norm(row[0], schema) != body or not row[1] or row[2] != "plpgsql":
            problems.append(f"function {name} differs from version 1 (body, return type or language)")
    return problems, sorted(extras)


def schema_verification(cur, strict_version1=False) -> dict:
    """Actual structure, inside the current transaction (no rollback): everything the app needs
    (db_schema.REQUIRED_*) plus both UNIQUE email constraints verified active in the catalog.
    With strict_version1 (baselining, or a database whose newest version is 1), also the exact
    version-1 definitions (version1_definition_problems)."""
    missing = db_schema._missing_objects(cur)
    unique = {}
    for index_name, table, column in db_schema.UNIQUE_EMAIL_CONSTRAINTS:
        unique[index_name] = db_schema.verify_unique_index(cur, index_name, table, column)["active"]
    definition, extras = version1_definition_problems(cur) if strict_version1 else ([], [])
    return {"missing": missing, "unique_email_active": unique, "definition_problems": definition, "extras": extras,
            "strict_version1": strict_version1,
            "ok": not missing and not definition and all(unique.values()),
            "checked": {"tables": len(db_schema.REQUIRED_SCHEMA),
                        "columns": sum(len(c) for c in db_schema.REQUIRED_SCHEMA.values()),
                        "indexes": len(db_schema.REQUIRED_INDEXES) + len(db_schema.UNIQUE_EMAIL_CONSTRAINTS),
                        "constraints": len(db_schema.REQUIRED_UNIQUE_CONSTRAINTS) + len(db_schema.REQUIRED_CHECK_CONSTRAINTS),
                        "triggers": len(db_schema.REQUIRED_TRIGGERS),
                        "version1_definitions": (sum(len(c) for c in VERSION1_COLUMNS.values()) + len(VERSION1_CONSTRAINTS)
                                                 + len(VERSION1_INDEXES) + len(VERSION1_TRIGGERS) + len(VERSION1_FUNCTIONS))
                                                if strict_version1 else 0}}
