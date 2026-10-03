"""DOCX/PDF extraction of the supplied documents (read-only)."""

from pathlib import Path

import pytest

from northstar.config import DOCUMENTS_DIR
from northstar.rag.catalog import CATALOG, catalog_entry
from northstar.rag.extraction import extract_document, list_documents, parse_heading, render_table


@pytest.fixture(scope="module")
def docs():
    return {p.name: extract_document(p) for p in list_documents(DOCUMENTS_DIR)}


def headings(doc):
    return {b.section: b for b in doc.blocks if b.kind == "heading" and b.section}


def text_of(doc):
    return "\n".join(b.text for b in doc.blocks)


def test_all_seven_documents_extracted_and_catalogued(docs):
    assert len(docs) == 7
    assert set(docs) == set(CATALOG)
    for name, d in docs.items():
        assert d.blocks and d.title == CATALOG[name].title, name


@pytest.mark.parametrize("name, code, version", [
    ("Leave_and_Absence_Policy_v4.0.docx", "HR-POL-02", "4.0"),
    ("Employee_Handbook_v3.1.docx", "HR-HB-01", "3.1"),
    ("Employee_FAQ_2025.docx", "HR-FAQ-01", "1.4"),
    ("Remote_and_Hybrid_Work_Policy_v2.0.pdf", "HR-POL-05", "2.0"),
    ("Information_Security_Policy_v3.2.pdf", "SEC-POL-01", "3.2"),
    ("Learning_and_Development_Policy_v1.2.pdf", "HR-POL-07", "1.2"),
    ("Travel_and_Expense_Policy_v2.3.pdf", "FIN-POL-03", "2.3"),
])
def test_metadata_table(docs, name, code, version):
    assert (docs[name].metadata["doc_code"], docs[name].metadata["version"]) == (code, version)


def test_faq_status_marks_it_as_reference(docs):
    assert "მოძველებულ" in docs["Employee_FAQ_2025.docx"].metadata["status"]


# --- DOCX ------------------------------------------------------------------------------------------

def test_docx_headings_and_article_numbers(docs):
    h = headings(docs["Leave_and_Absence_Policy_v4.0.docx"])
    assert h["4.4"].heading_title == "მოთხოვნის წარდგენა და წინასწარი შეტყობინება"
    assert h["4.4"].level == 2 and h["4"].level == 1
    assert {"1.4", "2.3", "4.3", "4.5", "4.6", "5.1", "6.4", "7.1", "7.2", "8.3", "9.3", "10.2", "12.3"} <= set(h)


def test_docx_has_no_page_numbers(docs):
    for name in ("Leave_and_Absence_Policy_v4.0.docx", "Employee_Handbook_v3.1.docx", "Employee_FAQ_2025.docx"):
        assert all(b.page is None for b in docs[name].blocks)


def test_docx_tables_keep_cell_association(docs):
    tables = [b for b in docs["Leave_and_Absence_Policy_v4.0.docx"].blocks if b.kind == "table"]
    notice = next(t for t in tables if "მინიმალური წინასწარი შეტყობინება" in t.text)
    assert "მოთხოვნილი სამუშაო დღეები: 1-დან 5-მდე; მინიმალური წინასწარი შეტყობინება: 5 სამუშაო დღე" in notice.text
    assert "6-დან 15-მდე; მინიმალური წინასწარი შეტყობინება: 15 სამუშაო დღე" in notice.text


def test_docx_list_items_and_paragraph_order(docs):
    blocks = docs["Leave_and_Absence_Policy_v4.0.docx"].blocks
    i = next(i for i, b in enumerate(blocks) if b.kind == "heading" and b.section == "4.6")
    following = [b for b in blocks[i + 1:i + 4]]
    assert following[0].kind == "paragraph"
    assert following[1].kind == "list_item" and "1 დეკემბრიდან 20 დეკემბრის ჩათვლით" in following[1].text


def test_faq_letter_sections(docs):
    h = headings(docs["Employee_FAQ_2025.docx"])
    assert h["ა.2"].heading_title == "რამდენი ხნით ადრე უნდა მოვითხოვო შვებულება?"
    assert {"ა", "ბ", "ე.3"} <= set(h)


