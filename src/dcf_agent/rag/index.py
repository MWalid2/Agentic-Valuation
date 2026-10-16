"""Filing index and hybrid retrieval: SQLite FTS5 (BM25) plus local embeddings.

Each filing is indexed once (by accession): its chunks, the fact IDs each chunk
holds, a full-text index (BM25), and, for the sections that matter (notes, MD&A,
market risk, statements), a 384-dimension `bge-small-en-v1.5` embedding computed
on the CPU from a compact view of the chunk (title, caption, headers, row labels).
Retrieval is restricted to the filings the caller allows (point in time, one
company), ranks by BM25 and by cosine similarity, fuses the two rankings
(reciprocal rank fusion), and can search the sections of the requested field
first ("debt" looks in the debt note before the whole filing).
"""

import json
import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np

from dcf_agent.rag.filings import PARSER_VERSION, Chunk, FilingText

EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
RRF_K = 60
STOPWORDS = set(
    "a an and are as at be by did do does for from has have how in is it its of on or "
    "the to was were what when which who will with year years company company's".split()
)
# Sections each field lives in, matched against text-block concept names.
FIELD_SECTIONS: dict[str, tuple[str, ...]] = {
    "debt": ("Debt", "Borrowing", "Notes Payable", "Credit"),
    "leases": ("Lease",),
    "dilution": ("ShareBased", "Share Based", "Compensation", "EarningsPerShare", "Stock"),
    "tax": ("IncomeTax", "Income Tax"),
    "one_off": ("Restructuring", "Impairment", "Goodwill", "OtherIncome", "Nonoperating"),
    "pension": ("Pension", "Retirement", "PostretirementBenefit"),
}
EMBEDDED_SECTIONS = re.compile(r"^(item_7|item_7a|item_8|item_2|item_1)$|TextBlock$")
Mode = Literal["bm25", "dense", "hybrid"]


def words(concept: str) -> str:
    """'us-gaap:DebtDisclosureTextBlock' -> 'Debt Disclosure'."""
    name = concept.split(":", 1)[-1].removesuffix("TextBlock").removesuffix("Table")
    return re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)


def embedding_text(chunk: Chunk) -> str:
    """What the embedding sees: labels and wording, not every number of a table."""
    head = f"{chunk.title}. {words(chunk.section)}."
    if chunk.kind == "table":
        lines = chunk.text.splitlines()
        caption = next((line for line in lines if line.startswith("Caption: ")), "")
        headers = [" ".join(c.text for c in row) for row in chunk.rows[: chunk.header_rows]]
        labels = [
            row[0].text for row in chunk.rows[chunk.header_rows :] if row and not row[0].facts
        ]
        body = " ".join([words(chunk.table_tag or ""), caption, *headers, "; ".join(labels)])
    else:
        body = chunk.text
    return f"{head} {body}"[:1000]


def fts_query(question: str) -> str:
    terms = [t for t in re.findall(r"[a-z0-9]+", question.lower()) if t not in STOPWORDS]
    return " OR ".join(f'"{t}"' for t in dict.fromkeys(terms)) or '""'


@dataclass(frozen=True)
class Hit:
    chunk_id: str
    accession: str
    section: str
    title: str
    kind: str
    text: str
    score: float
    targeted: bool


