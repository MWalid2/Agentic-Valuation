"""The valuation graph: Router, Gate 0, then the plan's nodes, checkpointed per step.

router -> gate0 (interrupt; loops until confirmed or stopped) -> plan steps in order
(data, normalize with Gate 1 on its proposed adjustments, wacc, answer; steps not
built yet are recorded and skipped) -> END.
Every step counts against the Budget (human waiting time excluded) and appends
audit Events; Gate 0 decisions are ApprovalRecords tied to a hash of what was shown.
"""

import hashlib
import json
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, ValidationError

from dcf_agent.agents.answer import Answer, render
from dcf_agent.agents.data_loop import DataReview
from dcf_agent.agents.normalizer import AdjustmentProposal, Flag
from dcf_agent.agents.router import INTENTS, PLANS, Route, Router
from dcf_agent.agents.wacc import WaccDecision
from dcf_agent.contracts.accounting import Adjustment, NormalizedFinancials
from dcf_agent.contracts.external import DataBundle
from dcf_agent.contracts.inputs import Intent, ValuationRequest
from dcf_agent.contracts.modeling import ForecastScenarios, WACCResult
from dcf_agent.contracts.provenance import Provenance, Sourced
from dcf_agent.contracts.workflow import ApprovalRecord, Budget, Event
from dcf_agent.engine.assemble import lease_rate, marginal_tax_rate
from dcf_agent.engine.normalize import NormalizationError, Policy, normalize
from dcf_agent.engine.valuation import value_scenarios, write_workbook
from dcf_agent.graph.serde import ContractSerializer
from dcf_agent.graph.state import AdjustmentDecision, GateDecision, GraphState

WIRED = ("data", "normalize", "wacc", "forecast", "value", "excel", "answer")
OUTPUT_INTENT: dict[str, Intent] = {"quick": "I6", "excel": "I9"}
MODES: dict[str, Literal["info", "quick", "full"]] = {
    "question": "info",
    "quick": "quick",
    "excel": "full",
}
Component = Literal["router", "data", "normalizer", "wacc", "forecast", "engine", "system"]
Forecasting = Callable[
    [DataBundle, NormalizedFinancials, WACCResult],
    tuple[ForecastScenarios | None, tuple[str, ...]],
]
# Proposed adjustments, flagged recurring items, and notes.
Proposals = tuple[tuple[AdjustmentProposal, ...], tuple[Flag, ...], tuple[str, ...]]
MARGINAL_TAX = 0.25  # policy when the filings' rate is not read


def default_budget() -> Budget:
    return Budget(max_steps=30, max_tokens=50_000, max_wall_seconds=2700.0)


def digest(value: Any) -> str:
    text = value.model_dump_json() if isinstance(value, BaseModel) else json.dumps(value)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass
class Services:
    """What the nodes call; tests inject fakes, `graph.services` opens the real sources."""

    router: Router
    # The bundle and, when the Data Agent loop ran, how it handled blockers.
    collect: Callable[[ValuationRequest, str | None], tuple[DataBundle, DataReview | None]]
    # The WACC and, when the WACC Agent decided, its decision.
    wacc: Callable[[DataBundle], tuple[WACCResult, WaccDecision | None]]
    answer: Callable[[GraphState], Answer] = render
    # The Normalization Agent's proposals; None: the step is recorded and skipped.
    normalize: Callable[[DataBundle], Proposals] | None = None
    # The Forecast Agent's scenarios and notes; None: the step is recorded and skipped.
    forecast: Forecasting | None = None
    # Where the full model's workbook is written; None: not written.
    models: Path | None = None
    now: Callable[[], datetime] = lambda: datetime.now(UTC)


