"""CPU-only, sequential Ollama benchmark; no user .env or response cache."""

import argparse
import json
import statistics
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import httpx
import psutil
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    intent: Literal["data", "wacc", "valuation", "audit", "modify"]
    rationale: str = Field(min_length=1, max_length=180)


CASES = (
    ("What was revenue last year?", "data"),
    ("Estimate the cost of capital.", "wacc"),
    ("What is fair value per share?", "valuation"),
    ("Where did the debt figure come from?", "audit"),
    ("Set operating margin to 25%.", "modify"),
    ("Show the latest cash balance.", "data"),
    ("Which beta should we use for WACC?", "wacc"),
    ("Build the full DCF valuation.", "valuation"),
    ("Explain the source of this tax rate.", "audit"),
    ("Change terminal growth to 2%.", "modify"),
    ("How much revenue did the company report?", "data"),
    ("What discount rate do you recommend?", "wacc"),
    ("Give me a quick fair-value estimate.", "valuation"),
    ("Show the provenance of shares outstanding.", "audit"),
    ("Replace the bull-case growth assumption.", "modify"),
    ("Show historical operating income.", "data"),
    ("Compare bottom-up and regression beta.", "wacc"),
    ("Calculate the intrinsic equity value.", "valuation"),
    ("Which filing supports this lease liability?", "audit"),
    ("Update the forecast tax rate to 21%.", "modify"),
)

SUITE_VERSION = "intent-v2"
INTENT_CRITERIA = """Classify the user's requested action using these definitions:
data: retrieve reported or historical financial figures, without valuation or source tracing.
wacc: estimate or choose the discount rate, cost of capital, or beta method.
valuation: calculate fair/intrinsic value or build a DCF valuation model.
audit: trace where an existing number came from, including its source or filing evidence.
modify: explicitly change, set, replace, or update a model assumption.
Classify the action, not just the financial noun. A request to change a tax rate is modify;
a request for its source is audit. A request about beta selection is wacc.
Return schema-matching JSON with a brief one-sentence rationale.
Request: """


class MemorySampler:
    def __init__(self) -> None:
        self.stop = threading.Event()
        self.peak_rss = 0
        self.peak_system = 0
        self.thread = threading.Thread(target=self.sample, daemon=True)

    def sample(self) -> None:
        while not self.stop.is_set():
            rss = 0
            for process in psutil.process_iter(["name", "memory_info"]):
                try:
                    if any(
                        name in (process.info["name"] or "").lower() for name in ("ollama", "llama")
                    ):
                        rss += process.info["memory_info"].rss
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            self.peak_rss = max(self.peak_rss, rss)
            self.peak_system = max(self.peak_system, psutil.virtual_memory().used)
            self.stop.wait(0.1)


