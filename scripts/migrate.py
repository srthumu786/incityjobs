"""Guarded, ledgered migration runner for InCityJobs (B01-T1). ADMINISTRATOR USE ONLY.

The app never runs this and never migrates. Run it against a Neon *test branch* or a local
database after explicit authorization, never against production (it refuses the production
endpoint). Same target verification and TLS rules as scripts/check_migration.py.

    read -rs INCITY_DB_URL                           # TEST branch owner string, sslmode=verify-full (hidden)
    read -rs NEON_API_KEY && export NEON_API_KEY     # hidden; revoke it afterwards
    T="--neon-project-id <id> --ca-file <CA bundle> --production-endpoint ep-PROD --expect-endpoint ep-TEST --expect-branch <name>"
    python scripts/migrate.py $T --status             # read-only: ledger, code, pending, problems
    python scripts/migrate.py $T                      # dry run of the pending migrations (always rolled back)
    python scripts/migrate.py $T --apply              # apply the pending migrations
    python scripts/migrate.py $T --baseline           # dry run: record an ALREADY-migrated database as version 1
    python scripts/migrate.py $T --baseline --apply   # ... and commit that record

Nothing is committed without --apply. Migrations are defined in db_migrations.py and migrations/.

Before connecting: the code's migrations are checked (versions 1..N, no duplicates or gaps,
pinned baseline checksum, transaction-safe files) and the target is verified (Neon host,
sslmode=verify-full with --ca-file, endpoint ids, Neon's API confirming the branch). Then, in ONE
transaction (lock_timeout 10 s, statement_timeout 120 s):
  1. a transaction-scoped advisory lock: if another runner holds it, exit 4 at once;
  2. the connecting role's privileges are checked (ledger access; for --apply also CREATE on the
     schema and ownership of the app tables; for --baseline CREATE): if lacking, exit 6
     (INSUFFICIENT PRIVILEGE), reported separately from missing schema objects;
  3. a fence lock (SHARE UPDATE EXCLUSIVE on beta_employers, 3 s wait): it does not block the
     app's reads and writes, but it conflicts with any DDL on that table, so the older
     scripts/check_migration.py (whose migration always alters beta_employers) cannot change the
     schema at the same time; if the fence can't be taken, exit 4;
  4. the ledger is checked (structure, one success per version, append-only triggers) and
     compared with the code: MODIFIED, MISSING, OUT OF ORDER or DUPLICATE -> exit 5, nothing run;
  5. --apply / dry run: every pending migration runs in version order, then the actual schema is
     verified (everything the app needs, UNIQUE email constraints active) and, with --apply, a
     duplicate-insert probe proves enforcement; one ledger row per migration is written; only if
     everything passed does --apply commit, otherwise all of it is rolled back;
     --baseline: nothing is run; the actual schema is verified first and only if it is complete
     is version 1 recorded (method 'baselined'). The baseline check is STRICT: every version-1
     column type, NOT NULL rule and default, primary and foreign key (with its ON DELETE rule),
     UNIQUE and CHECK definition, index and trigger definition and the audit function body
     must match db_migrations.VERSION1_* exactly (extra legacy objects are reported, not refused). A database with no ledger whose schema is already
     complete must be baselined: --apply refuses to re-run the baseline on it.
After a migration fails during --apply, a second transaction records one ledger row with
success = false (error type and SQLSTATE only), creating the ledger if it had been rolled back
with the failed transaction. Failures before any migration ran (privileges, locks, integrity)
are reported but not recorded. After a commit, a read-only re-verification runs.
All ledger times come from the DATABASE clock (clock_timestamp()), never this computer's clock.

Exit codes:
    0  done (or dry run: would succeed; status: up to date)
    1  not complete or an error before commit: nothing committed (status: pending work or no ledger)
    2  usage error, or refused by target verification (nothing contacted)
    3  committed, but the read-only re-verification failed (the commit stands; do not re-run;
       restore from the backup branch or report the output)
    4  another migration runner holds the lock: nothing done
    5  ledger or code integrity problem (modified, missing, out-of-order, duplicate): nothing run
    6  insufficient privilege for the connecting role: nothing done
    7  schema drift or deviation: the database differs from what the ledger last recorded, or (no
       ledger yet) existing objects differ from version 1. Nothing done. Resolve it explicitly:
       revert the change, or re-run with --accept-drift (ledgered; recorded with the next
       migration) or --repair-version1-drift (no ledger; lets version 1 overwrite what it defines,
       recorded) after the deviation has been reviewed and approved.
       Both options require SEPARATE ADMINISTRATIVE APPROVAL for each use, and must NOT be used
       on Neon staging or production without explicit written authorization for that database.

Drift detection: after every successful migration or baseline the ledger row records a catalog
snapshot of the schema (db_migrations.catalog_snapshot: tables, columns, constraints, indexes,
triggers, functions). --status and --apply compare the live catalog with the newest snapshot;
any difference is reported before anything runs.
"""
import argparse
import json
import os
import subprocess
import sys

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(SCRIPTS)
sys.path[:0] = [p for p in (SCRIPTS, REPO) if p not in sys.path]
import psycopg2  # noqa: E402
import check_migration as guard  # noqa: E402  (target verification, TLS, redacted output, probe)
import db_migrations as dbm  # noqa: E402
import db_schema  # noqa: E402

