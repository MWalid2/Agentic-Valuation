# 16. Multi-company Data Agent checks

Tested on 2026-10-13, using a valuation cutoff of 2026-10-11. This is a
development-case integration test, not evidence of universal company coverage.
The initial extraction implementation was held fixed during the first runs.
Nike was then rerun after the generic citation-template refinement below.

## Results

| Company | Financial period | Financial values matched | Prices | FRED series | Damodaran tables | Combined result |
|---|---|---|---|---|---|---|
| Apple (previous baseline) | FY2025, ended 2025-09-27 | 11/11 | 500 | 3/3 | 5/5 | Passed |
| Coca-Cola | FY2025, ended 2025-12-31 | 11/11 | 500 | 3/3 | 5/5 | Passed |
| Nike (snapshot-v5 rerun) | FY2026, ended 2026-05-31 | 11/11 | 500 | 3/3 | 5/5 | Passed |

In the initial runs, both new cases passed the balance-sheet identity check and matched the actual
cover-page share observation dates. Coca-Cola needed one debt correction. Nike
needed corrections for debt, liabilities, and equity, which were accepted only
after validation. Its operating-income correction still failed.

The initial uncached Qwen3 4B SEC runs took approximately 6.5 minutes for Coca-Cola and
7.7 minutes for Nike. They ran sequentially with `snapshot-v4` instructions,
512 output tokens, and model-response caching disabled. SEC documents were
replayed from the saved raw filings. Yahoo prices were fetched live for each
ticker. FRED and Damodaran reused the eligible shared cutoff/version caches from
the preceding live adapter check; they do not need company-specific rate data.
The final bundles were then rebuilt offline with the fresh SEC interpretations.

## What was verified

- All eleven financial fields were compared to the independently recorded filing
  references in `tests/fixtures/sec/snapshot-reference.json`, within one dollar
  or one share. Expected amounts were not given to the model.
- The financial period and actual share observation date matched each reference.
- Each company had 500 dated Yahoo daily observations and all three FRED series:
  DGS10, BAMLC0A0CM, and T10YIE.
- All five Damodaran datasets were available at the cutoff. The selected industry
  was `Beverage (Soft)` for Coca-Cola and `Apparel` for Nike.
- Rate availability, table publication dates, and bundle identity/cutoff contracts
  were validated. Macro assembly succeeded for both companies.
- Derived market cap agreed with price times split-aligned SEC shares. This is
  an arithmetic check, not an independent market-cap comparison.
- The Data Agent retained usable external inputs when a financial field failed,
  and its quality gate correctly blocked Nike's incomplete financial snapshot.

## Nike failure and verified refinement

The supported reference derivation is gross profit less total selling and
administrative expense: $19.911B - $16.114B = $3.797B. These rows come from Nike's
[FY2026 filing](https://www.sec.gov/Archives/edgar/data/320187/000032018726000088/nke-20260531.htm).

The first model answer selected gross profit and two expense components as
additions, without a subtraction. Its correction selected only the SG&A total.
Neither is operating income, so both were rejected in the original run. The field
was left ambiguous with no accepted amount. Debt, revenue, net income, cash, assets, liabilities,
equity, CFO, capex, and shares all matched their references.

Nike's two share-class citations have no matching companyfacts counterparts;
their original filing evidence is retained with explicit warnings. All bundles
also retain the existing limitations around retrospective price data, carried-
forward SEC shares, Treasury-yield proxies, and mandatory human review.

The `snapshot-v5` refinement supplies a validated citation template for a unique
gross-profit/expense-total pair. It contains no company name or expected amount.
The local model still selects the evidence, and Python validates and calculates
the result. Direct operating-income totals take precedence; known omitted
operating expenses prevent this derivation.

The uncached Qwen3 4B rerun took 413.81 seconds (about 6.9 minutes), using saved
SEC documents. It selected gross profit `I3` minus SG&A `I6` on the first attempt.
All eleven values, the share observation date, and the balance check passed.
Debt, liabilities, and equity still needed their bounded correction. The two
share-class companyfacts warnings remain. The combined Nike bundle was rebuilt
offline with the new SEC result and eligible saved external responses; no new
Yahoo/FRED/NYU requests were made for this rerun.

All three saved bundles now pass the reference checker. Apple and Coca-Cola
retain their earlier live-model results; their recorded extraction tests also
pass with the changed code. The local suite has 312 passing tests, including
both prior wrong Nike selections and derivation rejection boundaries. Lint,
formatting, and type checks pass. This fixes the observed development-case
failure, not the full historical-financial or Phase 1 acceptance criteria.

## Artifacts and repeatable checks

- `outputs/coca-cola-data.json`: complete Coca-Cola bundle.
- `outputs/nike-data.json`: complete eleven-field Nike bundle and diagnostics.
- `outputs/data-agent-cohort-check.json`: per-company checks and field matches.
- `data/edgar-agent/snapshots/{Coca-Cola,Nike}.json`: uncached model decisions,
  citations, warnings, timing, and reference comparisons.

Check the saved bundles without network access or another LLM run:

```powershell
uv run python scripts/check_data_bundles.py `
  outputs/apple-data.json outputs/coca-cola-data.json outputs/nike-data.json
```

The checker now exits with status 0 for the three saved bundles. Mandatory review
and source limitations remain; see [the architecture review](17-data-layer-review.md).
Use the commands in
[the source-adapter guide](15-data-agent-sources.md) to rebuild bundles; use
`scripts/check_snapshot_agent.py --offline-sec --company Nike` for a fresh local
model evaluation against cached SEC evidence.
