"""Answers: every number in an answer body is a cited figure with a source entry."""

import json
import re
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from test_llm import config as llm_config
from test_wacc import inputs

from dcf_agent.agents.answer import (
    LABELS,
    WACC_ITEMS,
    Answer,
    Citations,
    render,
    select_concepts,
)
from dcf_agent.contracts.external import DataBundle
from dcf_agent.contracts.inputs import (
    ConceptStatus,
    FinancialPeriod,
    RawFinancials,
    ValuationRequest,
)
from dcf_agent.contracts.provenance import Provenance, Sourced
from dcf_agent.contracts.quality import DataQualityCheck, DataQualityReport
from dcf_agent.engine.assemble import AssemblyError, assemble
from dcf_agent.engine.wacc import compute_wacc
from dcf_agent.llm import LocalLLM

AS_OF = date(2026, 10, 11)
REFERENCE = [Path(f"outputs/{n}-data.json") for n in ("apple", "coca-cola", "nike")]
ALLOWED = (
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),  # dates
    re.compile(r"\bFY\d{4}\b"),  # fiscal-year labels
    re.compile(r"\[\d+\]"),  # source markers
)


def unsourced(answer: Answer, names: tuple[str, ...] = ()) -> list[str]:
    """Digits left in the body once cited figures, dates, labels and names are removed."""
    body = answer.body
    numbers = {s.number for s in answer.sources}
    markers = {int(m) for m in re.findall(r"\[(\d+)\]", body)}
    assert markers <= numbers, f"markers without a source entry: {markers - numbers}"
    for figure in answer.figures:
        assert figure.source in numbers
        source = next(s for s in answer.sources if s.number == figure.source)
        assert source.ref == figure.value.prov.ref and source.ref.strip()
    # Whole figures only, longest first: removing "5.00% [1]" must not eat "25.00% [1]".
    cited = sorted({f"{f.display} [{f.source}]" for f in answer.figures}, key=len, reverse=True)
    for text in cited:
        body = re.sub(rf"(?<![\w.,$-]){re.escape(text)}", " ", body)
    for name in names:
        body = body.replace(name, " ")
    for pattern in ALLOWED:
        body = pattern.sub(" ", body)
    return re.findall(r"\S*\d\S*", body)


def sec(value: float, end: date, ref: str = "https://www.sec.gov/x.htm#f-1") -> Sourced[float]:
    return Sourced[float](
        value=value,
        prov=Provenance(
            source="sec_xbrl",
            ref=ref,
            as_of=AS_OF,
            period_end=end,
            filed=date(2026, 2, 20),
            confidence="high",
            note="income_statement; accession=0000000001-26-000001",
        ),
    )


def period(kind: str, year: int, start: date, end: date, scale: float = 1.0) -> FinancialPeriod:
    values = {
        "revenue": sec(1_000e6 * scale, end),
        "cogs": sec(400e6 * scale, end),
        "ebit": sec(250e6 * scale, end),
        "net_income": sec(180e6 * scale, end),
        "operating_cash_flow": sec(300e6 * scale, end),
        "capex": sec(80e6 * scale, end),
        "cash": sec(120e6, end),
        "debt": sec(500e6, end),
        "payables": sec(90e6, end),
    }
    return FinancialPeriod(
        kind=kind,  # type: ignore[arg-type]
        fiscal_year=year,
        period_start=start,
        period_end=end,
        values=values,
        statuses={
            "inventory": ConceptStatus(status="not_presented", reason="No inventory line"),
            "payables": ConceptStatus(status="reported", basis="includes accrued liabilities"),
        },
    )


def request(intent: str, question: str) -> ValuationRequest:
    return ValuationRequest(
        ticker="EXM",
        cik="0000000001",
        company_name="Example 3D Corp",  # digits in a name are not figures
        valuation_date=AS_OF,
        mode="info",
        intent=intent,  # type: ignore[arg-type]
        question=question,
    )


def bundle(req: ValuationRequest) -> DataBundle:
    raw = RawFinancials(
        cik=req.cik,
        as_of=AS_OF,
        annual=(
            period("annual", 2024, date(2024, 1, 1), date(2024, 12, 31), 0.9),
            period("annual", 2025, date(2025, 1, 1), date(2025, 12, 31)),
        ),
        ttm=period("ttm", 2026, date(2025, 7, 1), date(2026, 6, 30), 1.1),
    )
    check = DataQualityCheck(
        check_id="seed:0", field="seed", status="passed", severity="info", message="Seed"
    )
    return DataBundle(request=req, raw=raw, quality=DataQualityReport(as_of=AS_OF, checks=(check,)))


def answer(intent: str, question: str, **extra: Any) -> Answer:
    req = request(intent, question)
    return render({"request": req, "data": bundle(req), **extra})


NAMES = ("Example 3D Corp", "EXM")


def test_checker_catches_a_number_without_a_source():
    cite = Citations()
    shown = cite.cite("Revenue", sec(5e9, AS_OF), "usd")
    good = cite.answer("I1", "t", f"Revenue {shown}.")
    assert unsourced(good) == []
    bad = cite.answer("I1", "t", f"Revenue {shown}, up 12% on last year.")
    assert unsourced(bad) == ["12%"]


