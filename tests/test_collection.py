"""Automatic collection inputs: industry mapping, cover-page shares and the 10-K seed."""

from datetime import date
from io import BytesIO
from unittest.mock import Mock

import pytest
from openpyxl import Workbook
from test_extraction import CUTOFF, END, FILING, START, fact, income, package

from dcf_agent.data.cache import DataUnavailable
from dcf_agent.data.industry import SHEET, match_industry, parse_classification
from dcf_agent.data.sec import Fact, SECClient
from dcf_agent.tools import data as tools

HEADER = ["Company Name", "Exchange:Ticker", "Industry Group", "Country", "SIC Code"]


def workbook(rows: list[list[object]], filler: int = 1000) -> bytes:
    book = Workbook()
    sheet = book.active
    sheet.title = SHEET
    sheet.append(["Damodaran industry classification"])  # title rows precede the header
    sheet.append(HEADER)
    for row in rows:
        sheet.append(row)
    for i in range(filler):
        sheet.append([f"Filler {i}", f"NYSE:F{i}", "Retail (General)", "United States", 5331])
    buffer = BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def test_classification_keeps_us_listings_and_normalizes_tickers():
    rows = parse_classification(
        workbook(
            [
                ["Apple Inc.", "NasdaqGS:AAPL", "Computers/Peripherals", "US", 3571],
                ["Berkshire (NYSE:BRK.B)", "NYSE:BRK.B", "Insurance (General)", "US", 6331],
                ["Toyota (TSE:7203)", "TSE:7203", "Auto & Truck", "Japan", 3711],
            ]
        )
    )
    tickers = {r["ticker"]: r for r in rows}
    assert tickers["AAPL"]["industry"] == "Computers/Peripherals"
    assert tickers["AAPL"]["sic"] == "3571"
    assert "7203" not in tickers and "BRK.B" in tickers


def test_classification_with_too_few_us_rows_is_rejected():
    with pytest.raises(DataUnavailable):
        parse_classification(workbook([], filler=10))


ROWS = [
    {"ticker": "AAPL", "company": "Apple", "industry": "Computers/Peripherals", "sic": "3571"},
    {"ticker": "BRK.B", "company": "Berkshire", "industry": "Insurance (General)", "sic": "6331"},
    {"ticker": "AAA", "company": "A", "industry": "Software (System & Application)", "sic": "7372"},
    {"ticker": "BBB", "company": "B", "industry": "Software (System & Application)", "sic": "7372"},
    {"ticker": "CCC", "company": "C", "industry": "Software (Internet)", "sic": "7372"},
    {"ticker": "OLD", "company": "Old", "industry": "Retired label", "sic": "7372"},
]
LABELS = {
    "Computers/Peripherals",
    "Insurance (General)",
    "Software (System & Application)",
    "Software (Internet)",
}


def test_ticker_match_wins_and_sec_share_class_tickers_match():
    assert match_industry(ROWS, "aapl", "9999", LABELS).method == "ticker"
    assert match_industry(ROWS, "BRK-B", None, LABELS).industry == "Insurance (General)"


def test_sic_fallback_proposes_the_majority_and_lists_alternatives():
    match = match_industry(ROWS, "NEW", "7372", LABELS)
    assert match.method == "sic" and match.industry == "Software (System & Application)"
    # Labels absent from the industry tables are never proposed.
    assert match.alternatives == ("Software (Internet)",)
    assert "67%" in match.note and "confirm" in match.note


def test_no_ticker_or_sic_match_asks_for_an_explicit_choice():
    with pytest.raises(DataUnavailable, match="choose one explicitly"):
        match_industry(ROWS, "NEW", "0100", LABELS)


def shares(value: float, end: date) -> Fact:
    return Fact(
        namespace="dei",
        tag="EntityCommonStockSharesOutstanding",
        unit="shares",
        value=value,
        start=None,
        end=end,
        filed=FILING.filed,
        accession=FILING.accession,
        form="10-K",
        fiscal_year=2025,
        fiscal_period="FY",
    )


def test_cover_shares_use_the_total_with_its_measurement_date():
    found = tools.package_cover_shares(
        package([], [shares(900.0, date(2026, 1, 20)), shares(910.0, date(2026, 2, 10))]),
        "0000000001",
        CUTOFF,
    )
    assert found is not None
    assert (found.shares.value, found.observed_on) == (910.0, date(2026, 2, 10))
    assert found.shares.prov.period_end == date(2026, 2, 10)


def test_cover_shares_sum_share_classes_when_no_total_is_tagged():
    key = ("dei:EntityCommonStockSharesOutstanding", None, date(2026, 2, 10), "shares")
    axis = "us-gaap:StatementClassOfStockAxis"
    dimensional = {
        (*key, ((axis, "us-gaap:CommonClassAMember"),)): 300.0,
        (*key, ((axis, "us-gaap:CommonClassBMember"),)): 1200.0,
        # Another axis is not a share-class split.
        (*key, (("dei:LegalEntityAxis", "ex:SubsidiaryMember"),)): 50.0,
    }
    found = tools.package_cover_shares(package([], [], dimensional=dimensional), "1", CUTOFF)
    assert found is not None and found.shares.value == 1500.0
    assert "2 share classes" in (found.shares.prov.note or "")
    assert tools.package_cover_shares(package([], []), "1", CUTOFF) is None


def full_year_package():
    rows = [
        ("us-gaap:Revenues", "Revenue"),
        ("us-gaap:CostOfRevenue", "Cost of revenue"),
        ("us-gaap:NetIncomeLoss", "Net income"),
        ("us-gaap:EarningsPerShareBasic", "EPS"),
    ]
    calcs = [
        ("us-gaap:NetIncomeLoss", "us-gaap:Revenues", 1),
        ("us-gaap:NetIncomeLoss", "us-gaap:CostOfRevenue", -1),
    ]
    values = {
        "us-gaap:Revenues": 100,
        "us-gaap:CostOfRevenue": 60,
        "us-gaap:NetIncomeLoss": 40,
    }
    sheet, facts = income(rows, calcs, values)
    # The 10-K also presents an earlier quarter; only the full year anchors the seed.
    quarter = date(2025, 10, 1)
    facts.append(fact("us-gaap:Revenues", 30, quarter))
    return package([sheet], facts)


def test_seed_takes_the_latest_10k_full_year_period_and_values(monkeypatch):
    sec = Mock(spec=SECClient)
    later_q = FILING.model_copy(
        update={"accession": "q", "form": "10-Q", "report_date": date(2026, 3, 31)}
    )
    sec.get_submissions.return_value = (later_q, FILING)
    monkeypatch.setattr(tools, "reporting_entity", lambda *_: None)
    monkeypatch.setattr(tools, "read_package", lambda *_: full_year_package())
    seed = tools.filing_seed(sec, "0000000001", CUTOFF)
    assert seed.filing == FILING and seed.source_cik == "0000000001"
    assert (seed.period.period_start, seed.period.period_end) == (START, END)
    assert seed.period.fiscal_year == 2025
    assert seed.period.values["revenue"].value == 100
    assert seed.period.values["net_income"].value == 40
    assert seed.seed.annual == (seed.period,)


def test_seed_needs_an_eligible_10k(monkeypatch):
    sec = Mock(spec=SECClient)
    sec.get_submissions.return_value = (FILING.model_copy(update={"filed": date(2026, 12, 1)}),)
    monkeypatch.setattr(tools, "reporting_entity", lambda *_: None)
    with pytest.raises(DataUnavailable, match="No eligible 10-K"):
        tools.filing_seed(sec, "0000000001", CUTOFF)
