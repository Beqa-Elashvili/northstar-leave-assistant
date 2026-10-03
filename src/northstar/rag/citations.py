"""Georgian source citations built only from stored chunk metadata (never invented).

DOCX: "შვებულებისა და გაცდენის პოლიტიკა v4.0, მუხლი 4.4 („მოთხოვნის წარდგენა ...“)"
PDF:  "დისტანციური და ჰიბრიდული მუშაობის პოლიტიკა v2.0, გვერდი 2, მუხლი 4.1 („ლიმიტი“)"
FAQ:  "ხშირად დასმული კითხვები თანამშრომლებისთვის v1.4, პუნქტი ა.2 („...“)"
"""

from __future__ import annotations


def format_citation(*, title: str, version: str | None, section: str | None, section_title: str | None,
                    page: int | None) -> str:
    parts = [f"{title} v{version}" if version else title]
    if page is not None:
        parts.append(f"გვერდი {page}")
    if section is None:
        parts.append("დოკუმენტის ზოგადი ინფორმაცია")
    else:
        label = "პუნქტი" if not section[0].isdigit() else "მუხლი"
        text = f"{label} {section}"
        if section_title:
            text += f" („{section_title}“)"
        parts.append(text)
    return ", ".join(parts)
