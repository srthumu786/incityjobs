"""Print the matching-engine assessment table (docs/MATCHING_ENGINE_ASSESSMENT.md).

Usage (from the repository root):
    python scripts/assess_matching.py

Uses the labelled set in tests/matching_eval_set.py and the UNCHANGED engine in
matching.py. Needs no database and no network.
"""
import os
import statistics
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [ROOT, os.path.join(ROOT, "tests")]
from matching import MIN_SEMANTIC_SCORE  # noqa: E402
from matching_eval_set import evaluate  # noqa: E402


def main() -> int:
    rows = evaluate()
    print(f"Threshold: {MIN_SEMANTIC_SCORE}%\n")
    print(f"{'Occupation':26} {'Resume':22} {'Words':>5} {'Score':>7}  Result  Correct  Dictionary skills")
    for r in rows:
        print(f"{r['occupation']:26} {r['resume']:22} {r['words']:5} {r['score']:6.2f}%  "
              f"{'match ' if r['passes'] else 'gated '}  {'yes' if r['passes'] == r['should_pass'] else 'NO ':3}      "
              f"{', '.join(r['skills']) or '-'}")
    for kind in ("short (qualified)", "realistic (qualified)", "unqualified"):
        group = [r for r in rows if r["resume"] == kind]
        print(f"\n{kind:22}: {sum(r['passes'] for r in group)}/{len(group)} pass, "
              f"median {statistics.median(r['score'] for r in group):.2f}%, "
              f"range {min(r['score'] for r in group):.2f}-{max(r['score'] for r in group):.2f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