class Recorder:
    """Builds the events and approvals of one node invocation with unique IDs."""

    def __init__(self, state: GraphState, config: RunnableConfig, services: Services) -> None:
        self.run_id = str(config.get("configurable", {}).get("thread_id", "local"))
        self.count = len(state.get("audit_log", ()))
        self.approvals = len(state.get("approvals", ()))
        self.events: list[Event] = []
        self.now = services.now

    def event(
        self,
        component: Component,
        action: Literal[
            "tool_call", "llm_call", "state_change", "human_decision", "warning", "error"
        ],
        rationale: str,
        *,
        inputs: Any = None,
        outputs: Any = None,
        seconds: float | None = None,
    ) -> Event:
        self.count += 1
        item = Event(
            event_id=f"{self.run_id}:{self.count:04d}",
            run_id=self.run_id,
            timestamp=self.now(),
            component=component,
            action=action,
            input_hash=digest(inputs) if inputs is not None else None,
            output_hash=digest(outputs) if outputs is not None else None,
            rationale=rationale[:500],
            duration_seconds=seconds,
        )
        self.events.append(item)
        return item

    def approval(
        self,
        decision: Literal["approved", "rejected", "edited"],
        shown: Any,
        edits: tuple[str, ...],
        gate: Literal[0, 1] = 0,
        autopilot: bool = False,
    ) -> ApprovalRecord:
        self.approvals += 1
        return ApprovalRecord(
            approval_id=f"{self.run_id}:gate{gate}:{self.approvals}",
            gate=gate,
            decision=decision,
            actor="policy" if autopilot else "user",
            timestamp=self.now(),
            input_hash=digest(shown),
            autopilot=autopilot,
            edit_refs=edits,
        )


def gate_payload(route: Route) -> dict[str, Any]:
    """What Gate 0 shows, as plain data for any interface."""
    company = route.company
    return {
        "gate": 0,
        "status": route.status,
        "ready": route.ready,
        "company": company.model_dump(mode="json") if company else None,
        "candidates": [c.model_dump(mode="json") for c in route.candidates],
        "valuation_date": str(route.valuation_date) if route.valuation_date else None,
        "date_source": route.date_source,
        "date_error": route.date_error,
        "scope": (
            {
                "status": route.scope.status,
                "reasons": list(route.scope.missing.get("scope", ())),
                "notes": list(route.scope.review_notes),
            }
            if route.scope
            else None
        ),
        "question": route.question,
        "intent": route.intent,
        "plan": list(route.plan),
        "notes": list(route.notes),
        "options": ["question", "quick", "excel"],
        "actions": ["confirm", "choose", "redate", "stop"],
    }


def proposal_item(proposal: AdjustmentProposal) -> dict[str, Any]:
    a = proposal.adjustment
    share = a.materiality_pct_ebit
    return {
        "id": a.adjustment_id,
        "item": a.item,
        "period_end": str(a.period_end),
        "direction": a.direction,
        "amount": a.amount.value,
        "pct_ebit": share,
        "confidence": a.confidence,
        "ambiguous": a.ambiguous,
        "rationale": a.rationale,
        "passage": (proposal.passage or "")[:500] or None,
        "evidence": [f"{c.accession} {c.section}" for c in a.evidence],
    }


def adjustments_payload(state: GraphState) -> dict[str, Any]:
    """What Gate 1 shows for normalization, as plain data for any interface."""
    proposals = state.get("proposals", ())
    bundle = state.get("data")
    insights = bundle.filing_insights if bundle is not None else None
    readings = []
    if insights is not None:
        for item in (*insights.captive_finance, *insights.tax_items):
            if item.confidence != "high":
                readings.append(f"{item.item_id}: {item.description} ({item.confidence})")
        for security in insights.dilutive_securities:
            if security.count.prov.confidence != "high":
                readings.append(
                    f"{security.security_id}: {security.count.value:,.0f} "
                    f"({security.count.prov.note or 'filing'}; {security.count.prov.confidence})"
                )
    return {
        "gate": 1,
        "kind": "adjustments",
        "items": [proposal_item(p) for p in proposals if p.review],
        "auto_approved": [proposal_item(p) for p in proposals if not p.review],
        "flags": [f.model_dump(mode="json") for f in state.get("flags", ())],
        "readings": readings,
        "notes": list(state.get("normalizer_notes", ())),
        "actions": ["approve_all", "decide", "stop"],
    }


