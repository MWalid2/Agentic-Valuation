"""Phase 6 step 5: compare open models per role, on the same inputs and the role's checks.

    uv run python scripts/compare_models.py forecast --models qwen3:1.7b qwen3:4b qwen3:8b
    uv run python scripts/compare_models.py comps
    uv run python scripts/compare_models.py critic

The role's model is swapped through a config override; the response cache is off, so
every call runs and times are real. Scores:
- forecast: the model's pick used (not the fallback), support-rule rejections, hard
  and soft bounds, spread (bull and bear at least 15% from base);
- comps: keep/drop against the hand labels in `evals/comps/peer-labels.json`
  (accuracy, wrong peers kept, good peers dropped);
- critic: seeded story flaws on KO (a story contradicting its numbers, bull identical
  to base, an unsupported claim): caught when a concern cites the seeded story or the
  matching rubric score is 3 or lower; false alarms on the unchanged stories.
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from dcf_agent.agents.comps import CompsAgent
from dcf_agent.agents.critic import review, run_rubric
from dcf_agent.agents.forecast import ForecastAgent
from dcf_agent.config import AppConfig, load_config
from dcf_agent.contracts.external import DataBundle
from dcf_agent.data.damodaran import DamodaranClient
from dcf_agent.data.prices import PriceClient
from dcf_agent.data.sec import SECClient
from dcf_agent.engine.assemble import assemble
from dcf_agent.engine.drivers import check_coherence, driver_stats, industry_benchmarks, quick_dcf
from dcf_agent.engine.normalize import Policy, normalize
from dcf_agent.engine.valuation import value_scenarios
from dcf_agent.engine.wacc import compute_wacc
from dcf_agent.llm import LocalLLM

ROOT = Path(__file__).resolve().parents[1]
BUNDLES = ROOT / "outputs/insights-cohort"
SPREAD = 0.15


def configured(base: AppConfig, role: str, model: str) -> AppConfig:
    roles = dict(base.models.roles)
    roles[role] = roles[role].model_copy(update={"model": model})  # type: ignore[index]
    runtime = base.models.runtime.model_copy(update={"response_cache": False})
    models = base.models.model_copy(update={"roles": roles, "runtime": runtime})
    settings = base.settings.model_copy(update={"mock_llm": False})
    return base.model_copy(update={"models": models, "settings": settings})


def pieces(ticker: str) -> tuple[DataBundle, Any, Any, Any]:
    bundle = DataBundle.model_validate_json(
        (BUNDLES / f"{ticker}.json").read_text(encoding="utf-8")
    )
    assembled = assemble(bundle, marginal_tax=0.25)
    wacc = compute_wacc(assembled.wacc_inputs)
    assert bundle.raw is not None
    normalized = normalize(bundle.raw, Policy(marginal_tax_rate=0.25))[0]
    return bundle, assembled, wacc, normalized


def forecast_role(llm: LocalLLM, tickers: list[str]) -> list[dict[str, Any]]:
    rows = []
    for ticker in tickers:
        bundle, assembled, wacc, normalized = pieces(ticker)
        began = time.monotonic()
        result = ForecastAgent(llm).run(bundle, normalized, wacc, assembled)
        seconds = time.monotonic() - began
        stats, industry = driver_stats(normalized), industry_benchmarks(bundle)
        values, hard, soft = {}, [], []
        for item in result.scenarios.scenarios if result.scenarios else ():
            preview = quick_dcf(item, wacc, assembled)
            checked = check_coherence(
                item, wacc, history=stats, industry=industry, preview=preview.result
            )
            values[item.scenario] = preview.per_share
            hard += list(checked.hard)
            soft += list(checked.soft)
        base = values.get("base")
        rows.append(
            {
                "ticker": ticker,
                "model_pick": result.chosen_by == "model",
                "rejected": sum("rejected" in n.lower() for n in result.notes),
                "hard_ok": bool(values) and not hard,
                "soft_ok": bool(values) and not soft,
                "spread_ok": base is not None
                and base > 0
                and (values.get("bull") or 0) >= base * (1 + SPREAD)
                and (values.get("bear") or 0) <= base * (1 - SPREAD),
                "seconds": round(seconds, 1),
            }
        )
        print(f"  {ticker:5} {rows[-1]}", flush=True)
    return rows


def comps_role(llm: LocalLLM, config: AppConfig, tickers: list[str]) -> list[dict[str, Any]]:
    labels = json.loads((ROOT / "evals/comps/peer-labels.json").read_text(encoding="utf-8"))
    rows = []
    with (
        SECClient(config, offline=True) as sec,
        PriceClient(config, offline=True) as prices,
        DamodaranClient(config, offline=True) as damodaran,
    ):
        classification, _ = damodaran.get_classification()
        agent = CompsAgent(sec, prices, llm)
        for ticker in tickers:
            truth = {k: v for k, v in labels.get(ticker, {}).items() if not k.startswith("_")}
            if not truth:
                continue
            bundle, assembled, _, _ = pieces(ticker)
            industry = bundle.industry.name if bundle.industry else ""
            found = agent.find(
                ticker,
                industry,
                bundle.request.valuation_date,
                classification,
                assembled.base_revenue,
            )
            found = [c for c in found if c.ticker in truth]
            began = time.monotonic()
            verdicts = agent.review(ticker, industry, found)
            seconds = time.monotonic() - began
            # The model's own reading, before the fill-to-five rule.
            kept = {t: v.keep and not v.reason.startswith("restored") for t, v in verdicts.items()}
            fell_back = all(v.reason == "kept: no model reading" for v in verdicts.values())
            right = sum(kept[t] == truth[t] for t in kept)
            rows.append(
                {
                    "ticker": ticker,
                    "candidates": len(kept),
                    "accuracy": round(right / len(kept), 3) if kept else None,
                    "wrong_kept": [t for t in kept if kept[t] and not truth[t]],
                    "good_dropped": [t for t in kept if not kept[t] and truth[t]],
                    "fell_back": fell_back,
                    "seconds": round(seconds, 1),
                }
            )
            print(f"  {ticker:5} {rows[-1]}", flush=True)
    return rows


SEEDS = {
    "contradiction": (
        "base",
        "Margins collapse to about 10% as competition intensifies, and revenue shrinks every "
        "year of the forecast.",
        "narrative_consistency",
    ),
    "same_story": ("bull", None, "scenario_distinctness"),
    "unsupported": (
        "base",
        "The company will double its market share next year after acquiring its largest "
        "competitor, which management has already agreed.",
        "unsupported_claims",
    ),
}


def critic_role(llm: LocalLLM, tickers: list[str]) -> list[dict[str, Any]]:
    rows = []
    for ticker in tickers:
        bundle, assembled, wacc, normalized = pieces(ticker)
        forecast = ForecastAgent(None).run(bundle, normalized, wacc, assembled)
        assert forecast.scenarios is not None
        valuation = value_scenarios(bundle, forecast.scenarios, wacc, 0.25)
        clean = {
            "data": bundle,
            "wacc": wacc,
            "forecast": forecast.scenarios,
            "valuation": valuation,
        }
        base_story = next(s.narrative for s in forecast.scenarios.scenarios if s.scenario == "base")
        cases: list[tuple[str, dict[str, Any], str | None, str | None]] = [
            ("clean", clean, None, None)
        ]
        for name, (scenario, text, criterion) in SEEDS.items():
            story = (
                base_story
                if text is None
                else (base_story + " " + text if name == "unsupported" else text)
            )
            edited = tuple(
                s.model_copy(update={"narrative": story}) if s.scenario == scenario else s
                for s in forecast.scenarios.scenarios
            )
            state = {
                **clean,
                "forecast": forecast.scenarios.model_copy(update={"scenarios": edited}),
            }
            cases.append((name, state, scenario, criterion))
        for case, setup, seeded, rubric_key in cases:
            began = time.monotonic()
            report = run_rubric(llm, setup, review(setup))
            seconds = time.monotonic() - began
            concerns = [i for i in report.issues if i.issue_id.startswith("rubric")]
            cited = [i.evidence_refs[0].split(":")[0] for i in concerns]
            ran = bool(report.rubric_scores)
            caught = None
            if seeded is not None:
                low = (
                    ran and rubric_key is not None and report.rubric_scores.get(rubric_key, 5) <= 3
                )
                caught = ran and (f"{seeded}.story" in cited or low)
            rows.append(
                {
                    "ticker": ticker,
                    "case": case,
                    "ran": ran,
                    "caught": caught,
                    "concerns": len(concerns),
                    "cited": cited,
                    "scores": report.rubric_scores,
                    "seconds": round(seconds, 1),
                }
            )
            print(f"  {ticker:5} {case:13} {rows[-1]}", flush=True)
    return rows


def summary(role: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not rows:
        return {}
    seconds = round(sum(r["seconds"] for r in rows) / len(rows), 1)
    if role == "forecast":
        n = len(rows)
        return {
            "companies": n,
            "model_pick": sum(r["model_pick"] for r in rows),
            "rejected": sum(r["rejected"] for r in rows),
            "hard_ok": sum(r["hard_ok"] for r in rows),
            "soft_ok": sum(r["soft_ok"] for r in rows),
            "spread_ok": sum(r["spread_ok"] for r in rows),
            "seconds_per_company": seconds,
        }
    if role == "comps":
        scored = [r for r in rows if r["accuracy"] is not None and not r["fell_back"]]
        total = sum(r["candidates"] for r in scored)
        right = sum(round(r["accuracy"] * r["candidates"]) for r in scored)
        return {
            "companies": len(rows),
            "answered": len(scored),
            "accuracy": round(right / total, 3) if total else None,
            "wrong_kept": sum(len(r["wrong_kept"]) for r in scored),
            "good_dropped": sum(len(r["good_dropped"]) for r in scored),
            "seconds_per_call": seconds,
        }
    seeded = [r for r in rows if r["caught"] is not None]
    clean = [r for r in rows if r["case"] == "clean"]
    return {
        "calls": len(rows),
        "ran": sum(r["ran"] for r in rows),
        "seeded_caught": f"{sum(bool(r['caught']) for r in seeded)}/{len(seeded)}",
        "false_alarms_on_clean": sum(r["concerns"] for r in clean),
        "seconds_per_call": seconds,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("forecast", "comps", "critic"))
    parser.add_argument("--models", nargs="+", default=["qwen3:1.7b", "qwen3:4b", "qwen3:8b"])
    parser.add_argument("--tickers", nargs="*")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    defaults = {
        "forecast": ["AAPL", "CAT", "KO", "MSFT", "NKE"],
        "comps": ["AAPL", "BA", "CAT", "DE", "KO", "MCD", "MSFT", "NKE", "NVDA", "SNOW"],
        "critic": ["KO"],
    }
    tickers = args.tickers or defaults[args.role]
    base = load_config(ROOT)
    out = args.out or ROOT / f"outputs/model-comparison/{args.role}.json"
    results: dict[str, Any] = json.loads(out.read_text()) if out.exists() else {}
    for model in args.models:
        print(f"{args.role} with {model}", flush=True)
        config = configured(base, args.role, model)
        with LocalLLM(config) as llm:
            if args.role == "forecast":
                rows = forecast_role(llm, tickers)
            elif args.role == "comps":
                rows = comps_role(llm, config, tickers)
            else:
                rows = critic_role(llm, tickers)
        results[model] = {"summary": summary(args.role, rows), "rows": rows}
        print(f"  summary {results[model]['summary']}", flush=True)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(results, indent=2, default=str))
    print(json.dumps({m: r["summary"] for m, r in results.items()}, indent=2))


if __name__ == "__main__":
    main()
