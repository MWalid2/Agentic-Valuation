# 01. Architecture

## 1. System overview

```
┌────────────────────────────────────────────────────────────────────────┐
│  INTERFACE        Terminal chat → Chainlit (approve/edit/ask-why)      │
└───────────────────────────────┬────────────────────────────────────────┘
                                │ FastAPI (sessions, streaming, files)
┌───────────────────────────────▼────────────────────────────────────────┐
│  ORCHESTRATION    LangGraph state machine                              │
│                   shared ValuationState · checkpoints · interrupt/resume│
└───────────────────────────────┬────────────────────────────────────────┘
                                │
   ┌────────────┬───────────────┼────────────┬─────────────┬─────────────┐
   ▼            ▼               ▼            ▼             ▼             ▼
 Router      Data Agent    Filings Agent  Normalizer   Forecast Agent  WACC Agent
 (Plan)      (ReAct)       (RAG + CoT)    (CoT)        (ToT + CoT)     (ReAct)
   │            │               │            │             │             │
   └────────────┴───────────────┴─────┬──────┴─────────────┴─────────────┘
                                      ▼
                      ┌──────────────────────────────┐
                      │ DCF ENGINE (pure Python,      │  ◄─ the ONLY source of
                      │ no LLM, unit-tested)          │     valuation numbers
                      └──────────────┬───────────────┘
                                     ▼
                  Comps Agent ─► Critic Agent ─► Reporter (answer / Excel / memo)

┌────────────────────────────────────────────────────────────────────────┐
│  TOOL LAYER       EDGAR · FRED · Damodaran · yfinance/Stooq · RAG ·    │
│                   calculators · Excel builder · LibreOffice recalc     │
├────────────────────────────────────────────────────────────────────────┤
│  DATA LAYER       DuckDB + Parquet (raw/normalized) · one SQLite file  │
│                   (checkpoints, audit log, FTS5 text + vectors)        │
├────────────────────────────────────────────────────────────────────────┤
│  MODEL LAYER      Ollama (CPU) · ONE 3-8B model resident at a time     │
├────────────────────────────────────────────────────────────────────────┤
│  OBSERVABILITY    JSONL traces · SQLite audit log · tiny viewer        │
└────────────────────────────────────────────────────────────────────────┘
```

## 2. Two modes, one graph

The Router builds a plan (Plan-and-Execute) and the graph executes only the needed nodes.

| Mode | Triggered by | Pipeline | Output |
|---|---|---|---|
| **Info (Q&A)** | A specific question | Minimal subgraph from the dependency map (section 4) | Short answer + supporting numbers + sources |
| **Quick value** | "fair value per share?" | Full pipeline without Excel; Gate 2 required unless autopilot | Value range, key assumptions, top sensitivities |
| **Full model** | "build the Excel" | Full pipeline, all gates | `.xlsx` with live formulas + 2-4 page memo + audit log |

All intermediate results live in shared state and are cached by `(ticker, valuation_date, input_hash)`, so a follow-up question after a full run is nearly free.

## 3. Request lifecycle (full model)

```
 1  User: "Apple, 2026-10-11"
 2  Router: resolve ticker/CIK, validate date, check scope (non-financial?)
 3  ── GATE 0 ── confirm entity + choose output (question / quick / Excel)
 4  Data Agent  ║  Filings Agent         (run in parallel)
 5  Normalizer: adjustments, invested capital, ROIC, tax rates
 6  ── GATE 1 ── only if any adjustment is material (> 3% of EBIT) or ambiguous
 7  Forecast Agent (ToT) ║ WACC Agent     (parallel)
 8  ── GATE 2 ── APPROVE ASSUMPTIONS (main gate: edit / approve / ask why)
 9  DCF Engine: 3 scenarios + sensitivities → ValuationResult
10  Comps Agent: relative-value cross-check
11  Critic Agent: rubric + deterministic checks
12     └─ fail? route back to 7 (max 2 loops), re-pass Gate 2 if assumptions changed
13  ── GATE 3 ── final sign-off (show value, football field, flagged warnings)
14  Reporter: Excel + parity check + memo
```

