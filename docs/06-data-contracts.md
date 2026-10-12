# 06. Data Contracts and Pydantic

This guide explains the contracts planned for DCF-Agent. Evidence wrappers are
implemented in `src/dcf_agent/contracts/provenance.py`. Request, financial, market,
and macro input contracts are implemented in `src/dcf_agent/contracts/inputs.py`.
Filing insights, adjustments, and normalized history are in `accounting.py`;
data quality diagnostics are in `quality.py`. Forecast, WACC, valuation, and
comparable-company contracts are in `modeling.py`. Workflow contracts and the
runtime state-validation boundary are implemented in `workflow.py`.
The architecture schemas are abridged examples, not complete implementation
definitions.

## 1. What is a data contract?

A data contract defines the structure and rules of information exchanged between
components: field names, types, required values, and validation constraints.
For example, the Forecast Agent and DCF engine must agree on how revenue growth,
tax rates, scenario probabilities, and source references are represented.

Without a shared contract, one component might return `"25%"` while another
expects `0.25`, or a field might be missing until much later in the workflow.
Contracts help reject these problems at the boundary where the data enters.

## 2. Why use Pydantic?

Pydantic models let us describe contracts as annotated Python classes and
validate actual data at runtime. They provide:

| Capability | Why it matters here |
|---|---|
| Required fields and types | Components receive the information they expect |
| Field and model validators | Enforce ranges, matching forecast lengths, and date relationships |
| Structured validation errors | Explain which input failed and why |
| JSON serialization and parsing | Support caches, audit exports, and persisted results |
| JSON Schema generation | Describe the small structured outputs requested from local LLMs |
| Nested and generic models | Attach provenance consistently to different kinds of values |

Pydantic validates data while the application runs. Mypy checks annotated source
code during development. We use both because they catch different problems.

Pydantic may convert compatible inputs by default. We must explicitly choose
where strict types, rejected extra fields, and custom validators are needed.
For example, percentages need a documented representation; Pydantic does not
automatically know that `25` should mean `0.25`.

Schema-constrained LLM output and Pydantic validation also serve different roles:
the schema guides generation, while validation checks the returned result.
We will keep LLM response schemas small and convert their bounded choices into
richer application contracts using code.

## 3. The contracts and their roles

### Inputs and evidence

| Contract | Contents | Role |
|---|---|---|
| `Provenance` | Source, reference, period end, filing date, as-of date, confidence, notes | Record where an input came from and when it was available |
| `Sourced[T]` | A value of type `T` and its `Provenance` | Keep values attached to their evidence; `T` may be a number or another supported type |
| `ValuationRequest` | Company name, ticker, CIK, valuation date, mode, intent, question, autopilot choice | Define the requested company, date, and output |
| `RawFinancials` | Annual, quarterly, and TTM income statement, balance sheet, and cash flow values | Preserve reported financial inputs before normalization |
| `MarketData` | Price, observation date, shares, market capitalization, returns history, industry identifiers | Supply market inputs for beta, capital weights, comparisons, and reporting |
| `MacroData` | Risk-free rate, equity risk premium, country risk premium, spreads, other macro inputs | Supply dated inputs for the cost of capital and forecast constraints |
| `DataQualityReport` | Passed and failed checks, missing fields, fallbacks, confidence | Make data limitations visible and identify failures that must stop a run |
| `FilingInsights` | Debt schedule, leases, dilution, segments, guidance, one-offs, pensions, risks, citations | Capture evidence that XBRL alone does not provide cleanly |

### Modeling and results

