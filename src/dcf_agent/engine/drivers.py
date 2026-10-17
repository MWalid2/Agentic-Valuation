"""Forecast tools in code: driver statistics, industry benchmarks, fades, checks, preview.

Everything the Forecast Agent's numbers are built from, without a model:

- `driver_stats`: the company's own history from `NormalizedFinancials` (growth,
  clean operating margin, sales-to-capital, ROIC, reinvestment, tax), per year and
  summarized (mean, median, latest, trend, volatility, range, CAGR);
- `industry_benchmarks`: the Damodaran industry row already in the bundle (lease
  adjusted operating margin, sales to invested capital), each value sourced;
- `fade_path`: a driver held, then faded linearly or convexly to its terminal value;
- `forecast_years`: the next fiscal year ends after the valuation date;
- `check_coherence`: the hard bounds (an assumption set that fails cannot be valued)
  and soft bounds (flagged for Gate 2) of docs/01-architecture.md section 10;
- `quick_dcf`: the engine's value per share for one scenario, for previews.

Margins are on the normalized basis: clean EBIT includes the lease interest
component, so the industry comparison uses Damodaran's lease-adjusted margin.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from statistics import mean, median, pstdev
from typing import Literal

from dcf_agent.contracts.accounting import NormalizedFinancials, NormalizedPeriod
from dcf_agent.contracts.external import DataBundle
from dcf_agent.contracts.modeling import ForecastAssumptions, ValuationResult, WACCResult
from dcf_agent.contracts.provenance import Sourced
from dcf_agent.engine.assemble import Assembled
from dcf_agent.engine.dcf import EngineError, run_dcf

GDP_CAP = 0.04  # long-run nominal growth proxy: terminal growth never above it
HIGH_GROWTH = 0.25  # revenue growth above this for more than five years is flagged
HIGH_GROWTH_YEARS = 5
TV_SHARE = 0.75  # terminal value above this share of enterprise value is flagged
MOAT_SPREAD = 0.05  # terminal ROIC above WACC by more than this needs a moat story
WACC_RANGE = (0.05, 0.15)
INDUSTRY_MARGIN = "Pre-tax Lease adjusted Margin"
INDUSTRY_TURNOVER = "Sales/ Invested Capital (LTM)"


@dataclass(frozen=True)
class Series:
    """One driver over the history: values by fiscal year end, and their summary."""

    name: str
    values: tuple[tuple[date, float], ...]

    @property
    def numbers(self) -> list[float]:
        return [v for _, v in self.values]

    @property
    def latest(self) -> float | None:
        return self.values[-1][1] if self.values else None

    @property
    def mean(self) -> float | None:
        return mean(self.numbers) if self.values else None

    @property
    def median(self) -> float | None:
        return median(self.numbers) if self.values else None

    @property
    def volatility(self) -> float | None:
        return pstdev(self.numbers) if len(self.values) > 1 else None

    @property
    def low(self) -> float | None:
        return min(self.numbers) if self.values else None

    @property
    def high(self) -> float | None:
        return max(self.numbers) if self.values else None

    @property
    def trend(self) -> float | None:
        """Least-squares slope per year (positive: rising)."""
        if len(self.values) < 3:
            return None
        xs = [(d - self.values[0][0]).days / 365.25 for d, _ in self.values]
        ys = self.numbers
        mx, my = mean(xs), mean(ys)
        spread = sum((x - mx) ** 2 for x in xs)
        return (
            sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / spread
            if spread
            else None
        )


@dataclass(frozen=True)
class DriverStats:
    """The company's normalized history, as the forecast's starting evidence."""

    years: tuple[date, ...]
    revenue: tuple[float, ...]
    growth: Series
    margin: Series
    sales_to_capital: Series
    roic: Series
    reinvestment_rate: Series
    effective_tax: Series
    marginal_tax: float | None
    cagr: float | None
    ttm_margin: float | None = None
    ttm_growth: float | None = None
    notes: tuple[str, ...] = ()


def series(
    name: str, periods: Sequence[NormalizedPeriod], pick: Callable[[NormalizedPeriod], float | None]
) -> Series:
    return Series(
        name,
        tuple((p.period_end, v) for p in periods if (v := pick(p)) is not None),
    )


def value(item: Sourced[float] | None) -> float | None:
    return item.value if item is not None else None


def driver_stats(normalized: NormalizedFinancials) -> DriverStats:
    """Growth, margin, capital efficiency, returns and tax over the normalized years."""
    years = sorted(normalized.annual, key=lambda p: p.period_end)
    notes: list[str] = []
    revenue = [p.revenue.value for p in years]
    growth = Series(
        "revenue growth",
        tuple(
            (b.period_end, b.revenue.value / a.revenue.value - 1)
            for a, b in zip(years, years[1:], strict=False)
            if a.revenue.value > 0
        ),
    )
    margin = series(
        "operating margin (clean)",
        years,
        lambda p: p.clean_ebit.value / p.revenue.value if p.revenue.value > 0 else None,
    )
    cagr = None
    if len(years) >= 2 and revenue[0] > 0 and revenue[-1] > 0:
        span = (years[-1].period_end - years[0].period_end).days / 365.25
        cagr = (revenue[-1] / revenue[0]) ** (1 / span) - 1 if span > 0 else None
    if len(years) < 3:
        notes.append(f"short history: {len(years)} fiscal years")
    ttm = normalized.ttm
    ttm_margin = ttm_growth = None
    if ttm is not None and ttm.revenue.value > 0:
        ttm_margin = ttm.clean_ebit.value / ttm.revenue.value
        if years and years[-1].revenue.value > 0 and ttm.period_end > years[-1].period_end:
            ttm_growth = ttm.revenue.value / years[-1].revenue.value - 1
    marginal = years[-1].marginal_tax_rate.value if years else None
    return DriverStats(
        years=tuple(p.period_end for p in years),
        revenue=tuple(revenue),
        growth=growth,
        margin=margin,
        sales_to_capital=series("sales to capital", years, lambda p: value(p.sales_to_capital)),
        roic=series("ROIC", years, lambda p: value(p.roic)),
        reinvestment_rate=series("reinvestment rate", years, lambda p: value(p.reinvestment_rate)),
        effective_tax=series("effective tax rate", years, lambda p: p.effective_tax_rate.value),
        marginal_tax=marginal,
        cagr=cagr,
        ttm_margin=ttm_margin,
        ttm_growth=ttm_growth,
        notes=tuple(notes),
    )


@dataclass(frozen=True)
class Benchmarks:
    """The company's Damodaran industry: averages (the files give no percentiles)."""

    industry: str
    margin: Sourced[float] | None
    sales_to_capital: Sourced[float] | None
    firms: float | None = None