# --- PDF -------------------------------------------------------------------------------------------

def test_pdf_page_numbers_and_sections(docs):
    h = headings(docs["Remote_and_Hybrid_Work_Policy_v2.0.pdf"])
    assert (h["4.1"].page, h["4.1"].heading_title) == (2, "ლიმიტი")
    assert h["10.2"].page == 5 and h["10.1"].page == 4
    assert h["4"].level == 1 and h["4.1"].level == 2


def test_pdf_running_header_and_footer_removed(docs):
    text = text_of(docs["Remote_and_Hybrid_Work_Policy_v2.0.pdf"])
    assert "გვერდი 2" not in text
    assert "შიდა გამოყენებისთვის" not in text
    assert "შპს „ნორთსტარ სერვისეზი“ | " not in text


def test_pdf_wrapped_lines_joined_into_one_paragraph(docs):
    blocks = docs["Remote_and_Hybrid_Work_Policy_v2.0.pdf"].blocks
    para = next(b for b in blocks if "არაუმეტეს 2 სამუშაო დღე" in b.text)
    assert para.kind == "paragraph" and para.page == 2
    assert para.text.startswith("უშუალო ხელმძღვანელის წინასწარი თანხმობით") and para.text.endswith("კალენდარში.")


def test_pdf_table_rows(docs):
    travel = [b for b in docs["Travel_and_Expense_Policy_v2.3.pdf"].blocks if b.kind == "table"]
    hotel = next(t for t in travel if "ლიმიტი ერთ ღამეზე" in t.text)
    assert hotel.page == 3
    assert "ადგილი: ლონდონი; ლიმიტი ერთ ღამეზე: 180 გირვანქა სტერლინგი" in hotel.text
    per_diem = next(t for t in travel if "დღიური ნორმა" in t.text and "გერმანია" in t.text)
    assert "ქვეყანა: გერმანია; დღიური ნორმა: 60 ევრო" in per_diem.text


def test_pdf_table_continued_on_next_page_is_merged(docs):
    tables = [b for b in docs["Information_Security_Policy_v3.2.pdf"].blocks if b.kind == "table"]
    classes = [t for t in tables if t.rows and t.rows[0][0] == "კლასი"]
    assert len(classes) == 1
    assert [r[0] for r in classes[0].rows[1:]] == ["საჯარო", "შიდა", "კონფიდენციალური", "შეზღუდული"]
    assert classes[0].page == 1


def test_pdf_table_text_not_duplicated_as_paragraphs(docs):
    paragraphs = [b.text for b in docs["Travel_and_Expense_Policy_v2.3.pdf"].blocks if b.kind == "paragraph"]
    assert not any(p.startswith("ლონდონი") for p in paragraphs)


# --- helpers ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("4.4 მოთხოვნის წარდგენა", ("4.4", "მოთხოვნის წარდგენა")),
    ("10. დისტანციური მუშაობა და შვებულება", ("10", "დისტანციური მუშაობა და შვებულება")),
    ("ა.7 ბებია ან ბაბუა გარდამეცვალა.", ("ა.7", "ბებია ან ბაბუა გარდამეცვალა.")),
    ("ბ. დისტანციური მუშაობა", ("ბ", "დისტანციური მუშაობა")),
    ("როგორ გამოვიყენოთ ეს დოკუმენტი", (None, "როგორ გამოვიყენოთ ეს დოკუმენტი")),
])
def test_parse_heading(text, expected):
    assert parse_heading(text) == expected


def test_render_metadata_table():
    assert render_table([["ვერსია", "4.0"], ["სტატუსი", "მოქმედი"]]) == "ვერსია: 4.0\nსტატუსი: მოქმედი"


def test_unknown_document_is_treated_as_reference():
    entry = catalog_entry("Random.pdf")
    assert entry.authority == "reference_faq" and entry.domain == "general"


def test_unsupported_file_type(tmp_path: Path):
    f = tmp_path / "x.txt"
    f.write_text("x")
    with pytest.raises(ValueError):
        extract_document(f)
