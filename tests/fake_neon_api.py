"""TEST-ONLY stand-in for Neon's HTTP API and a connection recorder, for check_migration.py.

Imported by the test runner before running scripts/check_migration.py in a subprocess:
  - urllib.request.urlopen is replaced: requests to https://console.neon.tech/api/v2 are answered
    from the JSON scenario in $FAKE_NEON_SCENARIO; any other URL raises (no real network);
  - every API path requested and every database connection attempt is appended to $FAKE_NEON_LOG,
    so tests can prove a refused run never contacted the database;
  - the Authorization header is checked against $NEON_API_KEY and never logged.
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
    _log("db connect")
    return _real_connect(*args, **kwargs)


psycopg2.connect = _connect

if os.environ.get("FAKE_BREAK_PROBE"):
    # Simulate a uniqueness probe that does not see a unique violation (enforcement not proven).
    import db_schema
    db_schema.is_unique_violation = lambda exc: False
