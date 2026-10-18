"""Statuses through history: package precedence, carry-forward, one basis, readiness."""

from datetime import date, timedelta

import pytest
from pydantic import ValidationError
from test_history import CUTOFF, history_case

from dcf_agent.contracts.inputs import ConceptStatus, FinancialPeriod, RawFinancials
from dcf_agent.contracts.provenance import Provenance, Sourced
from dcf_agent.data.extraction import ConceptValue, Extraction
from dcf_agent.data.history import build_history, measurement_basis
from dcf_agent.data.readiness import assess_readiness
from dcf_agent.data.sec import Filing
from dcf_agent.data.xbrl import XBRLPackage


def sourced(value: float, end: date, filed: date) -> Sourced[float]:
    return Sourced[float](
        value=value,
        prov=Provenance(
            source="sec_xbrl",
            ref="ref",
            as_of=CUTOFF,
            period_end=end,
            filed=filed,
            confidence="high",
        ),
    )


def extraction(end: date, filed: date, values: dict, start: date | None = None) -> Extraction:
    """A filing whose own statements give these concept values for one period."""
    filing = Filing(
        accession=f"0000000009-{end.year % 100:02}-{end.month:06}",
        form="10-K" if end.month == 12 else "10-Q",
        filed=filed,
        report_date=end,
        primary_document="x.htm",
    )
    package = XBRLPackage(
        cik="0000000001",
        filing=filing,
        company_prefix="ex",
        fiscal_year=end.year,
        fiscal_period="FY",
        facts=(),
        decimals={},
        elements={},
        labels={},
        statements={},
    )
    periods = {}
    for concept, item in values.items():
        key = (None, end) if start is None else (start, end)
        if isinstance(item, ConceptValue):
            periods.setdefault(key, {})[concept] = item
        else:
            amount, basis = item if isinstance(item, tuple) else (item, None)
            periods.setdefault(key, {})[concept] = ConceptValue(
                "reported", sourced(amount, end, filed), basis=basis
            )
    return Extraction(package, {}, periods)


QUARTER_ENDS = [
    date(y, m, d) for y in (2024, 2025, 2026) for m, d in ((3, 31), (6, 30), (9, 30), (12, 31))
]
QUARTER_ENDS = [e for e in QUARTER_ENDS if e <= date(2026, 6, 30)]


def statuses_everywhere(concept: str, status: str) -> tuple[Extraction, ...]:
    return tuple(
        extraction(end, end + timedelta(days=45), {concept: ConceptValue(status, reason="test")})
        for end in QUARTER_ENDS
    )


def test_statement_structure_wins_over_tag_rule_and_records_why():
    seed, facts, filings = history_case()
    end = date(2025, 12, 31)
    structured = extraction(end, end + timedelta(days=45), {"cash": 999})
    raw, _ = build_history(seed, facts, filings, extractions=(structured,))
    period = next(p for p in raw.annual if p.period_end == end)
    assert period.values["cash"].value == 999
    assert "statement structure used" in period.statuses["cash"].reason


def test_broader_statement_basis_never_overrides_an_exact_value():
    seed, facts, filings = history_case()
    end = date(2025, 12, 31)
    broader = extraction(end, end + timedelta(days=45), {"cash": (150, "includes restricted cash")})
    raw, _ = build_history(seed, facts, filings, extractions=(broader,))
    period = next(p for p in raw.annual if p.period_end == end)
    assert period.values["cash"].value == 104  # Exact tagged cash kept.
    assert "exact value kept" in period.statuses["cash"].reason


def test_evidenced_absence_flows_to_annual_quarters_and_ttm():
    seed, facts, filings = history_case()
    raw, issues = build_history(
        seed, facts, filings, extractions=statuses_everywhere("minority_interest", "not_presented")
    )
    for period in (raw.annual[-1], raw.quarterly[-1], raw.ttm):
        assert period.statuses["minority_interest"].status == "not_presented"
        assert "minority_interest" not in period.values
    assert issues[f"ttm:{raw.ttm.period_end}:minority_interest"].startswith("not_presented")


