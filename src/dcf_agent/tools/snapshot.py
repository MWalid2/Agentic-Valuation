"""Full statement evidence without company-specific financial mappings."""

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal, cast

from lxml import etree, html

from dcf_agent.data.sec import Fact, SECError
from dcf_agent.tools.edgar import EDGARTools, EvidenceCell

Section = Literal["balance_sheet", "income_statement", "cash_flow", "shares", "disclosures"]


def read_filing_facts(
    tools: EDGARTools, statements: dict[Section, tuple[EvidenceCell, ...]] | None = None
) -> tuple[Fact, ...]:
    """Fallback when companyfacts omits a filing; preserve inline numeric contexts."""
    cells = [
        c
        for section in ("balance_sheet", "income_statement", "cash_flow", "disclosures")
        for c in (statements[section] if statements is not None else read_statement(tools, section))
    ]
    if not tools.document:
        raise SECError("No filing document for historical fallback")
    tree = html.fromstring(
        tools.document.encode(), parser=html.HTMLParser(encoding="utf-8", no_network=True)
    )
    years = {
        "".join(str(t) for t in n.itertext()).strip()
        for n in tree.iter()
        if isinstance(n.tag, str) and n.get("name") == "dei:DocumentFiscalYearFocus"
    }
    periods = {
        "".join(str(t) for t in n.itertext()).strip()
        for n in tree.iter()
        if isinstance(n.tag, str) and n.get("name") == "dei:DocumentFiscalPeriodFocus"
    }
    if len(years) != 1 or not next(iter(years)).isdigit() or len(periods) != 1:
        raise SECError("Filing lacks unique fiscal year/period metadata")
    year, period = int(next(iter(years))), next(iter(periods))
    if not 1900 <= year <= 9999 or period not in ("FY", "Q1", "Q2", "Q3", "Q4"):
        raise SECError("Unsupported fiscal year/period metadata")
    results = {}
    for cell in cells:
        if not cell.tag.startswith("us-gaap:"):
            continue
        key = (cell.tag, cell.period_start, cell.period_end, cell.unit, cell.value)
        results[key] = Fact(
            namespace="us-gaap",
            tag=cell.tag.split(":", 1)[1],
            unit=cell.unit,
            value=cell.value,
            start=cell.period_start,
            end=cell.period_end,
            filed=cell.filed,
            accession=cell.accession,
            form=tools.filing.form,
            fiscal_year=year,
            fiscal_period=period,
            original_tag=cell.tag,
            document_ref=cell.ref,
        )
    if not results:
        raise SECError("No supported statement facts in fallback filing")
    return tuple(results.values())


