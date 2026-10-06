# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for pluggable embedding providers and the indexer's use of them (#27)."""

from __future__ import annotations

import functools
import json
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import httpx
import pytest
import pytest_asyncio

from memory_manager.config import EmbeddingConfig
from memory_manager.db.migrate import migrate
from memory_manager.index.embeddings import (
    EmbeddingError,
    OllamaProvider,
    OpenAICompatibleProvider,
    provider_from_config,
)
from memory_manager.index.indexer import Indexer
from memory_manager.vault.note import Note, serialize
from memory_manager.vault.ulid import new_ulid

_CREATED = datetime(2024, 1, 1, tzinfo=UTC)
# Not a real credential - a fake key shaped like one, see tests/vault/test_secrets.py.
_FAKE_API_KEY = "sk-" + "A" * 40


def _install_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    """Make every `httpx.AsyncClient` built inside `embeddings.py` use `handler`.

    Both providers construct their own `httpx.AsyncClient(timeout=...)`
    internally, so the transport is injected by patching the module
    attribute `embeddings.py` calls through, not by adding a constructor
    parameter the production code doesn't have.
    """
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        httpx, "AsyncClient", functools.partial(httpx.AsyncClient, transport=transport)
    )


class TestOllamaProvider:
    async def test_sends_model_and_input(self, monkeypatch: pytest.MonkeyPatch) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"embeddings": [[0.1, 0.2], [0.3, 0.4]]})

        _install_transport(monkeypatch, handler)
        provider = OllamaProvider("http://ollama.local:11434", "bge-m3")

        result = await provider.embed(["a", "b"])

        assert result == [[0.1, 0.2], [0.3, 0.4]]
        assert len(requests) == 1
        assert str(requests[0].url) == "http://ollama.local:11434/api/embed"
        assert json.loads(requests[0].content) == {"model": "bge-m3", "input": ["a", "b"]}

    async def test_batches_according_to_batch_size(self, monkeypatch: pytest.MonkeyPatch) -> None:
        call_sizes: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            call_sizes.append(len(payload["input"]))
            return httpx.Response(200, json={"embeddings": [[0.0]] * len(payload["input"])})

        _install_transport(monkeypatch, handler)
        provider = OllamaProvider("http://ollama.local", "bge-m3", batch_size=32)
        texts = [f"text-{i}" for i in range(70)]

        result = await provider.embed(texts)

        assert len(result) == 70
        assert call_sizes == [32, 32, 6]

    async def test_retries_once_on_503_then_succeeds(self, monkeypatch: pytest.MonkeyPatch) -> None:
        responses = iter(
            [
                httpx.Response(503, text="unavailable"),
                httpx.Response(200, json={"embeddings": [[0.1]]}),
            ]
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return next(responses)

        _install_transport(monkeypatch, handler)
        provider = OllamaProvider("http://ollama.local", "bge-m3")

        result = await provider.embed(["a"])

        assert result == [[0.1]]

    async def test_400_raises_without_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(400, text="bad request")

        _install_transport(monkeypatch, handler)
        provider = OllamaProvider("http://ollama.local", "bge-m3")

        with pytest.raises(EmbeddingError):
            await provider.embed(["a"])
        assert attempts == 1

    async def test_exhausted_retries_raise_embedding_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, text="unavailable")

        _install_transport(monkeypatch, handler)
        provider = OllamaProvider("http://ollama.local", "bge-m3")

        with pytest.raises(EmbeddingError):
            await provider.embed(["a"])

    async def test_dimension_mismatch_inside_a_response_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"embeddings": [[0.1, 0.2], [0.3]]})

        _install_transport(monkeypatch, handler)
        provider = OllamaProvider("http://ollama.local", "bge-m3")

        with pytest.raises(EmbeddingError):
            await provider.embed(["a", "b"])


