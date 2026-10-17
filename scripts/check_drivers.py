"""Phase 4 step 1 exit check: driver statistics, industry benchmarks, bounds and preview.

On the golden set (bundles rebuilt by `check_insights.py`, offline): the normalized
history's driver statistics next to the company's Damodaran industry, for a hand
check; then the hand-written scenarios in `examples/assumptions/` through the
coherence checker and the quick DCF preview (they must pass the hard bounds).
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import yaml

from dcf_agent.cli import scenarios
from dcf_agent.contracts.external import DataBundle
from dcf_agent.engine.assemble import AssemblyError, assemble, lease_rate, marginal_tax_rate
from dcf_agent.engine.drivers import check_coherence, driver_stats, industry_benchmarks, quick_dcf
from dcf_agent.engine.normalize import NormalizationError, Policy, normalize
from dcf_agent.engine.wacc import WaccError, compute_wacc

ROOT = Path(__file__).resolve().parents[1]
POLICY_TAX = 0.25


def pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.1%}"


def ratio(value: float | None) -> str:
    return "-" if value is None else f"{value:.2f}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundles", type=Path, default=ROOT / "outputs/insights-cohort")
    parser.add_argument("--examples", type=Path, default=ROOT / "examples/assumptions")
    parser.add_argument("--out", type=Path, default=ROOT / "outputs/drivers-check.json")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    rows: list[dict[str, Any]] = []
    bundles: dict[str, DataBundle] = {}
    print(
        f"{'':5} {'years':>5} {'CAGR':>7} {'growth sd':>9} {'margin':>7} {'(range)':>15} "
        f"{'trend/yr':>8} {'S/IC':>5} {'ROIC':>7} {'eff tax':>7} | industry: margin, S/IC"
    )
    for path in sorted(args.bundles.glob("*.json")):
        bundle = DataBundle.model_validate_json(path.read_text(encoding="utf-8"))
        bundles[bundle.request.ticker] = bundle
        if bundle.raw is None:
            continue
        tax, _ = marginal_tax_rate(bundle, POLICY_TAX)
        policy = Policy(marginal_tax_rate=tax.value, lease_rate=lease_rate(bundle, 0.05))
        try:
            normalized, _ = normalize(bundle.raw, policy)
        except NormalizationError as error:
            print(f"{path.stem:5} not normalized: {error}")
            continue
        stats = driver_stats(normalized)
        industry = industry_benchmarks(bundle)
        m = stats.margin
        row = {
            "ticker": path.stem,
            "years": len(stats.years),
            "cagr": stats.cagr,
            "growth_volatility": stats.growth.volatility,
            "margin_latest": m.latest,
            "margin_low": m.low,
            "margin_high": m.high,
            "margin_trend": m.trend,
            "sales_to_capital": stats.sales_to_capital.median,
            "roic": stats.roic.median,
            "effective_tax": stats.effective_tax.median,
            "industry": industry.industry if industry else None,
            "industry_margin": industry.margin.value if industry and industry.margin else None,
            "industry_sales_to_capital": industry.sales_to_capital.value
            if industry and industry.sales_to_capital
            else None,
            "notes": list(stats.notes),
        }
        rows.append(row)
        print(
            f"{path.stem:5} {row['years']:>5} {pct(stats.cagr):>7} "
            f"{pct(stats.growth.volatility):>9} {pct(m.latest):>7} "
            f"({pct(m.low)}-{pct(m.high)}){'':>3} {pct(m.trend):>8} "
            f"{ratio(row['sales_to_capital']):>5} {pct(row['roic']):>7} "
            f"{pct(row['effective_tax']):>7} | {row['industry']}: "
            f"{pct(row['industry_margin'])}, {ratio(row['industry_sales_to_capital'])}"
        )
    checks = []
    for spec_path in sorted(args.examples.glob("*.yaml")):
        spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
        # The file names the company ("coca-cola.yaml"), or a ticker inside it.
        words = spec_path.stem.lower().split("-")
        found = bundles.get(str(spec.get("ticker", "")).upper()) or next(
            (
                b
                for b in bundles.values()
                if all(w in b.request.company_name.lower().replace("-", " ") for w in words)
            ),
            None,
        )
        if found is None or found.raw is None:
            print(f"{spec_path.name}: no bundle for this company")
            continue
        bundle, raw = found, found.raw
        try:
            assembled = assemble(
                bundle, marginal_tax=float(spec["marginal_tax_rate"]), filings_tax=False
            )
            wacc = compute_wacc(assembled.wacc_inputs)
        except (AssemblyError, WaccError) as error:
            print(f"{spec_path.name}: cannot value ({error})")
            continue
        as_of = bundle.request.valuation_date
        for item in scenarios(spec, raw, as_of, str(spec_path)):
            preview = quick_dcf(item, wacc, assembled)
            result = check_coherence(
                item, wacc, industry=industry_benchmarks(bundle), preview=preview.result
            )
            checks.append(
                {
                    "file": spec_path.name,
                    "scenario": item.scenario,
                    "hard": list(result.hard),
                    "soft": list(result.soft),
                    "per_share": preview.per_share,
                    "error": preview.error,
                }
            )
            print(
                f"{spec_path.name} {item.scenario:4}: hard {len(result.hard)} soft "
                f"{len(result.soft)}; value/share "
                f"{'-' if preview.per_share is None else f'{preview.per_share:,.2f}'}"
                + "".join(f"\n    {x}" for x in (*result.hard, *result.soft))
            )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"stats": rows, "examples": checks}, indent=2, default=str))


if __name__ == "__main__":
    main()
