# 11. Local Development Checks

This project is developed locally. There is no GitHub Actions workflow or hosted
CI requirement. Run these checks from the repository root before moving to a new
phase or accepting substantial changes:

```powershell
uv sync --locked --dev
uv run --no-sync pre-commit validate-config
uv run --no-sync ruff check .
uv run --no-sync ruff format --check .
uv run --no-sync mypy
uv run --no-sync pytest -q
```

Locked dependency installation checks consistency with `pyproject.toml`.
Ruff checks code quality and formatting; mypy checks types; pytest exercises
contracts, configuration, the LLM wrapper, and benchmark accounting. Tests use
fixtures and mocked HTTP responses, without Ollama or personal credentials.
Installing dependencies can require network access. Commit hooks provide an
additional local check but do not run pytest.

## Live wrapper acceptance

With local Ollama running, run:

```powershell
uv run --no-sync python scripts/check_wrapper.py
```

This runs all 20 explicit intent prompts through the actual `LocalLLM` wrapper
for Router (1.7B) and Filings (4B), using each role's configured context and output
limits. Response caching is disabled so every case measures live inference.
No personal `.env` is read. Calls are sequential and request immediate unloading.
The report records first-attempt validity, final validity, semantic correctness,
latency, and sampled memory. Acceptance requires at least 19/20 first-attempt
valid responses and 20/20 valid responses after the permitted repair, per model.

These are structured-output checks, not an evaluation of filing extraction or
financial judgment. Sampled process RSS is approximate; total system memory
includes other desktop applications. Full application memory remains a later check.
See `docs/phase-0-acceptance.md` for the latest measured outcome.
