"""Input boundary tests, including point-in-time leakage and partial history."""

from datetime import date

import pytest
from pydantic import ValidationError

from dcf_agent.contracts import (
    FinancialPeriod,
    MacroData,
    MacroObservation,
    MarketData,
    PriceObservation,
    Provenance,
    RawFinancials,
    Sourced,
    ValuationRequest,
)

CUTOFF = date(2026, 3, 1)
END = date(2025, 12, 31)


def sourced(value: float = 100.0, **overrides: object) -> dict[str, object]:
    return {
        "value": value,
        "prov": {
            "source": "sec_xbrl",
            "ref": "fixture:revenue",
            "period_end": END,
            "filed": date(2026, 2, 15),
            "as_of": CUTOFF,
            "confidence": "high",
            **overrides,
        },
    }


def period(**overrides: object) -> dict[str, object]:
    return {
        "kind": "annual",
        "fiscal_year": 2025,
        "period_start": date(2025, 1, 1),
        "period_end": END,
        "values": {"revenue": sourced()},
        **overrides,
    }


def request(**overrides: object) -> dict[str, object]:
    return {
        "ticker": "AAPL",
        "cik": "0000320193",
        "company_name": "Apple Inc.",
        "valuation_date": CUTOFF,
        "mode": "quick",
        **overrides,
    }


def test_request_json_round_trip() -> None:
    value = ValuationRequest.model_validate(request(plan=("data", "wacc")))
    assert ValuationRequest.model_validate_json(value.model_dump_json()) == value
    assert value.autopilot is False


@pytest.mark.parametrize(
    "overrides",
    [
        {"cik": "320193"},
        {"cik": "0000000000"},
        {"ticker": "aapl"},
        {"company_name": " "},
        {"mode": "info"},
        {"intent": "I12"},
        {"mode": "other"},
        {"plan": (" ",)},
        {"autopilot": "false"},
    ],
)
def test_request_rejects_invalid_intake(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ValuationRequest.model_validate(request(**overrides))


def test_info_request_accepts_a_question_and_dual_class_ticker() -> None:
    result = ValuationRequest.model_validate(
        request(ticker="BRK.B", mode="info", intent="I3", question="What is WACC?")
    )
    assert result.intent == "I3"


def test_partial_history_preserves_absent_fields_and_round_trips() -> None:
    raw = RawFinancials.model_validate(
        {"cik": "0000320193", "as_of": CUTOFF, "annual": (period(),)}
    )
    assert "ebit" not in raw.annual[0].values
    assert RawFinancials.model_validate_json(raw.model_dump_json()) == raw


@pytest.mark.parametrize(
    "overrides",
    [
        {"period_start": date(2026, 1, 1)},
        {"fiscal_quarter": 1},
        {"kind": "quarterly"},
        {"kind": "quarterly", "fiscal_quarter": 5},
        {"values": {}},
        {"values": {"invented_concept": sourced()}},
        {"values": {"revenue": sourced(period_end=date(2025, 9, 30))}},
    ],
)
def test_invalid_financial_period(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        FinancialPeriod.model_validate(period(**overrides))


def test_non_december_fiscal_period_and_signed_loss() -> None:
    end = date(2025, 9, 30)
    result = FinancialPeriod.model_validate(
        period(
            period_start=date(2024, 10, 1),
            period_end=end,
            values={"ebit": sourced(-10.0, period_end=end)},
        )
    )
    assert result.values["ebit"].value == -10.0


@pytest.mark.parametrize(
    "overrides",
    [
        {"annual": ()},
        {"annual": (period(), period())},
        {"annual": (period(kind="quarterly", fiscal_quarter=4),)},
        {"as_of": date(2025, 12, 30)},
        {"as_of": date(2026, 2, 20)},
        {"currency": "EUR"},
    ],
)
def test_invalid_raw_history(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RawFinancials.model_validate(
            {"cik": "0000320193", "as_of": CUTOFF, "annual": (period(),), **overrides}
        )


def price(on: date = date(2026, 2, 27), value: float = 100.0) -> dict[str, object]:
    return {
        "observed_on": on,
        "price": sourced(value, source="yfinance", filed=None, period_end=None),
    }


def market(**overrides: object) -> dict[str, object]:
    return {
        "ticker": "AAPL",
        "as_of": CUTOFF,
        "close": price(),
        "shares_outstanding": sourced(1000.0),
        "shares_observed_on": END,
        **overrides,
    }


def test_market_accepts_prior_trading_day_and_round_trips() -> None:
    result = MarketData.model_validate(market())
    assert result.close.observed_on < result.as_of
    assert MarketData.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "overrides",
    [
        {"close": price(date(2026, 3, 2))},
        {"close": price(value=0.0)},
        {"shares_outstanding": sourced(-1.0)},
        {"shares_observed_on": date(2026, 3, 2)},
        {"price_history": (price(), price())},
        {"price_history": (price(), price(date(2026, 2, 26)))},
        {"as_of": date(2026, 2, 20)},
    ],
)
def test_invalid_market_data(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        MarketData.model_validate(market(**overrides))


def macro(rate: float = 0.04, **overrides: object) -> dict[str, object]:
    return {
        "observed_on": date(2026, 2, 27),
        "available_on": date(2026, 2, 28),
        "rate": sourced(rate, source="fred", filed=None, period_end=None),
        **overrides,
    }


def test_macro_accepts_negative_risk_free_rate_and_zero_country_premium() -> None:
    data = MacroData.model_validate(
        {
            "as_of": CUTOFF,
            "risk_free_rate": macro(-0.01),
            "equity_risk_premium": macro(0.05),
            "country_risk_premium": macro(0.0),
        }
    )
    assert data.risk_free_rate.rate.value == -0.01
    assert MacroData.model_validate_json(data.model_dump_json()) == data


@pytest.mark.parametrize(
    "overrides",
    [{"available_on": date(2026, 3, 2)}, {"observed_on": date(2026, 3, 1)}],
)
def test_macro_rejects_unavailable_versions(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        MacroObservation.model_validate(macro(**overrides))


@pytest.mark.parametrize("field", ["equity_risk_premium", "country_risk_premium", "credit_spread"])
def test_macro_rejects_negative_premiums(field: str) -> None:
    payload = {
        "as_of": CUTOFF,
        "risk_free_rate": macro(),
        "equity_risk_premium": macro(),
        "country_risk_premium": macro(0.0),
        field: macro(-0.01),
    }
    with pytest.raises(ValidationError):
        MacroData.model_validate(payload)


def test_macro_rejects_dataset_cutoff_before_evidence() -> None:
    with pytest.raises(ValidationError):
        MacroData.model_validate(
            {
                "as_of": date(2026, 2, 20),
                "risk_free_rate": macro(),
                "equity_risk_premium": macro(),
                "country_risk_premium": macro(),
            }
        )


def test_price_observation_requires_finite_numeric_value() -> None:
    with pytest.raises(ValidationError):
        PriceObservation.model_validate(price(value=float("inf")))


def test_macro_can_be_constructed_from_typed_python_models() -> None:
    provenance = Provenance(source="fred", ref="DGS10", as_of=CUTOFF, confidence="high")
    result = MacroObservation(
        observed_on=date(2026, 2, 27),
        available_on=date(2026, 2, 28),
        rate=Sourced[float](value=0.04, prov=provenance),
    )
    assert result.rate.value == 0.04
