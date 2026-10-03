"""Grounded policy answers: retrieve passages, let the LLM word the answer, attach real citations.

Citations are never written by the LLM: it may only refer to passages by number ([1], [2]); the
"Source" lines are built from the stored metadata of exactly those passages.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from northstar.agent.llm import LLMProvider
from northstar.rag.catalog import Domain
from northstar.rag.retrieval import Retriever, RetrievalResult, build_context

NO_INFO = "მოწოდებულ კომპანიის დოკუმენტებში ამ კითხვაზე საკმარისი ინფორმაცია ვერ მოიძებნა."

SYSTEM = f"""\
შენ ხარ „ნორთსტარ სერვისეზის“ HR ასისტენტი. უპასუხე მხოლოდ ქართულად, მოკლედ და გასაგებად.
წესები:
1. გამოიყენე მხოლოდ ქვემოთ მოცემული დანომრილი წყაროები. ზოგადი ცოდნიდან ან ვარაუდით არაფერი დაამატო:
   არც ლიმიტები, არც თანხები, არც ვადები, არც ნებართვები.
2. ყოველი ფაქტის შემდეგ მიუთითე წყაროს ნომერი კვადრატულ ფრჩხილებში, მაგ. [1]. სხვა სახით წყარო არ ჩაწერო.
3. თუ წყაროები ერთმანეთს ეწინააღმდეგება, იხელმძღვანელე „მოქმედი სპეციალური პოლიტიკით“ და მოკლედ აღნიშნე,
   რომ FAQ/სახელმძღვანელოში მოცემული ინფორმაცია მოძველებულია.
4. თუ წყაროებში პასუხი არ არის, უპასუხე ზუსტად ამ წინადადებით: „{NO_INFO}“
5. არ შეაფასო, დამტკიცდება თუ არა კონკრეტული მოთხოვნა; ეს ხელმძღვანელისა და HR-ის გადაწყვეტილებაა.
6. წერე უბრალო ტექსტით ტერმინალისთვის: Markdown-ს (**, #, `) ნუ გამოიყენებ; ჩამონათვალისთვის გამოიყენე „• “.
"""

_CITE_RE = re.compile(r"\[(\d{1,2})\]")


@dataclass
class PolicyAnswer:
    text: str
    sources: list[str] = field(default_factory=list)
    found: bool = True


class PolicyAnswerer:
    def __init__(self, retriever: Retriever, llm: LLMProvider):
        self.retriever = retriever
        self.llm = llm

    async def retrieve(self, question: str, *, rephrasings: list[str] | None = None,
                       topic: Domain | None = None) -> RetrievalResult:
        # Retrieval uses blocking DB/HTTP clients; keep the event loop free.
        return await asyncio.to_thread(self.retriever.retrieve, question, 6, topic, rephrasings)

    async def answer(self, question: str, *, rephrasings: list[str] | None = None, topic: Domain | None = None,
                     instruction: str | None = None, on_chunk: Callable[[str], None] | None = None) -> PolicyAnswer:
        result = await self.retrieve(question, rephrasings=rephrasings, topic=topic)
        if not result.sufficient or not result.passages:
            return PolicyAnswer(NO_INFO, [], found=False)
        prompt = f"წყაროები:\n{build_context(result)}\n\nთანამშრომლის კითხვა: {question}"
        if instruction:
            prompt += f"\n\nდამატებითი მითითება: {instruction}"
        text = (await self.llm.generate_text(SYSTEM, prompt, on_chunk=on_chunk)).strip()
        if not text or NO_INFO in text:
            return PolicyAnswer(NO_INFO, [], found=False)
        cited = [int(n) for n in _CITE_RE.findall(text) if 1 <= int(n) <= len(result.passages)]
        numbers = list(dict.fromkeys(cited))
        if numbers:
            sources = [f"[{n}] {result.passages[n - 1].citation}" for n in numbers]
        else:
            # The model cited nothing: show the passages it was given, authoritative first.
            sources = [p.citation for p in result.passages if not p.superseded_by][:2]
        return PolicyAnswer(text, sources, found=True)
