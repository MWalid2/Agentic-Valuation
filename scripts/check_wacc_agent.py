"""WACC Agent versus the deterministic CLI result on ten companies.

Bundles are collected automatically (ticker and date only) when missing. The CLI
result is the default rule with the two-year weekly SPY regression
(`cli.market_regression`); the agent adds a five-year monthly regression and the
local model's choice of beta method. Passing: every agent WACC within 0.3
percentage points of the CLI's.
"""

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

from dcf_agent.agents.wacc import WaccAgent
from dcf_agent.cli import market_regression
from dcf_agent.config import load_config
from dcf_agent.contracts.external import DataBundle
from dcf_agent.engine.assemble import AssemblyError, assemble
from dcf_agent.engine.wacc import WaccError, compute_wacc
from dcf_agent.graph.services import MARGINAL_TAX, LiveServices

ROOT = Path(__file__).resolve().parents[1]
COMPANIES = ("AAPL", "KO", "NKE", "MSFT", "NVDA", "CAT", "DE", "BA", "SNOW", "MCD")
TOLERANCE = 0.003


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tickers", nargs="*", default=list(COMPANIES))
    parser.add_argument("--as-of", type=date.fromisoformat, default=date(2026, 10, 11))
    parser.add_argument("--today", type=date.fromisoformat, default=date(2026, 10, 15))
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--no-model", action="store_true", help="Agent without the model")
    parser.add_argument("--model", help="Ollama model for the WACC role, e.g. qwen3:4b")
    parser.add_argument("--bundles", type=Path, default=ROOT / "outputs/wacc-cohort")
    parser.add_argument("--out", type=Path, default=ROOT / "outputs/wacc-agent-check.json")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # any model text
    config = load_config(ROOT)
    if not args.no_model:
        config = config.model_copy(
            update={"settings": config.settings.model_copy(update={"mock_llm": False})}
        )
    if args.model:
        roles = dict(config.models.roles)
        roles["wacc"] = roles["wacc"].model_copy(update={"model": args.model})
        config = config.model_copy(
            update={"models": config.models.model_copy(update={"roles": roles})}
        )
    rows = []
    with LiveServices(config, args.today, offline=args.offline) as live:
        agent = WaccAgent(live.prices.get_price_history, None if args.no_model else live.llm)
        for ticker in args.tickers:
            path = args.bundles / f"{ticker}.json"
            if path.exists():
                bundle = DataBundle.model_validate_json(path.read_text(encoding="utf-8"))
            else:
                route = live.router.route(f"{ticker}, {args.as_of}")
                bundle, _ = live.collect(route.request("info"), None)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(bundle.model_dump_json(), encoding="utf-8")
            row: dict[str, object] = {"ticker": ticker}
            try:
                regression, note = market_regression(bundle, "SPY", args.offline)
                cli = compute_wacc(
                    assemble(bundle, marginal_tax=MARGINAL_TAX, regression=regression).wacc_inputs
                )
                began = time.monotonic()
                result, decision = agent.run(bundle)
                seconds = time.monotonic() - began
            except (AssemblyError, WaccError) as error:
                row["error"] = str(error)
                rows.append(row)
                print(f"{ticker:5} ERROR {error}")
                continue
            diff = result.wacc.value - cli.wacc.value
            row.update(
                cli_wacc=cli.wacc.value,
                cli_method=cli.beta_method,
                agent_wacc=result.wacc.value,
                agent_method=decision.method,
                difference=diff,
                within=abs(diff) <= TOLERANCE,
                flagged=decision.flagged,
                source=decision.source,
                reason=decision.reason,
                rejected=decision.rejected,
                evidence=decision.evidence,
                seconds=round(seconds, 1),
                cli_regression=note,
            )
            rows.append(row)
            print(
                f"{ticker:5} CLI {cli.wacc.value:.2%} ({cli.beta_method:9}) "
                f"agent {result.wacc.value:.2%} ({decision.method:9}, {decision.source}) "
                f"diff {diff * 100:+.2f}pp {'OK ' if abs(diff) <= TOLERANCE else 'OUT'}"
                f"{' FLAGGED' if decision.flagged else ''} {seconds:.1f}s"
            )
            print(f"      {' | '.join(decision.evidence)}")
            print(f"      reason: {decision.reason}")
            if decision.rejected:
                print(f"      rejected: {decision.rejected}")
    passed = [r for r in rows if r.get("within")]
    summary = {
        "companies": len(rows),
        "within_tolerance": len(passed),
        "flagged": sum(bool(r.get("flagged")) for r in rows),
        "errors": [r["ticker"] for r in rows if "error" in r],
        "tolerance_pp": TOLERANCE * 100,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2, default=str))
    print(json.dumps(summary))
    if len(passed) != len(rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
