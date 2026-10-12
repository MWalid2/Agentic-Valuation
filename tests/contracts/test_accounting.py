"""Evidence, materiality, normalized history, and explicit data failures."""

from datetime import date

import pytest
from pydantic import ValidationError

from dcf_agent.contracts import (
    Adjustment,
    DataQualityReport,
    DilutiveSecurity,
    FilingInsights,
    NormalizedFinancials,
)

CUTOFF = date(2026, 3, 1)
END = date(2025, 12, 31)
ACCESSION = "0000320193-26-000001"


def citation(**overrides: object) -> dict[str, object]:
    return {"accession": ACCESSION, "filed": date(2026, 2, 15), "section": "Note 5", **overrides}


def amount(value: float = 100.0, **overrides: object) -> dict[str, object]:
    return {
        "value": value,
        "prov": {
            "source": "sec_filing_text",
            "ref": ACCESSION + ":note-5",
            "filed": date(2026, 2, 15),
            "as_of": CUTOFF,
            "period_end": END,
            "confidence": "high",
            **overrides,
        },
    }


def adjustment(**overrides: object) -> dict[str, object]:
    return {
        "adjustment_id": "a1",
        "item": "Restructuring",
        "period_end": END,
        "as_of": CUTOFF,
        "target": "ebit",
        "amount": amount(3.0),
        "direction": "increase",
        "rationale": "Proposed non-recurring treatment",
        "evidence": (citation(),),
        "reference_ebit": amount(),
        "confidence": "high",
        **overrides,
    }


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, False),
        ({"amount": amount(3.01)}, True),
        ({"reference_ebit": amount(-100.0)}, False),
        ({"reference_ebit": amount(0.0)}, True),
        ({"confidence": "low"}, True),
        ({"ambiguous": True}, True),
    ],
)
def test_materiality_review_boundary(overrides: dict[str, object], expected: bool) -> None:
    result = Adjustment.model_validate(adjustment(**overrides))
    assert result.requires_review is expected
    if result.reference_ebit.value == 0:
        assert result.materiality_pct_ebit is None
    assert Adjustment.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "overrides",
    [
        {"amount": amount(-1.0)},
        {"evidence": ()},
        {"rationale": " "},
        {"evidence": (citation(filed=date(2026, 3, 2)),)},
        {"amount": amount(period_end=date(2025, 9, 30))},
        {"amount": amount(as_of=date(2026, 3, 2))},
    ],
)
def test_invalid_adjustment(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Adjustment.model_validate(adjustment(**overrides))


def test_low_confidence_evidence_requires_review_even_with_high_adjustment_confidence() -> None:
    result = Adjustment.model_validate(adjustment(amount=amount(1.0, confidence="low")))
    assert result.requires_review is True


def insights(**overrides: object) -> dict[str, object]:
    return {
        "cik": "0000320193",
        "as_of": CUTOFF,
        "debt_schedule": (
            {
                "instrument_id": "bond1",
                "description": "Senior notes",
                "principal": amount(),
                "citation": citation(),
            },
        ),
        **overrides,
    }


def test_filing_findings_round_trip_and_empty_findings_are_allowed() -> None:
    result = FilingInsights.model_validate(insights())
    assert FilingInsights.model_validate_json(result.model_dump_json()) == result
    assert FilingInsights.model_validate(insights(debt_schedule=())).debt_schedule == ()


@pytest.mark.parametrize(
    "debt",
    [
        {"principal": amount(-1.0)},
        {"principal": amount(ref="wrong-accession")},
        {"citation": citation(filed=date(2026, 2, 16))},
        {"citation": citation(accession="invalid")},
        {"citation": citation(filed=date(2026, 3, 2))},
        {"principal": amount(as_of=date(2026, 3, 2))},
        {"coupon": amount(1.5)},
    ],
)
def test_invalid_filing_findings(debt: dict[str, object]) -> None:
    payload = {
        "instrument_id": "bond1",
        "description": "Notes",
        "principal": amount(),
        "citation": citation(),
        **debt,
    }
    with pytest.raises(ValidationError):
        FilingInsights.model_validate(insights(debt_schedule=(payload,)))


def test_future_lease_payments_are_allowed_without_future_evidence() -> None:
    result = FilingInsights.model_validate(
        insights(
            lease_liabilities=(
                {
                    "lease_type": "operating",
                    "liability": amount(),
                    "citation": citation(),
                    "payments": ({"due_on": date(2028, 1, 1), "payment": amount(20.0)},),
                },
            )
        )
    )
    assert result.lease_liabilities[0].payments[0].due_on > CUTOFF


@pytest.mark.parametrize("kind", ["option", "warrant", "convertible"])
def test_dilution_requires_terms_needed_for_calculation(kind: str) -> None:
    with pytest.raises(ValidationError):
        DilutiveSecurity.model_validate(
            {"security_id": "s1", "kind": kind, "count": amount(), "citation": citation()}
        )


def normalized_period(**overrides: object) -> dict[str, object]:
    return {
        "kind": "annual",
        "period_start": date(2025, 1, 1),
        "period_end": END,
        "revenue": amount(),
        "clean_ebit": amount(-10.0),
        "nopat": amount(-8.0),
        "invested_capital": amount(-5.0),
        "effective_tax_rate": amount(-0.1),
        "marginal_tax_rate": amount(0.25),
        **overrides,
    }


def test_normalized_defaults_and_signed_metrics() -> None:
    result = NormalizedFinancials.model_validate(
        {"cik": "0000320193", "as_of": CUTOFF, "annual": (normalized_period(),)}
    )
    assert result.leases_as_debt is True
    assert result.capitalize_rnd is False
    assert result.sbc_treatment == "expense"
    assert result.annual[0].roic is None
    assert NormalizedFinancials.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "overrides",
    [
        {"annual": ()},
        {"annual": (normalized_period(), normalized_period())},
        {"annual": (normalized_period(kind="ttm"),)},
        {"annual": (normalized_period(marginal_tax_rate=amount(1.1)),)},
        {"annual": (normalized_period(adjustment_ids=("a1", "a1")),)},
        {"annual": (normalized_period(nopat=amount(period_end=date(2025, 9, 30))),)},
        {"as_of": date(2026, 2, 20)},
        {"sbc_treatment": "add_back"},
    ],
)
def test_invalid_normalized_history(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        NormalizedFinancials.model_validate(
            {"cik": "0000320193", "as_of": CUTOFF, "annual": (normalized_period(),), **overrides}
        )


@pytest.mark.parametrize(
    ("status", "severity", "blocked"),
    [
        ("passed", "error", False),
        ("failed", "error", True),
        ("not_checked", "error", True),
        ("failed", "warning", False),
    ],
)
def test_quality_report_blocking_status(status: str, severity: str, blocked: bool) -> None:
    report = DataQualityReport.model_validate(
        {
            "as_of": CUTOFF,
            "checks": (
                {
                    "check_id": "balance",
                    "field": "balance_sheet",
                    "status": status,
                    "severity": severity,
                    "message": "Fixture check",
                },
            ),
        }
    )
    assert report.has_blocking_issues is blocked
    assert DataQualityReport.model_validate_json(report.model_dump_json()) == report


def test_quality_report_rejects_empty_or_duplicate_checks() -> None:
    check = {
        "check_id": "balance",
        "field": "balance_sheet",
        "status": "passed",
        "severity": "error",
        "message": "Balanced",
    }
    for checks in ((), (check, check)):
        with pytest.raises(ValidationError):
            DataQualityReport.model_validate({"as_of": CUTOFF, "checks": checks})
