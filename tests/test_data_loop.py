"""Data Agent loop: bounded actions on blockers, guarded choices, no silent changes."""

import json
from pathlib import Path
from types import SimpleNamespace

from test_answer import AS_OF, period
from test_llm import config as llm_config

from dcf_agent.agents import data_loop as loop_module
from dcf_agent.agents.data_loop import DataLoop, blockers, value_changes
from dcf_agent.agents.lines import MappingStore
from dcf_agent.contracts.external import DataBundle, InputReadiness
from dcf_agent.contracts.inputs import RawFinancials, ValuationRequest
from dcf_agent.contracts.quality import DataQualityCheck, DataQualityReport
from dcf_agent.llm import LocalLLM

REQUEST = ValuationRequest(
    ticker="EXM",
    cik="0000000001",
    company_name="Example",
    valuation_date=AS_OF,
    mode="info",
    question="q",
)
WACC_BLOCK = InputReadiness(
    status="blocked",
    missing={"ttm:2026-06-30": ("captive_finance_debt (unresolved)",)},
    review_notes=(),
)
FINE = InputReadiness(status="needs_review", review_notes=())


def make(
    *,
    wacc: InputReadiness = FINE,
    normalization: InputReadiness = FINE,
    failed: tuple[str, ...] = (),
    revenue_scale: float = 1.0,
    drop: str | None = None,
) -> DataBundle:
    checks = [
        DataQualityCheck(
            check_id=f"{f}:0", field=f, status="failed", severity="error", message=f"{f} failed"
        )
        for f in failed
    ] or [
        DataQualityCheck(
            check_id="seed:0", field="seed", status="passed", severity="info", message="ok"
        )
    ]
    raw = RawFinancials(
        cik=REQUEST.cik,
        as_of=AS_OF,
        annual=(
            period(
                "annual",
                2025,
                AS_OF.replace(year=2025, month=1, day=1),
                AS_OF.replace(year=2025, month=12, day=31),
                revenue_scale,
            ),
        ),
    )
    if drop is not None:
        year = raw.annual[0]
        values = {k: v for k, v in year.values.items() if k != drop}
        raw = raw.model_copy(update={"annual": (year.model_copy(update={"values": values}),)})
    return DataBundle(
        request=REQUEST,
        raw=raw,
        readiness={"wacc": wacc, "normalization": normalization, "scope": FINE},
        quality=DataQualityReport(as_of=AS_OF, checks=tuple(checks), missing_fields=failed),
    )


class Collector:
    def __init__(self, *bundles: DataBundle) -> None:
        self.bundles, self.calls = list(bundles), 0

    def __call__(self) -> DataBundle:
        self.calls += 1
        return self.bundles[min(self.calls, len(self.bundles)) - 1]


def model(tmp_path: Path, *replies: dict) -> LocalLLM:
    return LocalLLM(llm_config(tmp_path), mock_responses=[json.dumps(r) for r in replies])


def test_clear_data_runs_collection_once():
    collect = Collector(make())
    bundle, review = DataLoop(None, None, online=True).run(collect, ("data", "wacc", "answer"))
    assert review.status == "clear" and review.steps == () and collect.calls == 1


def test_blockers_outside_the_plan_do_not_count():
    blocked = InputReadiness(
        status="blocked", missing={"annual:2025-12-31": ("payables (missing)",)}, review_notes=()
    )
    collect = Collector(make(normalization=blocked))
    _, review = DataLoop(None, None, online=True).run(collect, ("data", "wacc", "answer"))
    assert review.status == "clear"  # normalization is not in this plan
    _, review = DataLoop(None, None, online=True).run(
        Collector(make(normalization=blocked)), ("data", "normalize")
    )
    assert review.status == "ask_user"


def test_without_a_model_a_blocker_asks_the_user():
    collect = Collector(make(wacc=WACC_BLOCK))
    _, review = DataLoop(None, None, online=False).run(collect, ("data", "wacc", "answer"))
    assert review.status == "ask_user" and collect.calls == 1
    (step,) = review.steps
    assert step.action == "ask_user" and step.source == "policy"
    assert "captive_finance_debt (unresolved)" in review.question
    assert review.remaining[0].consumer == "wacc"


def test_failed_prices_are_retried_and_the_change_is_recorded():
    collect = Collector(make(failed=("prices", "market")), make())
    bundle, review = DataLoop(None, None, online=True).run(collect, ("data", "wacc"))
    assert review.status == "clear" and collect.calls == 2
    (step,) = review.steps
    assert step.action == "retry_prices" and step.before and step.after == ()


def test_offline_has_no_network_actions():
    found = blockers(make(failed=("prices",)), ("wacc",))
    options = DataLoop(None, None, online=False).applicable(make(), found, None, set())
    assert options == ["accept_with_flag", "ask_user"]


def test_a_choice_that_does_not_apply_is_rejected(tmp_path):
    with model(tmp_path, {"action": "classify_unknown_lines", "reason": "Lines unknown."}) as llm:
        _, review = DataLoop(llm, None, online=False).run(
            Collector(make(wacc=WACC_BLOCK)), ("wacc",)
        )
    (step,) = review.steps
    assert step.source == "policy" and step.action == "ask_user"
    assert "does not apply" in step.rejected