**Light-hardware note.** "Parallel" above means parallel network I/O (EDGAR, FRED, prices) only. All LLM calls go through a single-worker queue so only one model runs at a time. A full run takes roughly 30-45 minutes on a CPU-only machine, so it runs as a background job with checkpoints and progress updates. See section 13 and `03-stack.md`.

## 4. Intent catalog and dependency map (Info mode)

The Router classifies the question into an intent and runs only what the answer depends on.

| # | Intent | Example | Agents / components run | Output |
|---|---|---|---|---|
| I1 | Data snapshot | "Latest revenue and FCF?" | Data (+ Filings if from notes) | Numbers + sources |
| I2 | Historical analysis | "Margin and ROIC trend, 5 years?" | Data → Normalizer | Table + short commentary |
| I3 | Cost of capital | "What WACC would you use?" | Data → WACC | WACC build-up + sensitivity band |
| I4 | Assumption rationale | "Why 3% terminal growth?" | Reads state; reruns Forecast if absent | Reasoned answer citing rule/evidence |
| I5 | Scenario | "Bull-case 2030 revenue?" | Data → Filings → Normalizer → Forecast | Scenario drivers + narrative |
| I6 | Quick valuation | "Fair value per share?" | Full pipeline minus Excel | Value, range, key drivers |
| I7 | Sensitivity | "What if WACC +1%?" | DCF Engine on cached state | Delta table (no LLM except wording) |
| I8 | Comps | "How does it compare to peers?" | Comps | Peer table, implied range |
| I9 | Full model | "Build the Excel" | Everything | `.xlsx` + memo |
| I10 | Audit / explain | "Where does this number come from?" | Audit-log lookup | Provenance chain |
| I11 | Modify | "Set operating margin to 35%" | Patch state → invalidate downstream → rerun | Updated values with diff |

**Invalidation graph.** Changing a value re-runs only its dependents:

```
RawFinancials ─► Normalized ─► Forecast ─► DCF ─► Critic ─► Report
FilingInsights ─►┘                 ▲         ▲
MarketData/Macro ─► WACC ──────────┴─────────┘
```

## 5. Shared state and data contracts

### 5.1 Provenance wrapper (used for every sourced value)

```python
class Provenance(BaseModel):
    source: Literal["sec_xbrl", "sec_filing_text", "fred", "damodaran",
                    "yfinance", "stooq", "derived", "llm_estimate", "user_override"]
    ref: str                      # accession+tag, FRED series, dataset+version, URL
    period_end: date | None
    filed: date | None            # filing date, for point-in-time checks
    as_of: date
    confidence: Literal["high", "medium", "low"]
    note: str | None = None

class Sourced(BaseModel, Generic[T]):
    value: T
    prov: Provenance
```

### 5.2 Graph state

```python
class ValuationState(TypedDict):
    request: ValuationRequest          # ticker, cik, valuation_date, mode, intent, plan[]
    raw: RawFinancials                 # IS/BS/CF annual + quarterly + TTM, Sourced values
    market: MarketData                 # price, shares, market cap, returns history
    macro: MacroData                   # Rf, ERP, CRP, spreads, inflation
    filing_insights: FilingInsights    # debt schedule, leases, dilutive securities, guidance...
    normalized: NormalizedFinancials   # clean EBIT, invested capital, ROIC, tax rates
    adjustments: list[Adjustment]      # amount, rationale, evidence, materiality
    forecast: dict[Scenario, ForecastAssumptions]  # bear/base/bull + probabilities
    wacc: WACCResult
    valuation: dict[Scenario, ValuationResult]
    comps: CompsResult
    critique: CritiqueReport
    approvals: dict[Gate, ApprovalRecord]   # who/when/what changed
    artifacts: dict[str, Path]         # xlsx, memo, charts
    audit_log: list[Event]             # every tool call, rationale, hash of inputs
    budget: Budget                     # steps, tokens, wall-clock remaining
```

### 5.3 Key schemas (abridged)