| Contract | Contents | Role |
|---|---|---|
| `Adjustment` | Item, amount, direction, rationale, evidence, materiality, confidence | Record a proposed accounting adjustment and whether it needs review |
| `NormalizedFinancials` | Adjusted EBIT, EBITDA, NOPAT, taxes, operating working capital, invested capital, ROIC, reinvestment metrics | Provide consistent historical inputs for forecasting and valuation |
| `ForecastAssumptions` | One scenario's probability, yearly growth, margins, sales-to-capital, taxes, terminal parameters, narrative, rationale | Define an auditable bear, base, or bull forecast |
| `WACCResult` | Rates, beta calculations, costs of equity and debt, capital weights, WACC, sensitivity band, method notes | Record the deterministic discount-rate calculation and method choice |
| `ValuationResult` | FCFF, timing, discount factors, terminal value, enterprise value, bridge, dilution, equity value, per-share value, implied metrics | Hold the engine's calculated outputs for a scenario |
| `CompsResult` | Peers, multiples, dispersion, implied value range, selection rationale, divergence from DCF | Cross-check the DCF against comparable companies |
| `CritiqueReport` | Issues, severity, evidence, suggested fix, responsible component, scores, pass status | Record review findings and guide bounded revision loops |

### Workflow and traceability

| Contract | Contents | Role |
|---|---|---|
| `ApprovalRecord` | Gate, decision, actor, timestamp, approved input version, edits | Record exactly what was approved or changed |
| `Event` | Action, timestamp, component, input/output references or hashes, relevant rationale | Record tool calls, state changes, and decisions in the audit trail |
| `Budget` | Step, token, and elapsed-time limits and usage | Bound work and prevent runaway requests |
| `ValuationState` | Request, data, modeling outputs, approvals, artifacts, audit information, budget | Hold the shared workflow state that can be checkpointed and resumed |

Supporting nested contracts will describe items such as individual financial
periods, filing citations, debt instruments, and critique issues. Their precise
fields will be defined when we implement the corresponding group.

The architecture proposes `ValuationState` as a `TypedDict`, rather than a
Pydantic model. A `TypedDict` describes dictionary structure for type checking;
it does not perform runtime validation. Its payloads will use validated models,
and graph entry points and checkpoint restoration need explicit validation.
Some state fields must be absent or empty until their workflow stage completes.

## 4. Example: keeping a revenue value attached to its source

Conceptually, a sourced revenue input looks like this:

```text
Sourced[float]
├── value: 100,000,000
└── prov: Provenance
    ├── source: sec_xbrl
    ├── ref: filing accession + XBRL tag
    ├── period_end: 2025-12-31
    ├── filed: 2026-02-15
    ├── as_of: 2026-03-01
    └── confidence: high
```

These are illustrative values. The revenue describes a period ending in
December, but the filing did not become available until February. A valuation
dated January must not use it.

Period end, filing date, source observation date, and valuation date serve
different purposes. The implementation will define their meanings explicitly;
adapters and cross-object checks must enforce point-in-time eligibility.

Derived values also need traceability back to their inputs and calculation.
LLMs write explanations around engine outputs; they do not replace calculated
numbers in the report. User overrides must record their origin and decision.

## 5. How the contracts fit into the workflow

```text
ValuationRequest
    → RawFinancials + MarketData + MacroData + DataQualityReport
    → FilingInsights + Adjustment[]
    → NormalizedFinancials
    → ForecastAssumptions + WACCResult
    → ValuationResult
    → CompsResult + CritiqueReport
    → approved answers, workbook, memo, and audit export
```

This is a simplified dependency overview; some branches run independently.
`ValuationState` holds the accumulated results, while approval records and audit
events track decisions throughout the workflow.

## 6. What validation should enforce

Examples of planned checks include:

- Required provenance on sourced inputs.
- Valid source, confidence, mode, and scenario labels.
- Finite numeric inputs and clearly documented units and rate conventions.
- Compatible lengths for each scenario's yearly forecast drivers.
- Scenario probabilities within bounds and summing to one across the collection.
- Point-in-time eligibility relative to the request's valuation date.
- Terminal parameters satisfying the engine's required mathematical constraints.

Validation belongs at the level that has enough information. A single scenario
can validate its probability range, but a scenario collection must validate the
sum. A provenance record can validate its own fields, but comparison against a
request's valuation date requires a cross-object check.

Financial policy bounds, warning thresholds, and permitted overrides will be
handled explicitly. An override must not bypass mathematical constraints that
make the engine undefined, such as a zero Gordon-growth denominator.

