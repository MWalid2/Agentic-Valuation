"""Recorded statements, explicit model selections, and rejection boundaries."""

import json
from datetime import date
from pathlib import Path

import pytest
from test_edgar_agent import CASES, setup

from dcf_agent.agents.snapshot import FieldChoice, SnapshotAgent, validate_choice
from dcf_agent.data.sec import Fact, SECError
from dcf_agent.llm import LocalLLM
from dcf_agent.tools.snapshot import read_statement

REFERENCES = json.loads(Path("tests/fixtures/sec/snapshot-reference.json").read_text())
SELECTIONS = {
    "apple": [
        (["I1"], [], ["I7"], ["I11"]),
        ["B1"],
        ["B16", "B17", "B19"],
        ["B12"],
        ["B22"],
        ["B26"],
        ["C11"],
        ["C15"],
        ["S1"],
    ],
    "coca-cola": [
        (["I1"], [], ["I6"], ["I15"]),
        ["B1"],
        ["B19", "B20", "B24"],
        ["B17"],
        ["B23", "B24", "B25", "B26"],
        ["B32"],
        ["C11"],
        ["C16"],
        ["S1"],
    ],
    "nike": [
        (["I1"], ["I6"], ["I3"], ["I11"]),
        ["B1"],
        ["B13", "B19"],
        ["B12"],
        ["B18", "B19", "B20", "B21"],
        ["B26"],
        ["C11"],
        ["C15"],
        ["S1", "S2"],
    ],
}


def prepared(tmp_path, case):
    config, client, tools, _, _ = setup(tmp_path, case)
    tools.document = Path(f"tests/fixtures/sec/{case[0]}-snapshot.html").read_text(encoding="utf-8")
    return config, client, tools


@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("repair_debt", [False, True, "whole_group"])
def test_recorded_full_snapshot(tmp_path, case, repair_debt):
    config, client, tools = prepared(tmp_path, case)
    reference = next(r for r in REFERENCES if r["company"].lower() == case[0])
    income, *rest = SELECTIONS[case[0]]
    revenue, subtract, operating, net = income
    choices = [
        FieldChoice(concept="revenue", cell_ids=revenue),
        FieldChoice(concept="operating_income", cell_ids=operating, subtract_ids=subtract),
        FieldChoice(concept="net_income", cell_ids=net),
    ]
    concepts = [
        "cash",
        "debt",
        "total_assets",
        "total_liabilities",
        "equity",
        "operating_cash_flow",
        "capex",
        "shares_outstanding",
    ]
    choices.extend(
        FieldChoice(concept=c, cell_ids=ids) for c, ids in zip(concepts, rest, strict=True)
    )
    responses = [
        json.dumps({"fields": [c.model_dump() for c in choices[a:b]]})
        for a, b in ((0, 3), (3, 6), (6, 8), (8, 10), (10, 11))
    ]
    if repair_debt:
        original = json.loads(responses[1])
        debt = next(c for c in original["fields"] if c["concept"] == "debt")
        repaired = json.dumps(original if repair_debt == "whole_group" else {"fields": [debt]})
        debt["cell_ids"] = debt["cell_ids"][:1]
        responses[1] = json.dumps(original)
        responses.insert(2, repaired)
    with client, LocalLLM(config, mock_responses=responses) as llm:
        result = SnapshotAgent(llm).run(
            tools,
            period_start=date.fromisoformat(reference["period_start"]),
            fiscal_year=reference["fiscal_year"],
        )
    assert result.snapshot.is_complete, result.warnings
    assert result.balance_check == "passed"
    for concept, expected in reference["values"].items():
        assert result.snapshot.fields[concept].amount.value == expected
    assert (
        str(result.snapshot.fields["shares_outstanding"].observed_on)
        == reference["shares_observed_on"]
    )
    assert result.snapshot.review_required
    if repair_debt:
        assert any("corrected after validation rejection" in w for w in result.warnings)
    assert any("no companyfacts counterpart" in w for w in result.warnings)


@pytest.mark.parametrize(
    "concept,ids",
    [
        ("shares_outstanding", ["S1"]),
        ("total_assets", ["B1"]),
        ("cash", ["B12"]),
        ("total_liabilities", ["B18"]),
        ("capex", ["C27"]),
        ("operating_income", ["I9"]),
        ("debt", ["B13"]),
        ("revenue", ["invented"]),
    ],
)
def test_invalid_selections_are_rejected(tmp_path, concept, ids):
    _, client, tools = prepared(tmp_path, CASES[1])
    with client:
        ledger = {
            c.cell_id: c
            for section in ("balance_sheet", "income_statement", "cash_flow", "shares")
            for c in read_statement(tools, section)
        }
        with pytest.raises(SECError):
            validate_choice(
                FieldChoice(concept=concept, cell_ids=ids), ledger, tools, date(2025, 6, 1)
            )


