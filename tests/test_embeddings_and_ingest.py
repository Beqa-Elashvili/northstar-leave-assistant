"""Embedding providers (mocked Gemini, local hashing) and idempotent ingestion into pgvector."""

import math
import shutil
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session, sessionmaker

from northstar.config import DOCUMENTS_DIR
from northstar.database.models import DocumentChunk, RagDocument
from northstar.rag.embeddings import (
    EmbeddingError, GeminiEmbeddingProvider, LocalHashingEmbeddingProvider, RateLimiter, _retry_delay,
)
from northstar.rag.ingest import ingest_documents


def cos(a, b):
    return sum(x * y for x, y in zip(a, b))


# --- local provider -----------------------------------------------------------------------------

def test_local_provider_deterministic_and_normalised():
    p = LocalHashingEmbeddingProvider(768)
    v1, v2 = p.embed_documents(["შვებულების წინასწარი შეტყობინება"] * 2)
    assert v1 == v2 and len(v1) == 768
    assert math.isclose(math.sqrt(sum(x * x for x in v1)), 1.0, rel_tol=1e-9)


def test_local_provider_lexical_similarity():
    p = LocalHashingEmbeddingProvider(768)
    q = p.embed_query("წინასწარი შეტყობინება შვებულებაზე")
    near, far = p.embed_documents(["მოთხოვნის წარდგენა და წინასწარი შეტყობინება", "სასტუმროს ლიმიტი ლონდონში"])
    assert cos(q, near) > cos(q, far)


# --- Gemini provider (SDK mocked: no network, no key) ---------------------------------------------

class FakeModels:
    def __init__(self, fail_times=0, code=429):
        self.calls, self.fail_times, self.code = [], fail_times, code

    def embed_content(self, model, contents, config):
        from google.genai import errors

        self.calls.append((model, list(contents), config.task_type, config.output_dimensionality))
        if self.fail_times:
            self.fail_times -= 1
            raise errors.APIError(self.code, {"error": {"message": "quota"}})
        return SimpleNamespace(embeddings=[SimpleNamespace(values=[3.0, 4.0] + [0.0] * 766) for _ in contents])


def gemini(fake):
    p = GeminiEmbeddingProvider.__new__(GeminiEmbeddingProvider)
    p._client, p._model, p._dim = SimpleNamespace(models=fake), "gemini-embedding-001", 768
    p._limiter, p._log = RateLimiter(10_000), (lambda _m: None)
    return p


