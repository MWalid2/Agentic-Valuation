"""Hand-computed histories: fiscal calendars, YTD arithmetic, vintages, and TTM."""

from datetime import date, timedelta

import pytest

from dcf_agent.contracts.inputs import FinancialPeriod, RawFinancials
from dcf_agent.data.history import build_history, build_ttm
from dcf_agent.data.sec import Fact, Filing

CUTOFF = date(2026, 10, 11)


def history_case():
    facts, filings = [], []
    for year in range(2020, 2027):
        for quarter, (month, day) in enumerate(((3, 31), (6, 30), (9, 30), (12, 31)), 1):
            end = date(year, month, day)
            filed = end + timedelta(days=45)
            accession = f"0000000001-{year % 100:02}-{quarter:06}"
            form = "10-K" if quarter == 4 else "10-Q"
            filings.append(
                Filing(
                    accession=accession,
                    form=form,
                    filed=filed,
                    report_date=end,
                    primary_document="report.htm",
                )
            )
            for tag, value, start in (
                (
                    "RevenueFromContractWithCustomerExcludingAssessedTax",
                    (10, 30, 60, 100)[quarter - 1],
                    date(year, 1, 1),
                ),
                (
                    "NetCashProvidedByUsedInOperatingActivities",
                    (1, 3, 6, 10)[quarter - 1],
                    date(year, 1, 1),
                ),
                ("CashAndCashEquivalentsAtCarryingValue", 100 + quarter, None),
            ):
                facts.append(
                    Fact(
                        namespace="us-gaap",
                        tag=tag,
                        unit="USD",
                        value=float(value),
                        start=start,
                        end=end,
                        filed=filed,
                        accession=accession,
                        form=form,
                        fiscal_year=year,
                        fiscal_period="FY" if quarter == 4 else f"Q{quarter}",
                    )
                )
    annual = next(f for f in facts if f.end == date(2025, 12, 31) and f.tag.startswith("Revenue"))
    seed = RawFinancials(
        cik="0000000001",
        as_of=CUTOFF,
        annual=(
            FinancialPeriod(
                kind="annual",
                fiscal_year=2025,
                period_start=date(2025, 1, 1),
                period_end=date(2025, 12, 31),
                values={"revenue": annual.sourced("1", CUTOFF)},
            ),
        ),
    )
    return seed, tuple(facts), tuple(filings)


def test_five_years_eight_quarters_and_ttm_arithmetic():
    raw, issues = build_history(*history_case())
    assert [p.fiscal_year for p in raw.annual] == [2021, 2022, 2023, 2024, 2025]
    assert len(raw.quarterly) == 8
    assert raw.quarterly[-1].period_end == date(2026, 6, 30)
    q4 = next(p for p in raw.quarterly if p.fiscal_year == 2025 and p.fiscal_quarter == 4)
    assert q4.values["revenue"].value == 40
    assert q4.values["operating_cash_flow"].value == 4
    assert raw.quarterly[-1].values["operating_cash_flow"].value == 2
    assert raw.ttm.period_start == date(2025, 7, 1)
    assert raw.ttm.values["revenue"].value == 100
    assert raw.ttm.values["operating_cash_flow"].value == 10
    assert raw.ttm.values["cash"].value == 102  # Latest balance, not four balances added.
    assert "Quarter = cumulative" in q4.values["revenue"].prov.note
    assert "debt" not in raw.ttm.values
    assert "ttm:2026-06-30:debt" in issues


def test_future_fact_excluded_and_comparative_fiscal_label_not_used():
    seed, facts, filings = history_case()
    original = next(f for f in facts if f.end == date(2022, 12, 31) and f.tag.startswith("Revenue"))
    restated = original.model_copy(
        update={
            "value": 110.0,
            "fiscal_year": 2025,
            "filed": date(2026, 2, 14),
            "accession": "0000000001-25-000004",
        }
    )
    future = restated.model_copy(update={"value": 999.0, "filed": CUTOFF + timedelta(days=1)})
    raw, _ = build_history(seed, (*facts, restated, future), filings)
    period = next(p for p in raw.annual if p.fiscal_year == 2022)
    assert period.values["revenue"].value == 110
    assert period.period_start == date(2022, 1, 1)


