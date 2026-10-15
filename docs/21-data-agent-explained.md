# 21. The Data Agent, explained simply

This page explains what the Data Agent does, where its numbers come from, and
why they can be trusted. It is written for anyone who wants to understand the
data layer without reading the code. Technical detail lives in docs 12-20.

## 1. What the Data Agent is for

To value a company with a discounted cash flow (DCF), you need three kinds of
input:

1. **The company's own numbers**: revenue, profits, cash, debt, leases, cash
   flows, for several past years and the latest twelve months.
2. **Market numbers**: the share price and how it moves.
3. **Economy-wide and industry numbers**: interest rates, the equity risk
   premium, and industry benchmarks such as typical margins and betas.

The Data Agent collects all three for one company **as of a chosen date** and
hands them to the rest of the system as a single checked package, the
`DataBundle`. It does **not** value the company. Its job is to make sure the
inputs are correct, dated, sourced and honest about what is missing.

Three rules apply everywhere:

- **No look-ahead.** Only information published on or before the valuation date
  is used. A filing published the day after is invisible.
- **Every number has a source.** Each value carries where it came from (filing,
  line, accession number), when it was published, and how it was obtained.
- **Missing is never zero.** If something cannot be found or proven absent, it
  is reported as missing with a reason, never silently filled in.

## 2. The data sources

All sources are free and public.

