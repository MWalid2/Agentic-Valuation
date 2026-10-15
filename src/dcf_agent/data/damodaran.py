"""Allowlisted NYU tables with dated, immutable raw snapshots."""

import re
from datetime import date, datetime
from typing import Self

import httpx
from lxml import etree, html

from dcf_agent.config import AppConfig
from dcf_agent.contracts.external import BenchmarkRow, BenchmarkTable, Dataset
from dcf_agent.contracts.provenance import Provenance, Sourced
from dcf_agent.data.cache import DataUnavailable, SnapshotCache, get_response
from dcf_agent.data.industry import URL as INDUSTRY_URL
from dcf_agent.data.industry import IndustryMatch, match_industry, parse_classification

BASE = "https://pages.stern.nyu.edu/adamodar/New_Home_Page/"
FILES: dict[Dataset, str] = {
    "betas": "Betas",
    "margins": "margin",
    "sales_to_capital": "capex",
    "country_risk": "ctryprem",
    "spreads": "ratings",
}


def _text(node: etree._Element) -> str:
    return " ".join("".join(str(t) for t in node.itertext()).split())


def parse_table(
    dataset: Dataset, document: str, index: str, retrieved: date, as_of: date, version: str
) -> BenchmarkTable:
    try:
        tree = html.fromstring(document)
    except (ValueError, etree.ParserError):
        raise DataUnavailable("Invalid Damodaran HTML") from None
    content = _text(tree)
    analysis = re.search(
        r"(?:as of|updated:)\s+([A-Za-z]+)\s+(?:(\d{1,2}),?\s+)?(20\d{2})", content, re.I
    )
    analysis_on = None
    available = retrieved
    warnings = []
    if analysis:
        try:
            month = datetime.strptime(analysis[1], "%B").month
            analysis_on = date(int(analysis[3]), month, int(analysis[2] or 1))
            if analysis[2]:
                available = analysis_on
            else:
                release = re.search(
                    r"Data of last full update:\s*([A-Za-z]+ \d{1,2}, 20\d{2})",
                    _text(html.fromstring(index)),
                )
                if release:
                    released = datetime.strptime(release[1], "%B %d, %Y").date()
                    if (released.year, released.month) == (analysis_on.year, analysis_on.month):
                        available = released
        except ValueError:
            raise DataUnavailable("Invalid Damodaran analysis/publication date") from None
    if available == retrieved:
        warnings.append("Publication date not established; eligibility starts at first retrieval")
    if available > as_of:
        raise DataUnavailable("Damodaran version was not available at the requested cutoff")
    ref = BASE + "datafile/" + FILES[dataset] + ".html"
    rows: list[BenchmarkRow] = []
    for table in tree.iter("table"):
        headers: list[str] | None = None
        for row in table.iter("tr"):
            cells = [_text(c) for c in row if c.tag in ("td", "th")]
            if not cells:
                continue
            if cells[0].lower() in ("industry name", "country"):
                if len(cells) != len(set(cells)):
                    raise DataUnavailable("Duplicate Damodaran column headings")
                headers = cells
                continue
            if dataset == "spreads" and len(cells) >= 4 and cells[2].lower() == "rating is":
                headers = ["coverage_min", "coverage_max", "rating", "spread"]
                continue
            if headers is None:
                continue
            if dataset == "spreads":
                cells = cells[:4]
            if len(cells) != len(headers):
                if any(cells):
                    warnings.append("Skipped a row with an unsupported column layout")
                continue
            name = cells[2] if dataset == "spreads" else cells[0]
            values: dict[str, Sourced[float]] = {}
            units: dict[str, str] = {}
            texts: dict[str, str] = {}
            for i, (header, raw) in enumerate(zip(headers, cells, strict=True)):
                if (i == 0 and dataset != "spreads") or (dataset == "spreads" and i == 2):
                    continue
                if raw.upper() in ("", "NA", "N/A", "N/M", "-"):
                    continue
                try:
                    number = float(
                        raw.replace(",", "")
                        .replace("$", "")
                        .replace("%", "")
                        .replace("(", "-")
                        .replace(")", "")
                    )
                except ValueError:
                    texts[header] = raw
                    continue
                unit = "ratio"
                if "%" in raw:
                    number /= 100
                    unit = "decimal"
                elif "millions" in header.lower():
                    number *= 1_000_000
                    unit = "USD"
                elif "firms" in header.lower():
                    unit = "count"
                try:
                    values[header] = Sourced[float](
                        value=number,
                        prov=Provenance(
                            source="damodaran",
                            ref=ref,
                            as_of=as_of,
                            period_end=analysis_on,
                            confidence="medium",
                            note=(
                                f"{name}; {header}; published/first-seen {available}; "
                                f"snapshot {version}"
                            ),
                        ),
                    )
                except ValueError:
                    raise DataUnavailable("Nonfinite Damodaran numeric value") from None
                units[header] = unit
            if values:
                rows.append(BenchmarkRow(name=name, values=values, units=units, text=texts))
        if rows:
            break
    if not rows or len({r.name for r in rows}) != len(rows):
        raise DataUnavailable("Missing or duplicate Damodaran rows")
    return BenchmarkTable(
        dataset=dataset,
        as_of=as_of,
        analysis_on=analysis_on,
        available_on=available,
        retrieved_on=retrieved,
        ref=ref,
        version=version,
        rows=tuple(rows),
        warnings=tuple(warnings),
    )


