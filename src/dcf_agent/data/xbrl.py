"""Filing-level XBRL packages: issuer-authored statements, calculations and extensions.

Statements are identified from the filing's own structure (role definitions and
anchor concepts), not from English table wording. Extension elements are kept
alongside standard ones. No issuer names, tickers or expected amounts appear here.
"""

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Literal, cast

from lxml import etree

from dcf_agent.data.sec import Fact, Filing, SECClient, SECError, normalize_cik

LINK = "http://www.xbrl.org/2003/linkbase"
XLINK = "http://www.w3.org/1999/xlink"
XBRLI = "http://www.xbrl.org/2003/instance"
XSD = "http://www.w3.org/2001/XMLSchema"
XSI = "http://www.w3.org/2001/XMLSchema-instance"
SUMMATION = {
    "http://www.xbrl.org/2003/arcrole/summation-item",
    "https://xbrl.org/2023/arcrole/summation-item",
}
# Canonical prefixes for standard taxonomies, independent of an instance's own choices.
STANDARD = (
    (re.compile(r"^http://fasb\.org/us-gaap/"), "us-gaap"),
    (re.compile(r"^http://fasb\.org/srt/"), "srt"),
    (re.compile(r"^http://xbrl\.sec\.gov/([a-z]+)/"), ""),
)
STANDARD_PREFIXES = {"us-gaap", "srt", "dei", "country", "currency", "exch", "naics", "sic"}
STANDARD_PREFIXES |= {"stpr", "ecd", "cyd", "invest", "rr", "spac", "ffd", "snj", "oef"}
# Dimensional scaffolding is presentation structure, not a reported line item.
SCAFFOLDING = ("Axis", "Member", "Domain", "Table", "LineItems")

StatementKind = Literal["balance_sheet", "income_statement", "cash_flow"]
Members = tuple[tuple[str, str], ...]  # Sorted (axis, member) pairs; () is non-dimensional.

ASSETS = "us-gaap:Assets"
LIABILITIES_AND_EQUITY = "us-gaap:LiabilitiesAndStockholdersEquity"
EQUITY_TOTALS = {
    "us-gaap:StockholdersEquity",
    "us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    "us-gaap:Liabilities",
}
OPERATING_CASH = {
    "us-gaap:NetCashProvidedByUsedInOperatingActivities",
    "us-gaap:NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
}
OTHER_CASH = {
    "us-gaap:NetCashProvidedByUsedInInvestingActivities",
    "us-gaap:NetCashProvidedByUsedInInvestingActivitiesContinuingOperations",
    "us-gaap:NetCashProvidedByUsedInFinancingActivities",
    "us-gaap:NetCashProvidedByUsedInFinancingActivitiesContinuingOperations",
}
EARNINGS_PER_SHARE = {
    "us-gaap:EarningsPerShareBasic",
    "us-gaap:EarningsPerShareDiluted",
    "us-gaap:EarningsPerShareBasicAndDiluted",
    "us-gaap:IncomeLossFromContinuingOperationsPerBasicShare",
    "us-gaap:IncomeLossFromContinuingOperationsPerDilutedShare",
    "us-gaap:IncomeLossFromContinuingOperationsPerBasicAndDilutedShare",
}
NET_INCOME = {
    "us-gaap:NetIncomeLoss",
    "us-gaap:ProfitLoss",
    "us-gaap:NetIncomeLossAvailableToCommonStockholdersBasic",
}
COMPREHENSIVE = {
    "us-gaap:ComprehensiveIncomeNetOfTax",
    "us-gaap:ComprehensiveIncomeNetOfTaxIncludingPortionAttributableToNoncontrollingInterest",
}
EQUITY_AXIS = "us-gaap:StatementEquityComponentsAxis"
ROLE_DEFINITION = re.compile(
    r"^\s*[^-]*-\s*(Statement|Disclosure|Document|Schedule)\s*-\s*(.+?)\s*$", re.I
)


