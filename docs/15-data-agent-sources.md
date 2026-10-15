# 15. External adapters and the Data Agent

The Data Agent combines a SEC financial snapshot and historical financials with
market prices, macro rates, and Damodaran benchmarks. It calls every configured source, preserves
provenance, and returns a typed `DataBundle` with a `DataQualityReport`.
An unavailable source remains missing; the agent does not invent a replacement.

## Tools and responsibilities

| Tool | Source | Result |
|---|---|---|
| `get_sec_snapshot` | SEC EDGAR and the local snapshot interpreter | Eleven financial fields, dated shares, citations, balance validation |
| `get_raw_financials` | SEC primary statements and companyfacts reconciliation | Broader single-period RawFinancials and explicit missing concepts |
| `get_financial_history` | Dated SEC submissions and companyfacts | Five annual periods, eight standalone quarters, TTM, and scoped diagnostics |
| `get_price_history` | yfinance, then Stooq, then saved responses | Daily USD closes, split observations, source and adjustment diagnostics |
| `get_fred_rate` | FRED/ALFRED real-time API | DGS10 Treasury yield, BAMLC0A0CM investment-grade option-adjusted spread, T10YIE ten-year breakeven inflation |
| `get_damodaran` | NYU Stern | US industry betas, margins, sales-to-capital, country risk premiums, and large nonfinancial-firm rating/spread ranges |

`tools/data.py` binds these methods to one resolved ticker, CIK, and cutoff.
`agents/data.py` calls them in a deterministic sequence. This follows the Data
Agent specification: routine fetching needs no additional LLM calls. The SEC
snapshot interpreter still uses the local model unless a matching saved result
is provided. LangGraph wiring and open-ended source discovery are separate work.

Since Phase 2 (see [the Phase 2 plan](23-phase-2-plan.md)) the industry is
mapped automatically from Damodaran's company file (`get_industry`: ticker first,
SIC majority second) unless an exact label is passed, and the fiscal period,
seed values and shares come from the latest 10-K package (`get_filing_seed`,
`get_cover_shares`). The SEC snapshot below is an optional cross-check. The
country is supplied explicitly and defaults to `United States`. Industry tables
currently cover the US; the country-risk table covers other countries.

## What the handoff contains

`DataBundle` includes:

- The SEC `FinancialSnapshot`, available price history, and assembled `MarketData`.
- Enriched single-period `RawFinancials` and `raw_issues`; see [the handoff guide](18-raw-financials.md).
- All three FRED observations, even if macro assembly cannot complete.
- `MacroData`, combining Treasury yield, mature-market ERP, country risk, credit
  spread context, and inflation expectations when available.
- Selected industry rows, the country-risk table, and rating/spread ranges.
- Quality checks, missing inputs, recorded price fallbacks, and the tool-call log.

