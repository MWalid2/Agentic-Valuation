"""Router: message split, company resolution, dates, intent, scope and plan."""

import json
from datetime import date
from pathlib import Path
from unittest.mock import Mock

import pytest
from test_llm import config as llm_config

from dcf_agent.agents.router import Router, rule_intent
from dcf_agent.data.companies import CompanyIndex, tokens
from dcf_agent.data.dates import find_date
from dcf_agent.data.sec import Filing, SECClient
from dcf_agent.llm import LocalLLM

TODAY = date(2026, 10, 15)
LISTINGS = [
    ("AAPL", "0000320193", "Apple Inc."),
    ("APLE", "0001418121", "Apple Hospitality REIT, Inc."),
    ("KO", "0000021344", "COCA COLA CO"),
    ("COKE", "0000317540", "Coca-Cola Consolidated, Inc."),
    ("GOOGL", "0001652044", "Alphabet Inc."),
    ("GOOG", "0001652044", "Alphabet Inc."),
    ("F", "0000037996", "FORD MOTOR CO"),
    ("F-PB", "0000037996", "FORD MOTOR CO"),
    ("FORD", "0000038264", "FORWARD INDUSTRIES INC"),
    ("JNJ", "0000200406", "JOHNSON & JOHNSON"),
    ("JCI", "0000833444", "Johnson Controls International plc"),
    ("XOM", "0002115436", "ExxonMobil Holdings Corp"),
    ("TGT", "0000027419", "TARGET CORP"),
    ("CBDY", "0001111111", "Target Group Inc."),
    ("MSFT", "0000789019", "MICROSOFT CORP"),
    ("JPM", "0000019617", "JPMORGAN CHASE & CO"),
    ("MCD", "0000063908", "MCDONALDS CORP"),
]
FILINGS = (
    Filing(
        accession="0000320187-26-000050",
        form="10-K",
        filed=date(2026, 7, 20),
        report_date=date(2026, 5, 31),
        primary_document="k.htm",
    ),
    Filing(
        accession="0000320187-25-000050",
        form="10-K",
        filed=date(2025, 7, 20),
        report_date=date(2025, 5, 31),
        primary_document="k.htm",
    ),
)


def sec_client(sic: str = "3571") -> Mock:
    sec = Mock(spec=SECClient)
    sec.get_company_list.return_value = LISTINGS
    sec.get_company_profile.return_value = {"name": "X", "sic": sic, "sic_description": "d"}
    sec.get_submissions.side_effect = lambda cik, as_of: tuple(
        f for f in FILINGS if f.filed <= as_of
    )
    return sec


def router(sic: str = "3571", llm: LocalLLM | None = None) -> Router:
    return Router(sec_client(sic), TODAY, llm)


@pytest.mark.parametrize(
    ("name", "words"),
    [
        ("COCA COLA CO", ("coca", "cola")),
        ("McDonald's", ("mcdonalds",)),
        ("PROCTER & GAMBLE Co", ("procter", "gamble")),
        ("COSTCO WHOLESALE CORP /NEW", ("costco", "wholesale")),
        ("The Walt Disney Company", ("walt", "disney")),
    ],
)
def test_names_drop_legal_forms_and_punctuation(name, words):
    assert tokens(name) == words


@pytest.mark.parametrize(
    ("query", "status", "ticker"),
    [
        ("Apple", "resolved", "AAPL"),  # exact name beats the REIT's longer name
        ("KO", "resolved", "KO"),
        ("Coca-Cola", "resolved", "KO"),
        ("GOOG", "resolved", "GOOG"),  # an explicit share-class ticker is kept
        ("Alphabet", "resolved", "GOOGL"),  # the SEC's primary listing
        ("Ford", "resolved", "F"),  # a name, not the FORD ticker; never preferred stock
        ("Exxon Mobil", "resolved", "XOM"),  # spacing differs from the registrant name
        ("Target", "resolved", "TGT"),  # "Target Group" needs a word dropped
        ("Microsft", "resolved", "MSFT"),  # spelling
        ("JPMorgan", "resolved", "JPM"),
        ("McDonald's", "resolved", "MCD"),
        ("Johnson", "ambiguous", "JNJ"),
        ("Nonexistent Widgets", "not_found", None),
    ],
)
def test_company_resolution(query, status, ticker):
    result = CompanyIndex(LISTINGS).resolve(query)
    assert result.status == status
    if status == "resolved":
        assert result.best.ticker == ticker
    elif ticker:
        assert ticker in [c.ticker for c in result.candidates]


def test_other_share_classes_are_noted_without_preferred_listings():
    best = CompanyIndex(LISTINGS).resolve("Ford").best
    assert best.other_classes == ()
    assert CompanyIndex(LISTINGS).resolve("Alphabet").best.other_classes == ("GOOG",)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-10-11", date(2026, 10, 11)),
        ("10/01/2026", date(2026, 10, 1)),
        ("Oct 1, 2026", date(2026, 10, 1)),
        ("1 October 2026", date(2026, 10, 1)),
        ("today", TODAY),
        ("yesterday", date(2026, 10, 14)),
        ("30 days ago", date(2026, 9, 15)),
        ("2 weeks ago", date(2026, 10, 1)),
        ("3 months ago", date(2026, 7, 15)),
        ("a year ago", date(2025, 10, 15)),
        ("end of Q3 2025", date(2025, 9, 30)),
        ("the second quarter of 2026", date(2026, 6, 30)),
        ("end of 2025", date(2025, 12, 31)),
        ("end of last quarter", date(2026, 9, 30)),
        ("last month end", date(2026, 9, 30)),
        ("end of last year", date(2025, 12, 31)),
    ],
)
def test_dates(text, expected):
    found = find_date(text, TODAY)
    assert found is not None and found.error is None and found.value == expected


