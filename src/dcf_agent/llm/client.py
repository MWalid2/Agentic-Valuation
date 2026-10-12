"""Serialized local structured generation, replay cache, and offline fixtures."""

import hashlib
import json
import os
import tempfile
import time
from collections import deque
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal, Protocol, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from dcf_agent.config import AppConfig, Role


class LLMError(RuntimeError):
    """A bounded failure the agent can route to its deterministic default."""


class BackendReply(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    content: str
    prompt_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class Backend(Protocol):
    def generate(self, payload: dict[str, Any]) -> BackendReply: ...
    def close(self) -> None: ...


class MockBackend:
    """Consume explicit fixtures; never invent a valid response from a schema."""

    def __init__(self, responses: Iterable[str | BackendReply]) -> None:
        self.responses = deque(responses)
        self.requests: list[dict[str, Any]] = []

    def generate(self, payload: dict[str, Any]) -> BackendReply:
        self.requests.append(payload)
        if not self.responses:
            raise LLMError("Mock response fixtures exhausted")
        response = self.responses.popleft()
        return response if isinstance(response, BackendReply) else BackendReply(content=response)

    def close(self) -> None:
        pass


class OllamaBackend:
    """HTTP transport with no proxies/redirects and immediate model unloading."""

    def __init__(self, config: AppConfig, *, transport: httpx.BaseTransport | None = None) -> None:
        self.client = httpx.Client(
            base_url=config.settings.ollama_url,
            timeout=config.models.runtime.timeout_seconds,
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    def generate(self, payload: dict[str, Any]) -> BackendReply:
        try:
            response = self.client.post("/api/chat", json=payload)
            response.raise_for_status()
            body = response.json()
            if body.get("done") is not True:
                raise ValueError("Incomplete response")
            return BackendReply(
                content=body["message"]["content"],
                prompt_tokens=body.get("prompt_eval_count", 0),
                output_tokens=body.get("eval_count", 0),
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            raise LLMError("Local Ollama request failed or returned an invalid envelope") from None

    def close(self) -> None:
        self.client.close()


class LLMResult[T: BaseModel](BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    value: T
    origin: Literal["live", "mock", "cache", "fallback"]
    attempts: int
    repaired: bool = False
    warning: str | None = None
    cache_key: str
    prompt_tokens: int = 0
    output_tokens: int = 0


class LocalLLM:
    """One application-owned instance provides one FIFO inference worker.

    generate blocks its caller; agents can invoke it from background jobs.
    All inference, validation, repair, and cache writes run in the worker.
    """

    def __init__(
        self,
        config: AppConfig,
        *,
        mock_responses: Iterable[str | BackendReply] = (),
        backend: Backend | None = None,
    ) -> None:
        self.config = config
        if config.settings.mock_llm:
            if backend is not None and not isinstance(backend, MockBackend):
                raise ValueError("Mock mode requires a MockBackend")
            self.backend: Backend = backend or MockBackend(mock_responses)
        else:
            config.require_live_models()
            self.backend = backend or OllamaBackend(config)
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dcf-llm")
        self.closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.worker.shutdown(wait=True)
            self.backend.close()

    def generate[T: BaseModel](
        self,
        role: Role,
        prompt: str,
        response_model: type[T],
        *,
        prompt_version: str,
        fallback: Callable[[], T] | None = None,
    ) -> LLMResult[T]:
        if self.closed:
            raise LLMError("LLM wrapper is closed")
        if not prompt.strip() or not prompt_version.strip():
            raise ValueError("Prompt and prompt version cannot be blank")
        return self.worker.submit(
            self._generate, role, prompt, response_model, prompt_version, fallback
        ).result()

    def _generate[T: BaseModel](
        self,
        role: Role,
        prompt: str,
        response_model: type[T],
        prompt_version: str,
        fallback: Callable[[], T] | None,
    ) -> LLMResult[T]:
        settings = self.config.models.roles[role]
        options: dict[str, Any] = {
            "num_gpu": 0,
            "num_ctx": settings.num_ctx,
            "num_predict": settings.num_predict,
            "temperature": settings.temperature,
        }
        if self.config.models.runtime.threads is not None:
            options["num_thread"] = self.config.models.runtime.threads
        payload: dict[str, Any] = {
            "model": settings.model or "mock",
            "stream": False,
            "think": False,
            "keep_alive": 0,
            "format": response_model.model_json_schema(),
            "messages": [
                {"role": "system", "content": "Return only JSON matching the supplied schema."},
                {"role": "user", "content": prompt},
            ],
            "options": options,
        }
        mode: Literal["mock", "live"] = "mock" if self.config.settings.mock_llm else "live"
        identity = {
            "version": 1,
            "mode": mode,
            "role": role,
            "prompt_version": prompt_version,
            "backend": self.config.settings.ollama_url,
            "request": payload,
        }
        key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        cache_dir = self.config.settings.llm_cache_dir / mode
        cache_path = cache_dir / (key + ".json")
        if self.config.models.runtime.response_cache and cache_path.exists():
            try:
                value = response_model.model_validate_json(cache_path.read_text(encoding="utf-8"))
                return LLMResult[T](value=value, origin="cache", attempts=0, cache_key=key)
            except (ValidationError, UnicodeError):
                pass  # Regenerate incompatible or damaged entries; do not trust cached data.
        total_prompt = total_output = 0
        failure = "Structured output remained invalid after one repair"
        attempts = 0
        for attempt in range(2):
            attempts += 1
            start = time.monotonic()
            try:
                reply = self.backend.generate(payload)
            except LLMError:
                failure = "Local model unavailable or request failed"
                self._trace(role, key, attempt, start, "backend_error", 0, 0)
                break
            total_prompt += reply.prompt_tokens
            total_output += reply.output_tokens
            try:
                value = response_model.model_validate_json(reply.content)
            except ValidationError as error:
                self._trace(
                    role,
                    key,
                    attempt,
                    start,
                    "validation_error",
                    reply.prompt_tokens,
                    reply.output_tokens,
                )
                # Exclude raw invalid values from the repair diagnostic.
                errors = error.errors(include_input=False, include_context=False, include_url=False)
                payload = {
                    **payload,
                    "messages": [
                        *payload["messages"],
                        {"role": "assistant", "content": reply.content},
                        {
                            "role": "user",
                            "content": "Repair the JSON. Validation errors: " + json.dumps(errors),
                        },
                    ],
                }
                continue
            self._trace(
                role, key, attempt, start, "valid", reply.prompt_tokens, reply.output_tokens
            )
            if self.config.models.runtime.response_cache:
                cache_dir.mkdir(parents=True, exist_ok=True)
                descriptor, temporary = tempfile.mkstemp(dir=cache_dir, suffix=".tmp")
                try:
                    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                        stream.write(value.model_dump_json())
                    os.replace(temporary, cache_path)
                finally:
                    Path(temporary).unlink(missing_ok=True)
            return LLMResult[T](
                value=value,
                origin=mode,
                attempts=attempts,
                repaired=attempt == 1,
                cache_key=key,
                prompt_tokens=total_prompt,
                output_tokens=total_output,
            )
        if fallback is None:
            raise LLMError(failure)
        # Revalidate defaults rather than trusting an unchecked model instance.
        value = response_model.model_validate_json(fallback().model_dump_json())
        return LLMResult[T](
            value=value,
            origin="fallback",
            attempts=attempts,
            warning=failure,
            cache_key=key,
            prompt_tokens=total_prompt,
            output_tokens=total_output,
        )

    def _trace(
        self,
        role: Role,
        key: str,
        attempt: int,
        start: float,
        status: str,
        prompt_tokens: int,
        output_tokens: int,
    ) -> None:
        directory = self.config.settings.resolve_path(self.config.settings.data_dir) / "traces"
        directory.mkdir(parents=True, exist_ok=True)
        entry = {
            "role": role,
            "cache_key": key,
            "attempt": attempt + 1,
            "status": status,
            "duration_seconds": time.monotonic() - start,
            "prompt_tokens": prompt_tokens,
            "output_tokens": output_tokens,
        }
        with (directory / "llm.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry) + "\n")