```python
class ValuationRequest(BaseModel):
    ticker: str; cik: str; company_name: str
    valuation_date: date
    mode: Literal["info", "quick", "full"]
    intent: str | None                 # I1..I11
    question: str | None
    autopilot: bool = False

class ForecastAssumptions(BaseModel):          # one per scenario
    scenario: Literal["bear", "base", "bull"]; probability: float
    revenue_growth: list[Sourced[float]]       # year 1..N (N=5 or 10)
    operating_margin: list[Sourced[float]]
    sales_to_capital: list[Sourced[float]]     # drives reinvestment
    tax_rate: list[Sourced[float]]             # effective → marginal
    terminal_growth: Sourced[float]
    terminal_roic: Sourced[float]
    narrative: str                             # story that links the numbers
    rationale: dict[str, str]                  # per-assumption CoT summary

class WACCResult(BaseModel):
    rf, erp, crp, unlevered_beta, relevered_beta, cost_equity: float
    pretax_kd, tax_rate, aftertax_kd: float
    weight_equity, weight_debt: float
    wacc: float; wacc_band: tuple[float, float]
    method_notes: list[str]

class ValuationResult(BaseModel):
    fcff_by_year: list[float]; discount_factors: list[float]
    pv_explicit: float; terminal_value: float; pv_terminal: float
    enterprise_value: float
    bridge: dict[str, float]           # cash, debt, leases, minorities, pensions...
    equity_value: float; diluted_shares: float; value_per_share: float
    tv_share_of_ev: float
    implied: dict[str, float]          # implied EV/EBITDA, implied P/E, ROIC path
```

## 6. Point-in-time rules

| Input | Rule |
|---|---|
| Financial statements | Use only facts with `filed ≤ valuation_date`. For a given period, take the latest filing on or before the date (handles restatements correctly). |
| Latest annual / quarterly | Latest 10-K and 10-Qs filed on or before the date; TTM = FY + current YTD - prior-year YTD. Q4 = FY - 9M YTD. |
| Share count | Cover-page shares (`dei:EntityCommonStockSharesOutstanding`) of the latest filing, plus dilutive securities from notes. |
| Price | Close on the valuation date (or last trading day before it). |
| Risk-free rate | 10-year Treasury (FRED `DGS10`) on the date. |
| Equity risk premium | Damodaran implied ERP: latest value on or before the date. |
| Industry data | Latest Damodaran dataset released on or before the date. Snapshot each dataset version in the repo. |
| Beta | Regression on prices ending on the valuation date only. |
| News / web context | Restrict to items dated on or before the valuation date. |

If the valuation date is in the future or today, "latest available" is used and noted in the report.

## 7. Human-in-the-loop (HITL) gates

Implemented with LangGraph `interrupt()` and `Command(resume=...)`; state is checkpointed, so the user can leave and resume.

| Gate | When | Shown to user | User actions |
|---|---|---|---|
| **0 Intake** | After entity resolution | Company, ticker, CIK, FYE, latest filings, scope check, output options | Confirm / pick another entity / choose output |
| **1 Adjustments** | Only if an adjustment is > 3% of EBIT, or the agent confidence is low | Table: item, amount, evidence (filing section), proposed treatment | Accept / reject / edit each item |
| **2 Assumptions** (main) | Before DCF runs | Scenario table (growth, margin, reinvestment, tax, terminal), WACC build-up, narrative, evidence, Critic pre-flags | Approve / edit any cell / ask "why?" / request re-think of a scenario |
| **3 Sign-off** | After Critic passes | Value per share, scenario weights, football field, sensitivities, warnings | Approve and export / go back to Gate 2 |

Rules:
- Edits are validated against the sanity bounds in section 10 before they are accepted. Out-of-bounds edits require an explicit "I know" flag, which is stored in the audit log.
- **Autopilot:** gates auto-approve, but every warning is written into the report's "Unreviewed items" section.
- Timeout: a pending gate simply stays checkpointed; no auto-approval unless autopilot is on.

## 8. Reasoning patterns: where each is used