class DamodaranClient:
    def __init__(
        self,
        config: AppConfig,
        *,
        offline: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.offline = offline
        self.cache = SnapshotCache(config.settings.raw_cache_dir / "damodaran")
        self.http = httpx.Client(timeout=30, trust_env=False, transport=transport)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.http.close()

    def get_dataset(
        self, dataset: Dataset, as_of: date, *, refresh: bool = False
    ) -> BenchmarkTable:
        if dataset not in FILES or as_of > date.today():
            raise DataUnavailable("Unsupported Damodaran dataset or future cutoff")
        key = BASE + "datafile/" + FILES[dataset] + ".html"
        versions = self.cache.versions(key)
        fetch_error = None
        if not self.offline and (
            refresh or not versions or versions[0]["retrieved_at"][:10] != str(date.today())
        ):
            try:
                document = get_response(self.http, key).text
                index = get_response(self.http, BASE + "datacurrent.html").text
                self.cache.put(key, {"document": document, "index": index})
                versions = self.cache.versions(key)
            except DataUnavailable as error:
                fetch_error = str(error)
        for saved in versions:
            try:
                result = parse_table(
                    dataset,
                    saved["body"]["document"],
                    saved["body"]["index"],
                    date.fromisoformat(saved["retrieved_at"][:10]),
                    as_of,
                    saved["sha256"],
                )
                if fetch_error:
                    result = result.model_copy(
                        update={
                            "warnings": (*result.warnings, "Using cached version: " + fetch_error)
                        }
                    )
                return result
            except (DataUnavailable, ValueError, KeyError, TypeError):
                continue
        raise DataUnavailable(fetch_error or f"No eligible saved {dataset} version for {as_of}")

    def get_classification(self, *, refresh: bool = False) -> tuple[list[dict[str, str]], str]:
        """US company-to-industry rows and their retrieval date (current, not historical)."""
        versions = self.cache.versions(INDUSTRY_URL)
        if not self.offline and (refresh or not versions):
            # About 8 MB: allow more time than the small HTML tables need.
            content = get_response(self.http, INDUSTRY_URL, timeout=180).content
            self.cache.put(INDUSTRY_URL, {"rows": parse_classification(content)})
            versions = self.cache.versions(INDUSTRY_URL)
        if not versions:
            raise DataUnavailable("Damodaran industry classification is not cached")
        return versions[0]["body"]["rows"], versions[0]["retrieved_at"][:10]

    def industry_for(self, ticker: str, sic: str | None, betas: BenchmarkTable) -> IndustryMatch:
        """Industry label usable with the industry tables, with how it was chosen."""
        rows, retrieved = self.get_classification()
        match = match_industry(rows, ticker, sic, {r.name for r in betas.rows})
        return IndustryMatch(
            industry=match.industry,
            method=match.method,
            ticker=match.ticker,
            sic=match.sic,
            alternatives=match.alternatives,
            ref=match.ref,
            note=f"{match.note}; classification retrieved {retrieved}, not point-in-time",
        )

    @staticmethod
    def select(table: BenchmarkTable, name: str) -> BenchmarkRow:
        rows = [r for r in table.rows if r.name.casefold() == name.strip().casefold()]
        if len(rows) != 1:
            raise DataUnavailable(
                f"No exact {table.dataset} row for {name}; specify a source label"
            )
        return rows[0]
