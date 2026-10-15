# 22. Valuation core: normalizer, DCF engine, WACC, Excel and CLI

Built on 2026-10-15 to complete the deterministic Phase 1 core. No language
model is involved: every valuation number comes from tested Python, and the
Excel workbook reproduces it with live formulas.

```text
DataBundle --assemble--> engine inputs --normalize--> NormalizedFinancials
                          |                              (history, Excel)
                          +--> WACC calculators --> WACCResult
                          +--> scenario assumptions (YAML) --> ForecastAssumptions
                                         |
                                 DCF engine (3 scenarios) --> ValuationResult x3
                                         |
                        Excel workbook --LibreOffice recalc--> parity check
```

## Normalizer v0 (`engine/normalize.py`)

Turns reported annual and TTM periods into economic metrics
(`NormalizedFinancials`), using the v1 defaults:

| Item | Treatment |
|---|---|
| Operating leases | Treated as debt: EBIT gains the lease interest component (lease liability x pre-tax cost of debt); invested capital includes the lease liability |
| Stock-based compensation | An operating expense (not added back) |
| R&D | Not capitalized (flag stays off) |
| Tax | Effective rate when between 0% and 50%; otherwise the marginal rate |
| Invested capital | Financing side: parent equity + minority interest + preferred + temporary-equity claims + debt (+ leases) - cash - marketable securities |
| ROIC | NOPAT / average invested capital |
| Reinvestment | Capex - D&A + change in operating working capital (receivables + inventory - payables) |
| Adjustments | Approved `Adjustment` records change clean EBIT or invested capital and are listed by ID |

