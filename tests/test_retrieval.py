"""Hybrid, precedence-aware retrieval and citations (offline embedding provider, no API key)."""

import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from northstar.config import DOCUMENTS_DIR
from northstar.rag.citations import format_citation
from northstar.rag.embeddings import LocalHashingEmbeddingProvider
from northstar.rag.ingest import ingest_documents
from northstar.rag.retrieval import (
    Passage, RagNotReady, RetrievalResult, Retriever, build_context, keyword_prefixes, referenced_sections,
)
from northstar.rag.topics import detect_topic

LEAVE = "Leave_and_Absence_Policy_v4.0.docx"
REMOTE = "Remote_and_Hybrid_Work_Policy_v2.0.pdf"
FAQ = "Employee_FAQ_2025.docx"


# --- pure helpers ------------------------------------------------------------------------------

@pytest.mark.parametrize("question, topic", [
    ("რამდენი დღით ადრე უნდა მოვითხოვო შვებულება?", "leave"),
    ("ბებიაჩემი გარდაიცვალა", "leave"),
    ("რამდენი დღე შემიძლია ვიმუშაო სახლიდან?", "remote_work"),
    ("პაროლი დამავიწყდა", "security"),
    ("ACCA-ს გამოცდის საფასურს ფარავს კომპანია?", "learning"),
    ("სასტუმროს ლიმიტი მივლინებისას", "travel"),
    ("როგორ ხარ?", None),
])
def test_detect_topic(question, topic):
    assert detect_topic(question) == topic


def test_keyword_prefixes_drop_short_and_stop_words():
    assert keyword_prefixes("რამდენი დღე შემიძლია ვიმუშაო სახლიდან კვირაში?") == ["ვიმუშა", "სახლიდ", "კვირაშ"]


def test_referenced_sections():
    assert referenced_sections("რას ამბობს მუხლი 4.6 და 12.3?") == ["4.6", "12.3"]
    assert referenced_sections("2026 წელს 15 დღე") == []


def test_citation_formats():
    assert format_citation(title="შვებულებისა და გაცდენის პოლიტიკა", version="4.0", section="4.4",
                           section_title="მოთხოვნის წარდგენა და წინასწარი შეტყობინება", page=None) == \
        "შვებულებისა და გაცდენის პოლიტიკა v4.0, მუხლი 4.4 („მოთხოვნის წარდგენა და წინასწარი შეტყობინება“)"
    assert format_citation(title="დისტანციური და ჰიბრიდული მუშაობის პოლიტიკა", version="2.0", section="4.1",
                           section_title="ლიმიტი", page=2) == \
        "დისტანციური და ჰიბრიდული მუშაობის პოლიტიკა v2.0, გვერდი 2, მუხლი 4.1 („ლიმიტი“)"
    assert "პუნქტი ა.2" in format_citation(title="FAQ", version="1.4", section="ა.2", section_title=None, page=None)
    assert "დოკუმენტის ზოგადი ინფორმაცია" in format_citation(title="X", version=None, section=None,
                                                             section_title=None, page=None)


def passage(document, rank, domain, content, section="1.1", score=0.04):
    return Passage(chunk_id=hash((document, section)) % 10_000, document=document, title=document, version="1",
                   domain=domain, authority={1: "authoritative_policy", 2: "general_handbook", 3: "reference_faq"}[rank],
                   authority_rank=rank, section=section, section_title=None, page=None, content=content,
                   similarity=0.8, score=score, citation=f"{document} {section}")


def test_superseded_marking_and_ordering():
    faq = passage(FAQ, 3, "general", "მოთხოვნა შვებულების დაწყებამდე 5 სამუშაო დღით ადრე", "ა.2", score=0.05)
    policy = passage(LEAVE, 1, "leave", "შვებულების მოთხოვნა 5 ან 15 სამუშაო დღით ადრე", "4.4", score=0.04)
    unrelated = passage(FAQ, 3, "general", "ოფისის ბარათი დავკარგე", "გ.3", score=0.03)
    items = [faq, policy, unrelated]
    Retriever._mark_superseded(items)
    assert items[0] is policy
    assert faq.superseded_by == policy.citation and unrelated.superseded_by is None


def test_build_context_labels_sources():
    faq = passage(FAQ, 3, "general", "ტექსტი", "ა.2")
    faq.superseded_by = "შვებულებისა და გაცდენის პოლიტიკა v4.0, მუხლი 4.4"
    ctx = build_context(RetrievalResult(query="q", topic="leave", passages=[faq]))
    assert ctx.startswith("[1] წყარო: ") and "შეიძლება მოძველებული იყოს" in ctx and "უპირატესია" in ctx


# --- against the ingested documents ------------------------------------------------------------

