"""History and consumer readiness for a cohort, without the LLM snapshot.

Each company is seeded from its latest 10-K's own XBRL extraction, as in the
Data Agent (`filing_seed`); history then uses the same package-first collection.
Stored line decisions are reused; no model is called.
"""

import argparse
import json
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from dcf_agent.agents.lines import MappingStore
from dcf_agent.config import load_config
from dcf_agent.data.cache import DataUnavailable
from dcf_agent.data.readiness import assess_readiness
from dcf_agent.data.scope import assess_scope
from dcf_agent.data.sec import SECClient, SECError, normalize_cik
from dcf_agent.tools.data import collect_history, filing_seed

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cohorts", type=Path, nargs="+")
    parser.add_argument("--as-of", type=date.fromisoformat, default=date(2026, 10, 11))
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--out", type=Path, default=ROOT / "outputs/history-cohort.json")
    args = parser.parse_args()
    config = load_config(ROOT)
    mappings = MappingStore(config.settings.project_root / "data/mappings")
    reports: list[dict[str, Any]] = []
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
                report: dict[str, Any] = {"cohort": cohort.stem, "ticker": entry["ticker"]}
                try:
                    raw, issues, extractions = collect_history(
                        sec, filing_seed(sec, cik, args.as_of, mappings).seed, mappings, []
                    )
                except (SECError, DataUnavailable, ValueError) as error:
                    report["error"] = str(error)[:300]
                    reports.append(report)
                    print(f"{entry['ticker']:6} ERROR {report['error'][:120]}", flush=True)
                    continue
                readiness = assess_readiness(raw, issues)
                latest = max(
                    extractions,
                    key=lambda e: e.package.filing.report_date or date.min,
                    default=None,
                )
                readiness["scope"] = assess_scope(sec.get_company_profile(cik), latest)
                report.update(
                    {
                        "annual": len(raw.annual),
                        "quarters": len(raw.quarterly),
                        "ttm": str(raw.ttm.period_end) if raw.ttm else None,
                        "successor": issues.get("successor"),
                        "readiness": {
                            c: {"status": r.status, "missing": r.missing}
                            for c, r in readiness.items()
                        },
                    }
                )
                reports.append(report)
                states = " ".join(
                    f"{c[:4]}={'OK' if r.status == 'needs_review' else 'BLOCK'}"
                    for c, r in readiness.items()
                )
                print(
                    f"{entry['ticker']:6} {len(raw.annual)}A {len(raw.quarterly)}Q "
                    f"TTM={'y' if raw.ttm else 'n'} {states}",
                    flush=True,
                )
    passed = Counter(
        c
        for r in reports
        for c, v in r.get("readiness", {}).items()
        if v["status"] == "needs_review"
    )
    reasons: Counter[str] = Counter()
    for r in reports:
        for v in r.get("readiness", {}).values():
            for key, items in v["missing"].items():
                for item in items:
                    reasons[item if ":" in key else f"{key}: {item}"] += 1
    summary = {
        "companies": len(reports),
        "errors": sum("error" in r for r in reports),
        "needs_review": dict(passed),
        "top_blocking_reasons": reasons.most_common(25),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"summary": summary, "companies": reports}, indent=2), "utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
