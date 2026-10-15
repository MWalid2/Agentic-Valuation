"""End-to-end point-in-time: an earlier valuation date sees only filings filed by then.

Runs the Data Agent's history collection on the local SEC cache (offline) and
skips when the cache is not present.
"""

from datetime import date

import pytest

from dcf_agent.agents.lines import MappingStore
from dcf_agent.config import load_config
from dcf_agent.data.sec import SECClient, SECError
from dcf_agent.tools.data import collect_history, filing_seed

APPLE = "0000320193"
EARLY = date(2025, 6, 30)
LATE = date(2026, 10, 11)


def history(as_of: date):
    config = load_config(load_env_file=True)
    mappings = MappingStore(config.settings.project_root / "data/mappings")
    audit: list[str] = []
    try:
        sec = SECClient(config, offline=True)
    except ValueError as error:
        pytest.skip(f"SEC identity not configured: {error}")
    with sec:
        try:
            seed = filing_seed(sec, APPLE, as_of, mappings).seed
            raw, issues, extractions = collect_history(sec, seed, mappings, audit)
            filings = {f.accession: f for f in sec.get_submissions(APPLE, LATE)}
        except SECError as error:
            pytest.skip(f"SEC cache unavailable: {error}")
    return raw, extractions, audit, filings


def test_earlier_valuation_date_uses_only_filings_filed_by_then():
    raw, extractions, audit, filings = history(EARLY)
    periods = [*raw.annual, *raw.quarterly, *((raw.ttm,) if raw.ttm else ())]
    for period in periods:
        assert period.period_end <= EARLY
        for item in period.values.values():
            assert item.prov.as_of <= EARLY
            assert item.prov.filed is None or item.prov.filed <= EARLY
    # Every filing package read was filed on or before the cutoff.
    read = [a.split(":", 1)[1] for a in audit if a.startswith("read_xbrl_package:")]
    assert read and all(filings[acc].filed <= EARLY for acc in read)
    assert all(e.package.filing.filed <= EARLY for e in extractions)
    # The latest periods are exactly what had been published: FY2024 10-K, Q2 FY2025 10-Q.
    published = [f for f in filings.values() if f.filed <= EARLY and f.report_date]
    latest_10k = max(f.report_date for f in published if f.form == "10-K")
    latest_10q = max(f.report_date for f in published if f.form == "10-Q")
    assert raw.annual[-1].period_end == latest_10k == date(2024, 9, 28)
    assert raw.quarterly[-1].period_end == latest_10q
    assert raw.ttm is not None and raw.ttm.period_end == latest_10q


def test_later_date_adds_newer_filings_without_changing_older_periods():
    early, *_ = history(EARLY)
    late, *_ = history(LATE)
    assert late.annual[-1].period_end > early.annual[-1].period_end
    shared = {p.period_end: p for p in early.annual}
    for period in late.annual:
        if period.period_end in shared:
            before = shared[period.period_end].values["revenue"].value
            assert period.values["revenue"].value == before  # same reported revenue
