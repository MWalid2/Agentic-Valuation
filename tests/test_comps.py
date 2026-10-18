"""Comps: multiples, outliers, implied value, and the keep/drop with its restore rule."""

from datetime import date

import pytest

from dcf_agent.agents.comps import (
    CASH,
    REVENUE,
    Figures,
    Verdict,
    implied_per_share,
    latest,
    multiples,
    restore,
    ttm,
    without_outliers,
)
from dcf_agent.contracts.provenance import Provenance, Sourced
from dcf_agent.data.sec import Fact

AS_OF = date(2026, 10, 11)


def figures(**values: float) -> Figures:
    out = Figures("XYZ", "0000000001")
    for name, value in values.items():
        out.values[name] = Sourced[float](
            value=value,
            prov=Provenance(
                source="sec_xbrl",
                ref=f"https://data.sec.gov/api/xbrl/companyfacts/CIK0000000001.json ({name})",
                as_of=AS_OF,
                filed=date(2026, 8, 1),
                period_end=date(2026, 6, 30),
                confidence="high",
            ),
        )
    return out


def test_multiples_use_enterprise_value_and_skip_non_positive_denominators():
    f = figures(
        price=10, shares=100, debt=300, cash=100, ebitda=60, revenue=400, ebit=-5, net_income=50
    )
    m = multiples(f)
    assert m["ev_ebitda"].value == pytest.approx(1200 / 60)  # EV = 1000 + 300 - 100
    assert m["ev_sales"].value == pytest.approx(3.0)
    assert m["price_earnings"].value == pytest.approx(20.0)
    assert "ev_ebit" not in m  # negative operating income: no multiple
    assert multiples(figures(revenue=400)) == {}


def test_outliers_by_the_interquartile_rule_and_implied_value():
    assert without_outliers([10, 11, 12, 13, 80]) == [10, 11, 12, 13]
    assert without_outliers([10, 80, 90]) == [10, 80, 90]  # too few to judge
    target = figures(shares=100, ebitda=50, debt=200, cash=50, net_income=20)
    assert implied_per_share("ev_ebitda", 10, target) == pytest.approx((500 - 200 + 50) / 100)
    assert implied_per_share("price_earnings", 15, target) == pytest.approx(3.0)
    assert implied_per_share("ev_sales", 2, target) is None  # no revenue


def verdict(ticker: str, keep: bool, fit: int) -> Verdict:
    return Verdict(ticker=ticker, keep=keep, fit=fit, reason="r")


def test_a_strict_reading_is_filled_to_five_with_the_closest_never_the_unrelated():
    read = {
        "A": verdict("A", True, 5),
        "B": verdict("B", True, 4),
        "C": verdict("C", False, 3),
        "D": verdict("D", False, 2),
        "E": verdict("E", False, 1),
        "F": verdict("F", False, 3),
    }
    out = restore(read)
    assert {t for t, v in out.items() if v.keep} == {"A", "B", "C", "F", "D"}
    assert out["C"].reason.startswith("restored to reach 5 peers (fit 3/5)")
    assert not out["E"].keep  # unrelated (fit 1) stays out
    enough = {t: verdict(t, True, 4) for t in "ABCDE"} | {"F": verdict("F", False, 3)}
    assert restore(enough) == enough  # five kept: nothing restored


def fact(tag: str, start: date | None, end: date, value: float) -> Fact:
    return Fact(
        namespace="us-gaap",
        tag=tag,
        unit="USD",
        value=value,
        start=start,
        end=end,
        filed=end,
        accession="0000000001-26-000001",
        form="10-K",
        fiscal_year=end.year,
        fiscal_period="FY",
    )


def test_the_latest_tag_wins_and_an_abandoned_tag_is_not_the_latest():
    facts = [
        fact("Revenues", date(2017, 1, 1), date(2017, 12, 31), 1.0),  # stopped in 2018
        fact(
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            date(2025, 7, 1),
            date(2026, 6, 30),
            9.0,
        ),
        fact("CashAndCashEquivalentsAtCarryingValue", None, date(2018, 3, 31), 5.0),
    ]
    found = ttm(facts, REVENUE, AS_OF)
    assert found is not None and found[0] == 9.0 and found[1].end == date(2026, 6, 30)
    assert ttm(facts[:1], REVENUE, AS_OF) is None  # only a 2017 figure: stale
    assert latest(facts, CASH, AS_OF) is None


def test_missing_target_figures_are_filled_from_the_valuation_and_marked():
    target = figures(price=10, shares=100, debt=50)  # a new registrant: few facts
    own = figures(revenue=400, ebit=60, depreciation=20, debt=999, cash=30).values
    target.fill(own)
    assert target.get("revenue") == 400 and target.get("debt") == 50  # own read kept
    assert target.get("ebitda") == 80  # rebuilt from the filled EBIT and D&A
    assert target.filled == {"revenue", "ebit", "depreciation", "cash", "ebitda"}
    assert "filled from the valuation's data" in target.notes[-1]
    assert implied_per_share("ev_sales", 2, target) == pytest.approx((800 - 50 + 30) / 100)
