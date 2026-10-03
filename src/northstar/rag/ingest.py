"""Document ingestion: extract → chunk → embed → store (safe to re-run).

A document is re-processed only when its file content (SHA-256), the embedding model or the
chunker version changed; otherwise it is skipped. Each document is replaced atomically in its
own transaction, so chunks are never duplicated and a failure leaves the previous version intact.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from northstar.database.engine import session_scope
from northstar.database.models import RagDocument
from northstar.rag.catalog import catalog_entry
from northstar.rag.chunking import CHUNKER_VERSION, chunk_document
from northstar.rag.embeddings import EmbeddingProvider
from northstar.rag.extraction import extract_document, list_documents
from northstar.rag.store import VectorStore


@dataclass
class IngestReport:
    ingested: dict[str, int] = field(default_factory=dict)   # document -> chunk count
    skipped: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)


def pipeline_signature(provider: EmbeddingProvider) -> str:
    return f"{provider.signature}|chunker:{CHUNKER_VERSION}"


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ingest_documents(
    session_factory: sessionmaker[Session],
    provider: EmbeddingProvider,
    directory: Path,
    *,
    force: bool = False,
    log: Callable[[str], None] = lambda _msg: None,
) -> IngestReport:
    report = IngestReport()
    signature = pipeline_signature(provider)
    paths = list_documents(directory)

    for path in paths:
        sha = file_sha256(path)
        with session_scope(session_factory) as session:
            existing = VectorStore(session).get_document(path.name)
            unchanged = existing is not None and existing.sha256 == sha and existing.embedding_model == signature
        if unchanged and not force:
            report.skipped.append(path.name)
            log(f"  unchanged   {path.name}")
            continue

        extracted = extract_document(path)
        chunks = chunk_document(extracted)
        embeddings = provider.embed_documents([c.content for c in chunks])  # outside the DB transaction
        entry = catalog_entry(path.name)
        with session_scope(session_factory) as session:
            VectorStore(session).replace_document(
                RagDocument(
                    document=path.name, title=entry.title, doc_code=extracted.metadata.get("doc_code"),
                    version=extracted.metadata.get("version"), file_type=extracted.file_type, domain=entry.domain,
                    authority=entry.authority, authority_rank=entry.authority_rank, sha256=sha,
                    embedding_model=signature, chunk_count=len(chunks),
                ),
                chunks,
                embeddings,
            )
        report.ingested[path.name] = len(chunks)
        log(f"  ingested    {path.name}: {len(chunks)} chunks")

    with session_scope(session_factory) as session:
        report.removed = VectorStore(session).delete_documents_except(p.name for p in paths)
    for name in report.removed:
        log(f"  removed     {name} (file no longer present)")
    return report
