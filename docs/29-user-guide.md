# 29. User guide

The DCF agent values US non-financial companies from their SEC filings, on your own computer. It shows you each important choice before using it. Every number in its answers, workbook and report has a source you can check.

## Start

```powershell
ollama serve                                   # the local models (once per boot)
uv run python -m dcf_agent.cli chat            # the chat
uv run python -m dcf_agent.cli chat --session <id>   # resume a saved session
```

`.env` must contain `SEC_USER_AGENT` (your name and email, required by the SEC) and `FRED_API_KEY`. A company's data is downloaded the first time, then cached.

## A session

1. **Name a company and a date.** For example "Coca-Cola, 2026-10-11", or "KO, 2026-10-11, what is the cost of capital?". The date is the valuation date: nothing filed after it is used.
2. **Gate 0, confirm.** The company, date, industry and scope are shown. Press Enter to confirm, or type:
   - `ticker XYZ` for another company, `date YYYY-MM-DD` for another date;
   - `industry <label>` to override the industry;
   - `autopilot` (see below), or `stop`.
   Without a question, choose what you want: 1 a question, 2 a quick value, 3 the full model with the workbook and the report.
3. **Gate 1, adjustments.** One-off items found in the filings (restructuring charges, gains on sales) are proposed with the filing's own words. Approve, reject or edit each; small, confident ones are approved by policy and listed.
4. **Gate 2, assumptions.** Three scenarios (bear, base, bull) with their growth, margins, capital needs, terminal assumptions, values and stories. Press Enter to approve, or type:
   - `edit base margin 2028 25%`; add `! reason` to override a hard bound;
   - `why <question>`, `rethink bear <note>`, or `stop`.
5. **Valuation, comps and review.** The engine values the scenarios. Comps compare the value with peers' multiples. The Critic checks the result and fixes what it can, at most twice; the rest is listed for you.
6. **Gate 3, sign-off** (full model only). The value, the weights, the football field as text bars, the main drivers and the Critic's report. Press Enter to approve and export, or type `back <note>` (to Gate 2) or `stop`.
7. **The answer**, with numbered sources. Ask follow-up questions in the same session ("change the base margin in 2028 to 25%", "why this WACC?"); only what depends on a change is rerun.

**Autopilot** (type `autopilot` at Gate 0): Gates 1 to 3 approve themselves. Every item approved this way is listed in the report's "Unreviewed items" section. Use it for a first look, never instead of reading the gates.

## Commands

| Command | What it does |
|---|---|
| `/sources` | the sources of the last answer (filing, accession, period, URL) |
| `/state` | the session: company, date, steps done, decisions, budget |
| `/assumptions` | the current scenarios and their values |
| `/report` | the exported report files |
| `/trace` | writes the session's trace page (steps, model calls, approvals) to `outputs/traces/` |
| `/resume [id]` | list sessions, or switch to one |
| `/new`, `/help`, `/quit` | start over, help, leave (the session is saved) |

## Outputs

An approved full model is exported to `outputs/reports/<ticker>-<date>/`:

| File | Contents |
|---|---|
| `memo.html`, `memo.md` | The memo: summary, football field, scenarios, drivers, sensitivity, cost of capital, comps, adjustments, review and risks, sources |
| `model.xlsx` | The workbook. Cover first; every calculation is a formula over the blue inputs on Inputs; Checks compares the workbook with the engine and lists the Critic's issues; Sources lists each input's source |
| `football-field.svg` | The value ranges against the price |
| `summary.json` | The summary as data |
| `audit-log.json` | Every step, model call and approval of the session |
| `provenance-audit.json` | The audit: how many values were checked, by source, and any problem |

## Reading the results

- **The value is a range.** Bear, base and bull are weighted by probability. The football field puts them next to comps and the 52-week price.
- **Below the price is common** and is not an error: the risk-free rate and the betas are high in 2026, and the forecast is not anchored on the price. The memo says when a gap is large and whether the story explains it.
- **Comps are a cross-check** and are never blended into the value. Few peers or unusual peers are flagged.
- **The Critic's issues** are graded:
  - high: sent back to be fixed, or left only if it can't be fixed in the session;
  - medium: a warning to read;
  - low: a note.
- **A negative value per share** means claims exceed the operating value in that scenario. A share can't be worth less than zero; read it as a warning about the margins or the debt.

## When something fails

The chat says what happened and what to try. The main cases:

| Case | What to try |
|---|---|
| Company not resolved | Check the ticker and the date, or pick one of the candidates |
| Out of scope | Banks, insurers, REITs, regulated utilities and companies without a 10-K are outside v1 |
| Missing statements | The filings lack a statement the model needs; see the data warnings |
| Engine guardrail | The scenarios break a rule (for example WACC at or below growth); edit them at Gate 2 |
| Time limit | Rerun the company alone |

## Limits

- **v1 scope:** US filers in USD, not financial companies, REITs or regulated utilities (electric, gas distribution, water: their allowed returns near the cost of capital make an FCFF DCF unreliable). Loss-making companies run, with a known limit on survival.
- **Prices** come from Yahoo Finance or Stooq, which are unofficial sources.
- **The local models are small.** Their choices are checked by code, and the stories they write are labelled as theirs.
- **Not investment advice.**
