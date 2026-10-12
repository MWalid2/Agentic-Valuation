# 03. Stack: Free, Open Source, CPU-Only (16 GB RAM)

> **Target machine: CPU-only, 16 GB RAM, no GPU.** Every choice below is made to fit that. Licenses, model names and speeds change, so verify them when you pin versions. Throughput and run-time numbers are **estimates**; Phase 0 replaces them with measurements from your machine.

## 1. Design rules for a light machine

Details are in `01-architecture.md` section 13. In short:

1. **Deterministic first.** Every agent has a rule-based default path. The LLM only adds judgment where code cannot.
2. **Choose, don't invent.** The LLM picks from bounded options (low/medium/high, keep/drop, enum actions). Numbers come from code, tables and filings.
3. **One model resident at a time**, one LLM request at a time, 4-8k tokens of context at most.
4. **Cache every LLM response** so development and tests never pay for the same inference twice.
5. **Long runs are background jobs** with checkpoints and progress, not blocking chat turns.

## 2. Stack at a glance

| Layer | Choice | Alternative | License (verify) |
|---|---|---|---|
| Language / env | Python 3.12, `uv` | pip + venv | PSF / MIT-Apache |
| Orchestration | **LangGraph** + `langgraph-checkpoint-sqlite` (avoid the full `langchain` package) | Hand-rolled state machine | MIT |
| LLM runtime | **Ollama** (CPU) | `llama.cpp` `llama-server` (finer control over threads/KV cache) | MIT |
| LLM client | Thin in-house wrapper over `httpx` (queue, cache, retries, schema validation) | `openai` client against the local server | BSD / Apache-2.0 |
| Structured output | Ollama `format` JSON schema (or llama.cpp grammar) + **Pydantic v2** | `outlines` | MIT |
| Embeddings | **`fastembed`** (ONNX, no PyTorch) with `bge-small-en-v1.5` | `all-MiniLM-L6-v2` | Apache-2.0 / MIT |
| Text + vector search | **SQLite** FTS5 (BM25) + NumPy cosine similarity | `sqlite-vec` extension | Public domain / BSD |
| Reranker | Off by default | Tiny cross-encoder if retrieval evals demand it | n/a |
| Filings parsing | **edgartools** + `lxml` | BeautifulSoup | MIT |
| Analytics | pandas, NumPy, **DuckDB**, PyArrow/Parquet | Polars | BSD / MIT |
| Statistics | NumPy (OLS beta, standard error, R² by hand) | | BSD |
| Persistence | **One SQLite file** (LangGraph checkpoints, audit log, FTS5, vectors) | | Public domain |
| Cache | Files on disk (`diskcache` or plain JSON/Parquet) | | Apache-2.0 |
| Excel | **XlsxWriter** (write) + **openpyxl** (read/verify) | | BSD / MIT |
| Excel recalculation | **LibreOffice** headless (Calc only), run after the LLM is unloaded | `formulas` (pure Python) for a lighter, less complete check | MPL-2.0 / EUPL |
| Charts | matplotlib | | PSF-style |
| Memo | Markdown + HTML (PDF optional via the browser's print function) | `fpdf2` | n/a |
| News context | EDGAR earnings-release 8-Ks (no web search in v1) | `ddgs` / DuckDuckGo library later | n/a |
| API | **FastAPI** + Uvicorn (only when the UI needs it) | | MIT |
| UI | Phase 2-3: terminal chat (**Typer + Rich**). Phase 4+: **Chainlit** | Gradio, Streamlit | MIT / Apache-2.0 |
| Observability | JSONL traces + SQLite audit log + a small viewer script | Arize Phoenix (source-available), later | n/a |
| Testing | pytest, hypothesis, a mock-LLM fixture | | MIT / MPL |
| Quality | ruff, mypy, pre-commit | | MIT |
| Packaging | Plain processes and shell scripts | Docker later, optional | n/a |

## 3. What was removed from the heavy version

| Removed | Why | Replacement |
|---|---|---|
| vLLM, SGLang | Built for GPUs | Ollama / llama.cpp on CPU |
| LiteLLM | One local backend, so a router is dead weight | Thin wrapper |
| PostgreSQL | A server to install, run and feed RAM | SQLite |
| Chroma, Qdrant, pgvector | Extra services or dependencies for a corpus of a few thousand chunks | SQLite FTS5 + NumPy cosine |
| Redis | Not needed | Disk cache |
| Langfuse (self-hosted) | Recent versions need several services (databases, queue, object storage) | JSONL traces + SQLite audit log |
| SearXNG | Another container and more RAM | EDGAR 8-K press releases |
| Docker Compose | Overhead for a single-user local tool | Scripts; Docker optional later |
| statsmodels, scikit-learn, PyTorch | Heavy installs | NumPy; ONNX-based embedder |
| plotly, WeasyPrint | Extra dependencies and system libraries | matplotlib; Markdown + HTML |
| 14B, 30B and 70B models | Don't fit in 16 GB with context | 3-8B only |

## 4. Memory and disk budget

**RAM at peak (rough).**

| Item | RAM |
|---|---|
| OS, desktop, browser | 3-4 GB |
| Python app (LangGraph, pandas, DuckDB, SQLite) | about 1 GB |
| Embedding model (`bge-small`, ONNX) | 0.3-0.6 GB |
| LLM weights: S (3-4B, 4-bit) | about 2.5-3 GB |
| LLM weights: M (7-8B, 4-bit) | about 4.5-5.5 GB |
| KV cache (4-8k context) | 0.3-1.2 GB |
| LibreOffice headless (only after the LLM is unloaded) | 0.3-0.6 GB |
| **Typical peak (M model + app + embedder)** | **about 10-12 GB** |

Rules that keep you inside 16 GB: `OLLAMA_MAX_LOADED_MODELS=1`, `OLLAMA_NUM_PARALLEL=1`, context at 4096 (8192 hard cap), close heavy applications during full runs, and don't run LibreOffice while a model is loaded.

**Disk (rough).** Models 3-3.5 GB (S) + 5-6 GB (M); embedder under 0.2 GB; Python dependencies about 1-2 GB; LibreOffice about 1 GB; cached data about 50-150 MB per company (companyfacts JSON, a few filings, price history). A baseline of about 12-15 GB.

## 5. Open-weight LLMs on CPU

**Principle.** Models are selected by config, used through one wrapper, and chosen by **your own evals** (JSON validity, extraction accuracy, rubric agreement), not by leaderboard.

**Size classes (4-bit quantized, `Q4_K_M` as the default).**

| Class | Parameters | Used for |
|---|---|---|
| **S** | about 3-4B | Router, intent labels, WACC choice, comps keep/drop, Reporter narrative |
| **M** | about 7-8B | Filings extraction, normalization labels, Forecast axes and stories, Critic |
| (none) | 14B and larger | Not supported: weights plus context don't fit comfortably in 16 GB, and a 30B-class MoE or a ~20B model with 12+ GB of weights leaves no headroom |

**Candidate families (open weights; check each license).** Qwen3 (4B and 8B sizes), Phi-4-mini (about 3.8B), Gemma 3 (4B), Llama 3.2 (3B) and Llama 3.1 (8B), Mistral-7B-class or Ministral-8B-class models. Newer small releases may be better by the time you start, so re-check. For the Critic, pick a **different family** from the Forecast model (for example Forecast on a Qwen 8B and Critic on a Llama or Mistral 8B), loaded last so the swap happens once.

**CPU-specific tips.**
- **Turn off "thinking" modes** on models that have them (for example Qwen3's `/no_think`). Reasoning tokens cost minutes on CPU. Our CoT is a short structured `rationale` field instead.
- Set threads to your **physical core count**, not hyperthreads (`num_thread`).
- Keep `temperature` at 0-0.2 for extraction and selection; 0.5 only when you want varied wording.
- Limit `num_predict` per call (typically 60-250 tokens) so no call runs away.
- Prefer a **single M model for everything** if you want zero model swaps; it's simpler but slower for the cheap steps.

**Measured lighter-model throughput (2026-10-12, six physical CPU cores).**

| Class | Prompt processing | Generation |
|---|---|---|
| S (`qwen3:1.7b`, Q4_K_M) | 108.1 tokens/s | 11.9 tokens/s |
| S (`qwen3:4b`, Q4_K_M) | 42.6 tokens/s | 6.1 tokens/s |

These medians come from twenty short intent prompts per model, with 4096 context,
thinking off, CPU-only, and unloading after each call. They exclude loading time
and do not establish long-context throughput. See [benchmarks-light.md](benchmarks-light.md).
These lighter models replace the original S/M assignments in current configuration.
The historical 4B/8B baseline remains in [benchmarks.md](benchmarks.md).
The performance table in section 12 remains an unverified planning estimate;
the measured speeds are substantially slower than its original assumptions.

## 6. Serving configuration

```bash
# Environment for the Ollama server
OLLAMA_NUM_PARALLEL=1          # one request at a time
OLLAMA_MAX_LOADED_MODELS=1     # never keep two LLMs in RAM
OLLAMA_KEEP_ALIVE=2m           # unload soon after use so other steps get RAM
OLLAMA_FLASH_ATTENTION=1       # optional: lower KV-cache memory (verify support)
OLLAMA_KV_CACHE_TYPE=q8_0      # optional: halves KV-cache size (needs flash attention)
```

Per-call options (defaults in `config/models.yaml`):

```yaml
roles:
  router:    {model: <S tag>, num_ctx: 2048, temperature: 0.0, num_predict: 80}
  filings:   {model: <M tag>, num_ctx: 4096, temperature: 0.0, num_predict: 160}
  forecast:  {model: <M tag>, num_ctx: 4096, temperature: 0.3, num_predict: 300}
  critic:    {model: <M2 tag, different family>, num_ctx: 4096, temperature: 0.0, num_predict: 300}
runtime: {threads: <physical cores>, queue: single_worker, response_cache: true}
```

**Model tags change.** Look up current tags in the Ollama library when you set up.

## 7. Making small models reliable

1. **Schema-constrained decoding** for every output, with **flat schemas, enums and short fields**. Small models do much better on `{"level": "low|medium|high", "evidence": "..."}` than on nested objects.
2. **Constrained JSON actions instead of native tool calling** (this is how ReAct runs here): the model returns `{"action": <enum of allowed tools>, "args": {...}}`, code validates and executes it, and the loop stops after 4 steps. A rule-based default policy runs first.
3. **Narrow prompts, small contexts:** retrieve at most 4 passages of about 300-500 tokens; send tables as compact markdown; never paste a whole filing.
4. **Selection tasks over generation tasks:** "which of these 4 passages states total debt?" beats "find total debt".
5. **Ground first, then ask:** XBRL gives most numbers. The LLM sees filing text only for what XBRL lacks.
6. **Self-consistency only where it pays:** numeric fields with no XBRL ground truth, 2 runs at temperature 0 with differently worded prompts, a third only on disagreement.
7. **Escalation is cheap and local:** malformed output → one repair retry → switch that call to the M model → rule-based default → flag for the user.
8. **Prompt registry:** prompts live in versioned files with eval results next to them.
9. **Everything local:** no prompt data leaves the machine.

## 8. Retrieval stack (RAG without a vector server)

- **Chunking:** section-aware (Items 1, 1A, 7, 7A, 8 and the notes), 300-500 tokens, tables kept whole as markdown, with section and accession in metadata.
- **Storage:** one SQLite file. A `chunks` table with an FTS5 virtual table for BM25, plus a `vectors` table of float32 blobs (384 dimensions for `bge-small`).
- **Query:** BM25 top 20 and brute-force NumPy cosine top 20 (a few thousand vectors is instant), fused with reciprocal rank fusion, then top 4 returned with citations.
- **Indexing cost:** a 10-K is roughly 150-300 chunks; embedding it on CPU takes on the order of a minute or two. It's done once per accession and cached.
- **Index only what's needed in v1:** the latest 10-K and the 10-Qs filed after it, up to the valuation date.

## 9. Free data sources

| Source | Provides | Access | Limits / risks | Fallback |
|---|---|---|---|---|
| **SEC EDGAR** | Filing index, XBRL `companyfacts`, filings (10-K/10-Q/8-K), cover-page shares, earnings-release 8-K exhibits | Free, no key; **User-Agent with contact info required** | About 10 requests/sec; cache everything | Cached copy |
| **FRED** | 10Y Treasury (`DGS10`), other rates | Free API key | Generous limits | Treasury.gov yield-curve CSV |
| **Damodaran datasets** (NYU Stern) | Industry betas, margins, sales-to-capital, WACC, implied ERP, country risk, default-spread table, firm-to-industry mapping | Free Excel/CSV | Updated on a schedule; **snapshot each version** | Prior snapshot |
| **yfinance** (Yahoo) | Prices, splits, dividends | Free, unofficial | Can break; personal-use terms; use for prices only | **Stooq** CSV via `httpx`, then cache |
| **Stooq** | Daily historical prices | Free CSV | Coverage and format quirks | yfinance |
| Free-tier APIs (Alpha Vantage, Finnhub, FMP, Tiingo) | Prices, some fundamentals | Optional adapters | Tiny daily quotas | n/a |

**Light-machine data rules.**
- **No bulk datasets.** The SEC Financial Statement Data Sets are large. For comps, pull `companyfacts` for 8-10 peers (a few MB each, cached) instead.
- **No web search in v1.** Qualitative context comes from earnings-release 8-Ks, which are primary sources and respect the valuation date by construction.
- **Process with DuckDB/Parquet** (streaming, low RAM), not giant in-memory DataFrames.

**What isn't available for free, and the consequence.**

| Missing | Consequence in this design |
|---|---|
| Analyst consensus estimates | Forecasts come from history + guidance + Damodaran benchmarks + judgment at Gate 2 |
| True point-in-time fundamentals database | EDGAR `filed` dates give a good approximation |
| Corporate bond prices/yields | Cost of debt from synthetic rating, cross-checked with coupons in the debt schedule |
| Intraday/options data | Not needed for DCF |

**Adapter rules.** Each adapter returns typed results with provenance, caches raw responses, has a fallback chain, and has a recorded-response test so tests run offline.

## 10. Excel strategy

- **Formulas, not values.** The Inputs sheet drives every other sheet; key inputs are named ranges.
- **Recalculate and verify.** After the LLM is unloaded, run LibreOffice headless to recalculate, read cached values with `openpyxl(data_only=True)`, and compare to the engine (value per share, EV, WACC, TV). A mismatch fails the build. The pure-Python `formulas` library is a lighter fallback for CI.
- **Sensitivity grids.** Excel What-If Data Tables aren't portable across generators. Use explicit formulas per cell, or engine-computed values clearly labelled "static, computed by engine" beside a live base case.
- **Formatting.** Blue = inputs, black = formulas, green = cross-sheet links; units in headers; check cells show OK/ERROR.

## 11. Observability and audit

- **JSONL trace file per run:** every LLM call (role, model, prompt version, tokens in/out, latency, validation outcome) and every tool call (inputs hash, outputs hash, duration).
- **SQLite audit table:** every state mutation and every human decision at the gates.
- **`scripts/trace_view.py`:** a small terminal/HTML viewer to inspect a run, with per-agent time and token totals.
- **Provenance export:** the `Sources & Log` sheet is generated from the audit log.

## 12. Performance budget (estimates, to be replaced by Phase 0 benchmarks)

Built on the assumed throughput in section 5. Token counts are typical, per call.

| Step | Model | Calls | Tokens in / out | Time per call | Total |
|---|---|---|---|---|---|
| Router | S | 2 | 400 / 60 | about 8 s | about 15 s |
| Data (only on exceptions) | S | 0-3 | 600 / 80 | about 12 s | 0-40 s |
| Filings extraction | M | about 10 | 1,200 / 100 | about 50 s | about 8 min |
| Normalization labels | M | about 4 | 800 / 150 | about 50 s | about 3.5 min |
| WACC choice | S | 2 | 600 / 150 | about 18 s | about 35 s |
| Forecast (axes, rubric, 3 stories) | M | 5 | 800-1,500 / 100-250 | 50-90 s | about 5 min |
| Comps keep/drop | S | about 6 | 700 / 80 | about 13 s | about 80 s |
| Critic (per loop) | M | 2 | 2,000 / 250 | about 100 s | about 3.5 min |
| Reporter narrative | S | 4 | 800 / 250 | about 28 s | about 2 min |
| Model swaps | n/a | about 6 | n/a | 10-30 s | 1-3 min |
| Non-LLM: fetching, embedding, DCF, Excel, recalc | n/a | n/a | n/a | n/a | about 5-9 min |

**Full Excel model: roughly 30-45 minutes** (more with extra Critic loops or at Gate-driven re-runs). **Questions from cached state: about 10-30 seconds.** A first WACC question with no cache: about 2-3 minutes (data fetch plus one or two S calls). Gates add human waiting time, not compute time.

**How to cut time further.** Use the S model for more roles, reduce `num_predict`, reduce Filings extractions by trusting XBRL more, skip the Critic LLM rubric (keep the deterministic checks), or run a single M model to avoid swaps.

## 13. Setup (no Docker)

```bash
# 1. Python environment
uv init dcf-agent && cd dcf-agent
uv add langgraph langgraph-checkpoint-sqlite pydantic httpx pandas numpy duckdb pyarrow \
       xlsxwriter openpyxl edgartools yfinance fastembed matplotlib typer rich
uv add --dev pytest hypothesis ruff mypy pre-commit

# 2. LLM runtime (installers for Linux/macOS/Windows on the Ollama site)
curl -fsSL https://ollama.com/install.sh | sh
ollama pull <S model tag>      # about 3-4B, 4-bit
ollama pull <M model tag>      # about 7-8B, 4-bit

# 3. LibreOffice (Calc only is enough) for headless recalculation
sudo apt install libreoffice-calc      # or your OS installer

# 4. Config and benchmark
export SEC_USER_AGENT="Your Name your@email"
export FRED_API_KEY=...
python scripts/benchmark_llm.py        # records tokens/s and peak RAM into docs/benchmarks.md
```

## 14. Upgrade path

Nothing in the architecture assumes a GPU. If you later get more RAM or a GPU, change `config/models.yaml` to larger models, raise the context limits, turn on the optional reranker and debate mode, and keep everything else. The deterministic core and the human gates stay the same.
