# 02. Agents: Explicit Specification

Conventions used below:
- **Model class (CPU-only, 16 GB RAM):** `S` = small, about 3-4B parameters at 4-bit quantization (about 2.5-3 GB). `M` = medium, about 7-8B at 4-bit (about 4.5-5.5 GB). There is no larger class: 14B+ models don't fit comfortably. Only one model is loaded at a time. See `03-stack.md` for sizing and examples.
- **LLM scope:** each agent has a deterministic default path. The LLM only makes bounded choices (see the table after the overview). Rule: *choose, don't invent*. Numbers come from code, tables and filings.
- All outputs are **Pydantic objects** produced with schema-constrained decoding.
- `Sourced[...]` means the value carries a `Provenance` record.
- Tool signatures are given in short form; every tool returns a typed result or a typed error.

## Overview

| # | Component | Type | Pattern(s) | Model | Gate |
|---|---|---|---|---|---|
| 0 | Router / Supervisor | LLM | Plan-and-Execute | S | 0 |
| 1 | Data Agent | LLM + tools | ReAct, verification | S | - |
| 2 | Filings Analyst | LLM + RAG | RAG, CoT, self-consistency | M | feeds 1 |
| 3 | Normalization Agent | LLM + calc | CoT, program-aided | M | 1 |
| 4 | Forecast Agent | LLM + calc | ToT, CoT, (optional debate) | M | 2 |
| 5 | WACC Agent | LLM + calc | ReAct, CoT | S | 2 |
| 6 | DCF Engine | **Pure Python** | - | none | - |
| 7 | Comps Agent | LLM + tools | CoT, outlier reasoning | S | - |
| 8 | Critic Agent | LLM + rules | Reflection, LLM-as-judge | M (different family from Forecast) | triggers 2 |
| 9 | Reporter | LLM + builders | Structured generation | S | 3 |

### What the LLM decides vs what code decides (CPU mode)

| Agent | LLM decides (bounded) | Code decides | LLM calls per full run |
|---|---|---|---|
| Router | Intent label from the I1-I11 enum; wording of a disambiguation question | Ticker/CIK resolution, date parsing, scope check | 1-2 |
| Data | Only when every encoded fallback fails: the next alternative to try (enum) | Tag fallbacks, period building, validation, reconciliation | 0-3 |
| Filings | Which retrieved passage answers a question; classify an item; read numbers from short table snippets | Retrieval, table parsing, XBRL reconciliation | about 8-12 |
| Normalization | Label candidate items (recurring / non-recurring / operating) with a 1-2 sentence rationale | Amounts, NOPAT, ROIC, lease capitalization, materiality test | 3-5 |
| Forecast | A level per axis (low / medium / high) with evidence; the story for each of 3 scenarios | Driver values from history and Damodaran tables, fades, coherence scoring, pruning | about 5 |
| WACC | Which beta to trust (enum) with a one-sentence reason | All formulas, regression, synthetic rating | 1-2 |
| Comps | Keep/drop each candidate peer with a one-line reason | Multiples, outliers, ranges | 4-8 |
| Critic | Rubric scores (1-5) and one-line issues | All numeric checks | 2 per loop |
| Reporter | Narrative paragraphs around template variables | All numbers, Excel, charts | 3-4 |

---

## 0. Router / Supervisor

**Mission.** Understand the request, resolve the company, decide the mode and intent, build the execution plan, and own the HITL gates and routing.

**Inputs.** Raw user text (`"Apple, 2026-10-11"`, `"Why is Tesla's terminal growth 3%?"`), session state (to detect follow-ups).

**Outputs.** `ValuationRequest {ticker, cik, company_name, valuation_date, mode, intent, question, autopilot, plan[]}`

**Tools.**