## 7. What contracts cannot guarantee

A structurally valid input can still be wrong: a cited filing may be misread,
units may be incorrectly identified, or an assumption may be implausible.
Contracts do not independently establish source accuracy or financial truth.

The project also needs statement reconciliation, deterministic calculations,
unit and property tests, Excel parity checks, Critic review, and human approval.
Some abridged architecture schemas use plain numeric fields; implementation must
retain their provenance or derived lineage through contracts and audit records.

## 8. Implementation order

We will implement contracts in small groups, reviewing each before proceeding:

1. `Provenance` and `Sourced[T]`, with validation tests.
2. Requests, financial periods, raw financials, market and macro data.
3. Filing insights, adjustments, and normalized financials.
4. Forecast assumptions, WACC, valuation, and comparable-company results.
5. Critique, approvals, audit events, budgets, and shared state.

This order starts with the evidence wrapper that most later contracts depend on.

## 9. Using the first implemented contracts

```python
from datetime import date

from dcf_agent.contracts import Provenance, Sourced

revenue = Sourced[float](
    value=100_000_000.0,
    prov=Provenance(
        source="sec_xbrl",
        ref="accession:us-gaap:Revenues",
        period_end=date(2025, 12, 31),
        filed=date(2026, 2, 15),
        as_of=date(2026, 3, 1),
        confidence="high",
    ),
)

serialized = revenue.model_dump_json()
restored = Sourced[float].model_validate_json(serialized)
```

This is illustrative evidence, not a real filing reference. `as_of` means the
information cutoff, normally the valuation date. It is not the download date.
SEC sources require `filed`, and any supplied filing date must be no later than
the cutoff. `period_end` is optional and may be later than the cutoff for
guidance or forecasts. Other sources still need adapter-level checks of release
and observation dates.

Both models reject unknown fields, use strict type validation, and prevent
field reassignment after creation. Python inputs should use actual `date`
objects; JSON round trips support ISO date strings. References and supplied
notes cannot be empty or whitespace-only. Concrete numeric wrappers reject
NaN and infinity, and do not turn numeric strings or booleans into floats.

Use a concrete wrapper such as `Sourced[float]` or `Sourced[str]`, rather than
an unparameterized `Sourced`. Freezing prevents field reassignment, not mutation
inside arbitrary nested mutable values. Units, reference authenticity, and
derived-input lineage need later contracts and audit validation.

## 10. Request and input contracts now implemented

Import these contracts from `dcf_agent.contracts`.

| Contract | Current rules |
|---|---|
| `ValuationRequest` | Resolved uppercase ticker, nonzero ten-digit CIK, company name, valuation date, mode; Info mode requires a question; optional I1–I11 intent, autopilot, and execution plan |
| `FinancialPeriod` | Annual, quarterly, or TTM period; explicit start/end dates and fiscal year; only quarterly periods require a quarter number; sourced values keyed by supported financial concepts |
| `RawFinancials` | USD history, CIK, cutoff, annual/quarterly collections and optional TTM; rejects duplicate periods, wrong period kinds, future historical periods, and evidence after the cutoff |
| `PriceObservation` | Positive sourced USD-per-share price and its observation date |
| `MarketData` | Close, shares and share observation date, optional market cap and history; unique ascending history dates; explicit history adjustment convention |
| `MacroObservation` | Decimal rate, observation date, and publication date for this version; observation must precede or equal publication, which must precede or equal the evidence cutoff |
| `MacroData` | Risk-free rate, ERP, CRP, optional spread and inflation expectations; dated evidence; premiums and spreads cannot be negative |

Use unscaled dollars for monetary amounts and actual counts for shares.
Adapters must normalize expense, capex, buyback, and dividend magnitudes; profit
and cash-flow measures remain signed. The current generic financial-value mapping
does not independently enforce each concept's sign convention.
Rates use decimals: `0.04` means 4%. Financial ratio policy bounds will be added
with the modeling contracts; a decimal rate is not necessarily confined to 0–1.
Negative risk-free rates and operating losses are structurally permitted.

