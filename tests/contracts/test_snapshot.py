"""Snapshot dates, completeness, units, and explicit uncertainty."""

from datetime import date

import pytest
from pydantic import ValidationError

from dcf_agent.contracts import FinancialSnapshot
from dcf_agent.contracts.snapshot import FLOWS, SNAPSHOT_CONCEPTS


def payload():
    start, end, cutoff = date(2025, 1, 1), date(2025, 12, 31), date(2026, 3, 1)
    return {
        "cik": "0000021344",
        "as_of": cutoff,
        "kind": "annual",
        "fiscal_year": 2025,
        "period_start": start,
        "period_end": end,
        "fields": {
            c: {
                "status": "available",
                "unit": "shares" if c == "shares_outstanding" else "USD",
                "basis": "period" if c in FLOWS else "instant",
                "period_start": start if c in FLOWS else None,
                "observed_on": end,
                "amount": {
                    "value": 100.0,
                    "prov": {
                        "source": "sec_xbrl",
                        "ref": "fixture:filing",
                        "period_end": end,
                        "as_of": cutoff,
                        "filed": date(2026, 2, 20),
                        "confidence": "high",
                    },
                },
            }
            for c in SNAPSHOT_CONCEPTS
        },
    }


def test_complete_snapshot_and_json_roundtrip() -> None:
    snapshot = FinancialSnapshot.model_validate(payload())
    assert snapshot.is_complete and snapshot.review_required
    assert FinancialSnapshot.model_validate_json(snapshot.model_dump_json()) == snapshot


@pytest.mark.parametrize("status", ["missing", "ambiguous"])
def test_explicit_unavailable_field(status: str) -> None:
    data = payload()
    data["fields"]["debt"] = {
        "status": status,
        "unit": "USD",
        "basis": "instant",
        "explanation": "Evidence needs review",
    }
    assert not FinancialSnapshot.model_validate(data).is_complete
    data["fields"]["debt"]["amount"] = payload()["fields"]["debt"]["amount"]
    with pytest.raises(ValidationError):
        FinancialSnapshot.model_validate(data)


@pytest.mark.parametrize(
    "problem",
    [
        "omitted",
        "wrong_unit",
        "future",
        "wrong_end",
        "wrong_start",
        "zero_shares",
        "negative_capex",
        "estimate",
        "wrong_basis",
        "approval",
    ],
)
def test_invalid_boundaries(problem: str) -> None:
    data = payload()
    revenue = data["fields"]["revenue"]
    if problem == "omitted":
        del data["fields"]["cash"]
    elif problem == "wrong_unit":
        revenue["unit"] = "shares"
    elif problem == "future":
        revenue["amount"]["prov"]["as_of"] = date(2026, 4, 1)
    elif problem == "wrong_end":
        revenue["observed_on"] = date(2025, 12, 30)
    elif problem == "wrong_start":
        revenue["period_start"] = date(2025, 2, 1)
    elif problem == "zero_shares":
        data["fields"]["shares_outstanding"]["amount"]["value"] = 0.0
    elif problem == "negative_capex":
        data["fields"]["capex"]["amount"]["value"] = -1.0
    elif problem == "estimate":
        revenue["amount"]["prov"]["source"] = "llm_estimate"
    elif problem == "wrong_basis":
        revenue["basis"] = "instant"
    else:
        data["review_required"] = False
    with pytest.raises(ValidationError):
        FinancialSnapshot.model_validate(data)


def test_share_observation_can_follow_statement_end() -> None:
    data = payload()
    shares = data["fields"]["shares_outstanding"]
    shares["observed_on"] = shares["amount"]["prov"]["period_end"] = date(2026, 2, 10)
    assert FinancialSnapshot.model_validate(data).is_complete


def test_quarterly_cash_flows_retain_explicit_ytd_basis() -> None:
    data = payload()
    data.update(kind="quarterly", fiscal_quarter=4, period_start=date(2025, 10, 1))
    for concept in FLOWS:
        field = data["fields"][concept]
        if concept in ("capex", "operating_cash_flow"):
            field["basis"] = "ytd"
        else:
            field["period_start"] = data["period_start"]
    assert FinancialSnapshot.model_validate(data).is_complete
    data["fields"]["revenue"]["basis"] = "ytd"
    with pytest.raises(ValidationError):
        FinancialSnapshot.model_validate(data)
