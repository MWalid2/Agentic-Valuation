"""Score the Router on evals/router/prompts.jsonl (expected answers written first).

A prompt passes when the company status and ticker (or, when ambiguous, the
candidate list), the date (or its rejection), the intent and the scope verdict
all match. "Today" is fixed so relative dates are reproducible.
"""

import argparse
import json
import time
from datetime import date
from pathlib import Path
from statistics import median
from typing import Any

from dcf_agent.agents import router as router_module
from dcf_agent.agents.router import Route, Router
from dcf_agent.config import load_config
from dcf_agent.data.sec import SECClient
from dcf_agent.llm import LocalLLM

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 10, 15)


def score(route: Route, expected: dict[str, Any]) -> dict[str, bool]:
    tickers = [c.ticker for c in route.candidates]
    company = route.status == expected["status"] and (
        (route.company is not None and route.company.ticker == expected["ticker"])
        if expected["status"] == "resolved"
        else expected["ticker"] in tickers
    )
    if expected["date"] == "reject":
        when = route.date_error is not None
    else:
        when = route.date_error is None and str(route.valuation_date) == expected["date"]
    scope = True
    if expected["status"] == "resolved":
        blocked = route.scope is not None and route.scope.status == "blocked"
        scope = route.scope is not None and blocked == (expected["scope"] == "out")
    return {
        "company": company,
        "date": when,
        "intent": route.intent == expected["intent"],
        "scope": scope,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompts", type=Path, default=ROOT / "evals/router/prompts.jsonl")
    parser.add_argument("--no-model", action="store_true", help="Rules only; no model calls")
    parser.add_argument(
        "--model-intent",
        action="store_true",
        help="Disable the intent patterns so the model labels every question",
    )
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--out", type=Path, default=ROOT / "outputs/router-eval.json")
    args = parser.parse_args()
    config = load_config(ROOT)
    if not args.no_model:
        config = config.model_copy(
            update={"settings": config.settings.model_copy(update={"mock_llm": False})}
        )
    if args.model_intent:
        router_module.RULES = ()
    prompts = [json.loads(line) for line in args.prompts.read_text().splitlines() if line]
    results = []
    with SECClient(config, offline=args.offline) as sec:
        llm = None if args.no_model else LocalLLM(config)
        try:
            router = Router(sec, TODAY, llm)
            for case in prompts:
                start = time.monotonic()
                route = router.route(case["text"])
                seconds = time.monotonic() - start
                checks = score(route, case)
                results.append(
                    {
                        "id": case["id"],
                        "text": case["text"],
                        "passed": all(checks.values()),
                        "checks": checks,
                        "seconds": round(seconds, 2),
                        "got": {
                            "status": route.status,
                            "ticker": route.company.ticker if route.company else None,
                            "candidates": [c.ticker for c in route.candidates],
                            "date": str(route.valuation_date),
                            "date_error": route.date_error,
                            "intent": route.intent,
                            "intent_source": route.intent_source,
                            "extraction": route.extraction,
                            "scope": route.scope.status if route.scope else None,
                            "notes": route.notes,
                        },
                        "expected": {k: case[k] for k in ("status", "ticker", "date", "intent")}
                        | {"scope": case["scope"]},
                    }
                )
                mark = "OK  " if results[-1]["passed"] else "FAIL"
                failed = [k for k, ok in checks.items() if not ok]
                print(f"{mark} {case['id']:2} {case['text'][:60]:60} {' '.join(failed)}")
        finally:
            if llm is not None:
                llm.close()
    passed = sum(r["passed"] for r in results)
    fields = {k: sum(r["checks"][k] for r in results) for k in results[0]["checks"]}
    summary = {
        "passed": passed,
        "total": len(results),
        "accuracy": passed / len(results),
        "fields": fields,
        "median_seconds": median(r["seconds"] for r in results),
        "max_seconds": max(r["seconds"] for r in results),
        "model_extractions": sum(r["got"]["extraction"] == "model" for r in results),
        "model_intents": sum(r["got"]["intent_source"] == "model" for r in results),
        "mode": "rules only" if args.no_model else "model intent" if args.model_intent else "both",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"summary": summary, "results": results}, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