def test_data_answer_cites_reported_and_derived_values():
    reply = answer("I1", "latest revenue and free cash flow?")
    assert unsourced(reply, NAMES) == []
    labels = [f.label for f in reply.figures]
    assert labels[0].startswith("Revenue, FY2025")
    fcf = next(
        f for f in reply.figures if f.label.startswith("Free cash flow") and "FY2025" in f.label
    )
    assert fcf.value.value == pytest.approx(220e6) and fcf.value.prov.source == "derived"
    assert "operating cash flow - capex" in (fcf.value.prov.note or "")
    assert "TTM to 2026-06-30" in reply.body


def test_statuses_are_shown_as_words_and_bases_go_to_the_source_entry():
    reply = answer("I1", "inventory and payables")
    assert "not presented (evidenced zero)" in reply.body
    payables = next(f for f in reply.figures if f.label.startswith("Payables"))
    entry = next(s for s in reply.sources if s.number == payables.source)
    assert "basis: includes accrued liabilities" in (entry.note or "")
    assert unsourced(reply, NAMES) == []


def test_history_answer_covers_every_year_with_margins():
    reply = answer("I2", "operating margin trend")
    assert "| FY2024 | FY2025 | TTM to 2026-06-30 |" in reply.body
    margin = next(f for f in reply.figures if f.label.startswith("Operating margin, FY2025"))
    assert margin.display == "25.00%"
    assert unsourced(reply, NAMES) == []


def test_wacc_answer_cites_every_component_and_the_band():
    reply = answer("I3", "what WACC?", wacc=compute_wacc(inputs()))
    labels = {f.label for f in reply.figures}
    assert {"WACC", "WACC band low", "WACC band high", "Cost of equity", "Risk-free rate"} <= labels
    wacc_entry = next(s for s in reply.sources if "method notes" in (s.note or ""))
    assert "Band" in (wacc_entry.note or "")  # numbers in notes live in the source entry
    assert unsourced(reply, NAMES) == []


def test_wacc_answer_explains_a_missing_result():
    reply = answer("I3", "what WACC?", wacc_error="Readiness blocks valuation")
    assert "not computed" in reply.body and "Readiness blocks" in reply.body


def test_audit_answer_gives_the_chain_of_a_named_value():
    reply = answer("I10", "where does the revenue number come from?")
    assert "reported in an SEC filing filed 2026-02-20" in reply.body
    assert any("accession=" in (s.note or "") for s in reply.sources)
    assert unsourced(reply, NAMES) == []
    wacc = answer("I10", "where does the beta come from?", wacc=compute_wacc(inputs()))
    assert wacc.figures[0].label == "Relevered beta"


def test_concepts_follow_the_question_and_the_model_picks_only_from_the_list(tmp_path: Path):
    assert select_concepts("net debt?") == ("debt", "cash", "marketable_securities", "net_debt")
    assert select_concepts("operating margin") == ("revenue", "ebit", "operating_margin")
    reply = json.dumps({"concepts": ["goodwill", "inventory"]})
    with LocalLLM(llm_config(tmp_path), mock_responses=[reply]) as llm:
        assert select_concepts("tell me about the acquisitions", llm) == ("goodwill", "inventory")
    invalid = json.dumps({"concepts": ["market_share"]})
    with LocalLLM(llm_config(tmp_path / "b"), mock_responses=[invalid, invalid]) as llm:
        assert select_concepts("tell me about the acquisitions", llm)[0] == "revenue"


def reference_cases():
    for path in REFERENCE:
        if not path.exists():
            continue
        yield pytest.param(path, id=path.stem)


@pytest.mark.parametrize("path", list(reference_cases()))
def test_every_number_in_every_answer_has_a_source_on_reference_bundles(path: Path):
    data = DataBundle.model_validate_json(path.read_text(encoding="utf-8"))
    try:
        wacc = compute_wacc(assemble(data, marginal_tax=0.25).wacc_inputs)
    except AssemblyError:
        wacc = None
    names = (data.request.company_name, data.request.ticker)
    if data.industry is not None:
        names += (data.industry.name,)
    questions = [("I1", f"what is the {label.lower()}?") for label in LABELS]
    questions += [("I2", f"{label.lower()} over five years") for label in LABELS]
    questions += [("I10", f"where does the {label.lower()} come from?") for label in LABELS]
    questions += [("I10", f"where does the {pattern} come from?") for pattern, *_ in WACC_ITEMS]
    questions += [("I1", "tell me something"), ("I3", "what WACC?"), ("I6", "fair value?")]
    checked = 0
    for intent, question in questions:
        req = data.request.model_copy(update={"intent": intent, "question": question})
        reply = render({"request": req, "data": data, "wacc": wacc})
        assert unsourced(reply, names) == [], (intent, question, reply.body)
        checked += len(reply.figures)
    assert checked > 500
