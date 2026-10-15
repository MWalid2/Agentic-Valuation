# 14. Single-Period Financial Snapshot

`FinancialSnapshot` is the output boundary for `SnapshotAgent`. The agent selects
cited evidence for eleven fields from one SEC filing; Python reads the amounts,
performs supported derivations, and validates the result.

The snapshot records company CIK, information cutoff, fiscal year/quarter, and
statement start/end dates. Every core field must explicitly be available, missing,
or ambiguous. Missing and ambiguous fields need an explanation and cannot carry
an accepted amount. They are never silently replaced with zero.

| Field group | Fields | Units and dates |
|---|---|---|
| Income statement | Revenue, operating income, net income | USD, full reporting duration |
| Balance sheet | Cash, debt, assets, liabilities, equity | USD, reporting-period end |
| Cash flows | Operating cash flow, capex | USD, reporting duration or explicitly marked YTD |
| Share observation | Shares outstanding | Share count with its actual observation date |

Each available field uses `SnapshotField` with `Sourced[float]`: an amount,
mandatory provenance, units, basis, and observation/period-end date. Duration
fields also carry their actual start date. Cash-flow YTD figures remain labelled
YTD rather than being mistaken for an individual quarter.

Share observation dates may differ from the statement end, but cannot follow the
information cutoff. All other available fields must match the statement end.
SEC evidence or a dated derivation is required; unsupported LLM estimates are
rejected. Operating income is explicitly distinct from a company's broader EBIT
measure; the extraction step must justify any proposed equivalence.

The contract validates dates, stock/flow basis, USD/share units, and basic signs.
Profit, equity, and operating cash flow may be negative; capex uses a nonnegative
outflow magnitude and shares must be positive. Amounts are unscaled dollars and
share counts. `is_complete` checks availability only: it does not establish
accounting correctness or approval. `review_required` is always true.

## Extraction workflow

1. `EDGARTools` selects an eligible filing for the CIK, cutoff, and reporting end.
2. `tools/snapshot.py` reads inline-XBRL evidence from income, balance-sheet,
   cash-flow, and cover-page share disclosures. It preserves labels, tags, units,
   scales, signs, context dates, accession, and document citations.
3. The model selects short evidence IDs in five field groups. It cannot supply
   replacement amounts or fetch arbitrary URLs. A rejected selection receives
   at most one correction attempt per group, using validation feedback.
4. Python checks periods, units, statement type, supported meaning, component
   overlap, and share-class coverage. Matching companyfacts for the same
   accession/context must agree. Absent counterparts generate explicit warnings.
5. The result includes fields, decisions, warnings, and tool history. The balance
   check compares assets with liabilities, parent equity, minority interest, and
   supported temporary equity using a 0.5% tolerance.

Net income and equity belong to parent shareholders. Cash excludes investments;
debt excludes operating leases. Capex means cash PPE purchases, excluding noncash
additions and sale proceeds. Actual cover-page shares include all disclosed
classes and retain their observation date.

Supported operating-income derivations use gross profit less an expense subtotal.
The `snapshot-v5` prompt offers an explicit add/subtract citation template when
unique candidate rows pass validation. The model must still select evidence;
the template contains no expected amount and cannot bypass the final checks.
Nike uses gross profit minus total selling and administrative expense; its EBIT
is a different measure. Unfamiliar custom tags or unsupported derivations remain
ambiguous. This conservative validation does not claim universal taxonomy support.

## Run locally

```powershell
uv run pytest tests/test_snapshot_agent.py tests/contracts/test_snapshot.py
uv run python scripts/check_snapshot_agent.py --offline-sec
```

The second command uses the actual local model and cached SEC sources. Omit
`--offline-sec` to permit SEC retrieval with the configured contact header.
Use `--company Apple`, `--company Coca-Cola`, or `--company Nike` for one case.
It disables model-response caching, temporarily permits 512 output tokens, and
keeps the configured lightweight model and single-worker execution. Records go
to `data/edgar-agent/snapshots/`. Any unmatched field, incorrect share observation
date, or unsuccessful balance check makes the command fail.

Recorded tests cover Apple FY2025, Coca-Cola FY2025, and Nike FY2026 using explicit
model selections. They test extraction and rejection boundaries independently
from model quality. The live evaluation uses these same development cases, not
a held-out sample.

## Limits and next boundary

This is one filing, not annual/quarterly history or TTM construction. The parser
expects English, USD, inline-XBRL tables; unfamiliar layouts may produce partial
output. The caller supplies fiscal metadata and the requested start date. YTD
labeling has a separate test; the three reference filings are annual. Passing
the balance check and filling every field still requires human review, and does
not complete Phase 1 or establish coverage of all nonfinancial companies.