Every historical financial value must identify the same period end as its
containing period. Annual and quarterly collections use tuples, with no fixed
December year-end assumption. The values mapping remains a mutable dictionary;
frozen models prevent field reassignment, not edits inside that dictionary.
Validate reconstructed payloads at boundaries rather than mutating validated state.

Missing financial concepts are absent, never silently zero. Partial history is
allowed because different requests need different inputs. At least one financial
period is required, but statement completeness, the target five years/eight
quarters, balance identities, and cross-source reconciliation belong to adapters
and the forthcoming `DataQualityReport`.

The close can precede the cutoff on a weekend or holiday. This contract does not
prove that it is the most recent trading close, or that price and shares use a
consistent split basis; adapters must establish both. Likewise, a supplied
macro publication date must come from a trustworthy versioned source.

These datasets reject evidence with a later cutoff than their own. Joining them
to a `ValuationRequest` will additionally require matching company identity and
eligible dates across datasets in the shared-state validation layer. Future
request dates remain allowed for the documented latest-available behavior;
the intake layer will explain that behavior rather than rejecting them here.

Python constructors expect typed dates and tuple collections. JSON parsing
accepts ISO dates and arrays and reconstructs the corresponding typed objects.
Tests cover invalid requests, partial history, non-December periods, negative
profits, future evidence, market-history ordering, dated macro releases, and
JSON round trips. All run offline.

## 11. Filing, adjustment, normalization, and quality contracts

| Contract | Role and current validation |
|---|---|
| `FilingCitation` | Identifies an SEC accession, filing date, section, and optional page/table locator; accession format is checked |
| `FilingItem` | A cited description, confidence, and optional sourced amount for guidance, one-offs, risks, or other note findings |
| `DebtInstrument` | Sourced principal, optional coupon, maturity, and fair value; principal/fair value are nonnegative, coupons are decimals in 0–1 |
| `LeasePayment` / `LeaseDisclosure` | Operating or finance lease liability, optional rate, and future payment schedule |
| `DilutiveSecurity` | Counts and terms for options, warrants, RSUs, PSUs, and convertibles; options/warrants require strike prices, convertibles require principal and conversion shares |
| `SegmentData` | Cited revenue and optional operating income for a matching period |
| `FilingInsights` | Groups findings under a company and cutoff; checks cited filing eligibility and agreement with SEC amount references and filing dates |
| `Adjustment` | Proposed amount, direction, target metric, rationale, evidence, reference EBIT, confidence, and ambiguity flag |
| `NormalizedPeriod` | Sourced revenue, clean EBIT, NOPAT, invested capital, taxes, optional ratios and supporting metrics, plus applied adjustment IDs |
| `NormalizedFinancials` | Annual and optional TTM normalized history; checks period kinds, duplicates, evidence cutoffs, and methodology settings |
| `DataQualityCheck` | Explicit passed, failed, or not-checked outcome, severity, message, affected field, and evidence references |
| `DataFallback` | Attempted source, selected source, affected field, and reason |
| `DataQualityReport` | Nonempty uniquely identified checks, missing fields, fallbacks, and confidence by field |

`Adjustment.materiality_pct_ebit` is a decimal ratio calculated as
`amount / abs(reference_ebit)`. Exactly 3% does not trigger the materiality gate;
above 3% does. Zero EBIT produces `None` and requires review. Low confidence on
the adjustment or its sourced inputs, and ambiguity, also require review.
These are properties derived from validated inputs, not editable stored values.
`requires_review` does not record approval or apply an adjustment; the graph and
approval contracts will do that later.

Normalized history defaults to SBC as an expense, leases as debt, and R&D
capitalization off. Signed EBIT, invested capital, ROIC, reinvestment, and
effective tax rates are permitted; unusual values require later financial policy
checks. Marginal tax rates must lie in 0–1. Financial identities are not recomputed
inside the contracts; deterministic normalizers will calculate and reconcile them.
References to applied adjustments must later be resolved against approved records.

