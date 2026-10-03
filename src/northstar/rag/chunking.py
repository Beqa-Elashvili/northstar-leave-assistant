"""Structure-aware chunking: one chunk per article / sub-article (never arbitrary character cuts).

- Content under a sub-article heading (e.g. "4.4") forms one chunk, including its paragraphs,
  list items and tables.
- Text directly under an article heading (e.g. "4.") before its first sub-article is its own chunk.
- The preamble (title, metadata table, disclaimer) becomes a "document information" chunk.
- Only an unusually long section is split, and then only on block boundaries; every part
  repeats the section heading.

Each chunk's text starts with a short context line (document, article number and title) so
that both the embedding and the keyword index see where the text comes from.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from northstar.rag.catalog import CatalogEntry, catalog_entry
from northstar.rag.extraction import Block, ExtractedDocument

CHUNKER_VERSION = "1"
MAX_CHUNK_CHARS = 2200
PREAMBLE_TITLE = "დოკუმენტის შესახებ"


@dataclass
class Chunk:
    document: str
    chunk_index: int
    section: str | None
    section_title: str | None
    page: int | None
    content: str
    metadata: dict = field(default_factory=dict)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


@dataclass
class _Section:
    section: str | None
    title: str | None
    parent_section: str | None
    parent_title: str | None
    page: int | None
    blocks: list[Block] = field(default_factory=list)


def _group_sections(doc: ExtractedDocument) -> list[_Section]:
    sections: list[_Section] = []
    current = _Section(None, PREAMBLE_TITLE, None, None, 1 if doc.file_type == "pdf" else None)
    sections.append(current)
    parent: tuple[str | None, str | None] = (None, None)
    for block in doc.blocks:
        if block.kind == "title":
            continue
        if block.kind == "heading":
            title = block.heading_title or block.text
            if block.level <= 1:
                parent = (block.section, title)
                current = _Section(block.section, title, None, None, block.page)
            else:
                current = _Section(block.section, title, parent[0], parent[1], block.page)
            sections.append(current)
            continue
        current.blocks.append(block)
    # Drop article headings with no own text (their sub-articles carry the content).
    return [s for s in sections if s.blocks]


def _context_line(entry: CatalogEntry, doc: ExtractedDocument, s: _Section) -> str:
    version = doc.metadata.get("version")
    label = f"{entry.title} (ვერსია {version})" if version else entry.title
    if s.section is None:
        return f"{label} — {s.title}"
    heading = f"მუხლი {s.section}. {s.title}"
    if s.parent_section and s.parent_title:
        heading = f"{s.parent_section}. {s.parent_title} › {heading}"
    return f"{label} — {heading}"


def _split_blocks(blocks: list[Block]) -> list[list[Block]]:
    parts: list[list[Block]] = [[]]
    size = 0
    for block in blocks:
        if parts[-1] and size + len(block.text) > MAX_CHUNK_CHARS:
            parts.append([])
            size = 0
        parts[-1].append(block)
        size += len(block.text)
    return parts


def chunk_document(doc: ExtractedDocument) -> list[Chunk]:
    entry = catalog_entry(doc.document)
    chunks: list[Chunk] = []
    for s in _group_sections(doc):
        context = _context_line(entry, doc, s)
        for part_no, part in enumerate(_split_blocks(s.blocks)):
            body = "\n".join(("• " + b.text) if b.kind == "list_item" else b.text for b in part)
            pages = sorted({b.page for b in part if b.page is not None})
            page = pages[0] if pages else s.page
            metadata = {
                "document": doc.document,
                "title": entry.title,
                "doc_code": doc.metadata.get("doc_code"),
                "version": doc.metadata.get("version"),
                "effective": doc.metadata.get("effective") or doc.metadata.get("last_updated"),
                "file_type": doc.file_type,
                "domain": entry.domain,
                "authority": entry.authority,
                "authority_rank": entry.authority_rank,
                "section": s.section,
                "section_title": s.title,
                "parent_section": s.parent_section,
                "parent_title": s.parent_title,
                "page": page,
                "page_end": pages[-1] if pages else page,
                "part": part_no,
            }
            chunks.append(Chunk(doc.document, len(chunks), s.section, s.title, page,
                                f"{context}\n{body}", metadata))
    return chunks
