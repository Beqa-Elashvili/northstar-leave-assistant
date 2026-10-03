"""Test doubles for the agent: a scripted LLM, wired to the real MCP server and real retrieval."""

from __future__ import annotations

from dataclasses import dataclass, field

import anyio
from mcp.client.client import Client
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker

from northstar.agent.agent import AgentReply, HRAgent
from northstar.agent.intents import IntentExtraction
from northstar.agent.llm import LLMError
from northstar.agent.policy_qa import PolicyAnswerer
from northstar.agent.tools import McpLeaveTools
from northstar.config import DOCUMENTS_DIR
from northstar.mcp.server import build_server
from northstar.rag.embeddings import LocalHashingEmbeddingProvider
from northstar.rag.ingest import ingest_documents
from northstar.rag.retrieval import Retriever
from northstar.services.authorization import Principal, Role
from tests.mcp_helpers import CLOCK


def X(intent: str, **fields) -> IntentExtraction:  # noqa: N802 - short constructor for readable tests
    return IntentExtraction(intent=intent, **fields)


@dataclass
class FakeLLM:
    """Returns a pre-scripted extraction per user message; the policy answer cites passage [1]."""

    intents: dict[str, IntentExtraction] = field(default_factory=dict)
    answer: str = "პოლიტიკის მიხედვით ასეა [1]."
    structured_calls: int = 0
    text_prompts: list[str] = field(default_factory=list)

    async def generate_structured(self, system, prompt, schema):
        self.structured_calls += 1
        message = prompt.split("User message:\n", 1)[1]
        return self.intents[message]

    async def generate_text(self, system, prompt, on_chunk=None):
        self.text_prompts.append(prompt)
        if on_chunk:
            on_chunk(self.answer)
        return self.answer


class BrokenLLM(FakeLLM):
    async def generate_structured(self, system, prompt, schema):
        raise LLMError("Gemini unavailable (test)")

    async def generate_text(self, system, prompt, on_chunk=None):
        raise LLMError("Gemini unavailable (test)")


def ingest_test_documents(engine) -> Retriever:
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE rag_documents, document_chunks CASCADE"))
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    provider = LocalHashingEmbeddingProvider()
    ingest_documents(factory, provider, DOCUMENTS_DIR)
    return Retriever(factory, provider)


def converse(engine, retriever: Retriever, llm, messages: list, employee_id: str = "E1001",
             ) -> tuple[list[AgentReply], HRAgent]:
    """Run one conversation through a real MCP client/server pair (in-memory transport)."""

    async def go():
        server = build_server(Principal(Role.EMPLOYEE, employee_id), sessionmaker(bind=engine, expire_on_commit=False),
                              CLOCK)
        async with Client(server) as client:
            agent = HRAgent(McpLeaveTools(client), llm, PolicyAnswerer(retriever, llm), CLOCK.today())
            await agent.start()
            replies = []
            for m in messages:
                if callable(m):  # a step between messages, e.g. data changing behind the conversation
                    await anyio.to_thread.run_sync(m)
                else:
                    replies.append(await agent.handle(m))
            return replies, agent

    return anyio.run(go)
