"""Synthetic XBRL packages: structural statement identification and extension elements."""

from datetime import date
from pathlib import Path

import httpx
import pytest
from test_sec import config

from dcf_agent.data.sec import Filing, SECClient, SECError
from dcf_agent.data.xbrl import (
    classify_statement,
    package_files,
    parse_package,
    resolve_duplicates,
)

CIK = "0000000001"
FILING = Filing(
    accession="0000000001-26-000001",
    form="10-K",
    filed=date(2026, 2, 15),
    report_date=date(2025, 12, 31),
    primary_document="ex-20251231.htm",
)
GAAP = "https://xbrl.fasb.org/us-gaap/2025/elts/us-gaap-2025.xsd#us-gaap_"
ROLES = {
    "Earnings": "0000002 - Statement - Statements of Earnings",
    "Balance": "0000003 - Statement - Balance Sheets",
    "BalanceParenthetical": "0000004 - Statement - Balance Sheets (Parenthetical)",
    "Cash": "0000005 - Statement - Statements of Cash Flows",
    "Deficit": "0000006 - Statement - Statements of Stockholders' Deficit",
    "Debt": "0000007 - Disclosure - Debt (Details)",
}
NETWORKS = {
    "Earnings": (
        "IncomeStatementAbstract",
        [
            "Revenues",
            "CostOfRevenue",
            "OperatingIncomeLoss",
            "NetIncomeLoss",
            "EarningsPerShareBasic",
        ],
    ),
    "Balance": (
        "StatementOfFinancialPositionAbstract",
        [
            "CashAndCashEquivalentsAtCarryingValue",
            "Assets",
            "ex:RevolverBorrowings",
            "LongTermDebtNoncurrent",
            "Liabilities",
            "StockholdersEquity",
            "LiabilitiesAndStockholdersEquity",
        ],
    ),
    "Cash": (
        "StatementOfCashFlowsAbstract",
        [
            "NetIncomeLoss",
            "NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInInvestingActivities",
        ],
    ),
    "Deficit": (
        "StatementOfStockholdersEquityAbstract",
        ["StatementEquityComponentsAxis", "NetIncomeLoss"],
    ),
    "Debt": ("DebtDisclosureAbstract", ["Assets", "LongTermDebtNoncurrent", "StockholdersEquity"]),
}
CALCULATIONS = {
    "Earnings": [
        ("OperatingIncomeLoss", "Revenues", 1.0),
        ("OperatingIncomeLoss", "CostOfRevenue", -1.0),
    ],
    # Liabilities = revolver + long-term debt; equity is reported negative (a deficit).
    "Balance": [
        ("Liabilities", "ex:RevolverBorrowings", 1.0),
        ("Liabilities", "LongTermDebtNoncurrent", 1.0),
        ("LiabilitiesAndStockholdersEquity", "Liabilities", 1.0),
        ("LiabilitiesAndStockholdersEquity", "StockholdersEquity", 1.0),
    ],
}


def href(name: str) -> str:
    return "ex-20251231.xsd#ex_" + name[3:] if name.startswith("ex:") else GAAP + name


def link(kind: str, arcs: dict[str, list[tuple[str, str, str]]]) -> str:
    """kind is presentation or calculation; arcs[role] = (parent, child, attributes)."""
    body = []
    for role, items in arcs.items():
        names = dict.fromkeys(n for a in items for n in a[:2])
        locs = "".join(
            f'<link:loc xlink:type="locator" xlink:label="{n}" xlink:href="{href(n)}"/>'
            for n in names
        )
        arc_name = "presentationArc" if kind == "presentation" else "calculationArc"
        arcrole = "parent-child" if kind == "presentation" else "summation-item"
        rows = "".join(
            f'<link:{arc_name} xlink:type="arc" xlink:arcrole="http://www.xbrl.org/2003/arcrole/'
            f'{arcrole}" xlink:from="{p}" xlink:to="{c}" {attributes}/>'
            for p, c, attributes in items
        )
        body.append(
            f'<link:{kind}Link xlink:type="extended" xlink:role="http://ex.com/role/{role}">'
            f"{locs}{rows}</link:{kind}Link>"
        )
    return "".join(body)


