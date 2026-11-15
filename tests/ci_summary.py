"""Summarize `bash tests/run_local.sh` output for GitHub Actions.

    python3 tests/ci_summary.py <test output file> [<summary file>]

Writes a Markdown table (one row per suite) to the summary file (GitHub's $GITHUB_STEP_SUMMARY),
prints each problem as a GitHub error annotation, and exits 0 only if the output shows a complete,
successful run. Standard library only.

A run counts as successful only if ALL of these hold:
  - each of the eight suites appears exactly once, in the order run_local.sh runs them;
  - each suite prints exactly one summary line, with its own label, inside its own section;
  - that line reports exactly the expected number of checks (EXPECTED below; never zero), all passed;
  - the per-group counts in that line add up to the same total, each group fully passed;
  - the section contains exactly that many `PASS [...]` lines and no `FAIL [...]` line;
  - no summary, PASS or FAIL line appears before the first suite section, and no unknown suite
    section appears;
  - the last non-blank line is run_local.sh's final `ALL SUITES PASSED` marker, it appears once,
    after the last suite, and `SOME SUITES FAILED` does not appear.

When a suite gains or loses checks on purpose, update EXPECTED in the same commit.
"""
import re
import sys

# (file, label printed by the suite's summary line, expected number of checks), in run order.
EXPECTED = [
    ("test_units.py", "unit tests", 41),
    ("test_migration.py", "migration tests", 34),
    ("test_e2e.py", "end-to-end tests", 72),
    ("test_diagnostics.py", "diagnostics tests", 33),
    ("test_recommendations.py", "recommendation tests", 59),
    ("test_db_roles.py", "db role tests", 69),
    ("test_migration_partial.py", "partial-migration tests", 90),
    ("test_startup_readonly.py", "startup read-only tests", 36),
]
EXPECTED_TOTAL = sum(n for _, _, n in EXPECTED)  # 434
FINAL_OK, FINAL_FAILED = "ALL SUITES PASSED", "SOME SUITES FAILED"

SECTION = re.compile(r"^=== (?P<suite>\S+) ===$")
SUMMARY = re.compile(r"^(?P<label>[a-z -]+ tests): (?P<passed>\d+)/(?P<total>\d+) passed(?: \((?P<groups>[^()]*)\))?\s*$")
GROUP = re.compile(r"^(?P<name>[a-z][a-z0-9 _-]*) (?P<passed>\d+)/(?P<total>\d+)$")
PASS = re.compile(r"^PASS \[[^\]]+\] ")
FAIL = re.compile(r"^FAIL \[(?P<group>[^\]]+)\] (?P<message>.*)$")


def escape_annotation(text):
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def read_lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().splitlines()
    except OSError:
        return None


def parse(lines):
    """Split the output into the preamble, the suite sections (in order) and the final markers."""
    preamble, sections, finals = [], [], []
    current = None
    for number, line in enumerate(lines, 1):
        m = SECTION.match(line)
        if m:
            current = {"suite": m.group("suite"), "line": number, "summaries": [], "passes": 0, "fails": []}
            sections.append(current)
            continue
        if line.strip() in (FINAL_OK, FINAL_FAILED):
            finals.append((number, line.strip()))
            continue
        if current is None:
            preamble.append((number, line))
            continue
        m = SUMMARY.match(line)
        if m:
            current["summaries"].append(m)
        elif PASS.match(line):
            current["passes"] += 1
        else:
            m = FAIL.match(line)
            if m:
                current["fails"].append((m.group("group"), m.group("message")[:300]))
    return preamble, sections, finals


def check_suite(file, label, expected, section):
    """Return (row cells, list of problems) for one expected suite."""
    if section is None:
        return ("❌ did not report", "—"), [f"{file}: no `=== {file} ===` section in the output"]
    problems = []
    for group, message in section["fails"]:
        problems.append(f"{file}: FAIL [{group}] {message}")
    summaries = section["summaries"]
    if not summaries:
        problems.append(f"{file}: no summary line (the suite stopped early or its output was cut off)")
        counts = f"{section['passes']} passed lines / {expected} expected"
        return ("❌ failed" if section["fails"] else "❌ incomplete", counts), problems
    if len(summaries) > 1:
        problems.append(f"{file}: {len(summaries)} summary lines; expected exactly one")
    m = summaries[-1]
    passed, total = int(m.group("passed")), int(m.group("total"))
    if m.group("label") != label:
        problems.append(f"{file}: summary label is '{m.group('label')}', expected '{label}'")
    if total == 0:
        problems.append(f"{file}: reported zero checks")
    if total != expected:
        problems.append(f"{file}: reported {total} checks, expected {expected}")
    if passed != total:
        problems.append(f"{file}: {passed}/{total} passed")
    if section["passes"] != expected:
        problems.append(f"{file}: {section['passes']} PASS lines in the output, expected {expected}")
    groups_text = m.group("groups")
    if not groups_text:
        problems.append(f"{file}: summary line has no per-group counts")
    else:
        group_passed = group_total = 0
        for part in groups_text.split(", "):
            g = GROUP.match(part.strip())
            if not g:
                problems.append(f"{file}: unreadable group count '{part.strip()}'")
                continue
            gp, gt = int(g.group("passed")), int(g.group("total"))
            group_passed, group_total = group_passed + gp, group_total + gt
            if gt == 0 or gp != gt:
                problems.append(f"{file}: group '{g.group('name')}' {gp}/{gt}")
        if (group_passed, group_total) != (passed, total):
            problems.append(f"{file}: group counts add up to {group_passed}/{group_total}, "
                            f"summary says {passed}/{total}")
    result = "✅ passed" if not problems else ("❌ failed" if section["fails"] or passed != total else "❌ rejected")
    return (result, f"{passed}/{total}"), problems


