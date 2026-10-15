"""FCFF discounted cash flow: the only source of valuation numbers.

Damodaran-style drivers (growth, operating margin, sales-to-capital, tax) produce
FCFF = EBIT (1 - t) - change in revenue / sales-to-capital. Timing uses a stub
period and the mid-year convention; the terminal value is a Gordon growth value
whose reinvestment is consistent with the terminal ROIC. Pure functions only.
"""

import hashlib
import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import date

from dcf_agent.contracts.modeling import ForecastAssumptions, ValuationResult, WACCResult
from dcf_agent.contracts.provenance import Sourced

CALCULATION_REF = "dcf_agent.engine.dcf v1"
DAYS_PER_YEAR = 365.0


class EngineError(ValueError):
    """Inputs that would make the valuation meaningless (a hard guardrail)."""


@dataclass(frozen=True)
class Option:
    """A tranche of options or warrants for the treasury stock method."""

    count: float
    strike: float


@dataclass(frozen=True)
class Convertible:
    """If-converted test: converts when the shares are worth more than the principal."""

    principal: float
    conversion_shares: float


@dataclass(frozen=True)
class Shares:
    basic: float
    rsus: float = 0.0
    options: tuple[Option, ...] = ()
    convertibles: tuple[Convertible, ...] = ()


@dataclass(frozen=True)
class Drivers:
    """One scenario as plain numbers; the contract wrapper builds this."""

    valuation_date: date
    base_revenue: float
    year_ends: tuple[date, ...]
    revenue_growth: tuple[float, ...]
    operating_margin: tuple[float, ...]
    sales_to_capital: tuple[float, ...]
    tax_rate: tuple[float, ...]
    terminal_growth: float
    terminal_roic: float
    marginal_tax_rate: float
    wacc: float
    terminal_wacc: float
    risk_free_rate: float | None = None
    # Discounting convention: mid-year cash flows (default) or end of year.
    mid_year: bool = True
    # Optional discount rate per forecast year (e.g. fading toward a mature rate).
    wacc_path: tuple[float, ...] | None = None


@dataclass(frozen=True)
class Projection:
    revenue: tuple[float, ...]
    ebit: tuple[float, ...]
    nopat: tuple[float, ...]
    reinvestment: tuple[float, ...]
    fcff: tuple[float, ...]
    stub: float
    discount_times: tuple[float, ...]
    discount_factors: tuple[float, ...]
    pv_fcff: tuple[float, ...]
    terminal_nopat: float
    terminal_fcff: float
    terminal_value: float
    terminal_discount_time: float
    pv_terminal: float
    pv_explicit: float
    enterprise_value: float


@dataclass(frozen=True)
class Equity:
    equity_value: float
    diluted_shares: float
    value_per_share: float
    dilution: dict[str, float] = field(default_factory=dict)


def check_guardrails(d: Drivers) -> None:
    n = len(d.year_ends)
    for name, series in (
        ("revenue_growth", d.revenue_growth),
        ("operating_margin", d.operating_margin),
        ("sales_to_capital", d.sales_to_capital),
        ("tax_rate", d.tax_rate),
    ):
        if len(series) != n:
            raise EngineError(f"{name} must have one value per forecast year")
    if d.base_revenue <= 0:
        raise EngineError("Base revenue must be positive")
    if any(s <= 0 for s in d.sales_to_capital):
        raise EngineError("Sales-to-capital must be positive")
    if d.terminal_wacc <= d.terminal_growth:
        raise EngineError("Terminal WACC must exceed terminal growth")
    if d.terminal_roic <= 0 or d.terminal_growth >= d.terminal_roic:
        raise EngineError("Terminal growth must be below terminal ROIC (reinvestment < 100%)")
    if d.risk_free_rate is not None and d.terminal_growth > d.risk_free_rate:
        raise EngineError("Terminal growth cannot exceed the risk-free rate")
    if d.year_ends[0] <= d.valuation_date:
        raise EngineError("The first forecast year must end after the valuation date")
    if list(d.year_ends) != sorted(set(d.year_ends)):
        raise EngineError("Forecast year ends must be unique and ascending")


