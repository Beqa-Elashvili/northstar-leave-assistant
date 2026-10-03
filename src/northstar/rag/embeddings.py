"""Embedding providers.

- `GeminiEmbeddingProvider` (default): `gemini-embedding-001`, multilingual (Georgian), reduced
  to 768 dimensions and L2-normalised (Google recommends normalising reduced-size outputs).
- `LocalHashingEmbeddingProvider`: deterministic character n-gram hashing. No network and no
  key; used by the automated tests and as an explicit offline fallback (`EMBEDDING_PROVIDER=local`).
  It captures lexical overlap only, so retrieval quality is lower than with Gemini.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections import deque
from collections.abc import Callable
from typing import Protocol

from northstar.config import Settings, get_settings


class EmbeddingError(RuntimeError):
    """Embedding backend unavailable; the message is safe to show (no key, no request body)."""


class EmbeddingProvider(Protocol):
    @property
    def signature(self) -> str:
        """Identifies model + dimension; stored with ingested documents."""

    @property
    def dim(self) -> int: ...

    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


def _normalise(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm else vector


class RateLimiter:
    """Rolling one-minute window: Gemini counts every embedded text as one request."""

    def __init__(self, per_minute: int, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.per_minute, self._clock, self._sleep = per_minute, clock, sleep
        self._sent: deque[tuple[float, int]] = deque()

    def acquire(self, n: int) -> float:
        """Block until `n` more requests fit in the window; returns the seconds waited."""
        waited = 0.0
        while True:
            now = self._clock()
            while self._sent and now - self._sent[0][0] >= 60:
                self._sent.popleft()
            used = sum(count for _, count in self._sent)
            if used + n <= self.per_minute or not self._sent:
                self._sent.append((now, n))
                return waited
            delay = 60 - (now - self._sent[0][0]) + 0.5
            self._sleep(delay)
            waited += delay


def _retry_delay(exc) -> float | None:
    details = getattr(exc, "details", None)
    if not isinstance(details, dict):
        return None
    for item in details.get("error", {}).get("details", []):
        value = str(item.get("retryDelay", ""))
        if value.endswith("s"):
            try:
                return float(value[:-1])
            except ValueError:
                return None
    return None


class GeminiEmbeddingProvider:
    BATCH_SIZE = 50
    MAX_ATTEMPTS = 6

    def __init__(self, api_key: str, model: str, dim: int, requests_per_minute: int = 90,
                 log: Callable[[str], None] | None = None):
        from google import genai  # imported lazily: tests never need the SDK client

        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._dim = dim
        self._limiter = RateLimiter(requests_per_minute)
        self._log = log or (lambda _msg: None)

    @property
    def signature(self) -> str:
        return f"gemini:{self._model}:{self._dim}"

    @property
    def dim(self) -> int:
        return self._dim

    def _embed(self, texts: list[str], task_type: str) -> list[list[float]]:
        from google.genai import errors, types

        out: list[list[float]] = []
        batch_size = min(self.BATCH_SIZE, self._limiter.per_minute)
        for start in range(0, len(texts), batch_size):
            batch = texts[start:start + batch_size]
            waited = self._limiter.acquire(len(batch))
            if waited:
                self._log(f"    (rate limit: waited {waited:.0f}s)")
            for attempt in range(1, self.MAX_ATTEMPTS + 1):
                try:
                    response = self._client.models.embed_content(
                        model=self._model, contents=batch,
                        config=types.EmbedContentConfig(task_type=task_type, output_dimensionality=self._dim),
                    )
                    out.extend(_normalise(list(e.values)) for e in response.embeddings)
                    break
                except errors.APIError as exc:
                    if getattr(exc, "code", None) in (401, 402, 403):
                        raise EmbeddingError(f"Gemini API key/billing problem (HTTP {exc.code}); check the key and "
                                             "credits in Google AI Studio") from None
                    retryable = getattr(exc, "code", None) in (429, 500, 502, 503, 504)
                    if not retryable or attempt == self.MAX_ATTEMPTS:
                        raise EmbeddingError(f"Gemini embedding request failed (HTTP {getattr(exc, 'code', '?')})") from None
                    delay = max(_retry_delay(exc) or 0.0, min(5 * 2 ** (attempt - 1), 60))
                    self._log(f"    (Gemini HTTP {exc.code}: retrying in {delay:.0f}s)")
                    time.sleep(delay)
                except Exception as exc:  # network errors etc.
                    if attempt == self.MAX_ATTEMPTS:
                        raise EmbeddingError(f"Gemini embedding unavailable ({type(exc).__name__})") from None
                    time.sleep(min(2 ** attempt, 30))
        return out

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, "RETRIEVAL_DOCUMENT")

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], "RETRIEVAL_QUERY")[0]


_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


class LocalHashingEmbeddingProvider:
    """Hashed bag of words + character 3–5-grams (works for Georgian without a tokenizer)."""

    def __init__(self, dim: int = 768):
        self._dim = dim

    @property
    def signature(self) -> str:
        return f"local-hash:{self._dim}"

    @property
    def dim(self) -> int:
        return self._dim

    def _features(self, text: str) -> list[str]:
        features = []
        for token in _TOKEN_RE.findall(text.lower()):
            features.append(f"w:{token}")
            padded = f"<{token}>"
            for n in (3, 4, 5):
                features.extend(f"c:{padded[i:i + n]}" for i in range(max(len(padded) - n + 1, 0)))
        return features

    def _vector(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for feature in self._features(text):
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % self._dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[index] += sign * (2.0 if feature.startswith("w:") else 1.0)
        return _normalise(vec)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


def get_embedding_provider(settings: Settings | None = None,
                           log: Callable[[str], None] | None = None) -> EmbeddingProvider:
    settings = settings or get_settings()
    if settings.embedding_provider == "local":
        return LocalHashingEmbeddingProvider(settings.embedding_dim)
    if settings.gemini_api_key is None:
        raise EmbeddingError("GEMINI_API_KEY is not set (or set EMBEDDING_PROVIDER=local for offline mode).")
    return GeminiEmbeddingProvider(settings.gemini_api_key.get_secret_value(), settings.embedding_model,
                                   settings.embedding_dim, settings.embedding_rpm, log)
