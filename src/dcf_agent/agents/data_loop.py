"""Data Agent loop: act on readiness blockers with one bounded action at a time.

The deterministic collection runs first. Only when a consumer the plan needs is
blocked (or a required source failed) is an action chosen, from a fixed list, at
most four times:

- classify_unknown_lines: the guarded line classifier on material unmapped lines
  named in an unresolved reason; accepted decisions are stored and collection reruns
- refresh_sec_data: download the mutable SEC endpoints again (submissions,
  companyfacts) and rerun; filed documents are never re-downloaded
- retry_prices: rerun collection so the price sources are tried again
- retry_sources: rerun collection so Damodaran (current page, then archived copies)
  and FRED are searched again
- accept_with_flag: continue; the remaining blockers are listed and later steps
  explain what they could not compute
- ask_user: stop at Gate 1 with the blockers and let the user continue or stop

Code decides which actions apply; the model (role `data`) only chooses among them,
and a choice outside the applicable set is rejected. Line decisions that remove a
value or add a blocker are undone (the stored mappings restored) and recorded.
Without a model the default policy retries prices when they failed, otherwise asks
the user. Every step
records the blockers before and after and every value the action changed, so no
change is silent.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from dcf_agent.agents.lines import MappingStore, candidates, classify_unknown_lines
from dcf_agent.contracts.external import DataBundle
from dcf_agent.data.sec import SECClient
from dcf_agent.llm import LLMError, LocalLLM
from dcf_agent.tools.data import DataTools

PROMPT_VERSION = "data-loop-v1"
MAX_STEPS = 4
Action = Literal[
    "classify_unknown_lines",
    "refresh_sec_data",
    "retry_prices",
    "retry_sources",
    "accept_with_flag",
    "ask_user",
]
TERMINAL: tuple[Action, ...] = ("accept_with_flag", "ask_user")
DESCRIPTIONS: dict[Action, str] = {
    "classify_unknown_lines": "classify unmapped statement lines that block a concept, then "
    "rebuild the history",
    "refresh_sec_data": "download the SEC filing list and tagged facts again (a filing may be "
    "missing from the saved copy), then rebuild",
    "retry_prices": "try the price sources again",
    "retry_sources": "search Damodaran (including archived copies) and FRED again",
    "accept_with_flag": "continue; later steps explain what they cannot compute",
    "ask_user": "stop and ask the user whether to continue",
}
# Readiness consumers each plan step depends on.
CONSUMERS: dict[str, tuple[str, ...]] = {
    "data": ("scope",),
    "normalize": ("normalization",),
    "wacc": ("wacc", "scope"),
    "value": ("equity_bridge", "wacc", "normalization", "scope"),
    "excel": ("equity_bridge", "wacc", "normalization", "scope"),
}
# The sources each plan step needs: a failed source blocks only the plans that use it.
SEC_FIELDS = {"seed", "history"}
PRICE_FIELDS = {"prices", "market"}
SOURCE_FIELDS = {"DGS10", "country_risk", "betas", "spreads", "industry", "macro"}
NEEDS: dict[str, set[str]] = {
    "data": SEC_FIELDS,
    "normalize": SEC_FIELDS,
    "wacc": SEC_FIELDS | PRICE_FIELDS | SOURCE_FIELDS,
    "value": SEC_FIELDS | PRICE_FIELDS | SOURCE_FIELDS | {"margins", "sales_to_capital"},
    "excel": SEC_FIELDS | PRICE_FIELDS | SOURCE_FIELDS | {"margins", "sales_to_capital"},
}
RETRYABLE = SOURCE_FIELDS | {"margins", "sales_to_capital", "BAMLC0A0CM", "T10YIE"}


class Blocker(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    consumer: str  # a readiness consumer, or "quality" for a failed required source
    key: str
    reason: str

    def text(self) -> str:
        return f"{self.consumer}: {self.key}: {self.reason}"


class LoopStep(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    action: Action
    source: Literal["model", "policy"]
    reason: str
    before: tuple[str, ...]
    after: tuple[str, ...] = ()
    changes: tuple[str, ...] = ()  # values and mappings the action changed
    rejected: str | None = None
    # Line decisions that removed values or added blockers are undone; `changes` keeps them.
    reverted: bool = False


class DataReview(BaseModel):
    """How the loop ended; `question` is what Gate 1 shows when the user is asked."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    status: Literal["clear", "flagged", "ask_user"]
    steps: tuple[LoopStep, ...] = ()
    remaining: tuple[Blocker, ...] = ()
    question: str | None = None