Future lease payment dates and debt maturity dates are valid when disclosed in
an eligible historical filing. Citation authenticity and completeness still need
adapter checks. The filing validator checks SEC reference accession/date agreement;
it does not require derived values to share an SEC source, since derived inputs
may combine evidence from several filings. Full derived lineage belongs to audit
validation. Empty finding collections mean no recorded findings, not confirmed
zero balances or absence of risks. Use quality checks to record incomplete extraction.

`DataQualityReport.has_blocking_issues` is true when an error-severity check is
failed or not checked. Warning failures remain visible without blocking by this
property. Adapters must include all mandatory checks and assign their severity
correctly; the contract cannot detect an omitted check or execute a reconciliation.
An empty report is rejected rather than treated as success.

The models retain the existing strict, extra-field-rejecting, frozen policy.
Tuple collections and JSON round trips are supported; confidence mappings remain
mutable inside their frozen container, so updates should reconstruct and revalidate
the payload. Tests exercise date eligibility, citation agreement, missing dilution
terms, materiality boundaries, normalized periods, and quality failure states.

## 12. Forecast, WACC, valuation, and comparable-company contracts

| Contract | Implemented role and rules |
|---|---|
| `ForecastAssumptions` | One scenario's sourced drivers, dated horizon, terminal inputs, narrative, and rationale; driver lengths and evidence period ends must match |
| `ForecastScenarios` | Exactly one bear/base/bull; common cutoff and horizon; probabilities sum to one |
| `WACCResult` | Sourced inputs and calculated outputs, both beta methods where available, terminal WACC, and sensitivity band; capital weights sum to one and the band contains WACC |
| `ValuationResult` | Per-scenario cash flows, discount times/factors, PVs, signed equity bridge, dilution, per-share value, and implied metrics |
| `PeerMultiples` | Dated, sourced peer multiples and selection rationale; undefined multiples remain absent |
| `MultipleSummary` | Metric, sample size, ordered quartiles and median |
| `CompsResult` | Peers, summaries, optional implied value range and divergence, calculation reference, and warnings |

Forecast year ends are unique, ascending, and after the cutoff. Drivers use
decimal rates, with growth above -100%, positive sales-to-capital, and tax rates
in 0–1. Negative growth and margins are permitted. The final explicit tax rate
must equal the sourced marginal rate within numerical tolerance. The horizon is
not restricted to five or ten years so small fixtures and other configured
horizons remain possible; annual spacing is established by the forecast builder.

Terminal growth and ROIC are sourced, but macro-dependent policy checks
(`g <= Rf`, the GDP proxy cap, and moat-related ROIC checks) require the later
integration layer. The engine must enforce `terminal_wacc > terminal_growth`
and compute terminal reinvestment consistently. These cross-contract financial
checks are not claimed to be implemented by these standalone contracts.

WACC requires a bottom-up beta, with regression optional unless the selected
method is regression or average. Discount rates must exceed -100%. WACC formula
identities, beta selection quality, and mature-firm convergence are calculator
and review responsibilities. All sourced inputs are checked against the cutoff.

Valuation arrays must match the horizon; discount times are nonnegative and
strictly increasing, discount factors and diluted shares are positive, and the
terminal time cannot precede the final explicit cash-flow time. Bridge entries
are signed contributions: cash normally positive, debt normally negative.
Negative enterprise/equity values can be represented. Terminal share of EV is
`None` for zero EV; it is not restricted to 0–1 since signed cash flows can yield
unusual ratios. Values must be finite.

Calculated valuation numbers remain plain floats with `calculation_ref` and
`input_hash` identifying the engine run and inputs. Sourced bridge inputs retain
their individual provenance. These identifiers do not themselves authenticate
a computation: later audit validation and engine tests must establish lineage
and numeric identities. No stub-period convention is calculated here.

