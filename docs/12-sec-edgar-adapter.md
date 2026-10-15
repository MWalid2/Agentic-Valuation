# 12. EDGAR Document-Reading Agent

The interpretation flow was rebuilt on 2026-10-13. The former period builder,
custom-label remapping, issuer-specific debt recipes, and smoke script were
removed. The tested SEC transport remains for retrieval, caching, rate limiting,
and filing-date eligibility. The first agent task is a cited debt breakdown;
full `RawFinancials` construction remains future work. The follow-on
[single-period snapshot agent](14-financial-snapshot.md) expands the evidence
tools to eleven financial fields, with separate snapshot validation and tests.

## Components

| Component | Responsibility |
|---|---|
| `data/sec.py` | SEC contact header, validated URLs, snapshots, retries, dated filings and XBRL facts |
| `tools/edgar.py` | Read balance-sheet/debt-note tables and reconcile evidence with XBRL |
| `agents/edgar.py` | Local model chooses tools and selects cited debt balances |
| `llm/client.py` | Existing CPU-only 4B Filings role, structured output, repair, queue, cache |
| `scripts/check_edgar_agent.py` | Uncached live-model evaluation against reference debt totals |

`lxml` parses filing HTML; `lxml-stubs` supports strict type checking. No paid or
hosted model is used. The existing `.env` is preserved.

## Agent flow

The host lists filings eligible at the cutoff and selects the latest eligible
10-K/10-Q for the requested reporting date. The model requests
`read_balance_sheet` or `read_debt_note`, then answers with evidence IDs or reports
uncertainty. The response schema excludes retrieval actions already completed.
The model cannot supply arbitrary URLs or commands.

Evidence preserves original labels and tags, USD amounts, period end, filing date,
accession, and document/element citations. Inline XBRL supplies numeric scale,
sign, company identity, and context dates. Segment/dimensional values, unsupported
numeric transforms, and unrelated dates are excluded. No company-specific
financial mappings remain.

For this first task, the retrieval tool exposes debt-related liability rows and
filters obvious interest, securities, receivable, and fair-value rows. This bounded
filter can miss unfamiliar layouts; it does not establish universal coverage.

Python requires unique known citations, coverage of retrieved balance-sheet debt
rows, nonnegative balances, and no apparent total/component overlap. It sums the
original amounts; the model never supplies the accepted numbers. Matching
same-accession XBRL facts must agree. Missing counterparts produce warnings.

`validated` means these structural/numeric checks passed, not that accounting
completeness is proven. Every result has `review_required=true`. Combined debt
and finance-lease balances need policy review; operating leases are excluded.
Note-only answers, invalid citations, uncertainty, tool failures, or exhausted
steps produce `needs_review` without an accepted total.

There are at most three decisions by default (maximum four), each with the
wrapper's bounded schema repair. Evidence prompts are limited to 10,000 characters.
This is not a tokenizer-level context guarantee. Filing text is treated as
untrusted evidence; bounded tool access limits what the model can execute.

## Run locally

Configure `SEC_USER_AGENT` with your application/name and contact email. Install
`qwen3:4b` in local Ollama and run:

```powershell
uv run python scripts/check_edgar_agent.py
uv run python scripts/check_edgar_agent.py --offline-sec
uv run pytest tests/test_sec.py tests/test_edgar_agent.py -q
```

`--offline-sec` replays saved SEC snapshots while keeping the local model live.
Evaluation disables LLM response caching and writes decisions, citations,
warnings, and timing to `data/edgar-agent/evaluation.json` (ignored by Git).
Expected totals are held by the evaluation host, never shown in the prompt.

Reference balances: Coca-Cola $45.492B at 2025-12-31; Nike $7.942B at 2026-05-31;
Apple $98.657B at 2025-09-27. Sources:
[Coca-Cola](https://www.sec.gov/Archives/edgar/data/21344/000162828026010047/ko-20251231.htm),
[Nike](https://www.sec.gov/Archives/edgar/data/320187/000032018726000088/nke-20260531.htm),
[Apple](https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/aapl-20250927.htm).

These three cases do not establish accuracy across every nonfinancial company.
Broader extraction, unseen-company evaluation, non-USD/IFRS support, full
financial history, and valuation-graph integration remain later steps.

## Evaluation outcome — 2026-10-13

The final uncached Qwen3 4B run with saved SEC snapshots passed all three cases:

| Company | Report date | Cited carrying debt | Reference match |
|---|---|---|---|
| Coca-Cola | 2025-12-31 | $45.492B | Passed |
| Nike | 2026-05-31 | $7.942B | Passed |
| Apple | 2025-09-27 | $98.657B | Passed |

Each case used two model decisions, a balance-sheet read, and XBRL reconciliation.
All selected balances had matching companyfacts counterparts; no reconciliation
warnings were emitted. The final Ollama resident-model check found no loaded
models. Local validation passed 242 tests, Ruff lint/format checks, strict mypy,
and pre-commit configuration validation.

These are development cases used to refine the interaction, not a held-out
accuracy benchmark. Earlier runs exposed repeated tool requests and invalid
citation IDs; state-dependent actions and short document-scoped IDs address those
failure modes, with invalid answers still rejected. Accounting interpretation
continues to require human review.