def test_invalid_model_answers_produce_explicit_partial_snapshot(tmp_path):
    config, client, tools = prepared(tmp_path, CASES[2])
    with client, LocalLLM(config, mock_responses=['{"fields":[]}'] * 5) as llm:
        result = SnapshotAgent(llm).run(tools, period_start=date(2024, 9, 29), fiscal_year=2025)
    assert not result.snapshot.is_complete
    assert result.balance_check == "not_checked"
    assert len(result.snapshot.fields) == 11
    assert all(
        f.amount is None and f.status == "ambiguous" for f in result.snapshot.fields.values()
    )


@pytest.mark.parametrize("mutation", ["unit", "duration", "negative", "duplicate"])
def test_evidence_boundaries(tmp_path, mutation):
    _, client, tools = prepared(tmp_path, CASES[2])
    with client:
        cells = read_statement(tools, "cash_flow")
        ledger = {c.cell_id: c for c in cells}
        choice = FieldChoice(concept="capex", cell_ids=["C15"])
        updates = {
            "unit": {"unit": "shares"},
            "duration": {"period_start": None},
            "negative": {"value": -12715000000.0},
            "duplicate": {},
        }
        ledger["C15"] = ledger["C15"].model_copy(update=updates[mutation])
        if mutation == "duplicate":
            choice.cell_ids.append("C15")
        with pytest.raises(SECError):
            validate_choice(choice, ledger, tools, date(2024, 9, 29))


def test_cash_flow_ytd_is_preserved(tmp_path):
    _, client, tools = prepared(tmp_path, CASES[2])
    with client:
        ledger = {c.cell_id: c for c in read_statement(tools, "cash_flow")}
        field = validate_choice(
            FieldChoice(concept="operating_cash_flow", cell_ids=["C11"]),
            ledger,
            tools,
            date(2025, 6, 29),
        )
    assert field.basis == "ytd"
    assert field.period_start == date(2024, 9, 29)


def test_reconciliation_uses_duration_and_rejects_matching_context_conflict(tmp_path, monkeypatch):
    _, client, tools = prepared(tmp_path, CASES[2])
    with client:
        cell = next(c for c in read_statement(tools, "cash_flow") if c.cell_id == "C11")
        fact = Fact(
            namespace="us-gaap",
            tag=cell.tag.split(":")[-1],
            unit="USD",
            value=1.0,
            start=date(2025, 6, 29),
            end=cell.period_end,
            filed=cell.filed,
            accession=cell.accession,
            form="10-K",
            fiscal_year=2025,
            fiscal_period="FY",
        )
        monkeypatch.setattr(client, "get_company_facts", lambda *_: [fact])
        assert "no companyfacts counterpart" in tools.reconcile((cell,))[0]
        fact = fact.model_copy(update={"start": cell.period_start})
        with pytest.raises(SECError, match="disagrees"):
            tools.reconcile((cell,))


@pytest.mark.parametrize("wrong_ids", [["I3", "I4", "I5"], ["I6"]])
def test_operating_income_template_and_bounded_repair(tmp_path, monkeypatch, wrong_ids):
    config, client, tools = prepared(tmp_path, CASES[1])
    responses = [
        json.dumps(
            {
                "fields": [
                    {"concept": "revenue", "cell_ids": ["I1"]},
                    {"concept": "operating_income", "cell_ids": wrong_ids},
                    {"concept": "net_income", "cell_ids": ["I11"]},
                ]
            }
        ),
        '{"fields":[{"concept":"operating_income","cell_ids":["I3"],"subtract_ids":["I6"]}]}',
        *['{"fields":[]}'] * 4,
    ]
    prompts = []
    with client, LocalLLM(config, mock_responses=responses) as llm:
        generate = llm.generate

        def capture(role, prompt, schema, **kwargs):
            prompts.append(prompt)
            return generate(role, prompt, schema, **kwargs)

        monkeypatch.setattr(llm, "generate", capture)
        result = SnapshotAgent(llm).run(tools, period_start=date(2025, 6, 1), fiscal_year=2026)
    field = result.snapshot.fields["operating_income"]
    assert field.amount.value == 3797000000.0
    assert field.amount.prov.source == "derived"
    assert "subtract" in field.amount.prov.note
    assert '"cell_ids":["I3"],"subtract_ids":["I6"]' in prompts[0]
    assert "3797" not in prompts[0]  # Reference answers are never prompt inputs.
    assert len(prompts) == 6  # One semantic retry, no unbounded repair loop.


@pytest.mark.parametrize("extra_tag", ["ResearchAndDevelopmentExpense", "OperatingIncomeLoss"])
def test_operating_derivation_rejects_omissions_or_reported_total(tmp_path, extra_tag):
    _, client, tools = prepared(tmp_path, CASES[1])
    with client:
        ledger = {c.cell_id: c for c in read_statement(tools, "income_statement")}
        ledger["extra"] = ledger["I6"].model_copy(
            update={"cell_id": "extra", "tag": "us-gaap:" + extra_tag, "value": 100.0}
        )
        with pytest.raises(SECError):
            validate_choice(
                FieldChoice(concept="operating_income", cell_ids=["I3"], subtract_ids=["I6"]),
                ledger,
                tools,
                date(2025, 6, 1),
            )
