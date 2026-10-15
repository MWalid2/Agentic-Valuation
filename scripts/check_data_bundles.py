"""Check saved reference-company bundles without network or model calls."""

import argparse
import json
import math
from datetime import timedelta
from pathlib import Path
from typing import Any

from dcf_agent.build import build_fingerprint
from dcf_agent.contracts.external import DataBundle
from dcf_agent.contracts.inputs import FinancialConcept
from dcf_agent.data.history import build_ttm

# Damodaran's own company-file assignment, matched by ticker.
INDUSTRIES = {
    "0000320193": "Computers/Peripherals",
    "0000021344": "Beverage (Soft)",
    "0000320187": "Shoe",
}
# Reference field -> collected concept in the latest annual period.
CONCEPTS: dict[str, FinancialConcept] = {
    "revenue": "revenue",
    "operating_income": "ebit",
    "net_income": "net_income",
    "cash": "cash",
    "debt": "debt",
    "total_assets": "total_assets",
    "total_liabilities": "total_liabilities",
    "equity": "equity",
    "operating_cash_flow": "operating_cash_flow",
    "capex": "capex",
}


def evaluate(path: Path, references: list[dict[str, Any]]) -> dict[str, Any]:
    bundle = DataBundle.model_validate_json(path.read_text(encoding="utf-8"))
    reference = next(r for r in references if r["cik"].zfill(10) == bundle.request.cik)
    prices, market, raw = bundle.prices, bundle.market, bundle.raw
    # Collected automatically: the latest annual period must be the reference 10-K year.
    latest = raw.annual[-1] if raw is not None and raw.annual else None
    values = {}
    for field, concept in CONCEPTS.items():
        amount = latest.values.get(concept) if latest is not None else None
        values[field] = amount is not None and abs(amount.value - reference["values"][field]) <= 1.0
    checks = {
        "financial_values": all(values.values()),
        "financial_period": latest is not None
        and str(latest.period_start) == reference["period_start"]
        and str(latest.period_end) == reference["period_end"],
        # The newest cover page: never older than the 10-K cover, never after the cutoff.
        "share_observation_date": market is not None
        and reference["shares_observed_on"]
        <= str(market.shares_observed_on)
        <= str(bundle.request.valuation_date),
        "price_history": prices is not None and len(prices.observations) >= 400,
        "fred_series": set(bundle.rates) == {"DGS10", "BAMLC0A0CM", "T10YIE"}
        and all(
            rate.available_on <= bundle.request.valuation_date
            and rate.rate.prov.source == "fred"
            and rate.rate.prov.ref.endswith("/" + series)
            for series, rate in bundle.rates.items()
        ),
        "damodaran_tables": set(bundle.benchmarks)
        == {"betas", "margins", "sales_to_capital", "country_risk", "spreads"},
        "industry_selection": bundle.industry is not None
        and bundle.industry.method == "ticker"
        and all(
            dataset in bundle.benchmarks
            and [row.name for row in bundle.benchmarks[dataset].rows]
            == [INDUSTRIES[bundle.request.cik]]
            for dataset in ("betas", "margins", "sales_to_capital")
        ),
        "macro_assembly": bundle.macro is not None,
        "market_cap_arithmetic": market is not None
        and market.market_cap is not None
        and math.isclose(
            market.market_cap.value,
            market.close.price.value * market.shares_outstanding.value,
            rel_tol=1e-12,
        ),
        "quality_gate": not bundle.quality.has_blocking_issues,
        # A bundle built by older code cannot describe the current pipeline's behaviour.
        "current_build": bundle.build == build_fingerprint(),
    }
    recomputed = build_ttm(raw.quarterly, raw.as_of) if raw is not None else None
    checks["history_coverage"] = (
        raw is not None
        and len(raw.annual) == 5
        and len(raw.quarterly) == 8
        and all(
            a.period_end + timedelta(days=1) == b.period_start
            for periods in (raw.annual, raw.quarterly)
            for a, b in zip(periods[:-1], periods[1:], strict=True)
        )
        and raw.ttm is not None
        and recomputed is not None
        and raw.ttm.period_start == recomputed.period_start
        and raw.ttm.period_end == recomputed.period_end
        and set(raw.ttm.values) == set(recomputed.values)
        and all(abs(v.value - recomputed.values[c].value) <= 1 for c, v in raw.ttm.values.items())
    )
    return {
        "company": bundle.request.company_name,
        "ticker": bundle.request.ticker,
        "bundle": str(path),
        "as_of": str(bundle.request.valuation_date),
        "passed": all(checks.values()),
        "checks": checks,
        "financial_values": values,
        "price_count": len(prices.observations) if prices else 0,
        "price_source": prices.source if prices else None,
        "missing": bundle.quality.missing_fields,
        "annual_count": len(raw.annual) if raw else 0,
        "quarter_count": len(raw.quarterly) if raw else 0,
        "ttm_end": str(raw.ttm.period_end) if raw and raw.ttm else None,
        "history_review_items": len(bundle.history_issues),
        "readiness": {
            consumer: {"status": r.status, "missing": r.missing}
            for consumer, r in bundle.readiness.items()
        },
        "warnings": [c.message for c in bundle.quality.checks if c.severity == "warning"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundles", type=Path, nargs="+")
    parser.add_argument("--out", type=Path, default=Path("outputs/data-agent-cohort-check.json"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    references = json.loads((root / "tests/fixtures/sec/snapshot-reference.json").read_text())
    reports = [evaluate(path, references) for path in args.bundles]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(reports, indent=2), encoding="utf-8")
    for report in reports:
        print(
            f"{report['ticker']}: {'PASS' if report['passed'] else 'FAIL'}; "
            f"{sum(report['financial_values'].values())}/{len(CONCEPTS)} financial values; "
            f"{report['price_count']} prices"
            + ("" if report["checks"]["current_build"] else "; STALE build, rebuild first")
        )
        for consumer, readiness in report["readiness"].items():
            gaps = "; ".join(f"{k}: {', '.join(v)}" for k, v in readiness["missing"].items())
            print(f"  {consumer}: {readiness['status']}" + (f" ({gaps})" if gaps else ""))
    if not all(report["passed"] for report in reports):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