def industry_benchmarks(bundle: DataBundle) -> Benchmarks | None:
    """The industry row of the margin and capital-efficiency tables in the bundle."""
    tables = bundle.benchmarks
    if "margins" not in tables or not tables["margins"].rows:
        return None
    row = tables["margins"].rows[0]
    turnover = None
    if "sales_to_capital" in tables and tables["sales_to_capital"].rows:
        turnover = tables["sales_to_capital"].rows[0].values.get(INDUSTRY_TURNOVER)
    firms = row.values.get("Number of firms")
    return Benchmarks(
        industry=row.name,
        margin=row.values.get(INDUSTRY_MARGIN),
        sales_to_capital=turnover,
        firms=firms.value if firms is not None else None,
    )


def fade_path(
    start: float,
    end: float,
    years: int,
    *,
    hold: int = 0,
    shape: Literal["linear", "convex"] = "linear",
) -> tuple[float, ...]:
    """`years` values: `start` for `hold` years, then fading so the last year is `end`.

    Convex fades move most of the way early (half-life style); linear in equal steps.
    """
    if years < 1:
        raise ValueError("A path needs at least one year")
    hold = max(0, min(hold, years - 1))
    steps = years - hold
    path = [start] * hold
    for k in range(1, steps + 1):
        share = k / steps
        if shape == "convex":
            share = 1 - (1 - share) ** 2
        path.append(start + (end - start) * share)
    return tuple(path)


def years_later(day: date, years: int) -> date:
    """The same calendar day `years` later (February 29 becomes February 28)."""
    try:
        return day.replace(year=day.year + years)
    except ValueError:
        return date(day.year + years, 2, 28)


def forecast_years(last_fiscal_end: date, as_of: date, count: int) -> tuple[date, ...]:
    """The next `count` fiscal year ends after the valuation date, on the company's calendar."""
    end = last_fiscal_end
    while end <= as_of:
        end = years_later(end, 1)
    return tuple(years_later(end, k) for k in range(count))


