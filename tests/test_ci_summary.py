"""Tests for tests/ci_summary.py, the CI pass/fail reporter. No database needed.

    python3 tests/test_ci_summary.py

Feeds the reporter successful, incomplete, truncated and failed `run_local.sh` output and checks
that only a complete, fully successful run is accepted. Not one of the nine suites run by
run_local.sh (it would change their expected totals); the CI workflow runs it as its own step.
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _common import Checks  # noqa: E402
import ci_summary  # noqa: E402

c = Checks("ci reporter tests")
REPORTER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ci_summary.py")


def suite_lines(file, label, total, passed=None, groups=None, pass_lines=None, noise=False):
    passed = total if passed is None else passed
    groups = groups if groups is not None else f"security {passed}/{total}"
    lines = ["", f"=== {file} ==="]
    lines += [f"PASS [security] check {i}" for i in range(total if pass_lines is None else pass_lines)]
    if noise:
        lines += ["     debug: some value", "--- noise stderr (last 20 lines) ---", "warning: something"]
    lines += ["", f"{label}: {passed}/{total} passed ({groups})" if groups else f"{label}: {passed}/{total} passed"]
    return lines


def good_run(noise=False, **overrides):
    """A complete successful run; overrides[file] = dict of suite_lines() arguments."""
    lines = ["Throwaway PostgreSQL 16.4 running on socket /tmp/incity-pg.abc (port 54329, no TCP)"]
    for file, label, total in ci_summary.EXPECTED:
        args = {"total": total, **overrides.get(file, {})}
        lines += suite_lines(file, label, noise=noise, **args)
    return lines + ["", "ALL SUITES PASSED"]


def verdict(lines):
    ok, _, problems, _ = ci_summary.evaluate(lines)
    return ok, problems


def rejected(group, lines, reason, expect_text=None):
    ok, problems = verdict(lines)
    found = expect_text is None or any(expect_text in p for p in problems)
    c.ok(group, not ok and problems and found, f"rejected: {reason}",
         debug=lambda: problems or "accepted")


def replace_line(lines, old, new):
    return [new if line == old else line for line in lines]


def without_suite(lines, file):
    out, skip = [], False
    for line in lines:
        if line.startswith("=== "):
            skip = line == f"=== {file} ==="
        elif line == "ALL SUITES PASSED":
            skip = False
        if not skip:
            out.append(line)
    return out


E2E = "end-to-end tests: 72/72 passed (security 72/72)"

# --- successful output
ok, problems = verdict(good_run())
c.ok("success", ok and not problems, "a complete run with the expected totals is accepted", debug=lambda: problems)
c.ok("success", ci_summary.EXPECTED_TOTAL == 534, "expected totals add up to 534 (398 + 36 startup read-only + 100 migration ledger checks)")
c.ok("success", [n for _, _, n in ci_summary.EXPECTED] == [41, 34, 72, 33, 59, 69, 90, 36, 100],
     "expected per-suite totals are 41, 34, 72, 33, 59, 69, 90, 36, 100 in run order")
ok, problems = verdict(good_run(noise=True) + ["", "  "])
c.ok("success", ok, "debug lines, stderr tails and trailing blank lines don't affect a good run", debug=lambda: problems)
ok, problems = verdict(good_run(**{"test_units.py": {"groups": "security 26/26, processing 9/9, data 3/3, matching 3/3"}}))
c.ok("success", ok, "several group counts that add up to the total are accepted", debug=lambda: problems)

# --- zero, missing and wrong counts
rejected("counts", good_run(**{"test_units.py": {"total": 0}}), "a suite reporting 0/0", "zero checks")
rejected("counts", good_run(**{"test_units.py": {"total": 40}}), "41 expected but 40/40 reported",
         "reported 40 checks, expected 41")
rejected("counts", good_run(**{"test_units.py": {"total": 42}}), "41 expected but 42/42 reported",
         "reported 42 checks, expected 41")
everything_zero = good_run(**{f: {"total": 0} for f, _, _ in ci_summary.EXPECTED})
rejected("counts", everything_zero, "every suite reporting 0/0")
rejected("counts", good_run(**{"test_units.py": {"groups": ""}}), "summary line without group counts", "group counts")
rejected("counts", good_run(**{"test_units.py": {"groups": "security 40/40"}}), "group counts not adding up to the total",
         "add up")
rejected("counts", good_run(**{"test_units.py": {"groups": "security 41/41, data 0/0"}}), "a group with zero checks",
         "group 'data' 0/0")
rejected("counts", good_run(**{"test_units.py": {"pass_lines": 40}}),
         "summary says 41/41 but only 40 PASS lines were printed", "40 PASS lines")
rejected("counts", replace_line(good_run(), E2E, "unit tests: 72/72 passed (security 72/72)"),
         "a suite printing another suite's label", "label")

# --- incomplete output
for file, _, _ in ci_summary.EXPECTED:
    rejected("incomplete", without_suite(good_run(), file), f"{file} missing", f"{file}: no ")
subset = without_suite(without_suite(good_run(), "test_e2e.py"), "test_db_roles.py")
rejected("incomplete", subset, "an INCITY_SUITES subset run")
rejected("incomplete", [line for line in good_run() if line != E2E], "a suite with no summary line", "no summary line")
rejected("incomplete", [], "empty output", "empty")
rejected("incomplete", ["", "   "], "blank output", "empty")
ok, _, problems, _ = ci_summary.evaluate(None)
c.ok("incomplete", not ok and "could not be read" in problems[0], "rejected: unreadable output file")
rejected("incomplete", good_run()[:-1], "final marker missing", "marker is missing")
dup = good_run()
i = dup.index("=== test_e2e.py ===")
rejected("incomplete", dup[:i] + dup[i:i + 76] + dup[i:], "a suite section repeated", "appears 2 times")
rejected("incomplete", good_run() + ["", "=== test_extra.py ===", "PASS [x] y"], "an unknown suite section", "unknown")
rejected("incomplete", good_run()[:-2] + ["", E2E, "", "ALL SUITES PASSED"], "a second summary line in one suite",
         "summary lines")
swapped = good_run()
a, b = swapped.index("=== test_units.py ==="), swapped.index("=== test_migration.py ===")
e = swapped.index("=== test_e2e.py ===")
swapped = swapped[:a] + swapped[b:e] + swapped[a:b] + swapped[e:]
rejected("incomplete", swapped, "suites in the wrong order", "order")
forged = ["unit tests: 41/41 passed (security 41/41)"] + good_run()
rejected("incomplete", forged, "a summary line printed outside any suite section", "outside any suite")
rejected("incomplete", ["ALL SUITES PASSED"], "only the final marker")

# --- truncated output: every proper prefix of a good run must be rejected
run = good_run(noise=True)
accepted_prefixes = []
for n in range(len(run)):
    if verdict(run[:n])[0]:
        accepted_prefixes.append(n)
c.ok("truncated", not accepted_prefixes, f"all {len(run)} truncations of a good run are rejected",
     debug=lambda: accepted_prefixes)
cut = good_run()
cut[-1] = "ALL SUITES PAS"
rejected("truncated", cut, "final marker cut off mid-line", "marker is missing")
cut = good_run()
k = cut.index(E2E)
cut[k] = "end-to-end tests: 72/72 pas"
rejected("truncated", cut, "summary line cut off mid-line", "no summary line")

# --- failed output
fail_run = good_run()
k = fail_run.index(E2E)
fail_run = fail_run[:k - 1] + ["FAIL [security] cookie flag missing", "     debug: x"] + fail_run[k + 1:]
fail_run[-1] = "SOME SUITES FAILED"
rejected("failed", fail_run, "a suite stopping at its first failure", "FAIL [security] cookie flag missing")
rejected("failed", fail_run, "run_local.sh reporting SOME SUITES FAILED", "SOME SUITES FAILED")
rejected("failed", good_run()[:-1] + ["SOME SUITES FAILED"], "SOME SUITES FAILED with all suite totals right",
         "SOME SUITES FAILED")
rejected("failed", good_run(**{"test_units.py": {"passed": 40, "groups": "security 40/41"}}), "40/41 passed", "test_units.py: 40/41 passed")
mixed = good_run()
k = mixed.index("=== test_units.py ===")
mixed.insert(k + 1, "FAIL [data] row missing")
rejected("failed", mixed, "a FAIL line in a suite whose summary claims all passed", "FAIL [data] row missing")
rejected("failed", good_run()[:-1] + ["SOME SUITES FAILED", "ALL SUITES PASSED"], "both markers present",
         "SOME SUITES FAILED")
rejected("failed", good_run() + ["ALL SUITES PASSED"], "final marker repeated", "appears 2 times")
rejected("failed", good_run() + ["Traceback (most recent call last):"], "output continuing after the final marker",
         "continues after")
early = good_run()
early.remove("ALL SUITES PASSED")
early.insert(3, "ALL SUITES PASSED")
rejected("failed", early, "final marker before the last suite", "before the last suite")

# --- the command-line entry point, as the workflow calls it
with tempfile.TemporaryDirectory() as tmp:
    out_file, summary_file = os.path.join(tmp, "out.txt"), os.path.join(tmp, "summary.md")

    def run_reporter(lines, *extra):
        with open(out_file, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        return subprocess.run([sys.executable, REPORTER, out_file, *extra], capture_output=True, text=True)

    r = run_reporter(good_run(), summary_file)
    summary = open(summary_file, encoding="utf-8").read()
    c.ok("cli", r.returncode == 0 and "All nine suites passed: 534/534 checks." in summary
         and summary.count("✅ passed") == 9 and "::error" not in r.stdout,
         "good run: exit 0, 534/534 in the summary, nine passed rows, no error annotations",
         debug=lambda: (r.returncode, r.stdout, summary))
    r = run_reporter(good_run(**{"test_units.py": {"total": 0}}), summary_file)
    summary = open(summary_file, encoding="utf-8").read()
    c.ok("cli", r.returncode == 1 and "::error title=Test results::test_units.py: reported zero checks" in r.stdout
         and summary.count("## InCityJobs test results") == 2 and "Tests FAILED" in summary,
         "0/0 run: exit 1, error annotation, summary appended rather than overwritten",
         debug=lambda: (r.returncode, r.stdout, summary))
    r = run_reporter(fail_run)
    c.ok("cli", r.returncode == 1 and "❌ failed" in r.stdout and "SOME SUITES FAILED" in r.stdout,
         "failed run without a summary file: exit 1, table printed to standard output",
         debug=lambda: (r.returncode, r.stdout))
    r = subprocess.run([sys.executable, REPORTER, os.path.join(tmp, "missing.txt")], capture_output=True, text=True)
    c.ok("cli", r.returncode == 1 and "could not be read" in r.stdout, "missing output file: exit 1",
         debug=lambda: (r.returncode, r.stdout))
    r = subprocess.run([sys.executable, REPORTER], capture_output=True, text=True)
    c.ok("cli", r.returncode == 2 and "usage" in r.stdout, "no arguments: exit 2 with usage")
    r = run_reporter(good_run()[:-1] + ["SOME SUITES FAILED%0A::notice::forged"])
    c.ok("cli", r.returncode == 1 and "\n::notice" not in r.stdout, "output text can't inject workflow commands",
         debug=lambda: r.stdout)

c.summary()
