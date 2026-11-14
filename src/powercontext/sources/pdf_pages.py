# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Capture physical PDF pages with the pdf extra and search with the builtin extra."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from hashlib import sha256
from io import BytesIO
from math import log
from typing import TYPE_CHECKING, Annotated, NamedTuple, cast

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from powercontext.artifacts import MemoryCitation
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.models import Source, SourceMaterialization

if TYPE_CHECKING:
    from powercontext.builtin.runtime.application import ScopedMemoryApplication
    from powercontext.builtin.runtime.models import MemoryEntryRecord

PDF_PAGE_SOURCE_TYPE = "pdf-page"


def _nonblank_id(value: str) -> str:
    if not value.strip():
        raise ValueError("document_id must not be blank")  # noqa: TRY003
    return value


_DocumentId = Annotated[str, AfterValidator(_nonblank_id)]


class PDFDocumentInput(BaseModel):
    """Original PDF bytes and the caller's stable document identity."""

    document_id: _DocumentId
    content: bytes


class PDFPageInput(BaseModel):
    """One physical page of an exact PDF revision."""

    model_config = ConfigDict(frozen=True)

    document_id: _DocumentId
    document_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    page_number: Annotated[int, Field(ge=1)]
    text: str


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Extract all pages in physical order, or raise ValueError for an unreadable PDF."""

    from pypdf import PasswordType, PdfReader
    from pypdf.errors import PdfReadError
    from pypdf.generic import DictionaryObject

    from powercontext.sources._pdf_text import extract_page_text

    content = value.content
    # pypdf can recover a truncated EOF marker even in strict mode.
    if not content.rstrip(b"\x00\t\n\f\r ").endswith(b"%%EOF"):
        raise ValueError("PDF is missing its end-of-file marker")  # noqa: TRY003
    try:
        with BytesIO(content) as stream, PdfReader(stream, strict=True) as reader:
            if reader.is_encrypted and reader.decrypt("") == PasswordType.NOT_DECRYPTED:
                raise PdfReadError("PDF requires a password")  # noqa: TRY003, TRY301
            page_tree = cast(DictionaryObject, reader.root_object["/Pages"])
            count = page_tree["/Count"]
            if not isinstance(count, int) or count != len(reader.pages):
                raise PdfReadError("PDF page tree has an inconsistent count")  # noqa: TRY003, TRY301
            texts = tuple(extract_page_text(page) for page in reader.pages)
            if reader.is_encrypted:
                # For encrypted files len(reader.pages) uses /Count directly.
                try:
                    reader.get_page(count)
                except IndexError:
                    pass
                else:
                    raise PdfReadError("PDF page tree has an inconsistent count")  # noqa: TRY003
    except Exception as error:
        # Malformed objects and streams can also raise built-in exceptions in pypdf.
        raise ValueError("could not read every page of the PDF") from error  # noqa: TRY003

    digest = sha256(content).hexdigest()
    return tuple(
        PDFPageInput(
            document_id=value.document_id,
            document_sha256=digest,
            page_number=number,
            text=unicodedata.normalize("NFC", text),
        )
        for number, text in enumerate(texts, 1)
    )


class _PDFPageSource(Source):
    page: PDFPageInput


class _PDFPageAdapter:
    name = PDF_PAGE_SOURCE_TYPE
    input_class = PDFPageInput
    source_class = _PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> _PDFPageSource:
        # The digest and page number have fixed syntax, so the suffix is unambiguous
        # even when the document ID contains a separator.
        identity = f"{value.document_id}\0{value.document_sha256}\0{value.page_number}"
        return _PDFPageSource(
            name=sha256(identity.encode()).hexdigest(), materialization=SourceMaterialization.CAPTURED, page=value
        )

    async def read(self, source: _PDFPageSource, /) -> PDFPageInput:
        return source.page


class _PDFPageProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = _PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: _PDFPageSource, /) -> TextEvidence:
        page = source.page
        return TextEvidence(
            source_type=PDF_PAGE_SOURCE_TYPE,
            source_id=source.name,
            content=f"{page.document_id}\n{page.text}",
            metadata={
                "document_id": page.document_id,
                "document_sha256": page.document_sha256,
                "page_number": page.page_number,
            },
        )


PDF_PAGE_SOURCE_DEFINITION = AdapterSourceDefinition(_PDFPageAdapter(), projections=(_PDFPageProjection(),))

# ---------------------------------------------------------------------------
# Deterministic, offline evidence-page retrieval over one scoped Memory.
#
# Every physical page is one Memory entry whose text is ``document_id\nPAGE_TEXT``.
# The ranker rebuilds a compact in-process index from one exact Memory snapshot,
# applies BM25 over the question terms plus disclosure-structure signals (primary
# statement pages, MD&A/segment sections, page headings, numeric density), and
# keeps a small number of reserved slots for primary statements so a single
# repeated term cannot crowd out the page that actually carries the evidence.
# ---------------------------------------------------------------------------

_WORD = re.compile(r"[a-z]+|\d+")
_YEAR = re.compile(r"(?:19|20)\d{2}")
_STOP = frozenset({
    "a",
    "about",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "be",
    "been",
    "being",
    "both",
    "but",
    "by",
    "can",
    "could",
    "did",
    "do",
    "does",
    "each",
    "for",
    "from",
    "had",
    "has",
    "have",
    "how",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "may",
    "of",
    "on",
    "or",
    "our",
    "please",
    "should",
    "than",
    "that",
    "the",
    "their",
    "then",
    "there",
    "these",
    "they",
    "this",
    "those",
    "to",
    "was",
    "we",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "will",
    "with",
    "would",
    "you",
    "your",
})
# Answer-writing instructions carry no evidence and dilute a long natural question.
_INSTRUCTION = re.compile(
    r"(respond to the question|give a response|answer in |answer concisely|please state|"
    r"state that and explain|explain why|assuming the perspective|relying on the details|"
    r"use only the|based on the details shown|from the statement of cash flows and the)"
)
_STATEMENTS = {
    "income": re.compile(
        r"(?:consolidated )?statements? of (?:consolidated )?(?:income|earnings|operations)"
        r"|consolidated statements? of income"
    ),
    "balance": re.compile(r"balance sheets?|statements? of (?:consolidated )?financial position"),
    "cashflow": re.compile(r"statements? of (?:consolidated )?cash flows?"),
}
_SECTIONS = {
    "mda": re.compile(
        r"results of operations|analysis of consolidated|management.s discussion|executive overview|our business"
    ),
    "segment": re.compile(
        r"business segment|reportable segment|segment results|sales and operating income|performance by business"
    ),
    "notes": re.compile(r"note \d+|notes to (?:the )?consolidated financial statements"),
}
# Metric vocabulary that identifies which disclosure carries the evidence.
_LEXICON = (
    (
        re.compile(r"capital[ -]?intensive"),
        ("capital expenditures", "property plant equipment", "capex", "purchases of property"),
    ),
    (re.compile(r"liquidity|quick ratio|current ratio"), ("current assets", "current liabilities", "quick", "liquid")),
    (re.compile(r"\bdebt\b|borrowings"), ("long term debt", "debt", "borrowings")),
    (re.compile(r"inventory turnover|sold its inventory"), ("inventories", "cost of sales", "cost of products sold")),
    (re.compile(r"customer concentration"), ("customer", "customers", "net revenue", "accounted for")),
    (re.compile(r"gross margin|gross profit"), ("gross profit", "cost of products sold", "cost of sales")),
    (re.compile(r"operating margin|operating income|operating profit"), ("operating income", "operating profit")),
    (re.compile(r"revenue|sales"), ("net sales", "revenues", "sales")),
    (re.compile(r"segment"), ("business segment", "segments", "segment")),
    (re.compile(r"growth|grow"), ("results of operations", "sales increased", "growth")),
    (re.compile(r"ebitda|depreciation|amortization|capex"), ("depreciation", "amortization", "cash flows")),
    (re.compile(r"employees|workforce|headcount"), ("employees", "workforce")),
    (re.compile(r"legal|litigation|lawsuit"), ("legal proceedings", "litigation", "contingencies")),
    (re.compile(r"geograph|region|country|countries"), ("countries", "territories", "regions", "operations")),
)
_TOPIC_STATEMENTS = (
    (
        re.compile(r"capital[ -]?intensive|capex|cash flow|ebitda|depreciation"),
        frozenset({"cashflow", "balance", "income"}),
    ),
    (re.compile(r"liquidity|quick ratio|current ratio|working capital"), frozenset({"balance"})),
    (re.compile(r"gross margin|operating margin|revenue|sales|growth|earnings"), frozenset({"income"})),
    (re.compile(r"inventory"), frozenset({"balance", "income"})),
    (re.compile(r"debt|borrowings"), frozenset({"balance", "cashflow"})),
)
_TOPIC_SECTIONS = (
    (re.compile(r"drove|drive|change|gross margin|operating margin|revenue|sales|growth"), frozenset({"mda"})),
    (re.compile(r"segment"), frozenset({"segment"})),
)
_NUMERIC_QUESTION = re.compile(r"ratio|margin|percent|amount|how many|value|cost|revenue|sales|growth|calculate")

_K1 = 1.2
_B = 0.75
_STATEMENT_BOOST = 8.0
_SECTION_BOOST = 5.0
_HEADING_BOOST = 2.0
_NUMERIC_BOOST = 1.5
_MAX_STATEMENT_SLOTS = 6
_MAX_SECTION_SLOTS = 4
_MAX_CACHED_SCOPES = 4


def _stem(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "is", "us")):
        return word[:-1]
    return word


def _clean_question(question: str) -> str:
    clauses = re.split(r"(?<=[.;?])\s+", question)
    kept = [clause for clause in clauses if not _INSTRUCTION.search(clause.casefold())]
    return " ".join(kept) if kept else question


def _question_terms(question: str) -> tuple[str, ...]:
    words = (_stem(word) for word in _WORD.findall(_clean_question(question).casefold()))
    seen: dict[str, None] = {}
    for word in words:
        if len(word) > 1 and word not in _STOP:
            seen.setdefault(word, None)
    return tuple(seen)


def _company_grams(question: str) -> set[str]:
    words = _WORD.findall(_clean_question(question).casefold())
    grams: set[str] = set()
    for start in range(len(words)):
        for end in range(start + 1, min(start + 4, len(words)) + 1):
            grams.add("".join(words[start:end]))
    return grams


def _company_key(document_id: str) -> tuple[tuple[str, ...], str]:
    parts = []
    for part in document_id.split("_"):
        if _YEAR.fullmatch(part):
            break
        if part:
            parts.append(part.casefold())
    return tuple(parts), "".join(parts)


def _company_variants(document_id: str) -> set[str]:
    parts, key = _company_key(document_id)
    variants = {key, key.replace("and", "")}
    if len(parts) >= 2:
        variants.add("".join(part[0] for part in parts))
        initials = {part[0] for part in parts}
        if len(initials) == 1:
            # J&J-style names are also written as JnJ / JandJ.
            initial = next(iter(initials))
            variants.update({f"{initial}n{initial}", f"{initial}and{initial}", f"{initial}&{initial}"})
    return variants


def _company_matches(document_id: str, grams: set[str]) -> bool:
    if _company_variants(document_id) & grams:
        return True
    _, key = _company_key(document_id)
    return any(len(gram) >= 4 and gram in key for gram in grams)


def _heading_zone(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()][:10]
    return " ".join(lines).casefold()


class _Page(NamedTuple):
    citation: MemoryCitation
    source_id: str
    version_id: str
    document_id: str
    counts: Counter[str]
    length: int
    zone: str
    statements: frozenset[str]
    sections: frozenset[str]
    numeric: float


class _Corpus(NamedTuple):
    pages: tuple[_Page, ...]
    frequencies: Counter[str]
    total: int
    average: float

    def idf(self, term: str) -> float:
        frequency = self.frequencies.get(term, 0)
        return log(1 + (self.total - frequency + 0.5) / (frequency + 0.5))


_CORPORA: dict[object, _Corpus] = {}


async def _corpus_for(memory: ScopedMemoryApplication) -> _Corpus | None:
    """Index one exact Memory snapshot once per scope and reuse it across questions."""

    key = getattr(memory, "scope_id", None) or id(memory)
    cached = _CORPORA.get(key)
    if cached is not None:
        return cached

    snapshot = await memory.list()
    pages: list[_Page] = []
    frequencies: Counter[str] = Counter()
    for record in snapshot.entries:
        pages.extend(_page_from_record(record, frequencies))
    if not pages:
        return None
    total = len(pages)
    average = sum(page.length for page in pages) / total
    corpus = _Corpus(pages=tuple(pages), frequencies=frequencies, total=total, average=average)
    if len(_CORPORA) >= _MAX_CACHED_SCOPES:
        _CORPORA.pop(next(iter(_CORPORA)))
    _CORPORA[key] = corpus
    return corpus


def _page_from_record(record: MemoryEntryRecord, frequencies: Counter[str]) -> list[_Page]:
    sources = record.entry.sources
    if len(sources) != 1 or sources[0].source_type != PDF_PAGE_SOURCE_TYPE:
        return []
    label, _, body = record.entry.text.partition("\n")
    counts = Counter(_stem(word) for word in _WORD.findall(body.casefold()))
    frequencies.update(counts.keys())
    zone = _heading_zone(body)
    statements = frozenset(kind for kind, pattern in _STATEMENTS.items() if pattern.search(zone))
    if len(statements) >= 2:
        # A table of contents lists several statements; only a real statement page
        # carries one disclosure heading at the top of the page.
        statements = frozenset()
    sections = frozenset(name for name, pattern in _SECTIONS.items() if pattern.search(zone))
    digits = sum(count for word, count in counts.items() if word.isdigit())
    total = sum(counts.values())
    return [
        _Page(
            citation=record.citation,
            source_id=sources[0].source_id,
            version_id=record.entry.entry_version_id,
            document_id=label,
            counts=counts,
            length=total,
            zone=zone,
            statements=statements,
            sections=sections,
            numeric=digits / max(total, 1),
        )
    ]


def _rank_pages(
    corpus: _Corpus,
    question: str,
    limit: int,
    fts_ranks: dict[str, int],
) -> list[_Page]:
    terms = _question_terms(question)
    if not terms or limit <= 0:
        return []

    grams = _company_grams(question)
    documents = {page.document_id for page in corpus.pages}
    matched = {document for document in documents if _company_matches(document, grams)}

    lowered = question.casefold()
    topic_statements: set[str] = set()
    topic_sections: set[str] = set()
    expansions: list[str] = []
    for pattern, words in _LEXICON:
        if pattern.search(lowered):
            expansions.extend(words)
    for pattern, kinds in _TOPIC_STATEMENTS:
        if pattern.search(lowered):
            topic_statements.update(kinds)
    for pattern, names in _TOPIC_SECTIONS:
        if pattern.search(lowered):
            topic_sections.update(names)
    expanded = {_stem(word) for word in terms}
    for phrase in expansions:
        expanded.update(_stem(word) for word in phrase.split() if word not in _STOP)
    weights = {term: corpus.idf(term) for term in expanded}
    numeric_question = _NUMERIC_QUESTION.search(lowered) is not None

    scored: list[tuple[float, _Page]] = []
    for page in corpus.pages:
        lexical = 0.0
        for term, weight in weights.items():
            tf = page.counts.get(term, 0)
            if tf:
                lexical += weight * tf * (_K1 + 1) / (tf + _K1 * (1 - _B + _B * page.length / corpus.average))
        if lexical <= 0:
            continue
        score = lexical
        if topic_statements & page.statements:
            score += _STATEMENT_BOOST
        if topic_sections & page.sections:
            score += _SECTION_BOOST
        heading_hits = sum(1 for term in terms[:10] if term in page.zone)
        if heading_hits:
            score += _HEADING_BOOST * heading_hits
        if numeric_question and page.numeric > 0.12:
            score += _NUMERIC_BOOST
        rank = fts_ranks.get(page.version_id)
        if rank is not None:
            # Lexical order stays authoritative; the store's own ranking only
            # settles exact score ties between otherwise equivalent pages.
            score += 0.001 / (1 + rank)
        scored.append((score, page))

    if not scored:
        return []
    scored.sort(key=lambda item: item[0], reverse=True)

    # When the question names a company, its report's pages come first; a clear
    # lexical majority of one report does the same for unnamed questions. The
    # remaining reports stay available so a larger limit can still fill up.
    dominant = None
    if len(documents) > 1:
        mass: Counter[str] = Counter()
        for score, page in scored[:80]:
            mass[page.document_id] += score
        leading = mass.most_common(2)
        if leading and (len(leading) == 1 or leading[0][1] > 1.6 * leading[1][1]):
            dominant = leading[0][0]
    preferred: list[set[str]] = []
    if matched:
        preferred.append(matched)
    if dominant is not None and dominant not in matched:
        preferred.append({dominant})

    def tier(page: _Page) -> int:
        for index, documents_in_tier in enumerate(preferred):
            if page.document_id in documents_in_tier:
                return index
        return len(preferred)

    scored.sort(key=lambda item: (tier(item[1]), -item[0]))

    chosen: list[_Page] = []
    seen: set[str] = set()

    def take(page: _Page) -> None:
        if page.source_id not in seen:
            seen.add(page.source_id)
            chosen.append(page)

    # Reserve a few slots inside the preferred report for the primary statements a
    # question implies, so one repeated term cannot crowd out the evidence page.
    primary_tier = tier(scored[0][1])
    preferred_pages = [item for item in scored if tier(item[1]) == primary_tier]
    if topic_statements:
        by_kind: dict[str, list[tuple[float, _Page]]] = defaultdict(list)
        for item in preferred_pages:
            for kind in item[1].statements & topic_statements:
                by_kind[kind].append(item)
        reserved = 0
        for items in by_kind.values():
            for item in items[:2]:
                if reserved >= _MAX_STATEMENT_SLOTS:
                    break
                take(item[1])
                reserved += 1
    if topic_sections:
        by_section: dict[str, list[tuple[float, _Page]]] = defaultdict(list)
        for item in preferred_pages:
            for name in item[1].sections & topic_sections:
                by_section[name].append(item)
        reserved = 0
        for items in by_section.values():
            for item in items[:2]:
                if reserved >= _MAX_SECTION_SLOTS:
                    break
                take(item[1])
                reserved += 1
    for _, page in scored:
        if len(chosen) >= limit:
            break
        take(page)
    return chosen[:limit]


async def search_pdf_pages(
    memory: ScopedMemoryApplication, question: str, limit: int = 10
) -> tuple[MemoryCitation, ...]:
    """Search a scoped Memory for distinct, exactly citable PDF pages."""

    from powercontext.builtin.runtime.models import SearchMemoryRequest

    if limit <= 0 or not question.strip():
        return ()
    corpus = await _corpus_for(memory)
    if corpus is None:
        return ()

    fts_ranks: dict[str, int] = {}
    query = " ".join(_question_terms(question)[:24])
    if query:
        try:
            page = await memory.search(SearchMemoryRequest(query=query, mode="fts", limit=max(4 * limit, 64)))
        except Exception:
            page = None
        if page is not None:
            fts_ranks = {hit.entry_version_id: rank for rank, hit in enumerate(page.hits)}

    return tuple(page.citation for page in _rank_pages(corpus, question, limit, fts_ranks))


__all__ = ["PDF_PAGE_SOURCE_DEFINITION", "PDFDocumentInput", "PDFPageInput", "parse_pdf_document", "search_pdf_pages"]
