# 23. Phase 2 plan: Router, Data Agent, WACC Agent and terminal chat

Written on 2026-10-15, after Phase 1. Goal from the roadmap: a user types
`Apple, 2026-10-11`, confirms the company at Gate 0, and asks data, cost-of-capital
and provenance questions (intents I1, I3, I10) in a terminal chat, with every
number sourced. As before, everything must work for any US non-financial company;
Apple, Coca-Cola and Nike are test cases.

## Acceptance criteria (from the roadmap)

| Criterion | How we will measure it |
|---|---|
| Router: at least 95% correct company resolution and intent classification on a 50-prompt set (ambiguous names, relative dates) | `evals/router/prompts.jsonl` with expected company, date and intent, scored by a script |
| WACC for 10 companies within +/-0.3% of the deterministic CLI result | 10 automatically collected bundles; agent WACC versus `engine.wacc` default rule |
| Every number in an answer has a provenance record | Answers rendered only from `Sourced` values; a test fails if an answer contains a number without a source |

## What Phase 1 already provides

| Need | Existing piece |
|---|---|
| Company list, SIC code, filings | `SECClient.get_ticker_map`, `get_company_profile`, `get_submissions` |
| Scope | `data/scope.py` (financial SIC, blank checks, IFRS, non-USD) and readiness |
| Data collection | `DataAgent` + `DataTools` (`collect_history`, prices, FRED, Damodaran) |
| Cost of capital | `engine/wacc.py` (bottom-up, regression, synthetic rating, band) and `engine/assemble.py` |
| Local model | `llm/` wrapper: structured output, repair, cache, single worker, mock mode |
| Contracts | `ValuationRequest`, `ValuationState`, `Event`, `ApprovalRecord`, `Budget` |
| Dependencies | `langgraph`, `langgraph-checkpoint-sqlite` already installed |
| Intent model | `qwen3:1.7b` and `qwen3:4b` both scored 20/20 on the five-label intent benchmark |

## Gaps to close first

1. **Collection still needs manual inputs.** `collect_data.py` requires the
   fiscal period, fiscal year and the exact Damodaran industry label, and the
   eleven-field snapshot uses the local model (about 7 minutes). For a chat, the
   Router must supply everything automatically.
   - Fiscal period: taken from the latest eligible 10-K's own XBRL package.
   - Seed and shares: the package extraction already reproduces the reference
     values (111/111); cover-page shares come from the same filing's `dei` facts.
     The LLM snapshot becomes an optional cross-check rather than a required
     step, cutting collection from minutes to seconds once the SEC cache is warm.
   - Industry: Damodaran publishes a company-to-industry file (`indname.xlsx`:
     company, exchange:ticker, exact industry label, SIC, country). Match by
     ticker; otherwise use the most common Damodaran industry among US companies
     with the same SIC code; the user confirms or changes it at Gate 0. The file
     is current, not historical: noted as a point-in-time limit.
2. **`ValuationState` does not hold the full `DataBundle`** (readiness, issues,
   benchmarks, rates). Add a `data` field rather than splitting the bundle.
3. **No graph, gate, checkpoint, audit log or chat yet** (`graph/` and `api/` are
   empty placeholders; Typer and Rich are not installed).

## Design

```text
user text --> Router --(Gate 0: confirm company, date, industry, output)--> plan
plan --> Data node --> [WACC node] --> Answer node --> chat
            ^                  ^
            |                  +-- WACC Agent: compares beta methods, explains choice
            +-- Data Agent: deterministic collection; bounded model decisions on blockers
state: ValuationState in a LangGraph SQLite checkpoint; every step writes an Event
```

### Router (`agents/router.py`)

Code first, model only where code cannot decide:

| Step | Implementation |
|---|---|
| Extract company, date, question | Rules split `"<company>, <date>[, question]"`; the 1.7B model extracts the parts only when the rules fail |
| Resolve company | Exact ticker, then normalized-name match, then fuzzy match over the SEC ticker list (stdlib `difflib`); several close candidates trigger Gate 0 |
| Parse date | Deterministic: ISO and common formats, "today", "yesterday", "end of Q3 2025", "last fiscal year end", "N days/months ago"; future dates rejected; weekend/holiday dates allowed (prices use the last trading day) |
| Scope | Existing scope rules plus first-XBRL-filing date (warn if the date precedes it) |
| Intent | Rules for clear patterns (ISO-date-only first message, "where does X come from", "set X to Y"); otherwise the 1.7B model chooses from I1-I11 with definitions |
| Plan | Intent to node list using the architecture's dependency map |

