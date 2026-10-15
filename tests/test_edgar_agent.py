"""Recorded SEC tables and adversarial agent answers, all without network or LLM."""

import json
from datetime import date
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from dcf_agent.agents.edgar import EDGARAgent
from dcf_agent.config import load_config
from dcf_agent.data.sec import Filing, SECClient, SECError
from dcf_agent.llm import LocalLLM
from dcf_agent.tools.edgar import EDGARTools, debt_candidate, extract_cells

CASES = (
    ("coca-cola", "21344", "2025-12-31", "2026-02-20", "0001628280-26-010047", 45_492_000_000),
    ("nike", "320187", "2026-05-31", "2026-07-15", "0000320187-26-000088", 7_942_000_000),
    ("apple", "320193", "2025-09-27", "2025-10-31", "0000320193-25-000079", 98_657_000_000),
)


def setup(tmp_path: Path, case: tuple[str, str, str, str, str, int] = CASES[0]):
    name, cik, end, filed, acc, expected = case
    config = load_config(load_env_file=False)
    config = config.model_copy(
        update={
            "settings": config.settings.model_copy(
                update={
                    "project_root": tmp_path,
                    "sec_user_agent": SecretStr("Test test@example.com"),
                    "mock_llm": True,
                }
            )
        }
    )
    document = Path(f"tests/fixtures/sec/{name}-balance-sheet.html").read_text(encoding="utf-8")
    filing = Filing(
        accession=acc,
        form="10-K",
        filed=date.fromisoformat(filed),
        report_date=date.fromisoformat(end),
        primary_document="report.htm",
    )

    def respond(request: httpx.Request) -> httpx.Response:
        if "companyfacts" in request.url.path:
            return httpx.Response(200, json={"cik": int(cik), "facts": {}})
        if request.url.path.endswith("report.htm"):
            return httpx.Response(200, text=document)
        return httpx.Response(
            200,
            json={
                "cik": cik,
                "filings": {
                    "recent": {
                        "accessionNumber": [acc],
                        "form": ["10-K"],
                        "filingDate": [filed],
                        "reportDate": [end],
                        "primaryDocument": ["report.htm"],
                    },
                    "files": [],
                },
            },
        )

    client = SECClient(config, transport=httpx.MockTransport(respond), sleep=lambda _: None)
    tools = EDGARTools(client, cik, date(2026, 10, 11), period_end=date.fromisoformat(end))
    cells = tuple(
        c
        for c in extract_cells(document, filing, cik, tools.as_of, "balance_sheet")
        if debt_candidate(c)
    )
    return config, client, tools, cells, expected


def choice(action: str, ids: list[str] | None = None) -> str:
    return json.dumps({"action": action, "cell_ids": ids or [], "reason": "Use cited balances"})


@pytest.mark.parametrize("case", CASES)
def test_recorded_debt_and_tool_execution(tmp_path: Path, case) -> None:
    config, client, tools, cells, expected = setup(tmp_path, case)
    with (
        client,
        LocalLLM(
            config,
            mock_responses=[
                choice("read_balance_sheet"),
                choice("answer", [c.cell_id for c in cells]),
            ],
        ) as llm,
    ):
        result = EDGARAgent(llm).run(tools)
    assert result.status == "validated"
    assert result.total is not None and result.total.value == expected
    assert result.tool_calls == ("list_filings", "read_balance_sheet", "query_xbrl")
    assert result.review_required
    assert all(
        c.filed <= tools.as_of and c.period_end == tools.filing.report_date
        for c in result.components
    )


@pytest.mark.parametrize("bad", ["invented", "duplicate", "omitted", "empty"])
def test_bad_citations_cannot_produce_a_total(tmp_path: Path, bad: str) -> None:
    config, client, tools, cells, _ = setup(tmp_path)
    ids = [c.cell_id for c in cells]
    proposals = {"invented": ["made-up"], "duplicate": ids + ids, "omitted": ids[:-1], "empty": []}
    with (
        client,
        LocalLLM(
            config,
            mock_responses=[
                choice("read_balance_sheet"),
                choice("answer", proposals[bad]),
                choice("uncertain"),
            ],
        ) as llm,
    ):
        result = EDGARAgent(llm).run(tools)
    assert result.status == "needs_review" and result.total is None


