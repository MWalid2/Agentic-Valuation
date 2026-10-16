# RAG gold sets (Phase 3a, step 2)

Built on 2026-10-15 by `scripts/build_gold_sets.py`, before any retrieval code
existed, from each cohort company's latest 10-K filed by 2026-10-11.

## gold-xbrl.jsonl: XBRL-labelled questions (frozen)

One question per tagged note fact in a fixed bank (debt maturities, debt fair
value and rate, lease payments and discount rates, options, unvested awards,
antidilutive securities, unrecognized compensation, tax rates, unrecognized tax
benefits, loss carryforwards, restructuring, impairments, pension obligation).

| Field | Meaning |
|---|---|
| `answer`, `unit` | Consolidated fiscal-year-end value as the filing states it (unit resolved: USD, shares, pure, USD/shares) |
| `reference_fact_ids` | Inline-XBRL IDs of the facts stating it; a passage containing any of them answers the question |
| `distractors` | Earlier-period values of the same concept (the wrong column a reader might pick) |
| `split` | `test` when the first byte of SHA-256(ticker) is below 0x55, else `dev` |

Test: 225 questions, 17 companies. Development: 514 questions, 34 companies.
Questions and answers must not be edited; tuning uses only `dev`.

sha256: a72c9a125c3f9af576c17441099970f8b4c9dd30af4bc2c47f5161f7f897fa27

## narrative-draft.jsonl and narrative-review.md: narrative questions (to review)

30 questions about facts stated in prose (impairments, restructuring, debt
issued, buyback authorizations, disposals, litigation, acquisitions), 22
companies. The reference answer is the filing's own sentence containing the
fact. Review `narrative-review.md` (keep, fix or drop); the reviewed set becomes
`narrative.jsonl` and is frozen the same way.
