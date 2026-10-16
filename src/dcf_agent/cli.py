"""Command line: value a company from a checked DataBundle and scenario assumptions.

    python -m dcf_agent.cli value KO 2026-10-11 --assumptions examples/assumptions/coca-cola.yaml \\
        --out outputs/ko-model.xlsx

Deterministic: no LLM. The workbook is recalculated with LibreOffice and must
equal the engine to the cent per share.
"""

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml

from dcf_agent.config import load_config
from dcf_agent.contracts.external import DataBundle
from dcf_agent.contracts.inputs import RawFinancials
from dcf_agent.contracts.modeling import ForecastAssumptions, ValuationResult, WACCResult
from dcf_agent.contracts.provenance import Provenance, Sourced
from dcf_agent.data.cache import DataUnavailable
from dcf_agent.data.prices import PriceClient
from dcf_agent.engine.assemble import Assembled, AssemblyError, assemble, lease_rate
from dcf_agent.engine.dcf import EngineError, drivers_from, run_dcf, sensitivity, weighted_value
from dcf_agent.engine.normalize import NormalizationError, Policy, normalize
from dcf_agent.engine.wacc import Regression, WaccError, compute_wacc, regression_beta
from dcf_agent.excel.parity import ParityError, find_soffice, recalculate, verify
from dcf_agent.excel.workbook import ModelInputs, build_workbook

ROOT = Path(__file__).resolve().parents[2]


def find_bundle(ticker: str, as_of: date) -> Path:
    for path in sorted((ROOT / "outputs").glob("*-data.json")):
        try:
            head = json.loads(path.read_text(encoding="utf-8"))["request"]
        except (KeyError, ValueError, OSError):
            continue
        if head.get("ticker") == ticker.upper() and head.get("valuation_date") == str(as_of):
            return path
    raise SystemExit(
        f"No collected bundle for {ticker} at {as_of}; run scripts/collect_data.py first "
        "or pass --bundle"
    )


def year_ends(raw: RawFinancials, as_of: date, count: int) -> tuple[date, ...]:
    """Next fiscal year ends after the valuation date, from the company's own calendar."""
    if not raw.annual:
        raise SystemExit("No annual period to infer the fiscal calendar; give year_ends")
    end = raw.annual[-1].period_end
    while end <= as_of:
        end = (
            end.replace(year=end.year + 1)
            if not (end.month == 2 and end.day == 29)
            else (end + timedelta(days=365))
        )
    return tuple(end.replace(year=end.year + k) for k in range(count))


def scenarios(
    spec: dict[str, Any], raw: RawFinancials, as_of: date, source: str
) -> list[ForecastAssumptions]:
    marginal = float(spec["marginal_tax_rate"])
    result = []
    for name, s in spec["scenarios"].items():
        n = len(s["revenue_growth"])
        ends = (
            tuple(date.fromisoformat(str(d)) for d in s["year_ends"])
            if "year_ends" in s
            else year_ends(raw, as_of, n)
        )

        def item(value: float, end: date | None = None, *, scenario: str = name) -> Sourced[float]:
            return Sourced[float](
                value=float(value),
                prov=Provenance(
                    source="user_override",
                    ref=source,
                    as_of=as_of,
                    period_end=end,
                    confidence="medium",
                    note=f"{scenario} scenario assumption",
                ),
            )

        tax = list(s.get("tax_rate", [marginal] * n))
        result.append(
            ForecastAssumptions(
                as_of=as_of,
                scenario=name,
                probability=float(s["probability"]),
                year_ends=ends,
                revenue_growth=tuple(
                    item(v, e) for v, e in zip(s["revenue_growth"], ends, strict=True)
                ),
                operating_margin=tuple(
                    item(v, e) for v, e in zip(s["operating_margin"], ends, strict=True)
                ),
                sales_to_capital=tuple(
                    item(v, e) for v, e in zip(s["sales_to_capital"], ends, strict=True)
                ),
                tax_rate=tuple(item(v, e) for v, e in zip(tax, ends, strict=True)),
                terminal_growth=item(s["terminal_growth"]),
                terminal_roic=item(s["terminal_roic"]),
                marginal_tax_rate=item(marginal),
                narrative=str(s.get("narrative", f"{name} scenario")),
                rationale={k: str(v) for k, v in s.get("rationale", {"source": source}).items()},
            )
        )
    return result


