"""SEC JSON transport and dated, unit-checked company facts (no LLM)."""

import gzip
import hashlib
import json
import math
import re
import threading
import time
from collections.abc import Callable
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Literal, Self

import httpx
from pydantic import BaseModel, ConfigDict

from dcf_agent.config import AppConfig
from dcf_agent.contracts.provenance import Provenance, Sourced

IMMUTABLE = re.compile(r"^https://www\.sec\.gov/Archives/edgar/data/[0-9]+/[0-9]{18}/")


GZIP_MAGIC = bytes((0x1F, 0x8B))


def read_snapshot(path: Path) -> str:
    """Cached response text, plain or gzip-compressed (detected by its magic bytes)."""
    data = path.read_bytes()
    if data[:2] == GZIP_MAGIC:
        data = gzip.decompress(data)
    return data.decode("utf-8")


def write_snapshot(path: Path, text: str, *, compressed: bool) -> None:
    if compressed:
        path.write_bytes(gzip.compress(text.encode("utf-8"), compresslevel=6))
    else:
        path.write_text(text, encoding="utf-8")


class SECError(RuntimeError):
    """Unavailable or ambiguous SEC evidence; never substitute zero."""


def normalize_cik(cik: str) -> str:
    if not re.fullmatch(r"[0-9]{1,10}", cik) or int(cik) == 0:
        raise ValueError("CIK must contain 1–10 digits and be nonzero")
    return cik.zfill(10)