class TestOpenAICompatibleProvider:
    async def test_sends_bearer_token_and_sorts_results_by_index(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"index": 1, "embedding": [0.2]},
                        {"index": 0, "embedding": [0.1]},
                    ]
                },
            )

        _install_transport(monkeypatch, handler)
        provider = OpenAICompatibleProvider("http://api.local/v1", "text-embed", _FAKE_API_KEY)

        result = await provider.embed(["a", "b"])

        assert result == [[0.1], [0.2]]
        assert requests[0].headers["authorization"] == f"Bearer {_FAKE_API_KEY}"
        assert str(requests[0].url) == "http://api.local/v1/embeddings"

    async def test_without_api_key_sends_no_authorization_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1]}]})

        _install_transport(monkeypatch, handler)
        provider = OpenAICompatibleProvider("http://api.local/v1", "text-embed", None)

        await provider.embed(["a"])

        assert "authorization" not in requests[0].headers

    async def test_api_key_is_absent_from_repr(self) -> None:
        provider = OpenAICompatibleProvider("http://api.local/v1", "text-embed", _FAKE_API_KEY)
        assert _FAKE_API_KEY not in repr(provider)
        assert _FAKE_API_KEY not in str(provider)

    async def test_dimensions_parameter_is_sent_when_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})

        _install_transport(monkeypatch, handler)
        provider = OpenAICompatibleProvider(
            "http://api.local/v1", "text-embed", _FAKE_API_KEY, dimensions=256
        )

        await provider.embed(["a"])

        assert json.loads(requests[0].content)["dimensions"] == 256

    async def test_malformed_response_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [{"index": 0}]})

        _install_transport(monkeypatch, handler)
        provider = OpenAICompatibleProvider("http://api.local/v1", "text-embed", _FAKE_API_KEY)

        with pytest.raises(EmbeddingError):
            await provider.embed(["a"])


class TestProviderFromConfig:
    def test_none_returns_none(self) -> None:
        assert provider_from_config(EmbeddingConfig(provider="none")) is None

    def test_ollama_builds_ollama_provider(self) -> None:
        cfg = EmbeddingConfig(provider="ollama", url="http://ollama.local", model="bge-m3")

        provider = provider_from_config(cfg)

        assert isinstance(provider, OllamaProvider)
        assert provider.model == "bge-m3"

    def test_openai_builds_openai_compatible_provider(self) -> None:
        cfg = EmbeddingConfig(
            provider="openai",
            url="http://api.local/v1",
            model="text-embed",
            api_key=_FAKE_API_KEY,
            dimensions=256,
        )

        provider = provider_from_config(cfg)

        assert isinstance(provider, OpenAICompatibleProvider)
        assert provider.model == "text-embed"


# --- Indexer integration: a deterministic fake provider, no HTTP at all. ---


@dataclass
class _FakeProvider:
    """A deterministic embedding provider for indexer tests - no network."""

    model: str
    dimension: int = 4
    fail: bool = False
    calls: list[list[str]] = field(default_factory=list)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        if self.fail:
            raise EmbeddingError("fake provider configured to fail")
        return [[float(len(text) + i) for i in range(self.dimension)] for text in texts]


def _note(*, body: str = "Some body text.") -> Note:
    return Note(
        id=new_ulid(),
        title="A note",
        description="A description",
        type="fact",
        created=_CREATED,
        updated=_CREATED,
        body=body,
    )


def _write(vault_dir: Path, rel: str, note: Note) -> None:
    path = vault_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(serialize(note))


@pytest.fixture
def vault_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "vault"
    directory.mkdir()
    return directory


@pytest_asyncio.fixture
async def pool(test_database_url: str) -> AsyncIterator[asyncpg.Pool]:
    # A plain connection for the migration, not one from the pool below:
    # `migrate` takes an `asyncpg.Connection`, not a pool's connection proxy.
    migration_conn = await asyncpg.connect(test_database_url)
    try:
        await migrate(migration_conn)
    finally:
        await migration_conn.close()

    created_pool = await asyncpg.create_pool(test_database_url)
    try:
        yield created_pool
    finally:
        await created_pool.close()


