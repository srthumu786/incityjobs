"""Summarize `bash tests/run_local.sh` output for GitHub Actions.

    python3 tests/ci_summary.py <test output file> [<summary file>]

Writes a Markdown table (one row per suite: passed / failed / did not report) to the summary
file (GitHub's $GITHUB_STEP_SUMMARY), prints each failing check as a GitHub error annotation,
and exits 1 unless all seven suites reported every check passed. Standard library only.
"""
import re
import sys

SUITES = [  # (file, label printed by the suite's summary line)
    ("test_units.py", "unit tests"),
    ("test_migration.py", "migration tests"),
    ("test_e2e.py", "end-to-end tests"),
    ("test_diagnostics.py", "diagnostics tests"),
    ("test_recommendations.py", "recommendation tests"),
    ("test_db_roles.py", "db role tests"),
    ("test_migration_partial.py", "partial-migration tests"),
]
SUMMARY = re.compile(r"^(?P<label>[a-z -]+ tests): (?P<passed>\d+)/(?P<total>\d+) passed")
FAIL = re.compile(r"^FAIL \[(?P<group>[^\]]+)\] (?P<message>.*)$")
SECTION = re.compile(r"^=== (?P<suite>test_[a-z0-9_]+\.py) ===$")


def escape_annotation(text):
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main():
    if len(sys.argv) < 2:
        print("usage: ci_summary.py <test output> [<summary file>]")
        return 2
    try:
        lines = open(sys.argv[1], encoding="utf-8", errors="replace").read().splitlines()
    except OSError:
        lines = []
    results, failures, current = {}, [], None
    for line in lines:
        m = SECTION.match(line)
        if m:
            current = m.group("suite")
            continue
        m = SUMMARY.match(line)
        if m:
            results[m.group("label")] = (int(m.group("passed")), int(m.group("total")))
            continue
        m = FAIL.match(line)
        if m:
            failures.append((current or "unknown suite", m.group("group"), m.group("message")[:300]))

    rows, all_ok, passed_total, checks_total = [], True, 0, 0
    failed_suites = {f[0] for f in failures}
    for file, label in SUITES:
        if label in results:
            passed, total = results[label]
            passed_total, checks_total = passed_total + passed, checks_total + total
            ok = passed == total and file not in failed_suites
            rows.append(f"| `{file}` | {'✅ passed' if ok else '❌ failed'} | {passed}/{total} |")
        else:
            ok = False
            rows.append(f"| `{file}` | ❌ {'failed' if file in failed_suites else 'did not report'} | — |")
        all_ok = all_ok and ok

    verdict = (f"**All seven suites passed: {passed_total}/{checks_total} checks.**" if all_ok
               else f"**Tests FAILED.** {len(failures)} failing check(s); suites that did not finish are marked.")
    out = ["## InCityJobs test results", "", verdict, "", "| Suite | Result | Checks |", "| --- | --- | --- |", *rows]
    if failures:
        out += ["", "### Failing checks", ""] + [f"- `{s}` [{g}] {m}" for s, g, m in failures]
    out += ["", "Throwaway local PostgreSQL on a Unix socket; no Neon, production or repository secrets used."]
    text = "\n".join(out) + "\n"

    if len(sys.argv) > 2 and sys.argv[2]:
        with open(sys.argv[2], "a", encoding="utf-8") as fh:
            fh.write(text)
    else:
        print(text)
    for suite, group, message in failures:
        print(f"::error title={suite}::{escape_annotation(f'[{group}] {message}')}")
    if not all_ok:
        print(f"::error::Tests failed or did not finish ({len(failures)} failing check(s)).")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
