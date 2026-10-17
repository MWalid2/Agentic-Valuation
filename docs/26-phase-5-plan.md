# 26. Phase 5 plan: Comps, Critic, Reporter, Gate 3

Written on 2026-10-17, after Phase 4 ([25-phase-4-plan.md](25-phase-4-plan.md)).
Phase 4 values a company end to end; Phase 5 checks the valuation and
delivers it:

- relative valuation as a cross-check (Comps Agent);
- an adversarial review that can send work back (Critic Agent, at most two loops);
- the final outputs (Reporter: quick summary, memo, full workbook, football-field chart);
- a sign-off gate before export (Gate 3).

Same rules as before: any US non-financial company, every number sourced
(the provenance audit must stay clean), the model chooses and code calculates.

## What Phase 5 delivers

| Need | Today | After Phase 5 |
|---|---|---|
| Relative valuation | None | Peers from the company's Damodaran industry and a size band, kept or dropped by the model with a reason; EV/EBITDA, EV/Sales, EV/EBIT, P/E on TTM figures from the peers' SEC filings and prices point in time; outliers removed; implied range; divergence from the DCF |
| Review of the valuation | Gates 1 and 2 (human), bounds checker (code) | Critic: deterministic checks over the whole state (terminal value share, terminal growth and ROIC, reinvestment identity, margin steps, tax, DCF vs comps and vs price, bridge vs filings, Excel parity), a rubric from a separate model, issues routed back to the Forecast, WACC or Normalization step with a suggested fix; at most two loops, then the remaining issues go to Gate 3 |
| Outputs | Cited chat answers; the Phase 1 workbook | Quick summary (range, base vs price, three drivers, three risks, sensitivity snapshot); a two-to-four-page memo (Markdown and HTML), numbers injected from the engine; the workbook with Cover and Comps sheets and the Critic's flags; a football-field chart |
| Sign-off | None | Gate 3: value per share, scenario weights, football field, sensitivities, warnings; approve and export, or go back to Gate 2. Autopilot (optional) approves gates but lists every warning in an "Unreviewed items" section |

## Design

```text
approved valuation --> comps (peers, multiples, implied range)
   --> critic: deterministic checks + rubric --> issues by owner
         high-severity issue and loops < 2: back to forecast / wacc / normalize with the fix
   --> Gate 3 (approve and export, or back to Gate 2) --> reporter: summary, memo, workbook, chart
```

**1. Comps.**
- **Candidates (code):** companies in the same Damodaran industry (the `indname` table already used to classify the target), US-listed, non-financial, within a market-capitalization band (default 0.2x to 5x the target), capped at 10.
- **Keep or drop (model):** one call; for each candidate, keep or drop with a one-line reason, from its SEC business description (Item 1, the filing index).
- **Fundamentals (code):** TTM revenue, EBIT, EBITDA, net income, debt, cash and diluted shares from each peer's own SEC company facts filed by the valuation date, and its price on the valuation date. The same definitions as the target (operating income, not adjusted).
- **Multiples:** EV/EBITDA, EV/Sales, EV/EBIT, P/E; negative or undefined ones are omitted.
- **Outliers:** removed by the interquartile rule.
- **Summary:** quartiles and the implied per-share range from the target's own figures.
- **Warning:** fewer than five peers always carries one.

**2. Critic, deterministic.** The agent spec's table, checked on the full state:
- terminal value above 75% of EV;
- terminal growth above the risk-free rate or 4%;
- terminal ROIC below WACC, or more than 5 points above it without a moat rationale;
- reinvestment against growth;
- a margin step above 5 points in a year;
- growth above 25% beyond year 5;
- tax not converging to the marginal rate;
- DCF against comps (above 40%) and against the price (above 50%, which needs an explanation);
- debt, leases or cash differing from the filing by more than 2%;
- workbook parity (1 cent per share).

Each issue: severity, evidence (the cited records), suggested fix, owner.

