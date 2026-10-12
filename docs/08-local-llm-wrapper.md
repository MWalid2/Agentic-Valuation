# 08. Local LLM Wrapper

`LocalLLM` in `src/dcf_agent/llm/client.py` implements serialized requests,
Pydantic output validation, one repair retry, a disk response cache, explicit
fallbacks, and offline fixtures. Model selection and live benchmarking remain.

## Request flow

1. Submit a role, prompt, response Pydantic class, and prompt version.
2. One application-owned worker processes submissions serially.
3. Hash the request, schema, version, mode, model/options, and server origin.
4. Revalidate a cached response, or request a new response.
5. Validate generated JSON; on failure send one repair request.
6. Atomically cache valid output. On exhausted validation or transport failure,
   return a supplied validated default with a warning, or raise `LLMError`.

Repair diagnostics exclude raw invalid input values, although the original
response is included in the local repair context. Transport failure is not
retried as a JSON repair. Defaults are not cached as model output. Automatic
escalation to a different model remains an agent-level policy to implement.

## Offline example

```python
from typing import Literal
from pydantic import BaseModel, ConfigDict
from dcf_agent.config import load_config
from dcf_agent.llm import LocalLLM

class Choice(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    level: Literal["low", "medium", "high"]

config = load_config(load_env_file=False)
with LocalLLM(config, mock_responses=['{"level":"medium"}']) as llm:
    result = llm.generate(
        "forecast", "Select growth durability.", Choice,
        prompt_version="growth-v1", fallback=lambda: Choice(level="low"),
    )
    print(result.value.level, result.origin)
```

Mock mode consumes explicit fixtures; it does not invent responses from schemas.
Exhausted fixtures fail. Cache replay may consume no fixture; use temporary
storage or disable caching when testing fixture changes.

`LLMResult` exposes value, origin (`live`, `mock`, `cache`, `fallback`), attempt
count, repair flag, warning, cache key, and total backend-reported token counts.
Cache hits report zero inference attempts/tokens and do not restore original
repair or usage metadata.

## Local inference and limits

The HTTPX client disables environment proxies and redirects. Requests use
`stream=false`, `think=false`, JSON Schema, role options, and `keep_alive=0`.
Requests also set `num_gpu=0` for the CPU-only target.
The [Ollama chat API](https://docs.ollama.com/api/chat) documents these controls.
Immediate unloading favors free RAM but increases repeated-call load costs;
benchmarking will inform a later residency policy.

Use one wrapper per application; this is not a global lock across instances or
processes. Configure Ollama's one-model/one-request server limits separately.
Mock mode cannot select the HTTP backend. Live mode requires configured tags.

`generate` blocks its caller; invoke it from background jobs or an async thread
bridge. Closing drains queued work and closes the backend. Queue cancellation,
size limits, and end-to-end job deadlines are not implemented. HTTPX timeout
bounds network operations, not total queue wait plus inference. A lost connection
does not prove server inference stopped; server limits remain necessary.

Context/output caps are passed to Ollama. No model-specific tokenizer preflights
prompt length. Keep evidence and repair contexts small. Live setup must verify
model support for thinking controls, memory use, and output-budget adequacy.

## Storage and traceability

Validated responses go to `data/llm_cache/mock/` or `data/llm_cache/live/` as
SHA-256-named JSON. Writes use temporary files and atomic replacement; reads
revalidate and regenerate damaged/incompatible entries. Storage errors surface.
Fallbacks are not cached. Schema, parameters, and prompt version affect the key.
Updating weights under the same tag requires clearing cache or changing version.

Responses can contain sensitive local rationale; cache files are not encrypted.
The response cache stores no prompts or credentials. Call metadata goes to
`data/traces/llm.jsonl`: role, request hash, attempt, latency, token counts, and
validation outcome, without prompt or response text. Cache hits and fallback
selection have no dedicated trace event yet. Full per-run audit integration will
follow with orchestration.

## Verification

`uv run pytest tests/test_llm.py` covers output validation, repair, fallbacks,
cache replay and invalidation, damaged cache, HTTP failures, timeouts,
single-worker execution, trace contents, and shutdown. Tests use fixtures and
`httpx.MockTransport`, not a real server or user `.env`.

Offline correctness does not demonstrate model quality. The 20-prompt live
baseline, throughput measurements, provisional tags, and live wrapper smoke
checks are recorded in [benchmarks.md](benchmarks.md). The revised
[lighter-model benchmark](benchmarks-light.md) includes a 3.13 GiB sampled 4B
process peak. Full-app memory and grounded role-specific quality evaluation
remain open.
