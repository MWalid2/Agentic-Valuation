"""Provenance audit: every number a valuation uses must be justified and sourced.

Walks every `Sourced` value in the inputs of a valuation (the data bundle, the
normalized history, the approved adjustments, the WACC, the forecast) and checks:

- values from an external source (SEC, FRED, Damodaran, prices) point to a URL or an
  accession;
- derived values name what they were derived from (a URL, an accession, the engine's
  code or the policy) and say how in their note;
- user and policy values name the gate or the policy;
- nothing rests on a model's estimate.

A record that fails is reported with its source, reference and note, so it can be fixed.
"""

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from dcf_agent.contracts.accounting import evidence_records
from dcf_agent.contracts.provenance import Provenance

EXTERNAL = {"sec_xbrl", "sec_filing_text", "fred", "damodaran", "yfinance", "stooq"}
# A URL, a filing accession (bare or labelled), the engine's code, a gate or the policy.
TRACEABLE = re.compile(r"https?://|\d{10}-\d{2}-\d{6}|dcf_agent\.|policy|gate \d|engine")
PARTS = ("data", "normalized", "approved_adjustments", "wacc", "forecast")


@dataclass
class Audit:
    records: int = 0
    by_source: Counter[str] = field(default_factory=Counter)
    problems: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.problems


def problem(record: Provenance) -> str | None:
    ref, note = record.ref.strip(), (record.note or "").strip()
    if record.source == "llm_estimate":
        return "rests on a model estimate"
    if record.source in EXTERNAL and not re.search(
        r"https?://|\d{10}-\d{2}-\d{6}|[A-Z0-9]{4,}", ref
    ):
        return "external value without a URL, accession or series"
    if record.source == "derived":
        if not TRACEABLE.search(ref):
            return "derived value without a traceable reference"
        if not note:
            return "derived value without a note saying how"
    if record.source == "user_override" and not ref:
        return "user or policy value without its gate or policy"
    return None


def audit_records(records: Iterable[Provenance]) -> Audit:
    result = Audit()
    seen: set[tuple[str, str, str]] = set()
    for record in records:
        result.records += 1
        result.by_source[record.source] += 1
        why = problem(record)
        key = (record.source, record.ref[:80], (record.note or "")[:80])
        if why and key not in seen:
            seen.add(key)
            result.problems.append(
                f"{why}: {record.source} | {record.ref[:100]} | {(record.note or '')[:100]}"
            )
    return result


def audit_state(state: dict[str, Any]) -> Audit:
    """The audit of a session's valuation inputs (any part missing is skipped)."""
    records: list[Provenance] = []
    for part in PARTS:
        value = state.get(part)
        if value is not None:
            records += evidence_records(value)
    return audit_records(records)