def timing(
    valuation_date: date, year_ends: Sequence[date], *, mid_year: bool = True
) -> tuple[float, tuple[float, ...]]:
    """Stub fraction s and discount times.

    Mid-year: s/2, then s + (k - 2) + 0.5. End of year: s, then s + (k - 1).
    """
    s = (year_ends[0] - valuation_date).days / DAYS_PER_YEAR
    if not 0 < s <= 1 + 1e-9:
        raise EngineError("The first forecast year must end within a year of the valuation date")
    if mid_year:
        times = [s / 2] + [s + (k - 2) + 0.5 for k in range(2, len(year_ends) + 1)]
    else:
        times = [s + (k - 1) for k in range(1, len(year_ends) + 1)]
    return s, tuple(times)


def discount_factor(t: float, stub: float, rates: Sequence[float]) -> float:
    """Cumulative factor to time t when year k (ending at stub + k - 1) has its own rate."""
    factor, start = 1.0, 0.0
    for k, rate in enumerate(rates):
        end = stub + k
        span = min(t, end) - start
        if span > 0:
            factor *= (1 + rate) ** -span
        if t <= end:
            return factor
        start = end
    return float(factor * (1 + rates[-1]) ** -(t - start))


def project(d: Drivers) -> Projection:
    check_guardrails(d)
    revenue, ebit, nopat, reinvestment, fcff = [], [], [], [], []
    previous = d.base_revenue
    for g, margin, s2c, tax in zip(
        d.revenue_growth, d.operating_margin, d.sales_to_capital, d.tax_rate, strict=True
    ):
        current = previous * (1 + g)
        operating = current * margin
        after_tax = operating * (1 - tax)
        reinvest = (current - previous) / s2c
        revenue.append(current)
        ebit.append(operating)
        nopat.append(after_tax)
        reinvestment.append(reinvest)
        fcff.append(after_tax - reinvest)
        previous = current
    s, times = timing(d.valuation_date, d.year_ends, mid_year=d.mid_year)
    # Only the remaining part of the first year belongs to the buyer.
    flows = [fcff[0] * s, *fcff[1:]]
    rates = d.wacc_path or (d.wacc,) * len(d.year_ends)
    if len(rates) != len(d.year_ends):
        raise EngineError("wacc_path must have one rate per forecast year")
    factors = tuple(discount_factor(t, s, rates) for t in times)
    pv = tuple(f * df for f, df in zip(flows, factors, strict=True))
    terminal_nopat = (
        revenue[-1] * (1 + d.terminal_growth) * d.operating_margin[-1] * (1 - d.marginal_tax_rate)
    )
    terminal_fcff = terminal_nopat * (1 - d.terminal_growth / d.terminal_roic)
    terminal_value = terminal_fcff / (d.terminal_wacc - d.terminal_growth)
    # Gordon value at the end of year N; with mid-year cash flows, discount at N - 0.5.
    end_of_horizon = s + len(d.year_ends) - 1
    terminal_time = max(end_of_horizon - 0.5, times[-1]) if d.mid_year else end_of_horizon
    pv_terminal = terminal_value * discount_factor(terminal_time, s, rates)
    pv_explicit = sum(pv)
    return Projection(
        revenue=tuple(revenue),
        ebit=tuple(ebit),
        nopat=tuple(nopat),
        reinvestment=tuple(reinvestment),
        fcff=tuple(flows),
        stub=s,
        discount_times=times,
        discount_factors=factors,
        pv_fcff=pv,
        terminal_nopat=terminal_nopat,
        terminal_fcff=terminal_fcff,
        terminal_value=terminal_value,
        terminal_discount_time=terminal_time,
        pv_terminal=pv_terminal,
        pv_explicit=pv_explicit,
        enterprise_value=pv_explicit + pv_terminal,
    )


def dilute(equity_value: float, shares: Shares, *, tolerance: float = 1e-10) -> Equity:
    """Treasury stock method at the estimated intrinsic value, iterated to a fixed point.

    RSUs count in full; options add count x (price - strike) / price when in the
    money; a convertible converts (adding shares, and its principal back to
    equity) when its shares are worth more than its principal.
    """
    if shares.basic <= 0:
        raise EngineError("Basic shares must be positive")
    count = shares.basic + shares.rsus
    price = equity_value / count
    for _ in range(200):
        options = sum(
            o.count * (price - o.strike) / price for o in shares.options if price > o.strike > 0
        )
        options += sum(o.count for o in shares.options if o.strike == 0 and price > 0)
        converted = [c for c in shares.convertibles if price * c.conversion_shares > c.principal]
        equity = equity_value + sum(c.principal for c in converted)
        diluted = shares.basic + shares.rsus + options + sum(c.conversion_shares for c in converted)
        new_price = equity / diluted
        if abs(new_price - price) <= tolerance * max(1.0, abs(price)):
            price = new_price
            break
        price = new_price
    else:
        raise EngineError("Dilution did not converge")
    return Equity(
        equity_value=equity,
        diluted_shares=diluted,
        value_per_share=price,
        dilution={
            "basic": shares.basic,
            "rsus": shares.rsus,
            "options_tsm": options,
            "convertibles": sum(c.conversion_shares for c in converted),
        },
    )


