"""Phase 4 step 3 exit check: the Forecast Agent on the golden set.

For each company (bundles rebuilt by `check_insights.py`, offline; normalized
history without adjustments; WACC from the default rule): the axis levels chosen
(by the model, or by the default rule with --no-model), how many of the nine
worlds pass the hard bounds, bear/base/bull values, hard and soft flags of the three
scenarios, the spread (bull and bear at least 15% from base), the gap to the market
price, model calls and time. Targets: hard bounds 100%, soft at least 90% of
scenario sets without flags, spread met.
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from dcf_agent.agents.forecast import ForecastAgent
from dcf_agent.config import load_config
from dcf_agent.contracts.external import DataBundle
from dcf_agent.engine.assemble import AssemblyError, assemble, lease_rate, marginal_tax_rate
from dcf_agent.engine.drivers import check_coherence, driver_stats, industry_benchmarks, quick_dcf
from dcf_agent.engine.normalize import NormalizationError, Policy, normalize
from dcf_agent.engine.wacc import WaccError, compute_wacc
from dcf_agent.graph.services import MARGINAL_TAX
from dcf_agent.llm import LocalLLM

ROOT = Path(__file__).resolve().parents[1]
SPREAD = 0.15


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tickers", nargs="*")
    parser.add_argument("--bundles", type=Path, default=ROOT / "outputs/insights-cohort")
    parser.add_argument("--no-model", action="store_true")
    parser.add_argument("--out", type=Path, default=ROOT / "outputs/forecast-check.json")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    config = load_config(ROOT)
    config = config.model_copy(
        update={"settings": config.settings.model_copy(update={"mock_llm": False})}
    )
    rows: list[dict[str, Any]] = []
    paths = sorted(args.bundles.glob("*.json"))
    if args.tickers:
        paths = [p for p in paths if p.stem in args.tickers]
    with LocalLLM(config) as llm:
        agent = ForecastAgent(None if args.no_model else llm)
        for path in paths:
            bundle = DataBundle.model_validate_json(path.read_text(encoding="utf-8"))
            ticker = bundle.request.ticker
            if bundle.raw is None:
                continue
            try:
                assembled = assemble(bundle, marginal_tax=MARGINAL_TAX)
                wacc = compute_wacc(assembled.wacc_inputs)
                tax, _ = marginal_tax_rate(bundle, MARGINAL_TAX)
                policy = Policy(marginal_tax_rate=tax.value, lease_rate=lease_rate(bundle, 0.05))
                normalized, _ = normalize(bundle.raw, policy)
            except (AssemblyError, WaccError, NormalizationError) as error:
                print(f"{ticker:5} not valued: {error}")
                rows.append({"ticker": ticker, "error": str(error)})
                continue
            began = time.monotonic()
            forecast = agent.run(bundle, normalized, wacc, assembled)
            seconds = time.monotonic() - began
            stats = driver_stats(normalized)
            industry = industry_benchmarks(bundle)
            row: dict[str, Any] = {
                "ticker": ticker,
                "chosen_by": forecast.chosen_by,
                "choice": forecast.choice.model_dump(),
                "valid_worlds": sum(
                    bool(w.coherence and w.coherence.valid) for w in forecast.worlds
                ),
                "anchors": {k: v for k, v in vars(forecast.anchors).items() if k != "notes"},
                "notes": forecast.notes,
                "model_calls": forecast.model_calls,
                "seconds": round(seconds, 1),
                "price": bundle.market.close.price.value if bundle.market else None,
            }
            scenarios = forecast.scenarios.scenarios if forecast.scenarios else ()
            values: dict[str, float | None] = {}
            flags: dict[str, Any] = {}
            for item in scenarios:
                preview = quick_dcf(item, wacc, assembled)
                result = check_coherence(
                    item, wacc, history=stats, industry=industry, preview=preview.result
                )
                values[item.scenario] = preview.per_share
                flags[item.scenario] = {"hard": list(result.hard), "soft": list(result.soft)}
            base = values.get("base")
            row.update(
                {
                    "values": values,
                    "flags": flags,
                    "hard_ok": bool(flags) and all(not f["hard"] for f in flags.values()),
                    "soft_ok": bool(flags) and all(not f["soft"] for f in flags.values()),
                    "spread_ok": base is not None
                    and base > 0
                    and (values.get("bull") or 0) >= base * (1 + SPREAD)
                    and (values.get("bear") or 0) <= base * (1 - SPREAD),
                    "stories": {i.scenario: i.narrative for i in scenarios},
                }
            )
            rows.append(row)
            shown = ", ".join(
                f"{s} {v:,.2f}" if v is not None else f"{s} -" for s, v in values.items()
            )
            price = row["price"]
            print(
                f"{ticker:5} {forecast.chosen_by:7} growth {forecast.choice.growth:14} margin "
                f"{forecast.choice.margin:10} reinv {forecast.choice.reinvestment:13} moat "
                f"{forecast.choice.moat:6} | worlds {row['valid_worlds']}/9 | {shown} | price "
                f"{price if price is None else f'{price:,.2f}'} | hard {row['hard_ok']} soft "
                f"{row['soft_ok']} spread {row['spread_ok']} | {row['model_calls']} calls "
                f"{row['seconds']}s",
                flush=True,
            )
            for scenario, f in flags.items():
                for x in (*f["hard"], *f["soft"]):
                    print(f"      {scenario}: {x}")
    valued = [r for r in rows if "values" in r]
    summary = {
        "companies": len(rows),
        "valued": len(valued),
        "hard_ok": sum(r["hard_ok"] for r in valued),
        "soft_ok": sum(r["soft_ok"] for r in valued),
        "spread_ok": sum(r["spread_ok"] for r in valued),
        "model_calls": sum(r["model_calls"] for r in valued),
        "seconds": round(sum(r["seconds"] for r in valued), 1),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2, default=str))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
