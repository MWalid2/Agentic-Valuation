"""v1 scope decisions from SEC metadata and the filing's own XBRL."""

from dataclasses import replace

from test_extraction import package

from dcf_agent.data.extraction import Extraction
from dcf_agent.data.scope import assess_scope


def latest(currencies=frozenset({"USD"}), taxonomies=frozenset({"us-gaap"})) -> Extraction:
    pkg = replace(package([], []), currencies=currencies, taxonomies=taxonomies)
    return Extraction(pkg, {}, {})


def profile(sic: str | None, description: str = "") -> dict[str, str | None]:
    return {"name": "Example", "sic": sic, "sic_description": description}


def test_operating_us_gaap_usd_company_is_in_scope():
    result = assess_scope(profile("3571", "Electronic Computers"), latest())
    assert result.status == "needs_review" and not result.missing


def test_financial_real_estate_and_blank_check_companies_are_out_of_scope():
    bank = assess_scope(profile("6022", "State commercial banks"), latest())
    assert bank.status == "blocked" and "Financial" in bank.missing["scope"][0]
    reit = assess_scope(profile("6798", "Real estate investment trusts"), latest())
    assert reit.status == "blocked"
    spac = assess_scope(profile("6770", "Blank checks"), latest())
    assert "Blank-check" in spac.missing["scope"][0]


def test_ifrs_and_non_usd_reporting_are_out_of_scope_but_some_foreign_amounts_are_fine():
    ifrs = assess_scope(profile("2834"), latest(taxonomies=frozenset({"ifrs"})))
    assert "IFRS" in ifrs.missing["scope"][0]
    cad = assess_scope(profile("2834"), latest(currencies=frozenset({"CAD"})))
    assert "CAD" in cad.missing["scope"][0]
    mixed = assess_scope(profile("2834"), latest(currencies=frozenset({"USD", "EUR"})))
    assert mixed.status == "needs_review"
    assert any("EUR" in note for note in mixed.review_notes)


def test_missing_profile_and_filing_do_not_block_but_are_noted():
    result = assess_scope(profile(None), None)
    assert result.status == "needs_review"
    assert any("No SIC" in note for note in result.review_notes)
