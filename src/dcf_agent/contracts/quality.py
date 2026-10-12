"""Explicit diagnostics for incomplete, fallback, and failed data retrieval."""

from datetime import date
from typing import Literal, Self

from pydantic import Field, model_validator

from dcf_agent.contracts.accounting import Text
from dcf_agent.contracts.inputs import InputContract
from dcf_agent.contracts.provenance import Confidence


class DataQualityCheck(InputContract):
    check_id: Text
    field: Text
    status: Literal["passed", "failed", "not_checked"]
    severity: Literal["info", "warning", "error"]
    message: Text
    evidence_refs: tuple[Text, ...] = ()


class DataFallback(InputContract):
    field: Text
    attempted_source: Text
    selected_source: Text
    reason: Text


class DataQualityReport(InputContract):
    """No checks is not success. Failed or unchecked error checks block a run."""

    as_of: date
    checks: tuple[DataQualityCheck, ...] = Field(min_length=1)
    missing_fields: tuple[Text, ...] = ()
    fallbacks: tuple[DataFallback, ...] = ()
    confidence_by_field: dict[Text, Confidence] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_unique_checks(self) -> Self:
        ids = [check.check_id for check in self.checks]
        if len(set(ids)) != len(ids):
            raise ValueError("Quality check IDs must be unique")
        if len(set(self.missing_fields)) != len(self.missing_fields):
            raise ValueError("Missing field names must be unique")
        return self

    @property
    def has_blocking_issues(self) -> bool:
        return any(check.severity == "error" and check.status != "passed" for check in self.checks)