class ActionChoice(BaseModel):
    action: Action
    reason: str = Field(min_length=1, max_length=300)


def required_consumers(plan: tuple[str, ...]) -> set[str]:
    return {c for step in plan for c in CONSUMERS.get(step, ())}


def required_sources(plan: tuple[str, ...]) -> set[str]:
    return {field for step in plan for field in NEEDS.get(step, set())}


def blockers(bundle: DataBundle, plan: tuple[str, ...]) -> list[Blocker]:
    """Blocked readiness consumers and failed sources that this plan needs."""
    consumers, needed = required_consumers(plan), required_sources(plan)
    found = []
    for consumer in sorted(consumers):
        readiness = bundle.readiness.get(consumer)
        if readiness is None or readiness.status != "blocked":
            continue
        for key, reasons in sorted(readiness.missing.items()):
            found += [Blocker(consumer=consumer, key=key, reason=r) for r in reasons]
    failed = {c.field: c.message for c in bundle.quality.checks if c.severity == "error"}
    for field in bundle.quality.missing_fields:
        if field in needed:
            found.append(Blocker(consumer="quality", key=field, reason=failed.get(field, "failed")))
    return found


def value_changes(old: DataBundle, new: DataBundle) -> list[str]:
    """Every reported value that differs, appeared or disappeared between two bundles."""

    def table(bundle: DataBundle) -> dict[tuple[str, str, str], float]:
        raw = bundle.raw
        if raw is None:
            return {}
        periods = [*raw.annual, *raw.quarterly, *((raw.ttm,) if raw.ttm else ())]
        return {
            (p.kind, str(p.period_end), concept): v.value
            for p in periods
            for concept, v in p.values.items()
        }

    before, after = table(old), table(new)
    changes = []
    for key in sorted(set(before) | set(after)):
        a, b = before.get(key), after.get(key)
        if a != b:
            changes.append(f"{key[0]} {key[1]} {key[2]}: {a} -> {b}")
    if (old.market and old.market.shares_outstanding.value) != (
        new.market and new.market.shares_outstanding.value
    ):
        changes.append("market shares or price changed")
    return changes


