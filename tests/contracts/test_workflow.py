"""Workflow record and restoration boundaries."""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from dcf_agent.contracts import (
    ApprovalRecord,
    Budget,
    CritiqueReport,
    Event,
    ValuationRequest,
    ValuationSnapshot,
    validate_state,
)

NOW = datetime(2026, 10, 12, 12, tzinfo=UTC)


def approval(**overrides: object) -> dict[str, object]:
    return {
        "approval_id": "a1",
        "gate": 2,
        "decision": "approved",
        "actor": "user",
        "timestamp": NOW,
        "input_hash": "a" * 64,
        **overrides,
    }


def snapshot(**overrides: object) -> dict[str, object]:
    return {
        "request": {
            "ticker": "AAPL",
            "cik": "0000320193",
            "company_name": "Apple",
            "valuation_date": date(2026, 10, 12),
            "mode": "quick",
        },
        "budget": {"max_steps": 100, "max_tokens": 10000, "max_wall_seconds": 2700.0},
        **overrides,
    }


def test_snapshot_round_trip_and_typed_state() -> None:
    value = ValuationSnapshot.model_validate(snapshot(artifacts={"memo": Path("out/memo.md")}))
    restored = ValuationSnapshot.model_validate_json(value.model_dump_json())
    assert restored == value
    state = validate_state(restored)
    assert isinstance(state["request"], ValuationRequest)
    assert isinstance(state["budget"], Budget)
    assert "raw" not in state


@pytest.mark.parametrize(
    "overrides",
    [
        {"gate": 4},
        {"timestamp": datetime(2026, 10, 12)},
        {"input_hash": "abc"},
        {"autopilot": True, "decision": "rejected"},
        {"decision": "edited"},
        {"edit_refs": ("edit:1",)},
        {"bounds_override": True},
        {"override_reason": "Reason without override"},
        {"autopilot": True, "bounds_override": True, "override_reason": "Override"},
    ],
)
def test_invalid_approval(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ApprovalRecord.model_validate(approval(**overrides))


def test_edited_approval_and_explicit_override_round_trip() -> None:
    record = ApprovalRecord.model_validate(
        approval(
            decision="edited",
            edit_refs=("state:edited-inputs",),
            bounds_override=True,
            override_reason="Reviewed evidence supports this assumption",
        )
    )
    assert ApprovalRecord.model_validate_json(record.model_dump_json()) == record


@pytest.mark.parametrize(
    "used",
    [
        {"steps_used": 10},
        {"tokens_used": 100},
        {"wall_seconds_used": 30.0},
        {"steps_used": 11},
    ],
)
def test_budget_records_exhaustion_and_overrun(used: dict[str, object]) -> None:
    value = Budget.model_validate(
        {"max_steps": 10, "max_tokens": 100, "max_wall_seconds": 30.0, **used}
    )
    assert value.exhausted is True


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_steps": 0},
        {"tokens_used": -1},
        {"wall_seconds_used": float("inf")},
    ],
)
def test_invalid_budget(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Budget.model_validate(
            {"max_steps": 10, "max_tokens": 100, "max_wall_seconds": 30.0, **overrides}
        )


def issue(**overrides: object) -> dict[str, object]:
    return {
        "issue_id": "i1",
        "severity": "high",
        "description": "Invalid terminal growth",
        "evidence_refs": ("forecast:terminal",),
        "suggested_fix": "Review growth",
        "route_back_to": "forecast",
        **overrides,
    }


def test_unresolved_critique_can_be_recorded_after_cap() -> None:
    report = CritiqueReport.model_validate(
        {"issues": (issue(),), "score": 2.0, "passed": False, "revision_count": 2}
    )
    assert CritiqueReport.model_validate_json(report.model_dump_json()) == report


@pytest.mark.parametrize(
    "overrides",
    [
        {"passed": True},
        {"revision_count": 3},
        {"score": 6.0},
        {"issues": (issue(), issue())},
        {"rubric_scores": {"evidence": 0}},
        {"issues": (issue(evidence_refs=()),)},
    ],
)
def test_invalid_critique(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        CritiqueReport.model_validate(
            {"issues": (issue(),), "score": 2.0, "passed": False, **overrides}
        )


def test_audit_event_round_trip_and_invalid_timestamp() -> None:
    payload = {
        "event_id": "e1",
        "run_id": "r1",
        "timestamp": NOW,
        "component": "data",
        "action": "tool_call",
        "input_hash": "a" * 64,
        "duration_seconds": 0.5,
    }
    record = Event.model_validate(payload)
    assert Event.model_validate_json(record.model_dump_json()) == record
    with pytest.raises(ValidationError):
        Event.model_validate({**payload, "timestamp": datetime(2026, 10, 12)})


def test_state_rejects_duplicate_approval_ids() -> None:
    with pytest.raises(ValidationError, match="unique"):
        ValuationSnapshot.model_validate(snapshot(approvals=(approval(), approval())))


def test_state_rejects_wrong_company_and_future_datasets() -> None:
    for cik, cutoff in (("0000000001", date(2026, 10, 12)), ("0000320193", date(2026, 10, 13))):
        with pytest.raises(ValidationError):
            ValuationSnapshot.model_validate(
                snapshot(filing_insights={"cik": cik, "as_of": cutoff})
            )


def test_state_rejects_unknown_top_level_fields() -> None:
    with pytest.raises(ValidationError):
        validate_state(snapshot(unexpected="value"))


def bundle(valuation_date: date) -> dict[str, object]:
    request = {
        **snapshot()["request"],
        "valuation_date": valuation_date,
        "mode": "info",
        "question": "Collect inputs",
    }
    check = {
        "check_id": "seed:0",
        "field": "seed",
        "status": "passed",
        "severity": "info",
        "message": "Seed from the latest 10-K",
    }
    return {"request": request, "quality": {"as_of": valuation_date, "checks": (check,)}}


def test_state_carries_the_full_data_bundle_for_the_same_request() -> None:
    value = ValuationSnapshot.model_validate(snapshot(data=bundle(date(2026, 10, 12))))
    restored = ValuationSnapshot.model_validate_json(value.model_dump_json())
    assert restored == value
    assert validate_state(restored)["data"].request.mode == "info"
    with pytest.raises(ValidationError, match="Data bundle"):
        ValuationSnapshot.model_validate(snapshot(data=bundle(date(2026, 10, 11))))
