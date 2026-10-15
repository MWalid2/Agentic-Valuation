"""Read-only SEC evidence tools; no company-specific financial mappings."""

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

from lxml import etree, html
from pydantic import BaseModel, ConfigDict

from dcf_agent.data.sec import Fact, Filing, SECClient, SECError, normalize_cik


class EvidenceCell(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    cell_id: str
    label: str
    value: float
    unit: Literal["USD", "shares"] = "USD"
    period_start: date | None = None
    share_class: str | None = None
    tag: str
    period_end: date
    filed: date
    accession: str
    ref: str
    source: Literal["sec_filing_text", "sec_xbrl"] = "sec_filing_text"
    section: Literal[
        "balance_sheet", "debt_note", "income_statement", "cash_flow", "shares", "disclosures"
    ]


def extract_cells(
    document: str,
    filing: Filing,
    cik: str,
    as_of: date,
    section: Literal["balance_sheet", "debt_note"],
) -> tuple[EvidenceCell, ...]:
    """Keep original labels and tags, with validated instant/entity/unit contexts.

    Segment and instrument dimensions are excluded rather than summed blindly.
    """
    cik = normalize_cik(cik)
    if filing.filed > as_of or filing.report_date is None or filing.report_date > as_of:
        raise SECError("Filing has no eligible reporting date")
    try:
        tree = html.fromstring(
            document.encode(), parser=html.HTMLParser(encoding="utf-8", no_network=True)
        )
    except (ValueError, etree.ParserError):
        raise SECError("SEC filing could not be parsed") from None
    nodes = [n for n in tree.iter() if isinstance(n.tag, str)]

    def local(tag: str) -> str:
        return tag.lower().rsplit("}", 1)[-1].rsplit(":", 1)[-1]

    def text(node: etree._Element) -> str:
        return " ".join("".join(str(t) for t in node.itertext()).split())

    contexts: set[str] = set()
    units: set[str] = set()
    for node in nodes:
        if local(node.tag) == "context":
            children = {local(c.tag): c for c in node.iter() if isinstance(c.tag, str)}
            if any(k in children for k in ("segment", "scenario", "explicitmember", "typedmember")):
                continue
            identifier, instant = children.get("identifier"), children.get("instant")
            if identifier is None or instant is None:
                continue
            if (
                text(identifier).zfill(10) == cik
                and text(instant) == filing.report_date.isoformat()
            ):
                contexts.add(node.get("id", ""))
        elif local(node.tag) == "unit":
            unit_children = [c for c in node.iter() if isinstance(c.tag, str)]
            measures = [c for c in unit_children if local(c.tag) == "measure"]
            if (
                len(measures) == 1
                and text(measures[0]).rsplit(":", 1)[-1] == "USD"
                and not any(local(c.tag) == "divide" for c in unit_children)
            ):
                units.add(node.get("id", ""))
    ref = (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
        f"{filing.accession.replace('-', '')}/{filing.primary_document}"
    )
    cells: dict[tuple[str, str], EvidenceCell] = {}
    for table in tree.iter("table"):
        table_text = text(table).lower()
        is_balance = all(w in table_text for w in ("total assets", "liabilities", "equity"))
        if section == "balance_sheet" and not is_balance:
            continue
        if section == "debt_note" and (
            is_balance
            or not any(
                w in table_text
                for w in ("term debt", "borrowings", "commercial paper", "total debt")
            )
        ):
            continue
        liabilities = section == "debt_note"
        for row in table.iter("tr"):
            columns = [c for c in row if isinstance(c.tag, str) and local(c.tag) in ("td", "th")]
            if not columns:
                continue
            label = text(columns[0])
            lower = label.lower()
            if is_balance:
                if "liabilities" in lower:
                    liabilities = True
                if "equity" in lower and "liabilities" not in lower:
                    liabilities = False
            if not liabilities:
                continue
            for node in row.iter():
                if not isinstance(node.tag, str) or local(node.tag) != "nonfraction":
                    continue
                if (
                    node.get("contextref", "") not in contexts
                    or node.get("unitref", "") not in units
                ):
                    continue
                transform = node.get("format", "").rsplit(":", 1)[-1].lower()
                if transform not in (
                    "",
                    "num-dot-decimal",
                    "numdotdecimal",
                    "fixed-zero",
                    "zerodash",
                ):
                    continue
                number = text(node).replace(",", "").replace(" ", "")
                try:
                    value = Decimal(0) if number in ("—", "–", "-") else Decimal(number.strip("()"))
                    if node.get("sign") == "-" or number.startswith("("):
                        value = -abs(value)
                    scale = int(node.get("scale", "0"))
                    if not value.is_finite() or abs(scale) > 12:
                        continue
                    value *= Decimal(10) ** scale
                except (InvalidOperation, ValueError):
                    continue
                tag = node.get("name", "")
                key = (label, tag)
                cell_id = ("B" if section == "balance_sheet" else "N") + str(len(cells) + 1)
                cell = EvidenceCell(
                    cell_id=cell_id,
                    label=label,
                    value=float(value),
                    tag=tag,
                    period_end=filing.report_date,
                    filed=filing.filed,
                    accession=filing.accession,
                    ref=ref + ("#" + (node.get("id") or "") if node.get("id") else ""),
                    section=section,
                )
                if key in cells and cells[key].value != cell.value:
                    raise SECError("Conflicting values in the same filing row")
                if key in cells:
                    continue
                cells[key] = cell
    return tuple(cells.values())


def debt_candidate(cell: EvidenceCell) -> bool:
    label = cell.label.lower()
    if any(
        w in label
        for w in (
            "interest",
            "fair value",
            "principal",
            "securities",
            "receivable",
            "operating lease",
        )
    ):
        return False
    return (
        any(
            w in label
            for w in (
                "debt",
                "borrowings",
                "commercial paper",
                "notes payable",
                "loans payable",
                "loans and notes",
            )
        )
        and not any(w in label for w in ("maturities",))
    ) or "current maturities of long-term debt" in label


class EDGARTools:
    """Bound to one company, cutoff and filing; the model cannot supply URLs."""

    def __init__(
        self, client: SECClient, cik: str, as_of: date, *, period_end: date | None = None
    ) -> None:
        self.client, self.cik, self.as_of = client, normalize_cik(cik), as_of
        filings = [
            f
            for f in client.get_submissions(cik, as_of)
            if f.form in ("10-K", "10-Q", "10-K/A", "10-Q/A")
            and f.report_date is not None
            and f.report_date <= as_of
            and (period_end is None or f.report_date == period_end)
        ]
        if not filings:
            raise SECError("No eligible filing for the requested reporting period")
        self.filing = max(filings, key=lambda f: (f.report_date or date.min, f.filed, f.accession))
        self.document: str | None = None
        self.reconciliation_facts: tuple[Fact, ...] | None = None
        self.audit: list[str] = ["list_filings"]

    def read(self, section: Literal["balance_sheet", "debt_note"]) -> tuple[EvidenceCell, ...]:
        if self.document is None:
            self.document = self.client.get_filing_document(self.cik, self.filing, self.as_of)
        self.audit.append("read_" + section)
        return extract_cells(self.document, self.filing, self.cik, self.as_of, section)

    def reconcile(self, cells: tuple[EvidenceCell, ...]) -> tuple[str, ...]:
        self.audit.append("query_xbrl")
        facts = self.reconciliation_facts
        if facts is None:
            facts = self.client.get_company_facts(self.cik, self.as_of)
        warnings = []
        for cell in cells:
            rows = [
                f
                for f in facts
                if f.start == cell.period_start
                and f.end == cell.period_end
                and f.accession == cell.accession
                and f.namespace + ":" + f.tag == cell.tag
            ]
            if rows and any(f.unit != cell.unit or f.value != cell.value for f in rows):
                raise SECError("Filing amount disagrees with XBRL for the same accession/context")
            if not rows:
                warnings.append(
                    f"{cell.cell_id}: no companyfacts counterpart; filing evidence only"
                )
        return tuple(warnings)
