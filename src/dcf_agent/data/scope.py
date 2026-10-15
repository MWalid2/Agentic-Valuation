"""v1 scope: US non-financial operating companies reporting under US GAAP in USD.

Every reason is evidence from SEC metadata or the filing's own XBRL; nothing is
inferred from a company name alone. Pre-revenue companies are flagged by
readiness from the evidenced absence of revenue.
"""

from dcf_agent.contracts.external import InputReadiness
from dcf_agent.data.extraction import Extraction

FINANCIAL_SIC = range(6000, 6800)
BLANK_CHECK_SIC = "6770"


def assess_scope(profile: dict[str, str | None], latest: Extraction | None) -> InputReadiness:
    reasons: list[str] = []
    notes: list[str] = ["Scope covers US non-financial operating companies under US GAAP."]
    sic = profile.get("sic")
    description = profile.get("sic_description") or ""
    if sic == BLANK_CHECK_SIC:
        reasons.append("Blank-check company (SIC 6770): no operating business to value")
    elif sic and sic.isdigit() and int(sic) in FINANCIAL_SIC:
        reasons.append(
            f"Financial or real-estate company (SIC {sic} {description}): "
            "needs bank/insurer/REIT methods (Phase 7)"
        )
    elif not sic:
        notes.append("No SIC code on file; industry scope could not be confirmed")
    if latest is not None:
        package = latest.package
        if "ifrs" in package.taxonomies and "us-gaap" not in package.taxonomies:
            reasons.append("IFRS filer: v1 handles US GAAP only")
        foreign = sorted(package.currencies - {"USD"})
        if foreign and "USD" not in package.currencies:
            reasons.append("Reports in " + ", ".join(foreign) + ": v1 handles USD only")
        elif foreign:
            notes.append("Some amounts in " + ", ".join(foreign) + "; statements are in USD")
        end = package.filing.report_date
        captive = latest.periods.get((None, end), {}).get("captive_finance_debt") if end else None
        if captive is not None and captive.status == "reported":
            notes.append(f"Captive finance arm separated: {captive.basis}")
        temporary = latest.periods.get((None, end), {}).get("temporary_equity") if end else None
        if temporary is not None and temporary.status == "reported":
            notes.append(f"Temporary-equity claims: {temporary.basis}")
    return InputReadiness(
        status="blocked" if reasons else "needs_review",
        missing={"scope": tuple(reasons)} if reasons else {},
        review_notes=tuple(notes),
    )
