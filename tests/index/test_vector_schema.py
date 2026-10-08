# SPDX-License-Identifier: AGPL-3.0-only
"""Tests for the ADR-0016 `chunks` layout and the `EMBEDDING_DIMENSIONS` pin (#220).

`db.migrate.migrate(conn, backend="postgres")` applies
`migrations/postgres/0012_vector_layout.sql` in addition to the common
chain - `TestDimensionPin` exercises that pin directly; `TestNamespaceKind`
writes through `storage.postgres.PostgresBackend` (the same connection-path
wiring `app.py`'s `"postgres"` branch uses) and checks each chunk lands in
the partition matching its namespace's `namespaces.kind`;
`TestGitModeUnaffected` confirms a bare `migrate(conn)` (`backend="git"`,
the default) keeps the Git backend's own flat, `vector`-typed `chunks` and
`index/indexer.py`'s runtime `ensure_vector_index`, exactly as before this
migration existed.
"""

from __future__ import annotations

import functools
import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import httpx
import pytest
import pytest_asyncio
from storage.contract import note_bytes

from memory_manager.config import EmbeddingDimensionPinError
from memory_manager.db.migrate import POSTGRES_VECTOR_LAYOUT_VERSION, migrate
from memory_manager.index.embeddings import OpenAICompatibleProvider
from memory_manager.index.indexer import Indexer, VaultNotesSource
from memory_manager.storage.postgres import PostgresBackend
from memory_manager.vault.note import Note, serialize
from memory_manager.vault.ulid import new_ulid

_CREATED = datetime(2024, 1, 1, tzinfo=UTC)
_FAKE_API_KEY = "sk-" + "A" * 40


@pytest_asyncio.fixture
async def postgres_pool(test_database_url: str) -> AsyncIterator[asyncpg.Pool]:
    """A `backend="postgres"`-migrated pool - the ADR-0016 `chunks` layout applied."""
    migration_conn = await asyncpg.connect(test_database_url)
    try:
        await migrate(migration_conn, backend="postgres")
    finally:
        await migration_conn.close()

    pool = await asyncpg.create_pool(test_database_url)
    try:
        yield pool
    finally:
        await pool.close()


def _indexed_backend(pool: asyncpg.Pool) -> PostgresBackend:
    """A `PostgresBackend` wired with an `Indexer(pool, VaultNotesSource())` as its
    `index_hook` - the same wiring `app.py`'s `"postgres"` branch gives one
    (`tests/index/test_indexer_postgres_source.py`'s own `backend` fixture): a plain
    `PostgresBackend(pool)` writes `vault_notes` only, never `notes`/`chunks`.
    """
    indexer = Indexer(pool, VaultNotesSource())
    return PostgresBackend(pool, index_hook=indexer.index_on_connection)


async def _insert_namespace(pool: asyncpg.Pool, *, kind: str, alias: str) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "insert into namespaces (kind, external_key, alias) values ($1, $2, $3)",
            kind,
            f"ext-{alias}",
            alias,
        )


