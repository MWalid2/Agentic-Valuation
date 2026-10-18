# 04. Roadmap, Evaluation and Risks

Time estimates assume one developer working part-time (about 10-15 h/week) and are rough. Re-plan after Phase 1. The target machine is CPU-only with 16 GB RAM, so every phase is built against light models (3-8B) and the cache/replay tooling from Phase 0; don't wait on slow LLM runs while iterating.

## Guiding order

Build the **deterministic core first** (data → normalized financials → DCF → Excel), verify it without any LLM, then add agents one at a time. If the core is wrong, no amount of agent sophistication helps.

```
Phase 0  Foundations            ─► contracts, repo, local checks, local LLM running
Phase 1  Deterministic core     ─► EDGAR → Normalized → DCF → Excel (no LLM)     ★ most important
Phase 2  Router + Data + WACC   ─► Info mode (Q&A) works, first LangGraph
Phase 3  Filings RAG + Normalizer + Gate 1
Phase 4  Forecast (ToT) + Gate 2 ─► quick/full valuation end-to-end
Phase 5  Critic + Comps + Reporter + Gate 3
Phase 6  Evals, UI, hardening
Phase 7  Extensions
```

---

## Phase 0: Foundations (about 1 week)

**Deliverables**
- Repo scaffold (layout in `01-architecture.md`), `uv` env, ruff/mypy/pytest, pre-commit, local verification (no GitHub Actions).
- Pydantic contracts: `Provenance`, `Sourced`, `RawFinancials`, `NormalizedFinancials`, `ForecastAssumptions`, `WACCResult`, `ValuationResult`, `CritiqueReport`, `ValuationState`.
- Ollama running the approved lighter 1.7B and 4B 4-bit models, one resident at a time, with `OLLAMA_MAX_LOADED_MODELS=1`; a `scripts/benchmark_llm.py` that records prompt-processing and generation tokens/s and peak RAM for each candidate model. These replace the original 4B/8B plan to reduce desktop memory pressure.
- `llm/` wrapper with schema-constrained output, a repair retry, a single-worker request queue, an on-disk response cache (keyed by model + prompt hash + params), and a mock-LLM mode for tests.
- Config system for model roles, API keys, paths.

**Acceptance criteria**
- Local lint, formatting, type checks, and `pytest` pass; the LLM wrapper returns valid typed output for 20 sample prompts per selected model (≥ 95% valid on the first try, 100% after repair).
- Benchmarks are recorded in `docs/benchmarks-light.md` (original baseline preserved in `docs/benchmarks.md`); chosen models stay under 8 GiB sampled process RSS at peak, one resident at a time. This is a model-process budget, not total desktop memory. See `docs/phase-0-acceptance.md` for wrapper acceptance and limitations.

---

## Phase 1: Deterministic core (about 3-4 weeks) ★

Current data-layer compatibility and remaining handoff work are reviewed in
[`17-data-layer-review.md`](17-data-layer-review.md). Collection success is not
Phase 1 acceptance: constructed `RawFinancials` periods must also have the
validated concept coverage needed for normalization and valuation.

The 2026-10-14 extension now emits broader single-period `RawFinancials` and
missing-concept diagnostics; see [18-raw-financials.md](18-raw-financials.md).
The subsequent [history extension](19-financial-history.md) builds five annual
periods, eight standalone quarters and TTM, with standard filing-disclosure
fallbacks. Full concept coverage, fiscal-transition/custom-disclosure handling,
and normalization readiness remain pending; period counts alone are not acceptance.

**2026-10-15:** the general history plan ([20-general-history-plan.md](20-general-history-plan.md))
replaced tag lists with each filing's XBRL structure. Extraction matches 111 of 111
hand-recorded reference values within 0.5% across 11 companies (eight not used to
build the rules); readiness is measured on a 22-company targeted and 30-company
random cohort. The valuation core followed the same day
([22-valuation-core.md](22-valuation-core.md)): Normalizer v0, DCF engine, WACC
calculators, Excel workbook with LibreOffice parity to the cent, and the CLI.
**Phase 1 complete:** the engine matches Damodaran's FCFF template to rounding,
and an end-to-end point-in-time test confirms an earlier valuation date sees only
earlier filings. Next: Phase 2 (Router, Data Agent as a ReAct loop, WACC Agent,
terminal chat).

