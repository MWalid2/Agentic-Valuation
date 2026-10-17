"""The quick summary: value range against the price, three drivers, three risks, a grid.

Drivers are ranked by a tornado on the base scenario: each driver moved down and up
by a fixed step (the rest held), the engine revalues, and the widest swings in value
per share come first. Risks are the Critic's issues (most severe first), then the
10-K's risk factors. Every number here comes from the engine or the state.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any

from dcf_agent.contracts.external import DataBundle
from dcf_agent.contracts.modeling import CompsResult, ForecastScenarios, WACCResult
from dcf_agent.contracts.workflow import CritiqueReport
from dcf_agent.engine.assemble import assemble
from dcf_agent.engine.dcf import Drivers, EngineError, dilute, drivers_from, project
from dcf_agent.engine.valuation import Valuation

MARGINAL_TAX = 0.25


@dataclass(frozen=True)
class Swing:
    driver: str
    step: str  # how far it was moved, in words
    low: float  # value per share with the driver moved down
    high: float  # ... moved up

    @property
    def width(self) -> float:
        return abs(self.high - self.low)


@dataclass(frozen=True)
class Range:
    label: str
    low: float
    high: float
    mark: float | None = None  # a point inside the range (weighted value, median, last price)
    source: str = ""


@dataclass
class Summary:
    ticker: str
    company: str
    as_of: date
    weighted: float
    low: float
    high: float
    base: float | None
    price: float | None
    price_on: date | None
    scenarios: dict[str, tuple[float, float]]  # scenario: (probability, value per share)
    drivers: list[Swing]
    risks: list[str]
    waccs: tuple[float, ...]
    growths: tuple[float, ...]
    grid: tuple[tuple[float | None, ...], ...]
    ranges: list[Range] = field(default_factory=list)
    critic: CritiqueReport | None = None

    @property
    def gap(self) -> float | None:
        """Weighted value against the price (+ = above the price)."""
        return self.weighted / self.price - 1 if self.price else None


SHOCKS: tuple[tuple[str, str, Any], ...] = (
    (
        "Revenue growth",
        "+/-1 point a year",
        lambda d, s: replace(d, revenue_growth=tuple(g + s * 0.01 for g in d.revenue_growth)),
    ),
    (
        "Operating margin",
        "+/-1 point",
        lambda d, s: replace(d, operating_margin=tuple(m + s * 0.01 for m in d.operating_margin)),
    ),
    (
        "Sales to capital",
        "+/-10%",
        lambda d, s: replace(
            d, sales_to_capital=tuple(x * (1 + s * 0.10) for x in d.sales_to_capital)
        ),
    ),
    (
        "WACC",
        "+/-0.5 point",
        lambda d, s: replace(
            d,
            wacc=d.wacc + s * 0.005,
            terminal_wacc=d.terminal_wacc + s * 0.005,
            wacc_path=tuple(w + s * 0.005 for w in d.wacc_path) if d.wacc_path else None,
        ),
    ),
    (
        "Terminal growth",
        "+/-0.5 point",
        lambda d, s: replace(d, terminal_growth=d.terminal_growth + s * 0.005),
    ),
    (
        "Terminal ROIC",
        "+/-2 points",
        lambda d, s: replace(d, terminal_roic=max(0.01, d.terminal_roic + s * 0.02)),
    ),
)


def tornado(d: Drivers, bridge: float, shares: Any) -> list[Swing]:
    """Each driver down and up; the engine's value per share; widest swing first."""

    def value(changed: Drivers) -> float | None:
        try:
            return dilute(project(changed).enterprise_value + bridge, shares).value_per_share
        except EngineError:
            return None

    swings = []
    for name, step, shock in SHOCKS:
        down, up = value(shock(d, -1)), value(shock(d, 1))
        if down is not None and up is not None:
            swings.append(Swing(name, step, down, up))
    return sorted(swings, key=lambda s: -s.width)


def price_range(bundle: DataBundle) -> Range | None:
    """The 52-week low and high closes before the valuation date, and the last close."""
    if bundle.prices is None:
        return None
    as_of = bundle.request.valuation_date
    year = [
        o
        for o in bundle.prices.observations
        if as_of - timedelta(days=365) <= o.observed_on <= as_of
    ]
    if not year:
        return None
    values = [o.price.value for o in year]
    return Range(
        "52-week price",
        min(values),
        max(values),
        year[-1].price.value,
        f"{bundle.prices.source} closes {year[0].observed_on} to {year[-1].observed_on}",
    )


def headline(passage: str) -> str:
    """A risk factor's first sentence."""
    text = " ".join(passage.split())
    end = text.find(". ")
    return text[: end + 1] if 0 < end < 300 else text[:300]


def build_summary(state: dict[str, Any], risk_factors: Sequence[str] = ()) -> Summary:
    """The summary of a valued session; `risk_factors` are passages from the 10-K."""
    bundle: DataBundle = state["data"]
    wacc: WACCResult = state["wacc"]
    scenarios: ForecastScenarios = state["forecast"]
    valuation: Valuation = state["valuation"]
    comps: CompsResult | None = state.get("comps")
    critic: CritiqueReport | None = state.get("critique")
    assembled = assemble(bundle, marginal_tax=MARGINAL_TAX)
    base = next(s for s in scenarios.scenarios if s.scenario == "base")
    d = drivers_from(base, wacc, assembled.base_revenue, valuation.as_of)
    swings = tornado(d, sum(v.value for v in assembled.bridge.values()), assembled.shares)
    flagged: list[str] = []
    if critic is not None:
        order = {"high": 0, "medium": 1, "low": 2}
        for issue in sorted(critic.issues, key=lambda i: order[i.severity]):
            flagged.append(f"{issue.description} ({issue.severity}, Critic)")
    filed = [f"{headline(text)} (10-K risk factors)" for text in risk_factors]
    # Up to two of the Critic's, then the filing's, then the rest.
    risks = (flagged[:2] + filed + flagged[2:])[:3]
    base_result = valuation.scenario("base")
    ranges = [
        Range(
            "DCF scenarios (bear to bull)",
            valuation.low,
            valuation.high,
            valuation.weighted,
            "engine: probability-weighted value marked",
        )
    ]
    if comps is not None and comps.implied_value_range is not None:
        low, high = comps.implied_value_range
        ranges.append(
            Range(
                f"Comps ({len(comps.peers)} peers, median multiples)",
                low,
                high,
                None,
                comps.calculation_ref,
            )
        )
    if swings:
        widest = swings[0]
        ranges.append(
            Range(
                f"Base, {widest.driver.lower()} {widest.step}",
                min(widest.low, widest.high),
                max(widest.low, widest.high),
                base_result.value_per_share if base_result else None,
                "engine: tornado on the base scenario",
            )
        )
    market = price_range(bundle)
    if market is not None:
        ranges.append(market)
    return Summary(
        ticker=bundle.request.ticker,
        company=bundle.request.company_name,
        as_of=valuation.as_of,
        weighted=valuation.weighted,
        low=valuation.low,
        high=valuation.high,
        base=base_result.value_per_share if base_result else None,
        price=valuation.price,
        price_on=valuation.price_on,
        scenarios={
            r.scenario: (valuation.probabilities[r.scenario], r.value_per_share)
            for r in valuation.results
        },
        drivers=swings[:3],
        risks=risks,
        waccs=valuation.waccs,
        growths=valuation.growths,
        grid=valuation.grid,
        ranges=ranges,
        critic=critic,
    )
