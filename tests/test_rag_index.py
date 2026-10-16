"""Filing index: point-in-time scope, BM25, dense ranking, fusion and section targeting."""

from datetime import date
from pathlib import Path

import numpy as np
from test_filings_text import parsed

from dcf_agent.rag.filings import Chunk, FilingText
from dcf_agent.rag.index import FilingIndex, embedding_text, fts_query
from dcf_agent.rag.queries import expand


class KeywordEmbedder:
    """A deterministic stand-in for the ONNX model: one dimension per keyword."""

    WORDS = ("debt", "lease", "tax", "litigation", "revenue")

    def vector(self, text: str) -> np.ndarray:
        lower = text.lower()
        return np.array([lower.count(w) + 0.01 for w in self.WORDS], dtype=np.float32)

    def embed(self, texts, batch_size: int = 32):
        return [self.vector(t) for t in texts]

    def query_embed(self, texts):
        return [self.vector(t) for t in texts]


def index(tmp_path: Path, embedder=None) -> tuple[FilingIndex, FilingText]:
    text = parsed()
    ix = FilingIndex(tmp_path / "index.sqlite", embedder)
    ix.add(text, "0000000001")
    return ix, text


def test_indexing_is_once_per_filing_and_facts_map_to_chunks(tmp_path: Path):
    ix, text = index(tmp_path)
    ix.add(text, "0000000001")  # again: no duplicate rows
    count = ix.db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    assert count == len(text.chunks)
    debt_table = next(c for c in text.chunks if c.kind == "table")
    assert ix.chunk_ids_for_facts(text.accession, ["f1", "f2"]) == {debt_table.chunk_id}
    assert ix.chunk(debt_table.chunk_id) == debt_table


def test_bm25_finds_the_debt_table_and_scope_excludes_other_filings(tmp_path: Path):
    ix, text = index(tmp_path)
    hits = ix.search("notes due 2030 long-term debt", [text.accession], mode="bm25")
    assert hits and "Notes due 2030" in hits[0].text
    assert ix.search("notes due 2030", ["0000000002-26-000001"], mode="bm25") == []


def test_dense_and_hybrid_rank_by_meaning(tmp_path: Path):
    ix, text = index(tmp_path, KeywordEmbedder())
    assert ix.embed([text.accession]) > 0
    assert ix.embed([text.accession]) == 0  # vectors are kept
    dense = ix.search("litigation", [text.accession], mode="dense", k=1)
    assert "litigation" in dense[0].text.lower()
    hybrid = ix.search("debt notes", [text.accession], mode="hybrid", k=2)
    assert any("debt" in h.text.lower() for h in hybrid)


def test_targeting_puts_the_fields_section_first(tmp_path: Path):
    ix, text = index(tmp_path)
    hits = ix.search("ordinary course litigation debt", [text.accession], mode="bm25", field="debt")
    assert hits[0].targeted and hits[0].section == "us-gaap:DebtDisclosureTextBlock"


def test_embedding_text_keeps_labels_not_numbers():
    table = next(c for c in parsed().chunks if c.kind == "table")
    view = embedding_text(table)
    assert "Notes due 2030" in view and "Schedule Of Debt" in view and "1,200" not in view
    assert isinstance(table, Chunk)


def test_fts_query_quotes_terms_and_drops_stopwords():
    assert fts_query("What was the fair value of debt?") == '"fair" OR "value" OR "debt"'


def test_expansion_uses_filing_terms_and_fiscal_year_labels():
    end = date(2025, 12, 31)
    debt = expand("How much long-term debt principal is due in the second year?", end)
    assert "maturities of long-term debt" in debt and "2027" in debt
    lease = expand("What operating lease payments are due after year five?", end)
    assert "lessee" in lease and "thereafter" in lease
    assert expand("What was the effective income tax rate?").startswith("What was")


def test_a_filing_indexed_by_other_parser_code_is_rebuilt(tmp_path: Path):
    ix, text = index(tmp_path, KeywordEmbedder())
    ix.embed([text.accession])
    ix.db.execute("UPDATE filings SET parser = 'old' WHERE accession = ?", (text.accession,))
    ix.db.commit()
    assert not ix.indexed(text.accession)  # stale rows and vectors dropped
    assert ix.db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0] == 0
    ix.add(text, "0000000001")
    assert ix.indexed(text.accession) and ix.embed([text.accession]) > 0
