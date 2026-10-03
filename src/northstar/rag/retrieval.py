"""Hybrid, precedence-aware retrieval over the policy chunks.

1. Candidates: semantic search (pgvector cosine), keyword search (Georgian word-prefix full-text)
   and exact article references in the question ("მუხლი 4.4", "4.4").
2. Ranking: reciprocal-rank fusion of those signals, plus a boost for the document that is
   authoritative for the question's topic (leave → Leave Policy, remote work → Remote Work
   Policy, ...) and a small bonus by document authority (policy > handbook > FAQ).
3. Precedence: the best passage of the topic's authoritative policy is always included, and a
   Handbook/FAQ passage on the same topic as an included authoritative passage is marked as
   superseded (the documents themselves say the FAQ/Handbook may be outdated).
4. Sufficiency: if nothing is similar enough and no exact article matched, the result says so;
   the caller must then answer that the documents do not contain the information.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session, sessionmaker

from northstar.database.engine import session_scope
from northstar.database.models import DocumentChunk
from northstar.rag.catalog import CATALOG, Domain
from northstar.rag.citations import format_citation
from northstar.rag.embeddings import EmbeddingProvider
from northstar.rag.ingest import pipeline_signature
from northstar.rag.store import VectorStore
from northstar.rag.topics import detect_topic

RRF_K = 60
CANDIDATES = 40
WEIGHT_VECTOR = 1.0
WEIGHT_KEYWORD = 0.6
WEIGHT_SECTION = 1.5
DOMAIN_BOOST = 0.012
AUTHORITY_BONUS = {1: 0.004, 2: 0.002, 3: 0.0}
PREAMBLE_FACTOR = 0.6

# Keyword prefixes occurring in more than this share of all chunks carry no signal ("დღე", "შვებულება").
MAX_PREFIX_DOC_SHARE = 0.15

# Minimum cosine similarity of the best passage for the question to count as covered.
SUFFICIENCY_THRESHOLD = {"gemini": 0.66, "local-hash": 0.22}

_WORD_RE = re.compile(r"[\w-]+", re.UNICODE)
_SECTION_RE = re.compile(r"(?<![\d.])(\d{1,2}\.\d{1,2})(?![\d.])")
_STOP_WORDS = {
    "რა", "როგორ", "რამდენი", "როდის", "სად", "ვინ", "რომელი", "თუ", "და", "ან", "არის", "შემიძლია", "უნდა",
    "მაქვს", "ჩემი", "ჩემს", "მინდა", "კომპანიის", "პოლიტიკა", "პოლიტიკის", "მუხლი", "მუხლის", "the", "what",
    "how", "can", "does",
}


class RagNotReady(RuntimeError):
    """Documents are not ingested (or were ingested with a different embedding model)."""


@dataclass
class Passage:
    chunk_id: int
    document: str
    title: str
    version: str | None
    domain: str
    authority: str
    authority_rank: int
    section: str | None
    section_title: str | None
    page: int | None
    content: str
    similarity: float
    score: float
    citation: str
    superseded_by: str | None = None

    @property
    def is_authoritative(self) -> bool:
        return self.authority_rank == 1


@dataclass
class RetrievalResult:
    query: str
    topic: Domain | None
    passages: list[Passage] = field(default_factory=list)
    best_similarity: float = 0.0
    section_hit: bool = False
    sufficient: bool = False


def keyword_prefixes(query: str) -> list[str]:
    """Word stems for prefix search: Georgian suffixes are dropped by keeping the first 6 letters."""
    prefixes = []
    for word in _WORD_RE.findall(query.lower()):
        word = word.strip("-")
        if len(word) < 4 or word in _STOP_WORDS or word.isdigit():
            continue
        prefixes.append(word[:6] if len(word) > 6 else word)
    return list(dict.fromkeys(prefixes))[:12]


def referenced_sections(query: str) -> list[str]:
    return list(dict.fromkeys(_SECTION_RE.findall(query)))


def _passage(chunk: DocumentChunk, similarity: float, score: float) -> Passage:
    m = chunk.metadata_ or {}
    entry = CATALOG.get(chunk.document)
    title = m.get("title") or (entry.title if entry else chunk.document)
    return Passage(
        chunk_id=chunk.chunk_id, document=chunk.document, title=title, version=m.get("version"),
        domain=m.get("domain", "general"), authority=m.get("authority", "reference_faq"),
        authority_rank=int(m.get("authority_rank", 3)), section=chunk.section, section_title=chunk.section_title,
        page=chunk.page, content=chunk.content, similarity=similarity, score=score,
        citation=format_citation(title=title, version=m.get("version"), section=chunk.section,
                                 section_title=chunk.section_title, page=chunk.page),
    )


class Retriever:
    def __init__(self, session_factory: sessionmaker[Session], provider: EmbeddingProvider):
        self.session_factory = session_factory
        self.provider = provider
        provider_family = provider.signature.split(":", 1)[0]
        self.threshold = SUFFICIENCY_THRESHOLD.get(provider_family, 0.5)

    def check_ready(self, session: Session) -> None:
        docs = VectorStore(session).list_documents()
        if not docs:
            raise RagNotReady("documents are not ingested; run: python -m scripts.ingest_documents")
        expected = pipeline_signature(self.provider)
        if any(d.embedding_model != expected for d in docs):
            raise RagNotReady("documents were ingested with a different embedding model; re-run ingestion")

    def retrieve(self, query: str, k: int = 6, topic: Domain | None = None,
                 alternative_queries: list[str] | None = None) -> RetrievalResult:
        """`alternative_queries`: optional rephrasings (e.g. by the LLM, in policy terminology); the
        candidate lists of all variants are fused, so colloquial wording still finds formal articles."""
        query = query.strip()
        variants = list(dict.fromkeys([query] + [q.strip() for q in (alternative_queries or []) if q.strip()]))[:4]
        topic = topic or detect_topic(" ".join(variants))
        vectors = [self.provider.embed_query(v) for v in variants]  # outside the DB transaction

        with session_scope(self.session_factory) as session:
            self.check_ready(session)
            store = VectorStore(session)
            vector_lists = [store.vector_search(v, CANDIDATES) for v in vectors]
            prefixes = list(dict.fromkeys(p for v in variants for p in keyword_prefixes(v)))
            total, frequency = store.prefix_frequencies(prefixes)
            informative = [p for p in prefixes if 0 < frequency[p] <= max(3, MAX_PREFIX_DOC_SHARE * total)]
            keyword_hits = store.keyword_search(informative, CANDIDATES)
            sections = referenced_sections(query)
            topic_docs = [e.document for e in CATALOG.values() if topic and e.domain == topic]
            section_hits = store.section_search(sections, topic_docs or None) if sections else []

            chunks: dict[int, DocumentChunk] = {}
            similarity: dict[int, float] = {}
            fused: dict[int, float] = {}

            def add(chunk: DocumentChunk, rank: int, weight: float) -> None:
                chunks[chunk.chunk_id] = chunk
                fused[chunk.chunk_id] = fused.get(chunk.chunk_id, 0.0) + weight / (RRF_K + rank)

            for vector_hits in vector_lists:
                for rank, (chunk, sim) in enumerate(vector_hits, start=1):
                    similarity[chunk.chunk_id] = max(sim, similarity.get(chunk.chunk_id, 0.0))
                    add(chunk, rank, WEIGHT_VECTOR / len(vector_lists))
            for rank, (chunk, _r) in enumerate(keyword_hits, start=1):
                add(chunk, rank, WEIGHT_KEYWORD)
            for chunk in section_hits:
                add(chunk, 1, WEIGHT_SECTION)

            scored: list[Passage] = []
            for cid, chunk in chunks.items():
                m = chunk.metadata_ or {}
                score = fused[cid]
                if topic and m.get("domain") == topic:
                    score += DOMAIN_BOOST
                score += AUTHORITY_BONUS.get(int(m.get("authority_rank", 3)), 0.0)
                if chunk.section is None:
                    score *= PREAMBLE_FACTOR
                scored.append(_passage(chunk, similarity.get(cid, 0.0), score))

        scored.sort(key=lambda p: p.score, reverse=True)
        selected = scored[:k]
        selected = self._ensure_authoritative(selected, scored, topic, k)
        self._mark_superseded(selected)
        best = max((p.similarity for p in scored), default=0.0)
        return RetrievalResult(
            query=query, topic=topic, passages=selected, best_similarity=best, section_hit=bool(section_hits),
            sufficient=best >= self.threshold or bool(section_hits),
        )

    def is_ready(self) -> bool:
        with session_scope(self.session_factory) as session:
            try:
                self.check_ready(session)
            except RagNotReady:
                return False
        return True

    @staticmethod
    def _ensure_authoritative(selected: list[Passage], ranked: list[Passage], topic: Domain | None,
                              k: int) -> list[Passage]:
        """Never answer a topic only from the Handbook/FAQ when its authoritative policy has a candidate."""
        if topic is None or any(p.domain == topic and p.is_authoritative for p in selected):
            return selected
        best = next((p for p in ranked if p.domain == topic and p.is_authoritative), None)
        if best is None:
            return selected
        return [best] + selected[: k - 1]

    @staticmethod
    def _mark_superseded(passages: list[Passage]) -> None:
        authoritative = {p.domain: p for p in passages if p.is_authoritative}
        for p in passages:
            if p.is_authoritative:
                continue
            passage_topic = detect_topic(p.content)
            winner = authoritative.get(passage_topic) if passage_topic else None
            if winner is not None:
                p.superseded_by = winner.citation
        # Present authoritative passages first; relative order otherwise kept.
        passages.sort(key=lambda p: (p.superseded_by is not None, p.authority_rank, -p.score))


AUTHORITY_LABEL = {
    "authoritative_policy": "მოქმედი სპეციალური პოლიტიკა (უპირატესი წყარო)",
    "general_handbook": "ზოგადი სახელმძღვანელო (სპეციალურ პოლიტიკასთან წინააღმდეგობისას მოქმედებს პოლიტიკა)",
    "reference_faq": "საცნობარო FAQ (შეიძლება მოძველებული იყოს)",
}


def build_context(result: RetrievalResult) -> str:
    """Numbered passages for the LLM; each carries its exact citation and precedence status."""
    blocks = []
    for i, p in enumerate(result.passages, start=1):
        status = AUTHORITY_LABEL.get(p.authority, p.authority)
        if p.superseded_by:
            status += f"; ამ საკითხზე უპირატესია: {p.superseded_by}"
        blocks.append(f"[{i}] წყარო: {p.citation}\nსტატუსი: {status}\n{p.content}")
    return "\n\n".join(blocks)