def run_model(client: httpx.Client, model: str, root: Path, count: int) -> dict[str, Any]:
    details = client.post("/api/show", json={"model": model})
    details.raise_for_status()
    metadata = details.json()
    sampler = MemorySampler()
    sampler.thread.start()
    records: list[dict[str, Any]] = []
    rates_in: list[float] = []
    rates_out: list[float] = []
    peak_resident_bytes = 0
    try:
        for index, (question, expected) in enumerate(CASES[:count]):
            payload: dict[str, Any] = {
                "model": model,
                "stream": False,
                "think": False,
                "keep_alive": "30s",
                "format": Answer.model_json_schema(),
                "options": {
                    "num_ctx": 4096,
                    "num_predict": 160,
                    "temperature": 0,
                    "num_thread": psutil.cpu_count(logical=False) or 1,
                    "num_gpu": 0,
                },
                "messages": [
                    {
                        "role": "user",
                        "content": INTENT_CRITERIA + question,
                    }
                ],
            }
            first_valid = correct = valid = False
            predicted: str | None = None
            start = time.monotonic()
            attempts = 0
            failure: str | None = None
            for attempt in range(2):
                attempts += 1
                try:
                    response = client.post("/api/chat", json=payload)
                    response.raise_for_status()
                    body = response.json()
                    if body.get("done") is not True:
                        raise ValueError("Incomplete reply")
                    for count_key, duration_key, target in (
                        ("prompt_eval_count", "prompt_eval_duration", rates_in),
                        ("eval_count", "eval_duration", rates_out),
                    ):
                        duration = body.get(duration_key, 0) / 1e9
                        if duration > 0:
                            target.append(body.get(count_key, 0) / duration)
                    content = body["message"]["content"]
                    try:
                        answer = Answer.model_validate_json(content)
                    except ValidationError as error:
                        payload["messages"].extend(
                            [
                                {"role": "assistant", "content": content},
                                {
                                    "role": "user",
                                    "content": "Repair JSON: "
                                    + json.dumps(
                                        error.errors(
                                            include_input=False,
                                            include_context=False,
                                            include_url=False,
                                        )
                                    ),
                                },
                            ]
                        )
                        continue
                    valid = True
                    first_valid = attempt == 0
                    correct = answer.intent == expected
                    predicted = answer.intent
                    break
                except (httpx.HTTPError, ValueError, KeyError, TypeError):
                    failure = "transport_or_envelope_error"
                    break
            resident_response = client.get("/api/ps")
            resident_response.raise_for_status()
            for resident in resident_response.json().get("models", []):
                if resident["name"] == model:
                    peak_resident_bytes = max(peak_resident_bytes, resident["size"])
            client.post("/api/generate", json={"model": model, "keep_alive": 0}).raise_for_status()
            record: dict[str, Any] = {
                "case": index + 1,
                "question": question,
                "expected": expected,
                "predicted": predicted,
                "first_valid": first_valid,
                "valid": valid,
                "correct": correct,
                "attempts": attempts,
                "failure": failure,
                "wall_seconds": time.monotonic() - start,
            }
            records.append(record)
            print(f"{model}: {index + 1}/{count} valid={valid} correct={correct}", flush=True)
            (root / "data/benchmarks").mkdir(parents=True, exist_ok=True)
            (root / "data/benchmarks" / (model.replace(":", "_") + ".json")).write_text(
                json.dumps(records, indent=2), encoding="utf-8"
            )
    finally:
        try:
            client.post("/api/generate", json={"model": model, "keep_alive": 0}).raise_for_status()
        finally:
            sampler.stop.set()
            sampler.thread.join()
    return {
        "model": model,
        "suite_version": SUITE_VERSION,
        "details": metadata.get("details"),
        "license_apache": "Apache" in metadata.get("license", ""),
        "cases": len(records),
        "first_valid": sum(bool(r["first_valid"]) for r in records),
        "valid": sum(bool(r["valid"]) for r in records),
        "correct": sum(bool(r["correct"]) for r in records),
        "prompt_tps": statistics.median(rates_in) if rates_in else 0,
        "generation_tps": statistics.median(rates_out) if rates_out else 0,
        "ollama_peak_rss_gb": sampler.peak_rss / 1024**3,
        "ollama_resident_estimate_gb": peak_resident_bytes / 1024**3,
        "peak_system_used_gb": sampler.peak_system / 1024**3,
        "median_wall_seconds": statistics.median(float(r["wall_seconds"]) for r in records),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("models", nargs="+")
    parser.add_argument("--count", type=int, default=20, choices=range(1, 21))
    args = parser.parse_args()
    project_root = Path(__file__).resolve().parents[1]
    root = project_root / "data/benchmark-runs" / SUITE_VERSION
    result_dir = root / "data/benchmarks"
    result_dir.mkdir(parents=True, exist_ok=True)
    results = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(result_dir.glob("*.summary.json"))
    ]
    with httpx.Client(
        base_url="http://127.0.0.1:11434", timeout=180, trust_env=False, follow_redirects=False
    ) as client:
        # Unload existing residents before measuring sequential CPU runs.
        residents = client.get("/api/ps")
        residents.raise_for_status()
        for resident in residents.json().get("models", []):
            client.post(
                "/api/generate", json={"model": resident["name"], "keep_alive": 0}
            ).raise_for_status()
        for model in args.models:
            result = run_model(client, model, root, args.count)
            (result_dir / (model.replace(":", "_") + ".summary.json")).write_text(
                json.dumps(result, indent=2), encoding="utf-8"
            )
            results = [item for item in results if item["model"] != model]
            results.append(result)
            stamp = datetime.now(UTC).isoformat()
            lines = [
                "# Local CPU benchmarks",
                "",
                f"Measured: {stamp}",
                "",
                f"Physical cores: {psutil.cpu_count(logical=False)}; "
                f"RAM: {psutil.virtual_memory().total / 1024**3:.1f} GiB.",
                "",
                "4096 context, 160 output tokens, temperature 0, thinking off, GPU off. "
                "Each request inspects resident size then unloads the model. No response cache. "
                "Twenty short intent tasks with explicit action definitions; "
                "not an extraction, forecast-quality, or long-context evaluation.",
                "",
                "| Model | First valid | After repair | Correct intent | Prompt tok/s | "
                "Generation tok/s | Peak Ollama RSS GiB | Resident estimate GiB | "
                "Peak system used GiB | Median call s |",
                "|---|---|---|---|---|---|---|---|---|---|",
            ]
            for result in results:
                lines.append(
                    f"| {result['model']} | {result['first_valid']}/{result['cases']} | "
                    f"{result['valid']}/{result['cases']} | "
                    f"{result['correct']}/{result['cases']} | "
                    f"{result['prompt_tps']:.1f} | {result['generation_tps']:.1f} | "
                    f"{result['ollama_peak_rss_gb']:.2f} | "
                    f"{result['ollama_resident_estimate_gb']:.2f} | "
                    f"{result['peak_system_used_gb']:.2f} | "
                    f"{result['median_wall_seconds']:.1f} |"
                )
            lines.extend(
                [
                    "",
                    "RSS sums accessible Ollama processes and may double-count shared pages; "
                    "system memory includes other desktop applications. Sampling interval: 100 ms. "
                    "Throughput is median API-reported evaluation speed, excluding model loading.",
                    "Resident size is an Ollama estimate, not peak RSS. "
                    "RSS below the resident estimate may reflect incomplete sampling.",
                    "",
                    "The intent-v2 prompt explicitly defines all five actions. "
                    "Do not compare accuracy directly with the earlier undefined-label benchmark.",
                    "",
                    "Detailed model metadata:",
                    "",
                    "```json",
                    json.dumps(results, indent=2),
                    "```",
                    "",
                ]
            )
            (project_root / "docs/benchmarks-light.md").write_text(
                "\n".join(lines), encoding="utf-8"
            )


if __name__ == "__main__":
    main()
