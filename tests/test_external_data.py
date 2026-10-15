"""Recorded public responses plus adversarial source/fallback/date tests."""

import json
from datetime import date
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from dcf_agent.agents.data import premiums
from dcf_agent.config import load_config
from dcf_agent.data.cache import DataUnavailable, SnapshotCache
from dcf_agent.data.damodaran import FILES, DamodaranClient, parse_table
from dcf_agent.data.fred import FREDClient
from dcf_agent.data.prices import PriceClient, parse_prices

FIXTURES = Path("tests/fixtures/market")
CUTOFF = date(2026, 10, 11)
START = date(2024, 10, 11)


def configuration(tmp_path):
    config = load_config(load_env_file=False)
    return config.model_copy(
        update={
            "settings": config.settings.model_copy(
                update={
                    "project_root": tmp_path,
                    "fred_api_key": SecretStr("fixture-key"),
                }
            )
        }
    )


def table(dataset):
    return parse_table(
        dataset,
        (FIXTURES / f"{dataset}.html").read_text(encoding="utf-8"),
        (FIXTURES / "index.html").read_text(),
        date(2026, 10, 13),
        CUTOFF,
        "recorded",
    )


@pytest.mark.parametrize("dataset", FILES)
def test_recorded_damodaran_tables(dataset):
    result = table(dataset)
    assert result.available_on <= CUTOFF
    assert result.rows
    assert all(set(r.values) == set(r.units) for r in result.rows)
    if dataset == "betas":
        row = DamodaranClient.select(result, "Computers/Peripherals")
        assert row.values["Unlevered beta"].value == 1.31
        assert row.values["D/E Ratio"].value == pytest.approx(0.0462)
        assert row.units["D/E Ratio"] == "decimal"
    if dataset == "sales_to_capital":
        row = DamodaranClient.select(result, "Apparel")
        assert row.values["Capital Expenditures (US $ millions)"].value == 1_528_800_000


def test_country_erp_does_not_double_count_country_risk():
    erp, crp = premiums(table("country_risk"), "United States")
    total = (
        DamodaranClient.select(table("country_risk"), "United States")
        .values["Equity Risk Premium"]
        .value
    )
    assert erp.rate.value + crp.rate.value == pytest.approx(total)
    assert erp.rate.prov.source == "derived"


def test_damodaran_rejects_future_version_and_unknown_industry():
    with pytest.raises(DataUnavailable):
        parse_table(
            "betas",
            (FIXTURES / "betas.html").read_text(),
            (FIXTURES / "index.html").read_text(),
            CUTOFF,
            date(2025, 1, 1),
            "test",
        )
    with pytest.raises(DataUnavailable):
        DamodaranClient.select(table("betas"), "Made-up industry")


def test_damodaran_live_then_offline_and_version_retention(tmp_path):
    config = configuration(tmp_path)

    def respond(request):
        name = "index" if request.url.path.endswith("datacurrent.html") else "betas"
        return httpx.Response(200, text=(FIXTURES / f"{name}.html").read_text())

    with DamodaranClient(config, transport=httpx.MockTransport(respond)) as client:
        live = client.get_dataset("betas", CUTOFF)
    with DamodaranClient(config, offline=True) as client:
        assert client.get_dataset("betas", CUTOFF) == live
        with pytest.raises(DataUnavailable):
            client.get_dataset("betas", date(2025, 1, 1))
    cache = SnapshotCache(tmp_path / "versions")
    cache.put("source", {"value": 1})
    cache.put("source", {"value": 2})
    assert len(cache.versions("source")) == 2


def test_fred_recorded_vintage_and_secret_free_cache(tmp_path):
    config = configuration(tmp_path)
    body = json.loads((FIXTURES / "fred-dgs10.json").read_text())

    def respond(request):
        assert request.url.params["realtime_start"] == str(CUTOFF)
        assert request.url.params["realtime_end"] == str(CUTOFF)
        assert request.url.params["api_key"] == "fixture-key"
        return httpx.Response(200, json=body)

    with FREDClient(config, transport=httpx.MockTransport(respond)) as client:
        result = client.get_treasury_yield(CUTOFF)
    last = next(r for r in body["observations"] if r["value"] != ".")
    assert result.rate.value == float(last["value"]) / 100
    assert result.available_on <= CUTOFF
    assert all("fixture-key" not in p.read_text() for p in tmp_path.rglob("*.json"))
    with FREDClient(config, offline=True) as client:
        assert client.get_treasury_yield(CUTOFF) == result


@pytest.mark.parametrize("problem", ["missing", "future", "stale", "nan"])
def test_fred_invalid_observations(tmp_path, problem):
    row = {
        "date": "2026-10-09",
        "realtime_start": "2026-10-11",
        "realtime_end": "2026-10-11",
        "value": "4.0",
    }
    if problem == "missing":
        row["value"] = "."
    if problem == "future":
        row["realtime_start"] = "2026-10-12"
    if problem == "stale":
        row["date"] = "2026-09-01"
    if problem == "nan":
        row["value"] = "nan"
    with FREDClient(
        configuration(tmp_path),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"observations": [row]})),
    ) as client:
        with pytest.raises(DataUnavailable):
            client.get_treasury_yield(CUTOFF)