| Tool | Description |
|---|---|
| `resolve_company(text) → list[Candidate]` | Fuzzy-match against SEC `company_tickers.json` (name, ticker, CIK), ranked |
| `parse_date(text) → date` | Normalizes ("today", "end of Q3 2025", ISO dates); rejects invalid |
| `check_scope(cik) → ScopeResult` | Uses SIC code and statement structure: flags banks, insurers, REITs, shells, SPACs |
| `classify_intent(text, state) → Intent` | I1-I11 from the catalog in `01-architecture.md` |
| `ask_user(question, options)` | Triggers a HITL interrupt |

**Procedure.**
1. Extract `(company, date, optional question)` from the first message.
2. Resolve the company; if more than one plausible match, ask the user (Gate 0).
3. Validate the date and apply the point-in-time rule; warn if the date is before the company's first XBRL filing.
4. Check scope; if out of scope, explain and stop.
5. Classify intent. If the user gave only company + date, present three options: **question / quick fair value / full Excel**.
6. Emit a plan (ordered list of graph nodes) and start the graph.
7. For follow-ups, look up cached state and the invalidation graph; run the minimum nodes.

**Failure modes.** Ambiguous ticker (e.g. dual-class shares, ADRs), date in the future, unsupported industry, ambiguous follow-up ("change it"). In all cases, ask rather than guess.

---

## 1. Data Agent

**Implementation checkpoint (2026-10-13).** `agents/data.py` now calls the bounded
SEC, price, FRED, and Damodaran tools in `tools/data.py`, returning a `DataBundle`
and quality report. See [source adapters and local commands](15-data-agent-sources.md).
The output includes the original financial snapshot plus annual, quarterly and
TTM RawFinancials through the [history extension](19-financial-history.md).
Unsupported concepts stay explicit; full input coverage and graph integration
remain separate work.

**Mission.** Fetch structured financial, market and macro data as of the valuation date, validate it, and hand over a clean, provenance-tagged dataset.

**Inputs.** `ValuationRequest`.

**Outputs.**
- `RawFinancials`: 5 fiscal years + last 8 quarters + TTM of income statement, balance sheet, cash flow (revenue, COGS, SG&A, R&D, EBIT, D&A, SBC, interest, taxes, net income, cash, investments, receivables, inventory, payables, PP&E, goodwill, debt, leases, equity, minorities, preferred, CFO, capex, buybacks, dividends).
- `MarketData`: price, shares outstanding, market cap, daily and weekly price history (for beta), sector/industry codes.
- `MacroData`: 10Y Treasury yield, credit spreads, implied ERP, country risk premium (US ≈ 0 plus the Damodaran value), inflation expectations.
- `DataQualityReport`: checks passed/failed, fallbacks used, confidence per field.

**Tools.**

| Tool | Description |
|---|---|
| `get_submissions(cik)` | SEC submissions JSON (filing list with dates, form types, accession numbers) |
| `get_company_facts(cik, as_of)` | SEC XBRL `companyfacts`, filtered to `filed ≤ as_of` |
| `resolve_concept(concept)` | Maps a financial concept to an ordered list of candidate XBRL tags (e.g. revenue: `RevenueFromContractWithCustomerExcludingAssessedTax`, `Revenues`, `SalesRevenueNet`) |
| `build_periods(facts)` | Derives annual, quarterly (Q4 = FY - 9M), and TTM series; handles non-December fiscal years |
| `get_price_history(ticker, start, end)` | `yfinance` primary, Stooq fallback |
| `get_treasury_yield(date)` | FRED `DGS10` |
| `get_damodaran(dataset, as_of)` | Industry betas, margins, ERP, CRP, sales-to-capital, spreads table (snapshotted files) |
| `reconcile(a, b, tol)` | Compares two sources for the same field; returns delta and verdict |
| `validate_statements(raw)` | Balance sheet identity, cash-flow tie-out (change in cash), unit/scale detection, sign conventions |

