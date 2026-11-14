"""Preflight check and verified, all-or-nothing apply for the InCityJobs schema migration.

Run this against a Neon *test branch* or a local database, never against production.
See docs/STAGING_DEPLOYMENT.md.

    pip install -r requirements.txt
    read -rs INCITY_DB_URL && export INCITY_DB_URL      # TEST branch owner string (hidden)
    read -rs NEON_API_KEY && export NEON_API_KEY        # Neon API key (hidden; revoke it afterwards)
    python scripts/check_migration.py --neon-project-id <project id> \\
        --production-endpoint ep-PROD... --expect-endpoint ep-TEST... --expect-branch <test branch name>
    # add --apply to apply; without it the run is a dry run that always rolls back

Target verification, all BEFORE connecting to the database (any failure: exit 2, nothing contacted):
  1. the connection string's host must be a well-formed Neon host (ep-...[-pooler].<region>.aws|azure.neon.tech)
     and its sslmode, if given, must be require, verify-ca or verify-full;
  2. its endpoint id must equal --expect-endpoint and must not equal --production-endpoint;
  3. Neon's API is asked, independently of the ids typed above, which branch serves that endpoint:
     the endpoint must belong to --neon-project-id, be read-write and enabled, and have exactly the
     host being connected to; its branch must be named --expect-branch and must be neither the
     project's default nor its primary branch; the production endpoint must serve a different branch.

All-or-nothing: the migration, the catalog verification and the duplicate-insert probe run in ONE
transaction (lock_timeout 10 s, statement_timeout 120 s). Only if every UNIQUE constraint is
verified active AND the probe proves enforcement does --apply commit. Otherwise (duplicates, a
failed or invalid index, a probe failure, any error) the whole transaction is rolled back and no
schema change is kept. A dry run performs the same steps except the probe, and always rolls back.

Exit codes:
    0  dry run: the migration would complete / apply: committed and re-verified
    1  not complete: nothing was committed (or, after a commit, re-verification failed: report it)
    2  usage error, or refused by target verification (nothing was contacted)
"""
import argparse
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.request
import uuid
from urllib.parse import parse_qs, quote, urlsplit

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg2  # noqa: E402
import db_schema  # noqa: E402

NEON_API = "https://console.neon.tech/api/v2"
NEON_HOST = re.compile(r"^(ep-[a-z0-9]+(?:-[a-z0-9]+)*?)(-pooler)?\.(?:c-\d+\.)?[a-z0-9]+(?:-[a-z0-9]+)*\.(?:aws|azure)\.neon\.tech$")
STRONG_SSLMODES = {"require", "verify-ca", "verify-full"}
NEW_COLUMNS = ("employer_matching_consent", "employer_matching_consent_at", "employer_matching_consent_policy",
               "contact_disclosure_consent", "resume_version")


# --------------------------------------------------------------------------- target verification
def parse_target(db_url: str) -> dict:
    """Host, sslmode and database of a URI or key=value DSN (values only, never the password)."""
    text = (db_url or "").strip()
    if text.startswith(("postgres://", "postgresql://")):
        parts = urlsplit(text)
        query = parse_qs(parts.query)
        return {"host": (parts.hostname or "").lower(), "sslmode": (query.get("sslmode") or [""])[0],
                "dbname": parts.path.lstrip("/") or ""}
    pairs = dict(re.findall(r"(\w+)\s*=\s*'?([^\s']*)'?", text))
    return {"host": pairs.get("host", "").lower(), "sslmode": pairs.get("sslmode", ""), "dbname": pairs.get("dbname", "")}


def endpoint_of(db_url: str) -> str:
    m = NEON_HOST.match(parse_target(db_url)["host"])
    return m.group(1) if m else ""


def _norm(endpoint):
    return (endpoint or "").strip().lower().removesuffix("-pooler")


def static_guard(db_url, production, expected):
    """Checks that need no network. Returns None or the refusal reason."""
    target = parse_target(db_url)
    production, expected = _norm(production), _norm(expected)
    if not production.startswith("ep-"):
        return "--production-endpoint must be your production endpoint id (starts with ep-)"
    if not expected.startswith("ep-"):
        return "--expect-endpoint must be the test branch endpoint id (starts with ep-)"
    if expected == production:
        return "--expect-endpoint and --production-endpoint are the same"
    if not NEON_HOST.match(target["host"]):
        return "the target host is not a well-formed Neon host (ep-...[-pooler].<region>.aws|azure.neon.tech)"
    if target["sslmode"] and target["sslmode"] not in STRONG_SSLMODES:
        return f"sslmode={target['sslmode']} is too weak (use require, verify-ca or verify-full)"
    found = endpoint_of(db_url)
    if found == production:
        return f"the target endpoint {found} IS the production endpoint"
    if found != expected:
        return f"the target endpoint {found} is not the expected test endpoint {expected}"
    return None


def neon_get(path, api_key, ca_file=None):
    context = ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()
    request = urllib.request.Request(NEON_API + path, headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=15, context=context) as response:
        return json.loads(response.read().decode("utf-8"))


