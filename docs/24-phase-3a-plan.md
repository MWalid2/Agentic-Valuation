# 24. Phase 3a plan: filings RAG, Filings Analyst, Normalization Agent, Gate 1

Written on 2026-10-15, after Phase 2. Phase 3 reads what the XBRL statements do
not carry: note tables and text in 10-K, 10-Q and 8-K filings. Phase 3a builds
standard retrieval-augmented generation (RAG) and measures it; RAFT (fine-tuning
the reader on retrieved passages with distractors) is deferred to a possible
Phase 3b, decided on the measurements below. As before, everything must work for
any US non-financial company, and every number keeps a source.

## What Phase 3a delivers

| Need | Why it matters now | Consumer |
|---|---|---|
| Dilutive securities: options (count, weighted exercise price), RSUs/PSUs, convertibles | Shares are only the cover-page count today | Equity bridge (treasury-stock method in `engine.dcf`) |
| Debt schedule: instruments, principal, coupon, maturity | Cost of debt from actual borrowing, maturities | WACC, Normalizer |
| Lease disclosures: liability, discount rate, maturity table | Lease-as-debt uses the pre-tax cost of debt as a proxy | Normalizer |
| One-off items: restructuring, impairments, litigation, disposal gains, with the reason | Clean EBIT (proposed by the model, approved at Gate 1) | Normalization Agent |
| Tax: statutory to effective reconciliation | Replaces the fixed 25% marginal rate | WACC, Normalizer |
| Captive-finance split in the notes | Unblocks companies like Deere | Data readiness |

These fill the Phase 0 contracts in `contracts/accounting.py` (`FilingInsights`,
`DebtInstrument`, `LeaseDisclosure`, `DilutiveSecurity`, `FilingItem`,
`Adjustment` with `FilingCitation`). Segment data, guidance and business
descriptions are indexed now and used in Phases 4 and 5.

**XBRL first, RAG second.** Many note numbers are also detail-tagged in XBRL
(debt maturities by year, option counts and exercise prices, lease maturities).
Those are read deterministically, as in Phases 1 and 2; RAG is used for what is
not tagged. The tagged facts are also the ground truth that measures RAG.

## Documents

Only filings filed on or before the valuation date (point in time), for the
company's reporting entity (successor-aware, as in Phase 2):

| Document | Content used |
|---|---|
| 10-K primary document | Items 1, 1A, 7, 7A, 8: business, risks, MD&A, market risk, statements and all notes |
| 10-Q primary documents after the latest 10-K | Updated notes (new debt, restructuring) |
| 8-K Exhibit 99.1 (earnings releases) | Non-GAAP reconciliations, guidance (indexed now, used in Phase 4) |
| Exhibits 4 and 10 (indentures, credit agreements) | Coupons and maturities when notes are thin (later, only if the evaluation shows the need) |

The 10-K and 10-Q primary documents are already downloaded and cached.

## Design

```text
filing (iXBRL HTML) --parse--> sections + tables --chunk--> passages
   passages --index--> SQLite: FTS5 (BM25) + bge-small embeddings, metadata
question per field --hybrid search (filters: company, filed <= date, section)--> top 4
top 4 --qwen3:4b points to cells/quotes--> code reads, converts units, checks --> Sourced values
```

**1. Sections from the filing's own tags.** The inline-XBRL documents mark every
note, and many tables, with `...TextBlock` tags (`DebtDisclosureTextBlock`,
`ScheduleOfEffectiveIncomeTaxRateReconciliationTableTextBlock`): Coca-Cola's
latest 10-K has 89, Apple's 45. They give exact boundaries, including
continuations across pages. Parts without tags (Item 7 MD&A, Item 1) are split by
their Item headings. Older filings without inline XBRL use the heading parser.

**2. Chunks.** Tables are never split: each becomes a grid keeping row labels,
column headers, column dates and units ("in millions"). Prose is split into
passages of about 300-500 tokens with overlap, within one section. Each chunk has
a stable ID (accession, section, ordinal) and metadata: company, form, filed,
period, section tag, anchor in the document, and the inline-XBRL fact IDs it
contains.

**3. Index, once per filing.** SQLite (one file): FTS5 for keyword search (BM25)
and `bge-small-en-v1.5` embeddings (ONNX via `fastembed`, CPU) stored as float32,
cosine similarity in NumPy; the two rankings merged by reciprocal rank fusion.
Point-in-time and company filters apply before ranking.

**4. Retrieval per field.** Each `FilingInsights` field has a fixed question and a
target section ("scheduled principal repayments by year" searches the debt note
first, then the whole filing). At most four passages reach the model.

**5. Reading: point, don't type.** The model returns the IDs of the table cells
(or the quote) that answer, never the number itself; code reads the cell,
applies the unit and the sign, and checks the column date. Then: reconciliation
with XBRL where a tagged total exists (difference above 2% flagged), schedules
summing to their total, and self-consistency (two prompts with different wording;
disagreement is flagged low-confidence for Gate 1). Each value is a `Sourced`
value from `sec_filing_text` with a `FilingCitation` (accession, section,
table and cell).

**6. Normalization Agent.** Candidates come from code: XBRL elements for
restructuring, impairments, litigation and disposal gains, plus large
period-to-period swings. For each, retrieval finds the explanation; the model
labels it (recurring or not, operating or not) with a short reason; code
computes the amount. Rules from the agent spec: items in 3 of the last 5 years
are recurring unless the evidence says otherwise; above 3% of EBIT or low
confidence goes to Gate 1. Output: `Adjustment` records with evidence.

