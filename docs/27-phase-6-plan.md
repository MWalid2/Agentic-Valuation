# 27. Phase 6 plan: Evaluation, hardening, UI polish

Written on 2026-10-17, after Phase 5 ([26-phase-5-plan.md](26-phase-5-plan.md)).

Phases 1 to 5 built the system end to end:

```text
data -> normalization (Gate 1) -> WACC -> forecast (Gate 2) -> valuation -> comps
     -> Critic -> workbook -> sign-off (Gate 3) -> report
```

Ten companies were used throughout development. Phase 6 asks whether it holds up on companies it hasn't seen, without anyone watching. It then:

- measures where the time goes;
- compares open models per role;
- makes the chat easier to use and to recover from errors;
- documents and reviews the system.

The rule from every phase still holds: any US non-financial company, every number sourced, the model chooses and code calculates.

## What Phase 6 delivers

| Need | Today | After Phase 6 |
|---|---|---|
| Unattended runs | Autopilot exists; check scripts run one company at a time and stop on a crash | A batch runner: autopilot sessions, resumable, a time limit per company, one diagnostics record per company, failures sorted into classes |
| Generalization | 10 development companies (plus Phase 3a's cohorts for data) | 20 golden companies (10 new, chosen for profiles not yet tried) and 10 randomly sampled ones as a holdout, never used for tuning |
| Timing | Stage latencies from Phase 2; Phase 5 times with warm caches | Cold and warm times per company and per agent, from the traces; checked against the targets |
| Traces | JSONL model traces and the SQLite audit log, read by hand | A static HTML trace view per session (timeline, agents, model calls, tokens, warnings, approvals), written by a script |
| Model choice | Chosen by hand per role (the Critic fell back from 8b to 4b) | A comparison harness: same inputs, models swapped per role, scored on that role's own checks, plus time |
| Chat | Gates, answers, commands | Progress timeline, `/assumptions`, `/report`, `/trace`; errors that say what happened and what to do |
| Docs | Design and phase docs | A user guide, a developer guide, how to add a data source or an agent |
| Security | Ad hoc | A written review: inputs, model output handling, files, secrets, SEC fair-access rules |

## Design

**1. Batch runner (`scripts/run_batch.py`).**
- **Input:** a cohort file and a date.
- **Per company:** a full-model session (I9) on autopilot, with a wall-clock limit.
  - Each runs in a fresh checkpoint thread.
  - A crash in one company is caught and recorded; the batch moves on.
- **Resumable:** the results file is appended per company, and done companies are skipped on restart. A batch can therefore run in short chunks.
- **Per company, it records:**
  - outcome, steps reached, value, price;
  - Critic score and issues, comps peers;
  - audit status, parity and structure;
  - time per step;
  - warnings, the failure class, and the path of the exported report.
- **Failure classes** (each carries a reason the user can act on):
  - data (missing statements, no filing);
  - scope (financial, no 10-K);
  - engine guardrail (for example WACC at or below growth);
  - model (invalid output after repair);
  - time limit;
  - crash (with the exception).
- **What counts as success.** A run succeeds when it reaches the export with a value, a clean audit, workbook parity and structure, and no high-severity Critic issue left. A run that stops with a classified reason is a diagnosed failure. A run that exports while a hard check fails is a **wrong number**, the one outcome that must not happen.

**2. Companies.**
- **Golden 20:** the 10 development companies, plus 10 from `scripts/cohorts/targeted.json` with untried profiles:
  - large leases and thin margins: WMT, COST;
  - consumer staples: PG;
  - cyclical energy: XOM;
  - airline (leases, cyclical): DAL;
  - regulated utility: DUK;
  - loss-making growth: RIVN;
  - mega-cap software and ads: ORCL, META;
  - restaurant with heavy leases: SBUX.
- **Random holdout:** 10 drawn with a fixed seed from `scripts/cohorts/random.json` (active US non-financial 10-K filers). Reported separately and never used to tune anything; it shows how the system does on companies no one picked.

**3. Timing.**
- **Cold:** every model call runs, with the response cache off and sources replayed offline, as in `measure_latency.py`. **Warm:** the caches as a user would have them.
- **Per agent:** time and tokens from the model traces and the audit log's step durations.
- **Targets (roadmap):** a full model in 45 minutes or less, cold; an information answer from cache in under 60 seconds.

**4. Trace view (`scripts/trace_report.py`).** One HTML file per session, with no server and no new dependency:
- a timeline of steps with their durations;
- model calls by role (time, tokens, attempts, repairs);
- warnings, approvals (who, which gate, autopilot or not) and Critic loops.
It reads the checkpoint database and `data/traces/llm.jsonl`.

**5. Model comparison (`scripts/compare_models.py`).** Per role, the same saved inputs are run under each candidate model (a config override, not an edit), scored with that role's existing checks:

| Role | Scored on |
|---|---|
| Router | intent set accuracy |
| Filings | the dev set's calibration |
| Forecast | hard and soft bounds, support rule, spread |
| Comps | keep/drop against a hand-labelled peer list for the golden 20 |
| Critic | seeded-flaw stories (a story contradicting its numbers, a missing main risk) plus the share of concerns that are valid |
| Reporter | not used: no model text |

Each is also scored on time per call. Models are limited to what fits 16 GB RAM on CPU: qwen3 1.7b, 4b and 8b, plus one other open family at 3-8B if it runs in Ollama.

**6. Chat polish.**
- A progress line per step: "comps: 5 peers (12 s)".
- New commands:
  - `/assumptions` shows the current scenarios at any time;
  - `/report` lists the exported files;
  - `/trace` writes and opens the session's trace view.
- Error messages from a small catalogue keyed by failure class, each saying what happened and what to try (another date, an industry override, a smaller question).

**7. Docs.**
- **User guide:** sessions, the gates, commands, outputs and how to read them, limits.
- **Developer guide:**
  - layout, contracts, provenance, how to run the checks;
  - how to add a data source (an adapter, its contract, provenance, cache, tests);
  - how to add an agent (role config, schema, "choose, don't invent", fallback, tests, graph node).

**8. Security review** (written as `docs/28-security-review.md`):
- no code execution from model output or filings (model text is parsed into schemas only);
- inputs validated (tickers, dates, questions; free text escaped in the chat and the HTML memo);
- file paths built from validated tickers only;
- downloaded files kept as data;
- secrets kept in `.env` (as secret strings) and out of logs and traces;
- SEC fair access (the User-Agent from `.env`, rate limit, cache);
- the dependencies' licences.

## Acceptance criteria

| Criterion | Target | Measured by |
|---|---|---|
| Unattended golden run | At least 90% of the 20 succeed (18/20); every failure diagnosed; **no wrong number** | Batch runner |
| Random holdout | Reported in full; every failure diagnosed; no wrong number (no success rate target: it is a holdout) | Batch runner |
| Full model time | 45 minutes or less a company, cold, documented per agent | Timing run |
| Cached answer time | An information answer from cache in under 60 s | Timing run |
| Model comparison | Each model role scored on its checks and time; the choice per role recorded with its numbers | Comparison harness |
| Earlier checks | Unchanged | Existing tests and check scripts |

## Work breakdown

| Step | Work | Exit check |
|---|---|---|
| 1. Batch runner | Autopilot sessions, resume, time limit, diagnostics, failure classes | Scripted: a crash, a time-out and a guardrail each recorded and the batch continues; resume skips done companies |
| 2. Golden 20 | Run, sort failures, fix what is general (never a company-specific patch), rerun | 18/20 succeed, no wrong number |
| 3. Random holdout | Run once, report; fixes only if general, then rerun the golden 20 | Every failure diagnosed, no wrong number |
| 4. Timing and trace view | Cold and warm timing, per-agent tables, HTML trace view | Targets met or the gap explained; a trace view for one golden session |
| 5. Model comparison | Harness, peer labels for comps, Critic story seeds; run per role | A table per role; choices recorded in `config/models.yaml` with the reason |
| 6. Chat polish | Progress line, commands, error catalogue | Scripted chat sessions; a live walkthrough |
| 7. Docs and security | User guide, developer guide, security review | Reviewed by you |
| 8. Evaluation | Acceptance table, known limits | Table filled in |

## Decisions with defaults (change if you prefer)

| Decision | Default |
|---|---|
| Long runs | No single run of several hours: batches of about 5 companies (under an hour each), resumable. A full overnight batch is possible but not needed |
| Time limit per company | 45 minutes (the roadmap target); a company over it counts as a timed-out failure |
| Golden 20 | The 10 development companies plus WMT, COST, PG, XOM, DAL, DUK, RIVN, ORCL, META, SBUX |
| Holdout | 10 random US non-financial filers, fixed seed; reported, never tuned on |
| Utilities | **Changed (your choice, 2026-10-18): out of v1 scope.** Regulated utilities (SIC 4911, 4923, 4924, 4931, 4932, 4941) stop at Gate 0 with the reason; pipelines, independent power and waste services stay in. DUK valued at $10.84 against a $116.65 price: allowed returns near the cost of capital leave the equity almost no DCF value. In the golden 20, GEV (a 2024 spin-off) replaces DUK |
| Downloads | The new companies' and their peers' SEC data are downloaded once (online, as before), then cached |
| Model candidates | qwen3 1.7b, 4b, 8b, and one other open family at 3-8B if it runs in Ollama; nothing that needs a GPU or more than 16 GB |
| UI | The terminal chat stays the interface; no web app in this phase |

## Risks

| Risk | Mitigation |
|---|---|
| New profiles break extraction (leases, utilities, losses) | Failures are sorted into classes and fixed in general code with a test, never with a per-company patch; the holdout catches over-fitting to the golden 20 |
| A wrong number passes silently | "Wrong number" is its own outcome: any export with a failed hard check (audit, parity, structure, high Critic issue) fails the phase |
| Cold runs are slow on this CPU | Per-agent timing shows where the time goes; the model comparison weighs time, not only quality |
| Hand-labelled peer lists are subjective | Labels written once with reasons, before the model runs; reported as agreement, not truth |
| Long batches | Chunks of about 5 companies, resumable |

## Progress

Phase 6 is complete: steps 1-8 below, evaluation at the end.

**Step 1, batch runner (done, 2026-10-17).** `src/dcf_agent/batch.py`, `scripts/run_batch.py`, `scripts/cohorts/golden20.json`, `tests/test_batch.py`.
- **Per company:** a fresh session on autopilot with a wall-clock budget (45 minutes by default, `--minutes`), then structure and LibreOffice parity on the session's workbook.
- **Results:** one JSON line per company in `outputs/batch/<cohort>-<date>.jsonl`. Rerunning skips the companies already there; `--chunk N` limits a run to N new companies.
- **Outcomes:**
  - **success;**
  - **flagged:** exported, hard checks pass, but a high-severity Critic issue is left. It is listed in the report, so it isn't silent; this clarifies the plan's "no high-severity issue" rule;
  - **failed:** with a class (resolution, scope, data, normalization, WACC, forecast, engine, comps, export, time limit, crash), the reason and advice on what to do;
  - **wrong number:** exported with a failed audit, parity or structure check.
- **Tests:** success; engine guardrail, data, scope (a bank) and resolution failures; the time limit; a model-estimated value exported, which is a wrong number; a crash in each of two companies, recorded with the batch continuing; resume skipping both.

**Step 2, golden 20 (done, 2026-10-18).**
- **Result: 20 of 20 succeed** (`outputs/batch/golden20-2026-10-11.jsonl`). Every company exported with a clean audit, parity 8/8 and the structure check; no wrong number. Median 188 s, longest 418 s, with downloads and cached model replies.
- **Fixed on the way:**
  - **A negative value per share is now a Critic warning** (BA in the bear case, RIVN). Limited liability floors a share at zero.
  - **XOM had no peers.** Its current SEC registrant is a new holding company (CIK 0002115436) with almost no company facts of its own. Comps now fill missing target figures from the valuation's own sourced data. Filled figures are kept out of the Critic's bridge check, which must stay an independent read. XOM now has 2 peers (CVX, OXY).
  - **Utilities left v1 scope (your choice).** DUK was valued at $10.84 against a $116.65 price. GEV (a 2024 spin-off) replaced it and succeeds. DUK now stops at Gate 0 with the reason (`scripts/cohorts/scope-checks.json`).
  - **Model traces carry a timestamp**, so the trace view can place model calls in a session.
- **Flagged, not wrong:**
  - DAL (+75%; terminal value 96% of EV, heavy reinvestment) and ORCL (+109%; guided growth) are well above the price.
  - SNOW (-94%), RIVN (negative) and BA (negative) are well below it.
  - Each carries the Critic's medium warnings (terminal value share, comps gap, negative value) in its report.

| Company | Value | Price | Gap | Critic | Peers | Seconds |
|---|---|---|---|---|---|---|
| AAPL | 158.00 | 336.64 | -53% | 4.75 | 4 | 46 |
| BA | -7.51 | 190.37 | -104% | 3.8 | 7 | 130 |
| CAT | 447.04 | 800.47 | -44% | 5.0 | 5 | 56 |
| DE | 327.49 | 620.93 | -47% | 4.65 | 5 | 86 |
| KO | 50.56 | 88.05 | -43% | 5.0 | 5 | 48 |
| MCD | 183.36 | 235.23 | -22% | 5.0 | 8 | 52 |
| MSFT | 423.17 | 535.07 | -21% | 5.0 | 10 | 300 |
| NKE | 16.71 | 34.71 | -52% | 4.4 | 5 | 52 |
| NVDA | 295.01 | 229.28 | +29% | 4.65 | 5 | 47 |
| SNOW | 22.95 | 368.89 | -94% | 3.75 | 3 | 39 |
| WMT | 41.52 | 111.35 | -63% | 5.0 | 6 | 316 |
| COST | 569.97 | 946.92 | -40% | 4.75 | 5 | 315 |
| PG | 115.18 | 151.23 | -24% | 5.0 | 8 | 414 |
| XOM | 155.40 | 168.94 | -8% | 3.3 | 2 | 81 |
| DAL | 143.46 | 82.17 | +75% | 4.0 | 7 | 271 |
| GEV | 465.72 | 1,004.73 | -54% | 4.75 | 9 | 345 |
| RIVN | -19.73 | 13.96 | negative | 3.75 | 7 | 246 |
| ORCL | 296.00 | 141.40 | +109% | 4.75 | 7 | 371 |
| META | 357.23 | 718.67 | -50% | 5.0 | 3 | 310 |
| SBUX | 22.50 | 90.75 | -75% | 4.65 | 6 | 418 |

**Step 3, random holdout (done).** 10 US filers drawn with a fixed seed (`scripts/cohorts/holdout10.json`). It found what the golden companies never hit, and every finding was fixed in general code with a test:
- **Pre-revenue companies crashed** (`KeyError: 'revenue'`: ABAT, SLDB, NUMD). Normalization now stops them with the reason: outside v1, pre-revenue methods are Phase 7.
- **A wrong number: Corteva.** Yahoo recorded its 2026 separation as a 6.665-for-1 "split", and the SEC share count was multiplied by it (667M became 4.4B shares; value per share about 6.7 times too low). The audit and parity passed: the numbers were sourced and the workbook matched. Only the Critic's -93% gap to comps hinted at it. A provider split is now applied only when its ratio is a standard one; otherwise the market data is refused with the reason, so the company stops at the WACC instead of exporting a wrong value. Of the 31 batch sessions, only CTVA had a split applied.
- **A step that hangs is not stopped by the budget,** which is checked between steps. SYY's data step ran 12.5 hours, most likely the computer sleeping overnight: the cache wrote nothing between 01:00 and 13:00. Each company now runs in its own process and is killed at the time limit plus 5 minutes.
- **Securities without a statement line of their own** (SYY: disclosed in the notes, inside other lines) blocked normalization and the bridge. They are now counted as within the other lines (none deducted), with a note. A value that is genuinely missing still blocks.
- **Results:**

| Company | Result |
|---|---|
| BIOA, ADPT | success |
| NEON | flagged: operating margins above 300% on a near-zero revenue base; a new high-severity Critic check, two loops, left for review |
| CTVA | stops: non-standard split |
| FPS | stops: no TTM |
| ABAT | stops: debt unresolved |
| SLDB, NUMD | stop: pre-revenue |
| HNRG | stops: utility scope |
| SYY | success after the securities fix: $71.30 against a $78.14 price, 5 peers, parity 8/8 |

  No wrong number remains; every failure states its reason. The holdout added two more general rules:
  - **Securities inside other lines** are not double counted in the bridge either.
  - **An operating margin beyond +/-100% of revenue** is a high-severity Critic issue (19 of 19 seeded flaws caught).

**Step 6, chat polish (done).**
- **Progress lines:** each step prints what it found ("comps: 5 peers..."), with its warnings in yellow. The comps, Critic and sign-off steps were missing from the progress list.
- **New commands:** `/assumptions`, `/report`, `/trace`.
- **Failed runs** say what happened and what to try, using the batch's failure classes and advice.
- **Tests:** scripted sessions for a sign-off with the three commands, and for a failed value with its explanation.

**Step 7, docs and security (done).**
- [28-security-review.md](28-security-review.md): no code execution paths, schema-only model output, safe parsers (the HTML ones made explicit), allowlisted checkpoints, validated inputs and paths, escaping. Secrets never reach caches, errors or files; this was checked against the real keys, and no file contains them. SEC fair access; permissive licences.
- [29-user-guide.md](29-user-guide.md) and [30-developer-guide.md](30-developer-guide.md).

**Step 4, timing and trace view (done).** `scripts/trace_report.py`, `src/dcf_agent/report/trace.py`, `scripts/time_answers.py`, `run_batch.py --no-cache`.
- **Cold full model** (model cache off, every call runs; sources replayed offline, with workbook parity):

| Company | Total | Data | Normalization (filings) | WACC | Forecast | Comps | Critic |
|---|---|---|---|---|---|---|---|
| KO | **617 s** | 18 | 265 | 34 | 114 | 105 | 55 |
| AAPL | **351 s** | 9 | 4 | 35 | 100 | 115 | 67 |

  Both are far under the 45-minute target. The comps model switch (step 5) takes about 80 s off the comps step.
- **Answers from a finished session:** 12 of 12 under 60 s (median 0.5 s, longest 0.6 s), all cited. Questions: cost of capital, five-year revenue, the debt figure's source, bull and bear cases; on KO, AAPL and NKE.
- **Trace view:** one static HTML page per session (`outputs/traces/`), showing time by component, model calls by role (from the timestamped traces), approvals, the Critic's issues and the timeline. Also `/trace` in the chat.
- **Found: the forecast is not fully repeatable.** Cold runs gave KO 48.49 against 50.56 cached, and AAPL 152.36 against 158.00. The forecast role samples at temperature 0.3, so a new call can pick a different level; Gate 2 shows the picks. The response cache makes a rerun repeatable.

**Step 5, model comparison (done).** `scripts/compare_models.py`, `evals/comps/peer-labels.json` (hand labels for 91 candidates of the 10 development companies). Cache off; the same inputs for each model.

| Role | qwen3:1.7b | qwen3:4b | qwen3:8b | Choice |
|---|---|---|---|---|
| Forecast (5 companies) | bounds 5/5, spread 4/5, 34 s | bounds 5/5, **spread 5/5**, 59 s | bounds 5/5, spread 4/5, 120 s | **4b** (unchanged) |
| Comps (keep/drop vs labels) | **66.7%**, 6 wrong peers kept, 21 good dropped, 65 s | 65.4%, 5 kept, 23 dropped, 147 s | 2 of 10 answered (timeouts) | **1.7b** (was 4b): the same accuracy in less than half the time |
| Critic rubric (3 seeded story flaws on KO) | 0/3, 25 s | 1/3, no concerns on the clean stories, 68 s | **3/3**, but 3 concerns on the clean stories, 166 s | **4b** (unchanged): the 8b sits at the 180 s limit before the risk passages a real run adds |

- Both small models drop many good peers; the fill-to-five rule (Phase 5) is what keeps samples usable.
- The 8b is the better critic: worth it on faster hardware or with a longer timeout.
- The router (1.7b) and the WACC role (4b) keep their earlier evaluations (docs 10 and 23).

## Evaluation (step 8, 2026-10-18)

| Criterion | Target | Result |
|---|---|---|
| Unattended golden run | at least 18/20 succeed; every failure diagnosed; no wrong number | **20/20 succeed**: clean audit, parity 8/8, structure; no wrong number |
| Random holdout | reported in full; every failure diagnosed; no wrong number | 3 succeed (BIOA, ADPT, SYY), 1 flagged (NEON), 6 stop with a stated reason (CTVA split, FPS no TTM, ABAT debt unresolved, SLDB and NUMD pre-revenue, HNRG utility). **No wrong number after the fixes.** The first pass had one (CTVA, a provider "split") and three crashes; all were fixed in general code |
| Full model time | 45 minutes or less, cold | **KO 10.3 min, AAPL 5.9 min**, per agent in the step 4 table |
| Cached answer time | under 60 s | **0.5 s median, 0.6 s longest**, 12 of 12 cited |
| Model comparison | each role scored and the choice recorded | forecast 4b, comps 1.7b (changed), critic 4b; numbers in step 5 and reasons in `config/models.yaml` |
| Earlier checks | unchanged | 734 tests pass; ruff, format and mypy clean |

**Known limits:**
- **v1 scope** excludes financials, REITs, regulated utilities and pre-revenue companies. They stop at Gate 0 or at normalization with the reason; Phase 7 holds their methods.
- **Corporate actions:**
  - A spin-off or special distribution after the latest SEC share count stops the valuation until the next filing.
  - A new holding-company registrant (XOM) has thin company facts: comps fill the target from the valuation's own data.
- **Repeatability:** the forecast model samples (temperature 0.3), so a cold run can pick another level (KO 48.49 against 50.56). Cached replies make reruns repeatable, and Gate 2 shows every pick.
- **Small models:**
  - Comps keep/drop agrees with the hand labels about two-thirds of the time, and drops good peers more often than it keeps bad ones; the fill-to-five rule offsets this.
  - The 4b rubric catches 1 of 3 seeded story flaws (the 8b 3 of 3, but too slow for this CPU).
- **Values below the price** remain common (median golden gap about -45%), by the Phase 4 policy (risk-free rate, betas, no anchoring on the price). Large gaps carry the Critic's warnings and the story's explanation.
- **Machine sleep** pauses a batch: on Windows the time limit keeps counting while the computer sleeps. Run long batches with sleep disabled.
