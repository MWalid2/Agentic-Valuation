"""Check package extraction against recorded filing references and the balance identity.

References are JSON lists of {company, cik, period_start, period_end, values,
source[, accession]}. Each value must match within the Phase 1 tolerance (0.5%).
With --cohort, every company's latest 10-K and 10-Q is also checked for the
balance identity: total liabilities + parent equity + noncontrolling interest
+ temporary-equity claims = total assets. Runs offline by default; no LLM.
"""

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any

from dcf_agent.agents.lines import MappingStore
from dcf_agent.config import load_config
from dcf_agent.contracts.inputs import FinancialConcept
from dcf_agent.data.extraction import Extraction, extract
from dcf_agent.data.sec import Filing, SECClient, SECError, normalize_cik
from dcf_agent.data.xbrl import read_package

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REFERENCES = [
    ROOT / "tests/fixtures/sec/snapshot-reference.json",
    ROOT / "tests/fixtures/sec/extraction-reference.json",
]
ALIASES = {"operating_income": "ebit"}
# Dated cover-page observations are checked by the snapshot checker, not here.
SKIPPED = {"shares_outstanding"}
TOLERANCE = 0.005
CLAIMS: tuple[FinancialConcept, ...] = ("total_liabilities", "equity", "minority_interest")


def filing_for(sec: SECClient, cik: str, as_of: date, reference: dict[str, Any]) -> Filing:
    filings = sec.get_submissions(cik, as_of)
    if "accession" in reference:
        return next(f for f in filings if f.accession == reference["accession"])
    end = date.fromisoformat(reference["period_end"])
    forms = ("10-K",) if reference.get("form", "10-K") == "10-K" else ("10-Q",)
    matches = [f for f in filings if f.form in forms and f.report_date == end]
    if not matches:
        raise SECError(f"No {forms[0]} reporting {end}")
    return max(matches, key=lambda f: f.filed)


def check_references(
    sec: SECClient, files: list[Path], as_of: date, mappings: MappingStore
) -> list[dict[str, Any]]:
    results = []
    for path in files:
        for reference in json.loads(path.read_text(encoding="utf-8")):
            cik = normalize_cik(reference["cik"])
            start = date.fromisoformat(reference["period_start"])
            end = date.fromisoformat(reference["period_end"])
            try:
                filing = filing_for(sec, cik, as_of, reference)
                extraction = extract(
                    read_package(sec, cik, filing, as_of), as_of, mappings.load(cik)
                )
            except (SECError, StopIteration) as error:
                results.append(
                    {"company": reference["company"], "error": str(error) or "Filing not found"}
                )
                continue
            for name, expected in reference["values"].items():
                if name in SKIPPED:
                    continue
                concept = ALIASES.get(name, name)
                item = extraction.periods.get((None, end), {}).get(
                    concept
                ) or extraction.periods.get((start, end), {}).get(concept)
                got = item.value.value if item is not None and item.value is not None else None
                ok = got is not None and abs(got - expected) <= TOLERANCE * max(abs(expected), 1)
                results.append(
                    {
                        "company": reference["company"],
                        "period_end": str(end),
                        "concept": name,
                        "expected": expected,
                        "extracted": got,
                        "status": item.status if item is not None else "absent",
                        "basis": item.basis if item is not None else None,
                        "passed": ok,
                        "source": reference.get("source"),
                    }
                )
    return results


def identity(extraction: Extraction) -> dict[str, Any] | None:
    end = extraction.package.filing.report_date
    if end is None:
        return None
    values = extraction.periods.get((None, end))
    if not values:
        return None
    amounts: dict[FinancialConcept, float] = {}
    concepts: tuple[FinancialConcept, ...] = (*CLAIMS, "total_assets", "preferred_equity")
    for concept in concepts:
        item = values.get(concept)
        if item is not None and item.value is not None:
            amounts[concept] = item.value.value
        elif item is not None and item.status == "not_presented":
            amounts[concept] = 0.0
    if any(c not in amounts for c in (*CLAIMS, "total_assets")):
        return {"status": "incomplete"}
    preferred = values.get("preferred_equity")
    # Preferred stock in permanent equity is already inside parent equity.
    temporary = (
        amounts.get("preferred_equity", 0.0)
        if preferred is not None and "temporary equity" in (preferred.basis or "")
        else 0.0
    )
    claims = values.get("temporary_equity")
    if claims is not None and claims.value is not None:
        temporary += claims.value.value  # e.g. a blank-check company's redeemable shares
    gap = amounts["total_assets"] - sum(amounts[c] for c in CLAIMS) - temporary
    holds = abs(gap) <= max(TOLERANCE * abs(amounts["total_assets"]), 1.0)
    return {"status": "holds" if holds else "gap", "gap": gap, "assets": amounts["total_assets"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--references", type=Path, nargs="*", default=DEFAULT_REFERENCES)
    parser.add_argument("--cohort", type=Path, nargs="*", default=[])
    parser.add_argument("--as-of", type=date.fromisoformat, default=date(2026, 10, 11))
    parser.add_argument("--online", action="store_true", help="Allow SEC downloads")
    parser.add_argument("--out", type=Path, default=ROOT / "outputs/extraction-check.json")
    args = parser.parse_args()
    config = load_config(ROOT)
    mappings = MappingStore(config.settings.project_root / "data/mappings")
    report: dict[str, Any] = {}
    with SECClient(config, offline=not args.online) as sec:
        references = check_references(sec, args.references, args.as_of, mappings)
        report["references"] = references
        identities = []
        tickers: dict[str, str] | None = None
        for cohort in args.cohort:
            for entry in json.loads(cohort.read_text(encoding="utf-8")):
                cik = entry.get("cik")
                if cik is None:
                    tickers = tickers or sec.get_ticker_map()
                    cik = tickers.get(entry["ticker"].upper())
                if cik is None:
                    continue
                cik = normalize_cik(cik)
                for form in ("10-K", "10-Q"):
                    try:
                        options = [
                            f for f in sec.get_submissions(cik, args.as_of) if f.form == form
                        ]
                        if not options:
                            continue
                        filing = max(options, key=lambda f: (f.report_date or date.min, f.filed))
                        extraction = extract(
                            read_package(sec, cik, filing, args.as_of),
                            args.as_of,
                            mappings.load(cik),
                        )
                    except SECError as error:
                        identities.append(
                            {
                                "ticker": entry["ticker"],
                                "form": form,
                                "status": "error",
                                "error": str(error)[:200],
                            }
                        )
                        continue
                    result = identity(extraction)
                    if result is not None:
                        identities.append({"ticker": entry["ticker"], "form": form, **result})
        report["identity"] = identities
    failed = [r for r in references if not r.get("passed")]
    for r in references:
        if "error" in r:
            print(f"ERROR {r['company']}: {r['error']}")
        else:
            mark = "OK " if r["passed"] else "BAD"
            print(
                f"{mark} {r['company']:12} {r['period_end']} {r['concept']:20} "
                f"expected {r['expected']:>18,.0f} got "
                + ("None" if r["extracted"] is None else f"{r['extracted']:,.0f}")
            )
    passed = sum(bool(r.get("passed")) for r in references)
    print(f"References: {passed}/{len(references)} within {TOLERANCE:.1%}")
    if identities:
        counts: dict[str, int] = {}
        for r in identities:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        print("Balance identity:", counts)
        for r in identities:
            if r["status"] == "gap":
                print(
                    f"  gap {r['ticker']} {r['form']}: {r['gap']:,.0f} of assets {r['assets']:,.0f}"
                )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