def test_refresh_sets_the_switch_only_for_the_recollection(tmp_path):
    sec = SimpleNamespace(refresh_mutable=False)
    sec.start_refresh = lambda: setattr(sec, "refresh_mutable", True)
    sec.stop_refresh = lambda: setattr(sec, "refresh_mutable", False)
    seen = []

    def collect() -> DataBundle:
        seen.append(sec.refresh_mutable)
        return make(wacc=WACC_BLOCK)

    replies = (
        {"action": "refresh_sec_data", "reason": "Filing list may be stale."},
        {"action": "accept_with_flag", "reason": "Nothing else applies."},
    )
    with model(tmp_path, *replies) as llm:
        _, review = DataLoop(llm, None, online=True).run(collect, ("wacc",), None, sec)  # type: ignore[arg-type]
    assert seen == [False, True] and sec.refresh_mutable is False
    assert [s.action for s in review.steps] == ["refresh_sec_data", "accept_with_flag"]
    assert review.status == "flagged" and review.remaining
    assert review.steps[0].changes == ()  # same data: nothing changed, nothing hidden


def test_classified_lines_and_their_value_changes_are_recorded(tmp_path, monkeypatch):
    tools = SimpleNamespace(extractions=("extraction",))
    monkeypatch.setattr(loop_module, "candidates", lambda e: ["blocking line"])
    monkeypatch.setattr(
        loop_module,
        "classify_unknown_lines",
        lambda llm, extraction, store: {"ex:FinanceArmDebt": "captive_finance_debt"},
    )
    collect = Collector(make(wacc=WACC_BLOCK), make(revenue_scale=1.5))
    reply = {"action": "classify_unknown_lines", "reason": "An unmapped line blocks debt."}
    with model(tmp_path, reply) as llm:
        bundle, review = DataLoop(llm, MappingStore(tmp_path / "maps"), online=False).run(
            collect,
            ("wacc",),
            tools,  # type: ignore[arg-type]
        )
    assert review.status == "clear" and collect.calls == 2
    changes = review.steps[0].changes
    assert "mapping ex:FinanceArmDebt -> captive_finance_debt" in changes
    assert any("annual 2025-12-31 revenue: 1000000000.0 -> 1500000000.0" == c for c in changes)


def test_each_action_is_tried_once_and_the_loop_is_bounded(tmp_path):
    replies = [{"action": "refresh_sec_data", "reason": "Again."}] * 5
    with model(tmp_path, *replies) as llm:
        _, review = DataLoop(llm, None, online=True).run(
            Collector(make(wacc=WACC_BLOCK)), ("wacc",)
        )
    actions = [s.action for s in review.steps]
    assert actions[0] == "refresh_sec_data" and actions.count("refresh_sec_data") == 1
    assert review.status == "ask_user" and len(actions) <= 4
    assert "does not apply" in (review.steps[1].rejected or "")


def test_value_changes_lists_every_difference():
    assert value_changes(make(), make()) == []
    changed = value_changes(make(), make(revenue_scale=2.0))
    # The scale applies to the six flows; the balance-sheet items stay unchanged.
    assert (
        len(changed) == 6 and "annual 2025-12-31 revenue: 1000000000.0 -> 2000000000.0" in changed
    )
    assert not any(" cash:" in c or " debt:" in c for c in changed)


def test_review_survives_a_checkpoint_round_trip():
    from dcf_agent.graph.serde import ContractSerializer

    serde = ContractSerializer()
    _, review = DataLoop(None, None, online=False).run(Collector(make(wacc=WACC_BLOCK)), ("wacc",))
    assert serde.loads_typed(serde.dumps_typed(review)) == review


def test_line_decisions_that_lose_a_value_are_undone(tmp_path, monkeypatch):
    store = MappingStore(tmp_path / "maps")
    store.save("0000000001", "ex:Old", {"category": "receivables"})
    before = store.path("0000000001").read_bytes()
    tools = SimpleNamespace(extractions=("extraction",))
    monkeypatch.setattr(loop_module, "candidates", lambda e: ["blocking line"])

    def classify(llm, extraction, mappings):
        mappings.save("0000000001", "ex:TaxCredits", {"category": "receivables"})
        return {"ex:TaxCredits": "receivables"}

    monkeypatch.setattr(loop_module, "classify_unknown_lines", classify)
    first = make(wacc=WACC_BLOCK)
    # After the decision the period loses payables; after the undo it is as before.
    collect = Collector(first, make(wacc=WACC_BLOCK, drop="payables"), first)
    replies = (
        {"action": "classify_unknown_lines", "reason": "Unmapped lines."},
        {"action": "ask_user", "reason": "Still blocked."},
    )
    with model(tmp_path, *replies) as llm:
        bundle, review = DataLoop(llm, store, online=False).run(collect, ("wacc",), tools)  # type: ignore[arg-type]
    step = review.steps[0]
    assert step.reverted and collect.calls == 3
    assert store.path("0000000001").read_bytes() == before  # stored mappings restored
    assert "reverted: mapping ex:TaxCredits -> receivables" in step.changes
    assert any(c.startswith("reverted: annual 2025-12-31 payables") for c in step.changes)
    assert value_changes(first, bundle) == []  # the data is as it was


def test_failed_sources_block_only_the_plans_that_need_them():
    bundle = make(failed=("betas", "spreads"))
    assert blockers(bundle, ("data", "answer")) == []  # a data question does not use them
    assert {b.key for b in blockers(bundle, ("data", "wacc", "answer"))} == {"betas", "spreads"}


def test_failed_damodaran_tables_are_searched_again_by_default():
    collect = Collector(make(failed=("betas", "country_risk")), make())
    _, review = DataLoop(None, None, online=True).run(collect, ("wacc",))
    assert [s.action for s in review.steps] == ["retry_sources"] and review.status == "clear"
