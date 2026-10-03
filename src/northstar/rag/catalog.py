"""Document catalog: topic (domain) and precedence of each supplied document.

The precedence comes from the documents themselves, not from assumptions:
- Leave and Absence Policy 1.4: it prevails over the Employee Handbook, the FAQ and other
  general material; the FAQ is reference-only and may be outdated.
- Employee Handbook 1.2: specialised policies prevail over the handbook; the FAQ is a short
  reference.
- Remote and Hybrid Work Policy 1.3: replaces Handbook article 6 and the FAQ overview.
- FAQ header: "reference material; may contain outdated information".

Each specialised policy is authoritative only for its own domain.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Domain = Literal["leave", "remote_work", "security", "learning", "travel", "general"]
Authority = Literal["authoritative_policy", "general_handbook", "reference_faq"]

AUTHORITY_RANK: dict[str, int] = {"authoritative_policy": 1, "general_handbook": 2, "reference_faq": 3}


@dataclass(frozen=True)
class CatalogEntry:
    document: str
    title: str          # Georgian title as printed in the document
    domain: Domain
    authority: Authority

    @property
    def authority_rank(self) -> int:
        return AUTHORITY_RANK[self.authority]


CATALOG: dict[str, CatalogEntry] = {e.document: e for e in (
    CatalogEntry("Leave_and_Absence_Policy_v4.0.docx", "შვებულებისა და გაცდენის პოლიტიკა",
                 "leave", "authoritative_policy"),
    CatalogEntry("Remote_and_Hybrid_Work_Policy_v2.0.pdf", "დისტანციური და ჰიბრიდული მუშაობის პოლიტიკა",
                 "remote_work", "authoritative_policy"),
    CatalogEntry("Information_Security_Policy_v3.2.pdf",
                 "ინფორმაციული უსაფრთხოებისა და IT რესურსების გამოყენების პოლიტიკა", "security", "authoritative_policy"),
    CatalogEntry("Learning_and_Development_Policy_v1.2.pdf", "სწავლისა და პროფესიული განვითარების პოლიტიკა",
                 "learning", "authoritative_policy"),
    CatalogEntry("Travel_and_Expense_Policy_v2.3.pdf", "მივლინებისა და ხარჯების ანაზღაურების პოლიტიკა",
                 "travel", "authoritative_policy"),
    CatalogEntry("Employee_Handbook_v3.1.docx", "თანამშრომლის სახელმძღვანელო", "general", "general_handbook"),
    CatalogEntry("Employee_FAQ_2025.docx", "ხშირად დასმული კითხვები თანამშრომლებისთვის", "general", "reference_faq"),
)}


def catalog_entry(document: str) -> CatalogEntry:
    """Unknown documents are treated conservatively as reference material of general scope."""
    return CATALOG.get(document) or CatalogEntry(document, document, "general", "reference_faq")