**Deliverables**
1. **EDGAR adapter** with cache, rate limiting, User-Agent, point-in-time filter, and statement validation. The approved 2026-10-13 redesign starts with an LLM debt-reading agent backed by controlled SEC tools and deterministic citation/numeric checks. The single-period snapshot now covers eleven cited fields (see `14-financial-snapshot.md`). Full annual/quarterly/TTM inputs remain required before Phase 1 acceptance.
2. **Damodaran + FRED adapters** with versioned snapshots. Implemented for five NYU tables and three FRED vintage series; exposed through Data Agent tools. See `15-data-agent-sources.md`.
3. **Prices adapter** (`yfinance` → Stooq → cache). Implemented daily history, split handling, explicit fallback limitations, and Data Agent assembly; weekly return series remain future work.
4. **Normalization (rule-based v0):** NOPAT, invested capital, ROIC, tax rates; leases and SBC treatment per defaults; manual adjustment hook.
5. **DCF engine:** FCFF, stub period + mid-year, Gordon TV with ROIC consistency, bridge, TSM dilution, scenarios, sensitivities.
6. **WACC calculators:** bottom-up beta, regression beta, synthetic rating.
7. **Excel builder + LibreOffice recalc + parity check.**
8. A CLI: `python -m dcf_agent.cli value AAPL 2026-10-11 --assumptions assumptions.yaml --out aapl.xlsx`.