@dataclass(frozen=True)
class Element:
    """Schema metadata; known for extension elements, inferred from facts otherwise."""

    concept: str
    period_type: Literal["instant", "duration"] | None = None
    balance: Literal["debit", "credit"] | None = None
    abstract: bool = False


@dataclass(frozen=True)
class Line:
    concept: str
    label: str
    depth: int
    parent: str | None
    abstract: bool
    preferred_label: str | None = None

    @property
    def custom(self) -> bool:
        return self.concept.split(":", 1)[0] not in STANDARD_PREFIXES


@dataclass(frozen=True)
class CalcArc:
    parent: str
    child: str
    weight: float


@dataclass(frozen=True)
class Statement:
    kind: StatementKind
    role: str
    title: str
    lines: tuple[Line, ...]
    calculations: tuple[CalcArc, ...]

    @property
    def concepts(self) -> frozenset[str]:
        return frozenset(line.concept for line in self.lines)


@dataclass(frozen=True)
class CalcResult:
    parent: str
    start: date | None
    end: date
    reported: float
    computed: float
    tolerance: float
    # Declared children without a non-dimensional value (e.g. presented only by member).
    missing: tuple[str, ...] = ()

    @property
    def consistent(self) -> bool:
        return abs(self.reported - self.computed) <= self.tolerance

    @property
    def status(self) -> Literal["consistent", "incomplete", "inconsistent"]:
        if self.consistent:
            return "consistent"
        return "incomplete" if self.missing else "inconsistent"


@dataclass(frozen=True)
class XBRLPackage:
    cik: str
    filing: Filing
    company_prefix: str | None
    fiscal_year: int
    fiscal_period: str
    facts: tuple[Fact, ...]
    decimals: dict[tuple[str, date | None, date, str], int | None]
    elements: dict[str, Element]
    labels: dict[str, dict[str, str]]
    statements: dict[StatementKind, Statement]
    issues: dict[str, str] = field(default_factory=dict)
    # Values by explicit dimension members, e.g. revenue by product on the face statement.
    dimensional: dict[tuple[str, date | None, date, str, Members], float] = field(
        default_factory=dict
    )
    # Extensible enumerations name statement lines, e.g. where lease liabilities are included.
    enumerations: dict[tuple[str, date | None, date], tuple[str, ...]] = field(default_factory=dict)
    # Monetary units used anywhere in the instance, and accounting taxonomies of its facts.
    currencies: frozenset[str] = frozenset()
    taxonomies: frozenset[str] = frozenset()
    index: dict[tuple[str, date | None, date, str], float] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        for f in self.facts:
            self.index[(f"{f.namespace}:{f.tag}", f.start, f.end, f.unit)] = f.value

    def value(self, concept: str, start: date | None, end: date, unit: str = "USD") -> float | None:
        return self.index.get((concept, start, end, unit))

    def members(
        self, concept: str, start: date | None, end: date, unit: str = "USD"
    ) -> dict[Members, float]:
        return {
            key[4]: value
            for key, value in self.dimensional.items()
            if key[:4] == (concept, start, end, unit)
        }

    def check_calculations(
        self, kind: StatementKind, end: date | None = None
    ) -> tuple[CalcResult, ...]:
        """Issuer-declared sums at one period end (default: the report date)."""
        statement = self.statements[kind]
        end = end or self.filing.report_date
        children: dict[str, list[CalcArc]] = {}
        for arc in statement.calculations:
            children.setdefault(arc.parent, []).append(arc)
        intervals = {
            (f.start, f.end)
            for f in self.facts
            if f.end == end and (f.start is None) == (kind == "balance_sheet")
        }
        results = []
        for parent, arcs in children.items():
            for start, period_end in sorted(intervals, key=lambda i: (i[0] or date.min, i[1])):
                reported = self.value(parent, start, period_end)
                parts = [
                    (arc, value)
                    for arc in arcs
                    if (value := self.value(arc.child, start, period_end)) is not None
                ]
                if reported is None or not parts:
                    continue
                # Rounding: half a unit of the coarsest reported precision per summed item.
                precision = [
                    self.decimals.get((c, start, period_end, "USD"))
                    for c in (parent, *(arc.child for arc, _ in parts))
                ]
                coarsest = min((d for d in precision if d is not None), default=0)
                tolerance = 0.5 * 10 ** (-coarsest) * len(parts) + 1e-6
                computed = sum(arc.weight * (v or 0.0) for arc, v in parts)
                present = {arc.child for arc, _ in parts}
                missing = tuple(arc.child for arc in arcs if arc.child not in present)
                results.append(
                    CalcResult(parent, start, period_end, reported, computed, tolerance, missing)
                )
        return tuple(results)


