# Matching engine assessment (current engine, unchanged)

**Scope:** documents how the existing matching engine (`matching.py`: `Phase4SemanticEngine` and `evaluate_match`) behaves across occupations. Per the approved plan, the engine is **preserved unchanged in this phase**. This document is the testing evidence for deciding on a future extension.

**Reproduce:** `python scripts/assess_matching.py`. It needs no database and no network. The labelled set is in `tests/matching_eval_set.py`, and `tests/test_units.py` pins these exact results, so any engine change fails the test until this document is updated.

## How the engine scores
- **Score:** cosine similarity of word counts between the resume and the job text (title, department and requirements), after removing 13 stop words, shown as a percentage. A candidate matches when the score is ≥ 50%, they're within the job's radius, and both locations are known.
- **"Qualifications" shown to employers:** words from a fixed list of 11 tech terms: python, javascript, typescript, react, node, sql, aws, docker, kubernetes, machine learning, nlp.

## Results: 7 occupations × 3 resume types (threshold 50%)

| Occupation | Short qualified summary | Realistic qualified resume | Unqualified resume | Dictionary skills found |
|---|---|---|---|---|
| Software developer | 74.16% ✅ | **37.30% ❌** (96 words) | 0.00% ✅ | aws, docker, python, sql |
| Warehouse / forklift | 73.99% ✅ | **18.98% ❌** (81) | 3.01% ✅ | none |
| CDL truck driver | 72.11% ✅ | **18.23% ❌** (74) | 5.55% ✅ | none |
| Registered nurse | 87.67% ✅ | **21.06% ❌** (70) | 3.01% ✅ | none |
| Retail cashier | 74.85% ✅ | **6.54% ❌** (67) | 0.00% ✅ | none |
| Welder | 78.77% ✅ | **22.42% ❌** (76) | 0.00% ✅ | aws (wrong meaning, see W4) |
| Administrative assistant | 80.90% ✅ | **12.40% ❌** (68) | 8.73% ✅ | none |
| **Summary** | **7/7 match** (median 74.85%) | **0/7 match** (median 18.98%) | **0/7 false matches** (max 8.73%) | |

✅ = correct outcome, ❌ = a qualified candidate was rejected.

## Weaknesses

**W1. Length penalty: real resumes are rejected.**
- Every realistic, fully qualified one-page resume fails (6.5–37%). The same people's 1–2 line summaries pass (72–88%).
- Cosine similarity over all words means every extra sentence about experience, education or interests lowers the score.
- Measured by padding the same qualified warehouse summary with generic sentences:

| Words | Score |
|---|---|
| 13 | 74% |
| 33 | 42% |
| 63 | 19% |
| 113 | 10% |

Once the Candidate Module accepts uploaded resumes (typically 300–800 words), almost no real candidate will match.

**W2. No synonyms or abbreviations; exact words only.**
- "Commercial driver, Class A license, DOT physical…" scores 48% and is gated, while the same facts phrased with "CDL" pass.
- The realistic resumes say "Basic Life Support" (job asks for "BLS"), "Epic" (job asks for "EHR"), "point-of-sale software" ("POS"), "GMAW/GTAW" ("MIG/TIG"), "QuickBooks" ("invoicing"). None of these count.

**W3. Qualifications are tech-only.**
- 5 of 7 occupations get **no** recognised qualifications. Forklift, CDL, RN, OSHA and welding certifications don't appear in the dictionary.
- In employer profiles, these candidates show "none from the qualification dictionary", even when fully qualified.

**W4. Ambiguous terms.** "AWS" is matched as the cloud skill for a welder ("AWS D1.1" is an American Welding Society certification). A welder's anonymised profile therefore shows "aws" as a qualification, which is misleading.

**W5. Department text affects the score.** The department label is part of the job text: the same developer summary scores 74.16% for "Technical Services" and 76.09% for "Logistics". Jobs posted with the default department ("Logistics") are scored against an unrelated word.

