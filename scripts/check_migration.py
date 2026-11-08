"""Preflight check and verified apply for the InCityJobs schema migration.

Run this against a Neon *test branch* (a copy of production) or a local
database, never directly against production. See docs/STAGING_DEPLOYMENT.md.

    pip install -r requirements.txt
    export INCITY_DB_URL="postgresql://...neon TEST branch..."
    python scripts/check_migration.py            # read-only dry run (always rolled back)
    python scripts/check_migration.py --apply    # apply, then verify enforcement

Exit codes:
    0  dry run: all UNIQUE constraints can be enforced
       apply:   migration COMPLETE (catalog verification + enforcement probe passed)
    1  constraints blocked by duplicates or failed verification
    2  usage error
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg2  # noqa: E402
import db_schema  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="apply the migration and verify it (default: read-only dry run)")
    parser.add_argument("--db-url", default=os.environ.get("INCITY_DB_URL"), help="defaults to $INCITY_DB_URL")
    args = parser.parse_args()
    if not args.db_url:
        print("Set INCITY_DB_URL or pass --db-url (use a Neon test branch, not production).")
        return 2

    conn = psycopg2.connect(args.db_url, connect_timeout=10)
    probe = None
    try:
        if args.apply:
            report = db_schema.apply_schema(conn)
            mode = "APPLIED"
            if report["complete"]:
                probe = db_schema.probe_unique_enforcement(conn)
        else:
            # Run the migration inside a transaction and roll it back: shows exactly
            # what would happen without changing anything.
            report = db_schema.apply_schema(conn, commit=False)
            conn.rollback()
            mode = "DRY RUN (rolled back, nothing changed)"
    finally:
        conn.close()

    print(f"Migration {mode}\n")
    for table, entry in report["constraints"].items():
        verification = entry["verification"]
        print(f"{table}.{entry['column']}: UNIQUE constraint -> {entry['status']}"
              f" (catalog verification: {'active' if verification['active'] else 'NOT active'})")
        print(f"   {json.dumps(entry['profile'])}")
        for email_key, copies, ids in entry["duplicates"]:
            print(f"   duplicate: {email_key!r} x{copies} (row ids {ids})")
        for problem in verification["problems"]:
            if problem != "index does not exist" or args.apply:
                print(f"   verification: {problem}")
        if entry.get("error"):
            print(f"   error: {entry['error']}")
    print(f"\nHistorical rows at the old fallback point (kept, excluded from matching): {report['legacy_fallback_rows']}")

    if not args.apply:
        blocked = [t for t, e in report["constraints"].items() if e["status"] != "enforced"]
        print("\nAll UNIQUE constraints can be enforced." if not blocked else f"\nBlocked: {blocked}. Resolve the duplicates above, then run again.")
        return 0 if not blocked else 1

    if probe is not None:
        print(f"Enforcement probe (rolled back): {probe}")
    complete = report["complete"] and probe is not None and all(v == "enforced" for v in probe.values())
    print("\nMIGRATION COMPLETE: database-level email uniqueness verified active." if complete
          else "\nMIGRATION NOT COMPLETE: see the problems above.")
    return 0 if complete else 1


if __name__ == "__main__":
    sys.exit(main())