### Gate 0 (LangGraph `interrupt`)

Shows the resolved company, CIK, fiscal year end, latest eligible 10-K/10-Q,
industry, scope verdict and the output options (question / quick value / Excel).
The user confirms, picks another candidate, changes the industry, or stops. The
decision is stored as an `ApprovalRecord`. Quick value and Excel use the Phase 1
CLI path with an assumptions file until the Forecast Agent exists (Phase 4).

### Data Agent as a ReAct loop (`agents/data.py`)

The deterministic collection runs first. The model is consulted only when
readiness or scope reports a blocker, choosing one action from an enum (at most
four steps): `classify_unknown_lines` (existing guarded classifier),
`refresh_sec_data`, `retry_prices`, `accept_with_flag`, `ask_user`. Code
executes each action and re-checks readiness. Without a model, the default
policy (`accept_with_flag` for warnings, `ask_user` for blockers) still finishes.

### WACC Agent (`agents/wacc.py`)

Code computes bottom-up beta, regression betas over two windows (2 years
weekly, 5 years monthly) against SPY, the synthetic rating and the band. The
model chooses `bottom_up`, `regression` or `average` with a one-sentence reason;
a choice that differs from the code rule is flagged for review. The resulting
`WACCResult` is always recomputed by `engine.wacc`, so the agent adds reasoning,
not numbers.

### Answers (`agents/answer.py`)

| Intent | Answer |
|---|---|
| I1 data snapshot | Requested concepts for the latest period(s), with status, basis and filing link |
| I3 cost of capital | WACC build-up, both betas, rating, band, method notes |
| I10 audit | Provenance chain of a named value: filing, accession, element, derivation steps |

Text comes from templates filled with `Sourced` values; the model may only
choose which concepts the question asks for (from the concept enum). A unit test
scans every rendered answer for numbers lacking a source.

### Orchestration (`graph/`)

LangGraph `StateGraph` over `ValuationState`; `SqliteSaver` checkpoint in
`data/state/checkpoints.sqlite`; thread ID per session, so a closed chat can
resume. A `Budget` (steps, tokens, wall clock) caps each run; every node appends
`Event`s (tool calls, model calls, decisions) to the audit log.

### Terminal chat (`cli.py chat`)

Typer + Rich (planned in the stack): first message, Gate 0 prompt, progress for
collection, then a question loop with tables and source links. `/sources`,
`/state`, `/resume` and `/quit` commands. The existing `value` command stays.

## Work breakdown

| Step | Work | Exit check |
|---|---|---|
| 1. Automatic collection | Fiscal period and seed from the latest 10-K package, `dei` shares, Damodaran industry mapping, LLM snapshot optional; `DataBundle` in state | Bundles for the reference cohort match the current ones (111/111 references still exact) without manual arguments |
| 2. Router v0 | Parsing, resolution, dates, scope, intent rules plus model | 50-prompt set at least 95% |
| 3. Graph skeleton | State, nodes, checkpoints, audit log, budget, Gate 0 interrupt and resume | Integration test: interrupt at Gate 0, resume, finish; restart from checkpoint |
| 4. WACC Agent | Two regression windows, enum choice, flags | 10 companies within +/-0.3% of the CLI result |
| 5. Data Agent loop | Enum actions on blockers, default policy | Cohort run: blockers handled or explained, no silent changes |
| 6. Answers | I1, I3, I10 templates and provenance test | Every number in every answer has a source |
| 7. Terminal chat | Typer + Rich session over the graph | Scripted session test plus a manual walkthrough |
| 8. Evaluation and docs | Router report, WACC report, latency on the CPU | Acceptance table filled in; docs updated |

Steps 1 and 2 are independent; step 3 needs 2; steps 4-6 need 3; step 7 needs
6. Suggested order: 1, 2, 3, 6, 4, 5, 7, 8 (answers early so the chat is useful).

## Evaluation sets to build

- **Router (50 prompts):** tickers, full and partial names, ambiguous names
  (two share classes, similar names), misspellings, relative and absolute dates,
  questions of each intent, out-of-scope companies (a bank, a REIT), future dates.
  Written with expected answers before any model run.
- **WACC (10 companies):** the three reference companies plus seven from the
  targeted cohort with different profiles (high beta, debt-free, captive finance
  excluded, loss-making).

## Risks

