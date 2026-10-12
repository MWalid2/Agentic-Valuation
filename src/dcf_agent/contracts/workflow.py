"""Review records, audit metadata, execution limits, and checkpoint boundaries."""

from pathlib import Path
from typing import Annotated, Literal, Required, Self, TypedDict, cast

from pydantic import AwareDatetime, Field, model_validator

from dcf_agent.contracts.accounting import (
    Adjustment,
    FilingInsights,
    NormalizedFinancials,
    Text,
)
from dcf_agent.contracts.inputs import (
    InputContract,
    MacroData,
    MarketData,
    RawFinancials,
    ValuationRequest,
)
from dcf_agent.contracts.modeling import (
    CompsResult,
    ForecastScenarios,
    Scenario,
    ValuationResult,
    WACCResult,
)
from dcf_agent.contracts.quality import DataQualityReport

Gate = Literal[0, 1, 2, 3]
Node = Literal[
    "router", "data", "filings", "normalizer", "forecast", "wacc", "comps", "critic", "reporter"
]
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class CritiqueIssue(InputContract):
    issue_id: Text
    severity: Literal["low", "medium", "high"]
    description: Text
    evidence_refs: tuple[Text, ...] = Field(min_length=1)
    suggested_fix: Text
    route_back_to: Node


class CritiqueReport(InputContract):
    """High-severity issues prevent a clean pass, even after the revision cap."""

    issues: tuple[CritiqueIssue, ...] = ()
    score: float = Field(ge=1, le=5)
    rubric_scores: dict[Text, Annotated[int, Field(ge=1, le=5)]] = Field(default_factory=dict)
    passed: bool
    revision_count: int = Field(default=0, ge=0, le=2)

    @model_validator(mode="after")
    def validate_review(self) -> Self:
        if len({issue.issue_id for issue in self.issues}) != len(self.issues):
            raise ValueError("Critique issue IDs must be unique")
        if self.passed and any(issue.severity == "high" for issue in self.issues):
            raise ValueError("High-severity issues cannot be marked as a clean pass")
        return self


class ApprovalRecord(InputContract):
    """Decision tied to a specific input version; edits reference persisted payloads."""

    approval_id: Text
    gate: Gate
    decision: Literal["approved", "rejected", "edited"]
    actor: Text
    timestamp: AwareDatetime
    input_hash: Hash
    autopilot: bool = False
    edit_refs: tuple[Text, ...] = ()
    warning_refs: tuple[Text, ...] = ()
    bounds_override: bool = False
    override_reason: Text | None = None

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        if self.autopilot and self.decision != "approved":
            raise ValueError("Autopilot records must be auto-approvals")
        if (self.decision == "edited") != bool(self.edit_refs):
            raise ValueError("Only edited decisions require edit references")
        if self.bounds_override != (self.override_reason is not None):
            raise ValueError("Bounds overrides require an explicit reason")
        if self.autopilot and self.bounds_override:
            raise ValueError("Bounds overrides require an explicit human decision")
        return self


class Event(InputContract):
    """Audit references, not raw prompts, secrets, or arbitrary executable payloads."""

    event_id: Text
    run_id: Text
    timestamp: AwareDatetime
    component: Node | Literal["engine", "system"]
    action: Literal["tool_call", "llm_call", "state_change", "human_decision", "warning", "error"]
    input_hash: Hash | None = None
    output_hash: Hash | None = None
    input_ref: Text | None = None
    output_ref: Text | None = None
    rationale: Text | None = None
    duration_seconds: float | None = Field(default=None, ge=0)


class Budget(InputContract):
    """Record overrun rather than rejecting it; the executor enforces stopping."""

    max_steps: int = Field(gt=0)
    max_tokens: int = Field(gt=0)
    max_wall_seconds: float = Field(gt=0)
    steps_used: int = Field(default=0, ge=0)
    tokens_used: int = Field(default=0, ge=0)
    wall_seconds_used: float = Field(default=0, ge=0)

    @property
    def exhausted(self) -> bool:
        return (
            self.steps_used >= self.max_steps
            or self.tokens_used >= self.max_tokens
            or self.wall_seconds_used >= self.max_wall_seconds
        )


