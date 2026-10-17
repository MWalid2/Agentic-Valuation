"""Filings Analyst: pointers become exact values; checks decide the confidence."""

import json
from datetime import date
from pathlib import Path

from test_filings_text import parsed
from test_llm import config as llm_config

from dcf_agent.agents.filings import FieldSpec, FilingsAnalyst, parse_number, render
from dcf_agent.llm import LocalLLM
from dcf_agent.rag.index import FilingIndex
from dcf_agent.rag.queries import expand

END = date(2025, 12, 31)
TOTAL = FieldSpec(
    "us-gaap:DebtInstrumentCarryingAmount",
    "debt",
    "What was total debt?",
    "Which number is total debt at year end?",
    "USD",
)


def setup(tmp_path: Path):
    text = parsed()
    index = FilingIndex(tmp_path / "index.sqlite")
    index.add(text, "0000000001")
    hits = index.search(expand(TOTAL.question, END), [text.accession], k=4, field="debt")
    rendered = render(hits, index)
    ids = {
        fact.fact_id: cell_id
        for cell_id, (_, cell, _) in rendered.cells.items()
        for fact in cell.facts
    }
    return index, text, ids


def read(tmp_path: Path, *replies: dict, use_xbrl: bool = False, end: date = END):
    index, text, ids = setup(tmp_path)
    answers = [
        json.dumps({k: (ids.get(v, v) if k in ("cell", "quote") else v) for k, v in r.items()})
        for r in replies
    ]
    with LocalLLM(llm_config(tmp_path), mock_responses=answers) as llm:
        return FilingsAnalyst(index, llm).read(TOTAL, [text.accession], end, use_xbrl=use_xbrl)


def point(fact_id: str) -> dict:
    return {"found": True, "cell": fact_id}


def test_agreeing_pointers_give_the_exact_tagged_value_with_a_citation(tmp_path: Path):
    reading = read(tmp_path, point("f5"), point("f5"))
    assert reading.status == "found" and reading.confidence == "high" and reading.reasons == ()
    assert reading.value.value == 1_150_000_000 and reading.fact_id == "f5"
    assert reading.value.prov.source == "sec_filing_text" and reading.value.prov.ref.endswith("#f5")
    assert reading.citation.section == "us-gaap:DebtDisclosureTextBlock"
    assert reading.model_calls == 2


def test_a_period_with_no_year_end_cell_in_the_row_is_flagged(tmp_path: Path):
    # A fiscal year end the table does not show: nothing to correct to.
    reading = read(tmp_path, point("f6"), point("f6"), end=date(2026, 6, 30))
    assert reading.confidence == "low" and reading.value.value == 1_140_000_000
    assert any("not the fiscal year end" in r for r in reading.reasons)


def test_breakdown_figure_is_flagged(tmp_path: Path):
    reading = read(tmp_path, point("f1"), point("f1"))
    assert reading.confidence == "low" and any("breakdown" in r for r in reading.reasons)


def test_disagreement_is_settled_by_a_third_reading(tmp_path: Path):
    kept = read(tmp_path, point("f5"), point("f1"), point("f5"))
    assert kept.confidence == "high" and kept.value.value == 1_150_000_000
    assert kept.model_calls == 3
    outvoted = read(tmp_path / "b", point("f1"), point("f5"), point("f5"))
    assert outvoted.value.value == 1_150_000_000 and "outvoted" in " ".join(outvoted.reasons)
    split = read(tmp_path / "c", point("f5"), point("f1"), {"found": False})
    assert split.confidence == "low" and "readings disagree" in split.reasons


def test_an_id_given_as_the_quote_and_a_quoted_cell_number_are_cells(tmp_path: Path):
    as_quote = read(tmp_path, {"found": True, "quote": "f5"}, {"found": True, "quote": "f5"})
    assert as_quote.confidence == "high" and as_quote.fact_id == "f5"
    number = read(tmp_path / "b", {"found": True, "quote": "1,150"}, point("f5"))
    assert number.confidence == "high" and number.value.value == 1_150_000_000


def test_a_quote_not_in_the_passages_is_rejected(tmp_path: Path):
    missing = {"found": True, "quote": "about one billion dollars"}
    reading = read(tmp_path, missing, {"found": False}, {"found": False})
    assert reading.status == "not_found" and reading.value is None


def test_tagged_value_is_read_without_the_model(tmp_path: Path):
    index, text, _ = setup(tmp_path)
    reading = FilingsAnalyst(index, None).read(TOTAL, [text.accession], END)
    assert reading.method == "xbrl" and reading.confidence == "high" and reading.model_calls == 0
    assert reading.value.value == 1_150_000_000 and reading.value.prov.source == "sec_xbrl"


def test_a_cell_tagged_as_another_item_is_flagged(tmp_path: Path):
    other = FieldSpec(
        "us-gaap:LongTermDebtNoncurrent", "debt", TOTAL.question, TOTAL.rephrased, "USD"
    )
    index, text, ids = setup(tmp_path)
    replies = [json.dumps({"found": True, "cell": ids["f5"]})] * 2
    with LocalLLM(llm_config(tmp_path), mock_responses=replies) as llm:
        reading = FilingsAnalyst(index, llm).read(other, [text.accession], END, use_xbrl=False)
    assert reading.confidence == "low"
    assert any("tagged as DebtInstrumentCarryingAmount" in r for r in reading.reasons)