def evaluate(lines):
    """Return (all_ok, markdown rows, problems, passed checks) for the given output lines."""
    if lines is None:
        return False, [], ["the test output file could not be read"], 0
    if not any(line.strip() for line in lines):
        return False, [], ["the test output is empty"], 0
    preamble, sections, finals = parse(lines)
    problems = []

    for number, line in preamble:
        if SUMMARY.match(line) or PASS.match(line) or FAIL.match(line):
            problems.append(f"line {number}: test result outside any suite section: {line[:200]}")

    expected_files = [f for f, _, _ in EXPECTED]
    seen = [s["suite"] for s in sections]
    for name in sorted(set(seen) - set(expected_files)):
        problems.append(f"unknown suite section '{name}'")
    for name in expected_files:
        if seen.count(name) > 1:
            problems.append(f"{name}: appears {seen.count(name)} times")
    known_order = [s for s in seen if s in expected_files]
    if len(set(known_order)) == len(expected_files) and known_order != expected_files:
        problems.append("suites ran in an unexpected order: " + ", ".join(known_order))

    rows, passed_checks = [], 0
    by_name = {}
    for s in sections:
        by_name.setdefault(s["suite"], s)
    for file, label, expected in EXPECTED:
        (result, counts), suite_problems = check_suite(file, label, expected, by_name.get(file))
        rows.append(f"| `{file}` | {result} | {counts} | {expected} |")
        problems += suite_problems
        if not suite_problems:
            passed_checks += expected

    last_line = next((line.strip() for line in reversed(lines) if line.strip()), "")
    ok_markers = [n for n, text in finals if text == FINAL_OK]
    if any(text == FINAL_FAILED for _, text in finals):
        problems.append(f"run_local.sh reported '{FINAL_FAILED}'")
    if not ok_markers:
        problems.append(f"the final '{FINAL_OK}' marker is missing (the run did not finish or the output was cut off)")
    else:
        if len(ok_markers) > 1:
            problems.append(f"'{FINAL_OK}' appears {len(ok_markers)} times")
        if sections and ok_markers[-1] < sections[-1]["line"]:
            problems.append(f"'{FINAL_OK}' appears before the last suite")
        if last_line != FINAL_OK:
            problems.append(f"output continues after '{FINAL_OK}': {last_line[:200]}")

    return not problems, rows, problems, passed_checks


def report(all_ok, rows, problems, passed_checks):
    if all_ok:
        verdict = f"**All eight suites passed: {passed_checks}/{EXPECTED_TOTAL} checks.**"
    else:
        verdict = f"**Tests FAILED or the output is incomplete.** {len(problems)} problem(s) found."
    out = ["## InCityJobs test results", "", verdict, "",
           "| Suite | Result | Checks | Expected |", "| --- | --- | --- | --- |", *rows]
    if problems:
        out += ["", "### Problems", ""] + [f"- {p}" for p in problems]
    out += ["", "Throwaway local PostgreSQL on a Unix socket; no Neon, production or repository secrets used."]
    return "\n".join(out) + "\n"


def main(argv):
    if len(argv) < 2:
        print("usage: ci_summary.py <test output> [<summary file>]")
        return 2
    all_ok, rows, problems, passed_checks = evaluate(read_lines(argv[1]))
    text = report(all_ok, rows, problems, passed_checks)
    if len(argv) > 2 and argv[2]:
        with open(argv[2], "a", encoding="utf-8") as fh:
            fh.write(text)
    else:
        print(text)
    for problem in problems:
        print(f"::error title=Test results::{escape_annotation(problem)}")
    if all_ok:
        print(f"All eight suites passed: {passed_checks}/{EXPECTED_TOTAL} checks.")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