class ValuationState(TypedDict, total=False):
    """LangGraph state shape; use validate_state at entry/restoration boundaries."""

    request: Required[ValuationRequest]
    budget: Required[Budget]
    raw: RawFinancials
    market: MarketData
    macro: MacroData
    data_quality: DataQualityReport
    filing_insights: FilingInsights
    normalized: NormalizedFinancials
    adjustments: tuple[Adjustment, ...]
    forecast: ForecastScenarios
    wacc: WACCResult
    valuation: dict[Scenario, ValuationResult]
    comps: CompsResult
    critique: CritiqueReport
    approvals: tuple[ApprovalRecord, ...]
    artifacts: dict[Text, Path]
    audit_log: tuple[Event, ...]


class ValuationSnapshot(InputContract):
    """Runtime-validated, serializable state; stage outputs can be absent."""

    request: ValuationRequest
    budget: Budget
    raw: RawFinancials | None = None
    market: MarketData | None = None
    macro: MacroData | None = None
    data_quality: DataQualityReport | None = None
    filing_insights: FilingInsights | None = None
    normalized: NormalizedFinancials | None = None
    adjustments: tuple[Adjustment, ...] = ()
    forecast: ForecastScenarios | None = None
    wacc: WACCResult | None = None
    valuation: dict[Scenario, ValuationResult] = Field(default_factory=dict)
    comps: CompsResult | None = None
    critique: CritiqueReport | None = None
    approvals: tuple[ApprovalRecord, ...] = ()
    artifacts: dict[Text, Path] = Field(default_factory=dict)
    audit_log: tuple[Event, ...] = ()

    @model_validator(mode="after")
    def validate_state_consistency(self) -> Self:
        for dataset in (self.raw, self.filing_insights, self.normalized):
            if dataset is not None and dataset.cik != self.request.cik:
                raise ValueError("Dataset company must match the request")
        if self.market is not None and self.market.ticker != self.request.ticker:
            raise ValueError("Market ticker must match the request")
        datasets = (
            self.raw,
            self.market,
            self.macro,
            self.data_quality,
            self.filing_insights,
            self.normalized,
            self.wacc,
            self.comps,
            *self.adjustments,
            *self.valuation.values(),
        )
        if any(item is not None and item.as_of > self.request.valuation_date for item in datasets):
            raise ValueError("Dataset cutoff cannot follow valuation date")
        if self.forecast is not None:
            if any(item.as_of != self.request.valuation_date for item in self.forecast.scenarios):
                raise ValueError("Forecast cutoff must equal valuation date")
        for key, result in self.valuation.items():
            if result.scenario != key or result.as_of != self.request.valuation_date:
                raise ValueError("Valuation scenario and date must match state")
            if (
                self.forecast is not None
                and result.year_ends != self.forecast.scenarios[0].year_ends
            ):
                raise ValueError("Valuation horizon must match forecast horizon")
        if self.forecast is not None and self.wacc is not None:
            if any(
                item.terminal_growth.value >= self.wacc.terminal_wacc.value
                for item in self.forecast.scenarios
            ):
                raise ValueError("Terminal WACC must exceed terminal growth")
        for records, attr in (
            (self.adjustments, "adjustment_id"),
            (self.approvals, "approval_id"),
            (self.audit_log, "event_id"),
        ):
            ids = [getattr(record, attr) for record in records]
            if len(set(ids)) != len(ids):
                raise ValueError("State record IDs must be unique")
        if self.normalized is not None:
            known = {item.adjustment_id for item in self.adjustments}
            periods = (
                *self.normalized.annual,
                *((self.normalized.ttm,) if self.normalized.ttm is not None else ()),
            )
            if any(not set(period.adjustment_ids) <= known for period in periods):
                raise ValueError("Normalized history references unknown adjustments")
        return self

    def to_state(self) -> ValuationState:
        """Preserve validated models rather than dumping them as nested dicts."""
        payload = {
            name: getattr(self, name)
            for name in type(self).model_fields
            if getattr(self, name) is not None
        }
        return cast(ValuationState, payload)


def validate_state(payload: object) -> ValuationState:
    """Validate a Python state payload; JSON restoration uses the snapshot model."""
    return ValuationSnapshot.model_validate(payload).to_state()
