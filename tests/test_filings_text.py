"""Filing text store on a small inline-XBRL document with every structure seen in filings."""

from collections import Counter
from datetime import date

from dcf_agent.data.sec import Filing
from dcf_agent.rag.filings import lead_in, parse_filing

FILING = Filing(
    accession="0000000001-26-000001",
    form="10-K",
    filed=date(2026, 2, 15),
    report_date=date(2025, 12, 31),
    primary_document="ex-20251231.htm",
)


def fact(fact_id: str, concept: str, context: str, text: str, sign: str = "") -> str:
    signed = f' sign="{sign}"' if sign else ""
    return (
        f'<ix:nonFraction id="{fact_id}" name="{concept}" contextRef="{context}" '
        f'unitRef="usd" scale="6" decimals="-6"{signed}>{text}</ix:nonFraction>'
    )


def context(cid: str, end: str, member: bool = False) -> str:
    segment = (
        '<xbrli:segment><xbrldi:explicitMember dimension="us-gaap:DebtInstrumentAxis">'
        "ex:NotesMember</xbrldi:explicitMember></xbrli:segment>"
        if member
        else ""
    )
    return (
        f'<xbrli:context id="{cid}"><xbrli:entity><xbrli:identifier scheme="x">1'
        f"</xbrli:identifier>{segment}</xbrli:entity><xbrli:period><xbrli:instant>{end}"
        "</xbrli:instant></xbrli:period></xbrli:context>"
    )


F3 = fact("f3", "ex:FairValueAdjustment", "c25", "50", "-")
F5 = fact("f5", "us-gaap:DebtInstrumentCarryingAmount", "c25", "1,150")
F6 = fact("f6", "us-gaap:DebtInstrumentCarryingAmount", "c24", "1,140")
LITIGATION = "We are party to litigation arising in the ordinary course."

DOC = f"""<?xml version="1.0"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"
 xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:xbrldi="http://xbrl.org/2006/xbrldi">
<body>
<div style="display:none"><ix:header><ix:hidden>
 {fact("h1", "dei:EntityPublicFloat", "c25", "100")}
 <ix:nonNumeric id="hb" name="us-gaap:CommitmentsAndContingenciesDisclosureTextBlock"
   contextRef="c25" continuedAt="k1"></ix:nonNumeric>
</ix:hidden><ix:resources><xbrli:unit id="usd"><xbrli:measure>iso4217:USD</xbrli:measure>
</xbrli:unit>{context("c25", "2025-12-31")}{context("c24", "2024-12-31")}
{context("m25", "2025-12-31", member=True)}</ix:resources></ix:header></div>
<p>ITEM 7. MANAGEMENT'S DISCUSSION AND ANALYSIS</p>
<p>Revenue grew because volumes rose.</p>
<p>ITEM 8. FINANCIAL STATEMENTS AND SUPPLEMENTARY DATA</p>
<p>NOTE 11: DEBT AND BORROWING ARRANGEMENTS</p>
<ix:nonNumeric id="d1" name="us-gaap:DebtDisclosureTextBlock" contextRef="c25" continuedAt="d1c">
 <p>Loans and Notes Payable</p>
 <p>Our long-term debt consisted of the following (in millions):</p>
 <ix:nonNumeric id="t1" name="us-gaap:ScheduleOfDebtTableTextBlock" contextRef="c25">
 <table>
  <tr><td></td><td>December 31, 2025</td><td>December 31, 2024</td></tr>
  <tr><td>Notes due 2030</td><td>$</td><td>{fact("f1", "us-gaap:LongTermDebt", "m25", "1,200")}</td>
      <td>$</td><td>{fact("f2", "us-gaap:LongTermDebt", "c24", "1,100")}</td></tr>
  <tr><td>Fair value adjustment</td><td>(</td><td>{F3}</td><td>)</td>
      <td>{fact("f4", "ex:FairValueAdjustment", "c24", "40")}</td></tr>
 </table></ix:nonNumeric>
</ix:nonNumeric>
<p>F-5 Table of Contents</p>
<ix:continuation id="d1c">
 <table>
  <tr><td>Total debt</td><td>{F5}</td>
      <td>{F6}</td></tr>
 </table>
 <p>The notes are unsecured and rank equally. Interest is paid semi-annually.</p>
</ix:continuation>
<ix:continuation id="k1"><p>{LITIGATION}</p></ix:continuation>
<p>Our tax rate was <ix:nonNumeric id="x1" name="us-gaap:IncomeTaxDisclosureTextBlock"
  contextRef="c25">disclosed in the tax note</ix:nonNumeric> of these statements.</p>
</body></html>"""


