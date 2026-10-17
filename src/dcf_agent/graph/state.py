"""Graph state: the valuation state plus the session fields around it."""

import operator
from datetime import date
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dcf_agent.agents.answer import Answer
from dcf_agent.agents.data_loop import DataReview
from dcf_agent.agents.normalizer import AdjustmentProposal, Flag
from dcf_agent.agents.router import Route
from dcf_agent.agents.wacc import WaccDecision
from dcf_agent.contracts.accounting import Adjustment, NormalizedFinancials
from dcf_agent.contracts.external import DataBundle
from dcf_agent.contracts.inputs import ValuationRequest
from dcf_agent.contracts.modeling import ForecastScenarios, WACCResult
from dcf_agent.contracts.workflow import ApprovalRecord, Budget, Event
from dcf_agent.engine.valuation import Valuation

Status = Literal["running", "stopped", "done", "budget_exhausted", "failed"]
Output = Literal["question", "quick", "excel"]


class GraphState(TypedDict, total=False):
    """Fields are written by one node each; approvals and the audit log only grow."""

    message: str
    route: Route
    request: ValuationRequest
    output: Output
    industry: str | None
    budget: Budget
    data: DataBundle
    data_review: DataReview
    data_decision: Literal["continue", "stop"]
    # Normalization: proposals, Gate 1 approvals, and the normalized history.
    proposals: tuple[AdjustmentProposal, ...]
    flags: tuple[Flag, ...]
    normalizer_notes: tuple[str, ...]
    adjustments_decided: bool
    approved_adjustments: tuple[Adjustment, ...]
    normalized: NormalizedFinancials
    normalize_issues: dict[str, str]
    wacc: WACCResult
    wacc_error: str
    wacc_decision: WaccDecision
    # Forecast (bear/base/bull), its notes and rejected model picks, and the valuation.
    forecast: ForecastScenarios
    forecast_notes: tuple[str, ...]
    valuation: Valuation
    answer: str
    reply: Answer
    status: Status
    done: tuple[str, ...]
    approvals: Annotated[tuple[ApprovalRecord, ...], operator.add]
    audit_log: Annotated[tuple[Event, ...], operator.add]


class GateDecision(BaseModel):
    """The user's answer at Gate 0.

    confirm: accept the shown company and date (optionally with a question, an
    output choice or an industry override); choose: pick a candidate ticker;
    redate: give another date; stop: end the session.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    action: Literal["confirm", "choose", "redate", "stop"]
    ticker: str | None = Field(default=None, pattern=r"^[A-Za-z0-9.\-]+$")
    valuation_date: date | None = None
    output: Output | None = None
    question: str | None = Field(default=None, min_length=1)
    industry: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_action(self) -> "GateDecision":
        if self.action == "choose" and not self.ticker:
            raise ValueError("Choosing a company needs its ticker")
        if self.action == "redate" and self.valuation_date is None:
            raise ValueError("A new date is required")
        if self.output == "question" and self.question is not None and not self.question.strip():
            raise ValueError("Blank question")
        return self


class AdjustmentDecision(BaseModel):
    """The user's answer at Gate 1 on proposed adjustments.

    approve_all: accept every proposal shown; decide: one verdict per proposal
    (approve, reject, or edit with a new amount in USD); stop: end the session.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    action: Literal["approve_all", "decide", "stop"]
    approve: tuple[str, ...] = ()
    reject: tuple[str, ...] = ()
    edit: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_verdicts(self) -> "AdjustmentDecision":
        named = [*self.approve, *self.reject, *self.edit]
        if len(named) != len(set(named)):
            raise ValueError("Each adjustment takes one verdict")
        if self.action != "decide" and named:
            raise ValueError("Verdicts are given with the decide action")
        if any(amount < 0 for amount in self.edit.values()):
            raise ValueError("An edited amount is a non-negative size; the direction stays")
        return self