@dataclass(frozen=True)
class Coherence:
    """Hard failures block valuation; soft flags go to Gate 2 with the scenario."""

    scenario: str
    hard: tuple[str, ...] = ()
    soft: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.hard


def check_coherence(
    assumptions: ForecastAssumptions,
    wacc: WACCResult,
    *,
    history: DriverStats | None = None,
    industry: Benchmarks | None = None,
    preview: ValuationResult | None = None,
) -> Coherence:
    """The section-10 bounds for one assumption set."""
    hard: list[str] = []
    soft: list[str] = []
    g = assumptions.terminal_growth.value
    rf = wacc.rf.value
    roic = assumptions.terminal_roic.value
    if g > rf + 1e-12:
        hard.append(f"terminal growth {g:.2%} above the risk-free rate {rf:.2%}")
    if g > GDP_CAP + 1e-12:
        hard.append(f"terminal growth {g:.2%} above the {GDP_CAP:.0%} long-run cap")
    if g >= wacc.terminal_wacc.value:
        hard.append(f"terminal growth {g:.2%} not below the terminal WACC")
    if g >= roic:
        hard.append(f"terminal growth {g:.2%} not below terminal ROIC {roic:.2%}")
    if abs(assumptions.tax_rate[-1].value - assumptions.marginal_tax_rate.value) > 1e-9:
        hard.append("the tax rate does not converge to the marginal rate")
    if any(s.value <= 0 for s in assumptions.sales_to_capital):
        hard.append("sales-to-capital must be positive")
    drivers = (
        *assumptions.revenue_growth,
        *assumptions.operating_margin,
        *assumptions.sales_to_capital,
        *assumptions.tax_rate,
        assumptions.terminal_growth,
        assumptions.terminal_roic,
    )
    if any(not d.prov.ref.strip() for d in drivers):
        hard.append("a driver without a source")
    # Soft bounds.
    terminal_wacc = wacc.terminal_wacc.value
    if roic < terminal_wacc:
        soft.append(f"terminal ROIC {roic:.2%} below the terminal WACC {terminal_wacc:.2%}")
    if roic > terminal_wacc + MOAT_SPREAD and "moat" not in assumptions.rationale:
        soft.append(
            f"terminal ROIC {roic:.2%} more than {MOAT_SPREAD:.0%} above WACC without a moat "
            "rationale"
        )
    fast = [v.value for v in assumptions.revenue_growth if v.value > HIGH_GROWTH]
    if len(fast) > HIGH_GROWTH_YEARS:
        soft.append(f"revenue growth above {HIGH_GROWTH:.0%} for {len(fast)} years")
    peak = max(m.value for m in assumptions.operating_margin)
    if industry is not None and industry.margin is not None:
        ceiling = max(2 * industry.margin.value, industry.margin.value + 0.15)
        own = history.margin.high if history is not None else None
        if peak > ceiling and (own is None or peak > own + 0.02):
            soft.append(
                f"operating margin {peak:.1%} far above the industry ({industry.margin.value:.1%}) "
                "and the company's own history: needs a moat story"
            )
    if not WACC_RANGE[0] <= wacc.wacc.value <= WACC_RANGE[1]:
        soft.append(f"WACC {wacc.wacc.value:.2%} outside {WACC_RANGE[0]:.0%}-{WACC_RANGE[1]:.0%}")
    if (
        preview is not None
        and preview.tv_share_of_ev is not None
        and preview.tv_share_of_ev > TV_SHARE
    ):
        soft.append(f"terminal value {preview.tv_share_of_ev:.0%} of enterprise value")
    return Coherence(assumptions.scenario, tuple(hard), tuple(soft))


@dataclass(frozen=True)
class Preview:
    result: ValuationResult | None
    error: str | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def per_share(self) -> float | None:
        return self.result.value_per_share if self.result is not None else None


def quick_dcf(assumptions: ForecastAssumptions, wacc: WACCResult, assembled: Assembled) -> Preview:
    """The engine's value for one scenario; a guardrail failure is returned, not raised."""
    try:
        result = run_dcf(
            assumptions,
            wacc,
            base_revenue=assembled.base_revenue,
            bridge=assembled.bridge,
            shares=assembled.shares,
            valuation_date=assembled.as_of,
        )
    except (EngineError, ValueError) as error:
        return Preview(None, str(error))
    return Preview(result, None, assembled.notes)
