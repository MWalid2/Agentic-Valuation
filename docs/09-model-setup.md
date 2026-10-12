# 09. Local Model Setup and Benchmarking

This machine has six physical CPU cores and approximately 15.3 GiB RAM. Ollama
was already running (API version 0.40.3). The installed `qwen3:4b` was retained,
and `qwen3:8b` was downloaded. Both are Q4_K_M; their local license text identifies
Apache-2.0. See [the license record](../LICENSES.md).

## Provisional role assignments

Following the lighter-model benchmark, `config/models.yaml` uses `qwen3:1.7b`
for Router, Data, WACC, and Comps, and `qwen3:4b` for Filings, Normalizer,
Forecast, Critic, and Reporter. Both are S models, with six CPU threads.
The previously downloaded 8B model remains on disk but has no assigned role.
Thinking is disabled. The wrapper explicitly requests CPU-only
execution, serializes calls, and requests unloading after every response.

These are startup candidates, not validated production agents. The benchmark's
original short intent test exposed semantic errors despite valid JSON; the
revised explicit-label test scored 20/20 for both lighter models. Forecast and
filing extraction require their own grounded evaluation. Critic currently shares
the Qwen family with Forecast; a different-family comparison remains desirable.
No additional Critic model was downloaded in this step.

## Reproduce the benchmark

Run from the repository root, with Ollama running:

```powershell
uv run python scripts/benchmark_llm.py qwen3:1.7b qwen3:4b
```

The original baseline used undefined action labels. The current script uses
explicit `intent-v2` definitions documented in [10-intent-benchmark.md](10-intent-benchmark.md)
and writes `docs/benchmarks-light.md`, preserving the original report.
The script runs candidates sequentially, unloads existing residents first, and
requests 4096 context, up to 160 output tokens, temperature zero, thinking off,
and zero GPU layers. It performs 20 intent questions per model, validates a small
JSON schema, and retries once on schema failure. Semantic correctness is counted
separately and does not itself trigger a repair. It bypasses the response cache
and user's `.env` to measure real inference.

The script uses the local API directly so it can read Ollama's token-duration
metrics. Offline wrapper behavior has separate tests. Revised raw case records
and summaries go to `data/benchmark-runs/intent-v2/data/benchmarks/`; the report is
[docs/benchmarks-light.md](benchmarks-light.md). Historical files remain under
`data/benchmarks/`. Existing same-suite model summaries are preserved when
benchmarking another candidate. Rerunning a model replaces its previous summary.

`--count` supports smaller diagnostic runs, but only the full 20-case run can
support the Phase 0 structured-output sample criterion. A small diagnostic run
must not be described as a complete model evaluation. The separate one-call
4B memory probe was diagnostic and did not replace the 20-case result.

Median throughput uses API token counts divided by evaluation durations and
excludes load time. Median call latency includes model loading and any repair.
These short prompts do not measure long-context speed, realistic financial
extraction, or end-to-end valuation time. The original 30–45 minute full-run
estimate remains unverified.

## Memory measurements

The script samples accessible Ollama/llama process RSS and total system memory.
RSS may omit inaccessible processes or double-count shared pages; it must not
be interpreted as exact model memory. The first 4B run's process sampler did not
include the separately named llama runner, making its RSS result incomplete.
Ollama `/api/ps` resident-size estimates were inspected separately, confirming
CPU-only inference and 4096 context. Such estimates are not measured peak RSS.

The 8B corrected sampler observed 5.65 GiB process RSS; full desktop memory
peaked at 14.90 GiB. The original 4B RSS measurement was incomplete even after a
diagnostic follow-up. The subsequent [lighter-model benchmark](benchmarks-light.md)
observed 3.13 GiB 4B process RSS with resident inspection before unloading.
The existing desktop workload leaves limited free RAM during 8B inference.
Benchmark readings cover this workload, not a quiet machine with only the app.
Full application peak memory, retrieval, and workbook recalculation need further
measurement. Only one model should run, and LibreOffice must run after unloading.

## Using live mode

The existing `.env` is preserved. Mock mode remains the application's default.
The provided live integration smoke script runs one Router and one Filings call without
reading `.env`, bypassing the mock default:

```powershell
uv run python scripts/smoke_llm.py
```

Its validated responses can be cached. Remove those entries or change the prompt
version to repeat live inference. It raises if the simple instruction is not
followed. To use live mode in other scripts without editing `.env`, use a
process-local setting:

```powershell
$env:DCF_MOCK_LLM = "false"
uv run python your_live_script.py
Remove-Item Env:DCF_MOCK_LLM
```

`your_live_script.py` is an example placeholder, not an implemented CLI.
Its code should call `load_config()` and use `LocalLLM` with a small Pydantic
response class. There is no valuation CLI yet.

When starting the Ollama server, separately set `OLLAMA_MAX_LOADED_MODELS=1`
and `OLLAMA_NUM_PARALLEL=1`. Reading `.env` in Python cannot change an existing
server's environment. This session did not restart or replace the user's server.
The benchmark itself kept inference sequential and unloaded between calls.

## Remaining Phase 0 work

Model tags are configured, but structured-output validity does not demonstrate
financial judgment. Further checks need representative role prompts, grounded
filing snippets, model memory with the full app, and prompt/context budgets.
Checks run locally; see [11-local-checks.md](11-local-checks.md).
See the measured report for acceptance evidence and caveats.
