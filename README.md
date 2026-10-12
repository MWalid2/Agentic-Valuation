# Agentic Valuation

A local DCF valuation assistant. See [the documentation](docs/README.md) for the
architecture, agents, stack, and roadmap. Phase 0 is in progress; valuation
functionality has not been implemented yet.

## Development

Use Python 3.13 and uv, from the repository root:

```powershell
uv sync --frozen
uv run python -c "import dcf_agent; print(dcf_agent.__file__)"
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pre-commit install
```

Run contract validation tests with `uv run pytest`; no network or Ollama is needed.

## Package structure

| Directory under `src/dcf_agent/` | Responsibility |
|---|---|
| `contracts` | Validated data structures and provenance |
| `engine` | Deterministic valuation calculations |
| `data` | Data adapters and caching |
| `rag` | Filing indexing and retrieval |
| `agents` | Specialized agents and deterministic fallbacks |
| `tools` | Fixed tool interfaces exposed to agents |
| `graph` | Workflow, approval gates, and incremental reruns |
| `excel` | Workbook generation and engine parity |
| `llm` | Local model access, request queue, and response cache |
| `api` | Future API and UI integration |

Evidence wrappers, request and input contracts, filing insights, adjustments,
normalized financials, data quality reports, forecasts, WACC, valuation results,
comparable-company results, and workflow/state contracts are implemented; other packages
currently contain descriptive docstrings. `__init__.py`
identifies a directory as a Python package. `py.typed` marks the package as
providing inline type annotations for downstream type checkers.

Hatchling builds the package; uv manages dependencies and installs it in editable
mode for development. Source edits are available without reinstalling it.
The distribution name is `agentic-valuation`; its Python import name is
`dcf_agent`, matching the architecture document.

The root `main.py` is still the uv starter script, not a valuation CLI.