def test_conflicting_latest_alias_does_not_fall_back_to_an_old_value():
    seed, facts, filings = history_case()
    original = next(f for f in facts if f.end == date(2026, 6, 30) and f.tag.startswith("Revenue"))
    conflict = original.model_copy(update={"tag": "Revenues", "value": 99.0})
    raw, issues = build_history(seed, (*facts, conflict), filings)
    assert "revenue" not in raw.quarterly[-1].values
    assert raw.ttm is None
    assert any("Conflicting latest" in message for message in issues.values())


def test_gap_cannot_be_summed_into_ttm():
    seed, facts, filings = history_case()
    filings = tuple(f for f in filings if f.report_date != date(2025, 9, 30))
    raw, issues = build_history(seed, facts, filings)
    assert raw.ttm is None and "ttm" in issues
    assert not any(p.fiscal_year == 2025 and p.fiscal_quarter == 4 for p in raw.quarterly)


def test_reported_quarter_preferred_but_disagreement_is_flagged():
    seed, facts, filings = history_case()
    cumulative = next(
        f for f in facts if f.end == date(2026, 6, 30) and f.tag.startswith("Revenue")
    )
    direct = cumulative.model_copy(update={"start": date(2026, 4, 1), "value": 25.0})
    raw, issues = build_history(seed, (*facts, direct), filings)
    assert raw.quarterly[-1].values["revenue"].value == 25.0
    assert "quarter:2026:Q2:revenue:tie_out" in issues


def test_unrecognized_latest_filing_suppresses_stale_ttm():
    seed, facts, filings = history_case()
    facts = tuple(f for f in facts if f.end != date(2026, 6, 30))
    raw, issues = build_history(seed, facts, filings)
    assert raw.ttm is None
    assert "stale TTM" in issues["ttm"]


@pytest.mark.parametrize("days", [364, 371])
def test_non_december_52_and_53_week_fiscal_calendar(days):
    start = date(2024, 6, 2)
    durations = (91, 182, 273, days)
    facts, filings = [], []
    for quarter, duration in enumerate(durations, 1):
        end = start + timedelta(days=duration - 1)
        accession = f"0000000001-25-{quarter:06}"
        filed = end + timedelta(days=30)
        form = "10-K" if quarter == 4 else "10-Q"
        filings.append(
            Filing(
                accession=accession,
                filed=filed,
                report_date=end,
                form=form,
                primary_document="report.htm",
            )
        )
        facts.append(
            Fact(
                namespace="us-gaap",
                tag="Revenues",
                unit="USD",
                value=quarter * 10.0,
                start=start,
                end=end,
                filed=filed,
                accession=accession,
                form=form,
                fiscal_year=2025,
                fiscal_period="FY" if quarter == 4 else f"Q{quarter}",
            )
        )
    seed = RawFinancials(
        cik="0000000001",
        as_of=CUTOFF,
        annual=(
            FinancialPeriod(
                kind="annual",
                fiscal_year=2025,
                period_start=start,
                period_end=facts[-1].end,
                values={"revenue": facts[-1].sourced("1", CUTOFF)},
            ),
        ),
    )
    raw, _ = build_history(seed, tuple(facts), tuple(filings), annual_count=1, quarter_count=4)
    assert len(raw.quarterly) == 4 and raw.ttm is not None
    assert raw.ttm.values["revenue"].value == 40
    assert (raw.ttm.period_end - raw.ttm.period_start).days + 1 == days


def test_ttm_rejects_duplicate_quarters_and_missing_flow():
    raw, _ = build_history(*history_case())
    quarters = raw.quarterly[-4:]
    assert build_ttm((*quarters[:3], quarters[2]), CUTOFF) is None
    damaged = quarters[-1].model_copy(update={"values": {"cash": quarters[-1].values["cash"]}})
    assert build_ttm((*quarters[:3], damaged), CUTOFF) is None