LINKBASE = (
    '<link:linkbase xmlns:link="http://www.xbrl.org/2003/linkbase" '
    'xmlns:xlink="http://www.w3.org/1999/xlink">{}</link:linkbase>'
)


def presentation() -> str:
    arcs = {
        role: [(root, child, f'order="{i}"') for i, child in enumerate(children)]
        for role, (root, children) in NETWORKS.items()
    }
    return link("presentation", arcs)


def calculation(broken: bool = False) -> str:
    arcs = {
        role: [
            (p, c, f'weight="{-w if broken and c == "CostOfRevenue" else w}"') for p, c, w in items
        ]
        for role, items in CALCULATIONS.items()
    }
    return link("calculation", arcs)


def labels() -> str:
    return (
        '<link:labelLink xlink:type="extended" xlink:role="http://www.xbrl.org/2003/role/link">'
        '<link:loc xlink:type="locator" xlink:label="r" '
        f'xlink:href="{href("ex:RevolverBorrowings")}"/>'
        '<link:label xlink:type="resource" xlink:label="r_lab" xml:lang="en-US" '
        'xlink:role="http://www.xbrl.org/2003/role/label">Borrowings under revolver</link:label>'
        '<link:labelArc xlink:type="arc" '
        'xlink:arcrole="http://www.xbrl.org/2003/arcrole/concept-label" '
        'xlink:from="r" xlink:to="r_lab"/></link:labelLink>'
    )


def schema(roles: dict[str, str] = ROLES, embedded: str = "") -> str:
    role_types = "".join(
        f'<link:roleType roleURI="http://ex.com/role/{r}" id="{r}">'
        f"<link:definition>{d}</link:definition></link:roleType>"
        for r, d in roles.items()
    )
    return (
        '<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema" '
        'xmlns:xbrli="http://www.xbrl.org/2003/instance" '
        'xmlns:link="http://www.xbrl.org/2003/linkbase" '
        'xmlns:xlink="http://www.w3.org/1999/xlink" targetNamespace="http://ex.com/20251231">'
        f"<xs:annotation><xs:appinfo>{role_types}{embedded}</xs:appinfo></xs:annotation>"
        '<xs:element id="ex_RevolverBorrowings" name="RevolverBorrowings" '
        'xbrli:periodType="instant" xbrli:balance="credit" abstract="false"/>'
        "</xs:schema>"
    )


def context(cid: str, period: str, cik: str = CIK, segment: str = "") -> str:
    return (
        f'<context id="{cid}"><entity><identifier scheme="http://www.sec.gov/CIK">{cik}</identifier>'
        f"{segment}</entity><period>{period}</period></context>"
    )


