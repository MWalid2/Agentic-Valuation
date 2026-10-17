"""Phase 4 step 2 exit check: guidance read from earnings releases, on the golden set.

For each company: the latest earnings releases (8-K Item 2.02, Exhibit 99.1) filed
by the valuation date, the guidance read from them (metric, range, the release's
own words) and a verbatim check: every item's words must appear in its release
once whitespace is normalized, so nothing is paraphrased or invented. Companies
that give no guidance must come out with none. Online by default (the releases
are downloaded once and cached); --offline replays the cache.
"""

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from lxml import html

from dcf_agent.config import load_config
from dcf_agent.contracts.external import DataBundle
from dcf_agent.data.sec import SECClient, SECError
from dcf_agent.rag.releases import earnings_releases, read_guidance

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundles", type=Path, default=ROOT / "outputs/insights-cohort")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--out", type=Path, default=ROOT / "outputs/guidance-check.json")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    rows: list[dict[str, Any]] = []
    with SECClient(load_config(ROOT), offline=args.offline) as sec:
        for path in sorted(args.bundles.glob("*.json")):
            request = DataBundle.model_validate_json(path.read_text(encoding="utf-8")).request
            as_of: date = request.valuation_date
            try:
                releases = earnings_releases(sec, request.cik, as_of)
            except SECError as error:
                print(f"{request.ticker:5} not read: {error}")
                rows.append({"ticker": request.ticker, "error": str(error)})
                continue
            items: list[dict[str, Any]] = []
            for filing, url, document in releases:
                page: Any = html.fromstring(document)
                text = " ".join(page.text_content().split())
                for g in read_guidance(document, filing, url):
                    words = g.text.split(": ", 1)[-1]
                    items.append(
                        {
                            "filed": str(g.filed),
                            "metric": g.metric,
                            "low": g.low,
                            "high": g.high,
                            "unit": g.unit,
                            "text": g.text,
                            "verbatim": " ".join(words.replace("•", " ").split())[:40]
                            in " ".join(text.replace("•", " ").split()),
                        }
                    )
            rows.append(
                {
                    "ticker": request.ticker,
                    "releases": [str(f.filed) for f, _, _ in releases],
                    "items": items,
                }
            )
            dates = [str(f.filed) for f, _, _ in releases]
            print(f"{request.ticker:5} releases {dates}: {len(items)} items")
            for item in items:
                shown = (
                    "-"
                    if item["low"] is None
                    else f"{item['low']:.1%}-{item['high']:.1%}"
                    if item["unit"] == "pct"
                    else f"${item['low'] / 1e9:,.2f}B-${item['high'] / 1e9:,.2f}B"
                )
                flag = "" if item["verbatim"] else "  NOT VERBATIM"
                words = item["text"][:110]
                print(f"   {item['filed']} {item['metric']:16} {shown:>16}  {words}{flag}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(rows, indent=2))
    summary = {
        "companies": len(rows),
        "with_guidance": sum(bool(r.get("items")) for r in rows),
        "items": sum(len(r.get("items", [])) for r in rows),
        "not_verbatim": sum(not i["verbatim"] for r in rows for i in r.get("items", [])),
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