| Source | What it provides | How it is used |
|---|---|---|
| **SEC EDGAR filings** (10-K annual and 10-Q quarterly reports) | The company's financial statements | Main source for all company numbers |
| **SEC XBRL packages** (machine-readable files inside each filing) | Every number in the statements, with its label, its place in the statement and how lines add up | Read directly for each filing: the core of the extraction |
| **SEC companyfacts** (SEC's summary of tagged facts) | Standard tagged values across all filings | Fallback when a filing's own package cannot be read |
| **SEC submissions and company list** | Which filings exist, when they were filed; ticker, CIK, industry code (SIC) | Choosing filings, point-in-time cutoff, scope check |
| **Yahoo Finance**, then **Stooq** as fallback | Daily share prices and stock splits | Market price and market capitalisation |
| **FRED** (Federal Reserve Bank of St. Louis) | 10-year Treasury yield, investment-grade credit spread, expected inflation, as known on the valuation date | Risk-free rate and market context for the cost of capital |
| **Damodaran datasets (NYU Stern)** | Industry betas, margins, sales-to-capital ratios, country risk premiums, rating spreads | Benchmarks for the cost of capital and forecasts |
| **Damodaran company file** (`indname.xlsx`) | Each listed company's Damodaran industry, ticker and SIC code | Choosing the company's industry automatically |
| **Local AI model** (Qwen3 on your own computer, via Ollama) | Choosing evidence and classifying unusual lines | Only for decisions rules cannot make; never invents numbers |

Every downloaded response is saved locally, so any run can be replayed offline
with exactly the same inputs. Filing files never change after they are filed,
so they are stored compressed (about 16 times smaller).

## 3. What the Data Agent does, step by step

1. **Find the company's filings.** Using the company's CIK (SEC identifier), it
   lists every 10-K and 10-Q filed up to the valuation date.
2. **Start from the latest annual report.** The latest 10-K's own XBRL package
   gives the fiscal year (start and end dates) and its reported figures; its
   cover page, or a newer 10-Q's, gives the number of shares and the date they
   were counted. Nothing has to be typed in except the ticker and the date.
   (An optional AI "snapshot" of the same report can be run as a cross-check
   with `--interpret`; disagreements are reported, never used as values.)
3. **Read each filing's own structure.** For about 16 recent filings it opens the
   XBRL package and finds the balance sheet, income statement and cash flow
   statement by their structure, not by their wording.
4. **Understand every line.** Each line is classified (revenue, debt, lease,
   payables, preferred stock, ...). Standard lines are recognised from the
   accounting taxonomy; company-specific lines from where they sit in the
   company's own totals and from their label.
5. **Extract each concept for each period**, with a status (see section 5).
6. **Build the history**: five fiscal years, eight standalone quarters, and the
   trailing twelve months (TTM), deriving quarters from year-to-date figures.
7. **Collect market and economic data**: prices, Treasury yield, spreads,
   inflation, Damodaran benchmarks for the company's industry. The industry is
   Damodaran's own assignment of the company (matched by ticker); for a company
   not in his file, the most common industry among US companies with the same
   SIC code is proposed with alternatives, and flagged for confirmation. His
   file is current, not historical.
8. **Check readiness and scope**: for each later step (normalization, cost of
   capital, equity bridge) it states whether the inputs are complete, and
   whether the company is in scope at all.
9. **Return the `DataBundle`**, with every value sourced, every gap explained,
   and a list of items a person should review.

## 4. How we reached exact results

The first version looked numbers up through fixed lists of accounting tags. It
worked for a few companies and failed in general, because every company
presents its statements a little differently. We rebuilt the extraction around
one idea: **let each filing describe itself.**

### 4.1 Use the filing's own structure

Every SEC filing includes files that say which table is which statement, which
lines add up to which totals (with plus or minus signs), and what each line is
called, including lines the company invented itself. 95 of 104 filings we
checked use such company-specific lines on their main statements, so this
structure is essential.

- Statements are found by key items (for example "total assets" and "total
  liabilities and equity"), not by English words. Wording such as "net
  earnings" or "stockholders' deficit" no longer matters.
- The company's own totals are used to check sums: a total must equal its parts.
  Out of about 1,900 sums checked, only six were genuinely inconsistent, all
  errors in the companies' own data.

### 4.2 Understand lines, not just tags

- Standard lines are grouped into families (for example all the ways to tag
  debt), each with a note when a line is broader or narrower than the concept
  ("payables including accrued liabilities", "debt including finance leases").
- Company-specific lines are classified by where they sit in the company's own
  totals and by careful label rules.
- **Location tags**: companies must say in which balance-sheet line their lease
  liabilities are included. We use these tags directly.
- Lines shown split into columns or rows (for example related-party versus
  third-party receivables) are added up only when the statement itself shows
  that split, so nothing is counted twice.

### 4.3 Use structure when tags are unreliable

- Some companies tag "earnings before income taxes" with an unexpected element.
  The system finds it as *the amount from which income tax is subtracted* in the
  company's own calculation.
- When there is no "operating income" line, operating income is derived from
  pre-tax income by removing interest and other non-operating items, opening
  sub-totals such as "total costs and other deductions" when they contain
  interest.

### 4.4 Statuses instead of guesses

Every concept in every period gets one status:

| Status | Meaning | Example |
|---|---|---|
| `reported` | A cited value | Apple's revenue |
| `not_presented` | Proven absent, so zero | Apple has no preferred stock: the balance sheet adds up without it |
| `not_disclosed` | The filing does not give it | Apple does not disclose interest expense |
| `carried_forward` | An earlier dated value, used with its own date | Annual lease balance used for a quarter where it is not disclosed |
| `unresolved` | Conflicting or unclear | A line the system cannot place |

"Proven absent" needs positive evidence: all of the company's totals add up
without that line, and no note mentions it.

### 4.5 Consistency rules

- **One basis per series.** If a quarterly report only shows "payables and
  accrued liabilities", the whole history switches to that line so that
  working-capital changes are not distorted.
- **Latest filing wins** for each period, but only a filing that actually shows
  that period in its statements.
- **Evidenced zero is a zero** when deriving quarters and the twelve-month total.

### 4.6 The AI model is fenced in

The local model is asked only about company-specific lines that block a
decision (about 1 line in 60). It must choose from a short list of categories,
and a rule-based guard rejects choices the label does not support. In testing it
once classified "purchases of investments" as capital spending; the guard now
prevents that. Accepted decisions are saved and reused.

### 4.7 Policies for special cases

- **Captive finance arms** (for example Caterpillar Financial): when the
  statements split the finance arm's debt, industrial debt and finance-arm debt
  are reported separately. When they do not (Deere), the cost of capital and
  equity bridge are blocked with that reason.
- **Temporary equity** (redeemable preferred stock or redeemable shares) is
  recorded as a claim on the company.
- **Successor companies**: when a company reorganises into a new legal entity
  (Exxon in 2026), its history is read from the predecessor.
- **Out of scope** for this version: banks, insurers, REITs, blank-check
  companies, companies reporting under IFRS or in another currency, and
  companies without revenue.

## 5. How we know it works

| Check | Result |
|---|---|
| Hand-recorded values from the published reports of 11 companies (8 not used to build the rules) | **111 of 111** within 0.5%, almost all exact |
| Balance-sheet identity (assets = liabilities + equity + other claims) on 102 recent balance sheets | Holds on 100; the exceptions are one tiny shell company |
| Main statements found in a random sample of 30 non-financial companies | 60 of 60 filings |
| Readiness on 51 companies (22 chosen for difficult cases, 30 random) | Equity bridge 44, cost of capital 45, normalization 32; the rest have stated reasons, mostly out of scope or genuinely changing accounting bases |
| Automated tests | 411 passing, with strict type checks |

The companies used for testing include negative-equity firms (McDonald's,
Starbucks), non-December fiscal years (Microsoft, Nike, Walmart), loss-making
growth companies (Snowflake, Rivian), recent spin-offs (Kenvue, GE Vernova), a
utility with several registrants (Duke), captive finance (Caterpillar, Deere)
and many small companies.

## 6. What the Data Agent does not do (yet)

- It does not value the company or choose assumptions; that is the job of the
  Normalizer, WACC, forecast and DCF steps.
- Older periods are checked by arithmetic, identities and consistency, but hand
  references cover only the latest annual report.
- Some histories that cross a change of fiscal year or a SPAC merger are still
  shorter than five years.
- When accounting presentation genuinely changes between years (for example
  property including or excluding leased assets), the series is reported as
  inconsistent rather than adjusted.

## 7. Running it yourself

```powershell
# Collect a full bundle from the ticker and date (--offline replays saved data)
uv run python scripts/collect_data.py --offline --ticker AAPL --as-of 2026-10-11 `
  --out outputs/apple-data.json
# Optional: --industry "<exact Damodaran label>" to override the mapped industry,
# --interpret to add the local-model snapshot cross-check (minutes on a CPU)

# Rebuild the reference bundles and check them
uv run python scripts/rebuild_bundles.py

# Check extraction against hand-recorded references and the balance identity
uv run python scripts/check_extraction.py --cohort scripts/cohorts/targeted.json

# History and readiness across a cohort, without any AI calls
uv run python scripts/check_history_cohort.py scripts/cohorts/targeted.json --offline
```

The bundle's `readiness` section tells you, for each later step, whether the
inputs are complete (`needs_review`) or what blocks them (`blocked`, with
reasons). `review_required` is always true: a person approves the inputs before
they are used in a valuation.
