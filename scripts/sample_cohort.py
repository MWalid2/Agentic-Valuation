"""Sample an unbiased cohort of active US non-financial 10-K filers and save it.

The SEC ticker list is current, not point-in-time; the saved file makes the
sample reproducible after the list changes.
"""

import argparse
import json
import random
from datetime import date, timedelta
from pathlib import Path

from dcf_agent.config import load_config
from dcf_agent.data.sec import SECClient, SECError

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, default=30)
    parser.add_argument("--seed", type=int, default=20261014)
    parser.add_argument("--as-of", type=date.fromisoformat, default=date(2026, 10, 11))
    parser.add_argument("--out", type=Path, default=ROOT / "scripts/cohorts/random.json")
    args = parser.parse_args()
    selected: list[dict[str, str | None]] = []
    seen: set[str] = set()
    with SECClient(load_config(ROOT)) as sec:
        tickers = sorted(sec.get_ticker_map().items())
        random.Random(args.seed).shuffle(tickers)
        for ticker, cik in tickers:
            if len(selected) == args.size:
                break
            if cik in seen:
                continue
            seen.add(cik)
            try:
                profile = sec.get_company_profile(cik)
                sic = profile["sic"]
                # Financial and real-estate SIC codes (6000-6799) are outside v1 scope.
                if not sic or not sic.isdigit() or 6000 <= int(sic) <= 6799:
                    continue
                annual = [
                    f
                    for f in sec.get_submissions(cik, args.as_of)
                    if f.form == "10-K" and f.filed >= args.as_of - timedelta(days=456)
                ]
            except SECError:
                continue
            if not annual:
                continue
            selected.append({"ticker": ticker, "cik": cik, **profile})
            print(f"{len(selected):2}. {ticker} {profile['name']} (SIC {sic})", flush=True)
    args.out.write_text(json.dumps(selected, indent=2), encoding="utf-8")
    print(f"Saved {len(selected)} companies to {args.out}")


if __name__ == "__main__":
    main()
