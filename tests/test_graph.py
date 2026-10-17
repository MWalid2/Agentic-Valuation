"""Graph skeleton: Gate 0 interrupt and resume, checkpoint restart, budget and audit log."""

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from test_router import router
from test_wacc import inputs

from dcf_agent.contracts.external import DataBundle
from dcf_agent.contracts.inputs import ValuationRequest
from dcf_agent.contracts.quality import DataQualityCheck, DataQualityReport
from dcf_agent.contracts.workflow import Budget
from dcf_agent.engine.wacc import compute_wacc
from dcf_agent.graph.serde import TAG, ContractSerializer
from dcf_agent.graph.workflow import (
    Services,
    build_graph,
    open_checkpointer,
    pending_gate,
    resume,
    start,
    thread,
)

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)


def bundle(request: ValuationRequest) -> DataBundle:
    check = DataQualityCheck(
        check_id="seed:0", field="seed", status="passed", severity="info", message="Seed"
    )
    return DataBundle(
        request=request,
        quality=DataQualityReport(as_of=request.valuation_date, checks=(check,)),
    )


class Calls:
    def __init__(self, fail: bool = False) -> None:
        self.collected: list[tuple[str, str | None]] = []
        self.fail = fail

    def collect(self, request: ValuationRequest, industry: str | None) -> tuple[DataBundle, None]:
        self.collected.append((request.ticker, industry))
        if self.fail:
            raise RuntimeError("SEC unavailable")
        return bundle(request), None


def services(calls: Calls) -> Services:
    return Services(
        router=router(),
        collect=calls.collect,
        wacc=lambda _: (compute_wacc(inputs()), None),
        now=lambda: NOW,
    )


def test_gate0_interrupts_then_resume_runs_the_plan_to_an_answer(tmp_path: Path):
    calls = Calls()
    saver = open_checkpointer(tmp_path / "checkpoints.sqlite")
    graph = build_graph(services(calls), saver)
    first = start(graph, "s1", "Apple, 2026-10-11, what is the cost of capital?")
    gate = first["__interrupt__"][0].value
    assert gate["gate"] == 0 and gate["ready"] and gate["company"]["ticker"] == "AAPL"
    assert gate["intent"] == "I3" and calls.collected == []  # nothing runs before approval
    final = resume(graph, "s1", {"action": "confirm"})
    assert final["status"] == "done" and calls.collected == [("AAPL", None)]
    assert final["request"].plan == ("data", "wacc", "answer")
    assert final["done"] == ("data", "wacc", "answer")
    assert "WACC" in final["answer"]
    (approval,) = final["approvals"]
    assert approval.gate == 0 and approval.decision == "approved"
    components = [e.component for e in final["audit_log"]]
    assert components[:2] == ["router", "router"] and "data" in components
    ids = [e.event_id for e in final["audit_log"]]
    assert len(set(ids)) == len(ids) and all(e.run_id == "s1" for e in final["audit_log"])
    assert final["budget"].steps_used == 5  # router, gate0, data, wacc, answer
    saver.conn.close()


def test_restart_from_checkpoint_resumes_the_waiting_gate(tmp_path: Path):
    path = tmp_path / "checkpoints.sqlite"
    calls = Calls()
    saver = open_checkpointer(path)
    shown = start(build_graph(services(calls), saver), "s2", "KO, 2026-10-11")
    saver.conn.close()
    # A new process: new connection, new graph, same thread ID.
    saver = open_checkpointer(path)
    graph = build_graph(services(calls), saver)
    gate = pending_gate(graph, "s2")
    assert gate is not None and gate["company"]["ticker"] == "KO" and gate["intent"] is None
    assert gate == shown["__interrupt__"][0].value  # exactly what was shown before
    restored = graph.get_state(thread("s2")).values
    assert isinstance(restored["route"].candidates, tuple)  # validated, not just rebuilt
    assert isinstance(restored["budget"], Budget)
    final = resume(graph, "s2", {"action": "confirm", "output": "quick", "industry": "Beverage"})
    assert final["request"].intent == "I6" and final["request"].mode == "quick"
    assert calls.collected == [("KO", "Beverage")]
    assert final["approvals"][0].decision == "edited"
    assert final["approvals"][0].edit_refs == ("industry:Beverage",)
    # Without a forecast service the valuation steps are recorded and skipped, never silent.
    assert {"forecast", "value"} <= set(final["done"]) and "Not available" in final["answer"]
    assert any("Forecast needs" in (e.rationale or "") for e in final["audit_log"])
    assert pending_gate(graph, "s2") is None
    saver.conn.close()


def test_ambiguous_company_is_chosen_then_confirmed_with_a_question(tmp_path: Path):
    calls = Calls()
    graph = build_graph(services(calls), open_checkpointer(tmp_path / "c.sqlite"))
    gate = start(graph, "s3", "Johnson, 2026-10-11")["__interrupt__"][0].value
    assert gate["status"] == "ambiguous" and not gate["ready"]
    # Confirming an unresolved company is refused; the gate asks again.
    again = resume(graph, "s3", {"action": "confirm"})["__interrupt__"][0].value
    assert again["status"] == "ambiguous"
    chosen = resume(graph, "s3", {"action": "choose", "ticker": "JNJ"})["__interrupt__"][0].value
    assert chosen["ready"] and chosen["company"]["ticker"] == "JNJ"
    # Company and date only: the menu needs an output choice before anything runs.
    menu = resume(graph, "s3", {"action": "confirm"})["__interrupt__"][0].value
    assert menu["company"]["ticker"] == "JNJ" and calls.collected == []
    final = resume(
        graph,
        "s3",
        {"action": "confirm", "output": "question", "question": "which beta should we use?"},
    )
    assert final["request"].intent == "I3" and final["status"] == "done"
    warnings = [e for e in final["audit_log"] if e.action == "warning"]
    assert len(warnings) == 2


