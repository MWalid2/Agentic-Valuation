"""Daily prices: yfinance, Stooq, then a validated saved response."""

import csv
import importlib
import io
import math
import re
from collections.abc import Callable
from datetime import date, timedelta
from typing import Any, Self

import httpx

from dcf_agent.config import AppConfig
from dcf_agent.contracts.external import PriceHistory, SplitObservation
from dcf_agent.contracts.inputs import PriceObservation
from dcf_agent.contracts.provenance import Provenance, Sourced
from dcf_agent.data.cache import DataUnavailable, SnapshotCache, get_response

HistoryLoader = Callable[[str, date, date], dict[str, Any]]


def yahoo_history(ticker: str, start: date, end: date) -> dict[str, Any]:
    """Keep yfinance's pandas boundary here; do not use undated Ticker.info."""
    yf = importlib.import_module("yfinance")
    yf.config.debug.hide_exceptions = False  # Raise provider errors instead of returning empty.
    stock = yf.Ticker(ticker)
    frame = stock.history(
        start=str(start),
        end=str(end + timedelta(days=1)),
        interval="1d",
        auto_adjust=False,
        back_adjust=False,
        actions=True,
        repair=False,
        timeout=30,
    )
    metadata = stock.get_history_metadata()
    bars = []
    for stamp, row in frame.iterrows():
        close = float(row["Close"])
        if not math.isfinite(close):
            continue
        bars.append(
            {
                "date": str(stamp.date()),
                "close": close,
                "split": float(row.get("Stock Splits", 0.0)),
            }
        )
    return {"currency": metadata.get("currency"), "bars": bars}


def parse_prices(
    ticker: str, start: date, as_of: date, saved: dict[str, Any], warnings: list[str]
) -> PriceHistory:
    body = saved["body"]
    source = body["source"]
    retrieved = date.fromisoformat(saved["retrieved_at"][:10])
    raw = body["data"]
    if raw.get("currency") != "USD":
        raise DataUnavailable("Price currency is not confirmed USD")
    rows = raw["bars"]
    dates: set[date] = set()
    closes: list[tuple[date, float]] = []
    splits = []
    try:
        for row in rows:
            day = date.fromisoformat(row["date"])
            if day in dates:
                raise DataUnavailable("Duplicate price observation")
            dates.add(day)
            price, ratio = float(row["close"]), float(row.get("split", 0.0))
            if not math.isfinite(price) or price <= 0 or not math.isfinite(ratio) or ratio < 0:
                raise DataUnavailable("Invalid price or split ratio")
            if day > retrieved:
                continue
            if ratio:
                splits.append(SplitObservation(observed_on=day, ratio=ratio))
            if start <= day <= min(as_of, retrieved - timedelta(days=1)):
                closes.append((day, price))
    except (ValueError, KeyError, TypeError):
        raise DataUnavailable("Invalid price response") from None
    if not closes:
        raise DataUnavailable("No completed daily prices within requested dates")
    closes.sort()
    if (as_of - closes[-1][0]).days > 7:
        raise DataUnavailable("Latest price is more than seven days old")
    # Yahoo Close is split-adjusted even with auto_adjust=False. Undo actions
    # occurring AFTER the requested cutoff, never dividend-adjust the spot close.
    future_factor = math.prod(s.ratio for s in splits if s.observed_on > as_of)
    if source == "stooq":
        future_factor = 1.0
        warnings.append(
            "Stooq adjustment basis unverified; not eligible for a price/share market-cap bridge"
        )
    warnings.append(
        "Provider history is retrieved retrospectively; not a certified point-in-time price archive"
    )
    ref = (
        f"https://finance.yahoo.com/quote/{ticker}/history/"
        if source == "yfinance"
        else f"https://stooq.com/q/d/?s={ticker.lower()}.us"
    )
    observations = tuple(
        PriceObservation(
            observed_on=day,
            price=Sourced[float](
                value=price * future_factor,
                prov=Provenance(
                    source=source,
                    ref=ref,
                    as_of=as_of,
                    period_end=day,
                    confidence="medium",
                    note=(
                        f"Daily Close; snapshot {saved['sha256']}; fetched {retrieved}; "
                        f"future split reversal factor {future_factor:g}; no dividend adjustment"
                    ),
                ),
            ),
        )
        for day, price in closes
    )
    return PriceHistory(
        ticker=ticker,
        as_of=as_of,
        start=start,
        retrieved_on=retrieved,
        source=source,
        basis="split_adjusted" if source == "yfinance" else "provider_adjusted",
        observations=observations,
        splits=tuple(s for s in splits if start <= s.observed_on <= as_of),
        warnings=tuple(warnings),
    )


class PriceClient:
    def __init__(
        self,
        config: AppConfig,
        *,
        offline: bool = False,
        transport: httpx.BaseTransport | None = None,
        loader: HistoryLoader = yahoo_history,
    ) -> None:
        self.offline, self.loader = offline, loader
        self.cache = SnapshotCache(config.settings.raw_cache_dir / "prices")
        self.http = httpx.Client(timeout=30, trust_env=False, transport=transport)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.http.close()

    def get_price_history(self, ticker: str, start: date, as_of: date) -> PriceHistory:
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,14}", ticker) or not start < as_of <= date.today():
            raise DataUnavailable("Invalid ticker or price date range")
        key = f"{ticker}:{start}:{as_of}"
        failures: list[str] = []
        if not self.offline:
            for source in ("yfinance", "stooq"):
                try:
                    if source == "yfinance":
                        raw = self.loader(ticker, start, date.today())
                    else:
                        # Avoid silently translating class tickers to a different security.
                        if "." in ticker or "-" in ticker:
                            raise DataUnavailable(
                                "Stooq class-share symbol mapping is not configured"
                            )
                        response = get_response(
                            self.http,
                            "https://stooq.com/q/d/l/",
                            {
                                "s": ticker.lower() + ".us",
                                "i": "d",
                                "d1": start.strftime("%Y%m%d"),
                                "d2": as_of.strftime("%Y%m%d"),
                            },
                        )
                        raw = {
                            "currency": "USD",
                            "bars": [
                                {"date": r["Date"], "close": float(r["Close"]), "split": 0.0}
                                for r in csv.DictReader(io.StringIO(response.text))
                            ],
                        }
                    body = {"source": source, "data": raw}
                    candidate = {
                        "body": body,
                        "retrieved_at": date.today().isoformat(),
                        "sha256": "pending",
                    }
                    parse_prices(ticker, start, as_of, candidate, [])
                    saved = self.cache.put(key, body)
                    return parse_prices(ticker, start, as_of, saved, list(failures))
                except Exception as error:
                    # Third-party exception messages can contain cookies/URLs: redact them.
                    failures.append(f"{source} unavailable ({type(error).__name__})")
        for saved in self.cache.versions(key):
            try:
                return parse_prices(
                    ticker, start, as_of, saved, [*failures, "Using saved price response"]
                ).model_copy(update={"from_cache": True})
            except (DataUnavailable, ValueError, KeyError, TypeError):
                continue
        raise DataUnavailable("; ".join(failures) or "No eligible cached price response")