def market_regression(
    bundle: DataBundle, market: str | None, offline: bool
) -> tuple[Regression | None, str]:
    """Weekly regression of the company's prices on a market index series, if obtainable."""
    if not market:
        return None, "Regression beta not requested"
    if bundle.prices is None:
        return None, "No company price history: bottom-up beta only"
    as_of = bundle.request.valuation_date
    try:
        with PriceClient(load_config(ROOT), offline=offline) as prices:
            index = prices.get_price_history(market, bundle.prices.start, as_of)
        fit = regression_beta(
            [(o.observed_on, o.price.value) for o in bundle.prices.observations],
            [(o.observed_on, o.price.value) for o in index.observations],
        )
    except (DataUnavailable, WaccError) as error:
        return None, f"Regression beta unavailable ({error}): bottom-up beta only"
    return fit, (
        f"Regression beta vs {market}: {fit.beta:.2f} (R^2 {fit.r_squared:.2f}, "
        f"SE {fit.standard_error:.2f}, {fit.observations} weeks)"
    )


def base_sensitivity(
    forecasts: list[ForecastAssumptions], wacc: WACCResult, assembled: Assembled, as_of: date
) -> tuple[list[float], list[float], list[list[float | None]]] | None:
    base = next((f for f in forecasts if f.scenario == "base"), None)
    if base is None:
        return None
    d = drivers_from(base, wacc, assembled.base_revenue, as_of)
    waccs = [round(wacc.wacc.value + step, 6) for step in (-0.005, -0.0025, 0.0, 0.0025, 0.005)]
    growths = [round(d.terminal_growth + step, 6) for step in (-0.005, -0.0025, 0.0, 0.0025, 0.005)]
    bridge = sum(v.value for v in assembled.bridge.values())
    return waccs, growths, sensitivity(d, bridge, assembled.shares, waccs=waccs, growths=growths)