def test_undisclosed_quarter_balance_carries_the_annual_observation_with_its_date():
    seed, facts, filings = history_case()
    annual_end = date(2025, 12, 31)
    extractions = statuses_everywhere("operating_lease_liabilities", "not_disclosed")
    extractions += (
        extraction(annual_end, date(2026, 2, 20), {"operating_lease_liabilities": 300}),
    )
    raw, _ = build_history(seed, facts, filings, extractions=extractions)
    latest = raw.quarterly[-1]
    status = latest.statuses["operating_lease_liabilities"]
    assert status.status == "carried_forward" and status.observed_on == annual_end
    assert latest.values["operating_lease_liabilities"].prov.period_end == annual_end
    assert raw.ttm.statuses["operating_lease_liabilities"].status == "carried_forward"
    # Observations older than one fiscal year are not carried.
    old = statuses_everywhere("operating_lease_liabilities", "not_disclosed") + (
        extraction(date(2024, 12, 31), date(2025, 2, 20), {"operating_lease_liabilities": 300}),
    )
    raw, _ = build_history(seed, facts, filings, extractions=old)
    assert "operating_lease_liabilities" not in raw.quarterly[-1].values


def test_unresolved_balance_is_never_carried_forward():
    seed, facts, filings = history_case()
    extractions = statuses_everywhere("debt", "unresolved") + (
        extraction(date(2025, 12, 31), date(2026, 2, 20), {"debt": 500}),
    )
    raw, _ = build_history(seed, facts, filings, extractions=extractions)
    assert raw.quarterly[-1].statuses["debt"].status == "unresolved"
    assert "debt" not in raw.quarterly[-1].values


def payables_case(annual_alternative: bool):
    """Exact payables in annual filings; quarters show only payables with accrued."""
    seed, facts, filings = history_case()
    extractions = []
    for end in QUARTER_ENDS:
        filed = end + timedelta(days=45)
        if end.month == 12:
            values = {"payables": (900 + end.year, "includes accrued liabilities")}
            if not annual_alternative:
                values = {}
            facts_end = end
        else:
            values = {"payables": (800 + end.month, "includes accrued liabilities")}
            facts_end = None
        extractions.append(extraction(end, filed, values))
        if facts_end is not None:
            facts += tuple(
                f.model_copy(
                    update={"tag": "AccountsPayableCurrent", "value": 100.0, "start": None}
                )
                for f in facts
                if f.end == end and f.tag == "CashAndCashEquivalentsAtCarryingValue"
            )
    return seed, facts, filings, tuple(extractions)


def test_mixed_bases_are_harmonized_to_the_broader_line_when_every_period_has_it():
    seed, facts, filings, extractions = payables_case(annual_alternative=True)
    raw, _ = build_history(seed, facts, filings, extractions=extractions)
    bases = {
        measurement_basis(p.statuses["payables"].basis)
        for p in (*raw.annual[-2:], raw.ttm)
        if "payables" in p.values
    }
    assert bases == {"includes accrued liabilities"}
    readiness = assess_readiness(raw, {})
    assert "basis_inconsistent" not in readiness["normalization"].missing


def test_mixed_bases_without_a_consistent_alternative_block_normalization():
    seed, facts, filings, extractions = payables_case(annual_alternative=False)
    raw, issues = build_history(seed, facts, filings, extractions=extractions)
    readiness = assess_readiness(raw, issues)
    assert readiness["normalization"].missing.get("basis_inconsistent") == ("payables",)


def period(statuses: dict, values: dict | None = None) -> FinancialPeriod:
    end = date(2025, 12, 31)
    return FinancialPeriod(
        kind="ttm",
        fiscal_year=2025,
        period_start=date(2025, 1, 1),
        period_end=end,
        values={
            c: sourced(v, end, date(2026, 2, 1)) for c, v in (values or {"revenue": 1}).items()
        },
        statuses={c: ConceptStatus(status=s, reason="test") for c, s in statuses.items()},
    )


def test_readiness_accepts_evidenced_zero_and_documented_fallbacks_only():
    ttm = period(
        {
            "minority_interest": "not_presented",
            "preferred_equity": "not_presented",
            "debt": "not_presented",
            "marketable_securities": "not_presented",
            "interest_expense": "not_disclosed",
            "temporary_equity": "not_presented",
            "captive_finance_debt": "not_presented",
        },
        {
            "revenue": 1,
            "cash": 5,
            "ebit": 2,
            "equity": 3,
            "pretax_income": 2,
            "income_tax_expense": 1,
        },
    )
    raw = RawFinancials(cik="0000000001", as_of=CUTOFF, ttm=ttm)
    report = assess_readiness(raw, {})
    assert report["equity_bridge"].status == "needs_review"
    assert report["wacc"].status == "needs_review"  # Debt-free; interest fallback documented.
    blocked = period({"cash": "not_presented"}, {"revenue": 1})
    report = assess_readiness(RawFinancials(cik="0000000001", as_of=CUTOFF, ttm=blocked), {})
    assert "cash (not_presented)" in report["equity_bridge"].missing["ttm:2025-12-31"]


