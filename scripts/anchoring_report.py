"""Phase 4 step 8: anti-anchoring report, and the review file for five companies.

Reads the sessions of `check_value.py` (their checkpoints hold the forecast, the
WACC and the valuation) and reports, across the golden set:

- each company's base and weighted value against its market price (the price is never
  an input to the forecast);
- the correlation of log base value with log price (a value close to 1 with ratios near
  1 everywhere would suggest the model follows the market);
- for five companies, a review entry: drivers, WACC build-up, rationale, and a verdict
  to tick (plausible, too low, too high), written to `evals/forecast/anchoring-review.md`.
"""

import argparse
import json
import math
import sys
from pathlib import Path
from statistics import mean, median
from typing import Any

from dcf_agent.chat import sessions
from dcf_agent.graph.workflow import Services, build_graph, open_checkpointer, thread

ROOT = Path(__file__).resolve().parents[1]
REVIEW = ("KO", "MSFT", "NVDA", "CAT", "NKE")


def correlation(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 3:
        return None
    mx, my = mean(xs), mean(ys)
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if not sx or not sy:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / (sx * sy)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkpoints", type=Path, default=ROOT / "outputs/value-check.sqlite")
    parser.add_argument("--out", type=Path, default=ROOT / "evals/forecast/anchoring-review.md")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    saver = open_checkpointer(args.checkpoints)
    graph = build_graph(Services(router=None, collect=None, wacc=None), saver)  # type: ignore[arg-type]
    latest: dict[str, dict[str, Any]] = {}
    for session in sessions(saver.conn):  # most recent first
        if not session.startswith("value-") or "-quick-" not in session:
            continue
        ticker = session.split("-")[1]
        if ticker in latest:
            continue
        values = dict(graph.get_state(thread(session)).values)
        if values.get("valuation") is not None:
            latest[ticker] = values
    saver.conn.close()
    rows = []
    for ticker, state in sorted(latest.items()):
        valuation = state["valuation"]
        base = valuation.scenario("base")
        price = valuation.price
        rows.append(
            {
                "ticker": ticker,
                "base": base.value_per_share if base else None,
                "weighted": valuation.weighted,
                "price": price,
                "ratio": valuation.weighted / price if price else None,
            }
        )
    usable = [r for r in rows if r["base"] and r["base"] > 0 and r["price"]]
    corr = correlation(
        [math.log(r["base"]) for r in usable], [math.log(r["price"]) for r in usable]
    )
    ratios = [r["ratio"] for r in rows if r["ratio"] is not None]
    summary = {
        "companies": len(rows),
        "log_correlation_base_vs_price": corr,
        "median_value_to_price": median(ratios) if ratios else None,
        "below_price": sum(r < 1 for r in ratios),
        "within_20pct_of_price": sum(abs(r - 1) <= 0.2 for r in ratios),
    }
    print(json.dumps({"summary": summary, "rows": rows}, indent=2))
    lines = [
        "# Forecast anchoring review",
        "",
        "For each company: is the base case a plausible view of the business, given its "
        "history, industry and guidance? Judge the assumptions, not the distance from the "
        "price (the price is shown for context only and was never an input). Tick one "
        "verdict and say which assumption you would change.",
        "",
        f"Across the golden set: median weighted value / price "
        f"{summary['median_value_to_price']:.2f}; correlation of log base value with log "
        f"price {corr:.2f}; {summary['below_price']} of {len(ratios)} below the price."
        if corr is not None and summary["median_value_to_price"] is not None
        else "",
        "",
    ]
    for ticker in REVIEW:
        if ticker not in latest:
            continue
        state = latest[ticker]
        valuation, wacc, scenarios = state["valuation"], state["wacc"], state["forecast"]
        base = next(s for s in scenarios.scenarios if s.scenario == "base")
        values = {s: valuation.scenario(s).value_per_share for s in ("bear", "base", "bull")}
        tv = valuation.scenario("base").tv_share_of_ev or 0
        g0, g = base.revenue_growth[0].value, base.terminal_growth.value
        m0, m1 = base.operating_margin[0].value, base.operating_margin[-1].value
        lines += [
            f"## {state['request'].company_name} ({ticker})",
            "",
            f"- Weighted value {valuation.weighted:,.2f} (bear {values['bear']:,.2f}, base "
            f"{values['base']:,.2f}, bull {values['bull']:,.2f}); market price "
            f"{valuation.price:,.2f}.",
            f"- WACC {wacc.wacc.value:.2%} (risk-free {wacc.rf.value:.2%}, ERP "
            f"{wacc.erp.value:.2%}, beta {wacc.relevered_beta.value:.2f} {wacc.beta_method}); "
            f"terminal WACC {wacc.terminal_wacc.value:.2%}.",
            f"- Base: growth {g0:.1%} in year 1 to {g:.1%} terminal; margin {m0:.1%} to "
            f"{m1:.1%}; sales to capital {base.sales_to_capital[0].value:.2f}; terminal ROIC "
            f"{base.terminal_roic.value:.1%}; terminal value {tv:.0%} of EV.",
        ]
        for key, text in base.rationale.items():
            lines.append(f"  - {key}: {text[:260]}")
        lines += [
            "- Your verdict: [ ] plausible  [ ] too low  [ ] too high  "
            "Assumption you would change: ",
            "",
        ]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines), encoding="utf-8")
    print(f"Review file: {args.out}")


if __name__ == "__main__":
    main()
