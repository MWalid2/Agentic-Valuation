"""Validate the evidence boundary independently of data sources or LLMs."""

from datetime import date

import pytest
from pydantic import ValidationError

from dcf_agent.contracts import Provenance, Sourced


def evidence(**overrides: object) -> dict[str, object]:
    return {
        "source": "sec_xbrl",
        "ref": "accession:us-gaap:Revenues",
        "period_end": date(2025, 12, 31),
        "filed": date(2026, 2, 15),
        "as_of": date(2026, 3, 1),
        "confidence": "high",
        **overrides,
    }


def test_sourced_value_round_trips_through_json() -> None:
    revenue = Sourced[float](value=100_000_000.0, prov=Provenance.model_validate(evidence()))
    restored = Sourced[float].model_validate_json(revenue.model_dump_json())
    assert restored == revenue
    assert restored.prov.filed == date(2026, 2, 15)


@pytest.mark.parametrize("field", ["source", "ref", "as_of", "confidence"])
def test_required_evidence_fields(field: str) -> None:
    payload = evidence()
    del payload[field]
    with pytest.raises(ValidationError):
        Provenance.model_validate(payload)


@pytest.mark.parametrize(
    "overrides",
    [
        {"source": "unknown"},
        {"confidence": "certain"},
        {"ref": "   "},
        {"note": " "},
        {"filed": None},
        {"filed": date(2026, 3, 2)},
        {"as_of": "2026-03-01"},
        {"unexpected": "field"},
    ],
)
def test_rejects_invalid_evidence(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Provenance.model_validate(evidence(**overrides))


@pytest.mark.parametrize("source", ["sec_xbrl", "sec_filing_text"])
def test_both_sec_sources_require_a_filing_date(source: str) -> None:
    with pytest.raises(ValidationError, match="SEC sources require a filing date"):
        Provenance.model_validate(evidence(source=source, filed=None))


def test_filing_on_cutoff_is_eligible() -> None:
    result = Provenance.model_validate(evidence(as_of=date(2026, 2, 15)))
    assert result.filed == result.as_of


def test_non_filing_source_can_omit_period_and_filing_date() -> None:
    result = Provenance(
        source="user_override",
        ref="gate-2:terminal-growth",
        as_of=date(2026, 3, 1),
        confidence="medium",
    )
    assert result.filed is None
    assert result.period_end is None


def test_guidance_can_describe_a_future_period() -> None:
    result = Provenance.model_validate(
        evidence(source="sec_filing_text", period_end=date(2026, 12, 31))
    )
    assert result.period_end is not None
    assert result.period_end > result.as_of


@pytest.mark.parametrize("value", ["100", True, float("nan"), float("inf"), -float("inf")])
def test_numeric_wrapper_rejects_coercion_and_non_finite_values(value: object) -> None:
    with pytest.raises(ValidationError):
        Sourced[float].model_validate({"value": value, "prov": evidence()})


def test_wrapper_requires_provenance() -> None:
    with pytest.raises(ValidationError):
        Sourced[float].model_validate({"value": 100.0})


def test_wrapper_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        Sourced[float].model_validate({"value": 100.0, "prov": evidence(), "extra": 1})


def test_generic_wrapper_supports_text() -> None:
    result = Sourced[str](value="hold", prov=Provenance.model_validate(evidence()))
    assert result.value == "hold"


def test_validated_records_cannot_be_reassigned() -> None:
    provenance = Provenance.model_validate(evidence())
    value = Sourced[float](value=100.0, prov=provenance)
    with pytest.raises(ValidationError, match="frozen"):
        provenance.confidence = "low"
    with pytest.raises(ValidationError, match="frozen"):
        value.value = 200.0
