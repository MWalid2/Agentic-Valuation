# 18. Broader financial inputs and the RawFinancials handoff

Implemented and replayed on 2026-10-14. This step enriches one reporting period;
it does not construct five years, eight quarters, TTM, or normalized financials.

**Subsequent extension:** [19-financial-history.md](19-financial-history.md)
documents the history and standard-disclosure tools now run after this seed
extraction. The single-period results below describe the earlier implementation.

## What changed

The Data Agent now calls `get_raw_financials` after obtaining its SEC snapshot.
The tool rereads the eligible filing's primary statements, extracts supported
standard US-GAAP totals, reconciles them with matching companyfacts, and returns:

- `DataBundle.raw`: the existing `RawFinancials` contract, containing one annual
  or quarterly `FinancialPeriod` with sourced amounts in unscaled USD.
- `DataBundle.raw_issues`: an explanation for each concept absent from that period.
- Warnings and recorded tool calls in the existing quality/audit outputs.

The original snapshot remains available for its eleven-field diagnostics and
separately dated shares. Existing saved snapshots can be reused; enrichment
requires the SEC filing/cache as well. No extra model calls are needed.

## Additional concepts

| Group | Supported extraction |
|---|---|
| Income statement | Cost of sales, SG&A, R&D, gross interest expense, income-tax expense/benefit, pretax income |
| Operating cash-flow adjustments | Combined depreciation/depletion/amortization, stock-based compensation |
| Balance sheet | Current net trade receivables, inventory, accounts payable, net PP&E, goodwill, minority interest |
| Financing cash flows | Common-stock repurchases and dividends |
| Leases | Operating and finance lease liabilities: a direct total or both current and noncurrent components |

These are supported concepts, not guaranteed fields for every issuer. Only a
unique supported standard total is accepted. Multiple candidate rows, custom
tags, incomplete lease components, wrong durations, negative magnitudes where
unsupported, or reconciliation conflicts leave the concept absent. Known
missing values are never converted to zero. Footnote-only disclosures are not
searched by this step.

`RULES` in `data/financials.py` documents the narrow supported tag set. It does
not treat depreciation-only or depreciation-plus-accretion as interchangeable
with combined D&A, nor net interest as gross interest expense. No company names,
tickers, or expected financial amounts appear in the production mappings.

## Handoff semantics

- Snapshot operating profit maps to raw `ebit` with an explicit provenance note:
  operating profit excluding nonoperating income, before normalization. It is
  not a claim to reproduce every issuer's separately defined EBIT measure.
- Borrowing debt and lease liabilities remain separate. The Normalizer must
  later apply the project's lease policy consistently to debt and earnings.
- A cover-page share count observed after year-end stays in the snapshot/market
  output. It is not inserted into the year-end financial period with a false date.
- Every flow must match the requested period start and end. Annual or YTD cash
  flows are not relabelled as quarterly amounts. Quarter differencing is future work.
- Reported tax benefits and pretax losses remain signed. Supported expenses and
  cash outflows use positive magnitudes; unsupported negative cases require review.
- Missing raw concepts produce warnings. Failure to assemble any raw period is
  a blocking collection issue, but other sources are retained. Successful partial
  collection is still not a normalization/valuation readiness decision.

## Recorded replay results

All runs below used existing SEC snapshots, saved SEC filings, and cached external
sources at the unchanged cutoff of 2026-10-11. No new model or network run was used.

| Company | Financial period | Raw concepts present | Absent concepts explained |
|---|---|---|---|
| Apple | FY2025 | 23 | 9 |
| Coca-Cola | FY2025 | 23 | 9 |
| Nike | FY2026 | 23 | 9 |

The missing sets differ. Nike's supported operating lease components are $478M
current plus $2,613M noncurrent, giving $3,091M with both citations retained.
Nike's net interest presentation is not accepted as gross interest expense.
Apple and Coca-Cola do not supply a supported complete lease total in the primary
tables used here; their lease amounts are left absent pending footnote extraction.
Marketable securities, preferred equity, and change in cash are not yet mapped.

The three existing eleven-field reference checks still pass. Those checks cover
the original snapshot, not all new raw fields. New recorded-statement tests check
selected independent totals and rejection of duplicate candidates, period mismatch,
negative magnitudes, missing lease components and reconciliation conflicts.
Integration tests check the new handoff and retention of other sources on failure.

## Running it

The existing `scripts/collect_data.py` commands now include enrichment automatically.
For example:

```powershell
uv run python scripts/collect_data.py --offline `
  --ticker NKE --cik 320187 --name Nike --as-of 2026-10-11 `
  --period-start 2025-06-01 --period-end 2026-05-31 --fiscal-year 2026 `
  --industry Apparel --sec-snapshot data/edgar-agent/snapshots/Nike.json `
  --out outputs/nike-data.json

uv run pytest tests/test_raw_financials.py tests/test_data_agent.py
```

Inspect `raw.annual[0].values` and `raw_issues` in the output. Before implementing
normalization, the remaining work is historical period construction, missing
concept/footnote coverage, and explicit consumer-specific completeness checks.
