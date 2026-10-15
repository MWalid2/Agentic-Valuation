# 13. Cross-Company EDGAR Checks

Historical report for the replaced deterministic adapter. Its smoke script and
period builder were removed. See [the rebuilt agent](12-sec-edgar-adapter.md)
for the current implementation. Commands below are historical.

Checked on 2026-10-13 using an information cutoff of 2026-10-11. Coca-Cola and
Nike are the other reference companies suggested in the roadmap. No mappings
were changed during this check.

## Observed results

| Company | CIK | Annual / quarterly / TTM | Missing required period fields | Current quality checks |
|---|---|---|---|---|
| Apple | 0000320193 | 5 / 8 / yes | 0 | No blocking issues |
| Coca-Cola | 0000021344 | 5 / 8 / yes | 42 | Blocked |
| Nike | 0000320187 | 5 / 8 / yes | 56 | Blocked |

Coca-Cola and Nike submissions and companyfacts downloaded successfully, and
both replayed successfully from the local cache with `--offline`. Coca-Cola had
3,303 eligible submissions and Nike had 3,065. Output period ends, evidence
cutoffs, and filing dates were checked against the requested cutoff for all
three companies. No future-dated evidence was found in the selected results.

The missing counts are field-period pairs, not distinct concepts: each company
has 14 output periods (five annual, eight quarterly, and one TTM).

## Mapping gaps

- Coca-Cola: debt, period-end shares, and total liabilities are missing in all
  14 periods. Its standard facts include lease-inclusive long-term debt tags
  and commercial paper, which the current borrowing-only debt rules do not
  interpret. Lease-inclusive balances must be reconciled before using them.
- Nike: EBIT, debt, period-end shares, and total liabilities are missing in all
  14 periods. Its facts include current/noncurrent term debt and a `LongTermDebt`
  balance, but the current generic rule requires `DebtCurrent`. The meaning and
  completeness of its debt layout need filing verification. Its reported income
  statement also needs a supported EBIT extraction or derivation rule.
- Missing period-end shares must not be replaced with weighted-average shares
  or cover-page shares observed on a different date. These concepts have
  different roles and require separately dated handling where appropriate.
- Total liabilities may need a supported component sum or filing-table fallback;
  inferring it from assets minus equity would not independently validate the
  balance-sheet identity.

The adapter works for retrieval and period construction on these companies, but
its current mappings do not yet produce valuation-ready datasets for them.
Successful execution is not sufficient to mark either company as passing.

## Reproduce locally

```powershell
uv run python scripts/smoke_edgar.py 21344 2026-10-11 --offline
uv run python scripts/smoke_edgar.py 320187 2026-10-11 --offline
```

Omit `--offline` to download a missing snapshot. Existing snapshots are reused
unless `--refresh` is specified. Saved outputs are under `data/edgar-results/`.
Public source endpoints are the SEC companyfacts APIs for
[Coca-Cola](https://data.sec.gov/api/xbrl/companyfacts/CIK0000021344.json) and
[Nike](https://data.sec.gov/api/xbrl/companyfacts/CIK0000320187.json), plus each
company's submissions endpoint.

This check establishes integration behavior and exposes mapping gaps. It does
not meet the full Phase 1 requirement to reconcile key figures against filing
tables within 0.5%. The next step is to verify and implement the missing rules,
with recorded fixtures and regression tests for each company.
