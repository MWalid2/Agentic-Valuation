"""Step 1 exit check: the filing text store on a cohort.

For each company, the latest 10-K filed by the date and the 10-Qs after it are
parsed. Passing, per filing: every displayed numeric fact in exactly one chunk;
every text block (note or table) covered by a chunk; every table with tagged
numbers has its units and a header row; no unresolved continuation.
"""

import argparse
import json
import statistics
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

from dcf_agent.config import load_config
from dcf_agent.data.sec import SECClient, SECError
from dcf_agent.rag.filings import Chunk, FilingText, filing_texts
from dcf_agent.rag.filings import value_columns as columns

ROOT = Path(__file__).resolve().parents[1]


def value_columns(chunk: Chunk) -> int:
    return columns([list(r) for r in chunk.rows])


def headerless(tables: list[Chunk]) -> list[str]:
    """Tables with several value columns and neither a header row nor a lead-in caption."""
    return [
        c.chunk_id
        for c in tables
        if value_columns(c) >= 2 and not c.header_rows and "Caption: " not in c.text
    ]


def measure(text: FilingText, seconds: float) -> dict[str, Any]:
    counts = Counter(f for c in text.chunks for f in c.fact_ids)
    visible = set(text.visible_fact_ids)
    covered = {b for c in text.chunks for b in c.blocks}
    tables = [c for c in text.chunks if c.kind == "table" and c.facts]
    sizes = [c.tokens for c in text.chunks]
    checks = {
        "facts_once": not (visible - set(counts)) and all(v == 1 for v in counts.values()),
        "blocks_covered": set(text.text_blocks) <= covered,
        "table_units": all(c.units for c in tables),
        # Column headers matter when a table has two or more value columns; a lead-in
        # caption is accepted when the table has no header row of its own.
        "table_headers": not headerless(tables),
        "no_issues": not text.issues,
        # Every fact's unit resolved to a measure (USD, shares, pure, ...), not a filer's ID.
        "units_resolved": not text.unresolved_units,
    }
    return {
        "accession": text.accession,
        "form": text.form,
        "passed": all(checks.values()),
        "checks": checks,
        "inline_xbrl": bool(visible),
        "facts": len(visible),
        "facts_missing": len(visible - set(counts)),
        "facts_repeated": sum(v > 1 for v in counts.values()),
        "blocks": len(text.text_blocks),
        "blocks_missing": sorted(set(text.text_blocks) - covered),
        "tables_with_facts": len(tables),
        "tables_without_units": [c.chunk_id for c in tables if not c.units],
        "tables_without_header": headerless(tables),
        "empty_blocks": list(text.empty_blocks),
        "chunks": len(text.chunks),
        "median_tokens": statistics.median(sizes) if sizes else 0,
        "max_tokens": max(sizes, default=0),
        "issues": list(text.issues),
        "seconds": round(seconds, 2),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "cohorts",
        type=Path,
        nargs="*",
        default=[
            ROOT / "scripts/cohorts/reference.json",
            ROOT / "scripts/cohorts/targeted.json",
            ROOT / "scripts/cohorts/random.json",
        ],
    )
    parser.add_argument("--as-of", type=date.fromisoformat, default=date(2026, 10, 11))
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--out", type=Path, default=ROOT / "outputs/filing-text-check.json")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    tickers = list(
        dict.fromkeys(
            e["ticker"] for c in args.cohorts for e in json.loads(c.read_text(encoding="utf-8"))
        )
    )
    rows: list[dict[str, Any]] = []
    with SECClient(load_config(ROOT), offline=args.offline) as sec:
        tick = sec.get_ticker_map()
        for ticker in tickers:
            cik = tick.get(ticker.upper())
            if cik is None:
                rows.append({"ticker": ticker, "error": "not in the SEC ticker list"})
                continue
            began = time.monotonic()
            try:
                texts = filing_texts(sec, cik, args.as_of)
            except SECError as error:
                rows.append({"ticker": ticker, "error": str(error)})
                print(f"{ticker:6} ERROR {error}")
                continue
            seconds = (time.monotonic() - began) / max(len(texts), 1)
            for text in texts:
                row = {"ticker": ticker, **measure(text, seconds)}
                rows.append(row)
                failed = [k for k, ok in row["checks"].items() if not ok]
                print(
                    f"{ticker:6} {text.form:5} {text.accession} "
                    f"{'PASS' if row['passed'] else 'FAIL ' + ','.join(failed):24} "
                    f"facts {row['facts']:5} blocks {row['blocks']:4} "
                    f"tables {row['tables_with_facts']:3} chunks {row['chunks']:4}"
                    f"{'' if row['inline_xbrl'] else ' (no inline XBRL)'}"
                )
    filings = [r for r in rows if "checks" in r]
    summary = {
        "companies": len(tickers),
        "filings": len(filings),
        "passed": sum(r["passed"] for r in filings),
        "without_inline_xbrl": sum(not r["inline_xbrl"] for r in filings),
        "checks": {name: sum(r["checks"][name] for r in filings) for name in filings[0]["checks"]}
        if filings
        else {},
        "facts": sum(r["facts"] for r in filings),
        "facts_missing": sum(r["facts_missing"] for r in filings),
        "facts_repeated": sum(r["facts_repeated"] for r in filings),
        "errors": [r["ticker"] for r in rows if "error" in r],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2, default=str))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
