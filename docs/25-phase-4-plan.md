# 25. Phase 4 plan: Forecast Agent (Tree-of-Thought), Gate 2, end-to-end value

Written on 2026-10-17, after Phase 3a ([24-phase-3a-plan.md](24-phase-3a-plan.md)).
Phase 4 turns the system from "data and cost of capital" into a valuation:

- a Forecast Agent proposes bear, base and bull scenarios;
- the user reviews and edits them at Gate 2;
- the Phase 1 engine values them;
- the chat answers value, scenario, sensitivity and "why" questions.

As before, everything must work for any US non-financial company, every number
keeps a source, and the model chooses while code calculates.

## What Phase 4 delivers

| Need | Today | After Phase 4 |
|---|---|---|
| Forecast assumptions | Typed by hand in a YAML file (`examples/assumptions/`) for `cli value` | Proposed by the Forecast Agent from the company's normalized history, industry tables and guidance, with a rationale per assumption |
| Valuation in the graph | `value` is recorded as "not available in this phase" | Quick value (I6) end to end: forecast, Gate 2, DCF per scenario, weighted value, sensitivity |
| Human review of assumptions | None (the YAML is the review) | Gate 2: scenario table, WACC build-up, narrative, evidence, approve / edit a cell / ask why / re-think a scenario |
| Questions | I1, I2, I3, I10 | Also I4 (why an assumption), I5 (scenarios), I6 (value), I7 (sensitivity), I9 (full model with the Excel workbook), I11 (change an assumption) |
| Reruns | Whole steps | An edit reruns only what depends on it (invalidation graph) |

Already built and reused: the DCF engine (stub period, mid-year, terminal value,
guardrails, dilution with options, units and convertibles), scenario weighting,
the WACC x growth sensitivity grid, the Excel workbook with LibreOffice parity,
normalized history with approved adjustments (Gate 1), the WACC Agent, filing
insights, and the graph, checkpoints and chat.

## Design

```text
normalized history + industry tables + guidance --> driver statistics (code)
   --> axes and levels with evidence (model, constrained choices)
   --> branch grid, e.g. 3 x 3 worlds (code: values from history, tables, fades)
   --> deterministic scores (identities, bounds, plausibility) + one rubric call on the best 4
   --> 3 scenarios (bear/base/bull) with probabilities; 3 short stories (model)
   --> year-by-year drivers with fades to terminal values (code) --> preview value (engine)
   --> Gate 2 (approve, edit, ask why, re-think) --> DCF per scenario --> answer
```

**1. Driver statistics (code).** From `NormalizedFinancials` (up to five years
and the TTM):
- revenue growth (CAGR, trend, volatility);
- clean operating margin (with approved Gate 1 adjustments);
- sales-to-capital and ROIC;
- reinvestment rate;
- effective against marginal tax.

Plus Damodaran's industry margin and sales-to-capital tables (already collected)
for the company's industry: medians and percentiles where the file gives them.

**2. Guidance (filings).** The 8-K earnings releases (Exhibit 99.1), indexed
point in time (deferred from Phase 3a step 1), and the 10-K outlook text: stated
guidance on revenue growth, margins and capital spending, read as `FilingItem`s
with citations. When there is none, the record says so; the agent never invents
guidance.

