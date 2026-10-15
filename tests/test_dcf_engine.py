"""DCF engine: hand-computed cases to 1e-6, guardrails, dilution and properties."""

from datetime import date
from math import isclose

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from dcf_agent.contracts.modeling import ForecastAssumptions, WACCResult
from dcf_agent.contracts.provenance import Provenance, Sourced
from dcf_agent.engine.dcf import (
    Convertible,
    Drivers,
    EngineError,
    Option,
    Shares,
    dilute,
    project,
    run_dcf,
    sensitivity,
    timing,
    weighted_value,
)

VALUED = date(2025, 12, 31)
ENDS = (date(2026, 12, 31), date(2027, 12, 31))


def drivers(**overrides) -> Drivers:
    base = {
        "valuation_date": VALUED,
        "base_revenue": 100.0,
        "year_ends": ENDS,
        "revenue_growth": (0.10, 0.10),
        "operating_margin": (0.20, 0.20),
        "sales_to_capital": (2.0, 2.0),
        "tax_rate": (0.25, 0.25),
        "terminal_growth": 0.02,
        "terminal_roic": 0.10,
        "marginal_tax_rate": 0.25,
        "wacc": 0.08,
        "terminal_wacc": 0.08,
        "risk_free_rate": 0.04,
    }
    return Drivers(**{**base, **overrides})


def test_two_year_case_matches_hand_computation():
    p = project(drivers())
    # Year 1: revenue 110, EBIT 22, NOPAT 16.5, reinvestment 10 / 2 = 5, FCFF 11.5.
    # Year 2: revenue 121, EBIT 24.2, NOPAT 18.15, reinvestment 11 / 2 = 5.5, FCFF 12.65.
    assert p.revenue == pytest.approx((110.0, 121.0), abs=1e-9)
    assert p.fcff == pytest.approx((11.5, 12.65), abs=1e-9)
    # Full first year (s = 1): mid-year times 0.5 and 1.5.
    assert p.stub == 1.0 and p.discount_times == (0.5, 1.5)
    # Terminal: NOPAT = 121 x 1.02 x 0.20 x 0.75 = 18.513; reinvestment g / ROIC = 20%.
    assert isclose(p.terminal_nopat, 18.513, abs_tol=1e-9)
    assert isclose(p.terminal_fcff, 14.8104, abs_tol=1e-9)
    assert isclose(p.terminal_value, 14.8104 / 0.06, abs_tol=1e-9)
    expected_explicit = 11.5 / 1.08**0.5 + 12.65 / 1.08**1.5
    expected_terminal = (14.8104 / 0.06) / 1.08**1.5  # end of year 2, mid-year: t = 1.5
    assert isclose(p.pv_explicit, expected_explicit, abs_tol=1e-6)
    assert isclose(p.pv_terminal, expected_terminal, abs_tol=1e-6)
    assert isclose(p.enterprise_value, expected_explicit + expected_terminal, abs_tol=1e-6)


def test_stub_period_scales_first_year_and_shifts_discount_times():
    # Valuation 2026-07-02: 182 days to 2026-12-31, s = 182 / 365.
    p = project(drivers(valuation_date=date(2026, 7, 2)))
    s = 182 / 365
    assert isclose(p.stub, s, abs_tol=1e-12)
    assert p.discount_times == pytest.approx((s / 2, s + 0.5), abs=1e-12)
    assert isclose(p.fcff[0], 11.5 * s, abs_tol=1e-9)
    expected = 11.5 * s / 1.08 ** (s / 2) + 12.65 / 1.08 ** (s + 0.5)
    assert isclose(p.pv_explicit, expected, abs_tol=1e-6)
    assert isclose(p.terminal_discount_time, s + 0.5, abs_tol=1e-12)


def test_timing_rules():
    s, times = timing(
        date(2025, 12, 31), (date(2026, 12, 31), date(2027, 12, 31), date(2028, 12, 31))
    )
    assert s == 1.0 and times == (0.5, 1.5, 2.5)
    with pytest.raises(EngineError):
        timing(date(2025, 1, 1), (date(2026, 6, 30),))  # more than a year away


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"terminal_growth": 0.09}, "exceed terminal growth"),
        ({"terminal_growth": 0.05}, "risk-free"),
        ({"terminal_roic": 0.015}, "below terminal ROIC"),
        ({"sales_to_capital": (2.0, 0.0)}, "Sales-to-capital"),
        ({"revenue_growth": (0.1,)}, "one value per forecast year"),
        ({"base_revenue": 0.0}, "Base revenue"),
    ],
)
def test_guardrails_reject_meaningless_inputs(override, message):
    with pytest.raises(EngineError, match=message):
        project(drivers(**override))


def test_treasury_stock_method_converges_to_hand_solution():
    # Equity 1,000, 100 shares, 20 options struck at 5. Fixed point:
    # p = 1000 / (100 + 20 (p - 5) / p)  =>  120 p - 100 = 1000  =>  p = 9.1666...
    result = dilute(1000.0, Shares(basic=100.0, options=(Option(20, 5.0),)))
    assert isclose(result.value_per_share, 1100 / 120, abs_tol=1e-9)
    assert isclose(
        result.dilution["options_tsm"], 20 * (1100 / 120 - 5) / (1100 / 120), abs_tol=1e-9
    )
    # Out-of-the-money options and RSUs.
    result = dilute(1000.0, Shares(basic=100.0, rsus=25.0, options=(Option(20, 50.0),)))
    assert isclose(result.value_per_share, 8.0, abs_tol=1e-12)