class TestIndexerEmbedding:
    async def test_indexing_a_note_stores_embedding_model_and_dimension(
        self, pool: asyncpg.Pool, vault_dir: Path
    ) -> None:
        provider = _FakeProvider(model="fake-v1")
        indexer = Indexer(pool, vault_dir, provider)
        note = _note(body="# Heading\n\nSome text.")
        _write(vault_dir, "personal/fact/a.md", note)

        await indexer.index_paths(["personal/fact/a.md"])

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "select embedding, model, dimension from chunks where note_id = $1", note.id
            )
        assert len(rows) == 1
        assert rows[0]["embedding"] is not None
        assert rows[0]["model"] == "fake-v1"
        assert rows[0]["dimension"] == provider.dimension

    async def test_no_provider_leaves_embedding_null(
        self, pool: asyncpg.Pool, vault_dir: Path
    ) -> None:
        indexer = Indexer(pool, vault_dir)
        note = _note()
        _write(vault_dir, "personal/fact/a.md", note)

        await indexer.index_paths(["personal/fact/a.md"])

        async with pool.acquire() as conn:
            embedding = await conn.fetchval(
                "select embedding from chunks where note_id = $1", note.id
            )
        assert embedding is None

    async def test_failing_provider_leaves_note_indexed_with_embedding_null(
        self, pool: asyncpg.Pool, vault_dir: Path
    ) -> None:
        failing = _FakeProvider(model="fake-v1", fail=True)
        indexer = Indexer(pool, vault_dir, failing)
        note = _note()
        _write(vault_dir, "personal/fact/a.md", note)

        stats = await indexer.index_paths(["personal/fact/a.md"])

        assert stats.indexed == 1
        async with pool.acquire() as conn:
            embedding = await conn.fetchval(
                "select embedding from chunks where note_id = $1", note.id
            )
        assert embedding is None

    async def test_embed_pending_fills_in_a_previously_failed_embedding(
        self, pool: asyncpg.Pool, vault_dir: Path
    ) -> None:
        note = _note()
        _write(vault_dir, "personal/fact/a.md", note)
        failing = _FakeProvider(model="fake-v1", fail=True)
        await Indexer(pool, vault_dir, failing).index_paths(["personal/fact/a.md"])

        working = _FakeProvider(model="fake-v1")
        count = await Indexer(pool, vault_dir, working).embed_pending()

        assert count == 1
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "select embedding, model, dimension from chunks where note_id = $1", note.id
            )
        assert row is not None
        assert row["embedding"] is not None
        assert row["model"] == "fake-v1"

    async def test_embed_pending_reembeds_everything_after_a_model_change(
        self, pool: asyncpg.Pool, vault_dir: Path
    ) -> None:
        note = _note()
        _write(vault_dir, "personal/fact/a.md", note)
        old_provider = _FakeProvider(model="fake-v1")
        await Indexer(pool, vault_dir, old_provider).index_paths(["personal/fact/a.md"])

        new_provider = _FakeProvider(model="fake-v2")
        count = await Indexer(pool, vault_dir, new_provider).embed_pending()

        assert count == 1
        async with pool.acquire() as conn:
            model = await conn.fetchval("select model from chunks where note_id = $1", note.id)
        assert model == "fake-v2"

    async def test_reindex_backfills_embeddings_left_stale_on_unchanged_notes(
        self, pool: asyncpg.Pool, vault_dir: Path
    ) -> None:
        """A note whose file didn't change is never re-embedded by `index_paths`

        alone (it short-circuits on `UNCHANGED`); `reindex`'s trailing
        `embed_pending` call is what catches a chunk stamped with a model
        that is no longer the provider's current one.
        """
        note = _note()
        _write(vault_dir, "personal/fact/a.md", note)
        provider = _FakeProvider(model="fake-v1")
        indexer = Indexer(pool, vault_dir, provider)
        await indexer.index_paths(["personal/fact/a.md"])

        async with pool.acquire() as conn:
            await conn.execute(
                "update chunks set model = 'stale-model' where note_id = $1", note.id
            )

        await indexer.reindex()

        async with pool.acquire() as conn:
            model = await conn.fetchval("select model from chunks where note_id = $1", note.id)
        assert model == "fake-v1"

    async def test_hnsw_index_is_created_for_the_model_and_dimension(
        self, pool: asyncpg.Pool, vault_dir: Path
    ) -> None:
        provider = _FakeProvider(model="fake-v1", dimension=3)
        indexer = Indexer(pool, vault_dir, provider)
        note = _note()
        _write(vault_dir, "personal/fact/a.md", note)

        await indexer.index_paths(["personal/fact/a.md"])

        async with pool.acquire() as conn:
            rows = await conn.fetch("select indexname from pg_indexes where tablename = 'chunks'")
        names = {row["indexname"] for row in rows}
        assert any(name.startswith("chunks_hnsw_") for name in names)
