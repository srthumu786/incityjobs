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

## Options for a future extension (not implemented; each needs approval and re-assessment)
1. **Requirement-coverage scoring:** the share of the job's requirements found in the resume. This removes the length penalty (W1) and W6.
2. **Occupation dictionaries with synonyms and abbreviations** (CDL / commercial driver's license, BLS / Basic Life Support, MIG / GMAW), plus sense disambiguation for terms like "AWS" (W2–W4).
3. **Leave the department label out of the scored text**, or weight it lightly (W5).
4. **Keep this labelled set as the acceptance test,** and extend it with real, anonymised resumes once the Candidate Module collects them with consent.
