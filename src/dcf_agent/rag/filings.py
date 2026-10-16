"""Filing text store: sections, whole tables and passages from inline-XBRL filings.

A 10-K or 10-Q primary document is split by its own structure. Notes come from the
inline-XBRL `...TextBlock` tags (followed across `ix:continuation` elements when a
note spans pages); a table-level text block inside a note labels that table. Text
outside the notes is split by its Item headings. Tables are kept whole as grids
(row labels, column headers, units); every tagged number keeps its concept, unit,
scale, sign, period and whether it is a breakdown (dimensional) figure. Prose is
joined into passages of about 400 tokens with a short overlap.

Every visible numeric fact lands in exactly one chunk, which lets retrieval be
measured exactly against XBRL-tagged facts, and lets code read a number from the
tagged cell (value, scale, sign) instead of trusting a model's transcription.
"""

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from lxml import etree
from pydantic import BaseModel, ConfigDict

from dcf_agent.data.sec import Filing, SECClient, SECError
from dcf_agent.tools.data import periodic_filings

IX = "http://www.xbrl.org/2013/inlineXBRL"
XBRLI = "http://www.xbrl.org/2003/instance"
NON_FRACTION, NON_NUMERIC = f"{{{IX}}}nonFraction", f"{{{IX}}}nonNumeric"
CONTINUATION, HEADER = f"{{{IX}}}continuation", f"{{{IX}}}header"
BLOCK = {
    "p", "div", "table", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
    "center", "blockquote", "section",
}  # fmt: skip
ITEM = re.compile(r"^\s*item\s+(\d{1,2}[a-c]?)\s*[.:—-]?", re.I)
NOTE = re.compile(r"^\s*note\s+\d{1,2}\b", re.I)
UNITS = re.compile(
    r"\(?\s*in\s+(millions|thousands|billions)(?:\s+of\s+(?:U\.?S\.?\s+)?dollars)?"
    r"(?:,?\s+except\s+[^)]{0,60})?\)?",
    re.I,
)
SCALES = {3: "thousands", 6: "millions", 9: "billions"}
YEAR = re.compile(r"^(19|20)\d\d$")
NUMERIC = re.compile(r"^[\$\(\-—–]*\s*[\d,]+(\.\d+)?\s*%?\)?$")
TARGET_CHARS, MAX_CHARS, OVERLAP_CHARS = 1600, 2000, 240  # about 400, 500, 60 tokens
# Fingerprint of this parser: an index built by other parser code is rebuilt.
PARSER_VERSION = hashlib.sha256(open(__file__, "rb").read()).hexdigest()[:12]


class FactRef(BaseModel):
    """A tagged number as the filing states it; value = shown number x 10^scale x sign."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    fact_id: str
    concept: str
    unit: str | None  # resolved measure: "USD", "shares", "pure", "USD/shares", ...
    scale: int
    negative: bool
    start: date | None
    end: date | None
    dimensional: bool
    value: float | None = None  # in units (dollars, shares, ratio), sign applied
    shown: str = ""  # the text displayed in the filing, e.g. "1,200"
    # Explicit (axis, member) pairs, sorted; a typed member is listed with an empty member.
    members: tuple[tuple[str, str], ...] = ()


class Cell(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    text: str
    facts: tuple[FactRef, ...] = ()


class Chunk(BaseModel):
    """One retrievable unit; `text` is what is indexed and shown to the model."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    chunk_id: str
    accession: str
    form: str
    filed: date
    period_end: date | None
    section: str  # outermost note text-block concept, or "item_7" for text outside notes
    title: str
    kind: str  # "table" or "text"
    text: str
    order: int
    blocks: tuple[str, ...] = ()  # every enclosing text block, outermost first
    table_tag: str | None = None
    units: str | None = None
    rows: tuple[tuple[Cell, ...], ...] = ()
    header_rows: int = 0
    # Column headers copied from the table just above (a continued or comparative table).
    inherited_header: bool = False
    facts: tuple[FactRef, ...] = ()
    anchor: str | None = None

    @property
    def fact_ids(self) -> tuple[str, ...]:
        return tuple(f.fact_id for f in self.facts)

    @property
    def tokens(self) -> int:
        return len(self.text) // 4