def api_guard(db_url, project_id, production, expected, expected_branch, api_key, ca_file=None):
    """Independent confirmation from Neon's API. Returns (None, facts) or (reason, None)."""
    if not api_key:
        return "NEON_API_KEY is not set (enter it with: read -rs NEON_API_KEY && export NEON_API_KEY)", None
    if not re.fullmatch(r"[a-z0-9-]{1,60}", project_id or ""):
        return "--neon-project-id is missing or malformed", None
    project = quote(project_id, safe="")
    try:
        target_ep = neon_get(f"/projects/{project}/endpoints/{quote(_norm(expected), safe='')}", api_key, ca_file)["endpoint"]
        prod_ep = neon_get(f"/projects/{project}/endpoints/{quote(_norm(production), safe='')}", api_key, ca_file)["endpoint"]
        branch = neon_get(f"/projects/{project}/branches/{quote(target_ep['branch_id'], safe='')}", api_key, ca_file)["branch"]
    except urllib.error.HTTPError as exc:
        return f"Neon API answered HTTP {exc.code} (check the project id, endpoint ids and key)", None
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError) as exc:
        return f"Neon API could not be checked ({type(exc).__name__}); not continuing", None

    host = parse_target(db_url)["host"].replace("-pooler.", ".", 1)
    checks = [
        (target_ep.get("id") == _norm(expected), "the API returned a different endpoint id"),
        (target_ep.get("project_id") == project_id, "the endpoint belongs to a different Neon project"),
        ((target_ep.get("host") or "").lower() == host, "the endpoint's host in Neon is not the host being connected to"),
        (target_ep.get("type") == "read_write", "the endpoint is not read-write"),
        (not target_ep.get("disabled"), "the endpoint is disabled"),
        (branch.get("id") == target_ep.get("branch_id"), "the API returned a different branch"),
        (branch.get("name") == expected_branch, f"the endpoint serves branch {branch.get('name')!r}, not {expected_branch!r}"),
        (not branch.get("default"), "the endpoint serves the project's DEFAULT branch (production)"),
        (not branch.get("primary"), "the endpoint serves the project's PRIMARY branch (production)"),
        (prod_ep.get("branch_id") and prod_ep.get("branch_id") != branch.get("id"),
         "the production endpoint serves the same branch"),
    ]
    for ok, reason in checks:
        if not ok:
            return reason, None
    return None, {"branch": branch.get("name"), "branch_id": branch.get("id"), "init_source": branch.get("init_source"),
                  "parent_id": branch.get("parent_id"), "production_branch_id": prod_ep.get("branch_id")}


# --------------------------------------------------------------------------- migration
def probe_in_transaction(conn) -> dict:
    """Duplicate-insert probe inside the CURRENT transaction, each attempt in its own savepoint
    that is rolled back. Unlike db_schema.probe_unique_enforcement, it never ends the transaction,
    so the migration and its proof commit or roll back together. (Rolled-back inserts still
    advance the id sequences slightly.)"""
    results = {}
    with conn.cursor() as cur:
        for _, table, column in db_schema.UNIQUE_EMAIL_CONSTRAINTS:
            email = f"probe-{uuid.uuid4().hex[:12]}@example.invalid"
            cur.execute("SAVEPOINT icj_unique_probe;")
            try:
                cur.execute(f"INSERT INTO {table} ({column}) VALUES (%s);", (email,))
                cur.execute(f"INSERT INTO {table} ({column}) VALUES (%s);", (f"  {email.upper()} ",))
                results[table] = "NOT ENFORCED (duplicate accepted)"
            except psycopg2.Error as exc:
                results[table] = "enforced" if db_schema.is_unique_violation(exc) else f"error: {type(exc).__name__}"
            cur.execute("ROLLBACK TO SAVEPOINT icj_unique_probe;")
            cur.execute("RELEASE SAVEPOINT icj_unique_probe;")
    return results


def post_commit_problems(conn) -> list:
    """Read-only re-verification after the commit, in its own read-only transaction."""
    problems = []
    with conn.cursor() as cur:
        cur.execute("SET TRANSACTION READ ONLY;")
        for index_name, table, column in db_schema.UNIQUE_EMAIL_CONSTRAINTS:
            if not db_schema.verify_unique_index(cur, index_name, table, column)["active"]:
                problems.append(f"{index_name} not active")
        cur.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema = 'public' "
                    "AND table_name = 'candidate_profiles' AND column_name IN (" + ", ".join(["%s"] * len(NEW_COLUMNS)) + ");", NEW_COLUMNS)
        if cur.fetchone()[0] != len(NEW_COLUMNS):
            problems.append("consent/version columns incomplete")
        cur.execute("SELECT to_regclass('public.employer_recommendations') IS NOT NULL AND to_regclass('public.privacy_audit_log') IS NOT NULL;")
        if not cur.fetchone()[0]:
            problems.append("recommendation or audit table missing")
        cur.execute("SELECT count(*) FROM pg_trigger WHERE tgrelid = to_regclass('public.privacy_audit_log') AND NOT tgisinternal "
                    "AND tgenabled <> 'D' AND tgname IN ('privacy_audit_log_append_only', 'privacy_audit_log_no_truncate');")
        if cur.fetchone()[0] != 2:
            problems.append("audit triggers missing or disabled")
    conn.rollback()
    return problems