def instance(extra: str = "", cik: str = CIK) -> str:
    year = "<startDate>2025-01-01</startDate><endDate>2025-12-31</endDate>"
    end = "<instant>2025-12-31</instant>"
    segment = (
        '<segment><xbrldi:explicitMember dimension="us-gaap:StatementBusinessSegmentsAxis">'
        "ex:WidgetsMember</xbrldi:explicitMember></segment>"
    )
    values = {
        "us-gaap:Revenues": ("d", 1000),
        "us-gaap:CostOfRevenue": ("d", 600),
        "us-gaap:OperatingIncomeLoss": ("d", 400),
        "us-gaap:NetIncomeLoss": ("d", 300),
        "us-gaap:NetCashProvidedByUsedInOperatingActivities": ("d", 350),
        "us-gaap:NetCashProvidedByUsedInInvestingActivities": ("d", -50),
        "us-gaap:CashAndCashEquivalentsAtCarryingValue": ("i", 100),
        "us-gaap:Assets": ("i", 900),
        "ex:RevolverBorrowings": ("i", 200),
        "us-gaap:LongTermDebtNoncurrent": ("i", 800),
        "us-gaap:Liabilities": ("i", 1000),
        "us-gaap:StockholdersEquity": ("i", -100),
        "us-gaap:LiabilitiesAndStockholdersEquity": ("i", 900),
    }
    facts = "".join(
        f'<{name} contextRef="{c}" unitRef="usd" decimals="0" id="f{i}">{value}</{name}>'
        for i, (name, (c, value)) in enumerate(values.items())
    )
    return (
        '<xbrl xmlns="http://www.xbrl.org/2003/instance" '
        'xmlns:us-gaap="http://fasb.org/us-gaap/2025" '
        'xmlns:dei="http://xbrl.sec.gov/dei/2025" xmlns:ex="http://ex.com/20251231" '
        'xmlns:iso4217="http://www.xbrl.org/2003/iso4217" xmlns:xbrldi="http://xbrl.org/2006/xbrldi">'
        + context("d", year)
        + context("i", end)
        + context("seg", end, segment=segment)
        + context("other", end, cik="0000000002")
        + '<unit id="usd"><measure>iso4217:USD</measure></unit>'
        + '<dei:DocumentFiscalYearFocus contextRef="d">2025</dei:DocumentFiscalYearFocus>'
        + '<dei:DocumentFiscalPeriodFocus contextRef="d">FY</dei:DocumentFiscalPeriodFocus>'
        + f'<dei:EntityCentralIndexKey contextRef="d">{cik}</dei:EntityCentralIndexKey>'
        + facts
        + '<us-gaap:Assets contextRef="seg" unitRef="usd" decimals="0">5</us-gaap:Assets>'
        + '<us-gaap:Assets contextRef="other" unitRef="usd" decimals="0">7</us-gaap:Assets>'
        + extra
        + "</xbrl>"
    )


def documents(**overrides: str) -> dict[str, str]:
    return {
        "ex-20251231_htm.xml": instance(),
        "ex-20251231.xsd": schema(),
        "ex-20251231_pre.xml": LINKBASE.format(presentation()),
        "ex-20251231_cal.xml": LINKBASE.format(calculation()),
        "ex-20251231_lab.xml": LINKBASE.format(labels()),
        **overrides,
    }


def test_statements_identified_by_structure_not_wording():
    package = parse_package(CIK, FILING, documents())
    assert {k: s.title for k, s in package.statements.items()} == {
        "income_statement": "Statements of Earnings",
        "balance_sheet": "Balance Sheets",
        "cash_flow": "Statements of Cash Flows",
    }
    assert package.fiscal_year == 2025 and package.fiscal_period == "FY"
    assert package.company_prefix == "ex"
    assert not any(k.startswith("statement:") for k in package.issues)


def test_extension_element_has_label_metadata_and_value():
    package = parse_package(CIK, FILING, documents())
    line = next(
        line
        for line in package.statements["balance_sheet"].lines
        if line.concept == "ex:RevolverBorrowings"
    )
    assert line.custom and not line.abstract
    assert line.label == "Borrowings under revolver"
    assert line.parent == "us-gaap:StatementOfFinancialPositionAbstract"
    assert package.elements["ex:RevolverBorrowings"].balance == "credit"
    assert package.value("ex:RevolverBorrowings", None, date(2025, 12, 31)) == 200
    # Negative equity (a deficit) is preserved, not rejected.
    assert package.value("us-gaap:StockholdersEquity", None, date(2025, 12, 31)) == -100


def test_dimensional_and_other_entity_facts_are_excluded():
    package = parse_package(CIK, FILING, documents())
    assets = [f for f in package.facts if f.tag == "Assets"]
    assert [f.value for f in assets] == [900]


def test_calculations_detect_inconsistent_totals():
    package = parse_package(CIK, FILING, documents())
    assert all(
        r.consistent for kind in package.statements for r in package.check_calculations(kind)
    )
    broken = parse_package(
        CIK, FILING, documents(**{"ex-20251231_cal.xml": LINKBASE.format(calculation(broken=True))})
    )
    results = broken.check_calculations("income_statement")
    assert [(r.reported, r.computed, r.consistent) for r in results] == [(400, 1600, False)]


