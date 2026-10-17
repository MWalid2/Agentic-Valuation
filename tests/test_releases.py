"""Guidance from earnings releases: tables, forward sentences, and what is not guidance."""

from datetime import date

from dcf_agent.data.sec import Filing
from dcf_agent.rag.releases import parse_range, read_guidance

FILING = Filing(
    accession="0000000001-26-000010",
    form="8-K",
    filed=date(2026, 7, 28),
    report_date=date(2026, 7, 28),
    primary_document="ex99.htm",
    items=("2.02", "9.01"),
)
URL = "https://www.sec.gov/Archives/edgar/data/1/000000000126000010/ex99.htm"

RELEASE = """<html><body>
<p>Second quarter results</p>
<table>
<tr><td>Segment</td><td>Second Quarter 2026</td><td>2025</td><td>% Change</td></tr>
<tr><td>Net sales</td><td>$5,000</td><td>$4,500</td><td>11%</td></tr>
</table>
<p>Operating profit in the second quarter of 2026 included $392 million of expected tariff
costs.</p>
<p>Full Year 2026 Guidance</p>
<table>
<tr><td></td><td>Current</td><td>Prior</td></tr>
<tr><td>Organic revenues (non-GAAP)</td><td>Approx. 5% growth</td><td>4% to 5% growth</td></tr>
<tr><td>Gross margin</td><td>74.0</td><td>73.5</td></tr>
</table>
<p>For the full year fiscal 2027, the company expects its tax rate to be between 16.0% and
18.0%. The effective tax rate for fiscal 2027 is expected to be in the mid-20 percent range.</p>
</body></html>"""


def test_guidance_tables_and_forward_sentences_are_read_verbatim():
    found = read_guidance(RELEASE, FILING, URL)
    organic = next(g for g in found if g.metric == "organic_growth")
    assert (organic.low, organic.high, organic.unit) == (0.05, 0.05, "pct")  # current, not prior
    assert next(g for g in found if g.metric == "gross_margin").low == 0.74
    taxes = sorted((g.low, g.high) for g in found if g.metric == "tax_rate")
    assert taxes == [(0.16, 0.18), (0.24, 0.26)]
    assert all(g.accession == FILING.accession and g.url == URL for g in found)


def test_results_tables_and_past_statements_are_not_guidance():
    metrics = [g.metric for g in read_guidance(RELEASE, FILING, URL)]
    assert "revenue_growth" not in metrics  # the "% Change" results table
    assert "operating_income" not in metrics  # "included ... of expected tariff costs"


def test_ranges():
    assert parse_range("growth of 6% to 7% and more") == (0.06, 0.07, "pct")
    assert parse_range("Approx. $12.4 billion") == (12.4e9, 12.4e9, "usd")
    assert parse_range("$2.0 to $2.2 billion of capital expenditures") == (2.0e9, 2.2e9, "usd")
    assert parse_range("in the high-30 percent range") == (0.37, 0.39, "pct")
    assert parse_range("no number here") == (None, None, "")