**Acceptance criteria**
- Engine unit tests: hand-computed cases match to 1e-6; property tests pass.
- Cross-check of engine mechanics against a free published valuation template (e.g. Damodaran's FCFF spreadsheet) using identical inputs: value within 0.5%.
- On 3 reference companies (suggested: Apple, Coca-Cola, Nike), the Data Agent's extracted financials match the filings within 0.5% for revenue, EBIT, net income, cash, debt, shares.
- Excel parity: recalculated value per share equals the engine's to the cent.
- Point-in-time test: re-running with an earlier date uses only filings filed by that date (assert on accession dates).

---

## Phase 2: Router + Data Agent + WACC Agent: Info mode (about 2-3 weeks)

**Deliverables**
- LangGraph skeleton with shared state, checkpointing (SQLite), audit log.
- **Router** with company resolution, date parsing, scope check, intent classification, Gate 0.
- **Data Agent** as a ReAct loop on top of the Phase 1 adapters (the LLM decides fallbacks; the adapters do the work).
- **WACC Agent** (ReAct, comparing beta methods).
- Terminal chat (Typer + Rich): first message `"Apple, 2026-10-11"` → Gate 0 → answer data/WACC questions (intents I1, I3, I10). Chainlit comes in Phase 4, when Gate 2 needs an editable table.

**Acceptance criteria**
- Router: ≥ 95% correct company resolution and intent classification on a 50-prompt set (including ambiguous names and relative dates).
- WACC for 10 companies is within ±0.3% of the deterministic CLI result for the same inputs (the agent adds reasoning, not drift).
- Every number in an answer has a provenance record.

**Phase 2 complete (2026-10-15):** Router 50/50 on the 50-prompt set; WACC Agent
10/10 companies within 0.3 pt of the CLI (qwen3:4b); 4,231 answer figures across
393 answers, none without a source. Automatic collection from a ticker and a date,
LangGraph with Gate 0 and Gate 1 and SQLite checkpoints, a Data Agent loop with
guarded actions (52-company cohort: every blocker handled or explained, no
unrecorded change), archived Damodaran copies for past dates, and a terminal chat
with follow-up questions. Results, latency and known limits:
[23-phase-2-plan.md](23-phase-2-plan.md). Phase 3a (filings RAG, Normalizer, Gate 1) is in [24-phase-3a-plan.md](24-phase-3a-plan.md); Phase 4 is planned in [25-phase-4-plan.md](25-phase-4-plan.md).

---

## Phase 3: Filings RAG + Normalization Agent + Gate 1 (about 3 weeks)

**Deliverables**
- Filing downloader, section parser, table-preserving chunker, embeddings, hybrid retrieval, optional reranker.
- **Filings Analyst** producing `FilingInsights` with citations and self-consistency.
- **Normalization Agent** with CoT justifications, materiality threshold, **Gate 1** UI.
- Retrieval evals (hit-rate on a labelled question set) and extraction evals vs XBRL ground truth.

**Acceptance criteria**
- Retrieval hit-rate@5 ≥ 90% on a 100-question labelled set across 5 companies.
- Extraction accuracy of debt, lease and share-count figures ≥ 90% vs filings on the 7-8B model (the rest correctly flagged as low-confidence rather than wrong). XBRL-backed fields stay ≥ 99% because they are reconciled, not re-extracted.
- Adjustment suggestions on 5 companies reviewed manually: ≤ 1 false-positive per company.

---

## Phase 4: Forecast Agent (ToT) + Gate 2 (about 3-4 weeks)

**Deliverables**
- Tools: historical driver stats, industry benchmarks, fade paths, coherence scorer, `quick_dcf`, 8-K press-release context (filed on or before the valuation date).
- Tree-of-Thought pipeline: LLM picks axis levels with evidence → code enumerates a branch grid (e.g. 3 × 3) → deterministic scoring + one rubric call on the best 4 → prune → 3 scenarios → LLM writes the stories → expansion.
- **Gate 2 UI (Chainlit introduced here):** editable assumptions table, narrative, evidence links, "ask why" (intent I4), "re-think this scenario".
- Invalidation graph and incremental reruns (intent I11).
- End-to-end **Quick value** mode (intents I5, I6, I7).

**Acceptance criteria**
- 100% of generated assumption sets pass the hard bounds; ≥ 90% pass the soft-bound coherence checks on the golden set without edits.
- Scenario spread is meaningful (bear/bull differ by a documented minimum on value) and probabilities sum to 1.
- Anti-anchoring check: across golden companies, the correlation between the agent's base value and current market price is not suspiciously close to 1 (manual review of 5 cases).
- Editing one assumption reruns only dependent nodes (verified by test on the invalidation graph).

---

## Phase 5: Critic + Comps + Reporter + Gate 3 (about 3 weeks)

**Deliverables**
- **Comps Agent** using SEC bulk datasets and the price adapter; outlier handling.
- **Critic Agent** (deterministic checks + rubric) with the 2-loop routing.
- **Reporter:** Info answers, Quick summary, full workbook, memo (template-driven), football-field chart.
- **Gate 3** sign-off and export; "Unreviewed items" section for autopilot runs.

**Acceptance criteria**
- Seeded-error tests: inject 15 known flaws (terminal growth > Rf, ROIC < WACC, wrong debt figure, etc.); the Critic catches ≥ 90%.
- Critic loop terminates within the cap in 100% of runs.
- Full run for 3 golden companies produces an Excel that passes parity, formula-count and no-hardcode checks (key calc cells contain formulas, not constants).

---

## Phase 6: Evaluation, UI polish, hardening (about 3 weeks)

**Deliverables**
- Full eval suite run locally (see below), model-comparison harness for swapping open models.
- A small trace viewer over the JSONL traces and SQLite audit log (no external observability stack), plus latency and token metrics per agent.
- UI: side panel for assumptions, progress timeline, download buttons, error messages that explain what to do.
- Docs: user guide, developer guide, "how to add a new data source / new agent."
- Security review: no arbitrary code execution, input validation, secrets handling, SEC User-Agent compliance.

**Acceptance criteria**
- 20-company golden run completes unattended (autopilot, overnight batch) with ≥ 90% success, and failures produce diagnostics, not wrong numbers.
- End-to-end time on the 16 GB CPU machine is documented against a first target: full model in 45 minutes or less, Info answers from cache in under 60 seconds (adjust after Phase 4 timing).

---

## Phase 7: Extensions (backlog)

- Loss-making/high-growth companies (revenue-growth + target-margin convergence, survival probability).
- Banks/insurers (dividend discount / excess-return), REITs (FFO/AFFO, NAV), regulated utilities (regulated asset base or dividend discount; out of v1 scope since Phase 6).
- Reverse DCF and Monte Carlo on key drivers.
- IFRS filers and non-US currencies/risk-free rates and CRP.
- Multi-company comparison; portfolio-level views.
- Fine-tuning small models on your accepted outputs (only after you have enough reviewed data).
- MCP servers wrapping the tools so other clients can reuse them.

---

## Evaluation plan

| Eval | What it measures | Method | Target |
|---|---|---|---|
| Engine correctness | Math | Unit + property tests; parity with free templates | 100% pass |
| Data extraction | Financials vs filings | Compare with filing values on 5-20 companies | ≥ 99% (core lines) |
| Filing extraction | Debt/lease/shares/one-offs | Hand-labelled set | ≥ 90% or flagged |
| Retrieval | RAG hit-rate | Labelled Q→passage pairs | hit@5 ≥ 90% |
| Structured output | JSON validity | Rate on first attempt / after repair | ≥ 95% / 100% |
| Router | Entity + intent accuracy | 50-100 prompt set | ≥ 95% |
| Assumption quality | Coherence, bounds | Deterministic checks | 100% hard / ≥ 90% soft |
| Critic recall | Catches seeded errors | Inject flaws | ≥ 90% |
| Excel parity | Workbook = engine | Recalc via LibreOffice | 1 cent/share |
| Point-in-time | No look-ahead | Assert filing dates ≤ valuation date | 100% |
| End-to-end plausibility | Output vs benchmarks | Compare to market price band and peer multiples; manual review | Documented, reviewed |
| Model comparison | Open-model choice per role | Same eval suite, swap models | Choose best per role |
| Runtime budget | Time and RAM per agent and per full run | `benchmark_llm.py` + per-agent timers | Within the `03-stack.md` budget; regressions flagged |

**Golden set (suggested, varied profiles).** Stable cash generators (Coca-Cola, Procter & Gamble), mega-cap tech (Apple, Microsoft), high growth with heavy reinvestment (NVIDIA), cyclical (Caterpillar, an energy major), retail with big leases (Walmart, Costco), consumer brand (Nike), non-December fiscal year (Apple, Microsoft, Nike). Exclude financials, REITs and regulated utilities in v1.

**Important.** A DCF has no objective "right answer". Evals check mechanics, consistency, grounding, and plausibility, never "matches the market price".

---

## Risks and mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Free data source breaks (`yfinance`) | High | Medium | Adapter + cache + Stooq fallback; VCR tests; keep fundamentals on EDGAR |
| 3-8B model too weak for Forecast | High | High | "Choose, don't invent": the LLM picks levels, code instantiates numbers; deterministic coherence scoring; Gate 2 as the main control; swap models via config |
| Small models mis-extract numbers | High | High | Self-consistency, XBRL reconciliation, low-confidence flags, Gate 1 |
| XBRL tag inconsistency across companies | High | Medium | Tag fallback lists, HTML table parsing fallback, per-company overrides |
| CPU-only inference is slow; 16 GB RAM is tight | High | High | Deterministic-first design, one resident model, 4-8k context, LLM response cache and mock mode, background runs with checkpoints, Phase 0 benchmarks, no 14B+ models |
| Excel formulas diverge from the engine | Medium | High | Mandatory local recalc + parity test |
| Anchoring on market price | Medium | Medium | Anti-anchoring rule; keep price out of the Forecast prompt until the preview step |
| Scope creep (more agents, more features) | High | Medium | Phase gates with acceptance criteria; Phase 7 is backlog only |
| Licensing misreads | Low | Medium | Record each dependency/model license in a `LICENSES.md` at pin time |
| Misuse as investment advice | Medium | Medium | Disclaimers in UI, memo, and Excel cover |

---

## Open decisions (defaults in `README.md`)

1. Methodology: hybrid (default) vs pure Damodaran vs banker-style.
2. Interaction: interactive (default) vs autopilot-first.
3. R&D capitalization on or off by default.
4. Operating leases as debt (default) vs expense treatment.
5. First UI: Chainlit chat (default) vs Streamlit/Gradio.
6. Target hardware: fixed as CPU-only with 16 GB RAM. Revisit model classes and performance targets if you add a GPU or more RAM.

## First 10 tasks to start tomorrow

1. Create the repo with the layout from `01-architecture.md`.
2. Write the Pydantic contracts (`Provenance`, `Sourced`, core financial objects).
3. Implement the EDGAR adapter: submissions + companyfacts + cache + User-Agent.
4. Build `resolve_concept` tag lists for 20 core line items and test on Apple, Coca-Cola, Nike.
5. Implement `build_periods` (annual, quarterly, Q4 derivation, TTM) with tests.
6. Write the FCFF/terminal/bridge engine with hand-computed unit tests.
7. Add stub-period + mid-year logic with dedicated tests.
8. Download and snapshot Damodaran datasets; write the loaders.
9. Build the Excel exporter v0 (Inputs, FCFF, WACC, Bridge) and the LibreOffice parity check.
10. Run Ollama with one 3-4B model, benchmark it (tokens/s and peak RAM), and wire up the structured-output wrapper with the response cache.