def _parse(text: str) -> etree._Element:
    parser = etree.XMLParser(
        resolve_entities=False, no_network=True, remove_comments=True, huge_tree=True
    )
    try:
        return etree.fromstring(text.encode("utf-8"), parser)
    except etree.XMLSyntaxError:
        raise SECError("Malformed XBRL package file") from None


def _attr(node: etree._Element, namespace: str, name: str) -> str:
    return str(node.get(f"{{{namespace}}}{name}") or "")


def _local(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _text(node: etree._Element) -> str:
    return " ".join("".join(str(t) for t in node.itertext()).split())


def resolve_duplicates(
    observations: list[tuple[float, int | None, str]],
) -> tuple[float, int | None, str] | None:
    """XBRL duplicate rule: values must agree at the coarsest precision; keep the most precise.

    A statement in millions and a note rounded to billions are consistent duplicates.
    decimals=None means exact (INF or unspecified).
    """
    coarsest = min((d for _, d, _ in observations if d is not None), default=None)
    if coarsest is not None and len({round(v, coarsest) for v, _, _ in observations}) != 1:
        return None
    finest = max(observations, key=lambda o: float("inf") if o[1] is None else o[1])
    same_precision = {v for v, d, _ in observations if d == finest[1]}
    return finest if len(same_precision) == 1 else None


def classify_statement(concepts: Iterable[str]) -> StatementKind | None:
    """Anchor concepts identify a primary statement regardless of the issuer's wording."""
    names = set(concepts)
    if names & OPERATING_CASH and names & OTHER_CASH:
        return "cash_flow"
    if ASSETS in names and (LIABILITIES_AND_EQUITY in names or names & EQUITY_TOTALS):
        return "balance_sheet"
    # EPS is required on the face of the income statement and absent from equity
    # statements, so it outranks an equity-components axis some issuers attach.
    if names & EARNINGS_PER_SHARE:
        return "income_statement"
    if EQUITY_AXIS in names:
        return None  # Statement of stockholders' equity (or deficit).
    if names & NET_INCOME and not names & COMPREHENSIVE:
        return "income_statement"
    return None


def parse_package(
    cik: str, filing: Filing, documents: dict[str, str], primary_url: str | None = None
) -> XBRLPackage:
    """Parse an instance, schemas and linkbases already retrieved for one filing."""
    cik = normalize_cik(cik)
    if filing.report_date is None:
        raise SECError("Filing has no reporting date")
    trees = {name: _parse(text) for name, text in documents.items()}
    instances = [t for t in trees.values() if _local(t.tag) == "xbrl"]
    schemas = [t for t in trees.values() if _local(t.tag) == "schema"]
    if len(instances) != 1:
        raise SECError("Expected exactly one XBRL instance")
    instance = instances[0]
    issues: dict[str, str] = {}

    # Extension elements and role definitions come from the issuer's schema(s).
    namespaces = {uri: prefix for prefix, uri in instance.nsmap.items() if prefix}
    company_prefix: str | None = None
    by_id: dict[str, str] = {}
    elements: dict[str, Element] = {}
    roles: dict[str, str] = {}
    for schema in schemas:
        target = schema.get("targetNamespace", "")
        prefix = namespaces.get(target)
        if prefix is None:
            issues["schema:" + target] = "Schema namespace is not used by the instance"
            continue
        company_prefix = company_prefix or prefix
        for node in schema.iter(f"{{{XSD}}}element"):
            name = node.get("name")
            if not name:
                continue
            concept = f"{prefix}:{name}"
            if node.get("id"):
                by_id[str(node.get("id"))] = concept
            period_type = node.get("{http://www.xbrl.org/2003/instance}periodType")
            balance = node.get("{http://www.xbrl.org/2003/instance}balance")
            elements[concept] = Element(
                concept=concept,
                period_type=period_type if period_type in ("instant", "duration") else None,  # type: ignore[arg-type]
                balance=balance if balance in ("debit", "credit") else None,  # type: ignore[arg-type]
                abstract=node.get("abstract") == "true",
            )
        for node in schema.iter(f"{{{LINK}}}roleType"):
            definition = node.find(f"{{{LINK}}}definition")
            if node.get("roleURI") and definition is not None:
                roles[str(node.get("roleURI"))] = _text(definition)

    def concept_from_href(href: str) -> str | None:
        base, _, fragment = href.partition("#")
        if fragment in by_id and "://" not in base:
            return by_id[fragment]
        prefix, separator, name = fragment.partition("_")
        return f"{prefix}:{name}" if separator and prefix and name else None

    # Linkbases may be separate files or embedded in a schema; read them wherever they are.
    presentation: dict[str, list[tuple[str, str, float, str | None]]] = {}
    calculation: dict[str, list[CalcArc]] = {}
    labels: dict[str, dict[str, str]] = {}
    for tree in trees.values():
        for link_kind in ("presentationLink", "calculationLink", "labelLink"):
            for link in tree.iter(f"{{{LINK}}}{link_kind}"):
                role = _attr(link, XLINK, "role")
                locators: dict[str, str] = {}
                resources: dict[str, list[tuple[str, str]]] = {}
                for node in link:
                    local = _local(node.tag)
                    if local == "loc":
                        located = concept_from_href(_attr(node, XLINK, "href"))
                        if located is not None:
                            locators[_attr(node, XLINK, "label")] = located
                    elif local == "label":
                        lang = node.get("{http://www.w3.org/XML/1998/namespace}lang", "")
                        if lang.lower().startswith("en"):
                            resources.setdefault(_attr(node, XLINK, "label"), []).append(
                                (_attr(node, XLINK, "role"), _text(node))
                            )
                for arc in link:
                    local = _local(arc.tag)
                    source = locators.get(_attr(arc, XLINK, "from"))
                    target_label = _attr(arc, XLINK, "to")
                    if source is None:
                        continue
                    if local == "labelArc":
                        for label_role, text in resources.get(target_label, []):
                            labels.setdefault(source, {})[label_role.rsplit("/", 1)[-1]] = text
                        continue
                    child = locators.get(target_label)
                    if child is None:
                        continue
                    if local == "presentationArc":
                        preferred = arc.get("preferredLabel")
                        presentation.setdefault(role, []).append(
                            (
                                source,
                                child,
                                float(arc.get("order", "0") or 0),
                                preferred.rsplit("/", 1)[-1] if preferred else None,
                            )
                        )
                    elif local == "calculationArc" and _attr(arc, XLINK, "arcrole") in SUMMATION:
                        try:
                            weight = float(arc.get("weight", ""))
                        except ValueError:
                            issues[f"calculation:{role}:{source}"] = "Invalid calculation weight"
                            continue
                        calculation.setdefault(role, []).append(CalcArc(source, child, weight))

    # Instance: entity-matched, non-dimensional numeric facts in USD or shares.
    contexts: dict[str, tuple[date | None, date, Members]] = {}
    entities: set[str] = set()
    for node in instance.iter(f"{{{XBRLI}}}context"):
        identifier = node.find(f"{{{XBRLI}}}entity/{{{XBRLI}}}identifier")
        if identifier is not None:
            entities.add(_text(identifier).zfill(10))
        if identifier is None or _text(identifier).zfill(10) != cik:
            continue
        if node.find(f"{{{XBRLI}}}scenario") is not None:
            continue
        segment = node.find(f"{{{XBRLI}}}entity/{{{XBRLI}}}segment")
        members: Members = ()
        if segment is not None:
            explicit = [c for c in segment if _local(c.tag) == "explicitMember"]
            if not explicit or len(explicit) != len(segment):
                continue  # Typed or unrecognized dimensions are not interpreted.
            members = tuple(sorted((str(c.get("dimension", "")), _text(c)) for c in explicit))
        period_node = node.find(f"{{{XBRLI}}}period")
        if period_node is None:
            continue
        try:
            instant_node = period_node.find(f"{{{XBRLI}}}instant")
            if instant_node is not None:
                instant = date.fromisoformat(_text(instant_node))
                contexts[node.get("id", "")] = (None, instant, members)
                continue
            start_node = period_node.find(f"{{{XBRLI}}}startDate")
            end_node = period_node.find(f"{{{XBRLI}}}endDate")
            if start_node is not None and end_node is not None:
                interval = (
                    date.fromisoformat(_text(start_node)),
                    date.fromisoformat(_text(end_node)),
                )
                if interval[0] <= interval[1]:
                    contexts[node.get("id", "")] = (*interval, members)
        except ValueError:
            continue
    if cik not in entities:
        # E.g. a successor registrant listing its predecessor's filing after a reorganization.
        raise SECError(
            "Instance reports entity " + ", ".join(sorted(entities)) + ", not the requested "
            "company; check for a predecessor or successor registrant"
        )
    units: dict[str, Literal["USD", "shares"]] = {}
    currencies: set[str] = set()
    for node in instance.iter(f"{{{XBRLI}}}unit"):
        measures = [m for m in node.iter(f"{{{XBRLI}}}measure")]
        if len(measures) != 1 or node.find(f"{{{XBRLI}}}divide") is not None:
            continue
        raw_measure = _text(measures[0])
        measure = raw_measure.rsplit(":", 1)[-1]
        if raw_measure.startswith("iso4217:"):
            currencies.add(measure)
        if measure == "USD":
            units[node.get("id", "")] = "USD"
        elif measure == "shares":
            units[node.get("id", "")] = "shares"

    def prefix_for(uri: str) -> str | None:
        for pattern, canonical in STANDARD:
            match = pattern.match(uri)
            if match:
                return canonical or match.group(1)
        return namespaces.get(uri)

    document_info: dict[str, set[str]] = {}
    taxonomies: set[str] = set()
    numeric: dict[tuple[str, date | None, date, str], list[tuple[float, int | None, str]]] = {}
    by_member: dict[
        tuple[str, date | None, date, str, Members], list[tuple[float, int | None, str]]
    ] = {}
    enumerations: dict[tuple[str, date | None, date], tuple[str, ...]] = {}
    decimals: dict[tuple[str, date | None, date, str], int | None] = {}
    references: dict[tuple[str, date | None, date, str], str] = {}
    for node in instance:
        if not isinstance(node.tag, str) or node.get("contextRef") is None:
            continue
        uri, _, name = node.tag[1:].partition("}")
        if "ifrs.org" in uri:
            taxonomies.add("ifrs")
        elif "fasb.org/us-gaap" in uri:
            taxonomies.add("us-gaap")
        prefix = prefix_for(uri)
        context = contexts.get(str(node.get("contextRef")))
        if prefix is None or context is None or node.get(f"{{{XSI}}}nil") == "true":
            continue
        concept = f"{prefix}:{name}"
        if node.get("unitRef") is None:
            if prefix == "dei" and not context[2]:
                document_info.setdefault(name, set()).add(_text(node))
            elif name.endswith(("ExtensibleList", "ExtensibleEnumeration")) and not context[2]:
                named = []
                for item in _text(node).split():
                    namespace, _, local = item.partition("#")
                    element_prefix = prefix_for(namespace)
                    if element_prefix and local:
                        named.append(f"{element_prefix}:{local}")
                enumerations[(concept, context[0], context[1])] = tuple(named)
            continue
        unit = units.get(str(node.get("unitRef", "")))
        if unit is None:
            continue
        try:
            amount = float(_text(node))
        except ValueError:
            continue
        raw_decimals = node.get("decimals", "")
        try:
            precision = None if raw_decimals in ("", "INF") else int(raw_decimals)
        except ValueError:
            continue
        observation = (amount, precision, str(node.get("id") or ""))
        if context[2]:
            by_member.setdefault((concept, context[0], context[1], unit, context[2]), []).append(
                observation
            )
        else:
            numeric.setdefault((concept, context[0], context[1], unit), []).append(observation)

    years = document_info.get("DocumentFiscalYearFocus", set())
    periods = document_info.get("DocumentFiscalPeriodFocus", set())
    fiscal_year = int(next(iter(years))) if len(years) == 1 and next(iter(years)).isdigit() else 0
    fiscal_period = next(iter(periods)) if len(periods) == 1 else ""
    if not fiscal_year or fiscal_period not in ("FY", "Q1", "Q2", "Q3", "Q4"):
        issues["dei:fiscal"] = "No unique fiscal year/period focus; calendar must come from dates"
        fiscal_year = fiscal_year or filing.report_date.year
    identifiers = document_info.get("EntityCentralIndexKey", set())
    if identifiers and {i.zfill(10) for i in identifiers} != {cik}:
        raise SECError("Instance entity differs from the requested company")

    facts = []
    folder = (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{filing.accession.replace('-', '')}/"
    )
    for fact_key, observations in numeric.items():
        concept, start, end, fact_unit = fact_key
        chosen = resolve_duplicates(observations)
        if chosen is None:
            issues[f"duplicate:{concept}:{start}/{end}"] = "Inconsistent duplicate facts; excluded"
            continue
        amount, decimals[fact_key], fact_id = chosen
        if fact_id and primary_url:
            references[fact_key] = f"{primary_url}#{fact_id}"
        prefix, name = concept.split(":", 1)
        facts.append(
            Fact(
                namespace=prefix,
                tag=name,
                unit=cast(Literal["USD", "shares"], fact_unit),
                value=amount,
                start=start,
                end=end,
                filed=filing.filed,
                accession=filing.accession,
                form=filing.form,
                fiscal_year=fiscal_year,
                fiscal_period=fiscal_period,
                document_ref=references.get(fact_key, folder),
            )
        )
    dimensional: dict[tuple[str, date | None, date, str, Members], float] = {}
    for member_key, member_observations in by_member.items():
        selected = resolve_duplicates(member_observations)
        if selected is None:
            label = f"{member_key[0]}:{member_key[1]}/{member_key[2]}:{member_key[4]}"
            issues["duplicate:" + label] = "Inconsistent duplicate dimensional facts; excluded"
        else:
            dimensional[member_key] = selected[0]
    for fact in facts:
        concept = f"{fact.namespace}:{fact.tag}"
        if concept not in elements:
            elements[concept] = Element(
                concept=concept, period_type="instant" if fact.start is None else "duration"
            )

    def label_for(concept: str, preferred: str | None) -> str:
        options = labels.get(concept, {})
        return (
            options.get(preferred or "")
            or options.get("terseLabel")
            or options.get("label")
            or concept.split(":", 1)[1]
        )

    def lines_for(role: str) -> tuple[Line, ...]:
        arcs = presentation.get(role, [])
        children: dict[str, list[tuple[float, str, str | None]]] = {}
        targets = set()
        for source, target, order, preferred in arcs:
            children.setdefault(source, []).append((order, target, preferred))
            targets.add(target)
        roots = [s for s in dict.fromkeys(a[0] for a in arcs) if s not in targets]
        result: list[Line] = []

        def visit(
            concept: str, parent: str | None, depth: int, preferred: str | None, path: set[str]
        ) -> None:
            if concept in path:
                return
            name = concept.split(":", 1)[1]
            abstract = (
                elements.get(concept, Element(concept)).abstract
                or name.endswith("Abstract")
                or name.endswith(SCAFFOLDING)
            )
            result.append(
                Line(concept, label_for(concept, preferred), depth, parent, abstract, preferred)
            )
            for _, child, child_preferred in sorted(children.get(concept, [])):
                visit(child, concept, depth + 1, child_preferred, path | {concept})

        for root in roots:
            visit(root, None, 0, None, set())
        return tuple(result)

    statement_roles = [
        role
        for role, definition in sorted(roles.items(), key=lambda r: r[1])
        if (match := ROLE_DEFINITION.match(definition))
        and match.group(1).lower() == "statement"
        and "parenthetical" not in match.group(2).lower()
    ]
    if not statement_roles:
        issues["statements"] = "No role is defined as a Statement; all presentation roles checked"
        statement_roles = sorted(presentation)
    candidates: dict[StatementKind, list[Statement]] = {}
    for role in statement_roles:
        lines = lines_for(role)
        kind = classify_statement(line.concept for line in lines)
        if kind is None:
            continue
        match = ROLE_DEFINITION.match(roles.get(role, ""))
        candidates.setdefault(kind, []).append(
            Statement(
                kind=kind,
                role=role,
                title=match.group(2) if match else role,
                lines=lines,
                calculations=tuple(calculation.get(role, [])),
            )
        )
    statements: dict[StatementKind, Statement] = {}
    reported = {(f"{f.namespace}:{f.tag}", f.end) for f in facts}
    for kind, options in candidates.items():
        if len(options) > 1:
            # Prefer the role with most non-dimensional values at the report date.
            def coverage(statement: Statement) -> int:
                return sum((c, filing.report_date) in reported for c in statement.concepts)

            options = sorted(options, key=coverage, reverse=True)
            issues["statement:" + kind] = (
                "Multiple candidate statements; selected by report-date coverage: "
                + " | ".join(o.title for o in options)
            )
        statements[kind] = options[0]
    for required in ("balance_sheet", "income_statement", "cash_flow"):
        if required not in statements:
            issues["statement:" + required] = "No primary statement identified"
    return XBRLPackage(
        cik=cik,
        filing=filing,
        company_prefix=company_prefix,
        fiscal_year=fiscal_year,
        fiscal_period=fiscal_period,
        facts=tuple(facts),
        decimals=decimals,
        elements=elements,
        labels=labels,
        statements=statements,
        issues=issues,
        dimensional=dimensional,
        enumerations=enumerations,
        currencies=frozenset(currencies),
        taxonomies=frozenset(taxonomies),
    )


LINKBASE = re.compile(r"_(?:cal|pre|lab|def)\.xml$", re.I)


def package_files(names: Iterable[str]) -> tuple[str, ...]:
    """Instance, schemas and the calculation/presentation/label linkbases of a filing."""
    names = tuple(names)
    inline = [n for n in names if n.lower().endswith("_htm.xml")]
    legacy = [
        n
        for n in names
        if n.lower().endswith(".xml")
        and not LINKBASE.search(n)
        and n != "FilingSummary.xml"
        and not n.lower().endswith("_htm.xml")
    ]
    instances = inline or legacy
    if len(instances) != 1:
        raise SECError("Filing does not contain exactly one XBRL instance")
    schemas = [n for n in names if n.lower().endswith(".xsd")]
    linkbases = [n for n in names if re.search(r"_(?:cal|pre|lab)\.xml$", n, re.I)]
    if not schemas:
        raise SECError("Filing has no XBRL schema")
    return (*instances, *schemas, *linkbases)


def read_package(sec: SECClient, cik: str, filing: Filing, as_of: date) -> XBRLPackage:
    """Retrieve (or replay) one filing's XBRL package and parse it."""
    if filing.report_date is None or filing.report_date > as_of:
        raise SECError("Filing has no eligible reporting date")
    names = package_files(sec.get_filing_index(cik, filing, as_of))
    documents = {name: sec.get_filing_file(cik, filing, name, as_of) for name in names}
    primary = (
        f"https://www.sec.gov/Archives/edgar/data/{int(normalize_cik(cik))}/"
        f"{filing.accession.replace('-', '')}/{filing.primary_document}"
    )
    return parse_package(cik, filing, documents, primary)