def test_inconsistent_duplicates_are_excluded():
    duplicate = '<us-gaap:Revenues contextRef="d" unitRef="usd" decimals="0">999</us-gaap:Revenues>'
    package = parse_package(CIK, FILING, documents(**{"ex-20251231_htm.xml": instance(duplicate)}))
    assert package.value("us-gaap:Revenues", date(2025, 1, 1), date(2025, 12, 31)) is None
    assert "duplicate:us-gaap:Revenues:2025-01-01/2025-12-31" in package.issues


def test_linkbases_embedded_in_schema_are_read():
    docs = documents(**{"ex-20251231.xsd": schema(embedded=LINKBASE.format(presentation()))})
    del docs["ex-20251231_pre.xml"]
    package = parse_package(CIK, FILING, docs)
    assert set(package.statements) == {"income_statement", "balance_sheet", "cash_flow"}


def test_without_statement_roles_all_networks_are_classified_and_flagged():
    roles = {r: d.replace("Statement", "Schedule") for r, d in ROLES.items()}
    package = parse_package(CIK, FILING, documents(**{"ex-20251231.xsd": schema(roles)}))
    assert "statements" in package.issues
    # The debt disclosure also looks like a balance sheet; coverage picks the statement.
    assert package.statements["balance_sheet"].role.endswith("/Balance")
    assert "statement:balance_sheet" in package.issues


def test_predecessor_entity_filing_is_reported_not_silently_empty():
    predecessor = instance().replace(f">{CIK}<", ">0000000003<")
    with pytest.raises(SECError, match="0000000003.*predecessor or successor"):
        parse_package(CIK, FILING, documents(**{"ex-20251231_htm.xml": predecessor}))


def test_entity_mismatch_is_rejected():
    with pytest.raises(SECError, match="entity"):
        parse_package(CIK, FILING, documents(**{"ex-20251231_htm.xml": instance(cik="0000000002")}))


@pytest.mark.parametrize(
    ("concepts", "kind"),
    [
        ({"us-gaap:Assets", "us-gaap:LiabilitiesAndStockholdersEquity"}, "balance_sheet"),
        ({"us-gaap:NetIncomeLoss", "us-gaap:EarningsPerShareDiluted"}, "income_statement"),
        ({"us-gaap:ProfitLoss", "us-gaap:Revenues"}, "income_statement"),
        ({"us-gaap:NetIncomeLoss", "us-gaap:ComprehensiveIncomeNetOfTax"}, None),
        ({"us-gaap:NetIncomeLoss", "us-gaap:StatementEquityComponentsAxis"}, None),
        (
            {
                "us-gaap:NetIncomeLoss",
                "us-gaap:EarningsPerShareBasic",
                "us-gaap:StatementEquityComponentsAxis",
            },
            "income_statement",
        ),
        (
            {
                "us-gaap:NetIncomeLoss",
                "us-gaap:NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
                "us-gaap:NetCashProvidedByUsedInFinancingActivities",
            },
            "cash_flow",
        ),
    ],
)
def test_classification_anchors(concepts, kind):
    assert classify_statement(concepts) == kind


def test_package_file_selection():
    names = [
        "a_htm.xml",
        "a.xsd",
        "a_pre.xml",
        "a_cal.xml",
        "a_lab.xml",
        "a_def.xml",
        "FilingSummary.xml",
    ]
    assert package_files(names) == ("a_htm.xml", "a.xsd", "a_pre.xml", "a_cal.xml", "a_lab.xml")
    legacy = ["a-20181231.xml", "a.xsd", "a_pre.xml", "FilingSummary.xml"]
    assert package_files(legacy)[0] == "a-20181231.xml"
    with pytest.raises(SECError):
        package_files(["a.xsd", "a_pre.xml"])