def edited(adjustment: Adjustment, amount: float, approval_id: str) -> Adjustment:
    """The adjustment with the amount the user set at Gate 1 (the direction stays)."""
    prov = adjustment.amount.prov
    return adjustment.model_copy(
        update={
            "amount": Sourced[float](
                value=amount,
                prov=Provenance(
                    source="user_override",
                    ref=f"gate 1 {approval_id}",
                    as_of=prov.as_of,
                    period_end=prov.period_end,
                    confidence="high",
                    note=f"edited at Gate 1 from {adjustment.amount.value:,.0f}",
                ),
            )
        }
    )


def normalized_update(
    state: GraphState, approved: tuple[Adjustment, ...], rec: "Recorder"
) -> dict[str, Any]:
    """Normalize the history with the approved adjustments and the filing's rates."""
    bundle = state["data"]
    update: dict[str, Any] = {
        "approved_adjustments": approved,
        "adjustments_decided": True,
        "done": state.get("done", ()) + ("normalize",),
    }
    if bundle.raw is None:
        rec.event("normalizer", "warning", "No raw financials: nothing to normalize")
        return update
    tax, _ = marginal_tax_rate(bundle, MARGINAL_TAX)
    policy = Policy(marginal_tax_rate=tax.value, lease_rate=lease_rate(bundle, Policy().lease_rate))
    try:
        normalized, issues = normalize(bundle.raw, policy, approved)
    except NormalizationError as error:
        rec.event("normalizer", "warning", f"Normalization failed: {error}")
        return update
    rec.event(
        "normalizer",
        "state_change",
        f"Normalized {len(normalized.annual)} years with {len(approved)} approved adjustments",
        outputs=normalized,
    )
    return {**update, "normalized": normalized, "normalize_issues": issues}


def next_step(state: GraphState) -> str | None:
    request = state.get("request")
    if request is None:
        return None
    done = state.get("done", ())
    return next((step for step in request.plan if step not in done), None)