def parsed():
    return parse_filing(DOC, FILING, "https://www.sec.gov/x.htm", date(2025, 12, 31))


def chunks_in(section: str):
    return [c for c in parsed().chunks if c.section == section]


def test_every_visible_fact_lands_in_exactly_one_chunk_and_hidden_ones_in_none():
    text = parsed()
    counts = Counter(f for c in text.chunks for f in c.fact_ids)
    assert set(counts) == set(text.visible_fact_ids) == {"f1", "f2", "f3", "f4", "f5", "f6"}
    assert all(n == 1 for n in counts.values())
    assert text.hidden_fact_ids == ("h1",) and text.issues == ()


def test_note_follows_its_continuation_and_keeps_its_heading_as_title():
    debt = chunks_in("us-gaap:DebtDisclosureTextBlock")
    assert {c.title for c in debt} == {"NOTE 11: DEBT AND BORROWING ARRANGEMENTS"}
    assert any("unsecured and rank equally" in c.text for c in debt)  # from the continuation
    assert "F-5 Table of Contents" not in " ".join(c.text for c in debt)


def test_tagged_cells_carry_concept_scale_sign_period_and_dimension():
    table = next(c for c in chunks_in("us-gaap:DebtDisclosureTextBlock") if c.kind == "table")
    assert table.table_tag == "us-gaap:ScheduleOfDebtTableTextBlock"
    assert table.blocks == ("us-gaap:DebtDisclosureTextBlock", table.table_tag)
    assert table.units == "USD in millions" and table.header_rows == 1
    notes_row = table.rows[1]
    assert [c.text for c in notes_row] == ["Notes due 2030", "$1,200", "$1,100"]
    current = notes_row[1].facts[0]
    assert (current.concept, current.unit, current.scale, current.end, current.dimensional) == (
        "us-gaap:LongTermDebt",
        "USD",
        6,
        date(2025, 12, 31),
        True,
    )
    assert current.value == 1_200_000_000 and table.rows[2][1].facts[0].value == -50_000_000
    adjustment = table.rows[2][1]
    assert adjustment.text == "(50)" and adjustment.facts[0].negative
    assert "Caption: Our long-term debt consisted of the following (in millions):" in table.text


def test_a_continued_table_without_header_inherits_the_header_above():
    tables = [c for c in chunks_in("us-gaap:DebtDisclosureTextBlock") if c.kind == "table"]
    continued = tables[1]
    assert continued.inherited_header and continued.header_rows == 1
    assert "Columns (from the table above):" in continued.text
    assert "December 31, 2025 | December 31, 2024" in continued.text


def test_hidden_text_block_owns_its_visible_continuation():
    litigation = chunks_in("us-gaap:CommitmentsAndContingenciesDisclosureTextBlock")
    assert litigation and "ordinary course" in litigation[0].text


def test_inline_text_block_inside_a_paragraph_is_covered():
    text = parsed()
    covered = {b for c in text.chunks for b in c.blocks}
    assert set(text.text_blocks) <= covered
    assert "us-gaap:IncomeTaxDisclosureTextBlock" in covered


def test_text_outside_notes_is_split_by_item():
    mdna = chunks_in("item_7")
    assert mdna and "volumes rose" in mdna[0].text and mdna[0].title.startswith("ITEM 7.")


def test_lead_in_sentence():
    assert lead_in("Some context. The following table shows debt:") == (
        "The following table shows debt:"
    )
    assert lead_in("Revenue grew strongly this year.") is None