**Procedure (ReAct loop per field).**
1. *Think:* what concept do I need? *Act:* `resolve_concept` → fetch. *Observe:* is the fact present for the period, unit, and `filed` date? If not, try the next tag; if none, mark for the Filings Agent (parse from the filing text).
2. Build annual/quarterly/TTM; run `validate_statements`.
3. Pull market and macro data; cross-check market cap = price × shares against a second source if available.
4. Emit `DataQualityReport`. Any hard failure (balance sheet mismatch, missing revenue/EBIT) stops the run with a diagnostic.

**CPU mode.** The fallback chain (next XBRL tag → HTML statement parse → second price source → cache) is encoded in code, so the normal path uses no LLM calls. The LLM is consulted only when every encoded fallback fails, and then as a constrained JSON action (an enum of allowed next steps, max 4 steps).

**Failure modes and fallbacks.** Company uses custom XBRL extension tags → parse the HTML statements. Restated figures → use the rule in `01-architecture.md` section 6. Units wrong → hard stop. `yfinance` break → Stooq → cache.

---

## 2. Filings Analyst (RAG)

**Mission.** Extract what XBRL alone doesn't give cleanly, and ground it in the actual text of 10-K / 10-Q / 8-K filings with citations.

**Inputs.** Filings filed ≤ valuation date (latest 10-K, up to 3 subsequent 10-Qs, earnings-release 8-Ks), `RawFinancials` (to reconcile).

**Outputs.** `FilingInsights`:
- `debt_schedule`: instruments, principal, coupon, maturity, fair value
- `lease_liabilities`: operating and finance, with discount rate and maturity table
- `dilutive_securities`: options (count, strike), RSUs/PSUs, convertibles, warrants
- `segment_data`: revenue and operating income by segment, geographic mix
- `management_guidance`: revenue, margin, capex, tax-rate, share-count statements
- `one_off_items[]`: restructuring, impairments, litigation, M&A, with amounts
- `pension_obligations`, `contingent_liabilities`, `non_operating_assets` (equity-method, strategic investments)
- `risk_flags[]`, each with a **citation** (accession, item/section, page or table id).

**Tools.**

| Tool | Description |
|---|---|
| `download_filing(accession)` | Fetches and caches the primary document (edgartools) |
| `parse_sections(accession)` | Splits into Items (1, 1A, 7, 7A, 8, notes) with tables preserved as markdown |
| `index_filing(accession)` | Chunks section-aware (about 300-500 tokens, tables kept whole), embeds with a small CPU embedder, stores text (FTS5) and vectors in SQLite with metadata |
| `search_filings(query, filters)` | Hybrid FTS5-BM25 + cosine similarity (NumPy), top 4 passages, reranker off by default, returns passages with citations |
| `read_section(accession, item)` | Returns a full section for targeted reads |
| `extract_table(accession, table_id)` | Returns a parsed table as rows/columns |

**Procedure.**
1. Index the relevant filings once (cached by accession).
2. For each field in `FilingInsights`, run a targeted retrieval question, extract with schema-constrained output, and require a citation.
3. **Self-consistency (only where needed):** fields that XBRL already provides are reconciled, not re-extracted. For numeric fields with no XBRL ground truth, extract twice with differently worded prompts at temperature 0 and a third time only on disagreement; accept on agreement, otherwise flag with `confidence=low` and send to Gate 1.
4. Reconcile with XBRL (e.g. total debt in the note vs balance sheet). Differences above 2% are flagged.
5. Write a short CoT summary of notable findings.

**Failure modes.** Tables split across pages, multi-column HTML, small models misreading units. Mitigations: keep tables whole in chunks, include unit/headline text in chunk metadata, always cross-check against XBRL totals.

---

## 3. Normalization Agent

**Mission.** Convert reported numbers into economically meaningful ones for valuation.

**Inputs.** `RawFinancials`, `FilingInsights`, `MacroData`.

**Outputs.** `NormalizedFinancials` + `Adjustment[]`.
- Clean EBIT (excluding one-offs), EBITDA, NOPAT, effective vs marginal tax rate, operating NWC, net PP&E, invested capital, ROIC, sales-to-capital, historical reinvestment rate.
- `Adjustment {item, amount, direction, rationale, evidence_ref, materiality_pct_ebit, confidence}`.

