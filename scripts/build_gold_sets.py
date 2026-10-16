"""Build the Phase 3a gold sets from XBRL-tagged facts (written before any retrieval).

XBRL-labelled set: for each company's latest 10-K filed by the date, one question
per tagged note fact in a fixed bank (debt, leases, dilution, tax, one-off items).
The answer is the consolidated fiscal-year-end value; the reference is the IDs of
the facts that state it (a passage containing any of them answers the question);
prior-year values of the same concept are recorded as distractors.

Narrative set: facts stated inside prose (restructuring, impairments, litigation,
disposals, debt issued, buyback authorizations, acquisitions). The reference
answer is the filing's own sentence. These are drafts for human review.

Companies are split into development and test by a fixed hash of the ticker, so
the split cannot be tuned. References use fact IDs, which are stable within a
filing, not chunk IDs, which depend on the parser.
"""

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from dcf_agent.config import load_config
from dcf_agent.data.sec import SECClient
from dcf_agent.rag.filings import Chunk, FactRef, filing_texts

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "evals/rag"
TEST_SHARE = 0x55  # first byte of SHA-256(ticker) below this: test (about a third)

# Field group, concept, question. Questions name the item, never the XBRL concept.
BANK: tuple[tuple[str, str, str], ...] = (
    ("debt", "LongTermDebtMaturitiesRepaymentsOfPrincipalInNextTwelveMonths",
     "How much long-term debt principal is scheduled to be repaid in the next twelve months?"),
    ("debt", "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearTwo",
     "How much long-term debt principal is due in the second year after the balance sheet date?"),
    ("debt", "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearThree",
     "How much long-term debt principal matures in year three?"),
    ("debt", "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFour",
     "How much long-term debt principal matures in year four?"),
    ("debt", "LongTermDebtMaturitiesRepaymentsOfPrincipalInYearFive",
     "How much long-term debt principal matures in year five?"),
    ("debt", "LongTermDebtMaturitiesRepaymentsOfPrincipalAfterYearFive",
     "How much long-term debt principal matures after five years?"),
    ("debt", "LongTermDebtFairValue", "What was the fair value of the company's long-term debt?"),
    ("debt", "LongTermDebtWeightedAverageInterestRate",
     "What was the weighted-average interest rate on long-term debt?"),
    ("leases", "LesseeOperatingLeaseLiabilityPaymentsDueNextTwelveMonths",
     "What operating lease payments are due in the next twelve months?"),
    ("leases", "LesseeOperatingLeaseLiabilityPaymentsDueYearTwo",
     "What operating lease payments are due in the second year?"),
    ("leases", "LesseeOperatingLeaseLiabilityPaymentsDueAfterYearFive",
     "What operating lease payments are due after year five?"),
    ("leases", "LesseeOperatingLeaseLiabilityPaymentsDue",
     "What are the total undiscounted operating lease payments?"),
    ("leases", "OperatingLeaseWeightedAverageDiscountRatePercent",
     "What weighted-average discount rate is applied to operating leases?"),
    ("leases", "FinanceLeaseWeightedAverageDiscountRatePercent",
     "What weighted-average discount rate is applied to finance leases?"),
    ("leases", "FinanceLeaseLiabilityPaymentsDue",
     "What are the total undiscounted finance lease payments?"),
    ("dilution", "ShareBasedCompensationArrangementByShareBasedPaymentAwardOptionsOutstandingNumber",  # noqa: E501
     "How many stock options were outstanding at the end of the year?"),
    ("dilution",
     "ShareBasedCompensationArrangementByShareBasedPaymentAwardOptionsOutstandingWeightedAverageExercisePrice",
     "What was the weighted-average exercise price of the stock options outstanding?"),
    ("dilution",
     "ShareBasedCompensationArrangementByShareBasedPaymentAwardEquityInstrumentsOtherThanOptionsNonvestedNumber",
     "How many unvested restricted stock units or other non-option awards were outstanding?"),
    ("dilution", "AntidilutiveSecuritiesExcludedFromComputationOfEarningsPerShareAmount",
     "How many potentially dilutive securities were excluded from diluted earnings per share?"),
    ("dilution", "EmployeeServiceShareBasedCompensationNonvestedAwardsTotalCompensationCostNotYetRecognized",  # noqa: E501
     "How much stock-based compensation cost has not yet been recognized?"),
    ("tax", "EffectiveIncomeTaxRateContinuingOperations", "What was the effective income tax rate?"),  # noqa: E501
    ("tax", "EffectiveIncomeTaxRateReconciliationAtFederalStatutoryIncomeTaxRate",
     "What statutory federal income tax rate starts the tax rate reconciliation?"),
    ("tax", "EffectiveIncomeTaxRateReconciliationStateAndLocalIncomeTaxes",
     "How much did state and local income taxes add to the effective tax rate?"),
    ("tax", "UnrecognizedTaxBenefits", "What was the balance of unrecognized tax benefits?"),
    ("tax", "DeferredTaxAssetsOperatingLossCarryforwards",
     "What deferred tax asset relates to net operating loss carryforwards?"),
    ("tax", "OperatingLossCarryforwards", "How large are the net operating loss carryforwards?"),
    ("one_off", "RestructuringCharges", "How much were the restructuring charges for the year?"),
    ("one_off", "AssetImpairmentCharges", "What asset impairment charges were recorded in the year?"),  # noqa: E501
    ("one_off", "GoodwillImpairmentLoss", "What goodwill impairment was recorded in the year?"),
    ("pension", "DefinedBenefitPlanBenefitObligation",
     "What was the benefit obligation of the defined benefit plans at year end?"),
)  # fmt: skip
NARRATIVE: dict[str, str] = {
    "RestructuringCharges": "What restructuring charges did the company record, and for what?",
    "AssetImpairmentCharges": "What impairment charges were recorded, and why?",
    "GoodwillImpairmentLoss": "Was goodwill impaired, by how much, and why?",
    "ImpairmentOfIntangibleAssetsExcludingGoodwill": "What intangible asset impairments were "
    "recorded, and why?",
    "LossContingencyAccrualAtCarryingValue": "What amount is accrued for litigation or other "
    "contingencies, and for which matters?",
    "LossContingencyEstimateOfPossibleLoss": "What possible losses from legal matters does the "
    "company disclose?",
    "GainLossOnSaleOfBusiness": "What gain or loss did the company record on selling a business?",
    "GainLossOnDispositionOfAssets": "What gain or loss was recorded on disposals of assets?",
    "DebtInstrumentFaceAmount": "What debt did the company issue or hold, with what principal and "
    "terms?",
    "StockRepurchaseProgramAuthorizedAmount1": "How large is the authorized share repurchase "
    "program?",
    "StockRepurchaseProgramRemainingAuthorizedRepurchaseAmount1": "How much remains available "
    "under the share repurchase authorization?",
    "BusinessCombinationConsiderationTransferred1": "What did the company pay for its "
    "acquisitions?",
}
NARRATIVE_LIMIT, PER_COMPANY, PER_CONCEPT = 30, 3, 4


