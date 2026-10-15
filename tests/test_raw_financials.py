"""Raw handoff: exact durations, independently checked totals, no silent zeros."""

import json
from datetime import date
from pathlib import Path
from unittest.mock import Mock

import pytest
from test_edgar_agent import CASES
from test_snapshot_agent import prepared

from dcf_agent.agents.snapshot import SnapshotResult
from dcf_agent.contracts.inputs import ValuationRequest
from dcf_agent.contracts.snapshot import FLOWS, SNAPSHOT_CONCEPTS, FinancialSnapshot, SnapshotField
from dcf_agent.data.financials import build_raw_financials
from dcf_agent.data.sec import Fact, SECError
from dcf_agent.tools.data import DataTools
from dcf_agent.tools.snapshot import read_filing_facts, read_statement


def empty_snapshot(case):
    reference = next(
        r
        for r in json.loads(Path("tests/fixtures/sec/snapshot-reference.json").read_text())
        if r["company"].lower() == case[0]
    )
    return FinancialSnapshot(
        cik=case[1].zfill(10),
        as_of=date(2026, 10, 11),
        kind="annual",
        fiscal_year=reference["fiscal_year"],
        period_start=date.fromisoformat(reference["period_start"]),
        period_end=date.fromisoformat(case[2]),
        fields={
            c: SnapshotField(
                status="missing",
                unit="shares" if c == "shares_outstanding" else "USD",
                basis="period" if c in FLOWS else "instant",
                explanation="Not interpreted",
            )
            for c in SNAPSHOT_CONCEPTS
        },
    )


@pytest.mark.parametrize(
    "case,expected",
    [
        (CASES[0], {"sga": 14521e6, "minority_interest": 2106e6}),
        (
            CASES[1],
            {
                "cogs": 26487e6,
                "sga": 16114e6,
                "income_tax_expense": 792e6,
                "pretax_income": 3900e6,
                "operating_lease_liabilities": 3091e6,
            },
        ),
        (CASES[2], {"cogs": 220960e6, "sga": 27601e6, "rnd": 34550e6}),
    ],
)
def test_recorded_statement_enrichment(tmp_path, case, expected):
    _, client, tools = prepared(tmp_path, case)
    with client:
        raw, issues, _ = build_raw_financials(empty_snapshot(case), tools)
    values = raw.annual[0].values
    for concept, value in expected.items():
        assert values[concept].value == value
        assert values[concept].prov.filed == date.fromisoformat(case[3])
        assert "sec.gov" in values[concept].prov.ref
    assert "preferred_equity" not in values and "preferred_equity" in issues
    if case[0] == "nike":
        assert "interest_expense" not in values  # Net interest is not gross expense.
        assert values["operating_lease_liabilities"].prov.source == "derived"


def test_snapshot_bridge_preserves_share_date_and_defines_ebit(tmp_path):
    _, client, tools = prepared(tmp_path, CASES[2])
    snapshot = SnapshotResult.model_validate_json(
        Path("tests/fixtures/market/apple-snapshot.json").read_text()
    ).snapshot
    with client:
        raw, issues, _ = build_raw_financials(snapshot, tools)
    assert raw.annual[0].values["ebit"].value == snapshot.fields["operating_income"].amount.value
    assert "operating profit" in raw.annual[0].values["ebit"].prov.note
    assert "shares_outstanding" not in raw.annual[0].values
    assert "shares_outstanding" in issues
    assert raw.ttm is None and not raw.quarterly


def test_annual_cash_flows_are_not_relabelled_as_quarterly(tmp_path):
    _, client, tools = prepared(tmp_path, CASES[1])
    data = empty_snapshot(CASES[1]).model_dump()
    data.update(kind="quarterly", fiscal_quarter=4, period_start=date(2026, 3, 1))
    with client:
        raw, issues, _ = build_raw_financials(FinancialSnapshot.model_validate(data), tools)
    assert raw.quarterly and not raw.annual
    assert "stock_based_compensation" not in raw.quarterly[0].values
    assert "stock_based_compensation" in issues
    assert "net_ppe" in raw.quarterly[0].values


def test_raw_extraction_rejects_mismatched_company(tmp_path):
    _, client, tools = prepared(tmp_path, CASES[1])
    with client, pytest.raises(SECError, match="identity/cutoff/period"):
        build_raw_financials(empty_snapshot(CASES[2]), tools)


@pytest.mark.parametrize("metadata", [True, False])
def test_inline_history_fallback_preserves_context_and_requires_metadata(tmp_path, metadata):
    _, client, tools = prepared(tmp_path, CASES[1])
    if metadata:
        tools.document = tools.document.replace(
            "<body>",
            "<body>"
            '<ix:nonNumeric name="dei:DocumentFiscalYearFocus">2026</ix:nonNumeric>'
            '<ix:nonNumeric name="dei:DocumentFiscalPeriodFocus">FY</ix:nonNumeric>',
        )
    with client:
        if not metadata:
            with pytest.raises(SECError, match="metadata"):
                read_filing_facts(tools)
            return
        facts = read_filing_facts(tools)
    revenue = next(
        f for f in facts if f.tag == "RevenueFromContractWithCustomerExcludingAssessedTax"
    )
    assert revenue.value == 46398e6 and revenue.fiscal_year == 2026
    assert revenue.start == date(2025, 6, 1) and revenue.end == date(2026, 5, 31)
    assert revenue.document_ref and revenue.accession == tools.filing.accession