def test_package_file_names_are_validated_before_any_request(tmp_path: Path):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("No request expected")

    with SECClient(config(tmp_path), transport=httpx.MockTransport(refuse)) as sec:
        for name in ("../secret.xml", "a.htm", "https://x/a.xml"):
            with pytest.raises(SECError):
                sec.get_filing_file(CIK, FILING, name, date(2026, 3, 1))
        with pytest.raises(SECError, match="cutoff"):
            sec.get_filing_file(CIK, FILING, "a.xml", date(2026, 1, 1))


def test_duplicates_at_different_precision_are_consistent():
    # The statement in units and a note rounded to thousands: same fact, keep the precise one.
    rounded = '<us-gaap:Revenues contextRef="d" unitRef="usd" decimals="-3">1000</us-gaap:Revenues>'
    note = '<us-gaap:Assets contextRef="i" unitRef="usd" decimals="-2">900</us-gaap:Assets>'
    package = parse_package(
        CIK, FILING, documents(**{"ex-20251231_htm.xml": instance(rounded + note)})
    )
    assert package.value("us-gaap:Revenues", date(2025, 1, 1), date(2025, 12, 31)) == 1000
    assert not any(k.startswith("duplicate:") for k in package.issues)


@pytest.mark.parametrize(
    ("observations", "expected"),
    [
        ([(9_037e6, -6, "a"), (9.0e9, -8, "b")], (9_037e6, -6, "a")),
        ([(9_037e6, -6, "a"), (9.1e9, -8, "b")], None),
        ([(5.0, None, "a"), (5.0, 0, "b")], (5.0, None, "a")),
        ([(5.0, 0, "a"), (6.0, 0, "b")], None),
    ],
)
def test_resolve_duplicates(observations, expected):
    assert resolve_duplicates(observations) == expected


def test_dimensional_values_are_kept_separately():
    package = parse_package(CIK, FILING, documents())
    assert package.members("us-gaap:Assets", None, date(2025, 12, 31)) == {
        (("us-gaap:StatementBusinessSegmentsAxis", "ex:WidgetsMember"),): 5
    }


def test_extensible_list_names_the_statement_line_containing_a_disclosure():
    tag = "us-gaap:OperatingLeaseLiabilityCurrentStatementOfFinancialPositionExtensibleList"
    location = (
        f'<{tag} contextRef="i">http://fasb.org/us-gaap/2025#AccruedLiabilitiesCurrent '
        f"http://ex.com/20251231#OtherLiabilities</{tag}>"
    )
    package = parse_package(CIK, FILING, documents(**{"ex-20251231_htm.xml": instance(location)}))
    assert package.enumerations[(tag, None, date(2025, 12, 31))] == (
        "us-gaap:AccruedLiabilitiesCurrent",
        "ex:OtherLiabilities",
    )


def test_child_reported_only_by_member_makes_a_sum_incomplete_not_inconsistent():
    by_member = instance().replace(
        '<ex:RevolverBorrowings contextRef="i"', '<ex:RevolverBorrowings contextRef="seg"'
    )
    package = parse_package(CIK, FILING, documents(**{"ex-20251231_htm.xml": by_member}))
    liabilities = next(
        r for r in package.check_calculations("balance_sheet") if r.parent == "us-gaap:Liabilities"
    )
    assert liabilities.status == "incomplete"
    assert liabilities.missing == ("ex:RevolverBorrowings",)


def test_immutable_filing_files_are_cached_without_version_copies(tmp_path: Path):
    with SECClient(
        config(tmp_path),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, text="<xbrl/>")),
        sleep=lambda _: None,
    ) as sec:
        assert sec.get_filing_file(CIK, FILING, "a_htm.xml", date(2026, 3, 1)) == "<xbrl/>"
    cache = tmp_path / "data/raw/sec"
    assert len(list(cache.glob("*.json.gz"))) == 1 and not list(cache.glob("*.json"))
    assert not (cache / "versions").exists()
