# SPDX-License-Identifier: AGPL-3.0-only
"""Pluggable embedding providers for the index (#27).

An `EmbeddingProvider` turns a batch of chunk texts into vectors. There are
two HTTP-backed implementations, `OllamaProvider` and
`OpenAICompatibleProvider`, plus `provider_from_config`, which returns
`None` for `EmbeddingConfig(provider="none")` - the "no provider = full-text
only" case the indexer falls back to.

Both providers batch requests to `batch_size` texts, retry up to three
attempts total with exponential backoff on 5xx/429 responses, timeouts and
connection errors, and raise `EmbeddingError` for anything else (4xx other
than 429, a malformed response, or a dimension mismatch within one
response). An `EmbeddingError` is not fatal to indexing: callers store the
affected chunks without an embedding and pick them up again later via
`Indexer.embed_pending`.

API shapes verified against current upstream docs, retrieved 2026-10-06:
- Ollama `POST /api/embed`: request `{"model": ..., "input": [...]}`,
  response `{"embeddings": [[...], ...]}`.
  https://github.com/ollama/ollama/blob/main/docs/api.md
- OpenAI-compatible `POST {base}/embeddings`: request
  `{"model": ..., "input": [...]}`, response
  `{"data": [{"index": ..., "embedding": [...]}, ...]}`, items are sorted
  by `index` before use since providers are not guaranteed to preserve
  request order.
  https://platform.openai.com/docs/api-reference/embeddings
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator, Sequence
from typing import Any, Protocol

import httpx

from memory_manager.config import EmbeddingConfig

__all__ = [
    "EmbeddingError",
    "EmbeddingProvider",
    "OllamaProvider",
    "OpenAICompatibleProvider",
    "provider_from_config",
]

_logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
_RETRY_BASE_DELAY_SECONDS = 0.05
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_DEFAULT_OLLAMA_MODEL = "bge-m3"


class EmbeddingError(Exception):
    """Embedding a batch of texts failed after retries, or the response was invalid."""


class EmbeddingProvider(Protocol):
    """Something that turns chunk texts into embedding vectors."""

    model: str

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed `texts` in request order; raises `EmbeddingError` on failure."""
        ...


class _HttpEmbeddingProvider:
    """Shared batching, retry and dimension validation for the HTTP providers."""

    model: str

    def __init__(self, *, model: str, base_url: str, timeout: float, batch_size: int) -> None:
        self.model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._batch_size = batch_size

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []

        results: list[list[float]] = []
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for batch in _batched(texts, self._batch_size):
                embeddings = await self._embed_batch(client, batch)
                _validate_dimensions(embeddings, self.model)
                results.extend(embeddings)
        return results

    async def _embed_batch(
        self, client: httpx.AsyncClient, batch: Sequence[str]
    ) -> list[list[float]]:
        raise NotImplementedError

    async def _post_with_retry(
        self,
        client: httpx.AsyncClient,
        url: str,
        body: dict[str, Any],
        *,
        headers: dict[str, str] | None = None,
    ) -> Any:
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await client.post(url, json=body, headers=headers)
            except (httpx.TimeoutException, httpx.ConnectError) as exc:
                if attempt >= _MAX_ATTEMPTS:
                    raise EmbeddingError(
                        f"request to {url} failed after {attempt} attempts: {exc}"
                    ) from exc
                await _sleep(attempt)
                continue

            if response.status_code in _RETRYABLE_STATUS and attempt < _MAX_ATTEMPTS:
                await _sleep(attempt)
                continue

            if response.status_code in _RETRYABLE_STATUS or response.status_code >= 400:
                raise EmbeddingError(
                    f"request to {url} failed with status {response.status_code} "
                    f"after {attempt} attempt(s): {response.text[:200]!r}"
                )

            return response.json()


class OllamaProvider(_HttpEmbeddingProvider):
    """Embeds texts through Ollama's `POST /api/embed`."""

    def __init__(
        self, base_url: str, model: str, timeout: float = 30, batch_size: int = 32
    ) -> None:
        super().__init__(model=model, base_url=base_url, timeout=timeout, batch_size=batch_size)

    def __repr__(self) -> str:
        return f"OllamaProvider(base_url={self._base_url!r}, model={self.model!r})"

    async def _embed_batch(
        self, client: httpx.AsyncClient, batch: Sequence[str]
    ) -> list[list[float]]:
        data = await self._post_with_retry(
            client, f"{self._base_url}/api/embed", {"model": self.model, "input": list(batch)}
        )
        embeddings = data.get("embeddings") if isinstance(data, dict) else None
        if not isinstance(embeddings, list):
            raise EmbeddingError(
                f"Ollama response for model {self.model!r} has no 'embeddings' list: {data!r}"
            )
        return embeddings


class OpenAICompatibleProvider(_HttpEmbeddingProvider):
    """Embeds texts through an OpenAI-compatible `POST {base}/embeddings`."""

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None,
        timeout: float = 30,
        batch_size: int = 64,
        dimensions: int | None = None,
    ) -> None:
        super().__init__(model=model, base_url=base_url, timeout=timeout, batch_size=batch_size)
        self._api_key = api_key
        self._dimensions = dimensions

    def __repr__(self) -> str:
        return f"OpenAICompatibleProvider(base_url={self._base_url!r}, model={self.model!r})"

    async def _embed_batch(
        self, client: httpx.AsyncClient, batch: Sequence[str]
    ) -> list[list[float]]:
        body: dict[str, Any] = {"model": self.model, "input": list(batch)}
        if self._dimensions is not None:
            body["dimensions"] = self._dimensions
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else None

        data = await self._post_with_retry(
            client, f"{self._base_url}/embeddings", body, headers=headers
        )
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise EmbeddingError(
                f"OpenAI-compatible response for model {self.model!r} has no 'data' list: {data!r}"
            )
        try:
            ordered = sorted(items, key=lambda item: item["index"])
            return [item["embedding"] for item in ordered]
        except (KeyError, TypeError) as exc:
            raise EmbeddingError(
                f"OpenAI-compatible response for model {self.model!r} is malformed: {data!r}"
            ) from exc


def provider_from_config(cfg: EmbeddingConfig) -> EmbeddingProvider | None:
    """Build the `EmbeddingProvider` named by `cfg`, or `None` for `"none"`."""
    if cfg.provider == "none":
        return None
    if cfg.url is None:
        raise ValueError(f"EmbeddingConfig.url is required for provider {cfg.provider!r}")

    if cfg.provider == "ollama":
        return OllamaProvider(cfg.url, cfg.model or _DEFAULT_OLLAMA_MODEL)
    if cfg.provider == "openai":
        if cfg.model is None:
            raise ValueError("EmbeddingConfig.model is required for provider 'openai'")
        return OpenAICompatibleProvider(cfg.url, cfg.model, cfg.api_key, dimensions=cfg.dimensions)

    raise ValueError(f"unknown embedding provider {cfg.provider!r}")


def _batched(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _validate_dimensions(embeddings: Sequence[Sequence[float]], model: str) -> None:
    if not embeddings:
        return
    dimension = len(embeddings[0])
    for vector in embeddings:
        if len(vector) != dimension:
            raise EmbeddingError(
                f"model {model!r} returned vectors of differing dimension "
                f"({dimension} vs {len(vector)}) within one response"
            )


async def _sleep(attempt: int) -> None:
    await asyncio.sleep(_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)))