@pytest.fixture(scope="module")
def retriever(engine):
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE rag_documents, document_chunks CASCADE"))
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    provider = LocalHashingEmbeddingProvider()
    ingest_documents(factory, provider, DOCUMENTS_DIR)
    return Retriever(factory, provider)


def top_authoritative(result):
    return next(p for p in result.passages if p.is_authoritative)


pytestmark_db = pytest.mark.db


@pytestmark_db
def test_leave_notice_question_prefers_leave_policy_over_faq(retriever):
    r = retriever.retrieve("რამდენი სამუშაო დღით ადრე უნდა წარვადგინო ყოველწლიური შვებულების მოთხოვნა?")
    assert r.sufficient and r.topic == "leave"
    assert r.passages[0].document == LEAVE
    # The offline lexical embedder is weaker than Gemini (exact top-1 quality is checked by
    # scripts/evaluate_retrieval.py with Gemini); here 4.4 must be among the first three.
    assert (LEAVE, "4.4") in [(p.document, p.section) for p in r.passages[:3]]
    for p in r.passages:
        if p.document == FAQ and p.section == "ა.2":
            assert p.superseded_by and "4.4" in p.superseded_by


@pytestmark_db
def test_outdated_faq_never_ranks_above_authoritative_policy(retriever):
    r = retriever.retrieve("გამოუყენებელი შვებულების რამდენი დღე გადადის მომდევნო წელზე?")
    ranks = [p.authority_rank for p in r.passages if not p.superseded_by]
    assert ranks == sorted(ranks) and r.passages[0].authority_rank == 1
    assert any(p.document == LEAVE and p.section == "4.7" for p in r.passages)


@pytestmark_db
def test_remote_work_question_uses_remote_policy(retriever):
    r = retriever.retrieve("რამდენი დღე შემიძლია ვიმუშაო დისტანციურად კვირაში?")
    assert r.topic == "remote_work"
    assert top_authoritative(r).document == REMOTE
    assert any(p.document == REMOTE and p.section == "4.1" for p in r.passages)
    assert all(p.superseded_by for p in r.passages if p.authority_rank > 1 and detect_topic(p.content) == "remote_work")


@pytestmark_db
def test_specialised_policy_wins_in_its_own_domain(retriever):
    r = retriever.retrieve("რა სიგრძის უნდა იყოს პაროლი?")
    top = top_authoritative(r)
    assert top.document == "Information_Security_Policy_v3.2.pdf" and top.section == "4.1"


@pytestmark_db
def test_article_reference(retriever):
    r = retriever.retrieve("რას ამბობს მუხლი 4.6 შვებულების შესახებ?")
    assert r.section_hit and r.sufficient
    assert (r.passages[0].document, r.passages[0].section) == (LEAVE, "4.6")


@pytestmark_db
def test_source_metadata(retriever):
    r = retriever.retrieve("რამდენი დღე შემიძლია ვიმუშაო დისტანციურად კვირაში?")
    p = next(p for p in r.passages if p.document == REMOTE and p.section == "4.1")
    assert p.page == 2 and p.version == "2.0" and p.authority == "authoritative_policy"
    assert p.citation == "დისტანციური და ჰიბრიდული მუშაობის პოლიტიკა v2.0, გვერდი 2, მუხლი 4.1 („ლიმიტი“)"
    leave = retriever.retrieve("რამდენი სამუშაო დღით ადრე უნდა წარვადგინო ყოველწლიური შვებულების მოთხოვნა?")
    assert top_authoritative(leave).page is None  # DOCX: no page invented


@pytestmark_db
@pytest.mark.parametrize("question", ["როგორ მოვამზადო ხაჭაპური?", "ვინ მოიგო ფეხბურთის მსოფლიო ჩემპიონატი?"])
def test_unknown_question_is_insufficient(retriever, question):
    assert retriever.retrieve(question).sufficient is False


@pytestmark_db
def test_alternative_queries_are_fused(retriever):
    plain = retriever.retrieve("ლეპტოპი დავკარგე, რა ვქნა?")
    better = retriever.retrieve("ლეპტოპი დავკარგე, რა ვქნა?",
                                alternative_queries=["მოწყობილობის დაკარგვა ან მოპარვა, შეტყობინების ვადა"])
    rank = lambda r: next((i for i, p in enumerate(r.passages) if p.section == "10.2"), 99)  # noqa: E731
    assert rank(better) <= rank(plain) and rank(better) < 6


@pytestmark_db
def test_not_ready_without_documents_or_with_other_model(engine, retriever):
    class Other(LocalHashingEmbeddingProvider):
        @property
        def signature(self):
            return "other:768"

    with pytest.raises(RagNotReady):
        Retriever(retriever.session_factory, Other()).retrieve("შვებულება")