**Treatments (v1 defaults).**
- **SBC:** treated as an operating expense (not added back).
- **Operating leases:** capitalized as debt; EBIT adjusted for the implicit interest component; consistent in the bridge.
- **R&D:** optional capitalization (Damodaran style) via a flag; default **off** in v1, with a toggle in the config.
- **One-offs:** restructuring, impairments, litigation settlements, gains on disposals are stripped if non-recurring, with the reasoning logged.
- **Cash:** separate excess cash from operating cash (default: all cash and marketable securities treated as non-operating, with a minimum-operating-cash flag).
- **Tax:** effective tax rate over 3 years vs statutory; flag unusual rates (valuation allowance releases).

**Tools.**

| Tool | Description |
|---|---|
| `calc_nopat(ebit, tax)`, `calc_invested_capital(...)`, `calc_roic(...)` | Deterministic calculators |
| `capitalize_leases(lease_data, rate)` | PV of lease payments, implied interest |
| `capitalize_rnd(history, life)` | Optional R&D asset |
| `lookup_filing_note(query)` | RAG helper to justify an adjustment |
| `propose_adjustment(...)` | Records an adjustment with rationale |

**Procedure.** For each candidate item: (1) detect from `one_off_items` and XBRL anomalies, (2) CoT: is it recurring? is it operating? (3) compute amount via a tool, (4) log. If materiality > 3% of EBIT or confidence is low → Gate 1.

**Failure modes.** Over-adjusting (always stripping "unusual" items that recur every year). Rule: an item that appears in 3+ of the last 5 years is treated as recurring unless the evidence says otherwise.

---

## 4. Forecast Agent (the judgment agent)

**Mission.** Turn the company's history, strategy and industry context into scenario-based, internally consistent forecast assumptions.

**Inputs.** `NormalizedFinancials`, `FilingInsights` (guidance, segments), industry benchmarks, `MacroData`, news context.

**Outputs.** `dict[Scenario, ForecastAssumptions]` for bear / base / bull with probabilities, year-by-year drivers (revenue growth, operating margin, sales-to-capital, tax rate), terminal parameters, a narrative per scenario, and a `rationale` per assumption.

**Tools.**

| Tool | Description |
|---|---|
| `historical_driver_stats(metric, years)` | Mean, trend, volatility, percentile of growth/margin/capex for the company |
| `get_industry_benchmarks(industry)` | Damodaran margins, sales-to-capital, growth, ROIC distributions |
| `get_guidance(filing_insights)` | Management guidance extracted by the Filings Analyst |
| `get_press_releases(cik, before_date)` | Earnings-release 8-K exhibits from EDGAR, filed on or before the valuation date (no web search in v1) |
| `check_coherence(assumptions)` | **Deterministic** scorer (below) |
| `fade_path(start, end, years, shape)` | Generates linear/convex fades to terminal values |
| `quick_dcf(assumptions)` | Calls the engine for a fast per-share preview of a branch |

**Procedure: Tree-of-Thought.**
1. **Frame uncertainties.** From the evidence, name 2-3 key axes (e.g. *growth durability*, *margin trajectory*, *reinvestment efficiency*). The agent writes one line of evidence per axis.
2. **Generate branches (programmatic).** Code enumerates candidate worlds from axis levels, e.g. growth durability {low, medium, high} × margin trajectory {compress, hold, expand} = 9 worlds. Driver values come from the company's history and Damodaran tables via `fade_path`, not from the LLM. The LLM's only role in steps 1-2 is to pick a level and cite evidence per axis (one constrained JSON call).
3. **Score each branch (deterministic first, LLM second):**
   - *Growth-reinvestment identity:* implied reinvestment must support growth (`reinvestment = Δrevenue / sales_to_capital`).
   - *ROIC path:* implied ROIC stays within [WACC, a plausible ceiling] unless a moat story exists.
   - *Margin plausibility:* vs company history and industry percentiles.
   - *Scale plausibility:* implied revenue vs a market-size sanity check (share of industry revenue).
   - *Story consistency:* one LLM rubric call (1-5) on the 4 best-scoring branches only, checking the narrative against the numbers.