def test_repeated_tools_are_bounded(tmp_path: Path) -> None:
    config, client, tools, _, _ = setup(tmp_path)
    with client, LocalLLM(config, mock_responses=[choice("read_balance_sheet")] * 3) as llm:
        result = EDGARAgent(llm).run(tools)
    assert result.status == "needs_review"
    assert result.tool_calls.count("read_balance_sheet") == 1


def test_point_in_time_and_document_path_are_enforced(tmp_path: Path) -> None:
    _, client, tools, _, _ = setup(tmp_path)
    with client:
        with pytest.raises(SECError, match="after"):
            client.get_filing_document(tools.cik, tools.filing, date(2025, 1, 1))
        with pytest.raises(SECError, match="path"):
            client.get_filing_document(
                tools.cik,
                tools.filing.model_copy(update={"primary_document": "../../secret.htm"}),
                tools.as_of,
            )


def test_xbrl_disagreement_blocks_the_answer(tmp_path: Path) -> None:
    config, client, tools, cells, _ = setup(tmp_path)
    first = cells[0]
    # A cached SEC fact for the identical filing/context with a different amount.
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{tools.cik}.json"
    namespace, tag = first.tag.split(":")
    payload = {
        "cik": int(tools.cik),
        "facts": {
            namespace: {
                tag: {
                    "units": {
                        "USD": [
                            {
                                "end": str(first.period_end),
                                "val": first.value + 1,
                                "filed": str(first.filed),
                                "accn": first.accession,
                                "form": "10-K",
                                "fy": 2025,
                                "fp": "FY",
                            }
                        ]
                    }
                }
            }
        },
    }
    import hashlib

    path = client.cache_dir / (hashlib.sha256(url.encode()).hexdigest() + ".json")
    path.write_text(json.dumps({"url": url, "body": payload}))
    with (
        client,
        LocalLLM(
            config,
            mock_responses=[
                choice("read_balance_sheet"),
                choice("answer", [c.cell_id for c in cells]),
            ],
        ) as llm,
    ):
        result = EDGARAgent(llm).run(tools)
    assert result.total is None and result.status == "needs_review"
    assert any("disagrees" in warning for warning in result.warnings)


def test_sector_asset_and_fair_value_rows_are_not_debt() -> None:
    from dcf_agent.tools.edgar import EvidenceCell

    base = dict(
        cell_id="x",
        value=1.0,
        tag="test:Tag",
        period_end=date(2025, 12, 31),
        filed=date(2026, 2, 20),
        accession="x",
        ref="https://www.sec.gov/",
        section="balance_sheet",
    )
    for label in (
        "Debt securities",
        "Debt fair value",
        "Interest on debt",
        "Loans receivable",
        "Fair value of current maturities of long-term debt",
    ):
        assert not debt_candidate(EvidenceCell(label=label, **base))


def test_total_and_component_overlap_is_rejected(tmp_path: Path) -> None:
    config, client, tools, cells, _ = setup(tmp_path)
    from dcf_agent.tools.edgar import EvidenceCell

    original = cells[0]
    total = EvidenceCell(
        **{
            **original.model_dump(),
            "cell_id": "total",
            "label": "Total debt",
            "value": sum(c.value for c in cells),
        }
    )
    tools.read = lambda _: (*cells, total)
    with (
        client,
        LocalLLM(
            config,
            mock_responses=[
                choice("read_balance_sheet"),
                choice("answer", [c.cell_id for c in (*cells, total)]),
                choice("uncertain"),
            ],
        ) as llm,
    ):
        result = EDGARAgent(llm).run(tools)
    assert result.total is None
    assert any("double counting" in warning for warning in result.warnings)


def test_negative_component_is_not_accepted(tmp_path: Path) -> None:
    config, client, tools, cells, _ = setup(tmp_path)
    negative = cells[0].model_copy(update={"value": -1.0})
    altered = (negative, *cells[1:])
    tools.read = lambda _: altered
    with (
        client,
        LocalLLM(
            config,
            mock_responses=[
                choice("read_balance_sheet"),
                choice("answer", [c.cell_id for c in altered]),
                choice("uncertain"),
            ],
        ) as llm,
    ):
        result = EDGARAgent(llm).run(tools)
    assert result.total is None
    assert any("Negative" in warning for warning in result.warnings)
