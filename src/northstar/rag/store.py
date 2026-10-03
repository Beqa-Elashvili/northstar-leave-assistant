"""pgvector-backed chunk store (Supabase PostgreSQL). All SQL for the RAG tables lives here."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from northstar.database.models import EMBEDDING_DIM, DocumentChunk, RagDocument
from northstar.rag.chunking import Chunk


class VectorStore:
    def __init__(self, session: Session):
        self.session = session

    def get_document(self, name: str) -> RagDocument | None:
        return self.session.get(RagDocument, name)

    def list_documents(self) -> list[RagDocument]:
        return list(self.session.scalars(select(RagDocument).order_by(RagDocument.document)))

    def replace_document(self, document: RagDocument, chunks: list[Chunk], embeddings: list[list[float]]) -> None:
        """Delete any previous version of the document and its chunks, then insert the new ones."""
        if len(chunks) != len(embeddings):
            raise ValueError("one embedding per chunk is required")
        for vector in embeddings:
            if len(vector) != EMBEDDING_DIM:
                raise ValueError(f"embedding dimension {len(vector)} does not match vector({EMBEDDING_DIM})")
        self.session.execute(delete(RagDocument).where(RagDocument.document == document.document))
        self.session.flush()
        document.ingested_at = datetime.now(timezone.utc)  # operational timestamp, not a business date
        self.session.add(document)
        self.session.flush()
        self.session.add_all(
            DocumentChunk(
                document=c.document, chunk_index=c.chunk_index, section=c.section, section_title=c.section_title,
                page=c.page, content=c.content, content_hash=c.content_hash, embedding=vector, metadata_=c.metadata,
            )
            for c, vector in zip(chunks, embeddings)
        )
        self.session.flush()

    def delete_documents_except(self, keep: Iterable[str]) -> list[str]:
        keep = set(keep)
        stale = [d.document for d in self.list_documents() if d.document not in keep]
        if stale:
            self.session.execute(delete(RagDocument).where(RagDocument.document.in_(stale)))
        return stale

    def chunk_count(self) -> int:
        return int(self.session.scalar(select(func.count()).select_from(DocumentChunk)) or 0)

    # --- search (all parameterised; the query text never becomes SQL) --------------------------

    def vector_search(self, query_vector: list[float], limit: int) -> list[tuple[DocumentChunk, float]]:
        """Nearest chunks by cosine distance (HNSW index); returns (chunk, cosine similarity)."""
        distance = DocumentChunk.embedding.cosine_distance(query_vector)
        rows = self.session.execute(
            select(DocumentChunk, (1 - distance).label("similarity")).order_by(distance).limit(limit)
        ).all()
        return [(chunk, float(sim)) for chunk, sim in rows]

    def keyword_search(self, prefixes: list[str], limit: int) -> list[tuple[DocumentChunk, float]]:
        """Full-text search on the 'simple' tsvector with OR-ed prefix terms (Georgian is inflected)."""
        if not prefixes:
            return []
        query = func.to_tsquery("simple", " | ".join(f"{p}:*" for p in prefixes))
        rank = func.ts_rank_cd(DocumentChunk.search_tsv, query)
        rows = self.session.execute(
            select(DocumentChunk, rank.label("rank"))
            .where(DocumentChunk.search_tsv.op("@@")(query))
            .order_by(rank.desc())
            .limit(limit)
        ).all()
        return [(chunk, float(r)) for chunk, r in rows]

    def prefix_frequencies(self, prefixes: list[str]) -> tuple[int, dict[str, int]]:
        """(total chunks, number of chunks containing each prefix) in a single query."""
        if not prefixes:
            return self.chunk_count(), {}
        columns = [func.count().label("total")] + [
            func.count().filter(DocumentChunk.search_tsv.op("@@")(func.to_tsquery("simple", f"{p}:*"))).label(f"p{i}")
            for i, p in enumerate(prefixes)
        ]
        row = self.session.execute(select(*columns).select_from(DocumentChunk)).one()
        return int(row[0]), {p: int(row[i + 1]) for i, p in enumerate(prefixes)}

    def section_search(self, sections: list[str], documents: list[str] | None = None) -> list[DocumentChunk]:
        stmt = select(DocumentChunk).where(DocumentChunk.section.in_(sections))
        if documents:
            stmt = stmt.where(DocumentChunk.document.in_(documents))
        return list(self.session.scalars(stmt.order_by(DocumentChunk.document, DocumentChunk.chunk_index)))