def test_redate_and_stop(tmp_path: Path):
    graph = build_graph(services(Calls()), open_checkpointer(tmp_path / "c.sqlite"))
    gate = start(graph, "s4", "Apple, 2027-01-15")["__interrupt__"][0].value
    assert gate["date_error"] and not gate["ready"]
    gate = resume(graph, "s4", {"action": "redate", "valuation_date": "2026-10-11"})
    assert gate["__interrupt__"][0].value["valuation_date"] == "2026-10-11"
    final = resume(graph, "s4", {"action": "stop"})
    assert final["status"] == "stopped" and final["approvals"][0].decision == "rejected"
    assert "request" not in final


def test_out_of_scope_company_cannot_be_confirmed(tmp_path: Path):
    blocked = Services(
        router=router(sic="6021"),
        collect=Calls().collect,
        wacc=lambda _: (compute_wacc(inputs()), None),
        now=lambda: NOW,
    )
    graph = build_graph(blocked, open_checkpointer(tmp_path / "c.sqlite"))
    start(graph, "s5", "JPMorgan, 2026-10-11")
    gate = resume(graph, "s5", {"action": "confirm", "output": "quick"})
    assert gate["__interrupt__"][0].value["scope"]["status"] == "blocked"


def test_failed_collection_ends_with_an_explanation(tmp_path: Path):
    graph = build_graph(services(Calls(fail=True)), open_checkpointer(tmp_path / "c.sqlite"))
    start(graph, "s6", "Apple, 2026-10-11, latest revenue?")
    final = resume(graph, "s6", {"action": "confirm"})
    assert final["status"] == "failed" and "SEC unavailable" in final["answer"]
    assert final["audit_log"][-1].action == "error"


def test_budget_exhaustion_halts_before_the_next_step(tmp_path: Path):
    graph = build_graph(services(Calls()), open_checkpointer(tmp_path / "c.sqlite"))
    budget = Budget(max_steps=3, max_tokens=1000, max_wall_seconds=60)
    start(graph, "s7", "Apple, 2026-10-11, latest revenue?", budget)
    final = resume(graph, "s7", {"action": "confirm"})
    assert final["status"] == "budget_exhausted" and final["done"] == ("data",)


def test_invalid_gate_answer_is_recorded_and_asked_again(tmp_path: Path):
    graph = build_graph(services(Calls()), open_checkpointer(tmp_path / "c.sqlite"))
    start(graph, "s8", "Apple, 2026-10-11")
    again = resume(graph, "s8", {"action": "choose"})  # choose without a ticker
    assert again["__interrupt__"][0].value["gate"] == 0
    assert again["audit_log"][-1].action == "warning"


def test_serializer_validates_contracts_and_refuses_unknown_types():
    serde = ContractSerializer()
    request = ValuationRequest(
        ticker="KO",
        cik="0000021344",
        company_name="Coca-Cola",
        valuation_date=date(2026, 10, 11),
        mode="quick",
        plan=("data", "answer"),
    )
    kind, payload = serde.dumps_typed((request, {"note": [1, None]}))
    assert kind == TAG
    restored = serde.loads_typed((kind, payload))
    assert restored == (request, {"note": [1, None]}) and isinstance(restored[0].plan, tuple)
    forged = b'{"__model__": "os:system", "data": {}}'
    with pytest.raises(ValueError, match="allowlist"):
        serde.loads_typed((TAG, forged))
    invalid = payload.replace(b'"0000021344"', b'"0000000000"')
    with pytest.raises(ValueError):
        serde.loads_typed((kind, invalid))


def test_data_review_pauses_at_gate1_and_records_the_decision(tmp_path: Path):
    from dcf_agent.agents.data_loop import Blocker, DataReview, LoopStep

    blocker = Blocker(consumer="wacc", key="ttm", reason="captive_finance_debt (unresolved)")
    review = DataReview(
        status="ask_user",
        steps=(
            LoopStep(
                action="ask_user", source="policy", reason="default", before=(blocker.text(),)
            ),
        ),
        remaining=(blocker,),
        question="Some inputs are blocked; continue or stop?",
    )

    def collect(request, industry):
        return bundle(request), review

    live = Services(
        router=router(),
        collect=collect,
        wacc=lambda _: (compute_wacc(inputs()), None),
        now=lambda: NOW,
    )
    for action, status in (("continue", "done"), ("stop", "stopped")):
        graph = build_graph(live, open_checkpointer(tmp_path / f"{action}.sqlite"))
        start(graph, action, "Apple, 2026-10-11, what WACC?")
        gate = resume(graph, action, {"action": "confirm"})["__interrupt__"][0].value
        assert gate["gate"] == 1 and gate["blockers"] == [blocker.text()]
        assert pending_gate(graph, action) == gate
        final = resume(graph, action, {"action": action})
        assert final["status"] == status
        assert [a.gate for a in final["approvals"]] == [0, 1]
        assert final["approvals"][1].decision == (
            "approved" if action == "continue" else "rejected"
        )
        assert any(
            e.action == "warning" and "Data review" in e.rationale for e in final["audit_log"]
        )
        assert ("wacc" in final.get("done", ())) == (action == "continue")
