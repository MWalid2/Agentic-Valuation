# 05. Development Tools and Their Roles

This guide explains the tools installed during Phase 0, how they support the
valuation project, and how to use the current configuration. Installed packages
do not yet mean their planned application functionality has been implemented.

## 1. Environment and dependency management

**Python** runs the application. The repository currently uses Python 3.13,
although the original stack document proposes 3.12. `.python-version` selects
3.13 for uv; `pyproject.toml` declares Python 3.13 or newer.

**uv** manages the virtual environment and dependencies:

```powershell
uv sync --frozen         # Install the versions recorded in uv.lock
uv add package-name     # Add an application dependency
uv add --dev package-name # Add a development dependency
uv run mypy             # Run a tool in the project environment
```

- `pyproject.toml` records direct dependencies and tool settings.
- `uv.lock` records resolved versions, including indirect dependencies, for
  reproducible installation. Commit it alongside `pyproject.toml`.
- `.venv/` contains the local Python environment and is ignored by Git.
- `--frozen` prevents uv from updating the lockfile during the command.

## 2. Code quality: Ruff

Ruff provides linting and formatting. Linting detects issues such as unused
imports and undefined variables; formatting keeps the code layout consistent.
These checks help keep the data adapters, agents, and financial engine readable.

Configuration in `pyproject.toml`:

```toml
[tool.ruff]
target-version = "py313"
line-length = 100
include = ["*.py", "*.pyi", "*.ipynb"]
extend-exclude = [".uv-cache"]

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]
```

| Setting or rule | Meaning |
|---|---|
| `target-version` | Use Python 3.13 when deciding which syntax is supported |
| `line-length` | Prefer a line length of 100 characters; not every long line is rejected |
| `include` | Check Python source, type stub files, and notebooks |
| `extend-exclude` | Skip the local uv cache |
| `E` | Selected pycodestyle error checks |
| `F` | Pyflakes checks, including unused imports and undefined names |
| `I` | Import ordering |
| `UP` | Modern Python syntax suggestions |
| `B` | Common bug patterns from flake8-bugbear |

```powershell
uv run ruff check .          # Report lint issues
uv run ruff format --check . # Check formatting without editing files
uv run ruff format .         # Apply formatting
```

Markdown design documents are outside the configured file types. Ruff does not
verify that financial formulas are correct; that requires tests.

## 3. Type checking: mypy

Mypy checks type annotations without running the application. For example, it
can detect a string passed to a function that expects a numeric tax rate.
This supports reliable handoffs between financial data, assumptions, and results.

```python
def calculate_nopat(ebit: float, tax_rate: float) -> float:
    return ebit * (1 - tax_rate)

calculate_nopat(100.0, "25%")  # Mypy flags the string argument
```

Current configuration:

```toml
[tool.mypy]
python_version = "3.13"
strict = true
files = ["main.py", "src/dcf_agent", "scripts"]
exclude = ['^\.venv/', '^\.uv-cache/']
```

Strict mode requires annotations and enables stronger checks. The checks cover
`main.py`, the application package under `src/dcf_agent/`, and developer scripts.

The starter function now declares `def main() -> None`, meaning it returns no
value. Run the checks with `uv run mypy`.

Mypy cannot prove a valuation is financially correct. It checks type consistency.

## 4. Behavior testing: pytest and Hypothesis

**pytest** discovers and runs tests. Planned tests will check data contracts,
point-in-time filtering, financial calculations, and Excel parity.

```toml
[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "--strict-config --strict-markers -ra"
```

| Setting | Meaning |
|---|---|
| `testpaths` | Search `tests/` when no test path is supplied |
| `--strict-config` | Treat configuration warnings as errors |
| `--strict-markers` | Reject unregistered test markers |
| `-ra` | Summarize test outcomes other than passes |

Run tests with `uv run pytest`. Contract validation tests now exist under
`tests/contracts/`. Normally pytest uses exit code 5 if no tests are collected;
that outcome is not a passing test suite.

**Hypothesis** generates test inputs to check properties across many cases.
For example, with positive cash flows and fixed assumptions, increasing the
discount rate should reduce their present value. These tests complement
hand-calculated examples. Hypothesis is installed but has no project tests yet.

## 5. Automatic commit checks: pre-commit

Pre-commit runs configured checks before Git creates a commit. Its configuration
is in `.pre-commit-config.yaml`:

| Hook | Command | Scope |
|---|---|---|
| Ruff lint | `uv run --frozen ruff check` | Python files selected for the commit |
| Ruff formatting | `uv run --frozen ruff format --check` | Python files selected for the commit |
| Mypy | `uv run --frozen mypy` | Paths configured in `pyproject.toml` |

These are local hooks using the tools installed in our uv environment.
`language: system` means pre-commit does not create a separate environment for
each hook. Mypy uses `pass_filenames: false` so it checks its configured paths
rather than receiving a list of staged files.

The hook has been installed in this checkout at `.git/hooks/pre-commit`.
Each new checkout needs its own installation:

```powershell
uv run pre-commit install
uv run pre-commit validate-config
uv run pre-commit run --all-files
```

If a hook fails, the commit stops. Fix the issue, stage any corrected files, and
commit again. Formatting hooks currently report issues without fixing them.
Pytest is not included in the hooks. Run [local checks](11-local-checks.md)
for tests, lint, formatting, and types. `--all-files` operates on Git-tracked files, so newly
created files need to be added to Git before that command includes them.

## 6. Application libraries installed for Phase 0

| Library | Planned role in this project |
|---|---|
| `pydantic` | Validate data contracts such as provenance, financial inputs, and assumptions at runtime |
| `pydantic-settings` | Load and validate application settings from environment variables and, when configured, `.env` |
| `httpx` | Make HTTP requests to local Ollama and public data adapters |
| `pyyaml` | Read YAML configuration for model roles and assumptions |
| `langgraph` | Coordinate agent execution, routing, state, and human approval gates |
| `langgraph-checkpoint-sqlite` | Persist graph checkpoints in SQLite for interrupt and resume |

Pydantic and mypy serve different purposes: Pydantic validates actual data while
the application runs; mypy checks annotated code during development.

LangGraph installs supporting packages as indirect dependencies. Installing
them does not configure cloud tracing or implement our agent graph.

## 7. Supporting development packages

| Package | Role |
|---|---|
| `types-PyYAML` | Supply type information that helps mypy check PyYAML usage |
| `types-psutil` | Supply type information for memory and CPU measurements |
| `psutil` | Measure process memory and inspect hardware during planned local LLM benchmarks |

These are in the development dependency group. `scripts/benchmark_llm.py`
measures local model speed and memory; `scripts/smoke_llm.py` exercises live
wrapper calls. See the model setup guide and benchmark report.

## 8. Generated files and current verification

`.gitignore` excludes `.venv/`, `.uv-cache/`, `.pytest_cache/`, `.mypy_cache/`,
`.ruff_cache/`, and `.pre-commit-cache/`. These contain local environments or
generated tool data. `.env` is also ignored to keep local secrets out of Git.

At setup time, dependency imports, Ruff linting, Ruff formatting, mypy, and
pre-commit configuration validation passed. The Git hook was installed.
Contract validation tests have since been added. Checks run locally; no GitHub
Actions workflow is required or configured.

The application package structure now exists under `src/dcf_agent/`. See the
repository README for its layout and implemented contracts.