def read_statement(tools: EDGARTools, section: Section) -> tuple[EvidenceCell, ...]:
    if tools.document is None:
        tools.document = tools.client.get_filing_document(tools.cik, tools.filing, tools.as_of)
    tools.audit.append("read_" + section)
    try:
        tree = html.fromstring(
            tools.document.encode(), parser=html.HTMLParser(encoding="utf-8", no_network=True)
        )
    except (ValueError, etree.ParserError):
        raise SECError("Filing statements could not be parsed") from None

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1].lower()

    def text(node: etree._Element) -> str:
        return " ".join("".join(str(t) for t in node.itertext()).split())

    nodes = [n for n in tree.iter() if isinstance(n.tag, str)]
    contexts: dict[str, tuple[date | None, date, str | None]] = {}
    units: dict[str, Literal["USD", "shares"]] = {}
    for node in nodes:
        if local(node.tag) == "context":
            descendants = [c for c in node.iter() if isinstance(c.tag, str)]
            by_name = {local(c.tag): c for c in descendants}
            identifier = by_name.get("identifier")
            if identifier is None or text(identifier).zfill(10) != tools.cik:
                continue
            member = None
            if any(k in by_name for k in ("segment", "scenario", "explicitmember", "typedmember")):
                members = [c for c in descendants if local(c.tag) == "explicitmember"]
                if (
                    section != "shares"
                    or len(members) != 1
                    or "typedmember" in by_name
                    or "scenario" in by_name
                    or members[0].get("dimension") != "us-gaap:StatementClassOfStockAxis"
                ):
                    continue
                member = text(members[0])
            try:
                instant = by_name.get("instant")
                end_node = instant if instant is not None else by_name["enddate"]
                end = date.fromisoformat(text(end_node))
                start_node = by_name.get("startdate")
                start = date.fromisoformat(text(start_node)) if start_node is not None else None
                if end > tools.as_of or (start is not None and start > end):
                    continue
                if section != "shares" and end != tools.filing.report_date:
                    continue
                if section in ("balance_sheet", "shares") and start is not None:
                    continue
                if section in ("income_statement", "cash_flow") and start is None:
                    continue
                contexts[node.get("id", "")] = (start, end, member)
            except (KeyError, ValueError):
                continue
        elif local(node.tag) == "unit":
            descendants = [c for c in node.iter() if isinstance(c.tag, str)]
            measures = [c for c in descendants if local(c.tag) == "measure"]
            if len(measures) != 1 or any(local(c.tag) == "divide" for c in descendants):
                continue
            unit_name = text(measures[0]).rsplit(":", 1)[-1]
            if unit_name in ("USD", "shares"):
                units[node.get("id", "")] = cast(Literal["USD", "shares"], unit_name)
    ref = (
        f"https://www.sec.gov/Archives/edgar/data/{int(tools.cik)}/"
        f"{tools.filing.accession.replace('-', '')}/{tools.filing.primary_document}"
    )
    results: dict[tuple[str, str, date | None, date, str | None], EvidenceCell] = {}
    tables = [tree] if section in ("shares", "disclosures") else list(tree.iter("table"))
    for table in tables:
        content = text(table).lower()
        if section == "balance_sheet" and not all(
            w in content for w in ("total assets", "liabilities", "equity")
        ):
            continue
        if section == "income_statement" and not (
            any(w in content for w in ("revenue", "net sales"))
            and "net income" in content
            and any(w in content for w in ("cost of", "costs and expenses"))
        ):
            continue
        if section == "cash_flow" and not (
            all(
                w in content for w in ("investing activities", "financing activities", "net income")
            )
            and any(w in content for w in ("operating activities", "by operations"))
        ):
            continue
        for node in table.iter():
            if not isinstance(node.tag, str) or local(node.tag) != "nonfraction":
                continue
            context = contexts.get(node.get("contextref", ""))
            unit = units.get(node.get("unitref", ""))
            if (
                context is None
                or unit is None
                or (
                    section != "disclosures"
                    and unit != ("shares" if section == "shares" else "USD")
                )
            ):
                continue
            tag = node.get("name", "")
            if section == "shares" and tag != "dei:EntityCommonStockSharesOutstanding":
                continue
            row = next((a for a in node.iterancestors() if a.tag == "tr"), None)
            columns = (
                [c for c in row if isinstance(c.tag, str) and c.tag in ("td", "th")]
                if row is not None
                else []
            )
            label = text(columns[0]) if columns else "Cover-page shares outstanding"
            if section == "disclosures" and (not columns or not label):
                label = tag
            if not label and section != "shares":
                continue
            if section == "shares":
                label = "Cover-page shares outstanding" + ("; " + context[2] if context[2] else "")
            transform = node.get("format", "").rsplit(":", 1)[-1].lower()
            if transform not in ("", "num-dot-decimal", "numdotdecimal", "fixed-zero", "zerodash"):
                continue
            number = (
                "".join(str(t) for t in node.itertext() if t is not None)
                .strip()
                .replace(",", "")
                .replace("\u00a0", "")
            )
            try:
                amount = Decimal(0) if number in ("—", "–", "-") else Decimal(number.strip("()"))
                if node.get("sign") == "-" or number.startswith("("):
                    amount = -abs(amount)
                scale = int(node.get("scale", "0"))
                if abs(scale) > 12 or not amount.is_finite():
                    continue
                amount *= Decimal(10) ** scale
            except (InvalidOperation, ValueError):
                continue
            start, end, member = context
            key = (label, tag, start, end, member)
            if key in results:
                if results[key].value != float(amount):
                    raise SECError("Conflicting statement values in one context")
                continue
            prefix = {
                "balance_sheet": "B",
                "income_statement": "I",
                "cash_flow": "C",
                "shares": "S",
                "disclosures": "D",
            }[section]
            results[key] = EvidenceCell(
                cell_id=prefix + str(len(results) + 1),
                label=label,
                value=float(amount),
                unit=unit,
                tag=tag,
                period_start=start,
                period_end=end,
                share_class=member,
                filed=tools.filing.filed,
                accession=tools.filing.accession,
                ref=ref + ("#" + node.get("id", "") if node.get("id") else ""),
                section=section,
            )
        if results and section != "shares":
            break
    if section == "shares" and results:
        latest = max(c.period_end for c in results.values())
        return tuple(c for c in results.values() if c.period_end == latest)
    return tuple(results.values())
