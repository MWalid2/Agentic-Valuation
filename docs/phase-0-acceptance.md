# Phase 0 Acceptance — 2026-10-12

Phase 0 foundations are ready for Phase 1 under the approved local-only workflow
and lighter-model plan. GitHub Actions was removed at the user's request.

## Local checks

- `uv sync --locked --dev`: passed; lockfile and dependencies are consistent.
- Ruff lint and formatting: passed.
- Strict mypy: passed, 23 source files.
- Pre-commit configuration validation: passed.
- Offline pytest: 212 tests passed.

Tests cover contracts and configuration, schema validation and one repair,
flagged fallbacks, cache separation and revalidation, single-worker concurrency,
HTTP failures, and benchmark accounting. No credentials or live models are
needed for these tests.

## Actual wrapper acceptance

`scripts/check_wrapper.py` ran 20 prompts per model through `LocalLLM`, with
caching disabled, no personal `.env`, no fallback, and actual configured role
limits. Calls requested CPU inference and unloading after each response.

| Configured role | Model | First-attempt valid | Final valid | Correct intent | Median seconds |
|---|---|---|---|---|---|
| Router | `qwen3:1.7b` | 20/20 | 20/20 | 20/20 | 8.07 |
| Filings | `qwen3:4b` | 20/20 | 20/20 | 20/20 | 15.99 |

Both exceed the 95% first-attempt validity requirement and meet 100% final
validity. No live repair was necessary; repair behavior is covered by offline
tests. The final `/api/ps` check confirmed no models remained loaded.
Raw case records are in ignored `data/wrapper-check/results.json`.

## Memory evidence and limits

The wrapper run sampled 3.06 GiB peak Ollama/llama process RSS for 4B and
12.57 GiB peak total system usage. The 1.7B RSS reading of 0.09 GiB is incomplete
and must not be treated as model memory; its runner was not captured reliably.
Its peak system usage was 11.29 GiB.

The earlier [resident-inspected lighter benchmark](benchmarks-light.md) measured
1.93 GiB process RSS for 1.7B and 3.13 GiB for 4B, supporting the chosen models'
8 GiB model-process budget. RSS is sampled and approximate; total desktop memory
is higher and includes other applications. No claim is made that the entire
desktop stays below 8 GiB. The script's automatic pass/fail covers structured
output; memory evidence requires review of the report.

This is the same small development intent set used for benchmarking, not an
unseen evaluation or a financial extraction test. Financial-role assignments
remain provisional. Full application memory, grounded role evaluations, and
end-to-end valuation performance remain future checks. Ollama's server-wide
environment limits have not been reconfigured; these tests used sequential calls
and immediate unloading through the wrapper.

## Next review point

Phase 1 begins with the SEC EDGAR adapter: caching, rate limiting, SEC identity,
filing-date filters, and sourced financial data. No Phase 1 code was added here.