**7. Gate 1.** In the graph and the chat: a table of proposed adjustments and
low-confidence extractions, each with its passage; approve, reject or edit;
stored as gate-1 `ApprovalRecord`s (the Phase 2 data review becomes the first
part of this gate).

## Evaluation with RAGAS

RAG has two parts that fail differently: retrieval (did the right passage come
back?) and generation (did the answer use it faithfully?). RAGAS measures both.
Evaluation-only dependency (development group), not used at runtime.

**Gold sets, written before tuning (as the Router set):**

| Set | How it is built | Size |
|---|---|---|
| XBRL-labelled | For detail-tagged facts in the notes (debt maturities, option counts and prices, RSUs, lease maturities, statutory and effective tax rates): the question, the tagged value as reference answer, and the chunk(s) containing that fact's inline-XBRL ID as reference contexts. Automatic, exact | at least 100 questions, at least 5 companies for the acceptance set; more across the cohort for development |
| Hand-checked narrative | One-off items and their explanation, coupons, guidance statements: question, reference answer, reference passage; prepared by code and the model, verified by you | about 30 |

Companies are split between development (tuning) and test (reported) so the
numbers are not tuned on the reported set.

**Metrics:**

| Component | Metric | Judge needed? |
|---|---|---|
| Retrieval | RAGAS ID-based context recall at 4 (the reference chunk is among the four passages; the roadmap's hit-rate) and ID-based context precision; our hit@k by field and section | No |
| Extraction | Exact match of the value read by code against the XBRL label (within rounding), by field; rate of confident-wrong values | No |
| Narrative answers | RAGAS faithfulness (claims supported by the passages), response relevancy, factual correctness against the reference; context precision and recall with a reference | Yes |
| Distractors | RAGAS noise sensitivity (wrong answers caused by irrelevant passages); this is the number that decides whether RAFT (Phase 3b) is worth it | Yes |

**The judge is local:** qwen3:4b through Ollama's OpenAI-compatible endpoint, no
data leaves the machine. A small judge can be wrong, so it is validated first:
its judged context recall is compared with the exact ID-based truth on the
XBRL-labelled set. If agreement is low, judge metrics are reported as diagnostic
only and acceptance rests on the deterministic metrics. Judge metrics cost
several model calls per sample, so they run on the 30-sample narrative set at
milestones, not on every change.

## Acceptance criteria

Adapted from the roadmap, which assumed a 7-8B model; this machine runs 1.7B and 4B.

| Criterion | Target | Measured by |
|---|---|---|
| Retrieval | hit-rate@4 at least 90% on at least 100 labelled questions across at least 5 test companies | RAGAS ID-based context recall@4 |
| Extraction | debt, lease and share-count figures at least 90% correct with qwen3:4b; the rest flagged low-confidence, not wrong (confident-wrong at most 2%); XBRL-backed fields at least 99% (reconciled, not re-read) | Exact match against XBRL labels |
| Adjustments | at most 1 false positive per company on 5 companies | Your manual review |
| Narrative faithfulness | reported (target 0.9 if the judge is validated) | RAGAS faithfulness |
| Valuation use | diluted share counts close to each company's own diluted EPS denominator; Deere's WACC no longer blocked | Reference cohort |

## Work breakdown

| Step | Work | Exit check |
|---|---|---|
| 1. Filing text store | Fetch primary documents and EX-99.1 point in time; parse sections (text blocks, continuations, Item headings); tables to grids with units and dates; chunks with stable IDs and fact IDs | Reference cohort: every note text block found; every tagged numeric fact located in exactly one chunk; tables keep headers and units |
| 2. Gold sets | XBRL-labelled questions with reference answers and chunk IDs (development and test companies); narrative set drafted for your review | Sets frozen before any retrieval tuning |
| 3. Index and retrieval | FTS5, bge-small embeddings, fusion, filters, section-targeted queries; RAGAS harness with the local judge and judge validation | Retrieval hit@4 at least 90% on the test set |
| 4. Filings Analyst | Questions per field, cell pointing, unit and date checks, XBRL reconciliation, self-consistency, `FilingInsights` with citations | Extraction criteria above |
| 5. Insights in the valuation | Dilution (options by treasury stock, RSUs, convertibles) into the bridge; lease discount rate; captive split; marginal tax from the tax note | Diluted shares versus the EPS denominator; Deere unblocked; Phase 1 and 2 checks unchanged |
| 6. Normalization Agent | Candidates from code, explanation by retrieval, model labels with reasons, amounts by code, recurrence and materiality rules | Your review on 5 companies: at most 1 false positive each |
| 7. Gate 1 and chat | Adjustment table in the graph and chat; approvals; answers can quote filing passages (I10) and dilution and debt details (I1) | Scripted session tests; live walkthrough |
| 8. Evaluation and docs | RAGAS report (retrieval, extraction, faithfulness, noise sensitivity), latency, acceptance table; RAFT decision for Phase 3b | Acceptance table filled in |

Suggested order: 1, 2, 3, 4, 5, 6, 7, 8 (the gold sets come before retrieval so
tuning cannot leak into them).

## Dependencies

| Package | Use | Group |
|---|---|---|
| `fastembed` (ONNX runtime) | Local CPU embeddings, `bge-small-en-v1.5` (about 33M parameters) | Runtime |
| `ragas` | Evaluation metrics | Development only (it brings `datasets`, `langchain-community`, the OpenAI client) |

SQLite FTS5 and NumPy are already available. Versions pinned in `uv.lock`; a
conflict with the installed `langchain-core` is checked at installation.

## Risks

| Risk | Mitigation |
|---|---|
| Inline-XBRL variations (continuations, nested blocks), older filings without it | Continuation handling; Item-heading fallback; coverage measured in step 1 |
| Tables split across pages, units in headings | Tables kept whole; units and column dates in chunk metadata; code checks |
| A small model misreading tables | Cell pointing, code reading, XBRL reconciliation, self-consistency, low-confidence flags |
| A small judge mis-scoring RAGAS metrics | Judge validated against exact ID-based truth; deterministic metrics gate acceptance |
| Labels only for tagged facts (distribution shift to untagged ones) | Hand-checked narrative set measures untagged reading separately |
| CPU time | Index once per filing (embedding a 10-K: roughly 10-30 seconds); judge metrics only at milestones |

## Decisions with defaults (change if you prefer)

| Decision | Default |
|---|---|
| RAFT | Deferred to Phase 3b; decided on extraction accuracy and noise sensitivity |
| Embedding model | `bge-small-en-v1.5` (CPU, ONNX) |
| Reader model | qwen3:4b (role `filings`, already configured) |
| RAGAS judge | qwen3:4b through Ollama, validated against exact ID-based truth |
| Narrative gold set | Drafted by code and the model, verified by you (about 30 items) |
| Adjustment review | You review the proposals for 5 companies (acceptance criterion) |

## Progress

### Step 1: filing text store (done, 2026-10-15)

`src/dcf_agent/rag/filings.py`: `filing_texts(sec, cik, as_of)` parses the latest
10-K filed by the date and the 10-Qs after it into `Chunk`s (about 0.35 seconds
per filing).

| Part | Implementation |
|---|---|
| Sections | Inline-XBRL text blocks, followed across `ix:continuation` chains (also chains started by plain text facts, footnotes and facts in the hidden header); each chunk records every enclosing block, outermost first, so a nested policy or table block can be targeted; text blocks wrapped around inline text are included. Text outside notes is split by Item headings; notes are titled by their "NOTE n" heading |
| Tables | Kept whole as grids; "$", "(", "%" and ")" fragments joined to their numbers. Every tagged cell keeps its fact: concept, unit, scale, sign, period start and end, and whether it is a breakdown (dimensional) figure, from the document's own contexts. Units come from the tagged facts (for example "usd in millions"). Header rows end at the first row with a tagged number (years are headers). A multi-column table without a header inherits the one above it in the same section (continued statements, comparative roll-forwards), skipping page footers between them; the lead-in sentence ("The following table summarizes ...") is kept as a caption |
| Prose | Passages of about 400 tokens (at most 500) with a short sentence overlap, within one section; a fact in prose belongs to the first passage only |
| Hidden header | Facts in it are listed but never chunked; empty text blocks are listed separately |

**Exit check** (`scripts/check_filing_text.py`, 52 companies, 149 filings):
149/149 pass. All 178,373 displayed numeric facts land in exactly one chunk (0
missing, 0 repeated); every text block with content is covered; all 5,547 tables
with tagged numbers have units; every multi-column table has a header row, an
inherited header or a caption; no unresolved continuation. 55,733 chunks in total.

Found and fixed on the way: chains from footnotes and from the hidden header;
inline-wrapped text blocks; continued tables (Delta, Evertec, LanzaTech); a page
footer once accepted as a caption (Evertec passed for the wrong reason until this
was fixed); a column count confused by facts in a label cell. A shell edit had
put backspace characters in two line-classifier guard patterns in
`agents/lines.py` (`\bcash\b` could never match; "cost of sales" could pass as
revenue): fixed, no stored mapping affected, and a test now fails on any control
character in the source. Tests: `tests/test_filings_text.py` (8, a synthetic
document with every structure above). 595 tests pass.

Deferred: 8-K Exhibit 99.1 (earnings releases) is indexed when Phase 4 needs
guidance; the documents' exhibit types need the filing index page.

### Step 2: gold sets (done, narrative review pending, 2026-10-15)

`scripts/build_gold_sets.py` writes `evals/rag/` (described in its README).

**XBRL-labelled set (frozen).** A fixed bank of 30 questions about note facts
(debt maturities, fair value and rate; lease payments and discount rates;
options, unvested awards, antidilutive securities, unrecognized compensation;
tax rates, unrecognized tax benefits, loss carryforwards; restructuring,
impairments; pension obligation), answered from each company's latest 10-K by
the consolidated fiscal-year-end value. References are inline-XBRL fact IDs
(stable within a filing), not chunk IDs; earlier-period values of the same
concept are kept as distractors for step 4.

| Split | Questions | Companies | Debt | Leases | Dilution | Tax | One-off | With distractors |
|---|---|---|---|---|---|---|---|---|
| Test | 225 | 17 | 41 | 85 | 23 | 66 | 10 | 82 |
| Development | 514 | 34 | 111 | 172 | 71 | 137 | 21 | 199 |

The split is a fixed hash of the ticker (about a third to test), so it cannot be
tuned. `tests/test_gold_sets.py` fails if the file changes (SHA-256 recorded in
the README), if the split stops following the rule, or if the test set falls
below 100 questions or 5 companies.

**Narrative set (30 drafts, 22 companies) awaits review** in
`evals/rag/narrative-review.md`: questions about facts stated in prose, with the
filing's own sentence as the reference answer. Some drafts need fixing (one
answers where restructuring charges were booked rather than what for; one
sentence was cut by the splitter); that is what the review is for.

