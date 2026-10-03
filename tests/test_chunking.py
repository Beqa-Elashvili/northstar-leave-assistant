"""Structure-aware chunking (no database)."""

import pytest

from northstar.config import DOCUMENTS_DIR
from northstar.rag.chunking import MAX_CHUNK_CHARS, PREAMBLE_TITLE, chunk_document
from northstar.rag.extraction import Block, ExtractedDocument, extract_document, list_documents


@pytest.fixture(scope="module")
def chunks():
    return {p.name: chunk_document(extract_document(p)) for p in list_documents(DOCUMENTS_DIR)}


def by_section(chs):
    return {c.section: c for c in chs if c.metadata["part"] == 0}


def test_every_document_chunked(chunks):
    assert len(chunks) == 7 and all(chunks.values())


def test_one_chunk_per_sub_article(chunks):
    leave = chunks["Leave_and_Absence_Policy_v4.0.docx"]
    sections = [c.section for c in leave]
    assert sections.count("4.4") == 1 and sections.count("6.4") == 1
    c = by_section(leave)["4.4"]
    assert "წინასწარი შეტყობინების ვადა ითვლება" in c.content          # paragraph of 4.4
    assert "1-დან 5-მდე; მინიმალური წინასწარი შეტყობინება: 5 სამუშაო დღე" in c.content  # its table
    assert "4.5" not in c.content.split("\n", 1)[0]
    assert "ერთი მოთხოვნით შესაძლებელია არაუმეტეს 15" not in c.content  # 4.5 text is not in 4.4


def test_context_line_and_metadata_docx(chunks):
    c = by_section(chunks["Leave_and_Absence_Policy_v4.0.docx"])["4.4"]
    first_line = c.content.split("\n", 1)[0]
    assert first_line == ("შვებულებისა და გაცდენის პოლიტიკა (ვერსია 4.0) — 4. ყოველწლიური ანაზღაურებადი "
                          "შვებულება › მუხლი 4.4. მოთხოვნის წარდგენა და წინასწარი შეტყობინება")
    m = c.metadata
    assert (m["document"], m["section"], m["domain"], m["authority"], m["authority_rank"]) == \
        ("Leave_and_Absence_Policy_v4.0.docx", "4.4", "leave", "authoritative_policy", 1)
    assert m["section_title"] == "მოთხოვნის წარდგენა და წინასწარი შეტყობინება"
    assert c.page is None and m["page"] is None  # never invented for DOCX


def test_pdf_chunk_has_page(chunks):
    c = by_section(chunks["Remote_and_Hybrid_Work_Policy_v2.0.pdf"])["4.1"]
    assert (c.page, c.metadata["page"], c.metadata["doc_code"]) == (2, 2, "HR-POL-05")
    assert "არაუმეტეს 2 სამუშაო დღე" in c.content


def test_pdf_chunk_spanning_pages_records_range(chunks):
    sec = by_section(chunks["Information_Security_Policy_v3.2.pdf"])
    assert sec["2.1"].metadata["page"] == 1  # classification table starts on page 1 (continues on 2)


def test_article_intro_text_is_its_own_chunk(chunks):
    sec = by_section(chunks["Remote_and_Hybrid_Work_Policy_v2.0.pdf"])
    assert "დისტანციური სამუშაო დღე" in sec["2"].content   # "2. ტერმინები" has no sub-articles


def test_preamble_chunk(chunks):
    pre = chunks["Employee_FAQ_2025.docx"][0]
    assert pre.section is None and pre.section_title == PREAMBLE_TITLE
    assert "შეიძლება შეიცავდეს მოძველებულ ინფორმაციას" in pre.content
    assert pre.metadata["authority"] == "reference_faq"


def test_faq_question_chunks(chunks):
    sec = by_section(chunks["Employee_FAQ_2025.docx"])
    assert "5 სამუშაო დღით ადრე" in sec["ა.2"].content
    assert sec["ა.2"].metadata["authority_rank"] == 3


def test_chunk_indexes_unique_and_sizes_bounded(chunks):
    for chs in chunks.values():
        assert [c.chunk_index for c in chs] == list(range(len(chs)))
        assert all(len(c.content) <= MAX_CHUNK_CHARS + 400 for c in chs)


def test_content_hash_stable(chunks):
    again = chunk_document(extract_document(DOCUMENTS_DIR / "Employee_FAQ_2025.docx"))
    assert [c.content_hash for c in again] == [c.content_hash for c in chunks["Employee_FAQ_2025.docx"]]


def test_long_section_split_on_block_boundaries():
    para = "ა" * 900
    doc = ExtractedDocument("Leave_and_Absence_Policy_v4.0.docx", "docx", "t", {"version": "4.0"}, [
        Block("heading", "9.9 გრძელი", level=2, section="9.9", heading_title="გრძელი"),
        *[Block("paragraph", para) for _ in range(5)],
    ])
    parts = chunk_document(doc)
    assert len(parts) == 3
    assert all(p.section == "9.9" and "მუხლი 9.9. გრძელი" in p.content for p in parts)
    assert [p.metadata["part"] for p in parts] == [0, 1, 2]