4. **Prune** to the top 3 distinct branches; map them to bear/base/bull; assign probabilities (default 25/50/25, adjustable at Gate 2). The LLM then writes a 3-sentence story for each survivor (3 short calls).
5. **Expand** each into year-by-year drivers with fade to terminal values; terminal growth is set `≤ Rf`, tax converges to marginal, terminal ROIC follows the moat assumption.
6. **Preview** with `quick_dcf`; if base-case value is wildly off from the market price, require an explicit explanation rather than silently re-tuning (anti-anchoring rule).

**Key identity enforced.** `g = reinvestment_rate × ROIC`. Terminal `reinvestment_rate = g / ROIC_terminal`.

**Optional debate.** A "bull advocate" and "bear advocate" prompt each critique the base branch; the arbiter merges the valid points. Disabled by default on CPU (about 3x the cost); enable only if the M model passes the debate eval and you accept the run time.

**Failure modes.** Anchoring on the market price, over-optimistic extrapolation of recent growth, internally inconsistent stories. Mitigations: scoring is mostly deterministic, an anti-anchoring rule, Critic review, and Gate 2.

---

## 5. WACC Agent

**Mission.** Estimate the cost of capital as of the valuation date, with transparent method choices.

**Inputs.** `MarketData`, `MacroData`, `FilingInsights.debt_schedule`, `NormalizedFinancials`.

**Outputs.** `WACCResult {rf, erp, crp, unlevered_beta, relevered_beta, cost_equity, pretax_kd, tax_rate, aftertax_kd, weight_equity, weight_debt, wacc, wacc_band, method_notes}`.

**Tools.**

| Tool | Description |
|---|---|
| `bottom_up_beta(industry, de_ratio, tax)` | Industry unlevered beta (cash-corrected) from Damodaran, relevered at the company's D/E |
| `regression_beta(ticker, benchmark, window, freq)` | OLS beta on free price data (e.g. 2y weekly, 5y monthly vs S&P 500), computed with NumPy; returns beta, R², standard error |
| `synthetic_rating(ebit, interest_expense)` | Interest-coverage → rating → default spread (Damodaran table) |
| `cost_of_debt(rf, spread, cod_override)` | Pre-tax Kd, with an option to use actual bond yields if known |
| `market_weights(price, shares, debt, leases)` | Market-value weights (debt at market or book proxy) |
| `wacc_calc(...)` | Deterministic WACC and sensitivity band |

**Formulas.**
- `Ke = Rf + β × ERP (+ CRP × country exposure)`
- `β_levered = β_unlevered × (1 + (1 - t) × D/E)`
- `Kd_aftertax = (Rf + default_spread) × (1 - t)`
- `WACC = E/(D+E) × Ke + D/(D+E) × Kd_aftertax`
- **Terminal WACC:** converges toward a mature-firm WACC (beta → 1) if the firm's growth is high; documented as a flag.

**Procedure (ReAct, constrained).** Compute both beta methods; observe the standard error and R² of the regression; choose which to trust. Default rule in code: bottom-up unless the regression has R² ≥ 0.3 and standard error ≤ 0.15 (regression betas on free data are noisy). The LLM may pick {bottom_up, regression, average} with a one-sentence reason, and picks that deviate from the default rule are flagged for Gate 2; compute Kd from synthetic rating, compare to actual coupon/yield data from the debt schedule; compute weights; output a sensitivity band (±0.5% Rf/ERP/beta).

**Failure modes.** Unstable regression beta, thin industry samples, misclassified industry mapping. Always show both betas in the output.

---

## 6. DCF Engine (deterministic, no LLM)