class FilingText(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    accession: str
    form: str
    filed: date
    period_end: date | None
    url: str
    chunks: tuple[Chunk, ...]
    text_blocks: tuple[str, ...]  # every text-block concept with content
    empty_blocks: tuple[str, ...] = ()  # text blocks wrapping no text at all
    hidden_fact_ids: tuple[str, ...]  # facts in the hidden header, not in any chunk
    visible_fact_ids: tuple[str, ...]  # every displayed numeric fact
    unresolved_units: tuple[str, ...] = ()  # unit IDs with no definition in the document
    issues: tuple[str, ...] = ()


def local(element: etree._Element) -> str:
    tag = element.tag
    return etree.QName(tag).localname if isinstance(tag, str) else ""


def is_ix(element: etree._Element) -> bool:
    tag = element.tag
    return isinstance(tag, str) and tag.startswith("{" + IX + "}")


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace(" ", " ")).strip()


def text_of(element: etree._Element) -> str:
    return clean("".join(str(t) for t in element.itertext()))


def is_table_block(concept: str) -> bool:
    return "Table" in concept.split(":", 1)[-1]


Period = tuple[date | None, date | None, bool, tuple[tuple[str, str], ...]]


def parse_contexts(root: etree._Element) -> dict[str, Period]:
    """Context ID -> (start, end or instant, dimensional, (axis, member) pairs)."""

    def day(node: etree._Element | None) -> date | None:
        value = (node.text or "").strip() if node is not None else ""
        if not value:
            return None
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None

    contexts = {}
    for context in root.iter(f"{{{XBRLI}}}context"):
        period = context.find(f"{{{XBRLI}}}period")
        if period is None:
            continue
        instant = day(period.find(f"{{{XBRLI}}}instant"))
        start = day(period.find(f"{{{XBRLI}}}startDate"))
        end = instant or day(period.find(f"{{{XBRLI}}}endDate"))
        members = tuple(
            sorted(
                (
                    str(e.get("dimension") or ""),
                    (e.text or "").strip() if local(e) == "explicitMember" else "",
                )
                for e in context.iter()
                if local(e) in ("explicitMember", "typedMember")
            )
        )
        contexts[str(context.get("id"))] = (start, end, bool(members), members)
    return contexts


def parse_units(root: etree._Element) -> dict[str, str]:
    """Unit ID -> measure ("USD", "shares", "pure", "USD/shares"); IDs are filer-chosen."""
    units = {}
    for unit in root.iter(f"{{{XBRLI}}}unit"):
        measures = [
            str(m.text or "").strip().rsplit(":", 1)[-1] for m in unit.iter(f"{{{XBRLI}}}measure")
        ]
        if not measures or not all(measures):
            continue
        divide = unit.find(f"{{{XBRLI}}}divide") is not None
        units[str(unit.get("id"))] = "/".join(measures) if divide else measures[0]
    return units


@dataclass
class Context:
    """Where an element sits: its text blocks, outermost first."""

    blocks: tuple[str, ...] = ()

    @property
    def section(self) -> str | None:
        notes = [b for b in self.blocks if not is_table_block(b)]
        return notes[0] if notes else None

    @property
    def table_tag(self) -> str | None:
        tables = [b for b in self.blocks if is_table_block(b)]
        return tables[-1] if tables else None