| Risk | Mitigation |
|---|---|
| Fuzzy name matching picks the wrong company | Several close candidates always go to Gate 0; never guess |
| Industry file is current, not historical | Note it; the user confirms the industry at Gate 0 |
| CPU model latency in chat | Rules first; 1.7B for routing; cached answers; collection progress shown |
| Scope creep into Phase 4 (forecasting) | Quick/full values only through the existing assumptions-file path |

## Decisions with defaults (change if you prefer)

| Decision | Default |
|---|---|
| LLM snapshot in the default collection path | Off: package extraction is the source; snapshot available as an optional cross-check |
| Industry mapping | Damodaran company file by ticker, SIC fallback, confirmed at Gate 0 |
| Router model | `qwen3:1.7b` (faster; 20/20 on the intent benchmark) |
| Chat UI | Typer + Rich, as planned |

## Progress

### Step 1: automatic collection (done, 2026-10-15)

Collection now needs only a ticker and a date:

```powershell
uv run python scripts/collect_data.py --ticker KO --as-of 2026-10-11 --out outputs/ko.json
```

| Piece | Implementation |
|---|---|
| CIK and name | SEC ticker list and submissions profile |
| Fiscal period and seed | `filing_seed`: the latest eligible 10-K (successor-aware); its own full-year period (350-380 days ending at the report date); reported values from the package extraction; fiscal year from the document focus |
| Shares | `cover_shares`: `dei:EntityCommonStockSharesOutstanding` of the newest periodic filing that reports it (total, else the sum over share classes), with its measurement date |
| Industry | `data/industry.py` and `DamodaranClient.industry_for`: Damodaran's `indname.xlsx` (about 6,900 US listings) by ticker; otherwise the SIC majority among labels in the betas table, with up to three alternatives and a warning to confirm |
| Snapshot | Optional: `--interpret` (local model) or `--sec-snapshot` (saved); compared with the package values and reported as a warning, never used as a value |
| Handoff | `DataBundle.industry` records the label, method, SIC, alternatives and note; `ValuationState.data` carries the whole bundle |

**Exit check.** The three reference bundles were rebuilt offline from
`{ticker, as_of}` only: all checks pass, 10/10 reference values each (the
eleventh, shares, now comes from the newer 10-Q cover and is checked by date),
five years, eight quarters, TTM, and every readiness consumer at
`needs_review`, none blocked. `check_extraction.py`: 111/111 references,
balance identity unchanged. Cohort readiness (51 companies) unchanged:
normalization 32, WACC 45, equity bridge 44, scope 51. About 8 seconds per
company from the cache, instead of about 7 minutes with the model snapshot.

**Industry differences.** Apple and Coca-Cola map to the labels used before.
Damodaran's own file puts Nike in **Shoe**, not the Apparel label that was
typed by hand earlier; the automatic assignment is kept (the user can change it
at Gate 0 or with `--industry`).

**General fixes found on the way.**

