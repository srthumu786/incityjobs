"""Create, verify or remove the InCityJobs least-privilege roles on a Neon TEST branch.

PROPOSED (P0). For a Neon test branch or a local database only. It refuses to
run unless you name the production endpoint and the target is a different one.

    export INCITY_DB_URL="postgresql://neondb_owner:...@ep-TEST...neon.tech/neondb?sslmode=require"
    python scripts/apply_db_roles.py --production-endpoint ep-PROD...           # plan: read-only report
    python scripts/apply_db_roles.py --production-endpoint ep-PROD... --apply   # create roles + grants, then verify
    python scripts/apply_db_roles.py --production-endpoint ep-PROD... --verify  # verify only
    python scripts/apply_db_roles.py --production-endpoint ep-PROD... --remove  # roll back: drop both roles

Roles are created with SQL on purpose: Neon gives roles created in its Console,
CLI or API membership in neon_superuser (administrator rights); roles created
with SQL get only what is granted. On Neon a role belongs to the branch it was
created on, so creating them on the test branch does not touch production.

--apply generates strong random passwords on YOUR computer and writes the two
connection strings to a private file (permissions 600) outside the repository.
Nothing is printed except the file's path. Never commit or paste that file.

Exit codes: 0 success / plan shown; 1 verification failed; 2 refused or usage error.
"""
import argparse
import os
import secrets
import string
import sys
from urllib.parse import quote, urlsplit, urlunsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg2  # noqa: E402

ROLES = ("icj_app", "icj_readonly")
GRANTS_SQL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "db_roles_grants.sql")
TABLES = ("beta_employers", "candidate_profiles", "employer_jobs", "semantic_matches",
          "employer_recommendations", "privacy_audit_log", "system_telemetry_logs")
PRIVS = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")

# The complete expected table-privilege matrix. Anything not listed must be ABSENT.
EXPECTED = {
    "icj_app": {
        "beta_employers": {"SELECT", "INSERT", "UPDATE"},
        "candidate_profiles": {"SELECT", "INSERT", "UPDATE"},
        "employer_jobs": {"SELECT", "INSERT"},
        "semantic_matches": {"SELECT", "INSERT"},
        "employer_recommendations": {"SELECT", "INSERT", "UPDATE"},
        "privacy_audit_log": {"SELECT", "INSERT"},
        "system_telemetry_logs": {"INSERT"},
    },
    # beta_employers is column-level for icj_readonly (checked separately)
    "icj_readonly": {t: ({"SELECT"} if t != "beta_employers" else set()) for t in TABLES},
}
HIDDEN_FROM_READONLY = ("password_hash", "setup_code_hash")


def endpoint_of(url: str) -> str:
    host = urlsplit(url).hostname or ""
    return host.split(".")[0].replace("-pooler", "")


def new_password() -> str:
    # 40 characters, at least one of each class (well above Neon's strength rule)
    alphabet = string.ascii_letters + string.digits + "-_"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(40))
        if any(c.islower() for c in pw) and any(c.isupper() for c in pw) and any(c.isdigit() for c in pw) \
                and any(c in "-_" for c in pw):
            return pw


def role_url(owner_url: str, role: str, password: str) -> str:
    parts = urlsplit(owner_url)
    host = parts.hostname + (f":{parts.port}" if parts.port else "")
    return urlunsplit((parts.scheme, f"{quote(role)}:{quote(password)}@{host}", parts.path, parts.query, ""))


def existing_roles(cur) -> set:
    cur.execute("SELECT rolname FROM pg_roles WHERE rolname IN (%s, %s);", ROLES)
    return {r[0] for r in cur.fetchall()}


