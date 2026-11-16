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
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
                "AND table_name = %s;", (LEDGER_TABLE,))
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


def record(cur, migration, method, success, started_at, execution_ms, git_commit, detail_json):
    cur.execute(f"""INSERT INTO {LEDGER_TABLE} (version, name, checksum, method, success, started_at, execution_ms,
                                               runner, git_commit, detail)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb);""",
                (migration.version, migration.name, migration.checksum, method, success, started_at,
                 execution_ms, RUNNER, git_commit, detail_json))


def try_lock(cur) -> bool:
    """Transaction-scoped advisory lock: only one migration runner at a time. Never waits."""
    cur.execute("SELECT pg_try_advisory_xact_lock(%s);", (LOCK_KEY,))
    return bool(cur.fetchone()[0])


def schema_verification(cur) -> dict:
    """Actual structure, inside the current transaction (no rollback): everything the app needs
    (db_schema.REQUIRED_*) plus both UNIQUE email constraints verified active in the catalog."""
    missing = db_schema._missing_objects(cur)
    unique = {}
    for index_name, table, column in db_schema.UNIQUE_EMAIL_CONSTRAINTS:
        unique[index_name] = db_schema.verify_unique_index(cur, index_name, table, column)["active"]
    return {"missing": missing, "unique_email_active": unique,
            "ok": not missing and all(unique.values()),
            "checked": {"tables": len(db_schema.REQUIRED_SCHEMA),
                        "columns": sum(len(c) for c in db_schema.REQUIRED_SCHEMA.values()),
                        "indexes": len(db_schema.REQUIRED_INDEXES) + len(db_schema.UNIQUE_EMAIL_CONSTRAINTS),
                        "constraints": len(db_schema.REQUIRED_UNIQUE_CONSTRAINTS) + len(db_schema.REQUIRED_CHECK_CONSTRAINTS),
                        "triggers": len(db_schema.REQUIRED_TRIGGERS)}}