def split(ticker: str) -> str:
    return "test" if hashlib.sha256(ticker.encode()).digest()[0] < TEST_SHARE else "dev"


def fiscal_year(fact: FactRef, end: date) -> bool:
    """Consolidated value at the fiscal year end: an instant, or a duration of about a year."""
    if fact.dimensional or fact.end != end or fact.value is None:
        return False
    return fact.start is None or (end - fact.start) > timedelta(days=340)


def sentence_with(chunk: Chunk, shown: str) -> str | None:
    if not shown:
        return None
    for sentence in re.split(r"(?<=[.;])\s+(?=[A-Z])", chunk.text):
        if shown in sentence and len(sentence) > 40:
            return sentence.strip()
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "cohorts",
        type=Path,
        nargs="*",
        default=[ROOT / f"scripts/cohorts/{n}.json" for n in ("reference", "targeted", "random")],
    )
    parser.add_argument("--as-of", type=date.fromisoformat, default=date(2026, 10, 11))
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    tickers = list(
        dict.fromkeys(
            e["ticker"] for c in args.cohorts for e in json.loads(c.read_text(encoding="utf-8"))
        )
    )
    gold: list[dict[str, Any]] = []
    narrative: list[dict[str, Any]] = []
    with SECClient(load_config(ROOT), offline=args.offline) as sec:
        tick = sec.get_ticker_map()
        for ticker in tickers:
            texts = filing_texts(sec, tick[ticker], args.as_of)
            if not texts or texts[0].period_end is None:
                continue
            text, end = texts[0], texts[0].period_end
            facts = [(c, f) for c in text.chunks for f in c.facts]
            base = {
                "ticker": ticker,
                "cik": tick[ticker],
                "accession": text.accession,
                "form": text.form,
                "filed": str(text.filed),
                "period_end": str(end),
                "split": split(ticker),
                "url": text.url,
            }
            for group, name, question in BANK:
                concept = "us-gaap:" + name
                current = [f for _, f in facts if f.concept == concept and fiscal_year(f, end)]
                values = {round(f.value or 0.0, 6) for f in current}
                if not current or len(values) != 1:
                    continue  # absent, or inconsistent duplicates: not a clean label
                earlier = [
                    f
                    for _, f in facts
                    if f.concept == concept
                    and not f.dimensional
                    and f.end is not None
                    and f.end < end
                    and f.value is not None
                ]
                gold.append(
                    {
                        **base,
                        "id": f"{ticker}:{name}",
                        "field": group,
                        "concept": concept,
                        "question": question,
                        "answer": current[0].value,
                        "unit": current[0].unit,
                        "reference_fact_ids": sorted({f.fact_id for f in current}),
                        "distractors": [
                            {"fact_id": f.fact_id, "period_end": str(f.end), "value": f.value}
                            for f in earlier
                            if f.value != current[0].value
                        ][:4],
                    }
                )
            for chunk, fact in facts:
                name = fact.concept.split(":", 1)[-1]
                if chunk.kind != "text" or name not in NARRATIVE or fact.end is None:
                    continue
                if fact.end < end - timedelta(days=366):
                    continue  # an old comparative, not this year's story
                sentence = sentence_with(chunk, fact.shown)
                if sentence is None:
                    continue
                narrative.append(
                    {
                        **base,
                        "id": f"{ticker}:{name}:{fact.fact_id}",
                        "concept": fact.concept,
                        "question": NARRATIVE[name],
                        "reference_answer": sentence,
                        "reference_fact_ids": [fact.fact_id],
                        "section": chunk.section,
                        "title": chunk.title,
                        "status": "draft",
                    }
                )
            labelled = sum(g["ticker"] == ticker for g in gold)
            drafts = sum(n["ticker"] == ticker for n in narrative)
            print(f"{ticker:6} {labelled:3} labelled, {drafts:3} narrative candidates")

    # Narrative drafts: spread over companies and concepts, deterministic order.
    chosen: list[dict[str, Any]] = []
    per_company: Counter[str] = Counter()
    per_concept: Counter[str] = Counter()
    seen_sentences: set[str] = set()
    for item in sorted(narrative, key=lambda n: hashlib.sha256(n["id"].encode()).hexdigest()):
        if (
            per_company[item["ticker"]] >= PER_COMPANY
            or per_concept[item["concept"]] >= PER_CONCEPT
            or item["reference_answer"] in seen_sentences
        ):
            continue
        chosen.append(item)
        per_company[item["ticker"]] += 1
        per_concept[item["concept"]] += 1
        seen_sentences.add(item["reference_answer"])
        if len(chosen) == NARRATIVE_LIMIT:
            break

    OUT.mkdir(parents=True, exist_ok=True)
    gold_path = OUT / "gold-xbrl.jsonl"
    gold_path.write_text("".join(json.dumps(g) + "\n" for g in gold), encoding="utf-8")
    (OUT / "narrative-draft.jsonl").write_text(
        "".join(json.dumps(n) + "\n" for n in chosen), encoding="utf-8"
    )
    review = ["# Narrative gold set: review", ""]
    review.append(
        "For each item, check that the sentence answers the question from the filing. "
        "Mark one: keep, fix (write the corrected answer), or drop."
    )
    for i, item in enumerate(chosen, 1):
        review += [
            "",
            f"## {i}. {item['ticker']} ({item['split']}): {item['question']}",
            f"- Filing: {item['form']} {item['accession']} ({item['title'][:80]})",
            f"- Reference answer: {item['reference_answer']}",
            "- Decision: [ ] keep  [ ] fix  [ ] drop",
            "- Corrected answer (if fix):",
        ]
    (OUT / "narrative-review.md").write_text("\n".join(review) + "\n", encoding="utf-8")

    by_split: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for g in gold:
        by_split[g["split"]].append(g)
    summary: dict[str, Any] = {
        split_name: {
            "questions": len(items),
            "companies": len({g["ticker"] for g in items}),
            "fields": dict(Counter(g["field"] for g in items)),
            "with_distractors": sum(bool(g["distractors"]) for g in items),
        }
        for split_name, items in sorted(by_split.items())
    }
    summary["narrative"] = {
        "drafts": len(chosen),
        "companies": len({n["ticker"] for n in chosen}),
        "candidates": len(narrative),
    }
    summary["sha256_gold_xbrl"] = hashlib.sha256(gold_path.read_bytes()).hexdigest()
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