The country table's total equity premium already includes country risk. The
agent subtracts country risk to obtain mature-market ERP and supplies the two
components separately. This avoids adding country risk twice. These premiums
come from Damodaran's country-risk methodology, not a separate monthly ERP feed.
See the [country-risk table and methodology](https://pages.stern.nyu.edu/adamodar/New_Home_Page/datafile/ctryprem.html).

DGS10 is a Treasury-yield proxy at this stage. Sovereign-default adjustments,
country exposure weighting, and a company-specific synthetic credit rating
belong in the WACC calculation. The FRED investment-grade OAS is market context,
not an automatically assigned company borrowing spread. T10YIE is breakeven
inflation, not a pure inflation forecast.

## Dates, units, and caching

**FRED:** the adapter sets both `realtime_start` and `realtime_end` to the cutoff,
then selects the latest eligible observation. This uses the historical vintage
rather than today's revised value. Percent values are divided by 100. Missing
`.` values are skipped, invalid values are rejected, and daily observations more
than ten days old are unavailable. `available_on` records the API's real-time
boundary; it is not asserted to be the original publication date. See the
[FRED observations API](https://fred.stlouisfed.org/docs/api/fred/series_observations.html)
and [real-time periods](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html).

**Damodaran:** the adapter reads the original HTML tables and stores each changed
response as a separate content-hashed version. It uses an explicit update date,
or the [data index's full-update date](https://pages.stern.nyu.edu/adamodar/New_Home_Page/datacurrent.html)
when it matches the table's analysis month. Without publication evidence, a
version becomes eligible only on its first retrieval date. A historical request
cannot use a version published after its cutoff; it needs an eligible saved
version. This is metadata-based eligibility, not a complete historical archive.
Percentage cells become decimals, dollar-million columns become unscaled USD,
and ratios/counts retain explicit units. Unknown or missing cells are not zero.

**Prices:** `Ticker.history` explicitly disables automatic dividend adjustment
and repair and requests corporate actions. Yahoo Close still uses a split basis;
the adapter fetches subsequent actions and reverses splits after the requested
cutoff. Earlier history remains split-adjusted through that cutoff. Current-day
bars are excluded conservatively, and a last observation older than seven days
is rejected. Vendor history may have been revised: it is explicitly not marked
as a certified point-in-time archive. See the
[yfinance history interface](https://ranaroussi.github.io/yfinance/reference/api/yfinance.Ticker.history.html).

Stooq is a fallback for simple US symbols. Its adjustment basis is left
unverified; those prices remain visible, but the agent does not combine them
with SEC shares to claim a market cap. Class-share ticker translations are not
guessed. SEC shares are adjusted for splits between their observation and the
price date. Subsequent buybacks or issuance are not estimated, so the derived
market cap remains approximate and is not an independent cross-check.

Raw responses live under `data/raw/{fred,damodaran,prices}/`. Credentials are
excluded from cache keys, stored responses, provenance, and request-error text.
FRED needs `FRED_API_KEY`; SEC needs `SEC_USER_AGENT`. Existing `.env` values are
used without modification. `yfinance` was installed with `uv add yfinance`.

## Run the combined collection

This example reuses the matching Apple SEC interpretation from the snapshot
evaluation and fetches the other sources:

```powershell
uv run python scripts/collect_data.py `
  --ticker AAPL --as-of 2026-10-11 `
  --sec-snapshot data/edgar-agent/snapshots/Apple.json `
  --out outputs/apple-data.json
```

`--sec-snapshot` cross-checks a saved interpretation; `--interpret` runs a fresh
one with the configured local model; with neither, no model is called.
The script temporarily permits 512 output tokens for that role. A saved SEC
interpretation must match CIK, cutoff, period, year, and quarter exactly.

Add `--offline` to replay saved external responses; offline mode requires the
saved SEC interpretation. A fresh local model call is therefore not hidden in
an offline collection. `--price-start` overrides the default two-year interval.
The script writes partial results even when a required input is unavailable,
then exits with status 1. It exits with 0 when there are no blocking quality
checks; warnings and mandatory human review still remain visible.

## Verification and remaining scope

The live Apple run on 2026-10-13 used the saved, validated SEC snapshot and made
real requests to Yahoo, FRED, and NYU. It returned 500 daily prices, all three
FRED series, and all five Damodaran datasets, with no missing required inputs.
The same request replayed successfully offline. This checks data retrieval and
assembly; it is not a claim that all SEC snapshot interpretations are reliable.
The [multi-company follow-up](16-data-agent-company-checks.md) now passes
Coca-Cola's complete bundle. After the citation-template refinement, Nike also
matches all eleven financial fields in an uncached local-model run. All three
saved bundles pass the cohort checker. See the
[architecture review](17-data-layer-review.md) for downstream handoff gaps.

Offline tests include recorded public responses, rate/unit conversion, vintage
filtering, publication cutoffs, split handling, price-source failure and cache
fallback, secret redaction, invalid industry labels, and independent source
outages during Data Agent collection.

The [history extension](19-financial-history.md) now constructs annual, quarterly
and TTM periods and adds standard-disclosure fallback. Weekly return series,
normalization, WACC calculations and DCF valuation remain separate work. A bundle
with no missing source inputs is not equivalent to completing Phase 1.
