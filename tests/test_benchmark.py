"""Check benchmark accounting using simulated Ollama timings and repairs."""

import importlib.util
import json
from pathlib import Path

import httpx


def test_benchmark_counts_repair_and_accuracy_separately(tmp_path: Path) -> None:
    path = Path(__file__).resolve().parents[1] / "scripts/benchmark_llm.py"
    spec = importlib.util.spec_from_file_location("benchmark_llm", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path == "/api/show":
            return httpx.Response(200, json={"details": {}, "license": "Apache"})
        if request.url.path == "/api/generate":
            return httpx.Response(200, json={"done": True})
        if request.url.path == "/api/ps":
            return httpx.Response(200, json={"models": []})
        payload = json.loads(request.content)
        assert payload["messages"][0]["content"].startswith(module.INTENT_CRITERIA)
        assert payload["options"]["num_gpu"] == 0
        assert payload["think"] is False
        calls += 1
        content = "bad" if calls == 1 else '{"intent":"data","rationale":"Reported figure"}'
        return httpx.Response(
            200,
            json={
                "done": True,
                "message": {"content": content},
                "prompt_eval_count": 20,
                "prompt_eval_duration": 1_000_000_000,
                "eval_count": 10,
                "eval_duration": 2_000_000_000,
            },
        )

    with httpx.Client(
        base_url="http://localhost", transport=httpx.MockTransport(handler)
    ) as client:
        result = module.run_model(client, "fixture", tmp_path, 2)
    assert result["first_valid"] == 1
    assert result["valid"] == 2
    assert result["correct"] == 1
    assert result["prompt_tps"] == 20
    assert result["generation_tps"] == 5
    assert calls == 3
