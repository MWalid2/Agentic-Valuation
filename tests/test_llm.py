"""Offline structured generation, repair, caching, and serialized transport."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal

import httpx
import pytest
from pydantic import BaseModel, ConfigDict

from dcf_agent.config import ROLES, AppConfig, ModelsConfig, Settings
from dcf_agent.llm import LLMError, LocalLLM, MockBackend
from dcf_agent.llm.client import OllamaBackend


class Choice(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    level: Literal["low", "medium", "high"]


def config(root: Path, *, live: bool = False, cache: bool = True) -> AppConfig:
    return AppConfig(
        settings=Settings(project_root=root, mock_llm=not live),
        models=ModelsConfig.model_validate(
            {
                "roles": {role: {"size_class": "S", "model": "fixture-model"} for role in ROLES},
                "runtime": {"response_cache": cache},
            }
        ),
    )


def test_valid_output_and_restart_cache(tmp_path: Path) -> None:
    with LocalLLM(config(tmp_path), mock_responses=['{"level":"low"}']) as llm:
        first = llm.generate("router", "Select a level", Choice, prompt_version="v1")
        assert first.value.level == "low"
        assert first.origin == "mock"
    with LocalLLM(config(tmp_path)) as llm:
        second = llm.generate("router", "Select a level", Choice, prompt_version="v1")
        assert second.origin == "cache"
        assert second.attempts == 0


def test_single_repair_and_payload(tmp_path: Path) -> None:
    backend = MockBackend(['{"level":"wrong"}', '{"level":"high"}'])
    with LocalLLM(config(tmp_path), backend=backend) as llm:
        result = llm.generate("forecast", "Choose", Choice, prompt_version="v1")
    assert result.repaired and result.attempts == 2
    assert backend.requests[0]["format"] == Choice.model_json_schema()
    assert backend.requests[0]["keep_alive"] == 0
    assert backend.requests[0]["think"] is False
    assert "Repair the JSON" in backend.requests[1]["messages"][-1]["content"]


def test_failed_repair_fallback_is_flagged_and_not_cached(tmp_path: Path) -> None:
    with LocalLLM(config(tmp_path), mock_responses=["bad", "bad"]) as llm:
        result = llm.generate(
            "router", "Choose", Choice, prompt_version="v1", fallback=lambda: Choice(level="medium")
        )
    assert result.origin == "fallback" and result.warning
    assert result.attempts == 2
    assert not list((tmp_path / "data/llm_cache").glob("**/*.json"))


def test_failed_repair_raises_without_default(tmp_path: Path) -> None:
    with LocalLLM(config(tmp_path), mock_responses=["bad", "bad"]) as llm:
        with pytest.raises(LLMError):
            llm.generate("router", "Choose", Choice, prompt_version="v1")


def test_cache_key_changes_with_version_and_mode(tmp_path: Path) -> None:
    with LocalLLM(config(tmp_path), mock_responses=['{"level":"low"}'] * 2) as llm:
        one = llm.generate("router", "Choose", Choice, prompt_version="v1")
        two = llm.generate("router", "Choose", Choice, prompt_version="v2")
    assert one.cache_key != two.cache_key
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "done": True,
                "message": {"content": '{"level":"high"}'},
            },
        )
    )
    cfg = config(tmp_path, live=True)
    with LocalLLM(cfg, backend=OllamaBackend(cfg, transport=transport)) as llm:
        live = llm.generate("router", "Choose", Choice, prompt_version="v1")
    assert live.origin == "live" and live.cache_key != one.cache_key


def test_corrupt_cache_is_revalidated_and_regenerated(tmp_path: Path) -> None:
    with LocalLLM(config(tmp_path), mock_responses=['{"level":"low"}']) as llm:
        first = llm.generate("router", "Choose", Choice, prompt_version="v1")
    path = tmp_path / "data/llm_cache/mock" / (first.cache_key + ".json")
    path.write_text("broken", encoding="utf-8")
    with LocalLLM(config(tmp_path), mock_responses=['{"level":"high"}']) as llm:
        assert llm.generate("router", "Choose", Choice, prompt_version="v1").value.level == "high"


def test_one_worker_under_concurrent_callers(tmp_path: Path) -> None:
    threads: set[int] = set()

    class RecordingMock(MockBackend):
        def generate(self, payload: dict[str, Any]) -> Any:
            threads.add(threading.get_ident())
            return super().generate(payload)

    backend = RecordingMock(['{"level":"low"}'] * 8)
    with LocalLLM(config(tmp_path, cache=False), backend=backend) as llm:
        with ThreadPoolExecutor(max_workers=4) as callers:
            results = list(
                callers.map(
                    lambda i: llm.generate("router", str(i), Choice, prompt_version="v1"), range(8)
                )
            )
    assert len(results) == 8 and len(threads) == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(200, json={"done": False}),
        httpx.Response(200, json={"done": True, "message": {"content": 1}}),
    ],
)
def test_invalid_http_response_falls_back(tmp_path: Path, response: httpx.Response) -> None:
    cfg = config(tmp_path, live=True)
    transport = httpx.MockTransport(lambda request: response)
    with LocalLLM(cfg, backend=OllamaBackend(cfg, transport=transport)) as llm:
        result = llm.generate(
            "router", "Choose", Choice, prompt_version="v1", fallback=lambda: Choice(level="low")
        )
    assert result.origin == "fallback" and result.attempts == 1


def test_timeout_is_not_retried(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    cfg = config(tmp_path, live=True)
    with LocalLLM(cfg, backend=OllamaBackend(cfg, transport=httpx.MockTransport(handler))) as llm:
        with pytest.raises(LLMError):
            llm.generate("router", "Choose", Choice, prompt_version="v1")


def test_traces_exclude_prompt_and_response_text(tmp_path: Path) -> None:
    with LocalLLM(config(tmp_path), mock_responses=['{"level":"low"}']) as llm:
        llm.generate("router", "private fixture text", Choice, prompt_version="v1")
    trace = (tmp_path / "data/traces/llm.jsonl").read_text(encoding="utf-8")
    assert "private fixture text" not in trace and '"level"' not in trace
    assert json.loads(trace)["status"] == "valid"


def test_closed_wrapper_and_disabled_cache(tmp_path: Path) -> None:
    backend = MockBackend(['{"level":"low"}'] * 2)
    llm = LocalLLM(config(tmp_path, cache=False), backend=backend)
    for _ in range(2):
        llm.generate("router", "Choose", Choice, prompt_version="v1")
    assert len(backend.requests) == 2
    llm.close()
    with pytest.raises(LLMError, match="closed"):
        llm.generate("router", "Choose", Choice, prompt_version="v1")