**3. Critic, rubric and loops.**
- **Rubric:** one call of a separate model (qwen3:8b by default, the forecast uses qwen3:4b), scoring 1 to 5:
  - narrative-to-numbers consistency;
  - evidence quality;
  - scenario distinctness;
  - unsupported claims;
  - missing risks (from the 10-K's risk factors, Item 1A).
- **Loops:** a high-severity issue sends the state back to its owner with the suggested fix (for the forecast, as a Gate 2-style note or a code-applied edit). At most two loops; then the issues are warnings at Gate 3. The loop count is in the `CritiqueReport`.

**4. Reporter.**
- **Quick summary:**
  - value range and base value against the price;
  - three drivers (the largest sensitivities);
  - three risks (Critic issues and Item 1A);
  - a WACC x growth snapshot.
- **Memo:** a template, with the narrative sections from the model and every number injected from the engine (no number typed by the model).
- **Workbook:**
  - new Cover and Comps sheets;
  - the Critic's flags in Checks;
  - checks for formula count and "no hard-coded calculation cells" (key calculation cells contain formulas).
- **Football field:** an SVG drawn in code (DCF scenarios, comps range, 52-week price range), no new dependency.

**5. Gate 3 and export.**
- **Shown:** value per share, weights, football field, sensitivities, the Critic's report.
- **Actions:**
  - approve and export (memo, workbook, chart, audit log, in `outputs/reports/<ticker>-<date>/`);
  - go back to Gate 2;
  - stop.
- **Autopilot:** a session setting; gates approve themselves and every unreviewed warning is listed in the report.

## Acceptance criteria

| Criterion | Target | Measured by |
|---|---|---|
| Critic recall | at least 90% of 15 seeded flaws caught (terminal growth above Rf, ROIC below WACC, a wrong debt figure, a margin jump, tax not converging, TV share, broken reinvestment identity, comps divergence, parity break, ...) | Seeded-error tests |
| Critic loop | Terminates within the cap in 100% of runs | Tests and the golden run |
| Full model | For 3 golden companies: the workbook passes parity, formula-count and no-hardcode checks | LibreOffice recalculation |
| Comps | At least 5 peers after outliers for most golden companies; every multiple sourced; warnings otherwise | Golden run |
| Provenance | The audit stays clean, comps included | `engine/audit.py` |
| Earlier checks | Unchanged | Existing checks and tests |

## Work breakdown

| Step | Work | Exit check |
|---|---|---|
| 1. Comps tools | Candidates by industry and size, peer fundamentals from SEC company facts (point in time), prices, multiples, IQR outliers, implied range, divergence | Golden set: peers, multiples and ranges computed; two peer lists checked by hand |
| 2. Comps Agent | Keep or drop per candidate with a reason (one call), from business descriptions | No obviously wrong peer kept in the hand-checked lists |
| 3. Critic checks | The deterministic table as code, issues with evidence and owners | 15 seeded flaws: at least 90% caught |
| 4. Critic rubric and loops | Rubric call (separate model), routing back with a fix, two-loop cap | Loop terminates in 100% of runs; a seeded high-severity issue is fixed by its owner |
| 5. Reporter | Quick summary, memo template, workbook Cover/Comps/Checks, football-field SVG, workbook checks | 3 golden full runs pass parity, formula-count and no-hardcode |
| 6. Gate 3 and export | Sign-off, back to Gate 2, export folder, autopilot and unreviewed items | Scripted sessions; live walkthrough |
| 7. Evaluation and docs | Acceptance table, latency, known limits | Acceptance table filled in |

## Decisions with defaults (change if you prefer)

| Decision | Default |
|---|---|
| Peer source | Damodaran industry (`indname`), US, non-financial, market cap 0.2x-5x the target, at most 10 candidates; peers' SEC company facts downloaded once and cached (online the first time) |
| Multiples | EV/EBITDA, EV/Sales, EV/EBIT, P/E on TTM figures; comps are a cross-check, never blended into the DCF value |
| Outliers | Interquartile rule (1.5 x IQR) |
| Critic model | qwen3:8b (separate from the forecast's qwen3:4b); qwen3:4b if the 8b is too slow on this CPU. **Used: qwen3:4b** (the 8b timed out, step 4) |
| Loop cap | 2, as in the agent spec |
| Memo | Markdown and HTML; no PDF |
| Chart | SVG drawn in code (no matplotlib) |
| Autopilot | Off; per session when switched on |

## Dependencies

None new. SEC company facts for peers use the existing client (online the first
time, then cached).

## Risks

| Risk | Mitigation |
|---|---|
| Coarse peer lists | Damodaran's own industry assignment, a size band, the model's keep or drop with reasons, the sample size and dispersion always reported |
| Inconsistent peer accounting | Reported operating income for everyone (no peer adjustments); dispersion and outlier removal; comps only cross-check |
| A small model as critic | Deterministic checks carry the acceptance; the rubric adds, it never clears a deterministic issue |
| Loops that do not converge | Hard cap of two; remaining issues go to Gate 3 |
| Download volume for peers | At most 10 peers, cached; company facts only (no bulk datasets) |

## Progress

**Step 1, comps tools (done, 2026-10-17).** `agents/comps.py`, `scripts/check_comps.py`.
- **Candidates:** the target's Damodaran industry, screened by revenue from SEC frames. The size band widens (0.2-5x, then 0.05-20x, then any) until enough candidates are found; KO needed this.
- **Fundamentals:** each peer's TTM (FY + YTD - prior YTD) from its own company facts, point in time.
- **Enterprise value:** price x cover-page shares + debt - cash.
- **Statistics:** IQR outliers, inclusive quartiles.
- **Without the model:** all 10 golden companies have at least 5 peers, an implied range and a clean audit.

**Step 2, Comps Agent (done).** One qwen3:4b call keeps or drops each candidate with a reason.

| Company | Peers kept |
|---|---|
| AAPL | 2 of 10 (HPQ, HPE), with a small-sample warning. Xerox, Kodak, Diebold, Corsair, Immersion, NetApp and SMCI were dropped: the industry has no true peer for Apple |
| KO | 9 of 10 (PEP, KDP, MNST, COKE, ...) |
| NKE | 5 of 6 (DECK, CROX, SHOO, WWW, RCKY) |
| SNOW | 5 of 10 (TWLO, GDDY, AKAM, RXT, MDB); only EV/Sales is defined, since EBIT is negative |

No obviously wrong peer was kept. About 2 minutes per company on the first run (downloads), then cached.

**Step 3, Critic checks (done).** `agents/critic.py`: the deterministic table plus WACC, cost of equity and beta checks and the provenance audit. Each issue has a severity, evidence, a fix and an owner (forecast, wacc, data, reporter).
- **Calibration against the company's own history:**
  - **Reinvestment:** the implied return on new capital is flagged only above both 100% and 1.25x the company's best historical ROIC.
  - **Margin level:** flagged only when far above both the industry and the company's best year.
- **Clean golden set:** 9 of 10 companies have no issue. SNOW has one medium warning: in the bull case, terminal value is 795% of EV because the explicit years' cash flows are negative.
- **Seeded flaws (`tests/test_critic.py`):** 16 of 16 caught on KO (100%, target 90%):
  - terminal growth above Rf or 4%;
  - terminal ROIC below WACC, or high without a moat;
  - margin jump, margin level, late growth;
  - tax not converging; broken reinvestment;
  - TV share; comps and price divergence;
  - wrong debt; parity break;
  - WACC below Rf; a model-estimated value.

**Step 4, Critic rubric and loops (done).**
- **Graph:** the plan for a value (I6) or a full model (I9) is now value, comps, critic, (excel,) answer.
  - Comps and the Critic depend on the value, so a change (I11) reruns them too.
  - After a loop, comps reuses its peers; only the divergence moves.
- **Rubric:**
  - **What it judges:** the stories only. The numbers come from code and are checked already; when the prompt showed the policy formulas, the model misread the bear case's "-0.5%" shift as a terminal growth of -0.5%.
  - **Input:** the scenario stories with their numbers, the deterministic findings, and three risk-factor passages from the 10-K (Item 1A's preamble removed).
  - **Concerns:**
    - each must cite a story or a risk passage, or it is dropped;
    - they are low or medium only;
    - they are always owned by the forecast.
  - **What it can't do:** clear an issue or change `passed`.
- **Loops:**
  - **Mechanical fixes (code):** terminal growth capped at min(Rf, 4%), and the last tax rate set to marginal. Each is sourced to `dcf_agent.agents.critic` with a "Critic fix" note.
  - **Other forecast issues:** a re-think with the Critic's findings as the note.
  - **Workbook issues:** a rebuild.
  - **Issues for data, WACC or normalization:** their inputs don't change in a session, so they are not looped; they go to Gate 3 as warnings.
  - **Your Gate 2 overrides:** never sent back. A terminal growth above 4% that you set with a reason is a medium warning.
  - **Cap:** two loops, counted in the report.
- **Tests:**
  - a seeded terminal growth of 4.5% is fixed in one loop and the review then passes;
  - an issue that keeps coming back stops at the cap after three reviews and two re-thinks;
  - an issue for the data step is not looped;
  - a rubric concern without valid evidence is dropped.
- **Model:** qwen3:4b, not 8b.
  - qwen3:8b timed out (180 s) on the first prompt.
  - The 4b took 170 s on that prompt and 56 s on the shorter, stories-only one (1,773 to about 800 prompt tokens).
  - On KO it scored 5/5/5/5/4 with one fair concern, which led to a wording fix: the bull story read "moat wide (chosen narrow)"; it now says "one level above the base world's narrow, by scenario".

**Step 5, Reporter (done).** `report/summary.py`, `report/chart.py`, `report/memo.py`, `report/export.py`, `excel/checks.py`, `scripts/check_report.py`.
- **Quick summary:**
  - the value range and base value against the price;
  - three drivers, from a tornado on the base scenario run by the engine (growth +/-1 point, margin +/-1 point, sales to capital +/-10%, WACC +/-0.5 point, terminal growth +/-0.5 point, terminal ROIC +/-2 points; widest first);
  - three risks: up to two of the Critic's, then the 10-K's risk factors;
  - the WACC x growth grid.
- **Memo:** one structure rendered as Markdown and as HTML.
  - Every number is injected from the engine or the state. The only model text is the scenario stories approved at Gate 2, shown as such; no new model call.
  - Sections: summary, football field, scenarios, drivers, sensitivity, cost of capital, comps, Gate 1 adjustments, review and risks, unreviewed items, sources (the provenance audit's count), disclaimer.
- **Football field:** an SVG drawn in code, with the DCF scenarios (weighted value marked), comps, the widest driver, the 52-week price and the price line.
- **Workbook:**
  - **Cover** comes first. Its values are formulas over the model (weighted and scenario values, value against price, WACC, comps range); the price and the Critic's score are blue inputs.
  - **Comps:** the peers' multiples and the engine's quartiles as sourced inputs.
  - **Checks:** the Critic's issues and changes are added.
- **Structure check (`check_structure`):**
  - at least 12 formulas a year plus 14 per DCF sheet;
  - every key cell is a formula;
  - every number on Cover, WACC, Scenarios and the DCF sheets is a formula or a blue input (a number typed over a formula is caught, tested).
- **Exit check (AAPL, KO, NKE, full model through the graph):** parity 8/8, structure passed (445 formulas, none hard-coded) and audit clean, for all three.
- **Fixed on the way:**
  - **Bridge check:** it compared different definitions. Apple's bridge counts long-term marketable securities as cash, by policy; the comps read added only short-term investments. It now compares like for like:
    - cash against the filing's cash-and-equivalents tag, both ways;
    - debt only when the bridge holds less than the filing's tagged debt (the bridge adds current maturities, paper and leases);
    - only for the same period end. KO's and MCD's cached company facts were a quarter older than their balance sheets.
  - **Result:** clean on all 10 golden companies (most match to the dollar; CAT and DE skipped, captive finance). A wrong cash figure was added as a 17th seeded flaw.
  - **Fallback stories:** they were cut at a decimal point ("terminal WACC + 0.").
  - **Rubric:** it misread "fades to 3.5%" as a year-5 figure; the prompt now gives the terminal growth and the year it is reached.

**Step 6, Gate 3 and export (done).**
- **Gate 3** is the full model's "report" step, after the workbook. It shows:
  - values and weights, the ranges (as text bars in the chat), the drivers and the grid;
  - the Critic's score, issues and changes;
  - unreviewed items.
- **Actions at Gate 3:**
  - **Approve:** exports `outputs/reports/<ticker>-<date>/` with memo.md, memo.html, football-field.svg, summary.json, model.xlsx, audit-log.json (events and approvals) and provenance-audit.json.
  - **Back:** returns to Gate 2 with the note; valuation, comps, Critic, workbook and sign-off then rerun.
  - **Stop:** exports nothing.
- **Autopilot:** set at Gate 0 ("autopilot" in the chat).
  - Gates 1, 2 and 3 then answer themselves (continue, approve all, approve). Each approval is recorded with `autopilot` and actor "policy".
  - What each gate passed is collected and listed in the memo's "Unreviewed items (autopilot)" section.
- **The final answer** gives the Critic's score, the comps cross-check and the report folder.
- **Scripted sessions (`tests/test_gate3.py`):** approve and export, back then edit then sign off again, stop, autopilot end to end, chat commands.
- **Decision:** Gate 3 is in the full-model plan (I9) only. A quick value (I6) keeps its answer, with the Critic and comps lines, without a sign-off.
- **Fixed after the golden comps run** (`tests/test_comps.py`):
  - **Peer fill rule:** the model's keep or drop was too strict. Microsoft lost Adobe, Intuit and Workday; NVIDIA lost Qualcomm, TI and ADI, leaving 4 peers each. The model now also rates each candidate's fit (1-5). When fewer than five are kept, code restores the best-rated dropped candidates (fit 2 or more; never an unrelated one) up to five, each marked "restored" with a warning.
  - **Stale revenue tags:** peers' revenue came from a tag the company had stopped using. Twilio's and Akamai's "Revenues" ended in 2018, which gave SNOW an EV/Sales median of 51x. Each tag is now read separately, the most recent period wins, and figures older than about 18 months are treated as missing. This also corrected NVIDIA's peer P/E median (88x to 22x).

## Evaluation (step 7, 2026-10-17)

| Criterion | Target | Result |
|---|---|---|
| Critic recall | at least 90% of 15 seeded flaws | **17/17 (100%)** on KO (`tests/test_critic.py`) |
| Critic loop | terminates within the cap in 100% of runs | **100%**: a fixable flaw is fixed in one loop; an issue that keeps coming back stops at two loops; an issue for the data step is not looped (tests). Golden runs: 0 loops needed (13 runs) |
| Full model | 3 golden companies: parity, formula count, no hard-coded cell | **3/3** (AAPL, KO, NKE): parity 8/8 in LibreOffice, 445 formulas (at least 402), no hard-coded calculation cell |
| Comps | at least 5 peers for most golden companies; every multiple sourced; warnings otherwise | **8/10** with 5 or more peers. AAPL 4 and SNOW 3, both with warnings. Implied range for 10/10; audit clean 10/10 |
| Provenance | audit clean, comps included | **Clean**: 10/10 quick values, 3/3 full models, 10/10 comps |
| Earlier checks | unchanged | 718 tests pass. Phase 4 quick value on 10: valued 10/10, cited 10/10, audit clean 10/10, values identical to Phase 4 |

**Latency** (this CPU, after the first downloads):

| Run | Time |
|---|---|
| Quick value with comps and the Critic | 10-250 s a company (median about 60 s). Each new rubric call takes 25-75 s; a comps model call 30-100 s; cached calls take no time |
| Full model with Gate 3 and the export | 20-240 s a company. KO is slowest: its normalization reads more filings |
| First comps run for a company | 1-3 min (each peer's company facts and prices are downloaded once) |

**Known limits:**
- **Peer lists follow Damodaran's industry.** Apple's "Computers/Peripherals" holds no true peer; 4 remain (HPQ, HPE, SMCI, Xerox restored at fit 2), with a warning. SNOW's "Software (Internet)" candidates were rated unrelated by the model (MongoDB among them), leaving 3.
- **The comps model call can fail validation.** Microsoft's did, twice; all candidates are then kept, and it took 245 s.
- **The rubric is a 4b model.** Its concerns are cited and capped at medium, and it can't clear an issue, but some are weak. On NKE it raised a fair one (the bull case starts with lower year-1 growth than the base case) and a confused one (it read the story's terminal growth as year-5 growth, which the prompt now prevents).
- **The bridge check needs an independent read for the same period.** When the comps' cached company facts are a quarter older (KO, MCD), the check is skipped rather than compared.
- **Values below the price are by policy, not a bug** (Phase 4). The Critic flags gaps of more than 50% that the story doesn't explain, and DCF-to-comps gaps of more than 40% (NKE -68%, AAPL +56%), as medium warnings for Gate 3.
- **BA's value is negative (-7.51).** Losses and debt; the scenarios are shown at Gate 2 and Gate 3 for review.