**Mission.** The only source of valuation numbers. A tested Python library.

**Inputs.** `ForecastAssumptions` + `WACCResult` + `NormalizedFinancials` + `FilingInsights` (bridge items) + valuation date.

**Outputs.** `ValuationResult` per scenario + a probability-weighted result + sensitivity tables.

**Logic.**
- **FCFF** = `EBIT(1 - t) - reinvestment`, where `reinvestment = Δrevenue / sales_to_capital`. The Excel and report also show the explicit view `D&A, CapEx, ΔNWC` reconciled to the same total.
- **Stub period and mid-year convention.** Let `s = (next fiscal year-end - valuation date) / 365`. Year-1 FCFF is scaled by `s`; its discount time is `s/2`. For year `k ≥ 2`, time = `s + (k - 2) + 0.5`. This is a spec decision and is unit-tested.
- **Terminal value (Gordon):** `TV_N = FCFF_{N+1} / (WACC - g)` with `FCFF_{N+1} = NOPAT_{N+1} × (1 - g / ROIC_terminal)`. Apply the mid-year adjustment consistently (multiply by `(1 + WACC)^0.5`), equivalent to discounting at `N - 0.5`. **Exit multiple** is a cross-check only; if used, no mid-year adjustment.
- **Bridge:** `EV + cash & marketable securities + non-operating investments - debt - lease liabilities (if capitalized) - minority interest - preferred - after-tax pension deficit = equity value`.
- **Dilution:** RSUs/PSUs counted in full; options by the treasury stock method at the *estimated intrinsic value* (iterate to convergence); convertibles by if-converted vs debt test.
- **Scenarios:** run bear/base/bull; weight by probability.
- **Sensitivity:** WACC × terminal growth, growth × margin, terminal ROIC × WACC; tornado of the top 6 drivers.
- **Implied metrics:** implied EV/EBITDA, EV/Sales, P/E, TV share of EV, ROIC path.
- **Reverse DCF (extension):** solve for the growth or margin implied by the market price.

**Exposed tools.** `run_dcf(assumptions, wacc, bridge, date)`, `run_sensitivity(...)`, `run_reverse_dcf(...)`. Agents get valuation numbers only through these.