def print_report(report, applying):
    for table, entry in report["constraints"].items():
        verification = entry["verification"]
        print(f"{table}.{entry['column']}: UNIQUE constraint -> {entry['status']}"
              f" (catalog verification: {'active' if verification['active'] else 'NOT active'})")
        print(f"   {json.dumps(entry['profile'])}")
        for email_key, copies, ids in entry["duplicates"]:
            print(f"   duplicate: {email_key!r} x{copies} (row ids {ids})")
        for problem in verification["problems"]:
            if problem != "index does not exist" or applying:
                print(f"   verification: {problem}")
        if entry.get("error"):
            print(f"   error: {entry['error']}")
    print(f"\nHistorical rows at the old fallback point (kept, excluded from matching): {report['legacy_fallback_rows']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="apply the migration (default: dry run, always rolled back)")
    parser.add_argument("--db-url", default=os.environ.get("INCITY_DB_URL"), help="defaults to $INCITY_DB_URL")
    parser.add_argument("--neon-project-id", required=True, help="the Neon project id (Settings -> General)")
    parser.add_argument("--production-endpoint", required=True, help="the PRODUCTION endpoint id (ep-...); always refused")
    parser.add_argument("--expect-endpoint", required=True, help="the TEST branch endpoint id (ep-...)")
    parser.add_argument("--expect-branch", required=True, help="the TEST branch name, e.g. staging-test-gate")
    parser.add_argument("--expect-database", default="neondb", help="database the connection must land in (default neondb)")
    parser.add_argument("--ca-file", help="CA bundle for the Neon API's TLS check if the system store is not found")
    args = parser.parse_args()
    if not args.db_url:
        print("Set INCITY_DB_URL or pass --db-url (use a Neon test branch, not production).")
        return 2

    refused = static_guard(args.db_url, args.production_endpoint, args.expect_endpoint)
    if not refused:
        refused, facts = api_guard(args.db_url, args.neon_project_id, args.production_endpoint, args.expect_endpoint,
                                   args.expect_branch, os.environ.get("NEON_API_KEY"), args.ca_file)
    if refused:
        print(f"Refused before connecting: {refused}. Nothing was contacted or changed.")
        return 2
    print(f"Target endpoint: {endpoint_of(args.db_url)}; Neon confirms branch {facts['branch']!r} ({facts['branch_id']}), "
          f"not default, not primary, created as {facts['init_source']!r} from {facts['parent_id']}; "
          f"production endpoint serves {facts['production_branch_id']}.")

    conn = psycopg2.connect(args.db_url, connect_timeout=10)
    probe, committed = None, False
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL lock_timeout = '10s';")
            cur.execute("SET LOCAL statement_timeout = '120s';")
            cur.execute("SELECT current_database();")
            database = cur.fetchone()[0]
        if database != args.expect_database:
            conn.rollback()
            print(f"Refused: connected to database {database!r}, expected {args.expect_database!r}. Nothing was changed.")
            return 2
        report = db_schema.apply_schema(conn, commit=False)          # same transaction; nothing committed yet
        if args.apply and report["complete"]:
            probe = probe_in_transaction(conn)
        complete = report["complete"] and (not args.apply or all(v == "enforced" for v in (probe or {}).values()) and bool(probe))
        if args.apply and complete:
            conn.commit()
            committed = True
        else:
            conn.rollback()
        problems = post_commit_problems(conn) if committed else []
    finally:
        conn.close()   # an exception before commit leaves the transaction uncommitted: PostgreSQL rolls it back

    print(f"Migration {'APPLIED (committed as one transaction)' if committed else 'DRY RUN (rolled back, nothing changed)' if not args.apply else 'NOT APPLIED (rolled back, nothing changed)'}\n")
    print_report(report, args.apply)

    if not args.apply:
        blocked = [t for t, e in report["constraints"].items() if e["status"] != "enforced"]
        print("\nAll UNIQUE constraints can be enforced." if not blocked else
              f"\nBlocked: {blocked}. Resolve the duplicates above, then run again. --apply would commit nothing.")
        return 0 if not blocked else 1
    if probe is not None:
        print(f"Enforcement probe (inside the transaction, each attempt rolled back): {probe}")
    if not committed:
        print("\nMIGRATION NOT COMPLETE: nothing was committed; the database is exactly as before.")
        return 1
    if problems:
        print(f"\nMIGRATION COMMITTED BUT RE-VERIFICATION FAILED: {problems}. Stop and report this.")
        return 1
    print("\nMIGRATION COMPLETE: committed as one transaction, then re-verified "
          "(uniqueness active, five consent columns, both tables, both audit triggers enabled).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
