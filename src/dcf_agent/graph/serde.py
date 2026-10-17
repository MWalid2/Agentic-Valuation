"""Checkpoint serializer: project contracts as validated JSON, everything else by LangGraph.

LangGraph's default serializer restores Pydantic models without validation (a
tuple can come back as a list) and will stop restoring unregistered classes. Our
contracts are therefore written as JSON tagged with their class, restricted to an
allowlist of project modules, and validated again on load, so a restored session
satisfies the same contracts as a fresh one.
"""

import importlib
import inspect
import json
from typing import Any

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pydantic import BaseModel

TAG = "dcf-json"
MODULES = (
    "dcf_agent.contracts.accounting",
    "dcf_agent.contracts.external",
    "dcf_agent.contracts.inputs",
    "dcf_agent.contracts.modeling",
    "dcf_agent.contracts.provenance",
    "dcf_agent.contracts.quality",
    "dcf_agent.contracts.snapshot",
    "dcf_agent.contracts.workflow",
    "dcf_agent.engine.valuation",
    "dcf_agent.agents.answer",
    "dcf_agent.agents.data_loop",
    "dcf_agent.agents.normalizer",
    "dcf_agent.agents.router",
    "dcf_agent.agents.wacc",
    "dcf_agent.graph.state",
)


def registry() -> dict[str, type[BaseModel]]:
    """Every Pydantic model defined in the allowed modules, by "module:Class"."""
    found: dict[str, type[BaseModel]] = {}
    for name in MODULES:
        module = importlib.import_module(name)
        for _, cls in inspect.getmembers(module, inspect.isclass):
            if issubclass(cls, BaseModel) and cls.__module__ == name:
                found[f"{name}:{cls.__qualname__}"] = cls
    return found


class Unsupported(TypeError):
    pass


class ContractSerializer:
    def __init__(self) -> None:
        self.models = registry()
        self.names = {cls: name for name, cls in self.models.items()}
        # Plain values and LangGraph's own objects; no arbitrary class restoration.
        self.fallback = JsonPlusSerializer(allowed_msgpack_modules=None)

    def encode(self, value: Any) -> Any:
        if isinstance(value, BaseModel):
            name = self.names.get(type(value))
            if name is None:
                raise Unsupported(type(value).__name__)
            return {"__model__": name, "data": json.loads(value.model_dump_json())}
        if isinstance(value, tuple):
            return {"__tuple__": [self.encode(v) for v in value]}
        if isinstance(value, list):
            return [self.encode(v) for v in value]
        if isinstance(value, dict):
            if not all(isinstance(k, str) for k in value):
                raise Unsupported("non-string keys")
            return {"__dict__": {k: self.encode(v) for k, v in value.items()}}
        if value is None or isinstance(value, str | int | float | bool):
            return value
        raise Unsupported(type(value).__name__)

    def decode(self, value: Any) -> Any:
        if isinstance(value, list):
            return [self.decode(v) for v in value]
        if isinstance(value, dict):
            if "__model__" in value:
                cls = self.models.get(value["__model__"])
                if cls is None:
                    raise ValueError(f"Checkpoint holds a type outside the allowlist: {value}")
                return cls.model_validate_json(json.dumps(value["data"]))
            if "__tuple__" in value:
                return tuple(self.decode(v) for v in value["__tuple__"])
            return {k: self.decode(v) for k, v in value["__dict__"].items()}
        return value

    def dumps_typed(self, obj: Any) -> tuple[str, bytes]:
        try:
            return TAG, json.dumps(self.encode(obj)).encode("utf-8")
        except Unsupported:
            return self.fallback.dumps_typed(obj)

    def loads_typed(self, data: tuple[str, bytes]) -> Any:
        kind, payload = data
        if kind == TAG:
            return self.decode(json.loads(payload.decode("utf-8")))
        return self.fallback.loads_typed(data)
