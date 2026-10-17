"""The valuation of approved scenarios: DCF per scenario, weighted value, sensitivity.

A thin layer over `engine.dcf` for the graph: it assembles the bridge and the
shares from the bundle, values each scenario, weights them by probability, runs the
base scenario's WACC x terminal growth grid, and returns one validated record the
checkpoint can store. Optionally it writes the Excel workbook (the full model).
"""

from datetime import date
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from dcf_agent.contracts.accounting import NormalizedFinancials
from dcf_agent.contracts.external import DataBundle
from dcf_agent.contracts.modeling import ForecastScenarios, ValuationResult, WACCResult
from dcf_agent.engine.assemble import Assembled, assemble
from dcf_agent.engine.dcf import drivers_from, run_dcf, sensitivity, weighted_value

STEPS = (-0.005, -0.0025, 0.0, 0.0025, 0.005)  # sensitivity steps around base WACC and growth


class Valuation(BaseModel):
    """Values per scenario, the weighted value, and the base scenario's sensitivity grid."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    as_of: date
    results: tuple[ValuationResult, ...] = Field(min_length=1)
    probabilities: dict[str, float]
    weighted: float
    low: float  # the lowest scenario value per share
    high: float
    price: float | None = None
    price_on: date | None = None
    waccs: tuple[float, ...] = ()
    growths: tuple[float, ...] = ()
    grid: tuple[tuple[float | None, ...], ...] = ()
    notes: tuple[str, ...] = ()
    workbook: str | None = None

    def scenario(self, name: str) -> ValuationResult | None:
        return next((r for r in self.results if r.scenario == name), None)


def value_scenarios(
    bundle: DataBundle,
    scenarios: ForecastScenarios,
    wacc: WACCResult,
    marginal_tax: float,
    assembled: Assembled | None = None,
) -> Valuation:
    """Every scenario through the engine; the base one also through the sensitivity grid."""
    assembled = assembled or assemble(bundle, marginal_tax=marginal_tax)
    as_of = bundle.request.valuation_date
    results = tuple(
        run_dcf(
            item,
            wacc,
            base_revenue=assembled.base_revenue,
            bridge=assembled.bridge,
            shares=assembled.shares,
            valuation_date=as_of,
        )
        for item in scenarios.scenarios
    )
    probabilities: dict[str, float] = {s.scenario: s.probability for s in scenarios.scenarios}
    weighted = weighted_value(results, probabilities)
    base = next(s for s in scenarios.scenarios if s.scenario == "base")
    d = drivers_from(base, wacc, assembled.base_revenue, as_of)
    waccs = tuple(round(wacc.wacc.value + step, 6) for step in STEPS)
    growths = tuple(round(d.terminal_growth + step, 6) for step in STEPS)
    bridge = sum(v.value for v in assembled.bridge.values())
    grid = sensitivity(d, bridge, assembled.shares, waccs=list(waccs), growths=list(growths))
    market = bundle.market
    return Valuation(
        as_of=as_of,
        results=results,
        probabilities=probabilities,
        weighted=weighted,
        low=min(r.value_per_share for r in results),
        high=max(r.value_per_share for r in results),
        price=market.close.price.value if market is not None else None,
        price_on=market.close.observed_on if market is not None else None,
        waccs=waccs,
        growths=growths,
        grid=tuple(tuple(row) for row in grid),
        notes=tuple(assembled.notes),
    )


def write_workbook(
    bundle: DataBundle,
    scenarios: ForecastScenarios,
    wacc: WACCResult,
    valuation: Valuation,
    normalized: NormalizedFinancials | None,
    marginal_tax: float,
    out: Path,
) -> tuple[Path, Any]:
    """The full model as an Excel workbook (formulas, as `cli value --out` writes it).

    Returns the path and the workbook's cell map, for the parity check.
    """
    from dcf_agent.excel.workbook import ModelInputs, build_workbook

    assembled = assemble(bundle, marginal_tax=marginal_tax)
    out.parent.mkdir(parents=True, exist_ok=True)
    cells = build_workbook(
        ModelInputs(
            company=bundle.request.company_name,
            ticker=bundle.request.ticker,
            valuation_date=valuation.as_of,
            base_revenue=assembled.base_revenue,
            base_label=f"{assembled.base_period.kind} ending {assembled.base_period.period_end}",
            scenarios=scenarios.scenarios,
            results=valuation.results,
            weighted_value=valuation.weighted,
            wacc=wacc,
            wacc_inputs=assembled.wacc_inputs,
            bridge=assembled.bridge,
            basic_shares=assembled.shares.basic,
            raw=bundle.raw,
            normalized=normalized,
            notes=assembled.notes,
            sensitivity=(
                list(valuation.waccs),
                list(valuation.growths),
                [list(r) for r in valuation.grid],
            )
            if valuation.grid
            else None,
        ),
        out,
    )
    return out, cells