**3. Axes and levels (model, choose, don't invent).** One constrained JSON call:
for each axis the model picks a level from a fixed enum and cites one line of
evidence (a statistic, a table or a guidance item):
- growth durability: {fades fast, fades normally, durable};
- margin trajectory: {compresses, holds, expands};
- reinvestment efficiency: {below history, history, industry};
- moat (terminal ROIC): {none, narrow, wide}.

Numbers never come from the model.

**4. Branch grid and scores (code).** Each combination of levels is a world:
- **Driver paths from code:** each level maps to driver paths built from the history, the industry tables and fade rules (`fade_path`: linear or convex over the horizon to terminal values).
- **Deterministic scoring:**
  - growth must be paid for: reinvestment = Δrevenue / sales-to-capital;
  - the ROIC path stays within [WACC, ceiling] unless the moat level says otherwise;
  - margins are judged against the company's history and the industry percentiles;
  - growth above 25% a year for more than five years is flagged;
  - the terminal value's share of EV is flagged above 75%.
- **One rubric call** (1 to 5, story against numbers) on the best four only.

**5. Scenarios.**
- **Bear, base and bull:** the three best distinct branches, ordered by preview value.
- **Probabilities:** 25/50/25 by default, editable at Gate 2.
- **Stories:** three short model calls, three sentences each.

**6. Expansion (code).** Year-by-year drivers over the horizon:
- **Tax:** converges to the marginal rate (the filing's rate from Phase 3a, else policy).
- **Terminal growth:** at most the risk-free rate and 4%.
- **Terminal ROIC:** follows the moat level.
- **Terminal reinvestment:** g / ROIC (the engine enforces it).
- **Provenance:** every driver is `Sourced`: `derived` from cited statistics and tables, with the model's level choice in the rationale.

**7. Preview and anti-anchoring.**
- **Preview:** `quick_dcf` values each scenario.
- **No tuning toward the price:** the market price is not an input to any choice.
- **Far from the market:** if the base value is far from it (default: more than 50% away), the agent must explain why in the narrative rather than re-tune; the explanation is shown at Gate 2.

**8. Gate 2 (graph interrupt).** Shows:
- the scenario table (growth, margin, sales-to-capital, tax, terminal growth and ROIC, probability);
- the WACC build-up;
- the stories;
- the evidence links;
- the preview values;
- warnings.

The user's actions:
- **Approve.**
- **Edit any cell**, checked against the bounds; an out-of-bounds edit needs an explicit "I know" override with a reason, stored in the `ApprovalRecord`.
- **Ask why** (I4): the rationale and evidence of one assumption.
- **Re-think a scenario:** re-run axes 3 to 6 for that scenario with the user's note.

**9. Valuation and answers.**
- **Values:** `run_dcf` per approved scenario, the probability-weighted value, and the sensitivity grid; for I9, the Excel workbook with parity.
- **Answers:** cited templates, as in Phase 2: value per share and range (I6), scenario drivers and values (I5), "what if X changes" (I7, by rerunning the engine), and why (I4).

**10. Invalidation graph (I11).** A declared dependency map:
- an assumption feeds the scenario, which feeds the values and the answer;
- a WACC input feeds the WACC, which feeds the values and the answer;
- a Gate 1 adjustment feeds normalized history, which feeds the statistics, the forecast and the values.

An edit marks only its dependents stale; the next run executes those nodes only.

## Acceptance criteria

Adapted from the roadmap for qwen3:4b on this CPU.

| Criterion | Target | Measured by |
|---|---|---|
| Hard bounds | 100% of generated assumption sets pass (terminal growth at most Rf and 4%, tax converges to marginal, terminal reinvestment identity, provenance on every driver) | Deterministic checker on the golden set |
| Soft bounds | at least 90% of generated sets pass the soft checks without edits (ROIC vs WACC, margin vs industry percentiles, growth duration, TV share) | Same checker |
| Scenario spread | Bull at least 15% above base and bear at least 15% below base in value; probabilities sum to 1 | Engine preview |
| Anti-anchoring | Correlation of base value with market price across the golden set reported (not suspiciously close to 1); 5 cases reviewed by you | Report and your review |
| Incremental reruns | Editing one assumption reruns only its dependents | Test on the invalidation graph |
| End to end | Quick value (I6) for every golden company that readiness does not block, with Gate 2 and cited answers | Scripted sessions and a live walkthrough |
| Phase 1 to 3 checks | Unchanged | Existing checks and tests |
| Latency | Quick value at most 30 minutes from cache on this CPU (the roadmap's full model target is 45 minutes) | Timing |

**Golden set:**
- the WACC reference cohort: Apple, Coca-Cola, Nike, Microsoft, Nvidia, Caterpillar, Deere, Boeing, Snowflake, McDonald's, and Monster when cached;
- a development and test split of the wider cohort for tuning the scorer's thresholds, as in Phase 3a.

## Work breakdown

| Step | Work | Exit check |
|---|---|---|
| 1. Driver statistics and checker | `historical_driver_stats`, industry benchmarks from the Damodaran tables, `fade_path`, horizon from the fiscal calendar, the hard and soft bounds checker (`check_coherence`), `quick_dcf` | Tests on synthetic histories; statistics on the golden set look sane (hand check of 3); the YAML scenarios in `examples/` pass the checker |
| 2. Guidance | Index 8-K Exhibit 99.1 point in time; read guidance statements (revenue growth, margin, capex) with citations; "no guidance" recorded | Guidance found and cited for the golden companies that give it; no invented guidance on 3 that do not |
| 3. Forecast Agent | Axes and levels (one constrained call), branch grid, deterministic scores, rubric call, pruning to bear/base/bull, stories, expansion, preview, anti-anchoring rule | Hard bounds 100%, soft at least 90%, spread met on the golden set; model calls and time per company recorded |
| 4. Valuation in the graph | `forecast` and `value` steps: DCF per scenario, weighted value, sensitivity, Excel for I9; answers I5, I6, I7 | Quick value end to end on the golden set (offline); Excel parity unchanged |
| 5. Gate 2 | Interrupt with the scenario table and evidence; approve, edit with bounds and override, ask why (I4), re-think; chat rendering | Scripted session tests (approve, edit in and out of bounds, override recorded, re-think, resume at Gate 2) |
| 6. Invalidation graph | Dependency map, stale marking, partial reruns; I11 "change X to Y" | Tests: an edit reruns only dependents; answers update |
| 7. Interface | Terminal Gate 2 is built in step 5; Chainlit (web UI with an editable table) if you choose it (see decisions) | Live walkthrough |
| 8. Evaluation and docs | Acceptance table, anti-anchoring report and your review of 5 companies, latency, known limits | Acceptance table filled in |

Suggested order: 1, 2, 3, 4, 5, 6, 7, 8. Steps 1 to 4 already give a quick value
without Gate 2 (behind a flag, for testing); Gate 2 then becomes the default.

## Dependencies

| Package | Use | Group |
|---|---|---|
| None new for steps 1 to 6 | The engine, graph, chat and model wrapper exist | |
| `chainlit` | Web UI for Gate 2 (only if chosen) | Runtime, optional |

## Risks

| Risk | Mitigation |
|---|---|
| A small model choosing levels badly (as it labelled everything one-off in Phase 3a) | Levels are enums with cited evidence checked by code; scoring is deterministic; Gate 2 reviews everything |
| Anchoring on recent growth or on the market price | Fades to terminal values by rule; price not an input; the anti-anchoring rule and report |
| Implausible combinations (high growth, low reinvestment) | The growth-reinvestment identity and the ROIC path in the scorer |
| Companies with short or odd histories (loss makers, recent IPOs) | Industry tables weigh more when history is short; flagged at Gate 2; loss makers get a margin path toward the industry median with a stated horizon |
| CPU time | Few, short model calls (about 6 per company); everything else is code; responses cached |
| Editable table in a terminal | Cell edits by command ("base margin 2028 = 25%"); Chainlit if you want a grid |

## Decisions with defaults (change if you prefer)

| Decision | Default |
|---|---|
| Horizon | 10 years: explicit drivers fading to terminal values from year 6 |
| Axes | Growth durability x margin trajectory (3 x 3 = 9 worlds), with reinvestment and moat chosen once per company |
| Probabilities | 25/50/25 (bear/base/bull), editable at Gate 2 |
| Terminal growth | Base min(risk-free rate, 3%); bear 0.5 points lower; bull 0.5 points higher within the hard bound |
| Terminal ROIC by moat | None: terminal WACC; narrow: WACC + 2 points; wide: WACC + 5 points (needs evidence) |
| Anti-anchoring threshold | Base value more than 50% from the market price needs an explanation |
| Gate 2 interface | Terminal chat first; Chainlit afterwards if you want it |
| Debate (bull and bear advocates) | Off (about 3x the CPU time) |
| Reader model | qwen3:4b (`forecast` role, already configured) |

## Carried over from Phase 3a

- Optional: about 30 test-split extraction questions (about 40 minutes) to confirm the 96% development result on unseen companies.
- The narrative gold set review (`evals/rag/narrative-review.md`) is useful when narrative answers exist; step 2's guidance reading is the first consumer.
- 8-K Exhibit 99.1 indexing (deferred in Phase 3a step 1) is step 2 here.

## Progress

### Step 1: driver statistics and checker (done, 2026-10-17)

`src/dcf_agent/engine/drivers.py`, all code:

| Tool | What it does |
|---|---|
| `driver_stats` | From `NormalizedFinancials`: revenue growth, clean operating margin, sales-to-capital, ROIC, reinvestment rate, effective tax by fiscal year, each with mean, median, latest, least-squares trend per year, volatility and range; revenue CAGR; TTM margin and growth; a note when the history is short |
| `industry_benchmarks` | The company's Damodaran industry row already in the bundle: lease-adjusted operating margin (comparable with clean EBIT, which includes the lease interest component) and sales to invested capital, each sourced. The files give industry averages only, no percentiles |
| `fade_path` | A driver held for some years, then faded linearly or convexly (most of the way early) to its terminal value |
| `forecast_years` | The next fiscal year ends after the valuation date on the company's calendar (the CLI now uses it) |
| `check_coherence` | Hard bounds (the assumption set cannot be valued): terminal growth at most the risk-free rate and 4%, below the terminal WACC and the terminal ROIC; tax converges to the marginal rate; positive sales-to-capital; every driver sourced. Soft flags for Gate 2: terminal ROIC below the terminal WACC, or more than 5 points above it without a moat rationale; growth above 25% for more than five years; a peak margin far above the industry (more than twice its average and 15 points above it) and above the company's own history; WACC outside 5-15%; terminal value above 75% of EV |
| `quick_dcf` | The engine's value per share for one scenario; a guardrail failure is returned, not raised |

**Exit check** (`scripts/check_drivers.py`, the golden set offline):

| | Years | CAGR | Clean margin now (5-year range) | Sales/IC | ROIC | Industry margin, S/IC |
|---|---|---|---|---|---|---|
| Apple | 5 | 3.3% | 32.1% (29.9-32.1%) | 20.2 | 515% | 22.5%, 3.62 |
| Coca-Cola | 5 | 5.5% | 28.8% (21.3-28.8%) | 0.78 | 16.1% | 20.6%, 1.54 |
| Nike | 5 | -0.2% | 8.4% (8.2-14.5%), falling | 3.13 | 32.5% | 9.1%, 2.62 |
| Nvidia | 5 | 68.6% | 60.4% (15.8-62.5%) | 2.51 | 120% | 35.3%, 1.21 |
| Snowflake | 5 | 40.0% | -30.0% | 6.51 | negative | 3.7%, 1.35 |

Hand check (Coca-Cola, Nike, Snowflake) against their reports: consistent.

Two quirks the forecast must handle:
- Apple's invested capital is tiny after buybacks (ROIC 515%, sales-to-capital 20), so the industry is the better anchor for its reinvestment.
- Deere's capital includes the finance arm.

The hand-written Coca-Cola scenarios pass every hard bound. They are flagged soft for terminal value at 79-86% of EV and for terminal ROIC more than 5 points above WACC without a moat rationale, which is what the checker is for. Tests: `tests/test_drivers.py` (4). 654 tests pass.

### Step 2: guidance from earnings releases (done, 2026-10-17)

`src/dcf_agent/rag/releases.py`, code only (no model):

| Part | Implementation |
|---|---|
| Which releases | The 8-Ks filed by the valuation date with item 2.02 (results of operations), from the filing list (`Filing.items`, new); the latest two |
| Which document | The exhibit typed EX-99 on the filing's own index page (file names vary: Coca-Cola's is `a2026q2earningsreleaseex-9.htm`) |
| Tables | Tables that say "guidance" or "outlook", or are headed by it just above; a metric row is read in its first value column (the current guidance when current and prior are shown). Tables that compare reported periods ("% change", "compared to") are skipped |
| Sentences | A forward-looking verb form ("expects", "is expected to", "raising", "reaffirms"; not the adjective in "expected tariff costs"), a forward period (full year, fiscal year, outlook) and a parsed number; sentences are split within each block, never across a table |
| Numbers | Ranges ("7% to 8%", "$2.0 to $2.2 billion"), single values, "low/mid/high-NN percent" (mid-20 = 24-26%), and a bare number in a margin or tax row |
| Output | `FilingItem`s in `FilingInsights.management_guidance`: the release's own words, the midpoint as a sourced amount (the exhibit URL and accession), the citation; the latest release supersedes older statements of a metric. Read in `DataTools.get_filing_insights`; without the releases (offline, not cached) a note is recorded |

**Exit check** (`scripts/check_guidance.py`, golden set, releases downloaded once then cached):

| Company | Guidance read (latest release) |
|---|---|
| Coca-Cola | organic revenue about 5% growth; underlying tax rate 19.9%; comparable currency-neutral EPS 7-8% growth; free cash flow about $12.4B |
| Nike | effective tax rate in the mid-20 percent range (24-26%) for fiscal 2027 |
| Nvidia | fiscal 2027 tax rate 16-18%; next-quarter gross margin about 74% |
| Snowflake | full-year product revenue $6,070M (36% growth), non-GAAP operating margin 14.5% (both raised) |
| Apple, Boeing, Caterpillar, Deere, McDonald's, Microsoft | none in their latest two releases (correct: they do not give full-year guidance there, or give it outside the metrics read, such as Deere's net income forecast) |

All 18 items are verbatim in their releases; none is invented on the six
without guidance. Found while calibrating:
- Deere's segment results tables (quarter against prior-year quarter) and Caterpillar's "included ... expected tariff costs" were read as guidance; both are now excluded by rule.
- Table text ran into the next paragraph; tables are now removed from the prose once read.

Tests: `tests/test_releases.py` (3). 657 tests pass.

Not read yet:
- per-share ranges (EPS in dollars);
- net income forecasts;
- guidance given only in the 10-K's MD&A text.

### Step 3: Forecast Agent (done, 2026-10-17)

`src/dcf_agent/agents/forecast.py`: two model calls per company (qwen3:4b,
`forecast` role), everything else code.

| Part | Implementation |
|---|---|
| Anchors | Starting growth: revenue or organic guidance, otherwise half the latest growth and half the CAGR. Starting margin: clean operating margin (TTM, else latest). Sales-to-capital: the company's median kept between the industry's and 2.5x it (Coca-Cola's 0.78 includes acquired goodwill and Apple's 20 reflects almost no invested capital, so neither is the capital new growth needs). Tax: guidance or median effective rate, fading to the marginal rate. Terminal growth: min(risk-free, 3%) |
| Rule levels | From the statistics: growth fades fast when recent growth is 2 points below the CAGR, is durable when 2 points above it with low volatility; margin compresses or expands with its trend (more than 0.5 points a year); moat from ROIC against WACC (none, narrow above 5 points, wide above 15) |
| Model levels | One constrained call: a level per axis (growth durability, margin trajectory, reinvestment, moat) with evidence in under 20 words. A level that departs from the rule is kept only when the statistics support it (for example "compresses" needs a falling trend, a margin 10 points above the industry, or lower guidance); otherwise the rule applies and the rejection is listed for Gate 2 (the WACC Agent's approach) |
| Worlds | 3 x 3 (growth x margin): growth held 0, 2 or 4 years then a convex or linear fade (always convex above 25%); margins move to a target in five years (ten for loss makers) set against the industry and the company's own history; worlds failing a hard bound are dropped; each is previewed with the engine |
| Scenarios | Base: the chosen world. Bear and bull: one level down and up on both axes, and at the edge of the grid one step beyond it (a further 3-point margin step and 2 points of starting growth, the bull's margin step staying near the company's best year). Bear also needs 15% more capital per dollar of growth and has one moat level lower and 0.5 points lower terminal growth; bull the reverse within the hard bounds. Probabilities 25/50/25. Selection is structural: values never choose a scenario, and the market price is never an input |
| Stories | One call after the numbers are fixed; the market price is shown only then, to explain a gap above 50%. A story that only echoes labels or is too short is replaced by one written from the scenario's own rationale (qwen3:4b mostly echoes) |
| Evidence | Every driver is `Sourced` (`derived`, from the normalized history, the industry table and guidance) with a rationale per assumption naming its rule and the model's evidence |

**Exit check** (`scripts/check_forecast.py`, golden set, with the model):

| Company | Levels (after the support rule) | Bear / base / bull per share | Price | Soft clean | Spread | Rejected picks |
|---|---|---|---|---|---|---|
| Apple | durable, expands, moat wide | 132.02 / 149.78 / 170.15 | 336.64 | yes | no | 1 |
| Boeing | fades fast, expands, moat none | -16.22 / 65.81 / 146.35 | 190.37 | yes | yes | 2 |
| Caterpillar | durable, holds, moat narrow | 301.91 / 403.24 / 594.34 | 800.47 | yes | yes | 1 |
| Deere | fades fast, compresses, moat narrow | 237.06 / 322.81 / 427.30 | 620.93 | yes | yes | 0 |
| Coca-Cola | fades fast, compresses, moat narrow | 28.16 / 38.54 / 52.28 | 88.05 | yes | yes | 0 |
| McDonald's | fades normally, holds, moat narrow | 109.35 / 169.69 / 203.50 | 235.23 | yes | yes | 0 |
| Microsoft | durable, expands, moat wide | 329.04 / 401.12 / 461.88 | 535.07 | yes | yes | 0 |
| Nike | fades fast, compresses, moat narrow | 6.97 / 16.16 / 25.72 | 34.71 | yes | yes | 0 |
| Nvidia | fades fast, expands, moat wide | 222.25 / 267.29 / 371.96 | 229.28 | yes | yes | 0 |
| Snowflake | fades fast, expands, moat none | -8.48 / -1.34 / 5.90 | 368.89 | no | no | 2 |

- **Hard bounds:** 10/10.
- **Soft checks:** 9/10 sets clean (90%). Snowflake's bull terminal value is 767% of EV, because a loss maker's explicit years are negative.
- **Spread:** 8/10. Apple's bull cannot step past a wide moat (+14%); Snowflake is negative.
- **Model calls:** 20 (about 1 to 2 minutes per company on this CPU, the machine's sleep excluded).

Found while calibrating (each fixed by rule, not by tuning to prices):
- the model is pessimistic, as in Phase 3a (fades fast and compresses for most companies, also against the statistics), which led to the support rule;
- the output cap (300 tokens) cut the model's JSON, so the forecast role now has 512;
- picking bear and bull by value let a corner base end above its bear, so selection is now structural;
- acquired goodwill made reinvestment far too heavy for Coca-Cola.

**Anchoring, for step 8:** the base value is below the market price for 9 of
10 companies (all but Nvidia), often by 30-60%. Nothing was tuned toward prices,
so this measures the policy, not anchoring. Likely causes, to review at Step 8
before any change:
- WACCs of 9-12% (Damodaran betas and ERP);
- terminal returns only 0-5 points above WACC;
- the model's pessimistic levels where the statistics allow them;
- a loss maker's mature margin, which the industry average cannot give.

Tests: `tests/test_forecast.py` (5). 662 tests pass.

### Step 4: valuation in the graph (done, 2026-10-17)

| Part | Implementation |
|---|---|
| Plans | I4 (why): data, normalize, wacc, forecast, answer. I5 (scenarios), I6 (value), I7 (sensitivity): the same plus value. I9 (full model): plus the workbook |
| `forecast` step | `LiveServices.forecast`: the Forecast Agent on the normalized history (with the Gate 1 adjustments; without them if normalization did not run); its notes, including the model picks the statistics did not support, become warnings for Gate 2 |
| `value` step | `engine/valuation.py`: DCF per scenario, probability-weighted value, bear-bull range, base WACC x terminal growth grid (5 x 5); one validated `Valuation` record in the checkpoint; the market price only as context |
| `excel` step | The Phase 1 workbook, written to `outputs/models/<ticker>-<date>.xlsx`. Each scenario sheet now carries the engine's diluted share count and any converted convertible principal as labelled inputs: dilution is a fixed point (options by the treasury stock method at the value per share), not a sheet formula |
| Answers | I6: weighted value, range, each scenario's value with growth in year 1, margin in year 5 and terminal growth, the price for context, the items for review, the workbook. I5: drivers in years 1, 5 and 10 and the story per scenario. I7: the sensitivity grid. I4: the rationale and evidence of the named assumption (or all), with the model picks that were overruled. Every figure cited; engine values cite their calculation reference and input hash |
| Follow-ups | A session's forecast and valuation are reused by later questions |

**Exit check** (`scripts/check_value.py`): the graph, offline, with the live services and the local model.

For each company the script answers:
- Gate 0: confirm, quick value;
- Gate 1: approve all.

| Company | Weighted value per share (range) | Price | Time |
|---|---|---|---|
| Apple | 150.43 (132.02-170.15) | 336.64 | 15 s |
| Boeing | -7.51 (-49.58-51.80) | 190.37 | 122 s |
| Caterpillar | 425.68 (301.91-594.34) | 800.47 | 181 s |
| Deere | 327.49 (237.06-427.30) | 620.93 | 174 s |
| Coca-Cola | 47.83 (36.09-58.33) | 88.05 | 29 s |
| McDonald's | 171.53 (117.52-203.50) | 235.23 | 537 s (first normalizer readings) |
| Microsoft | 398.29 (329.04-461.88) | 535.07 | 24 s |
| Nike | 15.73 (6.61-25.06) | 34.71 | 105 s |
| Nvidia | 278.74 (219.23-367.76) | 229.28 | 145 s |
| Snowflake | 22.95 (7.16-40.10) | 368.89 | 207 s |

Results:
- All 10 valued, with cited answers.
- Coca-Cola's full model (I9): the workbook recalculated by LibreOffice matches the engine on 8/8 checks (values per share, enterprise values, weighted value, WACC).
- Time: at most 9 minutes per company, within the 30-minute target.

Boeing is negative once Gate 1 removes its $9.6B disposal gain. The values remain below the price for 9 of 10 companies (Step 3's finding, for Step 8).

Tests: `tests/test_valuation_graph.py` (6, on a saved reference bundle, skipped without it). 668 tests pass.