def build_graph(
    services: Services, checkpointer: SqliteSaver | None = None
) -> CompiledStateGraph[GraphState, None, GraphState, GraphState]:
    def counted(
        name: str, fn: Callable[[GraphState, Recorder], dict[str, Any]]
    ) -> Callable[[GraphState, RunnableConfig], dict[str, Any]]:
        """Run a node, count a step and its time, attach its events."""

        def run(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
            start = time.monotonic()
            recorder = Recorder(state, config, services)
            update = fn(state, recorder)
            # A node may set the budget itself (a follow-up starts a fresh run).
            budget = update.get("budget") or state.get("budget") or default_budget()
            update["budget"] = budget.model_copy(
                update={
                    "steps_used": budget.steps_used + 1,
                    "wall_seconds_used": budget.wall_seconds_used + time.monotonic() - start,
                }
            )
            if recorder.events:
                update["audit_log"] = update.get("audit_log", ()) + tuple(recorder.events)
            return update

        run.__name__ = name
        return run

    def router_node(state: GraphState, rec: Recorder) -> dict[str, Any]:
        start = time.monotonic()
        route = services.router.route(state["message"])
        rec.event(
            "router",
            "state_change",
            f"{route.status}; date {route.valuation_date or route.date_error}; "
            f"intent {route.intent} by {route.intent_source}; extraction by {route.extraction}",
            inputs=state["message"],
            outputs=route,
            seconds=time.monotonic() - start,
        )
        return {"route": route, "status": "running", "done": ()}

    def reroute(route: Route, ticker: str | None, when: date | None) -> Route:
        company = ticker or (route.company.ticker if route.company else None)
        text = f"{company}, {(when or route.valuation_date or services.router.today).isoformat()}"
        if route.question:
            text += f", {route.question}"
        return services.router.route(text)

    def gate0(state: GraphState, rec: Recorder) -> dict[str, Any]:
        route = state["route"]
        shown = gate_payload(route)
        try:
            decision = GateDecision.model_validate(interrupt(shown))
        except ValidationError as error:
            rec.event("router", "warning", "Gate 0 answer not understood: " + str(error))
            return {}
        rec.event("router", "human_decision", decision.model_dump_json(), inputs=shown)
        if decision.action == "stop":
            return {"status": "stopped", "approvals": (rec.approval("rejected", shown, ()),)}
        if decision.action in ("choose", "redate"):
            if decision.action == "redate" and route.company is None:
                rec.event("router", "warning", "Choose a company before changing the date")
                return {}
            new = reroute(route, decision.ticker, decision.valuation_date)
            rec.event("router", "state_change", f"Rerouted: {new.status}", outputs=new)
            return {"route": new}
        if not route.ready:
            rec.event("router", "warning", "Cannot confirm: company, date or scope unresolved")
            return {}
        question = decision.question or route.question
        output = decision.output or ("question" if question else None)
        if output is None:
            rec.event("router", "warning", "Choose a question, a quick value or the full Excel")
            return {}
        intent: Intent | None
        if output in OUTPUT_INTENT:
            intent = OUTPUT_INTENT[output]
        elif question == route.question and route.intent is not None:
            intent = route.intent
        else:
            intent, source = services.router.classify_intent(question)
            rec.event("router", "state_change", f"Intent {intent} by {source}", inputs=question)
        if intent is None:
            rec.event("router", "warning", "The question's intent is unclear; please rephrase")
            return {}
        plan = PLANS.get(intent) or ("answer",)
        base = route.request(MODES[output])
        request = ValuationRequest.model_validate(
            {
                **base.model_dump(),
                "intent": intent,
                "question": question if question else base.question,
                "plan": plan,
            }
        )
        edits = (f"industry:{decision.industry}",) if decision.industry else ()
        approval = rec.approval("edited" if edits else "approved", shown, edits)
        if intent not in PLANS:
            rec.event("router", "warning", f"{intent} ({INTENTS[intent]}) arrives in a later phase")
        return {
            "request": request,
            "output": output,
            "industry": decision.industry,
            "approvals": (approval,),
        }

    def data_node(state: GraphState, rec: Recorder) -> dict[str, Any]:
        start = time.monotonic()
        request = state["request"]
        try:
            bundle, review = services.collect(request, state.get("industry"))
        except Exception as error:  # a failed source must end the run with an explanation
            rec.event("data", "error", f"Collection failed: {error}", inputs=request)
            return {
                "status": "failed",
                "answer": f"Data collection failed: {error}",
                "done": state.get("done", ()) + ("data",),
            }
        quality = bundle.quality
        rec.event(
            "data",
            "tool_call",
            f"{len(bundle.tool_calls)} tool calls; missing: "
            + (", ".join(quality.missing_fields) or "none"),
            inputs=request,
            outputs=bundle,
            seconds=time.monotonic() - start,
        )
        update: dict[str, Any] = {"data": bundle, "done": state.get("done", ()) + ("data",)}
        if review is not None:
            update["data_review"] = review
            for step in review.steps:
                rec.event(
                    "data",
                    "llm_call" if step.source == "model" else "state_change",
                    f"{step.action} by {step.source}: {step.reason}; "
                    f"blockers {len(step.before)} -> {len(step.after)}; "
                    f"{len(step.changes)} changes recorded",
                    outputs=step,
                )
                if step.rejected:
                    rec.event("data", "warning", "Model choice rejected: " + step.rejected)
            if review.status != "clear":
                rec.event(
                    "data",
                    "warning",
                    f"Data review {review.status}: {len(review.remaining)} blockers remain",
                )
        return update

    def data_review(state: GraphState, rec: Recorder) -> dict[str, Any]:
        review = state["data_review"]
        shown = {
            "gate": 1,
            "question": review.question,
            "blockers": [b.text() for b in review.remaining],
            "steps": [s.model_dump(mode="json") for s in review.steps],
            "actions": ["continue", "stop"],
        }
        answer = interrupt(shown)
        action = answer.get("action") if isinstance(answer, dict) else answer
        if action not in ("continue", "stop"):
            rec.event("data", "warning", f"Gate 1 answer not understood: {answer!r}")
            return {}
        rec.event("data", "human_decision", f"Gate 1: {action}", inputs=shown)
        approval = rec.approval(
            "approved" if action == "continue" else "rejected", shown, (), gate=1
        )
        update: dict[str, Any] = {"data_decision": action, "approvals": (approval,)}
        if action == "stop":
            update["status"] = "stopped"
            update["answer"] = "Stopped at the data review."
        return update

    def wacc_node(state: GraphState, rec: Recorder) -> dict[str, Any]:
        start = time.monotonic()
        done = state.get("done", ()) + ("wacc",)
        bundle = state.get("data")
        if bundle is None:
            rec.event("wacc", "warning", "No data bundle; WACC skipped")
            return {"done": done}
        try:
            result, decision = services.wacc(bundle)
        except Exception as error:  # readiness blocks or missing inputs: explained, not fatal
            rec.event("wacc", "warning", f"WACC not computed: {error}")
            return {"done": done, "wacc_error": str(error)}
        rec.event(
            "wacc",
            "tool_call",
            f"WACC {result.wacc.value:.4f}",
            inputs=bundle.request,
            outputs=result,
            seconds=time.monotonic() - start,
        )
        update: dict[str, Any] = {"wacc": result, "done": done}
        if decision is not None:
            update["wacc_decision"] = decision
            rec.event(
                "wacc",
                "llm_call" if decision.source == "model" else "state_change",
                f"Beta method {decision.method} by {decision.source}: {decision.reason}",
                outputs=decision,
            )
            if decision.flagged:
                rec.event(
                    "wacc",
                    "warning",
                    f"Review: {decision.method} departs from the rule ({decision.rule_method})",
                )
            if decision.rejected:
                rec.event("wacc", "warning", "Model choice rejected: " + decision.rejected)
        return update

    def normalize_node(state: GraphState, rec: Recorder) -> dict[str, Any]:
        start = time.monotonic()
        done = state.get("done", ()) + ("normalize",)
        bundle = state.get("data")
        if bundle is None or services.normalize is None:
            rec.event("normalizer", "warning", "Normalization not available; step skipped")
            return {"done": done}
        try:
            proposals, flags, notes = services.normalize(bundle)
        except Exception as error:  # a failed reading must not end the valuation
            rec.event("normalizer", "warning", f"No adjustments proposed: {error}")
            return normalized_update(state, (), rec)
        rec.event(
            "normalizer",
            "llm_call",
            f"{len(proposals)} adjustments proposed "
            f"({sum(p.review for p in proposals)} for Gate 1); {len(flags)} items flagged",
            outputs=[p.adjustment.model_dump(mode="json") for p in proposals],
            seconds=time.monotonic() - start,
        )
        update: dict[str, Any] = {
            "proposals": proposals,
            "flags": flags,
            "normalizer_notes": notes,
            "adjustments_decided": False,
        }
        if any(p.review for p in proposals) or flags:
            return update  # dispatch sends the session to Gate 1
        # Small, confident adjustments are approved by policy, each on the record.
        approvals = tuple(
            rec.approval("approved", proposal_item(p), (), gate=1, autopilot=True)
            for p in proposals
        )
        return {
            **update,
            **normalized_update(state, tuple(p.adjustment for p in proposals), rec),
            "approvals": approvals,
        }

    def adjustment_review(state: GraphState, rec: Recorder) -> dict[str, Any]:
        shown = adjustments_payload(state)
        try:
            decision = AdjustmentDecision.model_validate(interrupt(shown))
        except ValidationError as error:
            rec.event("normalizer", "warning", "Gate 1 answer not understood: " + str(error))
            return {}
        reviewed = [p for p in state.get("proposals", ()) if p.review]
        ids = {p.adjustment.adjustment_id for p in reviewed}
        if decision.action == "decide":
            named = {*decision.approve, *decision.reject, *decision.edit}
            if named != ids:
                rec.event(
                    "normalizer",
                    "warning",
                    "Give one verdict for every adjustment shown; missing: "
                    + ", ".join(sorted(ids - named))
                    + ("; unknown: " + ", ".join(sorted(named - ids)) if named - ids else ""),
                )
                return {}
        rec.event("normalizer", "human_decision", decision.model_dump_json(), inputs=shown)
        if decision.action == "stop":
            return {
                "status": "stopped",
                "answer": "Stopped at the adjustments review.",
                "approvals": (rec.approval("rejected", shown, (), gate=1),),
            }
        approvals = []
        approved: list[Adjustment] = []
        for proposal in state.get("proposals", ()):
            a, item = proposal.adjustment, proposal_item(proposal)
            if not proposal.review:
                approvals.append(rec.approval("approved", item, (), gate=1, autopilot=True))
                approved.append(a)
            elif decision.action == "approve_all" or a.adjustment_id in decision.approve:
                approvals.append(rec.approval("approved", item, (), gate=1))
                approved.append(a)
            elif a.adjustment_id in decision.edit:
                amount = decision.edit[a.adjustment_id]
                record = rec.approval(
                    "edited", item, (f"{a.adjustment_id}:amount={amount:.0f}",), gate=1
                )
                approvals.append(record)
                approved.append(edited(a, amount, record.approval_id))
            else:
                approvals.append(rec.approval("rejected", item, (), gate=1))
        return {
            **normalized_update(state, tuple(approved), rec),
            "approvals": tuple(approvals),
        }

    def history(state: GraphState) -> NormalizedFinancials | None:
        """The normalized history (with Gate 1 adjustments), or the raw one normalized now."""
        normalized = state.get("normalized")
        bundle = state.get("data")
        if normalized is not None or bundle is None or bundle.raw is None:
            return normalized
        tax, _ = marginal_tax_rate(bundle, MARGINAL_TAX)
        policy = Policy(marginal_tax_rate=tax.value, lease_rate=lease_rate(bundle, 0.05))
        try:
            return normalize(bundle.raw, policy)[0]
        except NormalizationError:
            return None

    def forecast_node(state: GraphState, rec: Recorder) -> dict[str, Any]:
        start = time.monotonic()
        done = state.get("done", ()) + ("forecast",)
        bundle, wacc, normalized = state.get("data"), state.get("wacc"), history(state)
        if services.forecast is None or bundle is None or wacc is None or normalized is None:
            rec.event("forecast", "warning", "Forecast needs data, WACC and history; skipped")
            return {"done": done}
        try:
            scenarios, notes = services.forecast(bundle, normalized, wacc)
        except Exception as error:  # a failed forecast is explained, not fatal
            rec.event("forecast", "warning", f"No forecast: {error}")
            return {"done": done, "forecast_notes": (str(error),)}
        rec.event(
            "forecast",
            "llm_call",
            f"Scenarios {'built' if scenarios else 'not built'}; {len(notes)} notes",
            outputs=scenarios,
            seconds=time.monotonic() - start,
        )
        for note in notes:
            if note.startswith("Gate 2"):
                rec.event("forecast", "warning", note)
        update: dict[str, Any] = {"done": done, "forecast_notes": notes}
        if scenarios is not None:
            update["forecast"] = scenarios
        return update

    def value_node(state: GraphState, rec: Recorder) -> dict[str, Any]:
        start = time.monotonic()
        done = state.get("done", ()) + ("value",)
        bundle, wacc, scenarios = state.get("data"), state.get("wacc"), state.get("forecast")
        if bundle is None or wacc is None or scenarios is None:
            rec.event("engine", "warning", "Valuation needs data, WACC and scenarios; skipped")
            return {"done": done}
        try:
            valuation = value_scenarios(bundle, scenarios, wacc, MARGINAL_TAX)
        except Exception as error:  # an engine guardrail is explained, not fatal
            rec.event("engine", "warning", f"Not valued: {error}")
            return {"done": done}
        rec.event(
            "engine",
            "tool_call",
            f"Weighted value per share {valuation.weighted:,.2f} "
            f"(range {valuation.low:,.2f} to {valuation.high:,.2f})",
            outputs=valuation,
            seconds=time.monotonic() - start,
        )
        return {"done": done, "valuation": valuation}

    def excel_node(state: GraphState, rec: Recorder) -> dict[str, Any]:
        done = state.get("done", ()) + ("excel",)
        bundle, wacc = state.get("data"), state.get("wacc")
        scenarios, valuation = state.get("forecast"), state.get("valuation")
        if services.models is None or None in (bundle, wacc, scenarios, valuation):
            rec.event("engine", "warning", "Workbook not written (no valuation or no folder)")
            return {"done": done}
        assert bundle is not None and wacc is not None and scenarios is not None
        assert valuation is not None
        request = bundle.request
        out = services.models / f"{request.ticker}-{request.valuation_date}.xlsx"
        try:
            write_workbook(bundle, scenarios, wacc, valuation, history(state), MARGINAL_TAX, out)[0]
        except Exception as error:  # the valuation stands without its workbook
            rec.event("engine", "warning", f"Workbook not written: {error}")
            return {"done": done}
        rec.event("engine", "tool_call", f"Workbook written: {out}")
        return {"done": done, "valuation": valuation.model_copy(update={"workbook": str(out)})}

    def unwired(state: GraphState, rec: Recorder) -> dict[str, Any]:
        step = next_step(state)
        assert step is not None
        rec.event("system", "warning", f"Plan step {step} is not available in this phase")
        return {"done": state.get("done", ()) + (step,)}

    def answer_node(state: GraphState, rec: Recorder) -> dict[str, Any]:
        reply = services.answer(state)
        text = reply.text()
        rec.event("system", "state_change", "Answer rendered", outputs=text)
        return {
            "answer": text,
            "reply": reply,
            "status": "done",
            "done": state.get("done", ()) + ("answer",),
        }

    def followup(state: GraphState, rec: Recorder) -> dict[str, Any]:
        """A new question in a finished session: same company and date, reuse what exists."""
        question = state["message"].strip()
        request = state["request"]
        intent, source = services.router.classify_intent(question)
        rec.event(
            "router", "state_change", f"Follow-up intent {intent} by {source}", inputs=question
        )
        if intent is None:
            return {
                "answer": "I could not tell what this question asks; please rephrase it.",
                "status": "done",
            }
        plan = PLANS.get(intent) or ("answer",)
        if intent not in PLANS:
            rec.event("router", "warning", f"{intent} ({INTENTS[intent]}) arrives in a later phase")
        # Steps whose results the session already holds are not run again.
        held = {
            "data": "data",
            "normalize": "adjustments_decided",
            "wacc": "wacc",
            "forecast": "forecast",
            "value": "valuation",
        }
        done = tuple(step for step, field in held.items() if step in plan and state.get(field))
        updated = ValuationRequest.model_validate(
            {**request.model_dump(), "intent": intent, "question": question, "plan": plan}
        )
        budget = state.get("budget") or default_budget()
        return {
            "request": updated,
            "done": done,
            "status": "running",
            "answer": "",
            # Each question is a run with its own step and time budget.
            "budget": budget.model_copy(update={"steps_used": 0, "wall_seconds_used": 0.0}),
        }

    def halt(state: GraphState, rec: Recorder) -> dict[str, Any]:
        budget = state.get("budget") or default_budget()
        rec.event("system", "warning", f"Budget exhausted: {budget.model_dump_json()}")
        return {"status": "budget_exhausted", "answer": "Stopped: the run budget was exhausted."}

    def dispatch(state: GraphState) -> str:
        if state.get("status") in ("stopped", "failed", "done", "budget_exhausted"):
            return END
        budget = state.get("budget")
        if budget is not None and budget.exhausted:
            return "halt"
        if state.get("request") is None:
            return "gate0"
        review = state.get("data_review")
        if review is not None and review.status == "ask_user" and not state.get("data_decision"):
            return "data_review"
        if state.get("adjustments_decided") is False:
            return "adjustment_review"
        step = next_step(state)
        if step is None:
            return END
        return step if step in WIRED else "unwired"

    graph = StateGraph(GraphState)
    for name, fn in (
        ("router", router_node),
        ("followup", followup),
        ("gate0", gate0),
        ("data_review", data_review),
        ("data", data_node),
        ("normalize", normalize_node),
        ("adjustment_review", adjustment_review),
        ("forecast", forecast_node),
        ("value", value_node),
        ("excel", excel_node),
        ("wacc", wacc_node),
        ("answer", answer_node),
        ("unwired", unwired),
        ("halt", halt),
    ):
        graph.add_node(name, counted(name, fn))  # type: ignore[call-overload]

    def entry(state: GraphState) -> str:
        finished = state.get("status") == "done" and state.get("request") is not None
        return "followup" if finished else "router"

    graph.add_conditional_edges(START, entry, ["router", "followup"])
    targets = [
        "gate0",
        "data_review",
        "data",
        "normalize",
        "adjustment_review",
        "wacc",
        "forecast",
        "value",
        "excel",
        "answer",
        "unwired",
        "halt",
        END,
    ]
    for name in (
        "router",
        "followup",
        "gate0",
        "data",
        "data_review",
        "normalize",
        "adjustment_review",
        "wacc",
        "forecast",
        "value",
        "excel",
        "unwired",
    ):
        graph.add_conditional_edges(name, dispatch, targets)
    graph.add_edge("answer", END)
    graph.add_edge("halt", END)
    return graph.compile(checkpointer=checkpointer)


def open_checkpointer(path: Path) -> SqliteSaver:
    """SQLite checkpoints with the validated contract serializer; close `.conn` when done."""
    path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(path, check_same_thread=False), serde=ContractSerializer())


def thread(thread_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": thread_id}}


def start(
    graph: CompiledStateGraph[GraphState, None, GraphState, GraphState],
    thread_id: str,
    message: str,
    budget: Budget | None = None,
) -> dict[str, Any]:
    state: GraphState = {"message": message, "budget": budget or default_budget()}
    return dict(graph.invoke(state, thread(thread_id)))


def resume(
    graph: CompiledStateGraph[GraphState, None, GraphState, GraphState],
    thread_id: str,
    decision: GateDecision | dict[str, Any],
) -> dict[str, Any]:
    payload = decision.model_dump(mode="json") if isinstance(decision, GateDecision) else decision
    return dict(graph.invoke(Command(resume=payload), thread(thread_id)))


def pending_gate(
    graph: CompiledStateGraph[GraphState, None, GraphState, GraphState], thread_id: str
) -> dict[str, Any] | None:
    """The gate payload (Gate 0 or Gate 1) a session is waiting on, if any."""
    snapshot = graph.get_state(thread(thread_id))
    for item in snapshot.interrupts:
        if isinstance(item.value, dict) and item.value.get("gate") in (0, 1):
            return item.value
    return None