@dataclass
class Builder:
    filing: Filing
    period_end: date | None
    facts_of: dict[str, FactRef]
    chunks: list[Chunk] = field(default_factory=list)
    pending: list[tuple[str, list[FactRef], str | None]] = field(default_factory=list)
    pending_key: tuple[str, str, tuple[str, ...]] | None = None

    def chunk(self, **values: object) -> None:
        order = len(self.chunks)
        self.chunks.append(
            Chunk(
                chunk_id=f"{self.filing.accession}:{order:05d}",
                accession=self.filing.accession,
                form=self.filing.form,
                filed=self.filing.filed,
                period_end=self.period_end,
                order=order,
                **values,  # type: ignore[arg-type]
            )
        )

    def facts(self, element: etree._Element) -> list[FactRef]:
        found = [element] if local(element) == "nonFraction" and is_ix(element) else []
        found += list(element.iter(NON_FRACTION))
        ids = dict.fromkeys(str(e.get("id")) for e in found if e.get("id"))
        return [self.facts_of[i] for i in ids if i in self.facts_of]

    def add_text(
        self,
        key: tuple[str, str, tuple[str, ...]],
        text: str,
        facts: list[FactRef],
        anchor: str | None,
    ) -> None:
        if not text and not facts:
            return
        if self.pending_key != key:
            self.flush()
            self.pending_key = key
        self.pending.append((text, facts, anchor))
        if sum(len(t) for t, _, _ in self.pending) >= TARGET_CHARS:
            self.flush(keep_overlap=True)

    def flush(self, keep_overlap: bool = False) -> None:
        if not self.pending or self.pending_key is None:
            self.pending = []
            return
        section, title, blocks = self.pending_key
        text = " ".join(t for t, _, _ in self.pending if t)
        facts = list({f.fact_id: f for _, fs, _ in self.pending for f in fs}.values())
        anchor = next((a for _, _, a in self.pending if a), None)
        pieces = split_long(text) or [""]
        for i, piece in enumerate(pieces):
            self.chunk(
                section=section,
                title=title,
                kind="text",
                text=piece,
                blocks=blocks,
                # Facts belong to the first piece only: each fact in exactly one chunk.
                facts=tuple(facts) if i == 0 else (),
                anchor=anchor,
            )
        self.pending = []
        if keep_overlap:
            tail = overlap(pieces[-1])
            if tail:
                self.pending = [(tail, [], None)]

    def add_table(
        self, section: str, title: str, table: etree._Element, where: Context, before: str
    ) -> None:
        self.flush()
        self.pending_key = None
        rows = self.table_rows(table)
        if not rows:
            return
        header_rows = count_header_rows(rows)
        inherited = False
        # The table just above, skipping page furniture (short text such as a footer).
        previous = None
        for earlier in reversed(self.chunks[-4:]):
            if earlier.kind == "table":
                previous = earlier
                break
            # Page furniture (a footer, a page number) may sit outside the note,
            # between it and its continuation; real text ends the look-back.
            if len(earlier.text) > 200:
                break
        if (
            header_rows == 0
            and value_columns(rows) >= 2
            and previous is not None
            and previous.kind == "table"
            and previous.section == section
            and previous.header_rows
            and value_columns(list(map(list, previous.rows))) == value_columns(rows)
        ):
            rows = [list(r) for r in previous.rows[: previous.header_rows]] + rows
            header_rows, inherited = previous.header_rows, True
        facts = list({f.fact_id: f for r in rows for c in r for f in c.facts}.values())
        units = table_units(facts)
        if units is None:
            header_text = " ".join(c.text for r in rows[: header_rows + 1] for c in r)
            match = UNITS.search(header_text) or UNITS.search(before)
            units = clean(match.group(0)).strip("() ") if match else None
        label = where.table_tag.split(":", 1)[-1] if where.table_tag else title
        lines = [f"Table: {label}"] + ([f"Units: {units}"] if units else [])
        caption = lead_in(before)
        if caption:
            lines.append(f"Caption: {caption}")
        if inherited:
            lines.append("Columns (from the table above):")
        lines += [" | ".join(c.text for c in row) for row in rows]
        self.chunk(
            section=section,
            title=title,
            kind="table",
            text="\n".join(lines),
            blocks=where.blocks,
            table_tag=where.table_tag,
            units=units,
            rows=tuple(tuple(r) for r in rows),
            header_rows=header_rows,
            inherited_header=inherited,
            facts=tuple(facts),
            anchor=table.get("id"),
        )

    def table_rows(self, table: etree._Element) -> list[list[Cell]]:
        """Visible cells per row; "$", "(", "%" and ")" fragments joined to their numbers."""
        rows: list[list[Cell]] = []
        for tr in table.iter("{*}tr"):
            cells: list[Cell] = []
            for td in tr:
                if local(td) not in ("td", "th"):
                    continue
                text = text_of(td)
                facts = self.facts(td)
                if not text and not facts:
                    continue
                if cells and text in (")", "%", ")%", "%)") and not facts:
                    last = cells[-1]
                    cells[-1] = Cell(text=last.text + text, facts=last.facts)
                    continue
                if cells and cells[-1].text in ("$", "(", "$(") and not cells[-1].facts:
                    cells[-1] = Cell(text=cells[-1].text + text, facts=tuple(facts))
                    continue
                cells.append(Cell(text=text, facts=tuple(facts)))
            if cells:
                rows.append(cells)
        return rows


