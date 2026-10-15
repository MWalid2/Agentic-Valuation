"""Data Agent loop on a cohort: blockers handled or explained, no silent changes.

Each company is collected for the full valuation plan (data, wacc, value), so every
readiness consumer counts. Passing, per company: the loop ends clear, or with its
remaining blockers listed (flagged) and, when the user is asked, a question; and
every difference between the first collection and the final bundle appears in the
changes recorded by a step.
"""

import argparse
import json
import sys
import time
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from dcf_agent.agents.data import DataAgent
from dcf_agent.agents.data_loop import DataLoop, value_changes
from dcf_agent.agents.lines import MappingStore
from dcf_agent.agents.snapshot import SnapshotAgent
from dcf_agent.config import load_config
from dcf_agent.contracts.external import DataBundle
from dcf_agent.graph.services import LiveServices
from dcf_agent.tools.data import DataTools

ROOT = Path(__file__).resolve().parents[1]
PLAN = ("data", "wacc", "value", "answer")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "cohorts",
        type=Path,
        nargs="*",
        default=[ROOT / "scripts/cohorts/targeted.json", ROOT / "scripts/cohorts/random.json"],
    )
    parser.add_argument("--as-of", type=date.fromisoformat, default=date(2026, 10, 11))
    parser.add_argument("--today", type=date.fromisoformat, default=date(2026, 10, 15))
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--no-model", action="store_true")
    parser.add_argument("--out", type=Path, default=ROOT / "outputs/data-loop-check.json")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    config = load_config(ROOT)
    if not args.no_model:
        config = config.model_copy(
            update={"settings": config.settings.model_copy(update={"mock_llm": False})}
        )
    tickers = list(
        dict.fromkeys(
            e["ticker"] for c in args.cohorts for e in json.loads(c.read_text(encoding="utf-8"))
        )
    )
    rows: list[dict[str, Any]] = []
    with LiveServices(config, args.today, offline=args.offline) as live:
        for ticker in tickers:
            route = live.router.route(f"{ticker}, {args.as_of}")
            row: dict[str, Any] = {"ticker": ticker}
            if route.company is None or route.valuation_date is None:
                row["skipped"] = f"not resolved ({route.status})"
                rows.append(row)
                print(f"{ticker:6} skipped: {row['skipped']}")
                continue
            request = route.request("info")
            tools = DataTools(
                request,
                sec=live.sec,
                snapshot_agent=SnapshotAgent(live.llm),
                fred=live.fred,
                damodaran=live.damodaran,
                prices=live.prices,
                mappings=MappingStore(config.settings.project_root / "data/mappings"),
            )
            seen: list[DataBundle] = []

            def collect(tools: DataTools = tools, seen: list[DataBundle] = seen) -> DataBundle:
                bundle = DataAgent().run(tools, price_start=args.as_of - timedelta(days=730))
                seen.append(bundle)
                return bundle

            loop = DataLoop(
                None if args.no_model else live.llm, tools.mappings, online=not args.offline
            )
            began = time.monotonic()
            try:
                final, review = loop.run(collect, PLAN, tools, live.sec)
            except Exception as error:  # record and continue the cohort
                row["error"] = f"{type(error).__name__}: {error}"
                rows.append(row)
                print(f"{ticker:6} ERROR {row['error']}")
                continue
            recorded = {c for s in review.steps for c in s.changes}
            actual = set(value_changes(seen[0], final))
            silent = sorted(actual - recorded)
            explained = review.status == "clear" or (
                bool(review.remaining) and (review.status != "ask_user" or bool(review.question))
            )
            row.update(
                status=review.status,
                actions=[(s.action, s.source) for s in review.steps],
                rejected=[s.rejected for s in review.steps if s.rejected],
                initial_blockers=len(review.steps[0].before) if review.steps else 0,
                remaining=[b.text() for b in review.remaining],
                changes=sorted(recorded),
                silent_changes=silent,
                explained=explained,
                collections=len(seen),
                seconds=round(time.monotonic() - began, 1),
            )
            rows.append(row)
            print(
                f"{ticker:6} {review.status:9} "
                f"{' > '.join(f'{a}({s[0]})' for a, s in row['actions']) or '-':60} "
                f"remaining {len(review.remaining):2} changes {len(recorded):3} "
                f"silent {len(silent)} {row['seconds']}s"
            )
    done = [r for r in rows if "status" in r]
    summary = {
        "companies": len(rows),
        "completed": len(done),
        "status": dict(Counter(r["status"] for r in done)),
        "actions": dict(Counter(a for r in done for a, _ in r["actions"])),
        "by_model": sum(s == "model" for r in done for _, s in r["actions"]),
        "rejected_choices": sum(len(r["rejected"]) for r in done),
        "explained": sum(r["explained"] for r in done),
        "with_silent_changes": [r["ticker"] for r in done if r["silent_changes"]],
        "errors": [r["ticker"] for r in rows if "error" in r],
        "skipped": [r["ticker"] for r in rows if "skipped" in r],
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2, default=str))
    print(json.dumps(summary, indent=2))
    if summary["with_silent_changes"] or summary["explained"] != len(done) or summary["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
