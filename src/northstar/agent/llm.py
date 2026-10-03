"""LLM provider abstraction. Gemini-specific code lives only in `GeminiProvider`.

The LLM is used for language understanding (intent, leave type, dates, query rephrasing) and
for wording grounded policy answers. It never decides permissions or leave rules.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from northstar.config import Settings, get_settings

logger = logging.getLogger("northstar.llm")
T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """LLM unavailable or returned unusable output. Message is safe to log (no key, no prompt)."""


class LLMProvider(Protocol):
    async def generate_structured(self, system: str, prompt: str, schema: type[T]) -> T: ...

    async def generate_text(self, system: str, prompt: str,
                            on_chunk: Callable[[str], None] | None = None) -> str: ...


_RETRYABLE = {429, 500, 502, 503, 504}
_MODEL_UNAVAILABLE = {400, 404}
# Account-wide problems: no other model can help (e.g. 402 "prepayment credits are depleted").
_ACCOUNT_ERRORS = {401, 402, 403}


class GeminiProvider:
    ATTEMPTS_PER_MODEL = 2
    TIMEOUT_MS = 60_000

    def __init__(self, api_key: str, models: list[str], thinking_level: str = "LOW"):
        from google import genai
        from google.genai import types

        self._types = types
        self._client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=self.TIMEOUT_MS))
        self._models = [m for m in models if m]
        self._thinking = types.ThinkingConfig(thinking_level=thinking_level)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> GeminiProvider:
        settings = settings or get_settings()
        if settings.gemini_api_key is None:
            raise LLMError("GEMINI_API_KEY is not set")
        models = [settings.gemini_model] + [m.strip() for m in settings.gemini_fallback_models.split(",")]
        return cls(settings.gemini_api_key.get_secret_value(), list(dict.fromkeys(models)),
                   settings.gemini_thinking_level)

    def _config(self, system: str, schema: type[BaseModel] | None):
        kwargs = dict(system_instruction=system, temperature=0.1, thinking_config=self._thinking)
        if schema is not None:
            kwargs.update(response_mime_type="application/json", response_schema=schema, temperature=0.0)
        return self._types.GenerateContentConfig(**kwargs)

    async def _with_fallback(self, call):
        from google.genai import errors

        last = "no model configured"
        for model in self._models:
            for attempt in range(1, self.ATTEMPTS_PER_MODEL + 1):
                try:
                    return await call(model)
                except errors.APIError as exc:
                    last = f"{model}: HTTP {exc.code}"
                    logger.warning("Gemini call failed (%s)", last)
                    if exc.code in _ACCOUNT_ERRORS:
                        raise LLMError(f"Gemini API key/billing problem (HTTP {exc.code}); check the key and "
                                       "credits in Google AI Studio") from None
                    if exc.code in _MODEL_UNAVAILABLE:
                        break  # this model is not usable with the key; try the next one
                    if exc.code not in _RETRYABLE:
                        raise LLMError(f"Gemini request failed ({last})") from None
                    if attempt < self.ATTEMPTS_PER_MODEL:
                        await asyncio.sleep(1.5 * attempt)
                except (TimeoutError, OSError) as exc:
                    last = f"{model}: {type(exc).__name__}"
                    logger.warning("Gemini call failed (%s)", last)
        raise LLMError(f"Gemini unavailable ({last})")

    async def generate_structured(self, system: str, prompt: str, schema: type[T]) -> T:
        async def call(model: str) -> T:
            response = await self._client.aio.models.generate_content(
                model=model, contents=prompt, config=self._config(system, schema))
            if isinstance(response.parsed, schema):
                return response.parsed
            try:
                return schema.model_validate_json(response.text or "")
            except ValidationError:
                raise LLMError("Gemini returned output that does not match the schema") from None

        return await self._with_fallback(call)

    async def generate_text(self, system: str, prompt: str,
                            on_chunk: Callable[[str], None] | None = None) -> str:
        async def call(model: str) -> str:
            config = self._config(system, None)
            if on_chunk is None:
                response = await self._client.aio.models.generate_content(model=model, contents=prompt, config=config)
                return response.text or ""
            parts: list[str] = []
            stream = await self._client.aio.models.generate_content_stream(model=model, contents=prompt, config=config)
            try:
                async for chunk in stream:
                    if chunk.text:
                        parts.append(chunk.text)
                        on_chunk(chunk.text)
            except Exception:
                if parts:  # text was already shown: retrying would duplicate it
                    raise LLMError("Gemini stream interrupted") from None
                raise
            return "".join(parts)

        return await self._with_fallback(call)
