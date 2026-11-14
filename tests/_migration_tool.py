"""Run scripts/check_migration.py in a subprocess with the test-only Neon API stand-in
(tests/fake_neon_api.py). Shared by test_migration.py and test_migration_partial.py."""
import copy
import json
import os
import subprocess
import sys
import tempfile

from _common import REPO, TESTS

API_KEY = "test-neon-key-0123456789abcdef"
PROJECT = "proj-test"
STAGING_EP, PROD_EP = "ep-testbranch-1111", "ep-prod-0000"
STAGING_HOST = f"{STAGING_EP}.c-2.us-east-2.aws.neon.tech"

DEFAULT_SCENARIO = {"responses": {
    f"/projects/{PROJECT}/endpoints/{STAGING_EP}": {"endpoint": {
        "id": STAGING_EP, "project_id": PROJECT, "host": STAGING_HOST, "branch_id": "br-staging",
        "type": "read_write", "disabled": False}},
    f"/projects/{PROJECT}/endpoints/{PROD_EP}": {"endpoint": {
        "id": PROD_EP, "project_id": PROJECT, "host": f"{PROD_EP}.c-2.us-east-2.aws.neon.tech", "branch_id": "br-prod",
        "type": "read_write", "disabled": False}},
    f"/projects/{PROJECT}/branches/br-staging": {"branch": {
        "id": "br-staging", "name": "staging-test-gate", "default": False, "primary": False,
        "parent_id": "br-prod", "init_source": "schema-only"}},
    f"/projects/{PROJECT}/branches/br-prod": {"branch": {
        "id": "br-prod", "name": "production", "default": True, "primary": True}},
}}


def scenario(**changes):
    """DEFAULT_SCENARIO with changes: {'<path>': {'<field>': value}} merged into the object."""
    s = copy.deepcopy(DEFAULT_SCENARIO)
    for path, fields in changes.get("patch", {}).items():
        obj = s["responses"][path]
        next(iter(obj.values())).update(fields)
    for key in ("network_error", "http_errors"):
        if key in changes:
            s[key] = changes[key]
    return s


def default_args(db):
    return ["--neon-project-id", PROJECT, "--production-endpoint", PROD_EP, "--expect-endpoint", STAGING_EP,
            "--expect-branch", "staging-test-gate", "--expect-database", db]


def run(db, *args, url=None, scen=None, key=API_KEY, guard=True, break_probe=False):
    """Returns (exit code, output, log lines). log lines show 'api <path>' and 'db connect'."""
    user, pw = os.environ.get("PGSHIM_USER", "postgres"), os.environ["PGPASSWORD"]
    url = url or f"postgresql://{user}:{pw}@{STAGING_EP}-pooler.c-2.us-east-2.aws.neon.tech/neondb?sslmode=require"
    tmp = tempfile.mkdtemp(dir=os.environ["PGSHIM_HOST"])
    scen_file, log_file = os.path.join(tmp, "scenario.json"), os.path.join(tmp, "log.txt")
    json.dump(scen or DEFAULT_SCENARIO, open(scen_file, "w"))
    open(log_file, "w").close()
    env = dict(os.environ, PGSHIM_DB=db, INCITY_DB_URL=url, FAKE_NEON_SCENARIO=scen_file, FAKE_NEON_LOG=log_file,
               PYTHONPATH=os.pathsep.join([REPO, os.environ.get("PYTHONPATH", "")]))
    env.pop("NEON_API_KEY", None)
    if key:
        env["NEON_API_KEY"] = key
    if break_probe:
        env["FAKE_BREAK_PROBE"] = "1"
    argv = (default_args(db) if guard else []) + list(args)
    code = ("import sys, runpy, fake_neon_api; "
            f"sys.argv = ['check_migration.py'] + {argv!r}; "
            f"runpy.run_path({os.path.join(REPO, 'scripts', 'check_migration.py')!r}, run_name='__main__')")
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=TESTS)
    return p.returncode, p.stdout + p.stderr, open(log_file).read().splitlines()
