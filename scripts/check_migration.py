"""Preflight check for the InCityJobs schema migration.

Run this against a Neon *test branch* (a copy of production) before deploying,
never directly against production.

    pip install -r requirements.txt
    export INCITY_DB_URL="postgresql://...neon test branch..."
    python scripts/check_migration.py            # read-only report
    python scripts/check_migration.py --apply    # apply the migration to that database

The read-only mode runs inside a transaction that is always rolled back, so it
changes nothing. --apply runs the same migration the app runs at startup.
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
    parser.add_argument("--apply", action="store_true", help="apply the migration (default: read-only report)")
    parser.add_argument("--db-url", default=os.environ.get("INCITY_DB_URL"), help="defaults to $INCITY_DB_URL")
    args = parser.parse_args()
    if not args.db_url:
        print("Set INCITY_DB_URL or pass --db-url (use a Neon test branch, not production).")
        return 2

    conn = psycopg2.connect(args.db_url, connect_timeout=10)
    try:
        if args.apply:
            report = db_schema.apply_schema(conn)
            mode = "APPLIED"
        else:
            # Run the migration inside a transaction and roll it back: shows exactly
            # what would happen without changing anything.
            report = db_schema.apply_schema(conn, commit=False)
            conn.rollback()
            mode = "DRY RUN (rolled back, nothing changed)"
    finally:
        conn.close()

    print(f"Migration {mode}\n")
    ok = True
    for table, entry in report["constraints"].items():
        print(f"{table}.{entry['column']}: UNIQUE constraint -> {entry['status']}")
        print(f"   {json.dumps(entry['profile'])}")
        for email_key, copies, ids in entry["duplicates"]:
            print(f"   duplicate: {email_key!r} x{copies} (row ids {ids})")
        if entry.get("error"):
            print(f"   error: {entry['error']}")
        ok = ok and entry["status"] == "enforced"
    print("\nAll UNIQUE constraints can be enforced." if ok else "\nResolve the duplicates above, then run again.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