async def _chunk_partition(pool: asyncpg.Pool, note_id: str) -> tuple[str, str]:
    """`(tableoid::regclass, namespace_kind)` of `note_id`'s one chunk."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "select tableoid::regclass::text as partition, namespace_kind "
            "from chunks where note_id = $1",
            note_id,
        )
    assert row is not None
    return row["partition"], row["namespace_kind"]


@dataclass
class _FakeProvider:
    """A deterministic embedding provider - no network, fixed dimension."""

    model: str
    dimension: int
    calls: list[list[str]] = field(default_factory=list)

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(i) for i in range(self.dimension)] for _ in texts]


class TestDimensionPin:
    async def test_first_migrate_pins_the_default_when_unset(self, test_database_url: str) -> None:
        conn = await asyncpg.connect(test_database_url)
        try:
            await migrate(conn, backend="postgres")
            pinned = await conn.fetchval("select dimension from embedding_dimension")
        finally:
            await conn.close()

        assert pinned == 1024

    async def test_first_migrate_pins_an_explicit_value(self, test_database_url: str) -> None:
        conn = await asyncpg.connect(test_database_url)
        try:
            await migrate(conn, backend="postgres", embedding_dimensions=256)
            pinned = await conn.fetchval("select dimension from embedding_dimension")
        finally:
            await conn.close()

        assert pinned == 256

    async def test_a_later_matching_value_is_accepted(self, test_database_url: str) -> None:
        conn = await asyncpg.connect(test_database_url)
        try:
            await migrate(conn, backend="postgres", embedding_dimensions=256)
            # No error, and the pin is unchanged.
            await migrate(conn, backend="postgres", embedding_dimensions=256)
            pinned = await conn.fetchval("select dimension from embedding_dimension")
        finally:
            await conn.close()

        assert pinned == 256

    async def test_a_later_mismatching_value_is_refused(self, test_database_url: str) -> None:
        conn = await asyncpg.connect(test_database_url)
        try:
            await migrate(conn, backend="postgres", embedding_dimensions=256)
            with pytest.raises(EmbeddingDimensionPinError):
                await migrate(conn, backend="postgres", embedding_dimensions=1024)
        finally:
            await conn.close()

    async def test_a_provider_returning_another_dimension_is_refused(
        self, postgres_pool: asyncpg.Pool
    ) -> None:
        """Pinned at the default (1024, unset) - a 4-dim provider response is refused
        before it ever reaches `chunks.embedding` (`halfvec(1024)`)."""
        await _insert_namespace(postgres_pool, kind="user", alias="solo")
        backend = _indexed_backend(postgres_pool)
        note_id = new_ulid(_CREATED)
        content = note_bytes(id=note_id, title="Mismatch", body="Some body text.\n")
        written = await backend.write("solo/fact/a.md", content, if_version="new", client="ci")

        provider = _FakeProvider(model="fake-v1", dimension=4)
        indexer = Indexer(postgres_pool, VaultNotesSource(), provider)

        with pytest.raises(EmbeddingDimensionPinError):
            await indexer.embed_note_job(note_id, written.version)


class TestNamespaceKind:
    async def test_chunks_land_in_the_partition_matching_their_namespace_kind(
        self, postgres_pool: asyncpg.Pool
    ) -> None:
        await _insert_namespace(postgres_pool, kind="user", alias="solo")
        await _insert_namespace(postgres_pool, kind="group", alias="eng-team")
        await _insert_namespace(postgres_pool, kind="project", alias="launch")
        await _insert_namespace(postgres_pool, kind="org", alias="everyone")

        backend = _indexed_backend(postgres_pool)
        cases = {
            "solo/fact/a.md": ("user", "chunks_user"),
            "eng-team/fact/a.md": ("group", "chunks_group"),
            "launch/fact/a.md": ("project", "chunks_project"),
            "everyone/fact/a.md": ("org", "chunks_org"),
        }
        note_ids: dict[str, str] = {}
        for path in cases:
            note_id = new_ulid(_CREATED)
            note_ids[path] = note_id
            content = note_bytes(id=note_id, title="Note", body="Some body text.\n")
            await backend.write(path, content, if_version="new", client="ci")

        for path, (kind, partition) in cases.items():
            table, namespace_kind = await _chunk_partition(postgres_pool, note_ids[path])
            assert namespace_kind == kind
            assert table == partition

    async def test_an_alias_with_no_namespaces_row_falls_back_to_org(
        self, postgres_pool: asyncpg.Pool
    ) -> None:
        backend = _indexed_backend(postgres_pool)
        note_id = new_ulid(_CREATED)
        content = note_bytes(id=note_id, title="Unregistered", body="Some body text.\n")
        await backend.write("unregistered/fact/a.md", content, if_version="new", client="ci")

        table, namespace_kind = await _chunk_partition(postgres_pool, note_id)
        assert namespace_kind == "org"
        assert table == "chunks_org"


class TestReindexFullAnalyzes:
    """`reindex(full=True)` on a `VaultNotesSource` runs `ANALYZE chunks` once the
    rebuild is done (ADR-0007 addendum #117, #220's own checklist) - without it,
    `mm_frequent_lexemes` (0008_frequent_lexemes.sql) has no `pg_stats` row to read
    until an unrelated autovacuum happens to run.
    """

    async def test_reindex_full_leaves_chunks_analyzed(self, postgres_pool: asyncpg.Pool) -> None:
        await _insert_namespace(postgres_pool, kind="user", alias="solo")
        backend = _indexed_backend(postgres_pool)
        await backend.write(
            "solo/fact/a.md",
            note_bytes(id=new_ulid(_CREATED), title="A", body="Some body text.\n"),
            if_version="new",
            client="ci",
        )

        await Indexer(postgres_pool, VaultNotesSource()).reindex_full()

        async with postgres_pool.acquire() as conn:
            last_analyze = await conn.fetchval(
                "select last_analyze from pg_stat_user_tables where relname = 'chunks'"
            )
        assert last_analyze is not None


class TestProviderRequestBody:
    """`EMBEDDING_DIMENSIONS` unset never adds a `dimensions` key to the provider
    request (docs/research/vector-index.md §0: checked live against OpenRouter) -
    unrelated to the pin itself, which is a Postgres-mode-only, `chunks`-side
    concern (`TestDimensionPin` above); this is the provider-request contract
    `index/embeddings.py` already keeps, re-asserted here because #220's own
    checklist names it.
    """

    async def test_no_dimensions_key_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]})

        transport = httpx.MockTransport(handler)
        monkeypatch.setattr(
            httpx, "AsyncClient", functools.partial(httpx.AsyncClient, transport=transport)
        )
        provider = OpenAICompatibleProvider("http://api.local/v1", "text-embed", _FAKE_API_KEY)

        await provider.embed(["a"])

        assert "dimensions" not in json.loads(requests[0].content)


class TestGitModeUnaffected:
    async def test_bare_migrate_keeps_the_flat_vector_chunks_table(
        self, test_database_url: str
    ) -> None:
        conn = await asyncpg.connect(test_database_url)
        try:
            applied = await migrate(conn)
            assert POSTGRES_VECTOR_LAYOUT_VERSION not in applied

            column = await conn.fetchrow(
                "select data_type from information_schema.columns "
                "where table_name = 'chunks' and column_name = 'embedding'"
            )
            assert column is not None
            assert column["data_type"] == "USER-DEFINED"  # pgvector's `vector`, not `halfvec`

            has_namespace_kind = await conn.fetchval(
                "select exists(select 1 from information_schema.columns "
                "where table_name = 'chunks' and column_name = 'namespace_kind')"
            )
            assert has_namespace_kind is False
        finally:
            await conn.close()

    async def test_indexing_still_uses_the_runtime_per_model_hnsw_index(
        self, test_database_url: str, tmp_path: Path
    ) -> None:
        migration_conn = await asyncpg.connect(test_database_url)
        try:
            await migrate(migration_conn)
        finally:
            await migration_conn.close()

        vault_dir = tmp_path / "vault"
        vault_dir.mkdir()
        note = Note(
            id=new_ulid(_CREATED),
            title="A note",
            description="A description.",
            type="fact",
            created=_CREATED,
            updated=_CREATED,
            body="Some body text.\n",
        )
        note_path = vault_dir / "personal" / "fact" / "a.md"
        note_path.parent.mkdir(parents=True)
        note_path.write_bytes(serialize(note))

        pool = await asyncpg.create_pool(test_database_url)
        try:
            provider = _FakeProvider(model="fake-v1", dimension=4)
            indexer = Indexer(pool, vault_dir, provider)
            await indexer.index_paths(["personal/fact/a.md"])

            async with pool.acquire() as conn:
                names = {
                    row["relname"]
                    for row in await conn.fetch("select relname from pg_class where relkind = 'i'")
                }
        finally:
            await pool.close()

        assert any(name.startswith("chunks_hnsw_") for name in names)
