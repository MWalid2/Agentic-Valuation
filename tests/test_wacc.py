"""WACC calculators: hand-computed build-up, beta rule, rating, regression."""

from datetime import date, timedelta
from math import isclose

import pytest

from dcf_agent.contracts.external import BenchmarkRow, BenchmarkTable
from dcf_agent.contracts.provenance import Provenance, Sourced
from dcf_agent.engine.wacc import (
    Regression,
    WaccError,
    WaccInputs,
    choose_beta,
    compute_wacc,
    regression_beta,
    relever,
    synthetic_rating,
)

AS_OF = date(2026, 10, 11)


def sourced(value: float) -> Sourced[float]:
    return Sourced[float](
        value=value,
        prov=Provenance(source="damodaran", ref="test", as_of=AS_OF, confidence="high"),
    )


def table(rows: list[BenchmarkRow], dataset: str) -> BenchmarkTable:
    return BenchmarkTable(
        dataset=dataset,  # type: ignore[arg-type]
        as_of=AS_OF,
        analysis_on=date(2026, 1, 1),
        available_on=date(2026, 1, 9),
        retrieved_on=date(2026, 1, 9),
        ref="test",
        version="v",
        rows=tuple(rows),
    )


SPREADS = table(
    [
        BenchmarkRow(
            name=name,
            values={
                "coverage_min": sourced(lo),
                "coverage_max": sourced(hi),
                "spread": sourced(sp),
            },
            units={"coverage_min": "ratio", "coverage_max": "ratio", "spread": "decimal"},
        )
        for name, lo, hi, sp in (
            ("D", -100000.0, 0.199999, 0.19),
            ("BBB", 0.2, 3.249999, 0.02),
            ("A", 3.25, 8.499999, 0.01),
            ("AAA", 8.5, 100000.0, 0.006),
        )
    ],
    "spreads",
)
INDUSTRY = BenchmarkRow(
    name="Test industry",
    values={"Unlevered beta corrected for cash": sourced(0.8)},
    units={"Unlevered beta corrected for cash": "ratio"},
)


def inputs(**overrides) -> WaccInputs:
    base = {
        "as_of": AS_OF,
        "risk_free": sourced(0.04),
        "erp": sourced(0.05),
        "crp": sourced(0.0),
        "industry": INDUSTRY,
        "equity_value": sourced(800.0),
        "debt_value": sourced(200.0),
        "marginal_tax": sourced(0.25),
        "spreads": SPREADS,
        "ebit": 100.0,
        "interest_expense": 20.0,  # coverage 5 -> A, spread 1%
    }
    return WaccInputs(**{**base, **overrides})


def test_hand_computed_wacc_build_up():
    w = compute_wacc(inputs())
    beta = 0.8 * (1 + 0.75 * 0.25)  # D/E = 200 / 800
    assert isclose(w.bottom_up_beta.value, beta)
    assert isclose(w.cost_equity.value, 0.04 + beta * 0.05)
    assert isclose(w.pretax_kd.value, 0.05) and isclose(w.aftertax_kd.value, 0.0375)
    expected = 0.8 * (0.04 + beta * 0.05) + 0.2 * 0.0375
    assert isclose(w.wacc.value, expected)
    assert w.terminal_wacc.value == w.wacc.value  # beta below 1 stays
    assert w.wacc_band[0] < w.wacc.value < w.wacc_band[1]
    assert w.beta_method == "bottom_up"


def test_terminal_beta_converges_to_one_for_high_beta_firms():
    w = compute_wacc(
        inputs(
            industry=BenchmarkRow(
                name="Risky",
                values={"Unlevered beta corrected for cash": sourced(1.4)},
                units={"Unlevered beta corrected for cash": "ratio"},
            )
        )
    )
    assert isclose(w.terminal_wacc.value, 0.8 * (0.04 + 1.0 * 0.05) + 0.2 * 0.0375)
    assert w.terminal_wacc.value < w.wacc.value


def test_cost_of_debt_without_interest_uses_the_fallback_spread_and_says_so():
    w = compute_wacc(inputs(interest_expense=None, fallback_spread=sourced(0.012)))
    assert isclose(w.pretax_kd.value, 0.052)
    assert any("market credit spread" in n for n in w.method_notes)
    with pytest.raises(WaccError):
        compute_wacc(inputs(interest_expense=None))


def test_debt_free_company_has_full_equity_weight():
    w = compute_wacc(inputs(debt_value=sourced(0.0), interest_expense=None))
    assert w.weight_debt.value == 0 and isclose(w.wacc.value, w.cost_equity.value)


@pytest.mark.parametrize(
    ("ebit", "interest", "rating"),
    [(100, 5, "AAA"), (100, 20, "A"), (100, 50, "BBB"), (-10, 5, "D")],
)
def test_synthetic_rating_brackets(ebit, interest, rating):
    assert synthetic_rating(ebit, interest, SPREADS).name == rating


def test_relever_and_beta_rule():
    assert isclose(relever(1.0, 0.5, 0.2), 1.4)
    method, beta, _ = choose_beta(0.9, Regression(1.2, 0.45, 0.10, 104))
    assert (method, beta) == ("regression", 1.2)
    method, beta, note = choose_beta(0.9, Regression(1.2, 0.10, 0.30, 104))
    assert (method, beta) == ("bottom_up", 0.9) and "reliability rule" in note


def test_regression_recovers_a_known_beta():
    start = date(2024, 1, 5)  # a Friday
    market, stock = [], []
    m = s = 100.0
    for k in range(120):
        r = 0.01 * ((k * 7) % 11 - 5) / 5  # deterministic, varying market returns
        m *= 1 + r
        s *= 1 + 1.5 * r + (0.002 if k % 2 else -0.002)
        day = start + timedelta(weeks=k)
        market.append((day, m))
        stock.append((day, s))
    fit = regression_beta(stock, market)
    assert isclose(fit.beta, 1.5, abs_tol=0.02)
    assert fit.r_squared > 0.9 and fit.observations == 119
    assert fit.standard_error < 0.05
    with pytest.raises(WaccError):
        regression_beta(stock[:30], market[:30])