class DataLoop:
    def __init__(
        self,
        llm: LocalLLM | None,
        mappings: MappingStore | None,
        *,
        online: bool,
        max_steps: int = MAX_STEPS,
    ) -> None:
        self.llm, self.mappings, self.online, self.max_steps = llm, mappings, online, max_steps

    def applicable(
        self, bundle: DataBundle, found: list[Blocker], tools: DataTools | None, tried: set[str]
    ) -> list[Action]:
        options: list[Action] = []
        extractions = tools.extractions if tools is not None else ()
        if (
            "classify_unknown_lines" not in tried
            and self.llm is not None
            and self.mappings is not None
            and any(candidates(e) for e in extractions)
        ):
            options.append("classify_unknown_lines")

        def failed(fields: set[str]) -> bool:
            return any(b.consumer == "quality" and b.key in fields for b in found)

        if self.online and "refresh_sec_data" not in tried:
            if failed(SEC_FIELDS) or any(b.consumer != "quality" for b in found):
                options.append("refresh_sec_data")
        if self.online and "retry_prices" not in tried and failed(PRICE_FIELDS):
            options.append("retry_prices")
        if self.online and "retry_sources" not in tried and failed(RETRYABLE):
            options.append("retry_sources")
        return options + list(TERMINAL)

    def policy(self, options: list[Action]) -> Action:
        for action in ("retry_prices", "retry_sources"):
            if action in options:
                return action
        return "ask_user"

    def choose(
        self, found: list[Blocker], options: list[Action], tried: set[str]
    ) -> tuple[Action, str, Literal["model", "policy"], str | None]:
        default = self.policy(options)
        if self.llm is None:
            return default, "default policy (no model)", "policy", None
        prompt = "\n".join(
            [
                "Collected financial data has blockers. Choose one action.",
                "Blockers:",
                *(f"- {b.text()}" for b in found[:12]),
                "Actions available:",
                *(f"- {a}: {DESCRIPTIONS[a]}" for a in options),
                ("Already tried: " + ", ".join(sorted(tried))) if tried else "",
                "Return the action and one short sentence.",
            ]
        )
        try:
            reply = self.llm.generate("data", prompt, ActionChoice, prompt_version=PROMPT_VERSION)
        except LLMError as error:
            return default, f"default policy (model unavailable: {error})", "policy", None
        choice = reply.value
        if choice.action not in options:
            rejected = f"model chose {choice.action}, which does not apply here"
            return default, "default policy", "policy", rejected
        return choice.action, choice.reason.strip(), "model", None

    def snapshot_mappings(self) -> dict[Path, bytes]:
        assert self.mappings is not None
        root = self.mappings.root
        return {p: p.read_bytes() for p in root.glob("*.json")} if root.exists() else {}

    def restore_mappings(self, saved: dict[Path, bytes]) -> None:
        assert self.mappings is not None
        root = self.mappings.root
        for path in root.glob("*.json") if root.exists() else ():
            if path not in saved:
                path.unlink()
        for path, content in saved.items():
            path.write_bytes(content)

    def classify(self, tools: DataTools | None) -> list[str]:
        assert self.llm is not None and self.mappings is not None
        learned: list[str] = []
        for extraction in tools.extractions if tools is not None else ():
            if not candidates(extraction):
                continue
            decisions = classify_unknown_lines(self.llm, extraction, self.mappings)
            learned += [
                f"mapping {element} -> {category}" for element, category in decisions.items()
            ]
        return learned

    def run(
        self,
        collect: Callable[[], DataBundle],
        plan: tuple[str, ...],
        tools: DataTools | None = None,
        sec: SECClient | None = None,
    ) -> tuple[DataBundle, DataReview]:
        bundle = collect()
        steps: list[LoopStep] = []
        tried: set[str] = set()
        for _ in range(self.max_steps):
            found = blockers(bundle, plan)
            if not found:
                return bundle, DataReview(status="clear", steps=tuple(steps))
            before = tuple(b.text() for b in found)
            options = self.applicable(bundle, found, tools, tried)
            action, reason, source, rejected = self.choose(found, options, tried)
            if action in TERMINAL:
                steps.append(
                    LoopStep(
                        action=action,
                        source=source,
                        reason=reason,
                        before=before,
                        rejected=rejected,
                    )
                )
                return bundle, self.finish(action, found, steps)
            tried.add(action)
            learned: list[str] = []
            saved = None
            if action == "classify_unknown_lines":
                saved = self.snapshot_mappings()
                learned = self.classify(tools)
            refreshing = action == "refresh_sec_data" and sec is not None
            if refreshing and sec is not None:
                sec.start_refresh()
            try:
                updated = collect()
            finally:
                if refreshing and sec is not None:
                    sec.stop_refresh()
            after = tuple(b.text() for b in blockers(updated, plan))
            changes = learned + value_changes(bundle, updated)
            reverted = False
            lost = [c for c in changes if c.endswith("-> None")]
            if saved is not None and (lost or len(after) > len(before)):
                # Our own line decisions made the data worse: undo them and rebuild.
                self.restore_mappings(saved)
                updated = collect()
                after = tuple(b.text() for b in blockers(updated, plan))
                changes = ["reverted: " + c for c in changes] + value_changes(bundle, updated)
                reverted = True
            steps.append(
                LoopStep(
                    action=action,
                    source=source,
                    reason=reason,
                    before=before,
                    after=after,
                    changes=tuple(changes),
                    rejected=rejected,
                    reverted=reverted,
                )
            )
            bundle = updated
        found = blockers(bundle, plan)
        if not found:
            return bundle, DataReview(status="clear", steps=tuple(steps))
        return bundle, self.finish("ask_user", found, steps)

    def finish(self, action: Action, found: list[Blocker], steps: list[LoopStep]) -> DataReview:
        if action == "accept_with_flag":
            return DataReview(status="flagged", steps=tuple(steps), remaining=tuple(found))
        lines = [f"- {b.text()}" for b in found[:8]]
        if len(found) > 8:
            lines.append(f"- and {len(found) - 8} more")
        question = (
            "Some inputs needed for this request are blocked:\n"
            + "\n".join(lines)
            + "\nContinue (later steps will explain what they cannot compute) or stop?"
        )
        return DataReview(
            status="ask_user", steps=tuple(steps), remaining=tuple(found), question=question
        )
