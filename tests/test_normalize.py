"""Normalizer v0: hand-computed NOPAT, invested capital, ROIC and reinvestment."""

from datetime import date
from math import isclose

import pytest

from dcf_agent.contracts.accounting import Adjustment, FilingCitation
from dcf_agent.contracts.inputs import ConceptStatus, FinancialPeriod, RawFinancials
from dcf_agent.contracts.provenance import Provenance, Sourced
from dcf_agent.engine.normalize import NormalizationError, Policy, normalize

AS_OF = date(2026, 10, 11)


def sourced(value: float, end: date) -> Sourced[float]:
    return Sourced[float](
        value=value,
        prov=Provenance(
            source="sec_xbrl",
            ref=f"ref-{end}",
            as_of=AS_OF,
            period_end=end,
            filed=date(end.year + 1, 2, 1),
            confidence="high",
        ),
    )


def period(year: int, kind: str = "annual", **values: float) -> FinancialPeriod:
    end = date(year, 12, 31)
    base = {
        "revenue": 1000.0,
        "ebit": 200.0,
        "pretax_income": 180.0,
        "income_tax_expense": 45.0,  # 25% effective
        "equity": 500.0,
        "debt": 300.0,
        "cash": 100.0,
        "marketable_securities": 50.0,
        "operating_lease_liabilities": 100.0,
        "receivables": 150.0,
        "inventory": 100.0,
        "payables": 80.0,
        "depreciation_amortization": 40.0,
        "capex": 70.0,
        "net_ppe": 400.0,
    }
    base.update(values)
    statuses = {
        "minority_interest": ConceptStatus(status="not_presented", reason="test"),
        "preferred_equity": ConceptStatus(status="not_presented", reason="test"),
    }
    return FinancialPeriod(
        kind=kind,  # type: ignore[arg-type]
        fiscal_year=year,
        period_start=date(year, 1, 1),
        period_end=end,
        values={k: sourced(v, end) for k, v in base.items()},
        statuses=statuses,
    )


def raw(*periods: FinancialPeriod) -> RawFinancials:
    return RawFinancials(cik="0000000001", as_of=AS_OF, annual=periods)


def test_hand_computed_period_with_lease_capitalization():
    prior = period(2024, receivables=140.0, inventory=90.0, payables=70.0)  # NWC 160
    current = period(2025)  # NWC 170
    normalized, issues = normalize(raw(prior, current), Policy(lease_rate=0.05))
    assert not issues
    p = normalized.annual[-1]
    # Clean EBIT = 200 + lease interest 100 x 5% = 205; NOPAT at 25% effective = 153.75.
    assert isclose(p.clean_ebit.value, 205.0)
    assert isclose(p.effective_tax_rate.value, 0.25)
    assert isclose(p.nopat.value, 153.75)
    # Invested capital = 500 + 0 + 0 + 300 - 100 - 50 + 100 leases = 750 (both years).
    assert isclose(p.invested_capital.value, 750.0)
    assert isclose(p.roic.value, 153.75 / 750.0)
    assert isclose(p.sales_to_capital.value, 1000.0 / 750.0)
    # Reinvestment = capex 70 - D&A 40 + change in NWC (170 - 160) = 40.
    assert isclose(p.reinvestment.value, 40.0)
    assert isclose(p.reinvestment_rate.value, 40.0 / 153.75)
    assert isclose(p.ebitda.value, 245.0)
    assert p.revenue.value == 1000.0 and p.revenue.prov.source == "sec_xbrl"


def test_unusual_effective_tax_falls_back_to_marginal():
    loss = period(2025, pretax_income=-50.0, income_tax_expense=10.0)
    p = normalize(raw(loss), Policy(marginal_tax_rate=0.24))[0].annual[0]
    assert isclose(p.effective_tax_rate.value, 0.24)
    assert isclose(p.nopat.value, 205.0 * 0.76)


def test_genuinely_missing_input_is_reported_not_zero_filled():
    incomplete = period(2025)
    values = dict(incomplete.values)
    values.pop("marketable_securities")
    incomplete = incomplete.model_copy(update={"values": values})
    with pytest.raises(NormalizationError, match="marketable_securities"):
        normalize(raw(incomplete))


def test_partial_history_reports_unnormalizable_periods():
    bad = period(2024)
    bad = bad.model_copy(update={"values": {k: v for k, v in bad.values.items() if k != "ebit"}})
    normalized, issues = normalize(raw(bad, period(2025)))
    assert len(normalized.annual) == 1 and "annual:2024-12-31" in issues


def test_finance_leases_inside_debt_are_not_added_twice():
    combined = period(2025, finance_lease_liabilities=20.0)
    combined = combined.model_copy(
        update={
            "statuses": {
                **combined.statuses,
                "debt": ConceptStatus(
                    status="reported", basis="includes finance lease liabilities"
                ),
            }
        }
    )
    separate = period(2025, finance_lease_liabilities=20.0)
    assert isclose(normalize(raw(combined))[0].annual[0].invested_capital.value, 750.0)
    assert isclose(normalize(raw(separate))[0].annual[0].invested_capital.value, 770.0)


def test_approved_adjustment_changes_clean_ebit_and_is_recorded():
    end = date(2025, 12, 31)
    adjustment = Adjustment(
        adjustment_id="restructuring-2025",
        item="Restructuring charge",
        period_end=end,
        as_of=AS_OF,
        target="ebit",
        amount=sourced(30.0, end),
        direction="increase",
        rationale="Non-recurring plant closure",
        evidence=(
            FilingCitation(
                accession="0000000001-26-000001", filed=date(2026, 2, 1), section="Note 5"
            ),
        ),
        reference_ebit=sourced(200.0, end),
        confidence="high",
    )
    p = normalize(raw(period(2025)), adjustments=(adjustment,))[0].annual[0]
    assert isclose(p.clean_ebit.value, 235.0)
    assert p.adjustment_ids == ("restructuring-2025",)


def test_extreme_roic_is_flagged_for_review():
    cash_rich = period(2025, cash=700.0)  # invested capital 150
    normalized, issues = normalize(raw(cash_rich))
    assert normalized.annual[0].roic.value > 1
    assert "annual:2025-12-31:roic" in issues


def test_a_pre_revenue_company_is_explained_not_a_crash():
    base = period(2025)
    values = {k: v for k, v in base.values.items() if k != "revenue"}
    statuses = {
        **base.statuses,
        "revenue": ConceptStatus(status="not_presented", basis="no revenue line"),
    }
    pre = base.model_copy(update={"values": values, "statuses": statuses})
    with pytest.raises(NormalizationError, match="pre-revenue"):
        normalize(raw(pre))


def test_securities_without_a_statement_line_are_not_deducted_twice():
    base = period(2025)
    values = {k: v for k, v in base.values.items() if k != "marketable_securities"}
    statuses = {
        **base.statuses,
        "marketable_securities": ConceptStatus(
            status="unresolved",
            reason="marketable securities disclosed in notes but not presented as a statement line",
        ),
    }
    normalized, _ = normalize(raw(base.model_copy(update={"values": values, "statuses": statuses})))
    capital = normalized.annual[0].invested_capital
    assert "no separate securities line" in (capital.prov.note or "")
    full, _ = normalize(raw(base))
    assert capital.value >= full.annual[0].invested_capital.value  # nothing deducted
