"""The RAG gold sets stay frozen and reproducible."""

import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOLD = ROOT / "evals/rag/gold-xbrl.jsonl"


def items() -> list[dict]:
    return [json.loads(line) for line in GOLD.read_text(encoding="utf-8").splitlines()]


def test_gold_set_matches_its_recorded_checksum():
    recorded = re.search(r"sha256: ([0-9a-f]{64})", (GOLD.parent / "README.md").read_text())
    assert recorded and hashlib.sha256(GOLD.read_bytes()).hexdigest() == recorded.group(1)


def test_split_is_the_fixed_hash_rule_and_the_test_set_is_large_enough():
    gold = items()
    for item in gold:
        first = hashlib.sha256(item["ticker"].encode()).digest()[0]
        assert item["split"] == ("test" if first < 0x55 else "dev")
    test = [g for g in gold if g["split"] == "test"]
    assert len(test) >= 100 and len({g["ticker"] for g in test}) >= 5
    assert not {g["ticker"] for g in test} & {g["ticker"] for g in gold if g["split"] == "dev"}


def test_every_item_has_an_answer_unit_and_reference():
    for item in items():
        assert item["reference_fact_ids"] and item["unit"] and item["answer"] is not None
        assert item["question"].endswith("?") and "us-gaap" not in item["question"]
