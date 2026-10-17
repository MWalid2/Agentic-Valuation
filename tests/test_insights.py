"""Filing insights read in code: dilution, lease rate, marginal tax, captive split."""

from datetime import date

from test_filings_text import parsed

from dcf_agent.contracts.external import DataBundle
from dcf_agent.contracts.inputs import (
    ConceptStatus,
    FinancialPeriod,
    RawFinancials,
    ValuationRequest,
)
from dcf_agent.contracts.provenance import Provenance, Sourced
from dcf_agent.contracts.quality import DataQualityCheck, DataQualityReport
from dcf_agent.engine.assemble import lease_rate, marginal_tax_rate, shares_of
from dcf_agent.rag.filings import Cell, Chunk, FactRef
from dcf_agent.rag.insights import (
    FEDERAL_AMOUNT,
    FEDERAL_RATE,
    LEASE_LIABILITY,
    LEASE_RATE,
    OPTIONS,
    STATE_AMOUNT,
    STATE_EXPENSE,
    STATE_RATE,
    STRIKE,
    UNVESTED,
    Filing,
    apply_captive,
    captive_split,
    marginal_tax,
    read_insights,
    unvested,
)

END = date(2025, 12, 31)
AS_OF = date(2026, 3, 31)
CIK = "0000000001"
ACCESSION = "0000000001-26-000001"
ZW = "​"


def fact(
    concept: str,
    value: float,
    *,
    unit: str = "shares",
    start: date | None = None,
    end: date = END,
    members: tuple[tuple[str, str], ...] = (),
    fact_id: str | None = None,
) -> FactRef:
    return FactRef(
        fact_id=fact_id or f"{concept.split(':')[-1][:20]}-{value}-{end}",
        concept=concept,
        unit=unit,
        scale=0,
        negative=value < 0,
        start=start,
        end=end,
        dimensional=bool(members),
        value=value,
        members=members,
    )


def chunk(facts: tuple[FactRef, ...] = (), rows=(), header_rows: int = 0, n: int = 1) -> Chunk:
    return Chunk(
        chunk_id=f"{ACCESSION}:{n:05d}",
        accession=ACCESSION,
        form="10-K",
        filed=date(2026, 2, 20),
        period_end=END,
        section="us-gaap:Notes",
        title="NOTE",
        kind="table" if rows else "text",
        text="",
        order=n,
        rows=tuple(tuple(Cell(text=t) for t in row) for row in rows),
        header_rows=header_rows,
        facts=facts,
    )


def filing(*chunks: Chunk, form: str = "10-K", end: date = END) -> Filing:
    return Filing(
        accession=ACCESSION,
        form=form,
        filed=date(2026, 2, 20),
        period_end=end,
        url="https://www.sec.gov/x.htm",
        chunks=chunks,
    )


def year(end: date) -> date:
    return date(end.year, 1, 1)


AWARD = "us-gaap:AwardTypeAxis"


def test_fact_members_are_kept_from_the_filing_contexts():
    notes = [f for c in parsed().chunks for f in c.facts if f.dimensional]
    assert notes and all(
        f.members == (("us-gaap:DebtInstrumentAxis", "ex:NotesMember"),) for f in notes
    )


def test_options_and_units_become_dilutive_securities_with_evidence():
    found = filing(
        chunk(
            (
                fact(OPTIONS, 2_000_000),
                fact(STRIKE, 40.0, unit="USD/shares"),
                fact(
                    UNVESTED, 300_000, members=((AWARD, "us-gaap:RestrictedStockUnitsRSUMember"),)
                ),
                fact(UNVESTED, 100_000, members=((AWARD, "us-gaap:PerformanceSharesMember"),)),
            )
        )
    )
    insights, _, notes = read_insights([found], CIK, AS_OF, None)
    by_id = {s.security_id: s for s in insights.dilutive_securities}
    assert by_id["options"].count.value == 2_000_000 and by_id["options"].strike_price
    assert by_id["options"].strike_price.value == 40.0
    assert (
        by_id["options"].count.prov.source == "sec_xbrl"
        and ACCESSION in by_id["options"].count.prov.ref
    )
    assert by_id["units:RestrictedStockUnitsRSU"].kind == "rsu"
    assert by_id["units:PerformanceShares"].kind == "psu"
    assert any("sum of" in n for n in notes)


def test_an_award_member_that_totals_the_others_is_used_alone():
    total = ((AWARD, "ex:AllUnitsMember"),)
    found = filing(
        chunk(
            (
                fact(UNVESTED, 300, members=((AWARD, "ex:AMember"),)),
                fact(UNVESTED, 100, members=((AWARD, "ex:BMember"),)),
                fact(UNVESTED, 400, members=total),
            )
        )
    )
    securities, _ = unvested([found], AS_OF)
    assert [s.count.value for s in securities] == [400]