Comparable-company tickers and summary metrics must be unique, with peer cutoffs
matching the result. Summary sample sizes cannot exceed available multiples.
Fewer than five peers or fewer than five observations for a summary require a
warning. A value range requires a summary and ordered endpoints. Empty peers
are allowed with warnings and no invented value range. Outlier removal,
quartile calculation, currency consistency, and implied values belong to the
Comps calculator. `divergence_vs_dcf` is a signed decimal relative difference;
its calculation and handling of a zero denominator will be defined there.

These contracts are available from `dcf_agent.contracts`. Their tests cover JSON
round trips and rejection of invalid horizons, scenario weights, capital weights,
evidence dates, discount series, sample sizes, and ranges, without invoking LLMs.

## 13. Workflow, audit, and shared-state contracts

| Contract | Current role and rules |
|---|---|
| `CritiqueIssue` | Unique issue ID within a report, severity, description, evidence references, suggested fix, and responsible node |
| `CritiqueReport` | Issues, 1–5 score and rubric ratings, pass status, and revision count capped at two; a high-severity issue prevents a clean pass |
| `ApprovalRecord` | Gate 0–3, decision, actor, aware timestamp, SHA-256 input hash, edit/warning references, and explicit bounds-override reason |
| `Event` | Audit IDs, aware timestamp, component, action, optional hashes and persisted input/output references, rationale, and duration |
| `Budget` | Positive limits and nonnegative usage for steps, tokens, and elapsed seconds; `exhausted` becomes true when any limit is reached |
| `ValuationState` | Typed dictionary for LangGraph, requiring request and budget; other fields are stage-dependent |
| `ValuationSnapshot` | Pydantic runtime validation and serialization of the shared state |
| `validate_state` | Validate a Python payload and return a typed state preserving its nested model objects |

Approval and event timestamps describe actual workflow activity, not financial
information dates, and must include a timezone. Input/output hashes are lowercase
64-character hexadecimal SHA-256 identifiers; their format is checked, but the
audit layer must calculate and verify them. Edited decisions require persisted
edit references. Autopilot can record approval, but cannot record an explicit
human bounds override. Override reasons are required when the override flag is
set. Records do not authenticate actors or execute approvals.

At the Critic revision cap, unresolved issues remain a failed report. The later
graph may present eligible warnings at Gate 3 without relabeling the review as
clean. Mathematical failures must still prevent engine execution or export.
Budgets allow usage to exceed limits so the audit trail can record an overrun;
the executor is responsible for checking `exhausted` and stopping work.

The state validator checks company CIK/ticker agreement, data cutoffs not later
than the request, forecast and valuation dates matching the request, valuation
scenario keys and forecast horizons, unique adjustment/approval/event IDs, and
existence of adjustment IDs referenced by normalized history. When forecast and
WACC are both present, terminal WACC must exceed each scenario's terminal growth.
Other macro-dependent policy bounds and numeric identities remain integration
and calculator responsibilities.

Older source snapshots are permitted, but freshness and fallback warnings must
be supplied by adapters. Stage outputs may be absent; this validator does not
assert that an incomplete state is ready for valuation or export. Later graph
code must enforce prerequisites, current approval hashes, adjustment approval
status, pending gates, warning recording, budget limits, and input invalidation.

Example checkpoint round trip:

```python
from dcf_agent.contracts import ValuationSnapshot, validate_state

# payload contains the request, budget, and any completed stage outputs.
snapshot = ValuationSnapshot.model_validate(payload)
checkpoint_json = snapshot.model_dump_json()
restored = ValuationSnapshot.model_validate_json(checkpoint_json)
state = validate_state(restored)
```

`TypedDict` itself has no runtime checks. Use these validation boundaries when
accepting or restoring state. Nested models remain typed in `to_state()`;
`model_dump()` alone would instead turn them into dictionaries. Frozen models
still contain mutable mappings, so reconstruct and revalidate updates.
Artifact paths are metadata; path containment and safe file writes are separate
responsibilities. Tests cover restoration, identity/date mismatches, invalid
decisions and timestamps, duplicate IDs, revision caps, and budget exhaustion.