| Pattern | Where | How it is applied |
|---|---|---|
| **Plan-and-Execute** | Router | Produce an explicit plan (list of nodes) per request; revise if a step fails |
| **ReAct** | Data Agent, WACC Agent | Think → act → observe → retry/fallback, implemented as **constrained JSON actions** (an enum of allowed tools, max 4 steps) that code executes, not native tool calling, which 3-8B models handle poorly. A rule-based default policy runs first. |
| **CoT** | Filings, Normalizer, Forecast, Critic | Short structured rationale per decision, stored in `rationale` fields and audit log (not free-form chains) |
| **Tree-of-Thought** | Forecast scenarios; terminal-value method choice | Code enumerates branches from a grid of uncertainty levels, scores each with deterministic coherence checks, prunes to the top 3; the LLM only picks axis levels with evidence and writes the story for the survivors |
| **RAG** | Filings Agent | Hybrid BM25 + dense retrieval over section-aware chunks, answers must cite section/page |
| **Self-consistency** | Filings extraction (only fields with no XBRL ground truth) | Extract twice with differently worded prompts at temperature 0, a third time only on disagreement; accept on agreement, else flag for Gate 1 |
| **Program-aided reasoning** | All numeric work | The LLM emits parameters; Python tools do the math |
| **Reflection / Critic** | Critic loop | Separate agent (different prompt and ideally different model family) reviews; max 2 revision loops |
| **LLM-as-judge** | Critic, evals | Rubric-based scoring; always paired with deterministic checks |
| **Supervisor / multi-agent** | Whole graph | Specialized agents with typed hand-offs, parallel branches |
| **Human-in-the-loop** | Gates 0-3 | Approve or edit judgment calls |
| **Structured outputs** | Every agent edge | JSON-schema constrained decoding + Pydantic validation + one repair retry |
| **Memory** | Session + cross-session | Checkpoints (short-term), cache and past approved valuations (long-term) |
| **Bull/Bear debate (optional)** | Forecast | Two prompts argue the high and low case; the arbiter merges. Off by default on CPU (about 3x the cost); enable only if the M model handles it. |

## 9. Error handling and graceful degradation

| Failure | Response |
|---|---|
| XBRL tag missing | Try the tag fallback list, then parse the statement table from the filing HTML, then flag `confidence=low` |
| Price source down | Fallback `yfinance` → Stooq → cached; mark in report |
| Malformed LLM JSON | Constrained decoding first; on failure one repair prompt with the validation error; then switch that call to the M model; then use the rule-based default and flag it; then ask the user |
| Tool timeout / rate limit | Exponential backoff, respect EDGAR 10 req/s and local cache; surface a partial result |
| Balance sheet doesn't balance, units off (thousands vs millions) | Hard stop at Data Agent with a diagnostic; never continue silently |
| Loop or runaway | Per-request budget (steps, tokens, wall-clock); Critic loop capped at 2 |
| LLM disagrees with the engine | The engine wins; the LLM text is regenerated from engine output |
| Out of memory or an LLM call is too slow | Context cap and token budget per call; unload the model before LibreOffice runs; on timeout use the rule-based default and flag it |

## 10. Guardrails and sanity bounds

Checked before any assumption set can reach the DCF engine (hard bounds), or flagged (soft bounds):

| Check | Type | Rule |
|---|---|---|
| Terminal growth | Hard | `g ≤ risk-free rate` and `g ≤ long-run nominal GDP proxy (≈ 4%)` |
| Terminal ROIC | Soft | Should be `≥ WACC`; if `ROIC > WACC + 5%`, require moat justification |
| Terminal reinvestment | Hard | `reinvestment_rate = g / ROIC_terminal` (enforced by the engine) |
| WACC | Soft | Outside 5-15% needs a note; terminal WACC should converge toward mature-firm level |
| Margin | Soft | Above 90th percentile of the industry needs a moat story |
| Growth | Soft | Revenue CAGR above 25% for >5 years flagged |
| TV share of EV | Soft | > 75% flagged |
| Tax rate | Hard | Converges to marginal rate by terminal year |
| Provenance | Hard | No sourced value without a `Provenance` record |
| Code execution | Hard | LLMs call fixed tools only. If a sandboxed Python tool is ever added, it runs in a locked-down container with no network access. |

## 11. Caching and memory

