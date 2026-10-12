# 07. Application Configuration

Configuration is implemented in `src/dcf_agent/config.py`. Loading it validates
settings and reads a YAML file; it does not contact data providers, start Ollama,
or create storage directories. The [local LLM wrapper](08-local-llm-wrapper.md)
now implements serialized requests, repair, caching, and offline fixtures.

## Files and responsibilities

| File | Purpose |
|---|---|
| `.env` | Local settings and credentials; ignored by Git; existing contents are preserved |
| `.env.example` | Documented setting names without credentials; merge into `.env` as needed |
| `config/models.yaml` | Versioned role assignments, context/output limits, and runtime policy |
| `src/dcf_agent/config.py` | Pydantic settings, model-role validation, and configuration loader |

## Settings

| Environment variable | Default / meaning |
|---|---|
| `SEC_USER_AGENT` | Optional until EDGAR is used; supply your name and contact email |
| `FRED_API_KEY` | Optional until the FRED API is used |
| `DCF_MOCK_LLM` | `true`; enables offline responses through explicit wrapper fixtures |
| `DCF_OLLAMA_URL` | `http://127.0.0.1:11434`; only loopback HTTP(S) origins are accepted |
| `DCF_DATA_DIR` | `data`; raw cache, LLM response cache, and the SQLite file |
| `DCF_OUTPUT_DIR` | `outputs`; generated workbooks, memos, and other exports |
| `DCF_MODELS_FILE` | `config/models.yaml`; model-role configuration |

Environment variables override `.env`, which overrides defaults. Empty environment
values are ignored. Unknown `.env` entries are ignored so the existing file can
contain settings for future components. Credentials are `SecretStr` values:
ordinary repr/JSON output masks them. Code must deliberately unwrap a secret to
use it. This is masking, not encryption, and does not make arbitrary logging safe.

Relative paths are resolved against the root passed to `load_config`, which
defaults to the current working directory. Absolute configured paths are permitted;
write permissions and safe artifact handling remain separate responsibilities.
The SQLite path is `data/valuation.sqlite3`, raw cache is `data/raw`, and LLM cache
is `data/llm_cache` under the configured data directory. `data/` and `outputs/`
are ignored by Git; external custom directories need their own Git handling.

```python
from pathlib import Path

from dcf_agent.config import load_config

config = load_config(Path.cwd())
router_options = config.models.roles["router"]
database_path = config.settings.database_path
```

Use `load_config(root, load_env_file=False)` for isolated offline tools. No folders
are created until a later component needs to write to them.

## Models and CPU limits

The YAML defines all nine roles. Each has an S or M size class, optional model tag,
context limit, output limit, temperature, and disabled thinking policy. Context
is capped at 8192 tokens; typical defaults are 2048 or 4096. Output is capped at
512 tokens. Unsupported role names and extra YAML fields are rejected.

Tags now use only the lighter candidates in [the benchmark report](benchmarks-light.md).
Router, Data, WACC, and Comps use `qwen3:1.7b`; Filings, Normalizer, Forecast,
Critic, and Reporter use `qwen3:4b`. All are declared S models. Role complexity
does not require an M size class. These financial-role assignments are provisional;
the intent benchmark does not validate financial reasoning.
Turning mock mode off
requires every role to have a tag. The size class is a declared assignment, not
an inspection of downloaded weights; the benchmark/runtime must verify actual
model size, quantization, license, performance, and memory use. Using a different
family for Critic remains a model-selection responsibility.

Runtime policy requires one worker, one loaded model, and one parallel request.
Threads are set to the six measured physical cores. Response caching defaults on,
and the HTTP timeout defaults to 180 seconds. The wrapper implements serial
requests, caching, HTTP timeouts, and the requested thinking policy; configuration
alone does not enforce server behavior.

Separately configure the Ollama server environment when it is launched:

```text
OLLAMA_MAX_LOADED_MODELS=1
OLLAMA_NUM_PARALLEL=1
```

Loading our Python `.env` does not change the environment of an existing Ollama
process. Model unloading before Excel recalculation will be implemented later.

## Validation

Run `uv run pytest tests/test_config.py`. Tests use temporary files and isolated
environment variables; they never read the user's `.env`. They check setting
precedence, path resolution, masked secrets, offline defaults, missing live tags,
required roles, local URLs, and CPU policy limits.

The loader uses `yaml.safe_load` to avoid constructing arbitrary Python objects.
Missing files and invalid configuration raise errors instead of silently guessing.
The SEC identity presence check runs when requested; complete contact-format and
provider compliance validation belongs to the upcoming adapter. FRED credentials
are not required to import the package or run offline tests.