def test_marginal_tax_is_federal_plus_average_state_and_skips_loss_years():
    rates = [
        fact(FEDERAL_RATE, 0.21, unit="pure", start=year(e), end=e)
        for e in (date(2023, 12, 31), date(2024, 12, 31), END)
    ]
    state = [
        fact(STATE_RATE, 0.02, unit="pure", start=year(date(2024, 12, 31)), end=date(2024, 12, 31)),
        fact(STATE_RATE, 0.03, unit="pure", start=year(END), end=END),
        fact(
            STATE_RATE, -0.05, unit="pure", start=year(date(2023, 12, 31)), end=date(2023, 12, 31)
        ),
    ]
    loss = [
        fact(
            FEDERAL_AMOUNT,
            -10.0,
            unit="USD",
            start=year(date(2023, 12, 31)),
            end=date(2023, 12, 31),
        )
    ]
    item, _ = marginal_tax([filing(chunk((*rates, *state, *loss)))], AS_OF)
    assert item is not None and item.amount is not None
    assert abs(item.amount.value - 0.235) < 1e-9  # 21% + mean(2%, 3%); the loss year is skipped


def test_marginal_tax_from_amounts_or_state_expense():
    amounts = (
        fact(FEDERAL_AMOUNT, 210.0, unit="USD", start=year(END)),
        fact(STATE_AMOUNT, 30.0, unit="USD", start=year(END)),
    )
    item, _ = marginal_tax([filing(chunk(amounts))], AS_OF)
    assert item is not None and item.amount is not None and abs(item.amount.value - 0.24) < 1e-9
    expense = (
        fact(FEDERAL_AMOUNT, 210.0, unit="USD", start=year(END)),
        fact(STATE_EXPENSE[0], 50.0, unit="USD", start=year(END)),
    )
    item, _ = marginal_tax([filing(chunk(expense))], AS_OF)
    assert item is not None and item.amount is not None
    assert abs(item.amount.value - (0.21 + 50 * 0.79 / 1000)) < 1e-9
    assert "state tax expense" in item.description


def test_lease_rate_and_liability_are_read():
    found = filing(
        chunk((fact(LEASE_RATE, 0.045, unit="pure"), fact(LEASE_LIABILITY, 1e9, unit="USD")))
    )
    insights, _, _ = read_insights([found], CIK, AS_OF, None)
    (lease,) = insights.lease_liabilities
    assert (
        lease.discount_rate and lease.discount_rate.value == 0.045 and lease.liability.value == 1e9
    )


# A consolidating balance sheet: blanks omitted, eliminations mostly empty.
HEADER = [
    [ZW],
    ["CONDENSED BALANCE SHEETS"],
    ["EQUIPMENT", ZW, "FINANCIAL"],
    ["OPERATIONS", "SERVICES", "ELIMINATIONS", "CONSOLIDATED"],
    ["2025", "2024", "2025", "2024", "2025", "2024", "2025", "2024"],
]
ROWS = [
    ["Cash", "$10", "$9", "$5", "$4", "$15", "$13"],
    [
        "Short-term borrowings",
        ZW,
        "$400",
        ZW,
        "$900",
        ZW,
        "$13,000",
        "$12,600",
        "$13,400",
        "$13,500",
    ],
    ["Securitization borrowings", ZW, ZW, ZW, ZW, "6,500", "8,400", ZW, ZW, "6,500", "8,400"],
    [
        "Long-term borrowings",
        "8,700",
        "6,600",
        "34,800",
        "36,600",
        "(100)",
        "(50)",
        "43,400",
        "43,150",
    ],
    ["Interest payable", "1", "1", "2", "2", "3", "3"],
]


def consolidating(n: int = 7) -> Chunk:
    return chunk(rows=[*HEADER, *ROWS], header_rows=len(HEADER), n=n)


def test_captive_borrowings_are_read_when_every_column_adds_up():
    split = captive_split(filing(consolidating()), 63_300e6)
    assert not isinstance(split, str)
    # 13,000 + 6,500 (only financial services) + 34,800, in millions.
    assert split.finance == 54_300e6 and split.consolidated == 63_300e6
    assert split.rows == (
        "Short-term borrowings",
        "Securitization borrowings",
        "Long-term borrowings",
    )


def test_captive_split_refuses_when_the_table_does_not_match_the_tagged_debt():
    split = captive_split(filing(consolidating()), 70_000e6)
    assert isinstance(split, str) and "do not match" in split


