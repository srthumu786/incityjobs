"""TEST-ONLY stand-in for Neon's HTTP API and a connection recorder, for check_migration.py.

Imported by the test runner before running scripts/check_migration.py in a subprocess:
  - urllib.request.urlopen is replaced: requests to https://console.neon.tech/api/v2 are answered
    from the JSON scenario in $FAKE_NEON_SCENARIO; any other URL raises (no real network);
  - every API path requested and every database connection attempt is appended to $FAKE_NEON_LOG,
    so tests can prove a refused run never contacted the database;
  - the Authorization header is checked against $NEON_API_KEY and never logged.
FAKE_CLOCK_OFFSET_MINUTES shifts this process's clock (datetime.now, time.time), simulating an
administrator computer whose clock differs from the database's.
The tool itself has no test switch; this module only exists in tests/.
"""
import io
import json
import os
import urllib.error
import urllib.request

import psycopg2

BASE = "https://console.neon.tech/api/v2"
_scenario = json.load(open(os.environ["FAKE_NEON_SCENARIO"]))


def _log(line):
    with open(os.environ["FAKE_NEON_LOG"], "a") as fh:
        fh.write(line + "\n")


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _urlopen(request, timeout=None, context=None):
    url = request.full_url if hasattr(request, "full_url") else str(request)
    if not url.startswith(BASE + "/"):
        raise AssertionError(f"unexpected network call to {url}")
    assert context is not None and context.verify_mode != 0, "TLS verification must be on"
    assert request.get_header("Authorization") == f"Bearer {os.environ.get('NEON_API_KEY')}", "wrong or missing key"
    path = url[len(BASE):]
    _log("api " + path)
    if _scenario.get("network_error"):
        raise urllib.error.URLError("simulated network failure")
    if path in _scenario.get("http_errors", {}):
        raise urllib.error.HTTPError(url, _scenario["http_errors"][path], "error", {}, None)
    body = _scenario["responses"].get(path)
    if body is None:
        raise urllib.error.HTTPError(url, 404, "not found", {}, None)
    return _Response(json.dumps(body).encode())


urllib.request.urlopen = _urlopen

_real_connect = psycopg2.connect


def _connect(*args, **kwargs):
    _log(f"db connect sslmode={kwargs.get('sslmode')} sslrootcert={kwargs.get('sslrootcert')}")
    conn = _real_connect(*args, **kwargs)
    if os.environ.get("FAKE_DAMAGE_AFTER_COMMIT"):
        # Simulate something going wrong right after a successful commit (e.g. another session
        # dropping an object), so the post-commit re-verification fails.
        real_commit = conn.commit

        def commit():
            real_commit()
            import subprocess
            subprocess.run(["psql", "-X", "-q", "-h", os.environ["PGSHIM_HOST"], "-p", os.environ.get("PGSHIM_PORT", "5432"),
                            "-U", os.environ.get("PGSHIM_USER", "postgres"), "-d", os.environ["PGSHIM_DB"],
                            "-c", "DROP TRIGGER privacy_audit_log_no_truncate ON privacy_audit_log;"], check=True,
                           capture_output=True)
        conn.commit = commit
    return conn


psycopg2.connect = _connect

if os.environ.get("FAKE_BREAK_PROBE"):
    # Simulate a uniqueness probe that does not see a unique violation (enforcement not proven).
    import db_schema
    db_schema.is_unique_violation = lambda exc: False

if os.environ.get("FAKE_ERROR_WITH_PERSONAL_DATA"):
    # Simulate a database error whose text quotes personal data, raised AFTER the migration
    # statements ran inside the transaction (so a missing rollback would be visible).
    import db_schema
    _real_apply = db_schema.apply_schema

    def _apply_then_fail(conn, commit=True):
        _real_apply(conn, commit=False)
        raise psycopg2.IntegrityError(
            'duplicate key value violates unique constraint "uq_beta_employers_admin_email"\n'
            "DETAIL:  Key (lower(btrim(admin_email)))=(leak.person@example.com) already exists.\n"
            "CONTEXT: connection password=hunter2-secret-xyz", pgcode="23505")
    db_schema.apply_schema = _apply_then_fail

if os.environ.get("FAKE_CLOCK_OFFSET_MINUTES"):
    # Simulate an administrator computer whose clock differs from the database server's clock.
    import datetime as _dt
    import time as _time
    _offset = float(os.environ["FAKE_CLOCK_OFFSET_MINUTES"]) * 60
    _real_dt = _dt.datetime

    class _SkewedDatetime(_real_dt):
        @classmethod
        def now(cls, tz=None):
            return _real_dt.now(tz) + _dt.timedelta(seconds=_offset)

        @classmethod
        def utcnow(cls):
            return _real_dt.utcnow() + _dt.timedelta(seconds=_offset)

    _dt.datetime = _SkewedDatetime
    _real_time = _time.time
    _time.time = lambda: _real_time() + _offset

