# DCF-Agent: A Multi-Agent DCF Valuation Assistant

> Working title. **Constraint: 100% free and open source.** Open-weight LLMs run locally, free public data sources, open-source libraries. No paid APIs.
>
> **Target machine: CPU-only, 16 GB RAM, no GPU.** Everything below is sized for that: 3-8B quantized models, one loaded at a time, SQLite instead of server databases, no Docker services.

## 1. Vision

A conversational assistant that values a public US company with a discounted cash flow (DCF) model, as of a date the user chooses, and can either:

- **answer any specific question** about the valuation ("What WACC would you use?", "Why 3% terminal growth?", "What if margins reach 35%?"), or
- **deliver a complete, formula-driven Excel model** plus a short memo.

The user starts with one message:

```
Apple, 2026-10-11
```

The system confirms the company, asks what the user wants (a question, a quick fair value, or the full Excel model), then runs only the agents it needs.

## 2. Design principles

1. **LLMs reason, code calculates.** Every number that feeds the valuation (FCFF, WACC, terminal value, equity bridge, per-share value) is computed by deterministic, unit-tested Python. LLMs choose assumptions, read filings and write rationale. This matters even more with open-weight models, which are weaker at arithmetic.
2. **Every number has provenance.** Each value carries source, reference (accession number and XBRL tag, FRED series, dataset version), as-of date, and confidence. No number reaches the report without it.
3. **Point-in-time correctness.** The valuation date drives everything: only filings *filed* on or before it, the price on that date, the Treasury yield on that date. No look-ahead bias.
4. **Typed contracts between agents.** Agents exchange validated Pydantic objects, never free text.
5. **Humans approve the judgment calls** (assumptions and material adjustments), not the plumbing.
6. **Small, narrow agents.** Open-weight models do best with short contexts, one job, and constrained (JSON-schema) outputs. Large decisions are decomposed into small, checkable steps.
7. **Free data is imperfect, so degrade gracefully.** Every data adapter has a fallback and reports its confidence. Missing data produces a flagged warning, never a silent guess.
8. **Deterministic first, LLM as assistant (light-hardware rule).** On a 3-8B CPU model, every agent has a rule-based default path that works without the LLM. The LLM only adds judgment where code cannot.
9. **Choose, don't invent.** Small models select among bounded options (for example growth durability: low / medium / high) and write short rationales. Numbers are instantiated by code from history and benchmark tables, never typed by the LLM.

## 3. Scope

| | v1 | Later |
|---|---|---|
| Companies | US-listed, non-financial, positive operating history | Loss-making/high-growth (v2), banks/insurers (DDM, v3), REITs (FFO/NAV) |
| Accounting | US GAAP (via SEC XBRL) | IFRS, non-US |
| Method | FCFF DCF, 3 scenarios, probability-weighted | Monte Carlo, reverse DCF |
| Output | Chat answers + Excel + short memo | Side-panel assumption editor |

Banks, insurers and REITs are rejected or redirected at intake: an FCFF DCF is the wrong tool for them.

## 4. Defaults assumed (change any of these)

| Decision | Default |
|---|---|
| Methodology style | **Hybrid**: Damodaran-style story-to-numbers (growth, margin, sales-to-capital, fade to terminal) with explicit capex/D&A/NWC lines shown in the Excel |
| Interaction | **Interactive** with human gates; an `autopilot` toggle auto-approves and records warnings |
| Data | **Free only**: SEC EDGAR, FRED, Damodaran datasets, Yahoo (via `yfinance`), Stooq |
| First UI | Terminal chat (Typer + Rich) in early phases; Chainlit with approve/edit buttons from Phase 4; side panel later |
| Stock-based comp | Treated as a real expense (not added back) |
| Leases | Operating leases treated as debt, with consistent EBIT adjustment |
| LLM runtime | Ollama on CPU; one 3-4B (S) and one 7-8B (M) 4-bit model, only one resident at a time |
| Storage | One SQLite file (checkpoints, audit log, text and vector search) plus DuckDB/Parquet for data; no database servers, no Docker |
| News context | No web search in v1; use earnings-release 8-Ks from EDGAR |

## 5. Document map