def test_history_tool_falls_back_when_companyfacts_omits_the_filing(tmp_path, monkeypatch):
    _, client, edgar = prepared(tmp_path, CASES[1])
    document = edgar.document.replace(
        "<body>",
        "<body>"
        '<ix:nonNumeric name="dei:DocumentFiscalYearFocus">2026</ix:nonNumeric>'
        '<ix:nonNumeric name="dei:DocumentFiscalPeriodFocus">FY</ix:nonNumeric>',
    )
    monkeypatch.setattr(client, "get_filing_document", lambda *_: document)
    tools = DataTools(
        ValuationRequest(
            ticker="NKE",
            cik=edgar.cik,
            company_name="Nike",
            valuation_date=edgar.as_of,
            mode="info",
            question="History",
        ),
        sec=client,
        snapshot_agent=Mock(),
        fred=Mock(),
        damodaran=Mock(),
        prices=Mock(),
    )
    with client:
        seed, _, _ = build_raw_financials(empty_snapshot(CASES[1]), edgar)
        raw, issues = tools.get_financial_history(seed)
    assert raw.annual[-1].values["revenue"].value == 46398e6
    assert "read_history_filing:" + edgar.filing.accession in tools.audit
    assert "quarterly_history" in issues  # A single filing cannot fabricate eight quarters.


@pytest.mark.parametrize("mutation", [None, "other_filing", "conflict", "future"])
def test_footnote_facts_require_exact_filing_and_unambiguous_components(
    tmp_path, monkeypatch, mutation
):
    _, client, tools = prepared(tmp_path, CASES[1])
    facts = [
        Fact(
            namespace="us-gaap",
            tag=tag,
            unit="USD",
            value=value,
            start=None,
            end=tools.filing.report_date,
            filed=tools.filing.filed,
            accession=tools.filing.accession,
            form="10-K",
            fiscal_year=2026,
            fiscal_period="FY",
        )
        for tag, value in (
            ("FinanceLeaseLiabilityCurrent", 10.0),
            ("FinanceLeaseLiabilityNoncurrent", 90.0),
        )
    ]
    if mutation == "other_filing":
        facts = [f.model_copy(update={"accession": "0000320187-26-000999"}) for f in facts]
    elif mutation == "future":
        facts = [f.model_copy(update={"filed": date(2027, 1, 1)}) for f in facts]
    elif mutation == "conflict":
        facts.append(facts[0].model_copy(update={"value": 20.0}))
    monkeypatch.setattr(client, "get_company_facts", lambda *_: tuple(facts))
    with client:
        raw, issues, _ = build_raw_financials(empty_snapshot(CASES[1]), tools)
    if mutation is None:
        value = raw.annual[0].values["finance_lease_liabilities"]
        assert value.value == 100
        assert value.prov.source == "derived" and "FinanceLeaseLiabilityCurrent" in value.prov.note
        assert "read_filing_xbrl_disclosures" in tools.audit
    else:
        assert "finance_lease_liabilities" not in raw.annual[0].values
        assert "finance_lease_liabilities" in issues


@pytest.mark.parametrize(
    "mutation", ["duplicate", "duration", "negative", "conflict", "lease_component"]
)
def test_rejects_unsafe_supplemental_evidence(tmp_path, monkeypatch, mutation):
    _, client, tools = prepared(tmp_path, CASES[1])
    with client:
        sections = {
            s: list(read_statement(tools, s))
            for s in ("income_statement", "cash_flow", "balance_sheet")
        }
        target = next(
            c
            for c in sections["income_statement"]
            if c.tag == "us-gaap:SellingGeneralAndAdministrativeExpense"
        )
        if mutation == "duplicate":
            sections["income_statement"].append(target.model_copy(update={"cell_id": "duplicate"}))
        elif mutation in ("duration", "negative"):
            index = sections["income_statement"].index(target)
            sections["income_statement"][index] = target.model_copy(
                update=(
                    {"period_start": date(2025, 1, 1)}
                    if mutation == "duration"
                    else {"value": -1.0}
                )
            )
        elif mutation == "conflict":
            monkeypatch.setattr(
                tools, "reconcile", lambda _: (_ for _ in ()).throw(SECError("conflict"))
            )
        else:
            sections["balance_sheet"] = [
                c
                for c in sections["balance_sheet"]
                if c.tag != "us-gaap:OperatingLeaseLiabilityCurrent"
            ]
        monkeypatch.setattr(
            "dcf_agent.data.financials.read_statement", lambda _, section: tuple(sections[section])
        )
        if mutation == "conflict":
            with pytest.raises(SECError, match="No validated"):
                build_raw_financials(empty_snapshot(CASES[1]), tools)
        else:
            raw, issues, _ = build_raw_financials(empty_snapshot(CASES[1]), tools)
            concept = "operating_lease_liabilities" if mutation == "lease_component" else "sga"
            assert concept not in raw.annual[0].values and concept in issues