def test_earlier_cutoff_rebuilds_only_eligible_periods():
    seed, facts, filings = history_case()
    older = next(f for f in facts if f.end == date(2023, 12, 31) and f.tag.startswith("Revenue"))
    cutoff = date(2024, 8, 20)
    seed = RawFinancials(
        cik=seed.cik,
        as_of=cutoff,
        annual=(
            FinancialPeriod(
                kind="annual",
                fiscal_year=2023,
                period_start=date(2023, 1, 1),
                period_end=older.end,
                values={"revenue": older.sourced("1", cutoff)},
            ),
        ),
    )
    raw, _ = build_history(seed, facts, filings)
    assert raw.annual[-1].fiscal_year == 2023
    assert raw.ttm.period_end == date(2024, 6, 30)
    assert all(
        v.prov.filed <= cutoff
        for p in (*raw.annual, *raw.quarterly, raw.ttm)
        for v in p.values.values()
    )


def lease_fact(tag, value, end, filed, accession, form="10-K"):
    return Fact(
        namespace="us-gaap",
        tag=tag,
        unit="USD",
        value=float(value),
        start=None,
        end=end,
        filed=filed,
        accession=accession,
        form=form,
        fiscal_year=end.year,
        fiscal_period="FY",
    )


def test_components_pair_within_one_filing_despite_later_partial_comparative():
    seed, facts, filings = history_case()
    end = date(2025, 12, 31)
    annual = "0000000001-25-000004"
    later = "0000000001-26-000001"
    extra = (
        lease_fact("OperatingLeaseLiabilityCurrent", 5, end, date(2026, 2, 14), annual),
        lease_fact("OperatingLeaseLiabilityNoncurrent", 20, end, date(2026, 2, 14), annual),
        # A later 10-Q repeats only the noncurrent balance as a comparative.
        lease_fact("OperatingLeaseLiabilityNoncurrent", 20, end, date(2026, 5, 15), later, "10-Q"),
    )
    raw, issues = build_history(seed, (*facts, *extra), filings)
    period = next(p for p in raw.annual if p.period_end == end)
    assert period.values["operating_lease_liabilities"].value == 25
    assert annual in period.values["operating_lease_liabilities"].prov.note
    assert not any("vintage" in message for message in issues.values())


def test_latest_complete_filing_supersedes_older_components():
    seed, facts, filings = history_case()
    end = date(2025, 12, 31)
    extra = (
        lease_fact(
            "OperatingLeaseLiabilityCurrent", 5, end, date(2026, 2, 14), "0000000001-25-000004"
        ),
        lease_fact(
            "OperatingLeaseLiabilityNoncurrent", 20, end, date(2026, 2, 14), "0000000001-25-000004"
        ),
        lease_fact(
            "OperatingLeaseLiability", 27, end, date(2026, 5, 15), "0000000001-26-000001", "10-Q"
        ),
    )
    raw, _ = build_history(seed, (*facts, *extra), filings)
    period = next(p for p in raw.annual if p.period_end == end)
    assert period.values["operating_lease_liabilities"].value == 27


def test_total_disagreeing_with_components_in_one_filing_is_unresolved():
    seed, facts, filings = history_case()
    end = date(2025, 12, 31)
    accession = "0000000001-25-000004"
    extra = (
        lease_fact("OperatingLeaseLiability", 30, end, date(2026, 2, 14), accession),
        lease_fact("OperatingLeaseLiabilityCurrent", 5, end, date(2026, 2, 14), accession),
        lease_fact("OperatingLeaseLiabilityNoncurrent", 20, end, date(2026, 2, 14), accession),
    )
    raw, issues = build_history(seed, (*facts, *extra), filings)
    period = next(p for p in raw.annual if p.period_end == end)
    assert "operating_lease_liabilities" not in period.values
    assert issues["2025-01-01/2025-12-31:operating_lease_liabilities"] == (
        "Total disagrees with components"
    )
