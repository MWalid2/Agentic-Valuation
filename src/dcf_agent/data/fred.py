"""FRED rates selected using the requested historical real-time vintage."""

import json
from datetime import date, timedelta
from typing import Literal, Self

import httpx

from dcf_agent.config import AppConfig
from dcf_agent.contracts.inputs import MacroObservation
from dcf_agent.contracts.provenance import Provenance, Sourced
from dcf_agent.data.cache import DataUnavailable, SnapshotCache, get_response

Series = Literal["DGS10", "BAMLC0A0CM", "T10YIE"]
SERIES: tuple[Series, ...] = ("DGS10", "BAMLC0A0CM", "T10YIE")


class FREDClient:
    def __init__(
        self,
        config: AppConfig,
        *,
        offline: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.key = config.settings.fred_api_key
        self.offline = offline
        self.cache = SnapshotCache(config.settings.raw_cache_dir / "fred")
        self.http = httpx.Client(timeout=30, trust_env=False, transport=transport)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.http.close()

    def get_rate(self, series: Series, as_of: date, *, refresh: bool = False) -> MacroObservation:
        if series not in SERIES or as_of > date.today():
            raise DataUnavailable("Unsupported FRED series or future cutoff")
        params = {
            "series_id": series,
            "file_type": "json",
            "realtime_start": str(as_of),
            "realtime_end": str(as_of),
            "observation_start": str(as_of - timedelta(days=14)),
            "observation_end": str(as_of),
            "sort_order": "desc",
            "limit": "100",
        }
        key = json.dumps(params, sort_keys=True)
        saved = self.cache.versions(key)
        if saved and (self.offline or not refresh):
            body = saved[0]["body"]
        else:
            if self.offline:
                raise DataUnavailable(f"No cached FRED vintage for {series} at {as_of}")
            if self.key is None or not self.key.get_secret_value().strip():
                raise DataUnavailable("Set FRED_API_KEY to fetch historical FRED vintages")
            response = get_response(
                self.http,
                "https://api.stlouisfed.org/fred/series/observations",
                {**params, "api_key": self.key.get_secret_value()},
            )
            try:
                body = response.json()
            except ValueError:
                raise DataUnavailable("FRED returned invalid JSON") from None
            if not isinstance(body, dict) or "observations" not in body:
                raise DataUnavailable("FRED response lacks observations")
            self.cache.put(key, body)
        candidates = []
        try:
            for row in body["observations"]:
                if row["value"] == ".":
                    continue
                observed = date.fromisoformat(row["date"])
                available = date.fromisoformat(row["realtime_start"])
                until = date.fromisoformat(row["realtime_end"])
                if not available <= as_of <= until or observed > as_of:
                    continue
                candidates.append(
                    MacroObservation(
                        observed_on=observed,
                        available_on=max(observed, available),
                        rate=Sourced[float](
                            value=float(row["value"]) / 100.0,
                            prov=Provenance(
                                source="fred",
                                ref=f"https://fred.stlouisfed.org/series/{series}",
                                as_of=as_of,
                                period_end=observed,
                                confidence="high",
                                note=(
                                    f"Percent divided by 100; real-time vintage {as_of}. "
                                    "available_on is the API vintage boundary, "
                                    "not a claimed original release date."
                                ),
                            ),
                        ),
                    )
                )
        except (ValueError, KeyError, TypeError):
            raise DataUnavailable("Invalid FRED observation") from None
        if not candidates:
            raise DataUnavailable(f"No eligible FRED observations for {series}")
        result = max(candidates, key=lambda r: r.observed_on)
        if (as_of - result.observed_on).days > 10:
            raise DataUnavailable(f"FRED {series} observation is stale")
        return result

    def get_treasury_yield(self, as_of: date) -> MacroObservation:
        return self.get_rate("DGS10", as_of)
