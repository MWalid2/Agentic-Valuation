"""Normalization Agent: candidates by code, labels by the model, rules by code."""

import json
import re
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from test_filings_text import parsed
from test_llm import config as llm_config

from dcf_agent.agents import normalizer
from dcf_agent.agents.normalizer import (
    Candidate,
    NormalizationAgent,
    descendants,
    drop_nested,
    spikes,
)
from dcf_agent.contracts.inputs import FinancialPeriod, RawFinancials
from dcf_agent.contracts.provenance import Provenance, Sourced
from dcf_agent.data.xbrl import CalcArc
from dcf_agent.llm import LocalLLM
from dcf_agent.rag.index import FilingIndex

AS_OF = date(2026, 3, 31)
CIK = "0000000001"
ACCESSION = "0000000001-26-000001"
EBIT = 1_000e6


def year_end(year: int) -> date:
    return date(year, 12, 31)


def sourced(value: float, end: date, source: str = "sec_xbrl") -> Sourced[float]:
    return Sourced[float](
        value=value,
        prov=Provenance(
            source=source,  # type: ignore[arg-type]
            ref=f"accession {ACCESSION}",
            as_of=AS_OF,
            period_end=end,
            filed=date(2026, 2, 20) if source == "sec_xbrl" else None,
            confidence="high",
        ),
    )


def period(year: int) -> FinancialPeriod:
    end = year_end(year)
    return FinancialPeriod(
        kind="annual",
        fiscal_year=year,
        period_start=date(year, 1, 1),
        period_end=end,
        values={"ebit": sourced(EBIT, end)},
    )


PERIODS = [period(y) for y in range(2021, 2026)]


def candidate(amounts: dict[int, float], *, gain: bool = False, on_face: bool = False) -> Candidate:
    # The synthetic filing explains litigation, so retrieval finds a passage for it.
    item = Candidate(
        "us-gaap:LitigationSettlementExpense", "Litigation charges", "litigation", gain, on_face
    )
    for year, value in amounts.items():
        item.amounts[year_end(year)] = sourced(value, year_end(year))
        item.filings[year_end(year)] = (ACCESSION, date(2026, 2, 20))
    return item


def decide(tmp_path: Path, item: Candidate, *replies: dict):
    index = FilingIndex(tmp_path / "index.sqlite")
    text = parsed()
    index.add(text, CIK)
    raw = RawFinancials(cik=CIK, as_of=AS_OF, annual=tuple(PERIODS))
    years = {
        p.fiscal_year: item.amounts[p.period_end].value
        for p in PERIODS
        if p.period_end in item.amounts
    }
    answers = [json.dumps(r) for r in replies]
    with LocalLLM(llm_config(tmp_path), mock_responses=answers) as llm:
        agent = NormalizationAgent(None, index, llm if replies else None)  # type: ignore[arg-type]
        return agent.decide(item, PERIODS, raw, years, EBIT, [text.accession])


def label(recurring: bool, passage: str = "P1") -> dict:
    return {
        "recurring": recurring,
        "reason": "Per the note.",
        "passage": passage,
    }


def test_signs_follow_the_issuer_calculation_tree():
    arcs = [
        CalcArc("ex:OperatingIncome", "ex:GrossProfit", 1.0),
        CalcArc("ex:OperatingIncome", "ex:OtherCharges", -1.0),
        CalcArc("ex:GrossProfit", "ex:Revenue", 1.0),
        CalcArc("ex:GrossProfit", "ex:Cost", -1.0),
    ]
    package = SimpleNamespace(statements={"income_statement": SimpleNamespace(calculations=arcs)})
    signs = descendants(package, "ex:OperatingIncome")  # type: ignore[arg-type]
    assert signs == {"ex:GrossProfit": 1, "ex:OtherCharges": -1, "ex:Revenue": 1, "ex:Cost": -1}


def test_a_one_off_charge_is_added_back_with_its_evidence(tmp_path: Path):
    decision = decide(tmp_path, candidate({2024: 80e6}), label(recurring=False))
    assert decision.adjust and not decision.ambiguous and decision.confidence == "medium"
    (adjustment,) = decision.adjustments
    assert adjustment.direction == "increase" and adjustment.amount.value == 80e6
    assert adjustment.period_end == year_end(2024) and adjustment.requires_review  # 8% of EBIT
    assert {c.accession for c in adjustment.evidence} >= {ACCESSION}
    assert len(adjustment.evidence) == 2  # the tagged amount and the passage relied on


