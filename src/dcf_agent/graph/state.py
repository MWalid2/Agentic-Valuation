"""Graph state: the valuation state plus the session fields around it."""

import operator
from datetime import date
from typing import Annotated, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

from dcf_agent.agents.answer import Answer
from dcf_agent.agents.data_loop import DataReview
from dcf_agent.agents.router import Route
from dcf_agent.agents.wacc import WaccDecision
from dcf_agent.contracts.external import DataBundle
from dcf_agent.contracts.inputs import ValuationRequest
from dcf_agent.contracts.modeling import WACCResult
from dcf_agent.contracts.workflow import ApprovalRecord, Budget, Event

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
    wacc: WACCResult
    wacc_error: str
    wacc_decision: WaccDecision
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