def test_gemini_batches_normalises_and_sets_task_type(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    fake = FakeModels()
    out = gemini(fake).embed_documents([f"t{i}" for i in range(120)])
    assert len(out) == 120 and [len(c[1]) for c in fake.calls] == [50, 50, 20]
    assert out[0][:2] == [0.6, 0.8]                                   # L2-normalised
    assert fake.calls[0][2:] == ("RETRIEVAL_DOCUMENT", 768)
    gemini(fake).embed_query("q")
    assert fake.calls[-1][2] == "RETRIEVAL_QUERY"


def test_gemini_retries_rate_limit(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    fake = FakeModels(fail_times=2)
    assert len(gemini(fake).embed_documents(["x"])) == 1 and len(fake.calls) == 3


def test_gemini_non_retryable_error_is_safe(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _s: None)
    with pytest.raises(EmbeddingError) as exc:
        gemini(FakeModels(fail_times=1, code=400)).embed_documents(["x"])
    assert "400" in str(exc.value) and "key" not in str(exc.value).lower()


def test_rate_limiter_waits_for_the_window():
    now = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    limiter = RateLimiter(90, clock=lambda: now[0], sleep=sleep)
    assert limiter.acquire(50) == 0 and limiter.acquire(40) == 0     # 90 within the first minute
    now[0] = 10.0
    waited = limiter.acquire(50)                                      # must wait until t >= 60
    assert waited == pytest.approx(50.5) and now[0] >= 60


def test_retry_delay_is_read_from_error_details():
    exc = SimpleNamespace(details={"error": {"details": [{"@type": "x"}, {"retryDelay": "17s"}]}})
    assert _retry_delay(exc) == 17.0
    assert _retry_delay(SimpleNamespace(details=None)) is None


def test_signature():
    assert gemini(FakeModels()).signature == "gemini:gemini-embedding-001:768"
    assert LocalHashingEmbeddingProvider().signature == "local-hash:768"


# --- ingestion -------------------------------------------------------------------------------------

@pytest.fixture()
def rag_factory(engine):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE rag_documents, document_chunks CASCADE"))
    return sessionmaker(bind=engine, expire_on_commit=False)


def counts(factory):
    with Session(factory.kw["bind"]) as s:
        return (s.scalar(select(func.count()).select_from(RagDocument)),
                s.scalar(select(func.count()).select_from(DocumentChunk)))


@pytest.mark.db
def test_ingest_all_documents(rag_factory):
    report = ingest_documents(rag_factory, LocalHashingEmbeddingProvider(), DOCUMENTS_DIR)
    assert len(report.ingested) == 7 and not report.skipped
    docs, chunks = counts(rag_factory)
    assert docs == 7 and chunks == sum(report.ingested.values()) > 200
    with Session(rag_factory.kw["bind"]) as s:
        doc = s.get(RagDocument, "Leave_and_Absence_Policy_v4.0.docx")
        assert (doc.doc_code, doc.version, doc.domain, doc.authority_rank) == ("HR-POL-02", "4.0", "leave", 1)
        assert doc.embedding_model == "local-hash:768|chunker:1"
        row = s.scalars(select(DocumentChunk).where(DocumentChunk.document == "Remote_and_Hybrid_Work_Policy_v2.0.pdf",
                                                     DocumentChunk.section == "4.1")).one()
        assert row.page == 2 and row.metadata_["domain"] == "remote_work" and len(row.embedding) == 768


@pytest.mark.db
def test_rerun_skips_unchanged_and_creates_no_duplicates(rag_factory):
    provider = LocalHashingEmbeddingProvider()
    ingest_documents(rag_factory, provider, DOCUMENTS_DIR)
    before = counts(rag_factory)
    report = ingest_documents(rag_factory, provider, DOCUMENTS_DIR)
    assert len(report.skipped) == 7 and not report.ingested
    assert counts(rag_factory) == before


@pytest.mark.db
def test_force_reingest_replaces_without_duplicates(rag_factory):
    provider = LocalHashingEmbeddingProvider()
    ingest_documents(rag_factory, provider, DOCUMENTS_DIR)
    before = counts(rag_factory)
    report = ingest_documents(rag_factory, provider, DOCUMENTS_DIR, force=True)
    assert len(report.ingested) == 7 and counts(rag_factory) == before


@pytest.mark.db
def test_changed_file_and_removed_file(rag_factory, tmp_path):
    work = tmp_path / "documents"
    shutil.copytree(DOCUMENTS_DIR, work)
    provider = LocalHashingEmbeddingProvider()
    ingest_documents(rag_factory, provider, work)
    (work / "Employee_FAQ_2025.docx").unlink()
    shutil.copy(DOCUMENTS_DIR / "Employee_Handbook_v3.1.docx", work / "Employee_Handbook_v3.1.docx")
    report = ingest_documents(rag_factory, provider, work)
    assert report.removed == ["Employee_FAQ_2025.docx"]
    assert counts(rag_factory)[0] == 6


@pytest.mark.db
def test_different_embedding_model_triggers_reingest(rag_factory):
    ingest_documents(rag_factory, LocalHashingEmbeddingProvider(), DOCUMENTS_DIR)

    class OtherModel(LocalHashingEmbeddingProvider):
        @property
        def signature(self):
            return "other:768"

    report = ingest_documents(rag_factory, OtherModel(), DOCUMENTS_DIR)
    assert len(report.ingested) == 7


@pytest.mark.db
def test_failed_embedding_keeps_previous_version(rag_factory):
    ingest_documents(rag_factory, LocalHashingEmbeddingProvider(), DOCUMENTS_DIR)
    before = counts(rag_factory)

    class Broken(LocalHashingEmbeddingProvider):
        @property
        def signature(self):
            return "broken:768"

        def embed_documents(self, texts):
            raise EmbeddingError("down")

    with pytest.raises(EmbeddingError):
        ingest_documents(rag_factory, Broken(), DOCUMENTS_DIR)
    assert counts(rag_factory) == before