class Filing(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    accession: str
    form: str
    filed: date
    report_date: date | None
    primary_document: str
    # 8-K item codes, e.g. ("2.02", "9.01"): 2.02 is results of operations (earnings).
    items: tuple[str, ...] = ()


class Fact(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    namespace: str
    tag: str
    unit: Literal["USD", "shares"]
    value: float
    start: date | None
    end: date
    filed: date
    accession: str
    form: str
    fiscal_year: int
    fiscal_period: str
    original_tag: str | None = None
    document_ref: str | None = None

    def sourced(self, cik: str, as_of: date) -> Sourced[float]:
        return Sourced[float](
            value=self.value,
            prov=Provenance(
                source="sec_xbrl",
                as_of=as_of,
                period_end=self.end,
                filed=self.filed,
                confidence="medium" if self.original_tag else "high",
                ref=self.document_ref
                or f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
                f"{self.accession.replace('-', '')}/",
                note=f"{self.namespace}:{self.tag}; original_tag={self.original_tag}; "
                f"unit={self.unit}; "
                f"start={self.start}; accession={self.accession}",
            ),
        )


# Alternatives are equivalent concepts, not additive components.


class SECClient:
    """One shared client limits requests to five per second and caches JSON.

    refresh=False replays a pinned snapshot. refresh=True explicitly replaces it.
    offline=True never contacts the SEC, even if a snapshot is missing.
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        offline: bool = False,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        identity = config.settings.require_sec_identity()
        if not re.search(r"\S+@[^\s@]+\.[^\s@]+", identity) or "\n" in identity or "\r" in identity:
            raise ValueError("SEC_USER_AGENT must include an identity and contact email")
        self.cache_dir = config.settings.raw_cache_dir / "sec"
        self.offline = offline
        # While set, each mutable endpoint (submissions, companyfacts, ticker list) is
        # downloaded again once, on first use; filed archive documents never change and
        # are always replayed. See start_refresh.
        self.refresh_mutable = False
        self.refreshed: set[str] = set()
        self.clock, self.sleep = clock, sleep
        self.lock = threading.Lock()
        self.next_request = 0.0
        self.http = httpx.Client(
            headers={"User-Agent": identity, "Accept": "application/json"},
            timeout=30,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.http.close()

    def _json(self, url: str, *, refresh: bool = False) -> dict[str, Any]:
        body = self._fetch(url, refresh=refresh)
        if not isinstance(body, dict):
            raise SECError("Expected SEC JSON object")
        return body

    def get_filing_document(
        self, cik: str, filing: Filing, as_of: date, *, refresh: bool = False
    ) -> str:
        cik = normalize_cik(cik)
        if filing.filed > as_of:
            raise SECError("Filing is after the requested cutoff")
        if not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", filing.accession):
            raise SECError("Invalid filing accession")
        if not re.fullmatch(r"[A-Za-z0-9_\-.]+\.(?:htm|html)", filing.primary_document):
            raise SECError("Unsupported primary document path")
        url = (
            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
            f"{filing.accession.replace('-', '')}/{filing.primary_document}"
        )
        body = self._fetch(url, refresh=refresh, document=True)
        if not isinstance(body, str):
            raise SECError("Expected SEC filing text")
        return body

    def _folder(self, cik: str, filing: Filing, as_of: date) -> str:
        if filing.filed > as_of:
            raise SECError("Filing is after the requested cutoff")
        if not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", filing.accession):
            raise SECError("Invalid filing accession")
        return (
            f"https://www.sec.gov/Archives/edgar/data/{int(normalize_cik(cik))}/"
            f"{filing.accession.replace('-', '')}/"
        )

    def start_refresh(self) -> None:
        """Download each mutable endpoint again once, until `stop_refresh`."""
        self.refreshed = set()
        self.refresh_mutable = True

    def stop_refresh(self) -> None:
        self.refresh_mutable = False

    def get_ticker_map(self, *, refresh: bool = False) -> dict[str, str]:
        """Current SEC ticker list (not point-in-time): ticker -> zero-padded CIK."""
        body = self._json("https://www.sec.gov/files/company_tickers.json", refresh=refresh)
        try:
            return {
                str(row["ticker"]).upper(): normalize_cik(str(row["cik_str"]))
                for row in body.values()
            }
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SECError("Malformed SEC ticker list") from None

    def get_company_list(self, *, refresh: bool = False) -> list[tuple[str, str, str]]:
        """Current SEC listings in the file's order: (ticker, zero-padded CIK, name).

        Several tickers can share one CIK (share classes, preferred stock); the SEC
        lists a company's primary ticker first.
        """
        body = self._json("https://www.sec.gov/files/company_tickers.json", refresh=refresh)
        try:
            return [
                (
                    str(row["ticker"]).upper(),
                    normalize_cik(str(row["cik_str"])),
                    str(row["title"]),
                )
                for row in body.values()
            ]
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SECError("Malformed SEC ticker list") from None

    def get_company_profile(self, cik: str, *, refresh: bool = False) -> dict[str, str | None]:
        """Registrant name, SIC code and state of incorporation from submissions."""
        cik = normalize_cik(cik)
        body = self._json(f"https://data.sec.gov/submissions/CIK{cik}.json", refresh=refresh)
        return {
            "name": str(body.get("name") or "") or None,
            "sic": str(body.get("sic") or "") or None,
            "sic_description": str(body.get("sicDescription") or "") or None,
            "state_of_incorporation": str(body.get("stateOfIncorporation") or "") or None,
        }

    def get_filing_index(
        self, cik: str, filing: Filing, as_of: date, *, refresh: bool = False
    ) -> tuple[str, ...]:
        """File names in one accession folder; names are validated, never followed blindly."""
        body = self._json(self._folder(cik, filing, as_of) + "index.json", refresh=refresh)
        try:
            names = tuple(str(item["name"]) for item in body["directory"]["item"])
        except (KeyError, TypeError):
            raise SECError("Malformed SEC filing index") from None
        return tuple(n for n in names if re.fullmatch(r"[A-Za-z0-9_\-.]+", n))

    def get_filing_file(
        self, cik: str, filing: Filing, name: str, as_of: date, *, refresh: bool = False
    ) -> str:
        """One XBRL package file (instance, schema, linkbase or FilingSummary)."""
        if not re.fullmatch(r"[A-Za-z0-9_\-.]+\.(?:xml|xsd)", name):
            raise SECError("Unsupported XBRL package file name")
        body = self._fetch(self._folder(cik, filing, as_of) + name, refresh=refresh, document=True)
        if not isinstance(body, str):
            raise SECError("Expected SEC filing text")
        return body

    def _fetch(
        self, url: str, *, refresh: bool = False, document: bool = False
    ) -> dict[str, Any] | str:
        if self.refresh_mutable and not IMMUTABLE.match(url) and url not in self.refreshed:
            refresh = True
            self.refreshed.add(url)
        key = hashlib.sha256(url.encode()).hexdigest()
        # Immutable filing files are stored compressed; XBRL XML shrinks roughly tenfold.
        packed = self.cache_dir / f"{key}.json.gz"
        plain = self.cache_dir / f"{key}.json"
        path = packed if packed.exists() or (IMMUTABLE.match(url) and not plain.exists()) else plain
        with self.lock:
            if path.exists() and (not refresh or self.offline):
                try:
                    snapshot = json.loads(read_snapshot(path))
                    if snapshot["url"] != url or not isinstance(
                        snapshot["body"], str if document else dict
                    ):
                        raise ValueError("Invalid snapshot")
                    cached = snapshot["body"]
                    if isinstance(cached, str):
                        return cached
                    return dict(cached)
                except (ValueError, KeyError, TypeError):
                    if self.offline:
                        raise SECError("Corrupt offline SEC snapshot") from None
            if self.offline:
                raise SECError("SEC snapshot is unavailable offline")
            for attempt in range(3):
                self.sleep(max(0, self.next_request - self.clock()))
                self.next_request = self.clock() + 0.2
                try:
                    response = self.http.get(url)
                except httpx.TransportError:
                    if attempt == 2:
                        raise SECError("SEC transport failed after three attempts") from None
                    self.sleep(2**attempt)
                    continue
                if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                    delay = float(2**attempt)
                    retry = response.headers.get("Retry-After")
                    if retry:
                        try:
                            delay = max(delay, float(retry))
                        except ValueError:
                            try:
                                delay = max(
                                    delay,
                                    (
                                        parsedate_to_datetime(retry) - datetime.now(UTC)
                                    ).total_seconds(),
                                )
                            except (ValueError, TypeError):
                                pass
                    if not math.isfinite(delay) or delay > 60:
                        raise SECError("SEC requested a long retry delay; retry later")
                    self.sleep(delay)
                    continue
                try:
                    response.raise_for_status()
                    body = response.text if document else response.json()
                    if not isinstance(body, str if document else dict):
                        raise ValueError("Expected an object")
                except (httpx.HTTPError, ValueError):
                    raise SECError(
                        f"SEC returned invalid JSON or HTTP {response.status_code}"
                    ) from None
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                path = packed if IMMUTABLE.match(url) else plain
                temporary = path.with_name(path.name + ".tmp")
                write_snapshot(
                    temporary,
                    json.dumps(
                        {
                            "url": url,
                            "retrieved_at": datetime.now(UTC).isoformat(),
                            "body": body,
                        }
                    ),
                    compressed=path is packed,
                )
                temporary.replace(path)
                # Preserve historical payload versions even when the active snapshot is refreshed.
                # Files inside an accession folder are immutable once filed; copying them again
                # would only double the cache.
                if not IMMUTABLE.match(url):
                    version = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
                    archive_dir = self.cache_dir / "versions"
                    archive_dir.mkdir(exist_ok=True)
                    archive_path = archive_dir / f"{key}-{version}.json"
                    if not archive_path.exists():
                        archive_path.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
                if isinstance(body, str):
                    return body
                return dict(body)
        raise SECError("SEC request failed")

    def get_submissions(
        self, cik: str, as_of: date, *, refresh: bool = False
    ) -> tuple[Filing, ...]:
        cik = normalize_cik(cik)
        body = self._json(f"https://data.sec.gov/submissions/CIK{cik}.json", refresh=refresh)
        try:
            if normalize_cik(str(body["cik"])) != cik:
                raise ValueError("CIK mismatch")
            blocks = [body["filings"]["recent"]]
            for archive in body["filings"].get("files", []):
                if date.fromisoformat(archive["filingFrom"]) <= as_of:
                    name = archive["name"]
                    if not re.fullmatch(r"CIK[0-9]{10}-submissions-[0-9]+\.json", name):
                        raise ValueError("Invalid submissions archive name")
                    blocks.append(
                        self._json(f"https://data.sec.gov/submissions/{name}", refresh=refresh)
                    )
            selected: dict[str, Filing] = {}
            for block in blocks:
                columns = ("accessionNumber", "form", "filingDate", "reportDate", "primaryDocument")
                if len({len(block[c]) for c in columns}) != 1:
                    raise ValueError("Unequal filing columns")
                codes = block.get("items") or [""] * len(block["accessionNumber"])
                if len(codes) != len(block["accessionNumber"]):
                    raise ValueError("Unequal filing columns")
                rows = zip(*(block[c] for c in columns), codes, strict=True)
                for acc, form, filed, report, doc, item in rows:
                    filed_date = date.fromisoformat(filed)
                    if filed_date <= as_of:
                        selected[acc] = Filing(
                            accession=acc,
                            form=form,
                            filed=filed_date,
                            report_date=date.fromisoformat(report) if report else None,
                            primary_document=doc,
                            items=tuple(c for c in str(item).split(",") if c),
                        )
            return tuple(sorted(selected.values(), key=lambda f: (f.filed, f.accession)))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SECError("Malformed SEC submissions data") from None

    def get_company_facts(
        self, cik: str, as_of: date, *, refresh: bool = False
    ) -> tuple[Fact, ...]:
        cik = normalize_cik(cik)
        body = self._json(
            f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", refresh=refresh
        )
        facts: list[Fact] = []
        try:
            if normalize_cik(str(body["cik"])) != cik:
                raise ValueError("CIK mismatch")
            for namespace in ("us-gaap", "dei"):
                for tag, concept in body["facts"].get(namespace, {}).items():
                    for unit, entries in concept["units"].items():
                        if unit not in ("USD", "shares"):
                            continue
                        for entry in entries:
                            filed, end = (
                                date.fromisoformat(entry["filed"]),
                                date.fromisoformat(entry["end"]),
                            )
                            if (
                                filed > as_of
                                or end > as_of
                                or entry["form"] not in ("10-K", "10-Q", "10-K/A", "10-Q/A")
                            ):
                                continue
                            if isinstance(entry["val"], bool):
                                raise ValueError("Boolean fact")
                            start = date.fromisoformat(entry["start"]) if "start" in entry else None
                            if start is not None and start > end:
                                raise ValueError("Invalid fact interval")
                            facts.append(
                                Fact(
                                    namespace=namespace,
                                    tag=tag,
                                    unit=unit,
                                    value=float(entry["val"]),
                                    start=start,
                                    end=end,
                                    filed=filed,
                                    accession=entry["accn"],
                                    form=entry["form"],
                                    fiscal_year=entry.get("fy", end.year),
                                    fiscal_period=entry.get("fp", ""),
                                )
                            )
            return tuple(facts)
        except (KeyError, TypeError, ValueError, AttributeError):
            raise SECError("Malformed SEC companyfacts data") from None