| File | Contents |
|---|---|
| [`README.md`](README.md) | This overview |
| [`01-architecture.md`](01-architecture.md) | System design, flows, shared state, data contracts, human gates, reasoning patterns, guardrails |
| [`02-agents.md`](02-agents.md) | Explicit spec of every agent: role, inputs, outputs, tools, procedure, failure modes |
| [`03-stack.md`](03-stack.md) | Free/open-source stack for every layer, data-source limits, model sizing and hardware |
| [`04-roadmap.md`](04-roadmap.md) | Phased build plan, acceptance criteria, evaluation plan, risks |
| [`05-development-tools.md`](05-development-tools.md) | Installed tools, their roles, current configuration, and development commands |
| [`06-data-contracts.md`](06-data-contracts.md) | Why we use Pydantic, each planned contract's role, validation, and implementation order |
| [`07-configuration.md`](07-configuration.md) | Typed settings, credentials, paths, model roles, CPU limits, and configuration loading |
| [`08-local-llm-wrapper.md`](08-local-llm-wrapper.md) | Serialized local inference, repair, mocks, response cache, and limitations |
| [`09-model-setup.md`](09-model-setup.md) | Installed candidates, provisional role assignments, benchmark procedure, and live mode |
| [`10-intent-benchmark.md`](10-intent-benchmark.md) | Intent definitions, scoring criteria, revised lighter-model comparison, and limitations |
| [`11-local-checks.md`](11-local-checks.md) | Local verification, reproducible dependencies, and live wrapper acceptance |
| [`12-sec-edgar-adapter.md`](12-sec-edgar-adapter.md) | Local document-reading agent, controlled SEC tools, cited debt validation, and limits |
| [`13-company-adapter-checks.md`](13-company-adapter-checks.md) | Apple, Coca-Cola, and Nike integration results and remaining mapping gaps |
| [`14-financial-snapshot.md`](14-financial-snapshot.md) | Single-period extraction contract, field status, units, dates, and review policy |
| [`15-data-agent-sources.md`](15-data-agent-sources.md) | Damodaran, FRED, prices, bounded Data Agent tools, combined collection and offline replay |
| [`16-data-agent-company-checks.md`](16-data-agent-company-checks.md) | Multi-company integration results and reference comparisons |
| [`17-data-layer-review.md`](17-data-layer-review.md) | Architecture fit, downstream handoff gaps, and the purpose of extraction refinements |
| [`18-raw-financials.md`](18-raw-financials.md) | Broader statement inputs, single-period RawFinancials handoff, and missing-data boundaries |
| [`19-financial-history.md`](19-financial-history.md) | Annual/quarterly history, TTM, standard disclosure fallback, and period-specific gaps |
| [`20-general-history-plan.md`](20-general-history-plan.md) | Plan for history that generalizes to all US non-financial companies; progress log |
| [`21-data-agent-explained.md`](21-data-agent-explained.md) | Plain-language overview: what the Data Agent does, its sources, and how results were made exact |
| [`22-valuation-core.md`](22-valuation-core.md) | Normalizer, DCF engine, WACC calculators, Excel workbook with LibreOffice parity, and the CLI |
| [`23-phase-2-plan.md`](23-phase-2-plan.md) | Phase 2 plan, progress and acceptance results: Router, Gates 0 and 1, Data and WACC agents, sourced answers, LangGraph, terminal chat, latency, known limits |
| [`24-phase-3a-plan.md`](24-phase-3a-plan.md) | Phase 3a plan: filings RAG (sections from inline-XBRL text blocks, hybrid retrieval), Filings Analyst, Normalization Agent, Gate 1, RAGAS evaluation |
| [`phase-0-acceptance.md`](phase-0-acceptance.md) | Completed foundation checks, wrapper results, memory evidence, and limitations |
| [`benchmarks-light.md`](benchmarks-light.md) | Revised benchmark results for lighter candidates using explicit intent definitions |
| [`benchmarks.md`](benchmarks.md) | Measured local CPU throughput, structured-output validity, and memory observations |

## 6. Honest limitations of an all-free approach

- **No consensus estimates.** Forecasts rely on history, management guidance (8-K/10-K), industry benchmarks (Damodaran) and the user's judgment at Gate 2.
- **Free market data is fragile.** `yfinance` is an unofficial scraper and can break. We isolate it behind an adapter, cache aggressively, and keep Stooq as a fallback.
- **3-8B models are much weaker than large open models.** They are unreliable at long-context reasoning and native tool calling. The architecture compensates with narrow agents, constrained decoding, deterministic scoring, "choose, don't invent" prompts, and human gates. Expect the Forecast agent to be the weakest link, so Gate 2 is the most important control.
- **CPU-only means slow.** Expect a full Excel model to take roughly 30-45 minutes (a rough estimate; Phase 0 measures your real numbers), so full runs are background jobs with progress and checkpoints. Questions answered from cached state take seconds.
- **16 GB RAM is a hard budget.** One model resident at a time, 4-8k tokens of context, no 14B+ models, no database servers. See `03-stack.md`.

## 7. Disclaimer

The output is a modeling and educational tool. It is not investment advice.
