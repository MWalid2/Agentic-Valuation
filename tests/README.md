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

`test_sec.py` checks SEC transport, caching, cutoffs, archive retrieval, and retries.
`test_edgar_agent.py` checks recorded filing extraction, tool execution,
invented/omitted citations, bounded actions, and XBRL disagreements. Both run
offline; see `fixtures/sec/README.md` for recorded public fixture sources.

`test_external_data.py` checks recorded Damodaran/FRED/Yahoo data, date eligibility,
units, secret-free caching, corporate actions, and source fallbacks.
`test_data_agent.py` checks that all bounded tools participate, failures retain
partial results, and the market-cap bridge uses compatible share/price bases.
These tests run offline; sources are described in `fixtures/market/README.md`.

`test_raw_financials.py` covers cited statement enrichment, exact-accession
standard-disclosure fallback, and direct inline-XBRL history fallback.
`test_history.py` uses hand-computed fiscal periods to verify quarter differencing,
TTM flow/stock treatment, 52/53-week calendars, cutoff handling, restatements,
conflicting aliases, missing quarters, and stale-result suppression.
