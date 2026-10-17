"""Forecast tools: driver statistics, fades, year ends, coherence bounds and the preview."""

from datetime import date

import pytest
from test_wacc import inputs

from dcf_agent.contracts.inputs import ConceptStatus, FinancialPeriod, RawFinancials
from dcf_agent.contracts.modeling import ForecastAssumptions
from dcf_agent.contracts.provenance import Provenance, Sourced
from dcf_agent.engine.drivers import (
    check_coherence,
    driver_stats,
    fade_path,
    forecast_years,
    years_later,
)
from dcf_agent.engine.normalize import Policy, normalize
from dcf_agent.engine.wacc import compute_wacc

AS_OF = date(2026, 10, 11)


def sourced(value: float, end: date | None) -> Sourced[float]:
    return Sourced[float](
        value=value,
        prov=Provenance(
            source="sec_xbrl" if end else "derived",
            ref="accession 0000000001-26-000001",
            as_of=AS_OF,
            period_end=end,
            filed=date(2026, 2, 20) if end else None,
            confidence="high",
        ),
    )


def period(year: int, revenue: float, ebit: float) -> FinancialPeriod:
    end = date(year, 12, 31)
    values = {
        "revenue": revenue,
        "ebit": ebit,
        "pretax_income": ebit * 0.9,
        "income_tax_expense": ebit * 0.9 * 0.21,
        "equity": 3_000e6 + 100e6 * (year - 2021),
        "debt": 1_000e6,
        "cash": 500e6,
        "capex": 300e6,
        "depreciation_amortization": 200e6,
    }
    absent = ("minority_interest", "preferred_equity", "marketable_securities")
    return FinancialPeriod(
        kind="annual",
        fiscal_year=year,
        period_start=date(year, 1, 1),
        period_end=end,
        values={k: sourced(v, end) for k, v in values.items()},  # type: ignore[misc]
        statuses={k: ConceptStatus(status="not_presented") for k in absent},  # type: ignore[misc]
    )


def history():
    raw = RawFinancials(
        cik="0000000001",
        as_of=AS_OF,
        annual=(
            period(2022, 4_000e6, 800e6),
            period(2023, 4_400e6, 900e6),
            period(2024, 4_840e6, 1_000e6),
            period(2025, 5_324e6, 1_150e6),
        ),
    )
    normalized, _ = normalize(raw, Policy(marginal_tax_rate=0.25))
    return driver_stats(normalized)


def test_driver_statistics_summarize_the_normalized_history():
    stats = history()
    assert [round(g, 4) for g in stats.growth.numbers] == [0.1, 0.1, 0.1]
    assert stats.cagr == pytest.approx(0.1, abs=1e-3)
    assert stats.margin.latest == pytest.approx(1_150 / 5_324)
    assert stats.margin.trend is not None and stats.margin.trend > 0  # margins rising
    assert stats.growth.volatility == pytest.approx(0.0, abs=1e-9)
    assert stats.sales_to_capital.values and stats.roic.values
    assert stats.marginal_tax == 0.25 and not stats.notes


def test_fades_hold_then_reach_the_terminal_value():
    linear = fade_path(0.10, 0.03, 10, hold=5)
    assert linear[:5] == (0.10,) * 5 and linear[-1] == pytest.approx(0.03)
    assert all(a >= b for a, b in zip(linear, linear[1:], strict=False))
    convex = fade_path(0.10, 0.03, 10, hold=5, shape="convex")
    assert convex[5] < linear[5] and convex[-1] == pytest.approx(0.03)  # most of the way early
    assert fade_path(0.2, 0.1, 1) == (0.1,)


def test_forecast_years_follow_the_fiscal_calendar():
    assert forecast_years(date(2025, 9, 27), date(2026, 10, 11), 2) == (
        date(2027, 9, 27),
        date(2028, 9, 27),
    )
    assert years_later(date(2024, 2, 29), 1) == date(2025, 2, 28)


def assumptions(**change) -> ForecastAssumptions:
    ends = forecast_years(date(2025, 12, 31), AS_OF, 5)
    base = {
        "growth": [0.08, 0.07, 0.06, 0.05, 0.04],
        "margin": [0.2] * 5,
        "terminal_growth": 0.025,
        "terminal_roic": 0.12,
        "rationale": {"growth": "history"},
    }
    base.update(change)
    tax = [0.21, 0.22, 0.23, 0.24, 0.25]
    return ForecastAssumptions(
        as_of=AS_OF,
        scenario="base",
        probability=0.5,
        year_ends=ends,
        revenue_growth=tuple(sourced(v, e) for v, e in zip(base["growth"], ends, strict=True)),
        operating_margin=tuple(sourced(v, e) for v, e in zip(base["margin"], ends, strict=True)),
        sales_to_capital=tuple(sourced(2.0, e) for e in ends),
        tax_rate=tuple(sourced(v, e) for v, e in zip(tax, ends, strict=True)),
        terminal_growth=sourced(base["terminal_growth"], None),
        terminal_roic=sourced(base["terminal_roic"], None),
        marginal_tax_rate=sourced(0.25, None),
        narrative="Steady growth.",
        rationale=base["rationale"],
    )


def test_hard_bounds_block_and_soft_bounds_flag():
    wacc = compute_wacc(inputs())
    ok = check_coherence(assumptions(), wacc, history=history())
    assert ok.valid and not ok.hard
    high = check_coherence(assumptions(terminal_growth=0.05), wacc)
    assert not high.valid and any("risk-free" in h for h in high.hard)
    assert any("4% long-run cap" in h for h in high.hard)
    moat = check_coherence(assumptions(terminal_roic=wacc.terminal_wacc.value + 0.08), wacc)
    assert moat.valid and any("moat" in s for s in moat.soft)
    explained = check_coherence(
        assumptions(terminal_roic=wacc.terminal_wacc.value + 0.08, rationale={"moat": "brand"}),
        wacc,
    )
    assert not any("moat rationale" in s for s in explained.soft)
    fast = check_coherence(assumptions(growth=[0.3] * 5), wacc)
    assert not any("revenue growth above" in s for s in fast.soft)  # five years, not more