def drivers_from(
    assumptions: ForecastAssumptions,
    wacc: WACCResult,
    base_revenue: float,
    valuation_date: date,
) -> Drivers:
    return Drivers(
        valuation_date=valuation_date,
        base_revenue=base_revenue,
        year_ends=assumptions.year_ends,
        revenue_growth=tuple(v.value for v in assumptions.revenue_growth),
        operating_margin=tuple(v.value for v in assumptions.operating_margin),
        sales_to_capital=tuple(v.value for v in assumptions.sales_to_capital),
        tax_rate=tuple(v.value for v in assumptions.tax_rate),
        terminal_growth=assumptions.terminal_growth.value,
        terminal_roic=assumptions.terminal_roic.value,
        marginal_tax_rate=assumptions.marginal_tax_rate.value,
        wacc=wacc.wacc.value,
        terminal_wacc=wacc.terminal_wacc.value,
        risk_free_rate=wacc.rf.value,
    )


def input_hash(*parts: object) -> str:
    def encode(value: object) -> object:
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        if hasattr(value, "__dataclass_fields__"):
            return asdict(value)  # type: ignore[call-overload]
        return value

    text = json.dumps([encode(p) for p in parts], sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def run_dcf(
    assumptions: ForecastAssumptions,
    wacc: WACCResult,
    *,
    base_revenue: float,
    bridge: dict[str, Sourced[float]],
    shares: Shares,
    valuation_date: date,
) -> ValuationResult:
    """One scenario; bridge entries are signed contributions to equity value."""
    d = drivers_from(assumptions, wacc, base_revenue, valuation_date)
    p = project(d)
    equity = dilute(p.enterprise_value + sum(v.value for v in bridge.values()), shares)
    implied = {
        "ev_to_revenue_year1": p.enterprise_value / p.revenue[0],
        "terminal_reinvestment_rate": d.terminal_growth / d.terminal_roic,
        "stub_fraction": p.stub,
    }
    if p.ebit[0] != 0:
        implied["ev_to_ebit_year1"] = p.enterprise_value / p.ebit[0]
    return ValuationResult(
        as_of=valuation_date,
        scenario=assumptions.scenario,
        calculation_ref=CALCULATION_REF,
        input_hash=input_hash(assumptions, wacc, base_revenue, bridge, shares, valuation_date),
        year_ends=d.year_ends,
        fcff_by_year=p.fcff,
        discount_times=p.discount_times,
        discount_factors=p.discount_factors,
        pv_explicit=p.pv_explicit,
        terminal_value=p.terminal_value,
        terminal_discount_time=p.terminal_discount_time,
        pv_terminal=p.pv_terminal,
        enterprise_value=p.enterprise_value,
        bridge=bridge,
        equity_value=equity.equity_value,
        diluted_shares=equity.diluted_shares,
        value_per_share=equity.value_per_share,
        tv_share_of_ev=p.pv_terminal / p.enterprise_value if p.enterprise_value else None,
        implied=implied,
    )


def weighted_value(results: Sequence[ValuationResult], probabilities: dict[str, float]) -> float:
    """Probability-weighted value per share across scenarios."""
    if abs(sum(probabilities.values()) - 1) > 1e-9:
        raise EngineError("Scenario probabilities must sum to one")
    return sum(probabilities[r.scenario] * r.value_per_share for r in results)


def sensitivity(
    d: Drivers,
    bridge_total: float,
    shares: Shares,
    *,
    waccs: Sequence[float],
    growths: Sequence[float],
) -> list[list[float | None]]:
    """Value per share for each WACC (rows) x terminal growth (columns); None if invalid."""
    grid: list[list[float | None]] = []
    for w in waccs:
        row: list[float | None] = []
        for g in growths:
            try:
                p = project(replace(d, wacc=w, terminal_wacc=w, terminal_growth=g))
                row.append(dilute(p.enterprise_value + bridge_total, shares).value_per_share)
            except EngineError:
                row.append(None)
        grid.append(row)
    return grid
