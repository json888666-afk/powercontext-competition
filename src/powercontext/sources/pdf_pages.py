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

"""Complete PDF capture and offline, citation-preserving financial page retrieval."""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass
from typing import Annotated, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator

from powercontext.artifacts import MemoryCitation
from powercontext.builtin.runtime.models import MemoryEntriesPage, MemorySearchPage, SearchMemoryRequest
from powercontext.sources import TEXT_EVIDENCE_PROJECTION_KEY, AdapterSourceDefinition, TextEvidence
from powercontext.sources.models import Source, SourceMaterialization


class PDFDocumentInput(BaseModel):
    """One original, complete PDF revision supplied by the caller."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    content: bytes

    @field_validator("document_id")
    @classmethod
    def require_document_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("document_id must not be blank")  # noqa: TRY003
        return value


class PDFPageInput(BaseModel):
    """Text of one physical page, including pages without extractable text."""

    model_config = ConfigDict(frozen=True)

    document_id: str
    document_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    page_number: Annotated[int, Field(ge=1, strict=True)]
    text: str

    _require_document_id = field_validator("document_id")(PDFDocumentInput.require_document_id.__func__)


def parse_pdf_document(value: PDFDocumentInput) -> tuple[PDFPageInput, ...]:
    """Extract every page atomically; normalize unreadable input to ValueError.

    Permission-only encryption with an empty user password is supported. Extraction
    uses pypdf's plain mode so table numbers and all text orientations are retained.
    No text normalization is applied to the captured evidence.
    """

    from pypdf import PdfReader
    from pypdf.generic import DictionaryObject

    if (
        not value.document_id.strip()
        or not value.content.startswith(b"%PDF-")
        or not value.content.rstrip(b"\x00\t\n\f\r ").endswith(b"%%EOF")
    ):
        raise ValueError("expected a document ID and original PDF bytes")  # noqa: TRY003
    digest = hashlib.sha256(value.content).hexdigest()
    try:
        reader = PdfReader(io.BytesIO(value.content), strict=True)
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("PDF requires a user password")  # noqa: TRY003, TRY301
        count = len(reader.pages)
        tree = reader.root_object["/Pages"]
        if not isinstance(tree, DictionaryObject) or not count or count != tree["/Count"]:
            raise ValueError("PDF page tree is empty or incomplete")  # noqa: TRY003, TRY301
        pages = tuple(
            PDFPageInput(
                document_id=value.document_id,
                document_sha256=digest,
                page_number=index,
                text=page.extract_text() or "",
            )
            for index, page in enumerate(reader.pages, 1)
        )
    except Exception as error:
        raise ValueError("PDF could not be completely parsed") from error  # noqa: TRY003
    return pages


class PDFPageSource(Source):
    """Captured page payload; older revisions remain independently readable."""

    page: PDFPageInput


class _PDFPageAdapter:
    input_class = PDFPageInput
    name = "pdf-page"
    source_class = PDFPageSource

    async def resolve(self, value: PDFPageInput, /) -> PDFPageSource:
        # Hash an unambiguous tuple, rather than concatenating arbitrary IDs with
        # delimiters. Long/Unicode document IDs still yield bounded source names.
        identity = json.dumps(
            [value.document_id, value.document_sha256, value.page_number],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return PDFPageSource(
            name="pdf-page:" + hashlib.sha256(identity).hexdigest(),
            materialization=SourceMaterialization.CAPTURED,
            page=value,
        )

    async def read(self, source: PDFPageSource, /) -> PDFPageInput:
        return source.page


class _PDFPageProjection:
    name = TEXT_EVIDENCE_PROJECTION_KEY.name
    version = TEXT_EVIDENCE_PROJECTION_KEY.version
    source_class = PDFPageSource
    output_class: type[BaseModel] = TextEvidence

    def project(self, source: PDFPageSource, /) -> TextEvidence:
        page = source.page
        return TextEvidence(
            source_type="pdf-page",
            source_id=source.name,
            content=f"{page.document_id}\n{page.text}",
            metadata={
                "document_id": page.document_id,
                "document_sha256": page.document_sha256,
                "page_number": page.page_number,
            },
        )


PDF_PAGE_SOURCE_DEFINITION = AdapterSourceDefinition(_PDFPageAdapter(), projections=(_PDFPageProjection(),))


class _ScopedMemory(Protocol):
    async def search(self, request: SearchMemoryRequest, /) -> MemorySearchPage: ...

    async def list(self, *, include_inactive: bool = False) -> MemoryEntriesPage: ...


_WORDS = re.compile(r"[a-z]+|\d+")
_STOP = frozenset([
    "a",
    "an",
    "the",
    "and",
    "or",
    "of",
    "to",
    "in",
    "on",
    "at",
    "by",
    "for",
    "from",
    "with",
    "as",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "what",
    "which",
    "who",
    "how",
    "why",
    "when",
    "where",
    "do",
    "does",
    "did",
    "can",
    "could",
    "should",
    "would",
    "has",
    "have",
    "had",
    "it",
    "its",
    "this",
    "that",
    "these",
    "those",
    "any",
    "all",
    "not",
    "if",
    "then",
    "than",
    "s",
    "t",
    "give",
    "answer",
    "respond",
    "response",
    "question",
    "state",
    "calculate",
    "define",
    "using",
    "use",
    "only",
    "shown",
    "details",
    "assuming",
    "assume",
    "perspective",
    "investment",
    "analyst",
    "relying",
    "reported",
    "report",
    "fiscal",
    "fy",
    "usd",
    "million",
    "millions",
    "year",
    "years",
    "unadjusted",
    "primarily",
    "directly",
    "outlined",
    "explicitly",
    "quantity",
    "important",
    "materially",
    "ongoing",
])


def _stem(word: str) -> str:
    if len(word) > 5 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 5 and word.endswith("ing"):
        return word[:-3]
    if len(word) > 4 and word.endswith("s") and not word.endswith(("ss", "us")):
        return word[:-1]
    return word


def _tokens(text: str) -> list[str]:
    return [_stem(word) for word in _WORDS.findall(unicodedata.normalize("NFKC", text).casefold()) if word not in _STOP]


# General accounting vocabulary, independent of companies, years and benchmark
# questions. Expansion affects retrieval only; captured page text stays verbatim.
_EXPANSIONS = (
    (
        r"geograph|countr|regions?|territor|operates? in",
        "geographic geography country countries region segment international market operation",
    ),
    (
        r"legal|litigation|lawsuit|battles?",
        "legal litigation proceeding contingency contingencies lawsuit settlement claim",
    ),
    (r"restructur", "restructuring charge cost impairment productivity integration"),
    (
        r"income statements?|statements? of income|p&l|profit and loss",
        "consolidated statement income earnings operation revenue expense",
    ),
    (r"cash flows?|cashflow", "consolidated statement cash flow operating investing financing"),
    (
        r"ebitda|d&a|depreciation|amortization",
        "operating income depreciation amortization consolidated statement cash flow",
    ),
    (r"capex|capital expend|capital spend", "capital spending expenditure property plant equipment addition purchase"),
    (r"balance sheets?|financial position", "consolidated balance sheet asset liability equity"),
    (r"margin|profitab", "revenue sale income profit operating"),
    (r"dividend", "dividend distribution shareholder cash"),
    (r"buyback|repurchas", "repurchase common stock share treasury"),
)


def _query_weights(question: str) -> dict[str, float]:
    weights = dict.fromkeys(_tokens(question), 1.0)
    for pattern, expansion in _EXPANSIONS:
        if re.search(pattern, question, re.IGNORECASE):
            for token in _tokens(expansion):
                weights.setdefault(token, 0.35)
    return weights


def _bm25(counts: Counter[str], weights: dict[str, float], idf: dict[str, float], length_ratio: float) -> float:
    return sum(
        weight * idf.get(term, 0.0) * counts[term] * 2.2 / (counts[term] + 1.2 * (0.3 + 0.7 * length_ratio))
        for term, weight in weights.items()
        if counts[term]
    )


@dataclass(frozen=True)
class _Page:
    citation: MemoryCitation
    source_id: str
    document_id: str
    heading: str
    words: list[str]
    counts: Counter[str]


def _stored_pages(stored: MemoryEntriesPage) -> list[_Page]:
    pages = []
    seen = set()
    for record in stored.entries:
        sources = tuple(source for source in record.entry.sources if source.source_type == "pdf-page")
        if record.state != "active" or len(sources) != 1 or sources[0].source_id in seen:
            continue
        seen.add(sources[0].source_id)
        document_id, _, body = record.entry.text.partition("\n")
        words = _tokens(body)
        heading = "\n".join(line.strip() for line in body[:500].casefold().splitlines())
        pages.append(_Page(record.citation, sources[0].source_id, document_id, heading, words, Counter(words)))
    return pages


def _statement_patterns(question: str) -> tuple[str, ...]:
    # A question may need two complementary statements (e.g. EBITDA needs both
    # income and cash flows). Numeric tables often have few lexical query terms;
    # their actual headings supply an additional, general retrieval signal.
    intents = (
        (
            r"income statements?|statements? of (?:income|operations|earnings)|p&l|profit and loss|ebitda|operating income|margin|revenue|net income|earnings",
            r"(?m)^(?:consolidated\s+)?statements? of (?:income|operations|earnings)\b",
        ),
        (
            r"cash flows?|cashflow|ebitda|capex|capital expend|d&a",
            r"(?m)^(?:consolidated\s+)?statements? of cash flows?\b",
        ),
        (
            r"balance sheets?|financial position|current ratio|quick ratio|working capital|assets?|liabilit|inventor|receivable|debt.*equity|net debt",
            r"(?m)^(?:(?:consolidated\s+)?balance sheets?|statements? of financial position)\b",
        ),
    )
    return tuple(heading for intent, heading in intents if re.search(intent, question, re.IGNORECASE))


def _rank_pages(pages: list[_Page], question: str, weights: dict[str, float]) -> list[tuple[float, _Page]]:
    frequency: Counter[str] = Counter()
    for page in pages:
        frequency.update(page.counts.keys())
    idf = {term: math.log(1.0 + (len(pages) - count + 0.5) / (count + 0.5)) for term, count in frequency.items()}
    average = max(1.0, sum(len(page.words) for page in pages) / len(pages))
    question_tokens = set(_tokens(question))
    question_compact = "".join(_WORDS.findall(question.lower()))
    statement_patterns = _statement_patterns(question)
    statement_bonus = 0.85 * sum(weight * idf.get(term, 0.0) for term, weight in weights.items())
    ranked = []
    for page in pages:
        score = _bm25(page.counts, weights, idf, len(page.words) / average)
        # Passage scoring limits dilution of a short, decisive table or paragraph
        # in a long page. Overlap prevents arbitrary passage boundaries losing it.
        passage = max(
            (
                _bm25(Counter(page.words[start : start + 160]), weights, idf, 1.0)
                for start in range(0, len(page.words), 120)
            ),
            default=0.0,
        )
        score = 0.65 * score + 0.35 * passage
        if any(re.search(pattern, page.heading) for pattern in statement_patterns):
            score += statement_bonus
        company_words = {
            word for word in _tokens(page.document_id) if word.isalpha() and word not in {"k", "q", "annual", "report"}
        }
        if company_words and all(word in question_tokens or word in question_compact for word in company_words):
            score *= 2.5
        if any(year in question for year in re.findall(r"(?:19|20)\d{2}", page.document_id)):
            score *= 1.15
        ranked.append((score, page))
    return ranked


async def search_pdf_pages(memory: _ScopedMemory, question: str, limit: int = 10) -> tuple[MemoryCitation, ...]:
    """Rank persisted pages using public scoped Memory APIs, without a model.

    FTS supplies lexical candidates. A scope-local scan supplements them because
    long natural questions and accounting synonyms can fail FTS admission floors.
    At financial-report scale this bounded-by-scope scan also supplies corpus IDF.
    Only active stored entries with a PDF Source are eligible. No cache survives
    scope changes, source retirement, revisions, or Runtime restart.
    """

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
        raise ValueError("limit must be a non-negative integer")  # noqa: TRY003
    if limit == 0 or not question.strip():
        return ()
    weights = _query_weights(question)
    if not weights:
        return ()
    found = await memory.search(SearchMemoryRequest(query=question, mode="fts", limit=max(64, limit * 4)))
    fts_order = {hit.entry_version_id: index for index, hit in enumerate(found.hits)}
    stored = await memory.list()
    pages = _stored_pages(stored)
    if not pages:
        return ()
    ranked = []
    for score, page in _rank_pages(pages, question, weights):
        rank = fts_order.get(page.citation.entry_version_id)
        if rank is not None:
            score += 0.15 / (1 + rank)
        ranked.append((score, page.source_id, page.citation))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return tuple(citation for score, _identity, citation in ranked[:limit] if score > 0.0)


__all__ = ["PDF_PAGE_SOURCE_DEFINITION", "PDFDocumentInput", "PDFPageInput", "parse_pdf_document", "search_pdf_pages"]