**Found on the way (a step 1 bug).** `unitRef` is a filer-chosen ID ("U_USD",
"Unit_Standard_USD_rrYev...", "number"), not a unit; step 1 stored the ID, so
table units could read "u_usd in millions" while its check, which only required
units to be present, passed. Units now resolve through the document's
`xbrli:unit` definitions to measures (USD, shares, pure, USD/shares, or a filer's
own measure), and the step 1 check requires no unresolved unit ID: 149/149
filings pass. Facts also carry their value (shown number x 10^scale, sign
applied) and their displayed text.

### Step 3: index, retrieval and the RAGAS harness (done, 2026-10-16)

**Index** (`rag/index.py`): one SQLite file (`data/rag/index.sqlite`), WAL mode.
Per filing: chunks, the fact IDs each chunk holds, an FTS5 full-text index
(Porter stemming; text plus a labels column made of the title and the
text-block and table names), and optional 384-dimension `bge-small-en-v1.5`
embeddings (ONNX on the CPU) of a compact view of each chunk (title, caption,
headers, row labels; not every number). Each filing records the parser version
(a fingerprint of `rag/filings.py`); a filing indexed by other parser code is
dropped and rebuilt, embeddings included.

**Retrieval:** restricted to the allowed filings (one company, point in time);
BM25 and cosine rankings fused by reciprocal rank fusion; optional section
targeting (the field's notes first). **Query expansion** (`rag/queries.py`) adds
the filings' own terms ("maturities of long-term debt", "lessee lease liability
maturity", "antidilutive ...") and turns relative years into fiscal-year labels
("year two" with a fiscal year ending in 2025 becomes "2027"; "after year five"
becomes "thereafter"). It uses only the field and the filing's fiscal year end.

**Choice, on the development set only** (514 questions, 34 companies, hit@4):

| Configuration | hit@4 | MRR | Indexing cost |
|---|---|---|---|
| BM25 | 0.798 | 0.579 | seconds |
| BM25 + targeting | 0.840 | 0.607 | seconds |
| BM25 + expansion | 0.930 | 0.776 | seconds |
| **BM25 + expansion + targeting (chosen)** | **0.932** | 0.780 | seconds |
| Dense + expansion | 0.883 | 0.732 | about 1 CPU minute per company |
| Hybrid + expansion | 0.922 | 0.783 | same |
| Hybrid + expansion + targeting | 0.920 | 0.786 | same |

Embeddings did not beat BM25 on these questions (dense alone is clearly worse),
at a large CPU cost on this machine (about 10 chunks per second), so the default
is BM25; dense and hybrid stay available and will be measured again on the
narrative set, where wording varies more.

**Exit check, test set** (225 questions, 17 companies, run once with the chosen
configuration): **hit@4 0.902** (203/225), target 0.90: met, narrowly. RAGAS
ID-based context recall 0.898, precision 0.232 (four passages, usually one
relevant), MRR 0.715. By field: leases 0.99, tax 0.97, debt 0.90, dilution
**0.65** (0.90 on development: a generalization gap on 23 questions, not tuned on
test), one-off items 0.30 (the tagged amount often sits in an unusual place, such
as the cash-flow statement, while the retrieved notes discuss the same item;
these are searched with several passages by the Normalization Agent). Plain BM25
without expansion scored 0.747 on test.

**RAGAS judge** (`scripts/validate_rag_judge.py`): qwen3:4b through Ollama's
native API with JSON output (`langchain-ollama`). Through the OpenAI-compatible
endpoint, and even natively with thinking turned off, qwen3 reasons in free text
and hits the token limit; a JSON output format prevents that. Validation of
RAGAS `LLMContextRecall` against exact truth on 30 development questions plus 15
negatives (the question searched in another company's filings): 0 judge failures
on positives, 1 on negatives; the judge said "supported" for 27 of 30 positives
and for 3 negatives, all three the federal statutory rate (21% for every
company, so another filing genuinely supports it). Corrected: true negatives
11/11, positives 27/30, agreement 41/44 (0.93). The judge does not invent
support but sometimes misses it; about 36 seconds per judgement. Judge metrics
are therefore reported, and acceptance rests on the exact ID-based metrics.

**Dependencies:** `fastembed` (runtime); `ragas` 0.4.3 and `langchain-ollama`
(development). ragas imports a module removed in `langchain-community` 0.4, so
`langchain-community` is pinned below 0.4 (0.3.31 works with `langchain-core`
1.6); ragas needs `instructor`, which supports only `rich` below 15, so `rich` is
pinned to 14.x (the chat tests pass unchanged).

**Found on the way:** a note heading reused as the title of the next note (the
litigation note was titled "NOTE 11: DEBT", which section targeting then trusted);
"after year five" matched as "year five". Both fixed, with tests. 606 tests pass.

### Step 4: Filings Analyst (accepted below target, 2026-10-16)

`src/dcf_agent/agents/filings.py`: `FilingsAnalyst.read(spec, accessions,
period_end)` for 20 debt, lease and dilution fields (`FIELDS`), each a fixed
question, a reworded question and the us-gaap element that tags it.

| Part | Implementation |
|---|---|
| XBRL first | If the filing tags the field (consolidated, fiscal-year-end, right unit), the fact is read with no model call (`method="xbrl"`, source `sec_xbrl`) |
| Retrieval | The 10-K's top 4 passages first, then the next 4 across all allowed filings, excluding those already seen |
| Point, don't type | Passages are shown with IDs: table cells `[P2-R4-C1]` (row labels and spacer cells have none), numbers in prose `[P1-N3]`; the prompt names the fiscal year end and maps "year two" etc. to calendar years. The model returns an ID, or a verbatim quote when the number has none |
| Code reads | The cell's own fact (value, scale, sign); a quoted number that matches exactly one cell becomes that cell; a quote must be verbatim, scaled by its words or the table's units |
| Checks | Period (a cell for another year in the right row is corrected to the year-end cell), breakdown (dimensional) figures, unit, and the concept (a cell tagged as another us-gaap item is flagged) |
| Self-consistency | Two prompts; on disagreement a third, careful one decides by majority; no majority is flagged |

Every flag makes the reading low-confidence (for Gate 1); a quote, not a tagged
cell, is always low-confidence.

**Development calibration** (25 questions, XBRL shortcut disabled so the model
must read; `scripts/evaluate_filings_analyst.py`):

| Version | Correct | Of which high confidence | Confident-wrong | Not found |
|---|---|---|---|---|
| First | 48% | | | |
| + concept check | 52% | | | |
| + column correction, 10-K first, third prompt | 64% | | 0% | 4 |
| + cleaner passages, years in the prompt | **72%** | 64% | **0%** | 1 |

By field: leases 11/13, debt 6/8, dilution 1/4. The XBRL path read 25/25
correctly. All 6 wrong readings were flagged low-confidence: a lease-cost cell
for a lease payment (DAL), a carrying amount instead of the prose maturity
schedule (DE), a price for an amount (CART), antidilutive counts given in
rounded prose (KO, MCD), and a nil year-four maturity read as the nearest amount
(KVUE).

**Decision (2026-10-16):** the 90% reading target is not met with qwen3:4b; the
safety criterion is (0% confident-wrong, XBRL path 100%). Accepted as is and moved
on: tagged fields, the large majority in these filings, are read
deterministically, and every untagged reading the model is unsure of goes to Gate
1. Not run: the test split (hours on this CPU). Possible later fixes: a year-row
check for maturity schedules (the same idea as the column correction) and
year-to-number pairing in prose schedules.

Found on the way: a quote of only commas crashed the number parser (fixed, with a
test). 616 tests pass.

### Step 5: insights in the valuation (done, 2026-10-16)

`src/dcf_agent/rag/insights.py` reads, in code and without the model, from the
latest 10-K and later 10-Qs (the filing index). Each fact now keeps its
dimension members (axis, member) from the document's contexts, so a breakdown
can be summed safely.

| Insight | How it is read | Used by |
|---|---|---|
| Options | Options outstanding and their weighted-average exercise price at the latest filing's balance-sheet date (a total, or one tranche per member when only a breakdown is tagged) | `Shares.options`: treasury-stock method in `engine.dcf.dilute` |
| Unvested units | Nonvested RSUs and PSUs: the stated total; otherwise the award types on one axis summed (a member equal to the sum of the others is taken as their total); 33 of 51 10-Ks tag only the breakdown | `Shares.rsus` |
| Lease discount rate | Weighted-average operating lease discount rate, with the lease liability | Lease interest in `normalize` (`cli value`), instead of the pre-tax cost of debt |
| Marginal tax rate | Federal statutory rate plus the average state and local line of the rate reconciliation (net of the federal benefit) over the years shown; from amounts when only amounts are tagged; from state tax expense x (1 - federal rate) / pretax income when the reconciliation has no state line (Apple); loss years skipped; bounds 15-35%, otherwise the 25% policy | `assemble` (WACC: after-tax Kd, relevered beta); the CLI keeps the assumptions file's rate, which the forecast converges to |
| Captive-finance split | The supplemental consolidating balance sheet (industrial, financial services, eliminations, consolidated columns), untagged in Deere's filings. Each borrowing row's numbers are placed in (group, period) slots so that every period column adds up; blanks are omitted in filings, so ties are broken by the fewest eliminations, then by each number's position in the row. Accepted only if the consolidated borrowings equal the tagged consolidated debt | Unresolved `captive_finance_debt` becomes reported; `debt` becomes industrial debt (consolidated less captive), as when the balance sheet itself splits it |

The insights travel in `DataBundle.filing_insights` (`FilingInsights`, with two
new groups: `tax_items`, `captive_finance`); every value is `Sourced` with its
filing (accession in the reference) and a `FilingCitation`. The Data Agent reads
them after the history and before readiness (`DataTools.get_filing_insights`,
index at `data/rag/index.sqlite`); a failure is a warning, never a block.
Convertibles are not read yet.

**Found on the way.** With Deere's captive split read, its valuation was still
blocked: temporary equity "disclosed in notes but not presented as a statement
line". It is the redeemable noncontrolling interest ($44M) already inside the
mapped minority interest ($51M with $7M of ordinary NCI). When temporary equity
is only in the notes and equals a mapped claim (or one of its lines), it is now
`not_presented` (no double count), as the balance-sheet path already did.
McDonald's tags its EPS share counts without their scale (711.1 shares for 711.1
million); the exit check rescales such a count and says so.

**Exit check** (`scripts/check_insights.py`; the WACC cohort rebuilt offline
through the graph's services; Monster's filings are not cached):

| Company | Ours vs diluted EPS shares | Cover only | Marginal tax | Lease rate | WACC (25% -> filing tax) |
|---|---|---|---|---|---|
| Apple | +0.16% | -0.82% | 21.96% (from state tax expense) | not tagged | 10.99% -> 11.00% |
| Caterpillar | -0.14% | -0.61% | 21.83% | 4.0% | 9.82% -> 9.83% |
| Deere | -0.02% | -0.40% | 22.73% | 4.2% | blocked -> 8.96% |
| Coca-Cola | +0.15% | -0.24% | 22.17% | 3.9% | 7.71% -> 7.73% |
| McDonald's | -0.28% | -0.49% | 22.70% | not tagged | 8.25% -> 8.31% |
| Microsoft | +0.68% | -0.37% | 22.53% | 3.7% | 10.66% -> 10.67% |
| Nike | +0.92% | +0.07% | 22.93% | 3.6% | 9.33% -> 9.37% |
| Nvidia | -0.07% | -0.76% | 21.43% | 4.69% | 11.78% -> 11.78% |
| Boeing (loss) | not compared | | 23.80% | 3.97% | 9.03% -> 9.07% |
| Snowflake (loss) | not compared | | policy 25% (losses every year) | 6.1% | 12.41% (unchanged) |

All 8 comparable companies are within 3% (mean absolute gap 0.30%, against 0.47%
for the cover count alone); 6 of 8 move closer. Microsoft and Nike move past the
EPS count because every unvested unit is counted, while the issuers' treasury
method nets unrecognized compensation: deliberately conservative. For loss
periods the EPS count excludes all dilution (antidilutive), so it is no
reference. Deere is now valued (WACC and equity bridge no longer blocked).

**Earlier checks unchanged.** `check_extraction.py`: 111/111 references within
0.5%, balance identity unchanged (it fails only for the tiny shell LLPS, as
before; Exxon's entity-identity error is pre-existing). Cohort readiness
(`check_history_cohort.py`, XBRL only, no filing text; 52 companies): every
company's statuses unchanged, except that Deere no longer lists temporary equity
(its captive split comes from the filing text in the Data Agent) and SLDB, now
cached, is assessed instead of erroring. Tests: `tests/test_insights.py` (10:
members kept, options and units with evidence, a total member, marginal tax from
rates, amounts and state expense with loss years skipped, lease rate, the
consolidating table with blanks and a lone financial-services amount, refusal
when it does not match the tagged debt, the raw-financials update, and the
valuation using or falling back from the insights). 626 tests pass.

### Step 6: Normalization Agent (built, your review pending, 2026-10-16)

`src/dcf_agent/agents/normalizer.py` proposes EBIT adjustments for the last five
fiscal years and the TTM; approval and application stay at Gate 1 and in
`engine.normalize` (which already applies approved `Adjustment`s).

| Part | Implementation |
|---|---|
| Candidates (code) | Lines inside operating income on the latest 10-K's income statement whose label names an unusual item (inside = under the operating-income line, or pretax income when there is none, in the issuer's calculation tree; the sign comes from the tree), and us-gaap one-off elements tagged in the notes or cash-flow statement (restructuring, impairments, litigation, disposal gains and losses, acquisition costs), which US GAAP places inside income from operations |
| Amounts (code) | By fiscal year from the SEC company facts (point in time); TTM = fiscal year + year to date - prior year to date; items below 0.5% of EBIT in every year are skipped |
| Reading (model) | qwen3:4b (`normalizer` role): three retrieved passages; recurring or one-off, a short reason, and the passage relied on |
| Rules (code) | In 3 or more of the last 5 years: recurring and kept in EBIT; only an unusual year (at least 3x the median of the other years, and the excess at least 3% of EBIT) has its excess adjusted. In 1-2 years: adjusted unless the model reads it as a recurring cost. A recurring item the cited passage calls one-time or unusual is flagged for Gate 1, never stripped on that reading alone. A detail tagged in the notes inside an adjusted income-statement line is not adjusted twice |
| Output | `Adjustment` records (charges added back, gains removed) with the tagged amount's filing and the passage as evidence; above 3% of EBIT, low confidence or ambiguous goes to Gate 1 |

**Found while calibrating** (on the five review companies, before the review):
the model labels almost everything one-off, even when its own reason says "a
pattern of recurring charges", and calls impairments non-operating. Letting it
overturn the recurrence rule stripped Coca-Cola's other operating charges and
Nike's yearly impairments in every year; checking that the cited passage says
"one-time" or "unusual" was not enough either (Deere's "Special Items" note
contains those words but is about other items). The model therefore cannot
strip a recurring item; spikes are found by code. Its operating/non-operating
label was dropped.

**Review set** (`scripts/review_normalization.py` writes
`evals/normalization/review.md`): Coca-Cola, Nike, Boeing, Caterpillar, Deere;
13 candidates, 8 proposed, 11 period adjustments, all at Gate 1 (each above 3%
of EBIT or low confidence). Acceptance (at most one false positive per company)
was decided by your review (below).

**Review result (2026-10-17, `evals/normalization/reviewed-2026-10-17.md`).** 13
items:
- **Correct:** 11.
- **False positive:** 1. Nike's FY2026 severance amount: the tag gives $300M, the filing states $385M.
- **Missed adjustments:** 1 item, Coca-Cola's "other operating charges". It recurs, so only the excess over its median was adjusted (+$2,925M in 2024). Its Note 18 names large one-offs every year: the fairlife remeasurement ($1,702M in 2023, $3,109M in 2024) and the BodyArmor impairments ($760M, $960M).

Acceptance is met (no company has more than one false positive).

**Fix after the review.** For a recurring line on the income statement, code now
reads the components the filing names right after the line's total for the year
("$3,109 million related to the remeasurement of ..."), and adjusts those that
are one-offs:
- adjusted: impairments, remeasurements, litigation, divestitures, transaction costs, indemnification, discontinued operations;
- kept: programs and amortization;
- the components must not exceed the total;
- the excess-over-median rule remains for years without such a breakdown.

Coca-Cola now: +$1,745M (2023), +$4,008M (2024), +$1,137M (2025; its BodyArmor
impairment is not adjusted twice). The other four companies are unchanged. Not
fixed: a tagged amount that differs from the text (Nike); Gate 1 lets you edit it.

### Step 7: Gate 1 and chat (done, 2026-10-17)

**Graph.** A `normalize` step (in the plans of history questions, the quick value
and the full model: I2, I6, I9) runs the Normalization Agent
(`LiveServices.normalize`). Adjustments that need no review (at most 3% of EBIT,
not low confidence, not ambiguous) are approved by policy, each recorded as an
autopilot `ApprovalRecord`. Otherwise the session stops at **Gate 1**
(`adjustment_review`, an interrupt) showing:
- every adjustment for review: period, item, EBIT effect, share of EBIT,
  confidence, rationale, the filing passage and its evidence;
- the policy-approved ones;
- recurring items kept in EBIT but flagged;
- low-confidence filing readings (captive split, summed award types, marginal tax).

The answer (`AdjustmentDecision`) is approve all, decide (one verdict per item:
approve, reject, or edit with a new amount; incomplete verdicts are refused and
asked again), or stop. Each verdict is a gate-1 `ApprovalRecord` tied to a hash
of the item shown; an edit records `id:amount=...` and the new amount is
`user_override` evidence. The approved adjustments then normalize the history
(`engine.normalize`, with the filing's marginal tax and lease rate). The session
checkpoint holds the proposals (`AdjustmentProposal`, `Flag` are registered with
the serializer), so a chat closed at Gate 1 resumes there. The Phase 2 data
review keeps its own gate-1 payload; the chat tells them apart by `kind`.

**Answers.**
- Questions on dilution, lease rates, the tax rate or captive finance (I1, I10) are
  answered from `FilingInsights`, every figure cited to its fact.
- "Where does clean EBIT come from" (I10) and history questions (I2) show clean EBIT
  by year and each approved adjustment with its quoted passage, accession and section.
- Small dollar amounts now keep their cents (exercise prices).

**Chat.** Gate 1 shows the adjustment table and asks "approve all / decide each /
stop"; deciding asks per item (Enter approves, "edit 250" sets $250M). `/state`
reports approved versus proposed adjustments.

**Exit check.**
- **Scripted sessions** (`tests/test_gate1.py`, 6):
  - decide with approve, edit and policy approval, and clean EBIT exact;
  - an incomplete decision asked again;
  - stop;
  - no gate when nothing needs review;
  - restart from the checkpoint at Gate 1;
  - a follow-up quoting the passages.
- **Scripted chat** (`tests/test_chat.py`): decide each, with an edit.
- **Filing-details answer:** `tests/test_insights.py`.
- 643 tests pass.
- **Live walkthrough** (Nike, offline cache, qwen3:4b):
  - Gate 0, then the quick value; 1.5 seconds for the normalizer (cached readings); Gate 1 with two adjustments and the tax reading.
  - Approve one and edit the other to $250M.
  - The follow-up "where does the clean EBIT come from?" lists FY2022 to TTM clean EBIT and both adjustments with the severance passage quoted (accession 0000320187-26-000088).
  - "How many stock options are outstanding?" answers 76.8M at $96.44 and 12.5M units, each cited.
  - Valuation itself (`value`) is still recorded as not available in this phase.

### Step 8: evaluation and RAFT decision (done, 2026-10-17)

No new long runs (your choice, 2026-10-17): the results below come from the
measurements already made in Steps 3 to 7. The extraction test split (149
questions, about 3 to 4 hours on this CPU) was not run.

**Acceptance.**

| Criterion | Target | Result | Status |
|---|---|---|---|
| Retrieval | hit@4 at least 90% on at least 100 labelled test questions, 5 or more companies | 0.902 on 225 test questions, 17 companies (RAGAS ID-based context recall 0.898, precision 0.232) | Met (narrowly) |
| Extraction: reading by the model | debt, lease and share counts at least 90% correct | 96% (24/25 development questions, after the fixes below; 72% at high confidence); was 72% | Met on the development sample (test split not run) |
| Extraction: safety | confident-wrong at most 2% | 0/25 | Met (development sample) |
| Extraction: XBRL-backed | at least 99% | 25/25 | Met (development sample) |
| Noise sensitivity (no judge) | reported | 0/25 after the fixes (was 1/25: Coca-Cola's 2023 antidilutive count) | Reported |
| Adjustments | at most 1 false positive per company on 5 companies | Your review (2026-10-17): false positives Coca-Cola 0, Nike 1 (an amount), Boeing 0, Caterpillar 0, Deere 0; Coca-Cola's misses fixed afterwards | Met |
| Narrative faithfulness | reported (0.9 if the judge is validated) | Not measured: the narrative set awaits your review, and no component yet writes narrative answers from passages | Not measured |
| Valuation use: diluted shares | close to the EPS denominator | Within 3% on 8/8 comparable companies (mean gap 0.30%, against 0.47% for the cover count alone) | Met |
| Valuation use: Deere | WACC no longer blocked | WACC 8.96%; WACC and equity bridge ready | Met |
| Phase 1 and 2 checks | unchanged | 111/111 references, balance identity and cohort readiness unchanged; 643 tests pass | Met |

The RAGAS judge (qwen3:4b) agreed with exact truth on 41 of 44 cases (0.93):
it does not invent support but sometimes misses it. Acceptance therefore rests on
the exact, ID-based metrics.

**Latency** (this CPU; model calls dominate):

| Stage | Time | Note |
|---|---|---|
| Parse a filing into chunks | about 0.35 s | Inline XBRL, once per filing |
| Index (BM25) | seconds per company | Embeddings optional (about 1 CPU minute per company), off by default |
| Retrieval | 111 ms median, 160 ms p90 | 60 gold questions, with query expansion and section targeting |
| Filing insights (dilution, lease rate, tax, captive split) | 0.2 s per company | No model; after indexing |
| Filings Analyst, one untagged field | 81 s median | 2.3 model calls on average (two prompts, a third on disagreement) |
| Normalization Agent | about 1 model call per material candidate (2 to 3 per company here) | Cached readings: 1.5 s in the walkthrough |
| Gate 1 | human time | Excluded from the run budget |
| Data collection and WACC (Phase 2) | about 6 s and 21 s | Unchanged |

**Known limits.**
- Extraction is measured on 25 development questions (the test split was not run); the fixes below were made on the errors of that sample, so the test split could be lower.
- The model's reasons in normalization proposals can ramble or contradict the proposal; the amounts and rules are code.
- Convertible notes are read only when the conversion price is tagged by instrument (Snowflake); a price not tied to an instrument is skipped with a note.
- One-off items tagged only with company extension elements are found only when they are a line on the income statement.
- A TTM amount for a one-off needs year-to-date tags.
- Narrative questions (guidance, explanations) have a draft gold set but no answering component.

**RAFT decision: not now** (recommendation; Phase 3b stays open).

RAFT fine-tunes the reader on retrieved passages with distractors. Measured
here:
- Retrieval is not the bottleneck: 0.90 hit@4.
- Distractors cause few errors: 1 of 6 wrong readings. Most errors are a wrong row or column in a table the model could see, or a nil value read as the nearest amount.
- The XBRL-first path reads tagged fields exactly, and the model is used only for what is untagged.
- Confident errors are already 0%.

RAFT would target a minority of an already-flagged minority, and fine-tuning a
4B model on this CPU-only machine is not practical. Cheaper fixes for the same
errors come first:
- a year-row check for maturity schedules (as the column correction);
- pairing years with numbers in prose schedules;
- a larger reader model when one fits.

Revisit RAFT if the test-split extraction stays well below 90% after those
fixes, or if a GPU becomes available.

### Fixes after the evaluation (2026-10-17)

All in code; the model's prompts are unchanged except where passages now render
differently.

| Fix | What it does | Effect |
|---|---|---|
| Number IDs in prose | IDs are placed in document order with digit boundaries ("3 million, 3 million and 8 million" no longer stacks two IDs on the first number) | Prose readings point at the right numbers |
| Row label cells | A first cell that is a number gets an ID (schedules laid out by year columns have no row label) | The model can point at every amount |
| Schedule reader | For maturity fields, the fiscal year's row (or "thereafter"), the year's column, or "2029 – $6,556" in prose; a dash is zero. Used when the model finds nothing or its reading is flagged; a clean reading is kept | Delta, Walmart, Deere, Kenvue right |
| Prose year correction | A number for another year moves to the same element's number for the fiscal year end (the prose counterpart of the column correction); a quoted number that is a tagged number in the text is that number | Coca-Cola and McDonald's antidilutive counts right |
| Small quoted numbers | A quoted dollar amount or count below 1,000 without a scale word is rejected | Instacart: a duration no longer read as an amount |
| Stated charges | A tagged one-off whose year's charge the text states differently ("In fiscal 2026, the Company recognized $385 million of ... severance") uses the stated charge, with both in the rationale | Nike severance $385M (the review's false positive) |
| Convertibles | Principal outstanding and conversion price by instrument; conversion shares = principal / price; in `Shares.convertibles` for the if-converted test | Snowflake: two notes of $1,150M at $157.50 |
| Reasons | The model's reason in a proposal is cut to two sentences | Shorter Gate 1 text |

Development calibration after the fixes:

| | Before | After |
|---|---|---|
| Correct | 72% | 96% (24/25) |
| High confidence | 64% | 72% |
| Confident-wrong | 0% | 0% |
| Not found | 1 | 1 (Instacart's unrecognized compensation) |

By field: debt 8/8, leases 13/13, dilution 3/4. The Step 5 check is unchanged
(8/8 within 3%); the normalization review set changes only for Nike ($385M).
650 tests pass.

**RAFT, revisited:** with the reading errors fixed in code, the case for
fine-tuning is weaker still; the decision stands (not now).
