"""Gate 1 in the graph: proposed adjustments are approved, rejected or edited, then applied."""

from datetime import date
from pathlib import Path

from test_graph import NOW, Calls, bundle
from test_router import router
from test_wacc import inputs

from dcf_agent.agents.normalizer import AdjustmentProposal, Flag
from dcf_agent.contracts.accounting import Adjustment, FilingCitation
from dcf_agent.contracts.external import DataBundle
from dcf_agent.contracts.inputs import (
    ConceptStatus,
    FinancialPeriod,
    RawFinancials,
    ValuationRequest,
)
from dcf_agent.contracts.provenance import Provenance, Sourced
from dcf_agent.engine.wacc import compute_wacc
from dcf_agent.graph.workflow import (
    Services,
    build_graph,
    open_checkpointer,
    pending_gate,
    resume,
    start,
)

AS_OF = date(2026, 10, 11)
END = date(2025, 12, 31)
FILED = date(2026, 2, 20)
ACCESSION = "0000320193-26-000001"
EBIT = 1_000e6


def sourced(value: float) -> Sourced[float]:
    return Sourced[float](
        value=value,
        prov=Provenance(
            source="sec_xbrl",
            ref=f"accession {ACCESSION}",
            as_of=AS_OF,
            period_end=END,
            filed=FILED,
            confidence="high",
        ),
    )


def raw(cik: str) -> RawFinancials:
    values = {
        "revenue": 5_000e6,
        "ebit": EBIT,
        "pretax_income": 900e6,
        "income_tax_expense": 189e6,
        "equity": 3_000e6,
        "debt": 1_000e6,
        "cash": 500e6,
    }
    absent = ("minority_interest", "preferred_equity", "marketable_securities")
    period = FinancialPeriod(
        kind="annual",
        fiscal_year=2025,
        period_start=date(2025, 1, 1),
        period_end=END,
        values={k: sourced(v) for k, v in values.items()},  # type: ignore[misc]
        statuses={k: ConceptStatus(status="not_presented") for k in absent},  # type: ignore[misc]
    )
    return RawFinancials(cik=cik, as_of=AS_OF, annual=(period,))


def proposal(name: str, amount: float, direction: str, review: bool) -> AdjustmentProposal:
    return AdjustmentProposal(
        adjustment=Adjustment(
            adjustment_id=f"{name}:annual:{END}",
            item=name,
            period_end=END,
            as_of=AS_OF,
            target="ebit",
            amount=sourced(amount),
            direction=direction,  # type: ignore[arg-type]
            rationale=f"{name} is a one-off.",
            evidence=(FilingCitation(accession=ACCESSION, filed=FILED, section="us-gaap:X"),),
            reference_ebit=sourced(EBIT),
            confidence="medium",
        ),
        passage=f"The {name} was recorded in 2025.",
        review=review,
    )


CHARGE = proposal("Restructuring", 100e6, "increase", True)  # 10% of EBIT: Gate 1
GAIN = proposal("Gain on sale", 50e6, "decrease", True)
SMALL = proposal("Severance", 5e6, "increase", False)  # 0.5%, medium: approved by policy


def services(proposals: tuple[AdjustmentProposal, ...], flags: tuple[Flag, ...] = ()) -> Services:
    calls = Calls()

    def collect(request: ValuationRequest, industry: str | None) -> tuple[DataBundle, None]:
        calls.collect(request, industry)
        return bundle(request).model_copy(update={"raw": raw(request.cik)}), None

    return Services(
        router=router(),
        collect=collect,
        wacc=lambda _: (compute_wacc(inputs()), None),
        normalize=lambda _: (proposals, flags, ("operating income from us-gaap:X",)),
        now=lambda: NOW,
    )


def to_gate1(tmp_path: Path, proposals: tuple[AdjustmentProposal, ...], session: str = "g1"):
    saver = open_checkpointer(tmp_path / "checkpoints.sqlite")
    graph = build_graph(services(proposals), saver)
    start(graph, session, "Apple, 2026-10-11")
    shown = resume(graph, session, {"action": "confirm", "output": "quick"})
    return graph, saver, shown


def clean_ebit(final: dict) -> float:
    return final["normalized"].annual[-1].clean_ebit.value


