"""Shared helpers for the InCityJobs test suites (see tests/README.md)."""
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(REPO, "tests")
LEGACY_SCHEMA = os.path.join(TESTS, "fixtures", "legacy_main_schema.sql")
if REPO not in sys.path:
    sys.path.insert(0, REPO)


def require_local_postgres():
    host = os.environ.get("PGSHIM_HOST", "")
    if not host or not os.path.isdir(host):
        sys.exit("These tests need the throwaway local PostgreSQL started by tests/run_local.sh. "
                 "Run:  bash tests/run_local.sh")


def _psql(db):
    return ["psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1",
            "-h", os.environ["PGSHIM_HOST"], "-p", os.environ.get("PGSHIM_PORT", "5432"),
            "-U", os.environ.get("PGSHIM_USER", "postgres"), "-d", db]


def sql(db, text):
    """Run SQL directly with psql (test setup/inspection only) and return stdout."""
    r = subprocess.run(_psql(db), input=text, text=True, capture_output=True)
    if r.returncode:
        raise SystemExit(f"SQL failed in {db}: {r.stderr}\n{text}")
    return r.stdout.strip()


def fresh_db(name):
    """Drop and recreate a database on the throwaway cluster, and point the adapter at it."""
    subprocess.run(_psql("postgres") + ["-c", f"DROP DATABASE IF EXISTS {name}", "-c", f"CREATE DATABASE {name}"],
                   check=True, capture_output=True)
    os.environ["PGSHIM_DB"] = name


def load_legacy_schema(db):
    sql(db, open(LEGACY_SCHEMA, encoding="utf-8").read())


class Checks:
    """Collects results by group; stops at the first failure."""

    def __init__(self, suite):
        self.suite, self.results = suite, []

    def ok(self, group, condition, message, debug=None):
        self.results.append((group, bool(condition), message))
        print(("PASS " if condition else "FAIL ") + f"[{group}] {message}", flush=True)
        if not condition:
            if debug:
                print("     debug:", debug() if callable(debug) else debug)
            sys.exit(1)

    def summary(self):
        groups = {}
        for group, passed, _ in self.results:
            done, total = groups.get(group, (0, 0))
            groups[group] = (done + passed, total + 1)
        text = ", ".join(f"{g} {d}/{t}" for g, (d, t) in groups.items())
        total = len(self.results)
        print(f"\n{self.suite}: {sum(p for _, p, _ in self.results)}/{total} passed ({text})")
