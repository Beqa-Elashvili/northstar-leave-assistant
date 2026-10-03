"""GeminiProvider retry / fallback / error policy with a fake SDK client (no network, no key)."""

from types import SimpleNamespace

import anyio
import pytest
from google.genai import errors
from pydantic import BaseModel

from northstar.agent.llm import GeminiProvider, LLMError


class Out(BaseModel):
    value: str


class FakeAioModels:
    def __init__(self, script):
        self.script = script          # model -> list of outcomes (int = HTTP error, str = response text)
        self.calls = []

    async def generate_content(self, model, contents, config):
        self.calls.append(model)
        outcome = self.script[model].pop(0)
        if isinstance(outcome, int):
            raise errors.APIError(outcome, {"error": {"message": "x"}})
        return SimpleNamespace(parsed=None, text=outcome)


def provider(script, models):
    p = GeminiProvider.__new__(GeminiProvider)
    from google.genai import types

    p._types = types
    p._models = models
    p._thinking = types.ThinkingConfig(thinking_level="LOW")
    fake = FakeAioModels(script)
    p._client = SimpleNamespace(aio=SimpleNamespace(models=fake))
    return p, fake


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    async def instant(_seconds):
        return None
    monkeypatch.setattr("asyncio.sleep", instant)


def test_structured_output_parsed():
    p, _ = provider({"a": ['{"value": "ok"}']}, ["a"])
    assert anyio.run(p.generate_structured, "s", "p", Out).value == "ok"


def test_overloaded_model_retried_then_falls_back():
    p, fake = provider({"a": [503, 503], "b": ["ტექსტი"]}, ["a", "b"])
    assert anyio.run(p.generate_text, "s", "p") == "ტექსტი"
    assert fake.calls == ["a", "a", "b"]


def test_unavailable_model_skipped_immediately():
    p, fake = provider({"a": [404], "b": ["x"]}, ["a", "b"])
    assert anyio.run(p.generate_text, "s", "p") == "x" and fake.calls == ["a", "b"]


def test_billing_error_stops_without_trying_other_models():
    p, fake = provider({"a": [402], "b": ["never"]}, ["a", "b"])
    with pytest.raises(LLMError, match="402"):
        anyio.run(p.generate_text, "s", "p")
    assert fake.calls == ["a"]


def test_all_models_overloaded():
    p, _ = provider({"a": [503, 503], "b": [429, 429]}, ["a", "b"])
    with pytest.raises(LLMError, match="unavailable"):
        anyio.run(p.generate_text, "s", "p")


def test_invalid_structured_output():
    p, _ = provider({"a": ["not json"]}, ["a"])
    with pytest.raises(LLMError, match="schema"):
        anyio.run(p.generate_structured, "s", "p", Out)
