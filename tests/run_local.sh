#!/usr/bin/env bash
# Run every InCityJobs test suite against a THROWAWAY local PostgreSQL cluster.
#
# - Creates a brand-new cluster in a temporary directory, listening only on a
#   Unix socket (no TCP), and deletes it when the run ends.
# - Never uses INCITY_DB_URL, Streamlit secrets or any Neon connection string.
#
# Usage:  bash tests/run_local.sh
# Needs:  Python 3.11+, PostgreSQL 14+ server binaries (initdb, pg_ctl, psql).
#         Set PG_BIN=/path/to/postgres/bin if they aren't found automatically.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
TESTS="$REPO/tests"
PORT="${INCITY_TEST_PGPORT:-54329}"

# --- locate PostgreSQL server binaries
PG_BIN="${PG_BIN:-}"
if [ -z "$PG_BIN" ]; then
  if command -v pg_config >/dev/null 2>&1 && [ -x "$(pg_config --bindir)/initdb" ]; then
    PG_BIN="$(pg_config --bindir)"
  elif command -v initdb >/dev/null 2>&1; then
    PG_BIN="$(dirname "$(command -v initdb)")"
  else
    PG_BIN="$(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -1 || true)"
  fi
fi
if [ -z "$PG_BIN" ] || [ ! -x "$PG_BIN/initdb" ]; then
  echo "PostgreSQL server binaries not found. Install PostgreSQL 14+ or set PG_BIN." >&2
  exit 2
fi
export PATH="$PG_BIN:$PATH"

# --- throwaway cluster (short /tmp path keeps the socket path under OS limits)
WORK="$(mktemp -d /tmp/incity-pg.XXXXXX)"
AS=()
if [ "$(id -u)" = "0" ]; then          # initdb refuses to run as root
  chown postgres "$WORK"; chmod 755 "$WORK"
  AS=(runuser -u postgres --)
fi
cleanup() {
  "${AS[@]}" "$PG_BIN/pg_ctl" -D "$WORK/data" stop -m fast >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

# Password authentication (SCRAM), like Neon, so wrong-credential tests fail for real.
# The password is random per run and never leaves this machine.
TEST_PGPASSWORD="test-$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')"
printf '%s\n' "$TEST_PGPASSWORD" > "$WORK/pwfile"
[ ${#AS[@]} -gt 0 ] && chown postgres "$WORK/pwfile"
"${AS[@]}" "$PG_BIN/initdb" -D "$WORK/data" -A scram-sha-256 --pwfile="$WORK/pwfile" -U postgres -E UTF8 --locale=C >/dev/null  # UTF-8 like Neon
rm -f "$WORK/pwfile"
"${AS[@]}" "$PG_BIN/pg_ctl" -D "$WORK/data" -w -l "$WORK/server.log" \
  -o "-k $WORK -p $PORT -c listen_addresses=" start >/dev/null
echo "Throwaway PostgreSQL $("$PG_BIN/postgres" --version | awk '{print $3}') running on socket $WORK (port $PORT, no TCP)"

# --- point the local-only psycopg2 test adapter at it
export PGSHIM_HOST="$WORK" PGSHIM_PORT="$PORT" PGSHIM_USER=postgres PGPASSWORD="$TEST_PGPASSWORD"
export PYTHONPATH="$TESTS/pg_adapter:$TESTS"
unset INCITY_DB_URL

status=0
for suite in test_units.py test_migration.py test_e2e.py test_diagnostics.py test_recommendations.py; do
  echo; echo "=== $suite ==="
  python3 "$TESTS/$suite" 2> "$WORK/$suite.stderr" || { status=1; echo "--- $suite stderr (last 20 lines) ---"; tail -20 "$WORK/$suite.stderr"; }
done
echo
[ $status -eq 0 ] && echo "ALL SUITES PASSED" || echo "SOME SUITES FAILED"
exit $status
