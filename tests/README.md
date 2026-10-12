Run tests with `uv run pytest`.

`contracts/test_provenance.py` checks evidence requirements, cutoff dates,
strict input validation, immutable fields, generic wrappers, and JSON round trips.
These tests run offline without Ollama. Engine tests will follow with the engine.

`contracts/test_inputs.py` checks requests, financial periods and partial history,
market observations, macro publication dates, cutoff eligibility, and JSON round trips.

`contracts/test_accounting.py` checks filing evidence, dilution terms, adjustment
review rules, normalized history, and explicit data quality failures.

`contracts/test_modeling.py` checks scenario forecasts, WACC, engine-result
boundaries, comparable-company samples, and JSON round trips.

`contracts/test_workflow.py` checks critique reports, approval records, audit
events, budgets, and shared-state validation and restoration.