def test_short_history_of_a_recent_registrant_is_a_review_note_not_a_block():
    annual = tuple(
        FinancialPeriod(
            kind="annual",
            fiscal_year=y,
            period_start=date(y, 1, 1),
            period_end=date(y, 12, 31),
            values={"revenue": sourced(1, date(y, 12, 31), date(y + 1, 2, 1))},
        )
        for y in (2024, 2025)
    )
    raw = RawFinancials(cik="0000000001", as_of=CUTOFF, annual=annual)
    short = {
        "annual_history": "Only 2/5 annual periods available; "
        "the registrant has no earlier annual reports"
    }
    assert "annual_history" not in assess_readiness(raw, short)["normalization"].missing
    assert "annual_history" in assess_readiness(raw, {})["normalization"].missing


def test_status_contract_keeps_values_and_absence_consistent():
    with pytest.raises(ValidationError, match="disagrees with its value"):
        period({"revenue": "not_presented"})
    with pytest.raises(ValidationError, match="disagrees with its value"):
        period({"debt": "reported"})
    with pytest.raises(ValidationError, match="observation date"):
        ConceptStatus(status="reported", observed_on=date(2025, 1, 1))
    end = date(2025, 12, 31)
    carried = FinancialPeriod(
        kind="ttm",
        fiscal_year=2025,
        period_start=date(2025, 1, 1),
        period_end=end,
        values={"debt": sourced(5, date(2025, 6, 30), date(2025, 8, 1))},
        statuses={"debt": ConceptStatus(status="carried_forward", observed_on=date(2025, 6, 30))},
    )
    assert carried.values["debt"].prov.period_end == date(2025, 6, 30)


def test_quarter_and_ttm_treat_evidenced_absence_as_zero():
    seed, facts, filings = history_case()
    # Income tax appears only in annual reports; quarterly statements present no tax line.
    extractions = []
    for end in QUARTER_ENDS:
        start = date(end.year, 1, 1)
        item = (
            {"income_tax_expense": 12}
            if end.month == 12
            else {"income_tax_expense": ConceptValue("not_presented", reason="no tax line")}
        )
        extractions.append(extraction(end, end + timedelta(days=45), item, start=start))
    raw, _ = build_history(seed, facts, filings, extractions=tuple(extractions))
    q4 = next(p for p in raw.quarterly if p.fiscal_quarter == 4 and p.fiscal_year == 2025)
    assert q4.values["income_tax_expense"].value == 12
    assert raw.ttm.values["income_tax_expense"].value == 12
    assert "evidenced zero" in raw.ttm.statuses["income_tax_expense"].basis


def test_ttm_is_not_disclosed_when_a_quarter_lacks_the_value():
    seed, facts, filings = history_case()
    extractions = []
    for end in QUARTER_ENDS:
        start = date(end.year, 1, 1)
        item = (
            {"interest_expense": 4}
            if end.month == 12
            else {"interest_expense": ConceptValue("not_disclosed", reason="none")}
        )
        extractions.append(extraction(end, end + timedelta(days=45), item, start=start))
    raw, _ = build_history(seed, facts, filings, extractions=tuple(extractions))
    assert raw.ttm.statuses["interest_expense"].status == "not_disclosed"
    assert "interest_expense" not in raw.ttm.values


def test_securities_inside_other_lines_do_not_block_the_bridge():
    values = {"revenue": 1, "cash": 5, "debt": 2}
    statuses = {
        c: "not_presented"
        for c in (
            "minority_interest",
            "preferred_equity",
            "temporary_equity",
            "captive_finance_debt",
        )
    }
    ttm = period(statuses, values).model_copy(
        update={
            "statuses": {
                **period(statuses, values).statuses,
                "marketable_securities": ConceptStatus(
                    status="unresolved",
                    reason="marketable securities disclosed in notes but not presented as a "
                    "statement line",
                ),
            }
        }
    )
    report = assess_readiness(RawFinancials(cik="0000000001", as_of=CUTOFF, ttm=ttm), {})
    assert not any(
        "marketable_securities" in item
        for items in report["equity_bridge"].missing.values()
        for item in items
    )
    assert any("inside other statement lines" in n for n in report["equity_bridge"].review_notes)
