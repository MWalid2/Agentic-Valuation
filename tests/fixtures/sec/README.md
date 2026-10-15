# SEC recorded fixture

The `*-snapshot.html` fixtures retain the same filings' statement tables and
cover-page share observations, with their referenced inline-XBRL contexts/units.
For Nike, the income statement is the tagged primary statement rather than the
untagged MD&A comparison table. `snapshot-reference.json` records manually checked
values and source URLs. Expected amounts are never included in model prompts.

Snapshot definitions include parent net income/equity and cash PPE purchases.
Nike operating income is gross profit less total SG&A (19,911 - 16,114 million),
not EBIT. Coca-Cola liabilities sum 21,281 + 42,119 + 4,735 + 2,406 million;
Nike liabilities sum 12,547 + 5,942 + 2,613 + 2,443 million. Coca-Cola's balance
identity also includes 2,106 million of noncontrolling interests. Nike shares
sum class A and B, retaining the cover-page observation date.

The current agent tests use reduced original balance-sheet HTML tables and their
inline XBRL contexts/units from these public filings:

- `coca-cola-balance-sheet.html`: CIK 0000021344, accession 0001628280-26-010047,
  report 2025-12-31, filed 2026-02-20, `ko-20251231.htm`.
- `nike-balance-sheet.html`: CIK 0000320187, accession 0000320187-26-000088,
  report 2026-05-31, filed 2026-07-15, `nke-20260531.htm`.
- `apple-balance-sheet.html`: CIK 0000320193, accession 0000320193-25-000079,
  report 2025-09-27, filed 2025-10-31, `aapl-20250927.htm`.

Tables were extracted from local SEC snapshots downloaded on 2026-10-13. Values,
labels, scales, signs, and tags are preserved; unrelated document sections are
omitted. No contact headers or credentials are included. Reference totals are
checked by the host, never supplied in the model prompt.

The JSON fixture below is a historical artifact from the previous period builder.

`apple-2024-companyfacts.json` is a reduced public SEC companyfacts response for
Apple (CIK 0000320193), downloaded on 2026-10-12 from
`https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json`.

It retains supported standard tags at the FY2024 quarter ends, fiscal-year-start
duration or instantaneous contexts, and filings eligible by 2025-02-01.
Original and latest eligible disclosures are retained for each context so later
comparative fiscal labels do not replace original labels. Unrelated periods and
concepts are omitted. No contact headers or local credentials are included.

On 2026-10-13, the fixture was extended from the same cached response to include
commercial paper and current/noncurrent term debt under the same cutoff and
context selection rules. FY2024 total debt of $106.629B is cross-checked with
Apple's [FY2025 filing's comparative FY2024 balance sheet](https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/aapl-20250927.htm).

This checks parsing and period building against recorded public data; it is not
the Phase 1 three-company reconciliation against filing statement tables.
