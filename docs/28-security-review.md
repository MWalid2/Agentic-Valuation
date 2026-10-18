# 28. Security review

Written on 2026-10-17 (Phase 6, step 7). The system runs locally for one user. The review covers:

- what enters it: your messages, downloaded filings and data, model output;
- what it writes: caches, sessions, reports, traces;
- its secrets.

Each item gives the finding and where it is enforced.

## Summary

| Area | Finding | Status |
|---|---|---|
| Code execution | No `eval`, `exec`, `pickle`, `os.system`, `shell=True` or unsafe YAML anywhere in `src/` or `scripts/`. The one subprocess is LibreOffice for parity, called with an argument list | OK |
| Model output | Model text is only parsed into pydantic schemas (`extra="forbid"`, bounded fields); invalid output is repaired once, then a rule-based fallback is used. No model text is executed, used as a path or put into a formula | OK |
| Numbers from the model | "Choose, don't invent": the model picks levels or verdicts and code builds the numbers. The provenance audit fails any value resting on a model estimate (`engine/audit.py`); a run that exports one is a "wrong number" in the batch runner | OK |
| Downloaded documents | XML parsed with `resolve_entities=False, no_network=True` (`data/xbrl.py`, `rag/filings.py`); HTML with `no_network=True` (made explicit in `data/damodaran.py` and `rag/releases.py` in this review). Filing text is data: it reaches the model only inside a prompt, and its answers are schema-checked | OK (hardened) |
| Checkpoints | Sessions are restored through `ContractSerializer`: JSON with an allowlist of project modules, validated again on load (no pickle) | OK |
| Inputs | Tickers are validated by pattern (`^[A-Z0-9][A-Z0-9.\-]*$`) before use; dates are parsed; gate answers are validated by schemas (`GateDecision`, `AssumptionDecision`, `SignoffDecision`) and refused if they don't parse | OK |
| File paths | Output paths are built from validated tickers and dates only (`outputs/models/<ticker>-<date>.xlsx`, `outputs/reports/<ticker>-<date>/`, `outputs/traces/<session>.html`); no path comes from model text or a filing | OK |
| Escaping | Free text is escaped in the chat (`rich.markup.escape`), in the memo and trace HTML (`html.escape`, no scripts), and in the SVG chart; Markdown table cells have `|` replaced | OK |
| Secrets | `SEC_USER_AGENT` and `FRED_API_KEY` come from `.env` as `SecretStr`; the FRED cache key excludes the API key; HTTP errors are re-raised without the request URL (`data/cache.py`), so the key never reaches warnings, the audit log or the traces. A search of `outputs/` and `data/` finds no key | OK |
| SEC fair access | Every SEC request carries the User-Agent from `.env` (identity and email, checked at start); requests are spaced by the client's rate limiter, 429 and 5xx responses are retried with backoff, responses are cached (immutable filings forever) | OK |
| Network | Only the data sources' hosts are called (sec.gov, data.sec.gov, api.stlouisfed.org, pages.stern.nyu.edu, web.archive.org for Damodaran history, Yahoo Finance and Stooq for prices) and the local Ollama; clients use timeouts and `trust_env=False` | OK |
| Dependencies | MIT, BSD-3-Clause or Apache-2.0: langgraph, pydantic, httpx, lxml, openpyxl, rich, typer, pyyaml, fastembed, yfinance, numpy, pandas. Versions pinned in `uv.lock`; the checks run with `--frozen` | OK |

## Residual risks

- **Prompt injection through filings.** A filing could contain text meant to steer the model. Its effect is bounded:
  - the model only chooses among levels or verdicts;
  - support rules reject choices the statistics don't back;
  - the Critic and the audit check the result.
  A manipulated choice could still move a scenario within its bounds, which is why Gates 1 and 2 show the evidence.
- **Yahoo Finance and Stooq** are unofficial price sources. Prices are cross-checked where possible and dated, but they are not filings.
- **Autopilot** approves gates without a person; everything it approves is listed in the report as unreviewed.
- **Local files** (caches, sessions, reports) are not encrypted. They hold public data and your questions only, never your secrets.