- **Raw cache:** every EDGAR/FRED/price response is stored as Parquet/JSON keyed by URL + params + date. Reproducible re-runs and offline development.
- **Result cache:** `(ticker, valuation_date, input_hash)` → state snapshot.
- **LLM response cache:** keyed by `(model, prompt hash, params)`. Development, tests and re-runs never pay for the same inference twice.
- **Text and vector store:** per-filing chunks keyed by accession number in one SQLite file (FTS5 for BM25, float32 blobs for vectors); embeddings computed once.
- **Long-term memory:** approved valuations and user preferences (methodology flags, autopilot) saved per user. Used as defaults and for regression tests, never to bypass gates.

## 12. Repository layout

```
dcf-agent/
├── docs/                      # these markdown files
├── src/dcf_agent/
│   ├── contracts/             # Pydantic schemas (Provenance, State, Results)
│   ├── engine/                # deterministic DCF core: fcff.py, wacc.py, terminal.py,
│   │                          #   bridge.py, dilution.py, sensitivity.py, scenarios.py
│   ├── data/                  # adapters: edgar.py, fred.py, damodaran.py, prices.py
│   ├── rag/                   # chunking, indexing, retrieval
│   ├── agents/                # router, data, filings, normalizer, forecast, wacc,
│   │                          #   comps, critic, reporter
│   ├── tools/                 # tool wrappers exposed to agents
│   ├── graph/                 # LangGraph wiring, gates, routing, invalidation
│   ├── excel/                 # workbook builder, templates, recalc/parity check
│   ├── llm/                   # model registry, structured-output helpers, prompts,
│   │                          #   single-worker queue, response cache, mock LLM
│   └── api/                   # FastAPI + Chainlit app
├── data/                      # cached raw data, Damodaran snapshots (git-ignored)
├── tests/                     # unit (engine), integration (data), golden, evals
├── evals/                     # golden companies, expected values, judge rubrics
└── scripts/                   # setup, benchmark_llm.py, trace_view.py (no Docker needed)
```

## 13. Light-hardware design rules (CPU-only, 16 GB RAM)

1. **Deterministic first.** Every agent has a rule-based default path. The LLM is called only for decisions code can't make (see the "LLM decides vs code decides" table in `02-agents.md`). If the LLM is slow, malformed or unavailable, the pipeline still completes and the output is marked "rule-based default".
2. **Choose, don't invent.** The LLM picks from bounded options (enums, levels, keep/drop). Numbers are instantiated by code from the company's history, Damodaran tables and fade rules, then edited by the human at Gate 2.
3. **One model at a time.** A single-worker queue serializes all LLM calls. Work is ordered to minimize model swaps (S-model steps, then M-model steps, where dependencies allow).
4. **Context budget.** At most 4k tokens per call (8k hard cap). Retrieval returns at most 4 passages of 300-500 tokens. Never paste a whole filing.
5. **Short outputs.** Flat JSON schemas, enums, rationale fields of at most 2 sentences, `num_predict` set per role.
6. **No long reasoning mode.** Disable "thinking" modes. Chain-of-thought is a short structured `rationale` field.
7. **Cache and replay.** Every LLM response is cached; tests use a mock-LLM fixture; evals can replay recorded responses.
8. **Background execution.** A full run is a job with progress events and checkpoints. The user can close the terminal or tab and resume.

**Reasoning patterns in their light form.**

| Pattern | Light implementation |
|---|---|
| CoT | Two-sentence `rationale` fields per decision; no free-form chains |
| Tree-of-Thought | Grid enumeration in code + deterministic scoring + one rubric call on the best 4 + stories for 3 survivors |
| ReAct | Constrained JSON actions, max 4 steps, rule-based default policy first |
| RAG | SQLite FTS5 + small ONNX embedder, top 4 passages, citations required |
| Self-consistency | Only for numeric fields with no XBRL ground truth: 2 runs, a third on disagreement |
| Reflection / Critic | Deterministic checks find most issues; the LLM rubric is a handful of 1-5 scores; max 2 loops, each re-running only the flagged agent |
| LLM-as-judge | Small fixed rubric; judge from a different model family than the generator when possible |
| Human-in-the-loop | Unchanged: gates cost no compute and carry the most judgment |
| Supervisor / multi-agent | One process, LangGraph graph, typed hand-offs |
