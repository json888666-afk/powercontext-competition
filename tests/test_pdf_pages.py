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

"""PDF contract regressions using constructed bytes and real persisted Memory."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from powercontext.builtin.artifacts.memory import MemoryEntryInput
from powercontext.builtin.persistence.sqlite import SQLiteConfig
from powercontext.builtin.runtime import (
    BuiltinConfig,
    GetMemoryEntryRequest,
    SubmitSourceObservation,
    open_builtin_runtime,
)
from powercontext.builtin.scope import ScopeDraft
from powercontext.sources import (
    TEXT_EVIDENCE_PROJECTION_KEY,
    SourceDefinitionRegistry,
    manifest_for_definition,
    project_source_for_transport,
)
from powercontext.sources.pdf_pages import (
    PDF_PAGE_SOURCE_DEFINITION,
    PDFDocumentInput,
    PDFPageInput,
    parse_pdf_document,
    search_pdf_pages,
)


def make_pdf(texts: tuple[str, ...], *, password: str | None = None) -> bytes:
    writer = PdfWriter()
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    for text in texts:
        page = writer.add_blank_page(width=600, height=800)
        if text:
            page[NameObject("/Resources")] = DictionaryObject({
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})
            })
            stream = DecodedStreamObject()
            escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            stream.set_data(f"BT /F1 12 Tf 20 700 Td ({escaped}) Tj ET".encode("ascii"))
            page[NameObject("/Contents")] = writer._add_object(stream)
    if password is not None:
        writer.encrypt(password, owner_password="owner")  # noqa: S106
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_physical_pages_preserve_text_numbers_blanks_and_digest() -> None:
    content = make_pdf(("Revenue 1,234.50 2025", "", "Cash 8,765.00 2024"))
    pages = parse_pdf_document(PDFDocumentInput(document_id="ACME_2025_10K", content=content))
    assert [page.page_number for page in pages] == [1, 2, 3]
    assert [page.text for page in pages] == ["Revenue 1,234.50 2025", "", "Cash 8,765.00 2024"]
    assert {page.document_sha256 for page in pages} == {hashlib.sha256(content).hexdigest()}


@pytest.mark.parametrize("document_id", ["", " \t\n"])
def test_blank_document_ids_are_rejected(document_id: str) -> None:
    with pytest.raises(ValueError):
        PDFDocumentInput(document_id=document_id, content=make_pdf(("Text",)))
    with pytest.raises(ValueError):
        PDFPageInput(document_id=document_id, document_sha256="a" * 64, page_number=1, text="Text")


def test_password_protected_and_late_truncated_documents_fail() -> None:
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="secret", content=make_pdf(("Page",), password="secret")))  # noqa: S106
    content = make_pdf(("First readable page", "Second page"))
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="truncated", content=content[: content.rindex(b"xref")]))
    with pytest.raises(ValueError):
        parse_pdf_document(PDFDocumentInput(document_id="truncated-eof", content=content[:-2]))
    with pytest.raises(ValueError):
        parse_pdf_document(
            PDFDocumentInput(document_id="incomplete-tree", content=content.replace(b"/Count 2", b"/Count 3"))
        )


@pytest.mark.asyncio
async def test_source_names_are_bounded_and_revision_sensitive() -> None:
    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    page = PDFPageInput(document_id="财报/:\n" * 300, document_sha256="a" * 64, page_number=1, text="Revenue 12")
    source = await registry.resolve(page)
    assert len(source.name) <= 256
    assert source == await registry.resolve(page)
    assert await registry.read(source) == page
    assert source.name != (await registry.resolve(page.model_copy(update={"document_sha256": "b" * 64}))).name
    assert source.name != (await registry.resolve(page.model_copy(update={"page_number": 2}))).name
    assert source.name != (await registry.resolve(page.model_copy(update={"document_id": page.document_id + "x"}))).name


@pytest.mark.asyncio
async def test_retrieval_ranks_synonyms_and_respects_scope_and_limits(tmp_path: Path) -> None:
    class PagePipeline:
        async def extract(self, request, /):
            return tuple(
                MemoryEntryInput(
                    kind="source-evidence",
                    text=source.projection(TEXT_EVIDENCE_PROJECTION_KEY)["content"],
                    sources=(source,),
                )
                for source in request.sources
            )

    registry = SourceDefinitionRegistry((PDF_PAGE_SOURCE_DEFINITION,))
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'pdf.db'}"))
    async with open_builtin_runtime(config, candidate_pipeline=PagePipeline()) as runtime:
        await runtime.ingestion.register(manifest_for_definition(PDF_PAGE_SOURCE_DEFINITION))
        assert runtime.scopes is not None
        main = await runtime.scopes.create(ScopeDraft(title="Main", summary="Reports", idempotency_key="main"))
        other = await runtime.scopes.create(ScopeDraft(title="Other", summary="Private", idempotency_key="other"))
        for scope_id, document_id, texts in [
            (
                main.scope_id,
                "ACME_2025_10K",
                (
                    "Consolidated Statements of Cash Flows. Depreciation and amortization 123. Additions to property plant and equipment 45.",
                    "Legal proceedings. Litigation and contingent liabilities 67.",
                    "Business segments. We operate in Europe and Asia.",
                    "Unrelated executive compensation and pension awards.",
                    "Consolidated Statements of Income. Net revenue 1000. Operating profit 150. Net earnings 85.",
                ),
            ),
            (other.scope_id, "PRIVATE_2025_10K", ("Private litigation settlement cash flow EBITDA depreciation 999.",)),
        ]:
            for page in parse_pdf_document(PDFDocumentInput(document_id=document_id, content=make_pdf(texts))):
                source = project_source_for_transport(registry, await registry.resolve(page))
                await runtime.ingestion.submit(SubmitSourceObservation(scope_id=scope_id, observation=source))
            await runtime.memory.for_scope(scope_id).flush()
    # Search after reopening: every returned citation must resolve to the original
    # stored entry, and another scope's very similar content must stay excluded.
    async with open_builtin_runtime(config) as runtime:
        memory = runtime.memory.for_scope(main.scope_id)
        for question, expected in [
            ("What is ACME FY2025 EBITDA less capex?", "Consolidated Statements of Cash Flows"),
            ("What legal battles does ACME face?", "Legal proceedings"),
            ("What geographies does ACME operate in?", "Business segments"),
        ]:
            citations = await search_pdf_pages(memory, question, limit=1)
            assert len(citations) == 1
            entry = (await memory.get(GetMemoryEntryRequest(citation=citations[0]))).entry
            assert expected in entry.text
        citations = await search_pdf_pages(memory, "ACME cash flow litigation geography", limit=20)
        assert 1 <= len(citations) <= 5
        assert len({citation.entry_id for citation in citations}) == len(citations)
        for citation in citations:
            assert "PRIVATE" not in (await memory.get(GetMemoryEntryRequest(citation=citation))).entry.text
        statements = await search_pdf_pages(
            memory, "What is ACME EBITDA margin from income and cash flow statements?", limit=2
        )
        bodies = [(await memory.get(GetMemoryEntryRequest(citation=citation))).entry.text for citation in statements]
        assert any("Statements of Cash Flows" in body for body in bodies)
        assert any("Statements of Income" in body for body in bodies)
        assert await search_pdf_pages(memory, "cash", limit=0) == ()
        assert await search_pdf_pages(memory, " \n ", limit=20) == ()
        with pytest.raises(ValueError):
            await search_pdf_pages(memory, "cash", limit=-1)
