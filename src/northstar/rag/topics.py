"""Deterministic topic detection: which policy domain a question (or passage) is about.

Used to prefer the policy that is authoritative for that topic (e.g. remote-work questions →
Remote and Hybrid Work Policy, not the Handbook or the FAQ). Georgian is matched on word
stems because nouns and verbs are inflected (შვებულება / შვებულებას / შვებულების ...).
"""

from __future__ import annotations

from northstar.rag.catalog import Domain

TOPIC_STEMS: dict[Domain, tuple[str, ...]] = {
    "leave": (
        "შვებულ", "ავადმყოფ", "ავად ვარ", "გლოვ", "გარდაიცვალ", "გარდაცვალ", "გარდამეცვალ", "გარდაეცვალ",
        "დაკრძალ", "ბებია", "ბებიაჩემ", "ბაბუა", "დეკრეტ", "დედობ", "მამობ",
        "ბავშვის მოვლ", "შვილად აყვან", "გამოსაცდელ", "უხელფასო", "სამედიცინო ცნობ", "უქმე დღ", "დასვენებ",
        "გადატანილ", "გადმოტანილ", "გაცდენ", "სისხლის დონაცი", "ბალანს", "საგამოცდო", "leave", "vacation",
    ),
    "remote_work": (
        "დისტანციურ", "სახლიდან", "ჰიბრიდ", "ოფისში ყოფნ", "ძირითადი საათ", "საზღვარგარეთ მუშაობ",
        "საზღვარგარეთიდან", "remote", "home office",
    ),
    "security": (
        "პაროლ", "უსაფრთხოებ", "vpn", "mfa", "ავთენტიფიკაც", "ლეპტოპ", "ფიშინგ", "usb", "chatgpt", "copilot",
        "ხელოვნური ინტელექტ", "ai სერვის", "ai ინსტრუმენტ", "ინციდენტ", "დაშიფვრ", "wi-fi", "კლასიფიკაც",
        "ფაილების გაზიარებ", "მოწყობილობ", "ელფოსტის გადამისამართ",
    ),
    "learning": (
        "სწავლ", "ტრენინგ", "კვალიფიკაც", "კურს", "acca", "cima", "cfa", "cisa", "სერტიფიც", "გამოცდ",
        "პროფესიული განვითარ", "განმეორებით გამოცდ",
    ),
    "travel": (
        "მივლინებ", "ხარჯ", "სასტუმრო", "დღიური ნორმ", "per diem", "ავიაბილეთ", "ტაქსი", "მონიტორ",
        "ავანს", "ვიზ", "კილომეტრ", "წარმომადგენლობით", "კორპორაციული ბარათ", "ქვითარ", "ინვოის",
    ),
}


def topic_scores(text: str) -> dict[Domain, int]:
    lowered = text.lower()
    return {domain: sum(lowered.count(stem) for stem in stems) for domain, stems in TOPIC_STEMS.items()}


def detect_topic(text: str) -> Domain | None:
    """Main domain of the text, or None when no domain keyword occurs."""
    scores = topic_scores(text)
    best = max(scores, key=lambda d: scores[d])
    return best if scores[best] > 0 else None