def verify(cur) -> list:
    """Return a list of problems; empty means the roles match the design exactly."""
    problems = []
    present = existing_roles(cur)
    for role in ROLES:
        if role not in present:
            problems.append(f"{role}: missing")
    if problems:
        return problems
    cur.execute("""
        SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, rolinherit, rolcanlogin
        FROM pg_roles WHERE rolname IN (%s, %s) ORDER BY rolname;""", ROLES)
    for name, sup, cdb, crole, repl, bypass, inherit, login in cur.fetchall():
        if sup or cdb or crole or repl or bypass:
            problems.append(f"{name}: has an administrative attribute (superuser/createdb/createrole/replication/bypassrls)")
        if inherit:
            problems.append(f"{name}: INHERIT should be off")
        if not login:
            problems.append(f"{name}: cannot log in")
    cur.execute("""
        SELECT r.rolname, g.rolname FROM pg_auth_members m
        JOIN pg_roles r ON r.oid = m.member JOIN pg_roles g ON g.oid = m.roleid
        WHERE r.rolname IN (%s, %s);""", ROLES)
    for member, group in cur.fetchall():
        problems.append(f"{member}: is a member of {group} (must belong to no role)")
    for role in ROLES:
        cur.execute("SELECT has_schema_privilege(%s, 'public', 'CREATE'), "
                    "has_database_privilege(%s, current_database(), 'CREATE'), "
                    "has_database_privilege(%s, current_database(), 'TEMPORARY');", (role, role, role))
        schema_create, db_create, _temp = cur.fetchone()
        if schema_create or db_create:
            problems.append(f"{role}: can create objects (schema or database CREATE)")
        cur.execute("SELECT count(*) FROM pg_class c JOIN pg_roles r ON r.oid = c.relowner WHERE r.rolname = %s;", (role,))
        if cur.fetchone()[0]:
            problems.append(f"{role}: owns database objects")
        for table in TABLES:
            cur.execute("SELECT to_regclass(%s) IS NOT NULL;", (f"public.{table}",))
            if not cur.fetchone()[0]:
                problems.append(f"table {table} missing: run the schema migration first")
                continue
            for priv in PRIVS:
                cur.execute("SELECT has_table_privilege(%s, %s, %s);", (role, f"public.{table}", priv))
                has = cur.fetchone()[0]
                want = priv in EXPECTED[role][table]
                if has != want:
                    problems.append(f"{role} {priv} on {table}: {'granted' if has else 'missing'}, expected {'granted' if want else 'absent'}")
        cur.execute("""SELECT count(*) FROM information_schema.sequences s
                       WHERE s.sequence_schema = 'public'
                         AND (has_sequence_privilege(%s, s.sequence_schema || '.' || s.sequence_name, 'UPDATE')
                              OR has_sequence_privilege(%s, s.sequence_schema || '.' || s.sequence_name, 'SELECT'));""",
                    (role, role))
        if cur.fetchone()[0]:
            problems.append(f"{role}: has SELECT/UPDATE (setval) on a sequence; only USAGE is allowed")
    cur.execute("""SELECT count(*) FROM information_schema.sequences s WHERE s.sequence_schema = 'public'
                     AND NOT has_sequence_privilege('icj_app', s.sequence_schema || '.' || s.sequence_name, 'USAGE');""")
    if cur.fetchone()[0]:
        problems.append("icj_app: missing USAGE on an id sequence (inserts would fail)")
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema='public' AND table_name='beta_employers';")
    for (col,) in cur.fetchall():
        cur.execute("SELECT has_column_privilege('icj_readonly', 'public.beta_employers', %s, 'SELECT');", (col,))
        has = cur.fetchone()[0]
        if has == (col in HIDDEN_FROM_READONLY):
            problems.append(f"icj_readonly SELECT on beta_employers.{col}: {'granted' if has else 'missing'}, "
                            f"expected {'absent' if col in HIDDEN_FROM_READONLY else 'granted'}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db-url", default=os.environ.get("INCITY_DB_URL"), help="owner connection string of the TEST branch (default $INCITY_DB_URL)")
    ap.add_argument("--production-endpoint", required=True, help="your PRODUCTION endpoint id (ep-...), used to refuse production")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--verify", action="store_true")
    mode.add_argument("--remove", action="store_true")
    ap.add_argument("--credentials-file", default=os.path.expanduser("~/incityjobs-test-branch-roles.txt"))
    args = ap.parse_args()

    if not args.db_url:
        print("Set INCITY_DB_URL to the TEST branch owner connection string, or pass --db-url.")
        return 2
    prod = args.production_endpoint.strip().replace("-pooler", "")
    target = endpoint_of(args.db_url)
    if not prod.startswith("ep-"):
        print("Refused: --production-endpoint must be your production endpoint id (starts with ep-).")
        return 2
    if not target or target == prod or prod in args.db_url:
        print(f"Refused: the target endpoint ({target or 'unknown'}) is PRODUCTION or unknown. Use the test branch.")
        return 2
    print(f"Target endpoint: {target} (production is {prod}: different, continuing)")

    conn = psycopg2.connect(args.db_url, connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_user, current_database(), version();")
            user, db, ver = cur.fetchone()
            print(f"Connected as {user} to database {db}: {ver.split(',')[0]}")

            if args.remove:
                for role in sorted(existing_roles(cur)):
                    cur.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role};")
                    cur.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {role};")
                    cur.execute(f"REVOKE ALL ON SCHEMA public FROM {role};")
                    cur.execute(f'REVOKE ALL ON DATABASE "{db}" FROM {role};')
                    cur.execute(f"DROP ROLE {role};")
                    print(f"Removed role {role}")
                conn.commit()
                print("Rollback complete: no InCityJobs roles remain. Tables and data were not touched.")
                return 0

            if args.apply:
                present = existing_roles(cur)
                passwords = {}
                for role in ROLES:
                    pw = new_password()
                    passwords[role] = pw
                    # Defaults are already NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
                    # verify() below checks every attribute. A re-run only rotates the password
                    # (PostgreSQL 16+ refuses non-superusers re-stating some attributes).
                    if role in present:
                        cur.execute(f"ALTER ROLE {role} WITH PASSWORD %s;", (pw,))
                        print(f"rotated password of existing role {role}")
                    else:
                        cur.execute(f"CREATE ROLE {role} WITH LOGIN NOINHERIT PASSWORD %s;", (pw,))
                        print(f"created role {role}")
                with open(GRANTS_SQL, encoding="utf-8") as fh:
                    cur.execute(fh.read())
                problems = verify(cur)
                if problems:
                    conn.rollback()
                    print("Verification FAILED; everything rolled back:\n  - " + "\n  - ".join(problems))
                    return 1
                conn.commit()
                fd = os.open(args.credentials_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w") as fh:
                    fh.write("# InCityJobs TEST-BRANCH role credentials. Private; never commit or paste.\n")
                    for role in ROLES:
                        fh.write(f"{role} = {role_url(args.db_url, role, passwords[role])}\n")
                print(f"Applied and verified. Connection strings written to {args.credentials_file} (permissions 600).")
                return 0

            problems = verify(cur)
            conn.rollback()
            if args.verify:
                if problems:
                    print("Verification FAILED:\n  - " + "\n  - ".join(problems))
                    return 1
                print("Verification passed: both roles match the least-privilege design exactly.")
                return 0
            print("Plan (nothing changed). Current differences from the design:")
            print("  - " + "\n  - ".join(problems) if problems else "  none: already applied")
            print(f"Re-run with --apply to create the roles and apply {os.path.basename(GRANTS_SQL)}.")
            return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