def test_an_item_in_most_years_is_recurring_and_not_adjusted(tmp_path: Path):
    decision = decide(
        tmp_path, candidate({2022: 40e6, 2023: 50e6, 2024: 45e6}), label(recurring=True)
    )
    assert decision.rule_recurring and not decision.adjust and not decision.adjustments


def test_the_model_alone_cannot_make_a_recurring_item_one_off(tmp_path: Path):
    # The passage does not call the item unusual or one-time: the rule stands.
    decision = decide(
        tmp_path, candidate({2022: 40e6, 2023: 50e6, 2024: 45e6}), label(recurring=False)
    )
    assert decision.rule_recurring and not decision.adjust


def test_a_recurring_item_the_filing_calls_one_off_is_flagged_not_stripped(
    tmp_path: Path, monkeypatch
):
    # As if the cited passage said "one-time": the synthetic passage is about litigation.
    monkeypatch.setattr(normalizer, "ONE_OFF_WORDS", re.compile("litigation"))
    decision = decide(
        tmp_path, candidate({2022: 40e6, 2023: 50e6, 2024: 45e6}), label(recurring=False)
    )
    # Flagged for Gate 1, but never stripped on the reading alone.
    assert decision.ambiguous and decision.confidence == "low"
    assert not decision.adjust and not decision.adjustments


def test_without_the_model_the_rule_decides_at_low_confidence(tmp_path: Path):
    decision = decide(tmp_path, candidate({2024: 80e6}))
    assert decision.adjust and decision.label is None and decision.confidence == "low"


def test_an_unusual_year_of_a_recurring_gain_removes_only_the_excess(tmp_path: Path):
    item = candidate({2021: 20e6, 2022: 30e6, 2023: 10e6, 2025: 900e6}, gain=True)
    assert spikes(item, PERIODS, None) == {year_end(2025): 15e6}  # median of 20, 30, 10, 0
    decision = decide(tmp_path, item, label(recurring=True))
    (adjustment,) = decision.adjustments
    assert adjustment.direction == "decrease" and adjustment.amount.value == 885e6
    assert adjustment.amount.prov.source == "derived" and "excess" in (
        adjustment.amount.prov.note or ""
    )


def test_a_small_unusual_year_is_not_a_spike():
    item = candidate({2021: 20e6, 2022: 30e6, 2023: 10e6, 2025: 40e6})
    assert spikes(item, PERIODS, None) == {}  # 2.7x the typical level, and below 3% of EBIT


def test_a_tagged_detail_inside_an_adjusted_face_line_is_not_adjusted_twice(tmp_path: Path):
    face = decide(tmp_path, candidate({2024: 300e6}, on_face=True), label(recurring=False))
    detail = decide(tmp_path / "b", candidate({2024: 100e6, 2025: 90e6}), label(recurring=False))
    notes: list[str] = []
    drop_nested([face, detail], notes)
    assert [a.period_end for a in detail.adjustments] == [year_end(2025)] and notes


def test_named_one_off_components_are_read_from_the_filing_text():
    text = (
        "In 2024, the Company recorded other operating charges of $4,163 million. These charges "
        "consisted of $3,109 million related to the remeasurement of our contingent "
        "consideration liability, $760 million related to the impairment of our trademark, "
        "$133 million related to the Company's productivity and reinvestment program and $126 "
        "million related to the impairment of a trademark in Latin America. In addition, other "
        "operating charges included $15 million for the amortization of noncompete agreements. "
        "In 2023, the Company recorded other operating charges of $1,951 million."
    )
    found = normalizer.named_one_offs(text, "Other operating charges", 4_163e6)
    assert found is not None and found[0] == (3_109 + 760 + 126) * 1e6
    assert "program" not in found[1] and "amortization" not in found[1]
    # Another year's total, or a label that is not this line, finds nothing.
    assert normalizer.named_one_offs(text, "Other operating charges", 1_000e6) is None
    assert normalizer.named_one_offs(text, "Selling expenses", 4_163e6) is None


def test_the_charge_the_text_states_is_read_for_its_fiscal_year():
    text = (
        "In fiscal 2026, the Company recognized $385 million of estimated employee severance "
        "costs related to organizational changes. In fiscal 2024, the Company recognized "
        "restructuring charges of $443 million."
    )
    assert normalizer.stated_charges(text, 2026, "severance") == {385e6}
    assert normalizer.stated_charges(text, 2024, "restructuring") == {443e6}
    assert normalizer.stated_charges(text, 2025, "severance") == set()