def fact_value(node: etree._Element, scale: int) -> float | None:
    """The number a fact states: shown digits x 10^scale, negated when signed "-"."""
    shown = text_of(node)
    form = str(node.get("format", "")).rsplit(":", 1)[-1].lower()
    if form in ("fixed-zero", "zerodash", "fixedzero") or shown in ("—", "–", "-", "") and form:
        number = 0.0
    elif form in ("num-comma-decimal", "numcommadecimal"):
        try:
            number = float(shown.replace(".", "").replace(" ", "").replace(",", "."))
        except ValueError:
            return None
    else:
        try:
            number = float(shown.replace(",", "").replace(" ", ""))
        except ValueError:
            return None
    value = number * 10.0**scale
    return -value if node.get("sign") == "-" else value


def table_units(facts: list[FactRef]) -> str | None:
    """Units stated by the tagged numbers themselves (most common unit and scale)."""
    if not facts:
        return None
    unit, scale = Counter((f.unit, f.scale) for f in facts).most_common(1)[0][0]
    if unit is None:
        return None
    if unit == "pure":
        return "ratio or percent"
    return f"{unit} in {SCALES[scale]}" if scale in SCALES else unit


def lead_in(text: str) -> str | None:
    """The sentence introducing a table ("The following table summarizes ..."), if any."""
    sentence = re.split(r"(?<=[.!?])\s+", text.strip())[-1] if text.strip() else ""
    if len(sentence) < 25 or re.search(r"table of contents|^page\b|^f-\s?\d", sentence, re.I):
        return None  # page furniture, not an introduction
    if sentence.endswith(":") or re.search(r"\bfollowing\b|\btable\b", sentence, re.I):
        return sentence[-240:]
    return None


def value_columns(rows: list[list[Cell]]) -> int:
    """The usual number of tagged cells per row: the value columns to tell apart.

    The most common count, not the largest: a label cell may carry its own facts
    ("206,000,000 shares authorized") without adding a column.
    """
    counts = Counter(n for row in rows if (n := sum(1 for cell in row if cell.facts)))
    return counts.most_common(1)[0][0] if counts else 0


def count_header_rows(rows: list[list[Cell]]) -> int:
    """Rows before the first data row: one with a tagged number, else a number not a year."""
    tagged = any(c.facts for r in rows for c in r)
    for i, row in enumerate(rows):
        if tagged and any(c.facts for c in row):
            return i
        values = [c.text for c in row[1:]]
        if not tagged and any(NUMERIC.match(v) and not YEAR.match(v) for v in values):
            return i
    return 0


