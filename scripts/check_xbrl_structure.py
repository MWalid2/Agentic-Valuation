"""Measure structural statement identification on a cohort (latest 10-K and 10-Q each).

Reports, per filing: which primary statements were identified, their titles,
extension (company-specific) line items, issuer calculation consistency, and
package issues. No LLM is used.
"""

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any

from dcf_agent.config import load_config
from dcf_agent.data.sec import SECClient, SECError, normalize_cik
from dcf_agent.data.xbrl import StatementKind, read_package

ROOT = Path(__file__).resolve().parents[1]
KINDS: tuple[StatementKind, ...] = ("balance_sheet", "income_statement", "cash_flow")


def inspect(sec: SECClient, cik: str, as_of: date) -> list[dict[str, Any]]:
    filings = [f for f in sec.get_submissions(cik, as_of) if f.report_date is not None]
    records: list[dict[str, Any]] = []
    for form in ("10-K", "10-Q"):
        options = [f for f in filings if f.form == form]
        if not options:
            records.append({"form": form, "error": "No eligible filing"})
            continue
        filing = max(options, key=lambda f: (f.report_date or date.min, f.filed))
        record: dict[str, Any] = {
            "form": form,
            "accession": filing.accession,
            "report_date": str(filing.report_date),
        }
        try:
            package = read_package(sec, cik, filing, as_of)
        except SECError as error:
            records.append({**record, "error": str(error)})
            continue
        statements: dict[str, Any] = {}
        for kind in KINDS:
            statement = package.statements.get(kind)
            if statement is None:
                continue
            checks = package.check_calculations(kind)
            items = [line for line in statement.lines if not line.abstract]
            statements[kind] = {
                "title": statement.title,
                "line_items": len(items),
                "custom_items": [f"{line.label} ({line.concept})" for line in items if line.custom],
                "calculations": len(checks),
                "inconsistent": [
                    f"{c.parent} {c.start}/{c.end}: reported {c.reported:g}, sum {c.computed:g}"
                    for c in checks
                    if c.status == "inconsistent"
                ],
                "incomplete": [
                    f"{c.parent} {c.start}/{c.end}: missing {', '.join(c.missing)}"
                    for c in checks
                    if c.status == "incomplete"
                ],
            }
        records.append(
            {
                **record,
                "fiscal": f"{package.fiscal_year} {package.fiscal_period}",
                "statements": statements,
                "issues": package.issues,
                "location_tags": {
                    concept: list(named)
                    for (concept, _, end), named in package.enumerations.items()
                    if end == filing.report_date and "StatementOfFinancialPosition" in concept
                },
            }
        )
    return records


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cohort", type=Path)
    parser.add_argument("--as-of", type=date.fromisoformat, default=date(2026, 10, 11))
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    entries = json.loads(args.cohort.read_text(encoding="utf-8"))
    out = args.out or ROOT / "outputs" / f"xbrl-structure-{args.cohort.stem}.json"
    reports = []
    with SECClient(load_config(ROOT), offline=args.offline) as sec:
        tickers = None
        for entry in entries:
            cik = entry.get("cik")
            if cik is None:
                tickers = tickers or sec.get_ticker_map()
                cik = tickers.get(entry["ticker"].upper())
            if cik is None:
                reports.append({**entry, "error": "Ticker not in SEC list"})
                continue
            try:
                filings = inspect(sec, normalize_cik(cik), args.as_of)
            except SECError as error:
                filings = [{"error": str(error)}]
            reports.append({**entry, "cik": normalize_cik(cik), "filings": filings})
            for record in filings:
                found = record.get("statements", {})
                customs = sum(len(s["custom_items"]) for s in found.values())
                bad = sum(len(s["inconsistent"]) for s in found.values())
                partial = sum(len(s["incomplete"]) for s in found.values())
                checks = sum(s["calculations"] for s in found.values())
                print(
                    f"{entry['ticker']:6} {record.get('form', '?'):5} "
                    + (
                        f"ERROR {record['error']}"
                        if "error" in record
                        else f"{len(found)}/3 statements; {customs} custom lines; "
                        f"calc {checks - bad - partial}/{checks} consistent, "
                        f"{partial} incomplete, {bad} inconsistent"
                        + "".join(
                            f"; {k}: {v}"
                            for k, v in record["issues"].items()
                            if k.startswith("statement")
                        )
                    ),
                    flush=True,
                )
    filings = [r for report in reports for r in report.get("filings", [])]
    complete = [r for r in filings if len(r.get("statements", {})) == 3]
    checks = sum(s["calculations"] for r in filings for s in r.get("statements", {}).values())
    bad = sum(len(s["inconsistent"]) for r in filings for s in r.get("statements", {}).values())
    partial = sum(len(s["incomplete"]) for r in filings for s in r.get("statements", {}).values())
    summary = {
        "companies": len(reports),
        "filings": len(filings),
        "all_three_statements": len(complete),
        "calculations": checks,
        "incomplete_calculations": partial,
        "inconsistent_calculations": bad,
        "filings_with_lease_location_tags": sum(bool(r.get("location_tags")) for r in filings),
        "filings_with_custom_lines": sum(
            any(s["custom_items"] for s in r.get("statements", {}).values()) for r in filings
        ),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "companies": reports}, indent=2), "utf-8")
    print(json.dumps(summary, indent=2))
    print(f"Saved {out}")


if __name__ == "__main__":
    main()
