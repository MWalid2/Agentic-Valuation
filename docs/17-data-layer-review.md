# 17. Data layer architecture review

Reviewed on 2026-10-13 against the architecture, agent specifications, roadmap,
and implemented contracts. The direction supports the project, but the current
DataBundle is a staging result, not the complete input to a valuation.

**2026-10-14 follow-up:** the bundle now also carries an enriched single-period
`RawFinancials` as `raw`, plus missing-concept explanations. See
[the implementation guide](18-raw-financials.md). This addresses part of the
handoff gap described in this review; historical construction, full concept
coverage, readiness checks, and graph wiring remain outstanding.

**Later 2026-10-14 update:** [19-financial-history.md](19-financial-history.md)
adds annual/quarterly/TTM construction and standard-disclosure fallback. The
remaining gaps are concept coverage, accounting/vintage review, readiness
checks and graph wiring; the original review below records the earlier state.

## Compatibility with the larger project

The implemented sequence is controlled source tools -> cited SEC selections ->
Python validation and arithmetic -> typed bundle. It preserves the principles
of local models, explicit missing data, provenance, and code calculating numbers.
Routine external collection is deterministic; the SEC interpreter uses the LLM
to select evidence. This is the approved change from the original entirely
deterministic Phase 1 extraction plan. It is not yet the Phase 2 ReAct workflow.

| Consumer | What it can reuse now | What remains before integration |
|---|---|---|
| Shared workflow state | Existing MarketData and MacroData types | DataBundle is not wired into ValuationState; its `raw` field expects RawFinancials, not FinancialSnapshot |
| Normalizer | Eleven sourced financial concepts and extraction diagnostics | Broader concepts, annual/quarterly/TTM history, tax and working-capital inputs, leases, SBC and adjustment evidence |
| WACC calculator/agent | Treasury proxy, premiums, industry beta and rating tables, prices, borrowing debt | Tax rate, interest coverage, company credit spread, leverage policy, market benchmark and returns for regression beta |
| Forecast agent | Industry margins and sales-to-capital benchmarks | Normalized historical drivers and approved assumptions; industry averages must not silently replace company history |
| DCF engine | Useful source inputs for its eventual upstream calculators | NormalizedFinancials, ForecastAssumptions and WACCResult; the bundle is not a DCF input by itself |
| Critic and Reporter | Source references, dates, warnings and collection checks | Calculation lineage, valuation outputs, enforced approval gates and report assembly |

The historical target remains five annual periods, eight quarters, and TTM.
RawFinancials permits partial history structurally; that does not establish
readiness for normalization or valuation. Future readiness checks must evaluate
the fields and history each consumer actually needs.

## Semantic boundaries we must preserve

- `operating_income` in the snapshot is not automatically interchangeable with
  every definition of `ebit`. Map operating profit explicitly, retain its
  derivation, and distinguish nonoperating income before normalization.
- Borrowing debt currently excludes operating leases. The project default treats
  leases as debt with corresponding operating-profit adjustments. Extract lease
  liabilities separately and apply that policy consistently in normalization
  and the equity bridge; do not silently change the raw borrowing-debt meaning.
- Cover-page shares have their own observation date, often after fiscal year-end.
  They belong in the dated market bridge, not as a falsely year-end observation
  in FinancialPeriod. Split alignment cannot account for unobserved buybacks or
  issuance, so the resulting market cap remains approximate.
- A quarterly cash-flow statement may be year-to-date. Preserve that basis and
  derive standalone quarters from compatible cumulative periods before TTM.
- Treasury yields and investment-grade spreads are proxies/context, not a
  complete company WACC. Country risk must not be counted again inside total ERP.
- SEC filing cutoffs and FRED vintage requests are useful date controls. Yahoo
  history is retrospective, and Damodaran eligibility depends on publication
  metadata or saved versions. These do not establish a universal point-in-time
  archive across all sources.
- `quality` currently checks collection requirements. No blocking collection
  issue does not mean valuation-ready. `review_required=True` records a
  requirement; it is not yet an enforced human approval gate.

## What the refinements do and why they matter

| Refinement | Purpose and benefit |
|---|---|
| Short evidence IDs instead of model-generated amounts | The model points to filing rows; Python reads the numbers and performs arithmetic |
| Debt coverage and total/component checks | Catch omitted commercial paper/current maturities and avoid counting a total plus its components |
| Parent income/equity and full share-class selection | Keep ownership basis consistent for eventual per-share valuation |
| Period, unit, filing and statement checks | Prevent adding dollars to shares or mixing fiscal periods and unrelated statements |
| Companyfacts reconciliation | Detect conflicting numeric evidence where matching facts exist; unmatched filing evidence remains explicitly warned |
| Balance-sheet identity | Catch some omissions or double counts; passing it does not prove all fields correct |
| Bounded correction | Give the local model one semantic repair opportunity, then retain an explicit ambiguity instead of a guessed number |
| Versioned source caches and independent source failures | Make runs replayable and retain usable data when one source fails |
| Rate/unit conversion and price/share split alignment | Keep downstream arithmetic on consistent units and share bases |
| Independent reference cohort | Compare extracted values with recorded filing references; a schema-valid response alone is not correctness |

These are reusable controls, not ticker-specific exceptions. Current supported
tags and statement structures are intentionally limited. Three development
companies cannot establish coverage for all nonfinancial companies; custom tags,
unusual expense presentations, and unseen filings still require broader testing.

Runtime also matters: the earlier uncached single-period runs took roughly
6.5-7.7 minutes on the local 4B model. Repeating that workflow independently for
every historical period would threaten the full-run budget. Historical building
should reuse validated mappings and cached evidence, perform period arithmetic
in code, and reserve model calls for new or ambiguous presentations. No larger
model is required by this refinement.

## Nike refinement

The earlier model added expense components to gross profit, then selected SG&A
alone on retry. Both were correctly rejected. The new `snapshot-v5` prompt adds
an explicit citation template when a unique gross-profit row and a unique
supported operating-expense total pass the existing validator. The model still
chooses; it receives IDs, not the expected answer. Python subtracts the expense,
reconciles the citations, and preserves both source references.

There is no production check for Nike, its ticker, row IDs or expected amounts.
Reported operating income takes precedence. Known omitted operating-expense
tags, incompatible periods, and ambiguous candidate totals prevent offering the
template. This is a narrow supported derivation, not a general accounting proof
that all custom expense disclosures have been understood. Unknown presentations
still need review and additional coverage.

## Recommended next boundary

After the Nike check, extend the data layer to produce RawFinancials with the
needed concepts and historical periods, explicit operating-profit mapping,
separate lease inputs, and consumer-specific completeness checks. Then build
the deterministic Normalizer and WACC/DCF calculations. Avoid adding more
open-ended agent reasoning before these numeric handoffs work.

See [the cohort report](16-data-agent-company-checks.md) for measured results and
[the source guide](15-data-agent-sources.md) for adapter limitations.
