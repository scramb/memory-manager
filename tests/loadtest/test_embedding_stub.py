# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the deterministic embedding stub (#268).

Drives `loadtest.embedding_stub`'s Starlette app through the real
`memory_manager.index.embeddings.OpenAICompatibleProvider` - not a raw HTTP
client - so a pass here means the production request/response path the
real server uses against a real embedding provider also works against this
stub, not just that the stub's own handler happens to agree with itself.
`httpx.ASGITransport` makes the provider's own `httpx.AsyncClient` talk to
the Starlette app in-process (no socket), the same monkeypatch seam
`tests/index/test_embeddings.py` uses for a `MockTransport` handler.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

import httpx
import pytest
from starlette.applications import Starlette

from loadtest.embedding_stub import create_app, load_query_vector_keys
from loadtest.vectors import near_vector, synthetic_vector
from memory_manager.index.embeddings import OpenAICompatibleProvider

_DIMENSION = 1024
_MARKER_QUERY = "What is tagged with the unique marker loadtestmarker000001?"
_VECTOR_KEY = "01ARZ3NDEKTSV4RRFFQ69G5FAV:0"


def _install_asgi_transport(monkeypatch: pytest.MonkeyPatch, app: Starlette) -> None:
    """Make every `httpx.AsyncClient` built inside `embeddings.py` talk to `app`."""
    transport = httpx.ASGITransport(app=app)
    monkeypatch.setattr(
        httpx, "AsyncClient", functools.partial(httpx.AsyncClient, transport=transport)
    )


@pytest.fixture
def queries_path(tmp_path: Path) -> Path:
    """A one-entry `queries.jsonl`, shaped exactly like `loadtest.generate`'s output."""
    entry = {
        "id": "q000001",
        "query": _MARKER_QUERY,
        "expected": ["01ARZ3NDEKTSV4RRFFQ69G5FAV"],
        "namespaces": ["user-00001"],
        "vector_key": _VECTOR_KEY,
    }
    path = tmp_path / "queries.jsonl"
    path.write_text(json.dumps(entry, sort_keys=True) + "\n", encoding="utf-8")
    return path


class TestLoadQueryVectorKeys:
    def test_maps_query_text_to_vector_key(self, queries_path: Path) -> None:
        assert load_query_vector_keys(queries_path) == {_MARKER_QUERY: _VECTOR_KEY}

    def test_ignores_trailing_blank_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "queries.jsonl"
        path.write_text(json.dumps({"query": "q", "vector_key": "k:0"}) + "\n\n", encoding="utf-8")
        assert load_query_vector_keys(path) == {"q": "k:0"}


class TestEmbeddingsEndpoint:
    async def test_marker_query_embeds_to_its_targets_near_vector(
        self, monkeypatch: pytest.MonkeyPatch, queries_path: Path
    ) -> None:
        app = create_app(load_query_vector_keys(queries_path), dimension=_DIMENSION)
        _install_asgi_transport(monkeypatch, app)
        provider = OpenAICompatibleProvider(
            "http://stub/v1", "stub-model", api_key=None, dimensions=_DIMENSION
        )

        result = await provider.embed([_MARKER_QUERY])

        assert result == [near_vector(_VECTOR_KEY, _DIMENSION)]

    async def test_arbitrary_text_embeds_to_its_own_synthetic_vector(
        self, monkeypatch: pytest.MonkeyPatch, queries_path: Path
    ) -> None:
        app = create_app(load_query_vector_keys(queries_path), dimension=_DIMENSION)
        _install_asgi_transport(monkeypatch, app)
        provider = OpenAICompatibleProvider(
            "http://stub/v1", "stub-model", api_key=None, dimensions=_DIMENSION
        )
        text = "a chunk body the generator never planted a marker in"

        result = await provider.embed([text])

        assert result == [synthetic_vector(text, _DIMENSION)]
        assert len(result[0]) == _DIMENSION

    async def test_batches_a_mix_of_marker_and_arbitrary_text_in_one_request(
        self, monkeypatch: pytest.MonkeyPatch, queries_path: Path
    ) -> None:
        app = create_app(load_query_vector_keys(queries_path), dimension=_DIMENSION)
        _install_asgi_transport(monkeypatch, app)
        provider = OpenAICompatibleProvider(
            "http://stub/v1", "stub-model", api_key=None, dimensions=_DIMENSION
        )
        other_text = "another arbitrary chunk of text"

        result = await provider.embed([_MARKER_QUERY, other_text])

        assert result == [
            near_vector(_VECTOR_KEY, _DIMENSION),
            synthetic_vector(other_text, _DIMENSION),
        ]

    async def test_falls_back_to_the_stub_dimension_when_a_request_omits_it(
        self, monkeypatch: pytest.MonkeyPatch, queries_path: Path
    ) -> None:
        stub_dimension = 256
        app = create_app(load_query_vector_keys(queries_path), dimension=stub_dimension)
        _install_asgi_transport(monkeypatch, app)
        # No `dimensions=` - the real server's provider for a model/config
        # without `EmbeddingConfig.dimensions` set never sends the field.
        provider = OpenAICompatibleProvider("http://stub/v1", "stub-model", api_key=None)

        result = await provider.embed(["some query text"])

        assert len(result[0]) == stub_dimension

    async def test_is_deterministic_across_separate_requests(
        self, monkeypatch: pytest.MonkeyPatch, queries_path: Path
    ) -> None:
        app = create_app(load_query_vector_keys(queries_path), dimension=_DIMENSION)
        _install_asgi_transport(monkeypatch, app)
        provider = OpenAICompatibleProvider(
            "http://stub/v1", "stub-model", api_key=None, dimensions=_DIMENSION
        )

        first = await provider.embed(["repeat this text"])
        second = await provider.embed(["repeat this text"])

        assert first == second


class TestHealthz:
    async def test_returns_200(self) -> None:
        app = create_app({}, dimension=_DIMENSION)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://stub") as client:
            response = await client.get("/healthz")

        assert response.status_code == 200