@pytest.mark.parametrize("text", ["2027-01-15", "2026-10-16", "1990-01-01", "2026-02-30"])
def test_future_early_and_invalid_dates_are_rejected(text):
    found = find_date(text, TODAY)
    assert found is not None and found.value is None or found.error is not None


def test_last_fiscal_year_end_uses_the_latest_10k():
    found = find_date("last fiscal year end", TODAY, lambda: date(2026, 5, 31))
    assert found.value == date(2026, 5, 31)
    assert find_date("last fiscal year end", TODAY).error is not None


@pytest.mark.parametrize(
    ("question", "intent"),
    [
        ("latest revenue and free cash flow?", "I1"),
        ("margin and ROIC trend over 5 years", "I2"),
        ("how has operating margin evolved since 2021?", "I2"),
        ("which beta should we use?", "I3"),
        ("why did you use 3% terminal growth?", "I4"),
        ("what is the bull-case revenue in 2030?", "I5"),
        ("fair value per share?", "I6"),
        ("what if WACC rises by 1%?", "I7"),
        ("how does it compare to its peers?", "I8"),
        ("build the full Excel model", "I9"),
        ("where does the lease liability number come from?", "I10"),
        ("which XBRL element is the debt figure taken from?", "I10"),
        ("set the operating margin to 8%", "I11"),
        ("tell me something interesting", None),
    ],
)
def test_intent_patterns(question, intent):
    assert rule_intent(question) == intent


def test_company_and_date_only_opens_the_gate_menu():
    route = router().route("Apple, 2026-10-11")
    assert route.ready and route.extraction == "rules"
    assert route.company.ticker == "AAPL" and route.valuation_date == date(2026, 10, 11)
    assert route.intent is None and route.plan == ("gate0_menu",)
    request = route.request("info")
    assert request.cik == "0000320193" and request.question == "Collect inputs"


def test_written_date_with_comma_and_question():
    route = router().route("Apple, Oct 1, 2026, what is the cost of equity?")
    assert route.valuation_date == date(2026, 10, 1)
    assert route.question == "what is the cost of equity?"
    assert route.intent == "I3" and route.plan == ("data", "wacc", "answer")


def test_no_date_assumes_today_and_says_so():
    route = router().route("Apple")
    assert route.valuation_date == TODAY and "today assumed" in route.date_source


def test_future_date_is_not_ready():
    route = router().route("Apple, 2027-01-15")
    assert route.date_error and route.valuation_date is None and not route.ready
    with pytest.raises(ValueError):
        route.request()


def test_financial_company_is_out_of_scope():
    route = router(sic="6021").route("JPMorgan, 2026-10-11")
    assert route.scope.status == "blocked" and not route.ready


def test_date_before_any_periodic_filing_is_blocked():
    route = router().route("Apple, 2025-01-15")
    assert route.scope.status == "blocked"
    assert "No 10-K or 10-Q filed" in route.scope.missing["scope"][-1]


def test_ambiguous_company_lists_candidates_for_gate_0():
    route = router().route("Johnson, 2026-10-11")
    assert route.status == "ambiguous" and route.company is None and route.scope is None
    assert {"JNJ", "JCI"} <= {c.ticker for c in route.candidates}


def test_last_fiscal_year_end_is_resolved_with_the_company():
    route = router().route("Apple, last fiscal year end")
    assert route.valuation_date == date(2026, 5, 31)


def test_free_form_message_asks_the_model_for_the_company_phrase(tmp_path: Path):
    reply = json.dumps({"company": "Target"})
    with LocalLLM(llm_config(tmp_path), mock_responses=[reply]) as llm:
        route = router(llm=llm).route("Is Target undervalued as of 2026-09-01?")
    assert route.extraction == "model" and route.company.ticker == "TGT"
    assert route.intent == "I6" and route.intent_source == "rule"
    assert route.question == "Is Target undervalued as of 2026-09-01?"


def test_a_phrase_absent_from_the_message_is_discarded(tmp_path: Path):
    reply = json.dumps({"company": "Microsoft"})
    with LocalLLM(llm_config(tmp_path), mock_responses=[reply]) as llm:
        route = router(llm=llm).route("Is this stock undervalued?")
    assert route.status == "not_found" and route.company is None


def test_model_labels_a_question_no_pattern_recognizes(tmp_path: Path):
    reply = json.dumps({"intent": "I8", "rationale": "Asks for a comparison"})
    with LocalLLM(llm_config(tmp_path), mock_responses=[reply]) as llm:
        route = router(llm=llm).route("Apple, 2026-10-11, how does it stack up?")
    assert route.intent == "I8" and route.intent_source == "model"
    assert route.plan == () and "later phase" in route.notes[-1]