**W6. No weighting of requirements.** "Forklift certification" counts the same as "years" or "experience". A candidate who repeats generic words from the posting can outscore one who has the specific required certification.

**What works:** unqualified candidates are rejected reliably, with no false matches across 7 cross-occupation pairs (max 8.73%). The gates around the score also work: contact email, known locations, legacy fallback exclusion and per-job radius.

## Impact on Recommended Candidates
- Recommendations are correct when they appear: they're consented, anonymised and isolated per employer. But with real resumes they will be **rare**, because of W1.
- Profiles show qualifications only for tech roles (W3), and can show a misleading one for welders (W4).
- No change is made in this phase.

## Recommendations for improving accuracy across occupations
Not implemented. Each step needs approval, and must be re-assessed against the labelled set before it's enabled.

### R1. Score requirement coverage, not whole-document similarity (fixes W1 and W6)
- Turn each job's requirements into a list of items. Split on commas and semicolons, and let employers mark items as **must-have** or **nice-to-have** (a small change to the job form).
- Score = weighted share of requirement items found in the resume. Must-haves gate the match; nice-to-haves rank it.
- Resume length then no longer matters. Extra experience can't lower a score; only missing requirements can.
- Keep the existing gates unchanged: contact email, known locations, fallback exclusion and radius.

### R2. Occupation-aware skills and credentials dictionary (fixes W2, W3 and W4)
- Replace the 11 tech terms with a maintained dictionary grouped by occupation family: tech, logistics and warehouse, transportation, healthcare, retail, skilled trades, office.
- Each entry has its **synonyms and abbreviations**. Examples:
  - `CDL` = commercial driver's license, Class A
  - `BLS` = Basic Life Support
  - `MIG` = GMAW
  - `POS` = point of sale
  - `EHR` = Epic or Cerner
- Detect **credentials with patterns** (e.g. `AWS D1.1`, `OSHA 10/30`, `Class A`, `RN license`). Resolve ambiguous terms by context: `AWS` next to `D1.1` or welding words means the American Welding Society, not the cloud.
- Consider aligning to a public occupation and skills taxonomy (such as O*NET or ESCO) so the vocabulary stays maintained.
- Privacy benefit: employer profiles can then show non-tech qualifications (e.g. "Forklift certification, OSHA 10"). Because these are dictionary labels, they stay safe to show. Free text is still never shown.

### R3. Normalise text before matching
- Handle plurals and stems, numbers ("2+ years", "two years") and phrase matching ("point of sale").
- Leave the department label out of the scored text, or give it a small weight (fixes W5).

### R4. Calibrate per occupation family, with evidence
- Grow the labelled set: more occupations, and several resumes per type including borderline cases.
- Once the Candidate Module collects them with consent, add real anonymised resumes.
- Track precision and recall for each occupation family. Choose thresholds per family rather than one global 50%.
- Acceptance criteria before enabling a new engine:
  - ≥ 90% of realistic qualified resumes match
  - ≤ 5% of unqualified resumes match
  - no occupation family below 80% recall
  - `tests/test_units.py` pins updated together with this document.

### R5. Run the new engine alongside the old before switching
- Compute the new score next to the old one without showing it to employers. Compare results on the staging data.
- Switch only after review. Store the engine version on each recommendation, so changing engines re-surfaces items exactly once (the fingerprint mechanism already supports this).

### R6. Optional, later: semantic embeddings
- A sentence-embedding model can catch paraphrases beyond the dictionary.
- It adds a model dependency, cost and explainability questions. Use it only as a ranking signal on top of R1 and R2, never as the only gate.
- Any external AI service would need a privacy review, because resume text would leave the platform.

**Suggested order:** R3 and R2 (dictionary first) → R1 → R4 calibration → R5 shadow comparison. R1 and R2 together address the 0/7 realistic-resume result. R4 and R5 are what make it safe to switch.
