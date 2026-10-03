"""pgvector-backed chunk store (Supabase PostgreSQL). All SQL for the RAG tables lives here."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone

from sqlalchemy import delete, select
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
        from sqlalchemy import func

        return int(self.session.scalar(select(func.count()).select_from(DocumentChunk)) or 0)
