"""Forecast and result validation without an LLM or valuation engine."""

from datetime import date

import pytest
from pydantic import ValidationError

from dcf_agent.contracts import (
    CompsResult,
    ForecastAssumptions,
    ForecastScenarios,
    ValuationResult,
    WACCResult,
)

CUTOFF = date(2026, 3, 1)
ENDS = (date(2026, 12, 31), date(2027, 12, 31))


def sourced(value: float, end: date | None = None) -> dict[str, object]:
    return {
        "value": value,
        "prov": {
            "source": "derived",
            "ref": "fixture:calculation",
            "as_of": CUTOFF,
            "period_end": end,
            "confidence": "high",
        },
    }


def forecast(**overrides: object) -> dict[str, object]:
    return {
        "as_of": CUTOFF,
        "scenario": "base",
        "probability": 0.5,
        "year_ends": ENDS,
        "revenue_growth": tuple(sourced(0.05, end) for end in ENDS),
        "operating_margin": tuple(sourced(0.2, end) for end in ENDS),
        "sales_to_capital": tuple(sourced(2.0, end) for end in ENDS),
        "tax_rate": tuple(sourced(0.25, end) for end in ENDS),
        "terminal_growth": sourced(0.02),
        "terminal_roic": sourced(0.1),
        "marginal_tax_rate": sourced(0.25),
        "narrative": "Stable business",
        "rationale": {"growth": "Historical trend"},
        **overrides,
    }


def test_forecast_scenarios_round_trip() -> None:
    result = ForecastScenarios.model_validate(
        {
            "scenarios": (
                forecast(scenario="bear", probability=0.25),
                forecast(),
                forecast(scenario="bull", probability=0.25),
            )
        }
    )
    assert ForecastScenarios.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "overrides",
    [
        {"probability": 1.1},
        {"year_ends": tuple(reversed(ENDS))},
        {"revenue_growth": ()},
        {"sales_to_capital": tuple(sourced(0.0, end) for end in ENDS)},
        {"revenue_growth": tuple(sourced(-1.0, end) for end in ENDS)},
        {"tax_rate": tuple(sourced(0.2, end) for end in ENDS)},
        {"operating_margin": tuple(sourced(0.2) for _ in ENDS)},
        {"as_of": date(2026, 2, 1)},
        {"terminal_roic": sourced(0.0)},
    ],
)
def test_invalid_forecast(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ForecastAssumptions.model_validate(forecast(**overrides))


@pytest.mark.parametrize(
    "third",
    [
        forecast(scenario="base", probability=0.25),
        forecast(scenario="bull", probability=0.4),
        forecast(scenario="bull", probability=0.25, as_of=date(2026, 3, 2)),
    ],
)
def test_invalid_scenario_collection(third: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ForecastScenarios.model_validate(
            {
                "scenarios": (
                    forecast(scenario="bear", probability=0.25),
                    forecast(),
                    third,
                )
            }
        )


def wacc(**overrides: object) -> dict[str, object]:
    return {
        "as_of": CUTOFF,
        "rf": sourced(0.04),
        "erp": sourced(0.05),
        "crp": sourced(0.0),
        "unlevered_beta": sourced(1.0),
        "relevered_beta": sourced(1.1),
        "bottom_up_beta": sourced(1.1),
        "beta_method": "bottom_up",
        "cost_equity": sourced(0.095),
        "pretax_kd": sourced(0.05),
        "tax_rate": sourced(0.25),
        "aftertax_kd": sourced(0.0375),
        "weight_equity": sourced(0.8),
        "weight_debt": sourced(0.2),
        "wacc": sourced(0.0835),
        "terminal_wacc": sourced(0.08),
        "wacc_band": (0.075, 0.095),
        "method_notes": ("Bottom-up default",),
        **overrides,
    }


def test_wacc_round_trip() -> None:
    result = WACCResult.model_validate(wacc())
    assert WACCResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "overrides",
    [
        {"weight_debt": sourced(0.4)},
        {"beta_method": "average"},
        {"wacc_band": (0.09, 0.08)},
        {"wacc_band": (0.01, 0.02)},
        {"terminal_wacc": sourced(-1.0)},
        {"erp": sourced(-0.01)},
        {"as_of": date(2026, 2, 1)},
        {"wacc_band": (0.01, float("inf"))},
    ],
)
def test_invalid_wacc(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        WACCResult.model_validate(wacc(**overrides))


def valuation(**overrides: object) -> dict[str, object]:
    return {
        "as_of": CUTOFF,
        "scenario": "base",
        "calculation_ref": "run:1",
        "input_hash": "abc",
        "year_ends": ENDS,
        "fcff_by_year": (10.0, 12.0),
        "discount_times": (0.4, 1.3),
        "discount_factors": (0.97, 0.90),
        "pv_explicit": 20.5,
        "terminal_value": 100.0,
        "terminal_discount_time": 1.3,
        "pv_terminal": 90.0,
        "enterprise_value": 110.5,
        "bridge": {"cash": sourced(10.0), "debt": sourced(-20.0)},
        "equity_value": 100.5,
        "diluted_shares": 10.0,
        "value_per_share": 10.05,
        "tv_share_of_ev": 90.0 / 110.5,
        **overrides,
    }


def test_valuation_round_trip_and_signed_bridge() -> None:
    result = ValuationResult.model_validate(valuation())
    assert result.bridge["debt"].value == -20.0
    assert ValuationResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "overrides",
    [
        {"fcff_by_year": (10.0,)},
        {"discount_factors": (0.0, 0.9)},
        {"discount_times": (1.0, 0.5)},
        {"diluted_shares": 0.0},
        {"terminal_discount_time": 0.1},
        {"enterprise_value": 0.0},
        {"value_per_share": float("nan")},
        {"year_ends": tuple(reversed(ENDS))},
    ],
)
def test_invalid_valuation(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ValuationResult.model_validate(valuation(**overrides))


def comps(**overrides: object) -> dict[str, object]:
    return {
        "as_of": CUTOFF,
        "calculation_ref": "comps:1",
        "peers": (
            {
                "ticker": "AAPL",
                "cik": "0000320193",
                "as_of": CUTOFF,
                "selection_rationale": "Similar business",
                "ev_sales": sourced(3.0),
            },
        ),
        "summaries": (
            {
                "metric": "ev_sales",
                "sample_size": 1,
                "first_quartile": 3.0,
                "median": 3.0,
                "third_quartile": 3.0,
            },
        ),
        "warnings": ("Small sample",),
        "implied_value_range": (5.0, 10.0),
        **overrides,
    }


def test_comps_round_trip_and_unavailable_comparison() -> None:
    result = CompsResult.model_validate(comps())
    assert CompsResult.model_validate_json(result.model_dump_json()) == result
    partial = CompsResult.model_validate(comps(peers=(), summaries=(), implied_value_range=None))
    assert partial.implied_value_range is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"warnings": ()},
        {"implied_value_range": (10.0, 5.0)},
        {"summaries": ()},
        {"as_of": date(2026, 3, 2)},
        {
            "summaries": (
                {
                    "metric": "ev_sales",
                    "sample_size": 2,
                    "first_quartile": 2.0,
                    "median": 3.0,
                    "third_quartile": 4.0,
                },
            )
        },
        {
            "summaries": (
                {
                    "metric": "ev_sales",
                    "sample_size": 1,
                    "first_quartile": 4.0,
                    "median": 3.0,
                    "third_quartile": 2.0,
                },
            )
        },
    ],
)
def test_invalid_comps(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CompsResult.model_validate(comps(**overrides))