def test_convertible_converts_only_when_shares_are_worth_more_than_principal():
    # Principal 100 into 10 shares. Equity 1,000 net of the debt, 100 basic shares.
    converts = dilute(1000.0, Shares(basic=100.0, convertibles=(Convertible(100.0, 10.0),)))
    assert isclose(converts.value_per_share, 1100 / 110, abs_tol=1e-12)
    stays = dilute(500.0, Shares(basic=100.0, convertibles=(Convertible(100.0, 10.0),)))
    assert isclose(stays.value_per_share, 5.0, abs_tol=1e-12)


def sourced(value: float, end: date | None = None) -> Sourced[float]:
    return Sourced[float](
        value=value,
        prov=Provenance(
            source="user_override", ref="test", as_of=VALUED, confidence="high", period_end=end
        ),
    )


def assumptions(scenario="base", probability=1.0, growth=0.10) -> ForecastAssumptions:
    return ForecastAssumptions(
        as_of=VALUED,
        scenario=scenario,
        probability=probability,
        year_ends=ENDS,
        revenue_growth=tuple(sourced(growth, e) for e in ENDS),
        operating_margin=tuple(sourced(0.20, e) for e in ENDS),
        sales_to_capital=tuple(sourced(2.0, e) for e in ENDS),
        tax_rate=tuple(sourced(0.25, e) for e in ENDS),
        terminal_growth=sourced(0.02),
        terminal_roic=sourced(0.10),
        marginal_tax_rate=sourced(0.25),
        narrative="Test scenario",
        rationale={"all": "hand-computed test"},
    )


def wacc_result() -> WACCResult:
    return WACCResult(
        as_of=VALUED,
        rf=sourced(0.04),
        erp=sourced(0.05),
        crp=sourced(0.0),
        unlevered_beta=sourced(0.8),
        relevered_beta=sourced(0.9),
        bottom_up_beta=sourced(0.9),
        beta_method="bottom_up",
        cost_equity=sourced(0.085),
        pretax_kd=sourced(0.05),
        tax_rate=sourced(0.25),
        aftertax_kd=sourced(0.0375),
        weight_equity=sourced(0.9),
        weight_debt=sourced(0.1),
        wacc=sourced(0.08),
        terminal_wacc=sourced(0.08),
        wacc_band=(0.075, 0.085),
        method_notes=("test",),
    )


def test_run_dcf_builds_a_validated_result_with_bridge_and_shares():
    bridge = {"cash": sourced(50.0), "debt": sourced(-30.0)}
    result = run_dcf(
        assumptions(),
        wacc_result(),
        base_revenue=100.0,
        bridge=bridge,
        shares=Shares(basic=10.0),
        valuation_date=VALUED,
    )
    ev = project(drivers()).enterprise_value
    assert isclose(result.enterprise_value, ev, abs_tol=1e-9)
    assert isclose(result.equity_value, ev + 20.0, abs_tol=1e-9)
    assert isclose(result.value_per_share, (ev + 20.0) / 10.0, abs_tol=1e-9)
    assert result.tv_share_of_ev is not None and 0 < result.tv_share_of_ev < 1
    assert len(result.input_hash) == 64


def test_probability_weighted_value():
    results = [
        run_dcf(
            assumptions(name, p, growth),
            wacc_result(),
            base_revenue=100.0,
            bridge={},
            shares=Shares(basic=10.0),
            valuation_date=VALUED,
        )
        for name, p, growth in (("bear", 0.25, 0.0), ("base", 0.5, 0.1), ("bull", 0.25, 0.2))
    ]
    probabilities = {"bear": 0.25, "base": 0.5, "bull": 0.25}
    expected = sum(probabilities[r.scenario] * r.value_per_share for r in results)
    assert isclose(weighted_value(results, probabilities), expected, abs_tol=1e-12)
    with pytest.raises(EngineError):
        weighted_value(results, {"bear": 0.5, "base": 0.5, "bull": 0.5})


def test_sensitivity_grid_marks_invalid_cells():
    grid = sensitivity(
        drivers(), 0.0, Shares(basic=10.0), waccs=(0.07, 0.08, 0.01), growths=(0.01, 0.02)
    )
    assert grid[0][0] is not None and grid[0][0] > grid[1][0]  # lower WACC, higher value
    assert grid[2] == [None, None]  # WACC below or equal to growth is invalid


@settings(max_examples=60, deadline=None)
@given(
    wacc=st.floats(0.05, 0.12),
    bump=st.floats(0.001, 0.03),
    growth=st.floats(-0.05, 0.25),
)
def test_higher_wacc_lowers_value(wacc, bump, growth):
    low = project(drivers(wacc=wacc, terminal_wacc=wacc, revenue_growth=(growth, growth)))
    high = project(
        drivers(wacc=wacc + bump, terminal_wacc=wacc + bump, revenue_growth=(growth, growth))
    )
    assert high.enterprise_value < low.enterprise_value


@settings(max_examples=60, deadline=None)
@given(growth=st.floats(0.0, 0.2), bump=st.floats(0.005, 0.05))
def test_growth_adds_value_only_when_returns_exceed_the_cost_of_capital(growth, bump):
    # Incremental return on capital = sales-to-capital x margin x (1 - t) = 2 x 0.2 x 0.75 = 30%.
    rich = drivers(revenue_growth=(growth, growth))
    richer = drivers(revenue_growth=(growth + bump, growth + bump))
    assert project(richer).enterprise_value > project(rich).enterprise_value
    # At 1% incremental return (sales-to-capital 0.0667) growth destroys value.
    poor = dict(sales_to_capital=(1 / 15, 1 / 15))
    assert (
        project(drivers(revenue_growth=(growth + bump,) * 2, **poor)).pv_explicit
        < project(drivers(revenue_growth=(growth,) * 2, **poor)).pv_explicit
    )