Evidenced absence counts as zero; a genuinely missing input stops that period
with a reason. ROIC above 100% and non-positive invested capital are flagged
(Apple's buybacks and $132B of cash and securities give ROIC above 200%).

## DCF engine (`engine/dcf.py`)

- **FCFF** = EBIT (1 - t) - change in revenue / sales-to-capital.
- **Stub period and mid-year**: s = (first fiscal year end - valuation date) / 365;
  year-1 FCFF is scaled by s and discounted at s/2; year k at s + (k - 2) + 0.5.
- **Terminal value**: Gordon growth on next-year NOPAT with reinvestment g / ROIC,
  at the terminal WACC, discounted at the end of the horizon less half a year.
- **Guardrails** (hard errors): terminal WACC above growth, growth below terminal
  ROIC and not above the risk-free rate, positive sales-to-capital, consistent
  horizons.
- **Bridge**: signed contributions from enterprise value to equity value.
- **Dilution**: RSUs in full, options by the treasury stock method at the
  estimated intrinsic value (iterated to a fixed point), convertibles by the
  if-converted test.
- **Scenarios** are weighted by probability; **sensitivity** grids rerun the core
  over WACC x terminal growth.
- **Conventions**: mid-year discounting by default; end-of-year discounting and a
  per-year discount-rate path (for example fading toward a mature cost of
  capital) are options, used for the template cross-check below.

Tests check two hand-computed valuations to 1e-6 (full first year and a stub
period), the treasury-stock fixed point, the convertible switch, every guardrail,
and properties: a higher WACC always lowers value; growth adds value only when
the incremental return exceeds the cost of capital.

## WACC calculators (`engine/wacc.py`)

| Step | Rule |
|---|---|
| Bottom-up beta | Damodaran industry unlevered beta (cash-corrected), relevered at market D/E: beta_u x (1 + (1 - t) D/E) |
| Regression beta | OLS on weekly returns (52+ common weeks) with R^2 and standard error |
| Beta choice | Bottom-up, unless the regression has R^2 >= 0.3 and standard error <= 0.15 |
| Cost of equity | Rf + beta x ERP + CRP |
| Cost of debt | Rf + spread from the synthetic rating (interest coverage, Damodaran table); without disclosed interest, the investment-grade market spread with a note; debt-free firms carry no debt weight |
| Weights | Market capitalization and book debt including capitalized leases |
| Terminal WACC | Beta converges to 1 when above 1 |
| Band | Rf and ERP +/-0.5%, beta +/-0.1 |

`engine/assemble.py` builds these inputs and the equity bridge from a
`DataBundle`. It refuses to value a company whose readiness blocks the WACC,
the equity bridge or scope, and it never uses a missing amount.

## Excel workbook and parity (`excel/`)

Sheets: Inputs (all assumptions, blue), WACC (formulas), one DCF sheet per
scenario (projection, stub timing, discount factors, terminal value, bridge,
value per share), Scenarios (probability-weighted value), Historical,
Normalized, Checks (engine versus workbook, with differences) and Sources
(provenance and notes).

`excel/parity.py` recalculates the workbook headless with LibreOffice, using a
temporary profile that forces recalculation on load (your own LibreOffice
settings are untouched), then reads the results back. Parity requires value per
share equal to the cent; enterprise value and WACC are checked too.

## CLI

```powershell
uv run python -m dcf_agent.cli value KO 2026-10-11 `
  --assumptions examples/assumptions/coca-cola.yaml --out outputs/ko-model.xlsx
```

The command finds the collected bundle for the ticker and date (or `--bundle`),
assembles inputs, normalizes history, computes WACC, runs bear/base/bull,
builds the workbook, recalculates it and prints the parity checks; it exits
non-zero on any failure. `--no-recalc` skips LibreOffice. Assumptions are a YAML
file per company: per scenario, probability, revenue growth, operating margin,
sales-to-capital and tax by year (the final tax rate equals the marginal rate),
terminal growth and terminal ROIC. Forecast year ends follow the company's own
fiscal calendar unless given.

Coca-Cola run (illustrative assumptions, not a view): WACC 7.71% (AAA synthetic
rating from 9.5x coverage), values per share 30.78 / 44.79 / 57.86 for
bear/base/bull, weighted 44.55. All eight parity checks pass: value per share
identical to six decimals, enterprise value within a thousandth of a dollar.

## Phase 1 acceptance status

| Criterion | Status |
|---|---|
| Engine unit tests match hand computations to 1e-6; property tests | Met |
| Extraction matches filings within 0.5% on reference companies | Met: 111/111 values, 11 companies |
| Excel parity: recalculated value per share equals the engine to the cent | Met (LibreOffice, all scenarios) |
| CLI `value TICKER DATE --assumptions ... --out ...` | Met (uses a collected bundle) |
| Cross-check against a free published template (Damodaran FCFF) within 0.5% | Met: identical to rounding (0.0000%) |
| Point-in-time rerun with an earlier date uses only earlier filings | Met: end-to-end history test on the SEC cache |

## Phase 1 completion (2026-10-15)

**Damodaran template cross-check** (`scripts/check_template_parity.py`). The
script downloads Damodaran's free FCFF template (fcffsimpleginzu.xlsx), keeps
its example company and inputs, sets its documented reinvestment-lag option to
"no lag" (as in this engine), recalculates it with LibreOffice, and values the
same per-year path with the engine (end-of-year discounting, the template's
cost-of-capital fade from 7.06% to 8.81%). Every figure matches to rounding:

| Figure | Template | Engine |
|---|---|---|
| PV of 10-year FCFF | 16,606.2404 | 16,606.2404 |
| Terminal value | 43,858.8187 | 43,858.8187 |
| Value of operating assets | 37,729.2322 | 37,729.2322 |
| Value per share | 7.2369 | 7.2369 |

The recorded case (`tests/fixtures/damodaran/ginzu-template.json`) is replayed by
the test suite without network or LibreOffice.

**Point in time** (`tests/test_point_in_time.py`). Apple's history rebuilt with a
2025-06-30 valuation date from the same cache: every value, period and filing
read is dated on or before the cutoff; the latest annual period is FY2024 and the
TTM ends at the latest 10-Q published by then. Rebuilding at 2026-10-11 adds
newer periods without changing older reported revenue.

**Regression beta in the CLI.** `--market SPY` (default) fetches the market series
through the price adapter and regresses weekly returns; the spec's rule decides
(Coca-Cola: 0.14 with R^2 0.02, Nike: 1.02 with R^2 0.18, both below the
threshold, so the bottom-up beta is kept and the decision is noted). `--market ""`
skips it.

**Sensitivity sheet.** Base-scenario value per share over WACC +/-1% x terminal
growth +/-1%, computed by the engine; its centre equals the base value.

All Phase 1 acceptance criteria are met. 451 tests, lint, format and strict
mypy pass.

Known limits, deferred to later phases: dilution inputs (RSUs, options,
convertibles) are not yet extracted, so shares are the dated cover-page count
(Filings Analyst, Phase 3); one-off items are only adjustable through approved
adjustments, not yet detected (Normalization Agent, Phase 3); captive-finance
equity is not yet valued separately; base revenue is the TTM and the first
forecast year grows from it to the next fiscal year end; assumptions come from
a YAML file until the Forecast Agent (Phase 4).