def value(args: argparse.Namespace) -> int:
    as_of = date.fromisoformat(args.date)
    path = Path(args.bundle) if args.bundle else find_bundle(args.ticker, as_of)
    bundle = DataBundle.model_validate_json(path.read_text(encoding="utf-8"))
    spec = yaml.safe_load(Path(args.assumptions).read_text(encoding="utf-8"))
    marginal = float(spec["marginal_tax_rate"])
    regression, regression_note = market_regression(bundle, args.market, args.offline)
    try:
        # The assumptions file's marginal rate rules here (forecast taxes converge to it).
        assembled = assemble(
            bundle, marginal_tax=marginal, regression=regression, filings_tax=False
        )
        wacc = compute_wacc(assembled.wacc_inputs)
        normalized, normalize_issues = (
            normalize(
                bundle.raw,
                Policy(
                    marginal_tax_rate=marginal, lease_rate=lease_rate(bundle, wacc.pretax_kd.value)
                ),
            )
            if bundle.raw is not None
            else (None, {})
        )
        forecasts = scenarios(spec, bundle.raw, as_of, str(args.assumptions)) if bundle.raw else []
        results: list[ValuationResult] = [
            run_dcf(
                f,
                wacc,
                base_revenue=assembled.base_revenue,
                bridge=assembled.bridge,
                shares=assembled.shares,
                valuation_date=as_of,
            )
            for f in forecasts
        ]
        weighted = weighted_value(results, {f.scenario: f.probability for f in forecasts})
    except (AssemblyError, EngineError, WaccError, NormalizationError, ValueError) as error:
        print(f"Cannot value {args.ticker}: {error}", file=sys.stderr)
        return 1
    print(f"{bundle.request.company_name} ({bundle.request.ticker}) as of {as_of}")
    print(
        f"  WACC {wacc.wacc.value:.2%} (band {wacc.wacc_band[0]:.2%}-{wacc.wacc_band[1]:.2%}), "
        f"beta {wacc.bottom_up_beta.value:.2f} ({wacc.beta_method})"
    )
    for r in results:
        print(
            f"  {r.scenario:5} EV {r.enterprise_value / 1e9:,.1f}B  equity "
            f"{r.equity_value / 1e9:,.1f}B  value/share {r.value_per_share:,.2f}  "
            f"TV share {r.tv_share_of_ev or 0:.0%}"
        )
    print(f"  Probability-weighted value per share: {weighted:,.2f}")
    if bundle.market is not None:
        print(
            f"  Market price {bundle.market.close.price.value:,.2f} on "
            f"{bundle.market.close.observed_on} (shown for context only)"
        )
    for note in (regression_note, *assembled.notes, *normalize_issues.values()):
        print(f"  note: {note}")
    if not args.out:
        return 0
    out = Path(args.out)
    cells = build_workbook(
        ModelInputs(
            company=bundle.request.company_name,
            ticker=bundle.request.ticker,
            valuation_date=as_of,
            base_revenue=assembled.base_revenue,
            base_label=f"{assembled.base_period.kind} ending {assembled.base_period.period_end}",
            scenarios=forecasts,
            results=results,
            weighted_value=weighted,
            wacc=wacc,
            wacc_inputs=assembled.wacc_inputs,
            bridge=assembled.bridge,
            basic_shares=assembled.shares.basic,
            raw=bundle.raw,
            normalized=normalized,
            notes=(regression_note, *assembled.notes, *normalize_issues.values()),
            sensitivity=base_sensitivity(forecasts, wacc, assembled, as_of),
        ),
        out,
    )
    print(f"  Workbook: {out}")
    if args.no_recalc:
        return 0
    if find_soffice() is None:
        print("  LibreOffice not found: parity not checked (install it or set DCF_SOFFICE)")
        return 1
    try:
        recalculated = recalculate(out, out.parent / "recalculated")
    except ParityError as error:
        print(f"  {error}", file=sys.stderr)
        return 1
    checks = verify(
        recalculated,
        cells,
        value_per_share={r.scenario: r.value_per_share for r in results},
        enterprise_value={r.scenario: r.enterprise_value for r in results},
        weighted=weighted,
        wacc=wacc.wacc.value,
    )
    for c in checks:
        status = "OK " if c.passed else "FAIL"
        print(
            f"  parity {status} {c.label}: engine {c.engine:,.6f} workbook "
            + ("n/a" if c.workbook is None else f"{c.workbook:,.6f}")
        )
    return 0 if all(c.passed for c in checks) else 1


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["chat"]:
        # The interactive chat is a Typer app (dcf_agent.chat); it owns its options.
        from dcf_agent.chat import app

        app(argv[1:], prog_name="dcf_agent chat")
        return 0
    parser = argparse.ArgumentParser(prog="dcf_agent", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("chat", help="Interactive chat (see: chat --help)")
    v = commands.add_parser("value", help="Value a company and build the Excel model")
    v.add_argument("ticker")
    v.add_argument("date", help="Valuation date, YYYY-MM-DD")
    v.add_argument("--assumptions", required=True, help="Scenario assumptions YAML")
    v.add_argument("--bundle", help="DataBundle JSON (default: matching outputs/*-data.json)")
    v.add_argument("--out", help="Excel workbook path")
    v.add_argument("--no-recalc", action="store_true", help="Skip LibreOffice parity check")
    v.add_argument(
        "--market", default="SPY", help="Market index series for regression beta ('' to skip)"
    )
    v.add_argument("--offline", action="store_true", help="Use saved price responses only")
    args = parser.parse_args(argv)
    return value(args)


if __name__ == "__main__":
    raise SystemExit(main())