FENCE_TABLE = "beta_employers"   # check_migration.py's migration always runs ALTER TABLE on it first


class Refusal(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def git_commit():
    """(commit sha or None, working tree has uncommitted changes)."""
    try:
        sha = subprocess.run(["git", "-C", REPO, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10).stdout.strip()
        dirty = bool(subprocess.run(["git", "-C", REPO, "status", "--porcelain", "--untracked-files=no"],
                                    capture_output=True, text=True, timeout=10).stdout.strip())
        return (sha if len(sha) == 40 else None), dirty
    except (OSError, subprocess.SubprocessError):
        return None, False


def start(cur, read_only=False):
    if read_only:
        cur.execute("SET TRANSACTION READ ONLY;")
    cur.execute("SET LOCAL lock_timeout = '10s';")
    cur.execute("SET LOCAL statement_timeout = '120s';")


def error_type(exc):
    code = getattr(exc, "pgcode", None)
    return type(exc).__name__ + (f" (SQLSTATE {code})" if code else "")


def read_state(cur, code):
    """(ledger exists, successful rows, pending, integrity problems)."""
    if not dbm.ledger_exists(cur):
        return False, [], list(code), []
    structure = dbm.ledger_problems(cur)
    rows = dbm.ledger_rows(cur) if not any("lacks column" in p for p in structure) else []
    pending, integrity = dbm.validate_ledger(code, rows)
    return True, rows, pending, structure + integrity


def say_verification(say, v):
    say(f"Schema verification{' (strict version-1 definitions)' if v['strict_version1'] else ''}: "
        f"{'complete' if v['ok'] else 'NOT complete'} (checked {json.dumps(v['checked'])}; "
        f"UNIQUE email constraints active: {v['unique_email_active']})")
    for item in v["missing"]:
        say(f"   missing: {item}")
    for item in v["definition_problems"]:
        say(f"   version-1 definition: {item}")
    for item in v["extras"]:
        say(f"   note (allowed): {item}")


def preflight(cur, args, say, mode):
    """Database, advisory lock, privileges and (for changes) the cross-tool fence."""
    cur.execute("SELECT current_database();")
    database = cur.fetchone()[0]
    if database != args.expect_database:
        raise Refusal(2, f"Refused: connected to database {database!r}, expected {args.expect_database!r}. Nothing was changed.")
    if mode != "status" and not dbm.try_lock(cur):
        raise Refusal(4, "REFUSED: another migration runner holds the migration lock. Nothing was done. "
                         "Wait for it to finish, then run --status.")
    problems = dbm.privilege_problems(cur, mode, dbm.ledger_exists(cur))
    if problems:
        raise Refusal(6, "INSUFFICIENT PRIVILEGE (this is not a schema problem): " + "; ".join(problems)
                      + ". Nothing was done. Run as the database owner (administrator) role.")
    if mode != "status":
        cur.execute("SELECT to_regclass(%s) IS NOT NULL;", (FENCE_TABLE,))
        if cur.fetchone()[0]:
            cur.execute("SAVEPOINT icj_fence;")
            try:
                cur.execute("SET LOCAL lock_timeout = '3s';")
                cur.execute(f"LOCK TABLE {FENCE_TABLE} IN SHARE UPDATE EXCLUSIVE MODE;")
                cur.execute("SET LOCAL lock_timeout = '10s';")
                cur.execute("RELEASE SAVEPOINT icj_fence;")
            except psycopg2.Error as exc:
                if getattr(exc, "pgcode", None) != "55P03":
                    raise
                raise Refusal(4, f"REFUSED: another session is changing the schema of {FENCE_TABLE} (for example "
                                 "scripts/check_migration.py --apply). Nothing was done. Wait for it to finish, then run --status.")


def strict_for(code, rows_after):
    """Exact version-1 definitions apply while the newest applied version is 1."""
    versions = [r[0] for r in rows_after] or [1]
    return max(versions) == 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--status", action="store_true", help="read-only report; changes nothing")
    mode.add_argument("--baseline", action="store_true", help="record an already-migrated database as version 1 (with --apply)")
    parser.add_argument("--apply", action="store_true", help="commit (default: dry run, always rolled back)")
    parser.add_argument("--db-url", default=os.environ.get("INCITY_DB_URL"), help="defaults to $INCITY_DB_URL")
    parser.add_argument("--neon-project-id", required=True)
    parser.add_argument("--production-endpoint", required=True, help="the PRODUCTION endpoint id (ep-...); always refused")
    parser.add_argument("--expect-endpoint", required=True, help="the TEST branch endpoint id (ep-...)")
    parser.add_argument("--expect-branch", required=True, help="the TEST branch name, e.g. staging-test-gate")
    parser.add_argument("--expect-database", default="neondb")
    parser.add_argument("--ca-file", required=True, help="trusted CA bundle (PEM) for PostgreSQL and the Neon API")
    parser.add_argument("--migrations-dir", default=dbm.MIGRATIONS_DIR, help="default: the repository's migrations/")
    parser.add_argument("--accept-drift", action="store_true",
                        help="ledgered database whose schema differs from the last recorded snapshot: run the pending "
                             "migrations anyway and record the accepted differences. Requires separate administrative approval "
                             "for each use; never on Neon staging or production without explicit authorization")
    parser.add_argument("--repair-version1-drift", action="store_true",
                        help="database WITHOUT a ledger whose existing objects differ from version 1: let the version-1 "
                             "migration overwrite what it defines and record the deviations. Requires separate administrative "
                             "approval for each use; never on Neon staging or production without explicit authorization")
    args = parser.parse_args()
    if args.status and args.apply:
        parser.error("--status is read-only; it cannot be combined with --apply")
    if (args.accept_drift or args.repair_version1_drift) and (args.status or args.baseline):
        parser.error("--accept-drift / --repair-version1-drift apply only to a dry run or --apply of pending migrations")
    api_key = os.environ.get("NEON_API_KEY")
    say = guard.Output((guard.db_password(args.db_url), api_key))

    code, code_problems = dbm.load_code_migrations(args.migrations_dir)
    say(f"Code migrations ({os.path.relpath(args.migrations_dir, REPO) if args.migrations_dir.startswith(REPO) else args.migrations_dir}): "
        + ", ".join(f"{m.version} {m.name} [{m.checksum[:12]}]" for m in code))
    if code_problems:
        for p in code_problems:
            say(f"   code problem: {p}")
        say("\nREFUSED: the code's migrations are inconsistent. Nothing was contacted or changed.")
        return 5
    if not args.db_url:
        say("Set INCITY_DB_URL or pass --db-url (use a Neon test branch, not production).")
        return 2
    ca_file = os.path.abspath(os.path.expanduser(args.ca_file))
    refused = guard.static_guard(args.db_url, args.production_endpoint, args.expect_endpoint, ca_file)
    if not refused:
        refused, facts = guard.api_guard(args.db_url, args.neon_project_id, args.production_endpoint,
                                         args.expect_endpoint, args.expect_branch, api_key, ca_file)
    if refused:
        say(f"Refused before connecting: {refused}. Nothing was contacted or changed.")
        return 2
    say(f"Target endpoint: {guard.endpoint_of(args.db_url)}; Neon confirms branch {facts['branch']!r}, not default, "
        f"not primary; production endpoint serves {facts['production_branch_id']}. TLS: sslmode={guard.REQUIRED_SSLMODE}.")
    sha, dirty = git_commit()

    try:
        conn = psycopg2.connect(args.db_url, connect_timeout=10, sslmode=guard.REQUIRED_SSLMODE, sslrootcert=ca_file)
    except psycopg2.Error as exc:
        say(f"Could not connect: {guard.describe_error(exc)}. Nothing was changed.")
        return 1
    try:
        return run(conn, args, code, say, sha, dirty)
    except Refusal as refusal:
        conn.rollback()
        say(refusal.message)
        return refusal.code
    finally:
        conn.close()   # an uncommitted transaction is rolled back by PostgreSQL when the connection closes


def privilege_exit(conn, exc, say):
    conn.rollback()
    say(f"INSUFFICIENT PRIVILEGE (this is not a schema problem): {guard.describe_error(exc)}. Nothing was committed.")
    return 6


def run(conn, args, code, say, sha, dirty):
    # ---------------------------------------------------------------- status (read-only)
    if args.status:
        try:
            with conn.cursor() as cur:
                start(cur, read_only=True)
                preflight(cur, args, say, "status")
                exists, rows, pending, problems = read_state(cur, code)
                verification = dbm.schema_verification(cur, strict_version1=strict_for(code, rows))
                drift_lines, has_snapshot = dbm.drift(cur) if exists and not problems else ([], False)
        except psycopg2.Error as exc:
            if getattr(exc, "pgcode", None) == "42501":
                return privilege_exit(conn, exc, say)
            raise
        conn.rollback()
        say(f"Ledger: {'present' if exists else 'none'}; applied: {[r[0] for r in rows] or 'none'}; "
            f"pending: {[m.version for m in pending] or 'none'}")
        say_verification(say, verification)
        if problems:
            for p in problems:
                say(f"   ledger problem: {p}")
            say("\nSTATUS: INTEGRITY PROBLEM. Nothing may run until it is resolved.")
            return 5
        if exists:
            say("Recorded schema snapshot: " + ("matches the live schema" if has_snapshot and not drift_lines else
                                                "DIFFERS from the live schema" if drift_lines else "none recorded yet"))
        if drift_lines:
            for line in drift_lines:
                say(f"   drift: {line}")
            say("\nSTATUS: SCHEMA DRIFT: the schema was changed outside the migration runner since the last recorded "
                "migration. Nothing may run until it is resolved (revert it, or --accept-drift after approval).")
            return 7
        if verification["deviations"]:
            say("\nSTATUS: SCHEMA DEVIATION from version 1: existing objects differ (listed above).")
            return 7
        if not exists:
            say("\nSTATUS: no ledger. " + ("The schema is complete: record it with --baseline (dry run first)."
                                          if verification["ok"] else "The schema is not complete: run the migrations (dry run first)."))
            return 1
        if pending or not verification["ok"]:
            say("\nSTATUS: pending migrations or incomplete schema.")
            return 1
        say("\nSTATUS: up to date.")
        return 0

    # ---------------------------------------------------------------- one transaction
    applying = args.apply
    failed = None   # (migration, error type, exception, database start time) when a migration raised
    try:
        with conn.cursor() as cur:
            start(cur)
            preflight(cur, args, say, "baseline" if args.baseline else "apply")
            exists, rows, pending, problems = read_state(cur, code)
            if problems:
                conn.rollback()
                for p in problems:
                    say(f"   ledger problem: {p}")
                say("\nREFUSED: ledger integrity problem. No migration was run; nothing was changed.")
                return 5

            if args.baseline:
                return baseline(conn, cur, args, code, rows, exists, say, sha, dirty)

            accepted, repaired = [], []
            if rows:
                drift_lines, _ = dbm.drift(cur)
                if drift_lines:
                    for line in drift_lines:
                        say(f"   drift: {line}")
                    if not args.accept_drift:
                        conn.rollback()
                        say("\nREFUSED: SCHEMA DRIFT. The schema differs from the snapshot recorded with the last migration "
                            "(changed outside the migration runner). No migration was run; nothing was changed. Revert the "
                            "change, or re-run with --accept-drift after the difference has been reviewed and approved.")
                        return 7
                    accepted = drift_lines
                    say("Proceeding with --accept-drift: the differences above will be recorded with this migration.")
            if not rows:
                verification = dbm.schema_verification(cur, strict_version1=True)   # BEFORE anything runs
                say_verification(say, verification)
                if verification["ok"]:
                    conn.rollback()
                    say("\nREFUSED: this database has no ledger but its schema is already complete. Record it with "
                        "--baseline instead of re-running the baseline migration. Nothing was changed.")
                    return 1
                if verification["deviations"]:
                    for item in verification["deviations"]:
                        say(f"   DEVIATION (exists but differs from version 1): {item}")
                    if not args.repair_version1_drift:
                        conn.rollback()
                        say("\nREFUSED: existing objects differ from version 1 (listed above). They are not repaired "
                            "silently. No migration was run; nothing was changed. Correct them by an approved change, or "
                            "re-run with --repair-version1-drift after review to let version 1 overwrite what it defines.")
                        return 7
                    repaired = verification["deviations"]
                    say("Proceeding with --repair-version1-drift: version 1 will overwrite what it defines; the "
                        "deviations above will be recorded.")
            if not pending:
                conn.rollback()
                say("Nothing to do: every migration in the code is applied.")
                return 0
            if not exists:
                cur.execute(dbm.LEDGER_SQL)
            say(f"Pending: {', '.join(f'{m.version} {m.name}' for m in pending)}")
            times, report = {}, None
            for migration in pending:
                started = dbm.db_clock(cur)
                try:
                    cur.execute(f"SAVEPOINT icj_migration_{migration.version};")
                    if migration.is_baseline:
                        report = db_schema.apply_schema(conn, commit=False)
                        if not report["complete"]:
                            raise RuntimeError("UNIQUE email constraints cannot be enforced (see report)")
                    else:
                        cur.execute(migration.sql)
                    cur.execute(f"RELEASE SAVEPOINT icj_migration_{migration.version};")
                except Exception as exc:
                    failed = (migration, error_type(exc), exc, started)
                    raise
                times[migration.version] = (started, dbm.db_clock(cur))
            verification = dbm.schema_verification(cur, strict_version1=strict_for(code, rows + [(m.version,) for m in pending]))
            probe = guard.probe_in_transaction(conn) if applying else None
            proven = verification["ok"] and (not applying or (probe and all(v == "enforced" for v in probe.values())))
            snapshot = dbm.snapshot_record(cur)
            for migration in pending:
                detail = {"verification": {k: verification[k] for k in ("ok", "checked", "strict_version1")},
                          "probe": probe, "uncommitted_changes_in_checkout": dirty, "batch": [m.version for m in pending]}
                if accepted:
                    detail["accepted_drift"] = accepted
                if repaired:
                    detail["repaired_version1_deviations"] = repaired
                if migration is pending[-1]:
                    detail["schema_snapshot"] = snapshot
                dbm.record(cur, migration, "applied", True, *times[migration.version], sha, json.dumps(detail))
            if report is not None:
                guard.print_report(say, report, applying)
            say_verification(say, verification)
            if probe is not None:
                say(f"Enforcement probe (inside the transaction, rolled back): {probe}")
            if not applying:
                conn.rollback()
                say("\nDRY RUN: " + ("every pending migration ran and verified; rolled back, nothing changed."
                                     if verification["ok"] else "the schema would NOT be complete; rolled back, nothing changed."))
                return 0 if verification["ok"] else 1
            if not proven:
                conn.rollback()
                say("\nNOT APPLIED: verification or probe failed; the transaction was rolled back, nothing changed.")
                return 1
            conn.commit()
    except Refusal:
        raise
    except Exception as exc:
        if getattr(failed[2] if failed else exc, "pgcode", None) == "42501":
            code_ = privilege_exit(conn, failed[2] if failed else exc, say)
            if applying and failed:
                record_failure(conn, failed, sha, say)
            return code_
        conn.rollback()
        if failed:
            migration, kind, original, _ = failed
            say(f"ERROR in migration {migration.version} {migration.name}: {guard.describe_error(original)}")
        else:
            say(f"ERROR before commit: {guard.describe_error(exc)}")
        say("\nNOT APPLIED: nothing was committed; the transaction was rolled back.")
        if applying and failed:
            record_failure(conn, failed, sha, say)
        return 1

    problems = post_commit(conn, code)
    say(f"\nCommitted: {', '.join(f'{m.version} {m.name}' for m in pending)}.")
    if problems:
        say(f"COMMITTED, BUT RE-VERIFICATION FAILED: {problems}. The commit stands and cannot be undone "
            "automatically. Do not re-run. Restore from the backup branch or report this output.")
        return 3
    say("APPLIED AND RE-VERIFIED: ledger matches the code, schema complete.")
    return 0


def baseline(conn, cur, args, code, rows, exists, say, sha, dirty):
    if rows:
        conn.rollback()
        say(f"REFUSED: the ledger already records version(s) {[r[0] for r in rows]}; a baseline is only for a database "
            "with no ledger entries. Use --apply for pending migrations. Nothing was changed.")
        return 5
    verification = dbm.schema_verification(cur, strict_version1=True)   # actual structure, BEFORE anything is recorded
    say_verification(say, verification)
    if not verification["ok"]:
        conn.rollback()
        say("\nREFUSED: the schema does not match version 1, so it cannot be recorded as migrated. Nothing was "
            "recorded or created. Correct the differences above (a separate, approved change), or run the migrations "
            "if objects are missing (dry run first).")
        return 1
    if not exists:
        cur.execute(dbm.LEDGER_SQL)
    moment = dbm.db_clock(cur)
    dbm.record(cur, code[0], "baselined", True, moment, moment, sha,
               json.dumps({"verification": {k: verification[k] for k in ("ok", "checked", "strict_version1")},
                           "unique_email_active": verification["unique_email_active"],
                           "allowed_extras": verification["extras"],
                           "schema_snapshot": dbm.snapshot_record(cur),
                           "uncommitted_changes_in_checkout": dirty,
                           "note": "existing schema verified and recorded; no migration SQL was run"}))
    later = [m.version for m in code[1:]]
    if not args.apply:
        conn.rollback()
        say(f"\nDRY RUN: version 1 would be recorded as baselined. Pending afterwards: {later or 'none'}. Nothing changed.")
        return 0
    conn.commit()
    problems = post_commit(conn, code[:1])
    if problems:
        say(f"\nBASELINE COMMITTED, BUT RE-VERIFICATION FAILED: {problems}. Do not re-run; report this output.")
        return 3
    say(f"\nBASELINED: version 1 recorded after verifying the existing schema. Pending: {later or 'none'}.")
    return 0


def record_failure(conn, failed, sha, say):
    """After the rollback: one success = false row for the failed attempt, in its own transaction
    (re-creating the ledger if its creation was rolled back with the failed migration)."""
    migration, kind, _, started = failed
    try:
        with conn.cursor() as cur:
            start(cur)
            if not dbm.try_lock(cur):
                conn.rollback()
                say("Failure not recorded: another runner took the lock.")
                return
            cur.execute(dbm.LEDGER_SQL)
            dbm.record(cur, migration, "applied", False, started, dbm.db_clock(cur), sha,
                       json.dumps({"error": kind, "rolled_back": True}))
        conn.commit()
        say(f"Recorded the failed attempt in {dbm.LEDGER_TABLE} (success = false; {kind}).")
    except psycopg2.Error as exc:
        conn.rollback()
        say(f"Failure not recorded: {guard.describe_error(exc)}")


def post_commit(conn, code):
    """Read-only re-verification after a commit."""
    try:
        with conn.cursor() as cur:
            start(cur, read_only=True)
            exists, rows, pending, problems = read_state(cur, code)
            verification = dbm.schema_verification(cur, strict_version1=strict_for(code, rows))
            drift_lines, _ = dbm.drift(cur) if exists and not problems else ([], False)
        conn.rollback()
    except psycopg2.Error as exc:
        conn.rollback()
        return [f"re-verification could not run: {guard.describe_error(exc)}"]
    out = list(problems) + [f"drift: {d}" for d in drift_lines]
    if not exists:
        out.append("ledger missing")
    if pending:
        out.append(f"still pending: {[m.version for m in pending]}")
    out += [f"missing: {m}" for m in verification["missing"]]
    out += [f"version-1 definition: {m}" for m in verification["definition_problems"]]
    out += [f"{name} not active" for name, ok in verification["unique_email_active"].items() if not ok]
    return out


if __name__ == "__main__":
    sys.exit(main())