def test_prior_year_column_in_the_right_row_is_corrected_to_the_year_end(tmp_path: Path):
    # f6 is the 2024 total; the same row holds the 2025 total (f5).
    reading = read(tmp_path, point("f6"), point("f6"))
    assert reading.value.value == 1_150_000_000 and reading.fact_id == "f5"
    assert reading.confidence == "high"


def test_numbers_in_quotes_need_a_digit():
    assert parse_number("in millions, except per share") is None
    assert parse_number("(1,234.5)") == -1234.5 and abs(parse_number("4.4%") - 0.044) < 1e-12


def schedule_chunk(rows, header_rows, kind="table", text="", title="Debt"):
    from dcf_agent.rag.filings import Cell, Chunk

    return Chunk(
        chunk_id="0000000001-26-000001:00001",
        accession="0000000001-26-000001",
        form="10-K",
        filed=date(2026, 2, 20),
        period_end=END,
        section="us-gaap:DebtDisclosureTextBlock",
        title=title,
        kind=kind,
        text=text or "maturities of debt",
        order=1,
        units="USD in millions",
        rows=tuple(tuple(Cell(text=t) for t in row) for row in rows),
        header_rows=header_rows,
    )


def schedule(chunk, field_id: str):
    from dcf_agent.agents.filings import Rendered, schedule_reading

    spec_ = FieldSpec("us-gaap:" + field_id, "debt", "q", "q", "USD")
    found = schedule_reading(Rendered(text="", chunks=[chunk]), spec_, END)
    return found[0] if found else None


def test_schedule_rows_are_read_by_the_fiscal_year():
    rows = [["(in millions)", "Amount"], ["2026", "$500"], ["2029", "—"], ["Thereafter", "4,000"]]
    chunk = schedule_chunk(rows, 1)
    assert schedule(chunk, "LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths") == 500e6
    assert schedule(chunk, "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFour") == 0.0
    assert schedule(chunk, "LongTermDebtMaturitiesRepaymentsOfPrincipalAfterYearFive") == 4_000e6
    assert schedule(chunk, "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearTwo") is None


def test_schedules_by_year_column_and_in_prose():
    columns = schedule_chunk(
        [["2026", "2027", "2028", "2029", "Thereafter"], ["$750", "—", "$1,000", "—", "$5,000"]], 1
    )
    assert schedule(columns, "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearThree") == 1_000e6
    assert schedule(columns, "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearTwo") == 0.0
    from dcf_agent.rag.filings import FactRef

    prose = schedule_chunk(
        [], 0, kind="text", text="Debt maturing: 2026 – $8,921, 2029 – $6,556, and 2030 – $4,615."
    )
    fact = FactRef(
        fact_id="n4",
        concept="us-gaap:X",
        unit="USD",
        scale=6,
        negative=False,
        start=None,
        end=END,
        dimensional=False,
        value=6_556e6,
        shown="6,556",
    )
    prose = prose.model_copy(update={"facts": (fact,)})
    assert schedule(prose, "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFour") == 6_556e6


def test_a_small_quoted_number_is_not_an_amount(tmp_path: Path):
    reading = read(tmp_path, {"found": True, "quote": "1.43"}, {"found": False}, {"found": False})
    assert reading.status == "not_found"


def test_a_prose_number_for_another_year_moves_to_the_year_end_fact():
    from dcf_agent.agents.filings import Pointer, Rendered
    from dcf_agent.rag.filings import FactRef

    def number(fid: str, value: float, year: int) -> FactRef:
        return FactRef(
            fact_id=fid,
            concept="us-gaap:Antidilutive",
            unit="shares",
            scale=6,
            negative=False,
            start=date(year, 1, 1),
            end=date(year, 12, 31),
            dimensional=False,
            value=value,
            shown=f"{value / 1e6:g}",
        )

    text = "We excluded 3 million, 3 million and 8 million options in 2025, 2024 and 2023."
    chunk = schedule_chunk([], 0, kind="text", text=text)
    facts = {
        "P1-N1": number("a", 3e6, 2025),
        "P1-N2": number("b", 3e6, 2024),
        "P1-N3": number("c", 8e6, 2023),
    }
    rendered = Rendered(
        text=text, chunks=[chunk], numbers={k: (chunk, f) for k, f in facts.items()}
    )
    spec_ = FieldSpec("us-gaap:Antidilutive", "dilution", "q", "q", "shares")
    analyst = FilingsAnalyst(None, None)  # type: ignore[arg-type]
    picked = analyst.resolve(Pointer(found=True, cell="P1-N3"), rendered, spec_, END)
    assert picked is not None and picked[0] == 3e6 and picked[3] == "P1-N1" and not picked[4]
    quoted = analyst.resolve(Pointer(found=True, quote="8 million"), rendered, spec_, END)
    assert quoted is not None and quoted[0] == 3e6  # the quoted 2023 figure, moved to 2025
