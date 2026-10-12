"""Live Phase 0 acceptance through the actual wrapper, with no response cache."""

import json
import statistics
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml
from benchmark_llm import CASES, INTENT_CRITERIA, Answer, MemorySampler

from dcf_agent.config import AppConfig, ModelsConfig, Settings
from dcf_agent.llm import LLMError, LocalLLM


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    models = ModelsConfig.model_validate(
        yaml.safe_load((root / "config/models.yaml").read_text(encoding="utf-8"))
    )
    models = models.model_copy(
        update={"runtime": models.runtime.model_copy(update={"response_cache": False})}
    )
    config = AppConfig(
        settings=Settings(project_root=root, mock_llm=False, data_dir=Path("data/wrapper-check")),
        models=models,
    )
    summaries: list[dict[str, Any]] = []
    with httpx.Client(base_url=config.settings.ollama_url, trust_env=False, timeout=180) as client:
        residents = client.get("/api/ps")
        residents.raise_for_status()
        for resident in residents.json()["models"]:
            response = client.post(
                "/api/generate", json={"model": resident["name"], "keep_alive": 0}
            )
            response.raise_for_status()
        with LocalLLM(config) as llm:
            for role in ("router", "filings"):
                sampler = MemorySampler()
                sampler.thread.start()
                records: list[dict[str, Any]] = []
                try:
                    for index, (question, expected) in enumerate(CASES, 1):
                        start = time.monotonic()
                        record: dict[str, Any] = {
                            "question": question,
                            "expected": expected,
                            "valid": False,
                        }
                        try:
                            result = llm.generate(
                                role,
                                INTENT_CRITERIA + question,
                                Answer,
                                prompt_version="wrapper-acceptance-v1",
                            )
                            record.update(
                                valid=result.origin == "live",
                                attempts=result.attempts,
                                predicted=result.value.intent,
                                correct=result.value.intent == expected,
                            )
                        except LLMError as error:
                            record["error"] = str(error)
                        record["seconds"] = time.monotonic() - start
                        records.append(record)
                        print(f"{role} {index}/20: {record}", flush=True)
                finally:
                    sampler.stop.set()
                    sampler.thread.join()
                first = sum(r["valid"] and r["attempts"] == 1 for r in records)
                valid = sum(r["valid"] for r in records)
                summaries.append(
                    {
                        "role": role,
                        "model": models.roles[role].model,
                        "first_valid": first,
                        "final_valid": valid,
                        "correct": sum(r.get("correct", False) for r in records),
                        "median_seconds": statistics.median(r["seconds"] for r in records),
                        "peak_rss_gib": sampler.peak_rss / 1024**3,
                        "peak_system_gib": sampler.peak_system / 1024**3,
                        "passed": first >= 19 and valid == 20,
                        "records": records,
                    }
                )
        remaining = client.get("/api/ps")
        remaining.raise_for_status()
        unloaded = not remaining.json()["models"]
    output = root / "data/wrapper-check/results.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "models_unloaded": unloaded,
                "summaries": summaries,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    if not unloaded or not all(s["passed"] for s in summaries):
        raise SystemExit("Wrapper acceptance failed; inspect data/wrapper-check/results.json")
    print("Wrapper acceptance passed for both models; all models unloaded.")


if __name__ == "__main__":
    main()
