"""Offline checks for the retained SEC transport and fact lookup tools."""

from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from pydantic import SecretStr

from dcf_agent.config import AppConfig, ModelsConfig, Settings
from dcf_agent.data.sec import Fact, SECClient, SECError, normalize_cik

CIK = "0000000001"
CUTOFF = date(2025, 3, 1)


def config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        settings=Settings(project_root=tmp_path, sec_user_agent=SecretStr("Test test@example.com")),
        models=ModelsConfig.model_validate(yaml.safe_load(Path("config/models.yaml").read_text())),
    )


def fact(
    start: str | None,
    end: str,
    value: float,
    *,
    tag: str = "Revenues",
    filed: str = "2025-02-01",
    accession: str = "0000000001-25-000001",
    fp: str = "FY",
    form: str = "10-K",
    fy: int = 2024,
) -> Fact:
    return Fact(
        namespace="us-gaap",
        tag=tag,
        unit="USD",
        value=value,
        start=date.fromisoformat(start) if start else None,
        end=date.fromisoformat(end),
        filed=date.fromisoformat(filed),
        accession=accession,
        form=form,
        fiscal_year=fy,
        fiscal_period=fp,
    )


def year_facts() -> tuple[Fact, ...]:
    return tuple(
        fact("2024-01-01", end, value)
        for end, value in (
            ("2024-03-31", 10.0),
            ("2024-06-30", 30.0),
            ("2024-09-30", 60.0),
            ("2024-12-31", 100.0),
        )
    ) + (
        fact(None, "2024-12-31", 200.0, tag="Assets"),
        fact(None, "2024-12-31", 120.0, tag="Liabilities"),
        fact(None, "2024-12-31", 80.0, tag="StockholdersEquity"),
    )


def body(rows: tuple[Fact, ...]) -> dict[str, Any]:
    facts: dict[str, Any] = {}
    for f in rows:
        facts.setdefault(f.namespace, {}).setdefault(f.tag, {"units": {}})["units"].setdefault(
            f.unit, []
        ).append(
            {
                "val": f.value,
                "end": f.end.isoformat(),
                "filed": f.filed.isoformat(),
                "accn": f.accession,
                "form": f.form,
                "fy": f.fiscal_year,
                "fp": f.fiscal_period,
                **({"start": f.start.isoformat()} if f.start else {}),
            }
        )
    return {"cik": 1, "facts": facts}


@pytest.mark.parametrize("value", ["0", "", "-1", "12345678901", "../1", " 1"])
def test_invalid_cik(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_cik(value)


@pytest.mark.parametrize("status, attempts", [(403, 1), (404, 1), (429, 3), (503, 3)])
def test_bounded_http_failure(tmp_path: Path, status: int, attempts: int) -> None:
    calls = []
    sleeps = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, headers={"Retry-After": "2"})

    with SECClient(
        config(tmp_path), transport=httpx.MockTransport(respond), sleep=sleeps.append
    ) as client:
        with pytest.raises(SECError):
            client.get_company_facts("1", CUTOFF)
    assert len(calls) == attempts
    assert sum(sleeps) >= (attempts - 1) * 2


def test_rate_limit_and_refresh(tmp_path: Path) -> None:
    now = [0.0]
    starts = []

    def sleep(delay: float) -> None:
        now[0] += delay

    def respond(_: httpx.Request) -> httpx.Response:
        starts.append(now[0])
        return httpx.Response(200, json=body(year_facts()))

    with SECClient(
        config(tmp_path), transport=httpx.MockTransport(respond), clock=lambda: now[0], sleep=sleep
    ) as client:
        client.get_company_facts("1", CUTOFF)
        client.get_company_facts("1", CUTOFF, refresh=True)
    assert starts == [0, 0.2]


def test_session_refresh_downloads_each_mutable_endpoint_once(tmp_path: Path) -> None:
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=body(year_facts()))

    with SECClient(
        config(tmp_path), transport=httpx.MockTransport(respond), sleep=lambda _: None
    ) as client:
        client.get_company_facts("1", CUTOFF)
        client.start_refresh()
        for _ in range(3):  # a collection asks for the same endpoint several times
            client.get_company_facts("1", CUTOFF)
        client.stop_refresh()
        client.get_company_facts("1", CUTOFF)
        client.start_refresh()  # a later refresh downloads it again, once
        client.get_company_facts("1", CUTOFF)
        client.get_company_facts("1", CUTOFF)
    assert len(calls) == 3


def test_submissions_archives_and_cutoff(tmp_path: Path) -> None:
    def block(acc: str, filed: str) -> dict[str, list[str]]:
        return {
            "accessionNumber": [acc],
            "form": ["10-K"],
            "filingDate": [filed],
            "reportDate": ["2024-12-31"],
            "primaryDocument": ["report.htm"],
        }

    def respond(request: httpx.Request) -> httpx.Response:
        if "submissions-001" in request.url.path:
            return httpx.Response(200, json=block("old", "2024-02-01"))
        return httpx.Response(
            200,
            json={
                "cik": "1",
                "filings": {
                    "recent": block("future", "2025-04-01"),
                    "files": [
                        {"name": "CIK0000000001-submissions-001.json", "filingFrom": "2024-01-01"}
                    ],
                },
            },
        )

    with SECClient(config(tmp_path), transport=httpx.MockTransport(respond)) as client:
        assert [f.accession for f in client.get_submissions("1", CUTOFF)] == ["old"]


@pytest.mark.parametrize("bad_body", [{"cik": 2, "facts": {}}, {"cik": 1}, []])
def test_malformed_payload(tmp_path: Path, bad_body: Any) -> None:
    with SECClient(
        config(tmp_path),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=bad_body)),
    ) as client:
        with pytest.raises(SECError):
            client.get_company_facts("1", CUTOFF)


def test_refresh_preserves_prior_versions(tmp_path: Path) -> None:
    payloads = iter((body(year_facts()), body((fact("2024-01-01", "2024-12-31", 110),))))
    with SECClient(
        config(tmp_path),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=next(payloads))),
        sleep=lambda _: None,
    ) as client:
        client.get_company_facts("1", CUTOFF)
        client.get_company_facts("1", CUTOFF, refresh=True)
    assert len(list((tmp_path / "data/raw/sec/versions").glob("*.json"))) == 2


def test_missing_contact_fails_before_network(tmp_path: Path) -> None:
    invalid = config(tmp_path)
    invalid = invalid.model_copy(
        update={
            "settings": invalid.settings.model_copy(
                update={"sec_user_agent": SecretStr("Anonymous")}
            )
        }
    )
    with pytest.raises(ValueError, match="contact email"):
        SECClient(invalid)