def test_unresolved_captive_debt_is_resolved_in_the_raw_financials():
    prov = Provenance(
        source="sec_xbrl",
        ref="x",
        as_of=AS_OF,
        period_end=END,
        filed=date(2026, 2, 20),
        confidence="high",
    )
    period = FinancialPeriod(
        kind="annual",
        fiscal_year=2025,
        period_start=date(2025, 1, 1),
        period_end=END,
        values={"debt": Sourced[float](value=63_300e6, prov=prov)},
        statuses={
            "debt": ConceptStatus(status="reported"),
            "captive_finance_debt": ConceptStatus(status="unresolved", reason="not split"),
        },
    )
    raw = RawFinancials(cik=CIK, as_of=AS_OF, annual=(period,))
    fixed, items, notes = apply_captive(raw, [filing(consolidating())], AS_OF)
    latest = fixed.annual[-1]
    assert latest.values["captive_finance_debt"].value == 54_300e6
    assert latest.values["debt"].value == 9_000e6
    assert latest.statuses["captive_finance_debt"].status == "reported"
    assert "excludes captive-finance debt" in (latest.statuses["debt"].basis or "")
    assert len(items) == 1 and not notes


def data_bundle(insights) -> DataBundle:
    request = ValuationRequest(
        ticker="EXM",
        cik=CIK,
        company_name="Example",
        valuation_date=AS_OF,
        mode="info",
        intent="I1",
        question="q",
    )
    check = DataQualityCheck(
        check_id="seed:0", field="seed", status="passed", severity="info", message="Seed"
    )
    return DataBundle(
        request=request,
        filing_insights=insights,
        quality=DataQualityReport(as_of=AS_OF, checks=(check,)),
    )


def test_the_valuation_uses_the_filing_insights_and_falls_back_without_them():
    found = filing(
        chunk(
            (
                fact(OPTIONS, 2e6),
                fact(STRIKE, 40.0, unit="USD/shares"),
                fact(UNVESTED, 5e5),
                fact(LEASE_RATE, 0.045, unit="pure"),
                fact(LEASE_LIABILITY, 1e9, unit="USD"),
                fact(FEDERAL_RATE, 0.21, unit="pure", start=year(END)),
                fact(STATE_RATE, 0.03, unit="pure", start=year(END)),
            )
        )
    )
    insights, _, _ = read_insights([found], CIK, AS_OF, None)
    data = data_bundle(insights)
    shares, note = shares_of(data, 100e6)
    assert shares.rsus == 5e5 and [(o.count, o.strike) for o in shares.options] == [(2e6, 40.0)]
    assert "treasury stock" in note
    tax, _ = marginal_tax_rate(data, 0.25)
    assert abs(tax.value - 0.24) < 1e-9 and tax.prov.source == "derived"
    assert marginal_tax_rate(data, 0.25, filings=False)[0].value == 0.25
    assert lease_rate(data, 0.06) == 0.045
    empty = data_bundle(None)
    assert shares_of(empty, 100e6)[0].rsus == 0 and marginal_tax_rate(empty, 0.25)[0].value == 0.25
    assert lease_rate(empty, 0.06) == 0.06


def test_questions_about_the_notes_are_answered_from_the_insights():
    from dcf_agent.agents.answer import render

    found = filing(
        chunk(
            (
                fact(OPTIONS, 2e6),
                fact(STRIKE, 40.0, unit="USD/shares"),
                fact(FEDERAL_RATE, 0.21, unit="pure", start=year(END)),
                fact(STATE_RATE, 0.03, unit="pure", start=year(END)),
            )
        )
    )
    insights, _, _ = read_insights([found], CIK, AS_OF, None)
    data = data_bundle(insights)
    request = data.request.model_copy(
        update={"question": "How many stock options are outstanding, and the tax rate?"}
    )
    reply = render({"request": request, "data": data})
    assert "Dilutive securities" in reply.body and "$40.00" in reply.body
    assert "Marginal tax rate 24.00%" in reply.body
    assert all(f.value.prov.source in ("sec_xbrl", "derived") for f in reply.figures)


def test_convertible_notes_by_instrument():
    from dcf_agent.rag.insights import CONVERSION_PRICE, convertibles

    note = (("us-gaap:DebtInstrumentAxis", "ex:NotesDue2029Member"),)
    retired = (("us-gaap:DebtInstrumentAxis", "ex:NotesDue2025Member"),)
    found = filing(
        chunk(
            (
                fact(
                    CONVERSION_PRICE, 150.0, unit="USD/shares", end=date(2024, 9, 30), members=note
                ),
                fact("us-gaap:DebtInstrumentCarryingAmount", 1_500e6, unit="USD", members=note),
                fact(
                    CONVERSION_PRICE,
                    90.0,
                    unit="USD/shares",
                    end=date(2020, 9, 30),
                    members=retired,
                ),
            )
        )
    )
    securities, notes = convertibles([found], AS_OF)
    (security,) = securities
    assert security.kind == "convertible" and security.principal.value == 1_500e6
    assert security.conversion_shares.value == 10e6  # 1,500M / $150
    assert any("nothing outstanding" in n for n in notes)
    shares, _ = shares_of(data_bundle(read_insights([found], CIK, AS_OF, None)[0]), 100e6)
    assert [(c.principal, c.conversion_shares) for c in shares.convertibles] == [(1_500e6, 10e6)]
