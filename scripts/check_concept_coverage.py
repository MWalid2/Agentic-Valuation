"""Concept coverage on cohorts: status of every valuation concept at each filing's report date.

For each company's latest 10-K and 10-Q: balance-sheet concepts at the report
date, and flows for the longest period ending then (fiscal year or year to
date). No LLM is used; unknown extension lines are counted, not guessed.
"""

import argparse
import json
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from dcf_agent.agents.lines import MappingStore, candidates, classify_unknown_lines
from dcf_agent.config import load_config
from dcf_agent.data.extraction import extract
from dcf_agent.data.sec import SECClient, SECError, normalize_cik
from dcf_agent.data.xbrl import read_package
from dcf_agent.llm import LocalLLM

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cohorts", type=Path, nargs="+")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date(2026, 10, 11))
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--out", type=Path, default=ROOT / "outputs/concept-coverage.json")
    parser.add_argument(
        "--model",
        action="store_true",
        help="Ask the local model about material unknown lines; decisions go to data/mappings",
    )
    args = parser.parse_args()
    config = load_config(ROOT)
    store = MappingStore(config.settings.project_root / "data/mappings")
    llm = None
    if args.model:
        config = config.model_copy(
            update={"settings": config.settings.model_copy(update={"mock_llm": False})}
        )
        llm = LocalLLM(config)
    records: list[dict[str, Any]] = []
    with SECClient(config, offline=args.offline) as sec:
        tickers: dict[str, str] | None = None
        for cohort in args.cohorts:
            for entry in json.loads(cohort.read_text(encoding="utf-8")):
                cik = entry.get("cik")
                if cik is None:
                    tickers = tickers or sec.get_ticker_map()
                    cik = tickers.get(entry["ticker"].upper())
                if cik is None:
                    continue
                cik = normalize_cik(cik)
                try:
                    filings = [f for f in sec.get_submissions(cik, args.as_of) if f.report_date]
                except SECError:
                    continue
                for form in ("10-K", "10-Q"):
                    options = [f for f in filings if f.form == form]
                    if not options:
                        continue
                    filing = max(options, key=lambda f: (f.report_date or date.min, f.filed))
                    try:
                        package = read_package(sec, cik, filing, args.as_of)
                    except SECError as error:
                        records.append(
                            {
                                "cohort": cohort.stem,
                                "ticker": entry["ticker"],
                                "form": form,
                                "error": str(error),
                            }
                        )
                        continue
                    learned = store.load(cik)
                    extraction = extract(package, args.as_of, learned)
                    if llm is not None and candidates(extraction):
                        learned = classify_unknown_lines(llm, extraction, store)
                        extraction = extract(package, args.as_of, learned)
                    end = filing.report_date
                    assert end is not None
                    concepts: dict[str, Any] = {}
                    flows = [p for p in extraction.periods if p[1] == end and p[0] is not None]
                    selected = [(None, end)] + (
                        [min(flows, key=lambda p: p[0] or date.max)] if flows else []
                    )
                    for period in selected:
                        for concept, item in extraction.periods.get(period, {}).items():
                            concepts.setdefault(
                                concept,
                                {
                                    "status": item.status,
                                    "value": item.value.value if item.value else None,
                                    "basis": item.basis,
                                    "reason": item.reason,
                                },
                            )
                    records.append(
                        {
                            "cohort": cohort.stem,
                            "ticker": entry["ticker"],
                            "form": form,
                            "accession": filing.accession,
                            "concepts": concepts,
                            "unknown_lines": [
                                f"{u.line.label} ({u.side})" for u in extraction.unknown_lines()
                            ],
                        }
                    )
    table: dict[str, Counter[str]] = {}
    for record in records:
        for name, entry in record.get("concepts", {}).items():
            table.setdefault(name, Counter())[entry["status"]] += 1
    statuses = ("reported", "not_presented", "not_disclosed", "unresolved")
    print(f"{'concept':28} " + " ".join(f"{s:>13}" for s in statuses))
    for name, counts in table.items():
        print(f"{name:28} " + " ".join(f"{counts[s]:>13}" for s in statuses))
    errors = [r for r in records if "error" in r]
    print(f"{len(records)} filings; {len(errors)} unreadable")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {"summary": {c: dict(v) for c, v in table.items()}, "filings": records}, indent=2
        ),
        "utf-8",
    )
    print(f"Saved {args.out}")
    if llm is not None:
        llm.close()


if __name__ == "__main__":
    main()