class FilingIndex:
    """One SQLite file for every indexed filing; embeddings computed lazily."""

    def __init__(self, path: Path, embedder: Any = None) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # WAL: searches never wait for an indexing writer; a writer waits up to a minute.
        self.db = sqlite3.connect(path, timeout=60)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.embedder = embedder
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS filings(
                accession TEXT PRIMARY KEY, cik TEXT, form TEXT, filed TEXT,
                period_end TEXT, url TEXT, parser TEXT);
            CREATE TABLE IF NOT EXISTS chunks(
                chunk_id TEXT PRIMARY KEY, accession TEXT, section TEXT, title TEXT,
                kind TEXT, ord INTEGER, blocks TEXT, data TEXT);
            CREATE INDEX IF NOT EXISTS chunks_accession ON chunks(accession);
            CREATE TABLE IF NOT EXISTS facts(
                accession TEXT, fact_id TEXT, chunk_id TEXT,
                PRIMARY KEY (accession, fact_id));
            CREATE TABLE IF NOT EXISTS vectors(
                chunk_id TEXT PRIMARY KEY, model TEXT, vector BLOB);
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                chunk_id UNINDEXED, accession UNINDEXED, text, labels,
                tokenize = 'porter unicode61');
            """
        )

    def close(self) -> None:
        self.db.close()

    def indexed(self, accession: str) -> bool:
        """Indexed by the current parser; a filing from other parser code is dropped."""
        row = self.db.execute(
            "SELECT parser FROM filings WHERE accession = ?", (accession,)
        ).fetchone()
        if row is None:
            return False
        if row[0] == PARSER_VERSION:
            return True
        with self.db:
            self.db.execute(
                "DELETE FROM vectors WHERE chunk_id IN "
                "(SELECT chunk_id FROM chunks WHERE accession = ?)",
                (accession,),
            )
            for table in ("chunks", "chunks_fts", "facts", "filings"):
                self.db.execute(f"DELETE FROM {table} WHERE accession = ?", (accession,))
        return False

    def add(self, text: FilingText, cik: str) -> None:
        """Index a parsed filing (text only; embeddings come with `embed`)."""
        if self.indexed(text.accession):
            return
        with self.db:
            self.db.execute(
                "INSERT INTO filings VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    text.accession,
                    cik,
                    text.form,
                    str(text.filed),
                    str(text.period_end),
                    text.url,
                    PARSER_VERSION,
                ),
            )
            for chunk in text.chunks:
                labels = " ".join(
                    [chunk.title, *(words(b) for b in chunk.blocks), words(chunk.table_tag or "")]
                )
                self.db.execute(
                    "INSERT INTO chunks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        chunk.chunk_id,
                        chunk.accession,
                        chunk.section,
                        chunk.title,
                        chunk.kind,
                        chunk.order,
                        json.dumps(chunk.blocks),
                        chunk.model_dump_json(),
                    ),
                )
                self.db.execute(
                    "INSERT INTO chunks_fts VALUES (?, ?, ?, ?)",
                    (chunk.chunk_id, chunk.accession, chunk.text, labels),
                )
                self.db.executemany(
                    "INSERT OR IGNORE INTO facts VALUES (?, ?, ?)",
                    [(chunk.accession, f, chunk.chunk_id) for f in chunk.fact_ids],
                )

    def embed(self, accessions: Iterable[str]) -> int:
        """Embed the chunks of these filings that lack a vector; returns how many."""
        if self.embedder is None:
            return 0
        missing = []
        for accession in accessions:
            rows = self.db.execute(
                "SELECT c.chunk_id, c.section, c.data FROM chunks c LEFT JOIN vectors v "
                "ON v.chunk_id = c.chunk_id WHERE c.accession = ? AND v.chunk_id IS NULL",
                (accession,),
            ).fetchall()
            missing += [
                (cid, data) for cid, section, data in rows if EMBEDDED_SECTIONS.search(section)
            ]
        if not missing:
            return 0
        texts = [embedding_text(Chunk.model_validate_json(data)) for _, data in missing]
        vectors = self.embedder.embed(texts, batch_size=32)
        with self.db:
            for (chunk_id, _), vector in zip(missing, vectors, strict=True):
                array = np.asarray(vector, dtype=np.float32)
                array /= np.linalg.norm(array) or 1.0
                self.db.execute(
                    "INSERT OR REPLACE INTO vectors VALUES (?, ?, ?)",
                    (chunk_id, EMBEDDING_MODEL, array.tobytes()),
                )
        return len(missing)

    def chunk_ids_for_facts(self, accession: str, fact_ids: Sequence[str]) -> set[str]:
        marks = ",".join("?" for _ in fact_ids)
        rows = self.db.execute(
            f"SELECT chunk_id FROM facts WHERE accession = ? AND fact_id IN ({marks})",
            (accession, *fact_ids),
        )
        return {r[0] for r in rows}

    def chunk(self, chunk_id: str) -> Chunk:
        row = self.db.execute("SELECT data FROM chunks WHERE chunk_id = ?", (chunk_id,)).fetchone()
        return Chunk.model_validate_json(row[0])

    def bm25(self, question: str, accessions: Sequence[str], limit: int) -> list[str]:
        marks = ",".join("?" for _ in accessions)
        rows = self.db.execute(
            f"SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH ? AND accession IN ({marks}) "
            "ORDER BY bm25(chunks_fts, 1.0, 1.0, 1.0, 2.0) LIMIT ?",
            (fts_query(question), *accessions, limit),
        )
        return [r[0] for r in rows]

    def dense(self, question: str, accessions: Sequence[str], limit: int) -> list[str]:
        if self.embedder is None:
            return []
        marks = ",".join("?" for _ in accessions)
        rows = self.db.execute(
            f"SELECT v.chunk_id, v.vector FROM vectors v JOIN chunks c ON c.chunk_id = v.chunk_id "
            f"WHERE c.accession IN ({marks})",
            tuple(accessions),
        ).fetchall()
        if not rows:
            return []
        matrix = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float32).reshape(
            len(rows), -1
        )
        query = np.asarray(next(iter(self.embedder.query_embed([question]))), dtype=np.float32)
        query /= np.linalg.norm(query) or 1.0
        order = np.argsort(-(matrix @ query))[:limit]
        return [rows[i][0] for i in order]

    def targeted_ids(self, field: str | None, accessions: Sequence[str]) -> set[str]:
        keys = FIELD_SECTIONS.get(field or "", ())
        if not keys:
            return set()
        marks = ",".join("?" for _ in accessions)
        rows = self.db.execute(
            f"SELECT chunk_id, blocks, title FROM chunks WHERE accession IN ({marks})",
            tuple(accessions),
        )
        found = set()
        for chunk_id, blocks, title in rows:
            names = " ".join(words(b) for b in json.loads(blocks)) + " " + title
            compact = names.replace(" ", "")
            if any(k.replace(" ", "").lower() in compact.lower() for k in keys):
                found.add(chunk_id)
        return found

    def search(
        self,
        question: str,
        accessions: Sequence[str],
        *,
        k: int = 4,
        mode: Mode = "bm25",  # chosen on the development set: hybrid did not beat it
        field: str | None = None,
        pool: int = 50,
    ) -> list[Hit]:
        """Top k chunks of the allowed filings; the field's sections first when targeted."""
        rankings = []
        if mode in ("bm25", "hybrid"):
            rankings.append(self.bm25(question, accessions, pool))
        if mode in ("dense", "hybrid"):
            rankings.append(self.dense(question, accessions, pool))
        scores: dict[str, float] = {}
        for ranking in rankings:
            for rank, chunk_id in enumerate(ranking):
                scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank + 1)
        order = sorted(scores, key=lambda c: -scores[c])
        targeted = self.targeted_ids(field, accessions) if field else set()
        if targeted:
            # The field's own sections first, then the best of the rest.
            order = [c for c in order if c in targeted] + [c for c in order if c not in targeted]
        hits = []
        for chunk_id in order[:k]:
            chunk = self.chunk(chunk_id)
            hits.append(
                Hit(
                    chunk_id=chunk_id,
                    accession=chunk.accession,
                    section=chunk.section,
                    title=chunk.title,
                    kind=chunk.kind,
                    text=chunk.text,
                    score=scores[chunk_id],
                    targeted=chunk_id in targeted,
                )
            )
        return hits