**Testing.** Hand-computed unit tests with exact numbers; property tests (higher WACC → lower value; higher growth with ROIC > WACC → higher value); parity against free published valuation spreadsheet templates (e.g. Damodaran's FCFF templates) on identical inputs.

---

## 7. Comps Agent

**Mission.** Cross-check the DCF with relative valuation.

**Inputs.** `ValuationRequest`, `NormalizedFinancials`, `ValuationResult`.

**Outputs.** `CompsResult {peers[], multiples_table (EV/EBITDA, EV/Sales, EV/EBIT, P/E), median/quartiles, implied_value_range, divergence_vs_dcf, peer_selection_rationale}`.

**Tools.**

| Tool | Description |
|---|---|
| `find_peers(sic, size_band, k)` | Peers by SIC code and the Damodaran firm-to-industry mapping, plus a market-cap band; capped at 8-10 candidates (no bulk downloads) |
| `get_peer_fundamentals(tickers, as_of)` | From per-company XBRL `companyfacts` (cached on disk), filed ≤ date |
| `get_peer_prices(tickers, date)` | `yfinance`/Stooq |
| `compute_multiples(...)` | Deterministic |
| `remove_outliers(multiples, method)` | IQR / MAD rule |

**Procedure.** Code proposes 8-10 candidates; the LLM gives a keep/drop verdict with a one-line reason per candidate based on the business description (via RAG); compute multiples with the same adjustments as the target; drop outliers; produce an implied range. Flag divergence above 40% vs DCF for the Critic.

**Failure modes.** Poor peer lists (SIC codes are coarse), inconsistent accounting across peers, stale prices. Report the sample size and dispersion; never present a median from fewer than 5 peers without a warning.

---

## 8. Critic Agent

**Mission.** Adversarial review. Independent from the Forecast Agent (separate prompt, ideally a different model family) to avoid self-confirmation.

**Inputs.** Full state: assumptions, WACC, valuation, comps, adjustments, narrative.

**Outputs.** `CritiqueReport {issues[{id, severity, evidence, suggested_fix, route_back_to}], score, passed}`.

**Deterministic checks.**

| Check | Flag when |
|---|---|
| TV share of EV | > 75% |
| Terminal growth | > Rf, or > 4% |
| Terminal ROIC | < WACC (value destruction in perpetuity) or > WACC + 5% without moat |
| Reinvestment consistency | `|g - reinvestment × ROIC|` > 0.5% in any year |
| Margins | > industry 90th percentile, or a step change > 5 points in a year |
| Growth | Revenue CAGR > 25% beyond year 5 |
| Tax | Not converging to marginal rate |
| DCF vs comps | Divergence > 40% |
| DCF vs price | Divergence > 50% needs an explicit explanation |
| Bridge | Debt, leases or cash differ from the filing by > 2% |
| Excel parity | Recalculated workbook ≠ engine value per share (tolerance of 1 cent) |

**LLM rubric checks (scored 1-5).** Narrative-to-numbers consistency; evidence quality of key assumptions; scenario distinctness; unsupported claims; missing risks from the filings.

**Procedure (Reflection).** Run deterministic checks → LLM rubric → produce a report. If any issue is `high` severity, route to the owning agent (Forecast/WACC/Normalizer) with the suggested fix; max **2** loops. After the cap, pass remaining issues to the user at Gate 3 as warnings.

---

## 9. Reporter (Answer, Excel and Memo Builder)

**Mission.** Turn state into the final user-facing output.

**Inputs.** Approved state, `CritiqueReport`, user intent.

**Outputs.**
- **Info mode:** a concise answer (a few sentences plus a small table) with the key numbers and sources, plus an offer to go deeper.
- **Quick mode:** value range, base-case value per share vs price, 3 key drivers, 3 key risks, sensitivity snapshot.
- **Full mode:** the Excel workbook, a 2-4 page memo (Markdown and HTML; PDF optional), and the audit log export.

**Workbook structure (live formulas, not pasted values).**

| Sheet | Contents |
|---|---|
| Cover | Company, valuation date, value per share, scenario summary, disclaimer |
| Inputs | **All** assumptions in one place (blue-font inputs), scenario selector |
| Historical | Reported statements (5y + TTM) with source tags |
| Normalized | Adjustments, clean EBIT, invested capital, ROIC |
| Forecast | Revenue, margins, reinvestment, D&A/CapEx/ΔNWC bridge |
| FCFF | FCFF build, stub-period timing, discount factors, PVs |
| WACC | Full build-up with both beta methods |
| Terminal | Gordon TV, ROIC consistency, exit-multiple cross-check |
| Bridge | EV → equity → per share, dilution schedule (TSM) |
| Scenarios | Bear/base/bull outputs and probability-weighted value |
| Sensitivity | Data tables WACC × g, growth × margin, tornado |
| Comps | Peer table and implied range |
| Checks | Balance checks, parity with engine, flags from the Critic |
| Sources & Log | Every input with provenance; adjustment log; unreviewed items |

**Tools.**

| Tool | Description |
|---|---|
| `build_workbook(state)` | XlsxWriter/openpyxl with named ranges, formulas, formatting, and data validation |
| `recalc_workbook(path)` | LibreOffice headless recalculation; runs after the LLM is unloaded to save RAM |
| `verify_parity(path, state)` | Reads recalculated values and compares them to the engine (tolerance 1 cent per share) |
| `render_charts(state)` | Football field, FCFF waterfall, scenario bars (matplotlib) |
| `write_memo(state)` | Template-driven; the LLM fills only the narrative sections using engine numbers injected as variables |

**Rule.** The LLM never types a number into the memo; it writes text around template variables filled from the state.