- The seed initially carried broader-basis lines (Coca-Cola's "accounts payable
  and accrued expenses"), which history had already decided against in favour of
  the exact tag; offered again as a seed they looked like a conflict and the value
  was dropped. Broader-basis lines are now left out of the seed.
- Income-statement periods were anchored only on revenue or `NetIncomeLoss`. A
  company that tags its bottom line only as `ProfitLoss` (consolidated profit)
  and has no revenue (Solid Biosciences) had no income statement at all. Periods
  now also anchor on `ProfitLoss` and operating income, and `ProfitLoss` is
  accepted as net income only when the statement shows no noncontrolling
  interest. Solid Biosciences now builds five years and eight quarters and is
  correctly blocked as pre-revenue by the scope rule.

Tests: 466 pass (new: `test_collection.py` for the industry file, cover shares
and the seed; Data Agent orchestration rewritten for the optional snapshot;
`ProfitLoss` extraction cases; state round trip with the bundle).

### Step 2: Router v0 (done, 2026-10-15)

`agents/router.py` turns the first message into a `Route`: company, candidates,
valuation date (or why it was rejected), question, intent, scope verdict and the
graph plan. Nothing is final until Gate 0.

| Piece | Implementation |
|---|---|
| Split | Rules for `<company>, <date>[, question]` (the date may contain commas, e.g. "Oct 1, 2026"), `<company> <date>` and `<company>, <question>`; any other sentence goes to the model, which only copies the company phrase. The phrase must occur in the message, so an invented name is discarded |
| Company | `data/companies.py`: SEC listings (`get_company_list`, primary ticker first). Upper-case ticker; exact name up to its legal form; exact after also dropping words like Group or Holdings (ranked lower: "Target Corp" vs "Target Group Inc."); word prefix; spelling (difflib). Candidates within 0.05 of the best are equally plausible, so the user chooses. Preferred-stock listings are never chosen; other common share classes are noted |
| Date | `data/dates.py`: ISO, US numeric and written dates, today/yesterday, N days/weeks/months/years ago, quarter and year ends, end of last month/quarter/year, last fiscal year end (latest filed 10-K report date). Future and pre-EDGAR dates are rejected; no date means today, said explicitly |
| Scope | Existing SIC rules (financials, REITs, blank checks), plus a block when no 10-K or 10-Q was filed by the date |
| Intent | Ordered patterns (actions before topics: modify, sensitivity, audit, rationale, scenario, comps, full model, cost of capital, valuation, history, data), then `qwen3:1.7b` choosing from I1-I11 definitions |
| Plan | I1 data, answer; I2 + normalize; I3 + wacc; I6 + value; I9 + excel; I10 data, answer; company and date only: the Gate 0 menu. Other intents are classified and reported as arriving in a later phase |

**Exit check: 50/50 (100%), target 95%.** `evals/router/prompts.jsonl` was
written before the code. Rules alone: 45/50 (the five free-form sentences need
the model). Two prompts failed during development and led to general changes:

- The model returned null for plain sentences ("Is Target undervalued...") when
  offered a null answer; the field is now required and checked against the text.
  The new prompt was tried first on eight sentences outside the set (8/8).
- "Target" matched both Target Corp and Target Group Inc.; matches that need an
  organizational word dropped now rank below exact legal-name matches.

The intent patterns were written after the prompts, so intent accuracy on this
set is optimistic. Two independent checks:

- Model only (patterns off): 47/50 overall, 23/26 questions labelled correctly.
  Its misses are typical confusions (3-year revenue as a snapshot, "what if WACC
  rises" as cost of capital, "worth" as data), which the patterns cover.
- Fifteen new prompts after the set passed: all companies and dates right
  (misspelling, a bank blocked, a future date rejected); intents 13/15. The two
  misses ("evolved since 2021", "which XBRL element ... taken from") were added
  as general phrasings, so those two are no longer unseen.

Latency on the CPU: about 0.05 s for rule-handled messages; a model call (free
form or unrecognized question) about 5-7 s, cached afterwards.

### Step 3: graph skeleton (done, 2026-10-15)

`graph/workflow.py` builds a LangGraph `StateGraph` over `GraphState` (the
valuation state plus session fields: message, route, output choice, industry
override, answer, status, completed steps).

```text
router -> gate0 (interrupt) --confirm--> data -> wacc -> ... -> answer -> END
            ^      |  choose / redate / invalid / unresolved: ask again
            +------+  stop -> END
```

| Piece | Implementation |
|---|---|
| Gate 0 | `interrupt()` with a plain-data payload (company, candidates, date and its source or error, scope, intent, plan, notes, options). The answer is a validated `GateDecision`: confirm (with question, output and industry override), choose a candidate, redate, or stop. Unresolved, out-of-scope or unclear answers are refused with a warning event and the gate asks again; nothing is collected before confirmation |
| Approval | Each decision is an `ApprovalRecord` (gate 0) with the SHA-256 of exactly what was shown; an industry override makes it `edited` with an edit reference |
| Plan | From the Router's intent map: quick value is I6, full Excel I9, a question uses its intent. Steps not built yet (normalize, value, excel) are recorded as warnings and skipped, and the answer says so |
| Checkpoints | `SqliteSaver` at `data/state/checkpoints.sqlite`, one thread ID per session. A contract serializer stores our models as JSON tagged with their class, only from an allowlist of project modules, and validates them again on load (LangGraph's default restores models without validation, so a tuple came back as a list, and it will stop restoring unregistered classes) |
| Audit log | Every node appends `Event`s (router decision, human decision, tool calls with input and output hashes, warnings, errors) with IDs unique per session |
| Budget | Each node counts a step and its run time (time waiting for the user excluded); an exhausted budget stops before the next step |
| Failures | A failed collection ends the run with the error as the answer; a WACC that readiness blocks is a warning and the answer explains it |
| Services | Injected: tests use fakes; `graph/services.py` opens SEC, FRED, Damodaran, prices and the local model once per session. WACC uses the deterministic engine with a 25% US marginal tax rate until Step 4 |

**Exit check.** `tests/test_graph.py` (9 tests): interrupt at Gate 0, resume and
finish; restart with a new connection and graph finds exactly the payload that
was shown, with validated contracts, and finishes; ambiguous company chosen then
confirmed with a question; redate and stop; out-of-scope refusal; failed
collection; budget halt; invalid gate answer; serializer allowlist and
validation. Real run (`scripts/check_graph.py --offline`): Coca-Cola stops at
Gate 0, a second graph resumes from the SQLite checkpoint and finishes data,
WACC (7.71%, as the Phase 1 CLI) and answer in 7 seconds, with five audit
events. 540 tests pass.

Not yet: token counts in the budget (model calls happen inside the Router and the
optional snapshot and are not reported to the graph), follow-up questions within
a session (Step 7), and answer templates (Step 6).

### Step 6: answers (done, 2026-10-15)

`agents/answer.py` renders answers from templates and `Sourced` values; the graph's
answer node stores the text and the structured `Answer` (body, figures, sources).

| Intent | Answer |
|---|---|
| I1 data | Table of the requested items for the latest fiscal year and the TTM (stocks at the TTM end); statuses in words ("not presented (evidenced zero)", "not disclosed", "unresolved"); derived items (free cash flow, net debt, gross, operating and net margin) computed with their formula as provenance |
| I2 history | The same items across the five fiscal years and the TTM, with each fiscal year's dates |
| I3 cost of capital | WACC and its range, then every component: risk-free rate, ERP, CRP, unlevered and relevered beta, regression beta (when available), beta method, cost of equity, pre-tax and after-tax cost of debt, tax rate, weights, terminal WACC. A WACC that could not be computed is explained |
| I10 audit | For each named item (or WACC component): value, how it was obtained (reported in a filing on a date; sum of statement lines; sum of four quarters; year-to-date difference; carried forward) and the full chain in the source entry |
| Others | A short status, and which plan steps arrive later |

**Citation rule.** Every number in an answer body is a figure, the formatted value
followed by a marker `[n]`. Source entry `n` gives the source, the link (to the
exact fact in the filing for SEC values), filing date, period end, element,
accession, basis and derivation steps. Identical evidence shares one entry.
Numbers inside source entries (formula inputs, method notes such as interest
coverage) are part of the provenance record. The model may only choose which
items a question asks for, from the item list, when no keyword rule applies.

**Exit check: every number in every answer has a source.** `tests/test_answer.py`
removes every cited figure, date, fiscal-year label and the company's own name
and ticker from each body, and fails if any digit remains; it also checks that
every marker has a source entry whose link matches the figure's provenance. A
deliberately uncited "12%" is caught. On the three reference bundles: 393
answers (every item for I1, I2 and I10, every WACC component for I10, and I3),
4,231 figures, none uncited. The checker itself had a bug found by the test (removing
"5.00% [1]" also removed the end of "25.00% [1]"); it now removes whole figures
only. 551 tests pass. A graph session ("Nike, 2026-10-11, where does the debt
number come from?") restarts from its checkpoint and answers with the two
balance-sheet lines and their links in the filing.

### Step 4: WACC Agent (done, 2026-10-15)

`agents/wacc.py`. Code computes the bottom-up beta, two regressions against SPY
(two years of weekly returns, the window the CLI uses; five years of monthly
returns, new in `engine.wacc`), which reliability thresholds each window passes,
the synthetic rating and the band. The model (role `wacc`) sees these facts,
with the threshold verdicts computed for it, and returns only a method
(`bottom_up`, `regression`, `average`) and one sentence. The engine always
recomputes the WACC (`WaccInputs.beta_method`, with the rule's own verdict kept
in the notes).

**Guard.** As with the line classifier, a departure from the default rule needs
evidence code can verify: `regression` needs a window that meets the reliability
rule (that window's beta is used); `average`, a shrinkage toward the industry,
needs a window that explains the stock (R^2 >= 0.3) but is imprecise (only the
standard error fails). Otherwise the choice is rejected, recorded in the notes
and the audit log, and the rule applies. Accepted departures are flagged for
review. The graph records the decision (`wacc_decision`, an `llm_call` event,
warnings for flags and rejections) and the I3 answer says who chose the method.

**Model choice, measured on ten companies** (AAPL, KO, NKE, MSFT, NVDA, CAT, BA,
SNOW, MCD, MNST; high beta, captive finance, losses, negative equity, debt-free):

| Version | Model | Within 0.3 pt of the CLI | Notes |
|---|---|---|---|
| First prompt | qwen3:1.7b | 4 of the first 4 run, 2 far out | Chose "regression" while its own reason said the rule picks bottom-up |
| Guard, first "average" wording | qwen3:1.7b | 6/10 | "average" on 10 of 11; 3 rejected by the guard |
| Guard, shrinkage definition | qwen3:1.7b | 5/10 | Still averaged where its reasons said the thresholds fail |
| Guard, shrinkage definition | qwen3:4b | **10/10** | Kept the rule everywhere with correct, specific reasons |

The `wacc` role now uses qwen3:4b (`config/models.yaml`), about 33 seconds per
decision on the CPU. Deere was excluded from the ten: readiness correctly blocks
its WACC because its latest balance sheet does not split captive-finance debt
(the approved policy); Monster Beverage (debt-free) replaced it.

**Exit check: 10/10 within 0.3 percentage points of the CLI** (all identical,
none flagged). On these companies no regression met the rule (weekly standard
errors 0.11-0.37; R^2 often below 0.3), so the rule and the agent agree on the
bottom-up beta. Tests (`tests/test_wacc_agent.py`, 11): agreement, supported
regression (uses the reliable window, flagged), supported average (midpoint),
unsupported departures rejected, model failure, no regression (model not
asked), override arithmetic, monthly regression. 562 tests pass. A live graph
session ("Caterpillar, 2026-10-11, which beta should we use?") finishes in 37
seconds with the decision in the answer and the audit log.

The marginal tax rate is still the 25% US policy value (`graph/services.py`).

### Step 5: Data Agent loop (done, 2026-10-15)

`agents/data_loop.py` runs after the deterministic collection, only when a
readiness consumer the plan needs is blocked or a required source failed (plan
step to consumers: data to scope; wacc to wacc and scope; normalize to
normalization; value and excel to all four). Up to four steps, each action once:

| Action | What code does |
|---|---|
| `classify_unknown_lines` | The guarded line classifier on material unmapped lines named in an unresolved reason; decisions stored; collection reruns |
| `refresh_sec_data` | `SECClient.start_refresh`: each mutable endpoint (submissions, companyfacts) downloaded again once; filed documents never; collection reruns |
| `retry_prices` | Collection reruns, trying the price sources again |
| `accept_with_flag` | Continue; remaining blockers listed; later steps explain |
| `ask_user` | Gate 1 (data review): blockers and steps shown; continue or stop, stored as a gate-1 `ApprovalRecord` |

Code decides which actions apply (network actions only online; classification
only when a blocking unmapped line exists); the model (role `data`, qwen3:1.7b)
chooses among them; a choice outside that set is rejected and recorded. Without
a model: retry prices if they failed, otherwise ask the user. Every step records
the blockers before and after and every mapping and value it changed. Line
decisions that remove a value or add a blocker are undone (the stored mappings
restored, the collection rebuilt) and recorded as reverted.

**Exit check: blockers handled or explained, no silent changes.**
`scripts/check_data_loop.py` on the 52-company cohort with the full valuation
plan and the local model: 30 clear, 1 flagged (Oracle: two old lease values
missing), 21 asking the user with their blockers; 52/52 explained, 0 errors, 0
unrecorded changes (every difference between the first and the final bundle
appears in a step's changes). 31 actions chosen by the model, none rejected.
Remaining blockers are mostly genuine: no TTM or short history, pre-revenue or
shell companies, Deere's unsplit captive-finance debt.

**Problems the run found, fixed:**

- MIST: the classifier mapped "R&D tax credits receivable" to receivables, which
  removed the latest receivables value (recorded, but the data got worse). The
  undo rule above was added; the stored decision was removed from
  `data/mappings`; a rerun reverts it and keeps the value. The undo is
  all-or-nothing: the same decision would also have filled nine older
  receivables values, which are given up with it.
- During a refresh every request re-downloaded the filing list (four times for
  Church & Dwight in one rebuild); now once per endpoint per refresh.

**Open:** Church & Dwight spent almost two hours in its refresh step (the other
51 companies took 13-530 seconds); the cause was not captured because the run's
output was buffered. Likely a slow SEC download (the client's 30-second timeout
applies between bytes, not to a whole response). The refresh fix reduces the
exposure; a whole-response deadline is a possible follow-up.

576 tests pass (loop: 12, including the undo; Gate 1 pause, continue and stop in
the graph; refresh once per endpoint).
