# 19. Historical periods, TTM, and standard filing disclosures

Implemented on 2026-10-14. `DataBundle.raw` now targets five annual periods,
eight standalone quarters, and a trailing-twelve-month period. All amounts
retain dated SEC provenance. Period coverage and concept completeness are
separate: a constructed period may still lack required normalization inputs.

## Workflow and responsibilities

1. The existing SEC interpreter provides its cited snapshot.
2. `get_raw_financials` enriches that period from primary statements and standard
   XBRL disclosures in the exact same accession and filing date.
3. `get_financial_history` reads eligible SEC submissions and companyfacts and
   calls the deterministic builder in `data/history.py`. If companyfacts omits
   a recent filing's calendar facts, it reads that filing's inline-XBRL primary
   statements directly. This fallback checks fiscal metadata, preserves numeric
   contexts/citations, and is bounded to eight missing filings among the latest
   28 eligible filings. Offline mode requires their documents to be cached.
4. The builder constructs fiscal periods and performs quarter/TTM arithmetic.
   The Data Agent retains diagnostics alongside the typed RawFinancials result.

This adds no LLM calls per historical period. Existing saved interpretations and
SEC caches work with the normal `scripts/collect_data.py --offline` commands.
If history construction fails, the already collected seed and other sources
are retained and the collection quality report records the failure.

## Fiscal calendar and point-in-time rules

- Both filing dates and reporting ends must be on or before the requested cutoff.
- Annual calendar anchors use a 10-K's own report date and full-year duration.
  A comparative fact's `fy`/`fp` is not assumed to describe its historical period.
- Quarterly boundaries are tied to that fiscal year's start and compatible YTD
  disclosures in the 10-Q. Non-December years and 52/53-week years are supported.
- Full-year intervals must span 350-380 days; quarter intervals must span 70-110
  days. Fiscal transitions outside this supported range need review.
- For an exact concept, unit and interval, use the latest eligible filing date.
  Conflicting values/aliases at that date remain unavailable; do not silently
  fall back to an older observation. All components retain their filing vintage.
- Component totals such as leases and marketable securities require compatible
  current/noncurrent disclosures. Historical components from different filing
  vintages are not combined into a purported reported total.

## Standalone quarters and TTM

For flows, Q1 is the three-month YTD value. If a directly reported standalone
quarter exists, use it. Otherwise:

```text
Q2 = six months - first three months
Q3 = nine months - first six months
Q4 = full year - first nine months
TTM flow = latest four consecutive standalone quarters
TTM balance-sheet value = latest quarter-end balance
```

Subtraction requires the same fiscal start and consecutive cumulative endpoints.
It never subtracts a stock value or treats six-month cash flow as a single quarter.
An unsupported negative expense/outflow produced by subtraction is rejected.
Signed profits, tax benefits and cash flows retain their signs.

If a directly reported quarter disagrees with cumulative subtraction, retain the
reported quarter and flag the tie-out for vintage/rounding review. Cross-filing
subtraction explicitly records that comparability needs review: correct dates
alone cannot prove that restatements or accounting-policy changes are consistent.

TTM requires four consecutive fiscal quarters spanning a supported full-year
duration, including revenue in every quarter. Each other flow is included only
when all four values exist. Missing quarters or a newer known but unsupported
filing suppress TTM instead of returning an apparently current older result.

## Filing disclosures beyond the primary tables

The enrichment tool can now obtain supported standard totals from companyfacts
for the exact filing when the primary statement does not supply them. These
include lease liabilities and gross interest expense where disclosed. Source is
recorded as `sec_xbrl`, with the accession, tag and dates; this is structured
filing evidence, not a claim that a model read the surrounding prose.

Repeated identical facts are deduplicated. Conflicting values, a different
accession, a later filing, incomplete component pairs, or wrong durations are
not accepted. A primary-statement conflict is not bypassed with a fallback.

Marketable securities can use a supported total or current plus noncurrent
marketable securities. A current-only balance is not assumed to be the whole
portfolio. Change in cash supports the explicit cash-and-cash-equivalents tag;
restricted-cash-inclusive totals are not relabelled to fit it.

This does not implement general prose/footnote interpretation or arbitrary custom
tags. Preferred equity and unsupported presentations remain absent. Missing
historical borrowing-debt totals are not replaced by long-term debt alone, and
unreported historical operating income is not automatically replaced by EBIT
or gross profit minus an assumed complete expense set.

## Diagnostics and downstream use

- `financials` remains the original single-period snapshot, including cover-page
  shares at their actual observation date.
- `raw` contains the constructed annual/quarterly/TTM periods. Historical
  fiscal-end shares, when explicitly disclosed, are separate observations.
- `raw_issues` describes the seed enrichment step. History may subsequently find
  an independently dated value for a concept missing from that seed.
- `history_issues` scopes missing values, conflicts and calendar problems to the
  relevant period/concept. The quality report gives a concise count and coverage.
- Every result still requires review. Five annual periods and eight quarters do
  not establish normalization readiness, consistent restatements, or Phase 1 acceptance.

The next downstream consumer must check its required concepts, accounting basis,
lease treatment and historical continuity before calculating normalized results.

## Verification

Offline tests use hand-computed annual/YTD/quarter values to check Q2/Q3/Q4 and
TTM, latest-quarter stock selection, non-December 52/53-week calendars, future
filing rejection, comparative fiscal labels, conflicting aliases, missing quarters,
stale TTM suppression and reported-versus-derived discrepancies. Separate tests
cover exact-filing disclosure fallback and source failure isolation.

During the Coca-Cola replay, submissions contained the 2026-07-03 quarter but
companyfacts did not. A live refresh of the public companyfacts API still omitted
it. The primary filing was retrieved and cached, then the direct-filing fallback
successfully supplied that quarter on an offline replay. The earlier TTM was
suppressed until this missing quarter became available.

The Apple, Coca-Cola and Nike saved bundles are also replayed with the existing
2026-10-11 cutoff. The cohort checker verifies five annual periods, eight
consecutive quarters and TTM arithmetic in addition to its original eleven-field
snapshot references. The historical arithmetic check is not independent validation
of every historical amount; the hand-computed tests check the engine separately.

| Company | Annual periods | Standalone quarters | TTM end | Available TTM concepts |
|---|---|---|---|---|
| Apple | 5 | 8 | 2026-06-27 | 24 |
| Coca-Cola | 5 | 8 | 2026-07-03 | 22 |
| Nike | 5 | 8 | 2026-08-31 | 20 |

The additional standard disclosures include Apple FY2025 operating leases
($12.490B), finance leases ($1.230B) and marketable securities ($96.486B), plus
Coca-Cola FY2025 operating leases ($1.722B), interest expense ($1.654B) and
marketable securities ($1.934B). Their source tags/accessions are retained in
the saved bundles. Other missing concepts remain visible in `history_issues`.

```powershell
uv run pytest tests/test_history.py tests/test_raw_financials.py tests/test_data_agent.py
uv run python scripts/check_data_bundles.py `
  outputs/apple-data.json outputs/coca-cola-data.json outputs/nike-data.json
```

Outputs remain partial where concepts cannot be safely resolved. Broader company
coverage, custom disclosures, and normalization readiness remain separate work.

Final local verification: 342 tests passed; lint, formatting and type checks
passed. All three rebuilt bundles passed the expanded cohort checker.