def test_fred_missing_key(tmp_path):
    config = configuration(tmp_path)
    config = config.model_copy(
        update={"settings": config.settings.model_copy(update={"fred_api_key": None})}
    )
    with FREDClient(config) as client, pytest.raises(DataUnavailable, match="FRED_API_KEY"):
        client.get_treasury_yield(CUTOFF)


def prices_body():
    return json.loads((FIXTURES / "yahoo-aapl.json").read_text())


def test_yahoo_recorded_response_and_offline_replay(tmp_path):
    config = configuration(tmp_path)

    def load(ticker, start, end):
        assert ticker == "AAPL" and start == START and end >= CUTOFF
        return prices_body()

    with PriceClient(config, loader=load) as client:
        result = client.get_price_history("AAPL", START, CUTOFF)
    assert result.source == "yfinance" and result.observations[-1].observed_on == date(2026, 10, 9)
    assert not result.point_in_time_verified
    with PriceClient(config, offline=True) as client:
        replay = client.get_price_history("AAPL", START, CUTOFF)
    assert replay.from_cache and replay.observations == result.observations


def test_future_splits_are_removed_from_historical_price_basis():
    saved = {
        "retrieved_at": "2026-10-13",
        "sha256": "fixture",
        "body": {
            "source": "yfinance",
            "data": {
                "currency": "USD",
                "bars": [
                    {"date": "2026-10-09", "close": 50.0, "split": 0.0},
                    {"date": "2026-10-12", "close": 51.0, "split": 2.0},
                ],
            },
        },
    }
    result = parse_prices("AAPL", START, CUTOFF, saved, [])
    assert result.observations[0].price.value == 100
    assert result.splits == ()


def test_price_fallbacks_and_cache_after_both_fail(tmp_path):
    config = configuration(tmp_path)

    def fail(*_):
        raise RuntimeError("sensitive-provider-message")

    csv = "Date,Open,High,Low,Close,Volume\n2026-10-09,10,11,9,10,100\n"
    with PriceClient(
        config, loader=fail, transport=httpx.MockTransport(lambda _: httpx.Response(200, text=csv))
    ) as client:
        result = client.get_price_history("AAPL", START, CUTOFF)
    assert result.source == "stooq" and result.basis == "provider_adjusted"
    assert not any("sensitive-provider-message" in w for w in result.warnings)
    with PriceClient(
        config, loader=fail, transport=httpx.MockTransport(lambda _: httpx.Response(403))
    ) as client:
        result = client.get_price_history("AAPL", START, CUTOFF)
    assert result.from_cache
    assert any("stooq unavailable" in w for w in result.warnings)


@pytest.mark.parametrize("problem", ["currency", "duplicate", "negative", "stale"])
def test_prices_reject_unusable_data(problem):
    raw = {"currency": "USD", "bars": [{"date": "2026-10-09", "close": 10.0}]}
    if problem == "currency":
        raw["currency"] = "GBP"
    if problem == "duplicate":
        raw["bars"] *= 2
    if problem == "negative":
        raw["bars"][0]["close"] = -1.0
    if problem == "stale":
        raw["bars"][0]["date"] = "2026-09-01"
    saved = {
        "retrieved_at": "2026-10-13",
        "sha256": "fixture",
        "body": {"source": "yfinance", "data": raw},
    }
    with pytest.raises(DataUnavailable):
        parse_prices("AAPL", START, CUTOFF, saved, [])


def test_damodaran_finds_an_archived_copy_published_by_a_past_date(tmp_path):
    """The current page is newer than the date: the latest earlier capture is used."""
    config = configuration(tmp_path)
    current = (FIXTURES / "betas.html").read_text()
    older = current.replace("January 2026", "January 2025")
    capture = "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/datafile/Betas.html"
    requested = []

    def respond(request):
        requested.append(str(request.url))
        if request.url.host == "web.archive.org":
            if request.url.path == "/cdx/search/cdx":
                assert request.url.params["to"] == "20251231"
                rows = [f"key 20251220002928 {capture} text/html 200 D 1"]
                return httpx.Response(200, text="\n".join(rows))
            assert request.url.path == f"/web/20251220002928id_/{capture}"
            return httpx.Response(200, text=older)
        name = "index" if request.url.path.endswith("datacurrent.html") else "betas"
        return httpx.Response(200, text=(FIXTURES / f"{name}.html").read_text())

    with DamodaranClient(config, transport=httpx.MockTransport(respond)) as client:
        client.pause = 0
        found = client.get_dataset("betas", date(2025, 12, 31))
    assert found.analysis_on == date(2025, 1, 1)
    assert found.available_on == date(2025, 12, 20)  # the capture proves publication
    assert found.ref == f"https://web.archive.org/web/20251220002928/{capture}"
    assert any("Internet Archive" in w for w in found.warnings)
    assert all(v.prov.ref == found.ref for r in found.rows for v in r.values.values())
    with DamodaranClient(config, offline=True) as client:
        replay = client.get_dataset("betas", date(2025, 12, 31))
        assert replay == found  # saved for offline replay, identical
        with pytest.raises(DataUnavailable, match="published on or before 2025-06-30"):
            client.get_dataset("betas", date(2025, 6, 30))  # the capture is later