def split_long(text: str) -> list[str]:
    """Pieces of at most MAX_CHARS, cut at sentence ends where possible."""
    pieces: list[str] = []
    while len(text) > MAX_CHARS:
        cut = text.rfind(". ", TARGET_CHARS // 2, MAX_CHARS)
        cut = cut + 1 if cut > 0 else MAX_CHARS
        pieces.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        pieces.append(text)
    return pieces


def overlap(text: str) -> str:
    """The last sentence(s), up to OVERLAP_CHARS, repeated at the start of the next passage."""
    if len(text) <= OVERLAP_CHARS:
        return ""
    tail = text[-OVERLAP_CHARS:]
    start = tail.find(". ")
    return tail[start + 2 :] if start >= 0 else ""


def parse_filing(
    document: str, filing: Filing, url: str, period_end: date | None = None
) -> FilingText:
    parser = etree.XMLParser(recover=True, huge_tree=True, resolve_entities=False, no_network=True)
    root = etree.fromstring(document.encode("utf-8"), parser)
    if root is None:
        raise SECError("Unreadable filing document")
    issues: list[str] = []
    contexts = parse_contexts(root)
    units = parse_units(root)
    facts_of: dict[str, FactRef] = {}
    for node in root.iter(NON_FRACTION):
        fact_id = node.get("id")
        if not fact_id:
            continue
        start, end, dimensional, members = contexts.get(
            str(node.get("contextRef")), (None, None, False, ())
        )
        try:
            scale = int(node.get("scale") or 0)
        except ValueError:
            scale = 0
        facts_of[str(fact_id)] = FactRef(
            fact_id=str(fact_id),
            concept=str(node.get("name")),
            unit=units.get(str(node.get("unitRef")), node.get("unitRef")),
            scale=scale,
            negative=node.get("sign") == "-",
            start=start,
            end=end,
            dimensional=dimensional,
            value=fact_value(node, scale),
            shown=text_of(node),
            members=members,
        )
    # Text blocks and the continuation chains that extend them.
    heads: dict[str, str] = {}  # head element id -> text-block concept
    continued: dict[str, str] = {}  # continuation id -> head id
    blocks = [e for e in root.iter(NON_NUMERIC) if str(e.get("name", "")).endswith("TextBlock")]
    conts = {str(c.get("id")): c for c in root.iter(CONTINUATION)}
    block_by_id: dict[str, etree._Element] = {}
    for block in blocks:
        head = str(block.get("id") or id(block))
        heads[head] = str(block.get("name"))
        block_by_id[head] = block
        nxt = block.get("continuedAt")
        for _ in range(500):
            if not nxt:
                break
            continued[nxt] = head
            cont = conts.get(nxt)
            if cont is None:
                issues.append(f"Missing continuation {nxt} of {block.get('name')}")
                break
            nxt = cont.get("continuedAt")
    # Continuations of plain text facts and footnotes (not text blocks) are linked too.
    linked = set(continued)
    for fact in root.iter(NON_NUMERIC, f"{{{IX}}}footnote"):
        nxt = fact.get("continuedAt")
        for _ in range(500):
            if not nxt or nxt in linked:
                break
            linked.add(nxt)
            cont = conts.get(nxt)
            nxt = cont.get("continuedAt") if cont is not None else None
    orphans = [c for c in conts if c not in linked]
    if orphans:
        issues.append(f"{len(orphans)} continuation(s) not linked to a text block")

    # The hidden header is removed only now: facts and text blocks in it may own
    # continuations shown in the body.
    headers = list(root.iter(HEADER))
    hidden = list(dict.fromkeys(str(e.get("id")) for h in headers for e in h.iter(NON_FRACTION)))
    hidden_heads = {str(e.get("id") or id(e)) for h in headers for e in h.iter(NON_NUMERIC)}
    hidden_conts = {str(e.get("id")) for h in headers for e in h.iter(CONTINUATION)}
    for h in headers:
        parent = h.getparent()
        if parent is not None:
            parent.remove(h)
    visible = [str(e.get("id")) for e in root.iter(NON_FRACTION) if e.get("id")]

    def context(element: etree._Element) -> Context:
        found: list[str] = []
        for ancestor in [element, *element.iterancestors()]:
            if not is_ix(ancestor):
                continue
            concept = None
            if local(ancestor) == "nonNumeric" and str(ancestor.get("name", "")).endswith(
                "TextBlock"
            ):
                concept = heads.get(str(ancestor.get("id") or id(ancestor)))
            elif local(ancestor) == "continuation":
                head = continued.get(str(ancestor.get("id")))
                concept = heads.get(head) if head else None
            if concept and concept not in found:
                found.append(concept)
        outer = list(reversed(found))  # outermost first
        # Text blocks wrapped around inline text inside this element.
        for inner in element.iter(NON_NUMERIC, CONTINUATION):
            if inner is element:
                continue
            if local(inner) == "nonNumeric":
                name = str(inner.get("name", ""))
                concept = (
                    heads.get(str(inner.get("id") or id(inner)))
                    if name.endswith("TextBlock")
                    else None
                )
            else:
                head = continued.get(str(inner.get("id")))
                concept = heads.get(head) if head else None
            if concept and concept not in outer:
                outer.append(concept)
        return Context(blocks=tuple(outer))

    has_block: dict[etree._Element, bool] = {}

    def mark(element: etree._Element) -> bool:
        flag = False
        for child in element:
            if mark(child) or local(child) in BLOCK:
                flag = True
        has_block[element] = flag
        return flag

    body = next(root.iter("{*}body"), root)
    mark(body)
    builder = Builder(filing, period_end, facts_of)
    state = {"item": "front", "title": "Cover", "previous": "", "heading": ""}
    titles: dict[str, str] = {}

    def title_for(section: str, first_text: str) -> str:
        if section not in titles:
            heading = state["heading"]
            titles[section] = heading if NOTE.match(heading) else first_text[:120]
            state["heading"] = ""  # a heading titles only the first note after it
        return titles[section]

    def emit_text(element: etree._Element) -> None:
        text = text_of(element)
        facts = builder.facts(element)
        if not text and not facts:
            return
        where = context(element)
        short = len(text) < 120
        if where.section is None:
            item = ITEM.match(text)
            if item and len(text) < 160:
                builder.flush()
                state["item"] = "item_" + item.group(1).lower()
                state["title"] = text
            elif short and (text.isupper() or NOTE.match(text)):
                state["title"] = text  # a statement title or a note heading
            if short and (NOTE.match(text) or text.isupper()):
                state["heading"] = text
            key = (state["item"], state["title"], where.blocks)
        else:
            key = (where.section, title_for(where.section, text), where.blocks)
        builder.add_text(key, text, facts, element.get("id"))
        state["previous"] = text[-300:]

    def walk(element: etree._Element) -> None:
        for child in element:
            if not isinstance(child.tag, str):
                continue
            if local(child) == "table":
                where = context(child)
                if where.section:
                    section = where.section
                    title = title_for(section, text_of(child))
                else:
                    section, title = state["item"], state["title"]
                builder.add_table(section, title, child, where, state["previous"])
            elif has_block.get(child):
                walk(child)
            else:
                emit_text(child)

    walk(body)
    builder.flush()
    # Displayed content only: a block counts if its own text or a continuation is shown.
    with_text = {
        heads[head]
        for head, element in block_by_id.items()
        if head not in hidden_heads and text_of(element)
    }
    with_text |= {
        heads[h]
        for c, h in continued.items()
        if c in conts and c not in hidden_conts and text_of(conts[c])
    }
    empty = sorted(set(heads.values()) - with_text)
    return FilingText(
        accession=filing.accession,
        form=filing.form,
        filed=filing.filed,
        period_end=period_end,
        url=url,
        chunks=tuple(builder.chunks),
        text_blocks=tuple(sorted(set(heads.values()) - set(empty))),
        empty_blocks=tuple(sorted(empty)),
        hidden_fact_ids=tuple(hidden),
        visible_fact_ids=tuple(dict.fromkeys(visible)),
        unresolved_units=tuple(
            sorted({str(n.get("unitRef")) for n in root.iter(NON_FRACTION)} - set(units))
        ),
        issues=tuple(dict.fromkeys(issues)),
    )


def filing_texts(sec: SECClient, cik: str, as_of: date) -> list[FilingText]:
    """The latest 10-K filed by the date and the 10-Qs filed after it, parsed."""
    source, annual = periodic_filings(sec, cik, as_of, ("10-K",))
    if not annual:
        return []
    latest = annual[0]
    _, quarters = periodic_filings(sec, cik, as_of, ("10-Q",))
    selected = [latest] + [
        q
        for q in quarters
        if q.report_date and latest.report_date and q.report_date > latest.report_date
    ]
    texts = []
    for filing in selected:
        url = (
            f"https://www.sec.gov/Archives/edgar/data/{int(source)}/"
            f"{filing.accession.replace('-', '')}/{filing.primary_document}"
        )
        document = sec.get_filing_document(source, filing, as_of)
        texts.append(parse_filing(document, filing, url, filing.report_date))
    return texts
