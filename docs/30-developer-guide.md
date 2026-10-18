# 30. Developer guide

How the code is organized, the rules every change keeps, how to run the checks, and how to add a data source or an agent. The design documents are [01-architecture.md](01-architecture.md), [02-agents.md](02-agents.md) and [06-data-contracts.md](06-data-contracts.md); the phase plans (23 to 27) record what was built, measured and decided.

## Layout (`src/dcf_agent/`)

| Package | Contents |
|---|---|
| `contracts/` | Pydantic models for everything that crosses a boundary: inputs, financials, forecasts, results, workflow records. `provenance.py` defines `Sourced[T]` and `Provenance` |
| `data/` | Source adapters (SEC EDGAR and XBRL, FRED, Damodaran, prices), the response cache, concept extraction, history, scope and readiness |
| `rag/` | Filing text: parsing into chunks, the SQLite index (BM25 plus local embeddings), insights from notes, earnings releases |
| `tools/` | What agents call on data (filings by period, snapshot helpers) |
| `engine/` | Deterministic finance: assembly of the bridge and shares, normalization, WACC, drivers, DCF, the valuation record, the provenance audit |
| `agents/` | Router, Data loop, Filings, Normalizer, WACC, Forecast, Comps, Critic; answers; Gate 2 helpers |
| `graph/` | LangGraph workflow (nodes, gates, dispatch, invalidation), state, checkpoint serializer, live services |
| `excel/` | The formula workbook, LibreOffice parity, structure checks |
| `report/` | Summary, football-field SVG, memo, export, trace view |
| `llm/` | The local model wrapper: Ollama, schemas, one repair, response cache, traces |
| `chat.py`, `cli.py`, `batch.py` | The terminal chat, the command line, unattended batch runs |

## Rules every change keeps

1. **Every number is sourced.** A value that enters a valuation is a `Sourced[float]`, with a source (sec_xbrl, sec_filing_text, fred, damodaran, yfinance, derived, user_override) and a reference to trace it. External values need a URL or an accession; derived ones name their inputs or the policy, and say how in their note. `engine/audit.py` enforces this, and the batch runner calls an export that fails it a wrong number.
2. **Choose, don't invent.** A model picks among levels, verdicts or items, with evidence. Code turns the pick into numbers and rejects picks the data doesn't support (the support rules in the WACC and Forecast agents).
3. **Point in time.** Nothing filed after the valuation date is used (contracts check evidence cutoffs).
4. **The engine is deterministic** and checked against the workbook (parity to the cent) and against templates.
5. **A failure is explained, not hidden.** A node catches its source's or the engine's errors, records a warning, and the session continues or stops with a reason.
6. **General, not company-specific.** A fix for one company must be a general rule with a test, never a special case for that company.

## Running the checks

```powershell
uv run --frozen ruff check
uv run --frozen ruff format --check
uv run --frozen mypy
uv run --frozen pytest -p no:cacheprovider --basetemp=<a writable folder>
```

Tests need no network and no Ollama: they use saved bundles (`outputs/insights-cohort/`), fake services and mock models. Live checks are scripts:

| Script | Checks |
|---|---|
| `check_value.py` | Phase 4 quick value |
| `check_comps.py` | Comps |
| `check_report.py` | Phase 5 full model with parity and structure |
| `run_batch.py` | Phase 6 unattended cohorts |
| `trace_report.py` | A session's trace page |
| `measure_latency.py` | Stage latency, cache off |

## Adding a data source

1. **Contract.** Define what it returns in `contracts/` (or reuse one), with `Sourced` values and point-in-time validation.
2. **Adapter** in `data/`:
   - an `httpx.Client` with a timeout and `trust_env=False`;
   - requests through `data/cache.get_response` (bounded retries; no URL in errors, so keys never leak);
   - responses kept in the response cache, so `offline=True` replays them;
   - parsers with `no_network=True` (and `resolve_entities=False` for XML).
3. **Provenance.** Every value gets its source, a reference (URL or series), `as_of`, the period and the filing date where they apply, and a note on how it was read.
4. **Secrets.** An API key goes in `.env` and `config.py` as a `SecretStr`, never into a cache key, a log or an error message.
5. **Tests.** Recorded responses through a mock transport: the happy path, missing fields, a bad response, offline replay.
6. **Wiring.** Open the client in `graph/services.LiveServices`; add the tool to `tools/` if agents call it.

## Adding an agent

1. **Role.** Add it to `Role` in `config.py` and to `config/models.yaml` (model, context, output length). Keep to models that fit 16 GB RAM on the CPU.
2. **Schema.** The model answers a pydantic model with `extra="forbid"` and bounded fields; picks are literals or IDs from a list you give it, never free numbers.
3. **Prompt and code.**
   - The prompt shows the evidence with IDs and asks for a pick with a reason.
   - Code checks the pick against the data (a support rule) and builds the numbers with provenance.
   - Bump `PROMPT_VERSION` when the prompt changes, which invalidates cached replies.
4. **Fallback.** With no model, or after a failed repair, a rule gives the answer, so tests and offline runs work.
5. **Graph node.**
   - A function in `graph/workflow.py` that records its events (`rec.event`) and catches its errors.
   - Add the node to `WIRED`, to the node list and to the dispatch targets; add its step to the intents' `PLANS` in `agents/router.py`.
   - Add its dependencies to `DEPENDS`, so changes invalidate it.
   - New state fields go in `graph/state.py`; a new contract module must be allowed in `graph/serde.py`.
6. **Gates.** If people must review its output, add a gate that asks through `ask(...)`, so autopilot can approve it on the record, and a screen in `chat.py`.
7. **Tests.** Unit tests on the rule and the fallback, a mock-model test of the parse and the support rule, and a graph test with fake services.
8. **Checks.** If its numbers reach the valuation, the audit must stay clean, and the Critic should get a check for its typical error.