def test_gate1_shows_the_proposals_then_applies_the_verdicts(tmp_path: Path):
    graph, saver, shown = to_gate1(tmp_path, (CHARGE, GAIN, SMALL))
    gate = shown["__interrupt__"][0].value
    assert gate["gate"] == 1 and gate["kind"] == "adjustments"
    assert [i["id"] for i in gate["items"]] == [
        CHARGE.adjustment.adjustment_id,
        GAIN.adjustment.adjustment_id,
    ]
    assert [i["id"] for i in gate["auto_approved"]] == [SMALL.adjustment.adjustment_id]
    assert gate["items"][0]["passage"] and gate["items"][0]["pct_ebit"] == 0.1
    assert "normalized" not in shown  # nothing is applied before the decision
    final = resume(
        graph,
        "g1",
        {
            "action": "decide",
            "approve": [CHARGE.adjustment.adjustment_id],
            "edit": {GAIN.adjustment.adjustment_id: 30e6},
        },
    )
    gate1 = [a for a in final["approvals"] if a.gate == 1]
    assert [(a.decision, a.autopilot) for a in gate1] == [
        ("approved", False),
        ("edited", False),
        ("approved", True),
    ]
    assert gate1[1].edit_refs == (f"{GAIN.adjustment.adjustment_id}:amount=30000000",)
    applied = {a.item: a for a in final["approved_adjustments"]}
    assert applied["Gain on sale"].amount.value == 30e6
    assert applied["Gain on sale"].amount.prov.source == "user_override"
    # Reported EBIT, plus the restructuring add-back, less the edited gain, plus severance.
    assert clean_ebit(final) == EBIT + 100e6 - 30e6 + 5e6
    assert final["status"] == "done" and "normalize" in final["done"]
    saver.conn.close()


def test_an_incomplete_decision_asks_again(tmp_path: Path):
    graph, saver, _ = to_gate1(tmp_path, (CHARGE, GAIN))
    again = resume(graph, "g1", {"action": "decide", "approve": [CHARGE.adjustment.adjustment_id]})
    assert again["__interrupt__"][0].value["kind"] == "adjustments"
    assert any("missing" in e.rationale for e in again["audit_log"] if e.action == "warning")
    final = resume(
        graph,
        "g1",
        {
            "action": "decide",
            "reject": [CHARGE.adjustment.adjustment_id, GAIN.adjustment.adjustment_id],
        },
    )
    assert final["approved_adjustments"] == () and clean_ebit(final) == EBIT
    saver.conn.close()


def test_stop_at_gate1_ends_the_session(tmp_path: Path):
    graph, saver, _ = to_gate1(tmp_path, (CHARGE,))
    final = resume(graph, "g1", {"action": "stop"})
    assert final["status"] == "stopped" and "normalized" not in final
    assert final["approvals"][-1].gate == 1 and final["approvals"][-1].decision == "rejected"
    saver.conn.close()


def test_without_items_for_review_no_gate_is_shown(tmp_path: Path):
    _, saver, final = to_gate1(tmp_path, (SMALL,))
    assert "__interrupt__" not in final and final["status"] == "done"
    assert [a.autopilot for a in final["approvals"] if a.gate == 1] == [True]
    assert clean_ebit(final) == EBIT + 5e6
    saver.conn.close()


def test_a_session_waiting_at_gate1_survives_a_restart(tmp_path: Path):
    _, saver, shown = to_gate1(tmp_path, (CHARGE, SMALL))
    saver.conn.close()
    saver = open_checkpointer(tmp_path / "checkpoints.sqlite")
    graph = build_graph(services((CHARGE, SMALL)), saver)
    gate = pending_gate(graph, "g1")
    assert gate == shown["__interrupt__"][0].value
    final = resume(graph, "g1", {"action": "approve_all"})
    assert clean_ebit(final) == EBIT + 105e6
    saver.conn.close()


def test_a_follow_up_quotes_the_passages_behind_clean_ebit(tmp_path: Path):
    graph, saver, _ = to_gate1(tmp_path, (CHARGE, SMALL))
    resume(graph, "g1", {"action": "approve_all"})
    final = start(graph, "g1", "where does the clean EBIT come from?")
    # Answered from the session's normalized history: nothing is collected or read again.
    assert final["request"].intent == "I10" and final["done"] == ("data", "answer")
    text = final["answer"]
    assert "where clean EBIT comes from" in text and "Restructuring" in text
    assert '"The Restructuring was recorded in 2025' in text and ACCESSION in text
    saver.conn.close()
