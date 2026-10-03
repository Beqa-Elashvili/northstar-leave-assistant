"""Retrieval quality check against the ingested documents (uses the configured embedding provider).

Each case lists the acceptable articles (one of them must be the top authoritative passage or
among the first two passages), or None when the documents do not contain the answer and
retrieval must report "insufficient". An optional rephrasing in policy terminology stands in for
the query rewrite the agent's LLM provides at runtime.

Usage:
    python -m scripts.evaluate_retrieval
"""

from __future__ import annotations

import sys
import time

from northstar.database.engine import get_session_factory
from northstar.rag.embeddings import get_embedding_provider
from northstar.rag.retrieval import Retriever

LEAVE = "Leave_and_Absence_Policy_v4.0.docx"
REMOTE = "Remote_and_Hybrid_Work_Policy_v2.0.pdf"
SECURITY = "Information_Security_Policy_v3.2.pdf"
LEARNING = "Learning_and_Development_Policy_v1.2.pdf"
TRAVEL = "Travel_and_Expense_Policy_v2.3.pdf"

Expected = list[tuple[str, str]] | None
CASES: list[tuple[str, Expected] | tuple[str, Expected, str]] = [
    ("რამდენი დღით ადრე უნდა წარვადგინო შვებულების მოთხოვნა?", [(LEAVE, "4.4")]),        # FAQ/Handbook say 5 for all
    ("გამოუყენებელი შვებულების რამდენი დღე გადადის მომდევნო წელზე?", [(LEAVE, "4.7")]),  # FAQ/Handbook: 10 until June
    ("როდის მჭირდება სამედიცინო ცნობა ავადმყოფობისას?", [(LEAVE, "6.3")]),              # FAQ/Handbook: > 3 days
    ("რა ხდება გამოსაცდელ ვადაში შვებულებასთან დაკავშირებით?", [(LEAVE, "4.3")]),
    ("ბებია გარდამეცვალა, რამდენი დღე მეკუთვნის?", [(LEAVE, "8.2")]),
    ("შემიძლია გამოცდისთვის შვებულება ავიღო?", [(LEAVE, "9.1"), (LEAVE, "9.3")]),
    ("როგორ მოვითხოვო მამობის შვებულება?", [(LEAVE, "10.3")]),
    ("უხელფასო შვებულება რამდენი დღით შეიძლება?", [(LEAVE, "7.1")]),
    ("რას ამბობს მუხლი 4.6?", [(LEAVE, "4.6")]),
    ("რამდენი დღე შემიძლია ვიმუშაო სახლიდან კვირაში?", [(REMOTE, "4.1")]),                 # FAQ/Handbook: 3 days
    ("შემიძლია საზღვარგარეთიდან ვიმუშაო?", [(REMOTE, "5.2")]),
    ("რა სიგრძის უნდა იყოს პაროლი?", [(SECURITY, "4.1")]),
    ("შემიძლია ChatGPT-ში კლიენტის დოკუმენტი ავტვირთო?", [(SECURITY, "6.3")]),
    ("ლეპტოპი დავკარგე, რა ვქნა?", [(SECURITY, "10.2")], "მოწყობილობის დაკარგვა ან მოპარვა, შეტყობინების ვადა"),
    ("რამდენ ხარჯს ფარავს კომპანია სასწავლო მასალებზე?", [(LEARNING, "5.2")]),
    ("რამდენია სასტუმროს ლიმიტი ლონდონში?", [(TRAVEL, "5.1")]),
    ("სახლისთვის მონიტორი ვიყიდე, მინაზღაურებენ?", [(TRAVEL, "8.1")]),
    ("რამდენია დღიური ნორმა გერმანიაში?", [(TRAVEL, "6.2")]),
    ("რამდენია კომპანიის საპენსიო შენატანი?", None),
    ("რა ხელფასს იღებს აღმასრულებელი დირექტორი?", None),
    ("როგორ მოვამზადო ხაჭაპური?", None),
]


def main() -> int:
    retriever = Retriever(get_session_factory(), get_embedding_provider())
    failures = 0
    for case in CASES:
        question, expected = case[0], case[1]
        rephrased = [case[2]] if len(case) > 2 else None
        result = retriever.retrieve(question, k=6, alternative_queries=rephrased)
        if expected is None:
            ok = not result.sufficient
            detail = f"best={result.best_similarity:.3f} sufficient={result.sufficient}"
        else:
            top_auth = next((p for p in result.passages if p.is_authoritative), None)
            in_top = [(p.document, p.section) for p in result.passages]
            ok = result.sufficient and (
                (top_auth is not None and (top_auth.document, top_auth.section) in expected)
                or any(hit in expected for hit in in_top[:2]))
            detail = f"top={top_auth.citation if top_auth else '-'}"
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {question}\n      {detail}")
        time.sleep(0.7)  # stay under the free-tier request rate
    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
